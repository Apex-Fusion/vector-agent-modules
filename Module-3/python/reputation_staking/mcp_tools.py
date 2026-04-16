"""
MCP Server Tools for Module 3: Reputation Staking.

Implements the 5 tools defined in the implementation spec (Section 12.2):
  1. reputation_stake    — Stake AP3X to back claimed capabilities
  2. reputation_endorse  — Endorse another agent's capabilities
  3. reputation_challenge — Challenge an agent's capability claim
  4. reputation_browse    — Find agents by capability/tier/score
  5. reputation_my_status — Check your own reputation status

Each tool is a standalone async function that can be:
  - Called directly from Python
  - Served via the REST API (indexer/api.py)
  - Integrated into an external MCP server (e.g., mcp-server repo)

Read-only tools (browse, my_status) use the indexer database.
Write tools (stake, endorse, challenge) use the ReputationStakingClient.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import List, Optional

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.models import ReputationTier

logger = logging.getLogger(__name__)


# ── Tool Result Types ───────────────────────────────────────────────────────


@dataclass
class ToolResult:
    """Standard result wrapper for MCP tools."""
    success: bool
    data: dict | list | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"success": self.success}
        if self.data is not None:
            d["data"] = self.data
        if self.error is not None:
            d["error"] = self.error
        return d


# ── Tool Definitions (for MCP registration) ────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "reputation_stake",
        "description": (
            "Stake AP3X to back your claimed capabilities — "
            "your stake IS your reputation"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Capabilities to back with stake",
                },
                "stake_ap3x": {
                    "type": "number",
                    "description": "AP3X amount to stake",
                },
            },
            "required": ["capabilities", "stake_ap3x"],
        },
    },
    {
        "name": "reputation_endorse",
        "description": (
            "Endorse another agent by staking AP3X — "
            "you lose stake if they're proven fraudulent"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_did": {
                    "type": "string",
                    "description": "Agent DID to endorse",
                },
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Capabilities to vouch for",
                },
                "stake_ap3x": {
                    "type": "number",
                    "description": "AP3X endorsement amount",
                },
            },
            "required": ["target_did", "capabilities", "stake_ap3x"],
        },
    },
    {
        "name": "reputation_challenge",
        "description": (
            "Challenge an agent's capability claim — "
            "earn their stake if you're right"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_did": {
                    "type": "string",
                    "description": "Agent DID to challenge",
                },
                "capability": {
                    "type": "string",
                    "description": "Specific capability to dispute",
                },
                "evidence": {
                    "type": "string",
                    "description": "Evidence that the claim is false",
                },
                "stake_ap3x": {
                    "type": "number",
                    "description": "AP3X challenge stake",
                },
            },
            "required": ["target_did", "capability", "evidence", "stake_ap3x"],
        },
    },
    {
        "name": "reputation_browse",
        "description": (
            "Find trustworthy agents by capability, reputation tier, or score"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "Filter by capability (optional)",
                },
                "min_tier": {
                    "type": "string",
                    "enum": ["unverified", "novice", "established", "trusted", "elite"],
                    "description": "Minimum reputation tier",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["score", "endorsements", "history"],
                    "description": "Sort order",
                    "default": "score",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "reputation_my_status",
        "description": (
            "Check your current reputation score, tier, endorsements, "
            "and active challenges"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_did": {
                    "type": "string",
                    "description": "Your agent DID (hex)",
                },
            },
            "required": ["agent_did"],
        },
    },
]


# ── Read-Only Tools (use IndexerStorage) ────────────────────────────────────


def reputation_browse(
    storage,
    capability: Optional[str] = None,
    min_tier: Optional[str] = None,
    sort_by: str = "score",
    limit: int = 20,
) -> ToolResult:
    """Find agents by capability, tier, or score.

    Args:
        storage: IndexerStorage instance.
        capability: Filter by staked capability (optional).
        min_tier: Minimum tier name (optional).
        sort_by: "score", "endorsements", or "history".
        limit: Maximum results.

    Returns:
        ToolResult with list of matching agents.
    """
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

    # Sort
    sort_keys = {
        "score": lambda s: s["net_score"],
        "endorsements": lambda s: s["endorsement_total"],
        "history": lambda s: s["history_bonus"],
    }
    key_fn = sort_keys.get(sort_by, sort_keys["score"])
    all_scores.sort(key=key_fn, reverse=True)

    # Format results
    results = []
    for s in all_scores[:limit]:
        stakes = storage.get_stakes_for_agent(s["agent_did"])
        caps = []
        for stake in stakes:
            caps.extend(json.loads(stake["capabilities"]) if stake["capabilities"] else [])

        results.append({
            "agent_did": s["agent_did"],
            "score_ap3x": s["net_score"] // DFM_PER_AP3X,
            "score_dfm": s["net_score"],
            "tier": s["tier"],
            "self_stake_ap3x": s["self_stake"] // DFM_PER_AP3X,
            "endorsement_total_ap3x": s["endorsement_total"] // DFM_PER_AP3X,
            "capabilities": list(set(caps)),
        })

    return ToolResult(success=True, data=results)


def reputation_my_status(storage, agent_did: str) -> ToolResult:
    """Check reputation status for an agent.

    Args:
        storage: IndexerStorage instance.
        agent_did: Agent DID hex.

    Returns:
        ToolResult with full reputation breakdown.
    """
    score = storage.get_score(agent_did)
    if not score:
        return ToolResult(
            success=False,
            error=f"Agent {agent_did[:16]}... not found in index",
        )

    stakes = storage.get_stakes_for_agent(agent_did)
    endorsements_received = storage.get_endorsements_for_agent(agent_did)
    endorsements_given = storage.get_endorsements_by_agent(agent_did)
    challenges = storage.get_challenges_for_agent(agent_did)
    bonuses = storage.get_bonuses_for_agent(agent_did)

    caps = []
    for s in stakes:
        caps.extend(json.loads(s["capabilities"]) if s["capabilities"] else [])

    active_challenges = [
        {
            "challenger_did": c["challenger_did"],
            "capability": c["capability"],
            "stake_ap3x": c["stake_amount"] // DFM_PER_AP3X,
            "state": c["state"],
        }
        for c in challenges
        if c["state"] == "Open"
    ]

    return ToolResult(
        success=True,
        data={
            "agent_did": agent_did,
            "tier": score["tier"],
            "score_ap3x": score["net_score"] // DFM_PER_AP3X,
            "score_dfm": score["net_score"],
            "breakdown": {
                "self_stake": score["self_stake"] // DFM_PER_AP3X,
                "endorsements": score["endorsement_total"] // DFM_PER_AP3X,
                "challenges": score["challenge_total"] // DFM_PER_AP3X,
                "history_bonus": score["history_bonus"] // DFM_PER_AP3X,
                "decay": score["decay"] // DFM_PER_AP3X,
            },
            "capabilities": list(set(caps)),
            "endorsements_received": len(endorsements_received),
            "endorsements_given": len(endorsements_given),
            "active_challenges": active_challenges,
            "history_bonuses": len(bonuses),
        },
    )


# ── Write Tools (use ReputationStakingClient) ──────────────────────────────


def reputation_stake(
    client,
    agent_did: str,
    capabilities: List[str],
    stake_ap3x: float,
    seed_utxo: Optional[str] = None,
) -> ToolResult:
    """Stake AP3X to back claimed capabilities.

    For first-time staking, a seed UTxO is created automatically.
    For existing stakes, this increases the stake amount.

    Args:
        client: ReputationStakingClient instance.
        agent_did: Agent DID hex.
        capabilities: List of capability strings.
        stake_ap3x: Amount in AP3X (will be converted to DFM).
        seed_utxo: Optional seed UTxO ref ("txhash#idx") for CreateStake.

    Returns:
        ToolResult with transaction hash.
    """
    stake_dfm = int(stake_ap3x * DFM_PER_AP3X)

    try:
        # Check if agent already has a stake
        existing = client.find_stake_utxo(agent_did)

        if existing:
            # TODO: IncreaseStake not yet implemented in client
            return ToolResult(
                success=False,
                error="Agent already has a stake. IncreaseStake not yet available.",
            )

        # New stake — need seed UTxO
        if not seed_utxo:
            logger.info("Creating seed UTxO for %s...", agent_did[:16])
            seed_tx = client.create_seed_utxo(
                agent_did, capabilities, min_lovelace=2_000_000
            )
            seed_utxo = f"{seed_tx}#0"
            logger.info("Seed UTxO: %s", seed_utxo)

        tx_hash = client.create_stake(
            agent_did, capabilities, stake_dfm, seed_utxo=seed_utxo
        )

        return ToolResult(
            success=True,
            data={
                "tx_hash": tx_hash,
                "agent_did": agent_did,
                "stake_ap3x": stake_ap3x,
                "capabilities": capabilities,
            },
        )
    except Exception as e:
        logger.exception("reputation_stake failed")
        return ToolResult(success=False, error=str(e))


def reputation_endorse(
    client,
    endorser_did: str,
    target_did: str,
    capabilities: List[str],
    stake_ap3x: float,
) -> ToolResult:
    """Endorse another agent's capabilities.

    Args:
        client: ReputationStakingClient instance.
        endorser_did: Endorser's agent DID hex.
        target_did: Target agent DID hex.
        capabilities: Capabilities to endorse.
        stake_ap3x: AP3X endorsement amount.

    Returns:
        ToolResult with transaction hash.
    """
    stake_dfm = int(stake_ap3x * DFM_PER_AP3X)

    try:
        tx_hash = client.mint_endorsement(
            endorser_did, target_did, capabilities, stake_dfm
        )
        return ToolResult(
            success=True,
            data={
                "tx_hash": tx_hash,
                "endorser_did": endorser_did,
                "target_did": target_did,
                "stake_ap3x": stake_ap3x,
                "capabilities": capabilities,
            },
        )
    except Exception as e:
        logger.exception("reputation_endorse failed")
        return ToolResult(success=False, error=str(e))


def reputation_challenge(
    client,
    challenger_did: str,
    target_did: str,
    capability: str,
    evidence: str,
    stake_ap3x: float,
) -> ToolResult:
    """Challenge an agent's capability claim.

    Args:
        client: ReputationStakingClient instance.
        challenger_did: Challenger's agent DID hex.
        target_did: Target agent DID hex.
        capability: Specific capability to dispute.
        evidence: Evidence description/hash.
        stake_ap3x: AP3X challenge stake.

    Returns:
        ToolResult with transaction hash.
    """
    import hashlib

    stake_dfm = int(stake_ap3x * DFM_PER_AP3X)
    evidence_hash = hashlib.blake2b(evidence.encode(), digest_size=32).hexdigest()

    try:
        tx_hash, _datum = client.mint_challenge(
            challenger_did=challenger_did,
            target_did=target_did,
            capability=capability,
            stake_amount=stake_dfm,
            evidence_hash=evidence_hash,
            evidence_uri=evidence[:256],
        )
        return ToolResult(
            success=True,
            data={
                "tx_hash": tx_hash,
                "challenger_did": challenger_did,
                "target_did": target_did,
                "capability": capability,
                "stake_ap3x": stake_ap3x,
                "evidence_hash": evidence_hash,
            },
        )
    except Exception as e:
        logger.exception("reputation_challenge failed")
        return ToolResult(success=False, error=str(e))
