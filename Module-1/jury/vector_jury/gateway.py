"""Model calls of the reference juror through the marketplace gateway (SPEC §13
step 1.3) and the check of the signed receipt (SPEC §10.4).

The gateway sells one-shot calls of ``llm.text.generate.v1`` suppliers:
``POST <gateway>/openai/v1/responses`` with ``Authorization: Bearer <API
key>``. The body pins the supplier with ``x_vector.supplier_pkh``. A completed
answer has the normal Responses shape and the extension ``x_vector`` with the
supplier's signed receipt (9 fields) and the escrow of the call.

The receipt check follows ``verifyEscrowReceipt`` of the marketplace
(``packages/shared/src/receipt/verify.ts``):

- exactly the 9 fields, ``agent_did == did:vector:<supplier agent id>``;
- ``blake2b_224(supplier public key) == supplier_pkh`` (the pinned supplier);
- the receipt's ``supplier_pkh`` and ``escrow_ref`` are the pinned supplier
  and the escrow of ``x_vector``;
- an Ed25519 signature over the UTF-8 bytes of the canonical JSON of the
  receipt (``canonical_json``, a port of ``packages/shared/src/cbor/
  canonical.ts``).

The juror also recomputes the two hashes of the receipt, as a reader of the
record does (SPEC §10.4): ``response_hash = sha256(canonical({output, status,
incomplete_details}))`` and ``prompt_hash = sha256(canonical({input,
instructions, max_output_tokens}))`` with the input normalised as the
marketplace does (a string becomes one user message with one ``input_text``
part). The gateway caps ``max_output_tokens`` at the supplier's limit, so the
check also tries the capped value. A call without a verified receipt gives no
vote.

The supplier public key comes from the DID document of the supplier agent
(``verificationMethod`` ``#<supplier_pkh>``, Multikey ``z6Mk…``), else from
the supplier's ``/capability`` (``pub_key_hex``). Its key hash binds it to
the pinned supplier either way.

Errors and retries (``buy``):

- HTTP 429 and 409, and HTTP 503 with a code that the gateway sends before it
  funds an escrow, mean "busy": wait as ``Retry-After`` says, then try the
  next supplier of the list, until the cut-off time.
- Any other failure after the request went out (5xx, a timeout, a broken
  connection, an invalid receipt) may follow a funded escrow: the gateway
  settles or recovers it by itself, and a new request is a new paid call.
  ``buy`` counts every request that is not "busy" as an attempt and stops at
  ``max_attempts`` (2 per case and juror by default). The caller keeps the
  count durably, so a restart does not reset it.
- Other 4xx answers (bad key, no funds, unknown model, bad parameter) stop
  at once.

The API key never goes into a log line or an exception message.
"""

import dataclasses
import email.utils
import hashlib
import json
import math
import re
import time
import unicodedata
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

#: The production gateway.
GATEWAY_URL = "https://api.marketplace.vector.apexfusion.org"
RESPONSES_PATH = "/openai/v1/responses"
#: The public did:vector resolver.
RESOLVER_URL = "https://id.vector.apexfusion.org/1.0/identifiers/"
#: The fields of a receipt for an identity escrow (Part 2 §6.3).
RECEIPT_FIELDS = ("prompt_hash", "response_hash", "model", "prompt_tokens", "completion_tokens", "wallclock_ms",
                  "supplier_pkh", "escrow_ref", "agent_did")
#: HTTP 503 codes that the gateway sends before it funds an escrow.
UNFUNDED_503_CODES = frozenset({
    "supplier_capacity_timeout", "suppliers_unavailable", "supplier_preflight_failed",
    "supplier_identity_changed", "wallet_transaction_pending", "wallet_operation_timeout",
})
#: The wait after a busy answer without ``Retry-After`` (seconds), and the
#: bounds of a wait that the gateway asks for.
DEFAULT_RETRY_AFTER = 5.0
MIN_RETRY_AFTER = 1.0
MAX_RETRY_AFTER = 60.0
#: The shortest time (seconds) that ``buy`` gives a call before the cut-off.
MIN_CALL_SECONDS = 60.0
#: The longest wait for one answer (seconds). The gateway answers after
#: inference and settlement (the slowest supplier has a 60-minute SLA).
MAX_CALL_SECONDS = 3900.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX56 = re.compile(r"^[0-9a-f]{56}$")
_SIGNATURE = re.compile(r"^[0-9a-fA-F]{128}$")
_PUBLIC_KEY = re.compile(r"^[0-9a-fA-F]{64}$")
_ESCROW_REF = re.compile(r"^[0-9a-f]{64}#(0|[1-9][0-9]*)$")


