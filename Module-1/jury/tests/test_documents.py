"""Documents on chain (§10): chunking, metadata in every reader format, the
vector-tx: URI, fetching and the hash check."""

import base64
import json

import cbor2
import pytest

from conftest import FakeKoios, h32
from vector_jury import documents as D


def check_chunks(doc: bytes):
    chunks = D.chunk(doc)
    assert b"".join(c.encode("utf-8") for c in chunks) == doc
    for c in chunks:
        assert 0 < len(c.encode("utf-8")) <= 64
        assert not c.startswith("0x")
    return chunks


def test_ascii_and_exact_boundaries():
    assert D.chunk(b"") == []
    assert check_chunks(b"a" * 64) == ["a" * 64]
    assert check_chunks(b"a" * 65) == ["a" * 64, "a"]
    assert check_chunks(b"a" * 128) == ["a" * 64, "a" * 64]


@pytest.mark.parametrize("char", ["é", "✓", "😀"])
def test_multibyte_characters_at_the_boundary(char):
    width = len(char.encode("utf-8"))
    for pad in range(64 - 2 * width, 65):
        doc = ("a" * pad + char * 3).encode("utf-8")
        chunks = check_chunks(doc)
        # No character is split: each chunk decodes on its own.
        assert all(isinstance(c, str) for c in chunks)
    chunks = check_chunks(("a" * 63 + char).encode("utf-8"))
    assert chunks == ["a" * 63, char]


def test_no_chunk_starts_with_0x():
    # A "0x" right at a boundary moves the boundary back one character.
    doc = ("a" * 64 + "0x1234").encode()
    chunks = check_chunks(doc)
    assert chunks == ["a" * 63, "a0x1234"]
    # A document that starts with "0x".
    assert check_chunks(b"0x00ff") == ["0", "x00ff"]
    # Many "0x" in a row and at every offset.
    for pad in range(60, 70):
        check_chunks(("b" * pad + "0x" * 80).encode())
    check_chunks(("0x" * 200).encode())
    # A multi-byte character before the boundary "0x".
    check_chunks(("a" * 62 + "é" + "0xabc").encode())


def test_invalid_utf8_is_refused():
    with pytest.raises(D.DocumentError, match="UTF-8"):
        D.chunk(b"abc\xff")
    with pytest.raises(D.DocumentError):
        D.chunk("é".encode("utf-8")[:1])


def doc_bytes():
    return json.dumps({"type": "vector-jury-record/1", "reason": "é ✓ 😀 " * 40, "x": "0x" * 50}).encode()


def test_metadata_roundtrip_in_every_reader_format():
    doc = doc_bytes()
    meta = D.metadata(doc, "juror-record")
    value = meta[D.LABEL]
    assert value["v"] == 1 and value["t"] == "juror-record" and value["m"] == "application/json"
    aux = D.auxiliary_data(doc, "juror-record")
    expected = D.Document("juror-record", "application/json", doc)
    # pycardano objects and CBOR (Shelley map, Alonzo tag 259, a transaction).
    assert D.parse_metadata(meta) == expected
    assert D.parse_metadata(aux) == expected
    raw = aux.to_cbor()
    assert D.parse_metadata(raw) == expected
    alonzo = cbor2.dumps(cbor2.CBORTag(259, {0: cbor2.loads(raw)}))
    assert D.parse_metadata(alonzo) == expected
    tx = cbor2.dumps([{0: [], 1: [], 2: 0}, {}, True, cbor2.loads(raw)])
    assert D.parse_metadata(tx) == expected
    # Koios tx_metadata: the answer, one entry, the metadata object.
    koios = [{"tx_hash": "ab" * 32, "metadata": {"22084": value}}]
    assert D.parse_metadata(koios) == expected
    assert D.parse_metadata(koios[0]) == expected
    assert D.parse_metadata(json.dumps(koios)) == expected
    assert D.parse_metadata({"22084": value}) == expected
    # Ogmios: plain JSON, the detailed schema, CBOR.
    assert D.parse_metadata({"hash": "00", "labels": {"22084": {"json": value}}}) == expected
    detailed = {"map": [
        {"k": {"string": "v"}, "v": {"int": 1}},
        {"k": {"string": "t"}, "v": {"string": "juror-record"}},
        {"k": {"string": "m"}, "v": {"string": "application/json"}},
        {"k": {"string": "d"}, "v": {"list": [{"string": c} for c in value["d"]]}},
    ]}
    assert D.parse_metadata({"labels": {"22084": {"json": detailed}}}) == expected
    ogmios_cbor = cbor2.dumps(value).hex()
    assert D.parse_metadata({"labels": {"22084": {"cbor": ogmios_cbor}}}) == expected
    assert expected.hash == D.doc_hash(doc) and len(expected.hash) == 32


