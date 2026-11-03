"""The Module-6 builders against the compiled validators.

conftest.applied_state applies the committed blueprint (plutus.json) with the
GovernanceConfig of the test deployment (11 fields, as
scripts/deploy_common.py builds it). Each test builds a transaction with the
client builders (real script hashes, addresses and reference scripts), and
shared/python/aiken_simulate.simulate runs every redeemer with `aiken tx
simulate` (Aiken v1.1.21). The time base is the SlotClock of the fake chain.
"""

from __future__ import annotations

import dataclasses

import pytest
from pycardano import ExecutionUnits

import governance_identity as gi
import vector_identity as vid
from aiken_simulate import aiken_available, resolved_inputs, simulate
from conftest import (
    CRITIC,
    ENDORSER,
    PROPOSAL_SEED,
    PROPOSER,
    SYSTEM_START_MS,
    applied_state,
    key_pair,
    make_world,
    public_key,
    record_datum,
)

pytestmark = pytest.mark.skipif(not aiken_available(), reason="aiken is not on PATH")


@pytest.fixture(scope="module")
def state():
    return applied_state()


@pytest.fixture
def world(state):
    world = make_world(state)
    # aiken tx simulate reports the units; the fake chain only declares them
    world.chain.evaluate_tx = lambda tx: {
        f"{key.tag.name.lower()}:{key.index}": ExecutionUnits(4_000_000, 1_500_000_000)
        for key in tx.transaction_witness_set.redeemer.keys()
    }
    return world


def run(world, tx):
    pool = [u for utxos in world.chain.store.values() for u in utxos]
    return simulate(tx, resolved_inputs(tx, pool), zero_time_ms=SYSTEM_START_MS, slot_length_ms=1000)


def spend_purpose(tx, utxo) -> str:
    inputs = sorted(tx.transaction_body.inputs, key=lambda i: (bytes(i.transaction_id), i.index))
    return f"Spend[{inputs.index(utxo.input)}]"


def sign(world, prepared, *extra):
    return gi.sign_prepared(prepared, keys=[world.wallet, *extra], change_address=world.wallet_address)


def terms() -> gi.ProposalTerms:
    return gi.ProposalTerms(vid.blake2b_256(b"proposal"), "ipfs://QmProposal", 25_000_000, 604_800_000)


def submission(world, records, activity_list=None):
    """The lock (on the fake chain) and the prepared SubmitProposal."""
    window = world.window()
    activity_list = activity_list or world.activity_list()
    lock = gi.prepare_proposal_lock(
        world.chain, world.deployment, records=records, refs=world.refs, activity_list=world.activity_list(),
        terms=terms(), credential_key_hash=world.key_hash, window=window, wallet_address=world.wallet_address,
    )
    locked = world.locked(world.deployment.proposal_spend, lock.datum, lock.datum.stake_amount + gi.POSITION_DEPOSIT)
    prepared = gi.prepare_submit_proposal(
        world.chain, world.deployment, locked=locked, records=records, refs=world.refs,
        activity_list=activity_list, window=window, wallet_address=world.wallet_address,
    )
    return locked, prepared


# ── SubmitProposal and the activity list ────────────────────────────────────


def test_first_submission_inserts_after_the_head(world):
    head = world.activity_list().head
    locked, prepared = submission(world, [world.agent(PROPOSER)])
    tx = sign(world, prepared)
    result = run(world, tx)
    assert result.passed, result.describe()
    assert {v.purpose for v in result.verdicts} == {spend_purpose(tx, locked), spend_purpose(tx, head.utxo), "Mint[0]"}


def test_first_submission_inserts_between_two_nodes(world):
    world.head(PROPOSER)
    before = world.activity_utxo(PROPOSER, 1, 0, next_did=ENDORSER)
    world.activity_utxo(ENDORSER, 1, 0, byte=0x81)
    _, prepared = submission(world, [world.agent(CRITIC)])
    tx = sign(world, prepared)
    result = run(world, tx)
    assert result.passed, result.describe()
    assert gi.ActivityDatum.from_data(tx.transaction_body.outputs[2].datum).next_did == CRITIC
    assert spend_purpose(tx, before.utxo) in {v.purpose for v in result.verdicts}


