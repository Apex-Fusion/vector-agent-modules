"""
RED tests for simulation.orchestrator.SimOrchestrator (Sim Phase 2 iter 2).

Claire — test-engineer pass.

These tests drive the design of the concurrent lifecycle manager that owns
multiple ScenarioRunner instances, runs each in its own daemon thread, and
exposes a unified status / stop / wait API.

Test doubles use a real ``ScenarioRunner`` subclass (``_FastMockScenario``
and friends below) — we deliberately avoid mocking ``ScenarioRunner`` itself
because the whole point is concurrent integration with the iter-1 contract.

All tests should fail with ``NotImplementedError`` (from the skeleton stubs)
in this RED phase. Catherine fills in the bodies in
``simulation/orchestrator.py`` during GREEN.
"""

from __future__ import annotations

import json
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

# Module file exists; method bodies raise NotImplementedError — that is the
# desired RED failure mode.
from simulation.orchestrator import SimOrchestrator
from simulation.scenario import ScenarioRunner


# ═══════════════════════════════════════════════════════════════════════
# Test doubles / fixtures
# ═══════════════════════════════════════════════════════════════════════


def _fake_deployment() -> Any:
    return types.SimpleNamespace(
        claim_hash="cc" * 28,
        challenge_hash="dd" * 28,
        jury_pool_hash="ee" * 28,
    )


def _scenario_kwargs(tmp_path: Path, name: str, rng_seed: int = 0) -> dict:
    """Per-scenario constructor kwargs. Each scenario gets its OWN dirs so
    that file isolation between scenarios is exercised (mirrors iter-1 style).
    """
    return dict(
        name=name,
        config={"epochs_per_day": 24, "n_agents": 1},
        deployment=_fake_deployment(),
        master_skey="<fake_skey>",
        master_vkey="<fake_vkey>",
        master_wallet_addr="addr1_fake",
        checkpoint_dir=tmp_path / "checkpoints",
        metrics_dir=tmp_path / "metrics",
        rng_seed=rng_seed,
    )


def _orch_dirs(tmp_path: Path) -> dict:
    """Per-orchestrator dirs (separate from any scenario's dirs)."""
    return dict(
        checkpoint_dir=tmp_path / "orch_checkpoints",
        metrics_dir=tmp_path / "orch_metrics",
    )


class _FastMockScenario(ScenarioRunner):
    """Minimal scenario that does a tiny sleep per epoch so we can observe
    concurrency, then returns a single epoch_tick event."""

    def __init__(self, *args, **kwargs):
        # Test-only switches consumed before delegating to the base init.
        self._sleep_per_epoch: float = kwargs.pop("sleep_per_epoch", 0.005)
        self._raise_on_epoch: int | None = kwargs.pop("raise_on_epoch", None)
        self._raise_immediately: bool = kwargs.pop("raise_immediately", False)
        super().__init__(*args, **kwargs)
        self.calls: list[int] = []

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        if self._raise_immediately and epoch == 0:
            raise RuntimeError("immediate boom")
        if self._raise_on_epoch is not None and epoch == self._raise_on_epoch:
            raise RuntimeError(f"boom at epoch {epoch}")
        if self._sleep_per_epoch > 0:
            time.sleep(self._sleep_per_epoch)
        self.calls.append(epoch)
        return [{"event_type": "epoch_tick", "epoch": epoch}]


class _BlockingScenario(ScenarioRunner):
    """Scenario that blocks each epoch until told to release. Used for
    thread-state assertions ("alive while running")."""

    def __init__(self, *args, **kwargs):
        self._release: threading.Event = kwargs.pop("release_event")
        super().__init__(*args, **kwargs)

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        # Wait until test releases us (or stop_event fires via the run loop's
        # post-epoch check). Use a timeout so a stuck test doesn't hang
        # forever.
        self._release.wait(timeout=5.0)
        return [{"event_type": "tick", "epoch": epoch}]


# ═══════════════════════════════════════════════════════════════════════
# Construction
# ═══════════════════════════════════════════════════════════════════════


