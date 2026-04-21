"""
Sybil detection for the reputation indexer.

Implements endorsement graph analysis from impl spec Section 9.1:
  1. Cycle detection: A→B→C→A endorsement rings
  2. Cluster flagging: >50% endorsements from mutual endorsers
  3. Sybil score: 0.0 (clean) to 1.0 (highly suspicious)

The sybil analysis runs after each indexer poll and stores flags
in the SQLite database for API consumption.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SybilFlag:
    """A sybil detection flag for an agent."""
    agent_did: str
    flag_type: str  # "cycle", "cluster", "provenance"
    severity: float  # 0.0 to 1.0
    details: str
    related_dids: list


def build_endorsement_graph(
    endorsements: list,
) -> Dict[str, Set[str]]:
    """Build a directed endorsement graph from DB rows.

    Args:
        endorsements: List of dicts with endorser_did, target_did.

    Returns:
        Adjacency list: {endorser_did: {target_did, ...}}
    """
    graph: Dict[str, Set[str]] = defaultdict(set)
    for e in endorsements:
        graph[e["endorser_did"]].add(e["target_did"])
    return graph


def find_cycles(graph: Dict[str, Set[str]], max_length: int = 5) -> List[List[str]]:
    """Find all cycles up to max_length in the endorsement graph.

    Uses DFS-based cycle detection. Returns list of cycles,
    where each cycle is a list of DIDs forming the ring.

    Args:
        graph: Adjacency list from build_endorsement_graph.
        max_length: Maximum cycle length to detect (default 5).

    Returns:
        List of cycles (each cycle is a list of DID strings).
    """
    cycles = []
    visited_global = set()

    for start in graph:
        if start in visited_global:
            continue

        # DFS from this node
        stack = [(start, [start])]
        visited_local = {start}

        while stack:
            node, path = stack.pop()

            for neighbor in graph.get(node, set()):
                if neighbor == start and len(path) >= 2:
                    cycles.append(path + [start])
                elif neighbor not in visited_local and len(path) < max_length:
                    visited_local.add(neighbor)
                    stack.append((neighbor, path + [neighbor]))

        visited_global.add(start)

    # Deduplicate: normalize cycles by rotating to start with smallest DID
    unique = set()
    result = []
    for cycle in cycles:
        # Remove the duplicate end node
        ring = cycle[:-1]
        if len(ring) < 2:
            continue
        # Normalize: rotate so smallest DID is first
        min_idx = ring.index(min(ring))
        normalized = tuple(ring[min_idx:] + ring[:min_idx])
        if normalized not in unique:
            unique.add(normalized)
            result.append(list(normalized))

    return result


def compute_cluster_scores(
    graph: Dict[str, Set[str]],
) -> Dict[str, float]:
    """Compute cluster suspicion scores for each agent.

    An agent's cluster score = fraction of their endorsers that
    also endorse each other (mutual endorsement density).

    Score > 0.5 indicates potential sybil cluster.

    Args:
        graph: Adjacency list from build_endorsement_graph.

    Returns:
        {agent_did: cluster_score} where 0.0 = clean, 1.0 = all mutual.
    """
    # Build reverse graph (who endorses whom)
    reverse_graph: Dict[str, Set[str]] = defaultdict(set)
    for endorser, targets in graph.items():
        for target in targets:
            reverse_graph[target].add(endorser)

    scores: Dict[str, float] = {}

    for agent, endorsers in reverse_graph.items():
        if len(endorsers) < 2:
            scores[agent] = 0.0
            continue

        # Count mutual endorsement pairs among this agent's endorsers
        endorser_list = list(endorsers)
        mutual_pairs = 0
        total_pairs = 0

        for i in range(len(endorser_list)):
            for j in range(i + 1, len(endorser_list)):
                total_pairs += 1
                a, b = endorser_list[i], endorser_list[j]
                # Check if a endorses b or b endorses a
                if b in graph.get(a, set()) or a in graph.get(b, set()):
                    mutual_pairs += 1

        scores[agent] = mutual_pairs / total_pairs if total_pairs > 0 else 0.0

    return scores


def analyze_sybil(storage) -> List[SybilFlag]:
    """Run full sybil analysis on indexed data.

    Args:
        storage: IndexerStorage instance with endorsement data.

    Returns:
        List of SybilFlag objects for flagged agents.
    """
    # Get all endorsements
    all_endorsements = storage.conn.execute(
        "SELECT endorser_did, target_did, stake_amount FROM endorsements"
    ).fetchall()
    all_endorsements = [dict(e) for e in all_endorsements]

    if not all_endorsements:
        return []

    graph = build_endorsement_graph(all_endorsements)
    flags: List[SybilFlag] = []

    # 1. Cycle detection
    cycles = find_cycles(graph)
    cycle_members = set()
    for cycle in cycles:
        for did in cycle:
            cycle_members.add(did)
            flags.append(SybilFlag(
                agent_did=did,
                flag_type="cycle",
                severity=min(1.0, 0.3 + 0.2 * (len(cycles))),
                details=f"Part of endorsement ring: {' → '.join(d[:12] + '...' for d in cycle)}",
                related_dids=[d for d in cycle if d != did],
            ))

    # 2. Cluster analysis
    cluster_scores = compute_cluster_scores(graph)
    for agent, score in cluster_scores.items():
        if score > 0.5:
            # Build reverse graph to find endorsers
            endorsers = [
                e["endorser_did"] for e in all_endorsements
                if e["target_did"] == agent
            ]
            flags.append(SybilFlag(
                agent_did=agent,
                flag_type="cluster",
                severity=score,
                details=(
                    f"Cluster score {score:.2f}: "
                    f"{int(score * 100)}% of endorsers mutually endorse each other"
                ),
                related_dids=endorsers,
            ))

    # Store flags
    storage.clear_sybil_flags()
    for flag in flags:
        storage.upsert_sybil_flag(
            agent_did=flag.agent_did,
            flag_type=flag.flag_type,
            severity=flag.severity,
            details=flag.details,
            related_dids=",".join(flag.related_dids),
        )

    logger.info(
        "Sybil analysis: %d flags across %d agents, %d cycles detected",
        len(flags),
        len(set(f.agent_did for f in flags)),
        len(cycles),
    )

    return flags
