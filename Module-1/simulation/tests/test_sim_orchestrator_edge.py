"""
Edge-case tests for simulation.orchestrator.SimOrchestrator (Sim Phase 2 iter 2, QA pass).

Claire — extends the 45-test green suite with edge-case coverage that the
original RED design pass did not surface. Each test corresponds to a hardening
question raised during the QA review.

Categories covered here:
  1. status-file atomicity when ``os.replace`` raises (no .tmp residue)
  2. concurrent ``status()`` + ``stop_all()`` from separate threads
  3. ``_read_last_scenario_error`` robustness vs malformed/empty/key-missing/mid-file JSONL
  4. ``wait(timeout)`` while ``stop_all`` is fired externally
  5. re-entry hazard: ``status()`` invoked from inside ``decide_and_act_for_epoch``
  6. snapshot payload schema after run+crash (no extra keys leaking in any phase)
  7. stale snapshot reflects natural completion when ``stop_all`` is never called
  8. checkpoint-dir collision: scenario uses orchestrator's checkpoint_dir
  9. concurrent ``register`` calls across threads
 10. external ``stop_event`` (user-owned) signalled without going through ``stop_all``

All tests must pass against the current orchestrator.py. Tests that probe a
behaviour the implementation does not promise are marked xfail with
strict=False so they flag a real bug if the contract tightens.
"""

from __future__ import annotations

import json
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from simulation.orchestrator import SimOrchestrator
from simulation.scenario import ScenarioRunner


# ═══════════════════════════════════════════════════════════════════════
# Test doubles / fixtures (mirror the iter-2 green suite for readability)
# ═══════════════════════════════════════════════════════════════════════


def _fake_deployment() -> Any:
    return types.SimpleNamespace(
        claim_hash="cc" * 28,
        challenge_hash="dd" * 28,
        jury_pool_hash="ee" * 28,
    )


def _scenario_kwargs(tmp_path: Path, name: str, rng_seed: int = 0) -> dict:
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
    return dict(
        checkpoint_dir=tmp_path / "orch_checkpoints",
        metrics_dir=tmp_path / "orch_metrics",
    )


def _status_path(tmp_path: Path) -> Path:
    return tmp_path / "orch_checkpoints" / "orchestrator_status.json"


class _FastMockScenario(ScenarioRunner):
    """Tiny per-epoch sleep, optional raise — copies the green-suite double."""

    def __init__(self, *args, **kwargs):
        self._sleep_per_epoch: float = kwargs.pop("sleep_per_epoch", 0.005)
        self._raise_on_epoch: int | None = kwargs.pop("raise_on_epoch", None)
        self._raise_immediately: bool = kwargs.pop("raise_immediately", False)
        super().__init__(*args, **kwargs)

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        if self._raise_immediately and epoch == 0:
            raise RuntimeError("immediate boom")
        if self._raise_on_epoch is not None and epoch == self._raise_on_epoch:
            raise RuntimeError(f"boom at epoch {epoch}")
        if self._sleep_per_epoch > 0:
            time.sleep(self._sleep_per_epoch)
        return [{"event_type": "epoch_tick", "epoch": epoch}]


class _BlockingScenario(ScenarioRunner):
    def __init__(self, *args, **kwargs):
        self._release: threading.Event = kwargs.pop("release_event")
        super().__init__(*args, **kwargs)

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        self._release.wait(timeout=5.0)
        return [{"event_type": "tick", "epoch": epoch}]


# ═══════════════════════════════════════════════════════════════════════
# 1. Status-file atomicity when os.replace raises
# ═══════════════════════════════════════════════════════════════════════


