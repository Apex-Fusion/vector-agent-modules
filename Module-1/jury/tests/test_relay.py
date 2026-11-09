"""Tests of the relay (``vector_jury.relay``, SPEC §12): every branch of the
decisions of §12.2 with times around the boundaries, the transaction log,
the Finalize lookup, and the execution guards (one OracleResolve per
challenge, ``hi <= D``, the Finalize fallback)."""

import json
import os
import stat

import cbor2
import pytest
from pycardano import (Address, Network, RawCBOR, Transaction, TransactionBody, TransactionOutput,
                       TransactionWitnessSet, UTxO, Value, VerificationKeyHash)

from conftest import AP3X, TEST_PARAMS_JSON, h28, h32, tokens, txin
from vector_jury import module1, relay, state
from vector_jury import types as T
from vector_jury.chain import TIME_BASES
from vector_jury.deployment import Deployment, encode_params, params_from_json, script_address

MIN = 60_000
HOUR = 60 * MIN
P = params_from_json(TEST_PARAMS_JSON)
J = h28(b"test jury deployment")
DEP = Deployment(network="mainnet", params=P, params_cbor=encode_params(P), script_hash=J, address=script_address(J),
                 reference_script=txin(b"jury ref"), plutus=None, txs={}, time="", test=True)
OPERATOR = h28(b"operator key")
RELAY = Address(VerificationKeyHash(OPERATOR), network=Network.MAINNET)
OTHER = Address(VerificationKeyHash(h28(b"somebody")), network=Network.MAINNET)
BASE = TIME_BASES["mainnet"]
T0 = BASE.slot_to_ms(38_000_000)
FOUNDATION = h32(b"foundation juror")


def slot(ms):
    return BASE.ms_to_slot(ms)


def challenge(*, challenged_at=T0, deadline=None, token=h32(b"challenge token"), claim_token=h32(b"claim token")):
    deadline = deadline if deadline is not None else challenged_at + 72 * HOUR
    datum = T.ChallengeDatum(claim_ref=(h32(b"claim ref"), 0), auditor_did=h32(b"auditor"),
                             auditor_credential=T.M1Credential(False, h28(b"auditor key")), stake_amount=60 * AP3X,
                             evidence_hash=h32(b"evidence"), evidence_uri=b"vector-tx:" + b"E" * 43,
                             challenged_at=challenged_at, resolution_deadline=deadline - challenged_at,
                             eligible_jurors=(), state=0, claim_token=claim_token)
    utxo = UTxO(txin(b"challenge out"), TransactionOutput(script_address(P.m1_challenge),
                                                          Value(60 * AP3X, tokens(P.m1_challenge, token)),
                                                          datum=RawCBOR(datum.cbor())))
    return module1.ChallengeOutput(utxo, token, datum)


def claim(token=h32(b"claim token")):
    datum = T.ClaimDatum(claimer_did=h32(b"claimer"), claimer_credential=T.M1Credential(False, h28(b"claimer key")),
                         claim_hash=h32(b"claim doc"), claim_type=b"service-quality", storage_uri=b"vector-tx:c",
                         stake_amount=50 * AP3X, submitted_at=T0 - HOUR, challenge_window=3_600_000, state=1)
    utxo = UTxO(txin(b"claim out"), TransactionOutput(script_address(P.m1_claim),
                                                      Value(50 * AP3X, tokens(P.m1_claim, token)),
                                                      datum=RawCBOR(datum.cbor())))
    return module1.ClaimOutput(utxo, token, datum)


def case_datum(*, opened_at, deadline, refund=RELAY, tally=(0, 0, 0), state_=None, fee=0, ballots=(),
               claim_token=h32(b"claim token"), challenge_token=h32(b"challenge token")):
    commit_end = opened_at + P.commit_window
    return T.Case(challenge=challenge_token, claim=claim_token,
                  challenge_out=T.OutputReference(h32(b"challenge out tx"), 0),
                  claim_out=T.OutputReference(h32(b"claim out tx"), 0), claimer=h32(b"claimer"),
                  auditor=h32(b"auditor"), claim_hash=h32(b"claim doc"), evidence_hash=h32(b"evidence"),
                  deadline=deadline, opened_at=opened_at, commit_end=commit_end,
                  reveal_end=commit_end + P.reveal_window, refund_to=T.plutus_address(refund), fee=fee,
                  ballots=list(ballots), tally=T.Tally(*tally), state=state_ or T.Open())


