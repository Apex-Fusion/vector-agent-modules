"""Plutus Data of the Module-1 jury (SPEC §5) and the Module-1 data that the
jury reads (SPEC §5.7).

The jury types are pycardano ``PlutusData`` classes. pycardano writes
constructor ``n`` (0..6) as CBOR tag ``121 + n`` with an indefinite field
list, and decodes the ``Union`` fields by their tags. The decoders of this
module are strict: they decode, write the value again and compare the two
Plutus Data trees, so a datum with an extra or a missing field, or with a
value of another type, raises ``DecodeError``.

Names that clash in the specification are prefixed here:

- ``MintAction``: ``MintRegister`` (Register), ``MintOpen`` (Open),
  ``MintBurn`` (Burn).
- ``CaseState``: ``Open`` and ``Settling`` (as in the specification).
- Plutus ``Address``: ``PlutusAddress`` (pycardano has its own ``Address``).

The Module-1 ``ClaimDatum`` and ``ChallengeDatum`` are plain dataclasses
(``ClaimDatum``, ``ChallengeDatum``) with the field order of
``Module-1/contracts/lib/adversarial_auditing/types.ak``.
"""

from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import Any, List, Optional, Tuple, Union

import cbor2
from pycardano import Address, Network, PlutusData, ScriptHash, TransactionId, TransactionInput, VerificationKeyHash
from pycardano.address import PointerAddress
from pycardano.serialization import ByteString

from . import _paths  # noqa: F401  (Module-1 on sys.path)
from simulation import vector_identity as vi


class DecodeError(ValueError):
    """A value is not the Plutus Data shape that the jury expects."""


# ── Vote (§5.1) ──────────────────────────────────────────────────────────────


@dataclass
class ClaimerWins(PlutusData):
    CONSTR_ID = 0


@dataclass
class AuditorWins(PlutusData):
    CONSTR_ID = 1


@dataclass
class Inconclusive(PlutusData):
    CONSTR_ID = 2


Vote = Union[ClaimerWins, AuditorWins, Inconclusive]

#: The vote names by constructor index (the same as the Module-1 Verdict).
VOTE_NAMES = ("ClaimerWins", "AuditorWins", "Inconclusive")
_VOTE_CLASSES = (ClaimerWins, AuditorWins, Inconclusive)


def vote(value: Union[str, int, PlutusData]) -> PlutusData:
    """A vote from its name ("ClaimerWins", ...), its index (0, 1, 2) or a
    vote object."""
    if isinstance(value, _VOTE_CLASSES):
        return value
    if isinstance(value, bool):
        raise ValueError(f"not a vote: {value!r}")
    if isinstance(value, int):
        if 0 <= value < 3:
            return _VOTE_CLASSES[value]()
        raise ValueError(f"vote index must be 0, 1 or 2, got {value}")
    if isinstance(value, str) and value in VOTE_NAMES:
        return _VOTE_CLASSES[VOTE_NAMES.index(value)]()
    raise ValueError(f"vote must be one of {', '.join(VOTE_NAMES)}, got {value!r}")


def vote_index(value: Union[str, int, PlutusData]) -> int:
    return vote(value).CONSTR_ID


def vote_name(value: Union[str, int, PlutusData]) -> str:
    return VOTE_NAMES[vote_index(value)]


# ── Option<Int> ──────────────────────────────────────────────────────────────


@dataclass
class Some(PlutusData):
    """Aiken ``Some(value)``: Constr 0 [value]."""

    CONSTR_ID = 0
    value: int


@dataclass
class Nothing(PlutusData):
    """Aiken ``None``: Constr 1 []."""

    CONSTR_ID = 1


OptionInt = Union[Some, Nothing]


def option_value(option: PlutusData) -> Optional[int]:
    return option.value if isinstance(option, Some) else None


# ── OutputReference ──────────────────────────────────────────────────────────


