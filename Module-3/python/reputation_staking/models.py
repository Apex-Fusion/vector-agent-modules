"""
Off-chain data models for Module 3: Reputation Staking.

These mirror the on-chain Aiken types defined in:
  reputation_staking/lib/reputation_staking/types.ak

Used by the indexer, scoring engine, and client API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


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
    """Computed reputation score for an agent."""
    agent_did: str
    self_stake: int       # DFM
    endorsement_total: int  # DFM (capped)
    challenge_total: int    # DFM (active challenges)
    history_bonus: int      # DFM
    decay: int              # DFM
    net_score: int          # DFM = stake + endorse - challenge + bonus - decay
    tier: ReputationTier = ReputationTier.Unverified


@dataclass
class ProtocolParamsInfo:
    """Off-chain representation of the ProtocolParams datum (22 fields)."""
    min_self_stake: int = 10_000_000        # 10 AP3X
    min_endorsement: int = 5_000_000        # 5 AP3X
    min_challenge_stake: int = 25_000_000   # 25 AP3X
    stake_cooldown: int = 21_600            # slots (~24h)
    endorsement_cooldown: int = 43_200      # slots (~48h)
    decay_rate: int = 100                   # basis points (1%)
    activity_window: int = 180              # epochs
    decay_collector_fee: int = 500          # basis points (5%)
    history_multiplier: int = 1_000         # basis points (10%)
    max_endorsement_multiplier: int = 3
    slash_rate_endorser: int = 5_000        # basis points (50%)
    protocol_fee_rate: int = 500            # basis points (5%)
    challenge_response_deadline: int = 10_800  # slots (~12h)
    min_agent_age: int = 21_600             # slots (~24h)
    escalation_window: int = 5_400          # slots (~6h)
    default_judgment_fee: int = 100         # basis points (1%)
    genesis_agent_cap: int = 50
    genesis_bonus_amount: int = 100_000_000  # 100 AP3X
    genesis_minting_window: int = 604_800    # slots (~28d)
    genesis_protection_period: int = 129_600  # slots (~60d)
    valid_capabilities: List[str] = field(default_factory=lambda: [
        "code_review", "testing", "deployment", "documentation",
        "security_audit", "architecture", "data_analysis", "ml_training",
    ])
    epoch_length: int = 900                 # slots (~4h)
