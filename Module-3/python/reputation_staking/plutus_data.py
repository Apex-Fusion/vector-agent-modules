"""
PyCardano PlutusData definitions matching the on-chain Aiken types.

Source of truth: reputation_staking/lib/reputation_staking/types.ak,
params.ak (ProtocolParams) and config.ak (ReputationConfig).

CONSTR_ID values and field order match the Aiken constructor indices.
All types are Plutus V3 (Conway). Every time is POSIX milliseconds.

Encoding gotchas:
  - Sum type variants use NESTED Constr:
    EndorsementValidatorDatum.Challenge(datum) = Constr(1, [Constr(0, [13 fields])])
  - OutputReference: TransactionId is raw ByteArray in V3 (no Constr wrapper)

An agent identifier in a `*_did` field is the 32-byte identifier of the
agent's identity record (did:vector), not a DID string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Type, TypeVar, Union

from pycardano import PlutusData
from pycardano.serialization import IndefiniteList, RawCBOR


# ── Credential Types ─────────────────────────────────────────────────────────


@dataclass
class VerificationKeyCredential(PlutusData):
    """Constructor 0: VerificationKey(VerificationKeyHash)"""
    CONSTR_ID = 0
    vkey_hash: bytes


@dataclass
class ScriptCredential(PlutusData):
    """Constructor 1: Script(ScriptHash)"""
    CONSTR_ID = 1
    script_hash: bytes


Credential = Union[VerificationKeyCredential, ScriptCredential]


# ── OutputReference (Plutus V3) ──────────────────────────────────────────────


@dataclass
class OutputReference(PlutusData):
    """Constructor 0: OutputReference { transaction_id, output_index }

    V3: TransactionId is a raw ByteArray (NOT wrapped in Constr).
    """
    CONSTR_ID = 0
    transaction_id: bytes  # 32-byte tx hash (raw, not Constr-wrapped)
    output_index: int


# ── Self-Stake Types (Section 4.1) ──────────────────────────────────────────


@dataclass
class StakeDatum(PlutusData):
    """StakeDatum — one per agent at the reputation validator address."""
    CONSTR_ID = 0
    agent_did: bytes
    owner_credential: Credential
    stake_amount: int
    staked_capabilities: IndefiniteList  # List<ByteArray>
    last_updated: int  # POSIX ms
    history_points: int


# StakeAction redeemers (reputation spend)

@dataclass
class CreateStakeRedeemer(PlutusData):
    """Constructor 0: CreateStake"""
    CONSTR_ID = 0


@dataclass
class IncreaseStakeRedeemer(PlutusData):
    """Constructor 1: IncreaseStake"""
    CONSTR_ID = 1


@dataclass
class DecreaseStakeRedeemer(PlutusData):
    """Constructor 2: DecreaseStake { amount }"""
    CONSTR_ID = 2
    amount: int


@dataclass
class UpdateCapabilitiesRedeemer(PlutusData):
    """Constructor 3: UpdateCapabilities { new_capabilities }"""
    CONSTR_ID = 3
    new_capabilities: IndefiniteList  # List<ByteArray>


@dataclass
class ClaimDecayRefundRedeemer(PlutusData):
    """Constructor 4: ClaimDecayRefund"""
    CONSTR_ID = 4


@dataclass
class SlashStakeRedeemer(PlutusData):
    """Constructor 5: SlashStake { challenge_ref }"""
    CONSTR_ID = 5
    challenge_ref: OutputReference


# StakeMintAction redeemers (reputation mint)

@dataclass
class MintStakeTokenRedeemer(PlutusData):
    """Constructor 0: MintStakeToken"""
    CONSTR_ID = 0


@dataclass
class BurnStakeTokenRedeemer(PlutusData):
    """Constructor 1: BurnStakeToken"""
    CONSTR_ID = 1


@dataclass
class MintHistoryBonusRedeemer(PlutusData):
    """Constructor 2: MintHistoryBonus"""
    CONSTR_ID = 2


@dataclass
class BurnGenesisBonusRedeemer(PlutusData):
    """Constructor 3: BurnGenesisBonus"""
    CONSTR_ID = 3


# ── Endorsement Types (Section 4.2) ─────────────────────────────────────────


@dataclass
class EndorsementDatum(PlutusData):
    """EndorsementDatum — one per endorser-target pair."""
    CONSTR_ID = 0
    endorser_did: bytes
    endorser_credential: Credential
    target_did: bytes
    stake_amount: int
    endorsed_capabilities: IndefiniteList  # List<ByteArray>
    created_at: int  # POSIX ms


# EndorsementAction redeemers

@dataclass
class IncreaseEndorsementRedeemer(PlutusData):
    """Constructor 0: IncreaseEndorsement"""
    CONSTR_ID = 0


@dataclass
class WithdrawEndorsementRedeemer(PlutusData):
    """Constructor 1: WithdrawEndorsement"""
    CONSTR_ID = 1


@dataclass
class SlashEndorsementRedeemer(PlutusData):
    """Constructor 2: SlashEndorsement { challenge_ref }"""
    CONSTR_ID = 2
    challenge_ref: OutputReference


# EndorsementMintAction redeemers

@dataclass
class MintEndorsementTokenRedeemer(PlutusData):
    """Constructor 0: MintEndorsementToken"""
    CONSTR_ID = 0


@dataclass
class BurnEndorsementTokenRedeemer(PlutusData):
    """Constructor 1: BurnEndorsementToken"""
    CONSTR_ID = 1


@dataclass
class MintChallengeTokenRedeemer(PlutusData):
    """Constructor 2: MintChallengeToken"""
    CONSTR_ID = 2


@dataclass
class BurnChallengeTokenRedeemer(PlutusData):
    """Constructor 3: BurnChallengeToken"""
    CONSTR_ID = 3


# ── Challenge Types (Section 4.3) ────────────────────────────────────────────


# RepChallengeState variants

@dataclass
class RepChallengeStateOpen(PlutusData):
    """Constructor 0: Open"""
    CONSTR_ID = 0


@dataclass
class RepChallengeStateEscalated(PlutusData):
    """Constructor 1: Escalated { audit_claim_ref }"""
    CONSTR_ID = 1
    audit_claim_ref: OutputReference


@dataclass
class RepChallengeStateResolved(PlutusData):
    """Constructor 2: Resolved { outcome }"""
    CONSTR_ID = 2
    outcome: "RepChallengeOutcomeData"


RepChallengeStateData = Union[
    RepChallengeStateOpen, RepChallengeStateEscalated, RepChallengeStateResolved
]

# RepChallengeOutcome variants


@dataclass
class RepChallengeOutcomeVerified(PlutusData):
    """Constructor 0: CapabilityVerified"""
    CONSTR_ID = 0


@dataclass
class RepChallengeOutcomeFalsified(PlutusData):
    """Constructor 1: CapabilityFalsified"""
    CONSTR_ID = 1


@dataclass
class RepChallengeOutcomeInconclusive(PlutusData):
    """Constructor 2: Inconclusive"""
    CONSTR_ID = 2


RepChallengeOutcomeData = Union[
    RepChallengeOutcomeVerified,
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeInconclusive,
]


@dataclass
class ReputationChallengeDatum(PlutusData):
    """ReputationChallengeDatum — 13 fields."""
    CONSTR_ID = 0
    challenger_did: bytes
    challenger_credential: Credential
    target_did: bytes
    target_credential: Credential
    challenged_capability: bytes
    stake_amount: int
    evidence_hash: bytes
    evidence_uri: bytes
    created_at: int  # POSIX ms
    counter_evidence_hash: bytes
    counter_evidence_uri: bytes
    response_submitted_at: int
    state: RepChallengeStateData


# RepChallengeAction redeemers

@dataclass
class WithdrawChallengeRedeemer(PlutusData):
    """Constructor 0: WithdrawChallenge"""
    CONSTR_ID = 0


@dataclass
class RespondToChallengeRedeemer(PlutusData):
    """Constructor 1: RespondToChallenge { counter_evidence_hash, counter_evidence_uri }"""
    CONSTR_ID = 1
    counter_evidence_hash: bytes
    counter_evidence_uri: bytes


@dataclass
class EscalateToAuditRedeemer(PlutusData):
    """Constructor 2: EscalateToAudit"""
    CONSTR_ID = 2


@dataclass
class ResolveEscalationRedeemer(PlutusData):
    """Constructor 3: ResolveEscalation"""
    CONSTR_ID = 3


@dataclass
class ResolveChallengeRedeemer(PlutusData):
    """Constructor 4: ResolveChallenge { outcome }"""
    CONSTR_ID = 4
    outcome: RepChallengeOutcomeData


@dataclass
class DefaultJudgmentRedeemer(PlutusData):
    """Constructor 5: DefaultJudgment"""
    CONSTR_ID = 5


@dataclass
class DistributeOutcomeRedeemer(PlutusData):
    """Constructor 6: DistributeOutcome"""
    CONSTR_ID = 6


@dataclass
class ExpireChallengeRedeemer(PlutusData):
    """Constructor 7: ExpireChallenge (anybody closes an unresolved
    challenge after the challenge timeout)"""
    CONSTR_ID = 7


# ── History Bonus Types (Section 4.4) ───────────────────────────────────────

# HistoryBonusSource variants (constructors 0-8)

@dataclass
class HistoryBonusSourceChallengeWon(PlutusData):
    CONSTR_ID = 0


@dataclass
class HistoryBonusSourceAuditClaimWon(PlutusData):
    CONSTR_ID = 1


@dataclass
class HistoryBonusSourceJurorDuty(PlutusData):
    CONSTR_ID = 2


@dataclass
class HistoryBonusSourceProposalAdopted(PlutusData):
    CONSTR_ID = 3


@dataclass
class HistoryBonusSourceCritiqueIncorporated(PlutusData):
    CONSTR_ID = 4


@dataclass
class HistoryBonusSourceEscrowCompleted(PlutusData):
    CONSTR_ID = 5


@dataclass
class HistoryBonusSourceUsefulWorkVerified(PlutusData):
    CONSTR_ID = 6


@dataclass
class HistoryBonusSourceSecurityReportValidated(PlutusData):
    CONSTR_ID = 7


@dataclass
class HistoryBonusSourceGenesisBonus(PlutusData):
    CONSTR_ID = 8


HistoryBonusSourceData = Union[
    HistoryBonusSourceChallengeWon,
    HistoryBonusSourceAuditClaimWon,
    HistoryBonusSourceJurorDuty,
    HistoryBonusSourceProposalAdopted,
    HistoryBonusSourceCritiqueIncorporated,
    HistoryBonusSourceEscrowCompleted,
    HistoryBonusSourceUsefulWorkVerified,
    HistoryBonusSourceSecurityReportValidated,
    HistoryBonusSourceGenesisBonus,
]


@dataclass
class HistoryBonusDatum(PlutusData):
    """HistoryBonusDatum — 5 fields."""
    CONSTR_ID = 0
    agent_did: bytes
    source: HistoryBonusSourceData
    bonus_points: int
    source_ref: OutputReference
    created_at: int  # POSIX ms


# ── Endorsement Validator Wrapper Types ──────────────────────────────────────
# These use NESTED Constr encoding:
#   Endorsement(datum) = Constr(0, [Constr(0, [6 fields])])
#   Challenge(datum) = Constr(1, [Constr(0, [13 fields])])


@dataclass
class EndorsementValidatorDatumEndorsement(PlutusData):
    """Constructor 0: Endorsement(EndorsementDatum) — nested Constr."""
    CONSTR_ID = 0
    datum: EndorsementDatum


@dataclass
class EndorsementValidatorDatumChallenge(PlutusData):
    """Constructor 1: Challenge(ReputationChallengeDatum) — nested Constr."""
    CONSTR_ID = 1
    datum: ReputationChallengeDatum


EndorsementValidatorDatumData = Union[
    EndorsementValidatorDatumEndorsement, EndorsementValidatorDatumChallenge
]


# ── Endorsement Validator Action Wrappers ────────────────────────────────────
# Also nested Constr:
#   EndorsementSpend(action) = Constr(0, [Constr(idx, [fields])])
#   ChallengeSpend(action) = Constr(1, [Constr(idx, [fields])])


@dataclass
class EndorsementSpendRedeemer(PlutusData):
    """Constructor 0: EndorsementSpend(EndorsementAction)"""
    CONSTR_ID = 0
    action: Union[
        IncreaseEndorsementRedeemer,
        WithdrawEndorsementRedeemer,
        SlashEndorsementRedeemer,
    ]


@dataclass
class ChallengeSpendRedeemer(PlutusData):
    """Constructor 1: ChallengeSpend(RepChallengeAction)"""
    CONSTR_ID = 1
    action: Union[
        WithdrawChallengeRedeemer,
        RespondToChallengeRedeemer,
        EscalateToAuditRedeemer,
        ResolveEscalationRedeemer,
        ResolveChallengeRedeemer,
        DefaultJudgmentRedeemer,
        DistributeOutcomeRedeemer,
        ExpireChallengeRedeemer,
    ]


# ── ProtocolParams ───────────────────────────────────────────────────────────


@dataclass
class ProtocolParams(PlutusData):
    """ProtocolParams datum — 22 fields (params.ak).

    The datum of the params record: the output at the params holder that
    holds the `params` record token. Durations are POSIX milliseconds,
    genesis_minting_window is an absolute POSIX time in milliseconds, and
    activity_window counts epochs of epoch_length milliseconds.
    """
    CONSTR_ID = 0
    min_self_stake: int
    min_endorsement: int
    min_challenge_stake: int
    stake_cooldown: int  # ms
    endorsement_cooldown: int  # ms
    decay_rate: int
    activity_window: int  # epochs
    decay_collector_fee: int
    history_multiplier: int
    max_endorsement_multiplier: int
    slash_rate_endorser: int
    protocol_fee_rate: int
    challenge_response_deadline: int  # ms
    min_agent_age: int  # ms
    escalation_window: int  # ms
    default_judgment_fee: int
    genesis_agent_cap: int
    genesis_bonus_amount: int
    genesis_minting_window: int  # POSIX ms
    genesis_protection_period: int  # ms
    valid_capabilities: IndefiniteList  # List<ByteArray>
    epoch_length: int  # ms


# ── ReputationConfig ─────────────────────────────────────────────────────────


@dataclass
class ReputationConfig(PlutusData):
    """ReputationConfig — 12 fields (config.ak), the parameter of both
    validators. identity_hash is the last field. The deploy scripts apply
    it; the SDK reads its values from the deploy state
    (reputation_staking.deployment)."""
    CONSTR_ID = 0
    refs_token_policy: bytes
    audit_claim_validator_hash: bytes
    audit_challenge_validator_hash: bytes
    audit_jury_pool_hash: bytes
    audit_refs_token_policy: bytes
    params_script_hash: bytes
    treasury_script_hash: bytes
    ap3x_policy_id: bytes
    ap3x_asset_name: bytes
    oracle_credential: bytes
    params_token_policy: bytes
    identity_hash: bytes


# ── Inline datums of UTxOs ───────────────────────────────────────────────────

T = TypeVar("T", bound=PlutusData)


def datum_cbor(datum: Any) -> bytes:
    """The CBOR bytes of an inline datum: RawCBOR (Ogmios), a PlutusData
    object, or bytes. Raises ValueError for an output without a datum."""
    if datum is None:
        raise ValueError("the output has no inline datum")
    if isinstance(datum, RawCBOR):
        return bytes(datum.cbor)
    if isinstance(datum, (bytes, bytearray)):
        return bytes(datum)
    to_cbor = getattr(datum, "to_cbor", None)
    if callable(to_cbor):
        encoded = to_cbor()
        return bytes.fromhex(encoded) if isinstance(encoded, str) else bytes(encoded)
    raise ValueError(f"cannot read an inline datum from {type(datum).__name__}")


def decode_datum(cls: Type[T], datum: Any) -> T:
    """Decode an inline datum as `cls`."""
    return cls.from_cbor(datum_cbor(datum))
