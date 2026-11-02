"""Offline tests of the ReputationStakingClient transaction builders.

Each test builds a signed transaction against a fake chain context
(tests/chain_fixtures.py) and checks the shape that the contracts need
(validators/*.ak, lib/reputation_staking/*_validation.ak, shared/identity.ak):
reference inputs (identity record, ancestors, ProtocolParams record,
CrossValidatorRefs), required signers (controller and owner), the validity
range (at most one hour; stored times equal the lower bound), datum fields,
redeemers, mint and payouts. The refusal tests check the client-side checks.
"""

from __future__ import annotations

import dataclasses

import pytest
from pycardano import PaymentSigningKey, Value

from reputation_staking import vector_identity as vi
from reputation_staking.client import ReputationStakingClient
from reputation_staking.deployment import DeploymentStateError
from reputation_staking.plutus_data import (
    BurnChallengeTokenRedeemer,
    BurnEndorsementTokenRedeemer,
    ChallengeSpendRedeemer,
    CreateStakeRedeemer,
    DistributeOutcomeRedeemer,
    EndorsementSpendRedeemer,
    EndorsementValidatorDatumChallenge,
    EndorsementValidatorDatumEndorsement,
    HistoryBonusDatum,
    MintChallengeTokenRedeemer,
    MintEndorsementTokenRedeemer,
    MintHistoryBonusRedeemer,
    MintStakeTokenRedeemer,
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeInconclusive,
    RepChallengeOutcomeVerified,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ReleaseChallengeRedeemer,
    ResolveChallengeRedeemer,
    SlashEndorsementRedeemer,
    SlashStakeRedeemer,
    StakeDatum,
    VerificationKeyCredential,
    decode_datum,
)
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from tests.chain_fixtures import (
    AP3X,
    CROSS_REFS,
    END_HASH,
    END_REF,
    HI,
    LO,
    REP_HASH,
    REP_REF,
    TIP_SLOT,
    TREASURY,
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
    public_key,
    redeemers_of,
    ref,
    refs_of,
    signers_of,
    stake_datum_of,
    utxo_at,
    witness_keys,
)

MIN_AGENT_AGE = 86_400_000  # ProtocolParams default (ms)


@pytest.fixture
def world():
    w = World()
    w.record("A")
    w.record("B")
    return w


# ── Deploy state and window ──────────────────────────────────────────────────


class TestClientSetup:
    def test_old_deploy_state_is_refused(self, world):
        old = dict(world.state)
        del old["identity_hash"]
        old["agent_registry_hash"] = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
        with pytest.raises(DeploymentStateError, match="Move the file"):
            ReputationStakingClient(world.context, world.wallet, old)

    def test_validity_range_longer_than_one_hour_is_refused(self, world):
        with pytest.raises(ValueError, match="one hour"):
            world.client(validity_slots=3601)
        assert world.client(validity_slots=3600).validity_slots == 3600

    def test_treasury_and_identity_addresses_from_deploy_state(self, world):
        client = world.client()
        assert client.treasury_addr == address(TREASURY)
        assert client.identity_addr == address("1d" * 28)
        assert client.tx_window().lo == LO and client.tx_window().hi == HI


# ── CreateStake ──────────────────────────────────────────────────────────────


