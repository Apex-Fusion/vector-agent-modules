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

The server reads the deploy state of DEPLOYMENT_NETWORK, the file that the
deploy scripts write: deploy/deploy_state.json (testnet) or
deploy/mainnet/deploy_state.json (mainnet). It does not start without a
state of a new deployment (with identity_hash). Move an old state file to a
different path.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Make the SDK + indexer importable when running from Module-3/dashboard
MODULE_ROOT = Path(os.getenv("MODULE3_ROOT", str(Path(__file__).parent.parent)))
SDK_ROOT = MODULE_ROOT / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from reputation_staking.deployment import load_deployment  # noqa: E402

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

if NETWORK not in _DEPLOY_STATE_FILES:
    raise SystemExit(f"DEPLOYMENT_NETWORK must be testnet or mainnet, got {NETWORK!r}")

# Raises DeploymentStateError (with the deployment order) for a missing file,
# an old deployment or a malformed value.
DEPLOYMENT = load_deployment(_DEPLOY_STATE_FILES[NETWORK])
logger.info(
    "Loaded deploy state for %s from %s (identity_hash %s)",
    NETWORK, _DEPLOY_STATE_FILES[NETWORK], DEPLOYMENT.identity_hash,
)


@app.get("/api/config")
def get_config():
    """Frontend configuration: network, explorer URL, contract addresses and
    the identity registry of the deployment."""
    return {
        "network": NETWORK,
        "explorer": _EXPLORERS.get(NETWORK, ""),
        "reputation_address": DEPLOYMENT.reputation_address,
        "endorsement_address": DEPLOYMENT.endorsement_address,
        "reputation_validator_hash": DEPLOYMENT.reputation_validator_hash,
        "endorsement_validator_hash": DEPLOYMENT.endorsement_validator_hash,
        "identity_hash": DEPLOYMENT.identity_hash,
        "identity_address": DEPLOYMENT.identity_address,
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})
