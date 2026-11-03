"""Client-side model of did:vector identity records.

Canonical copy: ``Module-3/python/reputation_staking/vector_identity.py``.
Identical copies: ``Module-1/simulation/vector_identity.py`` and
``Module-6/scripts/vector_identity.py``. The Module-3 test
``tests/test_vector_identity_copies.py`` checks that the three files are
equal. Change the canonical copy, then copy it to the other two paths.

The on-chain source of truth is the vendored Aiken library
``shared/lib/vector_identity.ak`` and ``shared/lib/vector_identity/`` (from
agents-marketplace ``contracts/identity-marketplace``). The specifications are
in agents-marketplace:

- Part 1, ``docs/did-vector-method.md``: §3 identifier and DID syntax, §5
  record format, §7.3 effective status.
- Part 2, ``docs/IDENTITY_REGISTRY.md``: §3.1 redeemers, §3.2 definitions,
  §3.5 spend rules, §3.7 tombstones, §3.10 library functions, §7 modules.

The module has no network access and needs only the Python standard library
and ``cbor2`` (it reads CBOR; ``cbor2<6`` with pycardano 0.19.2). A Plutus
Data value is a tree as ``cbor2`` decodes it: ``cbor2.CBORTag`` for a
constructor, ``list`` for a list, ``dict`` for a map, ``bytes`` and ``int``.
``serialise_data`` writes a tree with the Plutus encoding, so that hashes of
data match the on-chain ``cbor.serialise``.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Callable, Iterable, List, Optional, Tuple, Union

import cbor2

# ── Constants (Part 2 §3.10, shared/lib/vector_identity.ak) ─────────────────

PURPOSE_AUTHENTICATION = 1
PURPOSE_ASSERTION = 2
PURPOSE_TRANSACTION = 8
PURPOSE_DELEGATION = 16

RIGHT_SUSPEND = 1
RIGHT_REVOKE = 2
RIGHT_RECOVER = 4

#: The longest validity range (hi - lo) of a transaction that reads or changes
#: identity data: one hour.
MAX_RANGE_MS = 3_600_000
#: 9999-12-31T23:59:59.999Z in POSIX milliseconds.
MAX_TIME_MS = 253_402_300_799_999
DAY_MS = 86_400_000
DAYS_7_MS = 604_800_000
DAYS_30_MS = 2_592_000_000
#: The greatest subagent depth, and the most ancestors that `effective` reads.
MAX_DEPTH = 4

#: Reason code 64, disputeVerdict (Part 2 Appendix A). Only Module-1 uses it.
REASON_DISPUTE_VERDICT = 64

#: Script hash and policy ID of the legacy agent registry (Part 1 §4).
LEGACY_REGISTRY_HASH = bytes.fromhex(
    "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
)

DID_PREFIX = "did:vector:"
LEGACY_DID_PREFIX = "did:vector:agent:"

_HEX = frozenset("0123456789abcdef")


# ── Errors ──────────────────────────────────────────────────────────────────


class IdentityError(Exception):
    """An identity record cannot be used for the action."""


class IdentityDecodeError(IdentityError, ValueError):
    """A value does not decode as the expected Plutus Data shape."""


class RecordNotFoundError(IdentityError, LookupError):
    """No authentic record output exists for an identifier."""

    def __init__(self, agent_id: bytes, message: Optional[str] = None):
        self.agent_id = agent_id
        super().__init__(message or f"no identity record for {did_from_id(agent_id)}")


class MissingAncestorError(RecordNotFoundError):
    """A subagent's parent record is not available. On-chain, `effective`
    fails when an ancestor is not a reference input."""

    def __init__(self, agent_id: bytes):
        super().__init__(
            agent_id, f"ancestor record {did_from_id(agent_id)} is not available"
        )


class IdentityStatusError(IdentityError):
    """The effective status of a record is not the one the action needs."""

    def __init__(self, agent_id: bytes, effective_status: "Effective", needed: "Effective"):
        self.agent_id = agent_id
        self.effective = effective_status
        self.needed = needed
        super().__init__(
            f"{did_from_id(agent_id)} has effective status "
            f"{effective_status.value}, the action needs {needed.value}"
        )


# ── Record types (Part 1 §5; constructor order is the on-chain format) ─────


class KeyType(IntEnum):
    ED25519 = 0
    P256 = 1


class SignerKind(IntEnum):
    KEY_HASH = 0
    SCRIPT = 1
    CONTROLLER_SET = 2


class Role(IntEnum):
    ACCOUNTABLE = 0
    RESPONSIBLE = 1
    PROVIDER = 2
    DISTRIBUTOR = 3
    GUARDIAN = 4


class StatusKind(IntEnum):
    ACTIVE = 0
    SUSPENDED = 1
    REVOKED = 2
    RETIRED = 3


class Effective(Enum):
    """The effective status that `effective` computes (never stored)."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    RETIRED = "retired"


