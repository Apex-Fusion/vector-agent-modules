# Foundation Review Dashboard

Web dashboard for the Foundation Council to review and prioritize governance proposals. Foundation actions (adopt, reject) are executed via API endpoints.

## Setup

```bash
cd Module-6/dashboard
pip install -r requirements.txt
```

Requires the governance SDK on the Python path:
```bash
pip install -e /path/to/agent-sdk-py
```

## Running

```bash
cd Module-6/dashboard
MODULE6_DEPLOYMENT_FILE=../deploy/testnet/deployment.json uvicorn server:app --reload --port 8000
```

Open http://localhost:8000

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODULE6_DEPLOYMENT_FILE` | none (required) | Path of the deployment file (`deploy/<network>/deployment.json` format). The server stops when it is missing. |
| `VECTOR_OGMIOS_URL` | `endpoints.ogmios` of the deployment file | Ogmios endpoint |
| `VECTOR_SUBMIT_URL` | `endpoints.submit` of the deployment file | Tx submission endpoint |
| `GOVERNANCE_WALLET_ADDRESS` | none | Foundation wallet for the balance display |

## Identity registry

- Agent identifiers (proposer, critic, endorser, leaderboard) are canonical DIDs: `did:vector:<64 hex>`. `/api/agent/{did}` accepts the DID or the 64-hex identifier.
- A deployment file without `identity_hash` describes an old Module-6 deployment with the legacy agent registry (the v8 files in `deploy/` are such files). `/api/config` then returns `deployment_note`, and the page shows it above the tabs. A deployment file for the identity registry has `identity_hash` and no note.
- The dashboard does not read the legacy agent registry.
- `deploy/identity/mainnet/deployment.json` is the mainnet deployment file of the identity release (2026-11-03). It comes from the git-ignored `deploy/identity/state/mainnet/deploy_state.json`. The image copies it in, and `docker-compose.apex-web-identity.yml` selects it (`docs/apex-web-identity-dashboards.md` in the repo root).

## Features

- **Proposal queue** — ranked by quality signal (reputation, endorsements, controversy, track record)
- **Emergency proposals** — highlighted with countdown timer
- **Proposal detail** — critiques, endorsements, proposer track record, reward calculator
- **One-click actions** — adopt (with reward amount), reject, extend review, expire
- **Agent leaderboard** — all governance participants (proposers, critics, endorsers) ranked by activity
- **Treasury view** — total balance, batch UTxO count
- **Stats** — adoption rate, proposal counts, active agents (proposers, critics, endorsers)
- **Auto-refresh** — polls every 30 seconds

## Architecture

```
dashboard/
├── server.py          ← FastAPI backend (REST API, imports governance SDK)
├── identity_view.py   ← Deployment file (MODULE6_DEPLOYMENT_FILE), old-deployment note, canonical DIDs
├── static/
│   ├── index.html     ← Single-page dashboard
│   ├── style.css      ← Dark theme styling
│   └── app.js         ← Frontend logic (vanilla JS)
├── requirements.txt
└── README.md
```

The server connects to Vector via the SDK chain context (read-only) and reads the deployment file that `MODULE6_DEPLOYMENT_FILE` names. `identity_view.py` uses the shared identity module `../scripts/vector_identity.py`; the Docker image copies both.
