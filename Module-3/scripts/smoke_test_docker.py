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
import sys
import time
from pathlib import Path

try:
    import cbor2
except ImportError:
    print("ERROR: cbor2 not installed. Run: pip install cbor2")
    sys.exit(1)

# Add python/ to path so we can import the SDK
MODULE3_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MODULE3_ROOT / "python"))

from reputation_staking import (
    DockerChainBackend,
    ReputationStakingClient,
    derive_agent_nft_name_conway,
    derive_stake_token_name,
    script_hash_to_address,
    slot_to_posix_ms,
    vkey_hash_to_address,
)
from reputation_staking.constants import REGISTRY_POLICY_ID, TX_WAIT_SECONDS
from reputation_staking.datums import (
    build_agent_datum_json,
    build_register_redeemer_json,
)

# ── Config ───────────────────────────────────────────────────────────────────

DEPLOY_DIR = MODULE3_ROOT / "deploy"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"
SMOKE_STATE_FILE = DEPLOY_DIR / "smoke_state.json"

TX_WAIT = TX_WAIT_SECONDS


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


# ── Agent Registry helpers (not part of Module 3 SDK) ────────────────────────

def setup_registry_script(backend: DockerChainBackend, work_dir: str) -> None:
    """Write the Agent Registry Plutus script to Docker for minting agent NFTs."""
    registry_bp_path = None
    for candidate in [
        Path("/home/sisyphos/ai-sprint-2/vector-ai-agents/agent-registry/deploy/agent-registry/plutus.json"),
        MODULE3_ROOT.parent / "vector-ai-agents" / "agent-registry" / "deploy" / "agent-registry" / "plutus.json",
    ]:
        if candidate.exists():
            registry_bp_path = candidate
            break
    if not registry_bp_path:
        print("  ERROR: Agent Registry blueprint not found")
        sys.exit(1)

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

    envelope = {
        "type": "PlutusScriptV3",
        "description": "Agent Registry",
        "cborHex": cbor2.dumps(bytes.fromhex(reg_code)).hex(),
    }
    backend.write_json(f"{work_dir}/registry.plutus", envelope)


def register_agent(
    backend: DockerChainBackend,
    wallet_addr: str,
    vkey_hash: str,
    name: str,
    description: str,
    capabilities: list,
    current_slot: int,
    work_dir: str,
    protected_utxos: set,
) -> tuple:
    """Register an agent via Agent Registry. Returns (agent_did, tx_hash)."""
    registry_addr = script_hash_to_address(REGISTRY_POLICY_ID)

    txin, _ = backend.get_best_utxo(wallet_addr, protected_utxos)
    seed_tx_hash = txin.split("#")[0]
    seed_tx_ix = int(txin.split("#")[1])

    agent_did = derive_agent_nft_name_conway(seed_tx_hash, seed_tx_ix)
    print(f"  Seed UTXO: {txin}")
    print(f"  Agent DID: {agent_did[:32]}...")

    datum = build_agent_datum_json(
        vkey_hash, name, description, capabilities, "TestFramework", current_slot
    )
    backend.write_json(f"{work_dir}/agent_datum.json", datum)

    redeemer = build_register_redeemer_json(seed_tx_hash, seed_tx_ix)
    backend.write_json(f"{work_dir}/register_redeemer.json", redeemer)

    mint_value = f"1 {REGISTRY_POLICY_ID}.{agent_did}"

    collateral = backend.get_collateral(wallet_addr, protected_utxos)
    collateral_arg = f"--tx-in-collateral {collateral} " if collateral else ""

    backend.cardano_cli(
        f"conway transaction build {backend.network_flag} "
        f"--tx-in {txin} "
        f"--tx-out '{registry_addr}+10000000+{mint_value}' "
        f"--tx-out-inline-datum-file {work_dir}/agent_datum.json "
        f"--mint '{mint_value}' "
        f"--mint-script-file {work_dir}/registry.plutus "
        f"--mint-redeemer-file {work_dir}/register_redeemer.json "
        f"--required-signer-hash {vkey_hash} "
        f"{collateral_arg}"
        f"--change-address {wallet_addr} "
        f"--out-file {work_dir}/tx_reg.raw"
    )

    tx_hash = backend.sign_and_submit(
        "register_agent", f"{work_dir}/tx_reg.raw", f"{work_dir}/tx_reg.signed"
    )
    return agent_did, tx_hash


# ── Main smoke test ──────────────────────────────────────────────────────────

