#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Deploy via cardano-cli in Docker

Deploys Module 3 contracts to Vector testnet using the local cardano-node
running in a Docker container.

Deployment order (deploy_common.py): config_holder, config_seal, identity,
Module-1 with identity_party, the seal, then Module-3 (this script), then
Module-6. New Foundation operator keys deploy and operate the modules. The
wallet key in FEE_WALLET_DIR must be one of these keys. Old module keys are
not reused.

Inputs (environment variables, no defaults; deploy_common.DEPLOYMENT_INPUTS):
    VECTOR_IDENTITY_HASH              script hash of the identity contract
    VECTOR_MODULE1_CLAIM_HASH         claim validator hash of the new Module-1
    VECTOR_MODULE1_CHALLENGE_HASH     challenge validator hash of the new Module-1
    VECTOR_MODULE1_JURY_POOL_HASH     jury pool hash of the new Module-1
    VECTOR_MODULE1_REFS_TOKEN_POLICY  refs token policy of the new Module-1

Steps:
  0. Read the inputs. Stop when one is missing or not allowed.
  1. Compute NativeScript refs_token_policy from fee wallet vkey_hash
  2. Compute the params (tag 1) and treasury (tag 3) holders, owned by the
     wallet key, choose a seed UTxO and derive the record token policy
  3. Build ReputationConfig CBOR (12 fields, identity_hash last) and apply
     to both validators
  4. Mint the `params` record token and create the ProtocolParams record
     (output 0) in one transaction that spends the seed
  5. Deploy 2 validator reference scripts (CIP-33)
  6. Mint CrossValidatorRefs NFT + datum UTXO
  7. Save deployment state

Resume: deploy_state.json keeps the seed, the record token policy
(params_token_policy), the holder hashes, the owner key hash,
genesis_minting_window, identity_hash and the Module-1 values. A new run
reuses them and skips completed steps. A run with other input values stops.

Usage:
    export VECTOR_IDENTITY_HASH=<hex>
    export VECTOR_MODULE1_CLAIM_HASH=<hex>
    export VECTOR_MODULE1_CHALLENGE_HASH=<hex>
    export VECTOR_MODULE1_JURY_POOL_HASH=<hex>
    export VECTOR_MODULE1_REFS_TOKEN_POLICY=<hex>
    python3 scripts/deploy_docker.py
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import cbor2
except ImportError:
    print("ERROR: cbor2 not installed. Run: pip install cbor2")
    sys.exit(1)

from deploy_common import (
    DEPLOYMENT_INPUTS,
    HOLDER_BLUEPRINT_PATH,
    PARAMS_HOLDER_TAG,
    PARAMS_TOKEN_NAME,
    TREASURY_HOLDER_TAG,
    DeployInputError,
    apply_holder,
    apply_record_token_policy,
    build_protocol_params_json,
    build_reputation_config_cbor,
    deployment_inputs_from_env,
    deployment_inputs_state,
    genesis_minting_end_ms,
    parse_utxo_ref,
    resume_state_error,
)

# ── Config ───────────────────────────────────────────────────────────────────

DOCKER_CONTAINER = "vector-public-testnet-tools-10_1_4-vector-relay-1"
SOCKET_PATH = "ipc/node.socket"
NETWORK_FLAG = "--mainnet"
FEE_WALLET_DIR = "/tmp/m3dev"  # Dev wallet (funded from fee wallet)

MODULE3_ROOT = Path(__file__).parent.parent
CONTRACT_DIR = MODULE3_ROOT / "reputation-staking"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
DEPLOY_DIR = MODULE3_ROOT / "deploy"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"

# ── Constants ────────────────────────────────────────────────────────────────

AP3X_POLICY_ID = ""       # Native currency (lovelace) on Vector
AP3X_ASSET_NAME = ""      # Empty for native currency

TX_WAIT = 25  # seconds between transactions


# ── Docker exec helper ───────────────────────────────────────────────────────

