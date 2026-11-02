"""Tests for the indexer REST API endpoints."""

import pytest

from indexer.storage import IndexerStorage
from reputation_staking import vector_identity as vi

# 64-hex identifiers of identity records
A = "a1" * 32
B = "b2" * 32
C = "c3" * 32  # an agent whose identity record is suspended
DID_A = "did:vector:" + A
DID_B = "did:vector:" + B
NOW_MS = 1_762_000_000_000


@pytest.fixture
def storage(tmp_path):
    """Create a temporary SQLite database with test data."""
    db_path = str(tmp_path / "test.db")
    s = IndexerStorage(db_path)

    # Add three agents with scores
    s.upsert_score(
        agent_did=A, self_stake=100_000_000, endorsement_total=50_000_000,
        challenge_total=0, history_bonus=2_000_000, decay=0,
        net_score=152_000_000, tier="Established", slot=5000,
        identity_status="active", indexed_at_ms=NOW_MS,
    )
    s.upsert_score(
        agent_did=B, self_stake=10_000_000, endorsement_total=0,
        challenge_total=0, history_bonus=0, decay=0,
        net_score=10_000_000, tier="Novice", slot=5000,
        identity_status="active", indexed_at_ms=NOW_MS,
    )
    s.upsert_score(
        agent_did=C, self_stake=900_000_000, endorsement_total=0,
        challenge_total=0, history_bonus=0, decay=0,
        net_score=900_000_000, tier="Unverified", slot=5000,
        identity_status="suspended", indexed_at_ms=NOW_MS,
    )

    # Add stakes
    s.upsert_stake(
        "tx1#0", A, "owner_aaa", 100_000_000,
        ["code_review", "testing"], 4000, 0,
    )
    s.upsert_stake(
        "tx2#0", B, "owner_bbb", 10_000_000,
        ["testing"], 4000, 0,
    )
    s.upsert_stake(
        "tx5#0", C, "owner_ccc", 900_000_000,
        ["code_review"], 4000, 0,
    )

    # Add an endorsement
    s.upsert_endorsement(
        "tx3#0", B, A, 50_000_000,
        ["code_review"], 3000,
    )

    # Add a challenge
    s.upsert_challenge(
        "tx4#0", B, A, "testing",
        25_000_000, "Open", None, 4500,
    )

    # Add sybil flags
    s.upsert_sybil_flag(B, "cycle", 0.7, "Part of test ring", A)

    # Set indexer state
    s.set_state("last_poll_slot", "5000")
    s.set_state("last_poll_ms", str(NOW_MS))
    s.set_state("last_poll_time", "1700000000")
    s.set_state("agents_indexed", "3")

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
        assert data["last_poll_ms"] == str(NOW_MS)
        assert data["agents_indexed"] == "3"


class TestAgentEndpoint:
    def test_get_agent(self, client):
        r = client.get(f"/v1/reputation/agent/{A}")
        assert r.status_code == 200
        data = r.json()
        assert data["score"]["tier"] == "Established"
        assert data["score"]["net_score"] == 152_000_000
        assert data["score"]["identity_status"] == "active"
        assert len(data["stakes"]) == 1
        assert len(data["endorsements_received"]) == 1

    def test_get_agent_by_did(self, client):
        """The path takes a canonical DID or the 64-hex identifier."""
        by_did = client.get(f"/v1/reputation/agent/{DID_A}")
        by_hex = client.get(f"/v1/reputation/agent/{A.upper()}")
        assert by_did.status_code == 200 and by_hex.status_code == 200
        assert by_did.json() == by_hex.json()

    def test_output_has_canonical_dids(self, client):
        data = client.get(f"/v1/reputation/agent/{A}").json()
        assert data["score"]["agent_did"] == A
        assert data["score"]["agent_did_uri"] == DID_A
        endorsement = data["endorsements_received"][0]
        assert endorsement["endorser_did_uri"] == DID_B
        assert endorsement["target_did_uri"] == DID_A
        assert data["challenges"][0]["challenger_did_uri"] == DID_B
        assert data["stakes"][0]["agent_did_uri"] == DID_A

    def test_agent_not_found(self, client):
        r = client.get(f"/v1/reputation/agent/{'dd' * 32}")
        assert r.status_code == 404

    def test_not_an_identifier(self, client):
        for bad in ("nonexistent", "did:vector:agent:abc", "ab" * 31):
            r = client.get(f"/v1/reputation/agent/{bad}")
            assert r.status_code == 400, bad


