"""
Self-Improvement Dashboard — API Server

FastAPI backend serving the public governance explorer dashboard.
Connects to Vector testnet via the governance SDK.

Usage:
    cd Module-6/dashboard
    uvicorn server:app --reload --port 8000
"""

import asyncio
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

# Module root: defaults to parent.parent for local dev, override with GAME6_ROOT for Docker
GAME6_ROOT = Path(os.getenv("GAME6_ROOT", str(Path(__file__).parent.parent)))
sys.path.insert(0, str(GAME6_ROOT))
load_dotenv(GAME6_ROOT / ".env")

DEPLOY_STATE_FILE = GAME6_ROOT / "deploy" / "testnet" / "deployment.json"

# ── Global state ────────────────────────────────────────────────────────────

chain_context = None
ogmios_client = None
gov_indexer = None
deploy_state = None
governance_wallet_address = None
slot_to_posix_offset = 0  # posix_ms = slot * 1000 + offset


async def startup():
    global chain_context, ogmios_client, gov_indexer, deploy_state, governance_wallet_address

    from vector_agent.chain.ogmios import OgmiosClient
    from vector_agent.chain.submit import SubmitClient
    from vector_agent.chain.context import VectorChainContext
    from vector_agent.governance.indexer import GovernanceIndexer

    deploy_state = json.load(open(DEPLOY_STATE_FILE))
    endpoints = deploy_state.get("endpoints", {})
    hashes = deploy_state.get("hashes", {})
    holders = deploy_state.get("holders", {})

    ogmios_url = os.getenv("VECTOR_OGMIOS_URL", endpoints.get("ogmios", ""))
    submit_url = os.getenv("VECTOR_SUBMIT_URL", endpoints.get("submit", ""))

    ogmios_client = OgmiosClient(ogmios_url)
    submit_client = SubmitClient(submit_url) if submit_url else SubmitClient(ogmios_url)
    chain_context = VectorChainContext(ogmios_client, submit_client)

    # Governance wallet address for balance display (optional env var)
    governance_wallet_address = os.getenv("GOVERNANCE_WALLET_ADDRESS", "")

    # Indexer — read-only, needs only script hashes
    proposal_hash = hashes.get("proposal_spend", "")
    critique_hash = hashes.get("critique_spend", "")
    endorsement_hash = hashes.get("endorsement_spend", "")
    treasury_addr = holders.get("treasury", {}).get("address", "")

    gov_indexer = GovernanceIndexer(
        context=chain_context,
        proposal_spend_hash=proposal_hash,
        critique_spend_hash=critique_hash,
        endorsement_spend_hash=endorsement_hash,
        treasury_address=treasury_addr,
    )

    # Compute slot-to-POSIX offset: posix_ms = slot * 1000 + offset
    global slot_to_posix_offset
    import time
    tip = await ogmios_client.query_network_tip()
    current_slot = tip.get("slot", 0)
    current_posix_ms = int(time.time() * 1000)
    slot_to_posix_offset = current_posix_ms - (current_slot * 1000)

    print(f"[OK] Dashboard connected to {ogmios_url}")
    print(f"[OK] Slot-to-POSIX offset: {slot_to_posix_offset} (slot {current_slot})")

    # Prime the cache immediately, then start background refresh
    await _refresh_cache()
    asyncio.create_task(_cache_loop())


def _normalize_timestamp(ts) -> int:
    """Convert a timestamp to POSIX ms. Slot numbers (< 1 trillion) are converted using the genesis offset."""
    if not ts or not isinstance(ts, (int, float)):
        return 0
    ts = int(ts)
    if ts < 1_000_000_000_000:
        # Slot number → convert to POSIX ms
        return ts * 1000 + slot_to_posix_offset
    return ts


_cache_lock = asyncio.Lock()
_cache = {
    "proposals": [],
    "timeline": [],
    "leaderboard": [],
    "treasury": {},
    "stats": {},
}
CACHE_INTERVAL = 30  # seconds


