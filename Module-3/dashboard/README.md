# Module 3 Reputation Staking Dashboard

Public read-only frontend for the Module 3 reputation index. Shows a
leaderboard of staked agents, per-agent score breakdowns, active
challenges, sybil-detection flags, and aggregate network stats. Each agent
is a did:vector identity record; the dashboard shows its canonical DID
(`did:vector:<64 hex>`) and the effective status of its record.

## Local development

```bash
cd Module-3

# 1. Populate the SQLite index (one-shot or long-running)
#    --network mainnet hits Vector mainnet; omit or pass --network testnet for testnet
PYTHONPATH=python python -m indexer --network mainnet --once \
    --db dashboard/reputation_index.db

# 2. Serve the dashboard (static SPA + REST API on the same port)
cd dashboard
pip install -r requirements.txt
DEPLOYMENT_NETWORK=mainnet \
  MODULE3_ROOT=$(pwd)/.. \
  uvicorn server:app --reload --port 8000
```

`INDEXER_DB` defaults to `reputation_index.db` in the dashboard working directory — no env var needed if you indexed to that path. Override with `INDEXER_DB=/abs/path/to.db` if you keep separate per-network files.

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

## Deploy state

The server and the indexer read the deploy state that the deploy scripts
write: `deploy/deploy_state.json` (testnet) or
`deploy/mainnet/deploy_state.json` (mainnet). The state must belong to a
new deployment. It must have `identity_hash`, `params_token_policy`, the
params and treasury holders (`params_holder_hash`/`_address`,
`treasury_holder_hash`/`_address`), the validator hashes and addresses, the
Module-1 values and the `tx_hashes` of a complete deploy run. The server
does not start with a missing or old state (an old state has
`agent_registry_hash` and no `identity_hash`); the error tells you to move
the old file. Deployment order: identity registry, Module-1, Module-3,
Module-6.

## Tabs

- **Leaderboard** — the agents whose identity record is Active, ranked by
  net score, filterable by tier and capability. The **Identity** column
  shows the effective status at the last index time: `active`,
  `suspended`, `revoked`, `retired`, or `missing` (no record, or a missing
  ancestor record). An agent that is not Active has the tier Unverified and
  its amounts stay. The leaderboard leaves it out; the toggle "Show agents
  that are not Active" (`include_all=true`) shows all agents. Click a row
  for the full profile.
- **Challenges** — every challenge across all agents (Open / Escalated /
  Resolved), filterable by state.
- **Sybil Detection** — flags produced by `indexer.sybil.analyze_sybil`
  (cycles + mutual-endorsement clusters), sorted by severity.
- **Stats** — network-wide totals, tier and identity status distribution,
  `identity_hash`, last poll time.

The agent detail modal shows the canonical DID, the identity status, the
full score breakdown (self-stake, endorsement total, challenge total,
history bonus, decay) plus every endorsement received/given, every
challenge, and every history bonus for the selected agent. Times are POSIX
milliseconds, shown as local dates.

## Agent identifiers in the API

The `{did}` path of `/v1/reputation/*` accepts the canonical DID
(`did:vector:<64 lowercase hex>`) or the 64-hex identifier (any case);
another value gets HTTP 400. Each agent identifier in a response is the
64-hex identifier (`agent_did`, `endorser_did`, `target_did`,
`challenger_did`) and the canonical DID (`agent_did_uri`, ...). Sybil flags
also have `related_did_uris`.

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
reverse proxy — see the next section.

### Co-hosting with Module 6's Traefik

If the VM already runs Module 6 (or anything else with a Traefik on port
80), use `docker-compose.shared-traefik.yml`. It omits the Traefik
service, drops the port 80 binding, and joins Module 6's **existing**
Docker network as an external reference — no edits to Module 6 required.

```bash
# 1. Find the network Module 6's Traefik is already on:
docker network ls | grep module
# e.g. "module-6_default"

# 2. Configure and deploy:
cd Module-3/dashboard
cp .env.mainnet.example .env
# edit .env:
#   DASHBOARD_HOST=<your-module-3-hostname>
#   TRAEFIK_NETWORK=module-6_default   (or whatever step 1 returned)

docker compose -f docker-compose.shared-traefik.yml -p module3-mainnet up --build -d
```

Module 6's Traefik auto-discovers the Module 3 dashboard container by its
labels and routes `DASHBOARD_HOST` to it. No port 80 conflict, no second
Traefik instance, no Module 6 changes.

### Mainnet deployment checklist

- [ ] Contracts deployed (new deployment with `identity_hash`) — hashes in `deploy/mainnet/deploy_state.json`
- [x] Indexer runs clean on Vector mainnet (`--network mainnet`)
- [x] `/api/config`, `/v1/reputation/*`, `/v1/tools/*`, `/health` all live
- [x] Sybil detection (cycles + clusters) wired into the indexer loop
- [ ] DNS A record for `DASHBOARD_HOST` pointed at the VM
- [ ] VM has Docker + docker-compose installed, ports 80 reachable
- [ ] Optional: Traefik Let's Encrypt resolver + `entrypoints.websecure` for HTTPS (the shipped compose is HTTP-only)
- [ ] Optional: off-host backups for the `indexer-data` volume (SQLite is rebuildable from on-chain, so this is convenience not durability)

### `.env` variables

| Variable           | Example (testnet)                                  | Description |
|--------------------|----------------------------------------------------|-------------|
| `NETWORK`          | `testnet` or `mainnet`                             | Drives indexer `--network` flag + dashboard `/api/config` |
| `DASHBOARD_HOST`   | `module-3.vector.testnet.apexfusion.org`           | Traefik `Host()` rule — must be a live DNS A record pointing at the VM |
| `INDEXER_INTERVAL` | `60`                                               | Indexer poll interval in seconds |
| `TRAEFIK_NETWORK`  | `module-6_default`                                 | Only for `docker-compose.shared-traefik.yml` — the existing Traefik's Docker network |

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

## Contract addresses (driven by `/api/config`)

These come from `deploy/<network>/deploy_state.json` at server start and
are surfaced to the frontend for explorer links. `/api/config` returns
`network`, `explorer`, `reputation_address`, `endorsement_address`,
`reputation_validator_hash`, `endorsement_validator_hash`, `identity_hash`
and `identity_address` (the enterprise script address of `identity_hash`).

The values change with each deployment, so this README lists none. Read
them from the deploy state file or from `/api/config`.

Ogmios: `https://ogmios.vector.mainnet.apexfusion.org` (mainnet),
`https://ogmios.vector.testnet.apexfusion.org` (testnet). Explorer:
`https://explorer.vector.mainnet.apexfusion.org`,
`https://explorer.vector.testnet.apexfusion.org`.
