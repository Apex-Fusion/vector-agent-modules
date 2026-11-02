"""Tests for reputation_staking.vector_identity (the canonical copy).

`fixtures/aiken_serialise_vectors.json` holds Aiken v1.1.21 `cbor.serialise`
output (aiken-lang/stdlib v3.1.0, the vendored `vector_identity/types.ak`) for
the values that the builders below make. The identifier vectors are Part 1
§3.1 of docs/did-vector-method.md.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import cbor2
import pytest
from pycardano import (
    Address,
    MultiAsset,
    Network,
    ScriptHash,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
)
from pycardano.serialization import RawCBOR

from reputation_staking import vector_identity as vi

h = bytes.fromhex

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "aiken_serialise_vectors.json").read_text()
)

IDENTITY_HASH = h("1d" * 28)
OTHER_HASH = h("2e" * 28)
M1 = h("5c" * 28)
GUARDIAN = h("6a" * 28)
PARENT_ID = h("a2e5e227858e84f1a8f9b0c1246e6cbc9336d707d43ba43d0e1cb7c51c45f4c9")
CHILD_ID = h("3ddd07517205dd8a745887c33a09da0e4a5d63dca1f151ceeedaeeda0551c52b")
GRANDCHILD_ID = h("77" * 32)
CONTROLLER = h("c0" * 32)
ED_KEY = h("e1" * 32)
P256_KEY = h("02" + "a2" * 32)

LO = 1_762_000_000_000
HI = LO + vi.MAX_RANGE_MS


def big_record() -> vi.IdentityDatum:
    return vi.IdentityDatum(
        controller=CONTROLLER,
        keys=(
            vi.Key(vi.KeyType.ED25519, ED_KEY, 27, None),
            vi.Key(vi.KeyType.P256, P256_KEY, 3, h("99" * 64)),
        ),
        parties=(
            vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, 0, None, 3, None),
            vi.Party(vi.Role.RESPONSIBLE, vi.Signer.script(M1), b"example.org",
                     1_700_000_000_000, 253_402_300_799_999, 1, 1_800_000_000_000),
            vi.Party(vi.Role.GUARDIAN, vi.Signer.key_hash(GUARDIAN), None, 5, None, 4, None),
        ),
        guardian_threshold=1,
        pending_recovery=vi.PendingRecovery(h("d0" * 32), 1_750_000_000_000),
        lineage=vi.Lineage(PARENT_ID, 2, 1),
        subagent_epoch=3,
        name=b"Agent",
        description=(
            b"A description that is longer than sixty-four bytes so that the "
            b"encoder must split it into chunks of 64."
        ),
        framework=b"fw",
        endpoint=b"",
        capabilities=(b"code_review", b"testing"),
        created_at=1_762_000_000_000,
        status=vi.Status.suspended([
            vi.Hold(vi.Signer.script(M1), 64, 1_762_000_000_000, 1_762_604_800_000),
            vi.Hold(vi.Signer.controller_set(), 6, 1, 2),
        ]),
    )


def simple_record(**changes) -> vi.IdentityDatum:
    """An Active root record with a controller-set Accountable Party."""
    base = vi.IdentityDatum(
        controller=CONTROLLER,
        keys=(vi.Key(vi.KeyType.ED25519, ED_KEY, vi.PURPOSE_TRANSACTION, None),),
        parties=(
            vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, 0, None, 3, None),
        ),
        guardian_threshold=0,
        pending_recovery=None,
        lineage=vi.Lineage(),
        subagent_epoch=0,
        name=b"agent",
        description=b"",
        framework=b"",
        endpoint=b"https://agent.example",
        capabilities=(b"code_review",),
        created_at=LO - vi.DAY_MS,
        status=vi.Status.active(),
    )
    return dataclasses.replace(base, **changes)


# ── Identifier and serialisation ────────────────────────────────────────────


def test_identifier_vectors_part1():
    assert vi.serialise_data(vi.output_reference_data(bytes(32), 0)).hex() == (
        "d8799f5820" + "00" * 32 + "00ff"
    )
    assert vi.serialise_data(vi.output_reference_data(h("ab" * 32), 24)).hex() == (
        "d8799f5820" + "ab" * 32 + "1818ff"
    )
    assert vi.identifier_from_seed(bytes(32), 0) == PARENT_ID
    assert vi.identifier_from_seed(h("ab" * 32), 24) == CHILD_ID


def test_record_serialisation_matches_aiken():
    record = big_record()
    revoked = dataclasses.replace(record, status=vi.Status.revoked(
        vi.Signer.key_hash(GUARDIAN), 1, 99, 50, CHILD_ID))
    retired = dataclasses.replace(
        record, keys=(), parties=(), capabilities=(), status=vi.Status.retired(7))
    for name, datum in (("big", record), ("revoked", revoked), ("retired", retired)):
        expected = VECTORS[name].lower()
        assert vi.identity_datum_cbor(datum).hex() == expected, name
        assert vi.decode_identity_datum(expected) == datum, name


def test_generic_serialisation_matches_aiken():
    verdict = vi.constr(0, [PARENT_ID, vi.constr(1, []), 0,
                            vi.output_reference_data(h("ab" * 32), 300)])
    assert vi.serialise_data(verdict).hex() == VECTORS["verdict"].lower()
    ints = [0, 23, 24, 255, 256, 65536, 4294967296, 18446744073709551615,
            18446744073709551616, -1, -24, -25, -18446744073709551616,
            -18446744073709551617, 1180591620717411303424]
    assert vi.serialise_data(ints).hex() == VECTORS["ints"].lower()
    assert vi.hash_data(verdict) == vi.blake2b_256(h(VECTORS["verdict"]))


def test_constructor_tags_beyond_six():
    assert vi.constr(7, []).tag == 1280
    assert vi.constr(127, []).tag == 1400
    tag = vi.constr(128, [1])
    assert tag.tag == 102
    assert vi.unconstr(tag) == (128, [1])
    assert vi.serialise_data(tag).hex() == "d866821880" + "9f01ff"
    assert vi.unconstr(vi.constr(11, [5])) == (11, [5])


def test_decode_reads_every_input_form():
    record = simple_record()
    raw = vi.identity_datum_cbor(record)
    tree = vi.identity_datum_data(record)
    assert vi.decode_identity_datum(raw) == record
    assert vi.decode_identity_datum(raw.hex()) == record
    assert vi.decode_identity_datum(tree) == record
    assert vi.decode_identity_datum(RawCBOR(raw)) == record
    # cbor2 writes definite-length arrays; the shape is the same Plutus Data
    assert vi.decode_identity_datum(cbor2.dumps(tree)) == record


@pytest.mark.parametrize("raw", [
    b"\x00",                                     # an integer
    cbor2.dumps(vi.constr(0, [b"x"] * 13)),     # 13 fields
    cbor2.dumps(vi.constr(1, [b"x"] * 14)),     # constructor 1
    b"\xff\xff",                                 # not CBOR
])
def test_decode_rejects_other_shapes(raw):
    with pytest.raises(vi.IdentityDecodeError):
        vi.decode_identity_datum(raw)


def test_decode_rejects_bad_status_and_option():
    tree = vi.identity_datum_data(simple_record())
    fields = list(tree.value)
    fields[13] = vi.constr(4, [])
    with pytest.raises(vi.IdentityDecodeError):
        vi.decode_identity_datum(vi.constr(0, fields))
    fields = list(tree.value)
    fields[4] = vi.constr(2, [])
    with pytest.raises(vi.IdentityDecodeError):
        vi.decode_identity_datum(vi.constr(0, fields))


# ── DIDs ────────────────────────────────────────────────────────────────────


def test_did_forms():
    did = "did:vector:" + PARENT_ID.hex()
    assert vi.did_from_id(PARENT_ID) == did
    assert vi.id_from_did(did) == PARENT_ID
    legacy = "did:vector:agent:" + vi.LEGACY_REGISTRY_HASH.hex() + ":" + PARENT_ID.hex()
    assert vi.id_from_did(legacy) == PARENT_ID
    assert vi.normalize_agent_id(PARENT_ID.hex().upper()) == PARENT_ID
    assert vi.normalize_agent_id(did) == PARENT_ID
    assert vi.normalize_agent_id(PARENT_ID) == PARENT_ID


@pytest.mark.parametrize("bad", [
    "did:vector:" + "A2" * 32,                                   # uppercase
    "did:vector:testnet:" + "a2" * 32,                           # reserved form
    "did:vector:agent:" + "5d" * 28 + ":" + "a2" * 32,           # other policy
    "did:vector:" + "a2" * 31,                                   # short
    "did:web:example.org",
])
def test_did_rejects(bad):
    with pytest.raises(ValueError):
        vi.id_from_did(bad)


def test_normalize_rejects_wrong_length():
    with pytest.raises(ValueError):
        vi.normalize_agent_id(b"\x00" * 31)
    with pytest.raises(ValueError):
        vi.normalize_agent_id("ab" * 28)


# ── Keys, credentials, capabilities ─────────────────────────────────────────


def test_active_key_and_declared_credential():
    record = big_record()
    ed_hash = vi.blake2b_224(ED_KEY)
    p256_hash = vi.blake2b_224(P256_KEY)
    assert vi.active_key(record, ed_hash, vi.PURPOSE_TRANSACTION)       # 27 = 1+2+8+16
    assert vi.active_key(record, ed_hash, vi.PURPOSE_DELEGATION)
    assert vi.active_key(record, p256_hash, vi.PURPOSE_ASSERTION)
    assert not vi.active_key(record, p256_hash, vi.PURPOSE_TRANSACTION)  # P-256: never
    assert not vi.active_key(record, ed_hash, 4)                         # unused value
    assert vi.controller_key_hash(record) == vi.blake2b_224(CONTROLLER)
    assert vi.declared_credential(record, vi.blake2b_224(CONTROLLER))
    assert vi.declared_credential(record, ed_hash)
    assert not vi.declared_credential(record, p256_hash)
    only_assert = dataclasses.replace(
        record, keys=(vi.Key(vi.KeyType.ED25519, ED_KEY, vi.PURPOSE_ASSERTION, None),))
    assert not vi.declared_credential(only_assert, ed_hash)
    # The controller key is not a listed key: it is not an active key
    assert not vi.active_key(record, vi.blake2b_224(CONTROLLER), vi.PURPOSE_TRANSACTION)


def test_capabilities():
    record = simple_record(capabilities=(b"code_review", b"testing"))
    assert vi.has_capability(record, "testing")
    assert vi.has_capability(record, b"code_review")
    assert not vi.has_capability(record, "Testing")
    assert vi.missing_capabilities(record, ["testing", "audit", b"x"]) == [b"audit", b"x"]


# ── Effective status ────────────────────────────────────────────────────────


def no_lookup(_agent_id):
    raise AssertionError("no ancestor lookup expected")


def test_effective_active_and_holds():
    record = simple_record()
    assert vi.effective(record, no_lookup, LO, HI) == vi.Effective.ACTIVE
    hold = vi.Hold(vi.Signer.controller_set(), 6, LO - 10, LO + 1)
    assert vi.effective(simple_record(status=vi.Status.suspended([hold])),
                        no_lookup, LO, HI) == vi.Effective.SUSPENDED
    ended = vi.Hold(vi.Signer.controller_set(), 6, LO - 10, LO)          # until == lo
    assert vi.effective(simple_record(status=vi.Status.suspended([ended])),
                        no_lookup, LO, HI) == vi.Effective.ACTIVE


def test_effective_needs_an_accountable_party_over_the_window():
    late = vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, LO + 1, None, 3, None)
    short = vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, 0, HI - 1, 3, None)
    exact = vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, LO, HI, 3, None)
    other = vi.Party(vi.Role.RESPONSIBLE, vi.Signer.controller_set(), None, 0, None, 1, None)
    for parties, expected in (
        ((late,), vi.Effective.SUSPENDED),
        ((short,), vi.Effective.SUSPENDED),
        ((exact,), vi.Effective.ACTIVE),
        ((other,), vi.Effective.SUSPENDED),
        ((), vi.Effective.SUSPENDED),
    ):
        assert vi.effective(simple_record(parties=parties), no_lookup, LO, HI) == expected


def test_effective_tombstones_need_no_parent():
    child = simple_record(lineage=vi.Lineage(PARENT_ID, 0, 1))
    revoked = dataclasses.replace(child, status=vi.Status.revoked(vi.Signer.controller_set(), 0, LO))
    retired = dataclasses.replace(child, status=vi.Status.retired(LO))
    assert vi.effective(revoked, no_lookup, LO, HI) == vi.Effective.REVOKED
    assert vi.effective(retired, no_lookup, LO, HI) == vi.Effective.RETIRED


def test_effective_subagents():
    parent = simple_record(subagent_epoch=2)
    child = simple_record(lineage=vi.Lineage(PARENT_ID, 2, 1))
    lookup = {PARENT_ID: parent}.get
    assert vi.effective(child, lookup, LO, HI) == vi.Effective.ACTIVE
    # The parent ended its subagents
    bumped = {PARENT_ID: dataclasses.replace(parent, subagent_epoch=3)}.get
    assert vi.effective(child, bumped, LO, HI) == vi.Effective.REVOKED
    # The parent is suspended or retired: the child inherits it
    hold = vi.Hold(vi.Signer.controller_set(), 6, LO - 10, LO + 10)
    held = {PARENT_ID: dataclasses.replace(parent, status=vi.Status.suspended([hold]))}.get
    assert vi.effective(child, held, LO, HI) == vi.Effective.SUSPENDED
    gone = {PARENT_ID: dataclasses.replace(parent, status=vi.Status.retired(LO))}.get
    assert vi.effective(child, gone, LO, HI) == vi.Effective.RETIRED
    # A child is never better than its parent: an unaccountable parent suspends
    lonely = {PARENT_ID: dataclasses.replace(parent, parties=())}.get
    assert vi.effective(child, lonely, LO, HI) == vi.Effective.SUSPENDED
    with pytest.raises(vi.MissingAncestorError):
        vi.effective(child, {}.get, LO, HI)


def test_effective_reads_at_most_max_depth_ancestors():
    ids = [bytes([i]) * 32 for i in range(1, 7)]
    records = {}
    for depth, agent_id in enumerate(ids):
        lineage = vi.Lineage() if depth == 0 else vi.Lineage(ids[depth - 1], 0, depth)
        records[agent_id] = simple_record(lineage=lineage)
    assert vi.effective(records[ids[4]], records.get, LO, HI) == vi.Effective.ACTIVE
    with pytest.raises(vi.IdentityError):
        vi.effective(records[ids[5]], records.get, LO, HI)


# ── Record chains ───────────────────────────────────────────────────────────


def chain_fetch(records):
    def fetch(agent_id):
        datum = records.get(agent_id)
        return None if datum is None else vi.IdentityRecord(agent_id, datum, utxo=agent_id.hex())
    return fetch


def test_record_chain_walks_ancestors():
    records = {
        PARENT_ID: simple_record(),
        CHILD_ID: simple_record(lineage=vi.Lineage(PARENT_ID, 0, 1)),
        GRANDCHILD_ID: simple_record(lineage=vi.Lineage(CHILD_ID, 0, 2)),
    }
    chain = vi.record_chain(GRANDCHILD_ID, chain_fetch(records))
    assert [r.agent_id for r in chain] == [GRANDCHILD_ID, CHILD_ID, PARENT_ID]
    assert vi.effective_of_chain(chain, LO, HI) == vi.Effective.ACTIVE
    assert vi.record_chain(PARENT_ID.hex(), chain_fetch(records))[0].utxo == PARENT_ID.hex()


def test_record_chain_stops_like_effective():
    tomb = simple_record(lineage=vi.Lineage(PARENT_ID, 0, 1),
                         status=vi.Status.retired(LO))
    assert len(vi.record_chain(CHILD_ID, chain_fetch({CHILD_ID: tomb}))) == 1
    records = {
        PARENT_ID: simple_record(subagent_epoch=1, lineage=vi.Lineage(GRANDCHILD_ID, 0, 1)),
        CHILD_ID: simple_record(lineage=vi.Lineage(PARENT_ID, 0, 2)),
    }
    # The parent's epoch revokes the child: the grandparent is not read
    chain = vi.record_chain(CHILD_ID, chain_fetch(records))
    assert [r.agent_id for r in chain] == [CHILD_ID, PARENT_ID]
    assert vi.effective_of_chain(chain, LO, HI) == vi.Effective.REVOKED


def test_record_chain_missing_records():
    with pytest.raises(vi.RecordNotFoundError):
        vi.record_chain(CHILD_ID, chain_fetch({}))
    child = simple_record(lineage=vi.Lineage(PARENT_ID, 0, 1))
    with pytest.raises(vi.MissingAncestorError) as info:
        vi.record_chain(CHILD_ID, chain_fetch({CHILD_ID: child}))
    assert info.value.agent_id == PARENT_ID


def test_window_and_require_active():
    vi.check_window(LO, LO + vi.MAX_RANGE_MS)
    for lo, hi in ((LO, LO + vi.MAX_RANGE_MS + 1), (LO + 1, LO)):
        with pytest.raises(vi.IdentityError):
            vi.check_window(lo, hi)
    chain = [vi.IdentityRecord(PARENT_ID, simple_record())]
    assert vi.require_active(chain, LO, HI, ["code_review"]) == chain[0].datum
    with pytest.raises(vi.IdentityError, match="audit"):
        vi.require_active(chain, LO, HI, ["audit"])
    held = [vi.IdentityRecord(PARENT_ID, simple_record(status=vi.Status.suspended(
        [vi.Hold(vi.Signer.controller_set(), 6, LO, LO + 5)])))]
    with pytest.raises(vi.IdentityStatusError) as info:
        vi.require_active(held, LO, HI)
    assert info.value.effective == vi.Effective.SUSPENDED
    with pytest.raises(vi.IdentityError):
        vi.require_declared_credential(chain[0], h("00" * 28))
    vi.require_declared_credential(chain[0], vi.blake2b_224(ED_KEY))


# ── Authentic record outputs (pycardano objects) ────────────────────────────


def record_utxo(datum=None, *, script=IDENTITY_HASH, staking=None, tokens=None,
                coin=10_000_000, index=0):
    tokens = {IDENTITY_HASH: {PARENT_ID: 1}} if tokens is None else tokens
    address = Address(payment_part=ScriptHash(script), staking_part=staking,
                      network=Network.MAINNET)
    value = Value(coin, MultiAsset.from_primitive(tokens)) if tokens else Value(coin)
    if datum is False:
        output = TransactionOutput(address, value)
    else:
        raw = vi.identity_datum_cbor(simple_record() if datum is None else datum)
        output = TransactionOutput(address, value, datum=RawCBOR(raw))
    return UTxO(TransactionInput.from_primitive([h("ab" * 32), index]), output)


def test_record_from_utxo_accepts_authentic_outputs():
    record = vi.record_from_utxo(record_utxo(), IDENTITY_HASH)
    assert record is not None
    assert record.agent_id == PARENT_ID
    assert record.datum == simple_record()
    assert record.did == "did:vector:" + PARENT_ID.hex()
    # Tokens of other policies do not matter for the reader (find_record)
    mixed = record_utxo(tokens={IDENTITY_HASH: {PARENT_ID: 1}, OTHER_HASH: {b"x": 5}})
    assert vi.record_from_utxo(mixed, IDENTITY_HASH) is not None
    assert vi.find_record([mixed], IDENTITY_HASH, PARENT_ID).agent_id == PARENT_ID
    assert vi.find_record([mixed], IDENTITY_HASH, CHILD_ID) is None


@pytest.mark.parametrize("utxo", [
    record_utxo(script=OTHER_HASH),
    record_utxo(staking=VerificationKeyHash(h("33" * 28))),
    record_utxo(tokens={IDENTITY_HASH: {PARENT_ID: 2}}),
    record_utxo(tokens={IDENTITY_HASH: {PARENT_ID: 1, CHILD_ID: 1}}),
    record_utxo(tokens={OTHER_HASH: {PARENT_ID: 1}}),
    record_utxo(tokens={}),
    record_utxo(datum=False),
])
def test_record_from_utxo_rejects_other_outputs(utxo):
    assert vi.record_from_utxo(utxo, IDENTITY_HASH) is None


def test_record_from_utxo_with_a_bad_datum_fails():
    utxo = record_utxo()
    utxo.output.datum = RawCBOR(cbor2.dumps(vi.constr(0, [1])))
    with pytest.raises(vi.IdentityDecodeError):
        vi.record_from_utxo(utxo, IDENTITY_HASH)


# ── Redeemers and record changes ────────────────────────────────────────────


def test_redeemer_encodings():
    seed = h("ab" * 32)
    assert vi.serialise_data(vi.register_redeemer(seed, 24)).hex() == (
        "d8799fd8799f5820" + "ab" * 32 + "1818ffff")
    assert vi.serialise_data(vi.adopt_redeemer([b"\x01" * 32])).hex() == (
        "d87a9f9f5820" + "01" * 32 + "ffff")
    assert vi.serialise_data(vi.update_redeemer()).hex() == "d87980"
    assert vi.serialise_data(vi.suspend_redeemer(vi.by_party(2))).hex() == "d87b9fd87a9f02ffff"
    assert vi.serialise_data(vi.revoke_redeemer(vi.by_controller())).hex() == "d87d9fd87980ff"
    assert vi.serialise_data(vi.unsuspend_redeemer(vi.Signer.script(M1))).hex() == (
        "d87c9fd87a9f581c" + "5c" * 28 + "ffff")


def test_suspend_keeps_holds_of_current_other_authors():
    removed_key = vi.Signer.key_hash(h("44" * 28))
    holds = [
        vi.Hold(vi.Signer.script(M1), 64, LO - 100, LO + 100),        # kept: Script author
        vi.Hold(vi.Signer.controller_set(), 6, LO - 100, LO),         # dropped: not in force
        vi.Hold(removed_key, 6, LO - 100, LO + 100),                  # dropped: not a party
        vi.Hold(vi.Signer.key_hash(GUARDIAN), 6, LO - 100, LO + 100),  # replaced: same author
    ]
    guardian = vi.Party(vi.Role.PROVIDER, vi.Signer.key_hash(GUARDIAN), None, 0, None, 1, None)
    record = simple_record(parties=simple_record().parties + (guardian,),
                           status=vi.Status.suspended(holds))
    after = vi.suspended(record, vi.Signer.key_hash(GUARDIAN), 6, LO, LO + 500)
    assert after.status.holds == (
        holds[0],
        vi.Hold(vi.Signer.key_hash(GUARDIAN), 6, LO, LO + 500),
    )
    assert after.parties == record.parties
    assert vi.hold_of(record, vi.Signer.script(M1), LO) == holds[0]
    assert vi.hold_of(record, vi.Signer.controller_set(), LO) is None
    assert vi.hold_of(simple_record(), vi.Signer.script(M1), LO) is None


def test_tombstone_clears_the_listed_fields():
    record = big_record()
    status = vi.Status.revoked(vi.Signer.script(M1), vi.REASON_DISPUTE_VERDICT, LO)
    tomb = vi.tombstone(record, status)
    assert tomb.keys == () and tomb.parties == () and tomb.capabilities == ()
    assert tomb.guardian_threshold == 0 and tomb.pending_recovery is None
    assert (tomb.description, tomb.framework, tomb.endpoint) == (b"", b"", b"")
    assert (tomb.controller, tomb.name, tomb.lineage, tomb.subagent_epoch, tomb.created_at) == (
        record.controller, record.name, record.lineage, record.subagent_epoch, record.created_at)
    assert vi.is_tombstone(tomb)
    with pytest.raises(ValueError):
        vi.tombstone(record, vi.Status.active())
