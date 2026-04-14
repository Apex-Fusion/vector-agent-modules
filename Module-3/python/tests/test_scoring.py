"""Tests for reputation score computation and tier assignment."""

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    HistoryBonusSource,
    ProtocolParamsInfo,
    RepChallengeState,
    ReputationTier,
    StakeInfo,
)
from reputation_staking.scoring import (
    compute_decay,
    compute_collector_fee,
    compute_reputation_score,
    get_tier,
)
from reputation_staking.utils import slot_to_posix_ms


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
    def _make_stake(self, amount: int, slot: int) -> StakeInfo:
        return StakeInfo(
            agent_did="aa" * 32,
            owner="bb" * 28,
            stake_amount=amount,
            staked_capabilities=["code_review"],
            last_updated=slot_to_posix_ms(slot),
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

    def test_stake_only(self, default_params):
        stake = self._make_stake(10_000_000, 1000)
        score = compute_reputation_score(
            stake, [], [], [], default_params, current_slot=1000
        )
        assert score.net_score == 10_000_000
        assert score.tier == ReputationTier.Novice

    def test_with_endorsements(self, default_params):
        stake = self._make_stake(10_000_000, 1000)
        endorsements = [self._make_endorsement(5_000_000)]
        score = compute_reputation_score(
            stake, endorsements, [], [], default_params, current_slot=1000
        )
        assert score.endorsement_total == 5_000_000
        assert score.net_score == 15_000_000

    def test_endorsement_cap(self, default_params):
        """Endorsements are capped at max_endorsement_multiplier * self_stake."""
        stake = self._make_stake(10_000_000, 1000)
        # 50 AP3X in endorsements, but cap is 3 * 10 = 30 AP3X
        endorsements = [self._make_endorsement(50_000_000)]
        score = compute_reputation_score(
            stake, endorsements, [], [], default_params, current_slot=1000
        )
        assert score.endorsement_total == 30_000_000  # capped at 3x stake
        assert score.net_score == 40_000_000

    def test_with_active_challenge(self, default_params):
        stake = self._make_stake(100_000_000, 1000)
        challenges = [self._make_challenge(25_000_000)]
        score = compute_reputation_score(
            stake, [], challenges, [], default_params, current_slot=1000
        )
        assert score.challenge_total == 25_000_000
        assert score.net_score == 75_000_000

    def test_resolved_challenge_not_counted(self, default_params):
        """Resolved challenges should NOT reduce the score."""
        stake = self._make_stake(100_000_000, 1000)
        challenges = [self._make_challenge(25_000_000, RepChallengeState.Resolved)]
        score = compute_reputation_score(
            stake, [], challenges, [], default_params, current_slot=1000
        )
        assert score.challenge_total == 0
        assert score.net_score == 100_000_000

    def test_with_history_bonus(self, default_params):
        stake = self._make_stake(10_000_000, 1000)
        bonuses = [self._make_bonus(5_000_000)]
        score = compute_reputation_score(
            stake, [], [], bonuses, default_params, current_slot=1000
        )
        assert score.history_bonus == 5_000_000
        assert score.net_score == 15_000_000

    def test_with_decay(self, default_params):
        """Agent inactive for 190 epochs (10 past the 180 activity_window)."""
        # epoch_length = 900 slots, activity_window = 180 epochs
        # Stake at slot 0, current at slot (180 + 10) * 900 = 171000
        stake = self._make_stake(100_000_000, 0)  # 100 AP3X staked at slot 0
        current_slot = (180 + 10) * 900  # 10 inactive epochs past grace
        score = compute_reputation_score(
            stake, [], [], [], default_params, current_slot=current_slot
        )
        # decay = 100_000_000 * 100 * 10 / 10000 = 10_000_000
        assert score.decay == 10_000_000
        assert score.net_score == 90_000_000

    def test_no_decay_within_activity_window(self, default_params):
        stake = self._make_stake(100_000_000, 0)
        current_slot = 179 * 900  # Still within 180-epoch window
        score = compute_reputation_score(
            stake, [], [], [], default_params, current_slot=current_slot
        )
        assert score.decay == 0
        assert score.net_score == 100_000_000

    def test_score_floor_at_zero(self, default_params):
        """Score should never go negative."""
        stake = self._make_stake(10_000_000, 0)
        challenges = [self._make_challenge(50_000_000)]
        score = compute_reputation_score(
            stake, [], challenges, [], default_params, current_slot=0
        )
        assert score.net_score == 0
