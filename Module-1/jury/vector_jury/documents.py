"""Documents on chain (SPEC §10).

A document is a UTF-8 byte string. A transaction carries one document as
metadata under label 22084::

    22084: {"v": 1, "t": <type>, "m": <media type>, "d": [<chunk>, ...]}

``chunk`` splits a document into text chunks of at most 64 bytes, only at
character boundaries, and never starts a chunk with ``0x``. ``metadata`` and
``auxiliary_data`` give the pycardano objects of a document.
``parse_metadata`` reads the document back from pycardano objects, CBOR,
Koios ``tx_metadata`` JSON and Ogmios JSON (plain, detailed schema or CBOR).
A reader rejects a chunk that is not text, that is longer than 64 bytes or
that starts with ``0x`` (some indexers show byte strings as ``0x…`` text).

``uri_for`` and ``txid_from_uri`` convert between a transaction id and its
``vector-tx:`` URI (unpadded base64url, §10.2). ``fetch`` reads the bytes of
a ``vector-tx:`` URI (Koios ``tx_metadata``) or an ``http(s)`` URI;
``check`` also compares the hash (``ok``, ``missing`` or ``mismatch``, the
statuses of the juror record, §10.4).
"""

import base64
import binascii
import dataclasses
import hashlib
import json
from typing import Any, Iterable, List, Optional, Union

import cbor2
from pycardano import AuxiliaryData, Metadata

#: The metadata label of jury documents.
LABEL = 22084
#: The format version (``v``).
VERSION = 1
#: The ledger limit of a metadata string, in bytes.
MAX_CHUNK = 64
#: The document types of SPEC §10.1.
TYPES = ("claim", "evidence", "juror-prompt", "juror-record", "juror-record-ref", "resolution")
URI_PREFIX = "vector-tx:"
#: The largest document that ``fetch`` reads from an http(s) URI.
MAX_FETCH_BYTES = 4 * 1024 * 1024


class DocumentError(ValueError):
    """A document, its metadata or its URI is not valid, or cannot be read."""


def doc_hash(doc: bytes) -> bytes:
    """blake2b-256 of the exact document bytes."""
    return hashlib.blake2b(bytes(doc), digest_size=32).digest()


# ── Chunks ───────────────────────────────────────────────────────────────────


def _text(doc: Union[bytes, bytearray, memoryview]) -> str:
    try:
        return bytes(doc).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentError(f"the document is not valid UTF-8: {error}") from None


def chunk(doc: bytes) -> List[str]:
    """The text chunks of a document: each is valid UTF-8 of at most 64 bytes,
    the split is only at character boundaries, and no chunk starts with
    ``0x``. The UTF-8 bytes of the chunks, joined, are the document. An empty
    document has no chunk. Raises DocumentError for bytes that are not
    UTF-8."""
    text = _text(doc)
    chunks: List[str] = []
    start = 0
    while start < len(text):
        if text.startswith("0x", start):
            if not chunks:
                # The document starts with "0x": "0" is a chunk of its own,
                # and the next chunk starts with "x".
                chunks.append("0")
                start += 1
                continue
            # Move the boundary back by one character: the new chunk starts
            # with the last character of the previous chunk, then "0", which
            # is never "0x". The previous chunk ended because the next
            # character did not fit, so it holds more than 60 bytes (at least
            # 15 characters) and stays non-empty.
            chunks[-1] = chunks[-1][:-1]
            start -= 1
        end, size = start, 0
        while end < len(text):
            width = len(text[end].encode("utf-8"))
            if size + width > MAX_CHUNK:
                break
            size += width
            end += 1
        chunks.append(text[start:end])
        start = end
    return chunks


def join(chunks: Any) -> bytes:
    """The document of a list of chunks (checked as a reader checks them)."""
    if not isinstance(chunks, list):
        raise DocumentError("the field d is not a list")
    parts = []
    for i, part in enumerate(chunks):
        if not isinstance(part, str):
            raise DocumentError(f"chunk {i} is not text")
        raw = part.encode("utf-8")
        if len(raw) > MAX_CHUNK:
            raise DocumentError(f"chunk {i} has {len(raw)} bytes, more than {MAX_CHUNK}")
        if part.startswith("0x"):
            raise DocumentError(f"chunk {i} starts with 0x")
        parts.append(raw)
    return b"".join(parts)