@dataclass(frozen=True)
class Signer:
    """`KeyHash { hash }`, `Script { hash }` or `ControllerSet`."""

    kind: SignerKind
    hash: Optional[bytes] = None

    @classmethod
    def key_hash(cls, key_hash: bytes) -> "Signer":
        return cls(SignerKind.KEY_HASH, bytes(key_hash))

    @classmethod
    def script(cls, script_hash: bytes) -> "Signer":
        return cls(SignerKind.SCRIPT, bytes(script_hash))

    @classmethod
    def controller_set(cls) -> "Signer":
        return cls(SignerKind.CONTROLLER_SET, None)


@dataclass(frozen=True)
class Key:
    key_type: KeyType
    key: bytes
    purposes: int
    pop: Optional[bytes] = None


@dataclass(frozen=True)
class Party:
    role: Role
    signer: Signer
    organisation: Optional[bytes]
    not_before: int
    not_after: Optional[int]
    rights: int
    removal_after: Optional[int]


@dataclass(frozen=True)
class Lineage:
    """`Root` when `parent` is None, else `Subagent { parent, parent_epoch,
    depth }`."""

    parent: Optional[bytes] = None
    parent_epoch: int = 0
    depth: int = 0

    @property
    def is_root(self) -> bool:
        return self.parent is None


@dataclass(frozen=True)
class Hold:
    by: Signer
    reason: int
    since: int
    until: int


@dataclass(frozen=True)
class PendingRecovery:
    new_controller: bytes
    effective_after: int


@dataclass(frozen=True)
class Status:
    """`Active`, `Suspended { holds }`, `Revoked { by, reason, at,
    invalid_from, successor }` or `Retired { at, successor }`."""

    kind: StatusKind
    holds: Tuple[Hold, ...] = ()
    by: Optional[Signer] = None
    reason: int = 0
    at: int = 0
    invalid_from: Optional[int] = None
    successor: Optional[bytes] = None

    @classmethod
    def active(cls) -> "Status":
        return cls(StatusKind.ACTIVE)

    @classmethod
    def suspended(cls, holds: Iterable[Hold]) -> "Status":
        return cls(StatusKind.SUSPENDED, holds=tuple(holds))

    @classmethod
    def revoked(
        cls,
        by: Signer,
        reason: int,
        at: int,
        invalid_from: Optional[int] = None,
        successor: Optional[bytes] = None,
    ) -> "Status":
        return cls(
            StatusKind.REVOKED, by=by, reason=reason, at=at,
            invalid_from=invalid_from, successor=successor,
        )

    @classmethod
    def retired(cls, at: int, successor: Optional[bytes] = None) -> "Status":
        return cls(StatusKind.RETIRED, at=at, successor=successor)

    @property
    def is_terminal(self) -> bool:
        return self.kind in (StatusKind.REVOKED, StatusKind.RETIRED)


@dataclass(frozen=True)
class IdentityDatum:
    """The inline datum of an identity record: `Constr 0` with 14 fields."""

    controller: bytes
    keys: Tuple[Key, ...]
    parties: Tuple[Party, ...]
    guardian_threshold: int
    pending_recovery: Optional[PendingRecovery]
    lineage: Lineage
    subagent_epoch: int
    name: bytes
    description: bytes
    framework: bytes
    endpoint: bytes
    capabilities: Tuple[bytes, ...]
    created_at: int
    status: Status


@dataclass(frozen=True)
class IdentityRecord:
    """An authentic record output: the identifier, the decoded datum and the
    caller's UTxO object (for example a pycardano ``UTxO``)."""

    agent_id: bytes
    datum: IdentityDatum
    utxo: Any = None

    @property
    def did(self) -> str:
        return did_from_id(self.agent_id)


# ── Hashes and identifiers (Part 1 §3) ─────────────────────────────────────


def blake2b_224(data: bytes) -> bytes:
    """BLAKE2b with a 28-byte digest (RFC 7693)."""
    return hashlib.blake2b(bytes(data), digest_size=28).digest()


def blake2b_256(data: bytes) -> bytes:
    """BLAKE2b with a 32-byte digest (RFC 7693)."""
    return hashlib.blake2b(bytes(data), digest_size=32).digest()


def output_reference_data(tx_id: bytes, index: int) -> cbor2.CBORTag:
    """Plutus V3 `OutputReference`: `Constr 0 [tx_id, output_index]`."""
    tx_id = bytes(tx_id)
    if len(tx_id) != 32:
        raise ValueError(f"tx_id must be 32 bytes, got {len(tx_id)}")
    return constr(0, [tx_id, int(index)])


def identifier_from_seed(tx_id: bytes, index: int) -> bytes:
    """`vector-id = blake2b_256(serialise(seed))` (Part 1 §3.1)."""
    return blake2b_256(serialise_data(output_reference_data(tx_id, index)))


def did_from_id(agent_id: Union[bytes, str]) -> str:
    """The canonical DID `did:vector:<64 lowercase hex>` (Part 1 §3)."""
    return DID_PREFIX + normalize_agent_id(agent_id).hex()


def _is_lowhex(text: str, length: int) -> bool:
    return len(text) == length and all(c in _HEX for c in text)


