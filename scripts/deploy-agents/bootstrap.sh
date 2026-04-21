#!/usr/bin/env bash
# Bootstrap the Vector agent swarm on the server.
#
# Two-phase install:
#   Phase 1 (no args):   creates layout, generates master wallet, stops so
#                        you can fund it.
#   Phase 2 (--continue): generates 9 agent wallets, copies templates, funds
#                         agents from master, prints cron + systemd install
#                         commands.
#
# Safe to re-run either phase. Nothing destructive — existing files are kept.
# Template updates are synced where safe (scripts, settings) but per-agent
# markdown (CLAUDE.md, PROMPT.md, memory/) is left alone once created.

set -euo pipefail
umask 077                              # new files default to 600 / 700

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/scripts/deploy-agents"
BASE="$HOME/vector-agents"

# Tier-2 (cron) agents only. Tier-1 (Module-1) are driven by the systemd
# watcher service, not cron.
TIER2_AGENTS=(
  m3-staker m3-endorser m3-challenger
  m6-proposer m6-critic m6-endorser
)
TIER1_AGENTS=(
  m1-claimer m1-auditor m1-juror
)
ALL_AGENTS=( "${TIER1_AGENTS[@]}" "${TIER2_AGENTS[@]}" )

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Phase 0: prereq checks ────────────────────────────────────────────────

command -v claude >/dev/null   || die "claude CLI not found in PATH"
command -v python3 >/dev/null  || die "python3 not found"
command -v flock >/dev/null    || die "flock not found (install util-linux)"
command -v timeout >/dev/null  || die "timeout not found (install coreutils)"

python3 -c "import pycardano, cbor2, requests" 2>/dev/null \
  || die "Missing Python deps. Run: cd $REPO_ROOT/Module-3/python && pip install -e '.[dev]'"

# ── Phase 1: layout + master wallet ───────────────────────────────────────

log "Creating base layout at $BASE"
mkdir -p "$BASE"/{master,bin,bin/scripts,locks,logs,agents}
chmod 700 "$BASE/master" "$BASE/locks"

if [[ ! -L "$BASE/shared" ]]; then
  log "Symlinking shared → $REPO_ROOT"
  ln -s "$REPO_ROOT" "$BASE/shared"
fi

log "Installing run-agent.sh + approved scripts (always refresh; security-critical)"
install -m 0755 "$DEPLOY_DIR/templates/run-agent.sh"       "$BASE/bin/run-agent.sh"
install -m 0755 "$DEPLOY_DIR/bin-scripts/tier1_watcher.py" "$BASE/bin/tier1_watcher.py"
install -m 0755 "$DEPLOY_DIR/bin-scripts/chain.py"         "$BASE/bin/scripts/chain.py"
install -m 0755 "$DEPLOY_DIR/bin-scripts/faucet_request.py" "$BASE/bin/scripts/faucet_request.py"
install -m 0700 "$DEPLOY_DIR/templates/fund-agents.py"     "$BASE/master/fund-agents.py"

# Ship env.example (never overwrite the real env file if it exists).
install -m 0600 "$DEPLOY_DIR/templates/env.example"        "$BASE/env.example"

if [[ ! -f "$BASE/master/wallet.skey" ]]; then
  log "Generating master wallet"
  BASE_DIR="$BASE/master" python3 - <<'PY'
import os
from pathlib import Path
from pycardano import PaymentSigningKey, PaymentVerificationKey, Address, Network

base = Path(os.environ["BASE_DIR"])
skey = PaymentSigningKey.generate()
skey.save(str(base / "wallet.skey"))
vkey = PaymentVerificationKey.from_signing_key(skey)
addr = Address(payment_part=vkey.hash(), network=Network.MAINNET)
(base / "wallet.addr").write_text(str(addr) + "\n")
print(f"[bootstrap] Master address: {addr}")
PY
  chmod 600 "$BASE/master/wallet.skey"
else
  log "Master wallet already exists: $(cat "$BASE/master/wallet.addr")"
fi

if [[ "${1:-}" != "--continue" ]]; then
  cat <<EOF

========================================================================
Phase 1 complete.

Master wallet address:
    $(cat "$BASE/master/wallet.addr")

NEXT STEPS:
  1. Send ~1,000 AP3X to the address above from your personal wallet.
  2. Wait for the tx to confirm on chain (~40s).
  3. Re-run this script with:
         bash scripts/deploy-agents/bootstrap.sh --continue
========================================================================
EOF
  exit 0
fi

# ── Phase 2: per-agent scaffolding ────────────────────────────────────────

log "Scaffolding ${#ALL_AGENTS[@]} agent directories"

for agent in "${ALL_AGENTS[@]}"; do
  adir="$BASE/agents/$agent"
  src="$DEPLOY_DIR/agents/$agent"

  [[ -d "$src" ]] || die "Missing template dir: $src"
  mkdir -p "$adir/memory" "$adir/.claude"

  # wallet (idempotent)
  if [[ ! -f "$adir/wallet.skey" ]]; then
    log "  $agent: generating wallet"
    BASE_DIR="$adir" python3 - <<'PY'