def case_output(datum, token=h32(b"case token"), tag=b"case utxo", deployment=DEP):
    utxo = UTxO(txin(tag), TransactionOutput(deployment.address, Value(10 * AP3X, tokens(J, token)),
                                             datum=T.to_cbor(datum)))
    return state.CaseOutput(deployment, utxo, token, datum)


def canonical(datum, *, deep=True, live=True, token=h32(b"case token")):
    opening = state.CaseOpening(DEP, token, "aa" * 32, 100, datum)
    return state.Canonical(DEP, opening, deep, case_output(datum, token) if live else None)


def dispute(*, challenged_at=T0, deadline=None, deployment=DEP, canonical_=None, case_known=False,
            finalized=None, stake_problems=(), with_claim=True):
    ch = challenge(challenged_at=challenged_at, deadline=deadline)
    return relay.Dispute(ch, claim() if with_claim else None, deployment, canonical_, case_known, finalized,
                         list(stake_problems))


def world(now, disputes=(), cases=(), jurors=(), verdicts=()):
    return relay.World(now, slot(now), 1000, list(disputes), list(cases), list(jurors), list(verdicts))


@pytest.fixture
def journal(tmp_path):
    return relay.Journal(tmp_path / "relay" / "txlog.jsonl")


def run(w, journal, agents=(FOUNDATION,)):
    return relay.decide(w, address=RELAY, operator_vkh=OPERATOR, foundation_agents=agents, journal=journal)


def kinds(actions):
    return [a.kind for a in actions]


# ── §12.2 step 2: no case ────────────────────────────────────────────────────


def test_open_waits_for_the_grace_period(journal):
    actions, notes = run(world(T0 + 30 * MIN - 1, [dispute()]), journal)
    assert actions == [] and "grace period" in notes[0].message
    actions, _ = run(world(T0 + 30 * MIN, [dispute()]), journal)
    assert kinds(actions) == ["open"]


def test_open_at_once_when_the_deadline_is_close(journal):
    now = T0 + MIN
    actions, _ = run(world(now, [dispute(deadline=now + 6 * HOUR + 40 * MIN)]), journal)
    assert kinds(actions) == ["open"]
    actions, notes = run(world(now, [dispute(deadline=now + 6 * HOUR + 40 * MIN + 1)]), journal)
    assert actions == [] and "grace" in notes[0].message


def test_no_time_when_no_case_can_finish(journal):
    now = T0 + HOUR
    actions, _ = run(world(now, [dispute(deadline=now + 6 * HOUR + 10 * MIN)]), journal)
    assert kinds(actions) == ["open"]
    actions, _ = run(world(now, [dispute(deadline=now + 6 * HOUR + 10 * MIN - 1)]), journal)
    assert kinds(actions) == ["resolve"]
    assert actions[0].resolution == {"verdict": "Inconclusive", "basis": "no-time", "jury": None}


def test_no_resolution_within_ten_minutes_of_the_deadline(journal):
    now = T0 + HOUR
    actions, _ = run(world(now, [dispute(deadline=now + 10 * MIN)]), journal)
    assert kinds(actions) == ["resolve"]
    actions, notes = run(world(now, [dispute(deadline=now + 10 * MIN - 1)]), journal)
    assert actions == [] and "less than 10 minutes" in notes[0].message


def test_no_jury_when_no_deployment_covers_the_challenge(journal):
    actions, _ = run(world(T0 + HOUR, [dispute(deployment=None)]), journal)
    assert kinds(actions) == ["resolve"]
    assert actions[0].resolution == {"verdict": "Inconclusive", "basis": "no-jury", "jury": None}


def test_a_known_case_blocks_a_second_open(journal):
    actions, notes = run(world(T0 + HOUR, [dispute(case_known=True)]), journal)
    assert actions == [] and "not canonical yet" in notes[0].message
    datum = case_datum(opened_at=T0 + 40 * MIN, deadline=T0 + 72 * HOUR)
    actions, notes = run(world(T0 + HOUR, [dispute(canonical_=canonical(datum, deep=False))]), journal)
    assert actions == [] and "blocks deep" in notes[0].message


def test_stake_mismatch_blocks_a_resolution(journal):
    now = T0 + HOUR
    actions, notes = run(world(now, [dispute(deadline=now + HOUR, stake_problems=["claim holds 1"])]), journal)
    assert actions == [] and "stakes do not match" in notes[0].message