@dataclass
class OutputReference(PlutusData):
    """Plutus V3 ``OutputReference { transaction_id, output_index }``."""

    CONSTR_ID = 0
    transaction_id: bytes
    output_index: int

    @classmethod
    def of(cls, ref: TransactionInput) -> "OutputReference":
        return cls(bytes(ref.transaction_id), int(ref.index))

    def to_input(self) -> TransactionInput:
        return TransactionInput(TransactionId(bytes(self.transaction_id)), self.output_index)


# ── Address ──────────────────────────────────────────────────────────────────


@dataclass
class VerificationKeyCredential(PlutusData):
    CONSTR_ID = 0
    hash: bytes


@dataclass
class ScriptCredential(PlutusData):
    CONSTR_ID = 1
    hash: bytes


Credential = Union[VerificationKeyCredential, ScriptCredential]


@dataclass
class StakeInline(PlutusData):
    CONSTR_ID = 0
    credential: Credential


@dataclass
class StakePointer(PlutusData):
    CONSTR_ID = 1
    slot: int
    transaction: int
    certificate: int


@dataclass
class SomeStake(PlutusData):
    CONSTR_ID = 0
    value: Union[StakeInline, StakePointer]


@dataclass
class NoStake(PlutusData):
    CONSTR_ID = 1


@dataclass
class PlutusAddress(PlutusData):
    """Plutus ``Address { payment_credential, stake_credential }``."""

    CONSTR_ID = 0
    payment_credential: Credential
    stake_credential: Union[SomeStake, NoStake]


def _credential(part) -> PlutusData:
    if isinstance(part, VerificationKeyHash):
        return VerificationKeyCredential(bytes(part.payload))
    if isinstance(part, ScriptHash):
        return ScriptCredential(bytes(part.payload))
    raise ValueError(f"not a credential: {part!r}")


def plutus_address(address: Union[Address, str]) -> PlutusAddress:
    """The Plutus ``Address`` of a pycardano address (or bech32 text)."""
    if isinstance(address, str):
        address = Address.from_primitive(address)
    if address.payment_part is None:
        raise ValueError(f"{address} has no payment part (a reward address)")
    stake = address.staking_part
    if stake is None:
        stake_data = NoStake()
    elif isinstance(stake, PointerAddress):
        stake_data = SomeStake(StakePointer(stake.slot, stake.tx_index, stake.cert_index))
    else:
        stake_data = SomeStake(StakeInline(_credential(stake)))
    return PlutusAddress(_credential(address.payment_part), stake_data)


def _part(credential: PlutusData):
    if isinstance(credential, VerificationKeyCredential):
        return VerificationKeyHash(bytes(credential.hash))
    return ScriptHash(bytes(credential.hash))


def address_of(address: PlutusAddress, network: Network = Network.MAINNET) -> Address:
    """The pycardano address of a Plutus ``Address`` (Vector uses network id 1
    on mainnet and testnet)."""
    stake = address.stake_credential
    if isinstance(stake, NoStake):
        staking = None
    elif isinstance(stake.value, StakePointer):
        p = stake.value
        staking = PointerAddress(p.slot, p.transaction, p.certificate)
    else:
        staking = _part(stake.value.credential)
    return Address(_part(address.payment_credential), staking, network=network)


# ── Juror datum (§5.2) ───────────────────────────────────────────────────────


@dataclass
class OpenVote(PlutusData):
    CONSTR_ID = 0
    case: bytes
    reveal_end: int


@dataclass
class Juror(PlutusData):
    """``JuryDatum.Juror`` (Constr 0)."""

    CONSTR_ID = 0
    agent: bytes
    owner: bytes
    prompt_hash: bytes
    registered_at: int
    exit_after: OptionInt
    open: List[OpenVote]

    def validate(self) -> None:
        _length(self.agent, 32, "Juror.agent")
        _length(self.owner, 28, "Juror.owner")
        _length(self.prompt_hash, 32, "Juror.prompt_hash")
        for entry in self.open:
            _length(entry.case, 32, "OpenVote.case")


