"""Tests of the juror driver (``scripts/juror_driver.py``) with a fake gateway
and no network: the model input equals the input of the service, the call
equals the call of the service, five votes are printed, a refused answer and a
failed call are shown and the run goes on, the dry run calls nothing, a key of
a juror is refused, and the key never appears in an output."""

import dataclasses
import importlib.util
import io
import json
import os
import random
import re
import subprocess
import sys
import types as pytypes
from pathlib import Path

import pytest

from conftest import JURY_DIR, MODULE1_DIR, SHARED_PYTHON, h32
from test_gateway import FIXTURE
from test_juror import DEP, MIN, T0, Ctx, case_output, dispute_for, juror_output
from vector_jury import documents, gateway, input_template as IT, juror as JR, state
from vector_jury.deployment import script_address

SCRIPT = JURY_DIR / "scripts" / "juror_driver.py"
FOUNDATION = JURY_DIR / "jurors" / "foundation" / "jurors.json"
GATEWAY_KEYS_EXAMPLE = JURY_DIR / "service" / "gateway-keys.env.example"

_spec = importlib.util.spec_from_file_location("juror_driver", SCRIPT)
JD = importlib.util.module_from_spec(_spec)
sys.modules["juror_driver"] = JD
_spec.loader.exec_module(JD)

#: The environment of a script run: no key, and the import paths of the library.
CLEAN_ENV = {name: value for name, value in os.environ.items()
             if name != JD.KEY_ENV and not name.startswith(JD.JUROR_KEY_PREFIX)}
CLEAN_ENV["PYTHONPATH"] = os.pathsep.join(str(p) for p in (JURY_DIR, MODULE1_DIR, SHARED_PYTHON))

NOW = 1_795_000_000.0
KEY = "vma_driver_secret_value_0123456789"
JUROR_KEY = "vma_juror_secret_value_9876543210"
CLAIM = b"The seller agent confirms purchase order PO-1042: item WIDGET-7, lot L-2291, quantity 120.\n"
EVIDENCE = (b"The confirmation that the seller agent signed lists quantity 100 for item WIDGET-7, lot L-2291. "
            b"The purchase order lists quantity 120.\n")
#: Pairs of documents: plain text, JSON with quotes and non-ASCII text, bytes that the template cleans, and a
#: pair that the template cuts.
DOCUMENT_PAIRS = {
    "plain": (CLAIM, EVIDENCE),
    "json": ('{"claim": "uptime \\"99.9%\\"", "note": "Kafé \u00e9t\u00e9 \U0001F600"}'.encode(),
             '{"observed": 87.5, "path": "C:\\\\logs\\\\a.txt"}'.encode()),
    "unclean": (b"a\x00b\x01c\xffd\\u0000e\tf\r\n", b"\x7f\x1b[31mred\x1b[0m"),
    "cut": (("\u00e9" * 2000).encode() + b"tail", b'"' * 3000),
}


# ── Fakes ────────────────────────────────────────────────────────────────────


def write_documents(directory: Path, claim: bytes = CLAIM, evidence: bytes = EVIDENCE):
    (directory / "claim.txt").write_bytes(claim)
    (directory / "evidence.txt").write_bytes(evidence)
    return str(directory / "claim.txt"), str(directory / "evidence.txt")


def write_juror_list(directory: Path, count: int = 5, key_env=lambda i: f"JURY_GATEWAY_KEY_TEST_{i}") -> Path:
    """A juror list with ``count`` jurors, ``juror-1`` and on, each with its own model, prompt and key variable."""
    entries = []
    for i in range(1, count + 1):
        agent = h32(b"driver juror %d" % i).hex()
        supplier = {"agent": FIXTURE["agent"], "supplier_pkh": FIXTURE["pkh"]}
        prompt = json.dumps({
            "type": "vector-jury-prompt/1", "juror": f"did:vector:{agent}",
            "model": {"capability": "llm.text.generate.v1", "model": f"model-{i}",
                      "suppliers": [{"agent": f"did:vector:{FIXTURE['agent']}", "supplier_pkh": FIXTURE["pkh"]}]},
            "instructions": f"Instructions of juror {i}.", "input_template": IT.TEMPLATE,
            "output": "One JSON object", "max_output_tokens": 1000 + i}).encode()
        (directory / f"juror-{i}.prompt.json").write_bytes(prompt)
        entries.append({"name": f"juror-{i}", "agent": agent, "model": f"model-{i}", "suppliers": [supplier],
                        "prompt_file": f"juror-{i}.prompt.json", "prompt_hash": documents.doc_hash(prompt).hex(),
                        "prompt_bytes": len(prompt), "max_output_tokens": 1000 + i, "api_key_env": key_env(i)})
    path = directory / "jurors.json"
    path.write_text(json.dumps({"format": JR.JURORS_FORMAT, "owner_vkh": "11" * 28, "jurors": entries}))
    return path


