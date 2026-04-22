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

# ── Per-role model selection ─────────────────────────────────────────────
# Default model is Haiku (cheap, fast). Roles that need robust error
# recovery or on-chain branch decisions get Sonnet — Haiku tends to invent
# "blockers" and never acknowledge successful submissions, burning stake
# silently. See MODEL_OVERRIDES below.
DEFAULT_MODEL="claude-haiku-4-5"
case "$ROLE" in
  # m6-proposer: Haiku hallucinated "cannot submit" while MCP was actually
  # broadcasting; Sonnet correctly reconciled prior submissions and did
  # on-chain-error-driven fallback (ParameterChange → GeneralSuggestion).
  # m3-staker: multi-step seed+stake flow needs consistent reconciliation
  # across runs; Haiku discarded a successful stake because pending_tx was
  # >2h old and wasted 14 AP3X on a redundant seed. Sonnet also needs to
  # respond to challenges (non-optional) — that's a correctness-critical path.
  m6-proposer|m3-staker) MODEL="claude-sonnet-4-6" ;;
  *)                     MODEL="$DEFAULT_MODEL"    ;;
esac

# Environment override wins, for quick A/B testing without committing:
#   MODEL=claude-sonnet-4-6 ~/vector-agents/run.sh m6-critic
MODEL="${MODEL_OVERRIDE:-$MODEL}"

BASE="$HOME/vector-agents"
PROMPT="$BASE/prompts/$ROLE.md"
STATE_DIR="$BASE/state/$ROLE"
LOG="$BASE/logs/$ROLE.log"

[[ -f "$PROMPT" ]] || { echo "missing prompt: $PROMPT" >&2; exit 2; }
mkdir -p "$STATE_DIR" "$BASE/logs"

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

cd "$STATE_DIR"
{
  printf '\n=== %s START %s (pid=%d, model=%s) ===\n' "$(date -Iseconds)" "$ROLE" "$$" "$MODEL"
} >>"$LOG"

rc=0
# Why each flag matters (learned from quota-burn incident):
#   --add-dir extends the session sandbox. Without this, the agent can only
#           read from CWD and any attempt to read the SDK, wallet file, or
#           master wallet gets denied — the agent then burns tokens trying
#           workarounds.
#   --dangerously-skip-permissions is stronger than bypassPermissions; it
#           eliminates every permission check, which matches our threat
#           model (one machine, one user, test funds) and prevents the agent
#           from entering permission-debug loops (the main quota burner
#           observed in the first smoke tests — the agent would invoke
#           /fewer-permission-prompts skill recursively).
#
# Note: we used to also pass --bare to skip CLAUDE.md auto-discovery and
# plugin sync, but --bare disables OAuth auth ("OAuth and keychain are never
# read"), which breaks our Pro-subscription login. Since the agent's CWD
# (~/vector-agents/state/<role>/) has no CLAUDE.md in its ancestry, auto-
# discovery doesn't pull in the repo's CLAUDE.md files anyway. Plugin sync
# still runs but that's a small fixed cost.
APPEND_SYSTEM='You are running in headless cron mode — there is NO human on the other end. Do not ask clarifying questions. Do not present menus. Do not propose next steps for approval. Do not defer execution to "a future run" or fabricate environmental problems to avoid acting. If you encounter a real technical error, attempt a concrete fix, and if that fails, record the exact error (stderr, traceback) in journal.md and exit. Your single deliverable per run is: either one on-chain tx broadcast OR one journaled reason why no tx was warranted. Never both narrate and defer.'

if ! timeout --signal=TERM --kill-after=15s 600s \
      claude -p "$(cat "$PROMPT")" \
        --append-system-prompt "$APPEND_SYSTEM" \
        --add-dir "$HOME/vector-agents/wallets" \
        --add-dir "$HOME/vector-agents/master" \
        --add-dir "$HOME/code/vector-agent-modules" \
        --add-dir "$HOME/code/agent-sdk-py" \
        --add-dir "$HOME/code/vector-ai-agents" \
        --dangerously-skip-permissions \
        --model "$MODEL" \
      >>"$LOG" 2>&1
then
  rc=$?
fi

{
  printf '[run] exit=%d\n'   "$rc"
  printf '=== %s END %s ===\n' "$(date -Iseconds)" "$ROLE"
} >>"$LOG"

exit "$rc"
