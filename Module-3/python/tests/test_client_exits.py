"""Offline tests of the payee rule and the closing builders of
ReputationStakingClient: DecreaseStake, WithdrawEndorsement and
WithdrawChallenge (exits: the controller of the record signs, the payee gets
the funds) and ExpireChallenge (permissionless).

Each test builds a signed transaction against the fake chain
(tests/chain_fixtures.py) and checks the shape that the contracts need:
inputs, reference inputs (the payee's record, the bound stake, the
ProtocolParams record, CrossValidatorRefs), required signers, redeemers,
mint, the continuing stake and the payouts. The refusal tests check the
client-side checks of the contract rules. tests/test_simulate.py runs these
builders against the compiled validators.
"""

from __future__ import annotations

import dataclasses

import pytest
from pycardano import PaymentSigningKey, Value

from reputation_staking import vector_identity as vi
from reputation_staking.client import challenge_timeout
from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.plutus_data import (
    BurnChallengeTokenRedeemer,
    BurnEndorsementTokenRedeemer,
    ChallengeSpendRedeemer,
    DecreaseStakeRedeemer,
    EndorsementSpendRedeemer,
    ExpireChallengeRedeemer,
    ReleaseChallengeRedeemer,
    RepChallengeOutcomeVerified,
    RepChallengeStateEscalated,
    RepChallengeStateResolved,
    ScriptCredential,
    VerificationKeyCredential,
    WithdrawChallengeRedeemer,
    WithdrawEndorsementRedeemer,
)
from reputation_staking.token_names import derive_endorsement_token_name
from tests.chain_fixtures import (
    AP3X,
    END_HASH,
    END_REF,
    HI,
    LO,
    REP_HASH,
    REP_REF,
    World,
    address,
    assert_window,
    challenge_utxo,
    endorsement_utxo,
    inputs_of,
    key_address,
    key_hash,
    mint_of,
    outputs_to,
    redeemers_of,
    ref,
    refs_of,
    signers_of,
    stake_datum_of,
    utxo_at,
    witness_keys,
)

PARAMS = ProtocolParamsInfo()
HOUR = 3_600_000


@pytest.fixture
def world():
    w = World()
    w.record("A")
    w.record("B")
    return w


def paid_to(tx, key: bytes) -> int:
    return sum(o.amount.coin for o in outputs_to(tx, key_address(key)))


# ── Payee rule ───────────────────────────────────────────────────────────────


class TestPayee:
    def test_declared_credential_is_the_payee(self, world):
        stored = VerificationKeyCredential(world.owner_a)
        assert world.client().payee(world.did("A"), stored) == stored

    def test_controller_key_when_the_record_drops_the_credential(self, world):
        world.record("A", keys=())
        payee = world.client().payee(world.did("A"), VerificationKeyCredential(world.owner_a))
        assert payee == VerificationKeyCredential(key_hash(world.ctrl["A"]))

    def test_controller_key_is_always_declared(self, world):
        world.record("A", keys=())
        controller = VerificationKeyCredential(key_hash(world.ctrl["A"]))
        assert world.client().payee(world.did("A"), controller) == controller

    def test_script_credential_is_never_declared(self, world):
        payee = world.client().payee(world.did("A"), ScriptCredential(b"\x09" * 28))
        assert payee == VerificationKeyCredential(key_hash(world.ctrl["A"]))

    def test_any_status_also_a_tombstone(self, world):
        world.record("A", status=vi.Status.retired(LO - 1))
        stored = VerificationKeyCredential(world.owner_a)
        assert world.client().payee(world.did("A"), stored) == stored

    def test_needs_the_record(self, world):
        world.context.remove(world.records.pop("A"))
        with pytest.raises(vi.RecordNotFoundError):
            world.client().payee(world.did("A"), VerificationKeyCredential(world.owner_a))


# ── DecreaseStake ────────────────────────────────────────────────────────────