def vote_text(vote: str, reason: str = "A reason.") -> str:
    return json.dumps({"vote": vote, "reason": reason})


class Reply:
    """What the fake gateway answers for a model: the text of the answer, with a refusal part and a status."""

    def __init__(self, text: str, refusal: str = "", status: str = "completed", incomplete=None):
        self.text, self.refusal, self.status, self.incomplete = text, refusal, status, incomplete


class FakeGateway:
    """A gateway client for ``gateway.buy``. ``replies`` maps a model name to a ``Reply``, to an exception that
    ``call`` raises, or to a list of these (one for each call)."""

    def __init__(self, replies=None):
        self.replies = {f"model-{i}": Reply(vote_text("ClaimerWins")) for i in range(1, 6)}
        self.replies.update(replies or {})
        self.calls = []

    def call(self, *, supplier_agent, supplier_pkh, model, instructions, input_text, max_output_tokens, timeout):
        self.calls.append(dict(supplier_agent=supplier_agent, supplier_pkh=supplier_pkh, model=model,
                               instructions=instructions, input_text=input_text,
                               max_output_tokens=max_output_tokens, timeout=timeout))
        reply = self.replies[model]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        content = [{"type": "output_text", "text": reply.text, "annotations": []}]
        if reply.refusal:
            content.append({"type": "refusal", "refusal": reply.refusal})
        response = {"output": [{"type": "message", "role": "assistant", "content": content}],
                    "status": reply.status, "incomplete_details": reply.incomplete}
        return gateway.ModelCall(
            supplier_agent=supplier_agent, supplier_pkh=supplier_pkh, supplier_pub_key="00" * 32, model=model,
            instructions=instructions, input=input_text, max_output_tokens=max_output_tokens,
            committed_max_output_tokens=max_output_tokens, response=response, receipt={}, receipt_signature="",
            escrow_ref=f"{len(self.calls):064x}#0", elapsed_ms=1500)


def run_driver(tmp_path, replies=None, *, args=(), environ=None, claim=CLAIM, evidence=EVIDENCE, jurors=None,
               output=True):
    """Run ``main`` on two documents and a juror list in ``tmp_path`` with a fake gateway. The result holds the
    exit code, the text of stdout and stderr, the fake gateway, the keys that ``make_gateway`` received and
    the report that the run wrote."""
    claim_path, evidence_path = write_documents(tmp_path, claim, evidence)
    jurors = jurors or write_juror_list(tmp_path)
    fake, made, out, err = FakeGateway(replies), [], io.StringIO(), io.StringIO()
    report_path = tmp_path / "run.json"
    argv = [claim_path, evidence_path, "--jurors", str(jurors), *args]
    if output:
        argv += ["--output", str(report_path)]
    code = JD.main(argv, environ={JD.KEY_ENV: KEY} if environ is None else environ, stdout=out, stderr=err,
                   make_gateway=lambda key: made.append(key) or fake, clock=lambda: NOW, sleep=lambda seconds: None)
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return pytypes.SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue(), fake=fake, keys=made,
                                   report=report, path=report_path, claim=claim_path, evidence=evidence_path)


def run_script(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120,
                          env=CLEAN_ENV if env is None else env, cwd=JURY_DIR)


@pytest.fixture
def no_gateway(monkeypatch):
    """Every way to a paid call fails the test. ``pytest.fail`` is not an ``Exception``, so no handler of the
    driver can hide it."""

    def refuse(*args, **kwargs):
        pytest.fail("the run called the gateway")

    monkeypatch.setattr(gateway, "buy", refuse)
    monkeypatch.setattr(gateway.Gateway, "call", refuse)


# ── The script ───────────────────────────────────────────────────────────────


def test_help_names_the_options_and_the_key_variable():
    result = run_script("--help")
    assert result.returncode == 0, result.stderr
    for text in ("--dry-run", "--output", "--jurors", "--key-env", "JUROR_DRIVER_GATEWAY_KEY", "JURY_GATEWAY_KEY_"):
        assert text in result.stdout


