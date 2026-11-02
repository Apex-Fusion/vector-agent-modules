"""Identity-checked Module-6 transactions.

Part 2 §7 of ``docs/IDENTITY_REGISTRY.md`` (agents-marketplace), Module-6 row.
A proposal submission, an amendment, a critique submission and an
endorsement submission need:

- a validity range with both bounds set and ``hi - lo <= 3_600_000`` ms;
- the identity record of the agent in the datum and the ancestor records
  that ``effective`` reads, as reference inputs. The record is Active over
  the whole range ``[lo, hi]``;
- a declared credential in the datum: ``VerificationKey(h)``. ``h`` is the
  hash of the controller key of the record or of a listed Ed25519 key with
  the transaction purpose. ``h`` is a required signer and signs.

Exits and settlements (withdrawals, adoption, rejection, expiry) read no
identity record. ``GovernanceClient`` of the Python SDK builds them.

The contracts in ``contracts/governance-suggestion`` give the shapes:

- ``validators/proposal.ak``: SubmitProposal spends a locked ProposalDatum
  output, mints the proposal token (and the first activity token) and
  updates the activity record of the proposer. AmendProposal spends the
  proposal and can incorporate critiques (IncorporateCritique).
- ``validators/critique.ak``: MintCritiqueToken spends a locked CritiqueDatum
  output, and ``critique_mint`` mints the critique token.
  MintEndorsementToken spends a locked endorsement output. ``critique_mint``
  accepts a mint only when the transaction spends an output at the critique
  validator, so an endorsement gets no token.

A new position needs two transactions: the lock (an output with the datum at
the validator address) and the creation (the validator spends the lock with
the creation redeemer). The lock runs no script, so the client checks every
contract rule before it locks. ``submitted_at`` and ``created_at`` are the
lower bound ``lo`` of the creation transaction. The client plans the creation
range before the lock and writes its ``lo`` in the locked datum. The creation
output keeps the datum with ``submitted_at`` (``created_at``) set to its own
``lo``.

Every time in a datum is POSIX time in milliseconds. A slot becomes POSIX
time with ``SlotClock``.

This module does not import the Python SDK (``vector_agent``). It uses
pycardano and the shared module ``vector_identity``. pycardano reads the
protocol parameters, including the Plutus V3 cost model, from the chain
context when a builder builds. The module contains no protocol parameters.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Optional, Union

import cbor2
from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    Network,
    PlutusV3Script,
    Redeemer,
    ScriptHash,
    Transaction,
    TransactionBuilder,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
    VerificationKeyWitness,
    plutus_script_hash,
)
from pycardano.serialization import NonEmptyOrderedSet, RawCBOR

import vector_identity as vid
from deploy_common import DEPLOYMENT_ORDER, ORACLE_TOKEN_NAME, PARAMS_TOKEN_NAME

# ── Constants ───────────────────────────────────────────────────────────────

#: Titles of the applied validators in the deploy state (scripts/deploy.py).
PROPOSAL_SPEND = "proposal.proposal_spend.spend"
PROPOSAL_MINT = "proposal.proposal_mint.mint"
CRITIQUE_SPEND = "critique.critique_spend.spend"
CRITIQUE_MINT = "critique.critique_mint.mint"
ENDORSEMENT_SPEND = "critique.endorsement_spend.spend"

#: Deploy-state keys (``tx_hashes``) of the reference-script transactions.
#: deploy.py puts each reference script at output 0.
REFERENCE_SCRIPT_KEYS = {
    PROPOSAL_SPEND: "proposal_spend_ref",
    PROPOSAL_MINT: "proposal_mint_ref",
    CRITIQUE_SPEND: "critique_spend_ref",
    CRITIQUE_MINT: "critique_mint_ref",
    ENDORSEMENT_SPEND: "endorsement_spend_ref",
}

#: Lovelace on top of the stake in a proposal, critique or endorsement
#: output. GovernanceClient uses the same amount.
POSITION_DEPOSIT = 2_000_000

#: Lovelace of a new activity record.
ACTIVITY_LOVELACE = 2_000_000

#: Default length of the validity range of an identity-checked transaction:
#: 30 minutes, half of vector_identity.MAX_RANGE_MS.
DEFAULT_RANGE_MS = 1_800_000

#: Default start of the range before the chain tip.
DEFAULT_START_LAG_MS = 60_000

#: The creation transaction must reach the chain at least this long before
#: the end of its range.
SUBMIT_MARGIN_MS = 120_000

#: Environment variable with the acting agent: ``did:vector:<64 hex>`` or the
#: 64-hex identifier.
AGENT_DID_ENV = "VECTOR_AGENT_DID"

#: Proposal parameter names that a ParameterChange proposal can name
#: (``params.is_governable_param`` in the contracts).
GOVERNABLE_PARAMS = frozenset({
    "MIN_CLAIM_STAKE", "MIN_CHALLENGE_WINDOW", "JURY_SIZE", "JURY_FEE_RATE",
    "MIN_SELF_STAKE", "MIN_ENDORSEMENT", "DECAY_RATE",
    "MAX_ENDORSEMENT_MULTIPLIER", "MIN_PROPOSAL_STAKE", "MIN_CRITIQUE_STAKE",
    "MIN_GOVERNANCE_ENDORSEMENT", "MIN_REVIEW_WINDOW", "MAX_REVIEW_WINDOW",
    "MAX_AMENDMENTS", "MAX_ACTIVE_PROPOSALS", "COOLDOWN_BETWEEN_PROPOSALS",
    "PROPOSER_REWARD_SHARE", "CRITIC_REWARD_SHARE", "PROTOCOL_FEE_RATE",
    "MIN_ADOPTION_REWARD", "MAX_ADOPTION_REWARD", "MAX_INCORPORATED_CRITIQUES",
    "MAX_TREASURY_REQUEST", "EMERGENCY_STAKE_MULTIPLIER",
    "EMERGENCY_REVIEW_WINDOW", "TREASURY_BATCH_SIZE", "MIN_TREASURY_BATCHES",
    "ADOPTION_REWARD_BASE", "GAME_ACTIVATION",
})


# ── Errors ──────────────────────────────────────────────────────────────────


class GovernanceClientError(ValueError):
    """The client refuses to build a Module-6 transaction."""


class DeploymentStateError(GovernanceClientError):
    """The deployment state cannot be used with the current contracts."""


class MissingReferenceError(GovernanceClientError):
    """An output that the contracts read is not available."""


class GovernanceDatumError(GovernanceClientError):
    """A value does not decode as the expected Module-6 type."""


# ── Plutus Data ─────────────────────────────────────────────────────────────


def _constr(index: int, fields: Sequence[Any] = ()) -> cbor2.CBORTag:
    return vid.constr(index, list(fields))


def _plain(value: Any) -> Any:
    """A Plutus Data tree with plain Python lists (pycardano decodes lists
    as frozen lists)."""
    if isinstance(value, cbor2.CBORTag):
        return cbor2.CBORTag(value.tag, _plain(value.value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, Mapping):
        return {_plain(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_plain(v) for v in value]
    return value


def _tree(value: Any) -> Any:
    try:
        return _plain(vid.data_from(value))
    except (vid.IdentityDecodeError, ValueError, cbor2.CBORDecodeError) as exc:
        raise GovernanceDatumError(f"not Plutus Data: {exc}") from exc


def _unconstr(value: Any, what: str) -> tuple[int, list]:
    try:
        return vid.unconstr(value)
    except vid.IdentityDecodeError as exc:
        raise GovernanceDatumError(f"{what}: {exc}") from exc


def _fields(value: Any, count: int, what: str, index: int = 0) -> list:
    got, fields = _unconstr(value, what)
    if got != index or len(fields) != count:
        raise GovernanceDatumError(
            f"{what}: expected constructor {index} with {count} fields, "
            f"got constructor {got} with {len(fields)} fields"
        )
    return fields


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GovernanceDatumError(f"{what}: expected an integer")
    return value


def _bytes(value: Any, what: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise GovernanceDatumError(f"{what}: expected a byte string")
    return bytes(value)


def _list(value: Any, what: str) -> list:
    if isinstance(value, (bytes, bytearray, str)) or not isinstance(value, Sequence):
        raise GovernanceDatumError(f"{what}: expected a list")
    return list(value)


def _bool_data(value: bool) -> cbor2.CBORTag:
    """Aiken ``Bool``: False is constructor 0, True is constructor 1."""
    return _constr(1 if value else 0)


def _bool_from(value: Any, what: str) -> bool:
    index, fields = _unconstr(value, what)
    if index not in (0, 1) or fields:
        raise GovernanceDatumError(f"{what}: not a Bool")
    return index == 1


def out_ref_data(ref: TransactionInput) -> cbor2.CBORTag:
    """Plutus V3 ``OutputReference`` of a pycardano ``TransactionInput``."""
    return vid.output_reference_data(bytes(ref.transaction_id), ref.index)


def _out_ref_from(value: Any, what: str) -> TransactionInput:
    tx_id, index = _fields(value, 2, what)
    tx_id = _bytes(tx_id, what)
    if len(tx_id) != 32:
        raise GovernanceDatumError(f"{what}: the transaction id is not 32 bytes")
    return TransactionInput(TransactionId(tx_id), _int(index, what))


def _utf8(value: Union[str, bytes]) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def inline_datum(tree: Any) -> RawCBOR:
    """An inline datum with the Plutus encoding of ``tree`` (byte strings
    longer than 64 bytes in chunks, as the ledger needs)."""
    return RawCBOR(vid.serialise_data(tree))


def _redeemer(tree: Any) -> Redeemer:
    return Redeemer(RawCBOR(vid.serialise_data(tree)))


# ── Module-6 types (lib/governance_suggestion/types.ak) ─────────────────────


@dataclass(frozen=True)
class Credential:
    """Aiken ``Credential``: ``VerificationKey(hash)`` (constructor 0) or
    ``Script(hash)`` (constructor 1)."""

    hash: bytes
    script: bool = False

    @classmethod
    def key(cls, key_hash: bytes) -> "Credential":
        key_hash = bytes(key_hash)
        if len(key_hash) != 28:
            raise GovernanceClientError("a verification key hash is 28 bytes")
        return cls(key_hash)

    def to_data(self) -> cbor2.CBORTag:
        return _constr(1 if self.script else 0, [self.hash])

    @classmethod
    def from_data(cls, value: Any) -> "Credential":
        index, fields = _unconstr(value, "Credential")
        if index not in (0, 1) or len(fields) != 1:
            raise GovernanceDatumError("Credential: not VerificationKey or Script")
        return cls(_bytes(fields[0], "Credential"), index == 1)


class ProposalKind(IntEnum):
    """Constructors of ``ProposalType``."""

    PARAMETER_CHANGE = 0
    TREASURY_SPEND = 1
    PROTOCOL_UPGRADE = 2
    GAME_ACTIVATION = 3
    GENERAL_SUGGESTION = 4


def parameter_change(param_name: Union[str, bytes], current_value: int, proposed_value: int) -> cbor2.CBORTag:
    return _constr(ProposalKind.PARAMETER_CHANGE, [_utf8(param_name), int(current_value), int(proposed_value)])


def treasury_spend(amount: int, recipient_description: Union[str, bytes]) -> cbor2.CBORTag:
    return _constr(ProposalKind.TREASURY_SPEND, [int(amount), _utf8(recipient_description)])


def protocol_upgrade(upgrade_description_hash: bytes) -> cbor2.CBORTag:
    return _constr(ProposalKind.PROTOCOL_UPGRADE, [bytes(upgrade_description_hash)])


def game_activation(game_id: int) -> cbor2.CBORTag:
    return _constr(ProposalKind.GAME_ACTIVATION, [int(game_id)])


def general_suggestion() -> cbor2.CBORTag:
    return _constr(ProposalKind.GENERAL_SUGGESTION)


class CritiqueKind(IntEnum):
    """Constructors of ``CritiqueType``."""

    SUPPORTIVE = 0
    OPPOSING = 1
    AMENDMENT = 2


def supportive() -> cbor2.CBORTag:
    return _constr(CritiqueKind.SUPPORTIVE)


def opposing() -> cbor2.CBORTag:
    return _constr(CritiqueKind.OPPOSING)


def amendment_critique(suggested_change_hash: bytes) -> cbor2.CBORTag:
    """A critique with a suggested change. The proposer can submit it for its
    own proposal (the other critique types need another agent)."""
    return _constr(CritiqueKind.AMENDMENT, [bytes(suggested_change_hash)])


class Priority(IntEnum):
    STANDARD = 0
    EMERGENCY = 1


class ProposalState(IntEnum):
    OPEN = 0
    AMENDED = 1
    ADOPTED = 2
    REJECTED = 3
    EXPIRED = 4
    WITHDRAWN = 5


#: States that carry a hash: Amended { previous_hash }, Adopted { .. } and
#: Rejected { .. }.
_STATES_WITH_HASH = frozenset({ProposalState.AMENDED, ProposalState.ADOPTED, ProposalState.REJECTED})

#: States that allow critiques, endorsements and amendments.
ACTIVE_STATES = frozenset({ProposalState.OPEN, ProposalState.AMENDED})


@dataclass(frozen=True)
class ProposalDatum:
    """``ProposalDatum`` (12 fields)."""

    proposer_did: bytes
    proposer_credential: Credential
    proposal_hash: bytes
    proposal_type: Any
    storage_uri: bytes
    stake_amount: int
    submitted_at: int
    review_window: int
    priority: Priority = Priority.STANDARD
    amendment_count: int = 0
    incorporated_critiques: tuple[TransactionInput, ...] = ()
    state: ProposalState = ProposalState.OPEN
    state_hash: Optional[bytes] = None

    def to_data(self) -> cbor2.CBORTag:
        state_fields = [] if self.state_hash is None else [self.state_hash]
        return _constr(0, [
            self.proposer_did,
            self.proposer_credential.to_data(),
            self.proposal_hash,
            self.proposal_type,
            self.storage_uri,
            self.stake_amount,
            self.submitted_at,
            self.review_window,
            _constr(self.priority),
            self.amendment_count,
            [out_ref_data(r) for r in self.incorporated_critiques],
            _constr(self.state, state_fields),
        ])

    @classmethod
    def from_data(cls, value: Any) -> "ProposalDatum":
        f = _fields(_tree(value), 12, "ProposalDatum")
        priority, priority_fields = _unconstr(f[8], "ProposalPriority")
        state_index, state_fields = _unconstr(f[11], "ProposalState")
        if priority not in (0, 1) or priority_fields:
            raise GovernanceDatumError("ProposalDatum: not a ProposalPriority")
        try:
            state = ProposalState(state_index)
        except ValueError as exc:
            raise GovernanceDatumError("ProposalDatum: not a ProposalState") from exc
        if len(state_fields) != (1 if state in _STATES_WITH_HASH else 0):
            raise GovernanceDatumError("ProposalDatum: malformed ProposalState")
        _unconstr(f[3], "ProposalType")
        return cls(
            proposer_did=_bytes(f[0], "proposer_did"),
            proposer_credential=Credential.from_data(f[1]),
            proposal_hash=_bytes(f[2], "proposal_hash"),
            proposal_type=f[3],
            storage_uri=_bytes(f[4], "storage_uri"),
            stake_amount=_int(f[5], "stake_amount"),
            submitted_at=_int(f[6], "submitted_at"),
            review_window=_int(f[7], "review_window"),
            priority=Priority(priority),
            amendment_count=_int(f[9], "amendment_count"),
            incorporated_critiques=tuple(
                _out_ref_from(r, "incorporated_critiques") for r in _list(f[10], "incorporated_critiques")
            ),
            state=state,
            state_hash=_bytes(state_fields[0], "state") if state_fields else None,
        )


@dataclass(frozen=True)
class ActivityDatum:
    """``ProposerActivityDatum``: the activity record of a proposer."""

    agent_did: bytes
    agent_credential: Credential
    active_proposal_count: int
    last_proposal_slot: int

    def to_data(self) -> cbor2.CBORTag:
        return _constr(0, [
            self.agent_did,
            self.agent_credential.to_data(),
            self.active_proposal_count,
            self.last_proposal_slot,
        ])

    @classmethod
    def from_data(cls, value: Any) -> "ActivityDatum":
        f = _fields(_tree(value), 4, "ProposerActivityDatum")
        return cls(
            agent_did=_bytes(f[0], "agent_did"),
            agent_credential=Credential.from_data(f[1]),
            active_proposal_count=_int(f[2], "active_proposal_count"),
            last_proposal_slot=_int(f[3], "last_proposal_slot"),
        )


@dataclass(frozen=True)
class CritiqueDatum:
    """``CritiqueDatum`` (9 fields)."""

    critic_did: bytes
    critic_credential: Credential
    proposal_ref: TransactionInput
    critique_hash: bytes
    storage_uri: bytes
    critique_type: Any
    stake_amount: int
    submitted_at: int
    incorporated: bool = False

    def to_data(self) -> cbor2.CBORTag:
        return _constr(0, [
            self.critic_did,
            self.critic_credential.to_data(),
            out_ref_data(self.proposal_ref),
            self.critique_hash,
            self.storage_uri,
            self.critique_type,
            self.stake_amount,
            self.submitted_at,
            _bool_data(self.incorporated),
        ])

    @classmethod
    def from_data(cls, value: Any) -> "CritiqueDatum":
        f = _fields(_tree(value), 9, "CritiqueDatum")
        kind, kind_fields = _unconstr(f[5], "CritiqueType")
        if kind not in tuple(CritiqueKind) or len(kind_fields) != (1 if kind == CritiqueKind.AMENDMENT else 0):
            raise GovernanceDatumError("CritiqueDatum: not a CritiqueType")
        return cls(
            critic_did=_bytes(f[0], "critic_did"),
            critic_credential=Credential.from_data(f[1]),
            proposal_ref=_out_ref_from(f[2], "proposal_ref"),
            critique_hash=_bytes(f[3], "critique_hash"),
            storage_uri=_bytes(f[4], "storage_uri"),
            critique_type=f[5],
            stake_amount=_int(f[6], "stake_amount"),
            submitted_at=_int(f[7], "submitted_at"),
            incorporated=_bool_from(f[8], "incorporated"),
        )

    @property
    def is_amendment(self) -> bool:
        return _unconstr(self.critique_type, "CritiqueType")[0] == CritiqueKind.AMENDMENT


@dataclass(frozen=True)
class EndorsementDatum:
    """``GovernanceEndorsementDatum`` (5 fields)."""

    endorser_did: bytes
    endorser_credential: Credential
    proposal_ref: TransactionInput
    stake_amount: int
    created_at: int

    def to_data(self) -> cbor2.CBORTag:
        return _constr(0, [
            self.endorser_did,
            self.endorser_credential.to_data(),
            out_ref_data(self.proposal_ref),
            self.stake_amount,
            self.created_at,
        ])

    @classmethod
    def from_data(cls, value: Any) -> "EndorsementDatum":
        f = _fields(_tree(value), 5, "GovernanceEndorsementDatum")
        return cls(
            endorser_did=_bytes(f[0], "endorser_did"),
            endorser_credential=Credential.from_data(f[1]),
            proposal_ref=_out_ref_from(f[2], "proposal_ref"),
            stake_amount=_int(f[3], "stake_amount"),
            created_at=_int(f[4], "created_at"),
        )


@dataclass(frozen=True)
class GovernanceParams:
    """``GovernanceParams`` (21 fields). Times are POSIX milliseconds."""

    min_proposal_stake: int
    min_critique_stake: int
    min_governance_endorsement: int
    min_review_window: int
    max_review_window: int
    max_amendments: int
    max_active_proposals: int
    proposal_cooldown: int
    proposer_reward_share: int
    critic_reward_share: int
    protocol_fee_rate: int
    min_adoption_reward: int
    max_adoption_reward: int
    max_incorporated_critiques: int
    max_treasury_request: int
    emergency_stake_multiplier: int
    emergency_review_window: int
    param_execution_delay: int
    min_prediction_pool: int
    credibility_pool_low_threshold: int
    credibility_pool_critical_threshold: int

    def to_data(self) -> cbor2.CBORTag:
        return _constr(0, [getattr(self, f.name) for f in dataclasses.fields(self)])

    @classmethod
    def from_data(cls, value: Any) -> "GovernanceParams":
        names = [f.name for f in dataclasses.fields(cls)]
        f = _fields(_tree(value), len(names), "GovernanceParams")
        return cls(*(_int(v, name) for v, name in zip(f, names)))


@dataclass(frozen=True)
class CrossRefs:
    """``GovernanceCrossRefs`` (5 fields): the hashes that the validators
    read at run time. ``endorsement_validator_hash`` (last) is the hash of
    the endorsement spend validator; the critique mint policy finds the
    endorsement inputs of a transaction with it."""

    proposal_validator_hash: bytes
    critique_validator_hash: bytes
    proposal_mint_hash: bytes
    critique_mint_hash: bytes
    endorsement_validator_hash: bytes

    def to_data(self) -> cbor2.CBORTag:
        return _constr(0, [
            self.proposal_validator_hash,
            self.critique_validator_hash,
            self.proposal_mint_hash,
            self.critique_mint_hash,
            self.endorsement_validator_hash,
        ])

    @classmethod
    def from_data(cls, value: Any) -> "CrossRefs":
        """Decode the 5-field datum. A 4-field datum (no
        endorsement_validator_hash) comes from an older deployment and
        raises GovernanceDatumError."""
        f = _fields(_tree(value), 5, "GovernanceCrossRefs")
        return cls(*(_bytes(v, "GovernanceCrossRefs") for v in f))


# Redeemers (ProposalAction, CritiqueAction, GovernanceEndorsementAction).

SUBMIT_PROPOSAL = _constr(0)
SPEND_ACTIVITY = _constr(8)
MINT_CRITIQUE_TOKEN = _constr(0)
INCORPORATE_CRITIQUE = _constr(2)
MINT_ENDORSEMENT_TOKEN = _constr(0)


def amend_proposal_redeemer(new_hash: bytes, new_uri: bytes, incorporated: Sequence[TransactionInput]) -> cbor2.CBORTag:
    """``AmendProposal { new_hash, new_uri, incorporated }``."""
    return _constr(2, [bytes(new_hash), bytes(new_uri), [out_ref_data(r) for r in incorporated]])


# ── Token names (lib/governance_suggestion/utils.ak) ───────────────────────


def proposal_token_name(locked_ref: TransactionInput) -> bytes:
    """``"prop_" ++ blake2b_256(serialise(locked_ref))[0..27]``. The name
    comes from the locked output that SubmitProposal spends."""
    return b"prop_" + vid.hash_data(out_ref_data(locked_ref))[:27]


def critique_token_name(locked_ref: TransactionInput) -> bytes:
    """``"crit_" ++ blake2b_256(serialise(locked_ref))[0..27]``."""
    return b"crit_" + vid.hash_data(out_ref_data(locked_ref))[:27]


def activity_token_name(agent_id: Union[bytes, str]) -> bytes:
    """``"pact_" ++ blake2b_256(agent identifier)[0..27]``."""
    return b"pact_" + vid.blake2b_256(vid.normalize_agent_id(agent_id))[:27]


# ── Time ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlotClock:
    """Slot to POSIX time: ``system_start_ms + slot * slot_length_ms``.
    Plutus scripts see the bounds of the validity range in this time."""

    system_start_ms: int
    slot_length_ms: int

    def __post_init__(self) -> None:
        if self.slot_length_ms <= 0:
            raise GovernanceClientError("the slot length must be positive")

    @classmethod
    def from_genesis(cls, genesis: Any) -> "SlotClock":
        """From pycardano ``GenesisParameters`` (``system_start`` and
        ``slot_length`` in seconds)."""
        return cls(int(genesis.system_start) * 1000, int(genesis.slot_length) * 1000)

    def posix_ms(self, slot: int) -> int:
        return self.system_start_ms + int(slot) * self.slot_length_ms


@dataclass(frozen=True)
class TxWindow:
    """The validity range of a transaction: ``validity_start`` and ``ttl``
    in slots, ``lo`` and ``hi`` in POSIX milliseconds. The range has at most
    ``vector_identity.MAX_RANGE_MS``."""

    clock: SlotClock
    start_slot: int
    ttl_slot: int

    def __post_init__(self) -> None:
        if self.ttl_slot <= self.start_slot:
            raise vid.IdentityError("the range must end after it starts")
        vid.check_window(self.lo, self.hi)

    @property
    def lo(self) -> int:
        return self.clock.posix_ms(self.start_slot)

    @property
    def hi(self) -> int:
        return self.clock.posix_ms(self.ttl_slot)


def window_from_tip(
    clock: SlotClock,
    tip_slot: int,
    length_ms: int = DEFAULT_RANGE_MS,
    lag_ms: int = DEFAULT_START_LAG_MS,
) -> TxWindow:
    """A range that starts ``lag_ms`` before the chain tip and lasts
    ``length_ms``. Refuses a range longer than one hour."""
    start = int(tip_slot) - max(0, lag_ms) // clock.slot_length_ms
    return TxWindow(clock, start, start + length_ms // clock.slot_length_ms)


# ── Keys ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SigningKeyPair:
    """A payment signing key and its verification key (normal or extended
    keys, as the SDK wallet gives them)."""

    signing_key: Any
    verification_key: Any

    @classmethod
    def from_signing_key(cls, signing_key: Any) -> "SigningKeyPair":
        return cls(signing_key, signing_key.to_verification_key())

    @property
    def key_hash(self) -> bytes:
        return bytes(self.verification_key.hash())


# ── Deployment ──────────────────────────────────────────────────────────────


def script_address(script_hash: bytes) -> Address:
    """The enterprise address of a script (no stake part). Vector uses the
    mainnet network tag."""
    return Address(payment_part=ScriptHash(bytes(script_hash)), network=Network.MAINNET)


def identity_address(identity_hash: bytes) -> Address:
    """The identity address IA of the identity registry."""
    return script_address(identity_hash)


@dataclass(frozen=True)
class DeployedScript:
    """An applied validator of the deployment."""

    title: str
    hash: bytes
    script: PlutusV3Script
    reference: Optional[UTxO] = None

    @property
    def address(self) -> Address:
        return script_address(self.hash)

    @property
    def witness(self) -> Union[UTxO, PlutusV3Script]:
        """The reference-script output, else the script itself."""
        return self.reference if self.reference is not None else self.script


def _hash_value(value: Any, name: str, source: str) -> bytes:
    if not value:
        raise DeploymentStateError(f"{source} has no {name}. Run scripts/deploy.py. {DEPLOYMENT_ORDER}")
    try:
        raw = bytes.fromhex(str(value))
    except ValueError as exc:
        raise DeploymentStateError(f"{source}: {name} is not hex") from exc
    if len(raw) != 28:
        raise DeploymentStateError(f"{source}: {name} is not 28 bytes")
    return raw


def identity_hash_from_state(state: Mapping[str, Any], source: str = "the deploy state") -> bytes:
    """``identity_hash`` of a deploy state. A state without it belongs to a
    Module-6 deployment with the legacy agent registry: the client refuses it
    and names the deployment order."""
    value = state.get("identity_hash")
    if not value:
        raise DeploymentStateError(
            f"{source} has no identity_hash. It belongs to a Module-6 deployment "
            "with the legacy agent registry, and the current contracts read "
            "identity records only. Deploy Module-6 again with scripts/deploy.py "
            f"and VECTOR_IDENTITY_HASH. {DEPLOYMENT_ORDER}"
        )
    raw = _hash_value(value, "identity_hash", source)
    if raw == bytes(28):
        raise DeploymentStateError(f"{source}: identity_hash is all zero. {DEPLOYMENT_ORDER}")
    if raw == vid.LEGACY_REGISTRY_HASH:
        raise DeploymentStateError(
            f"{source}: identity_hash is the legacy agent registry hash. {DEPLOYMENT_ORDER}"
        )
    return raw


def _deployed_script(state: Mapping[str, Any], title: str, source: str) -> DeployedScript:
    info = (state.get("validators") or {}).get(title) or {}
    code = info.get("compiled_code")
    if not code:
        raise DeploymentStateError(f"{source} has no compiled code for {title}. Run scripts/deploy.py.")
    script = PlutusV3Script(bytes.fromhex(code))
    script_hash = bytes(plutus_script_hash(script).payload)
    if info.get("hash") and bytes.fromhex(info["hash"]) != script_hash:
        raise DeploymentStateError(f"{source}: the compiled code of {title} does not have the hash {info['hash']}")
    reference = None
    ref_tx = (state.get("tx_hashes") or {}).get(REFERENCE_SCRIPT_KEYS[title])
    if ref_tx:
        reference = UTxO(
            TransactionInput(TransactionId(bytes.fromhex(ref_tx)), 0),
            TransactionOutput(script_address(script_hash), 0, script=script),
        )
    return DeployedScript(title, script_hash, script, reference)


@dataclass(frozen=True)
class GovernanceDeployment:
    """The Module-6 deployment that the identity-checked builders use."""

    identity_hash: bytes
    record_token_policy: bytes
    refs_token_policy: bytes
    params_holder_hash: bytes
    oracle_holder_hash: bytes
    cross_refs_address: Address
    cross_refs_tx: Optional[bytes]
    proposal_spend: DeployedScript
    proposal_mint: DeployedScript
    critique_spend: DeployedScript
    critique_mint: DeployedScript
    endorsement_spend: DeployedScript

    @classmethod
    def from_state(cls, state: Mapping[str, Any], source: str = "the deploy state") -> "GovernanceDeployment":
        """From ``wallets/deploy_state.json`` (scripts/deploy.py)."""
        identity_hash = identity_hash_from_state(state, source)
        holders = state.get("holders") or {}
        tx_hashes = state.get("tx_hashes") or {}
        cross_refs_address = state.get("cross_refs_address")
        if not cross_refs_address:
            raise DeploymentStateError(f"{source} has no cross_refs_address. Run scripts/deploy.py.")
        cross_refs_tx = tx_hashes.get("cross_refs_nft")
        return cls(
            identity_hash=identity_hash,
            record_token_policy=_hash_value(state.get("record_token_policy"), "record_token_policy", source),
            refs_token_policy=_hash_value(state.get("refs_token_policy"), "refs_token_policy", source),
            params_holder_hash=_hash_value((holders.get("params") or {}).get("hash"), "holders.params.hash", source),
            oracle_holder_hash=_hash_value((holders.get("oracle") or {}).get("hash"), "holders.oracle.hash", source),
            cross_refs_address=Address.from_primitive(cross_refs_address),
            cross_refs_tx=bytes.fromhex(cross_refs_tx) if cross_refs_tx else None,
            proposal_spend=_deployed_script(state, PROPOSAL_SPEND, source),
            proposal_mint=_deployed_script(state, PROPOSAL_MINT, source),
            critique_spend=_deployed_script(state, CRITIQUE_SPEND, source),
            critique_mint=_deployed_script(state, CRITIQUE_MINT, source),
            endorsement_spend=_deployed_script(state, ENDORSEMENT_SPEND, source),
        )

    @property
    def identity_address(self) -> Address:
        return identity_address(self.identity_hash)

    @property
    def params_address(self) -> Address:
        return script_address(self.params_holder_hash)

    @property
    def oracle_address(self) -> Address:
        return script_address(self.oracle_holder_hash)

    @property
    def cross_refs(self) -> CrossRefs:
        """The CrossRefs datum that matches this deployment."""
        return CrossRefs(
            self.proposal_spend.hash,
            self.critique_spend.hash,
            self.proposal_mint.hash,
            self.critique_mint.hash,
            self.endorsement_spend.hash,
        )


def load_deployment(path: Union[str, Path]) -> GovernanceDeployment:
    """Read a deploy state file and refuse a state without identity_hash."""
    path = Path(path)
    if not path.exists():
        raise DeploymentStateError(f"{path} does not exist. Run scripts/deploy.py. {DEPLOYMENT_ORDER}")
    return GovernanceDeployment.from_state(json.loads(path.read_text()), str(path))


def agent_id_from(value: Optional[str] = None, environ: Mapping[str, str] = os.environ) -> bytes:
    """The agent identifier from ``value`` (an argument) or from
    ``VECTOR_AGENT_DID``: ``did:vector:<64 hex>`` or 64 hex characters."""
    text = (value or environ.get(AGENT_DID_ENV, "")).strip()
    if not text:
        raise GovernanceClientError(
            f"no agent identifier. Set {AGENT_DID_ENV} to did:vector:<64 hex> or "
            "to the 64-hex identifier of an identity record, or pass it as an argument."
        )
    try:
        return vid.normalize_agent_id(text)
    except (TypeError, ValueError) as exc:
        raise GovernanceClientError(f"{text!r} is not a did:vector DID or a 64-hex identifier") from exc


# ── Records on chain ────────────────────────────────────────────────────────


def _payment_script(utxo: UTxO) -> Optional[bytes]:
    part = utxo.output.address.payment_part
    return bytes(part.payload) if isinstance(part, ScriptHash) else None


def _multi_asset(utxo_or_output: Any) -> Mapping:
    output = getattr(utxo_or_output, "output", utxo_or_output)
    amount = output.amount
    return {} if isinstance(amount, int) else (amount.multi_asset or {})


def _coin(utxo: UTxO) -> int:
    amount = utxo.output.amount
    return amount if isinstance(amount, int) else amount.coin


def policy_tokens(utxo_or_output: Any, policy: bytes) -> dict[bytes, int]:
    """The tokens of ``policy`` in an output: {asset name: quantity}."""
    tokens: dict[bytes, int] = {}
    for pid, assets in _multi_asset(utxo_or_output).items():
        if bytes(pid.payload) == bytes(policy):
            for name, quantity in assets.items():
                tokens[bytes(name.payload)] = tokens.get(bytes(name.payload), 0) + int(quantity)
    return {k: v for k, v in tokens.items() if v != 0}


def _lovelace_only(utxo: UTxO) -> bool:
    return not any(q for assets in _multi_asset(utxo).values() for q in assets.values())


def _value_key(utxo: UTxO) -> tuple:
    """The value of an output as a comparable tuple."""
    return (_coin(utxo), tuple(sorted(
        (bytes(pid.payload), bytes(name.payload), int(quantity))
        for pid, assets in _multi_asset(utxo).items()
        for name, quantity in assets.items()
    )))


def _is_record(utxo: UTxO, holder_hash: bytes, policy: bytes, name: bytes) -> bool:
    """``record_auth.is_record``: at the holder script with exactly one record
    token, and an inline datum (``find_record`` reads only inline datums)."""
    return (
        _payment_script(utxo) == bytes(holder_hash)
        and policy_tokens(utxo, policy).get(bytes(name), 0) == 1
        and utxo.output.datum is not None
    )


@dataclass(frozen=True)
class ParamsRecord:
    utxo: UTxO
    params: GovernanceParams


@dataclass(frozen=True)
class CrossRefsRecord:
    utxo: UTxO
    refs: CrossRefs


@dataclass(frozen=True)
class GovernanceReferences:
    """The reference inputs that every Module-6 validator reads: the params
    record (by its ``params`` record token) and the CrossRefs output."""

    params: ParamsRecord
    cross_refs: CrossRefsRecord


@dataclass(frozen=True)
class ActivityRecord:
    utxo: UTxO
    datum: ActivityDatum


@dataclass(frozen=True)
class ProposalRecord:
    utxo: UTxO
    datum: ProposalDatum


def find_params(utxos: Iterable[UTxO], deployment: GovernanceDeployment) -> ParamsRecord:
    """The params record: the output at the params holder with the
    ``params`` record token. Refuses when it is missing."""
    for utxo in utxos:
        if _is_record(utxo, deployment.params_holder_hash, deployment.record_token_policy, PARAMS_TOKEN_NAME):
            return ParamsRecord(utxo, GovernanceParams.from_data(utxo.output.datum))
    raise MissingReferenceError(
        f"no params record at {deployment.params_address}: no output there holds the "
        f"record token {deployment.record_token_policy.hex()}.{PARAMS_TOKEN_NAME.decode()}. "
        "The validators read GovernanceParams only from this record."
    )


def find_oracle(utxos: Iterable[UTxO], deployment: GovernanceDeployment) -> UTxO:
    """The oracle record: the output at the oracle holder with the ``oracle``
    record token. Oracle actions (adopt, reject, extend) read it."""
    for utxo in utxos:
        if _is_record(utxo, deployment.oracle_holder_hash, deployment.record_token_policy, ORACLE_TOKEN_NAME):
            return utxo
    raise MissingReferenceError(
        f"no oracle record at {deployment.oracle_address}: no output there holds the "
        f"record token {deployment.record_token_policy.hex()}.{ORACLE_TOKEN_NAME.decode()}."
    )


def find_cross_refs(utxos: Iterable[UTxO], deployment: GovernanceDeployment) -> CrossRefsRecord:
    """The CrossRefs output of this deployment: it holds a token of
    ``refs_token_policy`` and its 5-field datum names the validators of the
    deployment (a 4-field datum of an older deployment never matches). The
    output of the deploy transaction comes first."""
    matches = []
    for utxo in utxos:
        if not policy_tokens(utxo, deployment.refs_token_policy) or utxo.output.datum is None:
            continue
        try:
            refs = CrossRefs.from_data(utxo.output.datum)
        except GovernanceDatumError:
            continue
        if refs == deployment.cross_refs:
            matches.append(CrossRefsRecord(utxo, refs))
    if not matches:
        expected = deployment.cross_refs
        raise MissingReferenceError(
            f"no CrossRefs output for this deployment at {deployment.cross_refs_address}: "
            f"no output with a {deployment.refs_token_policy.hex()} token names "
            f"proposal_spend {expected.proposal_validator_hash.hex()}, "
            f"critique_spend {expected.critique_validator_hash.hex()}, "
            f"proposal_mint {expected.proposal_mint_hash.hex()}, "
            f"critique_mint {expected.critique_mint_hash.hex()} and "
            f"endorsement_spend {expected.endorsement_validator_hash.hex()} "
            f"(5-field GovernanceCrossRefs)."
        )
    matches.sort(key=lambda m: bytes(m.utxo.input.transaction_id) != deployment.cross_refs_tx)
    return matches[0]


def identity_records(
    utxos: Iterable[UTxO], deployment: GovernanceDeployment, agent_id: Union[bytes, str]
) -> list[vid.IdentityRecord]:
    """The authentic record of ``agent_id`` and the ancestor records that
    ``effective`` reads (``vector_identity.find_record`` and
    ``record_chain``), from the UTxOs at the identity address."""
    utxos = list(utxos)
    return vid.record_chain(agent_id, lambda aid: vid.find_record(utxos, deployment.identity_hash, aid))


def find_activity(
    utxos: Iterable[UTxO], deployment: GovernanceDeployment, agent_id: Union[bytes, str]
) -> Optional[ActivityRecord]:
    """The activity record of ``agent_id`` at the proposal validator: it holds
    exactly one activity token of the agent. None before the first
    proposal."""
    agent_id = vid.normalize_agent_id(agent_id)
    name = activity_token_name(agent_id)
    found = []
    for utxo in utxos:
        if _payment_script(utxo) != deployment.proposal_spend.hash:
            continue
        if policy_tokens(utxo, deployment.proposal_mint.hash).get(name, 0) != 1:
            continue
        datum = ActivityDatum.from_data(utxo.output.datum)
        if datum.agent_did == agent_id:
            found.append(ActivityRecord(utxo, datum))
    if len(found) > 1:
        refs = ", ".join(f"{u.utxo.input.transaction_id}#{u.utxo.input.index}" for u in found)
        raise GovernanceClientError(f"{vid.did_from_id(agent_id)} has several activity records: {refs}")
    return found[0] if found else None


def find_utxo(utxos: Iterable[UTxO], ref: TransactionInput) -> Optional[UTxO]:
    return next((u for u in utxos if u.input == ref), None)


def proposal_token(utxo: UTxO, deployment: GovernanceDeployment) -> Optional[bytes]:
    """The proposal token (``prop_`` name, quantity 1) of a submitted
    proposal, or None."""
    names = [
        name for name, quantity in policy_tokens(utxo, deployment.proposal_mint.hash).items()
        if name.startswith(b"prop_") and quantity == 1
    ]
    return names[0] if len(names) == 1 else None


# ── Checks ──────────────────────────────────────────────────────────────────


def _position_datum(utxo: UTxO, script: DeployedScript, decode: Callable[[Any], Any], what: str) -> Any:
    if _payment_script(utxo) != script.hash:
        raise GovernanceClientError(f"{utxo.input} is not at the {what} validator {script.hash.hex()}")
    if utxo.output.datum is None:
        raise GovernanceDatumError(f"{utxo.input} has no inline datum")
    return decode(utxo.output.datum)


def _agent_of(records: Sequence[vid.IdentityRecord]) -> bytes:
    """The agent of a record chain: the agent of its first record."""
    if not records:
        raise GovernanceClientError(
            "no identity record: pass the record of the agent and its ancestors (identity_records)"
        )
    return records[0].agent_id


def _checked_chain(
    deployment: GovernanceDeployment,
    records: Sequence[vid.IdentityRecord],
    agent_id: bytes,
    window: TxWindow,
) -> list[vid.IdentityRecord]:
    """The record of ``agent_id`` and its ancestors from ``records``, each an
    authentic output at the identity address of the deployment. Refuses a
    missing record or ancestor and a record that is not Active over the
    whole range."""
    by_id: dict[bytes, vid.IdentityRecord] = {}
    for record in records:
        authentic = None if record.utxo is None else vid.record_from_utxo(record.utxo, deployment.identity_hash)
        if authentic is None or authentic.agent_id != record.agent_id:
            raise vid.IdentityError(
                f"{record.did} is not an authentic record at the identity address "
                f"{deployment.identity_address}"
            )
        by_id[authentic.agent_id] = authentic
    chain = vid.record_chain(agent_id, by_id.get)
    vid.require_active(chain, window.lo, window.hi)
    return chain


def _declared_key(chain: Sequence[vid.IdentityRecord], credential: Credential) -> bytes:
    """The key hash of a declared credential of ``chain[0]``."""
    if credential.script:
        raise vid.IdentityError(
            f"the credential is a script credential. {chain[0].did} needs a verification-key "
            "credential: its controller key or a listed key with the transaction purpose"
        )
    vid.require_declared_credential(chain[0], credential.hash)
    return credential.hash


def _check_references(deployment: GovernanceDeployment, refs: GovernanceReferences) -> None:
    params = refs.params.utxo
    if not _is_record(params, deployment.params_holder_hash, deployment.record_token_policy, PARAMS_TOKEN_NAME):
        raise MissingReferenceError(f"{params.input} is not the params record of this deployment")
    if refs.cross_refs.refs != deployment.cross_refs or not policy_tokens(
        refs.cross_refs.utxo, deployment.refs_token_policy
    ):
        raise MissingReferenceError(f"{refs.cross_refs.utxo.input} is not the CrossRefs output of this deployment")


def _proposal_type_problem(proposal_type: Any, params: GovernanceParams) -> Optional[str]:
    """``validate_proposal_type`` of the contract."""
    kind, fields = _unconstr(proposal_type, "ProposalType")
    if kind == ProposalKind.PARAMETER_CHANGE and len(fields) == 3:
        name = bytes(fields[0]).decode("utf-8", "replace") if isinstance(fields[0], bytes) else ""
        return None if name in GOVERNABLE_PARAMS else f"{name!r} is not a governable parameter"
    if kind == ProposalKind.TREASURY_SPEND and len(fields) == 2:
        amount = fields[0]
        ok = isinstance(amount, int) and 0 < amount <= params.max_treasury_request
        return None if ok else f"the treasury amount must be in (0, {params.max_treasury_request}]"
    if kind == ProposalKind.PROTOCOL_UPGRADE and len(fields) == 1:
        ok = isinstance(fields[0], bytes) and len(fields[0]) == 32
        return None if ok else "the upgrade description hash is not 32 bytes"
    if kind == ProposalKind.GAME_ACTIVATION and len(fields) == 1:
        return None if isinstance(fields[0], int) and fields[0] > 0 else "the game id must be positive"
    if kind == ProposalKind.GENERAL_SUGGESTION and not fields:
        return None
    return "not a ProposalType"


def check_proposal_rules(datum: ProposalDatum, params: GovernanceParams) -> None:
    """The submission rules of ``validate_submit_proposal`` that read only
    the datum and the parameters (stake, hash, type, URI, review window,
    initial state)."""
    problems = []
    if len(datum.proposal_hash) != 32:
        problems.append("proposal_hash is not 32 bytes")
    if not datum.storage_uri:
        problems.append("storage_uri is empty")
    type_problem = _proposal_type_problem(datum.proposal_type, params)
    if type_problem:
        problems.append(type_problem)
    if datum.priority == Priority.STANDARD:
        if datum.stake_amount < params.min_proposal_stake:
            problems.append(f"the stake is below {params.min_proposal_stake} lovelace")
        if not params.min_review_window <= datum.review_window <= params.max_review_window:
            problems.append(
                f"the review window must be in [{params.min_review_window}, {params.max_review_window}] ms"
            )
    else:
        kind = _unconstr(datum.proposal_type, "ProposalType")[0]
        if kind not in (ProposalKind.PARAMETER_CHANGE, ProposalKind.PROTOCOL_UPGRADE):
            problems.append("an emergency proposal must be a ParameterChange or a ProtocolUpgrade")
        emergency_stake = params.min_proposal_stake * params.emergency_stake_multiplier // 1000
        if datum.stake_amount < emergency_stake:
            problems.append(f"the stake of an emergency proposal is below {emergency_stake} lovelace")
        if datum.review_window != params.emergency_review_window:
            problems.append(f"the review window of an emergency proposal must be {params.emergency_review_window} ms")
    if datum.state != ProposalState.OPEN or datum.state_hash is not None:
        problems.append("a new proposal must be Open")
    if datum.amendment_count != 0 or datum.incorporated_critiques:
        problems.append("a new proposal has no amendments and no incorporated critiques")
    if problems:
        raise GovernanceClientError("the proposal does not meet the contract rules: " + "; ".join(problems))


def check_rate_limit(
    activity: Optional[ActivityRecord], agent_id: bytes, params: GovernanceParams, lo: int
) -> None:
    """``validate_submit_rate_limit``: the number of active proposals and the
    cooldown since the last proposal (both from the activity record)."""
    if activity is None:
        return
    datum = activity.datum
    if datum.agent_did != agent_id:
        raise GovernanceClientError(f"{activity.utxo.input} is the activity record of another agent")
    if datum.active_proposal_count >= params.max_active_proposals:
        raise GovernanceClientError(
            f"{vid.did_from_id(agent_id)} has {datum.active_proposal_count} active proposals; "
            f"the limit is {params.max_active_proposals}"
        )
    allowed = datum.last_proposal_slot + params.proposal_cooldown
    if lo < allowed:
        raise GovernanceClientError(
            f"the proposal cooldown of {vid.did_from_id(agent_id)} ends at {allowed} (POSIX ms); "
            f"the range starts at {lo}"
        )


def _proposal_target(
    proposal: UTxO, deployment: GovernanceDeployment, ref: Optional[TransactionInput] = None
) -> ProposalDatum:
    """An active, submitted proposal that a critique or an endorsement
    names."""
    if ref is not None and proposal.input != ref:
        raise GovernanceClientError(f"the proposal reference input {proposal.input} is not {ref}")
    datum = _position_datum(proposal, deployment.proposal_spend, ProposalDatum.from_data, "proposal")
    if datum.state not in ACTIVE_STATES:
        raise GovernanceClientError(f"the proposal {proposal.input} is {datum.state.name}, not Open or Amended")
    if proposal_token(proposal, deployment) is None:
        raise GovernanceClientError(
            f"{proposal.input} holds no proposal token: it is a lock, not a submitted proposal. "
            "A position on it can never settle."
        )
    return datum


def check_critique_rules(datum: CritiqueDatum, proposal: ProposalDatum, params: GovernanceParams) -> None:
    """``validate_submit_critique`` rules besides identity and signature."""
    problems = []
    if datum.stake_amount < params.min_critique_stake:
        problems.append(f"the stake is below {params.min_critique_stake} lovelace")
    if len(datum.critique_hash) != 32:
        problems.append("critique_hash is not 32 bytes")
    if datum.incorporated:
        problems.append("a new critique is not incorporated")
    if datum.critic_did == proposal.proposer_did and not datum.is_amendment:
        problems.append("the proposer can only submit an Amendment critique of its own proposal")
    if problems:
        raise GovernanceClientError("the critique does not meet the contract rules: " + "; ".join(problems))


def check_endorsement_rules(datum: EndorsementDatum, proposal: ProposalDatum, params: GovernanceParams) -> None:
    """``validate_submit_endorsement`` rules besides identity and
    signature."""
    problems = []
    if datum.stake_amount < params.min_governance_endorsement:
        problems.append(f"the stake is below {params.min_governance_endorsement} lovelace")
    if datum.endorser_did == proposal.proposer_did:
        problems.append("an agent cannot endorse its own proposal")
    if problems:
        raise GovernanceClientError("the endorsement does not meet the contract rules: " + "; ".join(problems))


# ── Builders ────────────────────────────────────────────────────────────────


@dataclass
class PreparedTx:
    """A transaction builder with its inputs, outputs, mint, reference inputs,
    range and required signers. ``sign_prepared`` balances and signs it.

    ``position_index`` is the output index of the new or changed position
    (proposal, critique or endorsement); ``datum`` is its datum.
    """

    builder: TransactionBuilder
    required_signers: tuple[bytes, ...]
    datum: Any
    position_index: int = 0
    activity_index: Optional[int] = None
    minted: tuple[bytes, ...] = ()


def _identity_builder(
    context: Any,
    deployment: GovernanceDeployment,
    chain: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    window: TxWindow,
    signer: bytes,
    wallet_address: Address,
    extra_references: Sequence[UTxO] = (),
) -> TransactionBuilder:
    """A builder with the identity record and its ancestors, the params record
    and CrossRefs as reference inputs, the validity range and the declared
    key as required signer. The caller checks ``refs`` first
    (``_check_references``)."""
    builder = TransactionBuilder(context)
    builder.add_input_address(wallet_address)
    for record in chain:
        builder.reference_inputs.add(record.utxo)
    builder.reference_inputs.add(refs.params.utxo)
    builder.reference_inputs.add(refs.cross_refs.utxo)
    for utxo in extra_references:
        builder.reference_inputs.add(utxo)
    builder.validity_start = window.start_slot
    builder.ttl = window.ttl_slot
    builder.required_signers = [VerificationKeyHash(signer)]
    return builder


def _lock_builder(context: Any, window: TxWindow, wallet_address: Address, output: TransactionOutput) -> TransactionBuilder:
    """The lock: one output at a validator, paid by the wallet. Its ``ttl`` is
    the end of the creation range, so a late lock does not reach the chain."""
    builder = TransactionBuilder(context)
    builder.add_input_address(wallet_address)
    builder.add_output(output)
    builder.validity_start = window.start_slot
    builder.ttl = window.ttl_slot
    return builder


def _assets(policy: bytes, names: Mapping[bytes, int]) -> MultiAsset:
    return MultiAsset({ScriptHash(bytes(policy)): Asset({AssetName(n): q for n, q in names.items()})})


@dataclass(frozen=True)
class ProposalTerms:
    """The fields of a new proposal that the proposer chooses."""

    proposal_hash: bytes
    storage_uri: Union[str, bytes]
    stake_amount: int
    review_window: int
    proposal_type: Any = dataclasses.field(default_factory=general_suggestion)
    priority: Priority = Priority.STANDARD


def prepare_proposal_lock(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    activity: Optional[ActivityRecord],
    terms: ProposalTerms,
    credential_key_hash: bytes,
    window: TxWindow,
    wallet_address: Address,
) -> PreparedTx:
    """Step 1 of a proposal submission: lock the ProposalDatum at the proposal
    validator. ``window`` is the range of step 2; ``submitted_at`` is its
    ``lo``. The client checks every rule of SubmitProposal first: the
    identity record is Active over ``window``, the credential is declared,
    and the stake, review window, type and rate limit are valid."""
    agent_id = _agent_of(records)
    chain = _checked_chain(deployment, records, agent_id, window)
    credential = Credential.key(credential_key_hash)
    _declared_key(chain, credential)
    datum = ProposalDatum(
        proposer_did=agent_id,
        proposer_credential=credential,
        proposal_hash=bytes(terms.proposal_hash),
        proposal_type=_plain(terms.proposal_type),
        storage_uri=_utf8(terms.storage_uri),
        stake_amount=int(terms.stake_amount),
        submitted_at=window.lo,
        review_window=int(terms.review_window),
        priority=Priority(terms.priority),
    )
    _check_references(deployment, refs)
    check_proposal_rules(datum, refs.params.params)
    check_rate_limit(activity, agent_id, refs.params.params, window.lo)
    output = TransactionOutput(
        deployment.proposal_spend.address,
        Value(datum.stake_amount + POSITION_DEPOSIT),
        datum=inline_datum(datum.to_data()),
    )
    return PreparedTx(_lock_builder(context, window, wallet_address, output), (), datum)


def prepare_submit_proposal(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    locked: UTxO,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    activity: Optional[ActivityRecord],
    window: TxWindow,
    wallet_address: Address,
) -> PreparedTx:
    """Step 2 of a proposal submission (SubmitProposal).

    - Spends the locked proposal with SubmitProposal, and the activity record
      of the proposer (if it exists) with SpendActivity.
    - Mints the proposal token (name from the locked output) and, for the
      first proposal of the agent, the activity token (proposal_mint,
      redeemer SubmitProposal).
    - Output 0: the proposal with the token and ``submitted_at = lo``.
      Output 1: the activity record with the count plus one and
      ``last_proposal_slot = lo``.
    """
    datum = _position_datum(locked, deployment.proposal_spend, ProposalDatum.from_data, "proposal")
    if not _lovelace_only(locked):
        raise GovernanceClientError(f"{locked.input} holds tokens: it is not a proposal lock")
    agent_id = datum.proposer_did
    chain = _checked_chain(deployment, records, agent_id, window)
    signer = _declared_key(chain, datum.proposer_credential)
    datum = dataclasses.replace(datum, submitted_at=window.lo)
    _check_references(deployment, refs)
    params = refs.params.params
    check_proposal_rules(datum, params)
    check_rate_limit(activity, agent_id, params, window.lo)

    builder = _identity_builder(context, deployment, chain, refs, window, signer, wallet_address)
    token = proposal_token_name(locked.input)
    minted = {token: 1}
    builder.add_script_input(locked, script=deployment.proposal_spend.witness, redeemer=_redeemer(SUBMIT_PROPOSAL))
    builder.add_output(TransactionOutput(
        deployment.proposal_spend.address,
        Value(_coin(locked), _assets(deployment.proposal_mint.hash, {token: 1})),
        datum=inline_datum(datum.to_data()),
    ))

    activity_name = activity_token_name(agent_id)
    if activity is None:
        minted[activity_name] = 1
        activity_datum = ActivityDatum(agent_id, Credential.key(signer), 1, window.lo)
        activity_value = Value(ACTIVITY_LOVELACE, _assets(deployment.proposal_mint.hash, {activity_name: 1}))
        activity_address = deployment.proposal_spend.address
    else:
        if _payment_script(activity.utxo) != deployment.proposal_spend.hash or policy_tokens(
            activity.utxo, deployment.proposal_mint.hash
        ).get(activity_name, 0) != 1:
            raise GovernanceClientError(f"{activity.utxo.input} is not the activity record of {vid.did_from_id(agent_id)}")
        builder.add_script_input(
            activity.utxo, script=deployment.proposal_spend.witness, redeemer=_redeemer(SPEND_ACTIVITY)
        )
        activity_datum = dataclasses.replace(
            activity.datum,
            active_proposal_count=activity.datum.active_proposal_count + 1,
            last_proposal_slot=window.lo,
        )
        activity_value = activity.utxo.output.amount
        activity_address = activity.utxo.output.address
    builder.add_output(TransactionOutput(activity_address, activity_value, datum=inline_datum(activity_datum.to_data())))

    builder.mint = _assets(deployment.proposal_mint.hash, minted)
    builder.add_minting_script(deployment.proposal_mint.witness, redeemer=_redeemer(SUBMIT_PROPOSAL))
    return PreparedTx(builder, (signer,), datum, 0, 1, tuple(minted))


def prepare_amend_proposal(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    proposal: UTxO,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    new_hash: bytes,
    new_uri: Union[str, bytes],
    window: TxWindow,
    wallet_address: Address,
    incorporate: Sequence[UTxO] = (),
) -> PreparedTx:
    """AmendProposal by the proposer.

    - Spends the proposal with ``AmendProposal { new_hash, new_uri,
      incorporated }`` and each incorporated critique with
      IncorporateCritique.
    - Output 0: the proposal (same address and value) in state Amended with
      the new hash and URI, one more amendment and the incorporated
      critiques appended. Then one output per critique: same address, same
      value, ``incorporated = True``.
    """
    datum = _position_datum(proposal, deployment.proposal_spend, ProposalDatum.from_data, "proposal")
    if proposal_token(proposal, deployment) is None:
        raise GovernanceClientError(f"{proposal.input} holds no proposal token: it is not a submitted proposal")
    chain = _checked_chain(deployment, records, datum.proposer_did, window)
    signer = _declared_key(chain, datum.proposer_credential)
    _check_references(deployment, refs)
    params = refs.params.params
    new_hash = bytes(new_hash)
    new_uri = _utf8(new_uri)

    critiques: list[tuple[UTxO, CritiqueDatum]] = []
    for utxo in incorporate:
        critique = _position_datum(utxo, deployment.critique_spend, CritiqueDatum.from_data, "critique")
        if critique.incorporated:
            raise GovernanceClientError(f"the critique {utxo.input} is already incorporated")
        if critique.proposal_ref != proposal.input:
            raise GovernanceClientError(f"the critique {utxo.input} is about another proposal")
        critiques.append((utxo, critique))
    refs_in = [u.input for u, _ in critiques]
    problems = []
    if datum.state not in ACTIVE_STATES:
        problems.append(f"the proposal is {datum.state.name}, not Open or Amended")
    if len(new_hash) != 32 or new_hash == datum.proposal_hash:
        problems.append("the new hash must be 32 bytes and differ from the current hash")
    if not new_uri:
        problems.append("the new URI is empty")
    if datum.amendment_count + 1 > params.max_amendments:
        problems.append(f"the proposal already has {datum.amendment_count} of {params.max_amendments} amendments")
    if len(datum.incorporated_critiques) + len(critiques) > params.max_incorporated_critiques:
        problems.append(f"more than {params.max_incorporated_critiques} incorporated critiques")
    if len(set(refs_in)) != len(refs_in):
        problems.append("a critique is listed twice")
    shapes = [(vid.serialise_data(c.to_data()), _value_key(u)) for u, c in critiques]
    if len(set(shapes)) != len(shapes):
        problems.append("two critiques with the same datum and value cannot be incorporated in one transaction")
    if problems:
        raise GovernanceClientError("the amendment does not meet the contract rules: " + "; ".join(problems))

    amended = dataclasses.replace(
        datum,
        proposal_hash=new_hash,
        storage_uri=new_uri,
        amendment_count=datum.amendment_count + 1,
        incorporated_critiques=datum.incorporated_critiques + tuple(refs_in),
        state=ProposalState.AMENDED,
        state_hash=datum.proposal_hash,
    )
    builder = _identity_builder(context, deployment, chain, refs, window, signer, wallet_address)
    builder.add_script_input(
        proposal,
        script=deployment.proposal_spend.witness,
        redeemer=_redeemer(amend_proposal_redeemer(new_hash, new_uri, refs_in)),
    )
    builder.add_output(TransactionOutput(proposal.output.address, proposal.output.amount, datum=inline_datum(amended.to_data())))
    for utxo, critique in critiques:
        builder.add_script_input(utxo, script=deployment.critique_spend.witness, redeemer=_redeemer(INCORPORATE_CRITIQUE))
        incorporated = dataclasses.replace(critique, incorporated=True)
        builder.add_output(TransactionOutput(utxo.output.address, utxo.output.amount, datum=inline_datum(incorporated.to_data())))
    return PreparedTx(builder, (signer,), amended)


@dataclass(frozen=True)
class CritiqueTerms:
    """The fields of a new critique that the critic chooses."""

    critique_hash: bytes
    storage_uri: Union[str, bytes]
    stake_amount: int
    critique_type: Any = dataclasses.field(default_factory=supportive)


def prepare_critique_lock(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    proposal: UTxO,
    terms: CritiqueTerms,
    credential_key_hash: bytes,
    window: TxWindow,
    wallet_address: Address,
) -> PreparedTx:
    """Step 1 of a critique: lock the CritiqueDatum at the critique
    validator, with ``submitted_at = window.lo`` (the range of step 2). The
    client checks the rules of MintCritiqueToken first."""
    agent_id = _agent_of(records)
    chain = _checked_chain(deployment, records, agent_id, window)
    credential = Credential.key(credential_key_hash)
    _declared_key(chain, credential)
    target = _proposal_target(proposal, deployment)
    datum = CritiqueDatum(
        critic_did=agent_id,
        critic_credential=credential,
        proposal_ref=proposal.input,
        critique_hash=bytes(terms.critique_hash),
        storage_uri=_utf8(terms.storage_uri),
        critique_type=_plain(terms.critique_type),
        stake_amount=int(terms.stake_amount),
        submitted_at=window.lo,
    )
    _check_references(deployment, refs)
    check_critique_rules(datum, target, refs.params.params)
    output = TransactionOutput(
        deployment.critique_spend.address,
        Value(datum.stake_amount + POSITION_DEPOSIT),
        datum=inline_datum(datum.to_data()),
    )
    return PreparedTx(_lock_builder(context, window, wallet_address, output), (), datum)


def prepare_submit_critique(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    locked: UTxO,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    proposal: UTxO,
    window: TxWindow,
    wallet_address: Address,
) -> PreparedTx:
    """Step 2 of a critique (MintCritiqueToken).

    - Spends the locked critique with MintCritiqueToken.
    - Mints the critique token (name from the locked output) through
      critique_mint.
    - Reference inputs: the record chain, the params record, CrossRefs and
      the proposal.
    - Output 0: the critique with the token and ``submitted_at = lo``.
    """
    datum = _position_datum(locked, deployment.critique_spend, CritiqueDatum.from_data, "critique")
    if not _lovelace_only(locked):
        raise GovernanceClientError(f"{locked.input} holds tokens: it is not a critique lock")
    chain = _checked_chain(deployment, records, datum.critic_did, window)
    signer = _declared_key(chain, datum.critic_credential)
    target = _proposal_target(proposal, deployment, datum.proposal_ref)
    datum = dataclasses.replace(datum, submitted_at=window.lo)
    _check_references(deployment, refs)
    check_critique_rules(datum, target, refs.params.params)

    builder = _identity_builder(context, deployment, chain, refs, window, signer, wallet_address, [proposal])
    token = critique_token_name(locked.input)
    builder.add_script_input(locked, script=deployment.critique_spend.witness, redeemer=_redeemer(MINT_CRITIQUE_TOKEN))
    builder.add_output(TransactionOutput(
        deployment.critique_spend.address,
        Value(_coin(locked), _assets(deployment.critique_mint.hash, {token: 1})),
        datum=inline_datum(datum.to_data()),
    ))
    builder.mint = _assets(deployment.critique_mint.hash, {token: 1})
    builder.add_minting_script(deployment.critique_mint.witness, redeemer=_redeemer(MINT_CRITIQUE_TOKEN))
    return PreparedTx(builder, (signer,), datum, minted=(token,))


def prepare_endorsement_lock(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    proposal: UTxO,
    stake_amount: int,
    credential_key_hash: bytes,
    window: TxWindow,
    wallet_address: Address,
) -> PreparedTx:
    """Step 1 of an endorsement: lock the endorsement datum at the
    endorsement validator, with ``created_at = window.lo`` (the range of
    step 2). The client checks the rules of MintEndorsementToken first."""
    agent_id = _agent_of(records)
    chain = _checked_chain(deployment, records, agent_id, window)
    credential = Credential.key(credential_key_hash)
    _declared_key(chain, credential)
    target = _proposal_target(proposal, deployment)
    datum = EndorsementDatum(agent_id, credential, proposal.input, int(stake_amount), window.lo)
    _check_references(deployment, refs)
    check_endorsement_rules(datum, target, refs.params.params)
    output = TransactionOutput(
        deployment.endorsement_spend.address,
        Value(datum.stake_amount + POSITION_DEPOSIT),
        datum=inline_datum(datum.to_data()),
    )
    return PreparedTx(_lock_builder(context, window, wallet_address, output), (), datum)


def prepare_submit_endorsement(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    locked: UTxO,
    records: Sequence[vid.IdentityRecord],
    refs: GovernanceReferences,
    proposal: UTxO,
    window: TxWindow,
    wallet_address: Address,
) -> PreparedTx:
    """Step 2 of an endorsement (MintEndorsementToken).

    - Spends the locked endorsement with MintEndorsementToken.
    - No mint: critique_mint needs a spent critique output, so an
      endorsement has no token.
    - Reference inputs: the record chain, the params record, CrossRefs and
      the proposal.
    - Output 0: the endorsement (same value) with ``created_at = lo``.
    """
    datum = _position_datum(locked, deployment.endorsement_spend, EndorsementDatum.from_data, "endorsement")
    if not _lovelace_only(locked):
        raise GovernanceClientError(f"{locked.input} holds tokens: it is not an endorsement lock")
    chain = _checked_chain(deployment, records, datum.endorser_did, window)
    signer = _declared_key(chain, datum.endorser_credential)
    target = _proposal_target(proposal, deployment, datum.proposal_ref)
    datum = dataclasses.replace(datum, created_at=window.lo)
    _check_references(deployment, refs)
    check_endorsement_rules(datum, target, refs.params.params)

    builder = _identity_builder(context, deployment, chain, refs, window, signer, wallet_address, [proposal])
    builder.add_script_input(
        locked, script=deployment.endorsement_spend.witness, redeemer=_redeemer(MINT_ENDORSEMENT_TOKEN)
    )
    builder.add_output(TransactionOutput(
        deployment.endorsement_spend.address, locked.output.amount, datum=inline_datum(datum.to_data())
    ))
    return PreparedTx(builder, (signer,), datum)


def _missing_keys(needed: Iterable[bytes], by_hash: Mapping[bytes, SigningKeyPair]) -> None:
    missing = sorted(set(needed) - set(by_hash))
    if missing:
        raise GovernanceClientError(
            "the transaction needs the signatures of the key hashes "
            + ", ".join(h.hex() for h in missing)
            + ". Pass their signing keys."
        )


def sign_prepared(prepared: PreparedTx, *, keys: Sequence[SigningKeyPair], change_address: Address) -> Transaction:
    """Balance the transaction (change to ``change_address``) and sign it.

    The keys must cover the required signers (checked before the build) and
    the wallet inputs (checked after the coin selection). The client signs
    only with the keys that the caller passes. A prepared transaction builds
    once: after a refusal of the wallet keys, prepare it again."""
    by_hash: dict[bytes, SigningKeyPair] = {}
    for pair in keys:
        by_hash.setdefault(pair.key_hash, pair)
    _missing_keys(prepared.required_signers, by_hash)
    builder = prepared.builder
    body = builder.build(change_address=change_address)
    needed = set(prepared.required_signers)
    for utxo in list(builder.inputs) + list(builder.collaterals):
        part = utxo.output.address.payment_part
        if isinstance(part, VerificationKeyHash):
            needed.add(bytes(part))
    _missing_keys(needed, by_hash)
    witness = builder.build_witness_set(True)
    witness.vkey_witnesses = NonEmptyOrderedSet([
        VerificationKeyWitness(by_hash[h].verification_key, by_hash[h].signing_key.sign(body.hash()))
        for h in sorted(needed)
    ])
    return Transaction(body, witness)


# ── Chain access for the scripts (VectorChainContext async methods) ─────────


@dataclass(frozen=True)
class Submission:
    """The result of an identity-checked action."""

    tx: str
    position: TransactionInput
    lock_tx: Optional[str] = None
    activity: Optional[TransactionInput] = None
    minted: tuple[bytes, ...] = ()


async def load_references(context: Any, deployment: GovernanceDeployment) -> GovernanceReferences:
    """The params record and the CrossRefs output from the chain."""
    params = find_params(await context.async_utxos(str(deployment.params_address)), deployment)
    cross_refs = find_cross_refs(await context.async_utxos(str(deployment.cross_refs_address)), deployment)
    return GovernanceReferences(params, cross_refs)


async def load_identity(context: Any, deployment: GovernanceDeployment, agent_id: Union[bytes, str]) -> list[vid.IdentityRecord]:
    """The record of the agent and its ancestor records at the identity
    address."""
    return identity_records(await context.async_utxos(str(deployment.identity_address)), deployment, agent_id)


async def load_activity(context: Any, deployment: GovernanceDeployment, agent_id: Union[bytes, str]) -> Optional[ActivityRecord]:
    """The activity record of the agent, or None before its first proposal."""
    return find_activity(await context.async_utxos(str(deployment.proposal_spend.address)), deployment, agent_id)


async def load_proposal(context: Any, deployment: GovernanceDeployment, ref: TransactionInput) -> UTxO:
    """The proposal output ``ref`` at the proposal validator."""
    utxo = find_utxo(await context.async_utxos(str(deployment.proposal_spend.address)), ref)
    if utxo is None:
        raise MissingReferenceError(f"the proposal {ref} is not at the proposal validator")
    return utxo


async def plan_window(context: Any, length_ms: int = DEFAULT_RANGE_MS) -> TxWindow:
    """A range from the chain tip (genesis start time and slot length)."""
    clock = SlotClock.from_genesis(await context.async_genesis_param())
    return window_from_tip(clock, await context.async_last_block_slot(), length_ms)


async def submit_transaction(context: Any, tx: Transaction) -> str:
    """Submit a signed transaction and return its id."""
    await context.async_submit_tx_cbor(tx.to_cbor())
    return str(tx.id)


async def wait_for_output(
    context: Any, address: Address, ref: TransactionInput, attempts: int = 24, delay: float = 5.0
) -> UTxO:
    """Poll the address until the output ``ref`` exists."""
    for attempt in range(attempts):
        utxo = find_utxo(await context.async_utxos(str(address)), ref)
        if utxo is not None:
            return utxo
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    raise GovernanceClientError(f"the output {ref} did not appear at {address}")


async def _require_open_window(context: Any, window: TxWindow) -> None:
    tip = await context.async_last_block_slot()
    margin = SUBMIT_MARGIN_MS // window.clock.slot_length_ms
    if tip + margin >= window.ttl_slot:
        raise GovernanceClientError(
            f"the planned range ends at slot {window.ttl_slot} and the tip is at slot {tip}. "
            "Build the creation step again with a new range (prepare_submit_*)."
        )


def _keys(wallet: SigningKeyPair, credential: Optional[SigningKeyPair]) -> list[SigningKeyPair]:
    return [wallet] if credential is None else [wallet, credential]


async def _lock_then_create(
    context: Any,
    lock: PreparedTx,
    address: Address,
    create: Callable[[UTxO], PreparedTx],
    wallet: SigningKeyPair,
    credential: Optional[SigningKeyPair],
    wallet_address: Address,
    window: TxWindow,
    attempts: int,
    delay: float,
) -> tuple[str, Transaction, PreparedTx]:
    lock_tx = sign_prepared(lock, keys=[wallet], change_address=wallet_address)
    lock_id = await submit_transaction(context, lock_tx)
    locked = await wait_for_output(context, address, TransactionInput(lock_tx.id, lock.position_index), attempts, delay)
    await _require_open_window(context, window)
    step = create(locked)
    tx = sign_prepared(step, keys=_keys(wallet, credential), change_address=wallet_address)
    await submit_transaction(context, tx)
    return lock_id, tx, step


async def submit_proposal(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    agent_id: Union[bytes, str],
    terms: ProposalTerms,
    wallet: SigningKeyPair,
    wallet_address: Address,
    credential: Optional[SigningKeyPair] = None,
    attempts: int = 24,
    delay: float = 5.0,
) -> Submission:
    """Lock and submit a proposal of ``agent_id``. ``credential`` is the key
    of the declared credential; the wallet key when it is None."""
    window = await plan_window(context)
    records = await load_identity(context, deployment, agent_id)
    refs = await load_references(context, deployment)
    activity = await load_activity(context, deployment, agent_id)
    lock = prepare_proposal_lock(
        context, deployment, records=records, refs=refs, activity=activity, terms=terms,
        credential_key_hash=(credential or wallet).key_hash, window=window, wallet_address=wallet_address,
    )
    lock_id, tx, step = await _lock_then_create(
        context, lock, deployment.proposal_spend.address,
        lambda locked: prepare_submit_proposal(
            context, deployment, locked=locked, records=records, refs=refs, activity=activity,
            window=window, wallet_address=wallet_address,
        ),
        wallet, credential, wallet_address, window, attempts, delay,
    )
    return Submission(
        str(tx.id), TransactionInput(tx.id, step.position_index), lock_id,
        TransactionInput(tx.id, step.activity_index), step.minted,
    )


async def amend_proposal(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    proposal_ref: TransactionInput,
    new_hash: bytes,
    new_uri: Union[str, bytes],
    wallet: SigningKeyPair,
    wallet_address: Address,
    credential: Optional[SigningKeyPair] = None,
    incorporate: Sequence[TransactionInput] = (),
) -> Submission:
    """Amend a proposal. The proposer is the agent in the proposal datum."""
    window = await plan_window(context)
    proposal = await load_proposal(context, deployment, proposal_ref)
    datum = ProposalDatum.from_data(proposal.output.datum)
    records = await load_identity(context, deployment, datum.proposer_did)
    refs = await load_references(context, deployment)
    critique_utxos = await context.async_utxos(str(deployment.critique_spend.address))
    critiques = []
    for ref in incorporate:
        utxo = find_utxo(critique_utxos, ref)
        if utxo is None:
            raise MissingReferenceError(f"the critique {ref} is not at the critique validator")
        critiques.append(utxo)
    step = prepare_amend_proposal(
        context, deployment, proposal=proposal, records=records, refs=refs, new_hash=new_hash,
        new_uri=new_uri, window=window, wallet_address=wallet_address, incorporate=critiques,
    )
    tx = sign_prepared(step, keys=_keys(wallet, credential), change_address=wallet_address)
    await submit_transaction(context, tx)
    return Submission(str(tx.id), TransactionInput(tx.id, step.position_index))


async def submit_critique(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    agent_id: Union[bytes, str],
    proposal_ref: TransactionInput,
    terms: CritiqueTerms,
    wallet: SigningKeyPair,
    wallet_address: Address,
    credential: Optional[SigningKeyPair] = None,
    attempts: int = 24,
    delay: float = 5.0,
) -> Submission:
    """Lock and submit a critique of ``agent_id`` about a proposal."""
    window = await plan_window(context)
    records = await load_identity(context, deployment, agent_id)
    refs = await load_references(context, deployment)
    proposal = await load_proposal(context, deployment, proposal_ref)
    lock = prepare_critique_lock(
        context, deployment, records=records, refs=refs, proposal=proposal, terms=terms,
        credential_key_hash=(credential or wallet).key_hash, window=window, wallet_address=wallet_address,
    )
    lock_id, tx, step = await _lock_then_create(
        context, lock, deployment.critique_spend.address,
        lambda locked: prepare_submit_critique(
            context, deployment, locked=locked, records=records, refs=refs, proposal=proposal,
            window=window, wallet_address=wallet_address,
        ),
        wallet, credential, wallet_address, window, attempts, delay,
    )
    return Submission(str(tx.id), TransactionInput(tx.id, step.position_index), lock_id, minted=step.minted)


async def submit_endorsement(
    context: Any,
    deployment: GovernanceDeployment,
    *,
    agent_id: Union[bytes, str],
    proposal_ref: TransactionInput,
    stake_amount: int,
    wallet: SigningKeyPair,
    wallet_address: Address,
    credential: Optional[SigningKeyPair] = None,
    attempts: int = 24,
    delay: float = 5.0,
) -> Submission:
    """Lock and submit an endorsement of ``agent_id`` for a proposal."""
    window = await plan_window(context)
    records = await load_identity(context, deployment, agent_id)
    refs = await load_references(context, deployment)
    proposal = await load_proposal(context, deployment, proposal_ref)
    lock = prepare_endorsement_lock(
        context, deployment, records=records, refs=refs, proposal=proposal, stake_amount=stake_amount,
        credential_key_hash=(credential or wallet).key_hash, window=window, wallet_address=wallet_address,
    )
    lock_id, tx, step = await _lock_then_create(
        context, lock, deployment.endorsement_spend.address,
        lambda locked: prepare_submit_endorsement(
            context, deployment, locked=locked, records=records, refs=refs, proposal=proposal,
            window=window, wallet_address=wallet_address,
        ),
        wallet, credential, wallet_address, window, attempts, delay,
    )
    return Submission(str(tx.id), TransactionInput(tx.id, step.position_index), lock_id)


def sdk_reference_inputs(*utxos: UTxO) -> list[dict]:
    """Reference inputs in the format of ``GovernanceClient.
    set_governance_reference_inputs`` (for the exits that the SDK builds)."""
    return [
        {"tx_hash": str(u.input.transaction_id), "output_index": u.input.index, "address": str(u.output.address)}
        for u in utxos
    ]
