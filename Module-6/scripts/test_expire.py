"""
Module 6: Test Expire Proposal

Expires a submitted proposal after its review window (ExpireProposal,
permissionless). This is a time-dependent test that must be run separately
from the main smoke test: the lower bound of the expiry transaction must be
after submitted_at + review_window.

The expiry burns the proposal token, returns the stake to the proposer
credential and spends the node of the proposer in the activity list
(SpendActivity: the node continues with one active proposal less and the
same next_did; the contract refuses the node of another agent). The script
reads the activity list from the chain (governance_identity
.find_activity_list) and checks the node of the proposer of the proposal
before the expiry. The expiry reads no identity record.
GovernanceClient.expire_proposal of the Python SDK (0.2.0 or later) builds
it: the client finds the node, the GovernanceParams record, the CrossRefs
output and the reference scripts of the deployment itself.

Usage:
    # The proposal output, for example from scripts/test_expire_e2e.py
    nix-shell shell.nix --run "python scripts/test_expire.py <proposal_tx_hash> [output_index]"

    # Or the active proposal with the latest submitted_at:
    nix-shell shell.nix --run "python scripts/test_expire.py --latest"

    --state: the deploy state (default:
    deploy/identity/state/<network>/deploy_state.json of scripts/deploy.py;
    --network, default mainnet). The state of the previous deployment is used
    only with --state wallets/deploy_state.json.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Iterable, Optional

from dotenv import load_dotenv
from pycardano import TransactionId, TransactionInput, UTxO

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

import governance_identity as gi  # noqa: E402
import vector_identity as vid  # noqa: E402
from deploy_common import add_state_arguments, read_state, state_endpoints, state_path  # noqa: E402


def latest_proposal(utxos: Iterable[UTxO], deployment: gi.GovernanceDeployment) -> Optional[UTxO]:
    """The submitted proposal (it holds its proposal token) in the state Open
    or Amended with the latest submitted_at, or None."""
    found = []
    for utxo in utxos:
        if gi.proposal_token(utxo, deployment) is None or utxo.output.datum is None:
            continue
        datum = gi.ProposalDatum.from_data(utxo.output.datum)
        if datum.state in gi.ACTIVE_STATES:
            found.append((datum.submitted_at, str(utxo.input), utxo))
    return max(found, key=lambda entry: entry[:2])[2] if found else None


def proposer_node(activity_list: gi.ActivityList, datum: gi.ProposalDatum) -> gi.ActivityRecord:
    """The node of the proposer of ``datum`` in the activity list: the expiry
    spends it and continues it with active_proposal_count - 1. Refuses a
    proposer without a node or with a count of 0."""
    node = activity_list.node(datum.proposer_did)
    if node is None:
        raise gi.GovernanceClientError(
            f"{vid.did_from_id(datum.proposer_did)} has no node in the activity list"
        )
    if node.datum.active_proposal_count < 1:
        raise gi.GovernanceClientError(
            f"the node of {vid.did_from_id(datum.proposer_did)} has active_proposal_count "
            f"{node.datum.active_proposal_count}; the expiry needs at least 1"
        )
    return node


def expiry_inputs(
    utxos: Iterable[UTxO], deployment: gi.GovernanceDeployment, target: Optional[TransactionInput],
) -> tuple[UTxO, gi.ProposalDatum, gi.ActivityRecord]:
    """The proposal to expire (``target``, else ``latest_proposal``), its
    datum and the node of its proposer, from the UTxOs at the proposal
    validator. Raises GovernanceClientError."""
    utxos = list(utxos)
    if target is None:
        proposal = latest_proposal(utxos, deployment)
        if proposal is None:
            raise gi.MissingReferenceError("no Open or Amended proposal at the proposal validator")
    else:
        proposal = gi.find_utxo(utxos, target)
        if proposal is None or gi.proposal_token(proposal, deployment) is None:
            raise gi.MissingReferenceError(f"{target} is not a submitted proposal at the proposal validator")
    datum = gi.ProposalDatum.from_data(proposal.output.datum)
    if datum.state not in gi.ACTIVE_STATES:
        raise gi.GovernanceClientError(f"{proposal.input} is {datum.state.name}, not Open or Amended")
    return proposal, datum, proposer_node(gi.find_activity_list(utxos, deployment), datum)


def sdk_ref(ref: TransactionInput) -> dict:
    return {"tx_hash": str(ref.transaction_id), "output_index": ref.index}


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Module 6: expire a proposal (ExpireProposal)")
    parser.add_argument("tx_hash", nargs="?", help="the transaction of the proposal output")
    parser.add_argument("output_index", nargs="?", type=int, default=0, help="its output index (default 0)")
    parser.add_argument("--latest", action="store_true",
                        help="the active proposal with the latest submitted_at")
    add_state_arguments(parser)
    return parser.parse_args(argv)


async def test_expire(argv: list) -> bool:
    args = parse_args(argv[1:])
    if args.tx_hash:
        target = TransactionInput(TransactionId(bytes.fromhex(args.tx_hash)), args.output_index)
    elif args.latest:
        target = None
    else:
        print("Usage: python scripts/test_expire.py <tx_hash> [output_index]")
        print("       python scripts/test_expire.py --latest")
        return False

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient, GovernanceConfigError, GovernanceDeployment

    path = state_path(args)
    state = read_state(path, args.network)
    endpoints = state_endpoints(state, args.network)
    try:
        deployment = gi.GovernanceDeployment.from_state(state, str(path))
        sdk_deployment = GovernanceDeployment.from_deploy_state(state)
    except (gi.GovernanceClientError, GovernanceConfigError) as e:
        print(f"[ERROR] {e}")
        return False

    async with VectorAgent(
        ogmios_url=endpoints["ogmios_url"],
        submit_url=endpoints["submit_url"],
        skey_path=str(Path("wallets/payment.skey").absolute()),
    ) as agent:
        context = agent.context
        gov = GovernanceClient(agent, sdk_deployment)

        try:
            utxos = await context.async_utxos(str(deployment.proposal_spend.address))
            proposal, datum, node = expiry_inputs(utxos, deployment, target)
        except gi.GovernanceClientError as e:
            print(f"[ERROR] {e}")
            return False
        print(f"[INFO] Proposal: {proposal.input}")
        print(f"[INFO] Proposer: {vid.did_from_id(datum.proposer_did)}")
        print(f"[INFO] Activity node: {node.utxo.input} (active proposals: {node.datum.active_proposal_count})")

        clock = gi.SlotClock.from_genesis(await context.async_genesis_param())
        current_posix_ms = clock.posix_ms(await context.async_last_block_slot())
        expiry_time = datum.submitted_at + datum.review_window
        print(f"[INFO] submitted_at:  {datum.submitted_at} (POSIX ms)")
        print(f"[INFO] review_window: {datum.review_window} ms")
        print(f"[INFO] expiry_time:   {expiry_time}")
        print(f"[INFO] current_time:  {current_posix_ms} (POSIX ms)")
        if current_posix_ms <= expiry_time:
            remaining_ms = expiry_time - current_posix_ms
            print("\n[WAIT] Review window has NOT elapsed yet.")
            print(f"       Time remaining: {remaining_ms / 3_600_000:.1f} hours ({remaining_ms / 1000:.0f} seconds)")
            print("       Re-run this script after the window expires.")
            return False

        print("\n[INFO] Expiring proposal...")
        result = await gov.expire_proposal(sdk_ref(proposal.input))
        print(f"[PASS] Proposal expired: {result['tx_hash']}")
        print(f"       Explorer: {endpoints['explorer_url']}/tx/{result['tx_hash']}")
        return True


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(test_expire(sys.argv)) else 1)