class TestStatusFileAtomicityUnderFailure:
    def test_os_replace_failure_during_status_write_leaves_no_tmp_residue(
        self, tmp_path, monkeypatch
    ):
        """Mirror of the iter-1 scenario.py contract for the orchestrator's
        snapshot writer: when os.replace raises, the .tmp file MUST be cleaned
        up — no residue in checkpoint_dir.

        We register a scenario, write a successful snapshot once (so a valid
        prior status file exists and we can confirm it is unchanged), then
        monkeypatch os.replace to raise and call status() again.
        """
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(_FastMockScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))

        # 1) successful snapshot
        orch.status()
        path = _status_path(tmp_path)
        good_bytes = path.read_bytes()

        # 2) force os.replace inside the orchestrator module to raise
        import simulation.orchestrator as orch_mod

        def boom_replace(src, dst):  # noqa: ARG001
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(orch_mod.os, "replace", boom_replace)

        with pytest.raises(OSError):
            orch.status()

        # 3) prior valid status file is unchanged (atomic-replace contract)
        assert path.read_bytes() == good_bytes

        # 4) no leftover .tmp residue (this is the locked iter-2 contract:
        #    orchestrator.py's _write_status_snapshot has explicit cleanup).
        residues = [
            p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")
        ]
        assert residues == [], f"stale tmp file(s) left behind: {residues}"


# ═══════════════════════════════════════════════════════════════════════
# 2. Concurrent status() + stop_all() from separate threads
# ═══════════════════════════════════════════════════════════════════════


class TestConcurrentStatusAndStopAll:
    def test_status_loop_during_stop_all_yields_coherent_final_state(self, tmp_path):
        """While one thread hammers status() in a tight loop, another calls
        stop_all(). No exception, no file corruption, and the final on-disk
        snapshot must reflect post-stop state (alive=False for every scenario).
        """
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b", "c"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.005,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=10_000)  # big number, will be stopped early

        errors: list[str] = []
        stop_loop = threading.Event()

        def status_hammer():
            while not stop_loop.is_set():
                try:
                    orch.status()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"status raised: {e!r}")
                    return

        hammer = threading.Thread(target=status_hammer, daemon=True)
        hammer.start()
        try:
            time.sleep(0.05)  # let some epochs run
            orch.stop_all(timeout=5.0)
        finally:
            stop_loop.set()
            hammer.join(timeout=2.0)

        assert errors == [], errors

        # Final on-disk snapshot reflects post-stop state.
        payload = json.loads(_status_path(tmp_path).read_text())
        assert set(payload.keys()) == {"scenarios", "ts"}
        for nm in ("a", "b", "c"):
            assert payload["scenarios"][nm]["alive"] is False, (
                f"scenario {nm!r} still alive in final snapshot"
            )


# ═══════════════════════════════════════════════════════════════════════
# 3. _read_last_scenario_error robustness
# ═══════════════════════════════════════════════════════════════════════


