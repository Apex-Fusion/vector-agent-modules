"""Offline tests of the Module-6 deploy tools: scripts/deploy.py,
scripts/deploy_params.py and scripts/treasury_fund.py on the shared deploy
runner (shared/python/vector_deploy.py).

The runner builds each step against FakeOgmios
(shared/python/vector_deploy_testing.py) with the Vector mainnet protocol
parameters. FakeOgmios evaluates each redeemer with `aiken tx simulate`, so
the record token policy of the records step and the params holder of
deploy_params.py run as they do on Ogmios. FakeSubmit confirms a sent
transaction on the fake chain. The applied validators come from
contracts/governance-suggestion/plutus.json. A proposal submission of the
client builders (governance_identity.py) reads the records, the head of the
activity list and the CrossRefs output of the deploy, and the real validators
accept it. Nothing goes to a network; the keys come from fixed test seeds.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import cbor2
import pytest
import requests
from pycardano import (
    ExecutionUnits, MultiAsset, PaymentSigningKey, PaymentVerificationKey, Transaction, TransactionId,
    TransactionInput, TransactionOutput, UTxO, Value,
)
from pycardano.serialization import RawCBOR

import deploy
import deploy_common as dc
import deploy_params
import governance_identity as gi
import treasury_fund
import vector_deploy as vd
import vector_identity as vid
from aiken_simulate import aiken_available, apply_validator, resolved_inputs, simulate
from conftest import IDENTITY_HASH, PROPOSER, SYSTEM_START_MS, deploy_state, make_world
from holder_scripts import apply_holder
from vector_deploy_testing import FakeKoios, FakeOgmios, FakeSubmit

pytestmark = pytest.mark.skipif(not aiken_available(), reason="aiken is not on PATH")

OPERATOR_SKEY = PaymentSigningKey(hashlib.blake2b(b"module6-deploy-tests:operator", digest_size=32).digest())
OPERATOR_VKEY = PaymentVerificationKey.from_signing_key(OPERATOR_SKEY)
OPERATOR = bytes(OPERATOR_VKEY.hash().payload)
OPERATOR_ADDR = vd.key_address(OPERATOR)
REFERENCE_ADDRESS = str(vd.key_address(bytes(28)))
SEAL_SEED_ADDRESS = str(vd.key_address(b"\x5e" * 28))
CONFIG_SEED = "ab" * 32 + "#0"
COINS_PER_UTXO_BYTE = 4310

#: The operator funds: the seed (the largest output) and a small output.
SEED = TransactionInput(TransactionId(b"\x0f" * 32), 0)
SEED_REF = vd.ref_key(SEED)
#: The virtual output of --dry-run-funds (vector_deploy.Run).
VIRTUAL_SEED = hashlib.blake2b(b"vector-deploy dry-run funds", digest_size=32).hexdigest() + "#0"

#: The seal: the configuration record at config_holder holds a token of
#: config_policy and names M1 (identity_party of the Module-1 manifest).
CONFIG_POLICY = bytes.fromhex("cf" * 28)
CONFIG_HOLDER = bytes.fromhex("c4" * 28)
M1 = bytes.fromhex("e1" * 28)
CONFIG_RECORD = TransactionInput(TransactionId(b"\x5c" * 32), 0)

PUBLISH_STEPS = [name for name in deploy.STEP_NAMES if name.startswith("publish-")]
TREASURY_STEPS = [f"treasury-{i}" for i in range(1, deploy.TREASURY_BATCHES + 1)]


def funding() -> list[UTxO]:
    return [
        UTxO(SEED, TransactionOutput(OPERATOR_ADDR, Value(600_000_000))),
        UTxO(TransactionInput(TransactionId(b"\x0e" * 32), 1), TransactionOutput(OPERATOR_ADDR, Value(20_000_000))),
    ]


def config_record(names: bytes = M1) -> UTxO:
    """The configuration record of the seal on chain: its inline datum is the
    Plutus byte string of `names`."""
    value = Value(2_000_000, MultiAsset.from_primitive({CONFIG_POLICY: {b"identity-config": 1}}))
    return UTxO(CONFIG_RECORD, TransactionOutput(vd.script_address(CONFIG_HOLDER), value,
                                                 datum=RawCBOR(cbor2.dumps(bytes(names)))))


def identity_deployment(sealed: bool = True, module1_hash: bytes = M1) -> dict:
    seal = {"tx": "5c" * 32, "module1_hash": module1_hash.hex(), "config_record": vd.ref_key(CONFIG_RECORD)}
    return {
        "format": "vector-identity-deployment/1",
        "network": "mainnet",
        "identity_hash": IDENTITY_HASH.hex(),
        "config_policy": CONFIG_POLICY.hex(),
        "config_holder": CONFIG_HOLDER.hex(),
        "reference_address": REFERENCE_ADDRESS,
        "operator_address": str(OPERATOR_ADDR),
        "seal_seed_address": SEAL_SEED_ADDRESS,
        "config_seed": CONFIG_SEED,
        "refs": {"identity": "cd" * 32 + "#0"},
        "seal": seal if sealed else None,
    }


def module1_manifest(**changes) -> dict:
    """The values of a Module-1 manifest that the seal check reads."""
    manifest = {"format": vd.STATE_FORMAT, "module": "module1", "network": "mainnet",
                "identity": {"identity_hash": IDENTITY_HASH.hex()}, "hashes": {"identity_party": M1.hex()}}
    manifest.update(changes)
    return manifest


def chain_of(ogmios: FakeOgmios) -> vd.DeployChain:
    return vd.DeployChain(ogmios, vd.NETWORKS["mainnet"], koios=FakeKoios())


def ogmios_with(*utxos: UTxO, store: dict | None = None, seal: bytes | None = M1) -> FakeOgmios:
    """A fake chain with `utxos`. A new chain has the configuration record of
    a seal that names `seal` (None: no seal on chain)."""
    ogmios = FakeOgmios()
    if store:
        ogmios.store = dict(store)
    elif seal is not None:
        ogmios.add(config_record(seal))
    ogmios.add(*utxos)
    return ogmios


def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture(scope="module")
def files(tmp_path_factory) -> dict:
    """The identity deployment files (sealed, and without a seal), the
    Module-1 manifest and the operator key files."""
    tmp = tmp_path_factory.mktemp("inputs")
    paths = {"deployment": tmp / "deployment.json", "unsealed": tmp / "unsealed.json",
             "manifest": tmp / "module1-manifest.json", "skey": tmp / "operator.skey", "vkey": tmp / "operator.vkey"}
    paths["deployment"].write_text(json.dumps(identity_deployment()))
    paths["unsealed"].write_text(json.dumps(identity_deployment(sealed=False)))
    paths["manifest"].write_text(json.dumps(module1_manifest()))
    paths["skey"].write_text(json.dumps({"type": "PaymentSigningKeyShelley_ed25519", "description": "",
                                         "cborHex": "5820" + bytes(OPERATOR_SKEY.payload).hex()}))
    paths["vkey"].write_text(json.dumps({"type": "PaymentVerificationKeyShelley_ed25519", "description": "",
                                         "cborHex": "5820" + bytes(OPERATOR_VKEY.payload).hex()}))
    return {key: str(value) for key, value in paths.items()}


@pytest.fixture(scope="module")
def plan(files) -> dict:
    """The plan of deploy.py for the seed SEED. The tool applies the
    parameters in a temporary directory: contracts/ gets no file."""
    before = sorted(p.name for p in dc.CONTRACT_DIR.iterdir())
    identity = dc.load_identity_deployment(files["deployment"], "mainnet")
    result = deploy.module6_plan(identity, OPERATOR, SEED_REF)
    assert sorted(p.name for p in dc.CONTRACT_DIR.iterdir()) == before
    return result


def empty_state(path: Path) -> vd.DeployState:
    return vd.DeployState.load(path / deploy.STATE_FILE, module=deploy.MODULE, network="mainnet")


@pytest.fixture(scope="module")
def built(plan, tmp_path_factory) -> dict:
    """Every step built in one dry run: {step name: Built}, and the pool of
    UTxOs (on the fake chain and virtual) that the transactions read."""
    ogmios = ogmios_with(*funding())
    run = vd.Run(chain_of(ogmios), OPERATOR, protected={CONFIG_SEED}, protected_addresses={SEAL_SEED_ADDRESS},
                 log=lambda _m: None)
    state = empty_state(tmp_path_factory.mktemp("built"))
    result, found = vd.plan_steps(run, deploy.module6_steps(plan, state), None)
    assert found == []
    pool = list(ogmios.store.values()) + list(run.chain.virtual.values())
    return {"by_name": {b.step.name: b for b in result}, "order": [b.step.name for b in result],
            "pool": pool, "ogmios": ogmios}


def _tx(b) -> Transaction:
    return Transaction.from_cbor(b.cbor)


def _output(b, index: int) -> UTxO:
    return UTxO(TransactionInput(TransactionId(bytes.fromhex(b.tx_id)), index), _tx(b).transaction_body.outputs[index])


def _tokens(output) -> dict:
    assets = output.amount.multi_asset if not isinstance(output.amount, int) else None
    return {bytes(p.payload).hex(): {bytes(n.payload): q for n, q in a.items()} for p, a in (assets or {}).items()}


def _min_utxo(b, index: int) -> int:
    return (160 + len(vd.raw_outputs(b.cbor)[index])) * COINS_PER_UTXO_BYTE


def deployed_state(plan: dict, built: dict, path: Path) -> vd.DeployState:
    """The state that deploy.py writes when it sends the built steps."""
    state = empty_state(path)
    deploy.write_plan(state, plan)
    for name in deploy.STEP_NAMES:
        b = built["by_name"][name]
        state.record(name, b.tx_id, b.step.record(b.tx_id))
    deploy.update_state(state)
    return state


@pytest.fixture(scope="module")
def deployed(files, tmp_path_factory) -> dict:
    """A full send (run --submit) on the fake chain: the chain and the state
    after the deployment."""
    tmp = tmp_path_factory.mktemp("deployed")
    ogmios = ogmios_with(*funding())
    submit = FakeSubmit(ogmios)
    rc = deploy.main(["run", "--submit", "--deployment", files["deployment"], "--skey", files["skey"],
                      "--module1-manifest", files["manifest"], "--state-dir", str(tmp)],
                     chain=chain_of(ogmios), post=submit, sleep=no_sleep)
    assert rc == 0
    return {"store": dict(ogmios.store), "state": tmp / deploy.STATE_FILE, "sent": list(submit.sent)}


def copy_deployed(deployed: dict, tmp_path: Path) -> tuple[FakeOgmios, Path]:
    """A fake chain and a state directory of their own for one test."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    shutil.copy(deployed["state"], state_dir / deploy.STATE_FILE)
    return ogmios_with(store=deployed["store"]), state_dir


