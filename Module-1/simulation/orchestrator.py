"""
SimOrchestrator — concurrent lifecycle manager for ScenarioRunner instances.

Sim Phase 2 iteration 2: GREEN implementation (Catherine).

Design contract (locked by the test suite at
``simulation/tests/test_sim_orchestrator.py``):

- Registration:
    * ``register(scenario)`` adds a ScenarioRunner. Duplicate ``scenario.name``
      raises ValueError. Registration after ``start_all`` raises RuntimeError.

- Lifecycle (single-shot per orchestrator):
    * ``start_all(n_epochs)`` spawns one daemon thread per registered scenario
      and returns immediately (non-blocking). Each thread invokes
      ``scenario.run(n_epochs, stop_event=<orchestrator's event>)``.
    * Calling ``start_all`` a second time raises RuntimeError.
    * ``stop_all(timeout=30.0)`` sets the shared stop_event and joins all
      threads with the given timeout. Idempotent: calling it twice (or before
      ``start_all``) is a no-op and never raises. Writes a final
      ``orchestrator_status.json`` snapshot before returning.
    * ``wait(timeout=None)`` joins all threads. Returns True iff every thread
      finished within the timeout (or all already finished); False if any
      thread is still alive when the timeout elapses.

- Shared stop_event:
    * If ``__init__`` is given a ``threading.Event``, that exact instance is
      used. If None, the orchestrator creates one. The event is exposed as
      ``self.stop_event``.

- Status:
    * ``status()`` returns a dict keyed by scenario name with
      ``{"alive": bool, "epoch": int, "error": dict | None}``.
      ``alive`` reflects current thread state (True between start and join,
      False before start or after the thread has terminated). ``epoch`` is
      ``scenario._epoch`` (last completed epoch, -1 before any). ``error`` is
      a ``{"class": "<ExceptionClass>", "message": "<msg>"}`` dict when the
      scenario terminated via a ``scenario_error`` event (i.e.
      ``decide_and_act_for_epoch`` raised), else None. The JSONL metrics file
      is the source of truth for ``error`` — the orchestrator does NOT wrap
      the worker thread in try/except.
    * Every call to ``status()`` ALSO writes/refreshes the aggregate snapshot
      file ``orchestrator_status.json`` under the orchestrator's own
      ``checkpoint_dir`` (see Aggregate snapshot below). Concurrent calls
      from multiple threads are serialised by an internal lock so the file
      is never corrupted and the in-memory dict returned to the caller is
      a coherent snapshot.

- Aggregate snapshot (``orchestrator_status.json``):
    * Path: ``<checkpoint_dir>/orchestrator_status.json`` (the
      ``checkpoint_dir`` passed to ``__init__``).
    * Atomic write: serialise to ``<path>.tmp`` first, then ``os.replace``
      onto the final path. No ``.tmp`` file should remain after a successful
      write.
    * Refreshed on every ``status()`` call AND on ``stop_all()`` (final
      snapshot reflecting post-join state).
    * Payload shape::

          {
              "scenarios": {
                  "<name>": {"alive": bool, "epoch": int, "error": dict|None},
                  ...
              },
              "ts": "<ISO-8601 UTC, ending in 'Z'>"
          }

    * Calling ``status()`` before ``start_all`` writes a snapshot whose
      ``scenarios`` mirrors the registered set (or is empty if none) and
      whose ``ts`` is a valid ISO-8601 UTC timestamp.

- Isolation:
    * Each scenario runs in its own thread with its own checkpoint/metrics
      files (the scenario was constructed with its own dirs upstream — the
      orchestrator does NOT reassign them). One scenario raising must not
      interrupt or affect siblings.

- Threads are daemon=True so a runaway sim never blocks process exit.

The orchestrator's own ``checkpoint_dir`` and ``metrics_dir`` are created
at construction time. ``checkpoint_dir`` hosts ``orchestrator_status.json``
(see above). ``metrics_dir`` is reserved for future aggregated metrics.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from simulation.scenario import ScenarioRunner


def _utc_iso_now() -> str:
    """Return current UTC time as an ISO-8601 string ending in 'Z'.

    Format mirrors ``simulation.scenario._utc_iso_now`` (millisecond precision)
    so cross-file timestamps are directly comparable.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class SimOrchestrator:
    """Manage concurrent execution of multiple ScenarioRunner instances.

    See module docstring for the full contract.
    """

    #: Filename of the aggregate snapshot under ``checkpoint_dir``.
    STATUS_FILENAME = "orchestrator_status.json"

    def __init__(
        self,
        checkpoint_dir: Path,
        metrics_dir: Path,
        stop_event: "threading.Event | None" = None,
    ):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.metrics_dir = Path(metrics_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        self.stop_event = stop_event if stop_event is not None else threading.Event()

        # Insertion-ordered map of name -> scenario.
        self._scenarios: dict[str, ScenarioRunner] = {}
        # Insertion-ordered map of name -> Thread (populated by start_all).
        self._threads: dict[str, threading.Thread] = {}

        # Single lock guards: snapshot construction + status-file write.
        self._snapshot_lock = threading.Lock()

        self._started: bool = False

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register(self, scenario: ScenarioRunner) -> None:
        """Register a scenario. Raises ValueError on duplicate name; raises
        RuntimeError if called after ``start_all``."""
        if self._started:
            raise RuntimeError(
                "SimOrchestrator.register: cannot register after start_all"
            )
        if scenario.name in self._scenarios:
            raise ValueError(
                f"SimOrchestrator.register: duplicate scenario name {scenario.name!r}"
            )
        self._scenarios[scenario.name] = scenario

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start_all(self, n_epochs: int) -> None:
        """Spawn one daemon thread per registered scenario and return.

        Each thread calls ``scenario.run(n_epochs, stop_event=self.stop_event)``.
        Non-blocking. Raises RuntimeError if called more than once.
        """
        if self._started:
            raise RuntimeError("SimOrchestrator.start_all: already started")
        self._started = True
        for name, scenario in self._scenarios.items():
            t = threading.Thread(
                target=scenario.run,
                args=(n_epochs, self.stop_event),
                daemon=True,
                name=f"sim-orch-{name}",
            )
            self._threads[name] = t
            t.start()

    def stop_all(self, timeout: float = 30.0) -> None:
        """Signal stop, join all threads with a timeout, and write a final
        ``orchestrator_status.json`` snapshot.

        Idempotent: safe to call multiple times; safe to call before
        ``start_all`` (no-op apart from the snapshot write).
        """
        if not self._started:
            # Pre-start: no threads to signal/join. Still refresh snapshot
            # for observability (matches status()'s side-effect contract).
            self.status()
            return

        self.stop_event.set()
        for t in self._threads.values():
            t.join(timeout=timeout)

        # Final snapshot reflects post-join state.
        self.status()

    def wait(self, timeout: "float | None" = None) -> bool:
        """Block until all scenario threads finish.

        Returns True iff every thread finished within ``timeout`` seconds
        (or all already finished); False if any thread is still alive when
        the timeout elapses.

        MUST NOT set ``self.stop_event`` — only ``stop_all`` does that.
        """
        if not self._threads:
            return True

        # If timeout is None, simply join each in turn (joins block forever).
        if timeout is None:
            for t in self._threads.values():
                t.join()
            return True

        deadline = _monotonic() + timeout
        for t in self._threads.values():
            remaining = deadline - _monotonic()
            if remaining <= 0:
                # Out of budget — check whether already done.
                if t.is_alive():
                    return False
                continue
            t.join(timeout=remaining)
            if t.is_alive():
                return False
        # Final sweep: confirm every thread really did terminate.
        return all(not t.is_alive() for t in self._threads.values())

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, dict]:
        """Return ``{name: {"alive": bool, "epoch": int, "error": dict|None}}``
        for every registered scenario. Also (atomically) refreshes the
        aggregate ``orchestrator_status.json`` snapshot under
        ``checkpoint_dir``."""
        with self._snapshot_lock:
            snapshot: dict[str, dict] = {}
            for name, scenario in self._scenarios.items():
                t = self._threads.get(name)
                alive = bool(t.is_alive()) if t is not None else False
                epoch = int(getattr(scenario, "_epoch", -1))
                error = self._read_last_scenario_error(scenario)
                snapshot[name] = {
                    "alive": alive,
                    "epoch": epoch,
                    "error": error,
                }
            self._write_status_snapshot(snapshot)
            return snapshot

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _read_last_scenario_error(scenario: ScenarioRunner) -> "dict | None":
        """Tail the scenario's JSONL metrics file for the last
        ``event_type == "scenario_error"`` event.

        Returns ``{"class": <exception_class>, "message": <message>}`` for
        the most recent such event, or None if the file is missing/empty
        or contains no scenario_error events.

        Defensive against malformed JSON lines (skip them) since the metrics
        file is a streaming append target.
        """
        path = getattr(scenario, "metrics_path", None)
        if path is None:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return None
        except OSError:
            return None

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("event_type") == "scenario_error":
                return {
                    "class": entry.get("exception_class", ""),
                    "message": entry.get("message", ""),
                }
        return None

    def _write_status_snapshot(self, snapshot: dict[str, dict]) -> None:
        """Atomically write ``orchestrator_status.json`` under
        ``checkpoint_dir``.

        Implementation contract:
          * Build payload ``{"scenarios": <snapshot>, "ts": "<UTC ISO-8601 Z>"}``.
          * Write to ``<path>.tmp`` then ``os.replace`` onto the final path.
          * On exception, attempt to unlink the ``.tmp`` to avoid leftover
            residue, then re-raise the original exception.
          * Caller is responsible for taking the snapshot lock.
        """
        path = self.checkpoint_dir / self.STATUS_FILENAME
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"scenarios": snapshot, "ts": _utc_iso_now()}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            try:
                os.replace(tmp, path)
            except Exception:
                # Atomicity preserved (old file still valid) but tmp may
                # remain — clean it up so no residue is left.
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        except Exception:
            # Best-effort tmp cleanup if write itself failed before replace.
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


# Imported here (and not at top) to keep the public surface tidy. ``time`` is
# only used by ``wait()``.
def _monotonic() -> float:
    import time

    return time.monotonic()
