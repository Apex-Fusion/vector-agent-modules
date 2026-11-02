#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Full End-to-End Smoke Test via Ogmios

Remote-only test with PyCardano + Ogmios HTTP, matching Module 1 and Module 6.

The test uses two agents whose identity records exist (did:vector). Register
them beforehand with the identity tools. Before the first transaction the
test checks:
  - each record has effective status Active and lists the capabilities that
    the test stakes (Agent A: code_review, testing);
  - a signing key of each controller is available: the wallet key, or the
    key file of --agent-a-controller-skey / --agent-b-controller-skey;
  - Agent B's record is at least min_agent_age old (B challenges A);
  - the wallet key is the oracle key of the deployment (it resolves the
    challenges and signs the history bonuses).

Steps:
  1. Create seed UTXO at reputation address
  2. CreateStake for Agent A
  3. MintEndorsement from Agent B -> Agent A
  4. MintChallenge from Agent B against Agent A
  5. ResolveChallenge (oracle: CapabilityVerified)
  6. DistributeOutcome (CapabilityVerified, ChallengeWon bonus for A)
  7. MintChallenge #2 (B challenges A again)
  8. ResolveChallenge #2 (oracle: CapabilityFalsified)
  9. SlashEndorsement (B's endorsement of A)
  10. DistributeOutcome + SlashStake (CapabilityFalsified, bonus for B)

Inputs (an argument, else the environment variable):
  --agent-a                  M3_SMOKE_AGENT_A                  DID or 64 hex
  --agent-b                  M3_SMOKE_AGENT_B                  DID or 64 hex
  --agent-a-controller-skey  M3_SMOKE_AGENT_A_CONTROLLER_SKEY  optional key file
  --agent-b-controller-skey  M3_SMOKE_AGENT_B_CONTROLLER_SKEY  optional key file
  --wallet-skey              M3_WALLET_SKEY                    wallet key file

The run state (deploy/smoke_state_ogmios.json) keeps the completed steps.
A run resumes from it. A state of another deployment or other agents stops
the run: move that file to a different path.

Usage:
    export M3_SMOKE_AGENT_A=did:vector:<64 hex>
    export M3_SMOKE_AGENT_B=did:vector:<64 hex>
    cd Module-3 && PYTHONPATH=python:$PYTHONPATH python3 scripts/smoke_test_ogmios.py
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from pycardano import PaymentSigningKey

# Add python/ to path so we can import the SDK
MODULE3_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MODULE3_ROOT / "python"))