# ── §12.2 step 3: the canonical case ─────────────────────────────────────────


def test_canonical_case_votes_then_finalizes(journal):
    datum = case_datum(opened_at=T0 + 40 * MIN, deadline=T0 + 72 * HOUR)
    d = dispute(canonical_=canonical(datum))
    actions, notes = run(world(datum.reveal_end - 1, [d]), journal)
    assert actions == [] and "votes until" in notes[0].message
    actions, _ = run(world(datum.reveal_end, [d]), journal)
    assert kinds(actions) == ["finalize"] and actions[0].case.token == h32(b"case token")


@pytest.mark.parametrize("tally,verdict,basis", [((3, 0, 0), "ClaimerWins", "jury"), ((1, 2, 0), "AuditorWins", "jury"),
                                                 ((2, 1, 1), "Inconclusive", "no-result"),
                                                 ((2, 0, 0), "Inconclusive", "no-result")])
def test_resolution_from_the_finalized_tally(journal, tally, verdict, basis):
    datum = case_datum(opened_at=T0 + 40 * MIN, deadline=T0 + 72 * HOUR)
    final = relay.Finalized("bb" * 32, 200, T.Tally(*tally), True)
    d = dispute(canonical_=canonical(datum, live=False), finalized=final)
    actions, _ = run(world(datum.reveal_end + HOUR, [d]), journal)
    assert kinds(actions) == ["resolve"]
    assert actions[0].resolution == {"verdict": verdict, "basis": basis, "jury": {
        "deployment": J.hex(), "case": h32(b"case token").hex(), "opened_in": "aa" * 32, "finalized_in": "bb" * 32,
        "tally": {"ClaimerWins": tally[0], "AuditorWins": tally[1], "Inconclusive": tally[2]}}}


def test_resolution_waits_for_ten_blocks_and_the_indexer(journal):
    datum = case_datum(opened_at=T0 + 40 * MIN, deadline=T0 + 72 * HOUR)
    later = datum.reveal_end + HOUR
    actions, notes = run(world(later, [dispute(canonical_=canonical(datum, live=False))]), journal)
    assert actions == [] and "indexer" in notes[0].message
    shallow = relay.Finalized("bb" * 32, 995, T.Tally(3, 0, 0), False)
    actions, notes = run(world(later, [dispute(canonical_=canonical(datum, live=False), finalized=shallow)]), journal)
    assert actions == [] and "blocks deep" in notes[0].message


def test_fallback_resolution_without_finalize(journal):
    datum = case_datum(opened_at=T0 + 40 * MIN, deadline=T0 + 72 * HOUR, tally=(0, 3, 0))
    fallback = relay.Finalized(None, 300, T.Tally(0, 3, 0), True)
    actions, _ = run(world(datum.reveal_end + HOUR, [dispute(canonical_=canonical(datum), finalized=fallback)]),
                     journal)
    assert kinds(actions) == ["resolve"]
    assert actions[0].resolution["verdict"] == "AuditorWins"
    assert actions[0].resolution["jury"]["finalized_in"] is None


def test_jury_resolution_keeps_the_ten_minute_guard(journal):
    deadline = T0 + 72 * HOUR
    datum = case_datum(opened_at=T0 + 40 * MIN, deadline=deadline)
    final = relay.Finalized("bb" * 32, 200, T.Tally(3, 0, 0), True)
    d = dispute(deadline=deadline, canonical_=canonical(datum, live=False), finalized=final)
    assert kinds(run(world(deadline - 10 * MIN, [d]), journal)[0]) == ["resolve"]
    actions, notes = run(world(deadline - 10 * MIN + 1, [d]), journal)
    assert actions == [] and "TimeoutResolve after the deadline" in notes[0].message


# ── one OracleResolve per challenge, and §12.2 step 4 ───────────────────────


def test_never_a_second_oracle_resolve(journal):
    d = dispute(deadline=T0 + 5 * HOUR)
    journal.append({"action": "resolve", "tx": "cc" * 32, "challenge": d.token.hex(), "status": "rejected",
                    "ttl_slot": slot(T0 + 2 * HOUR)})
    assert kinds(run(world(T0 + HOUR, [d]), journal)[0]) == ["resolve"]
    journal.append({"action": "resolve", "tx": "dd" * 32, "challenge": d.token.hex(), "status": "sending",
                    "ttl_slot": slot(T0 + 2 * HOUR)})
    actions, notes = run(world(T0 + HOUR, [d]), journal)
    assert actions == [] and "never signs a second" in notes[0].message


