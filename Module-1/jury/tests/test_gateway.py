"""Tests of ``vector_jury.gateway``: the canonical JSON port, the receipt
check (``verifyEscrowReceipt``), the hashes of the request and the answer,
the output text, the HTTP errors and the retry rules of ``buy``.

``FIXTURE`` was made with the TypeScript code of agents-marketplace
(``signReceipt``, ``canonicalize``, ``responseRequestCommitment``,
``responseResultCommitment``, ``responseOutputText``) and a test key: the
Python port must give the same bytes and accept the same signature.
"""

import hashlib
import json

import pytest
import requests

from vector_jury import gateway as G

FIXTURE = json.loads(r'''{"pub":"90ac19189cdd401abf27d88d527bb28565cce7353920f56907f48a17a7f8d986","pkh":"779e647f45605f669786fabdafc011e8a0d2db9798ce17edfb4e619c","agent":"a7b2088985ad2c5dd914f0deaedbcdee71dbe25033ca29a3527c9bde65876d93","input":"{\"template\":\"vector-jury-input/1\",\"note\":\"Cafe\u0301 \u00e9t\u00e9 \\\"quoted\\\" \\\\ back\",\"n\":1}","instructions":"You are a juror. Answer with one JSON object.","prompt_hash":"a8304a0766eb7836b4c1de9fb932f4859ef61ec565270aa788a56ff28390957c","prompt_hash_2048":"62de9e80b2d3d6969951bfee9db0dfafb0c81b5839799a07dc16de0be760205a","response":{"id":"resp_x","object":"response","created_at":1,"model":"test-model","status":"completed","output":[{"id":"rs_1","type":"reasoning","summary":[{"type":"summary_text","text":"{\"vote\":\"AuditorWins\"}"}]},{"id":"msg_1","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"Thinking: {\"vote\": \"AuditorWins\", \"reason\": \"quoted from the evidence\"}\n","annotations":[]},{"type":"refusal","refusal":"no"},{"type":"output_text","text":"{\"vote\": \"ClaimerWins\", \"reason\": \"The evidence does not contradict the claim.\"}","annotations":[],"logprobs":[{"p":0.5},{"p":1},{"p":1e-07},{"p":123456789012345680000}]}]}],"usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30},"error":null,"incomplete_details":null},"response_hash":"e64421f2f84a503ff42f226eb740c8c5d179c20e920b04aac261cc7f324bed4f","output_text":"Thinking: {\"vote\": \"AuditorWins\", \"reason\": \"quoted from the evidence\"}\n{\"vote\": \"ClaimerWins\", \"reason\": \"The evidence does not contradict the claim.\"}","receipt":{"prompt_hash":"a8304a0766eb7836b4c1de9fb932f4859ef61ec565270aa788a56ff28390957c","response_hash":"e64421f2f84a503ff42f226eb740c8c5d179c20e920b04aac261cc7f324bed4f","model":"test-model","prompt_tokens":10,"completion_tokens":20,"wallclock_ms":1834,"supplier_pkh":"779e647f45605f669786fabdafc011e8a0d2db9798ce17edfb4e619c","escrow_ref":"4896a5506c9d6d4b1a0e5f7f3a3c1b2d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b#0","agent_did":"did:vector:a7b2088985ad2c5dd914f0deaedbcdee71dbe25033ca29a3527c9bde65876d93"},"signature":"2a27645ff9babcdbbcbc122f0cb45d2dfcbd68efffb83c78278649abd57935021b67d28d0fe96717025b1adccdfef8c5c45c59e187a4ea7a477c29b9dbdcff09","result_hash":"bb9dcc3ab0f80978a9790b7ab7b646da7eeb47ae15a5bae1169f50d63d987312","canonical":["{\"2\":\"y\",\"10\":\"x\",\"a\":[1,2.5,0,1e+21,1e-7,0.00001,123.456,1],\"b\":2,\"c\":{\"y\":true,\"z\":null},\"\u00e9\":\"\u00e9\"}"]}''')

