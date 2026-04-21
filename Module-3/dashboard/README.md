# Module 3 Reputation Staking Dashboard

Public read-only frontend for the Module 3 reputation index. Shows a
leaderboard of staked agents, per-agent score breakdowns, active
challenges, sybil-detection flags, and aggregate network stats.

## Local development

```bash
cd Module-3

# 1. Populate the SQLite index (one-shot or long-running)
PYTHONPATH=python python -m indexer --network testnet --once \
    --db dashboard/reputation_index.db

# 2. Serve the dashboard (static SPA + REST API on the same port)
cd dashboard
pip install -r requirements.txt
INDEXER_DB=$(pwd)/reputation_index.db \
  MODULE3_ROOT=$(pwd)/.. \
  DEPLOYMENT_NETWORK=testnet \
  uvicorn server:app --reload --port 8000
```

Open <http://localhost:8000>.

For continuous updates, run the indexer in a second terminal without
`--once`:

```bash
PYTHONPATH=python python -m indexer --network testnet \
    --db dashboard/reputation_index.db --interval 60
```

Both processes share the SQLite file — there's no locking issue because
the dashboard only reads.

## Environment variables

| Variable             | Default    | Description |
|----------------------|------------|-------------|
| `DEPLOYMENT_NETWORK` | `testnet`  | `testnet` or `mainnet` — selects deploy_state + explorer URL |
| `INDEXER_DB`         | `reputation_index.db` | Path to the SQLite file populated by the indexer |
| `MODULE3_ROOT`       | parent dir | Path to `Module-3/` (for deploy state lookup) |

## Tabs

- **Leaderboard** — all indexed agents ranked by net score, filterable by
  tier and capability. Click a row for the full profile.
- **Challenges** — every challenge across all agents (Open / Escalated /
  Resolved), filterable by state.
- **Sybil Detection** — flags produced by `indexer.sybil.analyze_sybil`
  (cycles + mutual-endorsement clusters), sorted by severity.
- **Stats** — network-wide totals, tier distribution, last poll time.

The agent detail modal shows the full score breakdown (self-stake,
endorsement total, challenge total, history bonus, decay) plus every
endorsement received/given, every challenge, and every history bonus for
the selected DID.

## Production deployment

`docker-compose.yml` mirrors the Module 6 pattern: Traefik on port 80
routing the dashboard host to the dashboard container, with a separate
`indexer` container polling Ogmios on a 60-second interval and writing
to a shared SQLite volume.

Network selection (testnet vs mainnet) is driven by a `.env` file —
the same compose file runs both.

```bash
cd Module-3/dashboard

# Testnet VM
cp .env.testnet.example .env
docker compose up --build -d

# Mainnet VM
cp .env.mainnet.example .env
docker compose up --build -d
```

If you ever need to run both on the same host, give them distinct
project names (each gets its own volumes, network, and container set)
and pick the env file explicitly:

```bash
docker compose --env-file .env.testnet.example -p module3-testnet up --build -d
docker compose --env-file .env.mainnet.example -p module3-mainnet up --build -d
```

Note: both stacks bind Traefik to port 80, so same-host co-hosting only
works if you drop one of the Traefik services and share a single
reverse proxy.

### `.env` variables

| Variable           | Example (testnet)                                  | Description |
|--------------------|----------------------------------------------------|-------------|
| `NETWORK`          | `testnet` or `mainnet`                             | Drives indexer `--network` flag + dashboard `/api/config` |
| `DASHBOARD_HOST`   | `module-3.vector.testnet.apexfusion.org`           | Traefik `Host()` rule — must be a live DNS A record pointing at the VM |
| `INDEXER_INTERVAL` | `60`                                               | Indexer poll interval in seconds |

Point the DNS A record for `DASHBOARD_HOST` at the VM, then open the URL.

## Architecture

```
dashboard/
├── server.py               ← FastAPI: imports indexer.api.app, adds static + /api/config
├── static/
│   ├── index.html          ← Single-page layout, 4 tabs + modal
│   ├── style.css           ← Dark theme (matches Module 6)
│   └── app.js              ← Fetch + render, agent detail modal
├── requirements.txt
├── Dockerfile              ← multi-service: indexer + dashboard share image
├── docker-compose.yml      ← Traefik + indexer + dashboard
└── README.md
```

The dashboard server re-exports every route from `indexer/api.py`
(`/health`, `/v1/reputation/*`, `/v1/tools/*`), so the frontend talks to
the same FastAPI app as external API consumers.
