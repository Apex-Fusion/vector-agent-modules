"""
FastAPI REST endpoints for the Module 3 Reputation Indexer.

Implements the API from impl spec Section 13.1:
  - GET /v1/reputation/agent/{did}       — Full reputation profile
  - GET /v1/reputation/leaderboard       — Ranked agents
  - GET /v1/reputation/endorsements/{did} — Endorsements given/received
  - GET /v1/reputation/challenges/{did}  — Active challenges
  - GET /v1/reputation/decayable         — Agents eligible for decay
  - GET /v1/reputation/stats             — Aggregate stats (AFI component)

Additional endpoints:
  - GET /health                          — Indexer health check
  - GET /v1/reputation/sybil             — Sybil detection flags
  - POST /v1/tools/{tool_name}           — MCP tool execution

Agent identifiers: a path or query value `{did}` is a canonical DID
(`did:vector:<64 hex>`) or the 64-hex identifier of the agent's identity
record; another value gets HTTP 400. Each agent identifier in the output is
the 64-hex identifier (`*_did`) and also the canonical DID (`*_did_uri`).

Identity rule: the leaderboard leaves out agents whose identity record was
not Active at the index time, unless `include_all=true`. Such agents have
the tier Unverified; their amounts stay.

Stakes: a score has `stake_status` (missing, below_minimum or staked). No
action burns a stake token, so a fully decayed stake stays with
stake_amount 0 and is below_minimum, not missing. Each stake row has its
`stake_id` (the seed output reference of CreateStake) and `challenge_locks`
(a list: the hex asset names of the challenge tokens of the open challenges
that lock the stake), and each endorsement row has `target_stake` (the
stake_id of the stake that it backs).

Usage:
    uvicorn indexer.api:app --host 0.0.0.0 --port 8080

    Or via the indexer CLI:
    python -m indexer --with-api
"""

from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from reputation_staking import vector_identity as vi
from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.utils import add_did_fields

from indexer.storage import IndexerStorage

app = FastAPI(
    title="Module 3 Reputation Indexer API",
    description=(
        "Reputation scores, stakes, endorsements, challenges, "
        "sybil detection, and MCP tool endpoints for Vector Module 3"
    ),
    version="1.2.0",
)

# Storage instance — shared across requests
_storage: IndexerStorage | None = None


def get_storage() -> IndexerStorage:
    global _storage
    if _storage is None:
        db_path = os.environ.get("INDEXER_DB", "reputation_index.db")
        _storage = IndexerStorage(db_path)
    return _storage


def agent_hex(agent_did: str) -> str:
    """The 64-hex identifier of a DID or identifier. HTTP 400 for another
    value."""
    try:
        return vi.normalize_agent_id(agent_did).hex()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=(
                "not an agent identifier: use did:vector:<64 lowercase hex> "
                "or 64 hex characters"
            ),
        ) from None


def _rows(rows: list) -> list:
    return [add_did_fields(row) for row in rows]


def _with_capabilities(rows: list) -> list:
    for row in rows:
        row["capabilities"] = json.loads(row["capabilities"]) if row["capabilities"] else []
    return _rows(rows)


def _stake_rows(rows: list) -> list:
    """Stake rows with the lists `capabilities` and `challenge_locks`."""
    for row in rows:
        row["challenge_locks"] = json.loads(row["challenge_locks"]) if row["challenge_locks"] else []
    return _with_capabilities(rows)


# ── Health ──────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Health check with indexer state."""
    storage = get_storage()
    return {
        "status": "ok",
        "last_poll_slot": storage.get_state("last_poll_slot"),
        "last_poll_ms": storage.get_state("last_poll_ms"),
        "last_poll_time": storage.get_state("last_poll_time"),
        "agents_indexed": storage.get_state("agents_indexed"),
        "sybil_flags": storage.get_state("sybil_flags"),
        "identity_hash": storage.get_state("identity_hash"),
    }


# ── Core Reputation Endpoints (Section 13.1) ───────────────────────────────


@app.get("/v1/reputation/agent/{agent_did}")
def get_agent(agent_did: str):
    """Full reputation profile for an agent (score, tier, identity status,
    stake, endorsements, challenges, history)."""
    agent_id = agent_hex(agent_did)
    storage = get_storage()
    score = storage.get_score(agent_id)
    if not score:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {
        "score": add_did_fields(score),
        "stakes": _stake_rows(storage.get_stakes_for_agent(agent_id)),
        "endorsements_received": _with_capabilities(storage.get_endorsements_for_agent(agent_id)),
        "endorsements_given": _with_capabilities(storage.get_endorsements_by_agent(agent_id)),
        "challenges": _rows(storage.get_challenges_for_agent(agent_id)),
        "history_bonuses": _rows(storage.get_bonuses_for_agent(agent_id)),
        "sybil_flags": _rows(storage.get_sybil_flags(agent_id)),
    }


