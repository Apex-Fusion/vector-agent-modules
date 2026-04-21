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

from indexer.storage import IndexerStorage

app = FastAPI(
    title="Module 3 Reputation Indexer API",
    description=(
        "Reputation scores, stakes, endorsements, challenges, "
        "sybil detection, and MCP tool endpoints for Vector Module 3"
    ),
    version="1.1.0",
)

# Storage instance — shared across requests
_storage: IndexerStorage | None = None


def get_storage() -> IndexerStorage:
    global _storage
    if _storage is None:
        db_path = os.environ.get("INDEXER_DB", "reputation_index.db")
        _storage = IndexerStorage(db_path)
    return _storage


# ── Health ──────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Health check with indexer state."""
    storage = get_storage()
    return {
        "status": "ok",
        "last_poll_slot": storage.get_state("last_poll_slot"),
        "last_poll_time": storage.get_state("last_poll_time"),
        "agents_indexed": storage.get_state("agents_indexed"),
        "sybil_flags": storage.get_state("sybil_flags"),
    }


# ── Core Reputation Endpoints (Section 13.1) ───────────────────────────────


@app.get("/v1/reputation/agent/{agent_did}")
def get_agent(agent_did: str):
    """Full reputation profile for an agent (score, tier, stake, endorsements, challenges, history)."""
    storage = get_storage()
    score = storage.get_score(agent_did)
    if not score:
        raise HTTPException(status_code=404, detail="Agent not found")

    stakes = storage.get_stakes_for_agent(agent_did)
    endorsements_received = storage.get_endorsements_for_agent(agent_did)
    endorsements_given = storage.get_endorsements_by_agent(agent_did)
    challenges = storage.get_challenges_for_agent(agent_did)
    bonuses = storage.get_bonuses_for_agent(agent_did)
    sybil_flags = storage.get_sybil_flags(agent_did)

    # Parse JSON capabilities back to lists
    for s in stakes:
        s["capabilities"] = json.loads(s["capabilities"]) if s["capabilities"] else []
    for e in endorsements_received + endorsements_given:
        e["capabilities"] = json.loads(e["capabilities"]) if e["capabilities"] else []

    return {
        "score": score,
        "stakes": stakes,
        "endorsements_received": endorsements_received,
        "endorsements_given": endorsements_given,
        "challenges": challenges,
        "history_bonuses": bonuses,
        "sybil_flags": sybil_flags,
    }


@app.get("/v1/reputation/leaderboard")
def leaderboard(
    capability: Optional[str] = Query(None, description="Filter by capability"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
    min_tier: Optional[str] = Query(None, description="Minimum tier"),
):
    """Ranked agents by reputation score, with optional filtering."""
    storage = get_storage()
    all_scores = storage.get_all_scores()

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

    return all_scores[:limit]


@app.get("/v1/reputation/endorsements/{agent_did}")
def get_endorsements(agent_did: str):
    """All endorsements given to and by an agent."""
    storage = get_storage()
    received = storage.get_endorsements_for_agent(agent_did)
    given = storage.get_endorsements_by_agent(agent_did)
    for e in received + given:
        e["capabilities"] = json.loads(e["capabilities"]) if e["capabilities"] else []
    return {"received": received, "given": given}


@app.get("/v1/reputation/challenges/{agent_did}")
def get_challenges(
    agent_did: str,
    state: Optional[str] = Query(None, description="Filter by state (Open, Escalated, Resolved)"),
):
    """Challenges targeting an agent, with optional state filter."""
    storage = get_storage()
    challenges = storage.get_challenges_for_agent(agent_did)
    if state:
        challenges = [c for c in challenges if c["state"] == state]
    return challenges


@app.get("/v1/reputation/decayable")
def get_decayable():
    """Agents eligible for decay processing."""
    storage = get_storage()
    last_slot = storage.get_state("last_poll_slot")
    current_slot = int(last_slot) if last_slot else 0
    return storage.get_decayable_agents(current_slot)


@app.get("/v1/reputation/stats")
def get_stats():
    """Aggregate stats for AFI component."""
    storage = get_storage()
    stats = storage.get_stats()
    stats["last_poll_slot"] = storage.get_state("last_poll_slot")
    stats["last_poll_time"] = storage.get_state("last_poll_time")
    return stats


# ── Sybil Detection Endpoints ──────────────────────────────────────────────


@app.get("/v1/reputation/sybil")
def get_sybil_flags():
    """All sybil detection flags, sorted by severity."""
    storage = get_storage()
    return storage.get_all_sybil_flags()


@app.get("/v1/reputation/sybil/{agent_did}")
def get_agent_sybil_flags(agent_did: str):
    """Sybil flags for a specific agent."""
    storage = get_storage()
    return storage.get_sybil_flags(agent_did)


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
):
    """MCP tool: Find agents by capability/tier/score."""
    from reputation_staking.mcp_tools import reputation_browse
    storage = get_storage()
    result = reputation_browse(storage, capability, min_tier, sort_by, limit)
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
    return storage.get_all_scores()


@app.get("/agents/{agent_did}")
def get_agent_legacy(agent_did: str):
    """Get detailed reputation score breakdown for an agent (legacy)."""
    return get_agent(agent_did)


@app.get("/leaderboard")
def leaderboard_legacy(limit: int = 50):
    """Top agents by reputation score (legacy)."""
    storage = get_storage()
    return storage.get_leaderboard(limit)
