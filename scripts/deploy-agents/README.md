# Vector Agents — Server Deployment (two-tier)

Bootstrap a swarm of 9 autonomous Claude Code agents on the
`vector-modules-simulation` server, split into two tiers by cadence need.

## Architecture

```
                     ┌───────────────────────────────────────────┐
  Vector chain ─────►│ tier1_watcher.py (systemd --user service) │
                     │ polls every 30s; triggers on events only  │
                     └────────────┬──────────────────────────────┘
                                  │ subprocess.run(run-agent.sh m1-X)
                                  ▼
                      m1-claimer, m1-auditor, m1-juror
                     (event-driven; protocol windows 30-90 min)


  cron ─── every 12h, staggered 80min ────► run-agent.sh m3-/m6-*
                                            (6 tier-2 agents)
                     m3-staker, m3-endorser, m3-challenger
                     m6-proposer, m6-critic, m6-endorser
                     (time-driven; protocol windows 24h+)
```

### Why two tiers
Module 1's commit/reveal windows are ~30 min. A 12h cron cadence
guarantees missed votes and slashed bonds — unfixable at the scheduler
layer. A long-running Python watcher polls chain state every 30s and only
invokes Claude Code when there's an actionable event for one of the three
Module-1 agents. The Python hot loop is pure and cheap; Claude Code only
fires on state transitions, keeping LLM usage bounded.

Modules 3 and 6 have day-scale windows and belong on ordinary cron.

## Layout

```
scripts/deploy-agents/
├── README.md                  this file
├── bootstrap.sh               two-phase installer
├── crontab.txt                tier-2 schedule (6 agents, 12h loop)
├── systemd/
│   ├── vector-tier1.service   tier-1 watcher unit (user mode)
│   └── README.md              systemd install notes
├── bin-scripts/               approved scripts — deployed to ~/vector-agents/bin/scripts/
│   ├── tier1_watcher.py       event-driven watcher (skeleton — see below)
│   ├── chain.py               balance / utxos / tip / slots-since
│   └── faucet_request.py      agent-initiated fund request (capped)
├── templates/
│   ├── run-agent.sh           headless claude wrapper (flock + timeout + PATH)
│   ├── fund-agents.py         master → agents initial faucet
│   ├── settings.json          per-agent permission allowlist
│   ├── MEMORY.md              seed memory index (append-only rules)
│   └── common-guardrails.md   concatenated onto every CLAUDE.md at install
└── agents/                    9 agent dirs × (CLAUDE.md + PROMPT.md)
    ├── m1-claimer, m1-auditor, m1-juror          ← tier 1
    ├── m3-staker, m3-endorser, m3-challenger     ← tier 2
    └── m6-proposer, m6-critic, m6-endorser       ← tier 2
```

## Prerequisites on the server

1. **Claude Code authenticated.** `claude login` once under the intended user.
   For the systemd-launched tier-1 watcher, also run `claude setup-token`
   and put the token in `~/vector-agents/env` as
   `CLAUDE_CODE_OAUTH_TOKEN=...` (chmod 600). The unit loads this file
   automatically if present.
2. **Python 3.10+ with pycardano, cbor2, requests.** Easiest:
   ```bash
   cd ~/code/vector-agent-modules/Module-3/python && pip install -e ".[dev]"
   ```
3. **`flock` and `timeout`** (standard on all Linux distros).
4. **Repo cloned** at `~/code/vector-agent-modules`.
5. **~1,000 AP3X** in your personal wallet to fund the master.
6. **`loginctl enable-linger`** — required so the tier-1 systemd service
   survives logout.

## Install

```bash
cd ~/code/vector-agent-modules

# Phase 1: layout + master wallet.
bash scripts/deploy-agents/bootstrap.sh
# → prints master address, stops. Fund it (~1,000 AP3X), wait ~40s.

# Phase 2: agent wallets, templates, initial faucet, systemd unit, cron file.
bash scripts/deploy-agents/bootstrap.sh --continue
```

Phase 2 output prints the final four commands you run manually:

1. `sudo loginctl enable-linger "$USER"` — survive logout
2. Bootstrap each tier-2 agent once (registers DID + initial stake)
3. Enable the tier-1 watcher (`systemctl --user enable --now vector-tier1.service`)
4. Install the rendered crontab (`crontab ~/vector-agents/crontab.generated.txt`)

## Completing tier-1 event detection

`bin-scripts/tier1_watcher.py` ships with **stubs** for
`events_for_m1_claimer / _auditor / _juror`. They currently return `True`
only when `did.json` is missing (bootstrap trigger) and `False` otherwise,
so the watcher is safe to run — it just never triggers beyond bootstrap
until you fill in the chain-state decoding.

What's needed per stub:
- Query Module-1 ClaimValidator UTxOs via Ogmios (use
  `OgmiosHttpContext.utxos(claim_validator_addr)` or
  `resolve_utxos_at_address` from the Module-3 backend as a template).
