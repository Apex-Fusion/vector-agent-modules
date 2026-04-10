"""
FastAPI REST endpoints for the reputation indexer.

Provides read-only access to indexed reputation data.

Usage:
    uvicorn indexer.api:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException

from indexer.storage import IndexerStorage

app = FastAPI(
    title="Module 3 Reputation Indexer",
    description="Reputation scores, stakes, endorsements, and challenges",
    version="0.1.0",
)

# Storage instance — shared across requests
_storage: IndexerStorage | None = None


def get_storage() -> IndexerStorage:
    global _storage
    if _storage is None:
        db_path = os.environ.get("INDEXER_DB", "reputation_index.db")
        _storage = IndexerStorage(db_path)
    return _storage


@app.get("/health")
def health():
    """Health check with indexer state."""
    storage = get_storage()
    return {
        "status": "ok",
        "last_poll_slot": storage.get_state("last_poll_slot"),
        "last_poll_time": storage.get_state("last_poll_time"),
        "agents_indexed": storage.get_state("agents_indexed"),
    }


@app.get("/agents")
def list_agents():
    """List all agents with their reputation scores."""
    storage = get_storage()
    return storage.get_all_scores()


@app.get("/agents/{agent_did}")
def get_agent(agent_did: str):
    """Get detailed reputation score breakdown for an agent."""
    storage = get_storage()
    score = storage.get_score(agent_did)
    if not score:
        raise HTTPException(status_code=404, detail="Agent not found")

    stakes = storage.get_stakes_for_agent(agent_did)
    endorsements_received = storage.get_endorsements_for_agent(agent_did)
    endorsements_given = storage.get_endorsements_by_agent(agent_did)
    challenges = storage.get_challenges_for_agent(agent_did)
    bonuses = storage.get_bonuses_for_agent(agent_did)

    # Parse JSON capabilities back to lists
    for s in stakes:
        s["capabilities"] = json.loads(s["capabilities"])
    for e in endorsements_received + endorsements_given:
        e["capabilities"] = json.loads(e["capabilities"])

    return {
        "score": score,
        "stakes": stakes,
        "endorsements_received": endorsements_received,
        "endorsements_given": endorsements_given,
        "challenges": challenges,
        "history_bonuses": bonuses,
    }


@app.get("/agents/{agent_did}/endorsements")
def get_agent_endorsements(agent_did: str):
    """Get endorsements given to and by an agent."""
    storage = get_storage()
    received = storage.get_endorsements_for_agent(agent_did)
    given = storage.get_endorsements_by_agent(agent_did)
    for e in received + given:
        e["capabilities"] = json.loads(e["capabilities"])
    return {"received": received, "given": given}


@app.get("/agents/{agent_did}/challenges")
def get_agent_challenges(agent_did: str):
    """Get challenges targeting an agent."""
    storage = get_storage()
    return storage.get_challenges_for_agent(agent_did)


@app.get("/leaderboard")
def leaderboard(limit: int = 50):
    """Top agents by reputation score."""
    storage = get_storage()
    return storage.get_leaderboard(limit)
