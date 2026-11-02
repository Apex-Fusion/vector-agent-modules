#!/usr/bin/env python3
"""
Module 3: Reputation Staking — MAINNET Deploy via Ogmios/PyCardano

Mainnet deployment using PyCardano + Ogmios HTTP.
Based on deploy_ogmios.py with mainnet-specific constants and endpoints.

Deployment order (deploy_common.py): config_holder, config_seal, identity,
Module-1 with identity_party, the seal, then Module-3 (this script), then
Module-6. New Foundation operator keys deploy and operate the modules. The
wallet key at WALLET_SKEY_PATH must be one of these keys. Old module keys
are not reused.

Inputs (environment variables, no defaults; deploy_common.DEPLOYMENT_INPUTS):
    VECTOR_IDENTITY_HASH              script hash of the identity contract
    VECTOR_MODULE1_CLAIM_HASH         claim validator hash of the new Module-1
    VECTOR_MODULE1_CHALLENGE_HASH     challenge validator hash of the new Module-1
    VECTOR_MODULE1_JURY_POOL_HASH     jury pool hash of the new Module-1
    VECTOR_MODULE1_REFS_TOKEN_POLICY  refs token policy of the new Module-1

Steps:
  0. Read the inputs. Stop when one is missing or not allowed.
  1. Compute NativeScript refs_token_policy from wallet vkey_hash
  2. Compute the params (tag 1) and treasury (tag 3) holders, owned by the
     wallet key, choose a seed UTxO and derive the record token policy
  3. Build ReputationConfig CBOR (12 fields, identity_hash last) and apply
     to both validators via aiken CLI
  4. Mint the `params` record token and create the ProtocolParams record
     (output 0) in one transaction that spends the seed
  5. Deploy 2 validator reference scripts (CIP-33)
  6. Mint CrossValidatorRefs NFT + datum UTXO
  7. Save deployment state

Resume: deploy/mainnet/deploy_state.json keeps the seed, the record token
policy (params_token_policy), the holder hashes, the owner key hash,
genesis_minting_window, identity_hash and the Module-1 values. A new run
reuses them and skips completed steps. A run with other input values stops.

Usage:
    export VECTOR_IDENTITY_HASH=<hex>
    export VECTOR_MODULE1_CLAIM_HASH=<hex>
    export VECTOR_MODULE1_CHALLENGE_HASH=<hex>
    export VECTOR_MODULE1_JURY_POOL_HASH=<hex>
    export VECTOR_MODULE1_REFS_TOKEN_POLICY=<hex>
    python3 scripts/deploy_mainnet_ogmios.py
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
    Redeemer,
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
    submit_tx as _submit_tx_orig,
    wait_for_tx,
)
from reputation_staking.utils import script_hash_to_address

from deploy_common import (
    DEPLOYMENT_INPUTS,
    HOLDER_BLUEPRINT_PATH,
    PARAMS_HOLDER_TAG,
    PARAMS_TOKEN_NAME,
    TREASURY_HOLDER_TAG,
    UNIT_REDEEMER_CBOR,
    DeployInputError,
    apply_holder,
    apply_record_token_policy,
    build_protocol_params_cbor,
    build_reputation_config_cbor,
    deployment_inputs_from_env,
    deployment_inputs_state,
    find_record_utxo,
    genesis_minting_end_ms,
    parse_utxo_ref,
    resume_state_error,
    select_seed_utxo,
    utxo_ref,
)

# ── MAINNET Config ──────────────────────────────────────────────────────────

MAINNET_OGMIOS_URL = "https://ogmios.vector.mainnet.apexfusion.org"
MAINNET_SUBMIT_URL = "https://submit.vector.mainnet.apexfusion.org/api/submit/tx"

CONTRACT_DIR = MODULE3_ROOT / "reputation-staking"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
DEPLOY_DIR = MODULE3_ROOT / "deploy" / "mainnet"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"

WALLET_SKEY_PATH = "/tmp/m3mainnet_payment.skey"

TX_WAIT = 25  # seconds between transactions

# ── Constants (mainnet) ────────────────────────────────────────────────────

AP3X_POLICY_ID = ""
AP3X_ASSET_NAME = ""


# ── Mainnet submit wrapper ─────────────────────────────────────────────────

def submit_tx(tx, submit_url: str = MAINNET_SUBMIT_URL) -> str:
    """Submit transaction using mainnet endpoint."""
    return _submit_tx_orig(tx, submit_url)


# ── NativeScript policy ────────────────────────────────────────────────────

def compute_native_script_policy(vkey_hash_hex: str) -> tuple:
    """Compute NativeScript policy ID for ScriptPubkey(vkey_hash)."""
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash_hex)])
    script_bytes = b"\x00" + native_script_cbor
    policy_id = hashlib.blake2b(script_bytes, digest_size=28).hexdigest()
    return policy_id, native_script_cbor


def make_pycardano_native_script(vkey_hash_hex: str) -> NativeScript:
    """Create PyCardano NativeScript for signing."""
    return ScriptPubkey(VerificationKeyHash(bytes.fromhex(vkey_hash_hex)))


# ── CrossValidatorRefs datum ───────────────────────────────────────────────

def build_cross_refs_cbor(reputation_hash: str, endorsement_hash: str) -> bytes:
    """Build CrossValidatorRefs as CBOR (Plutus Data)."""
    fields = [
        bytes.fromhex(reputation_hash),
        bytes.fromhex(endorsement_hash),
    ]
    return cbor2.dumps(cbor2.CBORTag(121, fields))


# ── Apply parameters via aiken CLI ─────────────────────────────────────────

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


# ── Deploy helpers ─────────────────────────────────────────────────────────

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


def mint_params_record(
    context: OgmiosHttpContext,
    skey: PaymentSigningKey,
    wallet_addr: Address,
    wallet_utxos: list,
    record_policy: dict,
    params_target: Address,
    params_cbor: bytes,
    protected_refs: set,
) -> str:
    """Mint the `params` record token and create the ProtocolParams record.

    One transaction spends the wallet UTxOs (the record token seed is one of
    them), mints exactly one `params` token of the one-shot record token
    policy, and creates the record at the params holder as output 0: inline
    ProtocolParams datum + token. The policy is a Plutus V3 script, so the
    transaction carries the script, a unit redeemer and collateral.
    Returns tx hash.
    """
    from pycardano.serialization import RawCBOR

    policy = ScriptHash(bytes.fromhex(record_policy["policy_id"]))
    token_name = AssetName(PARAMS_TOKEN_NAME)

    mint_ma = MultiAsset({policy: Asset({token_name: 1})})
    out_ma = MultiAsset({policy: Asset({token_name: 1})})

    builder = TransactionBuilder(context)
    builder.fee_buffer = 500_000

    for u in wallet_utxos:
        builder.add_input(u)

    builder.mint = mint_ma
    builder.add_minting_script(
        PlutusV3Script(bytes.fromhex(record_policy["compiled_code"])),
        redeemer=Redeemer(RawCBOR(UNIT_REDEEMER_CBOR)),
    )

    # Output 0: the ProtocolParams record (the SDK reads params_utxo#0)
    builder.add_output(
        TransactionOutput(
            params_target, Value(3_000_000, out_ma),
            datum=RawCBOR(params_cbor),
        )
    )

    builder.collaterals = [get_collateral_utxo(context, wallet_addr, protected_refs)]

    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx)
    print(f"  [params] Record token minted, ProtocolParams record created: {tx_hash}")
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


# ── State persistence ──────────────────────────────────────────────────────

def save_state(state: dict):
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ───────────────────────────────────────────────────────────────────

def deploy():
    print("=" * 60)
    print("Module 3: Reputation Staking — MAINNET Deployment")
    print("=" * 60)

    # Values of other deployments (deploy_common.DEPLOYMENT_INPUTS)
    try:
        inputs = deployment_inputs_from_env()
    except DeployInputError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print("\n--- Inputs ---")
    for field, (env_name, _, _) in DEPLOYMENT_INPUTS.items():
        print(f"  {env_name}: {inputs[field]}")

    # Check wallet
    if not Path(WALLET_SKEY_PATH).exists():
        print(f"ERROR: Wallet key not found at {WALLET_SKEY_PATH}")
        sys.exit(1)

    if not BLUEPRINT_PATH.exists():
        print("ERROR: Blueprint not found. Run 'aiken build' in reputation-staking/")
        sys.exit(1)

    if not HOLDER_BLUEPRINT_PATH.exists():
        print("ERROR: Holder blueprint not found. Run 'aiken build' in shared/holder-scripts/")
        sys.exit(1)

    # Load wallet
    skey, vkey, wallet_addr = load_wallet(WALLET_SKEY_PATH)
    vkey_hash_hex = str(vkey.hash())

    # Set up mainnet context
    context = OgmiosHttpContext(ogmios_url=MAINNET_OGMIOS_URL)

    # Protected refs (UTxOs to never spend as inputs)
    protected_refs = set()

    # Load existing state for resume
    state = {}
    if DEPLOY_STATE_FILE.exists():
        with open(DEPLOY_STATE_FILE) as f:
            state = json.load(f)
    tx_hashes = state.get("tx_hashes", {})
    state_error = resume_state_error(state, vkey_hash_hex, inputs)
    if state_error:
        print(f"ERROR: {DEPLOY_STATE_FILE}: {state_error}")
        sys.exit(1)

    # ── Step 0: Verify environment ──────────────────────────────────────

    print("\n--- Step 0: Verify Environment ---")
    current_slot = context.last_block_slot
    print(f"  Tip slot: {current_slot}")

    wallet_utxos = get_wallet_utxos(context, wallet_addr)
    total = sum(u.output.amount.coin if hasattr(u.output.amount, 'coin') else u.output.amount for u in wallet_utxos)
    print(f"  Wallet: {total / 1_000_000:.2f} AP3X ({len(wallet_utxos)} UTxOs)")
    print(f"  Address: {wallet_addr}")
    print(f"  VKey hash: {vkey_hash_hex}")
    print(f"  Network: MAINNET")
    print(f"  Ogmios: {MAINNET_OGMIOS_URL}")

    # ── Step 1: NativeScript policy ────────────────────────────────────

    print("\n--- Step 1: NativeScript (refs_token_policy) ---")
    refs_policy_id, _ = compute_native_script_policy(vkey_hash_hex)
    native_script = make_pycardano_native_script(vkey_hash_hex)
    print(f"  Policy ID: {refs_policy_id}")

    # ── Step 2: Holder scripts + record token policy ───────────────────

    print("\n--- Step 2: Holder Scripts + Record Token Policy ---")
    params_holder = apply_holder(PARAMS_HOLDER_TAG, vkey_hash_hex)
    treasury_holder = apply_holder(TREASURY_HOLDER_TAG, vkey_hash_hex)
    params_addr = script_hash_to_address(params_holder["hash"])
    treasury_addr = script_hash_to_address(treasury_holder["hash"])
    print(f"  Params holder:   {params_holder['hash']}")
    print(f"                   {params_addr}")
    print(f"  Treasury holder: {treasury_holder['hash']}")
    print(f"                   {treasury_addr}")

    seed_ref = state.get("record_token_seed")
    if seed_ref:
        # Never choose a new seed once a policy was saved
        print(f"  Seed (from deploy state): {seed_ref}")
    else:
        seed_ref = utxo_ref(select_seed_utxo(wallet_utxos))
        print(f"  Seed (new): {seed_ref}")

    record_policy = apply_record_token_policy(*parse_utxo_ref(seed_ref))
    params_token_policy = record_policy["policy_id"]
    saved_policy = state.get("params_token_policy")
    if saved_policy and saved_policy != params_token_policy:
        print(f"  ERROR: The seed gives policy {params_token_policy}, "
              f"but the deploy state has {saved_policy}.")
        print("         The record token script changed after the first run.")
        sys.exit(1)
    print(f"  Record token policy: {params_token_policy}")

    genesis_minting_window = state.get("genesis_minting_window") or genesis_minting_end_ms()
    print(f"  genesis_minting_window: {genesis_minting_window} (POSIX ms)")

    # Keep the seed, the record token policy and the inputs before the first
    # on-chain step
    state.update({
        "holder_owner_vkey_hash": vkey_hash_hex,
        "params_holder_hash": params_holder["hash"],
        "params_holder_address": params_addr,
        "treasury_holder_hash": treasury_holder["hash"],
        "treasury_holder_address": treasury_addr,
        "record_token_seed": seed_ref,
        "params_token_policy": params_token_policy,
        "genesis_minting_window": genesis_minting_window,
        **deployment_inputs_state(inputs),
    })
    save_state(state)

    # ── Step 3: Apply ReputationConfig ─────────────────────────────────

    print("\n--- Step 3: Apply ReputationConfig ---")
    config_cbor = build_reputation_config_cbor(
        refs_token_policy=refs_policy_id,
        audit_claim_validator_hash=inputs["audit_claim_validator_hash"],
        audit_challenge_validator_hash=inputs["audit_challenge_validator_hash"],
        audit_jury_pool_hash=inputs["audit_jury_pool_hash"],
        audit_refs_token_policy=inputs["audit_refs_token_policy"],
        params_script_hash=params_holder["hash"],
        treasury_script_hash=treasury_holder["hash"],
        ap3x_policy_id=AP3X_POLICY_ID,
        ap3x_asset_name=AP3X_ASSET_NAME,
        oracle_credential=vkey_hash_hex,
        params_token_policy=params_token_policy,
        identity_hash=inputs["identity_hash"],
    )
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

    print(f"  Reputation:  {reputation_hash}")
    print(f"               {reputation_addr}")
    print(f"  Endorsement: {endorsement_hash}")
    print(f"               {endorsement_addr}")

    # ── Step 4: Mint params record token + ProtocolParams record ───────
    # First on-chain step: one transaction spends the seed, mints exactly one
    # `params` token and creates the ProtocolParams record (output 0).

    if "params_utxo" not in tx_hashes:
        print("\n--- Step 4: Mint Params Record Token + ProtocolParams Record ---")

        wallet_utxos = get_wallet_utxos(context, wallet_addr, protected_refs)
        if any(utxo_ref(u) == seed_ref for u in wallet_utxos):
            params_cbor = build_protocol_params_cbor(genesis_minting_window)
            tx_hash = mint_params_record(
                context, skey, wallet_addr, wallet_utxos, record_policy,
                Address.from_primitive(params_addr), params_cbor, protected_refs,
            )
            tx_hashes["params_utxo"] = tx_hash
            state["tx_hashes"] = tx_hashes
            save_state(state)

            print(f"  Waiting {TX_WAIT}s for confirmation...")
            time.sleep(TX_WAIT)
        else:
            # The seed is spent: a prior run can have created the record
            record = find_record_utxo(
                context.utxos(params_addr), params_token_policy, PARAMS_TOKEN_NAME,
            )
            if record is None:
                print(f"  ERROR: Seed UTxO {seed_ref} is spent and the ProtocolParams record is not on chain.")
                print("         If the record transaction is pending, wait and run again.")
                print(f"         Otherwise move {DEPLOY_STATE_FILE} to a different path")
                print("         and start a new deployment.")
                sys.exit(1)
            tx_hashes["params_utxo"] = bytes(record.input.transaction_id).hex()
            state["tx_hashes"] = tx_hashes
            save_state(state)
            print(f"  ProtocolParams record found: {utxo_ref(record)}")
    else:
        print(f"\n--- Step 4: ProtocolParams record already created (skipping) ---")

    # ── Step 5: Deploy reference scripts ───────────────────────────────

    print("\n--- Step 5: Deploy Reference Scripts ---")

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

    # ── Step 6: Mint CrossValidatorRefs NFT ────────────────────────────

    if "cross_refs_nft" not in tx_hashes:
        print("\n--- Step 6: Mint CrossValidatorRefs NFT ---")

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
        print(f"\n--- Step 6: CrossRefs NFT already minted (skipping) ---")

    # ── Step 7: Save final state ───────────────────────────────────────

    print("\n--- Step 7: Save Final State ---")

    # Save applied blueprint
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_DIR / "plutus.json", "w") as f:
        json.dump(applied_bp, f, indent=2)

    state.update({
        "network": "vector-mainnet",
        "wallet_address": str(wallet_addr),
        "wallet_vkey_hash": vkey_hash_hex,
        "refs_token_policy": refs_policy_id,
        "reputation_validator_hash": reputation_hash,
        "endorsement_validator_hash": endorsement_hash,
        "reputation_address": reputation_addr,
        "endorsement_address": endorsement_addr,
        "params_holder_address": params_addr,
        "ap3x_policy_id": AP3X_POLICY_ID,
        "ap3x_asset_name": AP3X_ASSET_NAME,
        "validators": {
            title: {"hash": info["hash"]}
            for title, info in applied_validators.items()
            if ".else" not in title
        },
        "tx_hashes": tx_hashes,
    })
    save_state(state)

    print(f"  Applied blueprint: deploy/mainnet/plutus.json")
    print(f"  Full state:        deploy/mainnet/deploy_state.json")

    print("\n" + "=" * 60)
    print("MAINNET Deployment complete!")
    print("=" * 60)

    print(f"\n  Reputation validator:  {reputation_hash}")
    print(f"  Endorsement validator: {endorsement_hash}")
    print(f"  Refs token policy:     {refs_policy_id}")
    print(f"  Params token policy:   {params_token_policy}")
    print(f"  Identity hash:         {inputs['identity_hash']}")
    for name, tx in tx_hashes.items():
        print(f"  {name}: {tx}")


if __name__ == "__main__":
    deploy()