class TestReadLastScenarioErrorRobustness:
    def _bare_scenario(self, tmp_path: Path, name: str) -> _FastMockScenario:
        """Register a scenario without running it — we hand-craft its
        metrics file to exercise _read_last_scenario_error directly."""
        s = _FastMockScenario(**_scenario_kwargs(tmp_path / name, name))
        s.metrics_dir.mkdir(parents=True, exist_ok=True)
        return s

    def test_empty_metrics_file_yields_no_error(self, tmp_path):
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = self._bare_scenario(tmp_path, "alpha")
        s.metrics_path.write_text("")  # empty file
        orch.register(s)
        st = orch.status()
        assert st["alpha"]["error"] is None

    def test_malformed_last_line_does_not_crash_or_mask_real_error(self, tmp_path):
        """Garbage on the final line must be skipped; the most recent VALID
        scenario_error event should still be returned."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = self._bare_scenario(tmp_path, "alpha")
        valid_err = {
            "event_type": "scenario_error",
            "exception_class": "ValueError",
            "message": "bad input",
            "epoch": 2,
        }
        lines = [
            json.dumps({"event_type": "epoch_tick", "epoch": 0}),
            json.dumps(valid_err),
            "{ this is not valid json",  # malformed final line
        ]
        s.metrics_path.write_text("\n".join(lines) + "\n")
        orch.register(s)
        st = orch.status()
        assert isinstance(st["alpha"]["error"], dict)
        assert st["alpha"]["error"]["class"] == "ValueError"
        assert st["alpha"]["error"]["message"] == "bad input"

    def test_scenario_error_event_missing_exception_class_yields_empty_string(
        self, tmp_path
    ):
        """If a scenario_error event is missing the exception_class key (e.g.
        a future schema drift), the orchestrator must NOT KeyError — it should
        return the dict with empty defaults and let the caller decide."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = self._bare_scenario(tmp_path, "alpha")
        partial_err = {"event_type": "scenario_error", "message": "oops"}
        s.metrics_path.write_text(json.dumps(partial_err) + "\n")
        orch.register(s)
        st = orch.status()
        assert isinstance(st["alpha"]["error"], dict)
        # Defensive: missing key surfaces as "" rather than raising.
        assert st["alpha"]["error"]["class"] == ""
        assert st["alpha"]["error"]["message"] == "oops"

    def test_scenario_error_in_middle_of_file_is_still_returned(self, tmp_path):
        """The reader scans backwards for the LAST scenario_error event.
        Many tail events after the error must not mask it."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        s = self._bare_scenario(tmp_path, "alpha")
        lines: list[str] = []
        # Lots of pre-error noise.
        for i in range(500):
            lines.append(json.dumps({"event_type": "epoch_tick", "epoch": i}))
        # Error in the middle.
        err = {
            "event_type": "scenario_error",
            "exception_class": "RuntimeError",
            "message": "middle-of-file boom",
            "epoch": 500,
        }
        lines.append(json.dumps(err))
        # Lots of post-error noise (e.g. shutdown_complete, late metrics).
        for i in range(500):
            lines.append(json.dumps({"event_type": "shutdown_tick", "epoch": i}))
        s.metrics_path.write_text("\n".join(lines) + "\n")
        orch.register(s)

        st = orch.status()
        assert isinstance(st["alpha"]["error"], dict)
        assert st["alpha"]["error"]["class"] == "RuntimeError"
        assert st["alpha"]["error"]["message"] == "middle-of-file boom"


# ═══════════════════════════════════════════════════════════════════════
# 4. wait(timeout) interaction with externally-fired stop_all
# ═══════════════════════════════════════════════════════════════════════


class TestWaitInteractionWithStopAll:
    def test_wait_returns_cleanly_after_external_stop_all(self, tmp_path):
        """One thread sits in wait(timeout=long). Another thread calls
        stop_all() externally. wait() must return True (all joined) shortly
        after stop_all signals the event and joins the workers."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.005,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=10_000)

        results: dict[str, bool] = {}
        wait_done = threading.Event()

        def waiter():
            results["ok"] = orch.wait(timeout=5.0)
            wait_done.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        # Let the waiter actually enter wait() before we shut things down.
        time.sleep(0.05)
        assert not wait_done.is_set(), "waiter returned before stop_all fired"

        orch.stop_all(timeout=5.0)

        assert wait_done.wait(timeout=5.0), "wait() did not return after stop_all"
        assert results["ok"] is True
        # And the orchestrator should now report all scenarios dead.
        st = orch.status()
        assert all(st[nm]["alive"] is False for nm in ("a", "b"))


# ═══════════════════════════════════════════════════════════════════════
# 5. Re-entry: status() called from inside decide_and_act_for_epoch
# ═══════════════════════════════════════════════════════════════════════