# ── Canonical JSON (packages/shared/src/cbor/canonical.ts) ───────────────────


_ARRAY_INDEX = re.compile(r"^(0|[1-9][0-9]*)$")


def _js_number(value: float) -> str:
    """``JSON.stringify`` of a JavaScript number (ECMAScript Number::toString
    with the shortest round-trip digits)."""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(float(value)))
    mantissa, _, exponent = text.partition("e")
    whole, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    digits = (whole + fraction).lstrip("0")
    point = len(whole) + (int(exponent) if exponent else 0)
    if whole == "0" or not whole.strip("0"):
        # 0.000ddd: the point moves left by the leading zeros of the fraction.
        stripped = fraction.lstrip("0")
        point = (int(exponent) if exponent else 0) - (len(fraction) - len(stripped))
        digits = stripped
    digits = digits.rstrip("0") or "0"
    k, n = len(digits), point
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        exp = n - 1
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if exp >= 0 else "-") + str(abs(exp))
    return sign + out


def _js_string(text: str) -> str:
    """``JSON.stringify`` of an NFC-normalised string: no ASCII escapes for
    non-ASCII characters, lone surrogates as ``\\udxxx``."""
    out = json.dumps(unicodedata.normalize("NFC", text), ensure_ascii=False)
    return re.sub("[\ud800-\udfff]", lambda m: "\\u%04x" % ord(m.group()), out)


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def _object_keys(value: Mapping[str, Any]) -> List[Tuple[str, Any]]:
    """The entries of an object in the order of ``JSON.stringify(sortKeys(o))``:
    the keys sorted by UTF-16 code units, then NFC-normalised (a later key with
    the same normal form replaces the value in place), then array-index keys
    first in numeric order (JavaScript property order)."""
    ordered: Dict[str, Any] = {}
    for key in sorted(value, key=_utf16_key):
        if not isinstance(key, str):
            raise TypeError("canonical JSON object keys must be text")
        ordered[unicodedata.normalize("NFC", key)] = value[key]
    index_keys = sorted((k for k in ordered if _ARRAY_INDEX.match(k) and int(k) < 2 ** 32 - 1), key=int)
    indexes = set(index_keys)
    return [(k, ordered[k]) for k in index_keys + [k for k in ordered if k not in indexes]]


def canonical_json(value: Any) -> str:
    """The canonical JSON text of the marketplace: sorted keys, NFC strings,
    no whitespace, arrays in order, numbers as JavaScript writes them."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > 2 ** 53:
            return _js_number(float(value))
        return str(value)
    if isinstance(value, float):
        return _js_number(value)
    if isinstance(value, str):
        return _js_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(_js_string(k) + ":" + canonical_json(v) for k, v in _object_keys(value)) + "}"
    raise TypeError(f"canonical JSON cannot encode {type(value).__name__}")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Request and response commitments ─────────────────────────────────────────


def normalized_input(text: str) -> List[Dict[str, Any]]:
    """The normalised Responses input of a text input: one user message with
    one ``input_text`` part."""
    return [{"role": "user", "content": [{"type": "input_text", "text": text}], "type": "message"}]


def request_commitment(input_text: str, instructions: Optional[str], max_output_tokens: Optional[int]) -> Dict[str, Any]:
    """The execution fields of a request that the receipt's ``prompt_hash``
    commits (``responseRequestCommitment``)."""
    envelope: Dict[str, Any] = {"input": normalized_input(input_text)}
    if instructions is not None:
        envelope["instructions"] = instructions
    if max_output_tokens is not None:
        envelope["max_output_tokens"] = int(max_output_tokens)
    return envelope


def prompt_hash(input_text: str, instructions: Optional[str], max_output_tokens: Optional[int]) -> str:
    return sha256_hex(canonical_json(request_commitment(input_text, instructions, max_output_tokens)))


def committed_candidates(requested: int, *limits: Optional[int]) -> List[int]:
    """The values of ``max_output_tokens`` that the gateway can commit: the
    requested value, or the smaller of it and a known supplier limit."""
    out = [int(requested)]
    for limit in limits:
        if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < requested and limit not in out:
            out.append(limit)
    return out


def response_hash(response: Mapping[str, Any]) -> str:
    """``sha256(canonical({output, status, incomplete_details}))``."""
    return sha256_hex(canonical_json({
        "output": response.get("output"),
        "status": response.get("status"),
        "incomplete_details": response.get("incomplete_details"),
    }))


def _js_text(value: Any) -> str:
    """JavaScript ``String(value)`` for the values that ``text += part.text``
    can meet."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return canonical_json(value)
    if isinstance(value, list):
        return ",".join("" if v is None else _js_text(v) for v in value)
    return "[object Object]"