#: The private key of the fixture (a test key: sha256 of a fixed text).
TEST_SEED = hashlib.sha256(b"vector-jury test supplier key").digest()
ESCROW = FIXTURE["receipt"]["escrow_ref"]
API_KEY = "vma_test_secret_value"


def signing_key():
    from nacl.signing import SigningKey

    return SigningKey(TEST_SEED)


def sign(receipt: dict) -> str:
    return signing_key().sign(G.canonical_json(receipt).encode("utf-8")).signature.hex()


# ── canonical JSON and hashes ────────────────────────────────────────────────


def test_canonical_json_matches_typescript():
    sample = {"b": 2, "a": [1, 2.5, -0.0, 1e21, 1e-7, 0.00001, 123.456, 1.0], "10": "x", "2": "y",
              "é": "e\u0301", "c": {"z": None, "y": True}}
    assert G.canonical_json(sample) == FIXTURE["canonical"][0]


@pytest.mark.parametrize("value,text", [
    (0.1, "0.1"), (1e-6, "0.000001"), (1e-7, "1e-7"), (1.5e-7, "1.5e-7"), (1e20, "100000000000000000000"),
    (1e21, "1e+21"), (1.25e21, "1.25e+21"), (-2.5, "-2.5"), (100.0, "100"), (5e-324, "5e-324"),
    (float("nan"), "null"), (float("inf"), "null"), (2 ** 60, "1152921504606847000"), (2 ** 53, "9007199254740992"),
])
def test_js_numbers(value, text):
    assert G.canonical_json(value) == text


def test_strings_escape_like_json_stringify():
    assert G.canonical_json("a\"b\\c\n\x01\u2028é") == '"a\\"b\\\\c\\n\\u0001\u2028é"'
    assert G.canonical_json("\ud800") == '"\\ud800"'


def test_prompt_and_response_hashes_match_typescript():
    assert G.prompt_hash(FIXTURE["input"], FIXTURE["instructions"], 2500) == FIXTURE["prompt_hash"]
    assert G.prompt_hash(FIXTURE["input"], FIXTURE["instructions"], 2048) == FIXTURE["prompt_hash_2048"]
    assert G.response_hash(FIXTURE["response"]) == FIXTURE["response_hash"]


def test_output_text_is_response_output_text():
    assert G.output_text(FIXTURE["response"]["output"]) == FIXTURE["output_text"]
    assert G.refusal_text(FIXTURE["response"]["output"]) == "no"
    assert G.output_text([{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]},
                          {"type": "reasoning", "summary": []}]) == ""


def test_committed_candidates():
    assert G.committed_candidates(2500, 4096) == [2500]
    assert G.committed_candidates(2500, 2048) == [2500, 2048]
    assert G.committed_candidates(2500, None, True, 0) == [2500]


# ── keys and receipts ────────────────────────────────────────────────────────


def test_multikey_of_a_real_supplier():
    # did:vector:5649d6e5… (deepseek-v4-pro supplier), verification method #5e6cff6d…
    key = G.multikey_ed25519("z6MknUtaGP6Msco7aqu7YAtdxEQG3CFD7wQMmiQtdkFkEWN7")
    assert key == "7748612a37c7bb418230c9bcac19c2ba13754211a24201d3d32218d9b29eea14"
    assert G.blake2b_224_hex(bytes.fromhex(key)) == "5e6cff6d91ba035b36452054f40ee12f1c04ea63e6ae0eced5913e43"
    with pytest.raises(ValueError):
        G.multikey_ed25519("zQ3shokFTS3brHcDQrn82RUDfCZESWL1ZdCEJwekUDPQiYBme")  # secp256k1, not Ed25519


