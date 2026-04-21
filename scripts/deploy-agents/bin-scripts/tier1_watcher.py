#!/usr/bin/env python3
"""Tier-1 watcher for Module-1 agents (Claimer / Auditor / Juror).

Polls Vector chain every POLL_INTERVAL_S seconds. On detecting an actionable
event for one of the three Module-1 agents, it:
  1. Acquires the agent's run-agent.sh flock (blocking, short timeout) so
     state.json writes don't race with the agent.
  2. Writes a sanitized `pending_event` into state.json.
  3. Triggers the agent via run-agent.sh subprocess.

The watcher does all chain decoding deterministically in Python; Claude
Code is only invoked on actionable state transitions.

Logs go to stdout/stderr; systemd redirects to
~/vector-agents/logs/tier1-watcher.log. No Python FileHandler — avoids
double-writing the log.

IMPORTANT — what's NOT complete in this skeleton:
The `events_for_*` functions contain TODO markers where Module-1 chain
decoding belongs. Currently each returns (False, None) unless did.json is
missing (bootstrap trigger). See README.md §"Completing tier-1 event detection".
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple

BASE = Path.home() / "vector-agents"
sys.path.insert(0, str(BASE / "shared/Module-3/python"))
from reputation_staking.ogmios_backend import OgmiosHttpContext  # noqa: E402

POLL_INTERVAL_S = 30
COOLDOWN_S = 180
MAX_TRIGGERS_PER_HOUR = 8
STATE_FILE = BASE / "tier1.state.json"
RUN_AGENT = BASE / "bin" / "run-agent.sh"
LOCK_DIR = BASE / "locks"

AGENTS = ("m1-claimer", "m1-auditor", "m1-juror")
AGENT_RE = re.compile(r"^m[1-9]-[a-z]+$")

# Allowed `kind` values in pending_event. Any event we'd ever inject MUST
# use one of these. Unknown kinds from the decoder are dropped.
ALLOWED_EVENT_KINDS = {
    "bootstrap",
    "claim_challenged",
    "claim_resolved",
    "falsifiable_claim",
    "challenge_resolved",
    "reveal_window",
    "commit_window",
}

# Safe-shape for event values: string fields max 200 chars, charset
# [A-Za-z0-9:/_.\-], int fields clamped.
_SAFE_STR = re.compile(r"^[A-Za-z0-9:/_.\-]{0,200}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tier1")

_running = True


def _handle_sigterm(signum: int, _frame: Any) -> None:
    global _running
    log.info("received signal %d; shutting down after current tick", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# ── Watcher's own state (atomic writes) ───────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("state file corrupt, starting fresh")
    return {a: {"last_trigger_ts": 0.0, "triggers_this_hour": []} for a in AGENTS}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def can_trigger(state: dict, agent: str) -> bool:
    now = time.time()
    s = state[agent]
    if now - s["last_trigger_ts"] < COOLDOWN_S:
        return False
    s["triggers_this_hour"] = [t for t in s["triggers_this_hour"] if now - t < 3600]
    return len(s["triggers_this_hour"]) < MAX_TRIGGERS_PER_HOUR


def record_trigger(state: dict, agent: str) -> None:
    now = time.time()
    state[agent]["last_trigger_ts"] = now
    state[agent]["triggers_this_hour"].append(now)


# ── Event sanitization ────────────────────────────────────────────────────


def sanitize_event(event: dict) -> Optional[dict]:
    """Accept only whitelisted `kind` values and safe-shape string/int fields.

    Anything weird → return None, we don't inject the event. Agent still
    runs, just without event context (falls to null-branch in PROMPT.md).
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    if kind not in ALLOWED_EVENT_KINDS:
        log.warning("dropping event with disallowed kind=%r", kind)
        return None
    clean: dict = {"kind": kind}
    for k, v in event.items():
        if k == "kind":
            continue
        if isinstance(v, int):
            clean[k] = max(-2**63, min(2**63 - 1, v))
        elif isinstance(v, str):
            if _SAFE_STR.match(v):
                clean[k] = v
            else:
                log.warning("dropping unsafe string in event[%s]", k)
        else:
            log.warning("dropping non-str/int field event[%s] type=%s", k, type(v).__name__)
    return clean


# ── Agent state.json injection (flocked, atomic) ──────────────────────────


