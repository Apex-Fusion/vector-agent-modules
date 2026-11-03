"""Tests for reputation score computation and tier assignment."""

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    HistoryBonusSource,
    IdentityStatus,
    ProtocolParamsInfo,
    RepChallengeState,
    ReputationTier,
    StakeInfo,
)
from reputation_staking.scoring import (
    compute_decay,
    compute_collector_fee,
    compute_reputation_score,
    epoch_of,
    get_tier,
    inactive_epochs,
)

# A POSIX ms time; the default epoch_length is 14_400_000 ms (4 h).
T0 = 1_762_000_000_000
EPOCH = 14_400_000
ACTIVE = IdentityStatus.ACTIVE


class TestGetTier:
    def test_unverified(self):
        assert get_tier(0) == ReputationTier.Unverified

    def test_novice_lower_bound(self):
        assert get_tier(1 * DFM_PER_AP3X) == ReputationTier.Novice

    def test_novice_upper_bound(self):
        assert get_tier(99 * DFM_PER_AP3X) == ReputationTier.Novice

    def test_established_lower_bound(self):
        assert get_tier(100 * DFM_PER_AP3X) == ReputationTier.Established

    def test_established_upper_bound(self):
        assert get_tier(499 * DFM_PER_AP3X) == ReputationTier.Established

    def test_trusted_lower_bound(self):
        assert get_tier(500 * DFM_PER_AP3X) == ReputationTier.Trusted

    def test_trusted_upper_bound(self):
        assert get_tier(1999 * DFM_PER_AP3X) == ReputationTier.Trusted

    def test_elite_lower_bound(self):
        assert get_tier(2000 * DFM_PER_AP3X) == ReputationTier.Elite

    def test_elite_high_value(self):
        assert get_tier(50_000 * DFM_PER_AP3X) == ReputationTier.Elite

    def test_just_below_novice(self):
        """Score of 0.5 AP3X (500_000 DFM) should be Unverified (floor division)."""
        assert get_tier(500_000) == ReputationTier.Unverified


class TestComputeDecay:
    def test_no_inactive_epochs(self):
        assert compute_decay(100_000_000, 0, 100) == 0

    def test_negative_inactive_epochs(self):
        assert compute_decay(100_000_000, -5, 100) == 0

    def test_one_epoch_decay(self):
        # 100 AP3X * 100 basis points * 1 epoch / 10000 = 1 AP3X
        assert compute_decay(100_000_000, 1, 100) == 1_000_000

    def test_multiple_epoch_decay(self):
        # 100 AP3X * 100 bp * 10 epochs / 10000 = 10 AP3X
        assert compute_decay(100_000_000, 10, 100) == 10_000_000

    def test_decay_capped_at_stake(self):
        # 10 AP3X * 100 bp * 200 epochs / 10000 = 200 AP3X (> stake)
        result = compute_decay(10_000_000, 200, 100)
        assert result == 10_000_000  # capped at stake

    def test_zero_stake(self):
        assert compute_decay(0, 10, 100) == 0


class TestComputeCollectorFee:
    def test_basic_fee(self):
        # 10 AP3X decay * 500 bp / 10000 = 0.5 AP3X
        assert compute_collector_fee(10_000_000, 500) == 500_000

    def test_zero_decay(self):
        assert compute_collector_fee(0, 500) == 0


