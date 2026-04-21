"""
Module 3 Reputation Staking Dashboard — Server.

FastAPI backend that:
  - Re-exports the indexer REST API (read-only queries over the SQLite index)
  - Serves the static single-page frontend

Usage:
    # From Module-3/dashboard
    INDEXER_DB=../reputation_index.db uvicorn server:app --reload --port 8000

The SQLite database is populated by a separate indexer process:
    python -m indexer --network testnet --db reputation_index.db
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Make the SDK + indexer importable when running from Module-3/dashboard
MODULE_ROOT = Path(os.getenv("MODULE3_ROOT", str(Path(__file__).parent.parent)))
SDK_ROOT = MODULE_ROOT / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("dashboard")

# Import the indexer app. All /health, /v1/*, legacy routes come with it.
from indexer.api import app as app  # noqa: F401,E402

STATIC_DIR = Path(__file__).parent / "static"

# Network metadata, surfaced via /api/config for the frontend.
NETWORK = os.getenv("DEPLOYMENT_NETWORK", "testnet")
_EXPLORERS = {
    "testnet": "https://explorer.vector.testnet.apexfusion.org",
    "mainnet": "https://explorer.vector.mainnet.apexfusion.org",
}
_DEPLOY_STATE_FILES = {
    "testnet": MODULE_ROOT / "deploy" / "deploy_state.json",
    "mainnet": MODULE_ROOT / "deploy" / "mainnet" / "deploy_state.json",
}

_deploy_state = {}
try:
    path = _DEPLOY_STATE_FILES.get(NETWORK)
    if path and path.exists():
        _deploy_state = json.load(open(path))
        logger.info(f"Loaded deploy state for {NETWORK} from {path}")
except Exception as exc:
    logger.warning(f"Could not load deploy state: {exc}")


@app.get("/api/config")
def get_config():
    """Frontend configuration: network, explorer URL, contract addresses."""
    return {
        "network": NETWORK,
        "explorer": _EXPLORERS.get(NETWORK, ""),
        "reputation_address": _deploy_state.get("reputation_address", ""),
        "endorsement_address": _deploy_state.get("endorsement_address", ""),
        "reputation_validator_hash": _deploy_state.get("reputation_validator_hash", ""),
        "endorsement_validator_hash": _deploy_state.get("endorsement_validator_hash", ""),
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})