# ── The plan ─────────────────────────────────────────────────────────────────


def test_the_plan_applies_governance_config_to_the_five_validators(plan):
    holders = plan["holders"]
    assert [(info["tag"], role) for role, info in holders.items()] == dc.HOLDER_TAGS
    config = cbor2.loads(bytes.fromhex(plan["governance_config_cbor"]))
    assert bytes.fromhex(plan["governance_config_cbor"])[:3] == b"\xd8\x79\x8b"
    assert [f.hex() for f in config.value] == [
        plan["refs_token_policy"], "00" * 28, "00" * 28, "00" * 28, holders["oracle"]["hash"],
        holders["params"]["hash"], holders["treasury"]["hash"], "00" * 28, "00" * 28,
        plan["record_token_policy"], IDENTITY_HASH.hex(),
    ]
    assert plan["refs_token_policy"] == vd.native_policy_id(vd.operator_native_script(OPERATOR)).hex()
    for title, info in plan["validators"].items():
        module, validator, _ = title.split(".")
        applied = apply_validator(deploy.BLUEPRINT_PATH, module, validator, plan["governance_config_cbor"])
        assert (applied.hash, applied.compiled_code) == (info["hash"], info["compiled_code"]), title
    assert plan["cross_refs_address"] == holders["oracle"]["address"]
    assert plan["activity_head"]["address"] == str(vd.script_address(bytes.fromhex(plan["proposal_validator_hash"])))


