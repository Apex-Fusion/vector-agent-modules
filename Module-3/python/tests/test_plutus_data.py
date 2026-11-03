"""Tests for PlutusData types — CBOR round-trips and encoding verification."""

import sys
from pathlib import Path

import cbor2
import pytest
from pycardano.serialization import IndefiniteList, RawCBOR

from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.plutus_data import (
    AuditAuditorWins,
    AuditChallengeDatum,
    AuditClaimDatum,
    AuditOpen,
    AuditResolved,
    AuditSubmitClaimRedeemer,
    BurnChallengeTokenRedeemer,
    BurnGenesisBonusRedeemer,
    ChallengeSpendRedeemer,
    CreateStakeRedeemer,
    DecreaseStakeRedeemer,
    DistributeOutcomeRedeemer,
    EndorsementDatum,
    EndorsementSpendRedeemer,
    EndorsementValidatorDatumChallenge,
    EndorsementValidatorDatumEndorsement,
    EscalateToAuditRedeemer,
    ExpireChallengeRedeemer,
    HistoryBonusDatum,
    HistoryBonusSourceChallengeWon,
    InlineStakeCredential,
    LockForChallengeRedeemer,
    MintChallengeTokenRedeemer,
    MintEndorsementTokenRedeemer,
    MintHistoryBonusRedeemer,
    MintStakeTokenRedeemer,
    NoStakeCredential,
    OutputReference,
    PlutusAddress,
    ReleaseChallengeRedeemer,
    RepChallengeOutcomeVerified,
    RepChallengeStateEscalated,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ReputationChallengeDatum,
    ReputationConfig,
    ResolveChallengeRedeemer,
    ResolveEscalationRedeemer,
    SomeStakeCredential,
    StakeDatum,
    VerificationKeyCredential,
    WithdrawChallengeRedeemer,
    WithdrawEndorsementRedeemer,
    datum_cbor,
)

# The deploy helpers (scripts/deploy_common.py) build the config and the
# params datum that the SDK reads.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import deploy_common  # noqa: E402


VKEY_HASH = bytes.fromhex("2ef77ec4340057363c3824919a61db70ee9683ee9b7d15283aa91931")
AGENT_A_DID = bytes.fromhex("036dc41f85740d155904b4dff933c592ed3874bdea3b888724ed936ec6a08358")
AGENT_B_DID = bytes.fromhex("72f261c505ca9be49d7ba4bd38184bb97923fe3247862642e1ddc25fccd784b3")


class TestWave4Codecs:
    """The W4 contract changes: stake lock redeemers, Escalated with the
    claim token name, and the Module 1 Resolved state with refund_to."""

    def test_lock_redeemers(self):
        ref = OutputReference(transaction_id=b"\xaa" * 32, output_index=2)
        assert cbor2.loads(LockForChallengeRedeemer().to_cbor()) == cbor2.CBORTag(127, [])
        release = cbor2.loads(ReleaseChallengeRedeemer(challenge_ref=ref).to_cbor())
        assert release.tag == 1280 and release.value[0].value == [b"\xaa" * 32, 2]

    def test_escalated_holds_the_claim_token_name(self):
        state = RepChallengeStateEscalated(audit_claim_token=b"\x0c" * 32)
        assert cbor2.loads(state.to_cbor()) == cbor2.CBORTag(122, [b"\x0c" * 32])

    def test_module1_resolved_state_with_refund_to(self):
        refund_to = PlutusAddress(
            payment_credential=VerificationKeyCredential(VKEY_HASH),
            stake_credential=SomeStakeCredential(
                InlineStakeCredential(VerificationKeyCredential(b"\x05" * 28))),
        )
        datum = AuditChallengeDatum(
            claim_ref=OutputReference(transaction_id=b"\xaa" * 32, output_index=0),
            auditor_did=AGENT_B_DID,
            auditor_credential=VerificationKeyCredential(VKEY_HASH),
            stake_amount=50_000_000,
            evidence_hash=b"\x01" * 32,
            evidence_uri=b"ipfs://e",
            challenged_at=1000,
            resolution_deadline=2000,
            eligible_jurors=IndefiniteList([b"\x02" * 32]),
            state=AuditResolved(verdict=AuditAuditorWins(), refund_to=refund_to),
            claim_token=b"\x0c" * 32,
        )
        decoded = cbor2.loads(datum.to_cbor())
        assert len(decoded.value) == 11 and decoded.value[10] == b"\x0c" * 32
        state = decoded.value[9]
        assert state.tag == 124 and len(state.value) == 2  # Resolved { verdict, refund_to }
        assert state.value[0] == cbor2.CBORTag(122, [])
        assert AuditChallengeDatum.from_cbor(datum.to_cbor()) == datum
        enterprise = PlutusAddress(VerificationKeyCredential(VKEY_HASH), NoStakeCredential())
        assert cbor2.loads(enterprise.to_cbor()).value[1] == cbor2.CBORTag(122, [])