class TestDecreaseStake:
    def build(self, world, amount=40 * AP3X, **kwargs):
        kwargs.setdefault("controller_skey", world.ctrl["A"])
        return world.client().build_decrease_stake(world.did("A"), amount, **kwargs)

    def test_shape(self, world):
        stake = world.stake_a()
        tx = self.build(world)
        assert ref(stake) in inputs_of(tx)
        # The record (any status) for the controller signature and the payee
        assert refs_of(tx) == {ref(world.records["A"]), REP_REF} | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.ctrl["A"])}
        assert witness_keys(tx) == {key_hash(world.wallet), key_hash(world.ctrl["A"])}
        assert_window(tx)
        [stake_out] = outputs_to(tx, address(REP_HASH))
        before = stake_datum_of(stake)
        assert stake_out.datum == dataclasses.replace(
            before, stake_amount=60 * AP3X, last_updated=HI)
        assert stake_out.datum.stake_id == before.stake_id
        assert stake_out.amount.coin >= 60 * AP3X
        assert stake_out.amount.multi_asset == stake.output.amount.multi_asset
        assert paid_to(tx, world.owner_a) >= 40 * AP3X
        [(tag, data)] = redeemers_of(tx)
        assert tag == "spend" and data == DecreaseStakeRedeemer(40 * AP3X)
        assert mint_of(tx) == {}

    def test_pays_the_controller_when_the_record_drops_the_owner_key(self, world):
        world.stake_a()
        world.record("A", keys=())
        tx = self.build(world)
        assert paid_to(tx, world.owner_a) == 0
        assert paid_to(tx, key_hash(world.ctrl["A"])) >= 40 * AP3X

    def test_a_revoked_agent_can_exit(self, world):
        world.stake_a()
        world.record("A", status=vi.Status.revoked(vi.Signer.controller_set(), 1, LO - 1))
        self.build(world)

    def test_names_a_stake_by_its_stake_id(self, world):
        world.stake_a(amount=200 * AP3X)
        small = world.stake_a(amount=60 * AP3X)
        stake_id = stake_datum_of(small).stake_id
        tx = self.build(world, amount=20 * AP3X, stake_id=(
            f"{bytes(stake_id.transaction_id).hex()}#{stake_id.output_index}"
        ))
        assert ref(small) in inputs_of(tx)
        [stake_out] = outputs_to(tx, address(REP_HASH))
        assert stake_out.datum.stake_amount == 40 * AP3X

    def test_refuses_a_locked_stake(self, world):
        world.stake_a(challenge_locks=(world.lock_b(),))
        with pytest.raises(ValueError, match="locks the stake"):
            self.build(world)

    def test_refuses_during_the_cooldown(self, world):
        world.stake_a(last_updated=LO - PARAMS.stake_cooldown + 1)
        with pytest.raises(ValueError, match="stake_cooldown"):
            self.build(world)

    def test_cooldown_boundary(self, world):
        world.stake_a(last_updated=LO - PARAMS.stake_cooldown)
        self.build(world)

    def test_refuses_a_stake_below_the_minimum(self, world):
        world.stake_a(amount=15 * AP3X)
        with pytest.raises(ValueError, match="min_self_stake"):
            self.build(world, amount=6 * AP3X)

    def test_refuses_a_zero_amount(self, world):
        world.stake_a()
        with pytest.raises(ValueError, match="positive"):
            self.build(world, amount=0)

    def test_refuses_another_controller_key(self, world):
        world.stake_a()
        with pytest.raises(vi.IdentityError, match="not the controller"):
            self.build(world, controller_skey=world.ctrl["B"])

    def test_refuses_without_the_record(self, world):
        world.stake_a()
        world.context.remove(world.records.pop("A"))
        with pytest.raises(vi.RecordNotFoundError):
            self.build(world)


# ── WithdrawEndorsement ──────────────────────────────────────────────────────