def docker_exec(cmd: str, check: bool = True) -> str:
    """Run a command inside the Docker container."""
    full_cmd = ["docker", "exec", DOCKER_CONTAINER, "bash", "-c", cmd]
    result = subprocess.run(full_cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        raise RuntimeError(f"Docker command failed: {cmd[:80]}...")
    return result.stdout.strip()


def cardano_cli(args: str) -> str:
    """Run cardano-cli inside Docker. Adds --socket-path for commands that need it."""
    needs_socket = any(
        kw in args for kw in ["query ", "transaction build ", "transaction submit "]
    )
    socket_arg = f" --socket-path {SOCKET_PATH}" if needs_socket else ""
    return docker_exec(f"cardano-cli {args}{socket_arg}")


def write_json_to_docker(remote_path: str, data: dict):
    """Write a JSON file into the Docker container via docker cp."""
    local_tmp = f"/tmp/m3deploy_{Path(remote_path).name}"
    with open(local_tmp, "w") as f:
        json.dump(data, f)
    subprocess.run(
        ["docker", "cp", local_tmp, f"{DOCKER_CONTAINER}:{remote_path}"],
        check=True,
    )
    os.unlink(local_tmp)


# ── NativeScript policy ─────────────────────────────────────────────────────

def compute_native_script_policy(vkey_hash: str) -> tuple[str, dict]:
    """Compute NativeScript policy ID for ScriptPubkey(vkey_hash).
    Returns (policy_id, native_script_json)."""
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash)])
    script_bytes = b"\x00" + native_script_cbor
    policy_id = hashlib.blake2b(script_bytes, digest_size=28).hexdigest()
    # JSON format for cardano-cli
    native_script_json = {
        "type": "sig",
        "keyHash": vkey_hash,
    }
    return policy_id, native_script_json


# ── CrossValidatorRefs datum ─────────────────────────────────────────────────

def build_cross_refs_json(reputation_hash: str, endorsement_hash: str) -> dict:
    """Build CrossValidatorRefs as cardano-cli JSON ScriptData."""
    return {
        "constructor": 0,
        "fields": [
            {"bytes": reputation_hash},
            {"bytes": endorsement_hash},
        ],
    }


# ── Apply parameters ────────────────────────────────────────────────────────

def apply_reputation_config(config_cbor_hex: str) -> dict:
    """Apply ReputationConfig to both Module-3 validators."""
    current = str(BLUEPRINT_PATH)
    # Multi-validators: apply to module=reputation, validator=reputation
    # then module=endorsement, validator=endorsement (each covers mint+spend+else)
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

    # Clean up
    for i in range(len(validators_to_apply) - 1):
        f = CONTRACT_DIR / f"plutus-applied-{i}.json"
        if f.exists():
            f.unlink()

    return applied_bp


# ── Bech32 address ──────────────────────────────────────────────────────────

def script_hash_to_address(script_hash_hex: str) -> str:
    """Convert script hash to mainnet bech32 address via bech32 encoding."""
    # Mainnet script address: header byte 0x71 + 28-byte script hash
    header = bytes([0x71]) + bytes.fromhex(script_hash_hex)
    # bech32 encode with "addr" prefix
    import bech32
    converted = bech32.convertbits(header, 8, 5)
    return bech32.bech32_encode("addr", converted)


# ── Transaction helpers ──────────────────────────────────────────────────────

def get_wallet_address() -> str:
    """Get the wallet address from docker."""
    return docker_exec(f"cat {FEE_WALLET_DIR}/payment.addr")


def query_utxos(address: str) -> dict:
    """UTxOs at an address as cardano-cli JSON: {"<tx_id>#<index>": {...}}."""
    output = cardano_cli(
        f"conway query utxo {NETWORK_FLAG} "
        f"--address {address} "
        f"--out-file /dev/stdout"
    )
    return json.loads(output)


def get_wallet_utxo(wallet_addr: str) -> tuple[str, int]:
    """Get the largest wallet UTXO (tx_hash, lovelace)."""
    utxos = query_utxos(wallet_addr)
    # Find largest
    best_txin = None
    best_lovelace = 0
    for txin, data in utxos.items():
        lovelace = data["value"]["lovelace"]
        if lovelace > best_lovelace:
            best_lovelace = lovelace
            best_txin = txin
    return best_txin, best_lovelace


def select_seed_txin(wallet_addr: str) -> str:
    """Choose the record token seed: the largest wallet UTxO that holds
    lovelace only (no native tokens, no datum, no reference script)."""
    best_txin = None
    best_lovelace = 0
    for txin, data in query_utxos(wallet_addr).items():
        if set(data["value"]) != {"lovelace"}:
            continue
        if any(data.get(key) is not None for key in (
            "datum", "datumhash", "inlineDatum", "inlineDatumhash", "referenceScript",
        )):
            continue
        lovelace = data["value"]["lovelace"]
        if lovelace > best_lovelace:
            best_lovelace = lovelace
            best_txin = txin
    if best_txin is None:
        raise RuntimeError(
            "No wallet UTxO with lovelace only is available as the record token seed"
        )
    return best_txin


