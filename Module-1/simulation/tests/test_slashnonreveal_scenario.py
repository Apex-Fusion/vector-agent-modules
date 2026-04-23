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
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def fake_master_skey_bytes() -> bytes:
    return b"x" * 32


@pytest.fixture
def base_kwargs(tmp_path, fake_master_skey_bytes):
    # Self-contained stub deployment — tests never need a live manifest
    # since all chain interactions are mocked via OgmiosContext stubs.
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


# ---------------------------------------------------------------------------
# Regression tests: _step_timeout_resolve slot-boundary polling loop
#
# Bug: old code raised immediately when current_slot == deadline_slot
#      (strict inequality required by the on-chain validator).
# Fix: polls with wait_confirm(secs=5) until current_slot > deadline_slot,
#      with a 120s cap that raises TimeoutError if the chain tip stalls.
# ---------------------------------------------------------------------------

def _make_slot_counter(slots: list[int]):
    """Return a callable that pops the next slot from *slots* on each call
    and stashes the most-recently-returned value in .last_block_slot so
    callers that do ``ctx = OgmiosContext()`` get the updated slot."""
    class _SlotCtx:
        def __init__(self):
            self._queue = list(slots)
            self.last_block_slot = slots[0]

        def __call__(self):
            # Each OgmiosContext() construction consumes the next slot.
            if len(self._queue) > 1:
                self._queue.pop(0)
            self.last_block_slot = self._queue[0]
            return self

    return _SlotCtx()


def _challenge_cbor_stub(challenged_at_ms: int, resolution_deadline_ms: int):
    """Return a fake cbor2-decoded datum whose .value matches the field
    positions used in _step_timeout_resolve:
        value[6] = challenged_at_ms
        value[7] = resolution_deadline_ms
    """
    ns = SimpleNamespace()
    ns.value = {6: challenged_at_ms, 7: resolution_deadline_ms}
    return ns


