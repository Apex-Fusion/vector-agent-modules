"""
Edge-case tests for simulation.scenario.ScenarioRunner (Sim Phase 2 iter 1, QA pass).

Claire — extends the green test suite with edge-case coverage that the original
RED design pass did not surface. Each test corresponds to a hardening question
asked during the QA review.

Categories covered here:
  - Round-trip restore determinism over many epochs (1)
  - stop_event signalled from inside decide_and_act (mid-epoch) (2)
  - High-concurrency emit_event JSONL integrity (3)
  - Crash mid-checkpoint (os.replace monkeypatched to fail) (4)
  - scenario_error event shape: non-ASCII, empty message, BaseException (5)
  - emit_event input immutability with nested mutable values (6)
  - Explicit rng bit_generator.state equality post-restore (7)
  - n_epochs=0 no-op behaviour (8)
  - Read-only checkpoint_dir failure mode (9)

All tests must pass against the current scenario.py. Tests that probe a
behaviour the implementation does not promise (e.g. failed-checkpoint cleanup
is best-effort) are marked xfail with a clear strict=False reason so they
flag a real bug if the contract tightens.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from simulation.scenario import ScenarioRunner


# ═══════════════════════════════════════════════════════════════════════
# Local fixtures (mirror style of test_scenario_runner.py for readability)
# ═══════════════════════════════════════════════════════════════════════


def _fake_deployment() -> Any:
    return types.SimpleNamespace(
        claim_hash="cc" * 28,
        challenge_hash="dd" * 28,
        jury_pool_hash="ee" * 28,
    )


def _make_kwargs(tmp_path: Path, name: str = "edge_scenario", rng_seed: int = 42) -> dict:
    return dict(
        name=name,
        config={"epochs_per_day": 24, "n_agents": 10},
        deployment=_fake_deployment(),
        master_skey="<fake_skey>",
        master_vkey="<fake_vkey>",
        master_wallet_addr="addr1_fake",
        checkpoint_dir=tmp_path / "checkpoints",
        metrics_dir=tmp_path / "metrics",
        rng_seed=rng_seed,
    )


class _RngScenario(ScenarioRunner):
    """Subclass that exercises the runner rng on every epoch and returns a
    deterministic event stream derived from it. Used by the determinism and
    rng-restore tests."""

    def __init__(self, *args, **kwargs):
        self._raise_cls: type[BaseException] | None = kwargs.pop("raise_cls", None)
        self._raise_msg: str | None = kwargs.pop("raise_msg", None)
        self._raise_on: int | None = kwargs.pop("raise_on", None)
        super().__init__(*args, **kwargs)

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        if self._raise_on is not None and epoch == self._raise_on:
            cls = self._raise_cls or RuntimeError
            if self._raise_msg is None:
                # No-message exception (some classes need an arg, some don't).
                raise cls() if cls is not RuntimeError else cls()
            raise cls(self._raise_msg)
        # Two distinct rng draws so any state divergence shows up in the diff.
        a = int(self.rng.integers(0, 1_000_000_000))
        b = int(self.rng.integers(0, 1_000_000_000))
        return [{"event_type": "draw", "epoch": epoch, "a": a, "b": b}]


class _NoopScenario(ScenarioRunner):
    """No events, no rng draws — for n_epochs=0 / lifecycle smoke."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        self.calls += 1
        return []


def _read_events(path: Path) -> list[dict]:
    text = path.read_text() if path.exists() else ""
    return [json.loads(ln) for ln in text.strip().splitlines() if ln]


# ═══════════════════════════════════════════════════════════════════════
# 1. Restore round-trip determinism over many epochs
# ═══════════════════════════════════════════════════════════════════════


