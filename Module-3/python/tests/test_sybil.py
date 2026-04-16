"""Tests for sybil detection (endorsement graph analysis)."""

import pytest

from indexer.sybil import (
    SybilFlag,
    build_endorsement_graph,
    compute_cluster_scores,
    find_cycles,
)


class TestBuildEndorsementGraph:
    def test_empty(self):
        graph = build_endorsement_graph([])
        assert graph == {}

    def test_single_endorsement(self):
        endorsements = [{"endorser_did": "A", "target_did": "B"}]
        graph = build_endorsement_graph(endorsements)
        assert graph["A"] == {"B"}

    def test_multiple_endorsements(self):
        endorsements = [
            {"endorser_did": "A", "target_did": "B"},
            {"endorser_did": "A", "target_did": "C"},
            {"endorser_did": "B", "target_did": "C"},
        ]
        graph = build_endorsement_graph(endorsements)
        assert graph["A"] == {"B", "C"}
        assert graph["B"] == {"C"}


class TestFindCycles:
    def test_no_cycles(self):
        graph = {"A": {"B"}, "B": {"C"}}
        cycles = find_cycles(graph)
        assert cycles == []

    def test_simple_cycle(self):
        graph = {"A": {"B"}, "B": {"C"}, "C": {"A"}}
        cycles = find_cycles(graph)
        assert len(cycles) == 1
        assert len(cycles[0]) == 3
        # All three nodes should be in the cycle
        assert set(cycles[0]) == {"A", "B", "C"}

    def test_mutual_endorsement(self):
        graph = {"A": {"B"}, "B": {"A"}}
        cycles = find_cycles(graph)
        assert len(cycles) == 1
        assert len(cycles[0]) == 2

    def test_two_separate_cycles(self):
        graph = {
            "A": {"B"}, "B": {"A"},
            "C": {"D"}, "D": {"C"},
        }
        cycles = find_cycles(graph)
        assert len(cycles) == 2

    def test_max_length_respected(self):
        # A→B→C→D→E→A is length 5
        graph = {"A": {"B"}, "B": {"C"}, "C": {"D"}, "D": {"E"}, "E": {"A"}}
        cycles = find_cycles(graph, max_length=5)
        assert len(cycles) == 1

        # With max_length=3, shouldn't find this cycle
        cycles = find_cycles(graph, max_length=3)
        assert len(cycles) == 0


class TestClusterScores:
    def test_no_endorsements(self):
        scores = compute_cluster_scores({})
        assert scores == {}

    def test_single_endorser_clean(self):
        graph = {"A": {"B"}}
        scores = compute_cluster_scores(graph)
        # B has only one endorser, score should be 0
        assert scores.get("B", 0.0) == 0.0

    def test_independent_endorsers(self):
        # B and C both endorse A, but don't endorse each other
        graph = {"B": {"A"}, "C": {"A"}}
        scores = compute_cluster_scores(graph)
        assert scores["A"] == 0.0

    def test_mutual_endorsers(self):
        # B and C both endorse A, and B endorses C
        graph = {"B": {"A", "C"}, "C": {"A"}}
        scores = compute_cluster_scores(graph)
        assert scores["A"] == 1.0  # All endorser pairs are mutual

    def test_partial_mutual(self):
        # D, E, F endorse A. D↔E are mutual but F is independent
        graph = {"D": {"A", "E"}, "E": {"A", "D"}, "F": {"A"}}
        scores = compute_cluster_scores(graph)
        # 3 endorsers, 3 pairs: DE=mutual, DF=not, EF=not → 1/3
        assert abs(scores["A"] - 1 / 3) < 0.01