class FakeResolver:
    """The live resolver: the resolution result for Accept
    application/did-resolution, the bare DID document for application/json."""

    AGENT = "5649d6e5a1160c706ad8c227f7d2bab7e7bf00cd2953c6d7838fb80f89b8a503"
    PKH = "5e6cff6d91ba035b36452054f40ee12f1c04ea63e6ae0eced5913e43"

    def __init__(self):
        did = f"did:vector:{self.AGENT}"
        self.document = {"id": did, "verificationMethod": [
            {"id": f"{did}#{self.PKH}", "type": "Multikey",
             "publicKeyMultibase": "z6MknUtaGP6Msco7aqu7YAtdxEQG3CFD7wQMmiQtdkFkEWN7"}]}
        self.seen = []

    def __call__(self, url, timeout=None, headers=None):
        accept = (headers or {}).get("Accept")
        self.seen.append((url, accept))
        if url.endswith("/capability"):
            return Answer(404, {})
        if accept == "application/did-resolution":
            return Answer(200, {"didDocument": self.document, "didDocumentMetadata": {
                "vectorProfile": {"endpoint": "https://supplier.example"}}})
        return Answer(200, self.document)


def test_supplier_key_comes_from_the_resolution_result():
    resolver = FakeResolver()
    info = G.SupplierKeys(get=resolver).lookup(FakeResolver.AGENT, FakeResolver.PKH)
    assert (info.source, info.pub_key) == ("did", "7748612a37c7bb418230c9bcac19c2ba13754211a24201d3d32218d9b29eea14")
    assert resolver.seen[0] == (f"{G.RESOLVER_URL}did:vector:{FakeResolver.AGENT}", "application/did-resolution")


def test_supplier_key_refuses_an_unlisted_key():
    with pytest.raises(G.SupplierKeyError, match="no public key for supplier"):
        G.SupplierKeys(get=FakeResolver()).lookup(FakeResolver.AGENT, "00" * 28)


def problems(receipt=None, signature=None, pub=None, **overrides):
    escrow = {"escrow_ref": ESCROW, "supplier_pkh": FIXTURE["pkh"], "agent_id": FIXTURE["agent"], **overrides}
    return G.receipt_problems(FIXTURE["receipt"] if receipt is None else receipt,
                              FIXTURE["signature"] if signature is None else signature,
                              FIXTURE["pub"] if pub is None else pub, **escrow)


def test_typescript_receipt_verifies():
    assert G.verify_signature(FIXTURE["receipt"], FIXTURE["signature"], FIXTURE["pub"])
    assert problems() == []


def test_python_signature_equals_typescript():
    assert sign(FIXTURE["receipt"]) == FIXTURE["signature"]
    assert signing_key().verify_key.encode().hex() == FIXTURE["pub"]


@pytest.mark.parametrize("change,needle", [
    (lambda r: r.pop("agent_did"), "9 receipt fields"),
    (lambda r: r.update(extra=1), "9 receipt fields"),
    (lambda r: r.update(agent_did="did:vector:" + "11" * 32), "agent_did"),
    (lambda r: r.update(supplier_pkh="00" * 28), "names the supplier"),
    (lambda r: r.update(escrow_ref="00" * 32 + "#1"), "names the escrow"),
    (lambda r: r.update(completion_tokens=21), "signature"),
])
def test_receipt_changes_are_refused(change, needle):
    receipt = dict(FIXTURE["receipt"])
    change(receipt)
    found = problems(receipt)
    assert found and any(needle in p for p in found), found


def test_receipt_with_wrong_key_or_signature():
    other = "11" * 32
    assert any("blake2b_224" in p for p in problems(pub=other))
    assert any("signature" in p for p in problems(signature="00" * 64))
    assert any("signature" in p for p in problems(signature="zz"))
    assert problems(receipt=[]) == ["the receipt is not a JSON object"]


# ── the gateway client ───────────────────────────────────────────────────────


class FakeKeys:
    def __init__(self, cap=None):
        self.cap = cap
        self.calls = 0

    def lookup(self, agent, pkh):
        self.calls += 1
        if pkh != FIXTURE["pkh"]:
            raise G.SupplierKeyError(f"unknown supplier {pkh}")
        return G.SupplierInfo(agent, pkh, FIXTURE["pub"], self.cap, "test")


