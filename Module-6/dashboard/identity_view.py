"""Deployment file and agent identifiers for the Module-6 dashboard.

The dashboard reads one deployment file (``deploy/<network>/deployment.json``
format). The environment variable MODULE6_DEPLOYMENT_FILE gives its path;
there is no default. A deployment for the identity registry has
``identity_hash``. A file without it describes an old deployment with the
legacy agent registry: the dashboard shows its data with a note.

Module-6 datums store the 32-byte identifier of an identity record. The
dashboard shows it as the canonical DID ``did:vector:<64 hex>`` (Part 1 §3 of
``docs/did-vector-method.md`` in agents-marketplace).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Union

import vector_identity as vid

DEPLOYMENT_FILE_ENV = "MODULE6_DEPLOYMENT_FILE"

OLD_DEPLOYMENT_NOTE = (
    "This deployment file has no identity_hash. It describes an old Module-6 "
    "deployment with the legacy agent registry. The dashboard shows its "
    "proposals, critiques and endorsements, but new actions use the Module-6 "
    "deployment for the identity registry (deployment order: identity "
    "registry, Module-1, Module-3, Module-6)."
)


class DeploymentFileError(RuntimeError):
    """The deployment file is missing or not usable."""


def deployment_file(environ: Mapping[str, str] = os.environ) -> Path:
    """The path in MODULE6_DEPLOYMENT_FILE. Refuses a missing value or file."""
    value = environ.get(DEPLOYMENT_FILE_ENV, "").strip()
    if not value:
        raise DeploymentFileError(
            f"Set {DEPLOYMENT_FILE_ENV} to the path of the Module-6 deployment file "
            "(for example deploy/testnet/deployment.json)."
        )
    path = Path(value)
    if not path.is_file():
        raise DeploymentFileError(f"{DEPLOYMENT_FILE_ENV}: {path} is not a file.")
    return path


def load_deployment(path: Union[str, Path]) -> dict:
    """The deployment file. It must name the proposal, critique and
    endorsement validators (``hashes``)."""
    deployment = json.loads(Path(path).read_text())
    hashes = deployment.get("hashes") if isinstance(deployment, dict) else None
    missing = [k for k in ("proposal_spend", "critique_spend", "endorsement_spend") if not (hashes or {}).get(k)]
    if missing:
        raise DeploymentFileError(f"{path}: hashes has no {', '.join(missing)}.")
    return deployment


def deployment_note(deployment: Mapping[str, Any]) -> Optional[str]:
    """The old-deployment note, or None for an identity registry deployment."""
    return None if deployment.get("identity_hash") else OLD_DEPLOYMENT_NOTE


def agent_did(value: Any) -> Any:
    """The canonical DID of a 32-byte agent identifier (bytes or hex). Other
    values are not identifiers: they stay as they are (bytes as hex)."""
    raw = value
    if isinstance(value, str):
        try:
            raw = bytes.fromhex(value)
        except ValueError:
            return value
    if isinstance(raw, (bytes, bytearray)):
        return vid.did_from_id(bytes(raw)) if len(raw) == 32 else bytes(raw).hex()
    return value


def agent_key(value: str) -> str:
    """The hex identifier that the governance indexer stores, from a DID or
    from hex."""
    try:
        return vid.normalize_agent_id(value).hex()
    except (TypeError, ValueError):
        return value.strip().lower()


#: Fields that hold an agent identifier in the dashboard data.
AGENT_FIELDS = frozenset({"proposer_did", "critic_did", "endorser_did", "agent_did"})


def with_dids(value: Any) -> Any:
    """A copy of dashboard data with each agent field as a canonical DID and
    other bytes as hex."""
    if isinstance(value, Mapping):
        return {k: agent_did(v) if k in AGENT_FIELDS else with_dids(v) for k, v in value.items()}
    if isinstance(value, list):
        return [with_dids(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value