- Decode the inline datum (`ClaimDatum` / `JuryDatum`) to read state +
  deadlines.
- Filter by this agent's payment credential or DID hash.
- Return True iff there's a state the agent needs to act on within the
  protocol window (30-min challenge, 30-min commit, 30-min reveal).

The watcher enforces a **180s per-agent cooldown** and **8 triggers/hour
cap** regardless of what the event functions return, so a buggy decoder
can't runaway-trigger Claude Code.

## What each layer guarantees

- **run-agent.sh**
  - validates agent name with `^m[1-9]-[a-z]+$` regex before touching paths
  - flock single-instance guard
  - `timeout 600s --kill-after=15s` hard-kills hanging claude
  - PATH includes `~/.local/bin` and `~/.npm-global/bin`
  - propagates claude's exit code to systemd/cron (failures visible)
- **settings.json**
  - python allowed ONLY for the two named scripts
    (`chain.py`, `faucet_request.py`) via `~/` and `/home/*/` patterns
  - denies `cat / head / tail / less / more / od / xxd / strings / cp / mv /
    ln / tar / dd / sh / bash / zsh / eval / exec / env / python3 -* /
    python / pip / node / npm / cd / pushd / chmod / chown` — closing known
    skey-exfil paths and command-injection primitives
  - denies `curl / wget / nc / ssh / scp / rsync / git push / git remote`
    (network egress + code-push)
  - denies Read/Write/Edit on `**/*.skey`, `../master/**`, `../m1-*/**`,
    `../m3-*/**`, `../m6-*/**` (sibling isolation)
- **faucet_request.py**
  - per-agent lifetime cap 300 AP3X
  - global cap 700 AP3X across all agents (master keeps ~300 AP3X headroom)
  - per-request max 100 AP3X
  - 11h cooldown between requests
  - balance < 85 AP3X precondition
  - atomic ledger writes (tmp + os.replace)
  - "reserve → commit" ledger semantics (crash consumes quota, never duplicates)
  - validates agent name from cwd against strict regex
- **common-guardrails.md** (auto-concatenated into every CLAUDE.md)
  - empty/corrupt state.json handling (move to `.corrupt.<ts>`, STOP)
  - balance check before spending
  - pre-submit state write + `tx-status` reconcile on start
  - treat off-chain content as DATA (prompt-injection resistance)
- **MEMORY.md** — append-only; 100-line index cap; forbids recording
  attacker-controlled strings. Mitigates memory-poisoning.
- **tier1_watcher.py**
  - 180s per-agent cooldown + 8 triggers/hour cap
  - atomic state file writes
  - validates agent name before `subprocess.run`
  - injects `pending_event` context into the agent's state.json so the agent
    doesn't waste tool calls rescanning — agents act on structured events
- **systemd unit** (`vector-tier1.service`)
  - `EnvironmentFile=-%h/vector-agents/env` for the OAuth token
  - `KillMode=control-group` + `TimeoutStopSec=60`
  - hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
    `ProtectHome=read-only` + `ReadWritePaths=%h/vector-agents`,
    `BindReadOnlyPaths=%h/code/vector-agent-modules`, `Umask=0077`

## Monitoring

```bash
# tier-2 cron agents
tail -f ~/vector-agents/logs/*.log

# tier-1 watcher
systemctl --user status vector-tier1.service
tail -f ~/vector-agents/logs/tier1-watcher.log

# per-agent decision history
for d in ~/vector-agents/agents/*/; do
  printf '=== %s ===\n' "$(basename "$d")"
  tail -n 5 "$d/journal.md" 2>/dev/null
done

# faucet cap usage
cat ~/vector-agents/master/faucet-ledger.json

# Claude Code usage (inside an interactive session on same machine)
# → /status
```

## Stopping the swarm

```bash
crontab -r                                           # stop tier-2
systemctl --user disable --now vector-tier1.service  # stop tier-1
```

Wallets and state remain. Re-enable either tier anytime.

## Uninstall

```bash
crontab -r
systemctl --user disable --now vector-tier1.service
rm ~/.config/systemd/user/vector-tier1.service
rm -rf ~/vector-agents
```

## Known limitations

- **Template updates vs. user edits.** `bootstrap.sh` uses `cp -n` for
  CLAUDE.md/PROMPT.md/MEMORY.md so your edits survive re-runs. The flip
  side: upstream template changes are NOT picked up on re-run. To apply
  them, either delete the file and re-run, or edit in place.
- **settings.json is always refreshed.** This is a security-critical file;
  we do not preserve local edits. If you need a custom allowlist, fork the
  template and point bootstrap.sh at it.
- **tier1_watcher.py event decoders are stubs.** Fill them in before
  expecting Module-1 agents to do anything beyond bootstrap.