class TestCreateStake:
    def build(self, world, did=None, caps=("code_review", "testing"), **kwargs):
        seed = world.seed("A")
        kwargs.setdefault("controller_skey", world.ctrl["A"])
        return world.client().build_create_stake(
            did or world.did("A"), list(caps), 10 * AP3X, ref(seed), **kwargs
        ), seed

    def test_shape(self, world):
        tx, seed = self.build(world)
        assert refs_of(tx) == {ref(world.records["A"]), REP_REF} | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet), key_hash(world.ctrl["A"])}
        assert witness_keys(tx) == {key_hash(world.wallet), key_hash(world.ctrl["A"])}
        assert_window(tx)
        assert ref(seed) in inputs_of(tx)
        token = bytes.fromhex(derive_stake_token_name(world.ids["A"].hex()))
        assert mint_of(tx) == {(REP_HASH, token): 1}
        [out] = outputs_to(tx, address(REP_HASH))
        assert out.datum.last_updated == HI
        assert list(out.datum.challenge_locks) == []
        # stake_id: the output reference of the spent seed
        assert out.datum.stake_id.transaction_id == bytes(seed.input.transaction_id)
        assert out.datum.stake_id.output_index == seed.input.index
        assert bytes(out.datum.agent_did) == world.ids["A"]
        assert out.datum.owner_credential == VerificationKeyCredential(key_hash(world.wallet))
        assert out.amount.coin == 10 * AP3X
        kinds = {type(data) for _, data in redeemers_of(tx)}
        assert kinds == {CreateStakeRedeemer, MintStakeTokenRedeemer}

    def test_protected_deployment_utxos_are_not_spent(self, world):
        tx, _ = self.build(world)
        assert not inputs_of(tx) & {REP_REF, END_REF, CROSS_REFS}

    def test_hex_identifier_and_wallet_controller(self, world):
        """The controller can be the wallet key; the client signs once."""
        world.ctrl["A"] = world.wallet
        world.record("A")
        tx, _ = self.build(world, did=world.ids["A"].hex(), controller_skey=None)
        assert signers_of(tx) == {key_hash(world.wallet)}

    def test_subagent_has_parent_reference_input(self, world):
        world.record("P")
        world.record("A", lineage=vi.Lineage(world.ids["P"], 0, 1))
        tx, _ = self.build(world)
        assert {ref(world.records["A"]), ref(world.records["P"])} <= refs_of(tx)

    def test_refuses_suspended_record(self, world):
        hold = vi.Hold(vi.Signer.controller_set(), 1, LO - 1000, HI + 1)
        world.record("A", status=vi.Status.suspended([hold]))
        with pytest.raises(vi.IdentityStatusError):
            self.build(world)

    def test_refuses_record_without_capability(self, world):
        world.record("A", capabilities=("code_review",))
        with pytest.raises(vi.IdentityError, match="testing"):
            self.build(world)

    def test_refuses_missing_ancestor(self, world):
        world.record("A", lineage=vi.Lineage(world.ids["P"], 0, 1))
        with pytest.raises(vi.MissingAncestorError):
            self.build(world)

    def test_refuses_missing_record(self, world):
        world.context.remove(world.records.pop("A"))
        with pytest.raises(vi.RecordNotFoundError):
            self.build(world)

    def test_refuses_party_that_ends_inside_the_window(self, world):
        party = vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, 0, HI - 1, 3, None)
        world.record("A", parties=(party,))
        with pytest.raises(vi.IdentityStatusError):
            self.build(world)

    def test_refuses_without_controller_key(self, world):
        with pytest.raises(vi.IdentityError, match="controller_skey"):
            self.build(world, controller_skey=None)

    def test_refuses_wrong_controller_key(self, world):
        with pytest.raises(vi.IdentityError, match="not the controller"):
            self.build(world, controller_skey=world.ctrl["B"])


# ── MintEndorsement ──────────────────────────────────────────────────────────