def test_the_holders_have_their_own_addresses(plan):
    """holder(61|62|63, operator) never shares an address with the tags of
    the previous deployment (1, 2, 3) or of Module-3 (31, 33) for the same
    operator key."""
    ours = {info["address"] for info in plan["holders"].values()}
    assert len(ours) == 3
    others = {str(vd.script_address(bytes.fromhex(apply_holder(tag, OPERATOR.hex())["hash"])))
              for tag in (1, 2, 3, 31, 33)}
    assert len(others) == 5 and not ours & others


# ── Every step ───────────────────────────────────────────────────────────────


def test_every_step_builds_with_the_ledger_fee_and_size(built):
    assert built["order"] == list(deploy.STEP_NAMES)
    for name, b in built["by_name"].items():
        assert b.fee >= b.min_fee, name
        assert b.fee - b.min_fee < 5_000, name
        assert b.signed_size <= 16_384, name
        assert b.cost == b.fee + b.deposit + b.locked
        body = _tx(b).transaction_body
        assert all(u.output.address == OPERATOR_ADDR for u in built["pool"] if u.input in body.inputs), name
        assert vd.parse_ref(CONFIG_SEED) not in body.inputs


def test_only_the_records_step_runs_a_plutus_script(built):
    """aiken tx simulate (through FakeOgmios) runs the record token policy of
    the records step and reports its execution units."""
    for name, b in built["by_name"].items():
        if name == "records":
            assert set(b.budgets) == {"mint:0"}
            assert b.budgets["mint:0"].mem > 0 and b.budgets["mint:0"].steps > 0
        else:
            assert b.budgets == {}, name
    records = built["by_name"]["records"]
    assert any(tx == records.cbor for tx, _ in built["ogmios"].evaluated)


def test_each_step_spends_the_change_of_the_step_before(built):
    by_name = built["by_name"]
    for before, after in zip(deploy.STEP_NAMES, deploy.STEP_NAMES[1:]):
        inputs = _tx(by_name[after]).transaction_body.inputs
        assert any(bytes(i.transaction_id).hex() == by_name[before].tx_id for i in inputs), after


# ── Records ──────────────────────────────────────────────────────────────────


def test_records_spend_the_seed_and_mint_the_three_record_tokens(plan, built):
    b = built["by_name"]["records"]
    tx = _tx(b)
    body = tx.transaction_body
    assert SEED in body.inputs
    policy = plan["record_token_policy"]
    assert {bytes(p.payload).hex(): {bytes(n.payload): q for n, q in a.items()} for p, a in body.mint.items()} == {
        policy: {b"params": 1, b"oracle": 1, b"activity": 1}}
    (script,) = tx.transaction_witness_set.plutus_v3_script
    assert bytes(script).hex() == plan["record_token_compiled_code"]
    assert vd.script_hash_of(script).hex() == policy
    (redeemer,) = tx.transaction_witness_set.redeemer.values()
    assert redeemer.data.data == dc.UNIT_REDEEMER  # unit: constructor 0, no fields
    (collateral,) = body.collateral
    assert vd.lovelace_only(next(u for u in built["pool"] if u.input == collateral))


def test_records_hold_the_datums_of_the_aiken_types(plan, built):
    b = built["by_name"]["records"]
    params, oracle, head = (_output(b, i) for i in range(3))
    holders = plan["holders"]
    policy = plan["record_token_policy"]
    assert str(params.output.address) == holders["params"]["address"]
    assert str(oracle.output.address) == holders["oracle"]["address"]
    assert str(head.output.address) == plan["activity_head"]["address"]
    for utxo, name in ((params, b"params"), (oracle, b"oracle"), (head, b"activity")):
        assert _tokens(utxo.output) == {policy: {name: 1}}
        assert utxo.output.amount.coin == 3_000_000
    for index in range(3):
        assert _min_utxo(b, index) < 3_000_000
    expected = gi.GovernanceParams(**dc.DEFAULT_GOVERNANCE_PARAMS)
    assert vd.datum_cbor(params) == gi.inline_datum(expected.to_data()).cbor
    assert gi.GovernanceParams.from_data(params.output.datum) == expected
    oracle_tree = cbor2.loads(vd.datum_cbor(oracle))
    assert oracle_tree == gi._constr(0, [gi.Credential.key(OPERATOR).to_data(),
                                         bytes.fromhex(holders["treasury"]["hash"]), gi._constr(1)])
    assert gi.ActivityDatum.from_data(head.output.datum) == gi.ActivityDatum(
        b"", gi.Credential.key(OPERATOR), 0, 0, b"")


# ── Reference scripts, treasury batches, CrossRefs ───────────────────────────


@pytest.mark.parametrize("name", PUBLISH_STEPS)
def test_publish_puts_the_script_at_output_0_of_the_reference_address(plan, built, name):
    b = built["by_name"][name]
    out = _tx(b).transaction_body.outputs[0]
    role = name[len("publish-holder-"):] if name.startswith("publish-holder-") else None
    if role:
        expected = plan["holders"][role]
    else:
        title = next(t for t, short in dc.VALIDATOR_TITLES.items() if f"publish-{short}" == name)
        expected = plan["validators"][title]
    assert str(out.address) == REFERENCE_ADDRESS
    assert bytes(out.script).hex() == expected["compiled_code"]
    assert vd.script_hash_of(out.script).hex() == expected["hash"]
    assert out.datum is None and not _tokens(out)
    assert out.amount.coin == _min_utxo(b, 0)
    assert [o.index for o in b.outputs] == [0]
    assert _tx(b).transaction_body.mint is None


@pytest.mark.parametrize("name", TREASURY_STEPS)
def test_treasury_batches_hold_30_ap3x_and_the_batch_datum(plan, built, name):
    b = built["by_name"][name]
    out = _output(b, 0).output
    batch_id = int(name.split("-")[1])
    assert str(out.address) == plan["holders"]["treasury"]["address"]
    assert out.amount.coin == 30_000_000 and not _tokens(out)
    assert vd.datum_cbor(UTxO(None, out)) == dc.plutus_data_cbor(dc.treasury_batch_data(batch_id, True))
    assert cbor2.loads(vd.datum_cbor(UTxO(None, out))) == gi._constr(0, [batch_id, gi._constr(1)])
    assert [o.index for o in b.outputs] == [0]


