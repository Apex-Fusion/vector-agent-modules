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

The 7 agents are identity records (did:vector) that exist before the run.
Register them with the identity tools. Before the first transaction the
script checks that each record is Active and lists the capabilities that the
agent stakes, that a signing key of each controller is available (the wallet
key or a given key file), that the challengers (beta, gamma, delta) are at
least min_agent_age old, and that the wallet key is the oracle key.

Inputs, for NAME in alpha beta gamma delta epsilon sybil1 sybil2:
  --agent NAME=DID              or M3_DEMO_<NAME>                 (required)
  --controller-skey NAME=PATH   or M3_DEMO_<NAME>_CONTROLLER_SKEY (when the
                                   controller key is not the wallet key)
  --wallet-skey PATH            or M3_WALLET_SKEY (default /tmp/m3mainnet_payment.skey)

End state after run (approximate):
  Alpha    -> Elite       (stake 50  + endorsements 150 + bonus 1810 - 10 open)
  Beta     -> Trusted     (stake 50  + endorsements 150 + bonus 300)
  Gamma    -> Established (stake 110)
  Delta    -> Novice      (stake 10)
  Epsilon  -> Unverified  (stake 10, one open 25 AP3X challenge)
  Sybil1   -> Novice + sybil flag  (mutual cycle with Sybil2)
  Sybil2   -> Novice + sybil flag  (mutual cycle with Sybil1)

Resumable via deploy/mainnet/demo_state.json. A state of another deployment
or other agents stops the run: move that file to a different path.

Usage:
    cd Module-3 && PYTHONPATH=python:$PYTHONPATH python3 scripts/demo_seed_mainnet.py \
        --agent alpha=did:vector:<64 hex> ... --agent sybil2=did:vector:<64 hex>
