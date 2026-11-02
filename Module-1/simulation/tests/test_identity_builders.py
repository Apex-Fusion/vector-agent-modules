"""Offline tests of the identity registry clients of the Module-1 simulation
(Part 2 §3.4, §3.8, §7): identity Register (with and without Module-1
consent), Consent Update, the identity checks of SubmitClaim, OpenChallenge
and RegisterJuror, the verdict mint of ResolveJury, ApplyVerdict (suspend
and revoke), the verdict datum and token name, the deployment values and
the client-side refusals.

The tests build transactions against a fake chain context and inspect the
TransactionBuilder; nothing is signed with real keys or submitted.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
from pathlib import Path

import cbor2
import pytest
from pycardano import (
    Address, Asset, AssetName, MultiAsset, Network, PaymentSigningKey,
    PaymentVerificationKey, RawCBOR, ScriptHash, TransactionBuilder,
    TransactionId, TransactionInput, TransactionOutput, UTxO, Value,
)
from pycardano.utils import min_lovelace_post_alonzo

import simulation.tx_builder as txb
from simulation import identity_party as ip
from simulation import vector_identity as vi
from simulation.chain import reward_balance_from_summaries, slot_to_posix_ms
from simulation.config import (
    IDENTITY_DEPLOYMENT_ORDER, IDENTITY_MANIFEST_KEYS, IdentityConfigError,
    load_identity_deployment,
)
from simulation.tests.conftest import (
    CANNED_SLOT, CONFIG_HOLDER_ADDR, IDENTITY_ADDR, IDENTITY_HASH,
    IDENTITY_PARTY_HASH, V13_DEPLOYMENT, config_record_utxo,
    identity_record_utxo, sample_claim_token,
)

LO = slot_to_posix_ms(CANNED_SLOT - txb.VALIDITY_LAG_SLOTS)
HI = slot_to_posix_ms(CANNED_SLOT - txb.VALIDITY_LAG_SLOTS + txb.IDENTITY_WINDOW_SLOTS)
M1 = IDENTITY_PARTY_HASH


def _key(tag: bytes):
    skey = PaymentSigningKey(hashlib.blake2b(tag, digest_size=32).digest())
    vkey = PaymentVerificationKey.from_signing_key(skey)
    return skey, vkey, Address(payment_part=vkey.hash(), network=Network.MAINNET)


def _wallet_utxo(addr, tag: bytes, coin: int = 500_000_000) -> UTxO:
    txid = TransactionId(hashlib.blake2b(tag, digest_size=32).digest())
    return UTxO(TransactionInput(txid, 0), TransactionOutput(addr, Value(coin)))


def _data(redeemer) -> object:
    return vi.data_from(redeemer.data)


def _ref_ids(builder) -> set:
    return {
        (bytes(getattr(r, "input", r).transaction_id), getattr(r, "input", r).index)
        for r in builder.reference_inputs
    }


def _uid(utxo) -> tuple:
    return (bytes(utxo.input.transaction_id), utxo.input.index)


@pytest.fixture
def env(monkeypatch, mock_ogmios_context, sample_deployment_with_all_refs):
    """Patch the network helpers of tx_builder. ``env["signed"]`` records
    the signing keys of each build_and_sign call, ``env["builders"]`` the
    builders."""
    rec = {"builders": [], "signed": [], "ctx": mock_ogmios_context,
           "dep": sample_deployment_with_all_refs, "wallets": {}}

    def fake_build_and_sign(self, signing_keys, change_address=None, **kw):
        rec["builders"].append(self)
        rec["signed"].append(list(signing_keys))

        class _Tx:
            def to_cbor(self_inner):
                return b"\x00" * 64
        return _Tx()

    monkeypatch.setattr(TransactionBuilder, "build_and_sign", fake_build_and_sign)
    monkeypatch.setattr(txb, "ensure_collateral", lambda *a, **k: None)
    monkeypatch.setattr(txb, "evaluate_and_rebuild", lambda *a, **k: (b"", {}))
    monkeypatch.setattr(txb, "submit_tx", lambda tx_bytes: "cd" * 32)
    monkeypatch.setattr(txb, "tx_to_bytes", lambda tx: b"\x00")
    monkeypatch.setattr(txb, "wait_confirm", lambda *a, **k: None)
    monkeypatch.setattr(
        txb, "get_wallet_utxos_no_collateral",
        lambda ctx, addr: rec["wallets"].setdefault(
            str(addr), [_wallet_utxo(addr, str(addr).encode())]
        ),
    )
    return rec


# ── Deployment values ──────────────────────────────────────────────────────


def test_identity_deployment_values_are_read_from_the_manifest():
    ident = load_identity_deployment(V13_DEPLOYMENT)
    assert ident.identity_hash == IDENTITY_HASH
    assert ident.identity_party_hash == M1
    assert ident.verdict_policy.auditor_wins == ip.VerdictRule(ip.VerdictAction.SUSPEND, 604_800_000)
    assert ident.verdict_policy.claimer_wins is None
    dep = txb.DeploymentState(V13_DEPLOYMENT)
    assert dep.identity_addr == IDENTITY_ADDR
    assert dep.config_holder_addr == CONFIG_HOLDER_ADDR
    assert dep.identity_party_addr == Address(ScriptHash(M1), network=Network.MAINNET)
    assert bytes(dep.identity_party_reward_addr.to_primitive()) == b"\xf1" + M1


@pytest.mark.parametrize("path", IDENTITY_MANIFEST_KEYS)
def test_missing_identity_value_names_the_deployment_order(path):
    manifest = __import__("copy").deepcopy(V13_DEPLOYMENT)
    *parents, last = path.split(".")
    node = manifest
    for part in parents:
        node = node[part]
    del node[last]
    with pytest.raises(IdentityConfigError) as exc:
        load_identity_deployment(manifest)
    assert path in str(exc.value)
    assert IDENTITY_DEPLOYMENT_ORDER in str(exc.value)
    with pytest.raises(IdentityConfigError):
        txb.DeploymentState(manifest).identity


def test_placeholder_hash_is_refused():
    manifest = __import__("copy").deepcopy(V13_DEPLOYMENT)
    manifest["identity"]["identity_hash"] = "00" * 28
    with pytest.raises(IdentityConfigError, match="placeholder"):
        load_identity_deployment(manifest)


def test_auditing_config_matches_the_deploy_script_encoding():
    spec = importlib.util.spec_from_file_location(
        "identity_deploy",
        Path(__file__).resolve().parents[2] / "scripts" / "identity_deploy.py",
    )
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    config = {
        "identity": dict(V13_DEPLOYMENT["identity"]),
        "module1": dict(V13_DEPLOYMENT["module1"]),
        "verdict_policy": dict(V13_DEPLOYMENT["verdict_policy"]),
    }
    expected = deploy.encode(deploy.auditing_config(config))
    dep = txb.DeploymentState(V13_DEPLOYMENT)
    assert dep.auditing_config.cbor() == expected


def test_reward_balance_parser_reads_both_ogmios_formats():
    h = bytes(range(28))
    as_list = [{"from": "script", "credential": h.hex(),
                "rewards": {"ada": {"lovelace": 1_234}}, "deposit": {}}]
    as_map = {h.hex(): {"rewards": {"ada": {"lovelace": 5_678}}}}
    assert reward_balance_from_summaries(as_list, h) == 1_234
    assert reward_balance_from_summaries(as_map, h) == 5_678
    with pytest.raises(RuntimeError, match="not registered"):
        reward_balance_from_summaries([], h)


# ── Verdict datum ──────────────────────────────────────────────────────────


def test_verdict_token_name_is_blake2b_256_of_the_serialised_verdict():
    v = ip.IdentityVerdict(b"\xa0" * 32, ip.VerdictAction.SUSPEND, 604_800_000, b"\x78" * 32, 1)
    data = cbor2.CBORTag(121, [
        b"\xa0" * 32, cbor2.CBORTag(121, []), 604_800_000,
        cbor2.CBORTag(121, [b"\x78" * 32, 1]),
    ])
    assert v.token_name() == hashlib.blake2b(vi.serialise_data(data), digest_size=32).digest()
    assert v.token_name() == hashlib.blake2b(v.datum_cbor(), digest_size=32).digest()
    assert ip.decode_verdict(v.datum_cbor()) == v
    assert ip.verdict_from_output(RawCBOR(v.datum_cbor()), v.token_name()) == v
    with pytest.raises(vi.IdentityError, match="token name"):
        ip.verdict_from_output(RawCBOR(v.datum_cbor()), b"\x00" * 32)


def test_policy_gives_the_losing_side_or_no_verdict():
    policy = ip.VerdictPolicy(
        auditor_wins=ip.VerdictRule(ip.VerdictAction.REVOKE, 0),
        claimer_wins=ip.VerdictRule(ip.VerdictAction.SUSPEND, vi.DAY_MS),
    )
    kw = dict(claimer_did=b"\x01" * 32, auditor_did=b"\x02" * 32,
              challenge_tx_id=b"\x03" * 32, challenge_index=0)
    assert ip.resolution_verdict(policy, ip.AUDITOR_WINS, **kw).agent == b"\x01" * 32
    assert ip.resolution_verdict(policy, ip.CLAIMER_WINS, **kw).agent == b"\x02" * 32
    assert ip.resolution_verdict(policy, ip.INCONCLUSIVE, **kw) is None
    empty = ip.VerdictPolicy(None, None)
    assert ip.resolution_verdict(empty, ip.AUDITOR_WINS, **kw) is None
    with pytest.raises(ValueError):
        ip.VerdictRule(ip.VerdictAction.SUSPEND, vi.DAYS_30_MS + 1)
    with pytest.raises(ValueError):
        ip.VerdictRule(ip.VerdictAction.REVOKE, 1)


# ── Register ───────────────────────────────────────────────────────────────


def test_register_without_consent(env):
    payer_skey, payer_vkey, payer_addr = _key(b"payer")
    ctrl_skey, ctrl_vkey, _ = _key(b"controller")
    tx, did_hex = txb.build_register_identity(
        env["ctx"], env["dep"], payer_skey, payer_vkey, payer_addr, ctrl_skey,
        name=b"agent", description=b"vector agent worker", framework=b"Vector-Agent",
    )
    b = env["builders"][-1]
    seed = sorted(env["wallets"][str(payer_addr)],
                  key=lambda u: (bytes(u.input.transaction_id).hex(), u.input.index))[0]
    agent_id = vi.identifier_from_seed(bytes(seed.input.transaction_id), seed.input.index)
    assert did_hex == agent_id.hex()
    assert dict(b.mint[ScriptHash(IDENTITY_HASH)]) == {AssetName(agent_id): 1}
    (_, mint_red), = b._minting_script_to_redeemers
    assert _data(mint_red) == vi.register_redeemer(bytes(seed.input.transaction_id), seed.input.index)
    (out,) = b.outputs
    assert out.address == IDENTITY_ADDR and out.script is None
    d = vi.decode_identity_datum(out.datum)
    assert d.controller == bytes(ctrl_vkey.payload)
    assert d.status == vi.Status.active() and d.subagent_epoch == 0
    assert d.pending_recovery is None and d.lineage.is_root
    assert d.created_at == HI
    assert d.parties == (vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, LO, None, 3, None),)
    assert d.framework == b"Vector-Agent" and d.description == b"vector agent worker"
    assert out.amount.coin == max(txb.RECORD_MIN_LOVELACE, min_lovelace_post_alonzo(out, env["ctx"]))
    assert b.withdrawals is None
    assert list(b.required_signers) == [ctrl_vkey.hash()]
    assert b.ttl - b.validity_start == txb.IDENTITY_WINDOW_SLOTS
    signers = {bytes(PaymentVerificationKey.from_signing_key(k).hash()) for k in env["signed"][-1]}
    assert signers == {bytes(payer_vkey.hash()), bytes(ctrl_vkey.hash())}


def test_register_with_consent_and_listed_key(env):
    payer_skey, payer_vkey, payer_addr = _key(b"payer")
    ctrl_skey, ctrl_vkey, _ = _key(b"controller")
    env["ctx"].reward_balances[M1] = 3_500_000
    txb.build_register_identity(
        env["ctx"], env["dep"], payer_skey, payer_vkey, payer_addr, ctrl_skey,
        name=b"juror", transaction_keys=(payer_skey,),
        module1_role=vi.Role.ACCOUNTABLE,
    )
    b = env["builders"][-1]
    d = vi.decode_identity_datum(b.outputs[0].datum)
    assert d.keys == (vi.Key(vi.KeyType.ED25519, bytes(payer_vkey.payload), vi.PURPOSE_TRANSACTION),)
    assert d.parties[-1] == vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.script(M1), None, LO, None, 3, None)
    assert _uid(config_record_utxo()) in _ref_ids(b)
    reward = bytes(env["dep"].identity_party_reward_addr.to_primitive())
    assert dict(b.withdrawals) == {reward: 3_500_000}
    (_, w_red), = b._withdrawal_script_to_redeemers
    assert _data(w_red) == ip.consent_redeemer()
    assert set(b.required_signers) == {ctrl_vkey.hash(), payer_vkey.hash()}


def test_register_with_consent_refuses_a_missing_config_record(env):
    env["ctx"].register_utxos(CONFIG_HOLDER_ADDR, [])
    payer_skey, payer_vkey, payer_addr = _key(b"payer")
    with pytest.raises(vi.IdentityError, match="configuration record"):
        txb.build_register_identity(
            env["ctx"], env["dep"], payer_skey, payer_vkey, payer_addr, _key(b"c")[0],
            name=b"x", module1_role=vi.Role.RESPONSIBLE,
        )


def test_config_record_naming_another_hash_is_refused(env):
    env["ctx"].register_utxos(CONFIG_HOLDER_ADDR, [config_record_utxo(b"\xe2" * 28)])
    with pytest.raises(vi.IdentityError, match="names"):
        txb.find_config_record(env["ctx"], env["dep"])


def test_large_record_output_is_raised_above_ten_ap3x(env):
    record = vi.decode_identity_datum(identity_record_utxo(
        b"big", "aa" * 32, _key(b"c")[1]).output.datum)
    big = dataclasses.replace(
        record, description=b"d" * 256, endpoint=b"e" * 256, name=b"n" * 64,
        capabilities=tuple(bytes([i]) * 64 for i in range(16)),
    )
    ctx = env["ctx"]
    ctx._pp = dataclasses.replace(ctx._pp, coins_per_utxo_byte=10_000)
    out = txb.record_output(ctx, env["dep"], b"\xaa" * 32, big)
    assert out.amount.coin > txb.RECORD_MIN_LOVELACE
    assert out.amount.coin == min_lovelace_post_alonzo(out, ctx)


# ── Consent Update ─────────────────────────────────────────────────────────


def test_consent_update(env):
    payer_skey, payer_vkey, payer_addr = _key(b"payer")
    ctrl_skey, ctrl_vkey, _ = _key(b"controller")
    record = identity_record_utxo(b"rec", "bb" * 32, ctrl_vkey)
    env["ctx"].register_utxos(IDENTITY_ADDR, [record])
    env["ctx"].reward_balances[M1] = 7
    txb.build_consent_update(
        env["ctx"], env["dep"], payer_skey, payer_vkey, payer_addr, ctrl_skey, "bb" * 32,
    )
    b = env["builders"][-1]
    assert record in b.inputs
    assert _data(b._inputs_to_redeemers[record]) == vi.update_redeemer()
    old = vi.decode_identity_datum(record.output.datum)
    new = vi.decode_identity_datum(b.outputs[0].datum)
    assert new == dataclasses.replace(old, parties=old.parties + (
        vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.script(M1), None, LO, None, 3, None),))
    assert b.outputs[0].amount.coin >= record.output.amount.coin
    assert dict(b.outputs[0].amount.multi_asset) == dict(record.output.amount.multi_asset)
    assert _uid(config_record_utxo()) in _ref_ids(b)
    assert list(dict(b.withdrawals).values()) == [7]
    assert list(b.required_signers) == [ctrl_vkey.hash()]
    assert b.ttl - b.validity_start <= 3600


def test_consent_update_refuses_another_key_and_a_second_consent(env):
    payer = _key(b"payer")
    ctrl_skey, ctrl_vkey, _ = _key(b"controller")
    env["ctx"].register_utxos(IDENTITY_ADDR, [identity_record_utxo(
        b"rec", "bb" * 32, ctrl_vkey, module1_role=vi.Role.ACCOUNTABLE)])
    with pytest.raises(vi.IdentityError, match="controller"):
        txb.build_consent_update(env["ctx"], env["dep"], *payer, _key(b"other")[0], "bb" * 32)
    with pytest.raises(vi.IdentityError, match="already"):
        txb.build_consent_update(env["ctx"], env["dep"], *payer, ctrl_skey, "bb" * 32)


# ── Positions that read the record ─────────────────────────────────────────


def test_submit_claim_reads_the_record_and_declares_the_controller(
    patched_network, captured_builder, mock_ogmios_context, sample_deployment,
    sample_wallet, sample_did_hex, sample_identity_record_utxo,
):
    skey, vkey, addr = sample_wallet
    txb.build_submit_claim(
        mock_ogmios_context, sample_deployment, skey, vkey, addr,
        sample_did_hex, 50_000_000, challenge_window_ms=1_800_000,
        claim_hash=b"\x11" * 32, claim_type=b"data_indexing", storage_uri=b"ipfs://x",
    )
    b = captured_builder[-1]
    assert _uid(sample_identity_record_utxo) in _ref_ids(b)
    fields = cbor2.loads(b.outputs[0].datum.cbor).value
    assert fields[0] == bytes.fromhex(sample_did_hex)
    assert fields[1] == cbor2.CBORTag(121, [bytes(vkey.hash())])
    assert list(b.required_signers) == [vkey.hash()]
    assert b.ttl - b.validity_start <= 3600


def test_submit_claim_refusals(
    patched_network, captured_builder, mock_ogmios_context, sample_deployment,
    sample_wallet, sample_did_hex,
):
    skey, vkey, addr = sample_wallet
    kw = dict(challenge_window_ms=1_800_000, claim_hash=b"\x11" * 32,
              claim_type=b"t", storage_uri=b"u")
    # Undeclared key: another wallet key is neither controller nor listed.
    other_skey, other_vkey, other_addr = _key(b"stranger")
    with pytest.raises(vi.IdentityError, match="neither the controller"):
        txb.build_submit_claim(mock_ogmios_context, sample_deployment, other_skey,
                               other_vkey, other_addr, sample_did_hex, 50_000_000, **kw)
    # Window too long.
    with pytest.raises(vi.IdentityError, match="validity range"):
        txb.build_submit_claim(mock_ogmios_context, sample_deployment, skey, vkey, addr,
                               sample_did_hex, 50_000_000, window_slots=3601, **kw)
    # Not Active: a hold in force.
    hold = vi.Hold(vi.Signer.controller_set(), 0, LO - 1, HI + 1)
    suspended = identity_record_utxo(b"susp", sample_did_hex, vkey,
                                     status=vi.Status.suspended([hold]))
    mock_ogmios_context.register_utxos(IDENTITY_ADDR, [suspended])
    with pytest.raises(vi.IdentityStatusError):
        txb.build_submit_claim(mock_ogmios_context, sample_deployment, skey, vkey, addr,
                               sample_did_hex, 50_000_000, **kw)
    # No record.
    mock_ogmios_context.register_utxos(IDENTITY_ADDR, [])
    with pytest.raises(vi.RecordNotFoundError):
        txb.build_submit_claim(mock_ogmios_context, sample_deployment, skey, vkey, addr,
                               sample_did_hex, 50_000_000, **kw)
    assert not captured_builder


def test_open_challenge_names_the_claim_token_and_reads_the_auditor_record(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref, sample_auditor_wallet,
    sample_auditor_did_hex, sample_claim_utxo, sample_eligible_jurors,
    sample_auditor_identity_record_utxo,
):
    skey, vkey, addr = sample_auditor_wallet
    result = txb.build_open_challenge(
        mock_ogmios_context, sample_deployment_with_challenge_ref, skey, vkey, addr,
        sample_auditor_did_hex,
        f"{bytes(sample_claim_utxo.input.transaction_id).hex()}#0",
        sample_eligible_jurors, stake_amount=50_000_000,
        evidence_hash=b"\x22" * 32, evidence_uri=b"ipfs://e",
        resolution_deadline_ms=5_400_000, window_slots=3600,
    )
    b = captured_builder[-1]
    fields = cbor2.loads(b.outputs[0].datum.cbor).value
    assert len(fields) == 11 and fields[10] == sample_claim_token()
    assert result["claim_token_hex"] == sample_claim_token().hex()
    assert _uid(sample_auditor_identity_record_utxo) in _ref_ids(b)
    # With the longest window the claim deadline caps the upper bound.
    window_end = (slot_to_posix_ms(CANNED_SLOT - 120) + 1_800_000) // 1000 - txb.SYSTEM_START_UNIX
    assert b.ttl == window_end - 1
    assert b.ttl - b.validity_start <= 3600


def test_juror_bond_declares_the_listed_master_key(env):
    master_skey, master_vkey, master_addr = _key(b"master")
    juror_skey, juror_vkey, _ = _key(b"juror")
    listed = vi.Key(vi.KeyType.ED25519, bytes(master_vkey.payload), vi.PURPOSE_TRANSACTION)
    record = identity_record_utxo(b"jrec", "cc" * 32, juror_vkey, keys=(listed,))
    env["ctx"].register_utxos(IDENTITY_ADDR, [record])
    txb.build_juror_bond(env["ctx"], env["dep"], master_skey, master_vkey, master_addr,
                         25_000_000, juror_did_hex="cc" * 32)
    b = env["builders"][-1]
    assert _uid(record) in _ref_ids(b)
    fields = cbor2.loads(b.outputs[0].datum.cbor).value
    assert fields[0] == b"\xcc" * 32
    assert fields[1] == cbor2.CBORTag(121, [bytes(master_vkey.hash())])
    assert list(b.required_signers) == [master_vkey.hash()]
    assert b.ttl - b.validity_start <= 3600
    # Without the listed key the master key is not declared.
    env["ctx"].register_utxos(IDENTITY_ADDR, [identity_record_utxo(b"j2", "cc" * 32, juror_vkey)])
    with pytest.raises(vi.IdentityError, match="neither the controller"):
        txb.build_juror_bond(env["ctx"], env["dep"], master_skey, master_vkey, master_addr,
                             25_000_000, juror_did_hex="cc" * 32)


# ── ResolveJury verdict mint ───────────────────────────────────────────────


def _resolve(monkeypatch, ctx, dep, wallet, challenge, claim, jurors):
    by_txid = {bytes(u.input.transaction_id).hex(): u for u in [challenge, claim, *jurors]}
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: by_txid[txid])
    skey, vkey, addr = wallet
    return txb.build_resolve_jury(
        ctx, dep, skey, vkey, addr,
        f"{bytes(challenge.input.transaction_id).hex()}#{challenge.input.index}",
        f"{bytes(claim.input.transaction_id).hex()}#{claim.input.index}",
        [f"{bytes(u.input.transaction_id).hex()}#{u.input.index}" for u in jurors],
        jury_size=5, jury_fee_rate=1000,
    )


def test_resolve_jury_mints_the_verdict_for_auditor_wins(
    monkeypatch, patched_network_for_resolve_jury, captured_builder,
    mock_ogmios_context, sample_deployment_with_all_refs, sample_auditor_wallet,
    sample_challenge_utxo_voting, sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_auditor_wins, sample_cross_refs_utxo, sample_did_hex,
):
    result = _resolve(monkeypatch, mock_ogmios_context, sample_deployment_with_all_refs,
                      sample_auditor_wallet, sample_challenge_utxo_voting,
                      sample_claim_utxo_challenged, sample_revealed_juror_utxos_auditor_wins)
    b = captured_builder[-1]
    verdict = ip.IdentityVerdict(
        bytes.fromhex(sample_did_hex), ip.VerdictAction.SUSPEND, 604_800_000,
        bytes(sample_challenge_utxo_voting.input.transaction_id),
        sample_challenge_utxo_voting.input.index,
    )
    assert result["verdict_minted"] and result["identity_verdict"] == verdict.to_json()
    assert dict(b.mint[ScriptHash(M1)]) == {AssetName(verdict.token_name()): 1}
    claim_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["claim"]))
    assert dict(b.mint[claim_policy]) == {AssetName(sample_claim_token()): -1}
    reds = [_data(r) for _, r in b._minting_script_to_redeemers]
    assert ip.mint_verdict_redeemer() in reds
    out = b.outputs[-1]
    assert result["verdict_ref"] == f"{result['tx_hash']}#{len(b.outputs) - 1}"
    assert out.address == sample_deployment_with_all_refs.identity_party_addr
    assert dict(out.amount.multi_asset) == {
        ScriptHash(M1): Asset({AssetName(verdict.token_name()): 1})}
    assert ip.decode_verdict(out.datum) == verdict and out.script is None
    assert out.amount.coin == min_lovelace_post_alonzo(out, mock_ogmios_context)
    refs = _ref_ids(b)
    assert _uid(config_record_utxo()) in refs and _uid(sample_cross_refs_utxo) in refs
    # The continuing challenge keeps claim_token.
    assert cbor2.loads(b.outputs[0].datum.cbor).value[10] == sample_claim_token()


def test_resolve_jury_without_a_rule_mints_no_verdict(
    monkeypatch, patched_network_for_resolve_jury, captured_builder,
    mock_ogmios_context, sample_deployment_with_all_refs, sample_auditor_wallet,
    sample_challenge_utxo_voting, sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
):
    mock_ogmios_context.register_utxos(CONFIG_HOLDER_ADDR, [])
    result = _resolve(monkeypatch, mock_ogmios_context, sample_deployment_with_all_refs,
                      sample_auditor_wallet, sample_challenge_utxo_voting,
                      sample_claim_utxo_challenged, sample_revealed_juror_utxos_claimer_wins)
    b = captured_builder[-1]
    assert not result["verdict_minted"] and result["verdict_ref"] is None
    assert ScriptHash(M1) not in b.mint
    assert all(o.address != sample_deployment_with_all_refs.identity_party_addr for o in b.outputs)


def test_resolve_jury_with_a_rule_refuses_a_missing_config_record(
    monkeypatch, patched_network_for_resolve_jury, captured_builder,
    mock_ogmios_context, sample_deployment_with_all_refs, sample_auditor_wallet,
    sample_challenge_utxo_voting, sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_auditor_wins,
):
    mock_ogmios_context.register_utxos(CONFIG_HOLDER_ADDR, [])
    with pytest.raises(vi.IdentityError, match="configuration record"):
        _resolve(monkeypatch, mock_ogmios_context, sample_deployment_with_all_refs,
                 sample_auditor_wallet, sample_challenge_utxo_voting,
                 sample_claim_utxo_challenged, sample_revealed_juror_utxos_auditor_wins)


# ── ApplyVerdict ───────────────────────────────────────────────────────────


def _verdict_utxo(dep, verdict) -> UTxO:
    ma = MultiAsset()
    asset = Asset()
    asset[AssetName(verdict.token_name())] = 1
    ma[ScriptHash(M1)] = asset
    to = TransactionOutput(dep.identity_party_addr, Value(2_000_000, ma),
                           datum=RawCBOR(verdict.datum_cbor()))
    return UTxO(TransactionInput(TransactionId(b"\xee" * 32), 3), to)


@pytest.mark.parametrize("action,duration", [
    (ip.VerdictAction.SUSPEND, vi.DAYS_7_MS), (ip.VerdictAction.REVOKE, 0),
])
def test_apply_verdict(env, monkeypatch, action, duration):
    agent_vkey = _key(b"agent")[1]
    record = identity_record_utxo(b"arec", "dd" * 32, agent_vkey,
                                  module1_role=vi.Role.ACCOUNTABLE)
    env["ctx"].register_utxos(IDENTITY_ADDR, [record])
    env["ctx"].reward_balances[M1] = 1_000
    verdict = ip.IdentityVerdict(b"\xdd" * 32, action, duration, b"\x77" * 32, 0)
    vutxo = _verdict_utxo(env["dep"], verdict)
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: vutxo)
    skey, vkey, addr = _key(b"anybody")
    result = txb.build_apply_verdict(env["ctx"], env["dep"], skey, vkey, addr, "ee" * 32 + "#3")
    b = env["builders"][-1]
    old = vi.decode_identity_datum(record.output.datum)
    new = vi.decode_identity_datum(b.outputs[0].datum)
    author = vi.Signer.script(M1)
    if action is ip.VerdictAction.SUSPEND:
        assert _data(b._inputs_to_redeemers[record]) == vi.suspend_redeemer(vi.by_party(1))
        assert new == vi.suspended(old, author, 64, LO, LO + duration)
        assert result["until"] == LO + duration
    else:
        assert _data(b._inputs_to_redeemers[record]) == vi.revoke_redeemer(vi.by_party(1))
        assert new == vi.tombstone(old, vi.Status.revoked(author, 64, LO))
    assert _data(b._inputs_to_redeemers[vutxo]) == ip.verdict_spend_redeemer()
    assert dict(b.mint[ScriptHash(M1)]) == {AssetName(verdict.token_name()): -1}
    assert [_data(r) for _, r in b._minting_script_to_redeemers] == [ip.burn_verdict_redeemer()]
    (_, w_red), = b._withdrawal_script_to_redeemers
    assert _data(w_red) == ip.apply_verdict_redeemer()
    assert list(dict(b.withdrawals).values()) == [1_000]
    assert _uid(config_record_utxo()) in _ref_ids(b)
    assert b.outputs[0].address == IDENTITY_ADDR
    assert b.outputs[0].amount.coin >= record.output.amount.coin
    assert b.ttl - b.validity_start <= 3600
    assert list(b.required_signers) == [vkey.hash()]


def test_apply_verdict_keeps_a_longer_module1_hold(env, monkeypatch):
    agent_vkey = _key(b"agent")[1]
    long_hold = vi.Hold(vi.Signer.script(M1), 64, LO - vi.DAY_MS, LO + 20 * vi.DAY_MS)
    record = identity_record_utxo(b"arec", "dd" * 32, agent_vkey,
                                  module1_role=vi.Role.ACCOUNTABLE,
                                  status=vi.Status.suspended([long_hold]))
    env["ctx"].register_utxos(IDENTITY_ADDR, [record])
    verdict = ip.IdentityVerdict(b"\xdd" * 32, ip.VerdictAction.SUSPEND, vi.DAY_MS, b"\x77" * 32, 0)
    vutxo = _verdict_utxo(env["dep"], verdict)
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: vutxo)
    result = txb.build_apply_verdict(env["ctx"], env["dep"], *_key(b"x"), "ee" * 32 + "#3")
    assert result["until"] == LO + 20 * vi.DAY_MS


def test_apply_verdict_refusals(env, monkeypatch):
    agent_vkey = _key(b"agent")[1]
    verdict = ip.IdentityVerdict(b"\xdd" * 32, ip.VerdictAction.REVOKE, 0, b"\x77" * 32, 0)
    vutxo = _verdict_utxo(env["dep"], verdict)
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: vutxo)
    payer = _key(b"x")
    # No Module-1 party with the revoke right (Responsible has suspend only).
    env["ctx"].register_utxos(IDENTITY_ADDR, [identity_record_utxo(
        b"arec", "dd" * 32, agent_vkey, module1_role=vi.Role.RESPONSIBLE)])
    with pytest.raises(vi.IdentityError, match="Module-1 party"):
        txb.build_apply_verdict(env["ctx"], env["dep"], *payer, "ee" * 32 + "#3")
    # A datum that does not hash to the token name.
    other = ip.IdentityVerdict(b"\xdd" * 32, ip.VerdictAction.REVOKE, 0, b"\x76" * 32, 0)
    bad = UTxO(vutxo.input, TransactionOutput(
        vutxo.output.address, vutxo.output.amount, datum=RawCBOR(other.datum_cbor())))
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: bad)
    with pytest.raises(vi.IdentityError, match="token name"):
        txb.build_apply_verdict(env["ctx"], env["dep"], *payer, "ee" * 32 + "#3")
    assert not env["builders"]