class TestMintEndorsement:
    def build(self, world, caps=("code_review",), **kwargs):
        kwargs.setdefault("controller_skey", world.ctrl["B"])
        return world.client().build_mint_endorsement(
            world.did("B"), world.did("A"), list(caps), 20 * AP3X, **kwargs
        )

    def test_shape(self, world):
        stake = world.stake_a()
        tx = self.build(world)
        assert refs_of(tx) == {
            ref(world.records["B"]), ref(world.records["A"]), ref(stake), END_REF,
        } | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet), key_hash(world.ctrl["B"])}
        assert_window(tx)
        token = bytes.fromhex(
            derive_endorsement_token_name(world.ids["B"].hex(), world.ids["A"].hex())
        )
        assert mint_of(tx) == {(END_HASH, token): 1}
        [out] = outputs_to(tx, address(END_HASH))
        assert isinstance(out.datum, EndorsementValidatorDatumEndorsement)
        inner = out.datum.datum
        assert inner.created_at == LO
        assert inner.endorser_credential == VerificationKeyCredential(key_hash(world.wallet))
        assert bytes(inner.endorser_did) == world.ids["B"]
        assert bytes(inner.target_did) == world.ids["A"]
        # The bound stake: the stake that the creation reads
        assert inner.target_stake == stake_datum_of(stake).stake_id
        assert [type(d) for _, d in redeemers_of(tx)] == [MintEndorsementTokenRedeemer]

    def test_binds_the_largest_stake_or_the_named_stake(self, world):
        """An agent can have more than one stake: by default the endorsement
        reads the largest one; `target_stake` names another one."""
        small = world.stake_a(amount=20 * AP3X)
        large = world.stake_a(amount=200 * AP3X)
        tx = self.build(world)
        [out] = outputs_to(tx, address(END_HASH))
        assert out.datum.datum.target_stake == stake_datum_of(large).stake_id
        assert ref(large) in refs_of(tx) and ref(small) not in refs_of(tx)
        stake_id = stake_datum_of(small).stake_id
        named = f"{bytes(stake_id.transaction_id).hex()}#{stake_id.output_index}"
        tx = self.build(world, target_stake=named)
        [out] = outputs_to(tx, address(END_HASH))
        assert out.datum.datum.target_stake == stake_id
        assert ref(small) in refs_of(tx) and ref(large) not in refs_of(tx)

    def test_refuses_a_stake_id_the_target_does_not_hold(self, world):
        world.stake_a()
        with pytest.raises(RuntimeError, match="no stake with stake_id"):
            self.build(world, target_stake="ab" * 32 + "#0")

    def test_target_subagent_has_parent_reference_input(self, world):
        world.stake_a()
        world.record("P")
        world.record("A", lineage=vi.Lineage(world.ids["P"], 0, 1))
        assert ref(world.records["P"]) in refs_of(self.build(world))

    def test_refuses_target_not_active(self, world):
        world.stake_a()
        world.record("A", status=vi.Status.revoked(vi.Signer.controller_set(), 1, LO - 1))
        with pytest.raises(vi.IdentityStatusError):
            self.build(world)

    def test_refuses_target_without_capability(self, world):
        world.stake_a(capabilities=("code_review", "deployment"))
        with pytest.raises(vi.IdentityError, match="deployment"):
            self.build(world, caps=("deployment",))

    def test_refuses_endorser_not_active(self, world):
        world.stake_a()
        world.record("B", status=vi.Status.retired(LO - 1))
        with pytest.raises(vi.IdentityStatusError):
            self.build(world)

    def test_refuses_capability_the_stake_does_not_back(self, world):
        world.stake_a(capabilities=("testing",))
        with pytest.raises(ValueError, match="does not back"):
            self.build(world)

    def test_refuses_self_endorsement(self, world):
        with pytest.raises(ValueError, match="itself"):
            world.client().build_mint_endorsement(
                world.did("A"), world.ids["A"].hex(), ["code_review"], 20 * AP3X,
            )


# ── MintChallenge ────────────────────────────────────────────────────────────


