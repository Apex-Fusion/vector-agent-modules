"""The manifest format, the parameter rules of §3 and the applied script
check."""

import json

import pytest

from conftest import TEST_PARAMS_JSON, h28, h32
from vector_jury import deployment as D
from vector_jury import types as T


def manifest_json(**changes):
    params = D.params_from_json(TEST_PARAMS_JSON)
    data = {
        "format": D.MANIFEST_FORMAT,
        "network": "mainnet",
        "params": dict(TEST_PARAMS_JSON),
        "params_cbor": D.encode_params(params).hex(),
        "script_hash": h28(b"jury").hex(),
        "address": str(D.script_address(h28(b"jury"))),
        "reference_script": f"{h32(b'ref tx').hex()}#0",
        "plutus": "plutus.json",
        "txs": {"reference_script": h32(b"ref tx").hex()},
        "time": "2026-11-09T12:00:00Z",
    }
    data.update(changes)
    return data


def test_mainnet_params_follow_the_rules():
    params = D.params_from_json(D.MAINNET_PARAMS)
    assert D.params_problems(params) == []
    assert D.params_to_json(params) == D.MAINNET_PARAMS
    assert params.bond_min == 250_000_000 and params.quorum == 3


@pytest.mark.parametrize("change, match", [
    ({"bond_min": 30_000_000}, "bond_min"),               # < juror_floor + slash
    ({"slash": 0}, "slash must be positive"),
    ({"commit_window": -1}, "commit_window must be positive"),
    ({"majority_num": 3, "majority_den": 3}, "majority"),   # num < den fails
    ({"majority_num": 1, "majority_den": 3}, "majority"),   # den < 2 num fails
    ({"majority_num": 2, "majority_den": 4}, "majority"),   # den == 2 num fails (amendment 2: strict)
])
def test_params_rules(change, match):
    params = D.params_from_json({**D.MAINNET_PARAMS, **change})
    assert any(match in p for p in D.params_problems(params))


def test_params_json_is_strict():
    with pytest.raises(D.DeploymentError, match="missing"):
        D.params_from_json({k: v for k, v in D.MAINNET_PARAMS.items() if k != "quorum"})
    with pytest.raises(D.DeploymentError, match="unknown"):
        D.params_from_json({**D.MAINNET_PARAMS, "deposit": 1})
    with pytest.raises(D.DeploymentError, match="56 hex"):
        D.params_from_json({**D.MAINNET_PARAMS, "m1_claim": "abcd"})
    with pytest.raises(D.DeploymentError, match="integer"):
        D.params_from_json({**D.MAINNET_PARAMS, "quorum": "3"})
    with pytest.raises(D.DeploymentError, match="treasury"):
        D.params_from_json({**D.MAINNET_PARAMS, "treasury": "not an address"})


def test_manifest_roundtrip(tmp_path):
    path = tmp_path / "mainnet" / "manifest.json"
    D.write_manifest(path, manifest_json())
    loaded = D.load_manifest(path)
    assert loaded.script_hash == h28(b"jury") and loaded.network == "mainnet"
    assert loaded.reference_script.index == 0 and loaded.plutus == path.parent / "plutus.json"
    assert loaded.params == D.params_from_json(TEST_PARAMS_JSON)
    assert loaded.to_json()["params"] == TEST_PARAMS_JSON
    assert loaded.to_json()["plutus"] == "plutus.json"
    assert json.loads(path.read_text())["format"] == "vector-module1-jury-deployment/1"


@pytest.mark.parametrize("change, match", [
    ({"format": "other/1"}, "manifest"),
    ({"address": str(D.script_address(h28(b"other")))}, "address"),
    ({"params_cbor": D.encode_params(D.params_from_json(D.MAINNET_PARAMS)).hex()}, "params_cbor"),
    ({"reference_script": "abc"}, "reference_script"),
])
def test_manifest_checks(change, match):
    with pytest.raises(D.DeploymentError, match=match):
        D.deployment_from_json(manifest_json(**change))


def test_several_manifests(tmp_path):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    D.write_manifest(first, manifest_json())
    D.write_manifest(second, manifest_json(script_hash=h28(b"jury 2").hex(),
                                           address=str(D.script_address(h28(b"jury 2")))))
    assert [d.script_hash for d in D.load_manifests([first, second])] == [h28(b"jury"), h28(b"jury 2")]
    with pytest.raises(D.DeploymentError, match="same script hash"):
        D.load_manifests([first, first])
    testnet = tmp_path / "t.json"
    D.write_manifest(testnet, manifest_json(network="testnet"))
    with pytest.raises(D.DeploymentError, match="several networks"):
        D.load_manifests([first, testnet])


def test_applied_script_check(tmp_path):
    code = bytes.fromhex("5901000100")
    blueprint = tmp_path / "plutus.json"
    blueprint.write_text(json.dumps({"validators": [
        {"title": "jury.jury.mint", "compiledCode": code.hex(), "hash": D.script_hash_of(code).hex()},
    ]}))
    script = D.load_applied_script(blueprint, D.script_hash_of(code))
    assert bytes(script) == code
    with pytest.raises(D.DeploymentError, match="script hash"):
        D.load_applied_script(blueprint, h28(b"other"))
    blueprint.write_text(json.dumps({"validators": [
        {"title": "jury.jury.mint", "compiledCode": code.hex(), "parameters": [{"title": "params"}]},
    ]}))
    with pytest.raises(D.DeploymentError, match="open parameters"):
        D.load_applied_script(blueprint, D.script_hash_of(code))


def test_treasury_and_identity_addresses():
    loaded = D.deployment_from_json(manifest_json())
    assert str(loaded.treasury_address) == TEST_PARAMS_JSON["treasury"]
    assert loaded.identity_address.payment_part.payload == bytes.fromhex(TEST_PARAMS_JSON["identity_hash"])
    mainnet = D.params_from_json(D.MAINNET_PARAMS)
    assert str(D.script_address(mainnet.identity_hash)) == "addr1w8v43wprl248d9mapgrmm93zap7cuty97e5j7luw3kmhwcs8vm9vk"
    assert T.address_of(mainnet.treasury).staking_part is None
