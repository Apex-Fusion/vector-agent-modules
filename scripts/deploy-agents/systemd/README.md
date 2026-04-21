# Tier-1 systemd unit

User-mode service that runs the Module-1 event watcher (polls chain every
30s; triggers Claude Code agents only on actionable events).

## Install

```bash
# 1. Copy the unit into the user's systemd dir.
mkdir -p ~/.config/systemd/user
cp ~/code/vector-agent-modules/scripts/deploy-agents/systemd/vector-tier1.service \
   ~/.config/systemd/user/

# 2. Make sure the watcher script is in place (bootstrap.sh copies it there):
ls -l ~/vector-agents/bin/tier1_watcher.py

# 3. Generate a long-lived OAuth token for headless `claude -p` auth:
claude setup-token
# → prints a one-year token. Copy the example file and paste the token into it:
cp ~/vector-agents/env.example ~/vector-agents/env
chmod 600 ~/vector-agents/env
${EDITOR:-nano} ~/vector-agents/env     # paste the token after CLAUDE_CODE_OAUTH_TOKEN=
# The service unit loads this file automatically if present.
# Format is strict KEY=value — no `export`, no shell expansion.

# 4. Enable + start:
systemctl --user daemon-reload
systemctl --user enable --now vector-tier1.service

# 5. Verify:
systemctl --user status vector-tier1.service
tail -f ~/vector-agents/logs/tier1-watcher.log
```

## Make it survive logout

User services stop when the user logs out unless lingering is enabled:

```bash
sudo loginctl enable-linger "$USER"
```

## Stop / uninstall

```bash
systemctl --user disable --now vector-tier1.service
rm ~/.config/systemd/user/vector-tier1.service
systemctl --user daemon-reload
```

## Why the env file

Claude Code stores credentials under `~/.claude` after `claude login`.
Those credentials are sufficient for interactive use, but systemd-launched
headless runs may not find them under all configurations. A long-lived
OAuth token in `CLAUDE_CODE_OAUTH_TOKEN` is the documented headless path
and is robust against session-only credential stores.
