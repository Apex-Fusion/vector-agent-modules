"""Amendments 2 to 7: document reader and writer rules, the CBOR fallback,
the record lookup by hash, genuine challenges (§11.1), the stake check, the
dispute cases and the canonical case (§11.2), and the Open refund_to rule."""

import dataclasses

import pytest
from pycardano import Address, Network, PointerAddress, ScriptHash, TransactionOutput, UTxO, Value, VerificationKeyHash

from conftest import AP3X, TEST_PARAMS_JSON, FakeChain, Key, T0, h28, h32, tokens, txin
from vector_jury import builders, documents as D, module1, rules, state
from vector_jury import types as T
from vector_jury.deployment import Deployment, encode_params, params_from_json, script_address

CH, CL = h28(b"m1 challenge"), h28(b"m1 claim")


class Koios:
    def __init__(self):
        self.metadata, self.cbor, self.info, self.assets, self.mints = {}, {}, {}, {}, {}

    def tx_metadata(self, txs):
        return [{"tx_hash": t, "metadata": self.metadata.get(t)} for t in txs if t in self.metadata or t in self.cbor]

    def tx_cbor(self, txs):
        return [{"tx_hash": t, "cbor": self.cbor.get(t)} for t in txs]

    def tx_info(self, txs, full=False):
        return [self.info[t] for t in txs if t in self.info]

    def policy_asset_list(self, policy):
        return [{"asset_name": n.hex()} for n in self.assets.get(policy, [])]

    def asset_history(self, policy, name):
        return [{"minting_txs": [{"tx_hash": t, "quantity": "1", "block_time": 1} for t in self.mints.get(name, [])]}]


def test_reader_and_writer_rules():
    with pytest.raises(D.DocumentError, match="U\\+0000"):
        D.metadata(b'{"a": "\\u0000"}', "claim")
    with pytest.raises(D.DocumentError, match="U\\+0000"):
        D.metadata(b"a\x00b", "claim")
    for field in ("t", "m"):
        value = {"v": 1, "t": "claim", "m": "application/json", "d": ["ok"], field: "0xabc"}
        with pytest.raises(D.DocumentError, match="0x"):
            D.parse_metadata({"22084": value})
    value = {"v": 1, "t": "claim", "m": "application/json", "d": ["ok"], "extra": 5}
    assert D.parse_metadata({"22084": value}).data == b"ok"


def test_cbor_fallback_and_record_lookup():
    koios = Koios()
    record = b'{"type": "vector-jury-record/1"}'
    raw = D.auxiliary_data(record, "juror-record").to_cbor()
    reveal, overflow = h32(b"reveal").hex(), h32(b"overflow").hex()
    koios.metadata[reveal] = None                                    # no JSON, no CBOR, Blockfrost offline
    koios.cbor[overflow] = raw.hex() if isinstance(raw, bytes) else raw
    assert D.read_document(overflow, koios=koios).data == record     # CBOR fallback
    found = D.find_record(D.doc_hash(record), reveal_tx=reveal, candidate_txs=[overflow], koios=koios)
    assert found.where == "overflow" and found.data == record and found.tx_id == overflow
    koios.metadata[reveal] = {"22084": D.metadatum(record, "juror-record")}
    assert D.find_record(D.doc_hash(record), reveal_tx=reveal, koios=koios).where == "reveal"
    assert D.find_record(h32(b"other"), reveal_tx=reveal, koios=koios).where == "missing"


def m1():
    return module1.Module1(None, {}, "mainnet", CL, CH, Key(b"oracle").hash)


TOKEN = b"chl_" + h28(b"t")
DATUM = T.ChallengeDatum((h32(b"claim tx"), 0), h32(b"auditor"), T.M1Credential(False, h28(b"a")), 60 * AP3X,
                         h32(b"evidence 0"), b"vector-tx:" + b"A" * 43, 1_000, 259_200_000, (), 0,
                         b"clm_" + h28(b"claim"))


def entry(tx_index, datum=DATUM, token=TOKEN, value=None, cred=CH, tx_hash=None):
    out = {"tx_index": tx_index, "payment_addr": {"cred": cred.hex()}, "value": str(value or datum.stake_amount),
           "asset_list": [{"policy_id": CH.hex(), "asset_name": token.hex(), "quantity": "1"}],
           "inline_datum": {"bytes": datum.cbor().hex()}}
    if tx_hash is not None:
        out["tx_hash"] = tx_hash
    return out