class TestRestoreRoundTripDeterminism:
    def test_restore_midstream_matches_uninterrupted_run_event_for_event(self, tmp_path):
        """Run 10 epochs uninterrupted vs. (4 epochs + restore + 6 epochs).
        Both should produce byte-for-byte identical (a, b) draw pairs in order."""
        SEED = 13579

        ref = _RngScenario(**_make_kwargs(tmp_path / "ref", rng_seed=SEED))
        ref.run(n_epochs=10)
        ref_pairs = [(e["a"], e["b"]) for e in _read_events(ref.metrics_path)]
        assert len(ref_pairs) == 10

        kw = _make_kwargs(tmp_path / "interrupted", rng_seed=SEED)
        s1 = _RngScenario(**kw)
        s1.run(n_epochs=4)

        # Fresh process simulation: brand-new instance, same dirs.
        s2 = _RngScenario(**kw)
        assert s2.restore() is True
        s2.run(n_epochs=6)

        resumed_pairs = [(e["a"], e["b"]) for e in _read_events(s2.metrics_path)]
        assert resumed_pairs == ref_pairs, (
            "checkpoint+restore must reproduce uninterrupted draw sequence"
        )

    def test_restore_round_trip_through_multiple_checkpoints(self, tmp_path):
        """Three restore cycles (2+2+2+2 epochs) still match an 8-epoch
        uninterrupted reference. Catches drift that only appears after >1
        restore boundary."""
        SEED = 24680

        ref = _RngScenario(**_make_kwargs(tmp_path / "ref", rng_seed=SEED))
        ref.run(n_epochs=8)
        ref_pairs = [(e["a"], e["b"]) for e in _read_events(ref.metrics_path)]

        kw = _make_kwargs(tmp_path / "chunked", rng_seed=SEED)
        for chunk in range(4):
            s = _RngScenario(**kw)
            if chunk > 0:
                assert s.restore() is True
            s.run(n_epochs=2)

        chunked_pairs = [
            (e["a"], e["b"])
            for e in _read_events(kw["metrics_dir"] / f"{kw['name']}.jsonl")
        ]
        assert chunked_pairs == ref_pairs


# ═══════════════════════════════════════════════════════════════════════
# 2. stop_event signalled from inside the loop (mid-epoch race)
# ═══════════════════════════════════════════════════════════════════════


class TestStopEventMidEpoch:
    def test_stop_event_set_inside_decide_and_act_finishes_current_epoch_then_exits(
        self, tmp_path
    ):
        """A scenario sets the stop_event from *inside* its own decide_and_act
        on epoch 3. The runner contract checks stop_event AFTER the epoch
        completes its checkpoint, so we expect epochs 0..3 to run, then exit.
        Verifies (a) clean exit, (b) checkpoint reflects epoch 3, (c) no
        partial-epoch artifacts in metrics."""
        stop = threading.Event()
        kw = _make_kwargs(tmp_path)

        class _SelfStopping(ScenarioRunner):
            def __init__(self_inner, *a, **kw_inner):
                super().__init__(*a, **kw_inner)
                self_inner.calls = []

            def decide_and_act_for_epoch(self_inner, epoch):
                self_inner.calls.append(epoch)
                if epoch == 3:
                    stop.set()
                return [{"event_type": "tick", "epoch": epoch}]

        s = _SelfStopping(**kw)
        s.run(n_epochs=20, stop_event=stop)

        # (a) Clean exit at the right boundary.
        assert s.calls == [0, 1, 2, 3]
        # (b) Checkpoint reflects last clean epoch.
        data = json.loads(s.checkpoint_path.read_text())
        assert data["epoch"] == 3
        # (c) Exactly 4 events, all matching, no torn writes.
        events = _read_events(s.metrics_path)
        assert [e["epoch"] for e in events] == [0, 1, 2, 3]


# ═══════════════════════════════════════════════════════════════════════
# 3. High-concurrency emit_event from many threads
# ═══════════════════════════════════════════════════════════════════════