async def _refresh_cache():
    """Rebuild all cached data from chain in one pass."""
    try:
        # 1. Fetch all proposals (single Ogmios query, shared by all views)
        raw = await gov_indexer.get_proposals()
        proposals = [p for p in raw if p.get("has_proposal_token", True)]

        # 2. Enrich proposals with quality signals + critique summaries
        for p in proposals:
            try:
                p["quality_signal"] = await gov_indexer.compute_proposal_quality_signal(p)
            except Exception:
                p["quality_signal"] = 0.0
            ref = p.get("utxo_ref", {})
            try:
                p["critique_summary"] = await gov_indexer.get_quality_signal(ref["tx_hash"], ref.get("output_index", 0))
            except Exception:
                p["critique_summary"] = {}

        # 3. IPFS titles (parallel, cached individually)
        async def _enrich(p):
            uri = p.get("storage_uri", "")
            if isinstance(uri, bytes):
                uri = uri.decode("utf-8", errors="replace")
            meta = await _fetch_ipfs_meta(uri)
            p["ipfs_title"] = meta.get("title", "")
            p["ipfs_summary"] = meta.get("summary", "")

        await asyncio.gather(*[_enrich(p) for p in proposals], return_exceptions=True)
        proposals.sort(key=lambda p: p.get("quality_signal", 0), reverse=True)
        serialized_proposals = [_serialize_proposal(p) for p in proposals]

        # 4. Timeline — reuse proposals, fetch critiques/endorsements
        events = []
        for p in proposals:
            ref = p.get("utxo_ref", {})
            did = p.get("proposer_did", "")
            events.append({
                "type": "proposal",
                "timestamp": _normalize_timestamp(p.get("submitted_at", 0)),
                "agent_did": did.hex() if isinstance(did, bytes) else did,
                "proposal_type": p.get("proposal_type", "Unknown"),
                "state": p.get("state", "Unknown"),
                "stake": (p.get("stake_amount", 0)) / 1_000_000,
                "tx_hash": ref.get("tx_hash", ""),
                "output_index": ref.get("output_index", 0),
            })

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
                        "timestamp": _normalize_timestamp(c.get("submitted_at", 0)),
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
                        "timestamp": _normalize_timestamp(e.get("created_at", e.get("submitted_at", 0))),
                        "agent_did": edid.hex() if isinstance(edid, bytes) else edid,
                        "proposal_tx_hash": tx_hash,
                        "stake": (e.get("stake_amount", 0)) / 1_000_000,
                        "tx_hash": eref.get("tx_hash", ""),
                        "output_index": eref.get("output_index", 0),
                    })
            except Exception:
                pass

        events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)

        # 5. Leaderboard — collect all agent activity from events
        agent_activity = {}
        for ev in events:
            did = ev.get("agent_did", "")
            if not did:
                continue
            if did not in agent_activity:
                agent_activity[did] = {"proposals": 0, "critiques": 0, "endorsements": 0}
            if ev["type"] == "proposal":
                agent_activity[did]["proposals"] += 1
            elif ev["type"] == "critique":
                agent_activity[did]["critiques"] += 1
            elif ev["type"] == "endorsement":
                agent_activity[did]["endorsements"] += 1

        leaderboard = []
        for did, activity in agent_activity.items():
            entry = {
                "agent_did": did,
                "total_proposals": activity["proposals"],
                "critiques": activity["critiques"],
                "endorsements": activity["endorsements"],
                "adopted": 0,
                "rejected": 0,
                "expired": 0,
                "open": 0,
                "adoption_rate": 0.0,
            }
            if activity["proposals"] > 0:
                try:
                    record = await gov_indexer.get_agent_track_record(did)
                    by_state = record.get("by_state", {})
                    adopted = by_state.get("Adopted", 0)
                    entry["adopted"] = adopted
                    entry["rejected"] = by_state.get("Rejected", 0)
                    entry["expired"] = by_state.get("Expired", 0)
                    entry["open"] = by_state.get("Open", 0) + by_state.get("Amended", 0)
                    total = record.get("total_proposals", 0)
                    entry["adoption_rate"] = adopted / total if total > 0 else 0.0
                except Exception:
                    pass
            leaderboard.append(entry)
        leaderboard.sort(key=lambda a: (a["adopted"], a["total_proposals"], a["critiques"], a["endorsements"]), reverse=True)

        # 6. Stats — reuse proposals + events
        by_state = {}
        unique_proposers = set()
        unique_critics = set()
        unique_endorsers = set()
        for p in proposals:
            s = p.get("state", "Unknown")
            by_state[s] = by_state.get(s, 0) + 1
            did = p.get("proposer_did", "")
            if did:
                unique_proposers.add(did if isinstance(did, str) else did.hex())
        for ev in events:
            did = ev.get("agent_did", "")
            if not did:
                continue
            if ev["type"] == "critique":
                unique_critics.add(did)
            elif ev["type"] == "endorsement":
                unique_endorsers.add(did)
        total = len(proposals)
        adopted = by_state.get("Adopted", 0)
        stats = {
            "total_proposals": total,
            "by_state": by_state,
            "adoption_rate": adopted / total if total > 0 else 0.0,
            "unique_proposers": len(unique_proposers),
            "unique_critics": len(unique_critics),
            "unique_endorsers": len(unique_endorsers),
            "unique_agents": len(unique_proposers | unique_critics | unique_endorsers),
            "currently_open": by_state.get("Open", 0) + by_state.get("Amended", 0),
        }

        # 7. Treasury
        balance = await gov_indexer.get_treasury_balance()
        treasury = {
            "total_lovelace": balance.get("total_lovelace", 0),
            "total_apex": balance.get("total_lovelace", 0) / 1_000_000,
            "utxo_count": balance.get("utxo_count", 0),
        }

        # Commit to cache
        async with _cache_lock:
            _cache["proposals"] = serialized_proposals
            _cache["timeline"] = events[:50]
            _cache["leaderboard"] = leaderboard
            _cache["stats"] = stats
            _cache["treasury"] = treasury

        logger.info(f"Cache refreshed: {len(proposals)} proposals")
    except Exception as exc:
        logger.error(f"Cache refresh failed: {exc}")


