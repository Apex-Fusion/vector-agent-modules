"""Chain helpers, the jury state (§11 canonical case), eligibility and the
commit store."""

import dataclasses
import os
import stat

from pycardano import Address, TransactionOutput, UTxO, Value

from conftest import AP3X, TEST_PARAMS_JSON, FakeChain, T0, h28, h32, tokens, txin
from vector_jury import rules, state
from vector_jury import types as T
from vector_jury.chain import TIME_BASES, TimeBase
from vector_jury.commit_store import CommitStore, make_attempt
from vector_jury.deployment import Deployment, encode_params, params_from_json, script_address


def deployment(tag=b"jury") -> Deployment:
    params = params_from_json(TEST_PARAMS_JSON)
    script_hash = h28(tag)
    return Deployment("mainnet", params, encode_params(params), script_hash, script_address(script_hash),
                      txin(b"ref " + tag), None, {}, "")


def test_time_base():
    mainnet = TIME_BASES["mainnet"]
    assert mainnet.slot_to_ms(0) == 1756485600000
    assert mainnet.slot_to_ms(37_701_463) == 1756485600000 + 37_701_463_000
    assert mainnet.ms_to_slot(mainnet.slot_to_ms(100) + 999) == 100
    assert mainnet.ms_to_slot(mainnet.slot_to_ms(100) + 1, ceil=True) == 101
    assert mainnet.ms_to_slot(mainnet.slot_to_ms(100), ceil=True) == 100
    assert TimeBase(1000, zero_slot=10, slot_length_ms=500).slot_to_ms(12) == 2000


def juror_datum(agent=b"a"):
    return T.Juror(h32(agent), h28(b"owner"), h32(b"prompt"), 1, T.Nothing(), [])


def case_datum(challenge=b"challenge"):
    return T.Case(h32(challenge), h32(b"k"), T.OutputReference(h32(b"ch out"), 0), T.OutputReference(h32(b"cl out"), 0),
                  h32(b"cl"), h32(b"au"), h32(b"h"), h32(b"e"), 9, 1, 2, 3,
                  T.plutus_address(Address.from_primitive(TEST_PARAMS_JSON["treasury"])), 0, [], T.Tally(0, 0, 0),
                  T.Open())


def jury_utxo(d: Deployment, tag: bytes, token: bytes, datum, *, extra=None, address=None) -> UTxO:
    assets = tokens(d.script_hash, token)
    if extra is not None:
        assets += extra
    return UTxO(txin(tag), TransactionOutput(address or d.address, Value(20 * AP3X, assets), datum=datum))


def test_read_outputs_skips_foreign_outputs():
    d = deployment()
    chain = FakeChain(T0)
    juror_token = rules.juror_token_name((h32(b"s1"), 0))
    case_token = rules.case_token_name((h32(b"s2"), 0))
    chain.add(
        jury_utxo(d, b"juror", juror_token, juror_datum()),
        jury_utxo(d, b"case", case_token, case_datum()),
        jury_utxo(d, b"swapped", juror_token, case_datum()),                      # prefix and datum differ
        jury_utxo(d, b"extra", case_token, case_datum(), extra=tokens(h28(b"x"), b"x")),
        UTxO(txin(b"plain"), TransactionOutput(d.address, Value(5 * AP3X))),        # no token
        jury_utxo(d, b"bad datum", juror_token, T.Tally(1, 2, 3)),
    )
    jurors, cases = state.read_outputs(chain, d)
    assert [j.token for j in jurors] == [juror_token] and [c.token for c in cases] == [case_token]
    assert jurors[0].lovelace == 20 * AP3X and jurors[0].agent == h32(b"a")
    assert state.find_token(chain, [d], case_token).ref == cases[0].ref
    assert state.find_juror(chain, d, case_token) is None


def test_canonical_case():
    a, b = deployment(b"jury a"), deployment(b"jury b")
    chain = FakeChain(T0)
    tokens_by = {}
    for name, d in ((b"early", a), (b"late", b), (b"same block", b), (b"unknown", a)):
        token = rules.case_token_name((h32(name), 0))
        tokens_by[name] = token
        chain.add(jury_utxo(d, name, token, case_datum()))
        if name != b"unknown":
            chain.koios.mints[(d.script_hash.hex(), token.hex())] = [h32(b"open " + name).hex()]
    chain.koios.heights = {h32(b"open early").hex(): 100, h32(b"open late").hex(): 105,
                           h32(b"open same block").hex(): 100}
    canonical = state.canonical_cases(chain, [a, b])
    expected = min(tokens_by[b"early"], tokens_by[b"same block"])
    assert canonical[h32(b"challenge")].token == expected
    # Without any known opening there is no canonical case yet.
    lonely = deployment(b"jury c")
    chain.add(jury_utxo(lonely, b"lonely", rules.case_token_name((h32(b"lonely"), 0)), case_datum(b"other")))
    assert h32(b"other") not in state.canonical_cases(chain, [lonely])
    # Openings are cached.
    openings = state.Openings(chain)
    case = state.list_cases(chain, a)[0]
    first = openings.of(case)
    chain.koios.heights.clear()
    assert openings.of(case) == first


def test_eligibility():
    d = deployment()
    params = d.params
    juror_token = rules.juror_token_name((h32(b"s1"), 0))
    case_token = rules.case_token_name((h32(b"s2"), 0))
    datum = dataclasses.replace(case_datum(), commit_end=10**13)
    case = state.CaseOutput(d, jury_utxo(d, b"case", case_token, datum), case_token, datum)
    bonded = UTxO(txin(b"bonded"), TransactionOutput(d.address, Value(params.bond_min,
                                                                      tokens(d.script_hash, juror_token))))
    juror = state.JurorOutput(d, bonded, juror_token, juror_datum())
    assert state.eligibility(juror, case, 1 + params.eligibility_delay) == []
    assert any("can vote from" in p for p in state.eligibility(juror, case, params.eligibility_delay))
    case_elsewhere = state.CaseOutput(deployment(b"other jury"), case.utxo, case.token, case.datum)
    assert any("different deployments" in p for p in state.eligibility(juror, case_elsewhere,
                                                                        1 + params.eligibility_delay))


def test_commit_store(tmp_path):
    root = tmp_path / "state" / "commits"
    store = CommitStore(root)
    case, juror, jury = rules.case_token_name((h32(b"c"), 0)), rules.juror_token_name((h32(b"j"), 0)), h28(b"jury")
    record = b'{"vote": "AuditorWins"}'
    attempt = make_attempt(case, h32(b"agent"), "AuditorWins", record, created="now")
    path = store.add("mainnet", jury, case, juror, attempt)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    for directory in (root, root / "mainnet", path.parent.parent, path.parent):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700
    again = store.find("mainnet", jury, case, juror, attempt.commit)
    assert again == attempt and again.record_hash == attempt.record_hash
    assert attempt.commit == rules.commit_hash(case, h32(b"agent"), "AuditorWins", attempt.salt, attempt.record_hash)
    second = make_attempt(case, h32(b"agent"), "ClaimerWins", record)
    store.add("mainnet", jury, case, juror, second)
    assert len(store.attempts("mainnet", jury, case, juror)) == 2
    assert store.matching("mainnet", jury, case, juror, "AuditorWins", record) == attempt
    store.mark_sent("mainnet", jury, case, juror, second.commit, "ab" * 32)
    assert store.find("mainnet", jury, case, juror, second.commit).tx == "ab" * 32
    assert store.attempts("mainnet", jury, case, h28(b"none")) == []
