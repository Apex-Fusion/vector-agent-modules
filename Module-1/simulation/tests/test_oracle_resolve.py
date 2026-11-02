"""Tests of ``build_oracle_resolve``: the Foundation oracle resolves a
challenge in the state PendingOracle (OracleResolve, Phase 1.0; Part 2 §3.8).

Shape tests build the transaction against the fake chain context of conftest
and inspect the TransactionBuilder.

Simulate tests apply the committed blueprint (``contracts/plutus.json``) with
a test AuditingConfig and ProtocolParams (``oracle_active = true``), build the
transaction with the builder and the applied scripts (real hashes and
addresses), and run each redeemer with ``aiken tx simulate``
(``shared/python/aiken_simulate.py``). The time base is the system start of
``simulation.config``. Refusal tests change one thing in the built
transaction and check which redeemer fails.

Nothing is submitted. All keys come from fixed test seeds.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
from pathlib import Path

import pytest
from pycardano import (
    Address, Asset, AssetName, MultiAsset, Network, PaymentSigningKey,
    PaymentVerificationKey, RawCBOR, ScriptHash, TransactionBuilder,
    TransactionId, TransactionInput, TransactionOutput, UTxO, Value,
    VerificationKeyHash,
)
from pycardano.utils import min_lovelace_post_alonzo

import simulation.tx_builder as txb
from simulation import identity_party as ip
from simulation import vector_identity as vi
from simulation.chain import challenge_token_name, claim_token_name, slot_to_posix_ms
from simulation.config import SYSTEM_START_UNIX
from simulation.tests.conftest import (
    CANNED_SLOT, CONFIG_HOLDER, CONFIG_HOLDER_ADDR, CONFIG_POLICY, IDENTITY_HASH,
    IDENTITY_PARTY_HASH, V13_DEPLOYMENT, FakeOgmiosContext, config_record_utxo,
    identity_party_ref_script_utxo, identity_ref_script_utxo,
)
from aiken_simulate import aiken_available, apply_validator, resolved_inputs, simulate

MODULE_DIR = Path(__file__).resolve().parents[2]
BLUEPRINT = MODULE_DIR / "contracts" / "plutus.json"
#: POSIX milliseconds of slot 0: the system start that the client uses for
#: slot -> POSIX (simulation.chain.slot_to_posix_ms).
ZERO_TIME_MS = SYSTEM_START_UNIX * 1000

M1 = IDENTITY_PARTY_HASH
CHALLENGE_HASH = bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"])
CLAIM_HASH = bytes.fromhex(V13_DEPLOYMENT["hashes"]["claim"])
REFS_POLICY = bytes.fromhex(V13_DEPLOYMENT["module1"]["refs_token_policy"])

requires_aiken = pytest.mark.skipif(not aiken_available(), reason="aiken is not on PATH")


def _hash(tag: bytes) -> bytes:
    return hashlib.blake2b(tag, digest_size=32).digest()


def _key(tag: bytes):
    skey = PaymentSigningKey(_hash(tag))
    vkey = PaymentVerificationKey.from_signing_key(skey)
    return skey, vkey, Address(payment_part=vkey.hash(), network=Network.MAINNET)


ORACLE = _key(b"oracle-resolve:oracle")
FUNDER = _key(b"oracle-resolve:funder")
CLAIMER = _key(b"oracle-resolve:claimer")
AUDITOR = _key(b"oracle-resolve:auditor")
ORACLE_VKH = bytes(ORACLE[1].hash())

CLAIMER_DID = _hash(b"oracle-resolve:claimer-did")
AUDITOR_DID = _hash(b"oracle-resolve:auditor-did")
REASONING_HASH = _hash(b"oracle-resolve:reasoning")
CLAIM_STAKE = 50_000_000
AUDITOR_STAKE = 60_000_000
TOTAL = CLAIM_STAKE + AUDITOR_STAKE
CHALLENGE_TOKEN = challenge_token_name(_hash(b"oracle-resolve:challenge-seed"), 0)
CLAIM_TOKEN = claim_token_name(_hash(b"oracle-resolve:claim-seed"), 0)
OTHER_CLAIM_TOKEN = claim_token_name(_hash(b"oracle-resolve:other-claim-seed"), 0)

#: The placeholder policy of deploy/identity/config.json: AuditorWins
#: suspends the claimer for 7 days, ClaimerWins gives no verdict.
PLACEHOLDER_POLICY = {
    "auditor_wins": {"action": "suspend", "duration_ms": vi.DAYS_7_MS},
    "claimer_wins": None,
}
#: A policy with a rule for both decisive outcomes: ClaimerWins revokes the
#: auditor.
BOTH_POLICY = {
    "auditor_wins": {"action": "suspend", "duration_ms": vi.DAYS_7_MS},
    "claimer_wins": {"action": "revoke", "duration_ms": 0},
}


def _txin(tag: bytes, index: int = 0) -> TransactionInput:
    return TransactionInput(TransactionId(_hash(tag)), index)


def _ref(utxo: UTxO) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def _uid(utxo: UTxO) -> tuple:
    return (bytes(utxo.input.transaction_id), utxo.input.index)


def _tokens(policy: bytes, name: bytes, quantity: int = 1) -> MultiAsset:
    return MultiAsset({ScriptHash(policy): Asset({AssetName(name): quantity})})


def _key_address(key) -> Address:
    return Address(VerificationKeyHash(bytes(key[1].hash())), network=Network.MAINNET)


def _challenge_utxo(challenge_hash: bytes, *, state=None, claim_token: bytes = CLAIM_TOKEN) -> UTxO:
    """A challenge at the challenge address: the auditor stake, one challenge
    token and an 11-field ChallengeDatum (PendingOracle by default) that
    names ``claim_token``."""
    datum = vi.constr(0, [
        vi.output_reference_data(_hash(b"oracle-resolve:claim-before-challenge"), 0),
        AUDITOR_DID,
        vi.constr(0, [bytes(AUDITOR[1].hash())]),
        AUDITOR_STAKE,
        _hash(b"oracle-resolve:evidence"),
        b"ipfs://oracle-resolve-evidence",
        slot_to_posix_ms(CANNED_SLOT - 600),
        5_400_000,
        [],
        vi.constr(0, []) if state is None else state,
        claim_token,
    ])
    return UTxO(_txin(b"oracle-resolve:challenge"), TransactionOutput(
        Address(ScriptHash(challenge_hash), network=Network.MAINNET),
        Value(AUDITOR_STAKE, _tokens(challenge_hash, CHALLENGE_TOKEN)),
        datum=RawCBOR(vi.serialise_data(datum)),
    ))


def _claim_utxo(claim_hash: bytes, *, token: bytes = CLAIM_TOKEN, state: int = 1,
                tag: bytes = b"oracle-resolve:claim") -> UTxO:
    """A claim at the claim address: the claim stake, one claim token and a
    ClaimDatum in the state ``state`` (1 = Challenged)."""
    datum = vi.constr(0, [
        CLAIMER_DID,
        vi.constr(0, [bytes(CLAIMER[1].hash())]),
        _hash(b"oracle-resolve:claim-data"),
        b"data_indexing",
        b"ipfs://oracle-resolve-claim",
        CLAIM_STAKE,
        slot_to_posix_ms(CANNED_SLOT - 1200),
        1_800_000,
        vi.constr(state, []),
    ])
    return UTxO(_txin(tag), TransactionOutput(
        Address(ScriptHash(claim_hash), network=Network.MAINNET),
        Value(CLAIM_STAKE, _tokens(claim_hash, token)),
        datum=RawCBOR(vi.serialise_data(datum)),
    ))


def _wallet_utxo(addr: Address, tag: bytes) -> UTxO:
    return UTxO(_txin(tag), TransactionOutput(addr, Value(500_000_000)))


def _data(redeemer) -> object:
    return vi.data_from(redeemer.data)


def _ref_ids(builder) -> set:
    return {
        (bytes(getattr(r, "input", r).transaction_id), getattr(r, "input", r).index)
        for r in builder.reference_inputs
    }


def _resolve(world: dict, dep, verdict: str = ip.AUDITOR_WINS, *, wallet=FUNDER,
             challenge: UTxO | None = None, claim: UTxO | None = None, **kw) -> dict:
    """Run build_oracle_resolve on ``world`` (the oracle key signs unless
    ``oracle_skey`` is given)."""
    challenge = challenge or world["challenge"]
    claim = claim or world["claim"]
    world["put"](challenge, claim)
    kw.setdefault("reasoning_hash", REASONING_HASH)
    kw.setdefault("oracle_skey", ORACLE[0])
    return txb.build_oracle_resolve(
        world["ctx"], dep, *wallet, _ref(challenge), _ref(claim), verdict=verdict, **kw,
    )


def test_oracle_resolve_redeemer_follows_the_blueprint():
    """``OracleResolve { verdict, reasoning_hash }`` is Constr 2 of
    ChallengeAction, and the outcomes are the constructors of Verdict, as
    the blueprint defines them."""
    import json
    definitions = json.loads(BLUEPRINT.read_text())["definitions"]

    def constructors(name: str) -> dict:
        return {c["title"]: c["index"] for c in definitions[f"adversarial_auditing/types/{name}"]["anyOf"]}

    assert constructors("ChallengeAction")["OracleResolve"] == ip.ORACLE_RESOLVE_INDEX
    verdicts = constructors("Verdict")
    for outcome in ip.OUTCOMES:
        assert ip.outcome_data(outcome) == vi.constr(verdicts[outcome], [])
    assert vi.serialise_data(ip.oracle_resolve_redeemer(ip.AUDITOR_WINS, REASONING_HASH)) == (
        bytes.fromhex("d87b9fd87a805820") + REASONING_HASH + b"\xff"
    )
    with pytest.raises(ValueError, match="outcome"):
        ip.outcome_data("Draw")
    with pytest.raises(ValueError, match="32-byte"):
        ip.oracle_resolve_redeemer(ip.AUDITOR_WINS, REASONING_HASH[:31])


# ── Shape tests (fake context, dummy reference scripts) ────────────────────


@pytest.fixture
def shape(monkeypatch, mock_ogmios_context, sample_claim_ref_script_utxo,
          sample_challenge_ref_script_utxo, sample_cross_refs_utxo, sample_params_utxo):
    """The fake context with the V13 test hashes. ``shape["deployment"]``
    makes a DeploymentState whose oracle is ORACLE; ``shape["builders"]``
    and ``shape["signed"]`` record each build_and_sign call."""
    rec = {"builders": [], "signed": [], "ctx": mock_ogmios_context}
    utxos: dict = {}

    def deployment(policy=PLACEHOLDER_POLICY):
        manifest = copy.deepcopy(V13_DEPLOYMENT)
        manifest["module1"]["oracle_vkh"] = ORACLE_VKH.hex()
        manifest["verdict_policy"] = copy.deepcopy(policy)
        dep = txb.DeploymentState(manifest)
        dep._claim_ref_utxo = sample_claim_ref_script_utxo
        dep._challenge_ref_utxo = sample_challenge_ref_script_utxo
        dep._cross_refs_resolved = sample_cross_refs_utxo
        dep._params_resolved = sample_params_utxo
        dep._identity_ref_utxo = identity_ref_script_utxo()
        dep._identity_party_ref_utxo = identity_party_ref_script_utxo()
        monkeypatch.setattr(dep, "resolve_refs", lambda: None)
        return dep

    def put(*items):
        for u in items:
            utxos[bytes(u.input.transaction_id).hex()] = u

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
    monkeypatch.setattr(txb, "get_wallet_utxos_no_collateral",
                        lambda ctx, addr: [_wallet_utxo(addr, str(addr).encode())])
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: utxos[txid])
    rec.update(deployment=deployment, put=put,
               challenge=_challenge_utxo(CHALLENGE_HASH), claim=_claim_utxo(CLAIM_HASH))
    return rec


def test_auditor_wins_mints_the_verdict_of_the_claimer(shape):
    dep = shape["deployment"]()
    result = _resolve(shape, dep, ip.AUDITOR_WINS)
    b = shape["builders"][-1]

    # The verdict output (output 0): exact identity_party address, AP3X and
    # the verdict token only, the verdict as inline datum, no script.
    out = b.outputs[0]
    verdict = ip.decode_verdict(out.datum)
    assert verdict == ip.IdentityVerdict(
        agent=CLAIMER_DID, action=ip.VerdictAction.SUSPEND, duration=vi.DAYS_7_MS,
        challenge_tx_id=bytes(shape["challenge"].input.transaction_id), challenge_index=0,
        deposit_to=txb.plutus_address(FUNDER[2]),
        expires_at=slot_to_posix_ms(b.ttl) + vi.DAYS_30_MS,
    )
    token = verdict.token_name()
    assert out.address == dep.identity_party_addr and out.address.staking_part is None
    assert {bytes(p): dict(a) for p, a in out.amount.multi_asset.items()} == {M1: {AssetName(token): 1}}
    assert out.script is None
    assert out.amount.coin >= min_lovelace_post_alonzo(out, shape["ctx"])

    # Mint: both burns and exactly one verdict token under M1.
    assert {bytes(p): dict(a) for p, a in b.mint.items()} == {
        CHALLENGE_HASH: {AssetName(CHALLENGE_TOKEN): -1},
        CLAIM_HASH: {AssetName(CLAIM_TOKEN): -1},
        M1: {AssetName(token): 1},
    }
    resolve = ip.oracle_resolve_redeemer(ip.AUDITOR_WINS, REASONING_HASH)
    assert _data(b._inputs_to_redeemers[shape["challenge"]]) == resolve
    assert _data(b._inputs_to_redeemers[shape["claim"]]) == vi.constr(3, [])
    assert [_data(r) for _, r in b._minting_script_to_redeemers] == [
        resolve, vi.constr(3, []), ip.mint_verdict_redeemer(),
    ]

    # The auditor gets both stakes (no jury fee), in one exact output.
    (payout,) = b.outputs[1:]
    assert payout.address == _key_address(AUDITOR)
    assert payout.amount.coin == TOTAL and not payout.amount.multi_asset
    assert payout.datum is None and payout.datum_hash is None

    assert {_uid(dep.cross_refs_utxo), _uid(dep.params_utxo), _uid(config_record_utxo())} <= _ref_ids(b)
    assert list(b.required_signers) == [FUNDER[1].hash(), VerificationKeyHash(ORACLE_VKH)]
    assert {bytes(k.to_verification_key().hash()) for k in shape["signed"][-1]} == {
        bytes(FUNDER[1].hash()), ORACLE_VKH,
    }
    # Only the wallet funds the transaction: the other inputs are the
    # challenge and the claim.
    assert [u for u in b.inputs if isinstance(u.output.address.payment_part, VerificationKeyHash)] == [
        u for u in b.inputs if u.output.address == FUNDER[2]
    ]
    assert b.validity_start == CANNED_SLOT - txb.VALIDITY_LAG_SLOTS
    assert b.ttl - b.validity_start == txb.IDENTITY_WINDOW_SLOTS
    assert result == {
        "tx_hash": "cd" * 32, "verdict": ip.AUDITOR_WINS,
        "claimer_payout": None, "auditor_payout": TOTAL,
        "verdict_ref": "cd" * 32 + "#0", "verdict_token_hex": token.hex(),
        "identity_verdict": verdict.to_json(),
        "validity_start_slot": b.validity_start, "ttl_slot": b.ttl,
    }


def test_the_verdict_mint_has_no_withdrawal(shape, monkeypatch):
    """The ledger takes a withdrawal from a reward account only for its whole
    balance. That rule matters only for the transactions with the
    identity_party withdrawal (Consent, ApplyVerdict): MintVerdict does not
    read the withdrawals, so the oracle resolution withdraws nothing and
    does not read the balance of Script(M1)."""
    shape["ctx"].reward_balances[M1] = 7_000
    monkeypatch.setattr(txb, "module1_reward_balance",
                        lambda *a: pytest.fail("the oracle resolution reads no reward balance"))
    _resolve(shape, shape["deployment"](), ip.AUDITOR_WINS)
    b = shape["builders"][-1]
    assert M1 in {bytes(p) for p in b.mint}
    assert b.withdrawals is None and not b._withdrawal_script_to_redeemers


@pytest.mark.parametrize("policy,outcome,agent,payouts", [
    (BOTH_POLICY, ip.CLAIMER_WINS, AUDITOR_DID, (TOTAL, None)),
    (PLACEHOLDER_POLICY, ip.CLAIMER_WINS, None, (TOTAL, None)),
    (BOTH_POLICY, ip.INCONCLUSIVE, None, (CLAIM_STAKE, AUDITOR_STAKE)),
], ids=["claimer-wins-revoke", "claimer-wins-no-rule", "inconclusive"])
def test_outcomes(shape, policy, outcome, agent, payouts):
    """ClaimerWins with a rule names the auditor. ClaimerWins without a rule
    and Inconclusive mint nothing and need no configuration record. The
    stakes move without a jury fee."""
    if agent is None:
        shape["ctx"].register_utxos(CONFIG_HOLDER_ADDR, [])
    dep = shape["deployment"](policy)
    result = _resolve(shape, dep, outcome)
    b = shape["builders"][-1]
    claimer_payout, auditor_payout = payouts
    assert (result["claimer_payout"], result["auditor_payout"]) == payouts
    expected = []
    if claimer_payout is not None:
        expected.append((_key_address(CLAIMER), claimer_payout))
    if auditor_payout is not None:
        expected.append((_key_address(AUDITOR), auditor_payout))
    payout_outputs = [o for o in b.outputs if o.address != dep.identity_party_addr]
    assert [(o.address, o.amount.coin) for o in payout_outputs] == expected
    assert all(not o.amount.multi_asset and o.datum is None for o in payout_outputs)
    if agent is None:
        assert {bytes(p) for p in b.mint} == {CHALLENGE_HASH, CLAIM_HASH}
        assert len(b._minting_script_to_redeemers) == 2
        assert len(payout_outputs) == len(b.outputs)
        assert _uid(config_record_utxo()) not in _ref_ids(b)
        assert result["verdict_ref"] is None and result["identity_verdict"] is None
        assert result["verdict_token_hex"] is None
    else:
        verdict = ip.decode_verdict(b.outputs[0].datum)
        assert (verdict.agent, verdict.action, verdict.duration) == (agent, ip.VerdictAction.REVOKE, 0)
        assert dict(b.mint[ScriptHash(M1)]) == {AssetName(verdict.token_name()): 1}
        assert _uid(config_record_utxo()) in _ref_ids(b)
    assert b.withdrawals is None


def test_the_wallet_key_is_the_oracle_key_by_default(shape):
    result = _resolve(shape, shape["deployment"](), ip.AUDITOR_WINS, wallet=ORACLE, oracle_skey=None)
    b = shape["builders"][-1]
    assert list(b.required_signers) == [ORACLE[1].hash()]
    assert ip.decode_verdict(b.outputs[0].datum).deposit_to == txb.plutus_address(ORACLE[2])
    assert result["identity_verdict"]["deposit_to"] == txb.plutus_address(ORACLE[2]).to_json()


@pytest.mark.parametrize("change,error,match", [
    (dict(verdict="Draw"), ValueError, "verdict must be one of"),
    (dict(reasoning_hash=REASONING_HASH[:31]), ValueError, "32-byte"),
    (dict(oracle_skey=FUNDER[0]), ValueError, "oracle_vkh"),
    (dict(oracle_skey=None), ValueError, "oracle_vkh"),
    (dict(challenge=_challenge_utxo(CHALLENGE_HASH, state=vi.constr(1, []))), ValueError, "PendingOracle"),
    (dict(claim=_claim_utxo(CLAIM_HASH, state=0)), ValueError, "Challenged"),
    (dict(claim=_claim_utxo(CLAIM_HASH, token=OTHER_CLAIM_TOKEN)), ValueError, "claim_token"),
    (dict(window_slots=3601), vi.IdentityError, "validity range"),
], ids=["verdict", "reasoning-hash", "other-oracle-key", "wallet-is-not-oracle", "pending-jury",
        "open-claim", "claim-of-other-challenge", "range-over-one-hour"])
def test_refusals_before_any_side_effect(shape, change, error, match):
    with pytest.raises(error, match=match):
        _resolve(shape, shape["deployment"](), **{"verdict": ip.AUDITOR_WINS, **change})
    assert not shape["builders"]


def test_a_verdict_needs_the_configuration_record(shape):
    shape["ctx"].register_utxos(CONFIG_HOLDER_ADDR, [])
    dep = shape["deployment"]()
    with pytest.raises(vi.IdentityError, match="configuration record"):
        _resolve(shape, dep, ip.AUDITOR_WINS)
    assert not shape["builders"]
    # Without a verdict the record is not needed.
    _resolve(shape, dep, ip.INCONCLUSIVE)
    assert len(shape["builders"]) == 1


# ── Simulate tests (applied validators of contracts/plutus.json) ───────────


def _deploy_script():
    """scripts/identity_deploy.py as a module (its Plutus Data encoder)."""
    spec = importlib.util.spec_from_file_location(
        "identity_deploy", MODULE_DIR / "scripts" / "identity_deploy.py",
    )
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    return deploy


def _script_utxo(tag: bytes, applied) -> UTxO:
    """A UTxO with the applied validator as reference script."""
    return UTxO(_txin(tag), TransactionOutput(
        _key_address(FUNDER), Value(30_000_000), script=applied.script,
    ))


@pytest.fixture
def chain(monkeypatch):
    """Applies the Module-1 blueprint with the test configuration and makes
    the fake chain for ``chain["run"](policy, verdict, ...)``. ``run`` builds
    the transaction with build_oracle_resolve (real PyCardano balancing and
    signing), and returns (result, tx, world)."""
    deploy = _deploy_script()
    params = dict(deploy.load_config(deploy.DEFAULT_CONFIG)["protocol_params"], oracle_active=True)
    params_cbor = deploy.encode(deploy.protocol_params(params))
    captured: list = []

    def world(policy, *, manifest_oracle_vkh: bytes = ORACLE_VKH) -> dict:
        config = ip.AuditingConfig(
            identity_hash=IDENTITY_HASH, oracle_vkh=ORACLE_VKH, refs_token_policy=REFS_POLICY,
            config_policy=CONFIG_POLICY, config_holder=CONFIG_HOLDER,
            verdict_policy=ip.VerdictPolicy.from_json(policy),
        )
        two = (config.cbor().hex(), params_cbor.hex())
        claim = apply_validator(BLUEPRINT, "claim", "claim", *two)
        challenge = apply_validator(BLUEPRINT, "challenge", "challenge", *two)
        jury_pool = apply_validator(BLUEPRINT, "jury_pool", "jury_pool", *two)
        party = apply_validator(BLUEPRINT, "identity_party", "identity_party", *(
            p.hex() for p in ip.identity_party_parameters(
                config, bytes.fromhex(challenge.hash), bytes.fromhex(claim.hash),
            )
        ))
        m1 = bytes.fromhex(party.hash)
        refs = UTxO(_txin(b"oracle-sim:refs"), TransactionOutput(
            _key_address(FUNDER), Value(2_000_000, _tokens(REFS_POLICY, b"refs")),
            datum=RawCBOR(deploy.encode(deploy.cross_refs(claim.hash, challenge.hash, jury_pool.hash))),
        ))
        params_utxo = UTxO(_txin(b"oracle-sim:params"), TransactionOutput(
            _key_address(FUNDER), Value(2_000_000), datum=RawCBOR(params_cbor),
        ))
        scripts = {
            "claim": _script_utxo(b"oracle-sim:claim-ref", claim),
            "challenge": _script_utxo(b"oracle-sim:challenge-ref", challenge),
            "party": _script_utxo(b"oracle-sim:party-ref", party),
        }
        manifest = {
            "hashes": {"claim": claim.hash, "challenge": challenge.hash,
                       "jury_pool": jury_pool.hash, "identity_party": party.hash},
            "claim_ref": _ref(scripts["claim"]), "challenge_ref": _ref(scripts["challenge"]),
            "cross_refs_utxo": _ref(refs), "params_utxo": _ref(params_utxo),
            "identity": {"identity_hash": IDENTITY_HASH.hex(), "identity_ref": "a1" * 32 + "#0",
                         "config_policy": CONFIG_POLICY.hex(), "config_holder": CONFIG_HOLDER.hex()},
            "identity_party_ref": _ref(scripts["party"]),
            "module1": {"oracle_vkh": manifest_oracle_vkh.hex(), "refs_token_policy": REFS_POLICY.hex()},
            "verdict_policy": copy.deepcopy(policy),
        }
        dep = txb.DeploymentState(manifest)
        dep._claim_ref_utxo = scripts["claim"]
        dep._challenge_ref_utxo = scripts["challenge"]
        dep._cross_refs_resolved = refs
        dep._params_resolved = params_utxo
        dep._identity_party_ref_utxo = scripts["party"]
        monkeypatch.setattr(dep, "resolve_refs", lambda: None)
        assert dataclasses.replace(dep.auditing_config, oracle_vkh=ORACLE_VKH) == config

        ctx = FakeOgmiosContext()
        config_record = config_record_utxo(m1)
        ctx.register_utxos(CONFIG_HOLDER_ADDR, [config_record])
        challenge_utxo = _challenge_utxo(bytes.fromhex(challenge.hash))
        claim_utxo = _claim_utxo(bytes.fromhex(claim.hash))
        other_claim = _claim_utxo(bytes.fromhex(claim.hash), token=OTHER_CLAIM_TOKEN,
                                  tag=b"oracle-resolve:other-claim")
        wallets = {str(FUNDER[2]): _wallet_utxo(FUNDER[2], b"oracle-sim:funder"),
                   str(ORACLE[2]): _wallet_utxo(ORACLE[2], b"oracle-sim:oracle")}
        pool = [refs, params_utxo, config_record, challenge_utxo, claim_utxo, other_claim,
                *scripts.values(), *wallets.values()]
        return {"dep": dep, "ctx": ctx, "pool": pool, "m1": m1, "challenge": challenge_utxo,
                "claim": claim_utxo, "other_claim": other_claim, "wallets": wallets,
                "challenge_hash": bytes.fromhex(challenge.hash),
                "claim_hash": bytes.fromhex(claim.hash),
                "sizes": {name: s.size for name, s in
                          (("challenge", challenge), ("claim", claim), ("identity_party", party))}}

    def run(policy, verdict, *, claim_key: str = "claim", **kw):
        w = world(policy, manifest_oracle_vkh=kw.pop("manifest_oracle_vkh", ORACLE_VKH))
        by_ref = {(bytes(u.input.transaction_id).hex(), u.input.index): u for u in w["pool"]}
        monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: by_ref[(txid, int(idx))])
        monkeypatch.setattr(txb, "ensure_collateral", lambda *a, **k: None)
        monkeypatch.setattr(txb, "get_wallet_utxos_no_collateral",
                            lambda ctx, addr: [w["wallets"][str(addr)]])
        monkeypatch.setattr(txb, "evaluate_and_rebuild", lambda *a, **k: (b"", {}))
        monkeypatch.setattr(txb, "tx_to_bytes", lambda tx: captured.append(tx) or b"")
        monkeypatch.setattr(txb, "submit_tx", lambda tx_bytes: bytes(captured[-1].id).hex())
        kw.setdefault("reasoning_hash", REASONING_HASH)
        kw.setdefault("oracle_skey", ORACLE[0])
        wallet = kw.pop("wallet", FUNDER)
        result = txb.build_oracle_resolve(
            w["ctx"], w["dep"], *wallet, _ref(w["challenge"]), _ref(w[claim_key]),
            verdict=verdict, **kw,
        )
        return result, captured[-1], w

    return {"run": run}


def _simulate(tx, w):
    return simulate(tx, resolved_inputs(tx, w["pool"]), zero_time_ms=ZERO_TIME_MS)


def _purposes(tx, w, claim: UTxO | None = None) -> dict:
    """The redeemer purposes (aiken_simulate labels) of the transaction."""
    body = tx.transaction_body
    inputs = sorted((bytes(i.transaction_id), i.index) for i in body.inputs)
    policies = sorted(bytes(p) for p in body.mint) if body.mint else []
    labels = {
        "challenge_spend": f"Spend[{inputs.index(_uid(w['challenge']))}]",
        "claim_spend": f"Spend[{inputs.index(_uid(claim or w['claim']))}]",
        "challenge_burn": f"Mint[{policies.index(w['challenge_hash'])}]",
        "claim_burn": f"Mint[{policies.index(w['claim_hash'])}]",
    }
    if w["m1"] in policies:
        labels["verdict_mint"] = f"Mint[{policies.index(w['m1'])}]"
    return labels


@requires_aiken
def test_simulate_auditor_wins_with_verdict_passes(chain):
    """The real validators accept the AuditorWins resolution with the verdict
    mint: OracleResolve, ForfeitClaim, both burns and MintVerdict pass. The
    transaction has no withdrawal, and the wallet funds the AP3X of the
    verdict output (``deposit_to``)."""
    result, tx, w = chain["run"](PLACEHOLDER_POLICY, ip.AUDITOR_WINS)
    sim = _simulate(tx, w)
    labels = _purposes(tx, w)
    assert sim.passed, sim.describe() + "\n" + sim.report
    assert sorted(v.purpose for v in sim.verdicts) == sorted(labels.values())

    body = tx.transaction_body
    assert body.withdraws is None
    out = body.outputs[0]
    verdict = ip.verdict_from_output(out.datum, bytes.fromhex(result["verdict_token_hex"]))
    assert verdict.agent == CLAIMER_DID
    assert verdict.expires_at == slot_to_posix_ms(body.ttl) + vi.DAYS_30_MS
    assert verdict.deposit_to == txb.plutus_address(FUNDER[2])
    # The stakes pay the payout; the wallet pays the verdict output, the fee
    # and the change.
    wallet_in = sum(u.output.amount.coin for u in w["wallets"].values()
                    if _uid(u) in {(bytes(i.transaction_id), i.index) for i in body.inputs})
    change = sum(o.amount.coin for o in body.outputs if o.address == FUNDER[2])
    assert wallet_in - change - body.fee == out.amount.coin
    assert [o.amount.coin for o in body.outputs if o.address == _key_address(AUDITOR)] == [TOTAL]
    assert {bytes(s) for s in body.required_signers} == {bytes(FUNDER[1].hash()), ORACLE_VKH}


@requires_aiken
@pytest.mark.parametrize("policy,outcome,verdict_mint", [
    (BOTH_POLICY, ip.CLAIMER_WINS, True),
    (PLACEHOLDER_POLICY, ip.CLAIMER_WINS, False),
    (BOTH_POLICY, ip.INCONCLUSIVE, False),
], ids=["claimer-wins-revoke", "claimer-wins-no-rule", "inconclusive"])
def test_simulate_other_outcomes_pass(chain, policy, outcome, verdict_mint):
    """ClaimerWins with a rule mints the verdict of the auditor. ClaimerWins
    without a rule and Inconclusive mint nothing, and the real validators
    accept them without a verdict."""
    result, tx, w = chain["run"](policy, outcome)
    sim = _simulate(tx, w)
    labels = _purposes(tx, w)
    assert sim.passed, sim.describe() + "\n" + sim.report
    assert ("verdict_mint" in labels) is verdict_mint
    assert sorted(v.purpose for v in sim.verdicts) == sorted(labels.values())
    if verdict_mint:
        assert result["identity_verdict"]["agent"] == vi.did_from_id(AUDITOR_DID)


def _failing(sim, labels) -> set:
    names = {purpose: name for name, purpose in labels.items()}
    return {names[p] for p in sim.failing}


@requires_aiken
def test_simulate_refuses_a_missing_verdict_mint(chain, monkeypatch):
    """When the policy gives a rule for the outcome, the challenge validator
    (OracleResolve) needs the verdict mint. Without it, only the challenge
    spend fails."""
    monkeypatch.setattr(ip, "resolution_verdict", lambda *a, **k: None)
    _, tx, w = chain["run"](PLACEHOLDER_POLICY, ip.AUDITOR_WINS)
    sim = _simulate(tx, w)
    labels = _purposes(tx, w)
    assert "verdict_mint" not in labels
    assert _failing(sim, labels) == {"challenge_spend"}, sim.describe()


@requires_aiken
def test_simulate_refuses_a_wrong_expires_at(chain, monkeypatch):
    """``expires_at`` must be the upper bound + 30 days. A verdict with
    another value (its token name and its datum agree) fails MintVerdict."""
    real = ip.resolution_verdict

    def later(*a, **k):
        v = real(*a, **k)
        return None if v is None else dataclasses.replace(v, expires_at=v.expires_at + 1)

    monkeypatch.setattr(ip, "resolution_verdict", later)
    _, tx, w = chain["run"](PLACEHOLDER_POLICY, ip.AUDITOR_WINS)
    sim = _simulate(tx, w)
    assert _failing(sim, _purposes(tx, w)) == {"verdict_mint"}, sim.describe()


@requires_aiken
def test_simulate_refuses_a_deposit_to_other_than_the_funder(chain, monkeypatch):
    """The token name hashes the verdict with ``deposit_to`` = the funding
    wallet. A datum that names another address in ``deposit_to`` fails
    MintVerdict (the mint reads ``deposit_to`` from the datum, so the token
    name does not match)."""
    other = txb.plutus_address(_key_address(AUDITOR))
    real_datum = ip.IdentityVerdict.datum_cbor
    monkeypatch.setattr(ip.IdentityVerdict, "datum_cbor",
                        lambda self: real_datum(dataclasses.replace(self, deposit_to=other)))
    _, tx, w = chain["run"](PLACEHOLDER_POLICY, ip.AUDITOR_WINS)
    assert ip.decode_verdict(tx.transaction_body.outputs[0].datum).deposit_to == other
    sim = _simulate(tx, w)
    assert _failing(sim, _purposes(tx, w)) == {"verdict_mint"}, sim.describe()


@requires_aiken
def test_simulate_refuses_the_claim_of_another_challenge(chain, monkeypatch):
    """The challenge names its claim token. A transaction that spends and
    burns the claim of another challenge fails the challenge spend
    (bound_claim), the claim spend (ForfeitClaim) and MintVerdict. The
    builder refuses this claim; the test gives it a datum reader that
    reports the other claim token."""
    real_fields = txb.challenge_datum_fields

    def other_claim_token(utxo):
        fields = real_fields(utxo)
        fields[txb.CHALLENGE_CLAIM_TOKEN] = OTHER_CLAIM_TOKEN
        return fields

    monkeypatch.setattr(txb, "challenge_datum_fields", other_claim_token)
    _, tx, w = chain["run"](PLACEHOLDER_POLICY, ip.AUDITOR_WINS, claim_key="other_claim")
    sim = _simulate(tx, w)
    labels = _purposes(tx, w, claim=w["other_claim"])
    assert _failing(sim, labels) == {"challenge_spend", "claim_spend", "verdict_mint"}, sim.describe()


@requires_aiken
def test_simulate_refuses_a_verdict_for_inconclusive(chain, monkeypatch):
    """Inconclusive gives no verdict: a verdict mint in an Inconclusive
    resolution fails MintVerdict (no rule), and the challenge spend
    passes."""
    real = ip.resolution_verdict
    monkeypatch.setattr(ip, "resolution_verdict",
                        lambda policy, outcome, **k: real(policy, ip.AUDITOR_WINS, **k))
    _, tx, w = chain["run"](BOTH_POLICY, ip.INCONCLUSIVE)
    sim = _simulate(tx, w)
    labels = _purposes(tx, w)
    assert "verdict_mint" in labels
    assert _failing(sim, labels) == {"verdict_mint"}, sim.describe()


@requires_aiken
def test_simulate_refuses_a_resolution_without_the_oracle_signature(chain):
    """The challenge spend needs the signature of oracle_vkh. A manifest
    that names another key lets the builder sign with that key only, and
    the challenge spend fails."""
    _, tx, w = chain["run"](PLACEHOLDER_POLICY, ip.AUDITOR_WINS,
                            manifest_oracle_vkh=bytes(FUNDER[1].hash()), oracle_skey=None)
    assert ORACLE_VKH not in {bytes(s) for s in tx.transaction_body.required_signers}
    sim = _simulate(tx, w)
    assert _failing(sim, _purposes(tx, w)) == {"challenge_spend"}, sim.describe()
