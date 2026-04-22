"""
ScenarioRunner — abstract base for sim scenarios.

Sim Phase 2 iteration 1: GREEN implementation.

Design contract (locked by the test suite):

- Isolation per scenario: each runner owns an independent numpy.random.Generator
  seeded from rng_seed, a checkpoint file at <checkpoint_dir>/<name>.json, and a
  metrics file at <metrics_dir>/<name>.jsonl. No shared mutable state.

- Epoch loop: run(n_epochs, stop_event=None) iterates epoch counters
  self._epoch + 1 .. (self._epoch + 1 + n_epochs). Each iteration:
    1. call decide_and_act_for_epoch(epoch)
    2. append each returned event dict via emit_event
    3. checkpoint atomically (tmp file + os.replace)
    4. check stop_event.is_set() — if set, exit cleanly after final checkpoint.

- Checkpoint payload (JSON-serializable dict):
    {
      "name":            str,
      "epoch":           int,          # last completed epoch (-1 before any)
      "rng_state":       dict,         # numpy Generator.bit_generator.state
      "schema_version":  int,          # = 1 in this iteration
      "custom":          dict,         # from subclass _checkpoint_payload()
    }

- emit_event: thread-safe append of one JSON line to the metrics JSONL file.
  Each event is enriched with scenario name and ISO-8601 UTC timestamp before
  being serialized.

- Error policy: if decide_and_act_for_epoch raises, the runner emits a single
  error event (event_type="scenario_error", with exception class + message +
  traceback) and stops gracefully with a final checkpoint. The epoch counter
  is NOT advanced for the failing epoch — restore resumes at the last cleanly-
  completed epoch.
"""

from __future__ import annotations

import abc
import json
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


CHECKPOINT_SCHEMA_VERSION = 1


def _utc_iso_now() -> str:
    """Return current UTC time as an ISO-8601 string ending in 'Z'.

    Format example: '2026-04-15T14:03:22.118Z' (millisecond precision).
    """
    now = datetime.now(timezone.utc)
    # millisecond precision, trailing 'Z' for UTC.
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _jsonable_rng_state(state: dict) -> dict:
    """Convert a numpy bit_generator.state dict into a JSON-serializable form.

    numpy stores some integer fields as numpy ints / arrays of uint32; the
    JSON encoder handles plain int but not numpy types, so we coerce.
    """

    def _conv(v: Any) -> Any:
        # numpy scalar
        if isinstance(v, np.generic):
            return v.item()
        # numpy array
        if isinstance(v, np.ndarray):
            return [_conv(x) for x in v.tolist()]
        if isinstance(v, dict):
            return {k: _conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_conv(x) for x in v]
        return v

    return _conv(state)


