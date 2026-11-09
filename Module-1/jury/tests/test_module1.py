"""Module-1 for the jury: challenge reads, the resolution document, the
submit hook of the simulation builders and the ``auxiliary_data`` argument of
``build_oracle_resolve`` / ``build_timeout_resolve``."""

import json
import os

import pytest
from pycardano import (
    Address, Network, PlutusV3Script, RawCBOR, ScriptHash, Transaction, TransactionBody, TransactionBuilder,
    TransactionOutput, TransactionWitnessSet, UTxO, Value,
)

from conftest import AP3X, RAW_PROTOCOL, FakeChain, Key, T0, h28, h32, tokens, txin
from vector_jury import documents, module1, rules
from vector_jury import types as T

import vector_deploy as vd

NETWORK = os.environ.get("APEX_NETWORK", "testnet").strip().lower() or "testnet"
CLAIM_HASH, CHALLENGE_HASH = h28(b"m1 claim script"), h28(b"m1 challenge script")


def m1() -> module1.Module1:
    manifest = {"network": NETWORK, "hashes": {"claim": CLAIM_HASH.hex(), "challenge": CHALLENGE_HASH.hex()},
                "module1": {"oracle_vkh": Key(b"oracle").hash.hex()}, "protocol_params": {"resolution_deadline": 259_200_000}}
    return module1.Module1(None, manifest, NETWORK, CLAIM_HASH, CHALLENGE_HASH, Key(b"oracle").hash)


def challenge(tag: bytes, state: int, challenged_at: int, claim_token: bytes):
    datum = T.ChallengeDatum((h32(tag), 0), h32(b"auditor"), T.M1Credential(False, Key(b"auditor").hash), 60 * AP3X,
                             h32(b"evidence"), b"vector-tx:" + b"E" * 43, challenged_at, 259_200_000, (), state,
                             claim_token)
    token = rules.token_name(b"chl_", (h32(tag + b" seed"), 0))
    return token, UTxO(txin(tag), TransactionOutput(Address(ScriptHash(CHALLENGE_HASH), network=Network.MAINNET),
                                                     Value(60 * AP3X, tokens(CHALLENGE_HASH, token)),
                                                     datum=RawCBOR(datum.cbor())))


def claim(tag: bytes, token: bytes, state: int = 1):
    datum = T.ClaimDatum(h32(b"claimer"), T.M1Credential(False, Key(b"claimer").hash), h32(b"claim doc"), b"x",
                         b"vector-tx:" + b"C" * 43, 50 * AP3X, 1, 3_600_000, state)
    return UTxO(txin(tag), TransactionOutput(Address(ScriptHash(CLAIM_HASH), network=Network.MAINNET),
                                             Value(50 * AP3X, tokens(CLAIM_HASH, token)), datum=RawCBOR(datum.cbor())))


def test_list_challenges_and_claims():
    chain = FakeChain(T0)
    claim_token = rules.token_name(b"clm_", (h32(b"claim seed"), 0))
    late, late_utxo = challenge(b"late", 0, 2_000, claim_token)
    early, early_utxo = challenge(b"early", 0, 1_000, claim_token)
    resolved, resolved_utxo = challenge(b"resolved", 3, 500, claim_token)
    chain.add(late_utxo, early_utxo, resolved_utxo, claim(b"claim", claim_token))
    chain.add(UTxO(txin(b"junk"), TransactionOutput(Address(ScriptHash(CHALLENGE_HASH), network=Network.MAINNET),
                                                    Value(2 * AP3X))))
    pending = module1.list_challenges(chain, m1())
    assert [c.token for c in pending] == [early, late]
    assert pending[0].deadline == 1_000 + 259_200_000
    assert {c.token for c in module1.list_challenges(chain, m1(), pending_oracle=False)} == {early, late, resolved}
    found = module1.find_claim(chain, m1(), claim_token)
    assert found is not None and found.datum.challenged
    assert module1.challenge_of_ref(chain, m1(), pending[0].ref).token == early