class Answer:
    def __init__(self, status, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def answer_ok(receipt=None, signature=None, response=None):
    body = dict(FIXTURE["response"] if response is None else response)
    body["x_vector"] = {"receipt": FIXTURE["receipt"] if receipt is None else receipt,
                        "receipt_signature": FIXTURE["signature"] if signature is None else signature,
                        "escrow_ref": ESCROW}
    return Answer(200, body)


class FakePost:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.bodies = []
        self.headers = []

    def __call__(self, url, json=None, timeout=None, headers=None):
        assert url.endswith("/openai/v1/responses")
        self.bodies.append(json)
        self.headers.append(headers)
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(post, cap=None):
    return G.Gateway(API_KEY, url="https://gw.test", post=post, keys=FakeKeys(cap))


def call(gw, **kw):
    args = dict(supplier_agent=FIXTURE["agent"], supplier_pkh=FIXTURE["pkh"], model="test-model",
                instructions=FIXTURE["instructions"], input_text=FIXTURE["input"], max_output_tokens=2500, timeout=30)
    args.update(kw)
    return gw.call(**args)


def test_call_returns_a_verified_call_and_pins_the_supplier():
    post = FakePost(answer_ok())
    result = call(client(post))
    body = post.bodies[0]
    assert body["x_vector"] == {"supplier_pkh": FIXTURE["pkh"]}
    assert body["input"] == FIXTURE["input"] and body["max_output_tokens"] == 2500 and body["store"] is False
    assert post.headers[0]["Authorization"] == f"Bearer {API_KEY}"
    assert result.escrow_ref == ESCROW and result.committed_max_output_tokens == 2500
    assert result.text == FIXTURE["output_text"] and result.supplier_pub_key == FIXTURE["pub"]
    assert "x_vector" not in result.response
    assert G.ModelCall.from_json(json.loads(json.dumps(result.to_json()))) == result


def test_call_accepts_the_committed_cap():
    receipt = dict(FIXTURE["receipt"], prompt_hash=FIXTURE["prompt_hash_2048"])
    post = FakePost(answer_ok(receipt, sign(receipt)))
    result = call(client(post, cap=2048))
    assert result.committed_max_output_tokens == 2048


@pytest.mark.parametrize("kind", ["no_vector", "output", "prompt", "model", "status"])
def test_call_refuses_an_answer_that_the_receipt_does_not_cover(kind):
    if kind == "no_vector":
        answer = Answer(200, dict(FIXTURE["response"]))
        extra = {}
    elif kind == "output":
        response = json.loads(json.dumps(FIXTURE["response"]))
        response["output"][1]["content"][2]["text"] = '{"vote": "AuditorWins", "reason": "changed"}'
        answer, extra = answer_ok(response=response), {}
    elif kind == "prompt":
        answer, extra = answer_ok(), {"input_text": FIXTURE["input"] + " "}
    elif kind == "model":
        answer, extra = answer_ok(), {"model": "other-model"}
    else:
        response = dict(FIXTURE["response"], status="failed")
        answer, extra = answer_ok(response=response), {}
    with pytest.raises(G.ReceiptError) as raised:
        call(client(FakePost(answer)), **extra)
    assert raised.value.kind == "failed"
    assert API_KEY not in str(raised.value)


@pytest.mark.parametrize("answer,kind", [
    (Answer(429, {"error": {"code": "concurrency_queue_full", "message": "full"}}, {"Retry-After": "1"}), "busy"),
    (Answer(409, {"error": {"code": "supplier_busy"}}), "busy"),
    (Answer(503, {"error": {"code": "supplier_capacity_timeout"}}, {"Retry-After": "1"}), "busy"),
    (Answer(503, {"error": {"code": "suppliers_unavailable"}}), "busy"),
    (Answer(503, {"error": {"code": "overloaded"}}), "failed"),
    (Answer(502, {"error": {"code": "receipt_verification_failed"}}), "failed"),
    (Answer(504, {"error": {"code": "supplier_timeout"}}), "failed"),
    (Answer(500, None, text="<html>"), "failed"),
    (Answer(400, {"error": {"code": "unsupported_parameter"}}), "stop"),
    (Answer(401, {"error": {"code": "invalid_api_key"}}), "stop"),
    (Answer(402, {"error": {"code": "insufficient_funds"}}), "stop"),
    (requests.ConnectTimeout("connect"), "busy"),
    (requests.ReadTimeout("read"), "failed"),
    (requests.ConnectionError("reset"), "failed"),
])
def test_http_errors(answer, kind):
    with pytest.raises(G.GatewayError) as raised:
        call(client(FakePost(answer)))
    assert raised.value.kind == kind
    assert API_KEY not in str(raised.value) and API_KEY not in repr(client(FakePost()))


@pytest.mark.parametrize("header,seconds", [(None, 5.0), ("3", 3.0), ("0", 1.0), ("600", 60.0), ("soon", 5.0),
                                            ("Wed, 21 Oct 2015 07:28:10 GMT", 1.0)])
def test_retry_after(header, seconds):
    assert G.retry_after_seconds(header) == seconds


# ── buy: suppliers, retries and paid attempts ────────────────────────────────


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


SUPPLIERS = (G.Supplier(FIXTURE["agent"], FIXTURE["pkh"]), G.Supplier("22" * 32, "33" * 28))


def buy(post, *, cutoff=10_000.0, attempts=0, clock=None, suppliers=SUPPLIERS):
    clock = clock or Clock()
    counts = []
    outcome = G.buy(client(post), suppliers, model="test-model", instructions=FIXTURE["instructions"],
                    input_text=FIXTURE["input"], max_output_tokens=2500, cutoff=cutoff, attempts=attempts,
                    on_attempt=counts.append, clock=clock, sleep=clock.sleep)
    return outcome, counts, clock


def test_buy_moves_to_the_next_supplier_when_busy():
    post = FakePost(Answer(429, {"error": {"code": "concurrency_queue_full"}}, {"Retry-After": "2"}), answer_ok())
    # The second supplier has no key, so the third request goes to the first supplier again.
    outcome, counts, clock = buy(post)
    assert outcome.call is not None and outcome.attempts == 1
    assert counts == [1, 0, 1, 0, 1]
    assert clock.slept[0] == 2.0
    assert [e["result"] for e in outcome.log] == ["busy", "no-key", "ok"]


def test_buy_never_pays_more_than_twice():
    post = FakePost(Answer(502, {"error": {"code": "upstream_error"}}), Answer(504, {"error": {"code": "x"}}),
                    answer_ok())
    outcome, counts, _ = buy(post, suppliers=SUPPLIERS[:1])
    assert outcome.call is None and outcome.attempts == 2
    assert "2 paid attempts" in outcome.reason
    assert len(post.bodies) == 2 and counts == [1, 2]


def test_buy_counts_earlier_attempts():
    outcome, _, _ = buy(FakePost(answer_ok()), attempts=2)
    assert outcome.call is None and outcome.attempts == 2


def test_buy_stops_on_a_refusal_and_at_the_cutoff():
    outcome, counts, _ = buy(FakePost(Answer(402, {"error": {"code": "insufficient_funds"}})))
    assert outcome.call is None and outcome.attempts == 0 and counts == [1, 0]
    assert "refused" in outcome.reason
    outcome, _, _ = buy(FakePost(answer_ok()), cutoff=1_030.0)
    assert outcome.call is None and "cut-off" in outcome.reason
    busy = [Answer(503, {"error": {"code": "supplier_capacity_timeout"}}, {"Retry-After": "30"})] * 3
    outcome, _, clock = buy(FakePost(*busy), cutoff=1_100.0, suppliers=SUPPLIERS[:1])
    assert outcome.call is None and outcome.attempts == 0 and "cut-off" in outcome.reason


def test_buy_retries_a_bad_receipt_once():
    bad = dict(FIXTURE["receipt"], completion_tokens=99)
    post = FakePost(answer_ok(bad), answer_ok())
    outcome, counts, _ = buy(post, suppliers=SUPPLIERS[:1])
    assert outcome.call is not None and outcome.attempts == 2
    assert outcome.log[0]["result"] == "failed" and outcome.log[0]["escrow_ref"] == ESCROW