class TestReentryHazard:
    def test_status_callable_from_inside_decide_and_act(self, tmp_path):
        """A scenario holds a back-reference to the orchestrator and calls
        ``orch.status()`` mid-epoch. The snapshot lock must not deadlock — the
        scenario thread is the only writer in this test. Pins the contract:
        status() is safe to invoke from a worker thread."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        observed: list[dict] = []

        class _ReentrantScenario(ScenarioRunner):
            def decide_and_act_for_epoch(inner_self, epoch):  # noqa: N805
                # Re-enter status() from inside the worker thread.
                snap = orch.status()
                observed.append(snap)
                return [{"event_type": "tick", "epoch": epoch}]

        orch.register(_ReentrantScenario(**_scenario_kwargs(tmp_path / "a", "alpha")))
        orch.start_all(n_epochs=3)
        # If the snapshot lock were re-entrant-unsafe (or held across the
        # worker call site), this wait would time out instead of returning.
        assert orch.wait(timeout=5.0) is True
        st = orch.status()
        assert st["alpha"]["alive"] is False
        assert st["alpha"]["epoch"] == 2
        assert st["alpha"]["error"] is None
        # The worker observed itself as alive in each in-loop snapshot.
        assert len(observed) == 3
        for snap in observed:
            assert snap["alpha"]["alive"] is True


# ═══════════════════════════════════════════════════════════════════════
# 6. Snapshot payload schema across phases (exact key set, no leakage)
# ═══════════════════════════════════════════════════════════════════════


class TestSnapshotSchemaAcrossPhases:
    def test_snapshot_keys_exact_in_running_and_post_crash_phases(self, tmp_path):
        """Top-level keys must be EXACTLY {'scenarios', 'ts'} and per-scenario
        keys EXACTLY {'alive', 'epoch', 'error'} — across every phase, not
        just the pre-start state covered by the green suite."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        orch.register(
            _FastMockScenario(
                sleep_per_epoch=0.005,
                **_scenario_kwargs(tmp_path / "ok", "ok"),
            )
        )
        orch.register(
            _FastMockScenario(
                raise_on_epoch=1,
                **_scenario_kwargs(tmp_path / "boom", "boom"),
            )
        )
        orch.start_all(n_epochs=4)
        assert orch.wait(timeout=5.0) is True
        orch.status()  # final snapshot
        payload = json.loads(_status_path(tmp_path).read_text())

        assert set(payload.keys()) == {"scenarios", "ts"}, payload.keys()
        for nm in ("ok", "boom"):
            entry = payload["scenarios"][nm]
            assert set(entry.keys()) == {"alive", "epoch", "error"}, (
                f"scenario {nm!r} entry keys = {sorted(entry.keys())}"
            )
        # Crashed scenario carries the dict; ok scenario carries None.
        assert payload["scenarios"]["boom"]["error"]["class"] == "RuntimeError"
        assert payload["scenarios"]["ok"]["error"] is None


# ═══════════════════════════════════════════════════════════════════════
# 7. Stale snapshot reflects natural completion (no stop_all)
# ═══════════════════════════════════════════════════════════════════════