async def _cache_loop():
    while True:
        await asyncio.sleep(CACHE_INTERVAL)
        await _refresh_cache()


async def shutdown():
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


app = FastAPI(title="Self-Improvement Dashboard", lifespan=lifespan)

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


from fastapi.responses import Response


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def add_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ── IPFS title cache ───────────────────────────────────────────────────────

_ipfs_cache: dict[str, dict] = {}  # CID -> {"title": ..., "summary": ...}

IPFS_GATEWAYS = [
    "https://ipfs.filebase.io/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://dweb.link/ipfs/",
]


async def _fetch_ipfs_meta(storage_uri: str) -> dict:
    """Fetch title and summary from an IPFS document. Returns cached result if available."""
    if not storage_uri or not storage_uri.startswith("ipfs://"):
        return {}
    cid = storage_uri.replace("ipfs://", "")
    if cid in _ipfs_cache:
        return _ipfs_cache[cid]

    import httpx

    for gw in IPFS_GATEWAYS:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(gw + cid)
                if resp.status_code == 200:
                    doc = resp.json()
                    meta = {
                        "title": doc.get("title", ""),
                        "summary": doc.get("summary", ""),
                    }
                    _ipfs_cache[cid] = meta
                    return meta
        except Exception:
            continue

    _ipfs_cache[cid] = {}
    return {}


# ── Read-only Endpoints ────────────────────────────────────────────────────

@app.get("/api/proposals")
async def get_proposals(state: str | None = None, type: str | None = None):
    async with _cache_lock:
        proposals = _cache["proposals"]
    if state:
        proposals = [p for p in proposals if p.get("state") == state]
    if type:
        proposals = [p for p in proposals if p.get("proposal_type") == type]
    return proposals


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
    async with _cache_lock:
        return _cache["treasury"]


@app.get("/api/stats")
async def get_stats():
    async with _cache_lock:
        return _cache["stats"]


@app.get("/api/agent/{did}")
async def get_agent(did: str):
    return await gov_indexer.get_agent_track_record(did)


@app.get("/api/health")
async def health():
    try:
        from pycardano import Address as PycAddr

        tip = await ogmios_client.query_network_tip()

        # Query governance wallet balance (read-only, no skey needed)
        wallet_balance = 0
        if governance_wallet_address:
            try:
                utxos = await chain_context.async_utxos(PycAddr.from_primitive(governance_wallet_address))
                wallet_balance = sum(u.output.amount.coin if hasattr(u.output.amount, 'coin') else u.output.amount for u in utxos)
            except Exception:
                pass

        return {
            "status": "ok",
            "slot": tip.get("slot", 0),
            "wallet_balance_lovelace": wallet_balance,
            "wallet_balance_apex": round(wallet_balance / 1_000_000, 2),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/timeline")
async def get_timeline():
    async with _cache_lock:
        return _cache["timeline"]


@app.get("/api/leaderboard")
async def get_leaderboard():
    async with _cache_lock:
        return _cache["leaderboard"]


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
