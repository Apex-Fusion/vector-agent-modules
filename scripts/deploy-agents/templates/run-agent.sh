#!/usr/bin/env bash
# Headless Claude Code wrapper. Called by cron OR the tier-1 watcher.
#
#   run-agent.sh m1-juror
#
# Guarantees:
#  - rejects malformed agent names (regex gate) before touching the filesystem
#  - umask 077 so logs/state/journal aren't world-readable
#  - never starts a second instance of the same agent (flock)
#  - hard-kills claude after MAX_RUN_S
#  - auto-denies any unmatched tool call (permission-mode dontAsk) → no TTY prompts
#  - cd's into agent dir so CLAUDE.md + memory/MEMORY.md auto-load
#  - propagates claude's exit status (systemd/cron can detect failures)
#  - writes output to ~/vector-agents/logs/<agent>.log

set -euo pipefail
umask 077

export PATH="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH:-}"

AGENT="${1:-}"
[[ -n "$AGENT" ]] || { echo "usage: $(basename "$0") <agent-name>" >&2; exit 2; }

# Sanitize AGENT — must look like 'm1-juror', 'm3-staker', etc.
if [[ ! "$AGENT" =~ ^m[1-9]-[a-z]+$ ]]; then
  echo "invalid agent name: $AGENT" >&2
  exit 2
fi

BASE="${HOME}/vector-agents"
DIR="$BASE/agents/$AGENT"
LOG="$BASE/logs/$AGENT.log"
LOCK="$BASE/locks/$AGENT.lock"
MAX_RUN_S=600

[[ -d "$DIR" ]]           || { echo "No such agent dir: $DIR" >&2; exit 2; }
[[ -f "$DIR/PROMPT.md" ]] || { echo "Missing $DIR/PROMPT.md" >&2; exit 2; }
mkdir -p "$BASE/locks" "$BASE/logs"

# ── Single-instance guard ────────────────────────────────────────────────
exec 9>"$LOCK"
if ! flock -n 9; then
  printf '[%s] %s: another run in progress; skipping\n' "$(date -Iseconds)" "$AGENT" \
    >>"$LOG"
  exit 0
fi

cd "$DIR"

{
  printf '\n=== %s START %s (pid=%d) ===\n' "$(date -Iseconds)" "$AGENT" "$$"
} >>"$LOG" 2>&1

rc=0
# --permission-mode dontAsk: auto-deny anything not in allow/deny (no TTY
# prompt, no cron hang). Combined with the comprehensive settings.json
# deny list, unmatched tool calls fail fast instead of blocking.
if ! timeout --signal=TERM --kill-after=15s "${MAX_RUN_S}s" \
      claude -p "$(cat PROMPT.md)" \
        --model sonnet \
        --permission-mode dontAsk \
        --output-format text \
      >>"$LOG" 2>&1
then
  rc=$?
fi

{
  printf '[run-agent] claude exit=%d\n' "$rc"
  printf '=== %s END %s ===\n'            "$(date -Iseconds)" "$AGENT"
} >>"$LOG" 2>&1

exit "$rc"