class TestOutputReference:
    def test_constr_id(self):
        oref = OutputReference(transaction_id=b"\xaa" * 32, output_index=0)
        assert oref.CONSTR_ID == 0

    def test_round_trip(self):
        oref = OutputReference(transaction_id=b"\xab" * 32, output_index=5)
        cbor_bytes = oref.to_cbor()
        restored = OutputReference.from_cbor(cbor_bytes)
        assert restored.transaction_id == oref.transaction_id
        assert restored.output_index == oref.output_index

    def test_v3_encoding_no_constr_wrapper(self):
        """V3 TransactionId is raw bytes, not Constr(0, [bytes])."""
        oref = OutputReference(transaction_id=b"\xaa" * 32, output_index=0)
        # Decode the CBOR to inspect structure
        decoded = cbor2.loads(oref.to_cbor())
        # Should be a CBORTag (tag 121 = constructor 0)
        assert decoded.tag == 121
        fields = decoded.value
        # First field should be raw bytes, not another CBORTag
        assert isinstance(fields[0], bytes)
        assert fields[0] == b"\xaa" * 32


SEED_REF = OutputReference(transaction_id=b"\x5e" * 32, output_index=3)


class TestStakeDatum:
    def test_round_trip(self):
        datum = StakeDatum(
            agent_did=AGENT_A_DID,
            owner_credential=VerificationKeyCredential(VKEY_HASH),
            stake_amount=10_000_000,
            staked_capabilities=IndefiniteList([b"code_review"]),
            last_updated=1775799692000,
            history_points=0,
            challenge_locks=IndefiniteList([b"\x0c" * 32]),
            stake_id=SEED_REF,
        )
        cbor_bytes = datum.to_cbor()
        restored = StakeDatum.from_cbor(cbor_bytes)
        assert restored.agent_did == AGENT_A_DID
        assert restored.stake_amount == 10_000_000
        assert restored.history_points == 0
        assert list(restored.challenge_locks) == [b"\x0c" * 32]
        assert restored.stake_id == SEED_REF

    def test_field_count(self):
        """8 fields: challenge_locks, then stake_id (an OutputReference)
        last. A fully decayed stake has stake_amount 0."""
        datum = StakeDatum(
            agent_did=AGENT_A_DID,
            owner_credential=VerificationKeyCredential(VKEY_HASH),
            stake_amount=0,
            staked_capabilities=IndefiniteList([]),
            last_updated=0,
            history_points=0,
            challenge_locks=IndefiniteList([]),
            stake_id=SEED_REF,
        )
        decoded = cbor2.loads(datum.to_cbor())
        assert len(decoded.value) == 8
        assert list(decoded.value[6]) == []
        assert decoded.value[7] == cbor2.CBORTag(121, [b"\x5e" * 32, 3])