def test_resolution_document():
    claim_token = rules.token_name(b"clm_", (h32(b"claim seed"), 0))
    token, utxo = challenge(b"doc", 0, 1_000, claim_token)
    item = module1.ChallengeOutput(utxo, token, T.decode_challenge_datum(utxo.output.datum))
    doc = json.loads(module1.resolution_document(item, "Inconclusive", "no-time"))
    assert doc == {"type": "vector-jury-resolution/1", "challenge_out": item.ref, "challenge": token.hex(),
                   "claim": claim_token.hex(),
                   "verdict": "Inconclusive", "basis": "no-time", "jury": None}
    with pytest.raises(ValueError):
        module1.resolution_document(item, "Inconclusive", "guess")


def simple_tx() -> bytes:
    body = TransactionBody(inputs=[txin(b"hook input")], outputs=[TransactionOutput(Key(b"x").address, 2 * AP3X)],
                           fee=200_000)
    return vd.tx_bytes(Transaction(body, TransactionWitnessSet()))


def test_submit_hook_evaluates_and_sends_only_with_submit(monkeypatch):
    sim_chain, sim_tx = module1.simulation_modules(NETWORK)
    calls = []

    def fake_rpc(method, params=None):
        calls.append(method)
        if method == "submitTransaction":
            return {"transaction": {"id": "ee" * 32}}
        return []

    monkeypatch.setattr(sim_chain, "ogmios_rpc", fake_rpc)
    original = sim_tx.submit_tx
    tx = simple_tx()
    with module1.submit_hook(False, network=NETWORK) as sent:
        assert sim_tx.submit_tx is not original and sim_chain.submit_tx is sim_tx.submit_tx
        assert sim_tx.submit_tx(tx) == vd.transaction_id(tx)
    assert calls == [] and [s.submitted for s in sent] == [False]
    assert sim_tx.submit_tx is original
    with module1.submit_hook(True, network=NETWORK) as sent:
        assert sim_chain.submit_tx(tx) == "ee" * 32
    assert calls == ["submitTransaction"] and sent[0].submitted and sent[0].tx_id == "ee" * 32
    with pytest.raises(module1.Module1Error):
        module1.simulation_modules("mainnet" if NETWORK == "testnet" else "testnet")


class FakeContext:
    """The context that the simulation builders read (no network)."""

    def __init__(self, slot, wallet_utxos):
        self._slot = slot
        self._utxos = wallet_utxos
        self._pp = vd.protocol_parameters(RAW_PROTOCOL)

    @property
    def protocol_param(self):
        return self._pp

    @property
    def last_block_slot(self):
        return self._slot

    @property
    def network(self):
        return Network.MAINNET

    def genesis_param(self):
        return None

    def utxos(self, address):
        return list(self._utxos)


