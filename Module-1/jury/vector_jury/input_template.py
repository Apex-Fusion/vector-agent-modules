"""The model input of the reference juror: template ``vector-jury-input/1``
(SPEC §13 step 1.2).

The input is ONE JSON object, built only from the case and its two
documents, so the same case and documents always give the same bytes::

    {
      "template": "vector-jury-input/1",
      "jury": "<J, hex>",
      "case": "<case token name, hex>",
      "challenge": "<challenge token name, hex>",
      "claim": "<claim token name, hex>",
      "claimer": "did:vector:<agent>",
      "auditor": "did:vector:<agent>",
      "deadline": "<challenge deadline, ISO 8601 UTC with milliseconds>",
      "documents": {
        "claim":    {"uri": "…", "hash": "<expected hash, hex>", "status": "ok|missing|mismatch",
                     "cut": false, "text": "…"},
        "evidence": {"uri": "…", "hash": "…", "status": "…", "cut": false, "text": "…"}
      }
    }

The juror prompts name ``documents.claim.text`` and
``documents.evidence.text`` as untrusted data; every other field is a case
fact.

Rules:

- ``hash`` is the hash that the case holds (``claim_hash``,
  ``evidence_hash``); ``status`` is the result of the hash check (SPEC
  §10.2). ``text`` is the document that was read (also on ``mismatch``), or
  ``""`` when it is ``missing``.
- Characters: document bytes are decoded as UTF-8 (an invalid byte becomes
  U+FFFD); U+0000 is removed; the other control characters except tab, line
  feed and carriage return, and DEL, become U+FFFD; the six characters
  ``\\u0000`` become ``\\u 0000``. So the input and the juror record never
  hold U+0000 or the escape ``\\u0000`` (SPEC §10.1).
- ``text`` is the longest prefix of the document text with at most
  ``DOC_LIMIT`` (3,500) bytes of UTF-8 whose record size (``record_size``:
  the bytes of the text after it is escaped as a JSON string twice, once in
  the input and once in the juror record) is at most ``DOC_RECORD_LIMIT``
  (4,300). ``cut`` is true when the text is shorter than the document text.
- ``uri`` is the longest prefix of the URI with at most ``URI_LIMIT`` (256)
  bytes of UTF-8 and a record size of at most ``URI_RECORD_LIMIT`` (300).
- Serialisation: ``json.dumps`` with ``ensure_ascii=False`` and the
  separators ``(",", ":")``, keys in the order above.

The record sizes keep every juror record at 15,000 bytes or less with any
documents (SPEC §10.4): the input appears in the record as a JSON string, so
a quote of a document costs 4 bytes there. Plain text and usual JSON
documents reach the 3,500-byte limit first or are cut a little earlier.

A change of these rules or of the layout needs a new template name.
"""

import dataclasses
import datetime
import json
from typing import Optional, Tuple

#: The name of this template (SPEC §10.3 ``input_template``).
TEMPLATE = "vector-jury-input/1"
#: The largest text of one document (UTF-8 bytes, SPEC §13).
DOC_LIMIT = 3500
#: The largest record size of the text of one document (bytes).
DOC_RECORD_LIMIT = 4300
#: The largest URI (UTF-8 bytes, SPEC §13) and its largest record size.
URI_LIMIT = 256
URI_RECORD_LIMIT = 300
#: The document statuses (SPEC §10.4).
STATUSES = ("ok", "missing", "mismatch")

_KEEP = {"\t", "\n", "\r"}


def clean_text(text: str) -> str:
    """The text without U+0000, with U+FFFD for the other control characters
    (except tab, line feed and carriage return) and DEL, and without the
    escape text ``\\u0000``."""
    out = []
    for char in text:
        if char == "\x00":
            continue
        if (ord(char) < 0x20 and char not in _KEEP) or char == "\x7f":
            out.append("\ufffd")
        else:
            out.append(char)
    return "".join(out).replace("\\u0000", "\\u 0000")


def _escaped(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


def record_size(text: str) -> int:
    """The bytes of ``text`` in a juror record: escaped as a JSON string in
    the input, and the input escaped again as a JSON string in the record."""
    return len(_escaped(_escaped(text)).encode("utf-8"))


def cut_text(text: str, byte_limit: int, record_limit: int) -> Tuple[str, bool]:
    """The longest prefix of ``text`` (whole characters) with at most
    ``byte_limit`` bytes of UTF-8 and a ``record_size`` of at most
    ``record_limit``, and whether it is shorter than ``text``."""
    if len(text.encode("utf-8")) <= byte_limit and record_size(text) <= record_limit:
        return text, False
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        prefix = text[:mid]
        if len(prefix.encode("utf-8")) <= byte_limit and record_size(prefix) <= record_limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], True


def decode(data: bytes) -> str:
    """Document bytes as clean text (UTF-8, invalid bytes as U+FFFD)."""
    return clean_text(bytes(data).decode("utf-8", errors="replace"))


def uri_text(uri) -> str:
    """A URI (bytes or text) as it appears in the input and in the record."""
    text = decode(uri) if isinstance(uri, (bytes, bytearray)) else clean_text(str(uri))
    return cut_text(text, URI_LIMIT, URI_RECORD_LIMIT)[0]


@dataclasses.dataclass(frozen=True)
class CaseFacts:
    """The facts of a case (hex values lowercase, without a prefix)."""

    jury: str
    case: str
    challenge: str
    claim: str
    claimer: str
    auditor: str
    deadline_ms: int


@dataclasses.dataclass(frozen=True)
class DocumentInput:
    """A document of the case: its URI (bytes or text), the hash that the case
    holds (hex), the status of the check and the bytes that were read (None
    when missing)."""

    uri: object
    expected_hash: str
    status: str
    data: Optional[bytes] = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")


def iso_time(ms: int) -> str:
    when = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def document_object(doc: DocumentInput) -> dict:
    text, cut = ("", False) if doc.data is None else cut_text(decode(doc.data), DOC_LIMIT, DOC_RECORD_LIMIT)
    return {"uri": uri_text(doc.uri), "hash": doc.expected_hash, "status": doc.status, "cut": cut, "text": text}


def input_object(facts: CaseFacts, claim: DocumentInput, evidence: DocumentInput) -> dict:
    return {
        "template": TEMPLATE,
        "jury": facts.jury,
        "case": facts.case,
        "challenge": facts.challenge,
        "claim": facts.claim,
        "claimer": f"did:vector:{facts.claimer}",
        "auditor": f"did:vector:{facts.auditor}",
        "deadline": iso_time(facts.deadline_ms),
        "documents": {"claim": document_object(claim), "evidence": document_object(evidence)},
    }


def render(facts: CaseFacts, claim: DocumentInput, evidence: DocumentInput) -> str:
    """The model input of ``vector-jury-input/1`` (one JSON object)."""
    return json.dumps(input_object(facts, claim, evidence), ensure_ascii=False, separators=(",", ":"))
