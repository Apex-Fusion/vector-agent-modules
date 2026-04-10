#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Full End-to-End Smoke Test via Docker

Registers 2 agents, then exercises the full Module 3 lifecycle:
  1. Register Agent A + Agent B (with capabilities) via Agent Registry
  2. CreateStake for Agent A
  3. MintEndorsement from Agent B → Agent A
  4. MintChallenge from Agent B against Agent A
  5. ResolveChallenge (oracle — fee wallet = oracle)
  6. DistributeOutcome

Usage:
    python3 scripts/smoke_test_docker.py
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

try:
    import bech32
except ImportError:
    print("ERROR: bech32 not installed. Run: pip install bech32")
    sys.exit(1)


# ── Config ───────────────────────────────────────────────────────────────────

DOCKER_CONTAINER = "vector-public-testnet-tools-10_1_4-vector-relay-1"
SOCKET_PATH = "ipc/node.socket"
NETWORK_FLAG = "--mainnet"
DEV_WALLET_DIR = "/tmp/m3dev"  # Dev wallet (funded from fee wallet)

MODULE3_ROOT = Path(__file__).parent.parent
DEPLOY_DIR = MODULE3_ROOT / "deploy"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"
SMOKE_STATE_FILE = DEPLOY_DIR / "smoke_state.json"

# Agent Registry
REGISTRY_POLICY_ID = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"

# AP3X = native currency (lovelace on Vector). No separate policy/asset needed.
# Validators use empty policy_id + empty asset_name to check lovelace amounts.

TX_WAIT = 25  # seconds between transactions

# Reference script UTXOs (don't spend these)
REF_SCRIPT_TXINS = set()  # populated at startup

# Plutus V3 uses POSIX time (milliseconds) in the validity range, not slot numbers.
# Convert slot → POSIX ms using the Vector testnet genesis parameters.
SYSTEM_START_UNIX_S = 1752057484  # 2025-07-09T10:38:04Z
SLOT_LENGTH_S = 1  # 1 second per slot


def slot_to_posix_ms(slot: int) -> int:
    """Convert a slot number to POSIX time in milliseconds."""
    return (SYSTEM_START_UNIX_S + slot * SLOT_LENGTH_S) * 1000


# ── Docker exec helpers ──────────────────────────────────────────────────────