class TestConcurrentEmitEvent:
    def test_high_concurrency_emit_event_no_torn_lines_and_exact_count(self, tmp_path):
        """100 threads x 100 events = 10_000 events. Each line must:
        (a) parse as JSON (no torn writes),
        (b) carry the unique (tid, i) it was emitted with — proving no
            event was lost or duplicated, and the count is exact."""
        s = _NoopScenario(**_make_kwargs(tmp_path))
        N_THREADS = 100
        N_PER = 100
        TOTAL = N_THREADS * N_PER

        def worker(tid: int) -> None:
            for i in range(N_PER):
                s.emit_event({"event_type": "concurrent", "tid": tid, "i": i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = _read_events(s.metrics_path)
        assert len(events) == TOTAL, f"expected {TOTAL} events, got {len(events)}"

        # Every (tid, i) tuple must appear exactly once.
        seen = {(e["tid"], e["i"]) for e in events}
        expected = {(t, i) for t in range(N_THREADS) for i in range(N_PER)}
        assert seen == expected, "missing or duplicated event ids under concurrency"

        # All carry scenario+ts enrichment.
        assert all(e.get("scenario") == s.name for e in events)
        assert all("ts" in e for e in events)


# ═══════════════════════════════════════════════════════════════════════
# 4. Crash mid-checkpoint (os.replace failure)
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpointAtomicityUnderFailure:
    def test_os_replace_failure_during_checkpoint_preserves_prior_checkpoint(
        self, tmp_path, monkeypatch
    ):
        """Run 2 epochs successfully (checkpoint at epoch 1 exists), then
        force os.replace to raise on the next checkpoint. The prior on-disk
        checkpoint must remain valid and loadable — never torn."""
        s = _RngScenario(**_make_kwargs(tmp_path, rng_seed=11))
        s.run(n_epochs=2)

        # Snapshot the good checkpoint bytes.
        good_bytes = s.checkpoint_path.read_bytes()
        good_data = json.loads(good_bytes)
        assert good_data["epoch"] == 1

        # Now monkeypatch os.replace inside the scenario module to raise.
        import simulation.scenario as scenario_mod

        def boom_replace(src, dst):  # noqa: ARG001
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(scenario_mod.os, "replace", boom_replace)

        with pytest.raises(OSError):
            s.checkpoint()

        # Prior checkpoint file is unchanged (atomic-replace contract).
        assert s.checkpoint_path.read_bytes() == good_bytes
        # And it's still loadable in a fresh instance.
        s2 = _RngScenario(**_make_kwargs(tmp_path, rng_seed=11))
        assert s2.restore() is True
        assert s2._epoch == 1

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Best-effort cleanup of the .tmp file after a failed os.replace is "
            "not part of the locked contract. Iter 1 leaves the .tmp behind. "
            "If the contract tightens, this test will start passing — flag to Catherine."
        ),
    )
    def test_failed_checkpoint_does_not_leave_tmp_file_behind(self, tmp_path, monkeypatch):
        s = _RngScenario(**_make_kwargs(tmp_path, rng_seed=11))
        s.run(n_epochs=1)

        import simulation.scenario as scenario_mod

        monkeypatch.setattr(
            scenario_mod.os,
            "replace",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("fail")),
        )

        with pytest.raises(OSError):
            s.checkpoint()

        leftover = [p.name for p in s.checkpoint_dir.iterdir() if p.name.endswith(".tmp")]
        assert leftover == [], f"stale tmp file(s) left behind: {leftover}"


