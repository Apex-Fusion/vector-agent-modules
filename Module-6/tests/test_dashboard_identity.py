"""Offline tests of the dashboard helpers (dashboard/identity_view.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import identity_view as view
import vector_identity as vid
from conftest import PROPOSER

MODULE_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def write(tmp_path, data) -> Path:
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(data))
    return path


def deployment(**changes) -> dict:
    data = {
        "network": "vector-testnet",
        "endpoints": {"ogmios": "https://ogmios.example", "explorer": "https://explorer.example"},
        "hashes": {"proposal_spend": "11" * 28, "critique_spend": "22" * 28, "endorsement_spend": "33" * 28},
        "holders": {"treasury": {"address": "addr1example"}},
    }
    data.update(changes)
    return data


def test_the_deployment_file_comes_from_the_environment(tmp_path):
    path = write(tmp_path, deployment(identity_hash="1d" * 28))
    assert view.deployment_file({view.DEPLOYMENT_FILE_ENV: str(path)}) == path
    with pytest.raises(view.DeploymentFileError, match=view.DEPLOYMENT_FILE_ENV):
        view.deployment_file({})
    with pytest.raises(view.DeploymentFileError, match="is not a file"):
        view.deployment_file({view.DEPLOYMENT_FILE_ENV: str(tmp_path / "missing.json")})


def test_a_deployment_without_identity_hash_gets_the_old_deployment_note(tmp_path):
    new = view.load_deployment(write(tmp_path, deployment(identity_hash="1d" * 28)))
    assert view.deployment_note(new) is None
    old = view.load_deployment(write(tmp_path, deployment()))
    note = view.deployment_note(old)
    assert note == view.OLD_DEPLOYMENT_NOTE
    assert "no identity_hash" in note and "legacy agent registry" in note


def test_the_published_v8_deployments_are_old_deployments():
    for network in ("testnet", "mainnet"):
        data = view.load_deployment(MODULE_DEPLOY / network / "deployment.json")
        assert view.deployment_note(data) == view.OLD_DEPLOYMENT_NOTE


def test_a_deployment_file_must_name_the_validators(tmp_path):
    with pytest.raises(view.DeploymentFileError, match="endorsement_spend"):
        view.load_deployment(write(tmp_path, deployment(hashes={"proposal_spend": "11" * 28, "critique_spend": "22" * 28})))


def test_agent_identifiers_are_canonical_dids():
    did = vid.did_from_id(PROPOSER)
    assert view.agent_did(PROPOSER.hex()) == did
    assert view.agent_did(PROPOSER) == did
    assert view.agent_did("did_smoke_test_agent") == "did_smoke_test_agent"
    assert view.agent_did(b"short") == b"short".hex()
    assert view.agent_key(did) == PROPOSER.hex()
    assert view.agent_key(PROPOSER.hex().upper()) == PROPOSER.hex()
    data = {
        "proposer_did": PROPOSER.hex(),
        "proposal_hash": b"\x01" * 32,
        "critiques": [{"critic_did": PROPOSER.hex(), "stake_amount": 5}],
        "track_record": {"agent_did": PROPOSER.hex()},
    }
    assert view.with_dids(data) == {
        "proposer_did": did,
        "proposal_hash": "01" * 32,
        "critiques": [{"critic_did": did, "stake_amount": 5}],
        "track_record": {"agent_did": did},
    }