class TestMintChallenge:
    def build(self, world, capability="code_review", **kwargs):
        kwargs.setdefault("controller_skey", world.ctrl["B"])
        return world.client().build_mint_challenge(
            world.did("B"), world.did("A"), capability, 25 * AP3X,
            "ab" * 32, "ipfs://evidence", **kwargs
        )

    def test_shape(self, world):
        """The target's record is not read; the target's stake is spent with
        LockForChallenge and continues with the lock added first."""
        stake = world.stake_a(challenge_locks=(b"\x01" * 32,))
        tx, datum = self.build(world)
        assert refs_of(tx) == {ref(world.records["B"]), END_REF, REP_REF} | world.deployment_refs()
        assert ref(stake) in inputs_of(tx)
        assert signers_of(tx) == {key_hash(world.wallet), key_hash(world.ctrl["B"])}
        assert_window(tx)
        token = world.lock_b()
        assert mint_of(tx) == {(END_HASH, token): 1}
        [out] = outputs_to(tx, address(END_HASH))
        assert out.datum == datum
        inner = datum.datum
        assert inner.created_at == LO
        assert isinstance(inner.state, RepChallengeStateOpen)
        assert inner.challenger_credential == VerificationKeyCredential(key_hash(world.wallet))
        kinds = sorted(type(d).__name__ for _, d in redeemers_of(tx))
        assert kinds == ["LockForChallengeRedeemer", "MintChallengeTokenRedeemer"]
        [stake_out] = outputs_to(tx, address(REP_HASH))
        before = decode_datum(StakeDatum, stake.output.datum)
        assert stake_out.datum == dataclasses.replace(
            before, challenge_locks=[token, b"\x01" * 32])
        assert stake_out.amount == stake.output.amount

    def test_target_record_is_not_read(self, world):
        """A revoked target whose record lost the capability can be
        challenged while its stake lists the capability (rule D-dodge)."""
        world.stake_a(capabilities=("code_review", "deployment"))
        world.record("A", status=vi.Status.revoked(vi.Signer.controller_set(), 1, LO - 1))
        self.build(world, capability="deployment")

    def test_refuses_capability_the_stake_does_not_back(self, world):
        world.stake_a(capabilities=("testing",))
        with pytest.raises(ValueError, match="does not back"):
            self.build(world)

    def test_refuses_a_second_lock_with_the_same_name(self, world):
        world.stake_a(challenge_locks=(world.lock_b(),))
        with pytest.raises(ValueError, match="already locks"):
            self.build(world)

    def test_refuses_an_undeclared_wallet_key(self, world):
        """challenger_credential (the wallet key) must be the controller or a
        listed key with the transaction purpose."""
        world.stake_a()
        world.record("B", keys=())
        with pytest.raises(vi.IdentityError, match="neither the controller"):
            self.build(world)

    def test_target_credential_is_the_stake_owner(self, world):
        """target_credential = owner_credential of the target's stake (here
        a key other than the wallet key)."""
        world.stake_a()
        _, datum = self.build(world)
        assert datum.datum.target_credential == VerificationKeyCredential(world.owner_a)
        assert world.owner_a != key_hash(world.wallet)

    def test_challenger_age_boundary(self, world):
        world.stake_a()
        world.record("B", created_at=LO - MIN_AGENT_AGE)
        self.build(world)  # exactly min_agent_age old at lo
        world.record("B", created_at=LO - MIN_AGENT_AGE + 1)
        with pytest.raises(vi.IdentityError, match="min_agent_age"):
            self.build(world)


    def test_refuses_challenger_suspended(self, world):
        world.stake_a()
        hold = vi.Hold(vi.Signer.controller_set(), 1, LO - 1000, LO + 1)
        world.record("B", status=vi.Status.suspended([hold]))
        with pytest.raises(vi.IdentityStatusError):
            self.build(world)

    def test_refuses_challenger_with_missing_ancestor(self, world):
        world.stake_a()
        world.record("B", lineage=vi.Lineage(world.ids["P"], 0, 1))
        with pytest.raises(vi.MissingAncestorError):
            self.build(world)

    def test_refuses_without_challenger_controller_key(self, world):
        world.stake_a()
        with pytest.raises(vi.IdentityError, match="controller_skey"):
            self.build(world, controller_skey=None)


# ── ResolveChallenge (oracle) ────────────────────────────────────────────────


class TestResolveChallenge:
    def test_shape(self, world):
        challenge = world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
        ))
        tx = world.client().build_resolve_challenge(world.did("B"), world.did("A"), "code_review", 0)
        assert ref(challenge) in inputs_of(tx)
        assert refs_of(tx) == {END_REF} | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet)}
        assert tx.transaction_body.validity_start == TIP_SLOT
        [out] = outputs_to(tx, address(END_HASH))
        assert out.amount == challenge.output.amount
        before = decode_datum(EndorsementValidatorDatumChallenge, challenge.output.datum).datum
        after = out.datum.datum
        assert after.state == RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified())
        assert dataclasses.replace(after, state=before.state) == before
        [(tag, data)] = redeemers_of(tx)
        assert tag == "spend"
        assert data == ChallengeSpendRedeemer(
            action=ResolveChallengeRedeemer(outcome=RepChallengeOutcomeVerified())
        )
        assert mint_of(tx) == {}

    def test_refuses_wallet_that_is_not_the_oracle(self, world):
        world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
        ))
        other = PaymentSigningKey.generate()
        world.context.add(utxo_at(key_address(key_hash(other)), Value(100 * AP3X)))
        with pytest.raises(ValueError, match="oracle"):
            world.client(wallet=other).build_resolve_challenge(
                world.did("B"), world.did("A"), "code_review", 1,
            )


