"""Offline tests of the escalation builders of ReputationStakingClient:
EscalateToAudit (the Module-3 challenge moves to Module-1: the transaction
mints the one Module-1 claim token and creates the Module-1 claim of the
target) and ResolveEscalation (the challenge takes the verdict of the
resolved Module-1 challenge of that claim).

tests/test_simulate.py runs both builders against the compiled Module-3
validators and the compiled Module-1 claim validator.
"""

from __future__ import annotations

import dataclasses

import pytest
from pycardano import PaymentSigningKey

from reputation_staking import vector_identity as vi
from reputation_staking.client import escalation_deadline
from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.plutus_data import (
    AuditAuditorWins,
    AuditClaimDatum,
    AuditClaimerWins,
    AuditInconclusive,
    AuditOpen,
    AuditPendingOracle,
    AuditSubmitClaimRedeemer,
    ChallengeSpendRedeemer,
    EndorsementValidatorDatumChallenge,
    EscalateToAuditRedeemer,
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeInconclusive,
    RepChallengeOutcomeVerified,
    RepChallengeStateEscalated,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ResolveEscalationRedeemer,
    VerificationKeyCredential,
    decode_datum,
)
from reputation_staking.token_names import derive_audit_claim_token_name
from tests.chain_fixtures import (
    AP3X,
    CLAIM_HASH,
    CLAIM_REF,
    END_HASH,
    END_REF,
    LO,
    World,
    address,
    assert_window,
    audit_challenge_utxo,
    audit_resolved,
    challenge_utxo,
    inputs_of,
    key_hash,
    mint_of,
    outputs_to,
    public_key,
    redeemers_of,
    ref,
    refs_of,
    signers_of,
)

PARAMS = ProtocolParamsInfo()
HOUR = 3_600_000
CLAIM_STAKE = 50 * AP3X
WINDOW = 1_800_000
COUNTER = b"\x0f" * 32


@pytest.fixture
def world():
    w = World()
    w.record("A")
    w.record("B")
    return w


def claim_token_of(tx) -> bytes:
    """The claim token name of an escalation: from the first input."""
    first = min(tx.transaction_body.inputs, key=lambda i: (bytes(i.transaction_id), i.index))
    return bytes.fromhex(derive_audit_claim_token_name(bytes(first.transaction_id).hex(), first.index))


# ── EscalateToAudit ──────────────────────────────────────────────────────────


