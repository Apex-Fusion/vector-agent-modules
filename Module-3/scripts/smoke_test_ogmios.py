#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Full End-to-End Smoke Test via Ogmios

Remote-only version using PyCardano + Ogmios HTTP, matching Module 1 and Module 6.

Registers 2 agents, then exercises the full Module 3 lifecycle:
  1. Register Agent A (with capabilities) via Agent Registry
  2. Register Agent B (with capabilities) via Agent Registry
  3a. Create seed UTXO at reputation address
  3b. CreateStake for Agent A
  4. MintEndorsement from Agent B -> Agent A
  5. MintChallenge from Agent B against Agent A
  6. ResolveChallenge (oracle: CapabilityVerified)
  7. DistributeOutcome (CapabilityVerified)
  8. MintChallenge #2 (B challenges A again)
  9. ResolveChallenge #2 (oracle: CapabilityFalsified)
  10. SlashEndorsement (B's endorsement of A)
  11. DistributeOutcome + SlashStake (CapabilityFalsified)

Usage:
    python3 scripts/smoke_test_ogmios.py
"""

import hashlib
import json
import logging
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
    ExecutionUnits,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawCBOR,
    Redeemer,
    ScriptHash,
    TransactionBuilder,
    TransactionOutput,
    Value,
)

# Add python/ to path so we can import the SDK
MODULE3_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MODULE3_ROOT / "python"))

from reputation_staking import (
    ReputationStakingClient,
    derive_agent_nft_name_conway,
    derive_stake_token_name,
    script_hash_to_address,
    slot_to_posix_ms,
)
from reputation_staking.constants import REGISTRY_POLICY_ID, TX_WAIT_SECONDS
from reputation_staking.ogmios_backend import (
    NETWORK,
    OgmiosHttpContext,
    evaluate_tx,
    get_current_slot,
    get_wallet_utxos,
    get_collateral_utxo,
    load_wallet,
    submit_tx,
    tx_to_bytes,
    wait_for_tx,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

DEPLOY_DIR = MODULE3_ROOT / "deploy"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"
SMOKE_STATE_FILE = DEPLOY_DIR / "smoke_state_ogmios.json"

# Wallet .skey extracted from Docker container
WALLET_SKEY_PATH = "/tmp/m3dev_payment.skey"

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
        print("Some steps failed -- check errors above.")
        print("Re-run to resume from the last successful step.")
    print("=" * 60)


# ── Agent Registry helpers (not part of Module 3 SDK) ────────────────────────

def load_registry_script() -> PlutusV3Script:
    """Load the Agent Registry Plutus V3 script from the blueprint."""
    for candidate in [
        Path("/home/sisyphos/ai-sprint-2/vector-ai-agents/agent-registry/deploy/agent-registry/plutus.json"),
        MODULE3_ROOT.parent / "vector-ai-agents" / "agent-registry" / "deploy" / "agent-registry" / "plutus.json",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                bp = json.load(f)
            for v in bp.get("validators", []):
                if "spend" in v.get("title", "") or "mint" in v.get("title", ""):
                    return PlutusV3Script(bytes.fromhex(v["compiledCode"]))
    raise RuntimeError("Agent Registry blueprint not found")


def register_agent(
    context: OgmiosHttpContext,
    skey: PaymentSigningKey,
    vkey: PaymentVerificationKey,
    wallet_addr: Address,
    registry_script: PlutusV3Script,
    name: str,
    description: str,
    capabilities: list,
    protected_refs: set,
) -> tuple:
    """Register an agent via Agent Registry using PyCardano.

    Follows Module 1's wallet_factory.py pattern:
    - Build AgentDatum as cbor2.CBORTag -> RawCBOR
    - Register redeemer as cbor2.CBORTag -> RawCBOR
    - Two-pass evaluate-and-rebuild for execution budgets

    Returns (agent_did_hex, tx_hash).
    """
    registry_policy = ScriptHash(bytes.fromhex(REGISTRY_POLICY_ID))
    registry_addr = Address.from_primitive(script_hash_to_address(REGISTRY_POLICY_ID))

    current_slot = context.last_block_slot
    vkey_hash_bytes = bytes(vkey.hash())

    # Pick seed UTxO (smallest non-protected, non-collateral)
    wallet_utxos = get_wallet_utxos(context, wallet_addr, protected_refs)
    if not wallet_utxos:
        raise RuntimeError("No spendable wallet UTxOs found")

    # Sort deterministically for seed selection
    sorted_utxos = sorted(
        wallet_utxos,
        key=lambda u: (bytes(u.input.transaction_id).hex(), u.input.index),
    )
    seed_utxo = sorted_utxos[0]
    seed_tx_hash = bytes(seed_utxo.input.transaction_id)
    seed_tx_idx = seed_utxo.input.index

    # Derive agent DID (same as Module 1's agent_did_name)
    agent_did = derive_agent_nft_name_conway(
        seed_tx_hash.hex(), seed_tx_idx
    )
    print(f"  Seed UTXO: {seed_tx_hash.hex()}#{seed_tx_idx}")
    print(f"  Agent DID: {agent_did[:32]}...")

    # Build register redeemer: Constr(0, [Constr(0, [tx_hash, idx])])
    seed_ref_cbor = cbor2.CBORTag(121, [seed_tx_hash, seed_tx_idx])
    register_redeemer_cbor = RawCBOR(cbor2.dumps(cbor2.CBORTag(121, [seed_ref_cbor])))
    register_redeemer = Redeemer(
        register_redeemer_cbor,
        ExecutionUnits(mem=500_000, steps=200_000_000),
    )

    # Build agent datum: Constr(0, [credential, name, desc, caps, framework, endpoint, registered_at])
    agent_datum_cbor = cbor2.CBORTag(121, [
        cbor2.CBORTag(121, [vkey_hash_bytes]),  # VKCredential
        name.encode(),
        description.encode(),
        [cap.encode() for cap in capabilities],
        b"TestFramework",
        b"",  # endpoint
        slot_to_posix_ms(current_slot),
    ])

    # Agent NFT
    agent_nft_an = AssetName(bytes.fromhex(agent_did))
    mint_ma = MultiAsset()
    mint_a = Asset()
    mint_a[agent_nft_an] = 1
    mint_ma[registry_policy] = mint_a

    nft_ma = MultiAsset()
    nft_a = Asset()
    nft_a[agent_nft_an] = 1
    nft_ma[registry_policy] = nft_a

    collateral = get_collateral_utxo(context, wallet_addr, protected_refs)

    # --- Pass 1: Build with dummy execution units, evaluate ---
    builder1 = TransactionBuilder(context)
    builder1.fee_buffer = 500_000
    for u in wallet_utxos:
        builder1.add_input(u)
    builder1.mint = mint_ma
    builder1.add_minting_script(registry_script, register_redeemer)
    builder1.add_output(TransactionOutput(
        registry_addr, Value(15_000_000, nft_ma),
        datum=RawCBOR(cbor2.dumps(agent_datum_cbor)),
    ))
    builder1.required_signers = [vkey.hash()]
    builder1.validity_start = current_slot - 60
    builder1.ttl = current_slot + 3600
    builder1.collaterals = [collateral]

    tx1 = builder1.build_and_sign([skey], change_address=wallet_addr)
    tx1_hex = tx_to_bytes(tx1).hex()

    budgets = evaluate_tx(tx1_hex)
    logger.info("Evaluation budgets: %s", budgets)

    # Extract mint budget
    mint_budget = None
    for key, b in budgets.items():
        if "mint" in key:
            mint_budget = b
            break
    if mint_budget:
        register_redeemer = Redeemer(
            register_redeemer_cbor,
            ExecutionUnits(
                mem=int(mint_budget["mem"] * 1.2),
                steps=int(mint_budget["cpu"] * 1.2),
            ),
        )
    else:
        logger.warning("No mint budget found in evaluation, using defaults")

    # --- Pass 2: Rebuild with real execution units ---
    builder2 = TransactionBuilder(context)
    builder2.fee_buffer = 500_000
    for u in wallet_utxos:
        builder2.add_input(u)
    builder2.mint = mint_ma
    builder2.add_minting_script(registry_script, register_redeemer)
    builder2.add_output(TransactionOutput(
        registry_addr, Value(15_000_000, nft_ma),
        datum=RawCBOR(cbor2.dumps(agent_datum_cbor)),
    ))
    builder2.required_signers = [vkey.hash()]
    builder2.validity_start = current_slot - 60
    builder2.ttl = current_slot + 3600
    builder2.collaterals = [collateral]

    tx2 = builder2.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx2)
    return agent_did, tx_hash


# ── Main smoke test ──────────────────────────────────────────────────────────

def smoke_test():
    print("=" * 60)
    print("Module 3: Full End-to-End Smoke Test (Ogmios/Remote)")
    print("=" * 60)

    # Load deploy state
    if not DEPLOY_STATE_FILE.exists():
        print("ERROR: Deploy state not found. Run deploy_docker.py first.")
        sys.exit(1)

    # Check wallet
    if not Path(WALLET_SKEY_PATH).exists():
        print(f"ERROR: Wallet key not found at {WALLET_SKEY_PATH}")
        print("Extract from Docker: docker cp <container>:/tmp/m3dev/payment.skey /tmp/m3dev_payment.skey")
        sys.exit(1)

    # Set up context and wallet
    context = OgmiosHttpContext()
    skey, vkey, wallet_addr = load_wallet(WALLET_SKEY_PATH)

    # Set up client
    client = ReputationStakingClient.from_deploy_state(
        str(DEPLOY_STATE_FILE), context, skey,
    )

    # Load Agent Registry script
    registry_script = load_registry_script()

    # Load or init smoke state
    smoke = {}
    if SMOKE_STATE_FILE.exists():
        with open(SMOKE_STATE_FILE) as f:
            smoke = json.load(f)

    # Verify environment
    print("\n--- Environment Check ---")
    current_slot = context.last_block_slot
    print(f"  Tip slot: {current_slot}")

    wallet_utxos = get_wallet_utxos(context, wallet_addr)
    total_lovelace = sum(u.output.amount.coin for u in wallet_utxos)
    print(f"  Wallet: {total_lovelace / 1_000_000:.2f} AP3X ({len(wallet_utxos)} UTxOs)")
    print(f"  Address: {wallet_addr}")
    print(f"  Reputation: {client.reputation_hash[:16]}... at {str(client.reputation_addr)[:40]}...")
    print(f"  Endorsement: {client.endorsement_hash[:16]}... at {str(client.endorsement_addr)[:40]}...")

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
                context, skey, vkey, wallet_addr, registry_script,
                "SmokeTestAgentA", "Smoke test agent A for Module 3",
                ["code_review", "testing", "deployment"],
                client._protected_refs,
            )
            print(f"  TX: {tx_hash}")
            smoke["agent_a_did"] = agent_a_did
            smoke["agent_a_tx"] = tx_hash
            save_smoke_state(smoke)
            step_pass("Register Agent A")
            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("Register Agent A", e)
            import traceback; traceback.print_exc()
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
                context, skey, vkey, wallet_addr, registry_script,
                "SmokeTestAgentB", "Smoke test agent B for Module 3",
                ["code_review", "testing"],
                client._protected_refs,
            )
            print(f"  TX: {tx_hash}")
            smoke["agent_b_did"] = agent_b_did
            smoke["agent_b_tx"] = tx_hash
            save_smoke_state(smoke)
            step_pass("Register Agent B")
            print(f"  Waiting {TX_WAIT}s...")
            time.sleep(TX_WAIT)
        except Exception as e:
            step_fail("Register Agent B", e)
            import traceback; traceback.print_exc()
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
            import traceback; traceback.print_exc()
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
            import traceback; traceback.print_exc()
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 3b: CreateStake already done ({smoke['create_stake_tx'][:16]}...) ---")
        passed += 1

    # ── Step 4: MintEndorsement from Agent B -> Agent A ───────────────────

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
            import traceback; traceback.print_exc()
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
            # Store challenge datum as CBOR hex for JSON serialization
            smoke["challenge_datum_cbor"] = challenge_datum.to_cbor().hex() if hasattr(challenge_datum, 'to_cbor') else None
            save_smoke_state(smoke)
            step_pass("MintChallenge")
        except Exception as e:
            step_fail("MintChallenge", e)
            import traceback; traceback.print_exc()
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
            # Reconstruct challenge datum from CBOR if needed
            challenge_datum = _load_challenge_datum(smoke)
            if not challenge_datum:
                raise RuntimeError("Challenge datum not available in smoke state")

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
            import traceback; traceback.print_exc()
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
            challenge_datum = _load_challenge_datum(smoke)
            if not challenge_datum:
                raise RuntimeError("Challenge datum not available in smoke state")

            tx_hash = client.distribute_outcome(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                challenge_datum=challenge_datum,
            )
            print(f"  TX: {tx_hash}")
            smoke["distribute_outcome_tx"] = tx_hash
            save_smoke_state(smoke)
            step_pass("DistributeOutcome")
        except Exception as e:
            step_fail("DistributeOutcome", e)
            import traceback; traceback.print_exc()
            save_smoke_state(smoke)
    else:
        print(f"\n--- Step 7: DistributeOutcome already done ({smoke['distribute_outcome_tx'][:16]}...) ---")
        passed += 1

    # ══════════════════════════════════════════════════════════════════════
    # CapabilityFalsified Flow (Steps 8-11)
    # ══════════════════════════════════════════════════════════════════════

    # ── Step 8: MintChallenge #2 (B challenges A again, for falsified test) ──

    if "mint_challenge2_tx" not in smoke:
        print("\n--- Step 8: MintChallenge #2 (B challenges A on code_review, 25 AP3X) ---")
        try:
            evidence_data = b"capability_falsified_test_evidence_agent_a_cannot_code_review"
            evidence_hash = hashlib.blake2b(evidence_data, digest_size=32).hexdigest()

            tx_hash, challenge_datum = client.mint_challenge(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                stake_amount=25_000_000,
                evidence_hash=evidence_hash,
                evidence_uri="ipfs://falsified-test-evidence",
            )
            print(f"  TX: {tx_hash}")
            from reputation_staking.token_names import derive_challenge_token_name
            smoke["mint_challenge2_tx"] = tx_hash
            smoke["challenge2_utxo"] = f"{tx_hash}#0"
            smoke["challenge2_token_name"] = derive_challenge_token_name(
                agent_b_did, agent_a_did, "code_review"
            )
            smoke["challenge2_datum_cbor"] = challenge_datum.to_cbor().hex() if hasattr(challenge_datum, 'to_cbor') else None
            save_smoke_state(smoke)
            step_pass("MintChallenge #2")
        except Exception as e:
            step_fail("MintChallenge #2", e)
            import traceback; traceback.print_exc()
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 8: MintChallenge #2 already done ({smoke['mint_challenge2_tx'][:16]}...) ---")
        passed += 1

    # ── Step 9: ResolveChallenge #2 (Oracle: CapabilityFalsified) ────────

    if "resolve_challenge2_tx" not in smoke:
        print("\n--- Step 9: ResolveChallenge #2 (Oracle: CapabilityFalsified) ---")
        try:
            challenge_datum = _load_challenge_datum2(smoke)
            if not challenge_datum:
                raise RuntimeError("Challenge #2 datum not available in smoke state")

            tx_hash = client.resolve_challenge(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                outcome_constructor=1,  # CapabilityFalsified
                challenge_datum=challenge_datum,
            )
            print(f"  TX: {tx_hash}")
            smoke["resolve_challenge2_tx"] = tx_hash
            smoke["resolved_challenge2_utxo"] = f"{tx_hash}#0"
            save_smoke_state(smoke)
            step_pass("ResolveChallenge #2 (Falsified)")
        except Exception as e:
            step_fail("ResolveChallenge #2 (Falsified)", e)
            import traceback; traceback.print_exc()
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 9: ResolveChallenge #2 already done ({smoke['resolve_challenge2_tx'][:16]}...) ---")
        passed += 1

    # ── Step 10: SlashEndorsement (B's endorsement of A) ─────────────────

    if "slash_endorsement_tx" not in smoke:
        print("\n--- Step 10: SlashEndorsement (B's endorsement of A, 50% slash) ---")
        try:
            tx_hash = client.slash_endorsement(
                endorser_did=agent_b_did,
                target_did=agent_a_did,
                challenger_did=agent_b_did,
                capability="code_review",
                resolved_challenge_utxo_ref=smoke["resolved_challenge2_utxo"],
            )
            print(f"  TX: {tx_hash}")
            smoke["slash_endorsement_tx"] = tx_hash
            save_smoke_state(smoke)
            step_pass("SlashEndorsement")
        except Exception as e:
            step_fail("SlashEndorsement", e)
            import traceback; traceback.print_exc()
            save_smoke_state(smoke)
            print_results(passed, failed)
            return
    else:
        print(f"\n--- Step 10: SlashEndorsement already done ({smoke['slash_endorsement_tx'][:16]}...) ---")
        passed += 1

    # ── Step 11: DistributeOutcome + SlashStake (CapabilityFalsified) ────

    if "distribute_falsified_tx" not in smoke:
        print("\n--- Step 11: DistributeOutcome + SlashStake (CapabilityFalsified) ---")
        try:
            challenge_datum = _load_challenge_datum2(smoke)
            if not challenge_datum:
                raise RuntimeError("Challenge #2 datum not available in smoke state")

            tx_hash = client.distribute_falsified_outcome(
                challenger_did=agent_b_did,
                target_did=agent_a_did,
                capability="code_review",
                challenge_datum=challenge_datum,
            )
            print(f"  TX: {tx_hash}")
            smoke["distribute_falsified_tx"] = tx_hash
            save_smoke_state(smoke)
            step_pass("DistributeOutcome + SlashStake (Falsified)")
        except Exception as e:
            step_fail("DistributeOutcome + SlashStake (Falsified)", e)
            import traceback; traceback.print_exc()
            save_smoke_state(smoke)
    else:
        print(f"\n--- Step 11: DistributeOutcome + SlashStake already done ({smoke['distribute_falsified_tx'][:16]}...) ---")
        passed += 1

    print_results(passed, failed)


def _load_challenge_datum2(smoke: dict):
    """Load challenge #2 datum from smoke state."""
    from reputation_staking.plutus_data import EndorsementValidatorDatumChallenge

    cbor_hex = smoke.get("challenge2_datum_cbor")
    if cbor_hex:
        return EndorsementValidatorDatumChallenge.from_cbor(cbor_hex)
    return None


def _load_challenge_datum(smoke: dict):
    """Load challenge datum from smoke state.

    Tries CBOR hex first (new format), falls back to JSON dict (old format).
    """
    from reputation_staking.plutus_data import EndorsementValidatorDatumChallenge

    cbor_hex = smoke.get("challenge_datum_cbor")
    if cbor_hex:
        return EndorsementValidatorDatumChallenge.from_cbor(cbor_hex)

    # Fallback: old JSON format from docker-based smoke test
    json_datum = smoke.get("challenge_datum")
    if json_datum:
        return json_datum  # client.resolve_challenge handles JSON dicts

    return None


if __name__ == "__main__":
    smoke_test()
