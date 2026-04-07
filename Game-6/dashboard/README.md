# Foundation Review Dashboard

Private web dashboard for the Foundation Council to review, adopt, and reject governance proposals.

## Setup

```bash
cd Game-6/dashboard
pip install -r requirements.txt
```

Requires the governance SDK on the Python path:
```bash
pip install -e /path/to/agent-sdk-py
```

## Running

```bash
# Testnet (direct signing with skey on server)
cd Game-6/dashboard
uvicorn server:app --reload --port 8000

# Production (external signing — generates unsigned tx for offline signing)
SIGNING_MODE=external uvicorn server:app --port 8000
```

Open http://localhost:8000

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNING_MODE` | `direct` | `direct` = sign with skey on server, `external` = export unsigned tx |
| `VECTOR_OGMIOS_URL` | from deploy_state | Ogmios endpoint |
| `VECTOR_SUBMIT_URL` | from deploy_state | Tx submission endpoint |

## Features

- **Proposal queue** — ranked by quality signal (reputation, endorsements, controversy, track record)
- **Emergency proposals** — highlighted with countdown timer
- **Proposal detail** — critiques, endorsements, proposer track record, reward calculator
- **One-click actions** — adopt (with reward amount), reject, extend review, expire
- **Treasury view** — batch count, total balance, runway estimate
- **Stats** — adoption rate, proposal counts, chain health
- **Auto-refresh** — polls every 30 seconds

## Architecture

```
dashboard/
├── server.py          ← FastAPI backend (REST API, imports governance SDK)
├── static/
│   ├── index.html     ← Single-page dashboard
│   ├── style.css      ← Dark theme styling
│   └── app.js         ← Frontend logic (vanilla JS)
├── requirements.txt
└── README.md
```

The server connects to Vector testnet via the `VectorAgent` SDK and reads deployment config from `../wallets/deploy_state.json`.
