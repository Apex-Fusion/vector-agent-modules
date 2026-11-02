"""
Off-chain data models for Module 3: Reputation Staking.

These mirror the on-chain Aiken types defined in:
  reputation_staking/lib/reputation_staking/types.ak

Used by the indexer, scoring engine, and client API. Every time is POSIX
milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from reputation_staking.plutus_data import ProtocolParams, decode_datum


# ── Enums ────────────────────────────────────────────────────────────────────


class IdentityStatus(Enum):
    """The effective status of an agent's identity record at index time
    (vector_identity.effective with lo = hi = the index time), or MISSING
    when the record or one of the ancestors that `effective` reads is not
    at the identity address."""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    RETIRED = "retired"
    MISSING = "missing"


# ── Enums ────────────────────────────────────────────────────────────────────


class ReputationTier(Enum):
    """Reputation tier, matching on-chain scoring.ak thresholds."""
    Unverified = 0
    Novice = 1
    Established = 2
    Trusted = 3
    Elite = 4


class RepChallengeState(Enum):
    """Challenge lifecycle state."""
    Open = 0
    Escalated = 1
    Resolved = 2


class RepChallengeOutcome(Enum):
    """Challenge resolution outcome."""
    CapabilityVerified = 0
    CapabilityFalsified = 1
    Inconclusive = 2


class HistoryBonusSource(Enum):
    """Source of a history bonus, matching on-chain constructor indices."""
    ChallengeWon = 0
    AuditClaimWon = 1
    JurorDuty = 2
    ProposalAdopted = 3
    CritiqueIncorporated = 4
    EscrowCompleted = 5
    UsefulWorkVerified = 6
    SecurityReportValidated = 7
    GenesisBonus = 8


# ── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class StakeInfo:
    """Off-chain representation of a StakeDatum UTXO."""
    agent_did: str
    owner: str
    stake_amount: int
    staked_capabilities: List[str]
    last_updated: int  # POSIX ms
    history_points: int
    utxo_ref: Optional[str] = None
    token_name: Optional[str] = None


@dataclass
class EndorsementInfo:
    """Off-chain representation of an EndorsementDatum UTXO."""
    endorser_did: str
    target_did: str
    stake_amount: int
    endorsed_capabilities: List[str]
    created_at: int  # POSIX ms
    utxo_ref: Optional[str] = None
    token_name: Optional[str] = None


@dataclass
class ChallengeInfo:
    """Off-chain representation of a ReputationChallengeDatum UTXO."""
    challenger_did: str
    target_did: str
    challenged_capability: str
    stake_amount: int
    evidence_hash: str
    evidence_uri: str
    created_at: int  # POSIX ms
    state: RepChallengeState = RepChallengeState.Open
    outcome: Optional[RepChallengeOutcome] = None
    counter_evidence_hash: str = ""
    counter_evidence_uri: str = ""
    response_submitted_at: int = 0
    utxo_ref: Optional[str] = None
    token_name: Optional[str] = None


@dataclass
class HistoryBonusInfo:
    """Off-chain representation of a HistoryBonusDatum UTXO."""
    agent_did: str
    source: HistoryBonusSource
    bonus_points: int
    source_ref: str  # "tx_hash#ix"
    created_at: int  # POSIX ms
    utxo_ref: Optional[str] = None


@dataclass
class ReputationScore:
    """Computed reputation score for an agent.

    The amounts do not depend on identity_status. The tier is Unverified
    when identity_status is not ACTIVE (scoring.compute_reputation_score).
    """
    agent_did: str
    self_stake: int       # DFM
    endorsement_total: int  # DFM (capped)
    challenge_total: int    # DFM (active challenges)
    history_bonus: int      # DFM
    decay: int              # DFM
    net_score: int          # DFM = stake + endorse - challenge + bonus - decay
    tier: ReputationTier = ReputationTier.Unverified
    identity_status: IdentityStatus = IdentityStatus.MISSING


@dataclass
class ProtocolParamsInfo:
    """Off-chain representation of the ProtocolParams datum (22 fields).

    The defaults are the values that the deploy scripts write
    (scripts/deploy_common.py). Durations are POSIX milliseconds and
    activity_window counts epochs. genesis_minting_window is an absolute
    POSIX time in milliseconds (deployment time + 28 days); its default 0
    means "not known". Use `from_datum` to read the values of a deployment
    from its params record.
    """
    min_self_stake: int = 10_000_000        # 10 AP3X
    min_endorsement: int = 5_000_000        # 5 AP3X
    min_challenge_stake: int = 25_000_000   # 25 AP3X
    stake_cooldown: int = 86_400_000        # ms (24 h)
    endorsement_cooldown: int = 172_800_000  # ms (48 h)
    decay_rate: int = 100                   # basis points (1%)
    activity_window: int = 180              # epochs
    decay_collector_fee: int = 500          # basis points (5%)
    history_multiplier: int = 1_000         # basis points (10%)
    max_endorsement_multiplier: int = 3
    slash_rate_endorser: int = 5_000        # basis points (50%)
    protocol_fee_rate: int = 500            # basis points (5%)
    challenge_response_deadline: int = 43_200_000  # ms (12 h)
    min_agent_age: int = 86_400_000         # ms (24 h)
    escalation_window: int = 21_600_000     # ms (6 h)
    default_judgment_fee: int = 100         # basis points (1%)
    genesis_agent_cap: int = 50
    genesis_bonus_amount: int = 100_000_000  # 100 AP3X
    genesis_minting_window: int = 0         # POSIX ms (absolute time)
    genesis_protection_period: int = 5_184_000_000  # ms (60 days)
    valid_capabilities: List[str] = field(default_factory=lambda: [
        "code_review", "testing", "deployment", "documentation",
        "security_audit", "architecture", "data_analysis", "ml_training",
    ])
    epoch_length: int = 14_400_000          # ms (4 h)

    @classmethod
    def from_datum(cls, datum: Any) -> "ProtocolParamsInfo":
        """The values of a ProtocolParams datum (the inline datum of the
        params record: RawCBOR, PlutusData or CBOR bytes)."""
        p = decode_datum(ProtocolParams, datum)
        return cls(
            min_self_stake=p.min_self_stake,
            min_endorsement=p.min_endorsement,
            min_challenge_stake=p.min_challenge_stake,
            stake_cooldown=p.stake_cooldown,
            endorsement_cooldown=p.endorsement_cooldown,
            decay_rate=p.decay_rate,
            activity_window=p.activity_window,
            decay_collector_fee=p.decay_collector_fee,
            history_multiplier=p.history_multiplier,
            max_endorsement_multiplier=p.max_endorsement_multiplier,
            slash_rate_endorser=p.slash_rate_endorser,
            protocol_fee_rate=p.protocol_fee_rate,
            challenge_response_deadline=p.challenge_response_deadline,
            min_agent_age=p.min_agent_age,
            escalation_window=p.escalation_window,
            default_judgment_fee=p.default_judgment_fee,
            genesis_agent_cap=p.genesis_agent_cap,
            genesis_bonus_amount=p.genesis_bonus_amount,
            genesis_minting_window=p.genesis_minting_window,
            genesis_protection_period=p.genesis_protection_period,
            valid_capabilities=[
                bytes(c).decode("utf-8", errors="replace") for c in p.valid_capabilities
            ],
            epoch_length=p.epoch_length,
        )