# ── Case datum (§5.3) ────────────────────────────────────────────────────────


@dataclass
class Committed(PlutusData):
    CONSTR_ID = 0


@dataclass
class Revealed(PlutusData):
    CONSTR_ID = 1
    vote: Vote


@dataclass
class Paid(PlutusData):
    CONSTR_ID = 2
    vote: Vote


BallotStatus = Union[Committed, Revealed, Paid]


@dataclass
class Ballot(PlutusData):
    CONSTR_ID = 0
    agent: bytes
    juror: bytes
    commit: bytes
    status: BallotStatus


@dataclass
class Tally(PlutusData):
    CONSTR_ID = 0
    claimer: int
    auditor: int
    inconclusive: int

    @property
    def total(self) -> int:
        return self.claimer + self.auditor + self.inconclusive

    def counts(self) -> dict:
        """The counts by vote name (the ``tally`` object of the resolution
        document, SPEC §10.5)."""
        return {"ClaimerWins": self.claimer, "AuditorWins": self.auditor, "Inconclusive": self.inconclusive}

    def plus(self, value) -> "Tally":
        """The tally with one more vote ``value``."""
        index = vote_index(value)
        return Tally(self.claimer + (index == 0), self.auditor + (index == 1), self.inconclusive + (index == 2))


@dataclass
class Open(PlutusData):
    """``CaseState.Open`` (Constr 0)."""

    CONSTR_ID = 0


@dataclass
class Settling(PlutusData):
    """``CaseState.Settling { share }`` (Constr 1)."""

    CONSTR_ID = 1
    share: int


CaseState = Union[Open, Settling]


@dataclass
class Case(PlutusData):
    """``JuryDatum.Case`` (Constr 1, 17 fields, SPEC §5.3 after amendment 2:
    the document URIs are not in the case; ``challenge_out`` and
    ``claim_out`` are the Module-1 outputs that the opening read)."""

    CONSTR_ID = 1
    challenge: bytes
    claim: bytes
    challenge_out: OutputReference
    claim_out: OutputReference
    claimer: bytes
    auditor: bytes
    claim_hash: bytes
    evidence_hash: bytes
    deadline: int
    opened_at: int
    commit_end: int
    reveal_end: int
    refund_to: PlutusAddress
    fee: int
    ballots: List[Ballot]
    tally: Tally
    state: CaseState

    def validate(self) -> None:
        for name in ("challenge", "claim", "claimer", "auditor", "claim_hash", "evidence_hash"):
            _length(getattr(self, name), 32, f"Case.{name}")
        for name in ("challenge_out", "claim_out"):
            _length(getattr(self, name).transaction_id, 32, f"Case.{name}.transaction_id")
        for ballot in self.ballots:
            _length(ballot.agent, 32, "Ballot.agent")
            _length(ballot.juror, 32, "Ballot.juror")
            # Amendment 8: a reveal empties the commit (§6.10 rule 6).
            _length(ballot.commit, 32 if isinstance(ballot.status, Committed) else 0, "Ballot.commit")

    @property
    def settling(self) -> bool:
        return isinstance(self.state, Settling)

    @property
    def state_name(self) -> str:
        return "Settling" if self.settling else "Open"


JuryDatum = Union[Juror, Case]


def _length(value: bytes, size: int, what: str) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) != size:
        raise DecodeError(f"{what} must be {size} bytes")


# ── Redeemers (§5.4, §5.5) ───────────────────────────────────────────────────


@dataclass
class MintRegister(PlutusData):
    """``MintAction.Register { seed }`` (Constr 0)."""

    CONSTR_ID = 0
    seed: OutputReference


@dataclass
class MintOpen(PlutusData):
    """``MintAction.Open { seed }`` (Constr 1)."""

    CONSTR_ID = 1
    seed: OutputReference


@dataclass
class MintBurn(PlutusData):
    """``MintAction.Burn`` (Constr 2)."""

    CONSTR_ID = 2


MintAction = Union[MintRegister, MintOpen, MintBurn]