def evidence_step(koios, name, spent_tx, *, datum, outputs=None, constructor=1):
    tx = h32(name).hex()
    koios.info[tx] = {"tx_hash": tx, "assets_minted": [], "outputs": outputs or [entry(0, datum)],
                      "inputs": [entry(0, koios_datum(koios, spent_tx), tx_hash=spent_tx)],
                      "plutus_contracts": [{"spends_input": {"tx_hash": spent_tx, "tx_index": 0},
                                            "input": {"redeemer": {"datum": {"value": {
                                                "constructor": constructor,
                                                "fields": [{"bytes": "00"}, {"bytes": "01"}]}}}}}]}
    return tx


def koios_datum(koios, tx):
    return T.decode_challenge_datum(bytes.fromhex(koios.info[tx]["outputs"][0]["inline_datum"]["bytes"]))


def test_genuine_challenges():
    chain = FakeChain(T0)
    chain.koios = Koios()
    k = chain.koios
    made = h32(b"made").hex()
    k.info[made] = {"tx_hash": made, "outputs": [entry(0)],
                    "assets_minted": [{"policy_id": CH.hex(), "asset_name": TOKEN.hex(), "quantity": "1"}]}
    assert module1.genuine_token(chain, m1(), f"{made}#0") == TOKEN
    # Three SubmitEvidence steps: any depth.
    tx = made
    for i in range(1, 4):
        tx = evidence_step(k, b"step %d" % i, tx, datum=dataclasses.replace(DATUM, evidence_hash=h32(b"e%d" % i)))
    assert module1.is_genuine(chain, m1(), f"{tx}#0")
    # A changed field other than the evidence: not genuine.
    changed = evidence_step(k, b"changed", made, datum=dataclasses.replace(DATUM, resolution_deadline=1))
    assert not module1.is_genuine(chain, m1(), f"{changed}#0")
    # Lovelace other than the stake_amount: not genuine.
    padded = evidence_step(k, b"padded", made, datum=DATUM, outputs=[entry(0, DATUM, value=61 * AP3X)])
    assert not module1.is_genuine(chain, m1(), f"{padded}#0")
    # The token in a second output at the challenge address (amendment 5).
    second = evidence_step(k, b"second", made, datum=DATUM, outputs=[
        {"tx_index": 0, "payment_addr": {"cred": CH.hex()}, "value": "2000000", "asset_list": []}, entry(1)])
    assert not module1.is_genuine(chain, m1(), f"{second}#1")
    # Another redeemer than SubmitEvidence.
    other = evidence_step(k, b"other redeemer", made, datum=DATUM, constructor=2)
    assert not module1.is_genuine(chain, m1(), f"{other}#0")
    # No mint and no genuine input: not genuine. Two mints: not genuine.
    forged = h32(b"forged").hex()
    k.info[forged] = {"tx_hash": forged, "outputs": [entry(0)], "assets_minted": []}
    assert not module1.is_genuine(chain, m1(), f"{forged}#0")
    two = h32(b"two").hex()
    k.info[two] = {"tx_hash": two, "outputs": [entry(0)], "assets_minted": [
        {"policy_id": CH.hex(), "asset_name": TOKEN.hex(), "quantity": "1"},
        {"policy_id": CH.hex(), "asset_name": (b"chl_" + h28(b"x")).hex(), "quantity": "1"}]}
    assert not module1.is_genuine(chain, m1(), f"{two}#0")


def test_genuineness_of_a_transaction_that_koios_does_not_show_yet_is_not_cached():
    chain = FakeChain(T0)
    chain.koios = Koios()
    made = h32(b"late").hex()
    cache = {}
    assert module1.genuine_token(chain, m1(), f"{made}#0", cache=cache) is None
    assert cache == {}
    chain.koios.info[made] = {"tx_hash": made, "outputs": [entry(0)],
                              "assets_minted": [{"policy_id": CH.hex(), "asset_name": TOKEN.hex(), "quantity": "1"}]}
    assert module1.genuine_token(chain, m1(), f"{made}#0", cache=cache) == TOKEN
    assert cache == {f"{made}#0": TOKEN}