def test_timeout_after_the_deadline(journal):
    deadline = T0 + 5 * HOUR
    d = dispute(deadline=deadline)
    assert run(world(deadline, [d]), journal)[0] == []  # at D: not after D yet
    assert kinds(run(world(deadline + 1, [d]), journal)[0]) == ["timeout"]
    journal.append({"action": "resolve", "tx": "ee" * 32, "challenge": d.token.hex(), "status": "submitted",
                    "ttl_slot": slot(deadline + 1000)})
    actions, notes = run(world(deadline + 1, [d]), journal)
    assert actions == [] and "can still land" in notes[0].message
    assert kinds(run(world(deadline + 1001 * 1000, [d]), journal)[0]) == ["timeout"]
    bad = dispute(deadline=deadline, stake_problems=["challenge holds 1"])
    actions, notes = run(world(deadline + 2_000_000, [bad]), journal)
    assert actions == [] and "stakes do not match" in notes[0].message


def test_read_errors_and_missing_claims_do_nothing(journal):
    broken = dispute()
    broken.error = "ChainError: Koios down"
    actions, notes = run(world(T0 + HOUR, [broken, dispute(with_claim=False)]), journal)
    assert actions == [] and "read failed" in notes[0].message and "claim output" in notes[1].message


# ── steps 5-7 and verdicts ───────────────────────────────────────────────────


def juror_output(*, agent=FOUNDATION, owner=OPERATOR, open_=(), token=h32(b"juror token")):
    datum = T.Juror(agent=agent, owner=owner, prompt_hash=h32(b"prompt"), registered_at=T0 - 10 * 24 * HOUR,
                    exit_after=T.Nothing(), open=list(open_))
    utxo = UTxO(txin(b"juror utxo" + token), TransactionOutput(DEP.address, Value(250 * AP3X, tokens(J, token)),
                                                               datum=T.to_cbor(datum)))
    return state.JurorOutput(DEP, utxo, token, datum)


def test_slash_one_minute_after_reveal_end(journal):
    reveal_end = T0 + 5 * HOUR
    j = juror_output(agent=h32(b"other juror"), owner=h28(b"other owner"),
                     open_=[T.OpenVote(h32(b"some case"), reveal_end)])
    assert run(world(reveal_end + MIN - 1, jurors=[j]), journal)[0] == []
    actions, _ = run(world(reveal_end + MIN, jurors=[j]), journal)
    assert kinds(actions) == ["slash"] and actions[0].case_token == h32(b"some case")


def test_settling_claims_then_closes(journal):
    token = h32(b"juror token")
    ballot = T.Ballot(FOUNDATION, token, b"", T.Revealed(T.vote("ClaimerWins")))
    other = T.Ballot(h32(b"x"), h32(b"x token"), b"", T.Paid(T.vote("ClaimerWins")))
    datum = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR, fee=30 * AP3X, ballots=[ballot, other],
                       state_=T.Settling(10 * AP3X), tally=(2, 0, 0))
    case = case_output(datum)
    actions, _ = run(world(datum.reveal_end + MIN, cases=[case], jurors=[juror_output()]), journal)
    assert kinds(actions) == ["claim"] and actions[0].juror.token == token
    # A juror of somebody else: no claim; Close waits for the claim period.
    actions, _ = run(world(datum.reveal_end + MIN, cases=[case], jurors=[juror_output(owner=h28(b"x"))]), journal)
    assert actions == []
    actions, _ = run(world(datum.reveal_end + P.claim_period, cases=[case]), journal)
    assert kinds(actions) == ["close"]
    paid = case_output(case_datum(opened_at=T0, deadline=T0 + 72 * HOUR, fee=30 * AP3X, ballots=[other],
                                  state_=T.Settling(10 * AP3X), tally=(1, 0, 0)))
    assert kinds(run(world(datum.reveal_end + MIN, cases=[paid]), journal)[0]) == ["close"]