def output_text(output: Any) -> str:
    """The output text of a response (SPEC §10.4), a port of
    ``responseOutputText`` (``packages/shared/src/responses.ts``): for each
    ``message`` Item of role ``assistant``, the ``text`` of every content part
    that is not a ``refusal``, in order, without a separator."""
    text = ""
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") != "refusal":
                text += _js_text(part["text"]) if "text" in part else "undefined"
    return text


def refusal_text(output: Any) -> str:
    """The ``refusal`` parts of the assistant ``message`` Items, in order."""
    parts = []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
            continue
        parts += [p["refusal"] for p in item.get("content") or []
                  if isinstance(p, dict) and p.get("type") == "refusal" and isinstance(p.get("refusal"), str)]
    return "\n".join(parts)


# ── Keys and receipts ────────────────────────────────────────────────────────


def blake2b_224_hex(data: bytes) -> str:
    return hashlib.blake2b(bytes(data), digest_size=28).hexdigest()


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            raise ValueError("not base58btc")
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw


def multikey_ed25519(multibase: str) -> str:
    """The 32-byte Ed25519 public key (hex) of a Multikey ``z6Mk…`` value
    (base58btc, multicodec ``0xed01``)."""
    if not isinstance(multibase, str) or not multibase.startswith("z"):
        raise ValueError("not a base58btc multibase value")
    raw = _base58_decode(multibase[1:])
    if len(raw) != 34 or raw[:2] != b"\xed\x01":
        raise ValueError("not an Ed25519 Multikey")
    return raw[2:].hex()


