"""Tests for PlutusData types — CBOR round-trips and encoding verification."""

import cbor2
import pytest
from pycardano.serialization import IndefiniteList

from reputation_staking.plutus_data import (
    BurnChallengeTokenRedeemer,
    ChallengeSpendRedeemer,
    CreateStakeRedeemer,
    DistributeOutcomeRedeemer,
    EndorsementDatum,
    EndorsementSpendRedeemer,
    EndorsementValidatorDatumChallenge,
    EndorsementValidatorDatumEndorsement,
    HistoryBonusDatum,
    HistoryBonusSourceChallengeWon,
    MintChallengeTokenRedeemer,
    MintEndorsementTokenRedeemer,
    MintHistoryBonusRedeemer,
    MintStakeTokenRedeemer,
    OutputReference,
    RepChallengeOutcomeVerified,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ReputationChallengeDatum,
    ResolveChallengeRedeemer,
    StakeDatum,
    VerificationKeyCredential,
    WithdrawEndorsementRedeemer,
)


VKEY_HASH = bytes.fromhex("2ef77ec4340057363c3824919a61db70ee9683ee9b7d15283aa91931")
AGENT_A_DID = bytes.fromhex("036dc41f85740d155904b4dff933c592ed3874bdea3b888724ed936ec6a08358")
AGENT_B_DID = bytes.fromhex("72f261c505ca9be49d7ba4bd38184bb97923fe3247862642e1ddc25fccd784b3")


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


class TestStakeDatum:
    def test_round_trip(self):
        datum = StakeDatum(
            agent_did=AGENT_A_DID,
            owner_credential=VerificationKeyCredential(VKEY_HASH),
            stake_amount=10_000_000,
            staked_capabilities=IndefiniteList([b"code_review"]),
            last_updated=1775799692000,
            history_points=0,
        )
        cbor_bytes = datum.to_cbor()
        restored = StakeDatum.from_cbor(cbor_bytes)
        assert restored.agent_did == AGENT_A_DID
        assert restored.stake_amount == 10_000_000
        assert restored.history_points == 0

    def test_field_count(self):
        datum = StakeDatum(
            agent_did=AGENT_A_DID,
            owner_credential=VerificationKeyCredential(VKEY_HASH),
            stake_amount=10_000_000,
            staked_capabilities=IndefiniteList([]),
            last_updated=0,
            history_points=0,
        )
        decoded = cbor2.loads(datum.to_cbor())
        assert len(decoded.value) == 6


class TestEndorsementValidatorDatumNesting:
    """Critical test: Aiken sum types use NESTED Constr encoding."""

    def test_endorsement_variant_nested(self):
        """Endorsement(datum) = Constr(0, [Constr(0, [6 fields])])."""
        inner = EndorsementDatum(
            endorser_did=AGENT_B_DID,
            endorser_credential=VerificationKeyCredential(VKEY_HASH),
            target_did=AGENT_A_DID,
            stake_amount=5_000_000,
            endorsed_capabilities=IndefiniteList([b"code_review"]),
            created_at=1000,
        )
        wrapper = EndorsementValidatorDatumEndorsement(datum=inner)
        decoded = cbor2.loads(wrapper.to_cbor())
        # Outer: tag 121 (constructor 0)
        assert decoded.tag == 121
        outer_fields = decoded.value
        assert len(outer_fields) == 1
        # Inner: tag 121 (constructor 0) with 6 fields
        inner_decoded = outer_fields[0]
        assert inner_decoded.tag == 121
        assert len(inner_decoded.value) == 6

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
        r = MintHistoryBonusRedeemer()
        assert r.CONSTR_ID == 2

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