class TestWithdrawEndorsement:
    def setup(self, world, challenge_locks=(), created_at=LO - 3 * 86_400_000):
        stake = world.stake_a(challenge_locks=challenge_locks)
        endorsement = world.context.add(endorsement_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet),
            created_at=created_at, target_stake=stake_datum_of(stake).stake_id,
        ))
        return stake, endorsement

    def build(self, world, **kwargs):
        kwargs.setdefault("controller_skey", world.ctrl["B"])
        return world.client().build_withdraw_endorsement(world.did("B"), world.did("A"), **kwargs)

    def test_shape(self, world):
        stake, endorsement = self.setup(world)
        tx = self.build(world)
        assert ref(endorsement) in inputs_of(tx)
        assert ref(stake) not in inputs_of(tx)
        # The endorser's record and the bound stake are reference inputs
        assert refs_of(tx) == {
            ref(world.records["B"]), ref(stake), END_REF,
        } | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.ctrl["B"])}
        assert_window(tx)
        token = bytes.fromhex(
            derive_endorsement_token_name(world.ids["B"].hex(), world.ids["A"].hex())
        )
        assert mint_of(tx) == {(END_HASH, token): -1}
        assert outputs_to(tx, address(END_HASH)) == []
        assert paid_to(tx, key_hash(world.wallet)) >= 20 * AP3X
        kinds = {type(d) for _, d in redeemers_of(tx)}
        assert kinds == {EndorsementSpendRedeemer, BurnEndorsementTokenRedeemer}
        [spend] = [d for _, d in redeemers_of(tx) if isinstance(d, EndorsementSpendRedeemer)]
        assert spend.action == WithdrawEndorsementRedeemer()

    def test_pays_the_controller_when_the_record_drops_the_endorser_key(self, world):
        self.setup(world)
        world.record("B", keys=())
        tx = self.build(world)
        assert paid_to(tx, key_hash(world.ctrl["B"])) >= 20 * AP3X

    def test_a_retired_endorser_can_exit(self, world):
        self.setup(world)
        world.record("B", status=vi.Status.retired(LO - 1))
        self.build(world)

    def test_refuses_a_locked_bound_stake(self, world):
        self.setup(world, challenge_locks=(world.lock_b(),))
        with pytest.raises(ValueError, match="locks the bound stake"):
            self.build(world)

    def test_another_stake_of_the_target_does_not_matter(self, world):
        """Only the bound stake counts: a lock on another stake of the
        target does not stop the withdrawal."""
        self.setup(world)
        world.stake_a(amount=500 * AP3X, challenge_locks=(world.lock_b(),))
        self.build(world)

    def test_refuses_during_the_cooldown(self, world):
        self.setup(world, created_at=LO - PARAMS.endorsement_cooldown + 1)
        with pytest.raises(ValueError, match="endorsement_cooldown"):
            self.build(world)

    def test_refuses_without_the_bound_stake(self, world):
        world.context.add(endorsement_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet),
            created_at=LO - 3 * 86_400_000,
        ))
        with pytest.raises(RuntimeError, match="no stake with stake_id"):
            self.build(world)

    def test_refuses_without_the_controller_key(self, world):
        self.setup(world)
        with pytest.raises(vi.IdentityError, match="controller_skey"):
            self.build(world, controller_skey=None)


# ── WithdrawChallenge ────────────────────────────────────────────────────────


class TestWithdrawChallenge:
    def setup(self, world, created_at=LO - HOUR, **kwargs):
        stake = world.stake_a(challenge_locks=(world.lock_b(),))
        challenge = world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            created_at=created_at, **kwargs,
        ))
        return stake, challenge

    def build(self, world, **kwargs):
        kwargs.setdefault("controller_skey", world.ctrl["B"])
        return world.client().build_withdraw_challenge(
            world.did("B"), world.did("A"), "code_review", **kwargs
        )

    def test_shape(self, world):
        stake, challenge = self.setup(world)
        tx = self.build(world)
        assert {ref(challenge), ref(stake)} <= inputs_of(tx)
        assert refs_of(tx) == {
            ref(world.records["B"]), END_REF, REP_REF,
        } | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.ctrl["B"])}
        assert_window(tx)
        assert mint_of(tx) == {(END_HASH, world.lock_b()): -1}
        # ReleaseChallenge: the stake keeps its value and datum, without the lock
        [stake_out] = outputs_to(tx, address(REP_HASH))
        assert stake_out.datum == dataclasses.replace(stake_datum_of(stake), challenge_locks=[])
        assert stake_out.amount == stake.output.amount
        assert paid_to(tx, key_hash(world.wallet)) >= 25 * AP3X
        assert outputs_to(tx, address(END_HASH)) == []
        [release] = [d for _, d in redeemers_of(tx) if isinstance(d, ReleaseChallengeRedeemer)]
        assert bytes(release.challenge_ref.transaction_id) == bytes(challenge.input.transaction_id)
        spend = [d for _, d in redeemers_of(tx) if isinstance(d, ChallengeSpendRedeemer)]
        assert spend == [ChallengeSpendRedeemer(action=WithdrawChallengeRedeemer())]
        assert BurnChallengeTokenRedeemer in {type(d) for _, d in redeemers_of(tx)}

    def test_pays_the_controller_when_the_record_drops_the_wallet_key(self, world):
        self.setup(world)
        world.record("B", keys=())
        tx = self.build(world)
        assert paid_to(tx, key_hash(world.ctrl["B"])) >= 25 * AP3X

    def test_refuses_at_the_response_deadline(self, world):
        """The upper bound must be before created_at + challenge_response_deadline."""
        self.setup(world, created_at=HI - PARAMS.challenge_response_deadline)
        with pytest.raises(ValueError, match="response deadline"):
            self.build(world)

    def test_deadline_boundary(self, world):
        self.setup(world, created_at=HI - PARAMS.challenge_response_deadline + 1)
        self.build(world)

    def test_refuses_after_a_response(self, world):
        self.setup(world, counter_evidence_hash=b"\x0f" * 32, response_submitted_at=LO - 1000)
        with pytest.raises(ValueError, match="responded"):
            self.build(world)

    def test_refuses_a_resolved_challenge(self, world):
        self.setup(world, state=RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified()))
        with pytest.raises(ValueError, match="state Open"):
            self.build(world)

    def test_refuses_without_the_locked_stake(self, world):
        world.stake_a()
        world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            created_at=LO - HOUR,
        ))
        with pytest.raises(ValueError, match="holds the lock"):
            self.build(world)