@pytest.fixture
def resolve_world(monkeypatch):
    """A Module-1 deployment with dummy reference scripts, a PendingOracle
    challenge and its claim; the builders' final TransactionBuilder is
    captured instead of signed."""
    sim_chain, txb = module1.simulation_modules(NETWORK)
    oracle = Key(b"oracle")
    manifest = {
        "hashes": {"claim": CLAIM_HASH.hex(), "challenge": CHALLENGE_HASH.hex(), "jury_pool": h28(b"pool").hex(),
                   "identity_party": h28(b"party").hex()},
        "claim_ref": txin(b"claim ref").transaction_id.payload.hex() + "#0",
        "challenge_ref": txin(b"challenge ref").transaction_id.payload.hex() + "#0",
        "cross_refs_utxo": txin(b"refs").transaction_id.payload.hex() + "#0",
        "params_utxo": txin(b"params").transaction_id.payload.hex() + "#0",
        "identity": {"identity_hash": h28(b"identity").hex(), "identity_ref": "a1" * 32 + "#0",
                     "config_policy": h28(b"config").hex(), "config_holder": h28(b"holder").hex()},
        "identity_party_ref": "a2" * 32 + "#0",
        "module1": {"oracle_vkh": oracle.hash.hex(), "refs_token_policy": h28(b"refs policy").hex()},
        "verdict_policy": {"auditor_wins": {"action": "suspend", "duration_ms": 604_800_000}, "claimer_wins": None},
    }
    dep = txb.DeploymentState(manifest)
    script = PlutusV3Script(b"\x46\x45\x01\x00\x00\x22\x99")
    funder = Key(b"funder")
    dep._claim_ref_utxo = UTxO(txin(b"claim ref"), TransactionOutput(funder.address, 20 * AP3X, script=script))
    dep._challenge_ref_utxo = UTxO(txin(b"challenge ref"), TransactionOutput(funder.address, 20 * AP3X, script=script))
    dep._cross_refs_resolved = UTxO(txin(b"refs"), TransactionOutput(funder.address, 2 * AP3X))
    dep._params_resolved = UTxO(txin(b"params"), TransactionOutput(funder.address, 2 * AP3X))
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    slot = 40_000_000
    claim_token = rules.token_name(b"clm_", (h32(b"resolve claim seed"), 0))
    old = sim_chain.slot_to_posix_ms(slot - 300_000)
    _, ch = challenge(b"resolve challenge", 0, old, claim_token)
    cl = claim(b"resolve claim", claim_token)
    utxos = {bytes(u.input.transaction_id).hex(): u for u in (ch, cl)}
    wallet = UTxO(txin(b"funder wallet"), TransactionOutput(funder.address, 500 * AP3X))
    captured = []

    def capture(self, signing_keys, change_address=None, **kw):
        captured.append(self)

        class _Tx:
            def to_cbor(self_inner):
                return b"\x00"
        return _Tx()

    monkeypatch.setattr(TransactionBuilder, "build_and_sign", capture)
    monkeypatch.setattr(txb, "resolve_utxo", lambda txid, idx: utxos[txid])
    monkeypatch.setattr(txb, "ensure_collateral", lambda *a, **k: None)
    monkeypatch.setattr(txb, "get_wallet_utxos_no_collateral", lambda ctx, addr: [wallet])
    monkeypatch.setattr(txb, "evaluate_and_rebuild", lambda *a, **k: (b"", {}))
    monkeypatch.setattr(txb, "tx_to_bytes", lambda tx: b"\x00")
    monkeypatch.setattr(txb, "submit_tx", lambda data: "cd" * 32)
    monkeypatch.setattr(sim_chain, "pick_pure_ada_collateral", lambda ctx, addr, *a, **k: wallet)
    context = FakeContext(slot, [wallet])
    return {"txb": txb, "dep": dep, "ctx": context, "funder": funder, "oracle": oracle, "challenge": ch, "claim": cl,
            "captured": captured}


def refs(w):
    return (f"{bytes(w['challenge'].input.transaction_id).hex()}#0", f"{bytes(w['claim'].input.transaction_id).hex()}#0")


def test_oracle_resolve_sets_the_auxiliary_data_on_every_pass(resolve_world):
    w = resolve_world
    doc = b'{"type": "vector-jury-resolution/1"}'
    aux = documents.auxiliary_data(doc, "resolution")
    f = w["funder"]
    w["txb"].build_oracle_resolve(w["ctx"], w["dep"], f.skey, f.vkey, f.address, *refs(w), verdict="Inconclusive",
                                  reasoning_hash=documents.doc_hash(doc), oracle_skey=w["oracle"].skey,
                                  auxiliary_data=aux)
    assert w["captured"] and all(b.auxiliary_data is aux for b in w["captured"])
    w["captured"].clear()
    w["txb"].build_oracle_resolve(w["ctx"], w["dep"], f.skey, f.vkey, f.address, *refs(w), verdict="Inconclusive",
                                  reasoning_hash=documents.doc_hash(doc), oracle_skey=w["oracle"].skey)
    assert w["captured"] and all(b.auxiliary_data is None for b in w["captured"])


def test_timeout_resolve_sets_the_auxiliary_data(resolve_world):
    w = resolve_world
    aux = documents.auxiliary_data(b"{}", "resolution")
    f = w["funder"]
    w["txb"].build_timeout_resolve(w["ctx"], w["dep"], f.skey, f.vkey, f.address, *refs(w),
                                   resolution_deadline_ms=259_200_000, auxiliary_data=aux)
    assert w["captured"] and all(b.auxiliary_data is aux for b in w["captured"])
    w["captured"].clear()
    w["txb"].build_timeout_resolve(w["ctx"], w["dep"], f.skey, f.vkey, f.address, *refs(w),
                                   resolution_deadline_ms=259_200_000)
    assert w["captured"] and all(b.auxiliary_data is None for b in w["captured"])