def test_cross_refs_mints_the_governance_refs_token_with_the_operator_native_script(plan, built):
    b = built["by_name"]["cross-refs"]
    tx = _tx(b)
    policy = plan["refs_token_policy"]
    assert {bytes(p.payload).hex(): {bytes(n.payload): q for n, q in a.items()}
            for p, a in tx.transaction_body.mint.items()} == {policy: {b"governance_refs": 1}}
    witness = cbor2.loads(vd._tx_items(b.cbor)[1])
    assert set(witness) == {1}  # native scripts only: no Plutus script, no redeemer
    assert [cbor2.dumps(s) for s in witness[1]] == [cbor2.dumps([0, OPERATOR])]
    out = _output(b, 0)
    assert str(out.output.address) == plan["holders"]["oracle"]["address"]
    assert _tokens(out.output) == {policy: {b"governance_refs": 1}}
    assert out.output.amount.coin == _min_utxo(b, 0)
    deployment = gi.GovernanceDeployment.from_state(deploy_state(
        validators=plan["validators"], refs_token_policy=policy,
        cross_refs_address=plan["cross_refs_address"]))
    assert gi.CrossRefs.from_data(out.output.datum) == deployment.cross_refs


# ── The state ────────────────────────────────────────────────────────────────


def test_the_clients_load_the_deploy_state(deployed):
    """governance_identity.GovernanceDeployment.from_state reads the state of
    a send. Every reference script and the CrossRefs output are output 0 of
    their transactions. client_settings has every value."""
    data = json.loads(deployed["state"].read_text())
    assert data["format"] == vd.STATE_FORMAT and data["module"] == "module6" and data["network"] == "mainnet"
    assert list(data["steps"]) == list(deploy.STEP_NAMES) and data["pending"] == {}
    steps = data["steps"]
    records = steps["records"]["tx"]
    assert data["tx_hashes"] == {
        **{key: steps[name]["tx"] for name, key in dc.STEP_TX_KEYS.items()},
        **{f"treasury_batch_{i}": steps[f"treasury-{i}"]["tx"] for i in range(1, 4)},
        "params_utxo": records, "oracle_utxo": records, "activity_head": records,
    }
    assert data["record_token_seed"] == SEED_REF and data["holder_owner_vkey_hash"] == OPERATOR.hex()
    assert data["activity_head"]["tx"] == records
    assert data["governance_params"] == dc.DEFAULT_GOVERNANCE_PARAMS
    d = gi.GovernanceDeployment.from_state(data)
    assert d.identity_hash == IDENTITY_HASH
    assert d.record_token_policy.hex() == data["record_token_policy"]
    assert d.refs_token_policy.hex() == data["refs_token_policy"]
    assert d.params_holder_hash.hex() == data["holders"]["params"]["hash"]
    assert d.oracle_holder_hash.hex() == data["holders"]["oracle"]["hash"]
    assert str(d.cross_refs_address) == data["holders"]["oracle"]["address"]
    assert d.cross_refs_tx.hex() == steps["cross-refs"]["tx"]
    on_chain = deployed["store"]
    for script in (d.proposal_spend, d.proposal_mint, d.critique_spend, d.critique_mint, d.endorsement_spend):
        ref = vd.ref_key(script.reference.input)
        assert ref.endswith("#0") and ref in on_chain
        assert vd.script_hash_of(on_chain[ref].output.script) == script.hash
    assert all(value is not None for client in data["client_settings"].values() for value in client.values())
    mcp, sdk = data["client_settings"]["mcp-server"], data["client_settings"]["agent-sdk-py"]
    assert not any(name.endswith("_UTXO") for name in mcp)
    assert sdk["VECTOR_MODULE6_PARAMS_UTXO"] == f"{records}#0" and sdk["VECTOR_MODULE6_ORACLE_UTXO"] == f"{records}#1"
    assert sdk["VECTOR_MODULE6_CROSSREFS_UTXO"] == f"{steps['cross-refs']['tx']}#0"
    assert mcp["GOV_TREASURY_ADDRESS"] == data["holders"]["treasury"]["address"]
    assert data["client_settings"]["agent-sdk-py"]["VECTOR_MODULE6_PROPOSAL_SPEND_REF_UTXO"] == \
        f"{steps['publish-proposal_spend']['tx']}#0"


def test_a_new_run_finds_every_step_on_chain(files, deployed, tmp_path, capsys):
    ogmios, state_dir = copy_deployed(deployed, tmp_path)
    rc = deploy.main(["--deployment", files["deployment"], "--vkey", files["vkey"], "--state-dir", str(state_dir)],
                     chain=chain_of(ogmios))
    assert rc == 0
    assert "nothing to do: the chain shows every step" in capsys.readouterr().out


def test_a_state_of_another_format_is_refused(files, tmp_path, capsys):
    """The tool never writes the state of the previous deployment."""
    (tmp_path / deploy.STATE_FILE).write_text(json.dumps(deploy_state()))
    rc = deploy.main(["--deployment", files["deployment"], "--vkey", files["vkey"], "--state-dir", str(tmp_path)],
                     chain=chain_of(ogmios_with(*funding())))
    assert rc == 1
    assert "not a state file of this deploy tool" in capsys.readouterr().err
    assert json.loads((tmp_path / deploy.STATE_FILE).read_text()) == deploy_state()


# ── Runs: dry run, seed, resume, protection, offline signing ─────────────────


def test_a_dry_run_with_dry_run_funds_uses_the_virtual_output_as_seed(files, tmp_path):
    """The operator address has no funds. --dry-run-funds adds a virtual
    output; it is the record token seed. The dry run writes no state."""
    report = tmp_path / "report.json"
    ogmios = ogmios_with()
    rc = deploy.main(["--deployment", files["deployment"], "--vkey", files["vkey"], "--dry-run-funds", "600",
                      "--state-dir", str(tmp_path / "state"), "--report", str(report)], chain=chain_of(ogmios))
    assert rc == 0
    assert not (tmp_path / "state").exists()
    data = json.loads(report.read_text())
    assert data["seed_is_virtual"] and data["record_token_seed"] == VIRTUAL_SEED
    assert [t["step"] for t in data["transactions"]] == list(deploy.STEP_NAMES)
    assert all(t["virtual_inputs"] for t in data["transactions"])
    records = data["transactions"][0]
    assert VIRTUAL_SEED in records["virtual_inputs"] and set(records["budgets"]) == {"mint:0"}
    assert data["totals"]["cost"] == sum(t["cost"] for t in data["transactions"])


