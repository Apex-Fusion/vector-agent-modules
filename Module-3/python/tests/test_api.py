"""Tests for the indexer REST API endpoints."""

import json
import pytest

from indexer.storage import IndexerStorage


@pytest.fixture
def storage(tmp_path):
    """Create a temporary SQLite database with test data."""
    db_path = str(tmp_path / "test.db")
    s = IndexerStorage(db_path)

    # Add two agents with scores
    s.upsert_score(
        agent_did="aaa111", self_stake=100_000_000, endorsement_total=50_000_000,
        challenge_total=0, history_bonus=2_000_000, decay=0,
        net_score=152_000_000, tier="Established", slot=5000,
    )
    s.upsert_score(
        agent_did="bbb222", self_stake=10_000_000, endorsement_total=0,
        challenge_total=0, history_bonus=0, decay=0,
        net_score=10_000_000, tier="Novice", slot=5000,
    )

    # Add stakes
    s.upsert_stake(
        "tx1#0", "aaa111", "owner_aaa", 100_000_000,
        ["code_review", "testing"], 4000, 0,
    )
    s.upsert_stake(
        "tx2#0", "bbb222", "owner_bbb", 10_000_000,
        ["testing"], 4000, 0,
    )

    # Add an endorsement
    s.upsert_endorsement(
        "tx3#0", "bbb222", "aaa111", 50_000_000,
        ["code_review"], 3000,
    )

    # Add a challenge
    s.upsert_challenge(
        "tx4#0", "bbb222", "aaa111", "testing",
        25_000_000, "Open", None, 4500,
    )

    # Add sybil flags
    s.upsert_sybil_flag("bbb222", "cycle", 0.7, "Part of test ring", "aaa111")

    # Set indexer state
    s.set_state("last_poll_slot", "5000")
    s.set_state("last_poll_time", "1700000000")
    s.set_state("agents_indexed", "2")

    return s


@pytest.fixture
def app(storage, monkeypatch):
    """Create FastAPI test app with mock storage."""
    import indexer.api as api_module
    monkeypatch.setattr(api_module, "_storage", storage)
    return api_module.app


@pytest.fixture
def client(app):
    """Create test client."""
    from fastapi.testclient import TestClient
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["last_poll_slot"] == "5000"
        assert data["agents_indexed"] == "2"


class TestAgentEndpoint:
    def test_get_agent(self, client):
        r = client.get("/v1/reputation/agent/aaa111")
        assert r.status_code == 200
        data = r.json()
        assert data["score"]["tier"] == "Established"
        assert data["score"]["net_score"] == 152_000_000
        assert len(data["stakes"]) == 1
        assert len(data["endorsements_received"]) == 1

    def test_agent_not_found(self, client):
        r = client.get("/v1/reputation/agent/nonexistent")
        assert r.status_code == 404


class TestLeaderboard:
    def test_default(self, client):
        r = client.get("/v1/reputation/leaderboard")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        assert data[0]["agent_did"] == "aaa111"

    def test_filter_capability(self, client):
        r = client.get("/v1/reputation/leaderboard?capability=code_review")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["agent_did"] == "aaa111"

    def test_filter_tier(self, client):
        r = client.get("/v1/reputation/leaderboard?min_tier=established")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1


class TestEndorsements:
    def test_get_endorsements(self, client):
        r = client.get("/v1/reputation/endorsements/aaa111")
        assert r.status_code == 200
        data = r.json()
        assert len(data["received"]) == 1
        assert len(data["given"]) == 0


class TestChallenges:
    def test_get_challenges(self, client):
        r = client.get("/v1/reputation/challenges/aaa111")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["state"] == "Open"

    def test_filter_by_state(self, client):
        r = client.get("/v1/reputation/challenges/aaa111?state=Resolved")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 0


class TestStats:
    def test_stats(self, client):
        r = client.get("/v1/reputation/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_agents"] == 2
        assert data["total_endorsements"] == 1
        assert data["active_challenges"] == 1


class TestSybilEndpoints:
    def test_all_flags(self, client):
        r = client.get("/v1/reputation/sybil")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["agent_did"] == "bbb222"

    def test_agent_flags(self, client):
        r = client.get("/v1/reputation/sybil/bbb222")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["flag_type"] == "cycle"


class TestMCPToolEndpoints:
    def test_list_tools(self, client):
        r = client.get("/v1/tools")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 5

    def test_browse_tool(self, client):
        r = client.post("/v1/tools/reputation_browse")
        assert r.status_code == 200
        data = r.json()
        assert data["success"]
        assert len(data["data"]) == 2

    def test_my_status_tool(self, client):
        r = client.post("/v1/tools/reputation_my_status?agent_did=aaa111")
        assert r.status_code == 200
        data = r.json()
        assert data["success"]
        assert data["data"]["tier"] == "Established"


class TestLegacyEndpoints:
    def test_agents(self, client):
        r = client.get("/agents")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_leaderboard(self, client):
        r = client.get("/leaderboard")
        assert r.status_code == 200