def smoke_test():
    print("=" * 60)
    print("Module 3: Full End-to-End Smoke Test")
    print("=" * 60)

    # Load deploy state
    if not DEPLOY_STATE_FILE.exists():
        print("ERROR: Deploy state not found. Run deploy_docker.py first.")
        sys.exit(1)

    # Set up backend and client
    backend = DockerChainBackend()
    client = ReputationStakingClient.from_deploy_state(str(DEPLOY_STATE_FILE), backend)

    # Work directory for temp files (shared between registry ops and SDK)
    work_dir = "/tmp/m3sdk"
    backend.docker_exec(f"mkdir -p {work_dir}")

    # Write Module 3 Plutus scripts to Docker
    blueprint_path = DEPLOY_DIR / "plutus.json"
    if not blueprint_path.exists():
        print("ERROR: Applied Module 3 blueprint not found in deploy/")
        sys.exit(1)
    client.setup_scripts(str(blueprint_path))

    # Write Agent Registry script
    setup_registry_script(backend, work_dir)

    # Load or init smoke state
    smoke = {}
    if SMOKE_STATE_FILE.exists():
        with open(SMOKE_STATE_FILE) as f:
            smoke = json.load(f)

    wallet_addr = client.deploy["wallet_address"]
    vkey_hash = client.deploy["wallet_vkey_hash"]

    # Verify environment
    print("\n--- Environment Check ---")
    current_slot = backend.get_current_slot()
    tip = json.loads(backend.cardano_cli(f"conway query tip {backend.network_flag}"))
    print(f"  Node: slot {current_slot}, sync {tip['syncProgress']}")

    txin, balance = backend.get_best_utxo(wallet_addr, client._protected_utxos)
    print(f"  Wallet: {balance / 1_000_000:.2f} AP3X")
    print(f"  Reputation: {client.reputation_hash[:16]}... at {client.reputation_addr[:40]}...")
    print(f"  Endorsement: {client.endorsement_hash[:16]}... at {client.endorsement_addr[:40]}...")
    print(f"  CrossRefs: {client.cross_refs_utxo}")
    print(f"  Params:    {client.params_utxo}")

    passed = 0
    failed = 0

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
            agent_a_did, tx_hash = register_agent(
                backend, wallet_addr, vkey_hash,
                "SmokeTestAgentA", "Smoke test agent A for Module 3",
                ["code_review", "testing", "deployment"],
                current_slot, work_dir, client._protected_utxos,
            )
            print(f"  TX: {tx_hash}")
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
            agent_b_did, tx_hash = register_agent(
                backend, wallet_addr, vkey_hash,
                "SmokeTestAgentB", "Smoke test agent B for Module 3",
                ["code_review", "testing"],
                current_slot, work_dir, client._protected_utxos,
            )
            print(f"  TX: {tx_hash}")
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
            tx_hash = client.create_seed_utxo(agent_a_did, ["code_review"])
            print(f"  TX: {tx_hash}")
            smoke["seed_utxo_tx"] = tx_hash
            smoke["seed_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("Seed UTXO")
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
            tx_hash = client.create_stake(
                agent_did=agent_a_did,
                capabilities=["code_review", "testing"],
                stake_amount=10_000_000,
                seed_utxo=smoke["seed_utxo"],
            )
            print(f"  TX: {tx_hash}")
            smoke["create_stake_tx"] = tx_hash
            smoke["stake_utxo"] = f"{tx_hash}#0"
            smoke["stake_token_name"] = derive_stake_token_name(agent_a_did)
            save_smoke_state(smoke)
            step_pass("CreateStake")
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
            tx_hash = client.mint_endorsement(
                endorser_did=agent_b_did,
                target_did=agent_a_did,
                capabilities=["code_review"],
                stake_amount=5_000_000,
            )
            print(f"  TX: {tx_hash}")
            from reputation_staking.token_names import derive_endorsement_token_name
            smoke["mint_endorsement_tx"] = tx_hash
            smoke["endorsement_utxo"] = f"{tx_hash}#0"
            smoke["endorsement_token_name"] = derive_endorsement_token_name(agent_b_did, agent_a_did)
            save_smoke_state(smoke)
            step_pass("MintEndorsement")
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
            evidence_data = b"smoke_test_evidence_for_code_review_challenge"
            evidence_hash = hashlib.blake2b(evidence_data, digest_size=32).hexdigest()

            tx_hash, challenge_datum = client.mint_challenge(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                stake_amount=25_000_000,
                evidence_hash=evidence_hash,
                evidence_uri="ipfs://smoke-test-evidence",
            )
            print(f"  TX: {tx_hash}")
            from reputation_staking.token_names import derive_challenge_token_name
            smoke["mint_challenge_tx"] = tx_hash
            smoke["challenge_utxo"] = f"{tx_hash}#0"
            smoke["challenge_token_name"] = derive_challenge_token_name(
                agent_b_did, agent_a_did, "code_review"
            )
            smoke["challenge_datum"] = challenge_datum
            save_smoke_state(smoke)
            step_pass("MintChallenge")
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
            challenge_datum = smoke.get("challenge_datum")
            if not challenge_datum:
                raise RuntimeError("Challenge datum not in smoke state")

            tx_hash = client.resolve_challenge(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                outcome_constructor=0,  # CapabilityVerified
                challenge_datum=challenge_datum,
            )
            print(f"  TX: {tx_hash}")
            smoke["resolve_challenge_tx"] = tx_hash
            smoke["resolved_challenge_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("ResolveChallenge")
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
            challenge_datum = smoke.get("challenge_datum")
            if not challenge_datum:
                raise RuntimeError("Challenge datum not in smoke state")

            agent_a_reg_utxo = smoke.get("agent_a_utxo")
            if not agent_a_reg_utxo:
                agent_a_reg_utxo = client.find_agent_registry_utxo(agent_a_did)
            if not agent_a_reg_utxo:
                raise RuntimeError("Agent A registry UTXO not found")

            tx_hash = client.distribute_outcome(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                challenge_datum=challenge_datum,
                agent_a_reg_utxo=agent_a_reg_utxo,
            )
            print(f"  TX: {tx_hash}")
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


if __name__ == "__main__":
    smoke_test()
