"""Tests for cross-module bonus integration."""

import pytest

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.cross_module import (
    CROSS_MODULE_BONUSES,
    compute_bonus_value,
    get_module_for_bonus,
    summarize_cross_module_bonuses,
)
from reputation_staking.models import HistoryBonusInfo, HistoryBonusSource


class TestCrossModuleBonusConfig:
    def test_all_sources_mapped(self):
        for source in HistoryBonusSource:
            assert source in CROSS_MODULE_BONUSES

    def test_challenge_won_is_percentage(self):
        config = CROSS_MODULE_BONUSES[HistoryBonusSource.ChallengeWon]
        assert config.is_percentage_based
        assert config.module_number == 3

    def test_juror_duty_fixed(self):
        config = CROSS_MODULE_BONUSES[HistoryBonusSource.JurorDuty]
        assert not config.is_percentage_based
        assert config.default_bonus_ap3x == 2
        assert config.module_number == 1

    def test_proposal_adopted(self):
        config = CROSS_MODULE_BONUSES[HistoryBonusSource.ProposalAdopted]
        assert config.module_number == 6
        assert config.default_bonus_ap3x == 10

    def test_genesis_bonus(self):
        config = CROSS_MODULE_BONUSES[HistoryBonusSource.GenesisBonus]
        assert config.default_bonus_ap3x == 100


class TestComputeBonusValue:
    def test_fixed_bonus(self):
        value = compute_bonus_value(HistoryBonusSource.JurorDuty)
        assert value == 2 * DFM_PER_AP3X

    def test_percentage_bonus(self):
        # 10% of 100 AP3X stake = 10 AP3X
        value = compute_bonus_value(
            HistoryBonusSource.ChallengeWon,
            underlying_stake_dfm=100 * DFM_PER_AP3X,
            history_multiplier_bps=1000,
        )
        assert value == 10 * DFM_PER_AP3X

    def test_genesis_bonus(self):
        value = compute_bonus_value(HistoryBonusSource.GenesisBonus)
        assert value == 100 * DFM_PER_AP3X


class TestGetModuleForBonus:
    def test_known_source(self):
        config = get_module_for_bonus(HistoryBonusSource.UsefulWorkVerified)
        assert config is not None
        assert config.module_number == 9

    def test_module1_sources(self):
        for source in [HistoryBonusSource.AuditClaimWon, HistoryBonusSource.JurorDuty]:
            config = get_module_for_bonus(source)
            assert config.module_number == 1


class TestSummarizeBonuses:
    def test_empty_list(self):
        result = summarize_cross_module_bonuses([])
        assert result == {}

    def test_single_bonus(self):
        bonuses = [
            HistoryBonusInfo(
                agent_did="aaa",
                source=HistoryBonusSource.JurorDuty,
                bonus_points=2_000_000,
                source_ref="tx#0",
                created_at=1000,
            ),
        ]
        result = summarize_cross_module_bonuses(bonuses)
        key = "Module 1: Adversarial Auditing"
        assert key in result
        assert result[key]["count"] == 1
        assert result[key]["total_bonus_points"] == 2_000_000

    def test_multiple_modules(self):
        bonuses = [
            HistoryBonusInfo(
                agent_did="aaa",
                source=HistoryBonusSource.JurorDuty,
                bonus_points=2_000_000,
                source_ref="tx1#0",
                created_at=1000,
            ),
            HistoryBonusInfo(
                agent_did="aaa",
                source=HistoryBonusSource.ProposalAdopted,
                bonus_points=10_000_000,
                source_ref="tx2#0",
                created_at=2000,
            ),
        ]
        result = summarize_cross_module_bonuses(bonuses)
        assert len(result) == 2
        assert "Module 1: Adversarial Auditing" in result
        assert "Module 6: Governance Suggestion Engine" in result
