#!/usr/bin/env bash
# Invoke one LLM-driven agent for one run. Called by cron or manually.
#
#   run.sh m3-staker
#
# Minimalism on purpose:
#   - No permission sandbox beyond --permission-mode acceptEdits
#   - No flock (cron stagger handles it; if a run overshoots, next run just
#     queues and the 12h cadence absorbs it)
#   - 10-minute hard ceiling so a stuck run can't chew through the day

set -euo pipefail
umask 077

ROLE="${1:-}"
[[ -n "$ROLE" ]] || { echo "usage: $(basename "$0") <role>" >&2; exit 2; }
[[ "$ROLE" =~ ^m[1-9]-[a-z]+$ ]] || { echo "invalid role: $ROLE" >&2; exit 2; }

BASE="$HOME/vector-agents"
PROMPT="$BASE/prompts/$ROLE.md"
STATE_DIR="$BASE/state/$ROLE"
LOG="$BASE/logs/$ROLE.log"

[[ -f "$PROMPT" ]] || { echo "missing prompt: $PROMPT" >&2; exit 2; }
mkdir -p "$STATE_DIR" "$BASE/logs"

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

cd "$STATE_DIR"
{
  printf '\n=== %s START %s (pid=%d) ===\n' "$(date -Iseconds)" "$ROLE" "$$"
} >>"$LOG"

rc=0
if ! timeout --signal=TERM --kill-after=15s 600s \
      claude -p "$(cat "$PROMPT")" \
        --permission-mode acceptEdits \
        --model sonnet \
      >>"$LOG" 2>&1
then
  rc=$?
fi

{
  printf '[run] exit=%d\n'   "$rc"
  printf '=== %s END %s ===\n' "$(date -Iseconds)" "$ROLE"
} >>"$LOG"

exit "$rc"