@dataclass
class Commit(PlutusData):
    CONSTR_ID = 0
    case: bytes


@dataclass
class Reveal(PlutusData):
    CONSTR_ID = 1
    case: bytes


@dataclass
class Claim(PlutusData):
    CONSTR_ID = 2
    case: bytes


@dataclass
class Slash(PlutusData):
    CONSTR_ID = 3
    case: bytes


@dataclass
class RequestExit(PlutusData):
    CONSTR_ID = 4


@dataclass
class Exit(PlutusData):
    CONSTR_ID = 5


@dataclass
class TopUp(PlutusData):
    CONSTR_ID = 6


JurorAction = Union[Commit, Reveal, Claim, Slash, RequestExit, Exit, TopUp]


@dataclass
class CaseCommit(PlutusData):
    CONSTR_ID = 0
    juror: bytes
    commit: bytes


@dataclass
class CaseReveal(PlutusData):
    CONSTR_ID = 1
    juror: bytes
    vote: Vote
    salt: bytes
    record_hash: bytes


@dataclass
class Finalize(PlutusData):
    CONSTR_ID = 2
    result: Vote


@dataclass
class CaseClaim(PlutusData):
    CONSTR_ID = 3
    juror: bytes


@dataclass
class Close(PlutusData):
    CONSTR_ID = 4


CaseAction = Union[CaseCommit, CaseReveal, Finalize, CaseClaim, Close]


@dataclass
class OnJuror(PlutusData):
    """``SpendAction.OnJuror(JurorAction)`` (Constr 0)."""

    CONSTR_ID = 0
    action: JurorAction


@dataclass
class OnCase(PlutusData):
    """``SpendAction.OnCase(CaseAction)`` (Constr 1)."""

    CONSTR_ID = 1
    action: CaseAction


SpendAction = Union[OnJuror, OnCase]


# ── Parameter (§5.6) ─────────────────────────────────────────────────────────


@dataclass
class JuryParams(PlutusData):
    """The validator parameter (Constr 0, 16 fields, SPEC §5.6)."""

    CONSTR_ID = 0
    identity_hash: bytes
    m1_claim: bytes
    m1_challenge: bytes
    treasury: PlutusAddress
    bond_min: int
    slash: int
    juror_floor: int
    eligibility_delay: int
    exit_notice: int
    commit_window: int
    reveal_window: int
    resolve_margin: int
    claim_period: int
    quorum: int
    majority_num: int
    majority_den: int

    def validate(self) -> None:
        for name in ("identity_hash", "m1_claim", "m1_challenge"):
            _length(getattr(self, name), 28, f"JuryParams.{name}")


#: The field names of JuryParams in the order of SPEC §5.6.
PARAM_FIELDS = (
    "identity_hash", "m1_claim", "m1_challenge", "treasury", "bond_min", "slash", "juror_floor",
    "eligibility_delay", "exit_notice", "commit_window", "reveal_window", "resolve_margin",
    "claim_period", "quorum", "majority_num", "majority_den",
)


# ── Strict decoding ──────────────────────────────────────────────────────────


def to_cbor(data: PlutusData) -> bytes:
    raw = data.to_cbor()
    return raw if isinstance(raw, bytes) else bytes.fromhex(raw)


def _normal(value: Any) -> Any:
    """A comparable Plutus Data tree: definite and indefinite lists and the
    chunked and plain byte strings compare equal."""
    if isinstance(value, cbor2.CBORTag):
        return ("tag", value.tag, _normal(value.value))
    if isinstance(value, ByteString):
        return bytes(value.value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, bool):
        raise DecodeError("a boolean is not Plutus Data")
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        return ("map", tuple((_normal(k), _normal(v)) for k, v in value.items()))
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(_normal(v) for v in value)
    raise DecodeError(f"not Plutus Data: {type(value).__name__}")