from reputation_staking import ReputationStakingClient  # noqa: E402
from reputation_staking import vector_identity as vi  # noqa: E402
from reputation_staking.constants import (  # noqa: E402
    MAINNET_OGMIOS_URL,
    MAINNET_SUBMIT_URL,
    MAINNET_SYSTEM_START_UNIX_S,
    OGMIOS_URL,
    TX_SUBMIT_URL,
)
from reputation_staking.deployment import DeploymentStateError  # noqa: E402
from reputation_staking.ogmios_backend import (  # noqa: E402
    OgmiosHttpContext,
    get_wallet_utxos,
    load_wallet,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ── Networks ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Network:
    name: str
    ogmios_url: str
    submit_url: str
    deploy_dir: Path
    wallet_skey: str
    system_start: Optional[int] = None

    @property
    def deploy_state_file(self) -> Path:
        return self.deploy_dir / "deploy_state.json"


TESTNET = Network(
    name="testnet",
    ogmios_url=OGMIOS_URL,
    submit_url=TX_SUBMIT_URL,
    deploy_dir=MODULE3_ROOT / "deploy",
    wallet_skey="/tmp/m3dev_payment.skey",
)

MAINNET = Network(
    name="MAINNET",
    ogmios_url=MAINNET_OGMIOS_URL,
    submit_url=MAINNET_SUBMIT_URL,
    deploy_dir=MODULE3_ROOT / "deploy" / "mainnet",
    wallet_skey="/tmp/m3mainnet_payment.skey",
    system_start=MAINNET_SYSTEM_START_UNIX_S,
)


def use_network(network: Network):
    """Set the system start of the network before any slot conversion."""
    if network.system_start is not None:
        import reputation_staking.constants as _constants
        import reputation_staking.utils as _utils
        _utils.SYSTEM_START_UNIX_S = network.system_start
        _constants.SYSTEM_START_UNIX_S = network.system_start


# ── Inputs ───────────────────────────────────────────────────────────────────


def env_or(value: Optional[str], env_name: str) -> Optional[str]:
    return value if value else (os.environ.get(env_name) or None)


def agent_id(value: Optional[str], what: str) -> str:
    """The 64-hex identifier of a DID or identifier. Stops for a missing or
    malformed value."""
    if not value:
        sys.exit(
            f"ERROR: {what} is not set. Give the DID (did:vector:<64 hex>) of an "
            "identity record that exists. Register the agent with the identity "
            "tools first."
        )
    try:
        return vi.normalize_agent_id(value).hex()
    except (TypeError, ValueError) as exc:
        sys.exit(f"ERROR: {what}: {exc}")


def read_skey(path: Optional[str]) -> Optional[PaymentSigningKey]:
    if not path:
        return None
    if not Path(path).exists():
        sys.exit(f"ERROR: key file not found: {path}")
    return PaymentSigningKey.load(path)


def load_run_state(path: Path, identity_hash: str, agents: Dict[str, str]) -> dict:
    """The resumable state of a run. A state of another deployment or other
    agents stops the run."""
    if path.exists():
        with open(path) as f:
            state = json.load(f)
        if state.get("identity_hash") != identity_hash or state.get("agents") != agents:
            sys.exit(
                f"ERROR: {path} belongs to another deployment or to other agents. "
                "Move it to a different path to start a new run."
            )
        return state
    return {"identity_hash": identity_hash, "agents": agents}


def save_run_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def check_agent(client: ReputationStakingClient, name: str, agent_hex: str,
                capabilities, controller_skey, challenger: bool = False):
    """Check one agent before any transaction (see the module docstring)."""
    try:
        record = client.check_agent(
            agent_hex, capabilities, controller_skey, challenger=challenger,
        )
    except (vi.IdentityError, RuntimeError, ValueError) as exc:
        sys.exit(f"ERROR: {name} ({vi.did_from_id(agent_hex)}): {exc}")
    print(f"  {name}: {record.did} Active, lists {', '.join(capabilities) or '-'}, "
          f"controller key hash {vi.controller_key_hash(record.datum).hex()}"
          + (", old enough to challenge" if challenger else ""))
    return record


def require_oracle_wallet(client: ReputationStakingClient):
    wallet = bytes(client.vkey.hash()).hex()
    if wallet != client.deployment.oracle_vkey_hash:
        sys.exit(
            f"ERROR: the wallet key hash {wallet} is not the oracle key "
            f"{client.deployment.oracle_vkey_hash} of the deployment. Use the deploy wallet."
        )


# ── Steps ────────────────────────────────────────────────────────────────────


class Steps:
    """Run the steps in order. A completed step (its key in the run state)
    is skipped; the first failure stops the run."""

    def __init__(self, state: dict, path: Path):
        self.state = state
        self.path = path
        self.passed = 0
        self.failed = 0

    def run(self, key: str, label: str, fn: Callable[[], object]) -> bool:
        if key in self.state:
            print(f"\n--- {label}: already done ({self.state[key]}) ---")
            self.passed += 1
            return True
        print(f"\n--- {label} ---")
        try:
            result = fn()
        except Exception as exc:
            self.failed += 1
            print(f"  FAIL: {exc}")
            traceback.print_exc()
            save_run_state(self.path, self.state)
            return False
        self.state[key] = result
        save_run_state(self.path, self.state)
        self.passed += 1
        print(f"  PASS: {result}")
        return True

    def report(self, network: Network):
        total = self.passed + self.failed
        print(f"\n{'=' * 60}")
        print(f"Results ({network.name}): {self.passed}/{total} passed, {self.failed} failed")
        if self.failed == 0 and self.passed > 0:
            print("Full lifecycle smoke test PASSED!")
        elif self.failed > 0:
            print("Some steps failed -- check errors above.")
            print("Re-run to resume from the last successful step.")
        print("=" * 60)


def evidence_hash(text: bytes) -> str:
    return hashlib.blake2b(text, digest_size=32).hexdigest()


# ── Main smoke test ──────────────────────────────────────────────────────────


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Module 3 smoke test (Ogmios)")
    parser.add_argument("--agent-a", help="Agent A: DID or 64-hex identifier (M3_SMOKE_AGENT_A)")
    parser.add_argument("--agent-b", help="Agent B: DID or 64-hex identifier (M3_SMOKE_AGENT_B)")
    parser.add_argument("--agent-a-controller-skey",
                        help="Key file of A's controller (M3_SMOKE_AGENT_A_CONTROLLER_SKEY)")
    parser.add_argument("--agent-b-controller-skey",
                        help="Key file of B's controller (M3_SMOKE_AGENT_B_CONTROLLER_SKEY)")
    parser.add_argument("--wallet-skey", help="Wallet key file (M3_WALLET_SKEY)")
    return parser.parse_args(argv)


def smoke_test(network: Network, argv=None):
    args = parse_args(argv)
    use_network(network)

    print("=" * 60)
    print(f"Module 3: Full End-to-End Smoke Test ({network.name}, Ogmios)")
    print("=" * 60)

    agent_a = agent_id(env_or(args.agent_a, "M3_SMOKE_AGENT_A"), "Agent A (M3_SMOKE_AGENT_A)")
    agent_b = agent_id(env_or(args.agent_b, "M3_SMOKE_AGENT_B"), "Agent B (M3_SMOKE_AGENT_B)")
    if agent_a == agent_b:
        sys.exit("ERROR: Agent A and Agent B must be different agents")
    key_a = read_skey(env_or(args.agent_a_controller_skey, "M3_SMOKE_AGENT_A_CONTROLLER_SKEY"))
    key_b = read_skey(env_or(args.agent_b_controller_skey, "M3_SMOKE_AGENT_B_CONTROLLER_SKEY"))
    wallet_skey = env_or(args.wallet_skey, "M3_WALLET_SKEY") or network.wallet_skey
    if not Path(wallet_skey).exists():
        sys.exit(f"ERROR: Wallet key not found at {wallet_skey}")

    context = OgmiosHttpContext(ogmios_url=network.ogmios_url)
    skey, vkey, wallet_addr = load_wallet(wallet_skey)
    try:
        client = ReputationStakingClient.from_deploy_state(
            str(network.deploy_state_file), context, skey,
            ogmios_url=network.ogmios_url, submit_url=network.submit_url,
        )
    except DeploymentStateError as exc:
        sys.exit(f"ERROR: {exc}")

    print("\n--- Environment Check ---")
    print(f"  Tip slot: {context.last_block_slot}")
    wallet_utxos = get_wallet_utxos(context, wallet_addr)
    total_lovelace = sum(u.output.amount.coin for u in wallet_utxos)
    print(f"  Wallet: {total_lovelace / 1_000_000:.2f} AP3X ({len(wallet_utxos)} UTxOs)")
    print(f"  Address: {wallet_addr}")
    print(f"  Reputation: {client.reputation_hash} at {client.reputation_addr}")
    print(f"  Endorsement: {client.endorsement_hash} at {client.endorsement_addr}")
    print(f"  Identity registry: {client.deployment.identity_hash} at {client.identity_addr}")

    # ── Checks before any transaction ────────────────────────────────────
    print("\n--- Identity records ---")
    require_oracle_wallet(client)
    check_agent(client, "Agent A", agent_a, ["code_review", "testing"], key_a)
    check_agent(client, "Agent B", agent_b, [], key_b, challenger=True)

    state_file = network.deploy_dir / "smoke_state_ogmios.json"
    state = load_run_state(
        state_file, client.deployment.identity_hash, {"a": agent_a, "b": agent_b},
    )
    steps = Steps(state, state_file)

    def stake_a():
        return client.create_stake(
            agent_a, ["code_review", "testing"], 10_000_000,
            seed_utxo=f"{state['seed_utxo_tx']}#0", controller_skey=key_a,
        )

    def challenge(text: bytes, uri: str):
        tx_hash, _ = client.mint_challenge(
            agent_b, agent_a, "code_review", 25_000_000,
            evidence_hash(text), uri, controller_skey=key_b,
        )
        return tx_hash

    plan = [
        ("seed_utxo_tx", "Step 1: Create seed UTXO at reputation address",
         lambda: client.create_seed_utxo(agent_a, ["code_review"])),
        ("create_stake_tx", "Step 2: CreateStake (Agent A, 10 AP3X)", stake_a),
        ("mint_endorsement_tx", "Step 3: MintEndorsement (B endorses A, 5 AP3X)",
         lambda: client.mint_endorsement(
             agent_b, agent_a, ["code_review"], 5_000_000, controller_skey=key_b)),
        ("mint_challenge_tx", "Step 4: MintChallenge (B challenges A on code_review, 25 AP3X)",
         lambda: challenge(b"smoke_test_evidence_for_code_review_challenge",
                           "ipfs://smoke-test-evidence")),
        ("resolve_challenge_tx", "Step 5: ResolveChallenge (Oracle: CapabilityVerified)",
         lambda: client.resolve_challenge(agent_b, agent_a, "code_review", 0)),
        ("distribute_outcome_tx", "Step 6: DistributeOutcome (CapabilityVerified, bonus for A)",
         lambda: client.distribute_outcome(
             agent_b, agent_a, "code_review", mint_bonus=True, bonus_controller_skey=key_a)),
        ("mint_challenge2_tx", "Step 7: MintChallenge #2 (B challenges A on code_review)",
         lambda: challenge(b"capability_falsified_test_evidence_agent_a_cannot_code_review",
                           "ipfs://falsified-test-evidence")),
        ("resolve_challenge2_tx", "Step 8: ResolveChallenge #2 (Oracle: CapabilityFalsified)",
         lambda: client.resolve_challenge(agent_b, agent_a, "code_review", 1)),
        ("slash_endorsement_tx", "Step 9: SlashEndorsement (B's endorsement of A)",
         lambda: client.slash_endorsement(
             agent_b, agent_a, f"{state['resolve_challenge2_tx']}#0")),
        ("distribute_falsified_tx",
         "Step 10: DistributeOutcome + SlashStake (CapabilityFalsified, bonus for B)",
         lambda: client.distribute_falsified_outcome(
             agent_b, agent_a, "code_review", mint_bonus=True, bonus_controller_skey=key_b)),
    ]
    for key, label, fn in plan:
        if not steps.run(key, label, fn):
            break
    steps.report(network)


if __name__ == "__main__":
    smoke_test(TESTNET)
