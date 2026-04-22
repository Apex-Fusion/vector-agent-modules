"""
Construction-only tests for simulation.scenarios.slash_non_reveal
(iter-4 — SlashNonRevealScenario).

Live behaviour is exercised via the preflight harness and the
``_verify_lifecycle_slashnonreveal.py`` standalone script.

Unit-test scope (parallel to the WithdrawClaim + HappyPath constructors):

  - subclass inherits HappyPathScenario state correctly
  - initial lifecycle state "submit_claim"
  - vote_pattern overridden to alternating 0/1
  - checkpoint extension fields present + round-trip
  - decide_and_act_for_epoch stub-guard preserves construction contract
  - dispatch table declares every step name the subclass flow visits
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulation.scenarios.happy_path import (
    ROLE_CLAIMANT,
    ROLE_AUDITOR,
    ROLE_JUROR_PREFIX,
)
from simulation.scenarios.slash_non_reveal import (
    SlashNonRevealScenario,
    NON_REVEALER_JUROR_INDEX,
    RESOLUTION_DEADLINE_MS,
)


V15_DEPLOYMENT_PATH = Path(
    "/home/jelisaveta/.openclaw/workspace-apex/testnet/game1-sim-deployment.json"
)


@pytest.fixture
def fake_master_skey_bytes() -> bytes:
    return b"x" * 32


@pytest.fixture
def base_kwargs(tmp_path, fake_master_skey_bytes):
    if V15_DEPLOYMENT_PATH.exists():
        deployment = json.loads(V15_DEPLOYMENT_PATH.read_text())
    else:
        deployment = {
            "version": "v15-fallback",
            "claim_ref": "00" * 32 + "#0",
            "challenge_ref": "11" * 32 + "#0",
            "jury_pool_ref": "22" * 32 + "#0",
            "cross_refs_utxo": "33" * 32 + "#0",
            "params_utxo": "33" * 32 + "#1",
            "hashes": {
                "claim": "44" * 28,
                "challenge": "55" * 28,
                "jury_pool": "66" * 28,
            },
            "addresses": {
                "claim": "addr1_fake_claim",
                "challenge": "addr1_fake_challenge",
                "jury_pool": "addr1_fake_jury_pool",
            },
        }
    return dict(
        name="snr_test",
        config={"epochs_per_day": 24, "n_agents": 17},
        deployment=deployment,
        master_skey=fake_master_skey_bytes,
        master_vkey=b"vkey-stand-in",
        master_wallet_addr="addr1_master_fake",
        checkpoint_dir=tmp_path / "checkpoints",
        metrics_dir=tmp_path / "metrics",
        rng_seed=42,
    )


class TestConstruction:
    def test_subclass_instantiation(self, base_kwargs):
        s = SlashNonRevealScenario(**base_kwargs)
        assert s.name == "snr_test"
        assert s.rng_seed == 42

    def test_initial_lifecycle_state(self, base_kwargs):
        s = SlashNonRevealScenario(**base_kwargs)
        assert s._step == "submit_claim"
        assert s._slash_index == 0
        assert s._reset_index == 0
        assert s._timeout_resolve_done is False
        assert s._slashed_pool_index is None

    def test_jury_and_pool_sizes(self, base_kwargs):
        s = SlashNonRevealScenario(**base_kwargs, jury_size=5, pool_size=15)
        assert s.jury_size == 5
        assert s.pool_size == 15

    def test_wallet_roles_match_parent(self, base_kwargs):
        s = SlashNonRevealScenario(**base_kwargs, jury_size=5)
        assert ROLE_CLAIMANT in s._wallets
        assert ROLE_AUDITOR in s._wallets
        for i in range(5):
            assert f"{ROLE_JUROR_PREFIX}_{i}" in s._wallets

    def test_vote_pattern_is_alternating(self, base_kwargs):
        """The subclass overrides _build_vote_pattern to alternate 0/1
        across the 5 jurors so the 4 revealers that DO reveal split
        2/2 — leaving no natural majority. The 5th (non-revealer's) byte
        is never revealed but still drives the commit hash."""
        s = SlashNonRevealScenario(**base_kwargs, jury_size=5)
        # For jury_size=5 the pattern should be [0, 1, 0, 1, 0].
        assert s._vote_pattern == [0, 1, 0, 1, 0]

    def test_non_revealer_index_is_last(self, base_kwargs):
        # Ensure the constant is inside the valid range for the default
        # jury_size; the 5th juror (index 4) is the canonical non-revealer.
        s = SlashNonRevealScenario(**base_kwargs, jury_size=5)
        assert 0 <= NON_REVEALER_JUROR_INDEX < s.jury_size
        assert NON_REVEALER_JUROR_INDEX == 4

    def test_resolution_deadline_constant(self):
        # The subclass constant should be short-ish so timeout_resolve
        # is reachable within a live-testnet lifecycle budget (3-5 min
        # commit+reveal + slack).
        assert 60_000 <= RESOLUTION_DEADLINE_MS <= 600_000


class TestDecideAndActIsAbstract:
    def test_decide_and_act_raises_not_implemented(self, base_kwargs):
        s = SlashNonRevealScenario(**base_kwargs)
        with pytest.raises(NotImplementedError):
            s.decide_and_act_for_epoch(0)


class TestCheckpointExtension:
    """Slash-specific state must round-trip through the base checkpoint."""

    def test_payload_includes_subclass_fields(self, base_kwargs):
        s = SlashNonRevealScenario(**base_kwargs)
        payload = s._checkpoint_payload()
        assert "slash_index" in payload
        assert "reset_index" in payload
        assert "timeout_resolve_done" in payload
        assert "slashed_pool_index" in payload
        assert payload["slash_index"] == 0
        assert payload["reset_index"] == 0
        assert payload["timeout_resolve_done"] is False
        assert payload["slashed_pool_index"] is None

    def test_roundtrip_via_base_class_preserves_subclass_state(
        self, base_kwargs,
    ):
        s1 = SlashNonRevealScenario(**base_kwargs)
        s1._step = "timeout_resolve"
        s1._slash_index = 1
        s1._reset_index = 3
        s1._timeout_resolve_done = True
        s1._slashed_pool_index = 7
        s1.checkpoint()

        s2 = SlashNonRevealScenario(**base_kwargs)
        assert s2.restore() is True
        assert s2._step == "timeout_resolve"
        assert s2._slash_index == 1
        assert s2._reset_index == 3
        assert s2._timeout_resolve_done is True
        assert s2._slashed_pool_index == 7


class TestStepTransitions:
    """Validate every declared subclass step name is in the dispatch table."""

    def test_dispatch_known_steps_raises_not_implemented_on_stub(
        self, base_kwargs,
    ):
        s = SlashNonRevealScenario(**base_kwargs)
        s._agent_setup_done = True  # bypass setup gate
        for step in (
            "submit_claim",
            "open_challenge",
            "transition_to_voting",
            "select_jury",
            "commit_vote",
            "reveal_vote",
            "slash_non_reveal",
            "timeout_resolve",
            "reset_stale_active",
            "withdraw_jurors",
            "drain_to_master",
        ):
            s._step = step
            with pytest.raises(NotImplementedError):
                s.decide_and_act_for_epoch(0)