def test_the_seed_is_saved_before_the_first_step_and_a_resumed_run_reuses_it(files, tmp_path):
    """The submit endpoint fails after two transactions. The state has the
    seed and the two steps. The next run reuses the seed (the largest
    operator output is now another one), skips the two steps and sends the
    others."""
    ogmios = ogmios_with(*funding())

    class FailingSubmit(FakeSubmit):
        def __call__(self, url, data=None, headers=None, timeout=None, **kw):
            if len(self.sent) == 2:
                raise requests.ConnectionError("the submit endpoint is down")
            return super().__call__(url, data=data, headers=headers, timeout=timeout, **kw)

    argv = ["run", "--submit", "--deployment", files["deployment"], "--skey", files["skey"],
            "--module1-manifest", files["manifest"], "--state-dir", str(tmp_path)]
    assert deploy.main(argv, chain=chain_of(ogmios), post=FailingSubmit(ogmios), sleep=no_sleep) == 1
    state = json.loads((tmp_path / deploy.STATE_FILE).read_text())
    assert state["record_token_seed"] == SEED_REF
    assert list(state["steps"]) == ["records", "publish-holder-params"]
    assert state["client_settings"]["agent-sdk-py"]["VECTOR_MODULE6_PARAMS_UTXO"] == \
        f"{state['steps']['records']['tx']}#0"
    assert state["client_settings"]["mcp-server"]["GOV_PROPOSAL_SPEND_REF"] is None

    # A larger operator output than the change: a new seed choice would take it.
    ogmios.add(UTxO(TransactionInput(TransactionId(b"\x1f" * 32), 0), TransactionOutput(OPERATOR_ADDR, Value(900_000_000))))
    submit = FakeSubmit(ogmios)
    assert deploy.main(argv, chain=chain_of(ogmios), post=submit, sleep=no_sleep) == 0
    assert len(submit.sent) == len(deploy.STEP_NAMES) - 2
    state = json.loads((tmp_path / deploy.STATE_FILE).read_text())
    assert state["record_token_seed"] == SEED_REF
    assert list(state["steps"]) == list(deploy.STEP_NAMES)


def test_a_run_skips_the_steps_that_the_chain_shows(plan, built, tmp_path):
    """The chain shows the records, the proposal_mint reference script and
    treasury batch 1: only the other steps are built."""
    by_name = built["by_name"]
    shown = [*(_output(by_name["records"], i) for i in range(3)), _output(by_name["publish-proposal_mint"], 0),
             _output(by_name["treasury-1"], 0)]
    ogmios = ogmios_with(*funding(), *shown)
    run = vd.Run(chain_of(ogmios), OPERATOR, protected={CONFIG_SEED}, protected_addresses={SEAL_SEED_ADDRESS},
                 log=lambda _m: None)
    result, found = vd.plan_steps(run, deploy.module6_steps(plan, empty_state(tmp_path)), None)
    assert [b.step.name for b in result] == [
        n for n in deploy.STEP_NAMES if n not in ("records", "publish-proposal_mint", "treasury-1")]
    found = {step.name: values for step, values in found}
    records = by_name["records"].tx_id
    assert found["records"] == {"tx": records, "params": f"{records}#0", "oracle": f"{records}#1",
                                "activity_head": f"{records}#2"}
    mint = by_name["publish-proposal_mint"].tx_id
    assert found["publish-proposal_mint"] == {"tx": mint, "ref": f"{mint}#0"}
    batch = by_name["treasury-1"].tx_id
    assert found["treasury-1"] == {"tx": batch, "batch": f"{batch}#0", "lovelace": 30_000_000}


def test_a_spent_treasury_batch_stays_done(plan, built, tmp_path):
    """An adoption spends a treasury batch. The state keeps the step, and a
    new run does not make the batch again."""
    state = deployed_state(plan, built, tmp_path)
    step = deploy.treasury_step(state, plan["holders"]["treasury"]["address"], 1, 30_000_000)
    run = vd.Run(chain_of(ogmios_with(*funding())), OPERATOR, log=lambda _m: None)
    assert step.find(run) == {key: value for key, value in state.steps["treasury-1"].items() if key != "time"}
    records = deploy.records_step(state, plan)
    with pytest.raises(vd.DeployError, match="the chain does not show its result"):
        vd.plan_steps(run, [records], state)


def test_the_config_seed_and_the_seal_seed_address_are_protected(plan, files, tmp_path):
    """config_seed is the largest operator output: the tool never chooses it
    as seed and never spends it. A records step with that seed is refused.
    The operator address can never be the seal-seed address."""
    config_seed = UTxO(vd.parse_ref(CONFIG_SEED), TransactionOutput(OPERATOR_ADDR, Value(900_000_000)))
    report = tmp_path / "report.json"
    rc = deploy.main(["--deployment", files["deployment"], "--vkey", files["vkey"], "--state-dir",
                      str(tmp_path / "state"), "--report", str(report)],
                     chain=chain_of(ogmios_with(*funding(), config_seed)))
    assert rc == 0
    assert json.loads(report.read_text())["record_token_seed"] == SEED_REF
    run = vd.Run(chain_of(ogmios_with(*funding(), config_seed)), OPERATOR, protected={CONFIG_SEED},
                 protected_addresses={SEAL_SEED_ADDRESS}, log=lambda _m: None)
    state = empty_state(tmp_path)
    assert deploy.choose_seed(run, state) == (SEED_REF, False)
    assert config_seed not in run.wallet_utxos()
    result, _ = vd.plan_steps(run, deploy.module6_steps(plan, state), None)
    assert [b.step.name for b in result] == list(deploy.STEP_NAMES)
    for b in result:
        body = _tx(b).transaction_body
        assert config_seed.input not in body.inputs and config_seed.input not in (body.collateral or [])
    bad = dict(plan, record_token_seed=CONFIG_SEED)
    run = vd.Run(chain_of(ogmios_with(*funding(), config_seed)), OPERATOR, protected={CONFIG_SEED},
                 protected_addresses={SEAL_SEED_ADDRESS}, log=lambda _m: None)
    with pytest.raises(vd.DeployError, match="protected output"):
        vd.plan_steps(run, [deploy.records_step(state, bad)], None)
    with pytest.raises(vd.DeployError, match="protected address"):
        vd.Run(chain_of(ogmios_with()), OPERATOR, protected_addresses={str(OPERATOR_ADDR)})