# ── DistributeOutcome ────────────────────────────────────────────────────────


def resolved(outcome):
    return RepChallengeStateResolved(outcome=outcome)


class TestDistributeOutcome:
    def add_challenge(self, world, outcome):
        world.stake_a(challenge_locks=(world.lock_b(),))
        return world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            state=resolved(outcome),
        ))

    def test_verified_without_bonus(self, world):
        challenge = self.add_challenge(world, RepChallengeOutcomeVerified())
        stake = world.client().find_stake_utxo(world.did("A"))
        tx = world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")
        assert {ref(challenge), ref(stake)} <= inputs_of(tx)
        # A bonus is not a condition (W2-M36 decision 4). The record of the
        # payee's agent (the target) is a reference input (payee rule).
        assert refs_of(tx) == {ref(world.records["A"]), END_REF, REP_REF} | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet)}
        fee = 25 * AP3X * 500 // 10_000
        [target_out] = outputs_to(tx, key_address(world.owner_a))
        assert target_out.amount.coin == 25 * AP3X - fee
        [treasury_out] = outputs_to(tx, address(TREASURY))
        assert treasury_out.amount.coin >= fee
        # ReleaseChallenge: the stake keeps its value and loses the lock.
        [stake_out] = outputs_to(tx, address(REP_HASH))
        assert list(stake_out.datum.challenge_locks) == []
        assert stake_out.amount == stake.output.amount
        [release] = [d for _, d in redeemers_of(tx) if isinstance(d, ReleaseChallengeRedeemer)]
        assert bytes(release.challenge_ref.transaction_id) == bytes(challenge.input.transaction_id)
        token = bytes.fromhex(
            derive_challenge_token_name(world.ids["B"].hex(), world.ids["A"].hex(), "code_review")
        )
        assert mint_of(tx) == {(END_HASH, token): -1}
        kinds = {type(d) for _, d in redeemers_of(tx)}
        assert kinds == {ChallengeSpendRedeemer, BurnChallengeTokenRedeemer, ReleaseChallengeRedeemer}
        assert tx.transaction_body.validity_start == TIP_SLOT

    def test_verified_pays_the_controller_when_the_record_drops_the_key(self, world):
        """Payee rule: A's record no longer lists owner_a, so the payout goes
        to the controller key of A's record."""
        self.add_challenge(world, RepChallengeOutcomeVerified())
        world.record("A", keys=())
        tx = world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")
        assert outputs_to(tx, key_address(world.owner_a)) == []
        [payout] = outputs_to(tx, key_address(key_hash(world.ctrl["A"])))
        assert payout.amount.coin == 25 * AP3X - 25 * AP3X * 500 // 10_000

    def test_inconclusive_needs_the_challenger_record(self, world):
        self.add_challenge(world, RepChallengeOutcomeInconclusive())
        world.context.remove(world.records.pop("B"))
        with pytest.raises(vi.RecordNotFoundError):
            world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")

    def test_verified_with_bonus(self, world):
        challenge = self.add_challenge(world, RepChallengeOutcomeVerified())
        tx = world.client().build_distribute_outcome(
            world.did("B"), world.did("A"), "code_review",
            mint_bonus=True, bonus_controller_skey=world.ctrl["A"],
        )
        assert refs_of(tx) == {ref(world.records["A"]), END_REF, REP_REF} | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet), key_hash(world.ctrl["A"])}
        assert_window(tx)
        bonus_token = bytes.fromhex(derive_history_bonus_token_name(
            bytes(challenge.input.transaction_id).hex(), challenge.input.index,
        ))
        assert mint_of(tx)[(REP_HASH, bonus_token)] == 1
        [bonus_out] = [
            o for o in outputs_to(tx, address(REP_HASH)) if isinstance(o.datum, HistoryBonusDatum)
        ]
        assert bonus_out.datum.created_at == LO
        assert bytes(bonus_out.datum.agent_did) == world.ids["A"]
        assert MintHistoryBonusRedeemer in {type(d) for _, d in redeemers_of(tx)}

    def test_bonus_refuses_target_not_active(self, world):
        self.add_challenge(world, RepChallengeOutcomeVerified())
        world.record("A", status=vi.Status.revoked(vi.Signer.controller_set(), 1, LO - 1))
        client = world.client()
        with pytest.raises(vi.IdentityStatusError):
            client.build_distribute_outcome(
                world.did("B"), world.did("A"), "code_review",
                mint_bonus=True, bonus_controller_skey=world.ctrl["A"],
            )
        # Without the bonus the distribution needs no record
        client.build_distribute_outcome(world.did("B"), world.did("A"), "code_review")

    def test_inconclusive_pays_challenger(self, world):
        """The challenger gets the stake less half of the fee; the treasury
        gets that half."""
        self.add_challenge(world, RepChallengeOutcomeInconclusive())
        tx = world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")
        half = 25 * AP3X * 500 // 10_000 // 2
        [challenger_out] = [
            o for o in outputs_to(tx, key_address(key_hash(world.wallet)))
            if o.amount.coin == 25 * AP3X - half
        ]
        assert challenger_out
        [treasury_out] = outputs_to(tx, address(TREASURY))
        # At least the half (raised to the ledger minimum), less than the fee.
        assert half <= treasury_out.amount.coin < 2 * half
        with pytest.raises(ValueError, match="no winner"):
            world.client().build_distribute_outcome(
                world.did("B"), world.did("A"), "code_review", mint_bonus=True,
            )

    def test_falsified_needs_the_slash_builder(self, world):
        self.add_challenge(world, RepChallengeOutcomeFalsified())
        with pytest.raises(ValueError, match="distribute_falsified_outcome"):
            world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")