class TestEndorsementValidatorDatumNesting:
    """Critical test: Aiken sum types use NESTED Constr encoding."""

    def test_endorsement_variant_nested(self):
        """Endorsement(datum) = Constr(0, [Constr(0, [8 fields])])."""
        inner = EndorsementDatum(
            endorser_did=AGENT_B_DID,
            endorser_credential=VerificationKeyCredential(VKEY_HASH),
            target_did=AGENT_A_DID,
            stake_amount=5_000_000,
            endorsed_capabilities=IndefiniteList([b"code_review"]),
            created_at=1000,
            slashed_by=IndefiniteList([b"\x0c" * 32]),
            target_stake=SEED_REF,
        )
        wrapper = EndorsementValidatorDatumEndorsement(datum=inner)
        decoded = cbor2.loads(wrapper.to_cbor())
        # Outer: tag 121 (constructor 0)
        assert decoded.tag == 121
        outer_fields = decoded.value
        assert len(outer_fields) == 1
        # Inner: tag 121 (constructor 0) with 8 fields: slashed_by, then
        # target_stake (the stake_id of the bound stake) last
        inner_decoded = outer_fields[0]
        assert inner_decoded.tag == 121
        assert len(inner_decoded.value) == 8
        assert list(inner_decoded.value[6]) == [b"\x0c" * 32]
        assert inner_decoded.value[7] == cbor2.CBORTag(121, [b"\x5e" * 32, 3])
        assert EndorsementValidatorDatumEndorsement.from_cbor(wrapper.to_cbor()) == wrapper

    def test_challenge_variant_nested(self):
        """Challenge(datum) = Constr(1, [Constr(0, [13 fields])])."""
        inner = ReputationChallengeDatum(
            challenger_did=AGENT_B_DID,
            challenger_credential=VerificationKeyCredential(VKEY_HASH),
            target_did=AGENT_A_DID,
            target_credential=VerificationKeyCredential(VKEY_HASH),
            challenged_capability=b"code_review",
            stake_amount=25_000_000,
            evidence_hash=b"\xab" * 32,
            evidence_uri=b"ipfs://test",
            created_at=1000,
            counter_evidence_hash=b"",
            counter_evidence_uri=b"",
            response_submitted_at=0,
            state=RepChallengeStateOpen(),
        )
        wrapper = EndorsementValidatorDatumChallenge(datum=inner)
        decoded = cbor2.loads(wrapper.to_cbor())
        # Outer: tag 122 (constructor 1)
        assert decoded.tag == 122
        outer_fields = decoded.value
        assert len(outer_fields) == 1
        # Inner: tag 121 (constructor 0) with 13 fields
        inner_decoded = outer_fields[0]
        assert inner_decoded.tag == 121
        assert len(inner_decoded.value) == 13

    def test_challenge_datum_round_trip(self):
        inner = ReputationChallengeDatum(
            challenger_did=AGENT_B_DID,
            challenger_credential=VerificationKeyCredential(VKEY_HASH),
            target_did=AGENT_A_DID,
            target_credential=VerificationKeyCredential(VKEY_HASH),
            challenged_capability=b"code_review",
            stake_amount=25_000_000,
            evidence_hash=b"\xab" * 32,
            evidence_uri=b"ipfs://test",
            created_at=1000,
            counter_evidence_hash=b"",
            counter_evidence_uri=b"",
            response_submitted_at=0,
            state=RepChallengeStateOpen(),
        )
        wrapper = EndorsementValidatorDatumChallenge(datum=inner)
        cbor_bytes = wrapper.to_cbor()
        restored = EndorsementValidatorDatumChallenge.from_cbor(cbor_bytes)
        assert restored.datum.challenger_did == AGENT_B_DID
        assert restored.datum.stake_amount == 25_000_000


class TestHistoryBonusDatum:
    def test_round_trip(self):
        datum = HistoryBonusDatum(
            agent_did=AGENT_A_DID,
            source=HistoryBonusSourceChallengeWon(),
            bonus_points=0,
            source_ref=OutputReference(transaction_id=b"\xab" * 32, output_index=0),
            created_at=1000,
        )
        cbor_bytes = datum.to_cbor()
        restored = HistoryBonusDatum.from_cbor(cbor_bytes)
        assert restored.agent_did == AGENT_A_DID
        assert restored.bonus_points == 0