def test_build_sign_submit(files, tmp_path):
    """The offline flow: build writes the unsigned transactions and saves the
    seed, sign adds the operator witness without a network, submit sends them
    in order. The submit action checks the seal again: a seal that names
    another hash stops it before the first transaction."""
    ogmios = ogmios_with(*funding())
    common = ["--state-dir", str(tmp_path)]
    send = ["--deployment", files["deployment"], "--vkey", files["vkey"], "--module1-manifest", files["manifest"]]
    assert deploy.main(["build", *send, *common], chain=chain_of(ogmios)) == 0
    state = json.loads((tmp_path / deploy.STATE_FILE).read_text())
    assert state["record_token_seed"] == SEED_REF and list(state["pending"]) == list(deploy.STEP_NAMES)
    files_written = sorted(p.name for p in (tmp_path / "tx").glob("*.unsigned.json"))
    assert files_written == [f"{i:02d}-{name}.unsigned.json" for i, name in enumerate(deploy.STEP_NAMES, 1)]
    assert deploy.main(["sign", "--skey", files["skey"], *common]) == 0
    submit = FakeSubmit(ogmios)
    ogmios.add(config_record(b"\xe2" * 28))
    assert deploy.main(["submit", *send, *common], chain=chain_of(ogmios), post=submit, sleep=no_sleep) == 1
    assert submit.sent == []
    ogmios.add(config_record(M1))
    assert deploy.main(["submit", *send, *common], chain=chain_of(ogmios), post=submit, sleep=no_sleep) == 0
    assert len(submit.sent) == len(deploy.STEP_NAMES)
    assert all(OPERATOR in vd.vkey_witness_hashes(tx) for tx in submit.sent)
    state = json.loads((tmp_path / deploy.STATE_FILE).read_text())
    assert list(state["steps"]) == list(deploy.STEP_NAMES) and state["pending"] == {}
    assert state["tx_hashes"]["cross_refs_nft"] == state["steps"]["cross-refs"]["tx"]


def test_another_operator_key_is_refused(files, tmp_path, capsys):
    other = PaymentSigningKey(hashlib.blake2b(b"module6-deploy-tests:other", digest_size=32).digest())
    vkey = tmp_path / "other.vkey"
    vkey.write_text(json.dumps({"type": "PaymentVerificationKeyShelley_ed25519", "description": "",
                                "cborHex": "5820" + bytes(PaymentVerificationKey.from_signing_key(other).payload).hex()}))
    rc = deploy.main(["--deployment", files["deployment"], "--vkey", str(vkey), "--state-dir", str(tmp_path)],
                     chain=chain_of(ogmios_with(*funding())))
    assert rc == 1
    assert "is not the key of the operator address" in capsys.readouterr().err


# ── Sends wait for the seal of M1 ────────────────────────────────────────────


def send_argv(action: str, files: dict, state_dir: Path, *, deployment: str | None = None,
              manifest: str | None = None) -> list[str]:
    """The arguments of a send: run --submit (with --skey) or build (with --vkey)."""
    argv = ["run", "--submit", "--skey", files["skey"]] if action == "run" else [action, "--vkey", files["vkey"]]
    argv += ["--deployment", deployment or files["deployment"], "--state-dir", str(state_dir)]
    return argv + (["--module1-manifest", manifest] if manifest else [])


@pytest.mark.parametrize("action", ["run", "build"])
def test_a_send_needs_the_module1_manifest(files, tmp_path, capsys, action):
    submit = FakeSubmit(ogmios := ogmios_with(*funding()))
    assert deploy.main(send_argv(action, files, tmp_path), chain=chain_of(ogmios), post=submit, sleep=no_sleep) == 1
    assert "needs --module1-manifest" in capsys.readouterr().err
    assert submit.sent == [] and not (tmp_path / deploy.STATE_FILE).exists()


@pytest.mark.parametrize("action", ["run", "build"])
@pytest.mark.parametrize("case, note", [
    ("no seal yet", "has no seal yet (seal.config_record)"),
    ("no record on chain", "is not on chain"),
    ("another hash on chain", "the seal on chain names ANOTHER hash"),
    ("another hash in deployment.json", "deployment.json seal.module1_hash names ANOTHER hash"),
])
def test_a_send_waits_for_the_seal_of_m1(files, tmp_path, capsys, action, case, note):
    """Module-1 seal_status must find the configuration record on chain and
    it must name M1 of the Module-1 manifest. Else a send stops before it
    writes the state."""
    other = b"\xe2" * 28
    deployment, seal = files["deployment"], M1
    if case == "no seal yet":
        deployment = files["unsealed"]
    elif case == "no record on chain":
        seal = None
    elif case == "another hash on chain":
        seal = other
    else:
        deployment = str(tmp_path / "deployment.json")
        Path(deployment).write_text(json.dumps(identity_deployment(module1_hash=other)))
    ogmios = ogmios_with(*funding(), seal=seal)
    submit = FakeSubmit(ogmios)
    argv = send_argv(action, files, tmp_path / "state", deployment=deployment, manifest=files["manifest"])
    assert deploy.main(argv, chain=chain_of(ogmios), post=submit, sleep=no_sleep) == 1
    out = capsys.readouterr()
    assert f"Seal check: " in out.out and note in out.out
    assert "only after the seal of M1" in out.err
    assert submit.sent == [] and not (tmp_path / "state").exists()


@pytest.mark.parametrize("change, error", [
    ({"identity": {"identity_hash": "2e" * 28}}, "names another identity_hash"),
    ({"network": "testnet"}, "network 'testnet'"),
    ({"module": "module3"}, "not a manifest of Module-1"),
])
def test_the_module1_manifest_must_match_the_identity_deployment(files, tmp_path, capsys, change, error):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(module1_manifest(**change)))
    argv = send_argv("build", files, tmp_path / "state", manifest=str(manifest))
    assert deploy.main(argv, chain=chain_of(ogmios_with(*funding()))) == 1
    assert error in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("with_manifest", [True, False])
