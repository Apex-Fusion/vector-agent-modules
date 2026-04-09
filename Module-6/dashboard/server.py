"""
Vector Governance Explorer — API Server

FastAPI backend serving the public governance explorer dashboard.
Connects to Vector testnet via the governance SDK.

Usage:
    cd Module-6/dashboard
    uvicorn server:app --reload --port 8000
"""

import hashlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# File logging
LOG_FILE = Path(__file__).parent / "dashboard.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("dashboard")
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Add SDK and Module-6 root to path
GAME6_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(GAME6_ROOT))
load_dotenv(GAME6_ROOT / ".env")

DEPLOY_STATE_FILE = GAME6_ROOT / "wallets" / "deploy_state.json"
SKEY_PATH = GAME6_ROOT / "wallets" / "payment.skey"

# ── Global state ────────────────────────────────────────────────────────────

agent = None
gov_client = None
gov_indexer = None
deploy_state = None


async def startup():
    global agent, gov_client, gov_indexer, deploy_state

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient
    from vector_agent.governance.indexer import GovernanceIndexer

    deploy_state = json.load(open(DEPLOY_STATE_FILE))
    validators = deploy_state.get("validators", {})
    holders = deploy_state.get("holders", {})

    ogmios_url = deploy_state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = deploy_state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))
    skey_path = str(SKEY_PATH.absolute()) if SKEY_PATH.exists() else None

    agent = VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    )
    await agent.__aenter__()

    proposal_cbor = validators.get("proposal.proposal_spend.spend", {}).get("compiled_code", "")
    proposal_mint_cbor = validators.get("proposal.proposal_mint.mint", {}).get("compiled_code", "")
    critique_cbor = validators.get("critique.critique_spend.spend", {}).get("compiled_code", "")
    endorsement_cbor = validators.get("critique.endorsement_spend.spend", {}).get("compiled_code", "")

    gov_client = GovernanceClient(
        agent,
        proposal_script_cbor=proposal_cbor,
        proposal_mint_cbor=proposal_mint_cbor,
        critique_script_cbor=critique_cbor,
        endorsement_script_cbor=endorsement_cbor,
    )

    # Configure reference inputs
    ref_inputs = []
    tx_hashes = deploy_state.get("tx_hashes", {})
    refs_policy = deploy_state.get("refs_token_policy", "")

    from pycardano import Address as PycAddr

    for holder_name, utxo_key in [("params", "params_utxo"), ("oracle", "oracle_utxo")]:
        addr = holders.get(holder_name, {}).get("address", "")
        expected_tx = tx_hashes.get(utxo_key, "")
        if addr and expected_tx:
            try:
                utxos = await agent.context.async_utxos(PycAddr.from_primitive(addr))
                for u in utxos:
                    if u.output.datum is not None:
                        tx_hash = str(u.input.transaction_id)
                        if tx_hash == expected_tx:
                            ref_inputs.append({"tx_hash": tx_hash, "output_index": u.input.index, "address": addr})
                            break
            except Exception:
                pass

    # CrossRefs NFT
    oracle_addr = holders.get("oracle", {}).get("address", "")
    if oracle_addr:
        try:
            utxos = await agent.context.async_utxos(PycAddr.from_primitive(oracle_addr))
            for u in utxos:
                if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                    for pid in u.output.amount.multi_asset:
                        if pid.payload.hex() == refs_policy:
                            ref_inputs.append({
                                "tx_hash": str(u.input.transaction_id),
                                "output_index": u.input.index,
                                "address": oracle_addr,
                            })
                            break
        except Exception:
            pass

    if ref_inputs:
        gov_client.set_governance_reference_inputs(ref_inputs)

    # Reference script for CIP-33
    proposal_spend_ref_tx = tx_hashes.get("proposal_spend_ref", "")
    if proposal_spend_ref_tx:
        wallet = json.load(open(GAME6_ROOT / "wallets" / "governance_wallet.json"))
        gov_client.set_reference_utxos({
            "proposal": {"tx_hash": proposal_spend_ref_tx, "output_index": 0, "address": wallet["address"]}
        })

    # Indexer
    proposal_hash = validators.get("proposal.proposal_spend.spend", {}).get("hash", "")
    critique_hash = validators.get("critique.critique_spend.spend", {}).get("hash", "")
    endorsement_hash = validators.get("critique.endorsement_spend.spend", {}).get("hash", "")
    treasury_addr = holders.get("treasury", {}).get("address", "")

    gov_indexer = GovernanceIndexer(
        context=agent.context,
        proposal_spend_hash=proposal_hash,
        critique_spend_hash=critique_hash,
        endorsement_spend_hash=endorsement_hash,
        treasury_address=treasury_addr,
    )

    print(f"[OK] Dashboard connected to {ogmios_url}")
    print(f"[OK] {len(ref_inputs)} reference inputs configured")


