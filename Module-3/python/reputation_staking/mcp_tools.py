"""
MCP Server Tools for Module 3: Reputation Staking.

Implements the 5 tools defined in the implementation spec (Section 12.2):
  1. reputation_stake    — Stake AP3X to back claimed capabilities
  2. reputation_endorse  — Endorse another agent's capabilities
  3. reputation_challenge — Challenge an agent's capability claim
  4. reputation_browse    — Find agents by capability/tier/score
  5. reputation_my_status — Check your own reputation status

Each tool is a standalone function that can be:
  - Called directly from Python
  - Served via the REST API (indexer/api.py)
  - Integrated into an external MCP server (e.g., mcp-server repo)

Read-only tools (browse, my_status) use the indexer database.
Write tools (stake, endorse, challenge) use the ReputationStakingClient.

Agent identifiers: the tools accept a canonical DID (`did:vector:<64 hex>`)
or the 64-hex identifier of the agent's identity record. Each agent
identifier in the output is the 64-hex identifier (`*_did`) and also the
canonical DID (`*_did_uri`).

Identity rule: the indexer gives an agent whose identity record is not
Active at the index time the tier Unverified. reputation_browse leaves such
agents out unless `include_all` is true.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from reputation_staking import vector_identity as vi
from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.utils import add_did_fields

logger = logging.getLogger(__name__)

AGENT_ID_DESCRIPTION = (
    "Agent DID (did:vector:<64 hex>) or the 64-hex identifier of its identity record"
)


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
            "your stake IS your reputation. Your identity record must be "
            "Active and list each capability; its controller key signs."
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
            "you lose stake if they're proven fraudulent. Both identity "
            "records must be Active; the target's record lists each capability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_did": {
                    "type": "string",
                    "description": AGENT_ID_DESCRIPTION + " (agent to endorse)",
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
            "earn their stake if you're right. Your identity record must be "
            "Active and at least min_agent_age old."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_did": {
                    "type": "string",
                    "description": AGENT_ID_DESCRIPTION + " (agent to challenge)",
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
            "Find trustworthy agents by capability, reputation tier, or score. "
            "Agents whose identity record is not Active are left out unless "
            "include_all is true."
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
                "include_all": {
                    "type": "boolean",
                    "description": (
                        "Also list agents whose identity record is not Active "
                        "(default false)"
                    ),
                    "default": False,
                },
            },
        },
    },
    {
        "name": "reputation_my_status",
        "description": (
            "Check your current reputation score, tier, identity status, "
            "endorsements, and active challenges"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_did": {
                    "type": "string",
                    "description": AGENT_ID_DESCRIPTION,
                },
            },
            "required": ["agent_did"],
        },
    },
]

_TIER_ORDER = {
    "unverified": 0, "novice": 1, "established": 2,
    "trusted": 3, "elite": 4,
}


def _capabilities(stake_rows: list) -> List[str]:
    caps: List[str] = []
    for stake in stake_rows:
        caps.extend(json.loads(stake["capabilities"]) if stake["capabilities"] else [])
    return caps


# ── Read-Only Tools (use IndexerStorage) ────────────────────────────────────


def reputation_browse(
    storage,
    capability: Optional[str] = None,
    min_tier: Optional[str] = None,
    sort_by: str = "score",
    limit: int = 20,
    include_all: bool = False,
) -> ToolResult:
    """Find agents by capability, tier, or score.

    Args:
        storage: IndexerStorage instance.
        capability: Filter by staked capability (optional).
        min_tier: Minimum tier name (optional).
        sort_by: "score", "endorsements", or "history".
        limit: Maximum results.
        include_all: Also list agents whose identity record is not Active.

    Returns:
        ToolResult with list of matching agents.
    """
    all_scores = storage.get_all_scores(active_only=not include_all)

    # Filter by tier
    if min_tier:
        min_level = _TIER_ORDER.get(min_tier.lower(), 0)
        all_scores = [
            s for s in all_scores
            if _TIER_ORDER.get(s["tier"].lower(), 0) >= min_level
        ]

    # Filter by capability
    if capability:
        all_scores = [
            s for s in all_scores
            if capability in _capabilities(storage.get_stakes_for_agent(s["agent_did"]))
        ]

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
        caps = _capabilities(storage.get_stakes_for_agent(s["agent_did"]))
        results.append(add_did_fields({
            "agent_did": s["agent_did"],
            "identity_status": s["identity_status"],
            "stake_status": s["stake_status"],
            "score_ap3x": s["net_score"] // DFM_PER_AP3X,
            "score_dfm": s["net_score"],
            "tier": s["tier"],
            "self_stake_ap3x": s["self_stake"] // DFM_PER_AP3X,
            "endorsement_total_ap3x": s["endorsement_total"] // DFM_PER_AP3X,
            "capabilities": sorted(set(caps)),
        }))

    return ToolResult(success=True, data=results)


def reputation_my_status(storage, agent_did: str) -> ToolResult:
    """Check reputation status for an agent.

    Args:
        storage: IndexerStorage instance.
        agent_did: Canonical DID or 64-hex identifier of the agent.

    Returns:
        ToolResult with full reputation breakdown.
    """
    try:
        agent_hex = vi.normalize_agent_id(agent_did).hex()
    except (TypeError, ValueError) as exc:
        return ToolResult(success=False, error=str(exc))

    score = storage.get_score(agent_hex)
    if not score:
        return ToolResult(
            success=False,
            error=f"Agent {vi.did_from_id(agent_hex)} not found in index",
        )

    stakes = storage.get_stakes_for_agent(agent_hex)
    endorsements_received = storage.get_endorsements_for_agent(agent_hex)
    endorsements_given = storage.get_endorsements_by_agent(agent_hex)
    challenges = storage.get_challenges_for_agent(agent_hex)
    bonuses = storage.get_bonuses_for_agent(agent_hex)

    active_challenges = [
        add_did_fields({
            "challenger_did": c["challenger_did"],
            "capability": c["capability"],
            "stake_ap3x": c["stake_amount"] // DFM_PER_AP3X,
            "state": c["state"],
        })
        for c in challenges
        if c["state"] == "Open"
    ]

    return ToolResult(
        success=True,
        data=add_did_fields({
            "agent_did": agent_hex,
            "identity_status": score["identity_status"],
            "stake_status": score["stake_status"],
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
            "capabilities": sorted(set(_capabilities(stakes))),
            "endorsements_received": len(endorsements_received),
            "endorsements_given": len(endorsements_given),
            "active_challenges": active_challenges,
            "history_bonuses": len(bonuses),
        }),
    )


# ── Write Tools (use ReputationStakingClient) ──────────────────────────────


def reputation_stake(
    client,
    agent_did: str,
    capabilities: List[str],
    stake_ap3x: float,
    seed_utxo: Optional[str] = None,
    controller_skey=None,
) -> ToolResult:
    """Stake AP3X to back claimed capabilities.

    For first-time staking, a seed UTxO is created automatically. An agent
    that already has a stake gets an error: the client has no IncreaseStake.

    Args:
        client: ReputationStakingClient instance.
        agent_did: Canonical DID or 64-hex identifier of the agent.
        capabilities: List of capability strings.
        stake_ap3x: Amount in AP3X (will be converted to DFM).
        seed_utxo: Optional seed UTxO ref ("txhash#idx") for CreateStake.
        controller_skey: Signing key of the record's controller when it is
            not the client's wallet key.

    Returns:
        ToolResult with transaction hash.
    """
    stake_dfm = int(stake_ap3x * DFM_PER_AP3X)

    try:
        agent_hex = vi.normalize_agent_id(agent_did).hex()
        if client.find_stake_utxo(agent_hex):
            return ToolResult(
                success=False,
                error="Agent already has a stake. IncreaseStake is not available.",
            )

        # Check the record before the seed transaction
        client.check_agent(agent_hex, capabilities, controller_skey)

        # New stake — need seed UTxO
        if not seed_utxo:
            logger.info("Creating seed UTxO for %s...", agent_hex[:16])
            seed_tx = client.create_seed_utxo(
                agent_hex, capabilities, min_lovelace=2_000_000
            )
            seed_utxo = f"{seed_tx}#0"
            logger.info("Seed UTxO: %s", seed_utxo)

        tx_hash = client.create_stake(
            agent_hex, capabilities, stake_dfm, seed_utxo=seed_utxo,
            controller_skey=controller_skey,
        )

        return ToolResult(
            success=True,
            data=add_did_fields({
                "tx_hash": tx_hash,
                "agent_did": agent_hex,
                "stake_ap3x": stake_ap3x,
                "capabilities": capabilities,
            }),
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
    controller_skey=None,
) -> ToolResult:
    """Endorse another agent's capabilities.

    Args:
        client: ReputationStakingClient instance.
        endorser_did: Canonical DID or 64-hex identifier of the endorser.
        target_did: Canonical DID or 64-hex identifier of the target.
        capabilities: Capabilities to endorse.
        stake_ap3x: AP3X endorsement amount.
        controller_skey: Signing key of the endorser's controller when it is
            not the client's wallet key.

    Returns:
        ToolResult with transaction hash.
    """
    stake_dfm = int(stake_ap3x * DFM_PER_AP3X)

    try:
        endorser_hex = vi.normalize_agent_id(endorser_did).hex()
        target_hex = vi.normalize_agent_id(target_did).hex()
        tx_hash = client.mint_endorsement(
            endorser_hex, target_hex, capabilities, stake_dfm,
            controller_skey=controller_skey,
        )
        return ToolResult(
            success=True,
            data=add_did_fields({
                "tx_hash": tx_hash,
                "endorser_did": endorser_hex,
                "target_did": target_hex,
                "stake_ap3x": stake_ap3x,
                "capabilities": capabilities,
            }),
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
    controller_skey=None,
) -> ToolResult:
    """Challenge an agent's capability claim.

    Args:
        client: ReputationStakingClient instance.
        challenger_did: Canonical DID or 64-hex identifier of the challenger.
        target_did: Canonical DID or 64-hex identifier of the target.
        capability: Specific capability to dispute.
        evidence: Evidence description/hash.
        stake_ap3x: AP3X challenge stake.
        controller_skey: Signing key of the challenger's controller when it
            is not the client's wallet key.

    Returns:
        ToolResult with transaction hash.
    """
    stake_dfm = int(stake_ap3x * DFM_PER_AP3X)
    evidence_hash = hashlib.blake2b(evidence.encode(), digest_size=32).hexdigest()

    try:
        challenger_hex = vi.normalize_agent_id(challenger_did).hex()
        target_hex = vi.normalize_agent_id(target_did).hex()
        tx_hash, _datum = client.mint_challenge(
            challenger_did=challenger_hex,
            target_did=target_hex,
            capability=capability,
            stake_amount=stake_dfm,
            evidence_hash=evidence_hash,
            evidence_uri=evidence[:256],
            controller_skey=controller_skey,
        )
        return ToolResult(
            success=True,
            data=add_did_fields({
                "tx_hash": tx_hash,
                "challenger_did": challenger_hex,
                "target_did": target_hex,
                "capability": capability,
                "stake_ap3x": stake_ap3x,
                "evidence_hash": evidence_hash,
            }),
        )
    except Exception as e:
        logger.exception("reputation_challenge failed")
        return ToolResult(success=False, error=str(e))