def verify_signature(receipt: Mapping[str, Any], signature_hex: str, public_key_hex: str) -> bool:
    """Ed25519 over the UTF-8 bytes of ``canonical_json(receipt)``."""
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    if not isinstance(signature_hex, str) or not _SIGNATURE.match(signature_hex):
        return False
    if not isinstance(public_key_hex, str) or not _PUBLIC_KEY.match(public_key_hex):
        return False
    try:
        VerifyKey(bytes.fromhex(public_key_hex)).verify(canonical_json(dict(receipt)).encode("utf-8"),
                                                        bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def receipt_problems(receipt: Any, signature_hex: Any, public_key_hex: str, *, escrow_ref: str,
                     supplier_pkh: str, agent_id: str) -> List[str]:
    """The checks of ``verifyEscrowReceipt`` that fail (empty when the receipt
    is valid for the escrow of an identity advert)."""
    if not isinstance(receipt, dict):
        return ["the receipt is not a JSON object"]
    problems = []
    if sorted(receipt) != sorted(RECEIPT_FIELDS):
        problems.append(f"the receipt has the fields {sorted(receipt)}, not the 9 receipt fields")
    if not _HEX64.match(str(agent_id)):
        problems.append("the supplier agent id is not 64 lowercase hex characters")
    elif receipt.get("agent_did") != f"did:vector:{agent_id}":
        problems.append(f"agent_did is {receipt.get('agent_did')!r}, not did:vector:{agent_id}")
    if not isinstance(public_key_hex, str) or not _PUBLIC_KEY.match(public_key_hex):
        problems.append("the supplier public key is not 32 bytes of hex")
    elif blake2b_224_hex(bytes.fromhex(public_key_hex)) != supplier_pkh:
        problems.append("blake2b_224 of the supplier public key is not the supplier_pkh")
    if receipt.get("supplier_pkh") != supplier_pkh:
        problems.append(f"the receipt names the supplier {receipt.get('supplier_pkh')!r}, not {supplier_pkh}")
    if receipt.get("escrow_ref") != escrow_ref:
        problems.append(f"the receipt names the escrow {receipt.get('escrow_ref')!r}, not {escrow_ref}")
    for field in ("prompt_hash", "response_hash"):
        if not isinstance(receipt.get(field), str) or not _HEX64.match(receipt[field]):
            problems.append(f"{field} is not 32 bytes of lowercase hex")
    for field in ("prompt_tokens", "completion_tokens", "wallclock_ms"):
        value = receipt.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not 0 <= value <= 2 ** 53:
            problems.append(f"{field} is not a non-negative number (buildReceipt)")
    if not isinstance(receipt.get("model"), str) or not receipt["model"]:
        problems.append("model is not a non-empty string")
    if not problems and not verify_signature(receipt, signature_hex, public_key_hex):
        problems.append("the Ed25519 signature of the receipt is not valid")
    return problems


# ── Supplier keys ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SupplierInfo:
    """The public key of a supplier and the output token limit of its
    ``/capability`` (None when unknown)."""

    agent: str
    supplier_pkh: str
    pub_key: str
    max_output_tokens: Optional[int]
    source: str


class SupplierKeys:
    """Supplier public keys from the DID document (resolver), else from the
    supplier ``/capability``. Each key is checked against the key hash and
    cached."""

    def __init__(self, *, resolver_url: str = RESOLVER_URL, get: Callable = requests.get, timeout: float = 20,
                 ttl: float = 600, clock: Callable[[], float] = time.monotonic):
        self.resolver_url = resolver_url
        self._get = get
        self.timeout = timeout
        self.ttl = ttl
        self._clock = clock
        self._cache: Dict[Tuple[str, str], Tuple[float, SupplierInfo]] = {}

    def _json(self, url: str, accept: str = "application/json") -> Any:
        response = self._get(url, timeout=self.timeout, headers={"Accept": accept})
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        return response.json()

    def lookup(self, agent: str, supplier_pkh: str) -> SupplierInfo:
        key = (agent, supplier_pkh)
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < self.ttl:
            return cached[1]
        errors, pub_key, endpoint, source = [], None, None, ""
        try:
            # The resolution result (didDocument + didDocumentMetadata); with
            # Accept: application/json the resolver gives the bare DID document.
            doc = self._json(f"{self.resolver_url}did:vector:{agent}", accept="application/did-resolution")
            if not isinstance(doc.get("didDocument"), dict):
                raise ValueError("the answer is not a DID resolution result")
            meta = doc.get("didDocumentMetadata") or {}
            endpoint = ((meta.get("vectorProfile") or {}).get("endpoint")) or None
            for method in doc["didDocument"].get("verificationMethod") or []:
                if str(method.get("id", "")).endswith(f"#{supplier_pkh}"):
                    pub_key, source = multikey_ed25519(method.get("publicKeyMultibase")), "did"
        except (requests.RequestException, ValueError, AttributeError, TypeError) as error:
            errors.append(f"resolver: {error}")
        cap_tokens = None
        if endpoint:
            try:
                capability = self._json(endpoint.rstrip("/") + "/capability")
                if capability.get("supplier_pkh") == supplier_pkh:
                    tokens = capability.get("max_output_tokens")
                    cap_tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None
                    if pub_key is None and isinstance(capability.get("pub_key_hex"), str):
                        pub_key, source = capability["pub_key_hex"].lower(), "capability"
            except (requests.RequestException, ValueError, AttributeError, TypeError) as error:
                errors.append(f"capability: {error}")
        if pub_key is None or not _PUBLIC_KEY.match(pub_key):
            raise SupplierKeyError(f"no public key for supplier {supplier_pkh} ({'; '.join(errors) or 'not listed'})")
        if blake2b_224_hex(bytes.fromhex(pub_key)) != supplier_pkh:
            raise SupplierKeyError(f"the {source} key of supplier {supplier_pkh} does not hash to it")
        info = SupplierInfo(agent, supplier_pkh, pub_key, cap_tokens, source)
        self._cache[key] = (self._clock(), info)
        return info


class SupplierKeyError(RuntimeError):
    """No public key that hashes to the supplier's key hash."""


# ── Gateway ──────────────────────────────────────────────────────────────────


class GatewayError(RuntimeError):
    """A gateway call failed. ``kind`` is ``busy`` (no escrow was funded; try
    again later), ``stop`` (a 4xx answer that a retry does not fix) or
    ``failed`` (the call may have been funded)."""

    def __init__(self, kind: str, message: str, *, status: Optional[int] = None, code: Optional[str] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.retry_after = retry_after


class ReceiptError(GatewayError):
    """The answer has no valid receipt for the call."""

    def __init__(self, message: str, *, call: Optional["ModelCall"] = None):
        super().__init__("failed", message)
        self.call = call


def retry_after_seconds(value: Optional[str], *, now: Optional[float] = None) -> float:
    """The wait of a ``Retry-After`` header (seconds or an HTTP date), within
    [1, 60] s; 5 s when the header is missing or not valid."""
    if value is None or not str(value).strip():
        return DEFAULT_RETRY_AFTER
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER
        seconds = when.timestamp() - (time.time() if now is None else now)
    if math.isnan(seconds):
        return DEFAULT_RETRY_AFTER
    return min(MAX_RETRY_AFTER, max(MIN_RETRY_AFTER, seconds))


def classify(status: int, code: Optional[str]) -> str:
    """``busy``, ``stop`` or ``failed`` for an HTTP error status."""
    if status in (409, 429) or (status == 503 and code in UNFUNDED_503_CODES):
        return "busy"
    if 400 <= status < 500 and status != 408:
        return "stop"
    return "failed"


@dataclasses.dataclass
class ModelCall:
    """A verified model call: the request as sent, the full answer, its text
    and the signed receipt."""

    supplier_agent: str
    supplier_pkh: str
    supplier_pub_key: str
    model: str
    instructions: str
    input: str
    max_output_tokens: int
    committed_max_output_tokens: int
    response: Dict[str, Any]
    receipt: Dict[str, Any]
    receipt_signature: str
    escrow_ref: str
    elapsed_ms: int

    @property
    def output(self) -> List[Any]:
        return list(self.response.get("output") or [])

    @property
    def status(self) -> Any:
        return self.response.get("status")

    @property
    def incomplete_details(self) -> Any:
        return self.response.get("incomplete_details")

    @property
    def text(self) -> str:
        return output_text(self.response.get("output"))

    @property
    def refusal(self) -> str:
        return refusal_text(self.response.get("output"))

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ModelCall":
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls)})


