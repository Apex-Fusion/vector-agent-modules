"""
Reputation score computation for Module 3: Reputation Staking.

Pure computation — no chain interaction. Mirrors the on-chain logic in:
  reputation_staking/lib/reputation_staking/decay.ak
  reputation_staking/lib/reputation_staking/scoring.ak
  reputation_staking/validators/reputation.ak (ClaimDecayRefund)

Formula:
  R(agent) = self_stake + endorsements - active_challenges + history_bonus - decay

Times are POSIX milliseconds. An epoch is `epoch_length` milliseconds; the
epoch number of a time is `posix_ms // epoch_length` (epoch_of). Decay counts
the epochs that passed since the stake's `last_updated`, as ClaimDecayRefund
does on-chain: `(now_ms - last_updated) // epoch_length - activity_window`.

Identity rule (decision, Module-3 indexer): an agent whose identity record
does not have effective status Active at the index time gets the tier
Unverified. Its amounts stay as they are. The indexer leaves such an agent
out of the leaderboard unless the caller asks for all agents.

Stake rule: no action burns a stake token. A stake that decays below
min_self_stake continues with stake_amount 0, so its UTxO stays. Such a stake
is a stake below the minimum (StakeStatus.BELOW_MINIMUM), not a missing stake:
it keeps its owner, capabilities and stake_id, and its amount counts as it is
(0). The endorsement cap (self_stake × max_endorsement_multiplier) is then 0.
"""

from __future__ import annotations

from typing import List, Optional

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
    IdentityStatus,
    ProtocolParamsInfo,
    RepChallengeState,
    ReputationScore,
    ReputationTier,
    StakeInfo,
    StakeStatus,
)


def epoch_of(posix_ms: int, epoch_length: int) -> int:
    """The epoch number of a POSIX time in milliseconds."""
    if epoch_length <= 0:
        raise ValueError("epoch_length must be positive")
    return posix_ms // epoch_length


def inactive_epochs(last_updated_ms: int, now_ms: int, params: ProtocolParamsInfo) -> int:
    """Epochs of inactivity after the activity window, as ClaimDecayRefund
    computes them on-chain: (now - last_updated) // epoch_length -
    activity_window. Zero or less means no decay."""
    if params.epoch_length <= 0:
        raise ValueError("epoch_length must be positive")
    elapsed_epochs = (now_ms - last_updated_ms) // params.epoch_length
    return elapsed_epochs - params.activity_window


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


def stake_status(stake: Optional[StakeInfo], params: ProtocolParamsInfo) -> StakeStatus:
    """MISSING without a stake UTxO, BELOW_MINIMUM for a stake with
    stake_amount < min_self_stake (also a fully decayed stake with 0), else
    STAKED."""
    if stake is None:
        return StakeStatus.MISSING
    if stake.stake_amount < params.min_self_stake:
        return StakeStatus.BELOW_MINIMUM
    return StakeStatus.STAKED


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
    stake: Optional[StakeInfo],
    endorsements: List[EndorsementInfo],
    challenges: List[ChallengeInfo],
    history_bonuses: List[HistoryBonusInfo],
    params: ProtocolParamsInfo,
    now_ms: int,
    *,
    identity_status: IdentityStatus,
    agent_did: Optional[str] = None,
) -> ReputationScore:
    """Compute the full reputation score for an agent.

    Formula:
        R = self_stake
          + min(endorsement_total, self_stake * max_endorsement_multiplier)
          - active_challenge_total
          + history_bonus
          - decay

    Args:
        stake: The agent's StakeInfo, or None when the agent has no stake
            UTxO. A stake with stake_amount below min_self_stake (also 0
            after a full decay) is a stake (stake_status BELOW_MINIMUM).
        endorsements: All endorsements targeting this agent.
        challenges: All challenges against this agent.
        history_bonuses: All history bonus UTXOs for this agent.
        params: Protocol parameters.
        now_ms: The time of the computation, POSIX milliseconds (for decay).
        identity_status: The effective status of the agent's identity record
            at now_ms. When it is not ACTIVE, the tier is Unverified; the
            amounts do not change.
        agent_did: The agent identifier; needed when `stake` is None.

    Returns:
        Computed ReputationScore.
    """
    if stake is None and agent_did is None:
        raise ValueError("compute_reputation_score needs agent_did when the agent has no stake")
    self_stake = stake.stake_amount if stake is not None else 0

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

    # Decay after the activity window, counted from the stake's last update
    decay = 0
    if stake is not None:
        decay = compute_decay(
            self_stake,
            inactive_epochs(stake.last_updated, now_ms, params),
            params.decay_rate,
        )

    net_score = self_stake + endorsement_total - challenge_total + history_bonus - decay
    net_score = max(net_score, 0)

    tier = get_tier(net_score)
    if identity_status != IdentityStatus.ACTIVE:
        tier = ReputationTier.Unverified

    return ReputationScore(
        agent_did=agent_did if agent_did is not None else stake.agent_did,
        self_stake=self_stake,
        endorsement_total=endorsement_total,
        challenge_total=challenge_total,
        history_bonus=history_bonus,
        decay=decay,
        net_score=net_score,
        tier=tier,
        identity_status=identity_status,
        stake_status=stake_status(stake, params),
    )