class TestEscalateToAudit:
    def setup(self, world, created_at=LO - 13 * HOUR, response=True, challenger_key=None,
              state=None, capability="code_review"):
        world.stake_a(challenge_locks=(world.lock_b(capability),))
        extra = dict(counter_evidence_hash=COUNTER, response_submitted_at=created_at + HOUR)
        return world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], challenger_key or key_hash(world.wallet), world.owner_a,
            created_at=created_at, state=state, capability=capability,
            **(extra if response else {}),
        ))

    def build(self, world, capability="code_review", **kwargs):
        kwargs.setdefault("claimer_skey", world.owner_a_key)
        return world.client().build_escalate_to_audit(
            world.did("B"), world.did("A"), capability, CLAIM_STAKE, WINDOW, CLAIM_REF,
            storage_uri="ipfs://claim", **kwargs
        )

    def test_shape(self, world):
        """The target escalates: its declared key (target_credential) signs
        the Module-1 claim; the wallet pays the claim stake. The claim is a
        claim of the target on the challenged capability."""
        challenge = self.setup(world)
        tx, claim_token = self.build(world)
        assert claim_token == claim_token_of(tx)
        assert ref(challenge) in inputs_of(tx)
        assert CLAIM_REF not in inputs_of(tx)
        # The target's record (Module-1 claim rules), the Module-1 claim
        # reference script, ProtocolParams and CrossValidatorRefs
        assert refs_of(tx) == {
            ref(world.records["A"]), CLAIM_REF, END_REF,
        } | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet), world.owner_a}
        assert_window(tx)
        assert mint_of(tx) == {(CLAIM_HASH, claim_token): 1}
        # The claim: exactly the claim stake and the claim token
        [claim] = outputs_to(tx, address(CLAIM_HASH))
        assert claim.amount.coin == CLAIM_STAKE
        assert {(bytes(p.payload).hex(), bytes(n.payload)): q
                for p, a in claim.amount.multi_asset.items() for n, q in a.items()} == {
            (CLAIM_HASH, claim_token): 1}
        datum = decode_datum(AuditClaimDatum, claim.datum)
        before = decode_datum(EndorsementValidatorDatumChallenge, challenge.output.datum).datum
        assert datum == AuditClaimDatum(
            claimer_did=world.ids["A"],
            claimer_credential=VerificationKeyCredential(world.owner_a),
            claim_hash=vi.blake2b_256(b"\x0e" * 32 + COUNTER),
            claim_type=b"code_review",
            storage_uri=b"ipfs://claim",
            stake_amount=CLAIM_STAKE,
            submitted_at=LO,
            challenge_window=WINDOW,
            state=AuditOpen(),
        )
        # The challenge continues: same address and value, Escalated
        [cont] = outputs_to(tx, address(END_HASH))
        assert cont.amount == challenge.output.amount
        assert cont.datum.datum == dataclasses.replace(
            before, state=RepChallengeStateEscalated(audit_claim_token=claim_token))
        kinds = sorted(type(d).__name__ for _, d in redeemers_of(tx))
        assert kinds == ["AuditSubmitClaimRedeemer", "ChallengeSpendRedeemer"]
        spend = [d for _, d in redeemers_of(tx) if isinstance(d, ChallengeSpendRedeemer)]
        assert spend == [ChallengeSpendRedeemer(action=EscalateToAuditRedeemer())]
        assert AuditSubmitClaimRedeemer() in [d for _, d in redeemers_of(tx)]

    def test_the_challenger_escalates_with_the_target_claimer_key(self, world):
        """challenger_credential (the wallet key) signs for Module-3; the
        claimer key of the target signs the Module-1 claim."""
        self.setup(world)
        tx, _ = self.build(world)
        assert key_hash(world.wallet) in signers_of(tx)

    def test_the_claim_is_on_the_challenged_capability(self, world):
        """claim_type is the challenged capability of the challenge datum,
        and the claimer is the target (not the challenger, whose key is the
        wallet key)."""
        self.setup(world, capability="testing")
        tx, _ = self.build(world, capability="testing")
        [claim] = outputs_to(tx, address(CLAIM_HASH))
        datum = decode_datum(AuditClaimDatum, claim.datum)
        assert datum.claim_type == b"testing"
        assert datum.claimer_did == world.ids["A"]

    def test_deadline_boundary(self, world):
        hi = LO + 900_000
        created_at = hi - PARAMS.challenge_response_deadline - PARAMS.escalation_window
        self.setup(world, created_at=created_at)
        assert escalation_deadline(PARAMS, created_at) == hi
        self.build(world)

    def test_refuses_after_the_escalation_window(self, world):
        hi = LO + 900_000
        self.setup(world, created_at=hi - PARAMS.challenge_response_deadline - PARAMS.escalation_window - 1)
        with pytest.raises(ValueError, match="escalation window"):
            self.build(world)

    def test_refuses_without_a_response(self, world):
        self.setup(world, response=False)
        with pytest.raises(ValueError, match="response of the target"):
            self.build(world)

    def test_refuses_a_challenge_that_is_not_open(self, world):
        self.setup(world, state=RepChallengeStateEscalated(audit_claim_token=b"clm_" + b"\x02" * 28))
        with pytest.raises(ValueError, match="state Open"):
            self.build(world)

    def test_refuses_a_claimer_key_that_the_target_does_not_declare(self, world):
        self.setup(world)
        with pytest.raises(vi.IdentityError, match="neither the controller"):
            self.build(world, claimer_skey=PaymentSigningKey.generate())

    def test_refuses_a_target_that_is_not_active(self, world):
        self.setup(world)
        hold = vi.Hold(vi.Signer.controller_set(), 1, LO - 1000, LO + 1)
        world.record("A", status=vi.Status.suspended([hold]))
        with pytest.raises(vi.IdentityStatusError):
            self.build(world)

    def test_refuses_without_a_party_signature(self, world):
        """The wallet key is neither challenger_credential nor
        target_credential, and the claimer key is a declared key of the
        target that is not target_credential."""
        self.setup(world, challenger_key=key_hash(PaymentSigningKey.generate()))
        with pytest.raises(ValueError, match="challenger_credential or target_credential"):
            self.build(world, claimer_skey=world.wallet)

    def test_refuses_another_reference_script(self, world):
        self.setup(world)
        with pytest.raises(ValueError, match="not the Module-1 claim validator"):
            world.client().build_escalate_to_audit(
                world.did("B"), world.did("A"), "code_review", CLAIM_STAKE, WINDOW,
                world.state["tx_hashes"]["reputation_spend_ref"] + "#0",
                claimer_skey=world.owner_a_key,
            )

    def test_refuses_a_claim_stake_below_the_output_minimum(self, world):
        self.setup(world)
        with pytest.raises(ValueError, match="minimum lovelace"):
            world.client().build_escalate_to_audit(
                world.did("B"), world.did("A"), "code_review", 1, WINDOW, CLAIM_REF,
                claimer_skey=world.owner_a_key,
            )