class TestStepTimeoutResolvePollingLoop:
    """
    Unit tests for the slot-boundary polling loop introduced in
    _step_timeout_resolve (slash_non_reveal.py ~line 578-590).

    All network I/O is patched:
      - simulation.chain.OgmiosContext  → slot counter stub
      - simulation.chain.wait_confirm   → no-op (avoids real sleeps)
      - simulation.chain.resolve_utxo   → returns a fake UTxO datum
      - simulation.tx_builder.build_timeout_resolve → returns a fake result
      - time.sleep                       → no-op (coarse up-front sleep)
    """

    # ------------------------------------------------------------------
    # Shared setup helpers
    # ------------------------------------------------------------------

    SYSTEM_START = 1_666_656_000  # arbitrary fixed value matching chain.py

    def _make_scenario(self, base_kwargs) -> SlashNonRevealScenario:
        s = SlashNonRevealScenario(**base_kwargs)
        # Wire up just enough lifecycle state so _step_timeout_resolve runs.
        s._step = "timeout_resolve"
        s._challenge_ref = "aa" * 32 + "#0"
        s._claim_ref = "bb" * 32 + "#0"
        s._selected_pool_indices = list(range(5))
        s._agent_setup_done = True
        # Bypass the PaymentSigningKey guard — not relevant to slot logic.
        s._require_real_master_skey = lambda: None
        return s

    def _resolve_utxo_stub(self, challenged_at_ms: int,
                           resolution_deadline_ms: int):
        """Build a mock UTxO whose datum.cbor round-trips through cbor2."""
        import cbor2
        datum_obj = _challenge_cbor_stub(challenged_at_ms,
                                         resolution_deadline_ms)
        # Encode a minimal CBOR constr that cbor2.loads will give us back.
        # We re-encode the two fields at positions 6 and 7 inside a list.
        fields = [0] * 8
        fields[6] = challenged_at_ms
        fields[7] = resolution_deadline_ms
        cbor_bytes = cbor2.dumps(cbor2.CBORTag(121, fields))

        utxo = MagicMock()
        utxo.output.datum.cbor = cbor_bytes
        return utxo

    # ------------------------------------------------------------------
    # (a) WAITS when current_slot == deadline_slot
    # ------------------------------------------------------------------

    def test_waits_when_current_slot_equals_deadline_slot(
        self, base_kwargs,
    ):
        """Given current_slot == deadline_slot, the loop must poll at
        least once more before proceeding (not raise, not proceed early).
        """
        s = self._make_scenario(base_kwargs)

        # Pick a fixed system start and derive deadline slot.
        sys_start = self.SYSTEM_START
        challenged_at_ms = (sys_start + 1_000) * 1_000  # slot 1000 in ms
        resolution_deadline_ms = 450_000                  # +450 s

        deadline_ms = challenged_at_ms + resolution_deadline_ms
        deadline_slot = (deadline_ms // 1000) - sys_start  # == 1450

        # Slot sequence: [deadline_slot, deadline_slot+1]
        # First OgmiosContext() returns deadline_slot (boundary — must wait),
        # second returns deadline_slot+1 (OK to proceed).
        slot_counter = _make_slot_counter([deadline_slot, deadline_slot + 1])

        fake_build_result = {
            "tx_hash": "cc" * 32,
            "claim_stake_returned": 5_000_000,
            "auditor_stake_returned": 5_000_000,
        }
        resolve_utxo_stub = self._resolve_utxo_stub(
            challenged_at_ms, resolution_deadline_ms
        )
        wait_confirm_calls = []

        fake_ref_utxo = MagicMock()
        fake_ref_utxo.output.script = b"fake_script"

        with patch(
            "simulation.chain.OgmiosContext",
            side_effect=slot_counter,
        ), patch(
            "simulation.chain.wait_confirm",
            side_effect=lambda secs=5: wait_confirm_calls.append(secs),
        ), patch(
            "simulation.chain.resolve_utxo",
            return_value=resolve_utxo_stub,
        ), patch(
            "simulation.tx_builder.resolve_utxo",
            return_value=resolve_utxo_stub,
        ), patch(
            "simulation.tx_builder.resolve_ref_utxo",
            return_value=fake_ref_utxo,
        ), patch(
            "simulation.chain.SYSTEM_START_UNIX",
            new=sys_start,
        ), patch(
            "simulation.tx_builder.build_timeout_resolve",
            return_value=fake_build_result,
        ), patch(
            "time.sleep",
        ):
            events = s._step_timeout_resolve(epoch=1)

        # Must have polled at least once (the boundary wait).
        assert len(wait_confirm_calls) >= 1, (
            "Expected at least one wait_confirm poll when "
            "current_slot == deadline_slot"
        )
        # Must have produced a success event (not raised).
        event_types = [e["event_type"] for e in events]
        assert "timeout_resolve_success" in event_types
        assert s._timeout_resolve_done is True

    # ------------------------------------------------------------------
    # (b) PROCEEDS immediately when current_slot > deadline_slot
    # ------------------------------------------------------------------

    def test_proceeds_when_current_slot_exceeds_deadline_slot(
        self, base_kwargs,
    ):
        """Given current_slot > deadline_slot on the very first check,
        the loop must NOT poll at all and must proceed directly to
        build_timeout_resolve.
        """
        s = self._make_scenario(base_kwargs)

        sys_start = self.SYSTEM_START
        challenged_at_ms = (sys_start + 1_000) * 1_000
        resolution_deadline_ms = 450_000

        deadline_ms = challenged_at_ms + resolution_deadline_ms
        deadline_slot = (deadline_ms // 1000) - sys_start  # == 1450

        # First (and only) slot is already PAST the deadline.
        slot_counter = _make_slot_counter([deadline_slot + 1])

        fake_build_result = {
            "tx_hash": "dd" * 32,
            "claim_stake_returned": 5_000_000,
            "auditor_stake_returned": 5_000_000,
        }
        resolve_utxo_stub = self._resolve_utxo_stub(
            challenged_at_ms, resolution_deadline_ms
        )
        extra_wait_confirm_calls = []

        # We track wait_confirm calls that happen INSIDE the polling loop
        # by counting all calls after the pre-sleep resolves.
        fake_ref_utxo = MagicMock()
        fake_ref_utxo.output.script = b"fake_script"

        with patch(
            "simulation.chain.OgmiosContext",
            side_effect=slot_counter,
        ), patch(
            "simulation.chain.wait_confirm",
            side_effect=lambda secs=5: extra_wait_confirm_calls.append(secs),
        ), patch(
            "simulation.chain.resolve_utxo",
            return_value=resolve_utxo_stub,
        ), patch(
            "simulation.tx_builder.resolve_utxo",
            return_value=resolve_utxo_stub,
        ), patch(
            "simulation.tx_builder.resolve_ref_utxo",
            return_value=fake_ref_utxo,
        ), patch(
            "simulation.chain.SYSTEM_START_UNIX",
            new=sys_start,
        ), patch(
            "simulation.tx_builder.build_timeout_resolve",
            return_value=fake_build_result,
        ), patch(
            "time.sleep",
        ):
            events = s._step_timeout_resolve(epoch=1)

        # The ONLY wait_confirm calls should be the post-build confirm
        # (WAIT_CONFIRM_SECS), NOT any from the polling loop (secs=5).
        loop_polls = [c for c in extra_wait_confirm_calls if c == 5]
        assert loop_polls == [], (
            "No polling wait_confirm(secs=5) calls expected when "
            "current_slot > deadline_slot from the start"
        )
        event_types = [e["event_type"] for e in events]
        assert "timeout_resolve_success" in event_types
        assert s._timeout_resolve_done is True

    # ------------------------------------------------------------------
    # (c) Raises TimeoutError after 120s simulated cap
    # ------------------------------------------------------------------

    def test_raises_timeout_error_after_120s_cap(
        self, base_kwargs,
    ):
        """If the chain tip never advances past deadline_slot within
        120s (24 × 5s polls), the loop must raise TimeoutError.
        """
        s = self._make_scenario(base_kwargs)

        sys_start = self.SYSTEM_START
        challenged_at_ms = (sys_start + 1_000) * 1_000
        resolution_deadline_ms = 450_000

        deadline_ms = challenged_at_ms + resolution_deadline_ms
        deadline_slot = (deadline_ms // 1000) - sys_start

        # Slot never advances — always at deadline_slot (== boundary).
        # Provide enough entries to satisfy 24+ constructor calls.
        slot_counter = _make_slot_counter([deadline_slot] * 50)

        resolve_utxo_stub = self._resolve_utxo_stub(
            challenged_at_ms, resolution_deadline_ms
        )

        fake_ref_utxo = MagicMock()
        fake_ref_utxo.output.script = b"fake_script"

        with patch(
            "simulation.chain.OgmiosContext",
            side_effect=slot_counter,
        ), patch(
            "simulation.chain.wait_confirm",
        ), patch(
            "simulation.chain.resolve_utxo",
            return_value=resolve_utxo_stub,
        ), patch(
            "simulation.tx_builder.resolve_utxo",
            return_value=resolve_utxo_stub,
        ), patch(
            "simulation.tx_builder.resolve_ref_utxo",
            return_value=fake_ref_utxo,
        ), patch(
            "simulation.chain.SYSTEM_START_UNIX",
            new=sys_start,
        ), patch(
            "time.sleep",
        ):
            with pytest.raises(TimeoutError) as exc_info:
                s._step_timeout_resolve(epoch=1)

        assert "timeout_resolve" in str(exc_info.value).lower()
        assert "deadline_slot" in str(exc_info.value) or str(deadline_slot) in str(exc_info.value)