class TestResolvedChallengeState:
    def test_resolved_with_outcome(self):
        state = RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified())
        decoded = cbor2.loads(state.to_cbor())
        # tag 123 = constructor 2 (Resolved)
        assert decoded.tag == 123
        # Contains outcome: tag 121 = constructor 0 (CapabilityVerified)
        assert decoded.value[0].tag == 121


class TestRedeemers:
    def test_mint_stake_token(self):
        r = MintStakeTokenRedeemer()
        assert r.CONSTR_ID == 0

    def test_mint_endorsement_token(self):
        r = MintEndorsementTokenRedeemer()
        assert r.CONSTR_ID == 0

    def test_mint_challenge_token(self):
        r = MintChallengeTokenRedeemer()
        assert r.CONSTR_ID == 2

    def test_burn_challenge_token(self):
        r = BurnChallengeTokenRedeemer()
        assert r.CONSTR_ID == 3

    def test_mint_history_bonus(self):
        """StakeMintAction has no BurnStakeToken: MintHistoryBonus is 1 and
        BurnGenesisBonus is 2."""
        assert MintHistoryBonusRedeemer().CONSTR_ID == 1
        assert BurnGenesisBonusRedeemer().CONSTR_ID == 2
        assert cbor2.loads(MintHistoryBonusRedeemer().to_cbor()) == cbor2.CBORTag(122, [])

    def test_module1_claim_datum_and_submit_redeemer(self):
        """The Module-1 claim of an escalation: ClaimDatum (9 fields, state
        Open; claim_type is the challenged capability) and
        ClaimAction.SubmitClaim (constructor 0)."""
        datum = AuditClaimDatum(
            claimer_did=AGENT_A_DID,
            claimer_credential=VerificationKeyCredential(VKEY_HASH),
            claim_hash=b"\x01" * 32,
            claim_type=b"code_review",
            storage_uri=b"",
            stake_amount=50_000_000,
            submitted_at=1000,
            challenge_window=1_800_000,
            state=AuditOpen(),
        )
        decoded = cbor2.loads(datum.to_cbor())
        assert decoded.tag == 121 and len(decoded.value) == 9
        assert decoded.value[3] == b"code_review"
        assert decoded.value[8] == cbor2.CBORTag(121, [])
        assert AuditClaimDatum.from_cbor(datum.to_cbor()) == datum
        assert cbor2.loads(AuditSubmitClaimRedeemer().to_cbor()) == cbor2.CBORTag(121, [])

    def test_escalation_redeemers(self):
        """ChallengeSpend(EscalateToAudit) is constructor 2, ChallengeSpend(
        ResolveEscalation) constructor 3, ChallengeSpend(WithdrawChallenge) 0,
        and DecreaseStake { amount } constructor 2 of StakeAction."""
        for action, tag in (
            (WithdrawChallengeRedeemer(), 121),
            (EscalateToAuditRedeemer(), 123),
            (ResolveEscalationRedeemer(), 124),
        ):
            decoded = cbor2.loads(ChallengeSpendRedeemer(action=action).to_cbor())
            assert decoded.tag == 122 and decoded.value[0].tag == tag
        assert cbor2.loads(DecreaseStakeRedeemer(5).to_cbor()) == cbor2.CBORTag(123, [5])

    def test_resolve_challenge_redeemer_nested(self):
        """ChallengeSpend(ResolveChallenge { CapabilityVerified })."""
        inner = ResolveChallengeRedeemer(outcome=RepChallengeOutcomeVerified())
        wrapper = ChallengeSpendRedeemer(action=inner)
        decoded = cbor2.loads(wrapper.to_cbor())
        # Outer: tag 122 (constructor 1 = ChallengeSpend)
        assert decoded.tag == 122
        # Inner: tag 125 (constructor 4 = ResolveChallenge)
        assert decoded.value[0].tag == 125

    def test_distribute_outcome_redeemer_nested(self):
        """ChallengeSpend(DistributeOutcome)."""
        wrapper = ChallengeSpendRedeemer(action=DistributeOutcomeRedeemer())
        decoded = cbor2.loads(wrapper.to_cbor())
        assert decoded.tag == 122  # ChallengeSpend
        assert decoded.value[0].tag == 127  # constructor 6 = DistributeOutcome

    def test_endorsement_spend_withdraw(self):
        """EndorsementSpend(WithdrawEndorsement)."""
        wrapper = EndorsementSpendRedeemer(action=WithdrawEndorsementRedeemer())
        decoded = cbor2.loads(wrapper.to_cbor())
        assert decoded.tag == 121  # EndorsementSpend (constructor 0)
        assert decoded.value[0].tag == 122  # WithdrawEndorsement (constructor 1)

    def test_expire_challenge_redeemer_nested(self):
        """ChallengeSpend(ExpireChallenge): the last RepChallengeAction
        constructor (7, CBOR tag 1280)."""
        wrapper = ChallengeSpendRedeemer(action=ExpireChallengeRedeemer())
        decoded = cbor2.loads(wrapper.to_cbor())
        assert decoded.tag == 122  # ChallengeSpend
        assert decoded.value[0].tag == 1280  # constructor 7 = ExpireChallenge
        again = ChallengeSpendRedeemer.from_cbor(wrapper.to_cbor())
        assert isinstance(again.action, ExpireChallengeRedeemer)