def test_stake_problems():
    claim = T.ClaimDatum(h32(b"c"), T.M1Credential(False, h28(b"k")), h32(b"h"), b"x", b"u", 50 * AP3X, 1, 2, 1)
    good = UTxO(txin(b"claim"), TransactionOutput(script_address(CL), Value(50 * AP3X, tokens(CL, b"clm_" + h28(b"t"))),
                                                  datum=claim.cbor()))
    padded = UTxO(txin(b"padded"), TransactionOutput(script_address(CL), Value(51 * AP3X), datum=claim.cbor()))
    assert module1.stake_problems(good) == []
    assert "stake_amount" in module1.stake_problems(padded)[0]


def deployment(**windows):
    params = params_from_json(TEST_PARAMS_JSON)
    return Deployment("mainnet", params, encode_params(params), h28(b"jury"), script_address(h28(b"jury")),
                      txin(b"ref"), None, {}, "", **windows)


def test_dispute_cases_and_canonical_case(monkeypatch):
    d = deployment(cases_from=1_000, cases_until=2_000)
    assert d.current_for(1_000) and not d.current_for(2_000) and not d.current_for(999)
    assert state.deployment_for([d], 5_000) is None
    chain = FakeChain(T0)
    chain.koios = Koios()
    monkeypatch.setattr(type(chain), "block_height", lambda self: 120, raising=False)
    # The challenge_out "ff…" is not genuine; "ee…" is genuine but holds another token.
    monkeypatch.setattr(module1, "genuine_token", lambda chain, m1, ref, cache=None:
                        None if ref.startswith("ff") else (h32(b"other") if ref.startswith("ee") else h32(b"ch")))
    claim = h32(b"claim token")

    def case(tag, height, challenge_out=h32(b"genuine")):
        token = rules.case_token_name((h32(tag), 0))
        datum = T.Case(h32(b"ch"), claim, T.OutputReference(challenge_out, 0), T.OutputReference(h32(b"cl"), 0),
                       h32(b"a"), h32(b"b"), h32(b"h"), h32(b"e"), 9, 1, 2, 3,
                       T.plutus_address(Address(VerificationKeyHash(h28(b"r")), network=Network.MAINNET)), 0, [],
                       T.Tally(0, 0, 0), T.Open())
        tx = h32(b"open " + tag).hex()
        chain.koios.assets.setdefault(d.script_hash.hex(), []).append(token)
        chain.koios.mints[token.hex()] = [tx]
        chain.koios.info[tx] = {"tx_hash": tx, "block_height": height, "outputs": [{
            "asset_list": [{"policy_id": d.script_hash.hex(), "asset_name": token.hex(), "quantity": "1"}],
            "inline_datum": {"bytes": T.to_cbor(datum).hex()}}]}
        return token

    later = case(b"later", 105)
    first = case(b"first", 100)
    case(b"fake", 90, challenge_out=b"\xff" * 32)
    case(b"wrong token", 80, challenge_out=b"\xee" * 32)
    assert [o.token for o in state.dispute_cases(chain, [d], claim, 1_500, m1())] == [first, later]
    found = state.canonical_for(chain, [d], claim, 1_500, m1())
    assert found.opening.token == first and found.deep_enough and found.live is None
    monkeypatch.setattr(type(chain), "block_height", lambda self: 105, raising=False)
    assert not state.canonical_for(chain, [d], claim, 1_500, m1()).deep_enough
    assert state.canonical_for(chain, [d], claim, 5_000, m1()) is None


def test_refund_to_rule():
    jury = h28(b"jury")
    key = Address(VerificationKeyHash(h28(b"k")), network=Network.MAINNET)
    assert builders.refund_to_problems(key, jury) == []
    assert builders.refund_to_problems(Address(ScriptHash(jury), network=Network.MAINNET), jury)
    assert builders.refund_to_problems(Address(VerificationKeyHash(h28(b"k")), PointerAddress(1, 2, 3),
                                               network=Network.MAINNET), jury)
    # Amendment 7: not the payment credential of a party.
    assert builders.refund_to_problems(key, jury, (T.M1Credential(False, h28(b"k")),))
    assert builders.refund_to_problems(key, jury, (T.M1Credential(True, h28(b"k")),)) == []


def test_resolution_basis_no_jury():
    token = b"chl_" + h28(b"r")
    utxo = UTxO(txin(b"ch"), TransactionOutput(script_address(CH), Value(60 * AP3X, tokens(CH, token)),
                                               datum=DATUM.cbor()))
    item = module1.ChallengeOutput(utxo, token, DATUM)
    assert b'"basis":"no-jury"' in module1.resolution_document(item, "Inconclusive", "no-jury")