def id_from_did(did: str) -> bytes:
    """The identifier of a DID in the canonical form, or in the legacy form
    `did:vector:agent:<legacy policy>:<id>` with the legacy registry policy.

    Raises ValueError for any other text, also for uppercase characters and
    the reserved testnet form (Part 1 §3).
    """
    if not isinstance(did, str):
        raise ValueError("a DID is a string")
    if did.startswith(LEGACY_DID_PREFIX):
        rest = did[len(LEGACY_DID_PREFIX):]
        policy, _, ident = rest.partition(":")
        if not (_is_lowhex(policy, 56) and _is_lowhex(ident, 64)):
            raise ValueError(f"not a did:vector legacy DID: {did!r}")
        if bytes.fromhex(policy) != LEGACY_REGISTRY_HASH:
            raise ValueError(f"legacy DID with another policy: {did!r}")
        return bytes.fromhex(ident)
    if did.startswith(DID_PREFIX):
        ident = did[len(DID_PREFIX):]
        if _is_lowhex(ident, 64):
            return bytes.fromhex(ident)
    raise ValueError(f"not a canonical did:vector DID: {did!r}")


def normalize_agent_id(value: Union[bytes, str]) -> bytes:
    """A 32-byte identifier from 32 bytes, 64 hex characters (any case) or a
    DID (`id_from_did`)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != 32:
            raise ValueError(f"an identifier is 32 bytes, got {len(raw)}")
        return raw
    if isinstance(value, str):
        if value.startswith("did:"):
            return id_from_did(value)
        text = value.strip().lower()
        if _is_lowhex(text, 64):
            return bytes.fromhex(text)
        raise ValueError(f"not an identifier: {value!r}")
    raise TypeError(f"not an identifier: {type(value).__name__}")


# ── Plutus Data trees ───────────────────────────────────────────────────────
#
# Constructor tags: the Conway CDDL `constr<a>` rule
# (https://github.com/IntersectMBO/cardano-ledger/blob/master/eras/conway/impl/cddl-files/conway.cddl):
# 121..127 for alternatives 0..6, 1280..1400 for 7..127, 102 [index, fields]
# otherwise.


def constr(index: int, fields: Sequence[Any]) -> cbor2.CBORTag:
    """A Plutus Data constructor as a cbor2 tree."""
    if 0 <= index <= 6:
        return cbor2.CBORTag(121 + index, list(fields))
    if 7 <= index <= 127:
        return cbor2.CBORTag(1280 + index - 7, list(fields))
    if index < 0:
        raise ValueError("constructor index must not be negative")
    return cbor2.CBORTag(102, [index, list(fields)])


def unconstr(value: Any) -> Tuple[int, list]:
    """(index, fields) of a constructor tree. Raises IdentityDecodeError."""
    if not isinstance(value, cbor2.CBORTag):
        raise IdentityDecodeError(f"expected a constructor, got {type(value).__name__}")
    tag = value.tag
    if 121 <= tag <= 127:
        return tag - 121, _as_list(value.value)
    if 1280 <= tag <= 1400:
        return tag - 1280 + 7, _as_list(value.value)
    if tag == 102:
        inner = _as_list(value.value)
        if len(inner) != 2 or not isinstance(inner[0], int):
            raise IdentityDecodeError("malformed constructor with tag 102")
        return inner[0], _as_list(inner[1])
    raise IdentityDecodeError(f"tag {tag} is not a constructor")


def _as_list(value: Any) -> list:
    if isinstance(value, (bytes, bytearray, str)) or not isinstance(value, Sequence):
        raise IdentityDecodeError(f"expected a list, got {type(value).__name__}")
    return list(value)


def data_from(value: Any) -> Any:
    """A Plutus Data tree from CBOR bytes, CBOR hex, a tree, or a pycardano
    object (``RawPlutusData`` has ``.data``, ``RawCBOR`` has ``.cbor``,
    ``PlutusData`` has ``.to_cbor()``)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return cbor2.loads(bytes(value))
    if isinstance(value, str):
        return cbor2.loads(bytes.fromhex(value))
    if isinstance(value, cbor2.CBORTag):
        return value
    data = getattr(value, "data", None)
    if isinstance(data, cbor2.CBORTag):
        return data
    raw = getattr(value, "cbor", None)
    if isinstance(raw, (bytes, bytearray)):
        return cbor2.loads(bytes(raw))
    to_cbor = getattr(value, "to_cbor", None)
    if callable(to_cbor):
        encoded = to_cbor()
        return cbor2.loads(bytes.fromhex(encoded) if isinstance(encoded, str) else encoded)
    raise IdentityDecodeError(f"cannot read Plutus Data from {type(value).__name__}")


# Plutus Data encoding, as `serialiseData` and Aiken `cbor.serialise` write it
# (PlutusCore.Data, https://github.com/IntersectMBO/plutus/blob/master/plutus-core/plutus-core/src/PlutusCore/Data.hs;
# CBOR is RFC 8949): a non-empty list (also the fields of a constructor) is an
# indefinite-length array, an empty list is 0x80, a byte string longer than
# 64 bytes is an indefinite-length string of 64-byte chunks, an integer
# outside 64 bits is a bignum (tag 2 or 3), a map is a definite-length map.


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 0x100:
        return bytes([(major << 5) | 24, value])
    if value < 0x10000:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    if value < 0x100000000:
        return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")
    return bytes([(major << 5) | 27]) + value.to_bytes(8, "big")


