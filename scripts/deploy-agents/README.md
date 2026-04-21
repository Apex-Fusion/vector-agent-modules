# deploy-agents — LLM-driven Vector agent swarm

Nine autonomous agents (one per role across Modules 1/3/6) running on a single
server, each invoked on a 12h cron, staggered 80 min apart. Each agent is a
headless Claude Code invocation with a single role-specific prompt; it reads
its own state + journal, queries chain, decides one action, writes back.

## Layout

```
~/vector-agents/
├── wallets/            # 9 per-role .skey + .addr
├── master/             # faucet wallet (.skey + .addr)
├── prompts/            # 9 role charters (copies of scripts/deploy-agents/prompts/)
├── state/<role>/       # agent cwd — state.json, journal.md, events.jsonl
├── logs/<role>.log     # rolling log
├── run.sh              # headless wrapper invoked by cron
└── crontab.txt         # rendered schedule
```

## Install

```bash
# 1. Prereqs: python3 + pycardano
cd ~/code/vector-agent-modules/Module-3/python
pip install --user --break-system-packages -e .

# 2. Bootstrap (idempotent)
bash ~/code/vector-agent-modules/scripts/deploy-agents/bootstrap.sh

# 3. Fund the printed master address with ~1000 test AP3X from your wallet.

# 4. Install cron
crontab ~/vector-agents/crontab.txt

# 5. Smoke test
~/vector-agents/run.sh m3-staker
tail -f ~/vector-agents/logs/m3-staker.log
```

## Security model

One machine, one unix user, one Claude Code token, test funds. Permissions
are `--permission-mode acceptEdits` (no rule sandbox). Blast radius of a
compromised agent is that agent's own wallet — at most ~100 AP3X. Acceptable
for a testnet experiment.

If/when promoted to real mainnet, revisit: per-agent unix users, narrower
permission rules, offline-signed top-ups from a cold master wallet.

## Troubleshooting

- **Agent hangs** → `run.sh` has a 600s hard timeout. Check `~/vector-agents/logs/<role>.log` for the last entry.
- **Cron misses a slot** → check `systemctl status cron`; check crontab is
  installed (`crontab -l`). Missed slots are absorbed by the 12h cadence.
- **Balance too low to act** → agent should pull from master via inline
  python (pattern in the prompt). If master is dry, top it up.
- **Phase B (Module-1 voting) missing** → auditor + juror noop until
  `tx_builder.py` placeholders at lines 219-244 are implemented.
