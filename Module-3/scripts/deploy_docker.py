#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Deploy via cardano-cli in Docker

Deploys Module 3 contracts to Vector testnet using the local cardano-node
running in a Docker container.

Steps:
  1. Compute NativeScript refs_token_policy from fee wallet vkey_hash
  2. Build ReputationConfig CBOR and apply to both validators
  3. Deploy 2 validator reference scripts (CIP-33)
  4. Create ProtocolParams datum UTXO at holder address
  5. Mint CrossValidatorRefs NFT + datum UTXO
  6. Save deployment state

Usage:
    python3 scripts/deploy_docker.py
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import cbor2
except ImportError:
    print("ERROR: cbor2 not installed. Run: pip install cbor2")
    sys.exit(1)

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

AGENT_REGISTRY_HASH = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
AP3X_POLICY_ID = ""       # Native currency (lovelace) on Vector
AP3X_ASSET_NAME = ""      # Empty for native currency

# Module 1 contract hashes
MODULE1_CLAIM_HASH = "6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda"
MODULE1_CHALLENGE_HASH = "781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc"
MODULE1_JURY_POOL_HASH = "b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962"
MODULE1_REFS_TOKEN_POLICY = "205d5f77ffebf60b764ba4f1873eff3764f3d1d594e5dac477a928f9"

# Holder script hashes (params=tag1, treasury=tag3) from shared/holder-scripts
# These are the same across all modules (same holder validator)
PARAMS_HOLDER_HASH = "f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867"
TREASURY_HOLDER_HASH = "ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6"

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


# ── ReputationConfig CBOR ────────────────────────────────────────────────────

def build_reputation_config_cbor(refs_token_policy: str, oracle_vkey_hash: str) -> str:
    """Build ReputationConfig as Plutus Data CBOR hex.
    Constructor 0 with 12 fields matching the Aiken type."""
    fields = [
        bytes.fromhex(refs_token_policy),           # refs_token_policy
        bytes.fromhex(AGENT_REGISTRY_HASH),         # registry_policy_id
        bytes.fromhex(AGENT_REGISTRY_HASH),         # registry_script_hash
        bytes.fromhex(MODULE1_CLAIM_HASH),          # audit_claim_validator_hash
        bytes.fromhex(MODULE1_CHALLENGE_HASH),      # audit_challenge_validator_hash
        bytes.fromhex(MODULE1_JURY_POOL_HASH),      # audit_jury_pool_hash
        bytes.fromhex(MODULE1_REFS_TOKEN_POLICY),   # audit_refs_token_policy
        bytes.fromhex(PARAMS_HOLDER_HASH),          # params_script_hash
        bytes.fromhex(TREASURY_HOLDER_HASH),        # treasury_script_hash
        bytes.fromhex(AP3X_POLICY_ID),              # ap3x_policy_id
        bytes.fromhex(AP3X_ASSET_NAME),             # ap3x_asset_name
        bytes.fromhex(oracle_vkey_hash),            # oracle_credential
    ]
    config_data = cbor2.CBORTag(121, fields)
    return cbor2.dumps(config_data).hex()


# ── ProtocolParams datum ─────────────────────────────────────────────────────

def build_protocol_params_json() -> dict:
    """Build ProtocolParams as cardano-cli JSON ScriptData.
    Constructor 0 with 22 fields."""
    valid_capabilities = [
        {"bytes": b.hex()} for b in [
            b"code_review", b"testing", b"deployment", b"documentation",
            b"security_audit", b"architecture", b"data_analysis", b"ml_training",
        ]
    ]
    return {
        "constructor": 0,
        "fields": [
            {"int": 10_000_000},     # min_self_stake (10 AP3X)
            {"int": 5_000_000},      # min_endorsement (5 AP3X)
            {"int": 25_000_000},     # min_challenge_stake (25 AP3X)
            {"int": 21_600},         # stake_cooldown (24h)
            {"int": 43_200},         # endorsement_cooldown (48h)
            {"int": 100},            # decay_rate (1%)
            {"int": 180},            # activity_window (180 epochs)
            {"int": 500},            # decay_collector_fee (5%)
            {"int": 1000},           # history_multiplier (10%)
            {"int": 3},              # max_endorsement_multiplier
            {"int": 5000},           # slash_rate_endorser (50%)
            {"int": 500},            # protocol_fee_rate (5%)
            {"int": 10_800},         # challenge_response_deadline (12h)
            {"int": 21_600},         # min_agent_age (24h)
            {"int": 5_400},          # escalation_window (6h)
            {"int": 100},            # default_judgment_fee (1%)
            {"int": 50},             # genesis_agent_cap
            {"int": 100_000_000},    # genesis_bonus_amount (100 AP3X)
            {"int": 604_800},        # genesis_minting_window (28 days)
            {"int": 129_600},        # genesis_protection_period (60 days)
            {"list": valid_capabilities},  # valid_capabilities
            {"int": 900},            # epoch_length (4h)
        ],
    }


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