def test_a_dry_run_works_before_the_seal(files, tmp_path, capsys, with_manifest):
    """The identity deployment has no seal yet. The dry run builds every
    step, prints the seal status and writes it into the report."""
    report = tmp_path / "report.json"
    argv = ["--deployment", files["unsealed"], "--vkey", files["vkey"], "--state-dir", str(tmp_path / "state"),
            "--report", str(report)] + (["--module1-manifest", files["manifest"]] if with_manifest else [])
    assert deploy.main(argv, chain=chain_of(ogmios_with(*funding(), seal=None))) == 0
    note = "has no seal yet (seal.config_record)" if with_manifest else "not checked (no --module1-manifest"
    assert f"Seal check: " in (out := capsys.readouterr().out) and note in out
    data = json.loads(report.read_text())
    assert data["seal_check"]["ok"] is False and note in data["seal_check"]["note"]
    assert [t["step"] for t in data["transactions"]] == list(deploy.STEP_NAMES)
    assert not (tmp_path / "state").exists()


# ── The validators read the records of the deploy ────────────────────────────


def test_a_proposal_submission_reads_the_records_of_the_deploy(plan, built, tmp_path):
    """The first SubmitProposal of an agent reads the GovernanceParams record
    and the CrossRefs output of the deploy, spends the head of the activity
    list of the deploy, and uses the reference scripts of the deploy. The
    applied validators accept it (aiken tx simulate)."""
    state = deployed_state(plan, built, tmp_path)
    world = make_world(json.loads(state.path.read_text()))
    world.chain.evaluate_tx = lambda tx: {
        f"{key.tag.name.lower()}:{key.index}": ExecutionUnits(4_000_000, 1_500_000_000)
        for key in tx.transaction_witness_set.redeemer.keys()
    }
    by_name = built["by_name"]
    params, oracle, head = (_output(by_name["records"], i) for i in range(3))
    cross_refs = _output(by_name["cross-refs"], 0)
    scripts = [_output(by_name[f"publish-{short}"], 0) for short in dc.VALIDATOR_TITLES.values()]
    # The world of conftest has records, a head and reference scripts of its
    # own: the outputs of the deploy take their place.
    world.chain.store[str(world.deployment.proposal_spend.address)] = []
    for script in (world.deployment.proposal_spend, world.deployment.proposal_mint, world.deployment.critique_spend,
                   world.deployment.critique_mint, world.deployment.endorsement_spend):
        address = str(script.address)
        world.chain.store[address] = [u for u in world.chain.store.get(address, []) if u.input != script.reference.input]
    world.chain.add(params, oracle, head, cross_refs, *scripts)
    world.params, world.oracle, world.cross_refs = params, oracle, cross_refs
    assert world.activity_list().head.utxo.input == head.input

    proposer = world.agent(PROPOSER)
    window = world.window()
    lock = gi.prepare_proposal_lock(
        world.chain, world.deployment, records=[proposer], refs=world.refs, activity_list=world.activity_list(),
        terms=gi.ProposalTerms(vid.blake2b_256(b"proposal"), "ipfs://QmProposal", 25_000_000, 604_800_000),
        credential_key_hash=world.key_hash, window=window, wallet_address=world.wallet_address,
    )
    locked = world.locked(world.deployment.proposal_spend, lock.datum, lock.datum.stake_amount + gi.POSITION_DEPOSIT)
    prepared = gi.prepare_submit_proposal(
        world.chain, world.deployment, locked=locked, records=[proposer], refs=world.refs,
        activity_list=world.activity_list(), window=window, wallet_address=world.wallet_address,
    )
    tx = gi.sign_prepared(prepared, keys=[world.wallet], change_address=world.wallet_address)
    body = tx.transaction_body
    assert head.input in body.inputs
    assert {params.input, cross_refs.input} <= set(body.reference_inputs)
    assert {u.input for u in scripts} & set(body.reference_inputs)
    pool = [u for utxos in world.chain.store.values() for u in utxos]
    result = simulate(tx, resolved_inputs(tx, pool), zero_time_ms=SYSTEM_START_MS, slot_length_ms=1000)
    assert result.passed, result.describe()
    assert len(result.verdicts) == 3  # the lock, the head and the proposal token


# ── deploy_params.py ─────────────────────────────────────────────────────────


def test_deploy_params_refuses_a_state_without_a_params_record(files, plan, tmp_path, capsys):
    rc = deploy_params.main(["--vkey", files["vkey"], "--state-dir", str(tmp_path)], chain=chain_of(ogmios_with()))
    assert rc == 1
    assert "Run scripts/deploy.py first" in capsys.readouterr().err
    state = empty_state(tmp_path)
    deploy.write_plan(state, plan)
    deploy.update_state(state)
    rc = deploy_params.main(["--vkey", files["vkey"], "--state-dir", str(tmp_path)], chain=chain_of(ogmios_with()))
    assert rc == 1
    assert "has no params record yet. Run scripts/deploy.py first" in capsys.readouterr().err


def test_deploy_params_builds_the_replacement_of_the_params_record(deployed, tmp_path):
    """The transaction spends the params record with the params holder (unit
    redeemer, the operator as required signer, collateral) and creates it
    again at the same address with the `params` token and the new values. The
    holder runs in the evaluation (aiken tx simulate)."""
    ogmios, state_dir = copy_deployed(deployed, tmp_path)
    state = vd.DeployState.load(state_dir / deploy.STATE_FILE, module="module6", network="mainnet")
    values = dict(dc.DEFAULT_GOVERNANCE_PARAMS, proposal_cooldown=3_600_000, max_amendments=7)
    step = deploy_params.params_update_step(state, deploy_params.next_step_name(state), values)
    assert step.name == "params-update-1"
    run = vd.Run(chain_of(ogmios), OPERATOR, protected={CONFIG_SEED}, protected_addresses={SEAL_SEED_ADDRESS},
                 log=lambda _m: None)
    (b,), found = vd.plan_steps(run, [step], state)
    assert found == []
    tx = _tx(b)
    body = tx.transaction_body
    old = state.steps["records"]["params"]
    assert old in {vd.ref_key(i) for i in body.inputs}
    (purpose,) = b.budgets  # the holder spend is the only redeemer
    assert purpose.startswith("spend:") and b.budgets[purpose].mem > 0
    holder = state.data["holders"]["params"]
    assert [bytes(s).hex() for s in tx.transaction_witness_set.plutus_v3_script] == [holder["compiled_code"]]
    assert [bytes(s.payload) for s in body.required_signers] == [OPERATOR]
    assert body.collateral
    out = _output(b, 0).output
    assert str(out.address) == holder["address"]
    assert _tokens(out) == {state.data["record_token_policy"]: {b"params": 1}}
    assert out.amount.coin == 3_000_000
    assert gi.GovernanceParams.from_data(out.datum) == gi.GovernanceParams(**values)