def tree(raw: Any) -> Any:
    """The Plutus Data tree (cbor2 objects, plain lists) of CBOR bytes, CBOR
    hex, a pycardano datum object (``RawCBOR``, ``RawPlutusData``,
    ``PlutusData``) or a cbor2 tree."""
    try:
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return cbor2.loads(bytes(raw))
        if isinstance(raw, str):
            return cbor2.loads(bytes.fromhex(raw))
        if isinstance(raw, cbor2.CBORTag):
            return cbor2.loads(cbor2.dumps(raw, default=_plain_default))
        cbor = getattr(raw, "cbor", None)
        if isinstance(cbor, (bytes, bytearray)):
            return cbor2.loads(bytes(cbor))
        to_cbor = getattr(raw, "to_cbor", None)
        if callable(to_cbor):
            encoded = to_cbor()
            return cbor2.loads(bytes.fromhex(encoded) if isinstance(encoded, str) else encoded)
    except Exception as error:  # noqa: BLE001 - any decode error
        raise DecodeError(f"not Plutus Data: {error}") from None
    raise DecodeError(f"cannot read Plutus Data from {type(raw).__name__}")


def _plain_default(encoder, value):
    """cbor2 encoder hook for the list and byte-string types of pycardano."""
    if isinstance(value, ByteString):
        encoder.encode(bytes(value.value))
    elif isinstance(value, Sequence):
        encoder.encode(list(value))
    else:
        raise ValueError(f"cannot encode {type(value).__name__}")


def output_datum(output) -> Any:
    """The Plutus Data tree of the inline datum of a pycardano
    ``TransactionOutput`` (or None). Ogmios outputs hold ``RawCBOR``; outputs
    decoded from a transaction hold ``RawPlutusData`` for a constructor and
    the plain value (bytes, int, list) otherwise."""
    datum = getattr(output, "datum", None)
    if datum is None:
        return None
    if isinstance(datum, (bytes, bytearray, int, list)) and not isinstance(datum, bool):
        return datum
    return tree(datum)


def data_from_json(value: Any) -> Any:
    """A Plutus Data tree from the JSON form of Koios/db-sync
    (``{"constructor": n, "fields": [..]}``, ``{"bytes": hex}``,
    ``{"int": n}``, ``{"list": [..]}``, ``{"map": [{"k": .., "v": ..}]}``),
    usable by the decoders of this module."""
    if not isinstance(value, dict):
        raise DecodeError(f"not Plutus Data JSON: {value!r}")
    if "constructor" in value:
        return vi.constr(int(value["constructor"]), [data_from_json(f) for f in value.get("fields") or []])
    if "bytes" in value:
        return bytes.fromhex(value["bytes"])
    if "int" in value:
        return int(value["int"])
    if "list" in value:
        return [data_from_json(v) for v in value["list"]]
    if "map" in value:
        return {data_from_json(e["k"]): data_from_json(e["v"]) for e in value["map"]}
    raise DecodeError(f"not Plutus Data JSON: {value!r}")


def same_data(a: Any, b: Any) -> bool:
    """True when two values are the same Plutus Data (any list or byte string
    encoding)."""
    return _normal(tree(a)) == _normal(tree(b))


def _strict(cls, data: Any):
    try:
        value = cls.from_primitive(data)
    except Exception as error:  # noqa: BLE001 - pycardano raises several types
        raise DecodeError(f"not a {cls.__name__}: {error}") from None
    if _normal(cbor2.loads(to_cbor(value))) != _normal(data):
        raise DecodeError(f"not a {cls.__name__}: the value has other fields or field types")
    validate = getattr(value, "validate", None)
    if validate is not None:
        validate()
    return value


def decode_jury_datum(raw: Any) -> Union[Juror, Case]:
    """The ``JuryDatum`` of an inline datum (strict; field sizes checked)."""
    data = tree(raw)
    if not isinstance(data, cbor2.CBORTag) or data.tag not in (121, 122):
        raise DecodeError("not a JuryDatum (Constr 0 Juror or Constr 1 Case)")
    return _strict(Juror if data.tag == 121 else Case, data)


