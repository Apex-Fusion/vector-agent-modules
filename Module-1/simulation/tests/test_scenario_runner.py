"""
RED tests for simulation.scenario.ScenarioRunner (Sim Phase 2 iter 1).

Claire — test-engineer pass.

These tests drive the design of the abstract base class. A minimal mock
subclass (_MockScenario) is defined here so the base-class behaviour can be
exercised without any concrete scenario. Catherine fills in the bodies of the
concrete methods in simulation/scenario.py during GREEN.

All tests currently fail with NotImplementedError (from the skeleton stubs)
or AssertionError where the skeleton raises before even touching state.
"""

from __future__ import annotations

import json
import os
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

# NOTE: test import will succeed (module file exists). The stubbed concrete
# methods raise NotImplementedError, which is the desired failure mode.
from simulation.scenario import ScenarioRunner, CHECKPOINT_SCHEMA_VERSION


# ═══════════════════════════════════════════════════════════════════════
# Test doubles / fixtures
# ═══════════════════════════════════════════════════════════════════════


def _fake_deployment() -> Any:
    """Stand-in DeploymentState — ScenarioRunner shouldn't care about its
    internals at this layer; subclasses use it, base class only stores it."""
    return types.SimpleNamespace(
        claim_hash="cc" * 28,
        challenge_hash="dd" * 28,
        jury_pool_hash="ee" * 28,
    )


def _make_kwargs(tmp_path: Path, name: str = "test_scenario", rng_seed: int = 42) -> dict:
    ckpt = tmp_path / "checkpoints"
    metrics = tmp_path / "metrics"
    return dict(
        name=name,
        config={"epochs_per_day": 24, "n_agents": 10},
        deployment=_fake_deployment(),
        master_skey="<fake_skey>",
        master_vkey="<fake_vkey>",
        master_wallet_addr="addr1_fake",
        checkpoint_dir=ckpt,
        metrics_dir=metrics,
        rng_seed=rng_seed,
    )


class _MockScenario(ScenarioRunner):
    """Minimal concrete subclass for exercising base-class behaviour.

    Records every epoch it is asked to act on, and returns a deterministic
    list of events derived from self.rng so that deterministic-replay tests
    can compare two runs with the same seed.
    """

    def __init__(self, *args, **kwargs):
        # Test-only switches consumed before delegating to the base init.
        self._raise_on_epoch: int | None = kwargs.pop("raise_on_epoch", None)
        super().__init__(*args, **kwargs)
        self.calls: list[int] = []
        self.custom_state: dict[str, Any] = {"counter": 0, "log": []}

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        self.calls.append(epoch)
        self.custom_state["counter"] += 1
        self.custom_state["log"].append(epoch)

        if self._raise_on_epoch is not None and epoch == self._raise_on_epoch:
            raise RuntimeError(f"boom at epoch {epoch}")

        # Deterministic payload from the runner's own rng.
        roll = int(self.rng.integers(0, 1_000_000))
        return [
            {"event_type": "epoch_tick", "epoch": epoch, "roll": roll},
        ]

    def _checkpoint_payload(self) -> dict:
        return {"custom_state": self.custom_state}

    def _restore_payload(self, payload: dict) -> None:
        if "custom_state" in payload:
            self.custom_state = payload["custom_state"]


