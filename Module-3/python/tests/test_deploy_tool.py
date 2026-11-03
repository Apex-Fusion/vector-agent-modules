"""Offline tests of the Module-3 deploy (scripts/deploy_mainnet_ogmios.py) on
the shared deploy runner, with an offline Ogmios double (Vector mainnet
protocol parameters; the record token policy runs in aiken tx simulate).
Nothing goes to a network; keys come from fixed test seeds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cbor2
import pytest
from pycardano import (
    Address, PaymentSigningKey, PaymentVerificationKey, Transaction, TransactionId,
    TransactionInput, TransactionOutput, UTxO, Value,
)

from aiken_simulate import aiken_available

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deploy_common as dc  # noqa: E402
import deploy_mainnet_ogmios as m3  # noqa: E402
import vector_deploy as vd  # noqa: E402
from holder_scripts import HOLDER_TAGS, apply_holder  # noqa: E402
from reputation_staking.deployment import load_deployment  # noqa: E402
from vector_deploy_testing import FakeSubmit, fake_chain  # noqa: E402

pytestmark = pytest.mark.skipif(not aiken_available(), reason="aiken is not on PATH")

SKEY = PaymentSigningKey(hashlib.blake2b(b"m3-deploy:operator", digest_size=32).digest())
OPERATOR = bytes(PaymentVerificationKey.from_signing_key(SKEY).hash().payload)
OPERATOR_ADDR = vd.key_address(OPERATOR)
REFERENCE = "addr1vyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqkdl5mw"
STEPS = ["records", "publish-reputation", "publish-endorsement", "cross-refs"]


def deployment() -> dict:
    return {"format": "vector-identity-deployment/1", "network": "mainnet", "identity_hash": "1d" * 28,
            "config_policy": "cf" * 28, "config_holder": "c4" * 28, "reference_address": REFERENCE,
            "operator_address": str(OPERATOR_ADDR), "seal_seed_address": str(vd.key_address(b"\x5e" * 28)),
            "config_seed": "ab" * 32 + "#0", "refs": {"identity": "cd" * 32 + "#0"}, "seal": None}


def manifest(**changes) -> dict:
    data = {"format": "vector-module-deploy/1", "module": "module1", "network": "mainnet",
            "identity": {"identity_hash": "1d" * 28},
            "hashes": {"claim": "c1" * 28, "challenge": "c2" * 28, "jury_pool": "c3" * 28,
                       "identity_party": "e1" * 28},
            "module1": {"refs_token_policy": "b4" * 28}}
    data.update(changes)
    return data


def test_inputs_come_from_the_identity_deployment_and_the_module1_manifest():
    inputs = dc.deployment_inputs_from_files(deployment(), manifest())
    assert inputs == {"audit_claim_validator_hash": "c1" * 28, "audit_challenge_validator_hash": "c2" * 28,
                      "audit_jury_pool_hash": "c3" * 28, "audit_refs_token_policy": "b4" * 28,
                      "identity_hash": "1d" * 28}
    for bad, match in (
        (manifest(identity={"identity_hash": "1e" * 28}), "another identity_hash"),
        (manifest(network="testnet"), "network"),
        (manifest(module="module3"), "not a manifest"),
        (manifest(hashes={"claim": "00" * 28, "challenge": "c2" * 28, "jury_pool": "c3" * 28}), "all zero"),
        (manifest(module1={"refs_token_policy": "205d5f77ffebf60b764ba4f1873eff3764f3d1d594e5dac477a928f9"}),
         "old Module-1 refs token policy"),
    ):
        with pytest.raises(dc.DeployInputError, match=match):
            dc.deployment_inputs_from_files(deployment(), bad)


def test_module3_holders_have_their_own_tags():
    assert (dc.PARAMS_HOLDER_TAG, dc.TREASURY_HOLDER_TAG) == (31, 33)
    tags = [tag for roles in HOLDER_TAGS.values() for tag in roles.values()]
    assert len(tags) == len(set(tags))
    hashes = {tag: apply_holder(tag, OPERATOR.hex())["hash"] for tag in (1, 3, 31, 33, 61, 63)}
    assert len(set(hashes.values())) == 6


@pytest.fixture
def run_env(tmp_path):
    chain, ogmios, koios = fake_chain()
    ogmios.add(UTxO(TransactionInput(TransactionId(b"\x0f" * 32), 0),
                    TransactionOutput(OPERATOR_ADDR, Value(300_000_000))))
    run = vd.Run(chain, OPERATOR, protected={"ab" * 32 + "#0"}, log=lambda _m: None)
    state = vd.DeployState.load(tmp_path / "deploy_state.json", module="module3", network="mainnet")
    args = argparse.Namespace(network="mainnet")
    inputs = dc.deployment_inputs_from_files(deployment(), manifest())
    values, scripts, seed, policy = m3.plan(args, run, state, deployment(), inputs)
    state.data.update(values)
    state.save()
    steps = m3.module3_steps(run, values, scripts, seed, policy)
    return {"run": run, "ogmios": ogmios, "state": state, "values": values, "steps": steps,
            "scripts": scripts, "tmp": tmp_path}


def test_every_step_builds_and_the_record_policy_runs(run_env):
    built, found = vd.plan_steps(run_env["run"], run_env["steps"], None)
    assert found == [] and [b.step.name for b in built] == STEPS
    values = run_env["values"]
    by_name = {b.step.name: b for b in built}
    # The record token policy runs (aiken tx simulate in the Ogmios double).
    assert list(by_name["records"].budgets) == ["mint:0"]
    records = Transaction.from_cbor(by_name["records"].cbor).transaction_body
    seed = vd.parse_ref(values["record_token_seed"])
    assert seed in records.inputs
    out = records.outputs[0]
    assert str(out.address) == values["params_holder_address"]
    assert vd.has_token(UTxO(None, out), bytes.fromhex(values["params_token_policy"]), b"params")
    assert len(cbor2.loads(vd.datum_cbor(UTxO(None, out))).value) == 22
    for name, title in (("publish-reputation", "reputation.reputation.spend"),
                        ("publish-endorsement", "endorsement.endorsement.spend")):
        out = Transaction.from_cbor(by_name[name].cbor).transaction_body.outputs[0]
        assert str(out.address) == REFERENCE
        assert vd.script_hash_of(out.script).hex() == run_env["scripts"][title]["hash"]
    refs = Transaction.from_cbor(by_name["cross-refs"].cbor).transaction_body.outputs[0]
    assert str(refs.address) == values["params_holder_address"]
    assert vd.has_token(UTxO(None, refs), bytes.fromhex(values["refs_token_policy"]), b"reputation_refs")
    assert cbor2.loads(vd.datum_cbor(UTxO(None, refs))).value == [
        bytes.fromhex(values["reputation_validator_hash"]), bytes.fromhex(values["endorsement_validator_hash"])]
    # ReputationConfig: 12 fields, identity_hash last.
    config = cbor2.loads(bytes.fromhex(values["reputation_config_cbor"]))
    assert config.tag == 121 and len(config.value) == 12 and config.value[-1] == bytes.fromhex("1d" * 28)
    assert config.value[0] == bytes.fromhex(values["refs_token_policy"])
    for b in built:
        assert b.fee >= b.min_fee and b.signed_size <= 16_384


def test_build_sign_submit_writes_a_state_that_the_client_loads(run_env):
    run, state, ogmios = run_env["run"], run_env["state"], run_env["ogmios"]
    args = argparse.Namespace(action="build", submit=False, report=None, dry_run_funds=0.0, skey=None, vkey=None)
    vd.execute(args, run, run_env["steps"], state, tx_dir=run_env["tmp"] / "tx", header={},
               after=lambda: m3.update_state(state))
    vd.sign_pending(state, SKEY, OPERATOR, lambda _m: None)
    run.chain.virtual.clear()
    run.chain.spent.clear()
    submit = FakeSubmit(ogmios)
    vd.execute(argparse.Namespace(action="submit", submit=False, report=None, dry_run_funds=0.0, skey=None,
                                  vkey=None),
               run, run_env["steps"], state, tx_dir=run_env["tmp"] / "tx", header={},
               after=lambda: m3.update_state(state), post=submit, sleep=lambda _s: None)
    assert len(submit.sent) == 4 and list(state.steps) == STEPS
    deployed = load_deployment(json.loads(state.path.read_text()))
    assert deployed.cross_refs_nft == state.steps["cross-refs"]["ref"]
    assert deployed.reputation_spend_ref == state.steps["publish-reputation"]["ref"]
    assert deployed.params_holder_hash == run_env["values"]["params_holder_hash"]
    # A new run finds every step on chain and builds nothing.
    built, found = vd.plan_steps(run, run_env["steps"], state)
    assert built == [] and [s.name for s, _ in found] == STEPS


def _files(tmp_path, seal=None) -> list:
    dep = dict(deployment(), seal=seal)
    (tmp_path / "deployment.json").write_text(json.dumps(dep))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest()))
    skey = tmp_path / "op.skey"
    skey.write_text(json.dumps({"type": "PaymentSigningKeyShelley_ed25519", "description": "",
                                "cborHex": "5820" + bytes(SKEY.payload).hex()}))
    return ["--deployment", str(tmp_path / "deployment.json"), "--module1-manifest",
            str(tmp_path / "manifest.json"), "--state-dir", str(tmp_path / "state"), "--skey", str(skey)]


@pytest.fixture
def fake_ogmios(monkeypatch):
    _, ogmios, _ = fake_chain()
    ogmios.add(UTxO(TransactionInput(TransactionId(b"\x0f" * 32), 0),
                    TransactionOutput(OPERATOR_ADDR, Value(300_000_000))))
    monkeypatch.setattr(vd, "Ogmios", lambda url, **kw: ogmios)
    return ogmios


def test_a_send_waits_for_the_seal_of_m1(tmp_path, fake_ogmios, capsys):
    assert m3.deploy(["run", "--submit"] + _files(tmp_path)) == 1
    assert "only after the seal of M1" in capsys.readouterr().err
    # A seal of another hash: refused too (the record holds the config token).
    config_policy, holder = "cf" * 28, vd.script_address(bytes.fromhex("c4" * 28))
    record = UTxO(TransactionInput(TransactionId(b"\xee" * 32), 0), TransactionOutput(
        holder, Value(2_000_000, vd.tokens(bytes.fromhex(config_policy), {b"identity-config": 1})),
        datum=__import__("pycardano").RawCBOR(cbor2.dumps(b"\xe2" * 28))))
    fake_ogmios.add(record)
    seal = {"tx": "ee" * 32, "module1_hash": "e1" * 28, "config_record": "ee" * 32 + "#0"}
    assert m3.deploy(["run", "--submit"] + _files(tmp_path, seal)) == 1
    assert "ANOTHER hash" in capsys.readouterr().out


def test_a_dry_run_works_before_the_seal(tmp_path, fake_ogmios, capsys):
    assert m3.deploy(["run"] + _files(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Seal check: " in out and "Dry run: nothing was signed or sent." in out
    assert not (tmp_path / "state" / "deploy_state.json").exists()