def decode_juror(raw: Any) -> Juror:
    datum = decode_jury_datum(raw)
    if not isinstance(datum, Juror):
        raise DecodeError("not a Juror datum")
    return datum


def decode_case(raw: Any) -> Case:
    datum = decode_jury_datum(raw)
    if not isinstance(datum, Case):
        raise DecodeError("not a Case datum")
    return datum


def decode_spend_action(raw: Any) -> Union[OnJuror, OnCase]:
    data = tree(raw)
    if not isinstance(data, cbor2.CBORTag) or data.tag not in (121, 122):
        raise DecodeError("not a SpendAction (Constr 0 OnJuror or Constr 1 OnCase)")
    return _strict(OnJuror if data.tag == 121 else OnCase, data)


def decode_mint_action(raw: Any) -> Union[MintRegister, MintOpen, MintBurn]:
    data = tree(raw)
    classes = {121: MintRegister, 122: MintOpen, 123: MintBurn}
    if not isinstance(data, cbor2.CBORTag) or data.tag not in classes:
        raise DecodeError("not a MintAction")
    return _strict(classes[data.tag], data)


def decode_params(raw: Any) -> JuryParams:
    return _strict(JuryParams, tree(raw))


# ── Module-1 data (§5.7) ─────────────────────────────────────────────────────

#: ClaimState constructors of Module-1.
CLAIM_STATES = ("Open", "Challenged", "Validated", "Invalidated")
#: ChallengeState constructors of Module-1.
CHALLENGE_STATES = ("PendingOracle", "PendingJury", "Voting", "Resolved")


@dataclass(frozen=True)
class M1Credential:
    """A Module-1 ``Credential``: ``key`` (VerificationKey) or ``script``."""

    script: bool
    hash: bytes

    def data(self) -> cbor2.CBORTag:
        return vi.constr(1 if self.script else 0, [self.hash])


def _m1_credential(value: Any, what: str) -> M1Credential:
    index, fields = _constr(value, what)
    if index not in (0, 1) or len(fields) != 1 or not isinstance(fields[0], bytes):
        raise DecodeError(f"{what}: not a Credential")
    return M1Credential(index == 1, bytes(fields[0]))


def _constr(value: Any, what: str) -> Tuple[int, list]:
    try:
        return vi.unconstr(value)
    except vi.IdentityDecodeError as error:
        raise DecodeError(f"{what}: {error}") from None


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecodeError(f"{what}: expected an integer")
    return value


def _bytes(value: Any, what: str, size: Optional[int] = None) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise DecodeError(f"{what}: expected a byte string")
    if size is not None and len(value) != size:
        raise DecodeError(f"{what}: expected {size} bytes")
    return bytes(value)


@dataclass(frozen=True)
class ClaimDatum:
    """The Module-1 ``ClaimDatum`` (9 fields)."""

    claimer_did: bytes
    claimer_credential: M1Credential
    claim_hash: bytes
    claim_type: bytes
    storage_uri: bytes
    stake_amount: int
    submitted_at: int
    challenge_window: int
    state: int

    @property
    def state_name(self) -> str:
        return CLAIM_STATES[self.state] if 0 <= self.state < len(CLAIM_STATES) else f"Constr {self.state}"

    @property
    def challenged(self) -> bool:
        return self.state == 1

    def data(self) -> cbor2.CBORTag:
        return vi.constr(0, [
            self.claimer_did, self.claimer_credential.data(), self.claim_hash, self.claim_type,
            self.storage_uri, self.stake_amount, self.submitted_at, self.challenge_window,
            vi.constr(self.state, []),
        ])

    def cbor(self) -> bytes:
        return vi.serialise_data(self.data())