# ── ExpireChallenge ──────────────────────────────────────────────────────────

EXPIRED_AT = LO - challenge_timeout(PARAMS, 0) - 1  # created_at: the timeout is just before lo


class TestExpireChallenge:
    def setup(self, world, created_at=EXPIRED_AT, response=True, state=None):
        stake = world.stake_a(challenge_locks=(world.lock_b(),))
        extra = dict(counter_evidence_hash=b"\x0f" * 32, response_submitted_at=created_at + HOUR)
        challenge = world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            created_at=created_at, state=state, **(extra if response else {}),
        ))
        return stake, challenge

    def build(self, world, wallet=None):
        return world.client(wallet=wallet).build_expire_challenge(
            world.did("B"), world.did("A"), "code_review",
        )

    def test_shape(self, world):
        stake, challenge = self.setup(world)
        tx = self.build(world)
        assert {ref(challenge), ref(stake)} <= inputs_of(tx)
        # The challenger's record for the payee rule; no other signature
        assert refs_of(tx) == {
            ref(world.records["B"]), END_REF, REP_REF,
        } | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet)}
        assert_window(tx)
        assert mint_of(tx) == {(END_HASH, world.lock_b()): -1}
        [stake_out] = outputs_to(tx, address(REP_HASH))
        assert stake_out.datum == dataclasses.replace(stake_datum_of(stake), challenge_locks=[])
        assert stake_out.amount == stake.output.amount
        assert paid_to(tx, key_hash(world.wallet)) >= 25 * AP3X
        spend = [d for _, d in redeemers_of(tx) if isinstance(d, ChallengeSpendRedeemer)]
        assert spend == [ChallengeSpendRedeemer(action=ExpireChallengeRedeemer())]
        assert ReleaseChallengeRedeemer in {type(d) for _, d in redeemers_of(tx)}

    def test_an_escalated_challenge_expires(self, world):
        self.setup(world, state=RepChallengeStateEscalated(audit_claim_token=b"clm_" + b"\x02" * 28))
        self.build(world)

    def test_pays_the_controller_when_the_record_drops_the_challenger_key(self, world):
        self.setup(world)
        world.record("B", keys=())
        tx = self.build(world)
        assert paid_to(tx, key_hash(world.ctrl["B"])) >= 25 * AP3X

    def test_refuses_at_the_timeout(self, world):
        """The lower bound must be after challenge_timeout(created_at)."""
        self.setup(world, created_at=LO - challenge_timeout(PARAMS, 0))
        with pytest.raises(ValueError, match="times out at"):
            self.build(world)

    def test_refuses_an_open_challenge_without_a_response(self, world):
        self.setup(world, response=False)
        with pytest.raises(ValueError, match="DefaultJudgment"):
            self.build(world)

    def test_refuses_a_resolved_challenge(self, world):
        self.setup(world, state=RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified()))
        with pytest.raises(ValueError, match="distribute_outcome"):
            self.build(world)

    def test_anybody_can_expire(self, world):
        """Permissionless: a wallet that is no party signs alone."""
        self.setup(world)
        other = PaymentSigningKey.generate()
        world.context.add(utxo_at(key_address(key_hash(other)), Value(100 * AP3X)))
        tx = self.build(world, wallet=other)
        assert signers_of(tx) == {key_hash(other)}
        assert paid_to(tx, key_hash(world.wallet)) >= 25 * AP3X
