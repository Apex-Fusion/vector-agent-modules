"""Offline tests of the Foundation oracle (oracle/oracle.py)."""

from __future__ import annotations

import pytest
from pycardano import PaymentSigningKey, Value

from oracle.oracle import FoundationOracle
from reputation_staking.plutus_data import (
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeVerified,
    RepChallengeStateResolved,
    ResolveChallengeRedeemer,
    SlashStakeRedeemer,
)
from tests.chain_fixtures import (
    AP3X,
    World,
    challenge_utxo,
    key_address,
    key_hash,
    redeemers_of,
    utxo_at,
)


@pytest.fixture
def world():
    w = World()
    w.record("A")
    w.record("B")
    return w


def oracle_of(world) -> tuple:
    client = world.client()
    sent = []

    def submit(tx, wait):
        sent.append(tx)
        return str(tx.id)

    client._submit = submit
    return FoundationOracle(client), sent


def add_challenge(world, state=None):
    return world.context.add(challenge_utxo(
        world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a, state=state,
    ))


def test_refuses_client_without_oracle_key(world):
    other = PaymentSigningKey.generate()
    world.context.add(utxo_at(key_address(key_hash(other)), Value(100 * AP3X)))
    with pytest.raises(ValueError, match="not the oracle key"):
        FoundationOracle(world.client(wallet=other))


def test_resolve_reads_no_identity_record(world):
    add_challenge(world)
    oracle, sent = oracle_of(world)
    oracle.resolve_challenge(world.did("B"), world.did("A"), "code_review", 1)
    [tx] = sent
    [(_, data)] = redeemers_of(tx)
    assert data.action == ResolveChallengeRedeemer(outcome=RepChallengeOutcomeFalsified())
    assert not any(
        str(i.transaction_id) == str(world.records[n].input.transaction_id)
        for n in ("A", "B") for i in tx.transaction_body.reference_inputs
    )


def test_distribute_uses_the_slash_path_for_falsified(world):
    world.stake_a(challenge_locks=(world.lock_b(),))
    add_challenge(world, RepChallengeStateResolved(outcome=RepChallengeOutcomeFalsified()))
    oracle, sent = oracle_of(world)
    oracle.distribute_outcome(world.did("B"), world.did("A"), "code_review")
    [tx] = sent
    assert any(isinstance(d, SlashStakeRedeemer) for _, d in redeemers_of(tx))


def test_distribute_verified_with_bonus(world):
    world.stake_a(challenge_locks=(world.lock_b(),))
    add_challenge(world, RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified()))
    oracle, sent = oracle_of(world)
    oracle.distribute_outcome(
        world.did("B"), world.did("A"), "code_review",
        mint_bonus=True, bonus_controller_skey=world.ctrl["A"],
    )
    [tx] = sent
    signers = {bytes(s) for s in tx.transaction_body.required_signers}
    assert signers == {key_hash(world.wallet), key_hash(world.ctrl["A"])}