def write_agent_event(agent: str, event: dict) -> None:
    """Inject a `pending_event` into the agent's state.json.

    Coordinates with run-agent.sh's flock on the same file so we don't
    race with a running agent. If we can't acquire the lock within 10s
    (agent is running a long claude call), we skip this update — the next
    tick will try again.
    """
    agent_state = BASE / "agents" / agent / "state.json"
    lock_path = LOCK_DIR / f"{agent}.lock"
    LOCK_DIR.mkdir(parents=True, exist_ok=True)

    # Open lock file; hold exclusive flock for the read-modify-write.
    # run-agent.sh uses `flock -n` (non-blocking), so if an agent is
    # actively running we'll block here for up to 10s waiting.
    deadline = time.time() + 10
    with open(lock_path, "w") as lock_fd:
        while True:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    log.warning("could not acquire lock for %s in 10s; skipping inject", agent)
                    return
                time.sleep(0.2)

        try:
            current = json.loads(agent_state.read_text()) if agent_state.exists() else {}
            if not isinstance(current, dict):
                log.warning("agent %s state.json unparseable; leaving event uninjected", agent)
                return
        except json.JSONDecodeError:
            log.warning("agent %s state.json invalid JSON; leaving event uninjected", agent)
            return

        current["pending_event"] = event
        tmp = agent_state.with_suffix(agent_state.suffix + ".tmp")
        tmp.write_text(json.dumps(current, indent=2))
        os.replace(tmp, agent_state)
        # flock released when lock_fd closes at end of `with` block.


# ── Event detection (STUBS — fill in using Module-1 SDK) ─────────────────
#
# Each returns (bool, dict-or-None). The bool says "trigger". The dict is
# the context to inject; sanitize_event() validates it.


def events_for_m1_claimer(_ctx: OgmiosHttpContext, agent_dir: Path) -> Tuple[bool, Optional[dict]]:
    if not (agent_dir / "did.json").exists():
        return True, {"kind": "bootstrap"}
    # TODO: Module-1 ClaimValidator UTxO decoding
    return False, None


def events_for_m1_auditor(_ctx: OgmiosHttpContext, agent_dir: Path) -> Tuple[bool, Optional[dict]]:
    if not (agent_dir / "did.json").exists():
        return True, {"kind": "bootstrap"}
    return False, None


def events_for_m1_juror(_ctx: OgmiosHttpContext, agent_dir: Path) -> Tuple[bool, Optional[dict]]:
    if not (agent_dir / "did.json").exists():
        return True, {"kind": "bootstrap"}
    return False, None


EVENT_FN = {
    "m1-claimer": events_for_m1_claimer,
    "m1-auditor": events_for_m1_auditor,
    "m1-juror":   events_for_m1_juror,
}


# ── Main loop ─────────────────────────────────────────────────────────────


def trigger(agent: str, event: Optional[dict]) -> None:
    if not AGENT_RE.match(agent):
        log.error("refusing to trigger malformed agent name: %r", agent)
        return
    if event:
        clean = sanitize_event(event)
        if clean:
            try:
                write_agent_event(agent, clean)
            except Exception:
                log.exception("failed to inject event for %s; proceeding without context", agent)
    log.info("triggering %s event=%s", agent, (event or {}).get("kind"))
    try:
        subprocess.run(
            [str(RUN_AGENT), agent],
            timeout=650,        # slightly > run-agent.sh's MAX_RUN_S=600
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.error("run-agent.sh %s timed out after 650s", agent)
    except Exception:
        log.exception("run-agent.sh %s failed", agent)


def tick(ctx: OgmiosHttpContext, state: dict) -> None:
    for agent in AGENTS:
        try:
            if not can_trigger(state, agent):
                continue
            agent_dir = BASE / "agents" / agent
            should_fire, event = EVENT_FN[agent](ctx, agent_dir)
            if should_fire:
                record_trigger(state, agent)
                save_state(state)
                trigger(agent, event)
        except Exception:
            log.exception("tick failed for %s; will retry next poll", agent)


def main() -> int:
    log.info("tier1-watcher starting (poll=%ds, cooldown=%ds, cap=%d/h)",
             POLL_INTERVAL_S, COOLDOWN_S, MAX_TRIGGERS_PER_HOUR)
    ctx = OgmiosHttpContext()
    state = load_state()
    while _running:
        tick(ctx, state)
        for _ in range(POLL_INTERVAL_S):
            if not _running:
                break
            time.sleep(1)
    log.info("tier1-watcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