def check_call(call: ModelCall, supplier: SupplierInfo) -> List[str]:
    """Every check of a model call: the receipt (``receipt_problems``), its
    model, and the two hashes recomputed from the request and the answer."""
    problems = receipt_problems(call.receipt, call.receipt_signature, call.supplier_pub_key,
                                escrow_ref=call.escrow_ref, supplier_pkh=call.supplier_pkh,
                                agent_id=call.supplier_agent)
    if call.supplier_pub_key != supplier.pub_key:
        problems.append("the public key of the call is not the supplier's key")
    if call.receipt.get("model") != call.model:
        problems.append(f"the receipt names the model {call.receipt.get('model')!r}, not {call.model!r}")
    if call.receipt.get("response_hash") != response_hash(call.response):
        problems.append("response_hash does not match the answer")
    if call.receipt.get("prompt_hash") != prompt_hash(call.input, call.instructions, call.committed_max_output_tokens):
        problems.append("prompt_hash does not match the request")
    if call.status not in ("completed", "incomplete"):
        problems.append(f"the response status is {call.status!r}")
    if not _ESCROW_REF.match(str(call.escrow_ref)):
        problems.append("the escrow reference is not <tx id>#<index>")
    return problems


class Gateway:
    """The Responses endpoint of the gateway with one API key."""

    def __init__(self, api_key: str, *, url: str = GATEWAY_URL, post: Callable = requests.post,
                 keys: Optional[SupplierKeys] = None, connect_timeout: float = 15):
        if not api_key:
            raise ValueError("the gateway API key is empty")
        self._api_key = api_key
        self.url = url.rstrip("/")
        self._post = post
        self.keys = keys or SupplierKeys()
        self.connect_timeout = connect_timeout

    def __repr__(self) -> str:
        return f"Gateway({self.url!r}, api_key=<hidden>)"

    def body(self, *, model: str, instructions: str, input_text: str, max_output_tokens: int,
             supplier_pkh: str) -> Dict[str, Any]:
        return {"model": model, "instructions": instructions, "input": input_text,
                "max_output_tokens": int(max_output_tokens), "store": False,
                "x_vector": {"supplier_pkh": supplier_pkh}}

    def call(self, *, supplier_agent: str, supplier_pkh: str, model: str, instructions: str, input_text: str,
             max_output_tokens: int, timeout: float, clock: Callable[[], float] = time.monotonic) -> ModelCall:
        """One request, pinned to ``supplier_pkh``. Returns a verified call or
        raises GatewayError (``ReceiptError`` for a receipt that fails)."""
        supplier = self.keys.lookup(supplier_agent, supplier_pkh)
        body = self.body(model=model, instructions=instructions, input_text=input_text,
                         max_output_tokens=max_output_tokens, supplier_pkh=supplier_pkh)
        started = clock()
        try:
            response = self._post(f"{self.url}{RESPONSES_PATH}", json=body, timeout=(self.connect_timeout, timeout),
                                  headers={"Authorization": f"Bearer {self._api_key}",
                                           "Content-Type": "application/json"})
        except requests.RequestException as error:
            if _never_sent(error):
                raise GatewayError("busy", f"the gateway did not accept the connection: {type(error).__name__}",
                                   retry_after=DEFAULT_RETRY_AFTER) from None
            raise GatewayError("failed", f"the request failed after it was sent: {type(error).__name__}") from None
        elapsed_ms = int((clock() - started) * 1000)
        if response.status_code != 200:
            code, message = None, ""
            try:
                error = response.json().get("error") or {}
                code, message = error.get("code"), str(error.get("message") or "")[:300]
            except (ValueError, AttributeError):
                message = response.text[:300] if isinstance(response.text, str) else ""
            kind = classify(response.status_code, code)
            raise GatewayError(kind, f"HTTP {response.status_code} {code or ''}: {message}".strip(),
                               status=response.status_code, code=code,
                               retry_after=retry_after_seconds(response.headers.get("Retry-After"))
                               if kind == "busy" else None)
        try:
            data = response.json()
        except ValueError:
            raise GatewayError("failed", "the answer is not JSON") from None
        vector = data.get("x_vector") if isinstance(data, dict) else None
        if not isinstance(vector, dict) or not isinstance(vector.get("receipt"), dict):
            raise ReceiptError("the answer has no x_vector receipt")
        answer = {k: v for k, v in data.items() if k != "x_vector"}
        call = ModelCall(supplier_agent=supplier_agent, supplier_pkh=supplier_pkh, supplier_pub_key=supplier.pub_key,
                         model=model, instructions=instructions, input=input_text,
                         max_output_tokens=int(max_output_tokens), committed_max_output_tokens=int(max_output_tokens),
                         response=answer, receipt=dict(vector["receipt"]),
                         receipt_signature=str(vector.get("receipt_signature") or ""),
                         escrow_ref=str(vector.get("escrow_ref") or ""), elapsed_ms=elapsed_ms)
        # The gateway commits the smaller of the requested max_output_tokens
        # and the advert limit (SPEC §10.4): take the value that matches.
        for value in committed_candidates(call.max_output_tokens, supplier.max_output_tokens):
            if call.receipt.get("prompt_hash") == prompt_hash(input_text, instructions, value):
                call.committed_max_output_tokens = value
                break
        problems = check_call(call, supplier)
        if problems:
            raise ReceiptError("receipt not verified: " + "; ".join(problems), call=call)
        return call


