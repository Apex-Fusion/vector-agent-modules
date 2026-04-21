#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Demo Seed Script (MAINNET)

Populates mainnet on-chain state to showcase ALL reputation staking features
for a presentation:

  - All 5 tiers (Elite, Trusted, Established, Novice, Unverified)
  - Self-stake, endorsements (received + given), challenges
  - History bonuses (ChallengeWon, from resolved challenges)
  - Sybil detection flags (mutual-endorsement cycle)
  - An unresolved active challenge visible on the Challenges tab

End state after run (approximate):
  Alpha    -> Elite       (stake 50  + endorsements 150 + bonus 1860 - 10 open)
  Beta     -> Trusted     (stake 50  + endorsements 150 + bonus 300)
  Gamma    -> Established (stake 110)
  Delta    -> Novice      (stake 5)
  Epsilon  -> Unverified  (no stake, 1 incoming endorsement only)
  Sybil1   -> Novice + sybil flag  (mutual cycle with Sybil2)
  Sybil2   -> Novice + sybil flag  (mutual cycle with Sybil1)

Resumable via deploy/mainnet/demo_state.json — safe to re-run on failure.

Usage:
    cd Module-3 && PYTHONPATH=python:$PYTHONPATH python3 scripts/demo_seed_mainnet.py
"""

import hashlib
import json
import logging
import sys
import time
from pathlib import Path

from pycardano import PaymentSigningKey, PaymentVerificationKey, PlutusV3Script

MODULE3_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MODULE3_ROOT / "python"))

# Patch mainnet system start BEFORE any slot/time calls in the SDK.
import reputation_staking.utils as _utils
import reputation_staking.constants as _constants
MAINNET_SYSTEM_START_UNIX_S = 1756485600  # 2025-08-29T16:40:00Z
_utils.SYSTEM_START_UNIX_S = MAINNET_SYSTEM_START_UNIX_S
_constants.SYSTEM_START_UNIX_S = MAINNET_SYSTEM_START_UNIX_S

from reputation_staking import ReputationStakingClient
from reputation_staking.ogmios_backend import (
    OgmiosHttpContext,
    get_wallet_utxos,
    load_wallet,
)
from reputation_staking.plutus_data import EndorsementValidatorDatumChallenge

# Reuse the battle-tested agent registration helper from the smoke test.
from smoke_test_mainnet_ogmios import (  # noqa: E402
    MAINNET_OGMIOS_URL,
    MAINNET_SUBMIT_URL,
    load_registry_script,
    register_agent,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEPLOY_DIR = MODULE3_ROOT / "deploy" / "mainnet"
DEPLOY_STATE_FILE = DEPLOY_DIR / "deploy_state.json"
DEMO_STATE_FILE = DEPLOY_DIR / "demo_state.json"
WALLET_SKEY_PATH = "/tmp/m3mainnet_payment.skey"

TX_WAIT = 25  # seconds between txs

# ── Demo economics (in DFM = lovelace) ──────────────────────────────────────
AP3X = 1_000_000

# On-chain minimums (from mainnet protocol params): min_self_stake=10 AP3X,
# min_endorsement=5 AP3X. All amounts here must respect those.
ALPHA_SELF_STAKE = 50 * AP3X
BETA_SELF_STAKE = 50 * AP3X
GAMMA_SELF_STAKE = 110 * AP3X
DELTA_SELF_STAKE = 10 * AP3X   # at min_self_stake; still Novice (10 < 100)
SYBIL_SELF_STAKE = 10 * AP3X   # at min_self_stake

EPSILON_SELF_STAKE = 10 * AP3X  # at min_self_stake; open chg drops it to Unverified

ENDORSE_TO_ALPHA = 50 * AP3X   # × 3 endorsers to fill cap 150
ENDORSE_TO_BETA = 50 * AP3X    # × 3 endorsers to fill cap 150
ENDORSE_SYBIL = 5 * AP3X       # at min_endorsement, mutual

# Alpha's challenge cycles — shrinking stakes to accommodate tx fees eaten
# across each cycle. Total bonus = sum = 1810 AP3X. Alpha net = 50 + 150 +
# 1810 - 10 (open chg) = 2000 AP3X — just hits Elite threshold.
ALPHA_CYCLE_STAKES = [475, 475, 440, 420]  # AP3X per cycle
BETA_CHALLENGE_STAKE = 300 * AP3X   # × 1 cycle  → 300 bonus
OPEN_CHALLENGE_STAKE = 25 * AP3X    # at min_challenge_stake; vs Epsilon


# ── State management ───────────────────────────────────────────────────────

def load_state() -> dict:
    if DEMO_STATE_FILE.exists():
        with open(DEMO_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    with open(DEMO_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _load_cd(cbor_hex: str):
    return EndorsementValidatorDatumChallenge.from_cbor(cbor_hex)


# ── Step wrapper ───────────────────────────────────────────────────────────

def step(state: dict, key: str, label: str, fn, *, wait: int = TX_WAIT):
    """Run `fn()` if `key` not yet in `state`, persist result, sleep."""
    if key in state:
        print(f"\n[skip] {label} — already done ({state[key] if isinstance(state[key], str) else 'ok'})")
        return state[key]
    print(f"\n--- {label} ---")
    try:
        result = fn()
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback; traceback.print_exc()
        save_state(state)
        sys.exit(1)
    state[key] = result
    save_state(state)
    print(f"  OK: {result if isinstance(result, str) else 'complete'}")
    if wait:
        print(f"  waiting {wait}s for tx confirmation...")
        time.sleep(wait)
    return result


# ── Per-agent registration helper ──────────────────────────────────────────

def register(state, key, client, context, skey, vkey, wallet_addr, registry_script,
             name, description, capabilities):
    """Register an agent (or return cached DID), returning the DID string."""
    did_key = f"{key}_did"
    tx_key = f"{key}_tx"
    if did_key in state:
        print(f"\n[skip] Register {name} — DID {state[did_key][:24]}... already in state")
        return state[did_key]
    print(f"\n--- Register {name} ---")
    try:
        did, tx_hash = register_agent(
            context, skey, vkey, wallet_addr, registry_script,
            name, description, capabilities, client._protected_refs,
        )
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback; traceback.print_exc()
        save_state(state)
        sys.exit(1)
    state[did_key] = did
    state[tx_key] = tx_hash
    save_state(state)
    print(f"  OK: DID {did[:24]}... tx {tx_hash[:16]}...")
    print(f"  waiting {TX_WAIT}s...")
    time.sleep(TX_WAIT)
    return did


# ── Challenge cycle helper (mint -> resolve Verified -> distribute) ────────

def run_won_challenge(state, cycle_key, label, client, challenger_did, target_did,
                      capability, stake_amount):
    """Full win cycle: target wins → gets history bonus of `stake_amount` points."""
    mint_key = f"{cycle_key}_mint_tx"
    datum_key = f"{cycle_key}_datum_cbor"
    resolve_key = f"{cycle_key}_resolve_tx"
    distribute_key = f"{cycle_key}_distribute_tx"

    # Stage 1: mint challenge
    if mint_key not in state:
        print(f"\n--- {label} — mint challenge ({stake_amount // AP3X} AP3X) ---")
        try:
            evidence = f"{cycle_key}_evidence_{int(time.time())}".encode()
            evidence_hash = hashlib.blake2b(evidence, digest_size=32).hexdigest()
            tx_hash, cd = client.mint_challenge(
                challenger_did=challenger_did,
                target_did=target_did,
                capability=capability,
                stake_amount=stake_amount,
                evidence_hash=evidence_hash,
                evidence_uri=f"ipfs://demo-{cycle_key}",
            )
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback; traceback.print_exc()
            save_state(state)
            sys.exit(1)
        state[mint_key] = tx_hash
        state[datum_key] = cd.to_cbor().hex()
        save_state(state)
        print(f"  OK: tx {tx_hash[:16]}...   waiting {TX_WAIT}s")
        time.sleep(TX_WAIT)
    else:
        print(f"\n[skip] {label} — mint already done ({state[mint_key][:16]}...)")

    # Stage 2: resolve Verified (target wins)
    if resolve_key not in state:
        print(f"\n--- {label} — resolve Verified ---")
        try:
            cd = _load_cd(state[datum_key])
            tx_hash = client.resolve_challenge(
                challenger_did=challenger_did,
                target_did=target_did,
                capability=capability,
                outcome_constructor=0,  # CapabilityVerified
                challenge_datum=cd,
            )
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback; traceback.print_exc()
            save_state(state)
            sys.exit(1)
        state[resolve_key] = tx_hash
        save_state(state)
        print(f"  OK: tx {tx_hash[:16]}...   waiting {TX_WAIT}s")
        time.sleep(TX_WAIT)
    else:
        print(f"\n[skip] {label} — resolve already done ({state[resolve_key][:16]}...)")

    # Stage 3: distribute outcome (mints history bonus for TARGET)
    if distribute_key not in state:
        print(f"\n--- {label} — distribute outcome (mints history bonus for target) ---")
        try:
            cd = _load_cd(state[datum_key])
            tx_hash = client.distribute_outcome(
                challenger_did=challenger_did,
                target_did=target_did,
                capability=capability,
                challenge_datum=cd,
            )
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback; traceback.print_exc()
            save_state(state)
            sys.exit(1)
        state[distribute_key] = tx_hash
        save_state(state)
        print(f"  OK: tx {tx_hash[:16]}...   waiting {TX_WAIT}s")
        time.sleep(TX_WAIT)
    else:
        print(f"\n[skip] {label} — distribute already done ({state[distribute_key][:16]}...)")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("Module 3 Demo Seed — MAINNET")
    print("Showcases all 5 reputation tiers + sybil detection + active chg")
    print("=" * 64)

    if not DEPLOY_STATE_FILE.exists():
        sys.exit(f"Deploy state missing: {DEPLOY_STATE_FILE}")
    if not Path(WALLET_SKEY_PATH).exists():
        sys.exit(f"Wallet key missing: {WALLET_SKEY_PATH}")

    context = OgmiosHttpContext(ogmios_url=MAINNET_OGMIOS_URL)
    skey, vkey, wallet_addr = load_wallet(WALLET_SKEY_PATH)
    client = ReputationStakingClient.from_deploy_state(
        str(DEPLOY_STATE_FILE), context, skey,
        ogmios_url=MAINNET_OGMIOS_URL, submit_url=MAINNET_SUBMIT_URL,
    )
    registry_script = load_registry_script()
    state = load_state()

    # Environment banner
    wallet_utxos = get_wallet_utxos(context, wallet_addr)
    total = sum(u.output.amount.coin for u in wallet_utxos) / AP3X
    print(f"\nWallet: {total:.2f} AP3X across {len(wallet_utxos)} UTxOs")
    print(f"Address: {wallet_addr}")
    print(f"Current slot: {context.last_block_slot}")
    print(f"State file: {DEMO_STATE_FILE}")

    # ── Agent registrations ────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 1/7: Register 7 agents")
    print("=" * 64)

    alpha_did = register(state, "alpha", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoAlpha",
        "Demo agent showcasing Elite tier via challenges won",
        ["code_review", "testing", "deployment"])
    beta_did = register(state, "beta", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoBeta",
        "Demo agent showcasing Trusted tier",
        ["code_review", "security_audit"])
    gamma_did = register(state, "gamma", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoGamma",
        "Demo agent showcasing Established tier",
        ["data_analysis"])
    delta_did = register(state, "delta", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoDelta",
        "Demo agent showcasing Novice tier",
        ["documentation"])
    epsilon_did = register(state, "epsilon", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoEpsilon",
        "Demo agent showcasing Unverified tier (no stake)",
        ["research"])
    sybil1_did = register(state, "sybil1", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoSybil1",
        "Demo agent part of a mutual-endorsement sybil cluster",
        ["code_review"])
    sybil2_did = register(state, "sybil2", client, context, skey, vkey, wallet_addr,
        registry_script, "DemoSybil2",
        "Demo agent part of a mutual-endorsement sybil cluster",
        ["code_review"])

    # ── Alpha tier: Elite via stake + endorsements + 3 won challenges ─────
    print("\n" + "=" * 64)
    print("PHASE 2/7: Alpha → Elite")
    print("=" * 64)

    step(state, "alpha_seed_utxo", "Alpha seed UTXO",
         lambda: client.create_seed_utxo(alpha_did, ["code_review", "testing"]))
    step(state, "alpha_stake_tx", f"Alpha CreateStake ({ALPHA_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=alpha_did,
             capabilities=["code_review", "testing"],
             stake_amount=ALPHA_SELF_STAKE,
             seed_utxo=f"{state['alpha_seed_utxo']}#0"))

    # Fill Alpha endorsement cap (50 × 3 = 150) with 3 endorsements
    step(state, "beta_endorses_alpha",
         f"Beta → Alpha endorsement ({ENDORSE_TO_ALPHA // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=beta_did, target_did=alpha_did,
             capabilities=["code_review"], stake_amount=ENDORSE_TO_ALPHA))
    step(state, "gamma_endorses_alpha",
         f"Gamma → Alpha endorsement ({ENDORSE_TO_ALPHA // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=gamma_did, target_did=alpha_did,
             capabilities=["testing"], stake_amount=ENDORSE_TO_ALPHA))
    step(state, "delta_endorses_alpha",
         f"Delta → Alpha endorsement ({ENDORSE_TO_ALPHA // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=delta_did, target_did=alpha_did,
             capabilities=["code_review"], stake_amount=ENDORSE_TO_ALPHA))

    # Sequential won challenges with shrinking stakes → history bonuses
    for i, stake_ap3x in enumerate(ALPHA_CYCLE_STAKES, start=1):
        run_won_challenge(
            state, cycle_key=f"alpha_cycle_{i}",
            label=f"Alpha cycle {i}/{len(ALPHA_CYCLE_STAKES)} ({stake_ap3x} AP3X challenge)",
            client=client,
            challenger_did=beta_did,       # Beta challenges, Alpha wins
            target_did=alpha_did,
            capability="code_review",
            stake_amount=stake_ap3x * AP3X,
        )

    # ── Beta tier: Trusted via stake + endorsements + 1 won challenge ─────
    print("\n" + "=" * 64)
    print("PHASE 3/7: Beta → Trusted")
    print("=" * 64)

    step(state, "beta_seed_utxo", "Beta seed UTXO",
         lambda: client.create_seed_utxo(beta_did, ["security_audit"]))
    step(state, "beta_stake_tx", f"Beta CreateStake ({BETA_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=beta_did,
             capabilities=["security_audit"],
             stake_amount=BETA_SELF_STAKE,
             seed_utxo=f"{state['beta_seed_utxo']}#0"))

    # 1 won challenge → +300 bonus
    run_won_challenge(
        state, cycle_key="beta_cycle_1",
        label="Beta cycle 1/1 (300 AP3X challenge)",
        client=client,
        challenger_did=gamma_did,          # Gamma challenges, Beta wins
        target_did=beta_did,
        capability="security_audit",
        stake_amount=BETA_CHALLENGE_STAKE,
    )

    # 3 endorsements → fills cap 150
    step(state, "alpha_endorses_beta",
         f"Alpha → Beta endorsement ({ENDORSE_TO_BETA // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=alpha_did, target_did=beta_did,
             capabilities=["security_audit"], stake_amount=ENDORSE_TO_BETA))
    step(state, "gamma_endorses_beta",
         f"Gamma → Beta endorsement ({ENDORSE_TO_BETA // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=gamma_did, target_did=beta_did,
             capabilities=["security_audit"], stake_amount=ENDORSE_TO_BETA))
    step(state, "delta_endorses_beta",
         f"Delta → Beta endorsement ({ENDORSE_TO_BETA // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=delta_did, target_did=beta_did,
             capabilities=["security_audit"], stake_amount=ENDORSE_TO_BETA))

    # ── Gamma tier: Established (simple stake) ────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 4/7: Gamma → Established")
    print("=" * 64)

    step(state, "gamma_seed_utxo", "Gamma seed UTXO",
         lambda: client.create_seed_utxo(gamma_did, ["data_analysis"]))
    step(state, "gamma_stake_tx", f"Gamma CreateStake ({GAMMA_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=gamma_did,
             capabilities=["data_analysis"],
             stake_amount=GAMMA_SELF_STAKE,
             seed_utxo=f"{state['gamma_seed_utxo']}#0"))

    # ── Delta tier: Novice (simple stake) ─────────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 5/7: Delta → Novice")
    print("=" * 64)

    step(state, "delta_seed_utxo", "Delta seed UTXO",
         lambda: client.create_seed_utxo(delta_did, ["documentation"]))
    step(state, "delta_stake_tx", f"Delta CreateStake ({DELTA_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=delta_did,
             capabilities=["documentation"],
             stake_amount=DELTA_SELF_STAKE,
             seed_utxo=f"{state['delta_seed_utxo']}#0"))

    # ── Epsilon tier: Unverified ──────────────────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 6/7: Epsilon → Unverified")
    print("=" * 64)
    # Epsilon self-stakes at min (10 AP3X) to appear in the indexer, then an
    # unresolved 25 AP3X challenge (Phase 7) will subtract from net_score,
    # clamping it to 0 → Unverified tier.
    step(state, "epsilon_seed_utxo", "Epsilon seed UTXO",
         lambda: client.create_seed_utxo(epsilon_did, ["research"]))
    step(state, "epsilon_stake_tx", f"Epsilon CreateStake ({EPSILON_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=epsilon_did,
             capabilities=["research"],
             stake_amount=EPSILON_SELF_STAKE,
             seed_utxo=f"{state['epsilon_seed_utxo']}#0"))

    # ── Sybil cluster: mutual endorsement ring ─────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 7/7: Sybil cluster + open challenge")
    print("=" * 64)

    step(state, "sybil1_seed_utxo", "Sybil1 seed UTXO",
         lambda: client.create_seed_utxo(sybil1_did, ["code_review"]))
    step(state, "sybil1_stake_tx", f"Sybil1 CreateStake ({SYBIL_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=sybil1_did,
             capabilities=["code_review"],
             stake_amount=SYBIL_SELF_STAKE,
             seed_utxo=f"{state['sybil1_seed_utxo']}#0"))

    step(state, "sybil2_seed_utxo", "Sybil2 seed UTXO",
         lambda: client.create_seed_utxo(sybil2_did, ["code_review"]))
    step(state, "sybil2_stake_tx", f"Sybil2 CreateStake ({SYBIL_SELF_STAKE // AP3X} AP3X)",
         lambda: client.create_stake(
             agent_did=sybil2_did,
             capabilities=["code_review"],
             stake_amount=SYBIL_SELF_STAKE,
             seed_utxo=f"{state['sybil2_seed_utxo']}#0"))

    step(state, "sybil1_endorses_sybil2",
         f"Sybil1 → Sybil2 endorsement ({ENDORSE_SYBIL // AP3X} AP3X)",
         lambda: client.mint_endorsement(
             endorser_did=sybil1_did, target_did=sybil2_did,
             capabilities=["code_review"], stake_amount=ENDORSE_SYBIL))
    step(state, "sybil2_endorses_sybil1",
         f"Sybil2 → Sybil1 endorsement ({ENDORSE_SYBIL // AP3X} AP3X)  [completes cycle]",
         lambda: client.mint_endorsement(
             endorser_did=sybil2_did, target_did=sybil1_did,
             capabilities=["code_review"], stake_amount=ENDORSE_SYBIL))

    # ── Open challenge against Epsilon (unresolved, pushes to Unverified) ─
    if "open_challenge_mint_tx" not in state:
        print(f"\n--- Open challenge vs Epsilon ({OPEN_CHALLENGE_STAKE // AP3X} AP3X, unresolved) ---")
        try:
            evidence = b"demo_open_challenge_evidence"
            ehash = hashlib.blake2b(evidence, digest_size=32).hexdigest()
            tx_hash, cd = client.mint_challenge(
                challenger_did=gamma_did,
                target_did=epsilon_did,
                capability="research",
                stake_amount=OPEN_CHALLENGE_STAKE,
                evidence_hash=ehash,
                evidence_uri="ipfs://demo-open-challenge",
            )
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback; traceback.print_exc()
            save_state(state)
            sys.exit(1)
        state["open_challenge_mint_tx"] = tx_hash
        state["open_challenge_datum_cbor"] = cd.to_cbor().hex()
        save_state(state)
        print(f"  OK: tx {tx_hash[:16]}...   (left Open — will appear on Challenges tab)")
        time.sleep(TX_WAIT)
    else:
        print(f"\n[skip] Open challenge — already minted ({state['open_challenge_mint_tx'][:16]}...)")

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("DEMO SEED COMPLETE")
    print("=" * 64)
    print(f"Alpha   (Elite      ): {alpha_did}")
    print(f"Beta    (Trusted    ): {beta_did}")
    print(f"Gamma   (Established): {gamma_did}")
    print(f"Delta   (Novice     ): {delta_did}")
    print(f"Epsilon (Unverified ): {epsilon_did}")
    print(f"Sybil1  (Novice+flag): {sybil1_did}")
    print(f"Sybil2  (Novice+flag): {sybil2_did}")
    print()
    print("Next: run the indexer against mainnet to populate the dashboard:")
    print("  cd Module-3")
    print("  PYTHONPATH=python python -m indexer --network mainnet \\")
    print("    --db dashboard/reputation_index.db --once")
    print()
    final_utxos = get_wallet_utxos(context, wallet_addr)
    final_total = sum(u.output.amount.coin for u in final_utxos) / AP3X
    print(f"Final wallet: {final_total:.2f} AP3X ({len(final_utxos)} UTxOs)")


if __name__ == "__main__":
    main()
