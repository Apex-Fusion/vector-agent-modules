"""Tests for MCP server tools (read-only tools using mock storage)."""

import json
import pytest

from reputation_staking.mcp_tools import (
    TOOL_DEFINITIONS,
    reputation_browse,
    reputation_my_status,
)


class MockStorage:
    """In-memory mock of IndexerStorage for testing read-only tools."""

    def __init__(self):
        self._scores = {}
        self._stakes = {}
        self._endorsements_for = {}
        self._endorsements_by = {}
        self._challenges = {}
        self._bonuses = {}

    def add_agent(
        self, agent_did, net_score, tier, self_stake=0,
        endorsement_total=0, challenge_total=0, history_bonus=0, decay=0,
        capabilities=None, endorsements_received=0, endorsements_given=0,
    ):
        self._scores[agent_did] = {
            "agent_did": agent_did,
            "net_score": net_score,
            "tier": tier,
            "self_stake": self_stake,
            "endorsement_total": endorsement_total,
            "challenge_total": challenge_total,
            "history_bonus": history_bonus,
            "decay": decay,
            "last_updated_slot": 1000,
        }
        caps = capabilities or []
        self._stakes[agent_did] = [
            {
                "utxo_ref": f"fake#{agent_did[:8]}",
                "agent_did": agent_did,
                "owner_credential": "owner",
                "stake_amount": self_stake,
                "capabilities": json.dumps(caps),
                "last_updated": 1000,
                "history_points": 0,
            }
        ]
        self._endorsements_for[agent_did] = [{}] * endorsements_received
        self._endorsements_by[agent_did] = [{}] * endorsements_given
        self._challenges[agent_did] = []
        self._bonuses[agent_did] = []

    def get_all_scores(self):
        return sorted(self._scores.values(), key=lambda s: s["net_score"], reverse=True)

    def get_score(self, agent_did):
        return self._scores.get(agent_did)

    def get_stakes_for_agent(self, agent_did):
        return self._stakes.get(agent_did, [])

    def get_endorsements_for_agent(self, agent_did):
        return self._endorsements_for.get(agent_did, [])

    def get_endorsements_by_agent(self, agent_did):
        return self._endorsements_by.get(agent_did, [])

    def get_challenges_for_agent(self, agent_did):
        return self._challenges.get(agent_did, [])

    def get_bonuses_for_agent(self, agent_did):
        return self._bonuses.get(agent_did, [])


@pytest.fixture
def storage():
    s = MockStorage()
    s.add_agent(
        "aaa", net_score=500_000_000, tier="Trusted",
        self_stake=300_000_000, endorsement_total=200_000_000,
        capabilities=["code_review", "testing"],
    )
    s.add_agent(
        "bbb", net_score=50_000_000, tier="Novice",
        self_stake=50_000_000,
        capabilities=["testing"],
    )
    s.add_agent(
        "ccc", net_score=2_500_000_000, tier="Elite",
        self_stake=2_000_000_000, endorsement_total=500_000_000,
        capabilities=["code_review", "security_audit"],
    )
    return s


class TestToolDefinitions:
    def test_five_tools_defined(self):
        assert len(TOOL_DEFINITIONS) == 5

    def test_tool_names(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert names == {
            "reputation_stake", "reputation_endorse", "reputation_challenge",
            "reputation_browse", "reputation_my_status",
        }

    def test_all_have_schemas(self):
        for t in TOOL_DEFINITIONS:
            assert "input_schema" in t
            assert "description" in t


class TestReputationBrowse:
    def test_returns_all_sorted(self, storage):
        result = reputation_browse(storage)
        assert result.success
        assert len(result.data) == 3
        assert result.data[0]["agent_did"] == "ccc"  # highest score
        assert result.data[0]["tier"] == "Elite"

    def test_filter_by_capability(self, storage):
        result = reputation_browse(storage, capability="security_audit")
        assert result.success
        assert len(result.data) == 1
        assert result.data[0]["agent_did"] == "ccc"

    def test_filter_by_tier(self, storage):
        result = reputation_browse(storage, min_tier="trusted")
        assert result.success
        assert len(result.data) == 2  # Trusted + Elite
        tiers = {a["tier"] for a in result.data}
        assert "Novice" not in tiers

    def test_limit(self, storage):
        result = reputation_browse(storage, limit=1)
        assert result.success
        assert len(result.data) == 1

    def test_sort_by_endorsements(self, storage):
        result = reputation_browse(storage, sort_by="endorsements")
        assert result.success
        assert result.data[0]["agent_did"] == "ccc"  # 500 AP3X endorsements


class TestReputationMyStatus:
    def test_found_agent(self, storage):
        result = reputation_my_status(storage, "aaa")
        assert result.success
        assert result.data["tier"] == "Trusted"
        assert result.data["score_ap3x"] == 500
        assert "code_review" in result.data["capabilities"]

    def test_not_found(self, storage):
        result = reputation_my_status(storage, "nonexistent")
        assert not result.success
        assert "not found" in result.error

    def test_score_breakdown(self, storage):
        result = reputation_my_status(storage, "aaa")
        assert result.success
        bd = result.data["breakdown"]
        assert bd["self_stake"] == 300
        assert bd["endorsements"] == 200