class TestComputeReputationScore:
    def _make_stake(self, amount: int, last_updated_ms: int = T0) -> StakeInfo:
        return StakeInfo(
            agent_did="aa" * 32,
            owner="bb" * 28,
            stake_amount=amount,
            staked_capabilities=["code_review"],
            last_updated=last_updated_ms,
            history_points=0,
        )

    def _make_endorsement(self, amount: int) -> EndorsementInfo:
        return EndorsementInfo(
            endorser_did="cc" * 32,
            target_did="aa" * 32,
            stake_amount=amount,
            endorsed_capabilities=["code_review"],
            created_at=0,
        )

    def _make_challenge(
        self, amount: int, state: RepChallengeState = RepChallengeState.Open
    ) -> ChallengeInfo:
        return ChallengeInfo(
            challenger_did="dd" * 32,
            target_did="aa" * 32,
            challenged_capability="code_review",
            stake_amount=amount,
            evidence_hash="",
            evidence_uri="",
            created_at=0,
            state=state,
        )

    def _make_bonus(self, points: int) -> HistoryBonusInfo:
        return HistoryBonusInfo(
            agent_did="aa" * 32,
            source=HistoryBonusSource.ChallengeWon,
            bonus_points=points,
            source_ref="ab" * 32 + "#0",
            created_at=0,
        )

    def _score(self, params, stake, endorsements=(), challenges=(), bonuses=(),
               now_ms=T0, identity_status=ACTIVE):
        return compute_reputation_score(
            stake, list(endorsements), list(challenges), list(bonuses), params,
            now_ms, identity_status=identity_status,
        )

    def test_stake_only(self, default_params):
        score = self._score(default_params, self._make_stake(10_000_000))
        assert score.net_score == 10_000_000
        assert score.tier == ReputationTier.Novice
        assert score.identity_status == ACTIVE

    def test_with_endorsements(self, default_params):
        score = self._score(
            default_params, self._make_stake(10_000_000),
            endorsements=[self._make_endorsement(5_000_000)],
        )
        assert score.endorsement_total == 5_000_000
        assert score.net_score == 15_000_000

    def test_endorsement_cap(self, default_params):
        """Endorsements are capped at max_endorsement_multiplier * self_stake."""
        # 50 AP3X in endorsements, but cap is 3 * 10 = 30 AP3X
        score = self._score(
            default_params, self._make_stake(10_000_000),
            endorsements=[self._make_endorsement(50_000_000)],
        )
        assert score.endorsement_total == 30_000_000  # capped at 3x stake
        assert score.net_score == 40_000_000

    def test_with_active_challenge(self, default_params):
        score = self._score(
            default_params, self._make_stake(100_000_000),
            challenges=[self._make_challenge(25_000_000)],
        )
        assert score.challenge_total == 25_000_000
        assert score.net_score == 75_000_000

    def test_resolved_challenge_not_counted(self, default_params):
        """Resolved challenges should NOT reduce the score."""
        score = self._score(
            default_params, self._make_stake(100_000_000),
            challenges=[self._make_challenge(25_000_000, RepChallengeState.Resolved)],
        )
        assert score.challenge_total == 0
        assert score.net_score == 100_000_000

    def test_with_history_bonus(self, default_params):
        score = self._score(
            default_params, self._make_stake(10_000_000),
            bonuses=[self._make_bonus(5_000_000)],
        )
        assert score.history_bonus == 5_000_000
        assert score.net_score == 15_000_000

    def test_with_decay(self, default_params):
        """Agent inactive for 190 epochs (10 past the 180 activity_window)."""
        # epoch_length = 14_400_000 ms, activity_window = 180 epochs
        now_ms = T0 + (180 + 10) * EPOCH
        score = self._score(default_params, self._make_stake(100_000_000), now_ms=now_ms)
        # decay = 100_000_000 * 100 * 10 / 10000 = 10_000_000
        assert score.decay == 10_000_000
        assert score.net_score == 90_000_000

    def test_no_decay_within_activity_window(self, default_params):
        # One millisecond before the first inactive epoch after the window
        now_ms = T0 + (180 + 1) * EPOCH - 1
        score = self._score(default_params, self._make_stake(100_000_000), now_ms=now_ms)
        assert score.decay == 0
        assert score.net_score == 100_000_000

    def test_decay_counts_elapsed_epochs_like_claim_decay_refund(self, default_params):
        """Epochs count from last_updated, not from epoch boundaries: a stake
        updated 1 ms before an epoch boundary has no inactive epoch until
        181 full epochs pass (reputation.ak ClaimDecayRefund)."""
        last = epoch_of(T0, EPOCH) * EPOCH + EPOCH - 1
        stake = self._make_stake(100_000_000, last)
        assert inactive_epochs(last, last + 181 * EPOCH - 1, default_params) == 0
        assert inactive_epochs(last, last + 181 * EPOCH, default_params) == 1
        score = self._score(default_params, stake, now_ms=last + 181 * EPOCH - 1)
        assert score.decay == 0

    def test_epoch_of(self):
        assert epoch_of(0, EPOCH) == 0
        assert epoch_of(EPOCH - 1, EPOCH) == 0
        assert epoch_of(EPOCH, EPOCH) == 1
        assert epoch_of(T0, EPOCH) == T0 // EPOCH

    def test_score_floor_at_zero(self, default_params):
        """Score should never go negative."""
        score = self._score(
            default_params, self._make_stake(10_000_000),
            challenges=[self._make_challenge(50_000_000)],
        )
        assert score.net_score == 0

    def test_not_active_agent_is_unverified_and_keeps_amounts(self, default_params):
        """Identity rule: an agent that is not Active gets the tier
        Unverified; the amounts do not change."""
        stake = self._make_stake(2_500 * DFM_PER_AP3X)
        active = self._score(default_params, stake)
        assert active.tier == ReputationTier.Elite
        for status in (IdentityStatus.SUSPENDED, IdentityStatus.REVOKED,
                       IdentityStatus.RETIRED, IdentityStatus.MISSING):
            score = self._score(default_params, stake, identity_status=status)
            assert score.tier == ReputationTier.Unverified
            assert score.identity_status == status
            assert score.net_score == active.net_score
            assert score.self_stake == active.self_stake


class TestProtocolParamsDefaults:
    def test_defaults_are_milliseconds(self):
        """The defaults are the deploy values (W1-D B3), in ms."""
        p = ProtocolParamsInfo()
        assert p.stake_cooldown == 86_400_000
        assert p.endorsement_cooldown == 172_800_000
        assert p.challenge_response_deadline == 43_200_000
        assert p.min_agent_age == 86_400_000
        assert p.escalation_window == 21_600_000
        assert p.genesis_protection_period == 5_184_000_000
        assert p.epoch_length == 14_400_000
        assert p.activity_window == 180