class TestLeaderboard:
    def test_default(self, client):
        r = client.get("/v1/reputation/leaderboard")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        assert data[0]["agent_did"] == A
        assert data[0]["agent_did_uri"] == DID_A

    def test_not_active_agent_left_out_by_default(self, client):
        """The suspended agent has the highest amounts but is not listed."""
        ids = [row["agent_did"] for row in client.get("/v1/reputation/leaderboard").json()]
        assert C not in ids

    def test_include_all(self, client):
        data = client.get("/v1/reputation/leaderboard?include_all=true").json()
        assert [row["agent_did"] for row in data] == [C, A, B]
        assert data[0]["identity_status"] == "suspended"
        assert data[0]["tier"] == "Unverified"
        assert data[0]["net_score"] == 900_000_000

    def test_filter_capability(self, client):
        r = client.get("/v1/reputation/leaderboard?capability=code_review")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["agent_did"] == A

    def test_filter_tier(self, client):
        r = client.get("/v1/reputation/leaderboard?min_tier=established")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1


class TestEndorsements:
    def test_get_endorsements(self, client):
        r = client.get(f"/v1/reputation/endorsements/{DID_A}")
        assert r.status_code == 200
        data = r.json()
        assert len(data["received"]) == 1
        assert len(data["given"]) == 0
        assert data["received"][0]["capabilities"] == ["code_review"]


class TestChallenges:
    def test_get_challenges(self, client):
        r = client.get(f"/v1/reputation/challenges/{A}")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["state"] == "Open"
        assert data[0]["target_did_uri"] == DID_A

    def test_filter_by_state(self, client):
        r = client.get(f"/v1/reputation/challenges/{A}?state=Resolved")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 0


class TestStats:
    def test_stats(self, client):
        r = client.get("/v1/reputation/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_agents"] == 3
        assert data["total_endorsements"] == 1
        assert data["active_challenges"] == 1
        assert data["identity_status_distribution"] == {"active": 2, "suspended": 1}
        assert data["last_poll_ms"] == str(NOW_MS)


class TestDecayable:
    def test_decayable_uses_poll_time(self, client, storage):
        """A stake is decayable after activity_window + 1 epochs (ms)."""
        epoch = 14_400_000
        storage.set_state("last_poll_ms", str(4000 + 181 * epoch))
        rows = client.get("/v1/reputation/decayable").json()
        assert {row["agent_did"] for row in rows} == {A, B, C}
        storage.set_state("last_poll_ms", str(4000 + 181 * epoch - 1))
        assert client.get("/v1/reputation/decayable").json() == []


class TestSybilEndpoints:
    def test_all_flags(self, client):
        r = client.get("/v1/reputation/sybil")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["agent_did"] == B
        assert data[0]["agent_did_uri"] == DID_B
        assert data[0]["related_did_uris"] == [DID_A]

    def test_agent_flags(self, client):
        r = client.get(f"/v1/reputation/sybil/{DID_B}")
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

    def test_browse_tool_include_all(self, client):
        data = client.post("/v1/tools/reputation_browse?include_all=true").json()
        assert len(data["data"]) == 3

    def test_my_status_tool(self, client):
        r = client.post(f"/v1/tools/reputation_my_status?agent_did={DID_A}")
        assert r.status_code == 200
        data = r.json()
        assert data["success"]
        assert data["data"]["tier"] == "Established"
        assert data["data"]["agent_did_uri"] == DID_A


class TestLegacyEndpoints:
    def test_agents(self, client):
        r = client.get("/agents")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_leaderboard(self, client):
        r = client.get("/leaderboard")
        assert r.status_code == 200
        assert [row["agent_did"] for row in r.json()] == [A, B]
        assert vi.did_from_id(A) == r.json()[0]["agent_did_uri"]