def test_deploy_params_dry_run_send_and_no_change(files, deployed, tmp_path, capsys):
    ogmios, state_dir = copy_deployed(deployed, tmp_path)
    params_file = tmp_path / "params.json"
    values = dict(dc.DEFAULT_GOVERNANCE_PARAMS, proposal_cooldown=3_600_000)
    params_file.write_text(json.dumps(values))
    common = ["--state-dir", str(state_dir), "--params", str(params_file)]
    before = (state_dir / deploy.STATE_FILE).read_text()
    assert deploy_params.main(["--vkey", files["vkey"], *common], chain=chain_of(ogmios)) == 0
    out = capsys.readouterr().out
    assert "proposal_cooldown" in out and "86,400,000 -> 3,600,000" in out and "Dry run" in out
    assert (state_dir / deploy.STATE_FILE).read_text() == before

    submit = FakeSubmit(ogmios)
    assert deploy_params.main(["run", "--submit", "--skey", files["skey"], *common], chain=chain_of(ogmios),
                              post=submit, sleep=no_sleep) == 0
    assert len(submit.sent) == 1
    state = json.loads((state_dir / deploy.STATE_FILE).read_text())
    tx = state["steps"]["params-update-1"]["tx"]
    assert state["tx_hashes"]["params_utxo"] == tx
    assert "GOV_PARAMS_UTXO" not in state["client_settings"]["mcp-server"]
    assert state["client_settings"]["agent-sdk-py"]["VECTOR_MODULE6_PARAMS_UTXO"] == f"{tx}#0"
    assert state["governance_params"] == values
    record = ogmios.store[f"{tx}#0"]
    assert gi.GovernanceParams.from_data(record.output.datum) == gi.GovernanceParams(**values)
    assert gi.find_params([record], gi.GovernanceDeployment.from_state(state)).params.proposal_cooldown == 3_600_000

    capsys.readouterr()
    assert deploy_params.main(["--vkey", files["vkey"], *common], chain=chain_of(ogmios)) == 0
    assert "holds these values already: nothing to do" in capsys.readouterr().out
    # deploy.py still finds every step: the records step by its tokens.
    assert deploy.main(["--deployment", files["deployment"], "--vkey", files["vkey"], "--state-dir", str(state_dir)],
                       chain=chain_of(ogmios)) == 0
    assert "nothing to do: the chain shows every step" in capsys.readouterr().out


def test_deploy_params_refuses_a_bad_params_file(files, deployed, tmp_path, capsys):
    ogmios, state_dir = copy_deployed(deployed, tmp_path)
    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps({k: v for k, v in dc.DEFAULT_GOVERNANCE_PARAMS.items() if k != "max_amendments"}))
    rc = deploy_params.main(["--vkey", files["vkey"], "--state-dir", str(state_dir), "--params", str(params_file)],
                            chain=chain_of(ogmios))
    assert rc == 1
    assert "missing max_amendments" in capsys.readouterr().err


# ── treasury_fund.py ─────────────────────────────────────────────────────────


def test_next_batch_ids_follow_the_state():
    assert treasury_fund.next_batch_ids({}, 1) == [4]
    batches = {f"treasury_batch_{i}": "aa" * 32 for i in (1, 2, 3)}
    assert treasury_fund.next_batch_ids(batches, 2) == [4, 5]
    assert treasury_fund.next_batch_ids({**batches, "treasury_batch_7": "bb" * 32}, 1) == [8]


def test_treasury_fund_dry_run_and_send(files, deployed, tmp_path):
    ogmios, state_dir = copy_deployed(deployed, tmp_path)
    report = tmp_path / "report.json"
    common = ["--state-dir", str(state_dir), "--batches", "2", "--size", "40"]
    before = (state_dir / deploy.STATE_FILE).read_text()
    assert treasury_fund.main(["--vkey", files["vkey"], *common, "--report", str(report)], chain=chain_of(ogmios)) == 0
    assert (state_dir / deploy.STATE_FILE).read_text() == before
    data = json.loads(report.read_text())
    treasury = json.loads(before)["holders"]["treasury"]["address"]
    assert [t["step"] for t in data["transactions"]] == ["treasury-4", "treasury-5"]
    for t in data["transactions"]:
        assert [(o["index"], o["address"], o["coin"]) for o in t["outputs"]] == [(0, treasury, 40_000_000)]

    submit = FakeSubmit(ogmios)
    assert treasury_fund.main(["run", "--submit", "--skey", files["skey"], *common], chain=chain_of(ogmios),
                              post=submit, sleep=no_sleep) == 0
    state = json.loads((state_dir / deploy.STATE_FILE).read_text())
    for batch_id in (4, 5):
        tx = state["tx_hashes"][f"treasury_batch_{batch_id}"]
        batch = ogmios.store[f"{tx}#0"]
        assert str(batch.output.address) == treasury and batch.output.amount.coin == 40_000_000
        assert cbor2.loads(vd.datum_cbor(batch)) == dc.treasury_batch_data(batch_id, True)
    assert treasury_fund.next_batch_ids(state["tx_hashes"], 1) == [6]


def test_treasury_fund_needs_the_deploy_state_and_has_no_dry_run_flag(files, tmp_path, capsys):
    rc = treasury_fund.main(["--vkey", files["vkey"], "--state-dir", str(tmp_path)], chain=chain_of(ogmios_with()))
    assert rc == 1
    assert "Run scripts/deploy.py first" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        treasury_fund.main(["--vkey", files["vkey"], "--dry-run"])