# ═══════════════════════════════════════════════════════════════════════
# 5. scenario_error event shape: non-ASCII, empty message, BaseException
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioErrorEventShape:
    def test_non_ascii_exception_message_serialized_correctly(self, tmp_path):
        """Unicode in the exception message must round-trip through JSON."""
        msg = "boom — épsilon failed: 致命的なエラー"
        s = _RngScenario(
            raise_cls=ValueError,
            raise_msg=msg,
            raise_on=0,
            **_make_kwargs(tmp_path),
        )
        s.run(n_epochs=3)

        events = _read_events(s.metrics_path)
        errs = [e for e in events if e.get("event_type") == "scenario_error"]
        assert len(errs) == 1
        assert errs[0]["message"] == msg
        assert errs[0]["exception_class"] == "ValueError"

    def test_exception_with_empty_message_still_emits_well_formed_error(self, tmp_path):
        """An exception with no constructor arg → str(exc) == ''. The error
        event must still serialize with all required keys."""
        s = _RngScenario(
            raise_cls=RuntimeError,
            raise_msg="",  # empty string
            raise_on=0,
            **_make_kwargs(tmp_path),
        )
        s.run(n_epochs=2)

        events = _read_events(s.metrics_path)
        errs = [e for e in events if e.get("event_type") == "scenario_error"]
        assert len(errs) == 1
        err = errs[0]
        assert err["exception_class"] == "RuntimeError"
        assert err["message"] == ""
        assert "traceback" in err and "RuntimeError" in err["traceback"]
        assert err["epoch"] == 0
        # Final checkpoint exists; epoch counter unchanged from -1 since epoch 0 raised.
        assert s.checkpoint_path.exists()
        ckpt = json.loads(s.checkpoint_path.read_text())
        assert ckpt["epoch"] == -1

    def test_baseexception_subclass_is_NOT_caught_by_except_Exception(self, tmp_path):
        """Documents the contract: the runner catches `Exception`, not
        `BaseException`. KeyboardInterrupt / SystemExit propagate (correct
        behaviour — operators expect Ctrl-C to actually stop the sim).
        This test pins that invariant so a future broaden-the-except change
        is caught."""

        class _SystemExitScenario(ScenarioRunner):
            def decide_and_act_for_epoch(self, epoch):
                if epoch == 1:
                    raise SystemExit("operator-style exit")
                return []

        s = _SystemExitScenario(**_make_kwargs(tmp_path))
        with pytest.raises(SystemExit):
            s.run(n_epochs=5)

        # Epoch 0 completed and checkpointed; epoch 1 raised through the runner.
        events = _read_events(s.metrics_path)
        assert all(e.get("event_type") != "scenario_error" for e in events), (
            "BaseException must not be intercepted as a scenario_error"
        )


# ═══════════════════════════════════════════════════════════════════════
# 6. emit_event input immutability — nested values
# ═══════════════════════════════════════════════════════════════════════


class TestEmitEventImmutability:
    def test_emit_event_does_not_mutate_caller_dict_with_nested_payload(self, tmp_path):
        """Already covered for top-level keys. Here we additionally verify
        the caller's top-level dict identity and key set are untouched even
        when the payload contains nested mutable values (lists, dicts).
        Defense against a future implementation switching from `dict(event)`
        to in-place mutation."""
        s = _NoopScenario(**_make_kwargs(tmp_path))
        nested_list = [1, 2, 3]
        nested_dict = {"k": "v"}
        original = {
            "event_type": "x",
            "items": nested_list,
            "meta": nested_dict,
        }
        original_keys_snapshot = set(original.keys())
        original_id = id(original)

        s.emit_event(original)

        # Top-level identity & keys unchanged.
        assert id(original) == original_id
        assert set(original.keys()) == original_keys_snapshot
        assert "scenario" not in original
        assert "ts" not in original
        # Nested objects are still the same identities (shallow copy is fine
        # at this layer — the contract is "don't leak enrichment back").
        assert original["items"] is nested_list
        assert original["meta"] is nested_dict


# ═══════════════════════════════════════════════════════════════════════
# 7. Explicit rng bit_generator.state equality post-restore
# ═══════════════════════════════════════════════════════════════════════