# ── SlashEndorsement and DistributeOutcome + SlashStake ──────────────────────


class TestFalsifiedPath:
    def setup_falsified(self, world, endorsement_amount=20 * AP3X):
        """A's stake with the lock of B's resolved (Falsified) challenge, and
        B's endorsement of A bound to that stake. The endorser credential is
        a second key that B's record lists."""
        endorser_skey = PaymentSigningKey.generate()
        endorser_key = key_hash(endorser_skey)
        world.record("B", keys=(
            vi.Key(vi.KeyType.ED25519, public_key(world.wallet), vi.PURPOSE_TRANSACTION),
            vi.Key(vi.KeyType.ED25519, public_key(endorser_skey), vi.PURPOSE_TRANSACTION),
        ))
        stake = world.stake_a(challenge_locks=(world.lock_b(),))
        challenge = world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            state=resolved(RepChallengeOutcomeFalsified()),
        ))
        endorsement = world.context.add(endorsement_utxo(
            world.ids["B"], world.ids["A"], endorser_key, amount=endorsement_amount,
            target_stake=stake_datum_of(stake).stake_id,
        ))
        return challenge, endorsement, endorser_key

    def test_slash_continues_endorsement(self, world):
        challenge, endorsement, endorser_key = self.setup_falsified(world)
        stake = world.client().find_stake_utxo(world.did("A"))
        tx = world.client().build_slash_endorsement(
            world.did("B"), world.did("A"), ref(challenge),
        )
        assert ref(endorsement) in inputs_of(tx)
        assert ref(challenge) not in inputs_of(tx)
        # The challenge, the bound stake and the challenger's record (payee
        # rule) are reference inputs; the endorser's record is not needed
        # while the endorsement continues.
        assert refs_of(tx) == {
            ref(challenge), ref(stake), ref(world.records["B"]), END_REF,
        } | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet)}
        slash = 20 * AP3X * 5000 // 10_000
        fee = slash * 500 // 10_000
        [out] = outputs_to(tx, address(END_HASH))
        before = decode_datum(EndorsementValidatorDatumEndorsement, endorsement.output.datum).datum
        assert out.datum.datum == dataclasses.replace(
            before, stake_amount=20 * AP3X - slash, slashed_by=[world.lock_b()])
        assert out.datum.datum.target_stake == before.target_stake
        assert out.amount.coin >= 20 * AP3X - slash
        challenger_paid = sum(
            o.amount.coin for o in outputs_to(tx, key_address(key_hash(world.wallet)))
        )
        assert challenger_paid >= slash - fee
        [treasury_out] = outputs_to(tx, address(TREASURY))
        assert treasury_out.amount.coin >= fee
        assert mint_of(tx) == {}
        [(_, data)] = redeemers_of(tx)
        assert isinstance(data, EndorsementSpendRedeemer)
        assert isinstance(data.action, SlashEndorsementRedeemer)
        assert tx.transaction_body.validity_start == TIP_SLOT

    def test_slash_refuses_an_endorsement_of_another_stake(self, world):
        """The challenge locks one stake of A; an endorsement bound to
        another stake of A (without the lock) is not slashed."""
        challenge, _, endorser_key = self.setup_falsified(world)
        other = world.stake_a(amount=50 * AP3X)
        world.context.remove(world.client().find_endorsement_utxo(world.did("B"), world.did("A")))
        world.context.add(endorsement_utxo(
            world.ids["B"], world.ids["A"], endorser_key,
            target_stake=stake_datum_of(other).stake_id,
        ))
        with pytest.raises(ValueError, match="does not lock the bound stake"):
            world.client().build_slash_endorsement(world.did("B"), world.did("A"), ref(challenge))

    def test_slash_refuses_a_second_slash_by_the_same_challenge(self, world):
        _, _, endorser_key = self.setup_falsified(world)
        challenge = world.client().find_challenge_utxo(world.did("B"), world.did("A"), "code_review")
        world.context.remove(world.client().find_endorsement_utxo(world.did("B"), world.did("A")))
        world.context.add(endorsement_utxo(
            world.ids["B"], world.ids["A"], endorser_key, slashed_by=(world.lock_b(),),
        ))
        with pytest.raises(ValueError, match="already slashed"):
            world.client().build_slash_endorsement(world.did("B"), world.did("A"), ref(challenge))

    def test_slash_closes_small_endorsement(self, world):
        """remaining < min_endorsement: burn the token, pay the endorser's
        payee (here the declared endorser key) and add its record."""
        challenge, endorsement, endorser_key = self.setup_falsified(world, 8 * AP3X)
        tx = world.client().build_slash_endorsement(
            world.did("B"), world.did("A"), ref(challenge),
        )
        token = bytes.fromhex(
            derive_endorsement_token_name(world.ids["B"].hex(), world.ids["A"].hex())
        )
        assert mint_of(tx) == {(END_HASH, token): -1}
        assert outputs_to(tx, address(END_HASH)) == []
        [endorser_out] = outputs_to(tx, key_address(endorser_key))
        assert endorser_out.amount.coin >= 8 * AP3X - 8 * AP3X * 5000 // 10_000
        assert BurnEndorsementTokenRedeemer in {type(d) for _, d in redeemers_of(tx)}
        assert ref(world.records["B"]) in refs_of(tx)

    def test_slash_pays_the_controller_for_an_undeclared_endorser_key(self, world):
        """Payee rule: B's record no longer lists the endorser key, so the
        remainder of a closed endorsement goes to B's controller key."""
        challenge, _, endorser_key = self.setup_falsified(world, 8 * AP3X)
        world.record("B")
        tx = world.client().build_slash_endorsement(world.did("B"), world.did("A"), ref(challenge))
        assert outputs_to(tx, key_address(endorser_key)) == []
        [controller_out] = outputs_to(tx, key_address(key_hash(world.ctrl["B"])))
        assert controller_out.amount.coin == 8 * AP3X - 8 * AP3X * 5000 // 10_000

    def test_distribute_falsified_slashes_stake(self, world):
        challenge, _, _ = self.setup_falsified(world)
        stake = world.client().find_stake_utxo(world.did("A"))
        tx = world.client().build_distribute_falsified_outcome(
            world.did("B"), world.did("A"), "code_review",
        )
        assert {ref(challenge), ref(stake)} <= inputs_of(tx)
        assert refs_of(tx) == {ref(world.records["B"]), END_REF, REP_REF} | world.deployment_refs()
        target_slash = 100 * AP3X // 2
        fee = target_slash * 500 // 10_000
        [stake_out] = [
            o for o in outputs_to(tx, address(REP_HASH)) if isinstance(o.datum, StakeDatum)
        ]
        assert stake_out.datum.stake_amount == 100 * AP3X - target_slash
        # A slash is not an owner action: last_updated stays; the lock goes.
        before = decode_datum(StakeDatum, stake.output.datum)
        assert stake_out.datum.last_updated == before.last_updated
        assert list(stake_out.datum.challenge_locks) == []
        assert stake_out.amount.coin >= 100 * AP3X - target_slash
        challenger_paid = max(
            o.amount.coin for o in outputs_to(tx, key_address(key_hash(world.wallet)))
        )
        # held (the AP3X of the challenge UTxO) + slash − fee
        assert challenger_paid >= challenge.output.amount.coin + target_slash - fee
        kinds = [type(d) for _, d in redeemers_of(tx)]
        assert SlashStakeRedeemer in kinds and BurnChallengeTokenRedeemer in kinds
        [slash] = [d for _, d in redeemers_of(tx) if isinstance(d, SlashStakeRedeemer)]
        assert bytes(slash.challenge_ref.transaction_id) == bytes(challenge.input.transaction_id)
        spend = [d for _, d in redeemers_of(tx) if isinstance(d, ChallengeSpendRedeemer)]
        assert spend == [ChallengeSpendRedeemer(action=DistributeOutcomeRedeemer())]
        assert_window(tx)

    def test_distribute_falsified_with_challenger_bonus(self, world):
        self.setup_falsified(world)
        world.record("P")
        world.record("B", lineage=vi.Lineage(world.ids["P"], 0, 1))
        tx = world.client().build_distribute_falsified_outcome(
            world.did("B"), world.did("A"), "code_review",
            mint_bonus=True, bonus_controller_skey=world.ctrl["B"],
        )
        assert {ref(world.records["B"]), ref(world.records["P"])} <= refs_of(tx)
        assert signers_of(tx) == {key_hash(world.wallet), key_hash(world.ctrl["B"])}
        [bonus_out] = [
            o for o in outputs_to(tx, address(REP_HASH)) if isinstance(o.datum, HistoryBonusDatum)
        ]
        assert bytes(bonus_out.datum.agent_did) == world.ids["B"]
        assert bonus_out.datum.created_at == LO

    def test_slash_refuses_other_target(self, world):
        challenge, _, _ = self.setup_falsified(world)
        other = vi.identifier_from_seed(bytes.fromhex("0d" * 32), 0)
        world.context.add(endorsement_utxo(world.ids["B"], other, key_hash(world.wallet)))
        with pytest.raises(ValueError, match="another target"):
            world.client().build_slash_endorsement(world.did("B"), other.hex(), ref(challenge))


# ── Agent checks for scripts ─────────────────────────────────────────────────


class TestCheckAgent:
    def test_check_agent(self, world):
        record = world.client().check_agent(
            world.did("A"), ["code_review"], world.ctrl["A"],
        )
        assert record.agent_id == world.ids["A"]
        with pytest.raises(vi.IdentityError, match="deployment"):
            world.client().check_agent(world.did("A"), ["deployment"], world.ctrl["A"])
        with pytest.raises(vi.IdentityError, match="controller_skey"):
            world.client().check_agent(world.did("A"))
        # A target does not sign
        world.client().check_agent(world.did("A"), ["code_review"], signs=False)

    def test_check_challenger_age(self, world):
        world.record("B", created_at=LO - 1000)
        with pytest.raises(vi.IdentityError, match="min_agent_age"):
            world.client().check_agent(
                world.did("B"), controller_skey=world.ctrl["B"], challenger=True,
            )