class _CountingScenario(ScenarioRunner):
    """Even smaller subclass — just counts decide_and_act calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_calls = 0

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        self.n_calls += 1
        return []


# ═══════════════════════════════════════════════════════════════════════
# Abstract base contract
# ═══════════════════════════════════════════════════════════════════════


class TestAbstractContract:
    def test_ScenarioRunner_is_abstract(self, tmp_path):
        """Direct instantiation must fail — decide_and_act_for_epoch is abstract."""
        with pytest.raises(TypeError):
            ScenarioRunner(**_make_kwargs(tmp_path))  # type: ignore[abstract]

    def test_subclass_missing_decide_and_act_cannot_instantiate(self, tmp_path):
        """A subclass that forgets to implement the abstract method must also fail."""

        class Broken(ScenarioRunner):  # noqa: D401 — test fixture class
            pass

        with pytest.raises(TypeError):
            Broken(**_make_kwargs(tmp_path))  # type: ignore[abstract]

    def test_subclass_with_decide_and_act_can_be_constructed(self, tmp_path):
        """A proper subclass instantiates without error."""
        s = _MockScenario(**_make_kwargs(tmp_path))
        assert isinstance(s, ScenarioRunner)
        assert s.name == "test_scenario"


# ═══════════════════════════════════════════════════════════════════════
# Construction & attribute setup
# ═══════════════════════════════════════════════════════════════════════


class TestConstruction:
    def test_stores_all_constructor_args(self, tmp_path):
        kw = _make_kwargs(tmp_path, name="alpha", rng_seed=7)
        s = _MockScenario(**kw)
        assert s.name == "alpha"
        assert s.config == kw["config"]
        assert s.deployment is kw["deployment"]
        assert s.master_skey == kw["master_skey"]
        assert s.master_vkey == kw["master_vkey"]
        assert s.master_wallet_addr == kw["master_wallet_addr"]
        assert s.checkpoint_dir == kw["checkpoint_dir"]
        assert s.metrics_dir == kw["metrics_dir"]
        assert s.rng_seed == 7

    def test_creates_checkpoint_and_metrics_dirs(self, tmp_path):
        kw = _make_kwargs(tmp_path)
        # Dirs should not pre-exist
        assert not kw["checkpoint_dir"].exists()
        assert not kw["metrics_dir"].exists()
        _MockScenario(**kw)
        assert kw["checkpoint_dir"].is_dir()
        assert kw["metrics_dir"].is_dir()

    def test_checkpoint_and_metrics_paths_use_scenario_name(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path, name="happy_path"))
        assert s.checkpoint_path == tmp_path / "checkpoints" / "happy_path.json"
        assert s.metrics_path == tmp_path / "metrics" / "happy_path.jsonl"

    def test_epoch_counter_starts_at_minus_one(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        # Sentinel: -1 means "no epoch completed yet"; the first run starts at 0.
        assert s._epoch == -1

    def test_rng_is_seeded_deterministically(self, tmp_path):
        s1 = _MockScenario(**_make_kwargs(tmp_path / "a", rng_seed=1234))
        s2 = _MockScenario(**_make_kwargs(tmp_path / "b", rng_seed=1234))
        seq1 = [int(s1.rng.integers(0, 10_000)) for _ in range(5)]
        seq2 = [int(s2.rng.integers(0, 10_000)) for _ in range(5)]
        assert seq1 == seq2


# ═══════════════════════════════════════════════════════════════════════
# Lifecycle / loop
# ═══════════════════════════════════════════════════════════════════════


class TestRunLoop:
    def test_run_calls_decide_and_act_for_each_epoch(self, tmp_path):
        s = _CountingScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=5)
        assert s.n_calls == 5

    def test_run_passes_sequential_epoch_numbers(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=4)
        assert s.calls == [0, 1, 2, 3]

    def test_run_advances_epoch_counter(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=3)
        assert s._epoch == 2  # last completed epoch

    def test_run_emits_returned_events_to_metrics(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=3)
        lines = s.metrics_path.read_text().strip().splitlines()
        assert len(lines) == 3
        parsed = [json.loads(ln) for ln in lines]
        assert [e["epoch"] for e in parsed] == [0, 1, 2]
        assert all(e["event_type"] == "epoch_tick" for e in parsed)

    def test_stop_event_halts_loop_at_epoch_boundary(self, tmp_path):
        stop = threading.Event()
        s = _MockScenario(**_make_kwargs(tmp_path))

        # Set the stop event from inside decide_and_act on epoch 2 — we expect
        # the loop to finish epoch 2 (call count = 3), then exit before 3.
        original = s.decide_and_act_for_epoch

        def wrapper(epoch: int):
            out = original(epoch)
            if epoch == 2:
                stop.set()
            return out

        s.decide_and_act_for_epoch = wrapper  # type: ignore[assignment]
        s.run(n_epochs=10, stop_event=stop)
        assert s.calls == [0, 1, 2]

    def test_graceful_stop_writes_final_checkpoint(self, tmp_path):
        stop = threading.Event()
        s = _MockScenario(**_make_kwargs(tmp_path))
        stop.set()  # already signalled — runner should still do one epoch *at minimum* OR stop immediately
        # Contract: stop is checked AFTER the epoch runs, so we should see ≥1 checkpoint.
        # To test the final-checkpoint invariant more directly, don't pre-set; set mid-run:
        stop.clear()

        original = s.decide_and_act_for_epoch

        def wrapper(epoch: int):
            out = original(epoch)
            if epoch == 0:
                stop.set()
            return out

        s.decide_and_act_for_epoch = wrapper  # type: ignore[assignment]
        s.run(n_epochs=10, stop_event=stop)
        assert s.checkpoint_path.exists(), "checkpoint file must exist after graceful stop"
        data = json.loads(s.checkpoint_path.read_text())
        assert data["epoch"] == 0


# ═══════════════════════════════════════════════════════════════════════
# Checkpointing
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpointing:
    def test_checkpoint_writes_json_to_named_file(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path, name="scenario_x"))
        s.run(n_epochs=1)
        expected = tmp_path / "checkpoints" / "scenario_x.json"
        assert expected.is_file()
        data = json.loads(expected.read_text())
        assert data["name"] == "scenario_x"

    def test_checkpoint_content_includes_epoch_rng_state_and_custom(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=2)
        data = json.loads(s.checkpoint_path.read_text())
        assert data["epoch"] == 1
        assert "rng_state" in data and isinstance(data["rng_state"], dict)
        assert data["schema_version"] == CHECKPOINT_SCHEMA_VERSION
        assert "custom" in data
        # Subclass _checkpoint_payload contributed custom_state
        assert data["custom"]["custom_state"]["counter"] == 2
        assert data["custom"]["custom_state"]["log"] == [0, 1]

    def test_checkpoint_is_atomic_no_tmp_left_behind(self, tmp_path):
        """Atomic write should use a tmp file + rename. After completion,
        the only file in checkpoints/ is the final .json (no stray .tmp)."""
        s = _MockScenario(**_make_kwargs(tmp_path, name="atomic_test"))
        s.run(n_epochs=1)
        files = sorted(p.name for p in (tmp_path / "checkpoints").iterdir())
        assert files == ["atomic_test.json"], f"unexpected files: {files}"

    def test_checkpoint_overwrites_previous(self, tmp_path):
        """Repeated checkpoints produce a single file, not N versions."""
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=3)
        assert len(list((tmp_path / "checkpoints").iterdir())) == 1
        data = json.loads(s.checkpoint_path.read_text())
        assert data["epoch"] == 2

    def test_restore_returns_False_when_no_checkpoint_exists(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        assert s.restore() is False

    def test_restore_returns_True_and_loads_state_when_checkpoint_exists(self, tmp_path):
        kw = _make_kwargs(tmp_path)
        s1 = _MockScenario(**kw)
        s1.run(n_epochs=3)

        # Fresh instance with the same dirs/name.
        s2 = _MockScenario(**kw)
        assert s2.restore() is True
        assert s2._epoch == 2
        assert s2.custom_state["counter"] == 3
        assert s2.custom_state["log"] == [0, 1, 2]

    def test_restore_recovers_rng_state(self, tmp_path):
        """After restore, self.rng picks up where it left off — the next
        integers(...) call yields the same value a fresh runner at that point
        would have produced."""
        kw = _make_kwargs(tmp_path, rng_seed=99)

        # Reference: uninterrupted run of 6 epochs, capturing the roll sequence.
        ref = _MockScenario(**_make_kwargs(tmp_path / "ref", rng_seed=99))
        ref.run(n_epochs=6)
        ref_rolls = [json.loads(ln)["roll"] for ln in ref.metrics_path.read_text().strip().splitlines()]

        # Interrupted: run 3, restore in a new instance, run 3 more.
        s1 = _MockScenario(**kw)
        s1.run(n_epochs=3)

        s2 = _MockScenario(**kw)
        assert s2.restore() is True
        s2.run(n_epochs=3)

        resumed_rolls = [
            json.loads(ln)["roll"]
            for ln in s2.metrics_path.read_text().strip().splitlines()
        ]
        # s2 appends to the same metrics file, so resumed_rolls contains
        # the original 3 + the 3 after restore — total 6, matching ref exactly.
        assert resumed_rolls == ref_rolls

    def test_restore_recovers_epoch_counter(self, tmp_path):
        kw = _make_kwargs(tmp_path)
        s1 = _MockScenario(**kw)
        s1.run(n_epochs=4)
        assert s1._epoch == 3

        s2 = _MockScenario(**kw)
        s2.restore()
        assert s2._epoch == 3

    def test_restore_then_run_continues_epoch_sequence(self, tmp_path):
        """After restore at epoch=3, run(n_epochs=2) visits epochs 4 and 5."""
        kw = _make_kwargs(tmp_path)
        s1 = _MockScenario(**kw)
        s1.run(n_epochs=4)

        s2 = _MockScenario(**kw)
        s2.restore()
        s2.run(n_epochs=2)
        assert s2.calls == [4, 5]


# ═══════════════════════════════════════════════════════════════════════
# Metrics / emit_event
# ═══════════════════════════════════════════════════════════════════════


class TestMetrics:
    def test_emit_event_appends_jsonl_line(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.emit_event({"event_type": "hello", "x": 1})
        lines = s.metrics_path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "hello"
        assert parsed["x"] == 1

    def test_emit_event_multiple_calls_append(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        for i in range(5):
            s.emit_event({"event_type": "tick", "i": i})
        lines = s.metrics_path.read_text().strip().splitlines()
        assert [json.loads(ln)["i"] for ln in lines] == [0, 1, 2, 3, 4]

    def test_emit_event_includes_scenario_name(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path, name="my_scenario"))
        s.emit_event({"event_type": "x"})
        ev = json.loads(s.metrics_path.read_text().strip())
        assert ev["scenario"] == "my_scenario"

    def test_emit_event_includes_iso_timestamp(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.emit_event({"event_type": "x"})
        ev = json.loads(s.metrics_path.read_text().strip())
        assert "ts" in ev
        # ISO-8601 UTC: "...Z" or "+00:00"
        assert ev["ts"].endswith("Z") or ev["ts"].endswith("+00:00")
        # Parseable by datetime.fromisoformat (py3.11+)
        from datetime import datetime
        normalized = ev["ts"].replace("Z", "+00:00")
        datetime.fromisoformat(normalized)  # should not raise

    def test_emit_event_does_not_mutate_caller_dict(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        payload = {"event_type": "x", "v": 1}
        s.emit_event(payload)
        # Enrichment (scenario, ts) should not have been added to the caller's dict.
        assert "scenario" not in payload
        assert "ts" not in payload

    def test_emit_event_is_thread_safe(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        N_THREADS, N_PER = 50, 20  # 1000 total events

        def worker(tid: int):
            for i in range(N_PER):
                s.emit_event({"event_type": "t", "tid": tid, "i": i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = s.metrics_path.read_text().strip().splitlines()
        assert len(lines) == N_THREADS * N_PER
        # Every line must be valid JSON (no interleaved/torn writes).
        for ln in lines:
            parsed = json.loads(ln)  # should not raise
            assert parsed["event_type"] == "t"


# ═══════════════════════════════════════════════════════════════════════
# Isolation between scenarios
# ═══════════════════════════════════════════════════════════════════════


class TestIsolation:
    def test_two_scenarios_write_to_separate_files(self, tmp_path):
        kw_a = _make_kwargs(tmp_path, name="alpha")
        kw_b = _make_kwargs(tmp_path, name="beta")
        a = _MockScenario(**kw_a)
        b = _MockScenario(**kw_b)

        a.run(n_epochs=2)
        b.run(n_epochs=3)

        assert (tmp_path / "checkpoints" / "alpha.json").exists()
        assert (tmp_path / "checkpoints" / "beta.json").exists()
        assert (tmp_path / "metrics" / "alpha.jsonl").exists()
        assert (tmp_path / "metrics" / "beta.jsonl").exists()

        # No cross-contamination
        alpha_events = [
            json.loads(ln) for ln in (tmp_path / "metrics" / "alpha.jsonl").read_text().strip().splitlines()
        ]
        beta_events = [
            json.loads(ln) for ln in (tmp_path / "metrics" / "beta.jsonl").read_text().strip().splitlines()
        ]
        assert len(alpha_events) == 2
        assert len(beta_events) == 3
        assert all(e["scenario"] == "alpha" for e in alpha_events)
        assert all(e["scenario"] == "beta" for e in beta_events)

    def test_two_scenarios_have_independent_rng(self, tmp_path):
        a = _MockScenario(**_make_kwargs(tmp_path / "a", rng_seed=1))
        b = _MockScenario(**_make_kwargs(tmp_path / "b", rng_seed=2))
        seq_a = [int(a.rng.integers(0, 10_000_000)) for _ in range(10)]
        seq_b = [int(b.rng.integers(0, 10_000_000)) for _ in range(10)]
        assert seq_a != seq_b

    def test_scenario_rng_draws_do_not_affect_another_scenario(self, tmp_path):
        """Running scenario A's rng must not shift scenario B's sequence."""
        b_baseline = _MockScenario(**_make_kwargs(tmp_path / "b1", rng_seed=777))
        expected_b_seq = [int(b_baseline.rng.integers(0, 10_000)) for _ in range(5)]

        a = _MockScenario(**_make_kwargs(tmp_path / "a", rng_seed=1))
        for _ in range(100):
            a.rng.integers(0, 10_000)

        b = _MockScenario(**_make_kwargs(tmp_path / "b2", rng_seed=777))
        actual_b_seq = [int(b.rng.integers(0, 10_000)) for _ in range(5)]
        assert actual_b_seq == expected_b_seq


# ═══════════════════════════════════════════════════════════════════════
# Deterministic replay
# ═══════════════════════════════════════════════════════════════════════


class TestDeterministicReplay:
    def test_same_seed_reproduces_same_epoch_outputs(self, tmp_path):
        """Two independent runs with the same seed produce identical event
        streams (modulo ts/scenario enrichment)."""
        kw1 = _make_kwargs(tmp_path / "run1", rng_seed=2024)
        kw2 = _make_kwargs(tmp_path / "run2", rng_seed=2024)

        s1 = _MockScenario(**kw1)
        s2 = _MockScenario(**kw2)
        s1.run(n_epochs=8)
        s2.run(n_epochs=8)

        rolls1 = [json.loads(ln)["roll"] for ln in s1.metrics_path.read_text().strip().splitlines()]
        rolls2 = [json.loads(ln)["roll"] for ln in s2.metrics_path.read_text().strip().splitlines()]
        assert rolls1 == rolls2

    def test_different_seeds_produce_different_outputs(self, tmp_path):
        s1 = _MockScenario(**_make_kwargs(tmp_path / "a", rng_seed=1))
        s2 = _MockScenario(**_make_kwargs(tmp_path / "b", rng_seed=2))
        s1.run(n_epochs=8)
        s2.run(n_epochs=8)
        rolls1 = [json.loads(ln)["roll"] for ln in s1.metrics_path.read_text().strip().splitlines()]
        rolls2 = [json.loads(ln)["roll"] for ln in s2.metrics_path.read_text().strip().splitlines()]
        assert rolls1 != rolls2


# ═══════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    def test_decide_and_act_exception_writes_error_event(self, tmp_path):
        """Design decision: on decide_and_act exception, emit an error event
        and stop. Rationale: silent retry hides contract regressions; Chuck
        triages via the JSONL tail. (Confirm policy with Catherine.)"""
        kw = _make_kwargs(tmp_path)
        s = _MockScenario(raise_on_epoch=2, **kw)
        s.run(n_epochs=5)

        events = [json.loads(ln) for ln in s.metrics_path.read_text().strip().splitlines()]
        error_events = [e for e in events if e.get("event_type") == "scenario_error"]
        assert len(error_events) == 1
        err = error_events[0]
        assert err["epoch"] == 2
        assert err["exception_class"] == "RuntimeError"
        assert "boom at epoch 2" in err["message"]
        assert "traceback" in err and "RuntimeError" in err["traceback"]

    def test_decide_and_act_exception_stops_scenario(self, tmp_path):
        """After the error, no further epochs run."""
        kw = _make_kwargs(tmp_path)
        s = _MockScenario(raise_on_epoch=2, **kw)
        s.run(n_epochs=10)
        # Epoch 2 called, raised, loop exits — so calls == [0, 1, 2]
        assert s.calls == [0, 1, 2]

    def test_decide_and_act_exception_writes_final_checkpoint(self, tmp_path):
        """Even on error, runner must checkpoint so resume is possible."""
        kw = _make_kwargs(tmp_path)
        s = _MockScenario(raise_on_epoch=2, **kw)
        s.run(n_epochs=10)
        assert s.checkpoint_path.exists()
        data = json.loads(s.checkpoint_path.read_text())
        # epoch=1 is the last cleanly-completed epoch (epoch 2 raised);
        # error is annotated so a subsequent restore knows it.
        assert data["epoch"] == 1


# ═══════════════════════════════════════════════════════════════════════
# stop() method
# ═══════════════════════════════════════════════════════════════════════


class TestStopMethod:
    def test_stop_writes_final_checkpoint(self, tmp_path):
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=2)
        # Simulate external shutdown call
        s.stop()
        assert s.checkpoint_path.exists()
        data = json.loads(s.checkpoint_path.read_text())
        assert data["epoch"] == 1

    def test_stop_is_idempotent(self, tmp_path):
        """Calling stop twice must not raise."""
        s = _MockScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=1)
        s.stop()
        s.stop()  # should not raise