def test_own_cases_that_are_not_canonical_are_finalized(journal):
    datum = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR)
    own = case_output(datum, h32(b"own case"), b"own")
    foreign = case_output(case_datum(opened_at=T0, deadline=T0 + 72 * HOUR, refund=OTHER), h32(b"x case"), b"x")
    actions, _ = run(world(datum.reveal_end, cases=[own, foreign]), journal)
    assert kinds(actions) == ["finalize_own"] and actions[0].case.token == h32(b"own case")
    assert run(world(datum.reveal_end - 1, cases=[own]), journal)[0] == []
    # The canonical case is finalized once, by step 3.
    can = canonical(datum, token=h32(b"own case"))
    actions, _ = run(world(datum.reveal_end, [dispute(canonical_=can)], cases=[can.live]), journal)
    assert kinds(actions) == ["finalize"]


def test_cases_that_refund_to_the_treasury_count_as_own(journal):
    # SPEC §12.2 step 2: the relay opens its cases with refund_to = the treasury.
    datum = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR, refund=DEP.treasury_address)
    case = case_output(datum, h32(b"treasury case"), b"treasury")
    actions, _ = run(world(datum.reveal_end, cases=[case]), journal)
    assert kinds(actions) == ["finalize_own"] and actions[0].case.token == h32(b"treasury case")


def verdict(expires_at, problems=()):
    from simulation import identity_party as ip

    item = ip.IdentityVerdict(agent=h32(b"claimer"), action=ip.VerdictAction.SUSPEND, duration=7 * 24 * HOUR,
                              challenge_tx_id=h32(b"c"), challenge_index=0,
                              deposit_to=ip.PlutusAddress(ip.PlutusCredential(OPERATOR)), expires_at=expires_at)
    return relay.VerdictOutput(UTxO(txin(b"verdict"), TransactionOutput(RELAY, Value(2 * AP3X))), item, list(problems))


def test_verdicts_are_applied_or_reclaimed(journal):
    now = T0 + HOUR
    assert kinds(run(world(now, verdicts=[verdict(now + 30 * 24 * HOUR)]), journal)[0]) == ["apply_verdict"]
    actions, notes = run(world(now, verdicts=[verdict(now + HOUR, ["no Module-1 party"])]), journal)
    assert actions == [] and "no Module-1 party" in notes[0].message
    assert run(world(now, verdicts=[verdict(now - 2 * MIN)]), journal)[0] == []
    assert kinds(run(world(now, verdicts=[verdict(now - 2 * MIN - 1)]), journal)[0]) == ["reclaim_verdict"]


def test_priority_order(journal):
    datum = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR)
    own = case_output(datum, h32(b"own case"), b"own")
    slashable = juror_output(agent=h32(b"o"), owner=h28(b"o"), open_=[T.OpenVote(h32(b"c"), T0)], token=h32(b"j2"))
    late = dispute(deadline=datum.reveal_end + 5 * HOUR)
    actions, _ = run(world(datum.reveal_end, [late], cases=[own], jurors=[slashable],
                           verdicts=[verdict(datum.reveal_end + HOUR)]), journal)
    assert kinds(actions) == ["resolve", "apply_verdict", "finalize_own", "slash"]


# ── journal ──────────────────────────────────────────────────────────────────


def test_journal_is_durable_and_merges_entries(tmp_path):
    path = tmp_path / "relay" / "txlog.jsonl"
    log = relay.Journal(path)
    log.append({"action": "resolve", "tx": "ab" * 32, "challenge": "cd" * 32, "status": "sending", "ttl_slot": 5})
    log.append({"action": "resolve", "tx": "ab" * 32, "status": "confirmed"})
    log.append({"action": "open", "tx": "ef" * 32, "claim": "01" * 32, "status": "submitted", "at_ms": T0})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = relay.Journal(path)
    found = again.resolutions(bytes.fromhex("cd" * 32))
    assert len(found) == 1 and found[0]["status"] == "confirmed" and found[0]["ttl_slot"] == 5
    assert again.pending_open(bytes.fromhex("01" * 32), T0 + 29 * MIN)
    assert not again.pending_open(bytes.fromhex("01" * 32), T0 + 30 * MIN)
    assert [json.loads(line)["action"] for line in path.read_text().splitlines()] == ["resolve", "resolve", "open"]


# ── Finalize lookup ──────────────────────────────────────────────────────────


class FakeKoios:
    def __init__(self, rows, infos):
        self.rows, self.infos = rows, infos

    def get(self, path, params):
        assert path == "asset_txs" and params["_history"] == "true"
        return self.rows

    def tx_info(self, hashes, full=False):
        assert full
        return [self.infos[h] for h in hashes]