def _bytes(value: bytes) -> bytes:
    if len(value) <= 64:
        return _head(2, len(value)) + value
    chunks = b"".join(
        _head(2, len(value[i:i + 64])) + value[i:i + 64]
        for i in range(0, len(value), 64)
    )
    return b"\x5f" + chunks + b"\xff"


def _list(items: Sequence[Any]) -> bytes:
    if len(items) == 0:
        return b"\x80"
    return b"\x9f" + b"".join(serialise_data(item) for item in items) + b"\xff"


def serialise_data(value: Any) -> bytes:
    """The Plutus CBOR encoding of a Plutus Data tree."""
    if isinstance(value, bool):
        raise TypeError("bool is not Plutus Data; use constr(0|1, [])")
    if isinstance(value, int):
        if 0 <= value < 2 ** 64:
            return _head(0, value)
        if -(2 ** 64) <= value < 0:
            return _head(1, -1 - value)
        if value >= 0:
            magnitude = value
            tag = 2
        else:
            magnitude = -1 - value
            tag = 3
        size = max(1, (magnitude.bit_length() + 7) // 8)
        return _head(6, tag) + _bytes(magnitude.to_bytes(size, "big"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bytes(bytes(value))
    if isinstance(value, cbor2.CBORTag):
        index, fields = unconstr(value)
        if value.tag == 102:
            return _head(6, 102) + b"\x82" + _head(0, index) + _list(fields)
        return _head(6, value.tag) + _list(fields)
    if isinstance(value, Mapping):
        return _head(5, len(value)) + b"".join(
            serialise_data(k) + serialise_data(v) for k, v in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return _list(list(value))
    raise TypeError(f"not Plutus Data: {type(value).__name__}")


def hash_data(value: Any) -> bytes:
    """`blake2b_256(cbor.serialise(value))`, as Aiken computes it."""
    return blake2b_256(serialise_data(value))


# ── Encoders ────────────────────────────────────────────────────────────────


def option_data(value: Any) -> cbor2.CBORTag:
    """Aiken `Option`: `Some(x)` is `Constr 0 [x]`, `None` is `Constr 1 []`."""
    return constr(1, []) if value is None else constr(0, [value])


def signer_data(signer: Signer) -> cbor2.CBORTag:
    if signer.kind == SignerKind.CONTROLLER_SET:
        return constr(2, [])
    if signer.hash is None:
        raise ValueError(f"{signer.kind.name} signer needs a hash")
    return constr(int(signer.kind), [bytes(signer.hash)])


def key_data(key: Key) -> cbor2.CBORTag:
    return constr(0, [
        constr(int(key.key_type), []),
        bytes(key.key),
        int(key.purposes),
        option_data(None if key.pop is None else bytes(key.pop)),
    ])


def party_data(party: Party) -> cbor2.CBORTag:
    return constr(0, [
        constr(int(party.role), []),
        signer_data(party.signer),
        option_data(None if party.organisation is None else bytes(party.organisation)),
        int(party.not_before),
        option_data(party.not_after),
        int(party.rights),
        option_data(party.removal_after),
    ])


def lineage_data(lineage: Lineage) -> cbor2.CBORTag:
    if lineage.parent is None:
        return constr(0, [])
    return constr(1, [bytes(lineage.parent), int(lineage.parent_epoch), int(lineage.depth)])


def hold_data(hold: Hold) -> cbor2.CBORTag:
    return constr(0, [signer_data(hold.by), int(hold.reason), int(hold.since), int(hold.until)])


def status_data(status: Status) -> cbor2.CBORTag:
    if status.kind == StatusKind.ACTIVE:
        return constr(0, [])
    if status.kind == StatusKind.SUSPENDED:
        return constr(1, [[hold_data(h) for h in status.holds]])
    if status.kind == StatusKind.REVOKED:
        if status.by is None:
            raise ValueError("Revoked needs `by`")
        return constr(2, [
            signer_data(status.by),
            int(status.reason),
            int(status.at),
            option_data(status.invalid_from),
            option_data(None if status.successor is None else bytes(status.successor)),
        ])
    return constr(3, [
        int(status.at),
        option_data(None if status.successor is None else bytes(status.successor)),
    ])


def identity_datum_data(datum: IdentityDatum) -> cbor2.CBORTag:
    """The Plutus Data tree of a record datum (Part 1 §5)."""
    pending = datum.pending_recovery
    return constr(0, [
        bytes(datum.controller),
        [key_data(k) for k in datum.keys],
        [party_data(p) for p in datum.parties],
        int(datum.guardian_threshold),
        option_data(
            None if pending is None
            else constr(0, [bytes(pending.new_controller), int(pending.effective_after)])
        ),
        lineage_data(datum.lineage),
        int(datum.subagent_epoch),
        bytes(datum.name),
        bytes(datum.description),
        bytes(datum.framework),
        bytes(datum.endpoint),
        [bytes(c) for c in datum.capabilities],
        int(datum.created_at),
        status_data(datum.status),
    ])


def identity_datum_cbor(datum: IdentityDatum) -> bytes:
    """The inline datum bytes of a record (Plutus encoding)."""
    return serialise_data(identity_datum_data(datum))


# ── Decoders ────────────────────────────────────────────────────────────────


def _fields(value: Any, count: int, what: str, index: int = 0) -> list:
    got_index, fields = unconstr(value)
    if got_index != index or len(fields) != count:
        raise IdentityDecodeError(
            f"{what}: expected constructor {index} with {count} fields, "
            f"got constructor {got_index} with {len(fields)} fields"
        )
    return fields


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdentityDecodeError(f"{what}: expected an integer")
    return value


def _bytes_of(value: Any, what: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise IdentityDecodeError(f"{what}: expected a byte string")
    return bytes(value)


def _option(value: Any, decode: Callable[[Any], Any], what: str) -> Any:
    index, fields = unconstr(value)
    if index == 0 and len(fields) == 1:
        return decode(fields[0])
    if index == 1 and not fields:
        return None
    raise IdentityDecodeError(f"{what}: not an Option")


def _enum(value: Any, kind: type, what: str) -> Any:
    index, fields = unconstr(value)
    if fields:
        raise IdentityDecodeError(f"{what}: constructor without fields expected")
    try:
        return kind(index)
    except ValueError as exc:
        raise IdentityDecodeError(f"{what}: unknown constructor {index}") from exc


def decode_signer(value: Any) -> Signer:
    index, fields = unconstr(value)
    if index in (0, 1) and len(fields) == 1:
        return Signer(SignerKind(index), _bytes_of(fields[0], "signer hash"))
    if index == 2 and not fields:
        return Signer.controller_set()
    raise IdentityDecodeError("not a Signer")


def _decode_key(value: Any) -> Key:
    f = _fields(value, 4, "Key")
    return Key(
        key_type=_enum(f[0], KeyType, "KeyType"),
        key=_bytes_of(f[1], "key"),
        purposes=_int(f[2], "purposes"),
        pop=_option(f[3], lambda v: _bytes_of(v, "pop"), "pop"),
    )


def _decode_party(value: Any) -> Party:
    f = _fields(value, 7, "Party")
    return Party(
        role=_enum(f[0], Role, "Role"),
        signer=decode_signer(f[1]),
        organisation=_option(f[2], lambda v: _bytes_of(v, "organisation"), "organisation"),
        not_before=_int(f[3], "not_before"),
        not_after=_option(f[4], lambda v: _int(v, "not_after"), "not_after"),
        rights=_int(f[5], "rights"),
        removal_after=_option(f[6], lambda v: _int(v, "removal_after"), "removal_after"),
    )


def _decode_lineage(value: Any) -> Lineage:
    index, fields = unconstr(value)
    if index == 0 and not fields:
        return Lineage()
    if index == 1 and len(fields) == 3:
        return Lineage(
            parent=_bytes_of(fields[0], "parent"),
            parent_epoch=_int(fields[1], "parent_epoch"),
            depth=_int(fields[2], "depth"),
        )
    raise IdentityDecodeError("not a Lineage")


def _decode_hold(value: Any) -> Hold:
    f = _fields(value, 4, "Hold")
    return Hold(
        by=decode_signer(f[0]),
        reason=_int(f[1], "reason"),
        since=_int(f[2], "since"),
        until=_int(f[3], "until"),
    )


def _decode_pending(value: Any) -> PendingRecovery:
    f = _fields(value, 2, "PendingRecovery")
    return PendingRecovery(
        new_controller=_bytes_of(f[0], "new_controller"),
        effective_after=_int(f[1], "effective_after"),
    )


def _decode_status(value: Any) -> Status:
    index, fields = unconstr(value)
    opt_bytes = lambda v: _bytes_of(v, "successor")  # noqa: E731
    if index == 0 and not fields:
        return Status.active()
    if index == 1 and len(fields) == 1:
        return Status.suspended(_decode_hold(h) for h in _as_list(fields[0]))
    if index == 2 and len(fields) == 5:
        return Status.revoked(
            by=decode_signer(fields[0]),
            reason=_int(fields[1], "reason"),
            at=_int(fields[2], "at"),
            invalid_from=_option(fields[3], lambda v: _int(v, "invalid_from"), "invalid_from"),
            successor=_option(fields[4], opt_bytes, "successor"),
        )
    if index == 3 and len(fields) == 2:
        return Status.retired(
            at=_int(fields[0], "at"),
            successor=_option(fields[1], opt_bytes, "successor"),
        )
    raise IdentityDecodeError("not a Status")


def decode_identity_datum(value: Any) -> IdentityDatum:
    """Decode a record datum (CBOR bytes or hex, a tree, or a pycardano datum
    object; see `data_from`). Raises IdentityDecodeError for another shape."""
    try:
        tree = data_from(value)
    except IdentityDecodeError:
        raise
    except Exception as exc:  # cbor2 errors, bad hex
        raise IdentityDecodeError(f"not CBOR: {exc}") from exc
    f = _fields(tree, 14, "IdentityDatum")
    pending = _option(f[4], _decode_pending, "pending_recovery")
    return IdentityDatum(
        controller=_bytes_of(f[0], "controller"),
        keys=tuple(_decode_key(k) for k in _as_list(f[1])),
        parties=tuple(_decode_party(p) for p in _as_list(f[2])),
        guardian_threshold=_int(f[3], "guardian_threshold"),
        pending_recovery=pending,
        lineage=_decode_lineage(f[5]),
        subagent_epoch=_int(f[6], "subagent_epoch"),
        name=_bytes_of(f[7], "name"),
        description=_bytes_of(f[8], "description"),
        framework=_bytes_of(f[9], "framework"),
        endpoint=_bytes_of(f[10], "endpoint"),
        capabilities=tuple(_bytes_of(c, "capability") for c in _as_list(f[11])),
        created_at=_int(f[12], "created_at"),
        status=_decode_status(f[13]),
    )


# ── Library functions (Part 2 §3.10) ───────────────────────────────────────


def key_has_purpose(key: Key, purpose: int) -> bool:
    """The key's purposes contain `purpose`. The transaction and delegation
    purposes count only for Ed25519 keys; other purpose values never count."""
    if purpose in (PURPOSE_AUTHENTICATION, PURPOSE_ASSERTION):
        allowed = True
    elif purpose in (PURPOSE_TRANSACTION, PURPOSE_DELEGATION):
        allowed = key.key_type == KeyType.ED25519
    else:
        allowed = False
    return allowed and (key.purposes // purpose) % 2 == 1


def active_key(record: IdentityDatum, key_hash: bytes, purpose: int) -> bool:
    """The record lists a key with `blake2b_224(key) == key_hash` and the
    purpose. The controller key counts only when it is also a listed key."""
    key_hash = bytes(key_hash)
    return any(
        key_has_purpose(k, purpose) and blake2b_224(k.key) == key_hash
        for k in record.keys
    )


def controller_key_hash(record: IdentityDatum) -> bytes:
    """`blake2b_224(controller)`: the required signer for
    `controller_signed`."""
    return blake2b_224(record.controller)


def declared_credential(record: IdentityDatum, key_hash: bytes) -> bool:
    """A verification-key credential with this hash is a declared credential
    of the record: the controller's key hash, or a listed key with the
    transaction purpose (Part 2 §7, Module-1 and Module-6 rows)."""
    key_hash = bytes(key_hash)
    return key_hash == controller_key_hash(record) or active_key(
        record, key_hash, PURPOSE_TRANSACTION
    )


def _capability_bytes(capability: Union[bytes, str]) -> bytes:
    return capability.encode("utf-8") if isinstance(capability, str) else bytes(capability)


def has_capability(record: IdentityDatum, capability: Union[bytes, str]) -> bool:
    """Exact match of one capability (UTF-8 for a str)."""
    return _capability_bytes(capability) in record.capabilities


def missing_capabilities(
    record: IdentityDatum, capabilities: Iterable[Union[bytes, str]]
) -> List[bytes]:
    """The capabilities that the record does not list, in the given order."""
    return [
        _capability_bytes(c) for c in capabilities if not has_capability(record, c)
    ]


def independent(record: IdentityDatum) -> bool:
    """The record has no parent (lineage `Root`)."""
    return record.lineage.is_root


def is_tombstone(record: IdentityDatum) -> bool:
    """Status Revoked or Retired."""
    return record.status.is_terminal


def hold_in_force(status: Status, lo: int) -> bool:
    """A hold with `until > lo` exists."""
    return status.kind == StatusKind.SUSPENDED and any(h.until > lo for h in status.holds)


def covers(party: Party, lo: int, hi: int) -> bool:
    """`not_before <= lo`, and `not_after` is empty or `>= hi`."""
    return party.not_before <= lo and (party.not_after is None or party.not_after >= hi)


def accountable(record: IdentityDatum, lo: int, hi: int) -> bool:
    """Some party with role Accountable covers `[lo, hi]`."""
    return any(p.role == Role.ACCOUNTABLE and covers(p, lo, hi) for p in record.parties)


Lookup = Callable[[bytes], Optional[IdentityDatum]]


def effective(record: IdentityDatum, lookup: Lookup, lo: int, hi: int) -> Effective:
    """The effective status over `[lo, hi]`, as the on-chain `effective`
    computes it (the first rule that applies):

    1. a tombstone: Revoked or Retired;
    2. a subagent: the parent from `lookup` (MissingAncestorError when it
       returns None); Revoked when the parent's `subagent_epoch` is greater
       than `parent_epoch`, else the parent's effective status when it is not
       Active (at most MAX_DEPTH ancestors);
    3. a hold with `until > lo`: Suspended;
    4. no Accountable Party covers the window: Suspended;
    5. else Active.

    This is the on-chain rule for a transaction window. A resolver also
    counts confirmations (Part 1 §7.3); this function does not.
    """
    return _effective_within(record, lookup, lo, hi, MAX_DEPTH)


def _effective_within(
    record: IdentityDatum, lookup: Lookup, lo: int, hi: int, ancestors_left: int
) -> Effective:
    if record.status.kind == StatusKind.REVOKED:
        return Effective.REVOKED
    if record.status.kind == StatusKind.RETIRED:
        return Effective.RETIRED
    inherited = Effective.ACTIVE
    lineage = record.lineage
    if lineage.parent is not None:
        if ancestors_left <= 0:
            raise IdentityError("the lineage has more ancestors than MAX_DEPTH")
        parent = lookup(lineage.parent)
        if parent is None:
            raise MissingAncestorError(lineage.parent)
        if parent.subagent_epoch > lineage.parent_epoch:
            inherited = Effective.REVOKED
        else:
            inherited = _effective_within(parent, lookup, lo, hi, ancestors_left - 1)
    if inherited != Effective.ACTIVE:
        return inherited
    if hold_in_force(record.status, lo):
        return Effective.SUSPENDED
    if accountable(record, lo, hi):
        return Effective.ACTIVE
    return Effective.SUSPENDED


def record_chain(
    agent_id: Union[bytes, str],
    fetch: Callable[[bytes], Optional[IdentityRecord]],
) -> List[IdentityRecord]:
    """The record of `agent_id` and the ancestor records that `effective`
    reads, in order: [record, parent, grandparent, ...].

    A transaction that checks the effective status needs all of them as
    reference inputs. The walk stops at a tombstone, at a Root record, and at
    a parent whose `subagent_epoch` already revokes the child, as on-chain.
    `fetch(agent_id)` returns the authentic record or None. Raises
    RecordNotFoundError when the record is missing and MissingAncestorError
    when an ancestor is missing.
    """
    agent_id = normalize_agent_id(agent_id)
    first = fetch(agent_id)
    if first is None:
        raise RecordNotFoundError(agent_id)
    chain = [first]
    current = first
    ancestors_left = MAX_DEPTH
    while not is_tombstone(current.datum) and current.datum.lineage.parent is not None:
        if ancestors_left <= 0:
            raise IdentityError("the lineage has more ancestors than MAX_DEPTH")
        parent_id = current.datum.lineage.parent
        parent = fetch(parent_id)
        if parent is None:
            raise MissingAncestorError(parent_id)
        chain.append(parent)
        if parent.datum.subagent_epoch > current.datum.lineage.parent_epoch:
            break
        current = parent
        ancestors_left -= 1
    return chain


def chain_lookup(chain: Iterable[IdentityRecord]) -> Lookup:
    """A `lookup` for `effective` over the records of a chain."""
    by_id = {r.agent_id: r.datum for r in chain}
    return by_id.get


def effective_of_chain(chain: Sequence[IdentityRecord], lo: int, hi: int) -> Effective:
    """`effective` of `chain[0]` with the ancestors of the chain."""
    if not chain:
        raise ValueError("empty chain")
    return effective(chain[0].datum, chain_lookup(chain), lo, hi)


def check_window(lo: int, hi: int) -> None:
    """Raise IdentityError unless `lo <= hi` and `hi - lo <= MAX_RANGE_MS`
    (Part 2 §3.2 and §7)."""
    if lo > hi or hi - lo > MAX_RANGE_MS:
        raise IdentityError(
            f"validity range [{lo}, {hi}] must have lo <= hi and hi - lo <= {MAX_RANGE_MS}"
        )


def require_active(
    chain: Sequence[IdentityRecord],
    lo: int,
    hi: int,
    capabilities: Iterable[Union[bytes, str]] = (),
) -> IdentityDatum:
    """Check the window, that `chain[0]` is Active over `[lo, hi]` and that it
    lists each capability. Returns the record datum."""
    check_window(lo, hi)
    status = effective_of_chain(chain, lo, hi)
    if status != Effective.ACTIVE:
        raise IdentityStatusError(chain[0].agent_id, status, Effective.ACTIVE)
    missing = missing_capabilities(chain[0].datum, capabilities)
    if missing:
        raise IdentityError(
            f"{chain[0].did} does not list the capabilities "
            + ", ".join(m.decode("utf-8", "replace") for m in missing)
        )
    return chain[0].datum


def require_declared_credential(record: IdentityRecord, key_hash: bytes) -> None:
    """Raise IdentityError unless `key_hash` is a declared credential of the
    record (`declared_credential`)."""
    if not declared_credential(record.datum, key_hash):
        raise IdentityError(
            f"key hash {bytes(key_hash).hex()} is neither the controller of "
            f"{record.did} nor a listed key with the transaction purpose"
        )


# ── Authentic record outputs (Part 1 §4, Part 2 §3.2) ──────────────────────


def single_token(policy_tokens: Mapping[bytes, int], agent_id: bytes) -> bool:
    """The value holds exactly one token of the identity policy in total:
    `agent_id` with quantity 1."""
    items = list(policy_tokens.items())
    return len(items) == 1 and bytes(items[0][0]) == bytes(agent_id) and items[0][1] == 1


def record_from_utxo(utxo: Any, identity_hash: bytes) -> Optional[IdentityRecord]:
    """The record in a pycardano ``UTxO`` when it is authentic: at the exact
    identity address (payment credential `Script(identity_hash)`, no stake
    credential), exactly one token of `identity_hash` in total with quantity
    1, and an inline datum. Else None. Raises IdentityDecodeError when such
    an output has a datum that does not decode (it cannot exist under the
    contract; on-chain `find_record` fails).

    The function reads the pycardano attributes (``output.address.
    payment_part``, ``staking_part``, ``amount.multi_asset``,
    ``output.datum``) without importing pycardano.
    """
    identity_hash = bytes(identity_hash)
    output = getattr(utxo, "output", utxo)
    address = output.address
    payment = address.payment_part
    if type(payment).__name__ != "ScriptHash" or bytes(payment.payload) != identity_hash:
        return None
    if address.staking_part is not None:
        return None
    multi_asset = getattr(output.amount, "multi_asset", None) or {}
    tokens: dict = {}
    for policy, assets in multi_asset.items():
        if bytes(policy.payload) == identity_hash:
            for name, quantity in assets.items():
                tokens[bytes(name.payload)] = tokens.get(bytes(name.payload), 0) + int(quantity)
    tokens = {k: v for k, v in tokens.items() if v != 0}
    if len(tokens) != 1:
        return None
    (agent_id, quantity), = tokens.items()
    if quantity != 1:
        return None
    datum = getattr(output, "datum", None)
    if datum is None:
        return None
    return IdentityRecord(agent_id=agent_id, datum=decode_identity_datum(datum), utxo=utxo)


def find_record(
    utxos: Iterable[Any], identity_hash: bytes, agent_id: Union[bytes, str]
) -> Optional[IdentityRecord]:
    """The authentic record of `agent_id` among pycardano UTxOs (for example
    the UTxOs at the identity address), or None."""
    agent_id = normalize_agent_id(agent_id)
    for utxo in utxos:
        record = record_from_utxo(utxo, identity_hash)
        if record is not None and record.agent_id == agent_id:
            return record
    return None


# ── Identity redeemers (Part 2 §3.1) ───────────────────────────────────────


def register_redeemer(seed_tx_id: bytes, seed_index: int) -> cbor2.CBORTag:
    """`IdentityMint.Register { seed }`."""
    return constr(0, [output_reference_data(seed_tx_id, seed_index)])


def adopt_redeemer(proof: Sequence[bytes]) -> cbor2.CBORTag:
    """`IdentityMint.Adopt { proof }`."""
    return constr(1, [[bytes(p) for p in proof]])


def by_controller() -> cbor2.CBORTag:
    """`Actor.ByController`."""
    return constr(0, [])


def by_party(index: int) -> cbor2.CBORTag:
    """`Actor.ByParty { index }`."""
    return constr(1, [int(index)])


def update_redeemer() -> cbor2.CBORTag:
    """`IdentitySpend.Update`."""
    return constr(0, [])


def suspend_redeemer(actor: cbor2.CBORTag) -> cbor2.CBORTag:
    """`IdentitySpend.Suspend { actor }`."""
    return constr(2, [actor])


def unsuspend_redeemer(author: Signer) -> cbor2.CBORTag:
    """`IdentitySpend.Unsuspend { author }`."""
    return constr(3, [signer_data(author)])


def revoke_redeemer(actor: cbor2.CBORTag) -> cbor2.CBORTag:
    """`IdentitySpend.Revoke { actor }`."""
    return constr(4, [actor])


# ── Record changes (Part 2 §3.5, §3.7) ─────────────────────────────────────


def is_current_author(record: IdentityDatum, signer: Signer) -> bool:
    """`ControllerSet`, the signer of a party of the record, or any `Script`
    signer (Part 2 §3.2)."""
    if signer.kind in (SignerKind.CONTROLLER_SET, SignerKind.SCRIPT):
        return True
    return any(p.signer == signer for p in record.parties)


def holds_after_suspend(
    record: IdentityDatum, author: Signer, reason: int, lo: int, until: int
) -> Tuple[Hold, ...]:
    """The holds of `Suspend` by `author`: the holds of the record that are in
    force at `lo` and whose author is a current author other than `author`,
    in their order, then the new hold `{author, reason, lo, until}`."""
    kept = tuple(
        h for h in (record.status.holds if record.status.kind == StatusKind.SUSPENDED else ())
        if h.until > lo and is_current_author(record, h.by) and h.by != author
    )
    return kept + (Hold(by=author, reason=reason, since=lo, until=until),)


def hold_of(record: IdentityDatum, author: Signer, lo: int) -> Optional[Hold]:
    """The hold of `author` that is in force at `lo` (`until > lo`), or
    None."""
    if record.status.kind != StatusKind.SUSPENDED:
        return None
    for h in record.status.holds:
        if h.by == author and h.until > lo:
            return h
    return None


def suspended(
    record: IdentityDatum, author: Signer, reason: int, lo: int, until: int
) -> IdentityDatum:
    """The record after `Suspend` by `author` (other fields unchanged)."""
    return dataclasses.replace(
        record, status=Status.suspended(holds_after_suspend(record, author, reason, lo, until))
    )


def tombstone(record: IdentityDatum, status: Status) -> IdentityDatum:
    """The tombstone of a record with a terminal status (Part 2 §3.7): keys,
    parties, guardian threshold, pending recovery, description, framework,
    endpoint and capabilities are cleared; the other fields stay."""
    if not status.is_terminal:
        raise ValueError("a tombstone has status Revoked or Retired")
    return dataclasses.replace(
        record,
        keys=(),
        parties=(),
        guardian_threshold=0,
        pending_recovery=None,
        description=b"",
        framework=b"",
        endpoint=b"",
        capabilities=(),
        status=status,
    )
