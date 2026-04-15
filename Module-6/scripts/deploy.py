"""
Module 6: Full Testnet Deployment Script

Handles the complete deployment flow:
  1. Apply GovernanceConfig parameter to all validators via `aiken blueprint apply`
  2. Deploy holder reference scripts (params/oracle/treasury)
  3. Deploy Module-6 validator reference scripts (CIP-33)
  4. Create infrastructure UTxOs (GovernanceParams, Oracle, Treasury)
  5. Mint refs NFT and deploy GovernanceCrossRefs datum
  6. Save full deployment state

Usage:
    nix-shell shell.nix --run "python scripts/deploy.py"

Prerequisites:
    1. Wallet funded (run setup_wallet.py first)
    2. Aiken contracts compiled (aiken build in contracts/governance-suggestion)
    3. Holder scripts compiled (aiken build in shared/holder-scripts)
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import cbor2
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────

GAME6_ROOT = Path(__file__).parent.parent
REPO_ROOT = GAME6_ROOT.parent
CONTRACT_DIR = GAME6_ROOT / "contracts" / "governance-suggestion"
HOLDER_DIR = REPO_ROOT / "shared" / "holder-scripts"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
HOLDER_BLUEPRINT_PATH = HOLDER_DIR / "plutus.json"
WALLET_FILE = GAME6_ROOT / "wallets" / "governance_wallet.json"
SKEY_PATH = GAME6_ROOT / "wallets" / "payment.skey"
DEPLOY_STATE_FILE = GAME6_ROOT / "wallets" / "deploy_state.json"

# ── Network config ───────────────────────────────────────────────────────────

OGMIOS_URL = os.getenv("VECTOR_OGMIOS_URL", "https://ogmios.vector.testnet.apexfusion.org")
SUBMIT_URL = os.getenv("VECTOR_SUBMIT_URL", "https://submit.vector.testnet.apexfusion.org/api/submit/tx")
EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")

# ── Constants ────────────────────────────────────────────────────────────────

AGENT_REGISTRY_HASH = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
ZEROED_28 = "00" * 28


def load_wallet() -> dict:
    if not WALLET_FILE.exists():
        raise FileNotFoundError("Wallet not found. Run: python scripts/setup_wallet.py")
    with open(WALLET_FILE) as f:
        return json.load(f)


def load_blueprint(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def get_validator(blueprint: dict, title_prefix: str) -> dict:
    for v in blueprint["validators"]:
        if v["title"].startswith(title_prefix):
            return {"title": v["title"], "compiled_code": v["compiledCode"], "hash": v["hash"]}
    raise ValueError(f"Validator not found: {title_prefix}")


# ── NativeScript policy ─────────────────────────────────────────────────────

def compute_native_script_policy(vkey_hash: str) -> tuple[str, bytes]:
    """Compute NativeScript policy ID for ScriptPubkey(vkey_hash)."""
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash)])
    script_bytes = b"\x00" + native_script_cbor
    policy_id = hashlib.blake2b(script_bytes, digest_size=28).hexdigest()
    return policy_id, native_script_cbor


# ── GovernanceConfig CBOR ────────────────────────────────────────────────────

def build_governance_config_cbor(
    refs_token_policy: str,
    oracle_holder_hash: str,
    params_holder_hash: str,
    treasury_holder_hash: str,
) -> str:
    """Build GovernanceConfig as Plutus Data CBOR (hex).

    Constructor 0 with 11 fields (all ByteArray).
    """
    fields = [
        bytes.fromhex(refs_token_policy),       # refs_token_policy
        bytes.fromhex(ZEROED_28),               # prediction_validator_hash
        bytes.fromhex(AGENT_REGISTRY_HASH),     # registry_policy_id
        bytes.fromhex(AGENT_REGISTRY_HASH),     # registry_script_hash
        bytes.fromhex(ZEROED_28),               # reputation_validator_hash (Module-3 not deployed)
        bytes.fromhex(ZEROED_28),               # jury_pool_hash (Phase 1.2)
        bytes.fromhex(oracle_holder_hash),      # governance_oracle_hash
        bytes.fromhex(params_holder_hash),      # governance_params_hash
        bytes.fromhex(treasury_holder_hash),    # governance_treasury_hash
        bytes.fromhex(ZEROED_28),               # credibility_pool_hash (Phase 1.2)
        bytes.fromhex(ZEROED_28),               # protocol_params_hash
    ]
    # Plutus Data constructor: Tag 121 + 0 = constr(0, fields)
    config_data = cbor2.CBORTag(121, fields)
    return cbor2.dumps(config_data).hex()


# ── Apply parameters ────────────────────────────────────────────────────────

def apply_holder_tag(holder_blueprint: Path, tag: int) -> dict:
    """Apply an integer tag to the holder validator, return hash + compiled code."""
    tag_cbor = cbor2.dumps(tag).hex()
    result = subprocess.run(
        ["aiken", "blueprint", "apply", "-i", str(holder_blueprint),
         "-m", "holder", "-v", "holder", tag_cbor],
        capture_output=True, text=True, check=True,
    )
    applied = json.loads(result.stdout)
    v = applied["validators"][0]
    return {"hash": v["hash"], "compiled_code": v["compiledCode"]}


def apply_governance_config(config_cbor_hex: str) -> dict:
    """Apply GovernanceConfig to all Module-6 validators, return applied blueprint."""
    # Start from the raw blueprint
    current = str(BLUEPRINT_PATH)
    validators_to_apply = [
        ("proposal", "proposal_mint"),
        ("proposal", "proposal_spend"),
        ("critique", "critique_mint"),
        ("critique", "critique_spend"),
        ("critique", "endorsement_spend"),
    ]

    applied_bp = None
    for i, (module, validator) in enumerate(validators_to_apply):
        is_last = i == len(validators_to_apply) - 1
        cmd = [
            "aiken", "blueprint", "apply",
            "-i", current,
            "-m", module, "-v", validator,
            config_cbor_hex,
        ]
        if not is_last:
            # Write intermediate to temp file
            out_path = str(CONTRACT_DIR / f"plutus-applied-{i}.json")
            cmd.extend(["-o", out_path])
            subprocess.run(cmd, check=True)
            current = out_path
        else:
            # Last one — capture stdout
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            applied_bp = json.loads(result.stdout)
            current = None

    # Clean up intermediate files
    for i in range(len(validators_to_apply) - 1):
        f = CONTRACT_DIR / f"plutus-applied-{i}.json"
        if f.exists():
            f.unlink()

    return applied_bp


# ── Bech32 address computation ───────────────────────────────────────────────

def script_hash_to_testnet_address(script_hash_hex: str) -> str:
    """Convert a script hash to a Bech32 address (Vector uses mainnet flag)."""
    from pycardano import Address, Network, ScriptHash
    script_hash = ScriptHash(bytes.fromhex(script_hash_hex))
    addr = Address(payment_part=script_hash, network=Network.MAINNET)
    return str(addr)


# ── Deployment transactions ─────────────────────────────────────────────────

async def deploy_reference_script(agent, script_cbor_hex: str, label: str) -> str:
    """Deploy a validator as a reference script, return tx hash."""
    from pycardano import TransactionBuilder, TransactionOutput, PlutusV3Script

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2

    # Min UTXO for reference scripts: ~4400 lovelace per byte + 2 AP3X base
    # (Conway era charges heavily for inline scripts)
    min_lovelace = 2_000_000 + script_size * 4400
    # Round up to nearest AP3X + 2 AP3X safety margin
    min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)

    builder.add_output(
        TransactionOutput(
            agent._wallet.payment_address,
            min_lovelace,
            script=script,
        )
    )

    # pycardano underestimates fees for transactions with reference scripts
    # because it doesn't account for the script bytes in the fee calculation.
    # Add a fee buffer proportional to script size.
    builder.fee_buffer = max(300_000, script_size * 50)

    tx = builder.build_and_sign(
        signing_keys=[agent._wallet.payment_signing_key],
        change_address=agent._wallet.payment_address,
    )

    tx_cbor = tx.to_cbor()
    if isinstance(tx_cbor, bytes):
        tx_cbor_hex = tx_cbor.hex()
    else:
        tx_cbor_hex = tx_cbor

    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor_hex)
    print(f"  [{label}] Ref script deployed: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def create_datum_utxo(agent, address, datum_data, lovelace: int, label: str) -> str:
    """Create a UTXO at a script address with an inline datum."""
    from pycardano import TransactionBuilder, TransactionOutput, Address
    from pycardano.plutus import RawPlutusData

    if isinstance(address, str):
        address = Address.from_primitive(address)

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000  # pycardano fee underestimate workaround

    builder.add_output(
        TransactionOutput(
            address,
            lovelace,
            datum=RawPlutusData(datum_data),
        )
    )

    tx = builder.build_and_sign(
        signing_keys=[agent._wallet.payment_signing_key],
        change_address=agent._wallet.payment_address,
    )

    tx_cbor = tx.to_cbor()
    if isinstance(tx_cbor, bytes):
        tx_cbor = tx_cbor.hex()

    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor)
    print(f"  [{label}] Datum UTXO created: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def mint_refs_nft(agent, native_script_cbor: bytes, refs_datum_data, label: str, target_address: str = None) -> str:
    """Mint a refs NFT and create the CrossRefs UTXO with inline datum.

    If target_address is provided, the NFT is sent there (e.g. oracle holder).
    Otherwise it goes to the wallet address.
    """
    from pycardano import (
        TransactionBuilder, TransactionOutput, Asset, AssetName,
        MultiAsset, NativeScript, ScriptPubkey, Value, Address,
    )
    from pycardano.plutus import RawPlutusData

    # Build the NativeScript from raw CBOR
    native_script = NativeScript.from_primitive(cbor2.loads(native_script_cbor))

    policy_id = native_script.hash()
    asset_name = AssetName(b"governance_refs")

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000  # pycardano fee underestimate workaround

    # Mint the refs NFT
    builder.native_scripts = [native_script]
    builder.mint = MultiAsset({policy_id: Asset({asset_name: 1})})

    # Output: NFT + inline CrossRefs datum at target address
    # (defaults to wallet, but oracle holder is preferred to avoid coin selection)
    output_addr = Address.from_primitive(target_address) if target_address else agent._wallet.payment_address
    builder.add_output(
        TransactionOutput(
            output_addr,
            Value(3_000_000, MultiAsset({policy_id: Asset({asset_name: 1})})),
            datum=RawPlutusData(refs_datum_data),
        )
    )

    tx = builder.build_and_sign(
        signing_keys=[agent._wallet.payment_signing_key],
        change_address=agent._wallet.payment_address,
    )

    tx_cbor = tx.to_cbor()
    if isinstance(tx_cbor, bytes):
        tx_cbor = tx_cbor.hex()

    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor)
    print(f"  [{label}] Refs NFT minted: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


# ── Main deployment flow ─────────────────────────────────────────────────────

async def deploy():
    print("=" * 60)
    print("Module 6: Governance Suggestion Engine — Full Deployment")
    print("=" * 60)

    # ── Step 0: Load wallet and blueprints ────────────────────────────────

    wallet = load_wallet()
    print(f"\nWallet: {wallet['address']}")
    print(f"VKey Hash: {wallet['vkey_hash']}")

    if not BLUEPRINT_PATH.exists():
        print("ERROR: Module-6 blueprint not found. Run 'aiken build' in contracts/governance-suggestion/")
        return
    if not HOLDER_BLUEPRINT_PATH.exists():
        print("ERROR: Holder blueprint not found. Run 'aiken build' in shared/holder-scripts/")
        return

    blueprint = load_blueprint(BLUEPRINT_PATH)
    print(f"Blueprint: {blueprint['preamble']['title']} v{blueprint['preamble']['version']}")

    # ── Step 1: Compute holder hashes ─────────────────────────────────────

    print("\n--- Step 1: Holder Script Hashes ---")
    holders = {}
    for tag, name in [(1, "params"), (2, "oracle"), (3, "treasury")]:
        info = apply_holder_tag(HOLDER_BLUEPRINT_PATH, tag)
        holders[name] = info
        addr = script_hash_to_testnet_address(info["hash"])
        print(f"  {name}: hash={info['hash']}")
        print(f"         addr={addr}")

    # ── Step 2: Compute NativeScript policy ──────────────────────────────

    print("\n--- Step 2: NativeScript (refs_token_policy) ---")
    refs_policy_id, native_script_cbor = compute_native_script_policy(wallet["vkey_hash"])
    print(f"  Policy ID: {refs_policy_id}")

    # ── Step 3: Build GovernanceConfig and apply to validators ────────────

    print("\n--- Step 3: Apply GovernanceConfig ---")
    config_cbor = build_governance_config_cbor(
        refs_token_policy=refs_policy_id,
        oracle_holder_hash=holders["oracle"]["hash"],
        params_holder_hash=holders["params"]["hash"],
        treasury_holder_hash=holders["treasury"]["hash"],
    )
    print(f"  Config CBOR: {len(config_cbor) // 2} bytes")

    applied_bp = apply_governance_config(config_cbor)

    applied_validators = {}
    print("\n  Applied validator hashes:")
    for v in applied_bp["validators"]:
        title = v["title"]
        applied_validators[title] = {
            "hash": v["hash"],
            "compiled_code": v["compiledCode"],
        }
        print(f"    {title}: {v['hash']}")

    # Extract the key hashes (proposal mint/spend share hash, critique mint/spend share hash)
    proposal_hash = applied_validators["proposal.proposal_spend.spend"]["hash"]
    critique_hash = applied_validators["critique.critique_spend.spend"]["hash"]
    print(f"\n  Proposal validator hash: {proposal_hash}")
    print(f"  Critique validator hash: {critique_hash}")

    # ── Step 4: Deploy to testnet ────────────────────────────────────────

    print("\n--- Step 4: Deploy to Testnet ---")

    from vector_agent import VectorAgent

    async with VectorAgent(
        ogmios_url=OGMIOS_URL,
        submit_url=SUBMIT_URL,
        skey_path=str(SKEY_PATH.absolute()),
    ) as agent:
        balance = await agent.get_balance()
        print(f"  Balance: {balance.ada} AP3X ({balance.lovelace} lovelace)")

        min_required = 150_000_000  # ~150 AP3X for all deployments
        if balance.lovelace < min_required:
            print(f"  ERROR: Need at least {min_required / 1_000_000} AP3X. Fund wallet first.")
            save_deploy_state_offline(wallet, holders, refs_policy_id, applied_validators,
                                      proposal_hash, critique_hash)
            return

        # Resume support: load existing tx_hashes from prior runs
        existing_state = {}
        if DEPLOY_STATE_FILE.exists():
            with open(DEPLOY_STATE_FILE) as f:
                existing_state = json.load(f)
        tx_hashes = existing_state.get("tx_hashes", {})
        TX_WAIT = 20  # seconds between transactions for UTxO confirmation

        # 4a: Deploy holder reference scripts
        print("\n  4a. Deploying holder reference scripts...")
        for name in ["params", "oracle", "treasury"]:
            key = f"holder_{name}_ref"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
                continue
            tx = await deploy_reference_script(agent, holders[name]["compiled_code"], f"holder-{name}")
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 4b: Deploy Module-6 validator reference scripts
        print("\n  4b. Deploying Module-6 validator reference scripts...")
        ref_script_keys = [
            ("proposal.proposal_mint.mint", "proposal_mint"),
            ("proposal.proposal_spend.spend", "proposal_spend"),
            ("critique.critique_mint.mint", "critique_mint"),
            ("critique.critique_spend.spend", "critique_spend"),
            ("critique.endorsement_spend.spend", "endorsement_spend"),
        ]
        for title, label in ref_script_keys:
            key = f"{label}_ref"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
                continue
            tx = await deploy_reference_script(agent, applied_validators[title]["compiled_code"], label)
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 4c: Create GovernanceParams UTXO at params holder address
        if "params_utxo" not in tx_hashes:
            print("\n  4c. Creating GovernanceParams UTXO...")
            from vector_agent.governance.datums import build_governance_params
            params_datum = build_governance_params()
            params_addr = script_hash_to_testnet_address(holders["params"]["hash"])
            tx = await create_datum_utxo(
                agent, params_addr, params_datum.data, 3_000_000, "params"
            )
            tx_hashes["params_utxo"] = tx
            await asyncio.sleep(TX_WAIT)
        else:
            print(f"\n  4c. GovernanceParams already deployed (skipping)")

        # 4d: Create Oracle UTXO at oracle holder address
        if "oracle_utxo" not in tx_hashes:
            print("\n  4d. Creating Oracle UTXO...")
            from vector_agent.governance.datums import build_oracle_datum
            oracle_datum = build_oracle_datum(
                oracle_vkey_hash=bytes.fromhex(wallet["vkey_hash"]),
                treasury_script_hash=bytes.fromhex(holders["treasury"]["hash"]),
            )
            oracle_addr = script_hash_to_testnet_address(holders["oracle"]["hash"])
            tx = await create_datum_utxo(
                agent, oracle_addr, oracle_datum.data, 3_000_000, "oracle"
            )
            tx_hashes["oracle_utxo"] = tx
            await asyncio.sleep(TX_WAIT)
        else:
            print(f"\n  4d. Oracle already deployed (skipping)")

        # 4e: Create Treasury batch UTxOs at treasury holder address
        print("\n  4e. Creating Treasury batch UTxOs...")
        from vector_agent.governance.datums import build_treasury_batch_datum
        treasury_addr = script_hash_to_testnet_address(holders["treasury"]["hash"])
        for batch_id in range(1, 4):  # 3 batches
            key = f"treasury_batch_{batch_id}"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed (skipping)")
                continue
            batch_datum = build_treasury_batch_datum(batch_id, True)
            batch_lovelace = 30_000_000  # 30 AP3X per batch
            tx = await create_datum_utxo(
                agent, treasury_addr, batch_datum.data, batch_lovelace,
                f"treasury-batch-{batch_id}"
            )
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 4f: Mint refs NFT and deploy GovernanceCrossRefs datum
        if "cross_refs_nft" not in tx_hashes:
            print("\n  4f. Minting refs NFT + deploying GovernanceCrossRefs...")
            proposal_mint_hash = applied_validators.get(
                "proposal.proposal_mint.mint", {}
            ).get("hash", "")
            critique_mint_hash = applied_validators.get(
                "critique.critique_mint.mint", {}
            ).get("hash", "")
            cross_refs_data = cbor2.CBORTag(121, [
                bytes.fromhex(proposal_hash),
                bytes.fromhex(critique_hash),
                bytes.fromhex(proposal_mint_hash),
                bytes.fromhex(critique_mint_hash),
            ])
            # Mint at oracle holder address (not wallet) to prevent
            # coin selection from consuming the CrossRefs UTxO.
            oracle_holder_addr = script_hash_to_testnet_address(holders["oracle"]["hash"])
            tx = await mint_refs_nft(
                agent, native_script_cbor, cross_refs_data, "cross-refs",
                target_address=oracle_holder_addr,
            )
            tx_hashes["cross_refs_nft"] = tx
        else:
            print(f"\n  4f. CrossRefs NFT already minted (skipping)")

        print(f"\n  Final balance: {(await agent.get_balance()).ada} AP3X")

    # ── Step 5: Save deployment state ────────────────────────────────────

    save_deploy_state(wallet, holders, refs_policy_id, applied_validators,
                      proposal_hash, critique_hash, tx_hashes)

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print("=" * 60)
    print(f"\nNext: python scripts/smoke_test.py")


def save_deploy_state(wallet, holders, refs_policy_id, applied_validators,
                      proposal_hash, critique_hash, tx_hashes):
    state = {
        "network": "vector-testnet",
        "wallet_address": wallet["address"],
        "wallet_vkey_hash": wallet["vkey_hash"],
        "refs_token_policy": refs_policy_id,
        "holders": {
            name: {"hash": info["hash"],
                    "address": script_hash_to_testnet_address(info["hash"]),
                    "compiled_code": info["compiled_code"]}
            for name, info in holders.items()
        },
        "validators": {
            title: {"hash": info["hash"], "compiled_code": info["compiled_code"]}
            for title, info in applied_validators.items()
        },
        "proposal_validator_hash": proposal_hash,
        "critique_validator_hash": critique_hash,
        "agent_registry_hash": AGENT_REGISTRY_HASH,
        "cross_refs_address": script_hash_to_testnet_address(holders["oracle"]["hash"]),
        "tx_hashes": tx_hashes,
        "explorer_url": EXPLORER_URL,
        "ogmios_url": OGMIOS_URL,
        "submit_url": SUBMIT_URL,
    }
    DEPLOY_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nDeployment state saved to {DEPLOY_STATE_FILE}")


def save_deploy_state_offline(wallet, holders, refs_policy_id, applied_validators,
                               proposal_hash, critique_hash):
    """Save state without tx hashes (offline mode)."""
    save_deploy_state(wallet, holders, refs_policy_id, applied_validators,
                      proposal_hash, critique_hash, {})


if __name__ == "__main__":
    asyncio.run(deploy())