"""

import argparse
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

MODULE3_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MODULE3_ROOT / "python"))
sys.path.insert(0, str(Path(__file__).parent))

from smoke_test_ogmios import (  # noqa: E402
    MAINNET,
    agent_id,
    check_agent,
    load_run_state,
    read_skey,
    require_oracle_wallet,
    save_run_state,
    use_network,
)

use_network(MAINNET)

from reputation_staking import ReputationStakingClient  # noqa: E402
from reputation_staking.deployment import DeploymentStateError  # noqa: E402
from reputation_staking.ogmios_backend import (  # noqa: E402
    OgmiosHttpContext,
    get_wallet_utxos,
    load_wallet,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEPLOY_DIR = MAINNET.deploy_dir
DEPLOY_STATE_FILE = MAINNET.deploy_state_file
DEMO_STATE_FILE = DEPLOY_DIR / "demo_state.json"

TX_WAIT = 25  # seconds between txs

# Agents: the capabilities that each one stakes (its record lists them) and
# whether it challenges another agent (min_agent_age).
AGENTS = {
    "alpha": (["code_review", "testing"], False),
    "beta": (["security_audit"], True),
    "gamma": (["data_analysis"], True),
    "delta": (["documentation"], True),
    "epsilon": (["research"], False),
    "sybil1": (["code_review"], False),
    "sybil2": (["code_review"], False),
}

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
RESOLVED_CHALLENGE_STAKE = 25 * AP3X  # minted + resolved, not distributed → visible on Challenges tab


# ── Inputs ───────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(description="Module 3 demo seed (mainnet)")
    parser.add_argument("--agent", action="append", default=[], metavar="NAME=DID",
                        help="Agent DID or 64-hex identifier (else M3_DEMO_<NAME>)")
    parser.add_argument("--controller-skey", action="append", default=[], metavar="NAME=PATH",
                        help="Key file of the agent's controller (else M3_DEMO_<NAME>_CONTROLLER_SKEY)")
    parser.add_argument("--wallet-skey", help="Wallet key file (M3_WALLET_SKEY)")
    return parser.parse_args()


def named_values(pairs, what):
    values = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or name not in AGENTS:
            sys.exit(f"ERROR: {what} {pair!r}: use NAME=VALUE with NAME in {', '.join(AGENTS)}")
        values[name] = value
    return values


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
        save_run_state(DEMO_STATE_FILE, state)
        sys.exit(1)
    state[key] = result
    save_run_state(DEMO_STATE_FILE, state)
    print(f"  OK: {result if isinstance(result, str) else 'complete'}")
    if wait:
        print(f"  waiting {wait}s for tx confirmation...")
        time.sleep(wait)
    return result


def evidence_hash(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=32).hexdigest()


# ── Challenge cycle helper (mint -> resolve Verified -> distribute) ────────

def run_won_challenge(state, cycle_key, label, client, keys, challenger, target,
                      ids, capability, stake_amount):
    """Full win cycle: target wins → gets a history bonus of `stake_amount` points."""
    step(state, f"{cycle_key}_mint_tx", f"{label} — mint challenge ({stake_amount // AP3X} AP3X)",
         lambda: client.mint_challenge(
             ids[challenger], ids[target], capability, stake_amount,
             evidence_hash(f"{cycle_key}_evidence"), f"ipfs://demo-{cycle_key}",
             controller_skey=keys[challenger])[0])
    step(state, f"{cycle_key}_resolve_tx", f"{label} — resolve Verified",
         lambda: client.resolve_challenge(ids[challenger], ids[target], capability, 0))
    step(state, f"{cycle_key}_distribute_tx",
         f"{label} — distribute outcome (mints history bonus for target)",
         lambda: client.distribute_outcome(
             ids[challenger], ids[target], capability,
             mint_bonus=True, bonus_controller_skey=keys[target]))


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("Module 3 Demo Seed — MAINNET")
    print("Showcases all 5 reputation tiers + sybil detection + active chg")
    print("=" * 64)

    args = parse_args()
    given_ids = named_values(args.agent, "--agent")
    given_keys = named_values(args.controller_skey, "--controller-skey")
    ids = {
        name: agent_id(given_ids.get(name) or os.environ.get(f"M3_DEMO_{name.upper()}"),
                       f"agent {name} (M3_DEMO_{name.upper()})")
        for name in AGENTS
    }
    if len(set(ids.values())) != len(ids):
        sys.exit("ERROR: the 7 agents must be different agents")
    keys = {
        name: read_skey(given_keys.get(name)
                        or os.environ.get(f"M3_DEMO_{name.upper()}_CONTROLLER_SKEY"))
        for name in AGENTS
    }
    wallet_skey = args.wallet_skey or os.environ.get("M3_WALLET_SKEY") or MAINNET.wallet_skey
    if not Path(wallet_skey).exists():
        sys.exit(f"Wallet key missing: {wallet_skey}")

    context = OgmiosHttpContext(ogmios_url=MAINNET.ogmios_url)
    skey, vkey, wallet_addr = load_wallet(wallet_skey)
    try:
        client = ReputationStakingClient.from_deploy_state(
            str(DEPLOY_STATE_FILE), context, skey,
            ogmios_url=MAINNET.ogmios_url, submit_url=MAINNET.submit_url,
        )
    except DeploymentStateError as exc:
        sys.exit(f"ERROR: {exc}")

    # Environment banner
    wallet_utxos = get_wallet_utxos(context, wallet_addr)
    total = sum(u.output.amount.coin for u in wallet_utxos) / AP3X
    print(f"\nWallet: {total:.2f} AP3X across {len(wallet_utxos)} UTxOs")
    print(f"Address: {wallet_addr}")
    print(f"Current slot: {context.last_block_slot}")
    print(f"State file: {DEMO_STATE_FILE}")

    # ── Identity records (before any transaction) ──────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 1/7: Check the identity records of the 7 agents")
    print("=" * 64)
    require_oracle_wallet(client)
    for name, (capabilities, challenger) in AGENTS.items():
        check_agent(client, name, ids[name], capabilities, keys[name], challenger=challenger)
    state = load_run_state(DEMO_STATE_FILE, client.deployment.identity_hash, ids)
    alpha_did, beta_did, gamma_did, delta_did = ids["alpha"], ids["beta"], ids["gamma"], ids["delta"]
    epsilon_did, sybil1_did, sybil2_did = ids["epsilon"], ids["sybil1"], ids["sybil2"]

    def stake(name, amount):
        caps = AGENTS[name][0]
        step(state, f"{name}_seed_utxo", f"{name} seed UTXO",
             lambda: client.create_seed_utxo(ids[name], caps))
        step(state, f"{name}_stake_tx", f"{name} CreateStake ({amount // AP3X} AP3X)",
             lambda: client.create_stake(
                 ids[name], caps, amount, seed_utxo=f"{state[f'{name}_seed_utxo']}#0",
                 controller_skey=keys[name]))

    def endorse(endorser, target, capability, amount):
        step(state, f"{endorser}_endorses_{target}",
             f"{endorser} → {target} endorsement ({amount // AP3X} AP3X)",
             lambda: client.mint_endorsement(
                 ids[endorser], ids[target], [capability], amount,
                 controller_skey=keys[endorser]))

    # ── Alpha tier: Elite via stake + endorsements + won challenges ─────────
    print("\n" + "=" * 64)
    print("PHASE 2/7: Alpha → Elite")
    print("=" * 64)
    stake("alpha", ALPHA_SELF_STAKE)
    # Fill Alpha endorsement cap (50 × 3 = 150) with 3 endorsements. The
    # endorsers need a stake of their own only for their tier.
    endorse("beta", "alpha", "code_review", ENDORSE_TO_ALPHA)
    endorse("gamma", "alpha", "testing", ENDORSE_TO_ALPHA)
    endorse("delta", "alpha", "code_review", ENDORSE_TO_ALPHA)
    for i, stake_ap3x in enumerate(ALPHA_CYCLE_STAKES, start=1):
        run_won_challenge(
            state, f"alpha_cycle_{i}",
            f"Alpha cycle {i}/{len(ALPHA_CYCLE_STAKES)} ({stake_ap3x} AP3X challenge)",
            client, keys, "beta", "alpha", ids, "code_review", stake_ap3x * AP3X,
        )

    # ── Beta tier: Trusted via stake + endorsements + 1 won challenge ───────
    print("\n" + "=" * 64)
    print("PHASE 3/7: Beta → Trusted")
    print("=" * 64)
    stake("beta", BETA_SELF_STAKE)
    run_won_challenge(
        state, "beta_cycle_1", "Beta cycle 1/1 (300 AP3X challenge)",
        client, keys, "gamma", "beta", ids, "security_audit", BETA_CHALLENGE_STAKE,
    )
    endorse("alpha", "beta", "security_audit", ENDORSE_TO_BETA)
    endorse("gamma", "beta", "security_audit", ENDORSE_TO_BETA)
    endorse("delta", "beta", "security_audit", ENDORSE_TO_BETA)

    # ── Gamma, Delta, Epsilon: simple stakes ───────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 4-6/7: Gamma → Established, Delta → Novice, Epsilon → Unverified")
    print("=" * 64)
    stake("gamma", GAMMA_SELF_STAKE)
    stake("delta", DELTA_SELF_STAKE)
    # Epsilon self-stakes at min (10 AP3X) to appear in the indexer, then an
    # unresolved 25 AP3X challenge (Phase 7) subtracts from net_score,
    # clamping it to 0 → Unverified tier.
    stake("epsilon", EPSILON_SELF_STAKE)

    # ── Sybil cluster: mutual endorsement ring ─────────────────────────────
    print("\n" + "=" * 64)
    print("PHASE 7/7: Sybil cluster + resolved and open challenges")
    print("=" * 64)
    stake("sybil1", SYBIL_SELF_STAKE)
    stake("sybil2", SYBIL_SELF_STAKE)
    endorse("sybil1", "sybil2", "code_review", ENDORSE_SYBIL)
    endorse("sybil2", "sybil1", "code_review", ENDORSE_SYBIL)

    # Resolved challenge (mint + resolve, no distribute): Delta challenges
    # Gamma's data_analysis. It stays Resolved (CapabilityFalsified) for the
    # Challenges tab; no history bonus, no tier change.
    step(state, "resolved_challenge_mint_tx",
         f"Resolved challenge: Delta vs Gamma data_analysis ({RESOLVED_CHALLENGE_STAKE // AP3X} AP3X) — mint",
         lambda: client.mint_challenge(
             delta_did, gamma_did, "data_analysis", RESOLVED_CHALLENGE_STAKE,
             evidence_hash("demo_resolved_challenge_evidence"),
             "ipfs://demo-resolved-challenge", controller_skey=keys["delta"])[0])
    step(state, "resolved_challenge_resolve_tx", "Resolved challenge — resolve Falsified",
         lambda: client.resolve_challenge(delta_did, gamma_did, "data_analysis", 1))

    # Open challenge against Epsilon (unresolved, pushes it to Unverified)
    step(state, "open_challenge_mint_tx",
         f"Open challenge vs Epsilon ({OPEN_CHALLENGE_STAKE // AP3X} AP3X, unresolved)",
         lambda: client.mint_challenge(
             gamma_did, epsilon_did, "research", OPEN_CHALLENGE_STAKE,
             evidence_hash("demo_open_challenge_evidence"),
             "ipfs://demo-open-challenge", controller_skey=keys["gamma"])[0])

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("DEMO SEED COMPLETE")
    print("=" * 64)
    for name, label in (("alpha", "Elite"), ("beta", "Trusted"), ("gamma", "Established"),
                        ("delta", "Novice"), ("epsilon", "Unverified"),
                        ("sybil1", "Novice+flag"), ("sybil2", "Novice+flag")):
        print(f"{name:<8}({label:<11}): did:vector:{ids[name]}")
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
