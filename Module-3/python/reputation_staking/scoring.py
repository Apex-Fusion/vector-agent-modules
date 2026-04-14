"""
Reputation score computation for Module 3: Reputation Staking.

Pure computation — no chain interaction. Mirrors the on-chain logic in:
  reputation_staking/lib/reputation_staking/decay.ak
  reputation_staking/lib/reputation_staking/scoring.ak

Formula:
  R(agent) = self_stake + endorsements - active_challenges + history_bonus - decay
"""

from __future__ import annotations

from typing import List

from reputation_staking.constants import (
    DFM_PER_AP3X,
    TIER_ELITE_AP3X,
    TIER_ESTABLISHED_AP3X,
    TIER_NOVICE_AP3X,
    TIER_TRUSTED_AP3X,
)
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    ProtocolParamsInfo,
    RepChallengeState,
    ReputationScore,
    ReputationTier,
    StakeInfo,
)
from reputation_staking.utils import posix_ms_to_slot


def compute_decay(stake_amount: int, inactive_epochs: int, decay_rate: int) -> int:
    """Calculate the decay amount for an inactive agent.

    Mirrors on-chain decay.ak:calculate_decay exactly.

    Args:
        stake_amount: Total stake in DFM.
        inactive_epochs: Number of inactive epochs (after activity_window grace).
        decay_rate: Decay rate in basis points (100 = 1% per epoch).

    Returns:
        Decay amount in DFM, capped at stake_amount.
    """
    if inactive_epochs <= 0:
        return 0
    raw_decay = stake_amount * decay_rate * inactive_epochs // 10_000
    return min(raw_decay, stake_amount)


def compute_collector_fee(decay_amount: int, collector_fee_rate: int) -> int:
    """Calculate the collector fee from a decay amount.

    Mirrors on-chain decay.ak:calculate_collector_fee.
    """
    return decay_amount * collector_fee_rate // 10_000


def get_tier(score_dfm: int) -> ReputationTier:
    """Compute the reputation tier from a score in DFM.

    Mirrors on-chain scoring.ak:get_tier, which uses whole AP3X units.

    Thresholds:
        0          -> Unverified
        1-99 AP3X  -> Novice
        100-499    -> Established
        500-1999   -> Trusted
        2000+      -> Elite
    """
    score_ap3x = score_dfm // DFM_PER_AP3X
    if score_ap3x >= TIER_ELITE_AP3X:
        return ReputationTier.Elite
    elif score_ap3x >= TIER_TRUSTED_AP3X:
        return ReputationTier.Trusted
    elif score_ap3x >= TIER_ESTABLISHED_AP3X:
        return ReputationTier.Established
    elif score_ap3x >= TIER_NOVICE_AP3X:
        return ReputationTier.Novice
    else:
        return ReputationTier.Unverified


def compute_reputation_score(
    stake: StakeInfo,
    endorsements: List[EndorsementInfo],
    challenges: List[ChallengeInfo],
    history_bonuses: List[HistoryBonusInfo],
    params: ProtocolParamsInfo,
    current_slot: int,
) -> ReputationScore:
    """Compute the full reputation score for an agent.

    Formula:
        R = self_stake
          + min(endorsement_total, self_stake * max_endorsement_multiplier)
          - active_challenge_total
          + history_bonus
          - decay

    Args:
        stake: The agent's StakeInfo.
        endorsements: All endorsements targeting this agent.
        challenges: All challenges against this agent.
        history_bonuses: All history bonus UTXOs for this agent.
        params: Protocol parameters.
        current_slot: Current chain slot (for decay calculation).

    Returns:
        Computed ReputationScore.
    """
    self_stake = stake.stake_amount

    # Sum endorsements, capped at max_endorsement_multiplier * self_stake
    raw_endorsement_total = sum(e.stake_amount for e in endorsements)
    endorsement_cap = self_stake * params.max_endorsement_multiplier
    endorsement_total = min(raw_endorsement_total, endorsement_cap)

    # Sum active (Open) challenges only
    challenge_total = sum(
        c.stake_amount for c in challenges if c.state == RepChallengeState.Open
    )

    # Sum history bonus points
    history_bonus = sum(b.bonus_points for b in history_bonuses)

    # Compute decay based on inactivity
    last_active_slot = posix_ms_to_slot(stake.last_updated)
    current_epoch = current_slot // params.epoch_length
    last_active_epoch = last_active_slot // params.epoch_length
    epochs_since_active = current_epoch - last_active_epoch

    if epochs_since_active > params.activity_window:
        inactive_epochs = epochs_since_active - params.activity_window
        decay = compute_decay(self_stake, inactive_epochs, params.decay_rate)
    else:
        decay = 0

    net_score = self_stake + endorsement_total - challenge_total + history_bonus - decay
    net_score = max(net_score, 0)

    return ReputationScore(
        agent_did=stake.agent_did,
        self_stake=self_stake,
        endorsement_total=endorsement_total,
        challenge_total=challenge_total,
        history_bonus=history_bonus,
        decay=decay,
        net_score=net_score,
        tier=get_tier(net_score),
    )