class KoiosChain:
    def __init__(self, koios):
        self.koios = koios


def info(tx, height, index, datum=None, token=h32(b"case token")):
    outputs = []
    if datum is not None:
        outputs.append({"tx_index": 0, "asset_list": [{"policy_id": J.hex(), "asset_name": token.hex(),
                                                       "quantity": "1"}],
                        "inline_datum": {"bytes": T.to_cbor(datum).hex(), "value": None}})
    return {"tx_hash": tx, "block_height": height, "tx_block_index": index, "outputs": outputs}


def test_find_finalize_reads_the_spent_open_tally():
    opened = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR)
    voted = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR, tally=(1, 2, 0))
    settled = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR, tally=(1, 2, 0), state_=T.Settling(3), fee=9)
    infos = {"01" * 32: info("01" * 32, 100, 0, opened), "02" * 32: info("02" * 32, 101, 3, voted),
             "03" * 32: info("03" * 32, 105, 1, settled)}
    rows = [{"tx_hash": h} for h in ("03" * 32, "02" * 32, "01" * 32)]
    found = relay.find_finalize(KoiosChain(FakeKoios(rows, infos)), DEP, h32(b"case token"), 115)
    assert found == relay.Finalized("03" * 32, 105, T.Tally(1, 2, 0), True)
    assert not relay.find_finalize(KoiosChain(FakeKoios(rows, infos)), DEP, h32(b"case token"), 114).deep
    burned = {"01" * 32: infos["01" * 32], "04" * 32: info("04" * 32, 102, 0)}
    found = relay.find_finalize(KoiosChain(FakeKoios([{"tx_hash": h} for h in burned], burned)), DEP,
                                h32(b"case token"), 200)
    assert found.tx_id == "04" * 32 and found.tally == T.Tally(0, 0, 0)
    assert relay.find_finalize(KoiosChain(FakeKoios(rows[2:], infos)), DEP, h32(b"case token"), 200) is None


# ── execution guards ─────────────────────────────────────────────────────────


class Queue:
    def __init__(self, submit=True, confirmed=True):
        self.submit_flag, self.confirmed, self.labels = submit, confirmed, []

    def submit(self, label, build, priority=1):
        self.labels.append((label, priority))
        sent = build(self.submit_flag)
        if sent is not None and sent.submitted:
            sent.confirmed = self.confirmed
        return sent


class Ctx:
    def __init__(self, tmp_path, chain=None, key=object()):
        self.chain = chain
        self.m1 = object()
        self.address = RELAY
        self.operator_vkh = OPERATOR
        self.foundation_agents = [FOUNDATION]
        self.signing_key = key
        self.dry_run = False
        self.state_dir = tmp_path
        self.txs = Queue()
        self.logs = []

    def log(self, event, **fields):
        self.logs.append((event, fields))


class TxChain:
    """Enough of a chain for ``_m1_tx``: one collateral output, no scripts."""

    def __init__(self, tip):
        self.tip = tip
        self.sent = []

    def utxos_at(self, address):
        return [UTxO(txin(b"collateral"), TransactionOutput(RELAY, Value(5 * AP3X)))]

    def tip_slot(self):
        return self.tip

    def ms_to_slot(self, ms):
        return BASE.ms_to_slot(ms)

    def slot_to_ms(self, s):
        return BASE.slot_to_ms(s)

    def evaluate(self, data):
        raise AssertionError("no scripts in this test transaction")

    def submit(self, data):
        self.sent.append(data)
        return "f0" * 32


def tx_with_ttl(ttl):
    body = TransactionBody(inputs=[txin(b"in")], outputs=[TransactionOutput(RELAY, Value(2 * AP3X))], fee=200_000,
                           ttl=ttl)
    data = Transaction(body, TransactionWitnessSet()).to_cbor()
    return data if isinstance(data, bytes) else bytes.fromhex(data)