def docker_exec(cmd: str, check: bool = True) -> str:
    """Run a command inside the Docker container."""
    full_cmd = ["docker", "exec", DOCKER_CONTAINER, "bash", "-c", cmd]
    result = subprocess.run(full_cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        raise RuntimeError(f"Docker command failed: {cmd[:100]}...")
    return result.stdout.strip()


def cardano_cli(args: str) -> str:
    """Run cardano-cli inside Docker. Adds --socket-path for commands that need it."""
    needs_socket = any(
        kw in args for kw in ["query ", "transaction build ", "transaction submit "]
    )
    socket_arg = f" --socket-path {SOCKET_PATH}" if needs_socket else ""
    return docker_exec(f"cardano-cli {args}{socket_arg}")


def write_json_to_docker(remote_path: str, data):
    """Write a JSON file into the Docker container via docker cp."""
    local_tmp = f"/tmp/m3smoke_{Path(remote_path).name}"
    with open(local_tmp, "w") as f:
        json.dump(data, f)
    subprocess.run(
        ["docker", "cp", local_tmp, f"{DOCKER_CONTAINER}:{remote_path}"],
        check=True,
    )
    os.unlink(local_tmp)


def write_bytes_to_docker(remote_path: str, data: bytes):
    """Write raw bytes to Docker container via docker cp."""
    local_tmp = f"/tmp/m3smoke_{Path(remote_path).name}"
    with open(local_tmp, "wb") as f:
        f.write(data)
    subprocess.run(
        ["docker", "cp", local_tmp, f"{DOCKER_CONTAINER}:{remote_path}"],
        check=True,
    )
    os.unlink(local_tmp)


# ── Address helpers ──────────────────────────────────────────────────────────

def script_hash_to_address(script_hash_hex: str) -> str:
    """Convert script hash to mainnet bech32 address."""
    header = bytes([0x71]) + bytes.fromhex(script_hash_hex)
    converted = bech32.convertbits(header, 8, 5)
    return bech32.bech32_encode("addr", converted)


def vkey_hash_to_address(vkey_hash_hex: str) -> str:
    """Convert vkey hash to mainnet bech32 address (enterprise)."""
    header = bytes([0x61]) + bytes.fromhex(vkey_hash_hex)
    converted = bech32.convertbits(header, 8, 5)
    return bech32.bech32_encode("addr", converted)


# ── Wallet helpers ───────────────────────────────────────────────────────────

def get_wallet_address() -> str:
    return docker_exec(f"cat {DEV_WALLET_DIR}/payment.addr")


def get_wallet_vkey_hash() -> str:
    return docker_exec(
        f"cardano-cli conway address key-hash "
        f"--payment-verification-key-file {DEV_WALLET_DIR}/payment.vkey"
    )


def get_wallet_utxos(wallet_addr: str) -> dict:
    """Get all wallet UTXOs as dict."""
    output = cardano_cli(
        f"conway query utxo {NETWORK_FLAG} "
        f"--address {wallet_addr} --out-file /dev/stdout"
    )
    return json.loads(output)


def get_best_utxo(wallet_addr: str) -> tuple:
    """Get the largest spendable wallet UTXO (tx_hash#ix, lovelace).
    Skips UTXOs that hold reference scripts."""
    utxos = get_wallet_utxos(wallet_addr)
    best_txin = None
    best_lovelace = 0
    for txin, data in utxos.items():
        if txin in REF_SCRIPT_TXINS:
            continue
        lovelace = data["value"]["lovelace"]
        if lovelace > best_lovelace:
            best_lovelace = lovelace
            best_txin = txin
    return best_txin, best_lovelace


def get_utxo_at_address(addr: str) -> dict:
    """Query UTXOs at a script/other address."""
    output = cardano_cli(
        f"conway query utxo {NETWORK_FLAG} "
        f"--address {addr} --out-file /dev/stdout"
    )
    return json.loads(output)


def get_current_slot() -> int:
    """Get the current tip slot."""
    tip = json.loads(cardano_cli(f"conway query tip {NETWORK_FLAG}"))
    return tip["slot"]


def submit_tx(label: str, tx_file: str) -> str:
    """Submit a signed transaction and return tx hash."""
    cardano_cli(f"conway transaction submit {NETWORK_FLAG} --tx-file {tx_file}")
    tx_hash = docker_exec(
        f"cardano-cli conway transaction txid --tx-file {tx_file}"
    )
    print(f"  TX: {tx_hash}")
    return tx_hash


def get_collateral(wallet_addr: str) -> str:
    """Get a collateral UTXO (smallest spendable, pure lovelace preferred)."""
    utxos = get_wallet_utxos(wallet_addr)
    best = None
    best_lovelace = float("inf")
    for txin, data in utxos.items():
        if txin in REF_SCRIPT_TXINS:
            continue
        lovelace = data["value"]["lovelace"]
        # Prefer small UTXOs for collateral
        if lovelace < best_lovelace and lovelace >= 5_000_000:
            best_lovelace = lovelace
            best = txin
    return best or get_best_utxo(wallet_addr)[0]


def sign_and_submit(label: str, raw_file: str, signed_file: str) -> str:
    """Sign and submit a transaction."""
    cardano_cli(
        f"conway transaction sign {NETWORK_FLAG} "
        f"--tx-body-file {raw_file} "
        f"--signing-key-file {DEV_WALLET_DIR}/payment.skey "
        f"--out-file {signed_file}"
    )
    return submit_tx(label, signed_file)


# ── Token name derivation ───────────────────────────────────────────────────

def derive_token_name(prefix: bytes, data: bytes, slice_len: int = 27) -> bytes:
    """Derive a token name: prefix + blake2b_256(data)[:slice_len].
    Default slice_len=27: 5-byte prefix + 27 = 32 bytes (Cardano max)."""
    h = hashlib.blake2b(data, digest_size=32).digest()
    return prefix + h[:slice_len]


def derive_stake_token_name(agent_did_hex: str) -> str:
    """Derive stake token name hex."""
    return derive_token_name(b"rstk_", bytes.fromhex(agent_did_hex)).hex()


def derive_endorsement_token_name(endorser_did_hex: str, target_did_hex: str) -> str:
    """Derive endorsement token name hex."""
    pair = bytes.fromhex(endorser_did_hex) + bytes.fromhex(target_did_hex)
    return derive_token_name(b"rend_", pair).hex()


def derive_challenge_token_name(challenger_did_hex: str, target_did_hex: str, capability: str) -> str:
    """Derive challenge token name hex (slice_len=24 per Aiken)."""
    data = bytes.fromhex(challenger_did_hex) + bytes.fromhex(target_did_hex) + capability.encode()
    return derive_token_name(b"rchl_", data, slice_len=24).hex()


def derive_history_bonus_token_name(source_tx_hash: str, source_tx_ix: int) -> str:
    """Derive history bonus token name from source OutputReference.
    Uses builtin.serialise_data(oref) — Conway indefinite-length CBOR.
    V3: OutputReference = Constr(0, [ByteArray tx_hash, Int tx_ix])
    TransactionId is transparent (raw ByteArray), NOT wrapped in Constr."""
    tx_hash_bytes = bytes.fromhex(source_tx_hash)
    # Constr(0, [tx_hash, tx_ix]) — flat, no inner Constr for TransactionId
    oref_cbor = b"\xd8\x79\x9f" + cbor2.dumps(tx_hash_bytes) + cbor2.dumps(source_tx_ix) + b"\xff"
    return derive_token_name(b"hbonus_", oref_cbor, slice_len=24).hex()


# ── Agent Registry NFT name ─────────────────────────────────────────────────

def derive_agent_nft_name_conway(seed_tx_hash: str, seed_tx_ix: int) -> str:
    """Derive agent NFT asset name from seed OutputReference.
    Conway uses indefinite-length CBOR arrays (9F...FF encoding).

    OutputReference = Constr(0, [ByteString tx_hash, Int tx_ix])
    Conway CBOR: D8 79 (tag 121 = constructor 0) + 9F (indef array) + fields + FF (break)
    """
    tx_hash_bytes = bytes.fromhex(seed_tx_hash)
    # Constr(0, [tx_hash, tx_ix]) in Conway indefinite-length CBOR
    constr_cbor = b"\xd8\x79"  # tag 121 = constructor 0
    constr_cbor += b"\x9f"     # indefinite array start
    constr_cbor += cbor2.dumps(tx_hash_bytes)  # 5820 + 32 bytes
    constr_cbor += cbor2.dumps(seed_tx_ix)     # small int
    constr_cbor += b"\xff"     # break

    asset_name = hashlib.blake2b(constr_cbor, digest_size=32).hexdigest()
    return asset_name


# ── Datum builders (cardano-cli JSON ScriptData format) ──────────────────────

def vk_credential_json(vkey_hash: str) -> dict:
    """VerificationKeyCredential (constructor 0)."""
    return {"constructor": 0, "fields": [{"bytes": vkey_hash}]}


def build_agent_datum_json(owner_vkey_hash: str, name: str, description: str,
                           capabilities: list, framework: str, current_slot: int) -> dict:
    """Build AgentDatum for registration."""
    return {
        "constructor": 0,
        "fields": [
            vk_credential_json(owner_vkey_hash),
            {"bytes": name.encode().hex()},
            {"bytes": description.encode().hex()},
            {"list": [{"bytes": cap.encode().hex()} for cap in capabilities]},
            {"bytes": framework.encode().hex()},
            {"bytes": "".encode().hex()},  # endpoint (empty)
            {"int": current_slot * 1000},  # registered_at as POSIX ms
        ],
    }


def build_register_redeemer_json(seed_tx_hash: str, seed_tx_ix: int) -> dict:
    """Build Register { seed: OutputReference } redeemer."""
    return {
        "constructor": 0,  # Register
        "fields": [
            {
                "constructor": 0,  # OutputReference
                "fields": [
                    {"bytes": seed_tx_hash},
                    {"int": seed_tx_ix},
                ],
            }
        ],
    }


def build_stake_datum_json(agent_did: str, owner_vkey_hash: str,
                           stake_amount: int, capabilities: list,
                           current_slot: int) -> dict:
    """Build StakeDatum. last_updated uses POSIX ms (Plutus V3 validity range format)."""
    return {
        "constructor": 0,
        "fields": [
            {"bytes": agent_did},
            vk_credential_json(owner_vkey_hash),
            {"int": stake_amount},
            {"list": [{"bytes": cap.encode().hex()} for cap in capabilities]},
            {"int": slot_to_posix_ms(current_slot)},
            {"int": 0},  # history_points
        ],
    }


def build_endorsement_datum_json(endorser_did: str, endorser_vkey_hash: str,
                                  target_did: str, stake_amount: int,
                                  capabilities: list, current_slot: int) -> dict:
    """Build EndorsementDatum wrapped in EndorsementValidatorDatum.Endorsement.
    Aiken uses NESTED Constr encoding: Constr(0, [Constr(0, [6 fields])]).
    created_at uses POSIX ms (Plutus V3 validity range format)."""
    return {
        "constructor": 0,  # Endorsement variant
        "fields": [
            {
                "constructor": 0,  # Inner EndorsementDatum record
                "fields": [
                    {"bytes": endorser_did},
                    vk_credential_json(endorser_vkey_hash),
                    {"bytes": target_did},
                    {"int": stake_amount},
                    {"list": [{"bytes": cap.encode().hex()} for cap in capabilities]},
                    {"int": slot_to_posix_ms(current_slot)},
                ],
            },
        ],
    }


def build_challenge_datum_json(challenger_did: str, challenger_vkey_hash: str,
                                target_did: str, target_vkey_hash: str,
                                capability: str, stake_amount: int,
                                evidence_hash: str, evidence_uri: str,
                                current_slot: int) -> dict:
    """Build ReputationChallengeDatum wrapped in EndorsementValidatorDatum.Challenge.
    Aiken uses NESTED Constr encoding: Constr(1, [Constr(0, [13 fields])]).
    created_at uses POSIX ms (Plutus V3 validity range format)."""
    return {
        "constructor": 1,  # Challenge variant
        "fields": [
            {
                "constructor": 0,  # Inner ReputationChallengeDatum record
                "fields": [
                    {"bytes": challenger_did},
                    vk_credential_json(challenger_vkey_hash),
                    {"bytes": target_did},
                    vk_credential_json(target_vkey_hash),
                    {"bytes": capability.encode().hex()},
                    {"int": stake_amount},
                    {"bytes": evidence_hash},
                    {"bytes": evidence_uri.encode().hex()},
                    {"int": slot_to_posix_ms(current_slot)},
                    {"bytes": ""},  # counter_evidence_hash (empty)
                    {"bytes": ""},  # counter_evidence_uri (empty)
                    {"int": 0},     # response_submitted_at
                    {"constructor": 0, "fields": []},  # Open state
                ],
            },
        ],
    }


def build_resolved_challenge_datum_json(original_datum: dict, outcome_constructor: int) -> dict:
    """Update challenge datum to Resolved state.
    With nested encoding, inner fields are at fields[0]['fields']."""
    new_datum = json.loads(json.dumps(original_datum))
    # Inner ReputationChallengeDatum is at fields[0], state is inner field[12]
    new_datum["fields"][0]["fields"][12] = {
        "constructor": 2,  # Resolved
        "fields": [
            {"constructor": outcome_constructor, "fields": []}  # 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive
        ],
    }
    return new_datum


# ── Redeemer builders ────────────────────────────────────────────────────────

def redeemer_json(constructor: int, fields=None) -> dict:
    """Build a simple redeemer."""
    return {"constructor": constructor, "fields": fields or []}


# Reputation mint redeemers
MINT_STAKE_TOKEN = redeemer_json(0)       # MintStakeToken
BURN_STAKE_TOKEN = redeemer_json(1)       # BurnStakeToken

# Reputation spend redeemers
CREATE_STAKE = redeemer_json(0)           # CreateStake

# Endorsement mint redeemers
MINT_ENDORSEMENT_TOKEN = redeemer_json(0) # MintEndorsementToken
BURN_ENDORSEMENT_TOKEN = redeemer_json(1) # BurnEndorsementToken
MINT_CHALLENGE_TOKEN = redeemer_json(2)   # MintChallengeToken
BURN_CHALLENGE_TOKEN = redeemer_json(3)   # BurnChallengeToken

# Endorsement spend redeemers
# EndorsementSpend(WithdrawEndorsement)
WITHDRAW_ENDORSEMENT = {"constructor": 0, "fields": [{"constructor": 1, "fields": []}]}

# ChallengeSpend(ResolveChallenge { outcome })
def resolve_challenge_redeemer(outcome_constructor: int) -> dict:
    """ChallengeSpend(ResolveChallenge { outcome })."""
    return {
        "constructor": 1,  # ChallengeSpend
        "fields": [
            {
                "constructor": 4,  # ResolveChallenge
                "fields": [
                    {"constructor": outcome_constructor, "fields": []}
                ],
            }
        ],
    }


# ChallengeSpend(DistributeOutcome)
DISTRIBUTE_OUTCOME = {
    "constructor": 1,  # ChallengeSpend
    "fields": [
        {"constructor": 6, "fields": []}  # DistributeOutcome
    ],
}


# ── Main smoke test ──────────────────────────────────────────────────────────

def smoke_test():
    print("=" * 60)
    print("Module 3: Full End-to-End Smoke Test")
    print("=" * 60)

    # Load deploy state
    if not DEPLOY_STATE_FILE.exists():
        print("ERROR: Deploy state not found. Run deploy_docker.py first.")
        sys.exit(1)
    with open(DEPLOY_STATE_FILE) as f:
        deploy = json.load(f)

    # Load or init smoke state
    smoke = {}
    if SMOKE_STATE_FILE.exists():
        with open(SMOKE_STATE_FILE) as f:
            smoke = json.load(f)

    # Extract deploy info
    wallet_addr = deploy["wallet_address"]
    vkey_hash = deploy["wallet_vkey_hash"]
    reputation_hash = deploy["reputation_validator_hash"]
    endorsement_hash = deploy["endorsement_validator_hash"]
    refs_policy = deploy["refs_token_policy"]
    params_holder_addr = deploy["params_holder_address"]
    reputation_addr = deploy["reputation_address"]
    endorsement_addr = deploy["endorsement_address"]
    registry_addr = script_hash_to_address(REGISTRY_POLICY_ID)

    # Verify environment
    print("\n--- Environment Check ---")
    tip = json.loads(cardano_cli(f"conway query tip {NETWORK_FLAG}"))
    current_slot = tip["slot"]
    print(f"  Node: slot {current_slot}, sync {tip['syncProgress']}")

    txin, balance = get_best_utxo(wallet_addr)
    print(f"  Wallet: {balance / 1_000_000:.2f} AP3X")
    print(f"  Reputation: {reputation_hash[:16]}... at {reputation_addr[:40]}...")
    print(f"  Endorsement: {endorsement_hash[:16]}... at {endorsement_addr[:40]}...")

    docker_exec("mkdir -p /tmp/m3smoke")

    # Mark reference script UTXOs as non-spendable and track for reference usage
    tx_hashes = deploy.get("tx_hashes", {})
    reputation_ref_utxo = None
    endorsement_ref_utxo = None
    for key in ["reputation_spend_ref", "endorsement_spend_ref"]:
        if key in tx_hashes:
            ref_utxo = f"{tx_hashes[key]}#0"
            REF_SCRIPT_TXINS.add(ref_utxo)
            if key == "reputation_spend_ref":
                reputation_ref_utxo = ref_utxo
            else:
                endorsement_ref_utxo = ref_utxo

    # Use deploy state tx hashes to find exact UTXOs from current deployment
    cross_refs_tx = tx_hashes.get("cross_refs_nft", "")
    params_tx = tx_hashes.get("params_utxo", "")

    # Params UTXO: use deploy state tx hash if available
    params_utxo = f"{params_tx}#0" if params_tx else None
    if not params_utxo:
        params_utxos = get_utxo_at_address(params_holder_addr)
        for utxo_id, info in params_utxos.items():
            if "inlineDatum" in info:
                datum = info.get("inlineDatum", {})
                if isinstance(datum, dict) and "fields" in datum and len(datum["fields"]) == 22:
                    params_utxo = utxo_id

    # Cross-refs UTXO: use deploy state tx hash if available
    cross_refs_utxo = f"{cross_refs_tx}#0" if cross_refs_tx else None
    if not cross_refs_utxo:
        wallet_utxos = get_utxo_at_address(wallet_addr)
        for utxo_id, info in wallet_utxos.items():
            for pid in info["value"]:
                if pid == refs_policy:
                    cross_refs_utxo = utxo_id
                    break
            if cross_refs_utxo:
                break

    if not cross_refs_utxo:
        print("  ERROR: CrossValidatorRefs UTXO not found!")
        sys.exit(1)
    if not params_utxo:
        print("  ERROR: ProtocolParams UTXO not found!")
        sys.exit(1)
    # Don't accidentally spend the cross-refs UTXO (it's at the wallet address)
    REF_SCRIPT_TXINS.add(cross_refs_utxo)
    print(f"  CrossRefs: {cross_refs_utxo}")
    print(f"  Params:    {params_utxo}")

    # We need the registry script file for minting agent NFTs
    # Check both possible locations
    registry_bp_path = None
    for candidate in [
        Path("/home/sisyphos/ai-sprint-2/vector-ai-agents/agent-registry/deploy/agent-registry/plutus.json"),
        MODULE3_ROOT.parent / "vector-ai-agents" / "agent-registry" / "deploy" / "agent-registry" / "plutus.json",
    ]:
        if candidate.exists():
            registry_bp_path = candidate
            break
    if not registry_bp_path:
        print(f"  ERROR: Agent Registry blueprint not found")
        sys.exit(1)

    # Extract registry compiled code and write to docker
    with open(registry_bp_path) as f:
        reg_bp = json.load(f)
    reg_code = None
    for v in reg_bp["validators"]:
        if "spend" in v["title"]:
            reg_code = v["compiledCode"]
            break
    if not reg_code:
        print("  ERROR: Could not find registry validator code")
        sys.exit(1)

    # Double-CBOR wrap for cardano-cli
    reg_envelope = {
        "type": "PlutusScriptV3",
        "description": "Agent Registry",
        "cborHex": cbor2.dumps(bytes.fromhex(reg_code)).hex(),
    }
    write_json_to_docker("/tmp/m3smoke/registry.plutus", reg_envelope)

    # Also write reputation and endorsement scripts
    deploy_bp_path = DEPLOY_DIR / "plutus.json"
    if deploy_bp_path.exists():
        with open(deploy_bp_path) as f:
            mod3_bp = json.load(f)
    else:
        print("  ERROR: Applied Module 3 blueprint not found in deploy/")
        sys.exit(1)

    for v in mod3_bp["validators"]:
        title = v["title"]
        if ".else" in title:
            continue
        safe = title.replace(".", "_")
        envelope = {
            "type": "PlutusScriptV3",
            "description": title,
            "cborHex": cbor2.dumps(bytes.fromhex(v["compiledCode"])).hex(),
        }
        write_json_to_docker(f"/tmp/m3smoke/{safe}.plutus", envelope)

    passed = 0
    failed = 0

    def collateral_arg():
        """Return --tx-in-collateral flag using a spendable wallet UTXO."""
        c = get_collateral(wallet_addr)
        return f"--tx-in-collateral {c} " if c else ""

    def step_pass(label):
        nonlocal passed
        passed += 1
        print(f"  PASS")

    def step_fail(label, err):
        nonlocal failed
        failed += 1
        print(f"  FAIL: {err}")

    # ── Step 1: Register Agent A ─────────────────────────────────────────

    if "agent_a_did" not in smoke:
        print("\n--- Step 1: Register Agent A ---")
        try:
            txin, balance = get_best_utxo(wallet_addr)
            seed_tx_hash = txin.split("#")[0]
            seed_tx_ix = int(txin.split("#")[1])

            agent_a_did = derive_agent_nft_name_conway(seed_tx_hash, seed_tx_ix)
            print(f"  Seed UTXO: {txin}")
            print(f"  Agent A DID: {agent_a_did[:32]}...")

            # Build datum
            datum_a = build_agent_datum_json(
                vkey_hash, "SmokeTestAgentA", "Smoke test agent A for Module 3",
                ["code_review", "testing", "deployment"], "TestFramework", current_slot
            )
            write_json_to_docker("/tmp/m3smoke/agent_a_datum.json", datum_a)

            # Build redeemer
            redeemer_a = build_register_redeemer_json(seed_tx_hash, seed_tx_ix)
            write_json_to_docker("/tmp/m3smoke/register_a_redeemer.json", redeemer_a)

            mint_value = f"1 {REGISTRY_POLICY_ID}.{agent_a_did}"

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--tx-out '{registry_addr}+10000000+{mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/agent_a_datum.json "
                f"--mint '{mint_value}' "
                f"--mint-script-file /tmp/m3smoke/registry.plutus "
                f"--mint-redeemer-file /tmp/m3smoke/register_a_redeemer.json "
                f"--required-signer-hash {vkey_hash} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_reg_a.raw"
            )

            tx_hash = sign_and_submit("register_a", "/tmp/m3smoke/tx_reg_a.raw", "/tmp/m3smoke/tx_reg_a.signed")
            smoke["agent_a_did"] = agent_a_did
            smoke["agent_a_tx"] = tx_hash
            smoke["agent_a_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("Register Agent A")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("Register Agent A", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 1: Agent A already registered (DID: {smoke['agent_a_did'][:32]}...) ---")
        passed += 1

    # ── Step 2: Register Agent B ─────────────────────────────────────────

    if "agent_b_did" not in smoke:
        print("\n--- Step 2: Register Agent B ---")
        try:
            txin, balance = get_best_utxo(wallet_addr)
            seed_tx_hash = txin.split("#")[0]
            seed_tx_ix = int(txin.split("#")[1])

            agent_b_did = derive_agent_nft_name_conway(seed_tx_hash, seed_tx_ix)
            print(f"  Seed UTXO: {txin}")
            print(f"  Agent B DID: {agent_b_did[:32]}...")

            datum_b = build_agent_datum_json(
                vkey_hash, "SmokeTestAgentB", "Smoke test agent B for Module 3",
                ["code_review", "testing"], "TestFramework", current_slot
            )
            write_json_to_docker("/tmp/m3smoke/agent_b_datum.json", datum_b)

            redeemer_b = build_register_redeemer_json(seed_tx_hash, seed_tx_ix)
            write_json_to_docker("/tmp/m3smoke/register_b_redeemer.json", redeemer_b)

            mint_value = f"1 {REGISTRY_POLICY_ID}.{agent_b_did}"

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--tx-out '{registry_addr}+10000000+{mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/agent_b_datum.json "
                f"--mint '{mint_value}' "
                f"--mint-script-file /tmp/m3smoke/registry.plutus "
                f"--mint-redeemer-file /tmp/m3smoke/register_b_redeemer.json "
                f"--required-signer-hash {vkey_hash} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_reg_b.raw"
            )

            tx_hash = sign_and_submit("register_b", "/tmp/m3smoke/tx_reg_b.raw", "/tmp/m3smoke/tx_reg_b.signed")
            smoke["agent_b_did"] = agent_b_did
            smoke["agent_b_tx"] = tx_hash
            smoke["agent_b_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("Register Agent B")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("Register Agent B", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 2: Agent B already registered (DID: {smoke['agent_b_did'][:32]}...) ---")
        passed += 1

    agent_a_did = smoke["agent_a_did"]
    agent_b_did = smoke["agent_b_did"]

    # ── Step 3a: Create seed UTXO at reputation address ────────────────

    if "seed_utxo_tx" not in smoke:
        print("\n--- Step 3a: Create seed UTXO at reputation address ---")
        try:
            current_slot = get_current_slot()
            # Dummy StakeDatum for the seed UTXO (will be consumed by CreateStake)
            seed_datum = build_stake_datum_json(
                agent_a_did, vkey_hash, 2_000_000,
                ["code_review"], current_slot
            )
            write_json_to_docker("/tmp/m3smoke/seed_datum.json", seed_datum)

            txin, balance = get_best_utxo(wallet_addr)

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--tx-out '{reputation_addr}+2000000' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/seed_datum.json "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_seed.raw"
            )

            tx_hash = sign_and_submit("seed_utxo", "/tmp/m3smoke/tx_seed.raw", "/tmp/m3smoke/tx_seed.signed")
            smoke["seed_utxo_tx"] = tx_hash
            smoke["seed_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("Seed UTXO")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("Seed UTXO", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 3a: Seed UTXO already created ({smoke['seed_utxo_tx'][:16]}...) ---")
        passed += 1

    # ── Step 3b: CreateStake for Agent A ─────────────────────────────────

    if "create_stake_tx" not in smoke:
        print("\n--- Step 3b: CreateStake (Agent A, 10 AP3X) ---")
        try:
            current_slot = get_current_slot()
            stake_amount = 10_000_000  # 10 AP3X

            # Derive stake token name
            stake_token_name = derive_stake_token_name(agent_a_did)
            stake_mint_value = f"1 {reputation_hash}.{stake_token_name}"

            # Find Agent A's registry UTXO for reference input
            agent_a_reg_utxo = find_agent_registry_utxo(agent_a_did)
            if not agent_a_reg_utxo:
                raise RuntimeError("Agent A registry UTXO not found on-chain")
            print(f"  Agent A registry UTXO: {agent_a_reg_utxo}")

            # Find the seed UTXO at reputation address
            seed_utxo_id = smoke["seed_utxo"]
            print(f"  Seed UTXO: {seed_utxo_id}")

            # Build StakeDatum
            stake_datum = build_stake_datum_json(
                agent_a_did, vkey_hash, stake_amount,
                ["code_review", "testing"], current_slot
            )
            write_json_to_docker("/tmp/m3smoke/stake_datum.json", stake_datum)

            # Redeemers
            write_json_to_docker("/tmp/m3smoke/create_stake_redeemer.json", CREATE_STAKE)
            write_json_to_docker("/tmp/m3smoke/mint_stake_redeemer.json", MINT_STAKE_TOKEN)

            txin, balance = get_best_utxo(wallet_addr)

            # Spend the seed UTXO (script input) + wallet UTXO (fee + stake funds)
            # Output: stake UTXO at reputation addr with stake token
            # Use the same script file for both spend and mint (same multi-validator)
            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--tx-in {seed_utxo_id} "
                f"--tx-in-script-file /tmp/m3smoke/reputation_reputation_spend.plutus "
                f"--tx-in-inline-datum-present "
                f"--tx-in-redeemer-file /tmp/m3smoke/create_stake_redeemer.json "
                f"--read-only-tx-in-reference {agent_a_reg_utxo} "
                f"--read-only-tx-in-reference {params_utxo} "
                f"--read-only-tx-in-reference {cross_refs_utxo} "
                f"--tx-out '{reputation_addr}+{stake_amount}+{stake_mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/stake_datum.json "
                f"--mint '{stake_mint_value}' "
                f"--mint-script-file /tmp/m3smoke/reputation_reputation_spend.plutus "
                f"--mint-redeemer-file /tmp/m3smoke/mint_stake_redeemer.json "
                f"--required-signer-hash {vkey_hash} "
                f"--invalid-before {current_slot} "
                f"--invalid-hereafter {current_slot + 600} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_stake.raw"
            )

            tx_hash = sign_and_submit("create_stake", "/tmp/m3smoke/tx_stake.raw", "/tmp/m3smoke/tx_stake.signed")
            smoke["create_stake_tx"] = tx_hash
            smoke["stake_utxo"] = f"{tx_hash}#0"
            smoke["stake_token_name"] = stake_token_name
            save_smoke_state(smoke)
            step_pass("CreateStake")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("CreateStake", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 3: CreateStake already done ({smoke['create_stake_tx'][:16]}...) ---")
        passed += 1

    # ── Step 4: MintEndorsement from Agent B → Agent A ───────────────────

    if "mint_endorsement_tx" not in smoke:
        print("\n--- Step 4: MintEndorsement (B endorses A, 5 AP3X) ---")
        try:
            current_slot = get_current_slot()
            endorsement_amount = 5_000_000  # 5 AP3X

            endorsement_token_name = derive_endorsement_token_name(agent_b_did, agent_a_did)
            endorse_mint_value = f"1 {endorsement_hash}.{endorsement_token_name}"

            # Reference inputs: Agent B registry, Agent A registry, Agent A stake, params, cross-refs
            agent_a_reg_utxo = find_agent_registry_utxo(agent_a_did)
            agent_b_reg_utxo = find_agent_registry_utxo(agent_b_did)
            stake_utxo = find_stake_utxo(agent_a_did, reputation_addr, reputation_hash)

            if not agent_a_reg_utxo or not agent_b_reg_utxo:
                raise RuntimeError("Agent registry UTXOs not found")
            if not stake_utxo:
                raise RuntimeError("Agent A stake UTXO not found")

            print(f"  Agent A registry: {agent_a_reg_utxo}")
            print(f"  Agent B registry: {agent_b_reg_utxo}")
            print(f"  Agent A stake:    {stake_utxo}")

            endorsement_datum = build_endorsement_datum_json(
                agent_b_did, vkey_hash, agent_a_did,
                endorsement_amount, ["code_review"], current_slot
            )
            write_json_to_docker("/tmp/m3smoke/endorsement_datum.json", endorsement_datum)
            write_json_to_docker("/tmp/m3smoke/mint_endorsement_redeemer.json", MINT_ENDORSEMENT_TOKEN)

            txin, balance = get_best_utxo(wallet_addr)

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--read-only-tx-in-reference {agent_a_reg_utxo} "
                f"--read-only-tx-in-reference {agent_b_reg_utxo} "
                f"--read-only-tx-in-reference {stake_utxo} "
                f"--read-only-tx-in-reference {params_utxo} "
                f"--read-only-tx-in-reference {cross_refs_utxo} "
                f"--tx-out '{endorsement_addr}+{endorsement_amount}+{endorse_mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/endorsement_datum.json "
                f"--mint '{endorse_mint_value}' "
                f"--mint-script-file /tmp/m3smoke/endorsement_endorsement_mint.plutus "
                f"--mint-redeemer-file /tmp/m3smoke/mint_endorsement_redeemer.json "
                f"--required-signer-hash {vkey_hash} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_endorse.raw"
            )

            tx_hash = sign_and_submit("mint_endorsement", "/tmp/m3smoke/tx_endorse.raw", "/tmp/m3smoke/tx_endorse.signed")
            smoke["mint_endorsement_tx"] = tx_hash
            smoke["endorsement_utxo"] = f"{tx_hash}#0"
            smoke["endorsement_token_name"] = endorsement_token_name
            save_smoke_state(smoke)
            step_pass("MintEndorsement")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("MintEndorsement", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 4: MintEndorsement already done ({smoke['mint_endorsement_tx'][:16]}...) ---")
        passed += 1

    # ── Step 5: MintChallenge from Agent B against Agent A ───────────────

    if "mint_challenge_tx" not in smoke:
        print("\n--- Step 5: MintChallenge (B challenges A on code_review, 25 AP3X) ---")
        try:
            current_slot = get_current_slot()
            challenge_amount = 25_000_000  # 25 AP3X

            challenge_token_name = derive_challenge_token_name(agent_b_did, agent_a_did, "code_review")
            challenge_mint_value = f"1 {endorsement_hash}.{challenge_token_name}"

            # Reference inputs
            agent_a_reg_utxo = find_agent_registry_utxo(agent_a_did)
            agent_b_reg_utxo = find_agent_registry_utxo(agent_b_did)
            stake_utxo = find_stake_utxo(agent_a_did, reputation_addr, reputation_hash)

            if not agent_a_reg_utxo or not agent_b_reg_utxo:
                raise RuntimeError("Agent registry UTXOs not found")
            if not stake_utxo:
                raise RuntimeError("Agent A stake UTXO not found")

            # Evidence hash: 32 bytes
            evidence_data = b"smoke_test_evidence_for_code_review_challenge"
            evidence_hash = hashlib.blake2b(evidence_data, digest_size=32).hexdigest()

            challenge_datum = build_challenge_datum_json(
                agent_b_did, vkey_hash, agent_a_did, vkey_hash,
                "code_review", challenge_amount,
                evidence_hash, "ipfs://smoke-test-evidence",
                current_slot
            )
            write_json_to_docker("/tmp/m3smoke/challenge_datum.json", challenge_datum)
            write_json_to_docker("/tmp/m3smoke/mint_challenge_redeemer.json", MINT_CHALLENGE_TOKEN)

            txin, balance = get_best_utxo(wallet_addr)

            # MintChallenge requires validity range for min_agent_age check
            # The validator checks: validity_lower >= registered_at + min_agent_age
            # Since we just registered, we need to set lower bound to a reasonable value
            # min_agent_age = 21600 slots. Our agent was registered at current_slot.
            # For testnet, the params might be lenient. Let's try with a wide validity range.

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--read-only-tx-in-reference {agent_a_reg_utxo} "
                f"--read-only-tx-in-reference {agent_b_reg_utxo} "
                f"--read-only-tx-in-reference {stake_utxo} "
                f"--read-only-tx-in-reference {params_utxo} "
                f"--read-only-tx-in-reference {cross_refs_utxo} "
                f"--tx-out '{endorsement_addr}+{challenge_amount}+{challenge_mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/challenge_datum.json "
                f"--mint '{challenge_mint_value}' "
                f"--mint-script-file /tmp/m3smoke/endorsement_endorsement_mint.plutus "
                f"--mint-redeemer-file /tmp/m3smoke/mint_challenge_redeemer.json "
                f"--required-signer-hash {vkey_hash} "
                f"--invalid-before {current_slot} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_challenge.raw"
            )

            tx_hash = sign_and_submit("mint_challenge", "/tmp/m3smoke/tx_challenge.raw", "/tmp/m3smoke/tx_challenge.signed")
            smoke["mint_challenge_tx"] = tx_hash
            smoke["challenge_utxo"] = f"{tx_hash}#0"
            smoke["challenge_token_name"] = challenge_token_name
            smoke["challenge_datum"] = challenge_datum
            save_smoke_state(smoke)
            step_pass("MintChallenge")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("MintChallenge", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 5: MintChallenge already done ({smoke['mint_challenge_tx'][:16]}...) ---")
        passed += 1

    # ── Step 6: ResolveChallenge (Oracle — CapabilityVerified) ───────────

    if "resolve_challenge_tx" not in smoke:
        print("\n--- Step 6: ResolveChallenge (Oracle: CapabilityVerified) ---")
        try:
            current_slot = get_current_slot()

            # Find the challenge UTXO at endorsement address
            challenge_utxo_id = find_challenge_utxo(
                endorsement_addr, endorsement_hash, smoke["challenge_token_name"]
            )
            if not challenge_utxo_id:
                raise RuntimeError("Challenge UTXO not found on-chain")
            print(f"  Challenge UTXO: {challenge_utxo_id}")

            # Read the challenge datum from chain to build updated version
            challenge_datum = smoke.get("challenge_datum")
            if not challenge_datum:
                raise RuntimeError("Challenge datum not in smoke state")

            # Build resolved datum (CapabilityVerified = constructor 0)
            resolved_datum = build_resolved_challenge_datum_json(challenge_datum, 0)
            write_json_to_docker("/tmp/m3smoke/resolved_datum.json", resolved_datum)

            # Redeemer: ChallengeSpend(ResolveChallenge { CapabilityVerified })
            resolve_redeemer = resolve_challenge_redeemer(0)
            write_json_to_docker("/tmp/m3smoke/resolve_redeemer.json", resolve_redeemer)

            txin, balance = get_best_utxo(wallet_addr)

            # The challenge UTXO is spent (endorsement spend handler)
            # Output: updated challenge UTXO with Resolved state
            challenge_amount = 25_000_000
            challenge_mint_value_str = f"1 {endorsement_hash}.{smoke['challenge_token_name']}"

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--tx-in {challenge_utxo_id} "
                f"--tx-in-script-file /tmp/m3smoke/endorsement_endorsement_spend.plutus "
                f"--tx-in-inline-datum-present "
                f"--tx-in-redeemer-file /tmp/m3smoke/resolve_redeemer.json "
                f"--read-only-tx-in-reference {params_utxo} "
                f"--read-only-tx-in-reference {cross_refs_utxo} "
                f"--tx-out '{endorsement_addr}+{challenge_amount}+{challenge_mint_value_str}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/resolved_datum.json "
                f"--required-signer-hash {vkey_hash} "
                f"--invalid-before {current_slot} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_resolve.raw"
            )

            tx_hash = sign_and_submit("resolve_challenge", "/tmp/m3smoke/tx_resolve.raw", "/tmp/m3smoke/tx_resolve.signed")
            smoke["resolve_challenge_tx"] = tx_hash
            smoke["resolved_challenge_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("ResolveChallenge")

            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("ResolveChallenge", e)
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 6: ResolveChallenge already done ({smoke['resolve_challenge_tx'][:16]}...) ---")
        passed += 1

    # ── Step 7: DistributeOutcome ────────────────────────────────────────

    if "distribute_outcome_tx" not in smoke:
        print("\n--- Step 7: DistributeOutcome (CapabilityVerified) ---")
        try:
            current_slot = get_current_slot()

            # Find resolved challenge UTXO
            resolved_utxo_id = find_challenge_utxo(
                endorsement_addr, endorsement_hash, smoke["challenge_token_name"]
            )
            if not resolved_utxo_id:
                raise RuntimeError("Resolved challenge UTXO not found")
            print(f"  Resolved challenge: {resolved_utxo_id}")

            write_json_to_docker("/tmp/m3smoke/distribute_redeemer.json", DISTRIBUTE_OUTCOME)

            txin, balance = get_best_utxo(wallet_addr)

            # CapabilityVerified outcome:
            # - Challenger loses stake → goes to target
            # - protocol_fee = challenge_stake * protocol_fee_rate / 10000
            #   = 25_000_000 * 500 / 10000 = 1_250_000
            # - target gets: challenge_stake - protocol_fee = 23_750_000
            # - treasury gets: protocol_fee = 1_250_000
            challenge_amount = 25_000_000
            protocol_fee = challenge_amount * 500 // 10000  # 5%
            target_payout = challenge_amount - protocol_fee

            target_addr = vkey_hash_to_address(vkey_hash)  # target = Agent A owner = dev wallet
            treasury_addr = script_hash_to_address("ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6")

            # Burn challenge token (endorsement policy)
            burn_value = f"-1 {endorsement_hash}.{smoke['challenge_token_name']}"

            # Mint history bonus token (reputation policy) — required for CapabilityVerified
            # source_ref = the resolved challenge UTXO being spent
            resolved_tx_hash = resolved_utxo_id.split("#")[0]
            resolved_tx_ix = int(resolved_utxo_id.split("#")[1])
            hbonus_token_name = derive_history_bonus_token_name(resolved_tx_hash, resolved_tx_ix)
            hbonus_mint_value = f"1 {reputation_hash}.{hbonus_token_name}"

            # History bonus datum: HistoryBonusDatum
            # agent_did = target (Agent A — the one whose capability was verified)
            hbonus_datum = {
                "constructor": 0,
                "fields": [
                    {"bytes": smoke["agent_a_did"]},
                    {"constructor": 0, "fields": []},  # ChallengeWon
                    {"int": 0},  # bonus_points (nominal)
                    {  # source_ref: OutputReference (V3: TransactionId is raw ByteArray)
                        "constructor": 0,
                        "fields": [
                            {"bytes": resolved_tx_hash},
                            {"int": resolved_tx_ix},
                        ],
                    },
                    {"int": slot_to_posix_ms(current_slot)},  # created_at
                ],
            }
            write_json_to_docker("/tmp/m3smoke/hbonus_datum.json", hbonus_datum)

            # MintHistoryBonus redeemer (constructor 2 of ReputationMintAction)
            mint_hbonus_redeemer = redeemer_json(2)  # MintHistoryBonus
            write_json_to_docker("/tmp/m3smoke/mint_hbonus_redeemer.json", mint_hbonus_redeemer)

            # Write burn redeemer
            write_json_to_docker("/tmp/m3smoke/burn_challenge_redeemer.json", BURN_CHALLENGE_TOKEN)

            # Agent A registry reference input (for agent_exists check in history bonus mint)
            agent_a_reg_utxo = smoke.get("agent_a_utxo")

            # Use reference scripts to stay under tx size limit (two scripts inline > 16KB)
            combined_mint = f"{burn_value} + {hbonus_mint_value}"
            if endorsement_ref_utxo and reputation_ref_utxo:
                spend_args = (
                    f"--spending-tx-in-reference {endorsement_ref_utxo} "
                    f"--spending-plutus-script-v3 "
                    f"--spending-reference-tx-in-inline-datum-present "
                    f"--spending-reference-tx-in-redeemer-file /tmp/m3smoke/distribute_redeemer.json "
                )
                mint_args = (
                    f"--mint '{combined_mint}' "
                    f"--mint-tx-in-reference {endorsement_ref_utxo} "
                    f"--mint-plutus-script-v3 "
                    f"--mint-reference-tx-in-redeemer-file /tmp/m3smoke/burn_challenge_redeemer.json "
                    f"--policy-id {endorsement_hash} "
                    f"--mint-tx-in-reference {reputation_ref_utxo} "
                    f"--mint-plutus-script-v3 "
                    f"--mint-reference-tx-in-redeemer-file /tmp/m3smoke/mint_hbonus_redeemer.json "
                    f"--policy-id {reputation_hash} "
                )
            else:
                spend_args = (
                    f"--tx-in-script-file /tmp/m3smoke/endorsement_endorsement_spend.plutus "
                    f"--tx-in-inline-datum-present "
                    f"--tx-in-redeemer-file /tmp/m3smoke/distribute_redeemer.json "
                )
                mint_args = (
                    f"--mint '{combined_mint}' "
                    f"--mint-script-file /tmp/m3smoke/endorsement_endorsement_mint.plutus "
                    f"--mint-redeemer-file /tmp/m3smoke/burn_challenge_redeemer.json "
                    f"--mint-script-file /tmp/m3smoke/reputation_reputation_spend.plutus "
                    f"--mint-redeemer-file /tmp/m3smoke/mint_hbonus_redeemer.json "
                )

            cardano_cli(
                f"conway transaction build {NETWORK_FLAG} "
                f"--tx-in {txin} "
                f"--tx-in {resolved_utxo_id} "
                f"{spend_args}"
                f"--read-only-tx-in-reference {params_utxo} "
                f"--read-only-tx-in-reference {cross_refs_utxo} "
                f"--read-only-tx-in-reference {agent_a_reg_utxo} "
                f"--tx-out '{target_addr}+{target_payout}' "
                f"--tx-out '{treasury_addr}+{protocol_fee}' "
                f"--tx-out '{reputation_addr}+2000000+{hbonus_mint_value}' "
                f"--tx-out-inline-datum-file /tmp/m3smoke/hbonus_datum.json "
                f"{mint_args}"
                f"--invalid-before {current_slot} "
                f"--required-signer-hash {vkey_hash} "
                f"{collateral_arg()}"
                f"--change-address {wallet_addr} "
                f"--out-file /tmp/m3smoke/tx_distribute.raw"
            )

            tx_hash = sign_and_submit("distribute_outcome", "/tmp/m3smoke/tx_distribute.raw", "/tmp/m3smoke/tx_distribute.signed")
            smoke["distribute_outcome_tx"] = tx_hash
            save_smoke_state(smoke)
            step_pass("DistributeOutcome")

        except Exception as e:
            step_fail("DistributeOutcome", e)
            save_smoke_state(smoke)
    else:
        print(f"\n--- Step 7: DistributeOutcome already done ({smoke['distribute_outcome_tx'][:16]}...) ---")
        passed += 1

    print_results(passed, failed)


# ── UTXO lookup helpers ──────────────────────────────────────────────────────

def find_agent_registry_utxo(agent_did: str) -> str | None:
    """Find the registry UTXO for a given agent DID."""
    registry_addr = script_hash_to_address(REGISTRY_POLICY_ID)
    utxos = get_utxo_at_address(registry_addr)
    for utxo_id, info in utxos.items():
        for pid, assets in info["value"].items():
            if pid == REGISTRY_POLICY_ID and agent_did in assets:
                return utxo_id
    return None


def find_stake_utxo(agent_did: str, reputation_addr: str, reputation_hash: str) -> str | None:
    """Find the stake UTXO for a given agent at the reputation validator."""
    stake_token_name = derive_stake_token_name(agent_did)
    utxos = get_utxo_at_address(reputation_addr)
    for utxo_id, info in utxos.items():
        for pid, assets in info["value"].items():
            if pid == reputation_hash and stake_token_name in assets:
                return utxo_id
    return None


def find_challenge_utxo(endorsement_addr: str, endorsement_hash: str, challenge_token_name: str) -> str | None:
    """Find a challenge UTXO by its token."""
    utxos = get_utxo_at_address(endorsement_addr)
    for utxo_id, info in utxos.items():
        for pid, assets in info["value"].items():
            if pid == endorsement_hash and challenge_token_name in assets:
                return utxo_id
    return None


# ── State management ─────────────────────────────────────────────────────────

def save_smoke_state(smoke: dict):
    DEPLOY_DIR.mkdir(exist_ok=True)
    with open(SMOKE_STATE_FILE, "w") as f:
        json.dump(smoke, f, indent=2)


def print_results(passed: int, failed: int):
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed == 0 and passed > 0:
        print("Full lifecycle smoke test PASSED!")
    elif failed > 0:
        print("Some steps failed — check errors above.")
        print("Re-run to resume from the last successful step.")
    print("=" * 60)


if __name__ == "__main__":
    smoke_test()