@app.get("/v1/reputation/leaderboard")
def leaderboard(
    capability: Optional[str] = Query(None, description="Filter by capability"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
    min_tier: Optional[str] = Query(None, description="Minimum tier"),
    include_all: bool = Query(
        False, description="Also list agents whose identity record is not Active",
    ),
):
    """Ranked agents by reputation score, with optional filtering."""
    storage = get_storage()
    all_scores = storage.get_all_scores(active_only=not include_all)

    # Filter by tier
    if min_tier:
        tier_order = {
            "unverified": 0, "novice": 1, "established": 2,
            "trusted": 3, "elite": 4,
        }
        min_level = tier_order.get(min_tier.lower(), 0)
        all_scores = [
            s for s in all_scores
            if tier_order.get(s["tier"].lower(), 0) >= min_level
        ]

    # Filter by capability
    if capability:
        agents_with_cap = set()
        for s in all_scores:
            stakes = storage.get_stakes_for_agent(s["agent_did"])
            for stake in stakes:
                caps = json.loads(stake["capabilities"]) if stake["capabilities"] else []
                if capability in caps:
                    agents_with_cap.add(s["agent_did"])
                    break
        all_scores = [s for s in all_scores if s["agent_did"] in agents_with_cap]

    return _rows(all_scores[:limit])


@app.get("/v1/reputation/endorsements/{agent_did}")
def get_endorsements(agent_did: str):
    """All endorsements given to and by an agent."""
    agent_id = agent_hex(agent_did)
    storage = get_storage()
    return {
        "received": _with_capabilities(storage.get_endorsements_for_agent(agent_id)),
        "given": _with_capabilities(storage.get_endorsements_by_agent(agent_id)),
    }


@app.get("/v1/reputation/challenges/{agent_did}")
def get_challenges(
    agent_did: str,
    state: Optional[str] = Query(None, description="Filter by state (Open, Escalated, Resolved)"),
):
    """Challenges targeting an agent, with optional state filter."""
    agent_id = agent_hex(agent_did)
    storage = get_storage()
    challenges = storage.get_challenges_for_agent(agent_id)
    if state:
        challenges = [c for c in challenges if c["state"] == state]
    return _rows(challenges)


@app.get("/v1/reputation/decayable")
def get_decayable():
    """Agents eligible for decay processing at the time of the last poll."""
    storage = get_storage()
    defaults = ProtocolParamsInfo()
    last_ms = storage.get_state("last_poll_ms")
    activity_window = storage.get_state("activity_window")
    epoch_length = storage.get_state("epoch_length")
    return _rows(storage.get_decayable_agents(
        int(last_ms) if last_ms else 0,
        int(activity_window) if activity_window else defaults.activity_window,
        int(epoch_length) if epoch_length else defaults.epoch_length,
    ))


@app.get("/v1/reputation/stats")
def get_stats():
    """Aggregate stats for AFI component."""
    storage = get_storage()
    stats = storage.get_stats()
    stats["last_poll_slot"] = storage.get_state("last_poll_slot")
    stats["last_poll_ms"] = storage.get_state("last_poll_ms")
    stats["last_poll_time"] = storage.get_state("last_poll_time")
    return stats


# ── Sybil Detection Endpoints ──────────────────────────────────────────────


@app.get("/v1/reputation/sybil")
def get_sybil_flags():
    """All sybil detection flags, sorted by severity."""
    storage = get_storage()
    return _rows(storage.get_all_sybil_flags())


@app.get("/v1/reputation/sybil/{agent_did}")
def get_agent_sybil_flags(agent_did: str):
    """Sybil flags for a specific agent."""
    agent_id = agent_hex(agent_did)
    storage = get_storage()
    return _rows(storage.get_sybil_flags(agent_id))


# ── MCP Tool Endpoints ─────────────────────────────────────────────────────


@app.get("/v1/tools")
def list_tools():
    """List available MCP tools and their schemas."""
    from reputation_staking.mcp_tools import TOOL_DEFINITIONS
    return TOOL_DEFINITIONS


@app.post("/v1/tools/reputation_browse")
def tool_browse(
    capability: Optional[str] = None,
    min_tier: Optional[str] = None,
    sort_by: str = "score",
    limit: int = 20,
    include_all: bool = False,
):
    """MCP tool: Find agents by capability/tier/score."""
    from reputation_staking.mcp_tools import reputation_browse
    storage = get_storage()
    result = reputation_browse(storage, capability, min_tier, sort_by, limit, include_all)
    return result.to_dict()


@app.post("/v1/tools/reputation_my_status")
def tool_my_status(agent_did: str):
    """MCP tool: Check your reputation status."""
    from reputation_staking.mcp_tools import reputation_my_status
    storage = get_storage()
    result = reputation_my_status(storage, agent_did)
    return result.to_dict()


# ── Legacy Endpoints (backwards compatibility) ──────────────────────────────


@app.get("/agents")
def list_agents():
    """List all agents with their reputation scores (legacy)."""
    storage = get_storage()
    return _rows(storage.get_all_scores())


@app.get("/agents/{agent_did}")
def get_agent_legacy(agent_did: str):
    """Get detailed reputation score breakdown for an agent (legacy)."""
    return get_agent(agent_did)


@app.get("/leaderboard")
def leaderboard_legacy(limit: int = 50, include_all: bool = False):
    """Top agents by reputation score (legacy)."""
    storage = get_storage()
    return _rows(storage.get_leaderboard(limit, include_all))