class ScenarioRunner(abc.ABC):
    """Abstract base class for a single sim scenario.

    Subclasses MUST implement :meth:`decide_and_act_for_epoch`. They MAY
    override :meth:`_checkpoint_payload` to persist additional per-scenario
    state and :meth:`_restore_payload` to reconstruct it on resume.
    """

    def __init__(
        self,
        name: str,
        config: dict,
        deployment: Any,
        master_skey: Any,
        master_vkey: Any,
        master_wallet_addr: Any,
        checkpoint_dir: Path,
        metrics_dir: Path,
        rng_seed: int = 0,
    ):
        self.name = name
        self.config = config
        self.deployment = deployment
        self.master_skey = master_skey
        self.master_vkey = master_vkey
        self.master_wallet_addr = master_wallet_addr
        self.checkpoint_dir = Path(checkpoint_dir)
        self.metrics_dir = Path(metrics_dir)
        self.rng_seed = rng_seed

        # Ensure target dirs exist; safe if already present.
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        self.rng = np.random.default_rng(rng_seed)
        self._epoch: int = -1
        self._metrics_lock = threading.Lock()

        self.checkpoint_path: Path = self.checkpoint_dir / f"{name}.json"
        self.metrics_path: Path = self.metrics_dir / f"{name}.jsonl"

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def run(self, n_epochs: int, stop_event: "threading.Event | None" = None) -> None:
        """Run the scenario for up to ``n_epochs`` epochs.

        Starts at ``self._epoch + 1`` (so a fresh runner starts at epoch 0,
        a restored one resumes after the last completed epoch). Iterates
        ``n_epochs`` epochs total. Each iteration calls the subclass hook,
        emits its events, advances the epoch counter, and checkpoints.

        On subclass exception: emit a single ``scenario_error`` event,
        write a final checkpoint, and return without re-raising. The
        epoch counter is NOT advanced for the failing epoch.
        """
        start = self._epoch + 1
        end = start + n_epochs
        for epoch in range(start, end):
            try:
                events = self.decide_and_act_for_epoch(epoch)
            except Exception as exc:  # noqa: BLE001 — error policy is broad on purpose
                self.emit_event(
                    {
                        "event_type": "scenario_error",
                        "epoch": epoch,
                        "exception_class": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                # Final checkpoint — but do NOT advance _epoch past the
                # failing epoch (so restore resumes at the last clean one).
                self.checkpoint()
                return

            for ev in events:
                self.emit_event(ev)

            self._epoch = epoch
            self.checkpoint()

            if stop_event is not None and stop_event.is_set():
                return

    @abc.abstractmethod
    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        """Subclass scenario logic. Return a list of event dicts."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Checkpointing
    # ------------------------------------------------------------------ #

    def checkpoint(self) -> None:
        """Atomically persist scenario state to ``self.checkpoint_path``.

        Atomicity: write JSON to ``<path>.tmp`` then ``os.replace`` onto the
        final path. The replace is atomic on POSIX, so a crash mid-write
        leaves either the old file or the new file — never a torn file.
        """
        payload = {
            "name": self.name,
            "epoch": self._epoch,
            "rng_state": _jsonable_rng_state(self.rng.bit_generator.state),
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "custom": self._checkpoint_payload(),
        }
        tmp = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")
        # Write tmp, fsync-equivalent via flush+close, then atomic replace.
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            os.replace(tmp, self.checkpoint_path)
        except Exception:
            # Atomicity preserved (old file still valid) — clean up the tmp
            # so failed checkpoints don't leave residue, then re-raise.
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def restore(self) -> bool:
        """Load state from ``self.checkpoint_path`` if it exists.

        Returns True if state was restored, False if no prior checkpoint.
        Restores ``self._epoch``, the numpy Generator state, and invokes
        ``self._restore_payload(custom)`` so the subclass can reconstruct
        its own state.
        """
        if not self.checkpoint_path.exists():
            return False
        with open(self.checkpoint_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._epoch = int(data["epoch"])
        # numpy accepts the previously-serialized state dict directly.
        self.rng.bit_generator.state = data["rng_state"]
        self._restore_payload(data.get("custom", {}) or {})
        return True

    def _checkpoint_payload(self) -> dict:
        """Subclass override hook: return a JSON-serializable dict of scenario-
        specific state (agents, wallets, in-flight claims, etc.). Default {}."""
        return {}

    def _restore_payload(self, payload: dict) -> None:
        """Subclass override hook: reconstruct state from a prior
        _checkpoint_payload dict. Default no-op."""
        return None

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #

    def emit_event(self, event: dict) -> None:
        """Thread-safe append of one JSON line to ``self.metrics_path``.

        The event dict is enriched with ``"scenario"`` (= self.name) and
        ``"ts"`` (ISO-8601 UTC, millisecond precision, trailing 'Z') before
        serialization. The caller's dict is NOT mutated — we work on a copy.
        Uses self._metrics_lock so concurrent emit_event calls produce one
        complete JSON line per call (no torn writes).
        """
        enriched = dict(event)
        enriched["scenario"] = self.name
        enriched["ts"] = _utc_iso_now()
        line = json.dumps(enriched) + "\n"
        with self._metrics_lock:
            with open(self.metrics_path, "a", encoding="utf-8") as f:
                f.write(line)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Graceful shutdown — write a final checkpoint.

        Idempotent: callable any number of times without raising.
        """
        self.checkpoint()