class TestConstruction:
    def test_construct_with_default_stop_event(self, tmp_path):
        """If no stop_event is passed, the orchestrator creates its own."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        assert isinstance(orch.stop_event, threading.Event)
        assert not orch.stop_event.is_set()

    def test_construct_with_provided_stop_event(self, tmp_path):
        """A user-provided stop_event must be used as-is (same instance)."""
        ev = threading.Event()
        orch = SimOrchestrator(stop_event=ev, **_orch_dirs(tmp_path))
        assert orch.stop_event is ev

    def test_construct_creates_orchestrator_dirs(self, tmp_path):
        """checkpoint_dir and metrics_dir should be created if missing."""
        d = _orch_dirs(tmp_path)
        assert not d["checkpoint_dir"].exists()
        assert not d["metrics_dir"].exists()
        SimOrchestrator(**d)
        assert d["checkpoint_dir"].is_dir()
        assert d["metrics_dir"].is_dir()

    def test_construct_no_scenarios_registered(self, tmp_path):
        """Status of a fresh orchestrator returns an empty dict."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        assert orch.status() == {}


# ═══════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════


class TestRegistration:
    def test_register_single_scenario(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = _FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha"))
        orch.register(s)
        st = orch.status()
        assert "alpha" in st
        assert st["alpha"]["alive"] is False
        assert st["alpha"]["epoch"] == -1
        assert st["alpha"]["error"] is None

    def test_register_multiple_scenarios(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("alpha", "beta", "gamma"):
            orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / nm, nm)))
        st = orch.status()
        assert set(st.keys()) == {"alpha", "beta", "gamma"}

    def test_register_duplicate_name_raises_value_error(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        a1 = _FastMockScenario(**_scenario_kwargs(tmp_path / "a1", "alpha"))
        a2 = _FastMockScenario(**_scenario_kwargs(tmp_path / "a2", "alpha"))
        orch.register(a1)
        with pytest.raises(ValueError):
            orch.register(a2)

    def test_register_after_start_raises_runtime_error(self, tmp_path):
        """Once start_all has been called, no further registration is allowed."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = _FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha"))
        orch.register(s)
        orch.start_all(n_epochs=1)
        try:
            with pytest.raises(RuntimeError):
                orch.register(
                    _FastMockScenario(**_scenario_kwargs(tmp_path / "b", "beta"))
                )
        finally:
            orch.stop_all(timeout=5.0)


# ═══════════════════════════════════════════════════════════════════════
# start_all / non-blocking semantics
# ═══════════════════════════════════════════════════════════════════════


class TestStartAll:
    def test_start_all_is_non_blocking(self, tmp_path):
        """start_all must return promptly even when scenarios run for a while."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = _FastMockScenario(
            sleep_per_epoch=0.05,
            **_scenario_kwargs(tmp_path / "a", "alpha"),
        )
        orch.register(s)
        t0 = time.monotonic()
        orch.start_all(n_epochs=20)  # ~1s of work if blocking
        elapsed = time.monotonic() - t0
        try:
            # Give a generous bound — should be near-instant (well under 0.5s).
            assert elapsed < 0.5, f"start_all blocked for {elapsed:.3f}s"
        finally:
            orch.stop_all(timeout=10.0)

    def test_start_all_spawns_one_thread_per_scenario(self, tmp_path):
        """Each registered scenario runs in its own thread (alive=True after start)."""
        release = threading.Event()
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b", "c"):
            orch.register(
                _BlockingScenario(
                    release_event=release,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=1)
        try:
            # Threads are stuck inside decide_and_act waiting on release.
            # Allow a short window for them to enter the call.
            time.sleep(0.05)
            st = orch.status()
            assert all(st[nm]["alive"] is True for nm in ("a", "b", "c"))
        finally:
            release.set()
            orch.stop_all(timeout=5.0)

    def test_start_all_twice_raises_runtime_error(self, tmp_path):
        """The orchestrator is single-shot per lifecycle."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))
        orch.start_all(n_epochs=1)
        try:
            with pytest.raises(RuntimeError):
                orch.start_all(n_epochs=1)
        finally:
            orch.stop_all(timeout=5.0)

    def test_start_all_with_no_scenarios_is_a_noop(self, tmp_path):
        """start_all with zero registered scenarios should not raise."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.start_all(n_epochs=1)  # should not raise
        # And wait should return True immediately (nothing to wait on).
        assert orch.wait(timeout=1.0) is True

    def test_start_all_uses_orchestrators_stop_event(self, tmp_path):
        """When stop_event fires, scenarios receive it via the run loop."""
        ev = threading.Event()
        orch = SimOrchestrator(stop_event=ev, **_orch_dirs(tmp_path))
        s = _FastMockScenario(
            sleep_per_epoch=0.005,
            **_scenario_kwargs(tmp_path / "a", "alpha"),
        )
        orch.register(s)
        orch.start_all(n_epochs=10_000)  # would take far longer than the test
        time.sleep(0.05)
        ev.set()
        # All threads should drain quickly after the shared event fires.
        finished = orch.wait(timeout=5.0)
        assert finished is True
        st = orch.status()
        assert st["alpha"]["alive"] is False


# ═══════════════════════════════════════════════════════════════════════
# Threads run concurrently and stay daemon
# ═══════════════════════════════════════════════════════════════════════


class TestThreadProperties:
    def test_threads_are_daemon(self, tmp_path):
        """Daemon=True ensures a runaway sim never blocks process exit."""
        release = threading.Event()
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _BlockingScenario(
                release_event=release,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=1)
        try:
            # Give the thread time to start.
            time.sleep(0.05)
            # threading.enumerate() includes only currently-alive threads.
            sim_threads = [
                t for t in threading.enumerate() if t.daemon and t.is_alive()
            ]
            assert len(sim_threads) >= 1
        finally:
            release.set()
            orch.stop_all(timeout=5.0)

    def test_scenarios_run_concurrently(self, tmp_path):
        """Two scenarios that each take ~0.1s should finish in well under 0.2s
        if running concurrently (sequential would take ≥ 0.2s)."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.1,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        t0 = time.monotonic()
        orch.start_all(n_epochs=1)
        ok = orch.wait(timeout=5.0)
        elapsed = time.monotonic() - t0
        assert ok is True
        # Concurrent: ~0.1s. Sequential: ~0.2s. Leave plenty of slack.
        assert elapsed < 0.18, f"scenarios not concurrent: elapsed={elapsed:.3f}s"


# ═══════════════════════════════════════════════════════════════════════
# Isolation between scenarios
# ═══════════════════════════════════════════════════════════════════════


class TestIsolation:
    def test_one_scenario_crash_does_not_affect_siblings(self, tmp_path):
        """Scenario 'crashed' raises immediately; sibling 'ok' must complete."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        crashed = _FastMockScenario(
            raise_immediately=True,
            **_scenario_kwargs(tmp_path / "crashed", "crashed"),
        )
        ok = _FastMockScenario(
            sleep_per_epoch=0.005,
            **_scenario_kwargs(tmp_path / "ok", "ok"),
        )
        orch.register(crashed)
        orch.register(ok)
        orch.start_all(n_epochs=3)
        finished = orch.wait(timeout=5.0)
        assert finished is True
        st = orch.status()
        # Crashed scenario terminated via scenario_error
        assert st["crashed"]["alive"] is False
        assert isinstance(st["crashed"]["error"], dict)
        assert st["crashed"]["error"]["class"] == "RuntimeError"
        assert "immediate boom" in st["crashed"]["error"]["message"]
        # Sibling completed all epochs
        assert st["ok"]["alive"] is False
        assert st["ok"]["error"] is None
        assert st["ok"]["epoch"] == 2

    def test_each_scenario_writes_its_own_metrics_file(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("alpha", "beta"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.001,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=5.0) is True
        alpha_metrics = tmp_path / "alpha" / "metrics" / "alpha.jsonl"
        beta_metrics = tmp_path / "beta" / "metrics" / "beta.jsonl"
        assert alpha_metrics.exists()
        assert beta_metrics.exists()
        # No cross-contamination: every event in alpha.jsonl carries scenario=alpha.
        for ln in alpha_metrics.read_text().strip().splitlines():
            assert json.loads(ln)["scenario"] == "alpha"
        for ln in beta_metrics.read_text().strip().splitlines():
            assert json.loads(ln)["scenario"] == "beta"

    def test_each_scenario_writes_its_own_checkpoint_file(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("alpha", "beta"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.001,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=5.0) is True
        assert (tmp_path / "alpha" / "checkpoints" / "alpha.json").exists()
        assert (tmp_path / "beta" / "checkpoints" / "beta.json").exists()


# ═══════════════════════════════════════════════════════════════════════
# Stop / shutdown semantics
# ═══════════════════════════════════════════════════════════════════════


class TestStopAll:
    def test_stop_all_sets_stop_event(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))
        orch.start_all(n_epochs=1)
        orch.stop_all(timeout=5.0)
        assert orch.stop_event.is_set()

    def test_stop_all_joins_threads(self, tmp_path):
        """After stop_all returns within timeout, all threads must be dead."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = _FastMockScenario(
            sleep_per_epoch=0.005,
            **_scenario_kwargs(tmp_path / "a", "alpha"),
        )
        orch.register(s)
        orch.start_all(n_epochs=20)
        orch.stop_all(timeout=5.0)
        st = orch.status()
        assert st["alpha"]["alive"] is False

    def test_stop_all_is_idempotent(self, tmp_path):
        """Calling stop_all twice must not raise."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))
        orch.start_all(n_epochs=1)
        orch.stop_all(timeout=5.0)
        orch.stop_all(timeout=5.0)  # should not raise

    def test_stop_all_before_start_is_noop(self, tmp_path):
        """stop_all without prior start_all must not raise."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))
        orch.stop_all(timeout=1.0)  # should not raise
        # Stop event may or may not be set — but no thread should have been spawned.
        st = orch.status()
        assert st["alpha"]["alive"] is False


# ═══════════════════════════════════════════════════════════════════════
# wait() blocking primitive
# ═══════════════════════════════════════════════════════════════════════


class TestWait:
    def test_wait_returns_true_when_all_complete(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=3)
        assert orch.wait(timeout=5.0) is True

    def test_wait_returns_false_on_timeout(self, tmp_path):
        """If a scenario is still alive when the timeout elapses, wait
        returns False (and does NOT signal stop_event)."""
        release = threading.Event()
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _BlockingScenario(
                release_event=release,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=1)
        try:
            t0 = time.monotonic()
            ok = orch.wait(timeout=0.1)
            elapsed = time.monotonic() - t0
            assert ok is False
            # Roughly respects the timeout (not absurdly long).
            assert elapsed < 1.0
            # wait() must NOT set the stop_event; only stop_all does.
            assert not orch.stop_event.is_set()
        finally:
            release.set()
            orch.stop_all(timeout=5.0)

    def test_wait_with_no_threads_returns_true_immediately(self, tmp_path):
        """If start_all has not been called (or no scenarios), wait is a no-op."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        t0 = time.monotonic()
        ok = orch.wait(timeout=2.0)
        elapsed = time.monotonic() - t0
        assert ok is True
        assert elapsed < 0.5

    def test_wait_none_timeout_blocks_until_complete(self, tmp_path):
        """wait(timeout=None) joins until done — must return True for clean runs."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=None) is True


# ═══════════════════════════════════════════════════════════════════════
# status()
# ═══════════════════════════════════════════════════════════════════════


class TestStatus:
    def test_status_alive_true_while_running(self, tmp_path):
        release = threading.Event()
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _BlockingScenario(
                release_event=release,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=1)
        try:
            time.sleep(0.05)
            assert orch.status()["alpha"]["alive"] is True
        finally:
            release.set()
            orch.stop_all(timeout=5.0)

    def test_status_alive_false_after_completion(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=5.0) is True
        assert orch.status()["alpha"]["alive"] is False

    def test_status_epoch_reflects_last_completed(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=4)
        assert orch.wait(timeout=5.0) is True
        assert orch.status()["alpha"]["epoch"] == 3

    def test_status_error_none_for_clean_completion(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=1)
        assert orch.wait(timeout=5.0) is True
        assert orch.status()["alpha"]["error"] is None

    def test_status_error_populated_when_scenario_raises(self, tmp_path):
        """A scenario that raises mid-epoch should surface its error in status()."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                raise_on_epoch=1,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=5)
        assert orch.wait(timeout=5.0) is True
        st = orch.status()
        assert isinstance(st["alpha"]["error"], dict)
        assert st["alpha"]["error"]["class"] == "RuntimeError"
        assert st["alpha"]["error"]["message"] == "boom at epoch 1"
        # No legacy "<Class>: <msg>" colon-string form sneaks in.
        assert ":" not in st["alpha"]["error"]["class"]

    def test_status_keys_match_registered_names(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        names = ("a", "b", "c", "d")
        for nm in names:
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.001,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        st = orch.status()
        assert set(st.keys()) == set(names)


# ═══════════════════════════════════════════════════════════════════════
# Mixed scenarios & edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_scenarios_with_different_epoch_counts_all_complete(self, tmp_path):
        """Every scenario thread receives the same n_epochs from start_all,
        but their per-epoch durations differ. All must finish cleanly."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "fast", "fast"),
            )
        )
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.02,
                **_scenario_kwargs(tmp_path / "slow", "slow"),
            )
        )
        orch.start_all(n_epochs=3)
        assert orch.wait(timeout=10.0) is True
        st = orch.status()
        assert st["fast"]["epoch"] == 2
        assert st["slow"]["epoch"] == 2

    def test_scenario_that_raises_immediately_terminates_quickly(self, tmp_path):
        """A scenario that raises on epoch 0 should be reaped within the
        wait() timeout without hanging."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                raise_immediately=True,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=100)
        t0 = time.monotonic()
        ok = orch.wait(timeout=5.0)
        elapsed = time.monotonic() - t0
        assert ok is True
        assert elapsed < 1.0
        st = orch.status()
        assert st["alpha"]["alive"] is False
        assert isinstance(st["alpha"]["error"], dict)
        assert st["alpha"]["error"]["class"] == "RuntimeError"

    def test_stop_event_propagation_halts_long_running_scenario(self, tmp_path):
        """Setting stop_event externally should halt a scenario that would
        otherwise run for many epochs — at the next epoch boundary."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.005,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=10_000)
        time.sleep(0.05)
        orch.stop_event.set()
        finished = orch.wait(timeout=5.0)
        assert finished is True
        st = orch.status()
        assert st["alpha"]["alive"] is False
        # We got a few epochs in but NOT all 10_000.
        assert 0 <= st["alpha"]["epoch"] < 10_000

    def test_many_scenarios_concurrent(self, tmp_path):
        """Scale check: 10 scenarios all run and complete cleanly."""
        N = 10
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for i in range(N):
            nm = f"s{i:02d}"
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.005,
                    **_scenario_kwargs(tmp_path / nm, nm, rng_seed=i),
                )
            )
        orch.start_all(n_epochs=3)
        assert orch.wait(timeout=10.0) is True
        st = orch.status()
        assert len(st) == N
        for i in range(N):
            nm = f"s{i:02d}"
            assert st[nm]["alive"] is False
            assert st[nm]["error"] is None
            assert st[nm]["epoch"] == 2

    def test_status_callable_during_run(self, tmp_path):
        """status() must be safe to call mid-run (no race that crashes)."""
        release = threading.Event()
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b"):
            orch.register(
                _BlockingScenario(
                    release_event=release,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=1)
        try:
            time.sleep(0.05)
            for _ in range(5):
                st = orch.status()
                assert "a" in st and "b" in st
        finally:
            release.set()
            orch.stop_all(timeout=5.0)


# ═══════════════════════════════════════════════════════════════════════
# orchestrator_status.json aggregate snapshot (Chuck-locked, iter 2)
# ═══════════════════════════════════════════════════════════════════════


def _status_path(tmp_path: Path) -> Path:
    """Resolved location of the aggregate snapshot for an orchestrator
    constructed via ``_orch_dirs(tmp_path)``."""
    return tmp_path / "orch_checkpoints" / "orchestrator_status.json"


def _is_iso8601_z(ts: str) -> bool:
    """Strict-ish check: ISO-8601 UTC ending in 'Z' and parseable by
    ``datetime.fromisoformat`` after stripping the Z."""
    from datetime import datetime

    if not isinstance(ts, str) or not ts.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(ts[:-1])
    except ValueError:
        return False
    return True


class TestOrchestratorStatusFile:
    def test_status_file_appears_after_first_status_call(self, tmp_path):
        """Calling status() — even before start_all — must produce the file."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        path = _status_path(tmp_path)
        assert not path.exists()
        orch.status()
        assert path.is_file()

    def test_status_file_payload_shape(self, tmp_path):
        """Top-level keys are exactly {'scenarios', 'ts'}; scenarios is a dict;
        ts is an ISO-8601 UTC string ending in 'Z'."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("alpha", "beta"):
            orch.register(
                _FastMockScenario(**_scenario_kwargs(tmp_path / nm, nm))
            )
        orch.status()
        payload = json.loads(_status_path(tmp_path).read_text())
        assert set(payload.keys()) == {"scenarios", "ts"}
        assert isinstance(payload["scenarios"], dict)
        assert set(payload["scenarios"].keys()) == {"alpha", "beta"}
        for nm in ("alpha", "beta"):
            entry = payload["scenarios"][nm]
            assert set(entry.keys()) == {"alive", "epoch", "error"}
            assert entry["alive"] is False
            assert entry["epoch"] == -1
            assert entry["error"] is None
        assert _is_iso8601_z(payload["ts"]), payload["ts"]

    def test_status_file_before_start_has_empty_scenarios_when_none_registered(
        self, tmp_path
    ):
        """status() before start_all and with zero scenarios still writes a
        valid snapshot (empty 'scenarios' dict, valid ts)."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.status()
        payload = json.loads(_status_path(tmp_path).read_text())
        assert payload["scenarios"] == {}
        assert _is_iso8601_z(payload["ts"])

    def test_status_file_atomic_no_tmp_left_behind(self, tmp_path):
        """Successful writes leave no orchestrator_status.json.tmp residue."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))
        for _ in range(5):
            orch.status()
        ckpt_dir = tmp_path / "orch_checkpoints"
        assert (ckpt_dir / "orchestrator_status.json").is_file()
        residues = list(ckpt_dir.glob("orchestrator_status.json.tmp"))
        assert residues == [], f"leftover tmp files: {residues}"

    def test_status_file_mutates_across_lifecycle_phases(self, tmp_path):
        """Snapshot reflects phase transitions: pre-start → running → post-join,
        with epoch advancing and alive flipping."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        # Pre-start snapshot.
        orch.status()
        pre = json.loads(_status_path(tmp_path).read_text())
        assert pre["scenarios"]["alpha"]["alive"] is False
        assert pre["scenarios"]["alpha"]["epoch"] == -1
        # Run to completion, then snapshot again.
        orch.start_all(n_epochs=3)
        assert orch.wait(timeout=5.0) is True
        orch.status()
        post = json.loads(_status_path(tmp_path).read_text())
        assert post["scenarios"]["alpha"]["alive"] is False
        assert post["scenarios"]["alpha"]["epoch"] == 2
        assert post["scenarios"]["alpha"]["error"] is None
        # ts must be monotonically newer (string compare works for ISO-8601 Z).
        assert post["ts"] >= pre["ts"]

    def test_status_file_error_field_is_dict_after_crash(self, tmp_path):
        """After a scenario_error, the snapshot's error field is the same
        {'class', 'message'} dict shape returned by status()."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                raise_on_epoch=0,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=5.0) is True
        orch.status()
        payload = json.loads(_status_path(tmp_path).read_text())
        err = payload["scenarios"]["alpha"]["error"]
        assert isinstance(err, dict)
        assert err["class"] == "RuntimeError"
        assert "boom at epoch 0" in err["message"]

    def test_stop_all_writes_final_snapshot(self, tmp_path):
        """stop_all must refresh the snapshot before returning."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.001,
                **_scenario_kwargs(tmp_path / "a", "alpha"),
            )
        )
        # Note: we deliberately do NOT call status() here. The snapshot must
        # exist purely as a side effect of stop_all().
        orch.start_all(n_epochs=2)
        orch.stop_all(timeout=5.0)
        path = _status_path(tmp_path)
        assert path.is_file()
        payload = json.loads(path.read_text())
        # Final snapshot reflects post-join state.
        assert payload["scenarios"]["alpha"]["alive"] is False
        assert _is_iso8601_z(payload["ts"])

    def test_concurrent_status_calls_do_not_corrupt_file(self, tmp_path):
        """Multiple threads hammering status() must never produce a partial
        write. Every read of the file must yield a parseable JSON payload
        with the expected top-level keys."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b", "c"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.001,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        path = _status_path(tmp_path)

        errors: list[str] = []
        stop = threading.Event()

        def hammer_status():
            while not stop.is_set():
                try:
                    orch.status()
                except Exception as e:
                    errors.append(f"status raised: {e!r}")
                    return

        def hammer_read():
            while not stop.is_set():
                if not path.exists():
                    continue
                try:
                    txt = path.read_text()
                    if not txt:
                        continue
                    payload = json.loads(txt)
                    if set(payload.keys()) != {"scenarios", "ts"}:
                        errors.append(f"bad keys: {sorted(payload.keys())}")
                        return
                except json.JSONDecodeError as e:
                    errors.append(f"corrupt json: {e!r}")
                    return

        writers = [threading.Thread(target=hammer_status) for _ in range(4)]
        readers = [threading.Thread(target=hammer_read) for _ in range(2)]
        for t in writers + readers:
            t.daemon = True
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in writers + readers:
            t.join(timeout=2.0)
        assert errors == [], errors
