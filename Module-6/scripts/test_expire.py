"""
Module 6: Test Expire Proposal

Expires a submitted proposal after its review window (ExpireProposal,
permissionless). This is a time-dependent test that must be run separately
from the main smoke test: the lower bound of the expiry transaction must be
after submitted_at + review_window.

The expiry burns the proposal token, returns the stake to the proposer and
spends the node of the proposer in the activity list (SpendActivity: the
count goes down by one; the contract refuses the node of another agent). The
script reads the activity list from the chain
(governance_identity.find_activity_list) and takes the node of the proposer
of the proposal. It reads the GovernanceParams record and the CrossRefs
output of the deployment by their tokens (governance_identity
.load_references). The expiry reads no identity record;
GovernanceClient.validated_expire_proposal of the Python SDK builds it.

Usage:
    # The proposal output, for example from scripts/test_expire_e2e.py
    nix-shell shell.nix --run "python scripts/test_expire.py <proposal_tx_hash> [output_index]"

    # Or the active proposal with the latest submitted_at:
    nix-shell shell.nix --run "python scripts/test_expire.py --latest"
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

from dotenv import load_dotenv
from pycardano import TransactionId, TransactionInput, UTxO

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

import governance_identity as gi  # noqa: E402
import vector_identity as vid  # noqa: E402

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")


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


async def test_expire(argv: list) -> bool:
    if len(argv) > 1 and argv[1] != "--latest":
        target = TransactionInput(TransactionId(bytes.fromhex(argv[1])), int(argv[2]) if len(argv) > 2 else 0)
    elif "--latest" in argv:
        target = None
    else:
        print("Usage: python scripts/test_expire.py <tx_hash> [output_index]")
        print("       python scripts/test_expire.py --latest")
        return False

    state = json.load(open(DEPLOY_STATE_FILE))
    try:
        deployment = gi.GovernanceDeployment.from_state(state, str(DEPLOY_STATE_FILE))
    except gi.GovernanceClientError as e:
        print(f"[ERROR] {e}")
        return False
    validators = state.get("validators", {})

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient

    async with VectorAgent(
        ogmios_url=state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL")),
        submit_url=state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL")),
        skey_path=str(Path("wallets/payment.skey").absolute()),
    ) as agent:
        context = agent.context
        gov = GovernanceClient(
            agent,
            proposal_script_cbor=validators[gi.PROPOSAL_SPEND]["compiled_code"],
            proposal_mint_cbor=validators[gi.PROPOSAL_MINT]["compiled_code"],
        )
        proposal_spend_ref_tx = state.get("tx_hashes", {}).get("proposal_spend_ref", "")
        if proposal_spend_ref_tx:
            gov.set_reference_utxos({
                "proposal": {"tx_hash": proposal_spend_ref_tx, "output_index": 0, "address": state["wallet_address"]}
            })

        try:
            refs = await gi.load_references(context, deployment)
            utxos = await context.async_utxos(str(deployment.proposal_spend.address))
            proposal, datum, node = expiry_inputs(utxos, deployment, target)
        except gi.GovernanceClientError as e:
            print(f"[ERROR] {e}")
            return False
        # The expiry reads the params record and CrossRefs (no identity record)
        gov.set_governance_reference_inputs(gi.sdk_reference_inputs(refs.params.utxo, refs.cross_refs.utxo))
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
        result = await gov.validated_expire_proposal(
            utxo_ref=sdk_ref(proposal.input),
            activity_utxo_ref=sdk_ref(node.utxo.input),
            proposer_did=datum.proposer_did,
        )
        print(f"[PASS] Proposal expired: {result['tx_hash']}")
        explorer = state.get("explorer_url", "")
        if explorer:
            print(f"       Explorer: {explorer}/tx/{result['tx_hash']}")
        return True


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(test_expire(sys.argv)) else 1)