@pytest.mark.parametrize("change, match", [
    ({"v": 2}, "version"),
    ({"d": ["ok", "0xabcd"]}, "0x"),
    ({"d": ["a" * 65]}, "65 bytes"),
    ({"d": ["ok", b"\x00"]}, "not text"),
    ({"t": 5}, "text fields"),
])
def test_readers_refuse_bad_documents(change, match):
    value = {"v": 1, "t": "claim", "m": "application/json", "d": ["ok"], **change}
    with pytest.raises(D.DocumentError, match=match):
        D.parse_metadata(cbor2.dumps({D.LABEL: value}))


def test_missing_label_and_metadata():
    with pytest.raises(D.DocumentError, match="label 22084"):
        D.parse_metadata({"674": {"msg": ["x"]}})
    with pytest.raises(D.DocumentError, match="no metadata"):
        D.parse_metadata([{"tx_hash": "ab" * 32, "metadata": None}])


def test_writers_refuse_long_or_ambiguous_type_fields():
    with pytest.raises(D.DocumentError):
        D.metadata(b"x", "t" * 65)
    with pytest.raises(D.DocumentError):
        D.metadata(b"x", "0xclaim")
    with pytest.raises(D.DocumentError):
        D.metadata(b"\xff", "claim")


def test_uri_roundtrip():
    tx_id = bytes.fromhex("72ef1290" + "00" * 28)
    uri = D.uri_for(tx_id)
    assert uri.startswith("vector-tx:cu8SkA") and len(uri) == 53 and len(uri) - len("vector-tx:") == 43
    assert D.txid_from_uri(uri) == tx_id.hex() and D.txid_from_uri(uri.encode()) == tx_id.hex()
    assert D.uri_for(tx_id.hex()) == uri
    for _ in range(20):
        tx = h32(bytes([_]))
        assert D.txid_from_uri(D.uri_for(tx)) == tx.hex()
        assert base64.urlsafe_b64decode(D.uri_for(tx)[10:] + "=") == tx
    for bad in ("vector-tx:" + "A" * 42, "vector-tx:" + "A" * 43 + "=", "ipfs://x", "vector-tx:" + "+" * 43,
                "vector-tx:" + uri[10:-1] + "B"):
        with pytest.raises(D.DocumentError):
            D.txid_from_uri(bad)


def test_fetch_and_check_with_koios():
    doc = doc_bytes()
    tx_id = h32(b"publish tx").hex()
    koios = FakeKoios()
    koios.metadata[tx_id] = {"22084": D.metadata(doc, "claim")[D.LABEL]}
    uri = D.uri_for(tx_id)
    assert D.fetch(uri, koios=koios) == doc
    assert D.check(uri, D.doc_hash(doc), koios=koios).status == "ok"
    assert D.check(uri, h32(b"other"), koios=koios).status == "mismatch"
    missing = D.check(D.uri_for(h32(b"unknown")), D.doc_hash(doc), koios=koios)
    assert missing.status == "missing" and missing.data is None
    with pytest.raises(D.DocumentError, match="scheme"):
        D.fetch("ipfs://abc", koios=koios)
