"""Offline tests of the dashboard helpers (dashboard/identity_view.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pycardano import Address, Network, ScriptHash

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


def test_the_published_identity_mainnet_deployment_is_a_new_deployment():
    """dashboard/docker-compose.apex-web-identity.yml points
    MODULE6_DEPLOYMENT_FILE at this file. Its values come from the deploy state
    of the identity release (deploy/identity/state/mainnet/deploy_state.json)."""
    data = view.load_deployment(MODULE_DEPLOY / "identity" / "mainnet" / "deployment.json")
    assert view.deployment_note(data) is None
    assert data["identity_hash"] == "d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762"
    assert data["hashes"] == {
        "proposal_spend": "0dc0e8b60c6a3f4d16b43bf7b6dc9df4977337fafa561d08b5ce8768",
        "proposal_mint": "cb248e9d6e636db1c78242813a500febcc93a6c92475e7bd5e51e4a4",
        "critique_spend": "92f65ad8a23396d864c1fc571ff48c0b25008fc3d5f5d7ab3fd568c5",
        "critique_mint": "e29a533518133173935174ba396a6b17c99b4bcdd2effd3720db4747",
        "endorsement_spend": "2e7e2a48850adb3cf78d088fdbcfcc7a91f5c50f960d966dde083550",
    }
    assert data["holders"]["treasury"]["address"] == "addr1w83wjku6nd9uw90nmmzarrtgn0mdzxt2xx7kam4d5eq7a2s262ejl"
    assert data["endpoints"]["ogmios"] == "https://ogmios.vector.mainnet.apexfusion.org"
    for name, address in data["addresses"].items():
        script = ScriptHash(bytes.fromhex(data["hashes"][name]))
        assert address == str(Address(payment_part=script, network=Network.MAINNET))
    for holder in data["holders"].values():
        script = ScriptHash(bytes.fromhex(holder["hash"]))
        assert holder["address"] == str(Address(payment_part=script, network=Network.MAINNET))


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