def decode_claim_datum(raw: Any) -> ClaimDatum:
    index, f = _constr(tree(raw), "ClaimDatum")
    if index != 0 or len(f) != 9:
        raise DecodeError("not a ClaimDatum (Constr 0 with 9 fields)")
    state, state_fields = _constr(f[8], "ClaimDatum.state")
    if state_fields:
        raise DecodeError("ClaimDatum.state: a ClaimState has no fields")
    return ClaimDatum(
        claimer_did=_bytes(f[0], "claimer_did"),
        claimer_credential=_m1_credential(f[1], "claimer_credential"),
        claim_hash=_bytes(f[2], "claim_hash"),
        claim_type=_bytes(f[3], "claim_type"),
        storage_uri=_bytes(f[4], "storage_uri"),
        stake_amount=_int(f[5], "stake_amount"),
        submitted_at=_int(f[6], "submitted_at"),
        challenge_window=_int(f[7], "challenge_window"),
        state=state,
    )


@dataclass(frozen=True)
class ChallengeDatum:
    """The Module-1 ``ChallengeDatum`` (11 fields). ``state`` is the index of
    the ChallengeState constructor; ``state_fields`` keeps its fields as
    Plutus Data trees (``Voting`` and ``Resolved`` have fields)."""

    claim_ref: Tuple[bytes, int]
    auditor_did: bytes
    auditor_credential: M1Credential
    stake_amount: int
    evidence_hash: bytes
    evidence_uri: bytes
    challenged_at: int
    resolution_deadline: int
    eligible_jurors: Tuple[bytes, ...]
    state: int
    claim_token: bytes
    state_fields: Tuple[Any, ...] = field(default=(), compare=False)

    @property
    def deadline(self) -> int:
        """``challenged_at + resolution_deadline`` (POSIX ms)."""
        return self.challenged_at + self.resolution_deadline

    @property
    def state_name(self) -> str:
        return CHALLENGE_STATES[self.state] if 0 <= self.state < len(CHALLENGE_STATES) else f"Constr {self.state}"

    @property
    def pending_oracle(self) -> bool:
        return self.state == 0

    def data(self) -> cbor2.CBORTag:
        return vi.constr(0, [
            vi.output_reference_data(self.claim_ref[0], self.claim_ref[1]),
            self.auditor_did, self.auditor_credential.data(), self.stake_amount, self.evidence_hash,
            self.evidence_uri, self.challenged_at, self.resolution_deadline, list(self.eligible_jurors),
            vi.constr(self.state, list(self.state_fields)), self.claim_token,
        ])

    def cbor(self) -> bytes:
        return vi.serialise_data(self.data())


def decode_challenge_datum(raw: Any) -> ChallengeDatum:
    index, f = _constr(tree(raw), "ChallengeDatum")
    if index != 0 or len(f) != 11:
        raise DecodeError("not a ChallengeDatum (Constr 0 with 11 fields)")
    ref_index, ref = _constr(f[0], "ChallengeDatum.claim_ref")
    if ref_index != 0 or len(ref) != 2:
        raise DecodeError("ChallengeDatum.claim_ref: not an OutputReference")
    state, state_fields = _constr(f[9], "ChallengeDatum.state")
    jurors = f[8]
    if not isinstance(jurors, list):
        raise DecodeError("ChallengeDatum.eligible_jurors: expected a list")
    return ChallengeDatum(
        claim_ref=(_bytes(ref[0], "claim_ref.transaction_id", 32), _int(ref[1], "claim_ref.output_index")),
        auditor_did=_bytes(f[1], "auditor_did"),
        auditor_credential=_m1_credential(f[2], "auditor_credential"),
        stake_amount=_int(f[3], "stake_amount"),
        evidence_hash=_bytes(f[4], "evidence_hash"),
        evidence_uri=_bytes(f[5], "evidence_uri"),
        challenged_at=_int(f[6], "challenged_at"),
        resolution_deadline=_int(f[7], "resolution_deadline"),
        eligible_jurors=tuple(_bytes(j, "eligible_jurors[]") for j in jurors),
        state=state,
        claim_token=_bytes(f[10], "claim_token"),
        state_fields=tuple(state_fields),
    )