def find_record_txin(address: str, policy_id: str, token_name: bytes) -> Optional[str]:
    """The UTxO at `address` that holds exactly one record token
    (policy_id, token_name), or None."""
    for txin, data in query_utxos(address).items():
        if data["value"].get(policy_id, {}).get(token_name.hex()) == 1:
            return txin
    return None


def submit_tx(tx_file: str) -> str:
    """Submit a signed transaction."""
    return cardano_cli(
        f"conway transaction submit {NETWORK_FLAG} --tx-file {tx_file}"
    )


# ── Main deployment ─────────────────────────────────────────────────────────

def deploy():
    print("=" * 60)
    print("Module 3: Reputation Staking — Docker Deployment")
    print("=" * 60)

    # Values of other deployments (deploy_common.DEPLOYMENT_INPUTS)
    try:
        inputs = deployment_inputs_from_env()
    except DeployInputError as e:
        print(f"  ERROR: {e}")
        return
    print("\n--- Inputs ---")
    for field, (env_name, _, _) in DEPLOYMENT_INPUTS.items():
        print(f"  {env_name}: {inputs[field]}")

    # Load existing state for resume support
    state = {}
    if DEPLOY_STATE_FILE.exists():
        with open(DEPLOY_STATE_FILE) as f:
            state = json.load(f)
    tx_hashes = state.get("tx_hashes", {})

    # ── Step 0: Verify node and wallet ──────────────────────────────────

    print("\n--- Step 0: Verify Environment ---")
    tip = json.loads(cardano_cli(f"conway query tip {NETWORK_FLAG}"))
    print(f"  Node tip: slot {tip['slot']}, block {tip['block']}, sync {tip['syncProgress']}")

    wallet_addr = docker_exec(f"cat {FEE_WALLET_DIR}/payment.addr")
    vkey_json = json.loads(docker_exec(f"cat {FEE_WALLET_DIR}/payment.vkey"))
    vkey_hash = docker_exec(
        f"cardano-cli conway address key-hash "
        f"--payment-verification-key-file {FEE_WALLET_DIR}/payment.vkey"
    )
    print(f"  Wallet: {wallet_addr[:50]}...")
    print(f"  VKey hash: {vkey_hash}")

    txin, balance = get_wallet_utxo(wallet_addr)
    print(f"  Best UTXO: {txin} ({balance / 1_000_000:.2f} AP3X)")

    state_error = resume_state_error(state, vkey_hash, inputs)
    if state_error:
        print(f"  ERROR: {DEPLOY_STATE_FILE}: {state_error}")
        return

    if not BLUEPRINT_PATH.exists():
        print("  ERROR: Blueprint not found. Run 'aiken build' in reputation-staking/")
        return

    if not HOLDER_BLUEPRINT_PATH.exists():
        print("  ERROR: Holder blueprint not found. Run 'aiken build' in shared/holder-scripts/")
        return

    # ── Step 1: Compute NativeScript policy ─────────────────────────────

    print("\n--- Step 1: NativeScript (refs_token_policy) ---")
    refs_policy_id, native_script_json = compute_native_script_policy(vkey_hash)
    print(f"  Policy ID: {refs_policy_id}")

    # Write native script to docker container
    docker_exec("mkdir -p /tmp/m3deploy")
    write_json_to_docker("/tmp/m3deploy/refs_native_script.json", native_script_json)

    # ── Step 2: Holder scripts + record token policy ────────────────────

    print("\n--- Step 2: Holder Scripts + Record Token Policy ---")
    params_holder = apply_holder(PARAMS_HOLDER_TAG, vkey_hash)
    treasury_holder = apply_holder(TREASURY_HOLDER_TAG, vkey_hash)
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
        seed_ref = select_seed_txin(wallet_addr)
        print(f"  Seed (new): {seed_ref}")

    record_policy = apply_record_token_policy(*parse_utxo_ref(seed_ref))
    params_token_policy = record_policy["policy_id"]
    saved_policy = state.get("params_token_policy")
    if saved_policy and saved_policy != params_token_policy:
        print(f"  ERROR: The seed gives policy {params_token_policy}, "
              f"but the deploy state has {saved_policy}.")
        print("         The record token script changed after the first run.")
        return
    print(f"  Record token policy: {params_token_policy}")

    genesis_minting_window = state.get("genesis_minting_window") or genesis_minting_end_ms()
    print(f"  genesis_minting_window: {genesis_minting_window} (POSIX ms)")

    # Keep the seed, the record token policy and the inputs before the first
    # on-chain step
    state.update({
        "holder_owner_vkey_hash": vkey_hash,
        "params_holder_hash": params_holder["hash"],
        "params_holder_address": params_addr,
        "treasury_holder_hash": treasury_holder["hash"],
        "treasury_holder_address": treasury_addr,
        "record_token_seed": seed_ref,
        "params_token_policy": params_token_policy,
        "genesis_minting_window": genesis_minting_window,
        **deployment_inputs_state(inputs),
    })
    write_state(state)

    # Record token policy script for cardano-cli (double CBOR, as for the
    # validators below) and its unit redeemer (constructor 0, no fields)
    write_json_to_docker("/tmp/m3deploy/record_token_policy.plutus", {
        "type": "PlutusScriptV3",
        "description": "record_token.record_token.mint",
        "cborHex": cbor2.dumps(bytes.fromhex(record_policy["compiled_code"])).hex(),
    })
    write_json_to_docker("/tmp/m3deploy/unit_redeemer.json", {"constructor": 0, "fields": []})

    # ── Step 3: Apply ReputationConfig ──────────────────────────────────

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
        oracle_credential=vkey_hash,
        params_token_policy=params_token_policy,
        identity_hash=inputs["identity_hash"],
    )
    print(f"  Config CBOR: {len(config_cbor) // 2} bytes")

    applied_bp = apply_reputation_config(config_cbor)

    applied_validators = {}
    print("\n  Applied validator hashes:")
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

    print(f"\n  Reputation:  {reputation_hash}")
    print(f"               {reputation_addr}")
    print(f"  Endorsement: {endorsement_hash}")
    print(f"               {endorsement_addr}")
    print(f"  Params holder: {params_addr}")

    # Write compiled scripts as files in docker for reference script deployment
    # cardano-cli expects double-CBOR: cbor(cbor(flat_script))
    for title, info in applied_validators.items():
        if ".else" in title:
            continue
        safe_name = title.replace(".", "_")
        # compiledCode from blueprint is cbor(flat_script); wrap once more
        inner_bytes = bytes.fromhex(info["compiled_code"])
        double_cbor = cbor2.dumps(inner_bytes).hex()
        envelope = {
            "type": "PlutusScriptV3",
            "description": title,
            "cborHex": double_cbor,
        }
        write_json_to_docker(f"/tmp/m3deploy/{safe_name}.plutus", envelope)

    # ── Step 4: Mint params record token + ProtocolParams record ────────
    # First on-chain step: one transaction spends the seed, mints exactly one
    # `params` token and creates the ProtocolParams record (output 0).

    if "params_utxo" not in tx_hashes:
        print("\n--- Step 4: Mint Params Record Token + ProtocolParams Record ---")

        if seed_ref in query_utxos(wallet_addr):
            params_datum = build_protocol_params_json(genesis_minting_window)
            write_json_to_docker("/tmp/m3deploy/params_datum.json", params_datum)

            # Token: 1 <params_token_policy>.params
            params_mint_value = f"1 {params_token_policy}.{PARAMS_TOKEN_NAME.hex()}"

            # The seed is the only input and also the collateral. The record
            # token policy is a Plutus V3 script: --mint-script-file gives the
            # script witness, --mint-redeemer-file the unit redeemer.
            # Output 0 is the ProtocolParams record (the SDK reads params_utxo#0).
            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {seed_ref} "
                f"--tx-in-collateral {seed_ref} "
                f"--tx-out '{params_addr}+3000000+{params_mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3deploy/params_datum.json "
                f"--mint '{params_mint_value}' "
                f"--mint-script-file /tmp/m3deploy/record_token_policy.plutus "
                f"--mint-redeemer-file /tmp/m3deploy/unit_redeemer.json "
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3deploy/tx_params.raw"
            )

            cardano_cli(
                f"conway transaction sign {NETWORK_FLAG} "
                f"--tx-body-file /tmp/m3deploy/tx_params.raw "
                f"--signing-key-file {FEE_WALLET_DIR}/payment.skey "
                f"--out-file /tmp/m3deploy/tx_params.signed"
            )

            submit_tx(f"/tmp/m3deploy/tx_params.signed")

            tx_hash = docker_exec(
                f"cardano-cli conway transaction txid "
                f"--tx-file /tmp/m3deploy/tx_params.signed"
            )
            tx_hashes["params_utxo"] = tx_hash
            print(f"  ProtocolParams record: {tx_hash}#0")

            save_state(state, tx_hashes, applied_validators, reputation_hash,
                       endorsement_hash, reputation_addr, endorsement_addr,
                       refs_policy_id, vkey_hash, wallet_addr, params_addr)

            print(f"  Waiting {TX_WAIT}s for confirmation...")
            time.sleep(TX_WAIT)
        else:
            # The seed is spent: a prior run can have created the record
            record_txin = find_record_txin(params_addr, params_token_policy, PARAMS_TOKEN_NAME)
            if record_txin is None:
                print(f"  ERROR: Seed UTxO {seed_ref} is spent and the ProtocolParams record is not on chain.")
                print("         If the record transaction is pending, wait and run again.")
                print(f"         Otherwise move {DEPLOY_STATE_FILE} to a different path")
                print("         and start a new deployment.")
                return
            tx_hashes["params_utxo"] = parse_utxo_ref(record_txin)[0]
            print(f"  ProtocolParams record found: {record_txin}")

            save_state(state, tx_hashes, applied_validators, reputation_hash,
                       endorsement_hash, reputation_addr, endorsement_addr,
                       refs_policy_id, vkey_hash, wallet_addr, params_addr)
    else:
        print(f"\n--- Step 4: ProtocolParams record already created (skipping) ---")

    # ── Step 5: Deploy reference scripts (CIP-33) ───────────────────────

    print("\n--- Step 5: Deploy Reference Scripts ---")

    ref_scripts = [
        ("reputation.reputation.spend", "reputation_spend"),
        ("endorsement.endorsement.spend", "endorsement_spend"),
    ]

    for title, label in ref_scripts:
        key = f"{label}_ref"
        if key in tx_hashes:
            print(f"  [{label}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
            continue

        safe_name = title.replace(".", "_")
        script_file = f"/tmp/m3deploy/{safe_name}.plutus"

        # Get current UTXO
        txin, balance = get_wallet_utxo(wallet_addr)

        # Calculate min UTXO for reference script
        code_size = len(applied_validators[title]["compiled_code"]) // 2
        min_lovelace = 2_000_000 + code_size * 4400
        min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

        print(f"  [{label}] Script size: {code_size} bytes, min UTXO: {min_lovelace / 1_000_000:.1f} AP3X")

        # Build tx
        cardano_cli(
            f"conway transaction build {NETWORK_FLAG} "
            f"--tx-in {txin} "
            f"--tx-out '{wallet_addr}+{min_lovelace}' "
            f"--tx-out-reference-script-file {script_file} "
            f"--change-address {wallet_addr} "
            f"--out-file /tmp/m3deploy/tx_{label}.raw"
        )

        # Sign
        cardano_cli(
            f"conway transaction sign {NETWORK_FLAG} "
            f"--tx-body-file /tmp/m3deploy/tx_{label}.raw "
            f"--signing-key-file {FEE_WALLET_DIR}/payment.skey "
            f"--out-file /tmp/m3deploy/tx_{label}.signed"
        )

        # Submit
        submit_tx(f"/tmp/m3deploy/tx_{label}.signed")

        # Get tx hash
        tx_hash = docker_exec(
            f"cardano-cli conway transaction txid "
            f"--tx-file /tmp/m3deploy/tx_{label}.signed"
        )
        tx_hashes[key] = tx_hash
        print(f"  [{label}] Deployed: {tx_hash}")

        # Save state after each tx
        save_state(state, tx_hashes, applied_validators, reputation_hash,
                   endorsement_hash, reputation_addr, endorsement_addr,
                   refs_policy_id, vkey_hash, wallet_addr, params_addr)

        print(f"  Waiting {TX_WAIT}s for confirmation...")
        time.sleep(TX_WAIT)

    # ── Step 6: Mint CrossValidatorRefs NFT ─────────────────────────────

    if "cross_refs_nft" not in tx_hashes:
        print("\n--- Step 6: Mint CrossValidatorRefs NFT ---")

        refs_datum = build_cross_refs_json(reputation_hash, endorsement_hash)
        write_json_to_docker("/tmp/m3deploy/cross_refs_datum.json", refs_datum)

        txin, balance = get_wallet_utxo(wallet_addr)

        # Token: 1 <refs_policy_id>.reputation_refs
        token_name_hex = b"reputation_refs".hex()
        mint_value = f"1 {refs_policy_id}.{token_name_hex}"

        # Send cross-refs NFT to WALLET address (not params_addr) so that
        # find_protocol_params doesn't accidentally pick up the 2-field
        # CrossValidatorRefs datum when searching for the 22-field ProtocolParams.
        cardano_cli(
            f"conway transaction build {NETWORK_FLAG} "
            f"--tx-in {txin} "
            f"--tx-out '{wallet_addr}+3000000+{mint_value}' "
            f"--tx-out-inline-datum-file /tmp/m3deploy/cross_refs_datum.json "
            f"--mint '{mint_value}' "
            f"--minting-script-file /tmp/m3deploy/refs_native_script.json "
            f"--change-address {wallet_addr} "
            f"--out-file /tmp/m3deploy/tx_refs.raw"
        )

        cardano_cli(
            f"conway transaction sign {NETWORK_FLAG} "
            f"--tx-body-file /tmp/m3deploy/tx_refs.raw "
            f"--signing-key-file {FEE_WALLET_DIR}/payment.skey "
            f"--out-file /tmp/m3deploy/tx_refs.signed"
        )

        submit_tx(f"/tmp/m3deploy/tx_refs.signed")

        tx_hash = docker_exec(
            f"cardano-cli conway transaction txid "
            f"--tx-file /tmp/m3deploy/tx_refs.signed"
        )
        tx_hashes["cross_refs_nft"] = tx_hash
        print(f"  CrossRefs NFT minted: {tx_hash}")

        save_state(state, tx_hashes, applied_validators, reputation_hash,
                   endorsement_hash, reputation_addr, endorsement_addr,
                   refs_policy_id, vkey_hash, wallet_addr, params_addr)
    else:
        print(f"\n--- Step 6: CrossRefs NFT already minted (skipping) ---")

    # ── Step 7: Final state ─────────────────────────────────────────────

    print("\n--- Step 7: Save Final State ---")

    # Save applied blueprint
    DEPLOY_DIR.mkdir(exist_ok=True)
    with open(DEPLOY_DIR / "plutus.json", "w") as f:
        json.dump(applied_bp, f, indent=2)

    # Save deployment.json
    deployment = {
        "version": "v1",
        "hashes": {
            "reputation": reputation_hash,
            "endorsement": endorsement_hash,
        },
        "addresses": {
            "reputation": reputation_addr,
            "endorsement": endorsement_addr,
        },
    }
    for _, label in ref_scripts:
        key = f"{label}_ref"
        if key in tx_hashes:
            deployment[f"{key}_utxo"] = f"{tx_hashes[key]}#0"
    if "params_utxo" in tx_hashes:
        deployment["params_utxo"] = f"{tx_hashes['params_utxo']}#0"
    if "cross_refs_nft" in tx_hashes:
        deployment["cross_refs_utxo"] = f"{tx_hashes['cross_refs_nft']}#0"
    with open(DEPLOY_DIR / "deployment.json", "w") as f:
        json.dump(deployment, f, indent=2)

    save_state(state, tx_hashes, applied_validators, reputation_hash,
               endorsement_hash, reputation_addr, endorsement_addr,
               refs_policy_id, vkey_hash, wallet_addr, params_addr)

    print(f"\n  Applied blueprint: deploy/plutus.json")
    print(f"  Deployment info:   deploy/deployment.json")
    print(f"  Full state:        deploy/deploy_state.json")

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print("=" * 60)

    # Summary
    print(f"\n  Reputation validator:  {reputation_hash}")
    print(f"  Endorsement validator: {endorsement_hash}")
    print(f"  Refs token policy:     {refs_policy_id}")
    print(f"  Params token policy:   {params_token_policy}")
    print(f"  Params holder:         {params_holder['hash']}")
    print(f"  Identity hash:         {inputs['identity_hash']}")
    for name, tx in tx_hashes.items():
        print(f"  {name}: {tx}")


def save_state(state, tx_hashes, applied_validators, reputation_hash,
               endorsement_hash, reputation_addr, endorsement_addr,
               refs_policy_id, vkey_hash, wallet_addr, params_addr):
    state.update({
        "network": "vector-testnet",
        "wallet_address": wallet_addr,
        "wallet_vkey_hash": vkey_hash,
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
    write_state(state)


def write_state(state: dict):
    DEPLOY_DIR.mkdir(exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    deploy()