def test_second_submission_of_the_same_agent(world):
    window = world.window()
    world.head(PROPOSER)
    node = world.activity_utxo(PROPOSER, 1, window.lo - 2 * 86_400_000, next_did=ENDORSER)
    world.activity_utxo(ENDORSER, 1, 0, byte=0x81)
    _, prepared = submission(world, [world.agent(PROPOSER)])
    tx = sign(world, prepared)
    result = run(world, tx)
    assert result.passed, result.describe()
    assert spend_purpose(tx, node.utxo) in {v.purpose for v in result.verdicts}


def test_second_insertion_of_an_agent_with_a_node_fails(world):
    """The client view hides the node of PROPOSER, so the builder inserts it
    again after the head. The head names PROPOSER: SubmitProposal fails."""
    world.head(PROPOSER)
    world.activity_utxo(PROPOSER, 1, 0)
    real = world.activity_list()
    stale_view = gi.ActivityList(gi.ActivityRecord(real.head.utxo, dataclasses.replace(real.head.datum, next_did=b"")))
    locked, prepared = submission(world, [world.agent(PROPOSER)], stale_view)
    tx = sign(world, prepared)
    result = run(world, tx)
    assert not result.passed
    assert result.failing == [spend_purpose(tx, locked)], result.describe()


# ── Critiques and endorsements name the proposal by its seed ────────────────


def positions(world):
    """The proposer (wallet key), a proposal with the seed PROPOSAL_SEED, and
    another agent with its own key."""
    proposer = world.agent(PROPOSER)
    proposal = world.proposal_utxo(world.proposal_datum(submitted_at=SYSTEM_START_MS))
    return proposer, proposal


def test_critique_with_the_seed(world):
    proposer, proposal = positions(world)
    critic_key = key_pair()
    critic = world.agent(CRITIC, record_datum(public_key(critic_key)), byte=0x63)
    window = world.window()
    lock = gi.prepare_critique_lock(
        world.chain, world.deployment, records=[critic], refs=world.refs, proposal=proposal, seed=PROPOSAL_SEED,
        terms=gi.CritiqueTerms(vid.blake2b_256(b"critique"), "ipfs://QmCritique", 5_000_000),
        credential_key_hash=critic_key.key_hash, window=window, wallet_address=world.wallet_address,
        proposer_record=proposer,
    )
    locked = world.locked(world.deployment.critique_spend, lock.datum, 7_000_000)
    tx = sign(world, gi.prepare_submit_critique(
        world.chain, world.deployment, locked=locked, records=[critic], refs=world.refs, proposal=proposal,
        window=window, wallet_address=world.wallet_address, proposer_record=proposer,
    ), critic_key)
    result = run(world, tx)
    assert result.passed, result.describe()
    assert {v.purpose for v in result.verdicts} == {spend_purpose(tx, locked), "Mint[0]"}


def endorsement(world, with_proposer_record=True):
    proposer, proposal = positions(world)
    endorser_key = key_pair()
    endorser = world.agent(ENDORSER, record_datum(public_key(endorser_key)), byte=0x63)
    window = world.window()
    lock = gi.prepare_endorsement_lock(
        world.chain, world.deployment, records=[endorser], refs=world.refs, proposal=proposal, seed=PROPOSAL_SEED,
        stake_amount=5_000_000, credential_key_hash=endorser_key.key_hash, window=window,
        wallet_address=world.wallet_address, proposer_record=proposer,
    )
    locked = world.locked(world.deployment.endorsement_spend, lock.datum, 7_000_000)
    prepared = gi.prepare_submit_endorsement(
        world.chain, world.deployment, locked=locked, records=[endorser], refs=world.refs, proposal=proposal,
        window=window, wallet_address=world.wallet_address, proposer_record=proposer,
    )
    if not with_proposer_record:
        prepared.builder.reference_inputs.remove(proposer.utxo)
    return locked, sign(world, prepared, endorser_key)


def test_endorsement_mints_the_endorsement_token(world):
    locked, tx = endorsement(world)
    result = run(world, tx)
    assert result.passed, result.describe()
    assert {v.purpose for v in result.verdicts} == {spend_purpose(tx, locked), "Mint[0]"}


def test_endorsement_without_the_proposer_record_fails(world):
    locked, tx = endorsement(world, with_proposer_record=False)
    result = run(world, tx)
    assert not result.passed
    assert result.failing == [spend_purpose(tx, locked)], result.describe()