import os
from pathlib import Path
from pycardano import PaymentSigningKey, PaymentVerificationKey, Address, Network

base = Path(os.environ["BASE_DIR"])
skey = PaymentSigningKey.generate()
skey.save(str(base / "wallet.skey"))
vkey = PaymentVerificationKey.from_signing_key(skey)
addr = Address(payment_part=vkey.hash(), network=Network.MAINNET)
(base / "wallet.addr").write_text(str(addr) + "\n")
print(f"[bootstrap]   {base.name} address: {addr}")
PY
    chmod 600 "$adir/wallet.skey"
  else
    log "  $agent: wallet exists"
  fi

  # Markdown templates — preserve user edits (no clobber).
  # CLAUDE.md is role charter + common guardrails concatenated on first install.
  if [[ ! -f "$adir/CLAUDE.md" ]]; then
    cat "$src/CLAUDE.md" "$DEPLOY_DIR/templates/common-guardrails.md" > "$adir/CLAUDE.md"
  fi
  cp -n "$src/PROMPT.md"                      "$adir/PROMPT.md"
  cp -n "$DEPLOY_DIR/templates/MEMORY.md"     "$adir/memory/MEMORY.md"

  # Settings — ALWAYS refresh (these are security-critical; never leave stale).
  # Substitute __AGENT_DIR__ → //absolute-agent-dir so allow/deny rules match
  # Claude Code's resolved absolute paths. The `//` prefix is required by
  # Claude Code's permission path-matcher to denote an absolute filesystem
  # path (a single `/` is interpreted as relative to the project root).
  python3 - "$DEPLOY_DIR/templates/settings.json" "$adir/.claude/settings.json" "$adir" <<'PY'
import sys, os
src, dst, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3]
content = open(src, "r", encoding="utf-8").read()
# Claude Code permission syntax: // prefix marks an absolute filesystem path.
content = content.replace("__AGENT_DIR__", "/" + agent_dir)
with open(dst, "w", encoding="utf-8") as f:
    f.write(content)
os.chmod(dst, 0o644)
PY

  # Seed empty state files.
  [[ -f "$adir/state.json"   ]] || echo '{}' > "$adir/state.json"
  [[ -f "$adir/journal.md"   ]] || printf '# Journal — %s\n\n' "$agent" > "$adir/journal.md"
  [[ -f "$adir/events.jsonl" ]] || : > "$adir/events.jsonl"
done

# ── Fund agents ───────────────────────────────────────────────────────────

log "Funding agents from master wallet (100 AP3X each)"
python3 "$BASE/master/fund-agents.py" || die "Funding failed — check master wallet balance and retry"

# ── Install systemd unit (tier-1 watcher) ─────────────────────────────────

USER_SYSTEMD="$HOME/.config/systemd/user"
mkdir -p "$USER_SYSTEMD"
install -m 0644 "$DEPLOY_DIR/systemd/vector-tier1.service" "$USER_SYSTEMD/vector-tier1.service"
log "systemd unit installed: $USER_SYSTEMD/vector-tier1.service"

# ── Prepare user-specific crontab ─────────────────────────────────────────

CRON_FILE="$BASE/crontab.generated.txt"
sed "s|USER_PLACEHOLDER|${USER}|g" "$DEPLOY_DIR/crontab.txt" > "$CRON_FILE"
log "Crontab rendered for user '$USER': $CRON_FILE"

# ── Done ──────────────────────────────────────────────────────────────────

cat <<EOF

========================================================================
Phase 2 complete.

BEFORE GOING LIVE, do these in order:

  1. Enable lingering so the systemd watcher survives logout:
         sudo loginctl enable-linger "$USER"

  2. Bootstrap each agent ONCE (it registers its DID + initial stake):
         ~/vector-agents/bin/run-agent.sh m3-staker
         ~/vector-agents/bin/run-agent.sh m3-endorser
         ~/vector-agents/bin/run-agent.sh m3-challenger
         ~/vector-agents/bin/run-agent.sh m6-critic
         ~/vector-agents/bin/run-agent.sh m6-endorser
         ~/vector-agents/bin/run-agent.sh m6-proposer
         # Tier-1 agents bootstrap via the watcher (next step).

  3. Start the tier-1 watcher (handles m1-claimer/auditor/juror):
         systemctl --user daemon-reload
         systemctl --user enable --now vector-tier1.service
         systemctl --user status vector-tier1.service
     NOTE: the watcher's event detection is a skeleton — see
     scripts/deploy-agents/bin-scripts/tier1_watcher.py TODO markers.
     Until filled in, tier-1 agents bootstrap but never trigger otherwise.

  4. Install the tier-2 cron schedule (6 agents, 12h loop):
         crontab $CRON_FILE
         crontab -l   # verify

MONITOR:
     tail -f ~/vector-agents/logs/*.log
     systemctl --user status vector-tier1.service
     cat ~/vector-agents/master/faucet-ledger.json   # per-agent cap usage
========================================================================
EOF