class TestRngStateExactRestore:
    def test_bit_generator_state_equals_pre_checkpoint_state_after_restore(self, tmp_path):
        """Existing behavioural test compares draws. Here we assert the raw
        Generator.bit_generator.state dict is bit-for-bit equal — the
        strongest possible restore guarantee."""
        kw = _make_kwargs(tmp_path, rng_seed=2026)
        s1 = _RngScenario(**kw)
        s1.run(n_epochs=5)
        # Snapshot the in-memory rng state at the moment of last checkpoint.
        # Because `.state` returns a fresh dict each access, take it via numpy
        # round-trip to guarantee a stable plain-Python representation.
        from simulation.scenario import _jsonable_rng_state

        snapshot = _jsonable_rng_state(s1.rng.bit_generator.state)

        s2 = _RngScenario(**kw)
        assert s2.restore() is True
        restored = _jsonable_rng_state(s2.rng.bit_generator.state)

        assert restored == snapshot, (
            "rng bit_generator.state must be exactly equal after restore"
        )

        # Stronger: from this state, a single draw on s1 and s2 produces the
        # same value (proves the Generator is functionally identical, not
        # just that a structurally similar dict was stored).
        v1 = int(s1.rng.integers(0, 2**31 - 1))
        v2 = int(s2.rng.integers(0, 2**31 - 1))
        assert v1 == v2


# ═══════════════════════════════════════════════════════════════════════
# 8. n_epochs=0 — no-op
# ═══════════════════════════════════════════════════════════════════════


class TestEmptyEpochRange:
    def test_run_with_n_epochs_zero_is_a_complete_noop(self, tmp_path):
        """No decide_and_act calls, no events, no checkpoint, no exception.
        Important so callers can pass `n_epochs=remaining` without guarding
        the zero case."""
        s = _NoopScenario(**_make_kwargs(tmp_path))
        s.run(n_epochs=0)

        assert s.calls == 0
        assert not s.checkpoint_path.exists(), (
            "no checkpoint should be written when no epochs ran"
        )
        # Metrics file must not exist either (no events emitted).
        assert not s.metrics_path.exists()
        # Epoch counter unchanged.
        assert s._epoch == -1

    def test_run_with_n_epochs_zero_after_restore_does_not_advance(self, tmp_path):
        """After a real run + restore, run(n_epochs=0) is still a no-op and
        does not move the epoch counter."""
        kw = _make_kwargs(tmp_path)
        s1 = _NoopScenario(**kw)
        s1.run(n_epochs=3)

        s2 = _NoopScenario(**kw)
        s2.restore()
        epoch_before = s2._epoch
        s2.run(n_epochs=0)
        assert s2._epoch == epoch_before
        assert s2.calls == 0


# ═══════════════════════════════════════════════════════════════════════
# 9. Filesystem permissions
# ═══════════════════════════════════════════════════════════════════════


class TestReadOnlyCheckpointDir:
    def test_checkpoint_to_readonly_dir_raises_clear_oserror(self, tmp_path):
        """If the checkpoint directory is read-only, the runner must surface
        a real OSError (PermissionError) rather than silently swallow the
        failure. Operators rely on this surfacing for monitoring."""
        if os.geteuid() == 0:
            pytest.skip("running as root: filesystem permissions don't apply")

        s = _NoopScenario(**_make_kwargs(tmp_path))
        # Drop write perms on checkpoint_dir AFTER construction (so __init__'s
        # mkdir succeeds), simulating a misconfigured volume mount.
        s.checkpoint_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(OSError):
                s.checkpoint()
        finally:
            # Restore perms so pytest's tmp_path cleanup can run.
            s.checkpoint_dir.chmod(stat.S_IRWXU)

    def test_run_propagates_oserror_when_checkpoint_dir_readonly(self, tmp_path):
        """A failure inside checkpoint() during run() must propagate — silent
        swallow would lose all post-failure epochs without any signal."""
        if os.geteuid() == 0:
            pytest.skip("running as root: filesystem permissions don't apply")

        s = _NoopScenario(**_make_kwargs(tmp_path))
        s.checkpoint_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(OSError):
                s.run(n_epochs=1)
        finally:
            s.checkpoint_dir.chmod(stat.S_IRWXU)
