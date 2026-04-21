#!/usr/bin/env bash
# Invoke one LLM-driven agent for one run. Called by cron or manually.
#
#   run.sh m3-staker
#
# Minimalism on purpose:
#   - --bare strips CLAUDE.md auto-discovery, plugin sync, auto-memory, and
#     all the built-in skills. Prevents token burn from loading dozens of
#     unrelated CLAUDE.md files and from the agent auto-invoking debug skills
#     when it sees permission denials.
#   - --dangerously-skip-permissions: threat model is one machine, one user,
#     test funds. Blast radius of a rogue agent is its own ~100 AP3X.
#   - --add-dir widens the session sandbox so the agent can read wallets,
#     SDK, docs without fighting denials.
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
# Why each flag matters (learned from quota-burn incident):
#   --bare  skips CLAUDE.md auto-discovery, plugin sync, auto-memory, hooks.
#           Vector-agent-modules has ~50 CLAUDE.md files; without --bare each
#           run loads them all. Also prevents the /fewer-permission-prompts
#           skill from being available (so the agent can't fall into it).
#   --add-dir extends the session sandbox. Without this, the agent can only
#           read from CWD and any attempt to read the SDK, wallet file, or
#           master wallet gets denied — the agent then burns tokens trying
#           workarounds.
#   --dangerously-skip-permissions is stronger than bypassPermissions; it
#           eliminates every permission check, which matches our threat
#           model (one machine, one user, test funds) and prevents the agent
#           from entering permission-debug loops.
if ! timeout --signal=TERM --kill-after=15s 600s \
      claude -p "$(cat "$PROMPT")" \
        --bare \
        --add-dir "$HOME/vector-agents/wallets" \
        --add-dir "$HOME/vector-agents/master" \
        --add-dir "$HOME/code/vector-agent-modules" \
        --add-dir "$HOME/code/agent-sdk-py" \
        --add-dir "$HOME/code/vector-ai-agents" \
        --dangerously-skip-permissions \
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