def _never_sent(error: BaseException) -> bool:
    """True when ``requests`` failed before the request reached the gateway:
    a connect timeout, or no connection at all (DNS, refused)."""
    if isinstance(error, requests.ConnectTimeout):
        return True
    if not isinstance(error, requests.ConnectionError):
        return False
    try:
        from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
    except ImportError:  # pragma: no cover - urllib3 comes with requests
        return False
    reason = getattr(error.args[0], "reason", None) if error.args else None
    return isinstance(reason, (NewConnectionError, ConnectTimeoutError))


@dataclasses.dataclass(frozen=True)
class Supplier:
    agent: str
    supplier_pkh: str


@dataclasses.dataclass
class BuyOutcome:
    """The result of ``buy``: a verified call, or the reason why there is
    none. ``attempts`` counts the requests that may have been funded."""

    call: Optional[ModelCall]
    attempts: int
    reason: str = ""
    log: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


def buy(gateway: Gateway, suppliers: Sequence[Supplier], *, model: str, instructions: str, input_text: str,
        max_output_tokens: int, cutoff: float, attempts: int = 0, max_attempts: int = 2,
        on_attempt: Callable[[int], None] = lambda _n: None, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep) -> BuyOutcome:
    """Buy one verified model call. ``cutoff`` (POSIX seconds) is the latest
    time for an answer; ``attempts`` is the number of possibly funded
    requests made before for this case (``on_attempt(n)`` stores the new count
    BEFORE each request goes out, and again when an answer shows that no
    escrow was funded). The suppliers are tried in order, and the next one
    after each busy answer."""
    if not suppliers:
        return BuyOutcome(None, attempts, "no supplier is configured")
    index, log = 0, []
    while True:
        if attempts >= max_attempts:
            return BuyOutcome(None, attempts, f"{attempts} paid attempts made; no further call", log)
        remaining = cutoff - clock()
        if remaining < MIN_CALL_SECONDS:
            return BuyOutcome(None, attempts, "the cut-off time is reached", log)
        supplier = suppliers[index % len(suppliers)]
        attempts += 1
        on_attempt(attempts)
        entry = {"supplier_pkh": supplier.supplier_pkh, "attempt": attempts}
        try:
            call = gateway.call(supplier_agent=supplier.agent, supplier_pkh=supplier.supplier_pkh, model=model,
                                instructions=instructions, input_text=input_text,
                                max_output_tokens=max_output_tokens, timeout=min(remaining, MAX_CALL_SECONDS))
        except SupplierKeyError as error:
            attempts -= 1
            on_attempt(attempts)
            log.append({**entry, "result": "no-key", "error": str(error)})
            index += 1
            if index >= len(suppliers) * 3:
                return BuyOutcome(None, attempts, f"no supplier key: {error}", log)
            sleep(DEFAULT_RETRY_AFTER)
            continue
        except GatewayError as error:
            failed = {**entry, "result": error.kind, "status": error.status, "code": error.code,
                      "error": str(error)[:300]}
            if isinstance(error, ReceiptError) and error.call is not None:
                failed["escrow_ref"] = error.call.escrow_ref
            log.append(failed)
            if error.kind in ("busy", "stop"):
                # The gateway answered before it funded an escrow.
                attempts -= 1
                on_attempt(attempts)
            if error.kind == "stop":
                return BuyOutcome(None, attempts, f"the gateway refused the call: {error}", log)
            index += 1
            wait = error.retry_after if error.retry_after is not None else DEFAULT_RETRY_AFTER
            if clock() + wait + MIN_CALL_SECONDS > cutoff:
                return BuyOutcome(None, attempts, f"the cut-off time is reached (last answer: {error.kind})", log)
            sleep(wait)
            continue
        log.append({**entry, "result": "ok", "escrow_ref": call.escrow_ref, "elapsed_ms": call.elapsed_ms})
        return BuyOutcome(call, attempts, "", log)