def test_dry_run_of_the_script_on_the_foundation_list(tmp_path):
    claim, evidence = write_documents(tmp_path)
    result = run_script(claim, evidence, "--dry-run")
    assert result.returncode == 0, result.stderr
    _, specs = JR.load_jurors(FOUNDATION)
    assert len(specs) == 5 and all(spec.name in result.stdout and spec.model in result.stdout for spec in specs)
    shown = json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{"template"')))
    assert shown["template"] == IT.TEMPLATE and shown["documents"]["claim"]["text"] == CLAIM.decode()
    assert shown["documents"]["evidence"]["status"] == "ok" and "Dry run: nothing was sent" in result.stdout


def test_the_default_client_is_the_gateway_client_of_the_service():
    client = JD.default_gateway(KEY)
    assert isinstance(client, gateway.Gateway) and client.url == gateway.GATEWAY_URL
    assert isinstance(client.keys, gateway.SupplierKeys) and KEY not in repr(client)


# ── The input ────────────────────────────────────────────────────────────────


def test_made_up_facts_name_nothing_on_the_chain():
    facts = JD.made_up_facts(CLAIM, EVIDENCE, 1_000)
    assert facts == JD.made_up_facts(CLAIM, EVIDENCE, 1_000)
    assert facts != JD.made_up_facts(CLAIM, EVIDENCE + b" ", 1_000)
    assert [len(bytes.fromhex(value)) for value in (facts.jury, facts.case, facts.challenge, facts.claim,
                                                    facts.claimer, facts.auditor)] == [28, 32, 32, 32, 32, 32]
    assert len({facts.case, facts.challenge, facts.claim, facts.claimer, facts.auditor}) == 5
    assert facts.deadline_ms == 1_000 + 72 * 3600 * 1000
    claim_uri, evidence_uri = JD.document_uri("claim", CLAIM), JD.document_uri("evidence", EVIDENCE)
    assert claim_uri != evidence_uri and len(claim_uri) == 53
    assert documents.txid_from_uri(claim_uri) != documents.txid_from_uri(evidence_uri)


def test_documents_have_the_hash_and_the_status_that_the_service_gives():
    shown = json.loads(JD.render_input(JD.made_up_facts(CLAIM, EVIDENCE, 0), CLAIM, EVIDENCE))["documents"]
    assert shown["claim"] == {"uri": JD.document_uri("claim", CLAIM).decode(), "hash": documents.doc_hash(CLAIM).hex(),
                              "status": "ok", "cut": False, "text": CLAIM.decode()}
    assert shown["evidence"]["hash"] == documents.doc_hash(EVIDENCE).hex()


class Stop(Exception):
    """Raised by the fake purchase, to end a run after it."""


def recorder(calls):
    def buy(client, suppliers, **kwargs):
        calls.append((client, tuple(suppliers), kwargs))
        raise Stop()

    return buy


def service_purchase(tmp_path, monkeypatch, spec, facts, claim, evidence):
    """What the service gives to ``gateway.buy`` for a case with these facts, whose claim and evidence are these
    bytes. The code of the service reads the documents and builds the input."""
    jury = bytes.fromhex(facts.jury)
    deployment = dataclasses.replace(DEP, script_hash=jury, address=script_address(jury))
    case = case_output(token=bytes.fromhex(facts.case), claimer=bytes.fromhex(facts.claimer))
    datum = dataclasses.replace(
        case.datum, challenge=bytes.fromhex(facts.challenge), claim=bytes.fromhex(facts.claim),
        auditor=bytes.fromhex(facts.auditor), claim_hash=documents.doc_hash(claim),
        evidence_hash=documents.doc_hash(evidence), deadline=facts.deadline_ms)
    case = state.CaseOutput(deployment, case.utxo, case.token, datum)
    uris = {"claim": JD.document_uri("claim", claim), "evidence": JD.document_uri("evidence", evidence)}
    data = {uris["claim"]: claim, uris["evidence"]: evidence}

    def check(uri, expected, **options):
        assert documents.doc_hash(data[bytes(uri)]) == bytes(expected)
        return documents.Checked("ok", data[bytes(uri)])

    ctx = Ctx(tmp_path / spec.name, T0 + 10 * MIN)
    ctx.chain.koios = None
    member = JR.Juror(ctx, spec, sleep=lambda seconds: None, rng=random.Random(1))
    monkeypatch.setattr(member, "budget", lambda juror_output_, case_: 9_000)
    monkeypatch.setattr(member, "prompt_uri", lambda juror_output_: "vector-tx:" + "P" * 43)
    monkeypatch.setattr(member, "_gateway_client", lambda: object())
    monkeypatch.setattr(JR.relay, "output_datum", lambda chain, ref: ref)
    monkeypatch.setattr(JR.T, "decode_claim_datum", lambda ref: pytypes.SimpleNamespace(storage_uri=uris["claim"]))
    monkeypatch.setattr(JR.T, "decode_challenge_datum",
                        lambda ref: pytypes.SimpleNamespace(evidence_uri=uris["evidence"]))
    monkeypatch.setattr(JR.documents, "check", check)
    calls = []
    monkeypatch.setattr(JR.gateway, "buy", recorder(calls))
    with pytest.raises(Stop):
        member.commit(JR.Job("commit", spec.name, case, juror_output(), dispute_for(case)))
    return calls[0]


@pytest.mark.parametrize("claim,evidence", DOCUMENT_PAIRS.values(), ids=DOCUMENT_PAIRS.keys())
def test_input_and_call_equal_those_of_the_service(tmp_path, monkeypatch, claim, evidence):
    facts = JD.made_up_facts(claim, evidence, int(NOW * 1000))
    _, specs = JR.load_jurors(write_juror_list(tmp_path))
    expected = [service_purchase(tmp_path, monkeypatch, spec, facts, claim, evidence) for spec in specs]
    calls = []
    monkeypatch.setattr(gateway, "buy", recorder(calls))
    run_driver(tmp_path, claim=claim, evidence=evidence)
    assert len(calls) == len(expected) == 5
    for (_, suppliers, kwargs), (_, service_suppliers, service_kwargs) in zip(calls, expected):
        assert kwargs["input_text"] == service_kwargs["input_text"] == JD.render_input(facts, claim, evidence)
        for name in ("model", "instructions", "max_output_tokens", "max_attempts"):
            assert kwargs[name] == service_kwargs[name]
        assert suppliers == service_suppliers
    cut = claim == DOCUMENT_PAIRS["cut"][0]
    assert json.loads(calls[0][2]["input_text"])["documents"]["claim"]["cut"] is cut


# ── Votes, refused answers and failed calls ──────────────────────────────────


def test_five_votes_are_printed_with_their_reasons(tmp_path):
    replies = {"model-1": Reply(vote_text("AuditorWins", "The quantities differ.")),
               "model-2": Reply(vote_text("ClaimerWins", "The claim holds.")),
               "model-3": Reply("My answer: " + vote_text("AuditorWins", "Two numbers, 100 and 120.")),
               "model-4": Reply(vote_text("Inconclusive", "Not enough facts.")),
               "model-5": Reply(vote_text("AuditorWins", "The evidence is specific."))}
    result = run_driver(tmp_path, replies)
    assert result.code == 0 and result.err == ""
    for number, (vote, reason) in enumerate([("AuditorWins", "The quantities differ."),
                                             ("ClaimerWins", "The claim holds."),
                                             ("AuditorWins", "Two numbers, 100 and 120."),
                                             ("Inconclusive", "Not enough facts."),
                                             ("AuditorWins", "The evidence is specific.")], 1):
        assert f"[{number}/5] juror-{number} (model-{number})" in result.out
        assert f"  vote:    {vote}\n  reason:  {reason}\n" in result.out
        assert f"  juror-{number:<14} {vote}\n" in result.out
    assert "Tally: ClaimerWins 1, AuditorWins 3, Inconclusive 1, no valid vote 0, no answer 0" in result.out
    assert result.keys == [KEY]
    # One request for each juror, one after the other, each with the instructions and the model of its juror.
    assert [call["model"] for call in result.fake.calls] == [f"model-{i}" for i in range(1, 6)]
    assert [call["instructions"] for call in result.fake.calls] == [f"Instructions of juror {i}." for i in range(1, 6)]
    assert len({call["input_text"] for call in result.fake.calls}) == 1
    assert [call["max_output_tokens"] for call in result.fake.calls] == [1001, 1002, 1003, 1004, 1005]
    # A call waits as long as the service allows for one answer, and goes to the supplier of its juror.
    assert {call["timeout"] for call in result.fake.calls} == {gateway.MAX_CALL_SECONDS}
    assert {(call["supplier_agent"], call["supplier_pkh"]) for call in result.fake.calls} == {
        (FIXTURE["agent"], FIXTURE["pkh"])}


def test_a_refused_answer_is_shown_and_the_run_goes_on(tmp_path):
    replies = {"model-2": Reply("I think the claimer is right.", refusal="I cannot rule on this."),
               "model-3": Reply("Thinking about it", status="incomplete", incomplete={"reason": "max_output_tokens"}),
               "model-4": Reply('{"vote": "Maybe", "reason": "x"}')}
    result = run_driver(tmp_path, replies)
    assert result.code == 1 and len(result.fake.calls) == 5
    assert result.out.count("The parser finds no valid vote in the answer.") == 3
    assert "refusal: I cannot rule on this." in result.out and "answer:  I think the claimer is right." in result.out
    assert "the response is incomplete (details: {'reason': 'max_output_tokens'})" in result.out
    assert "  vote:    ClaimerWins\n" in result.out and "[5/5] juror-5" in result.out
    assert "Tally: ClaimerWins 2, AuditorWins 0, Inconclusive 0, no valid vote 3, no answer 0" in result.out
    statuses = [(juror["name"], juror["status"], juror["vote"]) for juror in result.report["jurors"]]
    assert statuses[1:4] == [("juror-2", "no-vote", "Inconclusive"), ("juror-3", "no-vote", "Inconclusive"),
                             ("juror-4", "no-vote", "Inconclusive")]
    assert result.report["jurors"][1]["refusal"] == "I cannot rule on this."


def test_a_failed_call_is_shown_and_the_run_goes_on(tmp_path):
    refused = gateway.GatewayError("stop", "HTTP 402 insufficient_funds: no funds", status=402,
                                   code="insufficient_funds")
    flaky = gateway.GatewayError("failed", "HTTP 502 upstream_error: bad", status=502, code="upstream_error")
    replies = {"model-1": refused, "model-2": RuntimeError("something broke"), "model-3": [flaky, flaky]}
    result = run_driver(tmp_path, replies)
    assert result.code == 1
    assert "the gateway refused the call: HTTP 402 insufficient_funds: no funds" in result.out
    assert "RuntimeError: something broke" in result.out
    assert "2 paid attempts made; no further call" in result.out
    assert "  vote:    none. The gateway gave no verified call." in result.out
    assert "  vote:    none. An error stopped the call." in result.out
    assert "[4/5] juror-4" in result.out and "  vote:    ClaimerWins\n" in result.out
    assert "Tally: ClaimerWins 2, AuditorWins 0, Inconclusive 0, no valid vote 0, no answer 3" in result.out
    # The service stops at two paid attempts for a juror: the third juror has two calls, the others one.
    assert [call["model"] for call in result.fake.calls] == ["model-1", "model-2", "model-3", "model-3", "model-4",
                                                             "model-5"]
    assert [(j["status"], j["attempts"]) for j in result.report["jurors"]] == [
        ("no-call", 0), ("error", 0), ("no-call", 2), ("vote", 1), ("vote", 1)]


def test_a_busy_answer_goes_to_the_next_try_as_in_the_service(tmp_path):
    busy = gateway.GatewayError("busy", "HTTP 429 concurrency_queue_full: full", status=429,
                                code="concurrency_queue_full", retry_after=2.0)
    result = run_driver(tmp_path, {"model-1": [busy, Reply(vote_text("AuditorWins", "After a wait."))]})
    assert result.code == 0 and [call["model"] for call in result.fake.calls][:2] == ["model-1", "model-1"]
    assert result.report["jurors"][0]["vote"] == "AuditorWins" and result.report["jurors"][0]["attempts"] == 1
    assert result.report["jurors"][0]["gateway_log"][0]["result"] == "busy"


def test_an_interrupt_keeps_the_jurors_that_finished(tmp_path):
    result = run_driver(tmp_path, {"model-3": KeyboardInterrupt()})
    assert result.code == 130
    assert ("Interrupted. The call that was running can still be charged. "
            "The output file holds the jurors that finished.") in result.out
    assert [j["status"] for j in result.report["jurors"]] == ["vote", "vote", "interrupted", "not-run", "not-run"]
    assert len(result.fake.calls) == 3


# ── The dry run ──────────────────────────────────────────────────────────────


def test_dry_run_prints_the_input_and_calls_nothing(tmp_path, no_gateway):
    result = run_driver(tmp_path, environ={}, args=["--dry-run"])
    assert result.code == 0 and result.fake.calls == [] and result.keys == [] and result.err == ""
    facts = JD.made_up_facts(CLAIM, EVIDENCE, int(NOW * 1000))
    assert JD.render_input(facts, CLAIM, EVIDENCE) in result.out.splitlines()
    assert "[1/5]" not in result.out and "Dry run: nothing was sent" in result.out
    assert result.report["dry_run"] is True and result.report["input"] == JD.render_input(facts, CLAIM, EVIDENCE)
    assert [j["status"] for j in result.report["jurors"]] == ["not-run"] * 5
    assert list(result.report["tally"].values()) == [0, 0, 0, 0, 0]


def test_dry_run_shows_a_cut_document(tmp_path, no_gateway):
    result = run_driver(tmp_path, environ={}, args=["--dry-run"], claim=DOCUMENT_PAIRS["cut"][0],
                        evidence=DOCUMENT_PAIRS["cut"][1])
    assert re.search(r"claim: .*, cut, the juror sees the first 3500 bytes\)", result.out)
    assert re.search(r"evidence: .*, cut, the juror sees the first \d+ bytes\)", result.out)
    assert result.report["documents"]["claim"]["cut"] is True
    assert "not cut" in run_driver(tmp_path, environ={}, args=["--dry-run"]).out


# ── The key ──────────────────────────────────────────────────────────────────


JUROR_VARIABLE = "JURY_GATEWAY_KEY_TEST_1"


@pytest.mark.parametrize("key_env,environ,needle", [
    # The variable is named by the juror list.
    (JUROR_VARIABLE, {JUROR_VARIABLE: JUROR_KEY}, f"{JUROR_VARIABLE} is the key variable of the juror juror-1"),
    # The variable starts like the key variables of the jurors, and the list does not name it.
    ("JURY_GATEWAY_KEY_BRAND_NEW", {"JURY_GATEWAY_KEY_BRAND_NEW": KEY},
     "JURY_GATEWAY_KEY_BRAND_NEW starts with JURY_GATEWAY_KEY_, the start of the key variables"),
    ("JURY_GATEWAY_KEY_BRAND_NEW", {},
     "JURY_GATEWAY_KEY_BRAND_NEW starts with JURY_GATEWAY_KEY_, the start of the key variables"),
    # The new variable holds the key of a juror.
    (JD.KEY_ENV, {JD.KEY_ENV: JUROR_KEY, "JURY_GATEWAY_KEY_TEST_3": JUROR_KEY},
     f"the key in {JD.KEY_ENV} is the key in JURY_GATEWAY_KEY_TEST_3"),
    (JD.KEY_ENV, {JD.KEY_ENV: JUROR_KEY, "JURY_GATEWAY_KEY_OTHER": JUROR_KEY},
     f"the key in {JD.KEY_ENV} is the key in JURY_GATEWAY_KEY_OTHER"),
], ids=["list-name", "prefix-name", "prefix-name-unset", "same-value-listed", "same-value-prefix"])
@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry-run"])
def test_a_key_of_a_juror_is_refused(tmp_path, key_env, environ, needle, dry):
    result = run_driver(tmp_path, args=["--key-env", key_env, *(["--dry-run"] if dry else [])], environ=environ)
    assert result.code == 2 and needle in result.err
    assert result.out == "" and result.fake.calls == [] and result.keys == [] and not result.path.exists()
    assert KEY not in result.err and JUROR_KEY not in result.err


def test_a_variable_of_a_listed_juror_is_refused_without_the_prefix(tmp_path):
    jurors = write_juror_list(tmp_path, key_env=lambda i: f"MY_JUROR_KEY_{i}")
    refused = run_driver(tmp_path, jurors=jurors, args=["--key-env", "MY_JUROR_KEY_2"],
                         environ={"MY_JUROR_KEY_2": KEY})
    assert refused.code == 2 and "MY_JUROR_KEY_2 is the key variable of the juror juror-2" in refused.err
    same = run_driver(tmp_path, jurors=jurors, environ={JD.KEY_ENV: KEY, "MY_JUROR_KEY_4": KEY})
    assert same.code == 2 and f"the key in {JD.KEY_ENV} is the key in MY_JUROR_KEY_4" in same.err
    assert refused.fake.calls == [] and same.fake.calls == []


def test_a_new_key_runs_while_the_keys_of_the_jurors_are_set(tmp_path):
    environ = {JD.KEY_ENV: KEY, **{f"JURY_GATEWAY_KEY_TEST_{i}": f"{JUROR_KEY}{i}" for i in range(1, 6)}}
    result = run_driver(tmp_path, environ=environ)
    assert result.code == 0 and result.keys == [KEY] and len(result.fake.calls) == 5
    other = run_driver(tmp_path, args=["--key-env", "MY_TRIAL_KEY"], environ={"MY_TRIAL_KEY": KEY})
    assert other.code == 0 and other.keys == [KEY]
    for text in (result.out, other.out, result.path.read_text()):
        assert KEY not in text and JUROR_KEY not in text


@pytest.mark.parametrize("environ", [{}, {JD.KEY_ENV: ""}], ids=["unset", "empty"])
def test_a_real_run_needs_a_key(tmp_path, environ):
    result = run_driver(tmp_path, environ=environ)
    assert result.code == 2 and f"{JD.KEY_ENV} (gateway API key) is empty" in result.err
    assert result.fake.calls == [] and result.keys == []
    assert run_driver(tmp_path, environ=environ, args=["--dry-run"]).code == 0


def test_the_key_is_never_printed_or_written(tmp_path):
    secret_error = gateway.GatewayError("stop", f"HTTP 401 invalid_api_key: {KEY} is not valid", status=401)
    replies = {"model-1": Reply(vote_text("AuditorWins", f"The key {KEY} and {JUROR_KEY} are in this reason.")),
               "model-2": secret_error, "model-3": RuntimeError(f"failed with {KEY}"),
               "model-4": Reply(f"No vote here, only {KEY}")}
    result = run_driver(tmp_path, replies, environ={JD.KEY_ENV: KEY, "JURY_GATEWAY_KEY_TEST_1": JUROR_KEY})
    assert result.code == 1
    for text in (result.out, result.err, result.path.read_text()):
        assert KEY not in text and JUROR_KEY not in text
    assert "The key <hidden> and <hidden> are in this reason." in result.out
    assert "RuntimeError: failed with <hidden>" in result.out
    assert result.report["jurors"][0]["reason"] == "The key <hidden> and <hidden> are in this reason."


def test_the_foundation_key_variables_are_the_names_that_the_driver_refuses():
    _, specs = JR.load_jurors(FOUNDATION)
    names = {spec.api_key_env for spec in specs}
    assert len(names) == 5 and all(name.startswith(JD.JUROR_KEY_PREFIX) for name in names)
    example = {line.split("=")[0] for line in GATEWAY_KEYS_EXAMPLE.read_text().splitlines()
               if line and not line.startswith("#")}
    assert example == names
    assert JD.KEY_ENV not in names and not JD.KEY_ENV.startswith(JD.JUROR_KEY_PREFIX)
    for name in names:
        assert JD.key_problems(name, {}, specs)
    assert not JD.key_problems(JD.KEY_ENV, {JD.KEY_ENV: KEY}, specs)


# ── Files ────────────────────────────────────────────────────────────────────


def test_the_output_file_has_the_votes_the_prompts_and_the_model_names(tmp_path):
    reason = "Zbog \u0161 and \u00e9 and \u202e and \u009b done."
    result = run_driver(tmp_path, {"model-1": Reply(vote_text("AuditorWins", reason))})
    raw = result.path.read_text()
    report = result.report
    assert raw.isascii() and oct(os.stat(result.path).st_mode & 0o777) == "0o600"
    assert not (tmp_path / "run.json.tmp").exists()
    assert report["format"] == JD.FORMAT and report["dry_run"] is False and report["template"] == IT.TEMPLATE
    facts = JD.made_up_facts(CLAIM, EVIDENCE, int(NOW * 1000))
    assert report["input"] == JD.render_input(facts, CLAIM, EVIDENCE) and report["facts"] == dataclasses.asdict(facts)
    assert report["documents"]["claim"] == {"file": result.claim, "bytes": len(CLAIM),
                                            "hash": documents.doc_hash(CLAIM).hex(), "cut": False}
    jurors = report["jurors"]
    assert [j["model"] for j in jurors] == [f"model-{i}" for i in range(1, 6)]
    assert [j["instructions"] for j in jurors] == [f"Instructions of juror {i}." for i in range(1, 6)]
    assert [j["vote"] for j in jurors] == ["AuditorWins"] + ["ClaimerWins"] * 4
    assert jurors[0]["reason"] == reason and jurors[0]["status"] == "vote" and jurors[0]["escrow"].endswith("#0")
    assert jurors[0]["supplier"] == f"did:vector:{FIXTURE['agent']}" and jurors[0]["output_text"].startswith("{")
    assert report["tally"] == {"ClaimerWins": 4, "AuditorWins": 1, "Inconclusive": 0, "no_valid_vote": 0,
                               "no_answer": 0}
    assert jurors[0]["prompt_hash"] == documents.doc_hash((tmp_path / "juror-1.prompt.json").read_bytes()).hex()


def test_a_terminal_shows_only_printable_text(tmp_path):
    reason = "red \x1b[31m, csi \u009b, flip \u202e, bell \x07, line one\nline two"
    plain = "no\u00a0break, thin\u2009space, caf\u00e9, \u0161, \U0001F600"
    text = '{"vote": "AuditorWins", "reason": ' + json.dumps(reason) + "}"
    result = run_driver(tmp_path, {"model-1": Reply(text), "model-2": Reply("\u009b2J \x1b]0;title\x07 no vote"),
                                   "model-3": Reply(vote_text("ClaimerWins", plain))})
    for bad in ("\x1b", "\u009b", "\u202e", "\x07"):
        assert bad not in result.out
    assert "csi \ufffd, flip \ufffd, bell \ufffd, line one\n           line two" in result.out
    assert "\u202e" in json.loads(result.path.read_text())["jurors"][0]["reason"]
    # Characters that a terminal shows are not changed.
    assert f"  reason:  {plain}\n" in result.out


def test_the_output_path_is_checked_before_the_first_call(tmp_path):
    missing = tmp_path / "missing" / "run.json"
    claim, evidence = write_documents(tmp_path)
    fake, out, err = FakeGateway(), io.StringIO(), io.StringIO()
    code = JD.main([claim, evidence, "--jurors", str(write_juror_list(tmp_path)), "--output", str(missing)],
                   environ={JD.KEY_ENV: KEY}, stdout=out, stderr=err, make_gateway=lambda key: fake)
    assert code == 2 and "cannot write" in err.getvalue() and fake.calls == [] and out.getvalue() == ""
    assert run_driver(tmp_path).code == 0


def test_a_later_failed_write_is_reported_and_the_run_goes_on(tmp_path, monkeypatch):
    replace, writes = os.replace, []

    def flaky_replace(source, target):
        writes.append(target)
        if len(writes) > 1:
            raise OSError(28, "No space left on device")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", flaky_replace)
    result = run_driver(tmp_path)
    # One write before the first call, and one after each of the five jurors. All five jurors ran.
    assert result.code == 1 and len(writes) == 6 and len(result.fake.calls) == 5
    assert result.out.count(f"Warning: cannot write {result.path}: No space left on device") == 5
    assert "Tally: ClaimerWins 5, " in result.out and not (tmp_path / "run.json.tmp").exists()


def test_a_document_that_cannot_be_read_is_refused(tmp_path, monkeypatch):
    claim, evidence = write_documents(tmp_path)
    jurors = write_juror_list(tmp_path)
    for argv, needle in (([str(tmp_path / "none.txt"), evidence], "cannot read the claim document"),
                         ([claim, str(tmp_path)], "cannot read the evidence document")):
        err = io.StringIO()
        assert JD.main([*argv, "--jurors", str(jurors), "--dry-run"], environ={}, stdout=io.StringIO(),
                       stderr=err) == 2 and needle in err.getvalue()
    monkeypatch.setattr(documents, "MAX_FETCH_BYTES", len(EVIDENCE))
    assert run_driver(tmp_path, args=["--dry-run"], environ={}).code == 0
    (tmp_path / "evidence.txt").write_bytes(EVIDENCE + b"x")
    err = io.StringIO()
    assert JD.main([claim, evidence, "--jurors", str(jurors), "--dry-run"], environ={}, stdout=io.StringIO(),
                   stderr=err) == 2 and f"larger than {len(EVIDENCE)} bytes" in err.getvalue()


def test_a_juror_list_that_cannot_be_used_is_refused(tmp_path, no_gateway):
    broken = tmp_path / "broken.json"
    broken.write_text("{}")
    result = run_driver(tmp_path, jurors=broken, args=["--dry-run"], environ={})
    assert result.code == 2 and f"the juror list {broken}" in result.err
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"format": JR.JURORS_FORMAT, "owner_vkh": "11" * 28, "jurors": []}))
    result = run_driver(tmp_path, jurors=empty, args=["--dry-run"], environ={})
    assert result.code == 2 and "has no juror" in result.err
    assert run_driver(tmp_path, args=["--dry-run"], environ={}).code == 0