# ── Metadata ─────────────────────────────────────────────────────────────────


def _short_text(value: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise DocumentError(f"the {what} must be non-empty text")
    if len(value.encode("utf-8")) > MAX_CHUNK:
        raise DocumentError(f"the {what} has more than {MAX_CHUNK} bytes")
    if value.startswith("0x"):
        raise DocumentError(f"the {what} must not start with 0x")
    return value


def metadatum(doc: bytes, doc_type: str, media: str = "application/json") -> dict:
    """The label 22084 value of a document. Writers refuse a document with
    U+0000 or the JSON escape ``\\u0000`` (some indexers drop such
    metadata)."""
    if b"\x00" in bytes(doc) or b"\\u0000" in bytes(doc):
        raise DocumentError("the document contains U+0000 or the escape \\u0000")
    return {"v": VERSION, "t": _short_text(doc_type, "document type"), "m": _short_text(media, "media type"),
            "d": chunk(doc)}


def metadata(doc: bytes, doc_type: str, media: str = "application/json") -> Metadata:
    """The transaction metadata ``{22084: {...}}`` of a document."""
    return Metadata({LABEL: metadatum(doc, doc_type, media)})


def auxiliary_data(doc: bytes, doc_type: str, media: str = "application/json") -> AuxiliaryData:
    """The auxiliary data of a transaction that carries the document (the
    Shelley form: the metadata map only)."""
    return AuxiliaryData(metadata(doc, doc_type, media))


@dataclasses.dataclass(frozen=True)
class Document:
    """A document read from metadata."""

    type: str
    media: str
    data: bytes

    @property
    def hash(self) -> bytes:
        return doc_hash(self.data)

    @property
    def text(self) -> str:
        return self.data.decode("utf-8")


# ── Reading metadata ─────────────────────────────────────────────────────────

_DETAILED = {"int", "string", "bytes", "list", "map"}


def _detailed(value: Any) -> Any:
    """Plain values of the Ogmios detailed metadata schema
    (``{"int": n}``, ``{"string": s}``, ``{"bytes": hex}``, ``{"list": [..]}``,
    ``{"map": [{"k": .., "v": ..}]}``)."""
    if not isinstance(value, dict) or len(value) != 1 or next(iter(value)) not in _DETAILED:
        raise DocumentError("not the Ogmios detailed metadata schema")
    kind, inner = next(iter(value.items()))
    if kind == "int":
        return int(inner)
    if kind == "string":
        return str(inner)
    if kind == "bytes":
        return bytes.fromhex(inner)
    if kind == "list":
        return [_detailed(v) for v in inner]
    return {_key(_detailed(e["k"])): _detailed(e["v"]) for e in inner}


def _key(value: Any) -> Any:
    return value if isinstance(value, (str, int)) else bytes(value)


def _is_detailed(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _DETAILED


def _ogmios_label(value: Any) -> Any:
    """The metadatum of an Ogmios label entry: ``{"json": ..}`` (plain or
    detailed) or ``{"cbor": hex}``."""
    if isinstance(value, dict) and "cbor" in value and "json" not in value:
        try:
            return cbor2.loads(bytes.fromhex(value["cbor"]))
        except (ValueError, cbor2.CBORDecodeError) as error:
            raise DocumentError(f"the Ogmios metadatum CBOR does not decode: {error}") from None
    if isinstance(value, dict) and "json" in value:
        inner = value["json"]
        return _detailed(inner) if _is_detailed(inner) else inner
    return value


def _label_value(labels: Any) -> Any:
    if not isinstance(labels, dict):
        raise DocumentError("the transaction metadata is not a map of labels")
    for key in (LABEL, str(LABEL)):
        if key in labels:
            return labels[key]
    raise DocumentError(f"the transaction has no metadata label {LABEL}")


def _from_cbor(raw: bytes) -> Any:
    try:
        value = cbor2.loads(raw)
    except cbor2.CBORDecodeError as error:
        raise DocumentError(f"the metadata CBOR does not decode: {error}") from None
    # A transaction [body, witnesses, valid, auxiliary data].
    if isinstance(value, list) and len(value) == 4 and isinstance(value[0], dict):
        value = value[3]
        if value is None:
            raise DocumentError("the transaction has no auxiliary data")
    # Alonzo auxiliary data: tag 259 {0: metadata, ...}.
    if isinstance(value, cbor2.CBORTag) and value.tag == 259:
        value = value.value.get(0) if isinstance(value.value, dict) else None
        if value is None:
            raise DocumentError("the auxiliary data has no metadata")
    # Shelley-Mary auxiliary data: [metadata, scripts].
    elif isinstance(value, list) and len(value) == 2 and isinstance(value[0], dict):
        value = value[0]
    return _label_value(value)


def _metadatum_of(source: Any, tx_hash: Optional[str]) -> Any:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _from_cbor(bytes(source))
    if hasattr(source, "auxiliary_data") and hasattr(source, "transaction_body"):
        if source.auxiliary_data is None:
            raise DocumentError("the transaction has no auxiliary data")
        source = source.auxiliary_data
    if hasattr(source, "to_cbor") and not isinstance(source, (dict, list)):
        raw = source.to_cbor()
        return _from_cbor(raw if isinstance(raw, bytes) else bytes.fromhex(raw))
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            raise DocumentError("the metadata text is not JSON") from None
    if isinstance(source, list):  # Koios tx_metadata response
        entries = [e for e in source if isinstance(e, dict) and (tx_hash is None or e.get("tx_hash") == tx_hash)]
        if len(entries) != 1:
            raise DocumentError(f"the Koios answer has {len(entries)} matching entries, expected one")
        source = entries[0]
    if isinstance(source, dict) and "tx_hash" in source and "metadata" in source:  # one Koios entry
        if source["metadata"] is None:
            raise DocumentError(f"transaction {source['tx_hash']} has no metadata")
        return _label_value(source["metadata"])
    if isinstance(source, dict) and "labels" in source:  # Ogmios transaction metadata
        return _ogmios_label(_label_value(source["labels"]))
    if isinstance(source, dict):  # a label map (Koios metadata object or Ogmios labels)
        return _ogmios_label(_label_value(source))
    raise DocumentError(f"cannot read metadata from {type(source).__name__}")


def parse_metadata(source: Any, *, tx_hash: Optional[str] = None) -> Document:
    """The label 22084 document of transaction metadata. ``source`` is a
    pycardano ``Transaction``, ``AuxiliaryData`` or ``Metadata``; CBOR bytes
    (a transaction, auxiliary data or a metadata map); Koios ``tx_metadata``
    JSON (the answer list, one entry or its ``metadata`` object; ``tx_hash``
    picks the entry); or Ogmios JSON (``{"labels": ..}`` with ``json``
    (plain or detailed schema) or ``cbor``). Raises DocumentError."""
    value = _metadatum_of(source, tx_hash)
    if not isinstance(value, dict):
        raise DocumentError(f"label {LABEL} is not a map")
    version = value.get("v")
    if isinstance(version, bool) or version != VERSION:
        raise DocumentError(f"label {LABEL} has version {version!r}, expected {VERSION}")
    doc_type, media = value.get("t"), value.get("m")
    if not isinstance(doc_type, str) or not isinstance(media, str):
        raise DocumentError(f"label {LABEL} needs text fields t and m")
    if doc_type.startswith("0x") or media.startswith("0x"):
        raise DocumentError(f"label {LABEL}: t or m starts with 0x")
    if "d" not in value:
        raise DocumentError(f"label {LABEL} has no field d")
    return Document(doc_type, media, join(value["d"]))


# ── URIs ─────────────────────────────────────────────────────────────────────


def _tx_bytes(tx_id: Union[str, bytes]) -> bytes:
    raw = bytes.fromhex(tx_id) if isinstance(tx_id, str) else bytes(tx_id)
    if len(raw) != 32:
        raise DocumentError("a transaction id has 32 bytes")
    return raw


def uri_for(tx_id: Union[str, bytes]) -> str:
    """``vector-tx:`` + unpadded base64url of the 32-byte transaction id
    (53 characters)."""
    return URI_PREFIX + base64.urlsafe_b64encode(_tx_bytes(tx_id)).rstrip(b"=").decode("ascii")


def txid_from_uri(uri: Union[str, bytes]) -> str:
    """The transaction id (64 hex) of a ``vector-tx:`` URI. Raises
    DocumentError for another URI or an encoding that is not canonical."""
    if isinstance(uri, (bytes, bytearray)):
        try:
            uri = bytes(uri).decode("ascii")
        except UnicodeDecodeError:
            raise DocumentError("the URI is not ASCII") from None
    if not isinstance(uri, str) or not uri.startswith(URI_PREFIX):
        raise DocumentError(f"not a {URI_PREFIX} URI: {uri!r}")
    body = uri[len(URI_PREFIX):]
    if len(body) != 43 or not all(c.isalnum() or c in "-_" for c in body):
        raise DocumentError(f"{uri!r}: expected 43 base64url characters after {URI_PREFIX}")
    try:
        raw = base64.urlsafe_b64decode(body + "=")
    except (binascii.Error, ValueError):
        raise DocumentError(f"{uri!r} is not base64url") from None
    if len(raw) != 32 or uri_for(raw) != uri:
        raise DocumentError(f"{uri!r} is not the canonical URI of a transaction id")
    return raw.hex()


# ── Fetching ─────────────────────────────────────────────────────────────────


def _as_text(uri: Union[str, bytes]) -> str:
    if isinstance(uri, (bytes, bytearray)):
        try:
            return bytes(uri).decode("utf-8")
        except UnicodeDecodeError:
            raise DocumentError("the URI is not UTF-8") from None
    return uri


def fetch_document(uri: Union[str, bytes], *, koios=None, timeout: int = 30) -> Document:
    """The document of a ``vector-tx:`` URI (``read_document`` of its
    transaction id)."""
    return read_document(txid_from_uri(uri), koios=koios, timeout=timeout)


def read_document(tx_id: str, *, koios=None, timeout: int = 30) -> Document:
    """The document in the metadata of a transaction, read with Koios
    ``tx_metadata``. When Koios gives no JSON for label 22084, the reader
    uses the CBOR of the transaction (Koios ``tx_cbor``). ``koios`` is a
    ``vector_jury.chain.Koios`` (default: Vector mainnet)."""
    if koios is None:
        from .chain import Koios, network_preset

        koios = Koios(network_preset("mainnet").koios_url, timeout=timeout)
    try:
        answer = koios.tx_metadata([tx_id])
    except Exception as error:  # noqa: BLE001 - network errors
        raise DocumentError(f"Koios tx_metadata of {tx_id}: {error}") from None
    entry = next((e for e in answer or [] if isinstance(e, dict) and e.get("tx_hash") == tx_id), None)
    if entry is None:
        raise DocumentError(f"Koios shows no transaction {tx_id}")
    metadata = entry.get("metadata")
    if isinstance(metadata, dict) and (str(LABEL) in metadata or LABEL in metadata):
        return parse_metadata(entry)
    try:
        rows = koios.tx_cbor([tx_id])
    except Exception:  # noqa: BLE001 - Vector Koios keeps no CBOR; Blockfrost below
        rows = []
    raw = next((r.get("cbor") for r in rows or [] if r.get("tx_hash") == tx_id), None)
    if raw:
        return parse_metadata(bytes.fromhex(raw))
    return _blockfrost_document(tx_id, timeout)


#: Vector Blockfrost (no /api/v0 prefix on this instance).
BLOCKFROST_URL = "https://blockfrost.vector.mainnet.apexfusion.org"


def _blockfrost_document(tx_id: str, timeout: int) -> Document:
    """The label 22084 document from Blockfrost ``/txs/<hash>/metadata/cbor``
    (``[{label, metadata | cbor_metadata}]``, CBOR hex)."""
    import requests

    try:
        response = requests.get(f"{BLOCKFROST_URL}/txs/{tx_id}/metadata/cbor", timeout=timeout)
        rows = response.json() if response.status_code == 200 else []
    except (requests.RequestException, ValueError) as error:
        raise DocumentError(f"Blockfrost metadata of {tx_id}: {error}") from None
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("label")) != str(LABEL):
            continue
        text = (row.get("metadata") or row.get("cbor_metadata") or "").removeprefix("\\x")
        raw = bytes.fromhex(text)
        try:
            return parse_metadata(raw)
        except DocumentError:
            return parse_metadata(cbor2.dumps({LABEL: cbor2.loads(raw)}))
    raise DocumentError(f"transaction {tx_id} has no label {LABEL} document (no JSON and no CBOR)")


@dataclasses.dataclass(frozen=True)
class FoundRecord:
    """A juror record found by its hash (SPEC §10.4 overflow): ``where`` is
    ``reveal`` (in the reveal transaction), ``overflow`` (a separate
    ``juror-record`` transaction) or ``missing``."""

    where: str
    data: Optional[bytes] = None
    tx_id: Optional[str] = None


def find_record(record_hash: Union[bytes, str], *, reveal_tx: Optional[str] = None,
                candidate_txs: Iterable[str] = (), koios=None) -> FoundRecord:
    """The juror record with ``record_hash`` (SPEC §10.4): in the metadata of
    the reveal transaction (``reveal``); or, when the reveal carries a
    ``juror-record-ref`` document, in the transaction of its ``vector-tx:``
    URI (``overflow``); else in the ``juror-record`` documents of the
    candidate transactions (``overflow``). The hash is always checked."""
    expected = bytes.fromhex(record_hash) if isinstance(record_hash, str) else bytes(record_hash)
    candidates = list(candidate_txs)
    if reveal_tx:
        try:
            doc = read_document(reveal_tx, koios=koios)
        except DocumentError:
            doc = None
        if doc is not None and doc.type == "juror-record" and doc.hash == expected:
            return FoundRecord("reveal", doc.data, reveal_tx)
        if doc is not None and doc.type == "juror-record-ref":
            try:
                candidates.insert(0, txid_from_uri(doc.data.decode("utf-8").strip()))
            except (DocumentError, UnicodeDecodeError):
                pass
    for tx_id in candidates:
        try:
            doc = read_document(tx_id, koios=koios)
        except DocumentError:
            continue
        if doc.type == "juror-record" and doc.hash == expected:
            return FoundRecord("overflow", doc.data, tx_id)
    return FoundRecord("missing")


def fetch(uri: Union[str, bytes], *, koios=None, timeout: int = 30, max_bytes: int = MAX_FETCH_BYTES) -> bytes:
    """The bytes of a document URI: ``vector-tx:`` (Koios ``tx_metadata``),
    ``https://`` or ``http://``. The caller compares the hash (``check``).
    Raises DocumentError."""
    text = _as_text(uri)
    if text.startswith(URI_PREFIX):
        return fetch_document(text, koios=koios, timeout=timeout).data
    if text.startswith(("https://", "http://")):
        import requests

        try:
            with requests.get(text, timeout=timeout, stream=True) as response:
                if response.status_code != 200:
                    raise DocumentError(f"{text}: HTTP {response.status_code}")
                data = b""
                for part in response.iter_content(65536):
                    data += part
                    if len(data) > max_bytes:
                        raise DocumentError(f"{text}: more than {max_bytes} bytes")
                return data
        except requests.RequestException as error:
            raise DocumentError(f"{text}: {error}") from None
    raise DocumentError(f"unsupported URI scheme: {text[:40]!r} (use vector-tx:, https:// or http://)")


@dataclasses.dataclass(frozen=True)
class Checked:
    """The result of ``check``: ``status`` is ``ok``, ``missing`` (the
    document cannot be read) or ``mismatch`` (another hash)."""

    status: str
    data: Optional[bytes] = None
    error: Optional[str] = None


def check(uri: Union[str, bytes], expected_hash: Union[bytes, str], **fetch_options) -> Checked:
    """Fetch a document and compare its blake2b-256 hash."""
    expected = bytes.fromhex(expected_hash) if isinstance(expected_hash, str) else bytes(expected_hash)
    try:
        data = fetch(uri, **fetch_options)
    except DocumentError as error:
        return Checked("missing", None, str(error))
    if doc_hash(data) != expected:
        return Checked("mismatch", data, f"hash {doc_hash(data).hex()}, expected {expected.hex()}")
    return Checked("ok", data)
