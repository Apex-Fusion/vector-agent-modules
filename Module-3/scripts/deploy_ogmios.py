#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Deploy via Ogmios/PyCardano

Remote deployment using PyCardano + Ogmios HTTP, matching Module 6 patterns.

Steps:
  1. Compute NativeScript refs_token_policy from wallet vkey_hash
  2. Build ReputationConfig CBOR and apply to both validators via aiken CLI
  3. Deploy 2 validator reference scripts (CIP-33)
  4. Create ProtocolParams datum UTXO at holder address
  5. Mint CrossValidatorRefs NFT + datum UTXO
  6. Save deployment state

Usage:
    python3 scripts/deploy_ogmios.py
"""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

try:
    import cbor2
except ImportError:
    print("ERROR: cbor2 not installed. Run: pip install cbor2")
    sys.exit(1)

from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    NativeScript,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    ScriptAll,
    ScriptHash,
    ScriptPubkey,
    TransactionBuilder,
    TransactionOutput,
    Value,
)
from pycardano.hash import VerificationKeyHash

# Add python/ to path
MODULE3_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MODULE3_ROOT / "python"))

from reputation_staking.ogmios_backend import (
    NETWORK,
    OgmiosHttpContext,
    get_collateral_utxo,
    get_wallet_utxos,
    load_wallet,
    submit_tx,
    wait_for_tx,
)
from reputation_staking.constants import TX_WAIT_SECONDS
from reputation_staking.utils import script_hash_to_address

# ── Config ───────────────────────────────────────────────────────────────────

CONTRACT_DIR = MODULE3_ROOT / "reputation-staking"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
DEPLOY_DIR = MODULE3_ROOT / "deploy"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"

WALLET_SKEY_PATH = "/tmp/m3dev_payment.skey"

# ── Constants (same as deploy_docker.py) ────────────────────────────────────

AGENT_REGISTRY_HASH = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
AP3X_POLICY_ID = ""
AP3X_ASSET_NAME = ""

MODULE1_CLAIM_HASH = "6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda"
MODULE1_CHALLENGE_HASH = "781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc"
MODULE1_JURY_POOL_HASH = "b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962"
MODULE1_REFS_TOKEN_POLICY = "205d5f77ffebf60b764ba4f1873eff3764f3d1d594e5dac477a928f9"

PARAMS_HOLDER_HASH = "f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867"
TREASURY_HOLDER_HASH = "ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6"

TX_WAIT = TX_WAIT_SECONDS


# ── NativeScript policy ─────────────────────────────────────────────────────

def compute_native_script_policy(vkey_hash_hex: str) -> tuple:
    """Compute NativeScript policy ID for ScriptPubkey(vkey_hash).
    Returns (policy_id_hex, native_script_cbor_bytes)."""
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash_hex)])
    script_bytes = b"\x00" + native_script_cbor
    policy_id = hashlib.blake2b(script_bytes, digest_size=28).hexdigest()
    return policy_id, native_script_cbor


def make_pycardano_native_script(vkey_hash_hex: str) -> NativeScript:
    """Create PyCardano NativeScript for signing."""
    return ScriptPubkey(VerificationKeyHash(bytes.fromhex(vkey_hash_hex)))


# ── ReputationConfig CBOR ────────────────────────────────────────────────────

def build_reputation_config_cbor(refs_token_policy: str, oracle_vkey_hash: str) -> str:
    """Build ReputationConfig as Plutus Data CBOR hex.
    Constructor 0 with 12 fields matching the Aiken type."""
    fields = [
        bytes.fromhex(refs_token_policy),
        bytes.fromhex(AGENT_REGISTRY_HASH),
        bytes.fromhex(AGENT_REGISTRY_HASH),
        bytes.fromhex(MODULE1_CLAIM_HASH),
        bytes.fromhex(MODULE1_CHALLENGE_HASH),
        bytes.fromhex(MODULE1_JURY_POOL_HASH),
        bytes.fromhex(MODULE1_REFS_TOKEN_POLICY),
        bytes.fromhex(PARAMS_HOLDER_HASH),
        bytes.fromhex(TREASURY_HOLDER_HASH),
        bytes.fromhex(AP3X_POLICY_ID) if AP3X_POLICY_ID else b"",
        bytes.fromhex(AP3X_ASSET_NAME) if AP3X_ASSET_NAME else b"",
        bytes.fromhex(oracle_vkey_hash),
    ]
    config_data = cbor2.CBORTag(121, fields)
    return cbor2.dumps(config_data).hex()


# ── ProtocolParams datum ─────────────────────────────────────────────────────

def build_protocol_params_cbor() -> bytes:
    """Build ProtocolParams as CBOR (Plutus Data).
    Constructor 0 with 22 fields."""
    valid_capabilities = [
        b"code_review", b"testing", b"deployment", b"documentation",
        b"security_audit", b"architecture", b"data_analysis", b"ml_training",
    ]
    fields = [
        10_000_000,     # min_self_stake (10 AP3X)
        5_000_000,      # min_endorsement (5 AP3X)
        25_000_000,     # min_challenge_stake (25 AP3X)
        21_600,         # stake_cooldown (24h)
        43_200,         # endorsement_cooldown (48h)
        100,            # decay_rate (1%)
        180,            # activity_window (180 epochs)
        500,            # decay_collector_fee (5%)
        1000,           # history_multiplier (10%)
        3,              # max_endorsement_multiplier
        5000,           # slash_rate_endorser (50%)
        500,            # protocol_fee_rate (5%)
        10_800,         # challenge_response_deadline (12h)
        21_600,         # min_agent_age (24h)
        5_400,          # escalation_window (6h)
        100,            # default_judgment_fee (1%)
        50,             # genesis_agent_cap
        100_000_000,    # genesis_bonus_amount (100 AP3X)
        604_800,        # genesis_minting_window (28 days)
        129_600,        # genesis_protection_period (60 days)
        valid_capabilities,  # valid_capabilities
        900,            # epoch_length (4h)
    ]
    return cbor2.dumps(cbor2.CBORTag(121, fields))


def build_cross_refs_cbor(reputation_hash: str, endorsement_hash: str) -> bytes:
    """Build CrossValidatorRefs as CBOR (Plutus Data)."""
    fields = [
        bytes.fromhex(reputation_hash),
        bytes.fromhex(endorsement_hash),
    ]
    return cbor2.dumps(cbor2.CBORTag(121, fields))


# ── Apply parameters via aiken CLI ──────────────────────────────────────────

def apply_reputation_config(config_cbor_hex: str) -> dict:
    """Apply ReputationConfig to both Module-3 validators via aiken CLI."""
    current = str(BLUEPRINT_PATH)
    validators_to_apply = [
        ("reputation", "reputation"),
        ("endorsement", "endorsement"),
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
            out_path = str(CONTRACT_DIR / f"plutus-applied-{i}.json")
            cmd.extend(["-o", out_path])
            subprocess.run(cmd, check=True)
            current = out_path
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            applied_bp = json.loads(result.stdout)

    # Clean up intermediates
    for i in range(len(validators_to_apply) - 1):
        f = CONTRACT_DIR / f"plutus-applied-{i}.json"
        if f.exists():
            f.unlink()

    return applied_bp


# ── Deploy helpers ───────────────────────────────────────────────────────────

def deploy_reference_script(
    context: OgmiosHttpContext,
    skey: PaymentSigningKey,
    wallet_addr: Address,
    script_cbor_hex: str,
    label: str,
    protected_refs: set,
) -> str:
    """Deploy a validator as a reference script (CIP-33). Returns tx hash."""
    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2

    # Min UTXO for reference scripts: ~4400 lovelace per byte + 2 AP3X base
    min_lovelace = 2_000_000 + script_size * 4400
    min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

    print(f"  [{label}] Script size: {script_size} bytes, min UTXO: {min_lovelace / 1_000_000:.1f} AP3X")

    builder = TransactionBuilder(context)
    builder.fee_buffer = max(300_000, script_size * 50)

    wallet_utxos = get_wallet_utxos(context, wallet_addr, protected_refs)
    for u in wallet_utxos:
        builder.add_input(u)

    builder.add_output(
        TransactionOutput(wallet_addr, min_lovelace, script=script)
    )

    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx)
    print(f"  [{label}] Deployed: {tx_hash}")
    return tx_hash


def create_datum_utxo(
    context: OgmiosHttpContext,
    skey: PaymentSigningKey,
    wallet_addr: Address,
    target_addr: Address,
    datum_cbor: bytes,
    lovelace: int,
    label: str,
    protected_refs: set,
) -> str:
    """Create a UTXO at target_addr with inline datum. Returns tx hash."""
    from pycardano.serialization import RawCBOR

    builder = TransactionBuilder(context)
    builder.fee_buffer = 300_000

    wallet_utxos = get_wallet_utxos(context, wallet_addr, protected_refs)
    for u in wallet_utxos:
        builder.add_input(u)

    builder.add_output(
        TransactionOutput(target_addr, lovelace, datum=RawCBOR(datum_cbor))
    )

    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx)
    print(f"  [{label}] Created: {tx_hash}")
    return tx_hash


def mint_refs_nft(
    context: OgmiosHttpContext,
    skey: PaymentSigningKey,
    vkey: PaymentVerificationKey,
    wallet_addr: Address,
    native_script: NativeScript,
    refs_policy_id: str,
    refs_datum_cbor: bytes,
    label: str,
    protected_refs: set,
) -> str:
    """Mint a refs NFT with inline datum at wallet address. Returns tx hash."""
    from pycardano.serialization import RawCBOR

    policy = ScriptHash(bytes.fromhex(refs_policy_id))
    token_name = AssetName(b"reputation_refs")

    mint_ma = MultiAsset({policy: Asset({token_name: 1})})
    out_ma = MultiAsset({policy: Asset({token_name: 1})})

    builder = TransactionBuilder(context)
    builder.fee_buffer = 300_000

    wallet_utxos = get_wallet_utxos(context, wallet_addr, protected_refs)
    for u in wallet_utxos:
        builder.add_input(u)

    builder.mint = mint_ma
    builder.native_scripts = [native_script]

    # Send NFT + datum to wallet address (not params_addr) to avoid
    # confusion with ProtocolParams datum during find_protocol_params
    builder.add_output(
        TransactionOutput(
            wallet_addr, Value(3_000_000, out_ma),
            datum=RawCBOR(refs_datum_cbor),
        )
    )

    builder.required_signers = [vkey.hash()]

    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx)
    print(f"  [{label}] Minted: {tx_hash}")
    return tx_hash


# ── State persistence ────────────────────────────────────────────────────────

def save_state(state: dict):
    DEPLOY_DIR.mkdir(exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ─────────────────────────────────────────────────────────────────────

def deploy():
    print("=" * 60)
    print("Module 3: Reputation Staking — Ogmios Deployment")
    print("=" * 60)

    # Check wallet
    if not Path(WALLET_SKEY_PATH).exists():
        print(f"ERROR: Wallet key not found at {WALLET_SKEY_PATH}")
        sys.exit(1)

    if not BLUEPRINT_PATH.exists():
        print("ERROR: Blueprint not found. Run 'aiken build' in reputation-staking/")
        sys.exit(1)

    # Load wallet
    skey, vkey, wallet_addr = load_wallet(WALLET_SKEY_PATH)
    vkey_hash_hex = str(vkey.hash())

    # Set up context
    context = OgmiosHttpContext()

    # Protected refs (UTxOs to never spend as inputs)
    protected_refs = set()

    # Load existing state for resume
    state = {}
    if DEPLOY_STATE_FILE.exists():
        with open(DEPLOY_STATE_FILE) as f:
            state = json.load(f)
    tx_hashes = state.get("tx_hashes", {})

    # ── Step 0: Verify environment ──────────────────────────────────────

    print("\n--- Step 0: Verify Environment ---")
    current_slot = context.last_block_slot
    print(f"  Tip slot: {current_slot}")

    wallet_utxos = get_wallet_utxos(context, wallet_addr)
    total = sum(u.output.amount.coin if hasattr(u.output.amount, 'coin') else u.output.amount for u in wallet_utxos)
    print(f"  Wallet: {total / 1_000_000:.2f} AP3X ({len(wallet_utxos)} UTxOs)")
    print(f"  Address: {wallet_addr}")
    print(f"  VKey hash: {vkey_hash_hex}")

    # ── Step 1: NativeScript policy ─────────────────────────────────────

    print("\n--- Step 1: NativeScript (refs_token_policy) ---")
    refs_policy_id, _ = compute_native_script_policy(vkey_hash_hex)
    native_script = make_pycardano_native_script(vkey_hash_hex)
    print(f"  Policy ID: {refs_policy_id}")

    # ── Step 2: Apply ReputationConfig ──────────────────────────────────

    print("\n--- Step 2: Apply ReputationConfig ---")
    config_cbor = build_reputation_config_cbor(refs_policy_id, vkey_hash_hex)
    print(f"  Config CBOR: {len(config_cbor) // 2} bytes")

    applied_bp = apply_reputation_config(config_cbor)

    applied_validators = {}
    print("  Applied validator hashes:")
    for v in applied_bp["validators"]:
        title = v["title"]
        applied_validators[title] = {
            "hash": v["hash"],
            "compiled_code": v["compiledCode"],
        }
        if ".else" not in title:
            print(f"    {title}: {v['hash']}")

    reputation_hash = applied_validators["reputation.reputation.spend"]["hash"]
    endorsement_hash = applied_validators["endorsement.endorsement.spend"]["hash"]
    reputation_addr = script_hash_to_address(reputation_hash)
    endorsement_addr = script_hash_to_address(endorsement_hash)
    params_addr = script_hash_to_address(PARAMS_HOLDER_HASH)

    print(f"  Reputation:  {reputation_hash}")
    print(f"               {reputation_addr}")
    print(f"  Endorsement: {endorsement_hash}")
    print(f"               {endorsement_addr}")

    # ── Step 3: Deploy reference scripts ────────────────────────────────

    print("\n--- Step 3: Deploy Reference Scripts ---")

    ref_scripts = [
        ("reputation.reputation.spend", "reputation_spend"),
        ("endorsement.endorsement.spend", "endorsement_spend"),
    ]

    for title, label in ref_scripts:
        key = f"{label}_ref"
        if key in tx_hashes:
            print(f"  [{label}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
            protected_refs.add(tx_hashes[key])
            continue

        tx_hash = deploy_reference_script(
            context, skey, wallet_addr,
            applied_validators[title]["compiled_code"],
            label, protected_refs,
        )
        tx_hashes[key] = tx_hash
        protected_refs.add(f"{tx_hash}#0")

        state.update({
            "tx_hashes": tx_hashes,
            "reputation_validator_hash": reputation_hash,
            "endorsement_validator_hash": endorsement_hash,
        })
        save_state(state)

        print(f"  Waiting {TX_WAIT}s for confirmation...")
        time.sleep(TX_WAIT)

    # ── Step 4: Create ProtocolParams datum UTXO ────────────────────────

    if "params_utxo" not in tx_hashes:
        print("\n--- Step 4: Create ProtocolParams UTXO ---")

        params_cbor = build_protocol_params_cbor()
        params_target = Address.from_primitive(params_addr)

        tx_hash = create_datum_utxo(
            context, skey, wallet_addr, params_target,
            params_cbor, 3_000_000, "params", protected_refs,
        )
        tx_hashes["params_utxo"] = tx_hash
        protected_refs.add(f"{tx_hash}#0")

        state["tx_hashes"] = tx_hashes
        save_state(state)

        print(f"  Waiting {TX_WAIT}s for confirmation...")
        time.sleep(TX_WAIT)
    else:
        print(f"\n--- Step 4: ProtocolParams already deployed (skipping) ---")
        protected_refs.add(tx_hashes["params_utxo"])

    # ── Step 5: Mint CrossValidatorRefs NFT ─────────────────────────────

    if "cross_refs_nft" not in tx_hashes:
        print("\n--- Step 5: Mint CrossValidatorRefs NFT ---")

        refs_datum_cbor = build_cross_refs_cbor(reputation_hash, endorsement_hash)

        tx_hash = mint_refs_nft(
            context, skey, vkey, wallet_addr,
            native_script, refs_policy_id, refs_datum_cbor,
            "cross_refs", protected_refs,
        )
        tx_hashes["cross_refs_nft"] = tx_hash

        state["tx_hashes"] = tx_hashes
        save_state(state)
    else:
        print(f"\n--- Step 5: CrossRefs NFT already minted (skipping) ---")

    # ── Step 6: Save final state ────────────────────────────────────────

    print("\n--- Step 6: Save Final State ---")

    # Save applied blueprint
    DEPLOY_DIR.mkdir(exist_ok=True)
    with open(DEPLOY_DIR / "plutus.json", "w") as f:
        json.dump(applied_bp, f, indent=2)

    state.update({
        "network": "vector-testnet",
        "wallet_address": str(wallet_addr),
        "wallet_vkey_hash": vkey_hash_hex,
        "refs_token_policy": refs_policy_id,
        "reputation_validator_hash": reputation_hash,
        "endorsement_validator_hash": endorsement_hash,
        "reputation_address": reputation_addr,
        "endorsement_address": endorsement_addr,
        "params_holder_address": params_addr,
        "agent_registry_hash": AGENT_REGISTRY_HASH,
        "ap3x_policy_id": AP3X_POLICY_ID,
        "ap3x_asset_name": AP3X_ASSET_NAME,
        "module1_refs_token_policy": MODULE1_REFS_TOKEN_POLICY,
        "validators": {
            title: {"hash": info["hash"]}
            for title, info in applied_validators.items()
            if ".else" not in title
        },
        "tx_hashes": tx_hashes,
    })
    save_state(state)

    print(f"  Applied blueprint: deploy/plutus.json")
    print(f"  Full state:        deploy/deploy_state.json")

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print("=" * 60)

    print(f"\n  Reputation validator:  {reputation_hash}")
    print(f"  Endorsement validator: {endorsement_hash}")
    print(f"  Refs token policy:     {refs_policy_id}")
    for name, tx in tx_hashes.items():
        print(f"  {name}: {tx}")


if __name__ == "__main__":
    deploy()