class TestNaturalCompletionSnapshot:
    def test_snapshot_after_natural_finish_reports_all_dead(self, tmp_path):
        """Multiple scenarios finish naturally (no stop_all). A subsequent
        status() must show alive=False for every one and write that to disk."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        for nm in ("a", "b", "c"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.001,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=5.0) is True

        st = orch.status()  # NOT stop_all — the contract here is post-natural
        for nm in ("a", "b", "c"):
            assert st[nm]["alive"] is False
            assert st[nm]["epoch"] == 1
            assert st[nm]["error"] is None

        payload = json.loads(_status_path(tmp_path).read_text())
        for nm in ("a", "b", "c"):
            assert payload["scenarios"][nm]["alive"] is False
            assert payload["scenarios"][nm]["epoch"] == 1


# ═══════════════════════════════════════════════════════════════════════
# 8. Checkpoint-dir collision
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpointDirCollision:
    def test_orchestrator_status_does_not_collide_with_scenario_checkpoint(
        self, tmp_path
    ):
        """Operator misconfiguration: the orchestrator's checkpoint_dir is
        the SAME directory as a scenario's checkpoint_dir. The orchestrator
        writes ``orchestrator_status.json``; the scenario writes
        ``<name>.json``. Different filenames → no collision, both files must
        co-exist intact."""
        shared_ckpt = tmp_path / "shared_checkpoints"
        shared_metrics = tmp_path / "shared_metrics"
        orch = SimOrchestrator(
            checkpoint_dir=shared_ckpt, metrics_dir=tmp_path / "orch_metrics_only"
        )
        # Scenario uses the SAME checkpoint dir as the orchestrator.
        scen_kwargs = dict(
            name="alpha",
            config={"epochs_per_day": 24, "n_agents": 1},
            deployment=_fake_deployment(),
            master_skey="<fake_skey>",
            master_vkey="<fake_vkey>",
            master_wallet_addr="addr1_fake",
            checkpoint_dir=shared_ckpt,
            metrics_dir=shared_metrics,
            rng_seed=7,
        )
        orch.register(_FastMockScenario(sleep_per_epoch=0.001, **scen_kwargs))
        orch.start_all(n_epochs=2)
        assert orch.wait(timeout=5.0) is True
        orch.status()  # ensure final snapshot written

        # Both files exist with their own content.
        scen_ckpt = shared_ckpt / "alpha.json"
        orch_status = shared_ckpt / "orchestrator_status.json"
        assert scen_ckpt.is_file(), f"scenario checkpoint missing in {shared_ckpt}"
        assert orch_status.is_file(), f"orch status missing in {shared_ckpt}"

        scen_data = json.loads(scen_ckpt.read_text())
        orch_data = json.loads(orch_status.read_text())
        # Scenario file has the scenario's checkpoint shape.
        assert scen_data.get("epoch") == 1
        # Orchestrator file has the orch snapshot shape.
        assert set(orch_data.keys()) == {"scenarios", "ts"}
        assert orch_data["scenarios"]["alpha"]["alive"] is False


# ═══════════════════════════════════════════════════════════════════════
# 9. Concurrent register from multiple threads
# ═══════════════════════════════════════════════════════════════════════


class TestConcurrentRegister:
    def test_concurrent_register_loses_no_scenarios_and_preserves_dedupe(
        self, tmp_path
    ):
        """Multiple threads call register() concurrently. Every uniquely-named
        scenario must appear; duplicate names should still raise ValueError on
        whichever thread loses the race."""
        orch = SimOrchestrator(**_orch_dirs(tmp_path))
        N = 30
        names = [f"s{i:02d}" for i in range(N)]
        # Pre-build all scenarios so register() is the only concurrent step.
        scenarios = [
            _FastMockScenario(**_scenario_kwargs(tmp_path / nm, nm)) for nm in names
        ]

        # Plus a handful of duplicate-name scenarios that must lose the race.
        dup_scenarios = [
            _FastMockScenario(**_scenario_kwargs(tmp_path / f"dup{i}", names[i]))
            for i in range(5)
        ]

        register_errors: list[Exception] = []
        dup_value_errors: list[ValueError] = []
        gate = threading.Event()

        def reg(s):
            gate.wait()
            try:
                orch.register(s)
            except ValueError as e:
                dup_value_errors.append(e)
            except Exception as e:  # noqa: BLE001
                register_errors.append(e)

        threads = [threading.Thread(target=reg, args=(s,)) for s in scenarios]
        threads += [threading.Thread(target=reg, args=(s,)) for s in dup_scenarios]
        for t in threads:
            t.start()
        gate.set()  # release them all roughly simultaneously
        for t in threads:
            t.join(timeout=5.0)

        assert register_errors == [], register_errors
        # Every uniquely-named scenario got registered exactly once.
        st = orch.status()
        assert set(st.keys()) == set(names), (
            f"missing or extra names. expected {set(names)}, got {set(st.keys())}"
        )
        # Each duplicate attempt lost the race and raised ValueError.
        # (Note: under the GIL, the unique-name attempt might also race a dup
        # and the dup wins — but then the unique-name retry would still pass
        # because we built one per name. We allow >= number of pure dups.)
        assert len(dup_value_errors) >= len(dup_scenarios), (
            f"expected at least {len(dup_scenarios)} ValueErrors from dups; "
            f"got {len(dup_value_errors)}"
        )


# ═══════════════════════════════════════════════════════════════════════
# 10. External stop_event semantics (no stop_all)
# ═══════════════════════════════════════════════════════════════════════


class TestExternalStopEvent:
    def test_user_owned_stop_event_stops_scenarios_and_wait_returns_true(
        self, tmp_path
    ):
        """User passes their OWN stop_event. They set it externally — never
        calling stop_all. All scenarios must drain at the next epoch boundary
        and orchestrator.wait() must return True."""
        ev = threading.Event()
        orch = SimOrchestrator(stop_event=ev, **_orch_dirs(tmp_path))
        for nm in ("a", "b"):
            orch.register(
                _FastMockScenario(
                    sleep_per_epoch=0.005,
                    **_scenario_kwargs(tmp_path / nm, nm),
                )
            )
        orch.start_all(n_epochs=10_000)
        # Confirm orchestrator's stop_event is the same instance the user holds.
        assert orch.stop_event is ev
        time.sleep(0.05)

        # User signals stop directly — never calling stop_all.
        ev.set()

        ok = orch.wait(timeout=5.0)
        assert ok is True
        st = orch.status()
        for nm in ("a", "b"):
            assert st[nm]["alive"] is False
            # Some epochs ran but not 10_000 — proves the stop took effect.
            assert 0 <= st[nm]["epoch"] < 10_000
            assert st[nm]["error"] is None