@pytest.fixture
def resolve_setup(tmp_path, monkeypatch):
    deadline = T0 + 5 * HOUR
    d = dispute(deadline=deadline)
    monkeypatch.setattr(module1, "genuine_challenges", lambda *a, **k: [d.challenge])
    monkeypatch.setattr(module1, "find_claim", lambda *a, **k: d.claim)
    monkeypatch.setattr(module1, "stake_problems", lambda *a, **k: [])
    monkeypatch.setattr(module1, "resolution_document",
                        lambda ch, verdict, basis, jury=None: json.dumps({"verdict": verdict, "basis": basis}).encode())
    monkeypatch.setattr(module1.Wallet, "from_signing_key",
                        classmethod(lambda cls, key: module1.Wallet(object(), object(), RELAY)))
    chain = TxChain(slot(T0 + HOUR))
    ctx = Ctx(tmp_path, chain)
    return ctx, d, deadline, monkeypatch


def resolve_action(d):
    return relay.Action("resolve", "test", d, resolution={"verdict": "Inconclusive", "basis": "no-time", "jury": None})


def test_resolve_refuses_an_upper_bound_after_the_deadline(resolve_setup):
    ctx, d, deadline, monkeypatch = resolve_setup
    monkeypatch.setattr(relay, "_capture", lambda m1, build: ({"ttl_slot": slot(deadline) + 1},
                                                             tx_with_ttl(slot(deadline) + 1)))
    r = relay.Relay(ctx)
    result = r.execute(resolve_action(d), world(T0 + HOUR, [d]))
    assert result["result"] == "failed" and "after the deadline" in result["error"]
    assert ctx.chain.sent == [] and r.journal.resolutions(d.token) == []


def test_resolve_logs_before_it_submits_and_only_once(resolve_setup):
    ctx, d, deadline, monkeypatch = resolve_setup
    monkeypatch.setattr(relay, "_capture", lambda m1, build: ({"ttl_slot": slot(deadline)},
                                                             tx_with_ttl(slot(deadline))))
    r = relay.Relay(ctx)
    result = r.execute(resolve_action(d), world(T0 + HOUR, [d]))
    assert result["result"] == "submitted" and len(ctx.chain.sent) == 1
    statuses = [e["status"] for e in r.journal.entries()]
    assert statuses == ["sending", "submitted", "confirmed"]
    assert ctx.txs.labels == [("resolve", 0)]
    again = r.execute(resolve_action(d), world(T0 + HOUR, [d]))
    assert again["result"] == "failed" and "already out" in again["error"] and len(ctx.chain.sent) == 1


def test_resolve_window_is_capped_at_the_deadline(resolve_setup):
    ctx, d, deadline, monkeypatch = resolve_setup
    seen = {}

    def capture(m1, build):
        class Sim:
            def build_oracle_resolve(self, *args, **kw):
                seen.update(kw)
                return {"ttl_slot": slot(deadline)}

        build(None, None, Sim())
        return {"ttl_slot": slot(deadline)}, tx_with_ttl(slot(deadline))

    monkeypatch.setattr(relay, "_capture", capture)
    ctx.chain.tip = slot(deadline - 12 * MIN)
    relay.Relay(ctx).execute(resolve_action(d), world(deadline - 12 * MIN, [d]))
    assert seen["window_slots"] == 12 * 60 + 60 - relay.TTL_MARGIN_SLOTS
    ctx.chain.tip = slot(deadline - 4 * MIN)
    ctx.state_dir = ctx.state_dir / "fresh"
    result = relay.Relay(ctx).execute(resolve_action(d), world(deadline - 4 * MIN, [d]))
    assert "too close" in result["error"]


def test_dry_run_without_a_key_reports_the_decision(resolve_setup):
    ctx, d, deadline, monkeypatch = resolve_setup
    ctx.signing_key = None
    ctx.txs = Queue(submit=False)
    result = relay.Relay(ctx).execute(resolve_action(d), world(T0 + HOUR, [d]))
    assert result["result"] == "decision" and ctx.chain.sent == []


def test_failed_finalize_switches_to_the_fallback(tmp_path):
    ctx = Ctx(tmp_path)
    r = relay.Relay(ctx)
    datum = case_datum(opened_at=T0, deadline=T0 + 72 * HOUR)
    action = relay.Action("finalize", "test", case=case_output(datum))

    def boom(*a, **k):
        raise relay.ActionError("evaluation failed")

    r._do_finalize = boom
    for _ in range(relay.FINALIZE_FAILURES):
        assert r.execute(action, world(datum.reveal_end))["result"] == "failed"
    assert r.caches.finalize_failed[(J, h32(b"case token"))] == relay.FINALIZE_FAILURES
    assert ctx.logs[-1][0] == "relay.action_failed"
