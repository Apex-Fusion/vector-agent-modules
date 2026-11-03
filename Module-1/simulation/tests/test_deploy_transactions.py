"""Offline tests of the Module-1 deploy transactions (``scripts/identity_deploy.py
deploy``) and of the shared deploy runner (``shared/python/vector_deploy.py``).

The runner builds each step against an offline Ogmios double with the Vector
mainnet protocol parameters (``shared/python/vector_deploy_testing.py``).
The applied validators come from ``contracts/plutus.json`` with a test
configuration in the oracle mode; the operator key is the oracle key of
``test_oracle_resolve``. ``aiken tx simulate``
(``shared/python/aiken_simulate.py``) evaluates the transactions. Nothing
goes to a network; all keys come from fixed test seeds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cbor2
import pytest
from nacl.signing import VerifyKey
from pycardano import (
    Address, ExecutionUnits, PaymentSigningKey, PaymentVerificationKey, PlutusData, PoolKeyHash,
    Redeemer, ScriptHash, StakeCredential, StakeDelegation, StakeDeregistration, Transaction,
    TransactionId, TransactionInput, TransactionOutput, UTxO, Value,
)

import simulation.tx_builder as txb
import vector_deploy as vd
from aiken_simulate import aiken_available, apply_validator, resolved_inputs, simulate
from holder_scripts import apply_holder
from simulation import identity_party as ip
from simulation.config import load_identity_deployment
from simulation.tests import test_oracle_resolve as tor
from simulation.tests.conftest import (
    CONFIG_HOLDER, CONFIG_HOLDER_ADDR, CONFIG_POLICY, IDENTITY_HASH, FakeOgmiosContext,
    config_record_utxo,
)
from vector_deploy_testing import FakeSubmit, fake_chain

requires_aiken = pytest.mark.skipif(not aiken_available(), reason="aiken is not on PATH")
pytestmark = requires_aiken

deploy = tor._deploy_script()

OPERATOR_SKEY, OPERATOR_VKEY, OPERATOR_ADDR = tor.ORACLE
OPERATOR = tor.ORACLE_VKH
REFERENCE_ADDRESS = deploy.bech32("addr", b"\x61" + bytes(28))
SEAL_SEED_ADDRESS = deploy.bech32("addr", b"\x61" + b"\x5e" * 28)
CONFIG_SEED = "ab" * 32 + "#0"
STEPS = ["publish-claim", "publish-challenge", "publish-jury_pool", "publish-identity_party",
         "register-identity_party", "records"]
COINS_PER_UTXO_BYTE = 4310


def identity_deployment() -> dict:
    return {
        "format": deploy.IDENTITY_DEPLOYMENT_FORMAT,
        "network": "mainnet",
        "identity_hash": IDENTITY_HASH.hex(),
        "config_policy": CONFIG_POLICY.hex(),
        "config_holder": CONFIG_HOLDER.hex(),
        "reference_address": REFERENCE_ADDRESS,
        "operator_address": str(OPERATOR_ADDR),
        "seal_seed_address": SEAL_SEED_ADDRESS,
        "config_seed": CONFIG_SEED,
        "refs": {"identity": "a1" * 32 + "#0"},
        "seal": None,
        "file": "deployment.json",
        "sha256": "00" * 32,
    }


@pytest.fixture(scope="module")
def plan() -> dict:
    """The manifest that ``plan --mode oracle`` writes for the test values,
    and the applied scripts of the plan."""
    config = deploy.load_config(deploy.DEFAULT_CONFIG)
    values = deploy.plan_values(config, identity_deployment(), OPERATOR, "oracle")
    config_cbor = deploy.encode(deploy.auditing_config(values)).hex()
    params_cbor = deploy.encode(deploy.protocol_params(values["protocol_params"])).hex()
    applied = {
        name: apply_validator(tor.BLUEPRINT, name, name, config_cbor, params_cbor)
        for name in ("claim", "challenge", "jury_pool")
    }
    applied["identity_party"] = apply_validator(
        tor.BLUEPRINT, "identity_party", "identity_party", config_cbor,
        deploy.encode(bytes.fromhex(applied["challenge"].hash)).hex(),
        deploy.encode(bytes.fromhex(applied["claim"].hash)).hex(),
    )
    hashes = {name: a.hash for name, a in applied.items()}
    holder = apply_holder(deploy.RECORDS_HOLDER_TAG, OPERATOR.hex())
    manifest = {
        "format": vd.STATE_FORMAT, "module": "module1", "network": "mainnet",
        "mode": "oracle", "placeholders": [], "not_for_deployment": False,
        "identity": values["identity"],
        "module1": dict(values["module1"], records_holder={
            "tag": deploy.RECORDS_HOLDER_TAG, "hash": holder["hash"],
            "address": deploy.script_addresses(holder["hash"])["enterprise"],
        }),
        "verdict_policy": values["verdict_policy"],
        "protocol_params": values["protocol_params"],
        "parameters": {
            "auditing_config_cbor": config_cbor,
            "protocol_params_cbor": params_cbor,
            "identity_party_challenge_hash_cbor": deploy.encode(bytes.fromhex(hashes["challenge"])).hex(),
            "identity_party_claim_hash_cbor": deploy.encode(bytes.fromhex(hashes["claim"])).hex(),
        },
        "hashes": hashes,
        "reference_address": REFERENCE_ADDRESS,
        "steps": {},
        "pending": {},
    }
    deploy.check_manifest(manifest, identity_deployment(), OPERATOR, "oracle")
    return {
        "manifest": manifest,
        "scripts": {name: bytes.fromhex(a.compiled_code) for name, a in applied.items()},
        "applied": applied,
        "holder": Address.from_primitive(manifest["module1"]["records_holder"]["address"]),
    }


def _funding(coin: int = 300_000_000, tag: bytes = b"\x0f") -> UTxO:
    return UTxO(TransactionInput(TransactionId(tag * 32), 0), TransactionOutput(OPERATOR_ADDR, Value(coin)))


def _run(plan, *, extra=(), funds: int = 300_000_000, dry_run_funds: int = 0):
    chain, ogmios, koios = fake_chain()
    if funds:
        ogmios.add(_funding(funds))
    ogmios.add(*extra)
    run = vd.Run(chain, OPERATOR, protected={CONFIG_SEED}, protected_addresses={SEAL_SEED_ADDRESS},
                 dry_run_funds=dry_run_funds, log=lambda _message: None)
    steps = deploy.module1_steps(vd, run, json.loads(json.dumps(plan["manifest"])), plan["scripts"])
    return run, steps, ogmios, koios


@pytest.fixture(scope="module")
def built(plan) -> dict:
    """Every step built in one dry run: {step name: Built}, and the pool of
    UTxOs (on the fake chain and virtual) that the transactions read."""
    run, steps, ogmios, _ = _run(plan)
    result, found = vd.plan_steps(run, steps, None)
    assert found == []
    pool = list(ogmios.store.values()) + list(run.chain.virtual.values())
    return {"by_name": {b.step.name: b for b in result}, "order": [b.step.name for b in result],
            "pool": pool, "run": run}


def _tx(b) -> Transaction:
    return Transaction.from_cbor(b.cbor)


def _witness_keys(b) -> set:
    raw = vd._tx_items(b.cbor)[1]
    return set(cbor2.loads(raw).keys())



# ── Every step ───────────────────────────────────────────────────────────────


def test_every_step_builds_with_the_ledger_fee_and_size(built):
    assert built["order"] == STEPS
    for name, b in built["by_name"].items():
        assert b.fee >= b.min_fee, name
        assert b.fee - b.min_fee < 5_000, name
        assert b.signed_size <= 16_384, name
        assert b.budgets == {}, f"{name}: no Plutus script runs in a Module-1 deploy transaction"
        assert b.cost == b.fee + b.deposit + b.locked


def test_every_step_passes_aiken_simulate_without_redeemers(built):
    """aiken tx simulate evaluates the transactions: there is no redeemer,
    so no script runs and the evaluation passes. Its phase-one check needs no
    script for the certificate 0 of Script(M1). It checks only Plutus
    scripts, so it reports the native-script policy of the records step as a
    missing validator; that step runs no Plutus script
    (test_records_mint_the_refs_token_with_the_operator_native_script)."""
    for name, b in built["by_name"].items():
        tx = _tx(b)
        assert 5 not in _witness_keys(b), f"{name} has redeemers"
        if name == "records":
            policy = tx.transaction_witness_set.native_scripts[0].hash().payload.hex()
            with pytest.raises(Exception, match=f"Missing {policy}"):
                simulate(tx, resolved_inputs(tx, built["pool"]), zero_time_ms=tor.ZERO_TIME_MS)
            continue
        sim = simulate(tx, resolved_inputs(tx, built["pool"]), zero_time_ms=tor.ZERO_TIME_MS)
        assert sim.passed and sim.verdicts == (), name


def test_only_the_operator_signs(built):
    """Every input is at the operator address. The records step uses the
    native script, so pycardano names the operator as required signer; the
    other steps name none."""
    for name, b in built["by_name"].items():
        body = _tx(b).transaction_body
        assert all(u.output.address == OPERATOR_ADDR
                   for u in built["pool"] if u.input in body.inputs), name
        assert {bytes(s.payload) for s in body.required_signers or []} <= {OPERATOR}, name


def test_each_step_spends_the_change_of_the_step_before(built):
    by_name = built["by_name"]
    for before, after in zip(STEPS, STEPS[1:]):
        inputs = _tx(by_name[after]).transaction_body.inputs
        assert any(bytes(i.transaction_id).hex() == by_name[before].tx_id for i in inputs), after


# ── Reference scripts ────────────────────────────────────────────────────────


@pytest.mark.parametrize("validator", ["claim", "challenge", "jury_pool", "identity_party"])
def test_publish_puts_the_script_at_the_reference_address_with_its_min_utxo(plan, built, validator):
    b = built["by_name"][f"publish-{validator}"]
    tx = _tx(b)
    out = tx.transaction_body.outputs[0]
    assert str(out.address) == REFERENCE_ADDRESS
    assert vd.script_hash_of(out.script).hex() == plan["manifest"]["hashes"][validator]
    assert bytes(out.script) == plan["scripts"][validator]
    assert out.datum is None and not out.amount.multi_asset
    raw = vd.raw_outputs(b.cbor)[0]
    assert out.amount.coin == (160 + len(raw)) * COINS_PER_UTXO_BYTE
    assert [o.index for o in b.outputs] == [0]
    assert tx.transaction_body.mint is None and tx.transaction_body.certificates is None


# ── Stake registration of Script(M1) ─────────────────────────────────────────


def test_stake_registration_is_certificate_0_without_witness(plan, built):
    """Certificate 0 (stake_registration, no deposit field) registers
    Script(M1). The Conway ledger needs no witness for it, so no script runs:
    the transaction has no redeemer and no script. The deposit is the
    stakeCredentialDeposit of the protocol parameters (5 AP3X on mainnet)."""
    b = built["by_name"]["register-identity_party"]
    m1 = bytes.fromhex(plan["manifest"]["hashes"]["identity_party"])
    entries = vd._body_entries(b.cbor)
    assert entries[4] == cbor2.dumps([[0, [1, m1]]])
    assert 5 not in entries  # no withdrawal
    assert _witness_keys(b) == set()  # no script, no redeemer before the signature
    body = _tx(b).transaction_body
    spent = sum(u.output.amount.coin for u in built["pool"] if u.input in body.inputs)
    assert spent - sum(o.amount.coin for o in body.outputs) - body.fee == 5_000_000 == b.deposit
    assert vd.stake_address(m1, script=True) == deploy.script_addresses(m1.hex())["reward"]


@pytest.mark.parametrize("certificate", ["deregistration", "delegation"])
def test_identity_party_fails_a_publish_purpose(plan, built, certificate):
    """A certificate that needs the witness of Script(M1) runs identity_party
    with a publish purpose, and identity_party fails it: Script(M1) can never
    be deregistered (its deposit stays) or delegated. The ledger runs the same
    purpose for a registration with the deposit field (certificate 7), so only
    certificate 0 can register Script(M1). (aiken's phase-one check does not
    ask a witness for certificate 7, so this test uses certificates 1 and 2.)"""
    run = built["run"]
    m1 = bytes.fromhex(plan["manifest"]["hashes"]["identity_party"])
    credential = StakeCredential(ScriptHash(m1))
    builder = run.builder()
    funding = _funding(tag=b"\x1f")
    builder.add_input(funding)
    if certificate == "deregistration":
        builder.certificates = [StakeDeregistration(credential)]
    else:
        builder.certificates = [StakeDelegation(credential, PoolKeyHash(b"\x70" * 28))]
    builder.add_certificate_script(
        plan["applied"]["identity_party"].script,
        Redeemer(PlutusData(), ExecutionUnits(1_000_000, 500_000_000)),
    )
    builder.collaterals = [funding]
    builder.ttl = run.chain.last_block_slot + 600
    body = builder.build(change_address=OPERATOR_ADDR)
    tx = Transaction(body, builder.build_witness_set())
    sim = simulate(tx, [funding], zero_time_ms=tor.ZERO_TIME_MS)
    assert not sim.passed and sim.failing == ["Publish[0]"]


# ── Records: CrossValidatorRefs token, CrossValidatorRefs and ProtocolParams ──


def test_records_mint_the_refs_token_with_the_operator_native_script(plan, built):
    b = built["by_name"]["records"]
    tx = _tx(b)
    policy = bytes.fromhex(plan["manifest"]["module1"]["refs_token_policy"])
    # ScriptPubkey(operator): CBOR [0, h'operator'], policy = blake2b-224(0x00 || script).
    native = cbor2.dumps([0, OPERATOR])
    assert hashlib.blake2b(b"\x00" + native, digest_size=28).digest() == policy
    assert vd.native_policy_id(vd.operator_native_script(OPERATOR)) == policy
    witness = cbor2.loads(vd._tx_items(b.cbor)[1])
    assert set(witness) == {1}  # native scripts only: no Plutus script, no redeemer
    assert [cbor2.dumps(s) for s in witness[1]] == [native]
    assert {bytes(p.payload): {bytes(n.payload): q for n, q in a.items()}
            for p, a in tx.transaction_body.mint.items()} == {policy: {b"auditing_refs": 1}}


def test_records_hold_the_datums_that_the_validators_read(plan, built):
    b = built["by_name"]["records"]
    body = _tx(b).transaction_body
    hashes = plan["manifest"]["hashes"]
    refs, params = body.outputs[0], body.outputs[1]
    assert refs.address == plan["holder"] == params.address
    assert vd.datum_cbor(UTxO(None, refs)) == deploy.encode(
        deploy.cross_refs(hashes["claim"], hashes["challenge"], hashes["jury_pool"]))
    assert vd.datum_cbor(UTxO(None, params)) == deploy.encode(
        deploy.protocol_params(plan["manifest"]["protocol_params"]))
    policy = bytes.fromhex(plan["manifest"]["module1"]["refs_token_policy"])
    assert vd.has_token(UTxO(None, refs), policy, b"auditing_refs")
    assert not params.amount.multi_asset
    for index, raw in enumerate(vd.raw_outputs(b.cbor)[:2]):
        assert body.outputs[index].amount.coin == (160 + len(raw)) * COINS_PER_UTXO_BYTE
    # The holder is holder(11, operator): only the operator can spend (retire) the records.
    assert plan["manifest"]["module1"]["records_holder"]["hash"] == apply_holder(11, OPERATOR.hex())["hash"]


def _resolution(monkeypatch, plan, refs: UTxO, params: UTxO):
    """build_oracle_resolve with the records ``refs`` and ``params`` and the
    applied validators of the plan; returns (tx, pool)."""
    applied = plan["applied"]
    scripts = {name: tor._script_utxo(f"deploy-sim:{name}".encode(), applied[name])
               for name in ("claim", "challenge", "identity_party")}
    manifest = dict(plan["manifest"])
    manifest.update({
        "claim_ref": tor._ref(scripts["claim"]), "challenge_ref": tor._ref(scripts["challenge"]),
        "identity_party_ref": tor._ref(scripts["identity_party"]),
        "cross_refs_utxo": tor._ref(refs), "params_utxo": tor._ref(params),
    })
    dep = txb.DeploymentState(manifest)
    dep._claim_ref_utxo = scripts["claim"]
    dep._challenge_ref_utxo = scripts["challenge"]
    dep._cross_refs_resolved = refs
    dep._params_resolved = params
    dep._identity_party_ref_utxo = scripts["identity_party"]
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    m1 = bytes.fromhex(manifest["hashes"]["identity_party"])
    ctx = FakeOgmiosContext()
    config_record = config_record_utxo(m1)
    ctx.register_utxos(CONFIG_HOLDER_ADDR, [config_record])
    challenge = tor._challenge_utxo(bytes.fromhex(manifest["hashes"]["challenge"]))
    claim = tor._claim_utxo(bytes.fromhex(manifest["hashes"]["claim"]))
    wallet = tor._wallet_utxo(tor.FUNDER[2], b"deploy-sim:funder")
    pool = [refs, params, config_record, challenge, claim, wallet, *scripts.values()]
    by_ref = {(bytes(u.input.transaction_id).hex(), u.input.index): u for u in pool}
    captured: list = []
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: by_ref[(txid, int(idx))])
    monkeypatch.setattr(txb, "ensure_collateral", lambda *a, **k: None)
    monkeypatch.setattr(txb, "get_wallet_utxos_no_collateral", lambda c, addr: [wallet])
    monkeypatch.setattr(txb, "evaluate_and_rebuild", lambda *a, **k: (b"", {}))
    monkeypatch.setattr(txb, "tx_to_bytes", lambda tx: captured.append(tx) or b"")
    monkeypatch.setattr(txb, "submit_tx", lambda tx_bytes: bytes(captured[-1].id).hex())
    txb.build_oracle_resolve(
        ctx, dep, *tor.FUNDER, tor._ref(challenge), tor._ref(claim),
        verdict=ip.AUDITOR_WINS, reasoning_hash=tor.REASONING_HASH, oracle_skey=OPERATOR_SKEY,
    )
    return captured[-1], pool


def _record_utxos(built):
    b = built["by_name"]["records"]
    body = _tx(b).transaction_body
    tx_id = TransactionId(bytes.fromhex(b.tx_id))
    return (UTxO(TransactionInput(tx_id, 0), body.outputs[0]),
            UTxO(TransactionInput(tx_id, 1), body.outputs[1]))


def test_the_validators_accept_the_records_of_the_deploy(monkeypatch, plan, built):
    """An oracle resolution reads the CrossValidatorRefs record of the deploy
    (the challenge spend authenticates it by the refs token) and the
    ProtocolParams record: the real validators accept the transaction."""
    refs, params = _record_utxos(built)
    tx, pool = _resolution(monkeypatch, plan, refs, params)
    sim = simulate(tx, resolved_inputs(tx, pool), zero_time_ms=tor.ZERO_TIME_MS)
    assert sim.passed, sim.describe() + "\n" + sim.report


def test_the_validators_refuse_a_refs_output_without_the_token(monkeypatch, plan, built):
    refs, params = _record_utxos(built)
    plain = UTxO(refs.input, TransactionOutput(refs.output.address, Value(refs.output.amount.coin),
                                               datum=refs.output.datum))
    tx, pool = _resolution(monkeypatch, plan, plain, params)
    sim = simulate(tx, resolved_inputs(tx, pool), zero_time_ms=tor.ZERO_TIME_MS)
    assert not sim.passed


# ── Runs: resume, protection, dry run, offline signing ───────────────────────


def test_a_run_skips_the_steps_that_the_chain_shows(plan, built):
    """The chain shows the claim reference script, the registration (Koios)
    and the records: only the other steps are built."""
    by_name = built["by_name"]
    claim_tx = _tx(by_name["publish-claim"])
    records_tx = _tx(by_name["records"])
    shown = [
        UTxO(TransactionInput(TransactionId(bytes.fromhex(by_name["publish-claim"].tx_id)), 0),
             claim_tx.transaction_body.outputs[0]),
        *_record_utxos(built),
    ]
    run, steps, ogmios, koios = _run(plan, extra=shown)
    m1 = bytes.fromhex(plan["manifest"]["hashes"]["identity_party"])
    koios.registered_addresses.add(vd.stake_address(m1, script=True))
    result, found = vd.plan_steps(run, steps, None)
    assert [b.step.name for b in result] == ["publish-challenge", "publish-jury_pool", "publish-identity_party"]
    found = {step.name: values for step, values in found}
    assert found["publish-claim"] == {"tx": by_name["publish-claim"].tx_id,
                                      "ref": f"{by_name['publish-claim'].tx_id}#0"}
    assert found["register-identity_party"] == {"tx": None, "registered": True}
    assert found["records"] == {"tx": by_name["records"].tx_id,
                                "cross_refs": f"{by_name['records'].tx_id}#0",
                                "params": f"{by_name['records'].tx_id}#1"}
    assert records_tx.transaction_body.outputs[0].address == plan["holder"]


def test_the_runner_never_spends_the_config_seed_or_the_seal_seed_address(plan):
    seed = UTxO(vd.parse_ref(CONFIG_SEED), TransactionOutput(OPERATOR_ADDR, Value(900_000_000)))
    run, steps, ogmios, _ = _run(plan, extra=[seed])
    assert seed not in run.wallet_utxos()
    result, _ = vd.plan_steps(run, steps, None)
    for b in result:
        assert vd.parse_ref(CONFIG_SEED) not in _tx(b).transaction_body.inputs
    chain, _, _ = fake_chain()
    with pytest.raises(vd.DeployError, match="protected"):
        vd.Run(chain, OPERATOR, protected_addresses={str(OPERATOR_ADDR)})


def test_a_dry_run_without_funds_uses_virtual_outputs(plan):
    """The operator address has no funds: --dry-run-funds adds a virtual
    UTxO. Each later step spends the virtual change of the step before, and
    the evaluation gets these outputs as additional UTxOs."""
    run, steps, ogmios, _ = _run(plan, funds=0, dry_run_funds=400_000_000)
    result, _ = vd.plan_steps(run, steps, None)
    assert [b.step.name for b in result] == STEPS
    assert all(b.virtual_inputs for b in result)
    assert all(additional for _cbor, additional in ogmios.evaluated)
    _, steps2, _, _ = _run(plan, funds=0)
    with pytest.raises(vd.DeployError, match="Fund the address"):
        vd.plan_steps(vd.Run(fake_chain()[0], OPERATOR, log=lambda _m: None), steps2[:1], None)


def test_the_witness_keeps_the_body_and_signs_its_hash(built):
    b = built["by_name"]["records"]
    signed = vd.add_vkey_witness(b.cbor, OPERATOR_SKEY)
    assert vd.transaction_id(signed) == b.tx_id
    assert vd._tx_items(signed)[0] == vd._tx_items(b.cbor)[0]
    (witness,) = Transaction.from_cbor(signed).transaction_witness_set.vkey_witnesses
    body_hash = hashlib.blake2b(vd._tx_items(b.cbor)[0], digest_size=32).digest()
    assert VerifyKey(bytes(witness.vkey.payload)).verify(body_hash, bytes(witness.signature)) == body_hash
    assert vd.vkey_witness_hashes(signed) == {OPERATOR}
    assert vd.add_vkey_witness(signed, OPERATOR_SKEY) == signed
    assert len(signed) == b.signed_size


def _args(action: str, **kw) -> argparse.Namespace:
    values = {"action": action, "submit": False, "report": None, "dry_run_funds": 0.0,
              "skey": None, "vkey": Path("operator.vkey")}
    values.update(kw)
    return argparse.Namespace(**values)


def test_build_sign_submit_records_the_reference_utxos(tmp_path, plan):
    """The offline flow: build writes unsigned files, sign adds the operator
    witness without a network, submit sends them in order and records each
    step; the manifest gets the keys that the Module-1 clients read."""
    state = vd.DeployState(tmp_path / "manifest.json", json.loads(json.dumps(plan["manifest"])))
    state.save()
    run, steps, ogmios, koios = _run(plan)
    vd.execute(_args("build"), run, steps, state, tx_dir=tmp_path / "tx", header={},
               after=lambda: deploy.update_manifest(state))
    assert list(state.pending) == STEPS
    files = sorted((tmp_path / "tx").glob("*.unsigned.json"))
    assert [f.name for f in files] == [f"{i:02d}-{name}.unsigned.json" for i, name in enumerate(STEPS, 1)]
    assert json.loads(files[0].read_text())["type"] == "Unwitnessed Tx ConwayEra"

    vd.sign_pending(state, OPERATOR_SKEY, OPERATOR, lambda _m: None)
    with pytest.raises(vd.DeployError, match="not the operator key"):
        vd.sign_pending(state, PaymentSigningKey(b"\x07" * 32), OPERATOR, lambda _m: None)

    submit = FakeSubmit(ogmios)
    run2, steps2, _, _ = (run, deploy.module1_steps(vd, run, state.data, plan["scripts"]), None, None)
    run2.chain.virtual.clear()
    run2.chain.spent.clear()
    vd.execute(_args("submit"), run2, steps2, state, tx_dir=tmp_path / "tx", header={},
               after=lambda: deploy.update_manifest(state), post=submit, sleep=lambda _s: None)
    assert len(submit.sent) == len(STEPS)
    assert all(OPERATOR in vd.vkey_witness_hashes(tx) for tx in submit.sent)
    assert list(state.steps) == STEPS and state.pending == {}
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    for name, key in (("claim", "claim_ref"), ("challenge", "challenge_ref"),
                      ("jury_pool", "jury_pool_ref"), ("identity_party", "identity_party_ref")):
        assert manifest[key] == f"{state.steps[f'publish-{name}']['tx']}#0"
    assert manifest["cross_refs_utxo"] == f"{state.steps['records']['tx']}#0"
    assert manifest["params_utxo"] == f"{state.steps['records']['tx']}#1"
    assert manifest["identity_party_registration"]["deposit"] == 5_000_000
    # The Module-1 clients load the manifest.
    ident = load_identity_deployment(manifest)
    assert ident.identity_party_hash.hex() == manifest["hashes"]["identity_party"]
    assert ident.identity_party_ref == manifest["identity_party_ref"]
    assert txb.DeploymentState(manifest).claim_hash == manifest["hashes"]["claim"]

    # A new run finds every step on chain and sends nothing.
    m1 = bytes.fromhex(manifest["hashes"]["identity_party"])
    koios.registered_addresses.add(vd.stake_address(m1, script=True))
    result, found = vd.plan_steps(run2, steps2, state)
    assert result == [] and [s.name for s, _ in found] == STEPS


def test_a_state_of_an_old_deployment_is_refused(tmp_path):
    old = Path(__file__).resolve().parents[3] / "Module-3" / "deploy" / "mainnet" / "deploy_state.json"
    with pytest.raises(vd.DeployError, match="not a state file of this deploy tool"):
        vd.DeployState.load(old, module="module3", network="mainnet")
    other = tmp_path / "state.json"
    other.write_text(json.dumps({"format": vd.STATE_FORMAT, "module": "module6", "network": "mainnet"}))
    with pytest.raises(vd.DeployError, match="belongs to module6"):
        vd.DeployState.load(other, module="module1", network="mainnet")


def test_the_options_of_a_send(tmp_path):
    vkey = tmp_path / "operator.vkey"
    vkey.write_text(json.dumps({"type": "PaymentVerificationKeyShelley_ed25519", "description": "",
                                "cborHex": "5820" + bytes(OPERATOR_VKEY.payload).hex()}))
    assert vd.operator_key_hash_from_args(_args("run", vkey=vkey)) == OPERATOR
    with pytest.raises(vd.DeployError, match="signing key"):
        vd.operator_key_hash_from_args(_args("run", vkey=vkey, submit=True))
    with pytest.raises(vd.DeployError, match="dry run"):
        vd.operator_key_hash_from_args(_args("build", vkey=vkey, dry_run_funds=5.0))
    with pytest.raises(vd.DeployError, match="signing key"):
        vd.operator_key_hash_from_args(_args("sign", vkey=vkey))


def test_signing_key_files(tmp_path):
    skey = PaymentSigningKey(hashlib.blake2b(b"deploy-tests:key", digest_size=32).digest())
    envelope = tmp_path / "op.skey"
    envelope.write_text(json.dumps({"type": "PaymentSigningKeyShelley_ed25519", "description": "",
                                    "cborHex": "5820" + bytes(skey.payload).hex()}))
    hex_file = tmp_path / "op.hex"
    hex_file.write_text(bytes(skey.payload).hex() + "\n")
    for path in (envelope, hex_file):
        assert vd.load_signing_key(path).payload == skey.payload
        assert vd.load_verification_key(path).hash() == PaymentVerificationKey.from_signing_key(skey).hash()
    bad = tmp_path / "bad.skey"
    bad.write_text(json.dumps({"type": "StakeSigningKeyShelley_ed25519", "cborHex": "5820" + "00" * 32}))
    with pytest.raises(vd.DeployError, match="PaymentSigningKeyShelley_ed25519"):
        vd.load_signing_key(bad)


def test_the_seal_on_chain_names_m1(plan):
    chain, ogmios, _ = fake_chain()
    m1 = bytes.fromhex(plan["manifest"]["hashes"]["identity_party"])
    record = config_record_utxo(m1)
    ogmios.add(record)
    deployment = dict(identity_deployment(), seal={
        "tx": "ee" * 32, "module1_hash": m1.hex(), "config_record": tor._ref(record)})
    datum = deploy.chain_seal_datum(vd, chain, deployment)
    assert deploy.seal_matches(datum, m1.hex())
    assert not deploy.seal_matches(datum, "e2" * 28)
    moved = UTxO(record.input, TransactionOutput(OPERATOR_ADDR, record.output.amount, datum=record.output.datum))
    ogmios.add(moved)
    with pytest.raises(deploy.PlanError, match="config_holder"):
        deploy.chain_seal_datum(vd, chain, deployment)
    with pytest.raises(deploy.PlanError, match="no seal"):
        deploy.chain_seal_datum(vd, chain, identity_deployment())


def test_protocol_parameters_come_from_ogmios():
    chain, ogmios, _ = fake_chain()
    pp = chain.protocol_param
    assert (pp.key_deposit, pp.coins_per_utxo_byte, pp.max_tx_size) == (5_000_000, 4310, 16_384)
    assert len(pp.cost_models["PlutusV3"]) == len(ogmios.protocol["plutusCostModels"]["plutus:v3"])
    assert list(pp.cost_models["PlutusV3"].values()) == ogmios.protocol["plutusCostModels"]["plutus:v3"]
    assert pp.min_fee_reference_scripts == {"base": 15.0, "range": 25600, "multiplier": 1.2}
    broken = dict(ogmios.protocol)
    del broken["stakeCredentialDeposit"]
    with pytest.raises(vd.DeployError, match="stakeCredentialDeposit"):
        vd.protocol_parameters(broken)
    ogmios.network = "testnet"
    with pytest.raises(vd.DeployError, match="started at"):
        chain.check_network()