async def shutdown():
    global agent
    if agent:
        await agent.__aexit__(None, None, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


app = FastAPI(title="Vector Governance Explorer", lifespan=lifespan)

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Read-only Endpoints ────────────────────────────────────────────────────

@app.get("/api/proposals")
async def get_proposals(state: str | None = None, type: str | None = None):
    proposals = await gov_indexer.get_proposals(state=state, proposal_type=type)

    # Filter out orphaned UTxOs (lock-only, no proposal token)
    proposals = [p for p in proposals if p.get("has_proposal_token", True)]

    # Compute quality signal for each
    for p in proposals:
        try:
            p["quality_signal"] = await gov_indexer.compute_proposal_quality_signal(p)
        except Exception:
            p["quality_signal"] = 0.0

        # Get critique/endorsement counts
        ref = p.get("utxo_ref", {})
        try:
            signal = await gov_indexer.get_quality_signal(ref["tx_hash"], ref.get("output_index", 0))
            p["critique_summary"] = signal
        except Exception:
            p["critique_summary"] = {}

    proposals.sort(key=lambda p: p.get("quality_signal", 0), reverse=True)

    # Serialize bytes for JSON
    return [_serialize_proposal(p) for p in proposals]


@app.get("/api/proposals/{tx_hash}/{output_index}")
async def get_proposal_detail(tx_hash: str, output_index: int):
    proposals = await gov_indexer.get_proposals()

    target = None
    for p in proposals:
        ref = p.get("utxo_ref", {})
        if ref.get("tx_hash") == tx_hash and ref.get("output_index") == output_index:
            target = p
            break

    if not target or not target.get("has_proposal_token", True):
        raise HTTPException(status_code=404, detail="Proposal not found")

    # Quality signal
    target["quality_signal"] = await gov_indexer.compute_proposal_quality_signal(target)

    # Critiques
    critiques = await gov_indexer.get_critiques(tx_hash, output_index)
    for c in critiques:
        try:
            c["quality"] = gov_indexer.compute_critique_quality(c, target, critiques)
        except Exception:
            c["quality"] = {}

    # Endorsements
    endorsements = await gov_indexer.get_endorsements(tx_hash, output_index)

    # Proposer track record
    proposer_did = target.get("proposer_did", "")
    track_record = await gov_indexer.get_agent_track_record(proposer_did) if proposer_did else {}

    return {
        "proposal": _serialize_proposal(target),
        "critiques": [_serialize_dict(c) for c in critiques],
        "endorsements": [_serialize_dict(e) for e in endorsements],
        "track_record": track_record,
    }


@app.get("/api/ipfs/{cid}")
async def fetch_ipfs_document(cid: str, expected_hash: str | None = None):
    """Fetch a document from IPFS gateways and optionally verify its blake2b_256 hash."""
    import httpx

    GATEWAYS = [
        f"https://ipfs.filebase.io/ipfs/{cid}",
        f"https://ipfs.io/ipfs/{cid}",
        f"https://cloudflare-ipfs.com/ipfs/{cid}",
        f"https://dweb.link/ipfs/{cid}",
    ]

    last_error = None
    for gateway_url in GATEWAYS:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(gateway_url)
                if resp.status_code == 200:
                    raw_bytes = resp.content
                    try:
                        content = resp.json()
                    except Exception:
                        content = resp.text

                    # Compute blake2b_256 of raw response bytes
                    computed_hash = hashlib.blake2b(raw_bytes, digest_size=32).hexdigest()

                    verified = None
                    if expected_hash:
                        verified = computed_hash == expected_hash
                        # Fallback: try canonical JSON re-serialization
                        if not verified and isinstance(content, (dict, list)):
                            canonical = json.dumps(content, separators=(",", ":"), sort_keys=False)
                            canonical_hash = hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()
                            if canonical_hash == expected_hash:
                                verified = True
                                computed_hash = canonical_hash

                    return {
                        "content": content,
                        "cid": cid,
                        "computed_hash": computed_hash,
                        "expected_hash": expected_hash,
                        "verified": verified,
                        "gateway": gateway_url,
                    }
        except Exception as e:
            last_error = str(e)
            continue

    raise HTTPException(status_code=502, detail=f"Failed to fetch from all IPFS gateways. Last error: {last_error}")


@app.get("/api/treasury")
async def get_treasury():
    balance = await gov_indexer.get_treasury_balance()
    return {
        "total_lovelace": balance.get("total_lovelace", 0),
        "total_apex": balance.get("total_lovelace", 0) / 1_000_000,
        "utxo_count": balance.get("utxo_count", 0),
    }


@app.get("/api/stats")
async def get_stats():
    all_proposals = await gov_indexer.get_proposals()
    all_proposals = [p for p in all_proposals if p.get("has_proposal_token", True)]

    by_state = {}
    unique_proposers = set()
    total_rewards = 0
    for p in all_proposals:
        s = p.get("state", "Unknown")
        by_state[s] = by_state.get(s, 0) + 1
        did = p.get("proposer_did", "")
        if did:
            unique_proposers.add(did if isinstance(did, str) else did.hex())

    total = len(all_proposals)
    adopted = by_state.get("Adopted", 0)

    return {
        "total_proposals": total,
        "by_state": by_state,
        "adoption_rate": adopted / total if total > 0 else 0.0,
        "unique_proposers": len(unique_proposers),
        "currently_open": by_state.get("Open", 0) + by_state.get("Amended", 0),
    }


@app.get("/api/agent/{did}")
async def get_agent(did: str):
    return await gov_indexer.get_agent_track_record(did)


@app.get("/api/health")
async def health():
    try:
        tip = await agent.context._ogmios.query_network_tip()
        balance = await agent.get_balance()
        return {
            "status": "ok",
            "slot": tip.get("slot", 0),
            "wallet_balance_lovelace": balance.lovelace,
            "wallet_balance_apex": balance.ada,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/timeline")
async def get_timeline(limit: int = 50):
    """Chronological feed of all governance events."""
    events = []

    proposals = await gov_indexer.get_proposals()
    proposals = [p for p in proposals if p.get("has_proposal_token", True)]
    for p in proposals:
        ref = p.get("utxo_ref", {})
        did = p.get("proposer_did", "")
        events.append({
            "type": "proposal",
            "timestamp": p.get("submitted_at", 0),
            "agent_did": did.hex() if isinstance(did, bytes) else did,
            "proposal_type": p.get("proposal_type", "Unknown"),
            "state": p.get("state", "Unknown"),
            "stake": (p.get("stake_amount", 0)) / 1_000_000,
            "tx_hash": ref.get("tx_hash", ""),
            "output_index": ref.get("output_index", 0),
        })

    # Gather critiques and endorsements for all proposals
    for p in proposals:
        ref = p.get("utxo_ref", {})
        tx_hash = ref.get("tx_hash", "")
        output_index = ref.get("output_index", 0)

        try:
            critiques = await gov_indexer.get_critiques(tx_hash, output_index)
            for c in critiques:
                cref = c.get("utxo_ref", {})
                cdid = c.get("critic_did", "")
                events.append({
                    "type": "critique",
                    "timestamp": c.get("submitted_at", 0),
                    "agent_did": cdid.hex() if isinstance(cdid, bytes) else cdid,
                    "critique_type": c.get("critique_type", "Unknown"),
                    "proposal_tx_hash": tx_hash,
                    "stake": (c.get("stake_amount", 0)) / 1_000_000,
                    "tx_hash": cref.get("tx_hash", ""),
                    "output_index": cref.get("output_index", 0),
                })
        except Exception:
            pass

        try:
            endorsements = await gov_indexer.get_endorsements(tx_hash, output_index)
            for e in endorsements:
                eref = e.get("utxo_ref", {})
                edid = e.get("endorser_did", "")
                events.append({
                    "type": "endorsement",
                    "timestamp": e.get("created_at", e.get("submitted_at", 0)),
                    "agent_did": edid.hex() if isinstance(edid, bytes) else edid,
                    "proposal_tx_hash": tx_hash,
                    "stake": (e.get("stake_amount", 0)) / 1_000_000,
                    "tx_hash": eref.get("tx_hash", ""),
                    "output_index": eref.get("output_index", 0),
                })
        except Exception:
            pass

    events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return events[:limit]


@app.get("/api/leaderboard")
async def get_leaderboard():
    """Agent leaderboard ranked by governance participation."""
    all_proposals = await gov_indexer.get_proposals()
    all_proposals = [p for p in all_proposals if p.get("has_proposal_token", True)]

    # Collect unique agent DIDs (proposers + critics)
    agent_dids = set()
    for p in all_proposals:
        did = p.get("proposer_did", "")
        if did:
            agent_dids.add(did.hex() if isinstance(did, bytes) else did)

    leaderboard = []
    for did in agent_dids:
        try:
            record = await gov_indexer.get_agent_track_record(did)
            by_state = record.get("by_state", {})
            total = record.get("total_proposals", 0)
            adopted = by_state.get("Adopted", 0)
            leaderboard.append({
                "agent_did": did,
                "total_proposals": total,
                "adopted": adopted,
                "rejected": by_state.get("Rejected", 0),
                "expired": by_state.get("Expired", 0),
                "open": by_state.get("Open", 0) + by_state.get("Amended", 0),
                "adoption_rate": adopted / total if total > 0 else 0.0,
            })
        except Exception:
            pass

    leaderboard.sort(key=lambda a: (a["adopted"], a["total_proposals"]), reverse=True)
    return leaderboard


# ── Helpers ─────────────────────────────────────────────────────────────────


def _serialize_proposal(p: dict) -> dict:
    """Convert bytes fields to hex strings for JSON serialization."""
    out = {}
    for k, v in p.items():
        if isinstance(v, bytes):
            out[k] = v.hex()
        elif isinstance(v, dict):
            out[k] = _serialize_dict(v)
        else:
            out[k] = v
    return out


def _serialize_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, bytes):
            out[k] = v.hex()
        elif isinstance(v, dict):
            out[k] = _serialize_dict(v)
        elif isinstance(v, list):
            out[k] = [_serialize_dict(i) if isinstance(i, dict) else (i.hex() if isinstance(i, bytes) else i) for i in v]
        else:
            out[k] = v
    return out