class TestReputationConfig:
    VALUES = dict(
        refs_token_policy="01" * 28,
        audit_claim_validator_hash="02" * 28,
        audit_challenge_validator_hash="03" * 28,
        audit_jury_pool_hash="04" * 28,
        audit_refs_token_policy="05" * 28,
        params_script_hash="06" * 28,
        treasury_script_hash="07" * 28,
        ap3x_policy_id="",
        ap3x_asset_name="",
        oracle_credential="0a" * 28,
        params_token_policy="0b" * 28,
        identity_hash="0c" * 28,
    )

    def test_twelve_fields_identity_hash_last(self):
        config = ReputationConfig(**{k: bytes.fromhex(v) for k, v in self.VALUES.items()})
        decoded = cbor2.loads(config.to_cbor())
        assert decoded.tag == 121
        assert len(decoded.value) == 12
        assert decoded.value[-1] == bytes.fromhex("0c" * 28)
        assert decoded.value[-2] == bytes.fromhex("0b" * 28)

    def test_same_data_as_the_deploy_scripts(self):
        """The SDK mirror has the field order of deploy_common (config.ak)."""
        config = ReputationConfig(**{k: bytes.fromhex(v) for k, v in self.VALUES.items()})
        deployed = deploy_common.build_reputation_config_cbor(**self.VALUES)
        assert cbor2.loads(config.to_cbor()) == cbor2.loads(bytes.fromhex(deployed))


class TestProtocolParamsRecord:
    def test_record_datum_of_the_deploy_scripts(self):
        """The params record datum of the deploy scripts decodes to the SDK
        defaults, with genesis_minting_window as an absolute POSIX ms time."""
        end_ms = 1_764_000_000_000
        raw = RawCBOR(deploy_common.build_protocol_params_cbor(end_ms))
        params = ProtocolParamsInfo.from_datum(raw)
        assert params == ProtocolParamsInfo(genesis_minting_window=end_ms)

    def test_datum_cbor_reads_raw_and_plutus_data(self):
        datum = StakeDatum(
            agent_did=AGENT_A_DID,
            owner_credential=VerificationKeyCredential(VKEY_HASH),
            stake_amount=1,
            staked_capabilities=IndefiniteList([b"x"]),
            last_updated=2,
            history_points=0,
            challenge_locks=IndefiniteList([]),
            stake_id=SEED_REF,
        )
        assert datum_cbor(datum) == datum.to_cbor()
        assert datum_cbor(RawCBOR(datum.to_cbor())) == datum.to_cbor()
        with pytest.raises(ValueError):
            datum_cbor(None)