def get_wallet_utxo(wallet_addr: str) -> tuple[str, int]:
    """Get the largest wallet UTXO (tx_hash, lovelace)."""
    output = cardano_cli(
        f"conway query utxo {NETWORK_FLAG} "
        f"--address {wallet_addr} "
        f"--out-file /dev/stdout"
    )
    utxos = json.loads(output)
    # Find largest
    best_txin = None
    best_lovelace = 0
    for txin, data in utxos.items():
        lovelace = data["value"]["lovelace"]
        if lovelace > best_lovelace:
            best_lovelace = lovelace
            best_txin = txin
    return best_txin, best_lovelace


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

    if not BLUEPRINT_PATH.exists():
        print("  ERROR: Blueprint not found. Run 'aiken build' in reputation-staking/")
        return

    # ── Step 1: Compute NativeScript policy ─────────────────────────────

    print("\n--- Step 1: NativeScript (refs_token_policy) ---")
    refs_policy_id, native_script_json = compute_native_script_policy(vkey_hash)
    print(f"  Policy ID: {refs_policy_id}")

    # Write native script to docker container
    docker_exec("mkdir -p /tmp/m3deploy")
    write_json_to_docker("/tmp/m3deploy/refs_native_script.json", native_script_json)

    # ── Step 2: Apply ReputationConfig ──────────────────────────────────

    print("\n--- Step 2: Apply ReputationConfig ---")
    config_cbor = build_reputation_config_cbor(refs_policy_id, vkey_hash)
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
    params_addr = script_hash_to_address(PARAMS_HOLDER_HASH)

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

    # ── Step 3: Deploy reference scripts (CIP-33) ───────────────────────

    print("\n--- Step 3: Deploy Reference Scripts ---")

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

    # ── Step 4: Create ProtocolParams datum UTXO ────────────────────────

    if "params_utxo" not in tx_hashes:
        print("\n--- Step 4: Create ProtocolParams UTXO ---")

        params_datum = build_protocol_params_json()
        write_json_to_docker("/tmp/m3deploy/params_datum.json", params_datum)

        txin, balance = get_wallet_utxo(wallet_addr)

        cardano_cli(
            f"conway transaction build {NETWORK_FLAG} "
            f"--tx-in {txin} "
            f"--tx-out '{params_addr}+3000000' "
            f"--tx-out-inline-datum-file /tmp/m3deploy/params_datum.json "
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
        print(f"  ProtocolParams UTXO: {tx_hash}")

        save_state(state, tx_hashes, applied_validators, reputation_hash,
                   endorsement_hash, reputation_addr, endorsement_addr,
                   refs_policy_id, vkey_hash, wallet_addr, params_addr)

        print(f"  Waiting {TX_WAIT}s for confirmation...")
        time.sleep(TX_WAIT)
    else:
        print(f"\n--- Step 4: ProtocolParams already deployed (skipping) ---")

    # ── Step 5: Mint CrossValidatorRefs NFT ─────────────────────────────

    if "cross_refs_nft" not in tx_hashes:
        print("\n--- Step 5: Mint CrossValidatorRefs NFT ---")

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
        print(f"\n--- Step 5: CrossRefs NFT already minted (skipping) ---")

    # ── Step 6: Final state ─────────────────────────────────────────────

    print("\n--- Step 6: Save Final State ---")

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
    print(f"  Params holder:         {PARAMS_HOLDER_HASH}")
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
    DEPLOY_DIR.mkdir(exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    deploy()