# ── ResolveEscalation ────────────────────────────────────────────────────────

CLAIM_TOKEN = b"clm_" + b"\x07" * 28


class TestResolveEscalation:
    def setup(self, world, verdict=None, auditor=None, state=None, **kwargs):
        challenge = world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            created_at=LO - 2 * 86_400_000,
            counter_evidence_hash=COUNTER, response_submitted_at=LO - 2 * 86_400_000 + HOUR,
            state=state or RepChallengeStateEscalated(audit_claim_token=CLAIM_TOKEN),
        ))
        audit = world.context.add(audit_challenge_utxo(
            CLAIM_TOKEN, auditor or world.ids["B"],
            audit_resolved(verdict or AuditAuditorWins()), **kwargs,
        ))
        return challenge, audit

    def build(self, world, audit_challenge_ref=None):
        return world.client().build_resolve_escalation(
            world.did("B"), world.did("A"), "code_review", audit_challenge_ref,
        )

    @pytest.mark.parametrize("verdict, outcome", [
        (AuditClaimerWins(), RepChallengeOutcomeVerified()),
        (AuditAuditorWins(), RepChallengeOutcomeFalsified()),
        (AuditInconclusive(), RepChallengeOutcomeInconclusive()),
    ])
    def test_maps_the_verdict(self, world, verdict, outcome):
        challenge, audit = self.setup(world, verdict=verdict)
        tx = self.build(world)
        assert inputs_of(tx) >= {ref(challenge)}
        assert ref(audit) not in inputs_of(tx)
        assert refs_of(tx) == {ref(audit), END_REF} | world.deployment_refs()
        assert signers_of(tx) == {key_hash(world.wallet)}
        assert mint_of(tx) == {}
        [cont] = outputs_to(tx, address(END_HASH))
        assert cont.amount == challenge.output.amount
        before = decode_datum(EndorsementValidatorDatumChallenge, challenge.output.datum).datum
        assert cont.datum.datum == dataclasses.replace(
            before, state=RepChallengeStateResolved(outcome=outcome))
        [(_, data)] = redeemers_of(tx)
        assert data == ChallengeSpendRedeemer(action=ResolveEscalationRedeemer())

    def test_named_module1_challenge(self, world):
        _, audit = self.setup(world)
        assert ref(audit) in refs_of(self.build(world, audit_challenge_ref=ref(audit)))

    def test_the_target_can_be_the_auditor(self, world):
        self.setup(world, auditor=world.ids["A"])
        self.build(world)

    def test_refuses_a_challenge_that_is_not_escalated(self, world):
        self.setup(world, state=RepChallengeStateOpen())
        with pytest.raises(ValueError, match="state Escalated"):
            self.build(world)

    def test_refuses_an_unresolved_module1_challenge(self, world):
        _, audit = self.setup(world)
        world.context.remove(audit)
        world.context.add(audit_challenge_utxo(CLAIM_TOKEN, world.ids["B"], AuditPendingOracle()))
        with pytest.raises(ValueError, match="not resolved"):
            self.build(world)

    def test_refuses_an_auditor_that_is_no_party(self, world):
        self.setup(world, auditor=vi.identifier_from_seed(bytes.fromhex("0d" * 32), 0))
        with pytest.raises(ValueError, match="neither the challenger nor the target"):
            self.build(world)

    def test_refuses_without_a_module1_challenge_of_the_claim(self, world):
        world.context.add(challenge_utxo(
            world.ids["B"], world.ids["A"], key_hash(world.wallet), world.owner_a,
            state=RepChallengeStateEscalated(audit_claim_token=CLAIM_TOKEN),
        ))
        world.context.add(audit_challenge_utxo(
            b"clm_" + b"\x08" * 28, world.ids["B"], audit_resolved(AuditAuditorWins()),
        ))
        with pytest.raises(RuntimeError, match="no Module-1 challenge"):
            self.build(world)

    def test_refuses_a_named_output_at_another_address(self, world):
        _, audit = self.setup(world, address_hash="c3" * 28)
        with pytest.raises(ValueError, match="is not a Module-1 challenge"):
            self.build(world, audit_challenge_ref=ref(audit))

    def test_refuses_a_named_output_with_another_claim_token(self, world):
        self.setup(world)
        other = world.context.add(audit_challenge_utxo(
            b"clm_" + b"\x08" * 28, world.ids["B"], audit_resolved(AuditAuditorWins()),
        ))
        with pytest.raises(ValueError, match="is not a Module-1 challenge"):
            self.build(world, audit_challenge_ref=ref(other))
