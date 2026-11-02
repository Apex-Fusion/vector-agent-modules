"""Module-1 as a party of identity records (Part 2 §3.8): the verdict datum,
the verdict policy, the AuditingConfig, the parameters of ``identity_party``,
the Module-1 party and the redeemers of the ``identity_party`` validator.

The on-chain source of truth is ``contracts/lib/adversarial_auditing/types.ak``
(``VerdictAction``, ``VerdictRule``, ``VerdictPolicy``, ``IdentityVerdict``,
``VerdictMint``, ``PartyApproval``, ``AuditingConfig``),
``contracts/lib/adversarial_auditing/identity_party.ak`` (handler rules) and
``contracts/validators/identity_party.ak``.

All code that depends on the shape of the verdict datum is in this module:
the type, its encoding and decoding, the token name, the mapping from the
verdict policy to a verdict, the record change of a verdict application and
the reclaim rule of an expired verdict. The transaction builders use only
the functions of this module and never encode or decode a verdict
themselves.

Only an oracle resolution (OracleResolve) gives a verdict. A jury resolution
(ResolveJury) and a timeout (TimeoutResolve) give no verdict: the auditor
names the eligible jurors of a challenge, so a jury outcome is not safe for
an identity verdict.

The module has no network access. It needs only ``cbor2`` and the shared
identity module ``simulation.vector_identity``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Mapping, Optional, Tuple, Union

import cbor2

from simulation import vector_identity as vi


#: Asset name of the identity configuration token (Part 2 §3.8): UTF-8
#: ``identity-config``. The configuration record at ``config_holder`` holds
#: this token, and its inline datum is the hash of ``identity_party`` (M1).
CONFIG_TOKEN_NAME = b"identity-config"

#: The challenge outcomes (``Verdict`` of types.ak), in constructor order.
CLAIMER_WINS = "ClaimerWins"
AUDITOR_WINS = "AuditorWins"
INCONCLUSIVE = "Inconclusive"
OUTCOMES = (CLAIMER_WINS, AUDITOR_WINS, INCONCLUSIVE)

#: The challenge actions that resolve a challenge (``ChallengeAction`` of
#: types.ak). Only ORACLE_RESOLVE can give a verdict.
ORACLE_RESOLVE = "OracleResolve"
RESOLVE_JURY = "ResolveJury"
TIMEOUT_RESOLVE = "TimeoutResolve"
RESOLUTIONS = (ORACLE_RESOLVE, RESOLVE_JURY, TIMEOUT_RESOLVE)

#: Milliseconds from the upper bound of the resolution transaction to the
#: ``expires_at`` of its verdict: 30 days (``days_30_ms``).
VERDICT_LIFETIME_MS = vi.DAYS_30_MS

#: The roles that Module-1 can hold, with the highest rights of each role
#: (Part 1 §5 "Parties", Part 2 §3.8 "Roles").
MODULE1_RIGHTS = {
    vi.Role.ACCOUNTABLE: vi.RIGHT_SUSPEND + vi.RIGHT_REVOKE,
    vi.Role.RESPONSIBLE: vi.RIGHT_SUSPEND,
}


# ── Verdict policy ──────────────────────────────────────────────────────────


class VerdictAction(IntEnum):
    """``VerdictAction``: ``VerdictSuspend`` (Constr 0) or ``VerdictRevoke``
    (Constr 1)."""

    SUSPEND = 0
    REVOKE = 1

    @property
    def label(self) -> str:
        return "suspend" if self is VerdictAction.SUSPEND else "revoke"


@dataclass(frozen=True)
class VerdictRule:
    """``VerdictRule { action, duration }``. A suspension lasts 1 ms to 30
    days; a revocation has the duration 0 (``rule_ok`` of identity_party.ak).
    """

    action: VerdictAction
    duration: int

    def __post_init__(self) -> None:
        if self.action is VerdictAction.SUSPEND:
            if not 0 < self.duration <= vi.DAYS_30_MS:
                raise ValueError("a suspension lasts 1 ms to 30 days")
        elif self.duration != 0:
            raise ValueError("a revocation has the duration 0")

    @classmethod
    def from_json(cls, value: Mapping[str, Any], name: str) -> "VerdictRule":
        """A rule in the format of ``deploy/identity/config.json``:
        ``{"action": "suspend" | "revoke", "duration_ms": int}``."""
        if not isinstance(value, Mapping):
            raise ValueError(f"{name}: expected an object or null")
        action = value.get("action")
        if action not in ("suspend", "revoke"):
            raise ValueError(f"{name}: action must be suspend or revoke")
        duration = value.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ValueError(f"{name}: duration_ms must be an integer")
        kind = VerdictAction.SUSPEND if action == "suspend" else VerdictAction.REVOKE
        try:
            return cls(kind, duration)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc

    def data(self) -> cbor2.CBORTag:
        return vi.constr(0, [vi.constr(int(self.action), []), int(self.duration)])


@dataclass(frozen=True)
class VerdictPolicy:
    """``VerdictPolicy { auditor_wins, claimer_wins }``. The agent of a
    verdict is the side that loses: AuditorWins names the claimer,
    ClaimerWins names the auditor. Inconclusive gives no verdict."""

    auditor_wins: Optional[VerdictRule]
    claimer_wins: Optional[VerdictRule]

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "VerdictPolicy":
        """The ``verdict_policy`` object of ``deploy/identity/config.json``.
        Both keys must be present; ``null`` means no verdict."""
        if not isinstance(value, Mapping):
            raise ValueError("verdict_policy: expected an object")
        rules = {}
        for key in ("auditor_wins", "claimer_wins"):
            if key not in value:
                raise ValueError(f"verdict_policy.{key} is missing (use null for no verdict)")
            raw = value[key]
            rules[key] = None if raw is None else VerdictRule.from_json(raw, f"verdict_policy.{key}")
        return cls(**rules)

    def rule(self, outcome: str) -> Optional[VerdictRule]:
        """The rule for a challenge outcome (``verdict_rule``)."""
        if outcome == AUDITOR_WINS:
            return self.auditor_wins
        if outcome == CLAIMER_WINS:
            return self.claimer_wins
        if outcome == INCONCLUSIVE:
            return None
        raise ValueError(f"unknown challenge outcome {outcome!r}")

    def data(self) -> cbor2.CBORTag:
        return vi.constr(0, [
            vi.option_data(None if r is None else r.data())
            for r in (self.auditor_wins, self.claimer_wins)
        ])


@dataclass(frozen=True)
class AuditingConfig:
    """``AuditingConfig``, the first parameter of every Module-1 validator.
    The field order is the on-chain order (types.ak)."""

    identity_hash: bytes
    oracle_vkh: bytes
    refs_token_policy: bytes
    config_policy: bytes
    config_holder: bytes
    verdict_policy: VerdictPolicy

    def data(self) -> cbor2.CBORTag:
        return vi.constr(0, [
            bytes(self.identity_hash),
            bytes(self.oracle_vkh),
            bytes(self.refs_token_policy),
            bytes(self.config_policy),
            bytes(self.config_holder),
            self.verdict_policy.data(),
        ])

    def cbor(self) -> bytes:
        """The Plutus encoding, as ``scripts/identity_deploy.py encode``
        writes it."""
        return vi.serialise_data(self.data())


def identity_party_parameters(
    config: AuditingConfig, challenge_hash: bytes, claim_hash: bytes
) -> Tuple[bytes, bytes, bytes]:
    """The parameters of ``identity_party`` in the order of ``aiken blueprint
    apply`` (``scripts/identity_deploy.py plan``): the AuditingConfig, the
    hash of the challenge validator, then the hash of the claim validator.
    Each item is the CBOR of its Plutus Data, as ``plan.json`` writes it
    (``auditing_config_cbor``, ``identity_party_challenge_hash_cbor``,
    ``identity_party_claim_hash_cbor``). The verdict mint reads the claim of
    a resolved challenge only at the claim validator ``claim_hash``. Raises
    ValueError when a hash does not have 28 bytes."""
    for name, value in (("challenge hash", challenge_hash), ("claim hash", claim_hash)):
        if not isinstance(value, (bytes, bytearray)) or len(value) != 28:
            raise ValueError(f"the {name} of identity_party must have 28 bytes")
    return (
        config.cbor(),
        vi.serialise_data(bytes(challenge_hash)),
        vi.serialise_data(bytes(claim_hash)),
    )


# ── Plutus addresses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlutusCredential:
    """``Credential`` of ``cardano/address``: ``VerificationKey(hash)``
    (Constr 0) or ``Script(hash)`` (Constr 1). ``hash`` has 28 bytes."""

    hash: bytes
    script: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.hash, (bytes, bytearray)) or len(self.hash) != 28:
            raise ValueError("a credential hash has 28 bytes")

    def data(self) -> cbor2.CBORTag:
        return vi.constr(1 if self.script else 0, [bytes(self.hash)])

    def to_json(self) -> dict:
        return {"type": "script" if self.script else "key", "hash": bytes(self.hash).hex()}


@dataclass(frozen=True)
class StakePointer:
    """``Pointer { slot_number, transaction_index, certificate_index }``
    (Constr 1 of ``StakeCredential``)."""

    slot_number: int
    transaction_index: int
    certificate_index: int


@dataclass(frozen=True)
class PlutusAddress:
    """``Address { payment_credential, stake_credential }`` of
    ``cardano/address``. ``stake`` is None (no stake part), a
    ``PlutusCredential`` (``Inline``) or a ``StakePointer``. The contracts
    compare the whole address: a payout output must have this payment part
    and this stake part."""

    payment: PlutusCredential
    stake: Union[None, PlutusCredential, StakePointer] = None

    def data(self) -> cbor2.CBORTag:
        if self.stake is None:
            stake = None
        elif isinstance(self.stake, PlutusCredential):
            stake = vi.constr(0, [self.stake.data()])
        else:
            pointer = self.stake
            stake = vi.constr(1, [
                int(pointer.slot_number),
                int(pointer.transaction_index),
                int(pointer.certificate_index),
            ])
        return vi.constr(0, [self.payment.data(), vi.option_data(stake)])

    def to_json(self) -> dict:
        if self.stake is None:
            stake = None
        elif isinstance(self.stake, PlutusCredential):
            stake = self.stake.to_json()
        else:
            stake = {"pointer": [
                self.stake.slot_number,
                self.stake.transaction_index,
                self.stake.certificate_index,
            ]}
        return {"payment_credential": self.payment.to_json(), "stake_credential": stake}


def _decode_credential(value: Any, what: str) -> PlutusCredential:
    index, fields = vi.unconstr(value)
    if (
        index not in (0, 1)
        or len(fields) != 1
        or not isinstance(fields[0], (bytes, bytearray))
        or len(fields[0]) != 28
    ):
        raise vi.IdentityDecodeError(f"{what}: not a Credential")
    return PlutusCredential(bytes(fields[0]), script=index == 1)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def decode_address(value: Any, what: str = "Address") -> PlutusAddress:
    """Decode the Plutus Data tree of an ``Address``. Raises
    ``IdentityDecodeError`` for another shape."""
    index, fields = vi.unconstr(value)
    if index != 0 or len(fields) != 2:
        raise vi.IdentityDecodeError(f"{what}: expected constructor 0 with 2 fields")
    payment = _decode_credential(fields[0], f"{what}.payment_credential")
    option, option_fields = vi.unconstr(fields[1])
    if option == 1 and not option_fields:
        return PlutusAddress(payment)
    if option != 0 or len(option_fields) != 1:
        raise vi.IdentityDecodeError(f"{what}.stake_credential: not an Option")
    kind, stake_fields = vi.unconstr(option_fields[0])
    if kind == 0 and len(stake_fields) == 1:
        return PlutusAddress(
            payment, _decode_credential(stake_fields[0], f"{what}.stake_credential")
        )
    if kind == 1 and len(stake_fields) == 3 and all(_is_int(v) for v in stake_fields):
        return PlutusAddress(payment, StakePointer(*stake_fields))
    raise vi.IdentityDecodeError(f"{what}.stake_credential: not a StakeCredential")


# ── Verdict datum ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IdentityVerdict:
    """``IdentityVerdict { agent, action, duration, challenge, deposit_to,
    expires_at }``: the inline datum of a verdict output. ``challenge`` is
    the output reference of the challenge input that the oracle resolution
    spends. ``deposit_to`` gets the AP3X of the verdict output when the
    verdict expires without an application. ``expires_at`` (POSIX ms) is the
    upper bound of the resolution plus 30 days: an application needs
    ``hi <= expires_at``, and a reclaim needs ``expires_at < lo``."""

    agent: bytes
    action: VerdictAction
    duration: int
    challenge_tx_id: bytes
    challenge_index: int
    deposit_to: PlutusAddress
    expires_at: int

    def data(self) -> cbor2.CBORTag:
        return vi.constr(0, [
            bytes(self.agent),
            vi.constr(int(self.action), []),
            int(self.duration),
            vi.output_reference_data(self.challenge_tx_id, self.challenge_index),
            self.deposit_to.data(),
            int(self.expires_at),
        ])

    def datum_cbor(self) -> bytes:
        """The inline datum bytes of the verdict output."""
        return vi.serialise_data(self.data())

    def token_name(self) -> bytes:
        """``verdict_token_name``: blake2b_256 of the serialised verdict,
        with all fields."""
        return vi.hash_data(self.data())

    def to_json(self) -> dict:
        return {
            "agent": vi.did_from_id(self.agent),
            "action": self.action.label,
            "duration_ms": self.duration,
            "challenge": f"{bytes(self.challenge_tx_id).hex()}#{self.challenge_index}",
            "deposit_to": self.deposit_to.to_json(),
            "expires_at": self.expires_at,
            "token_name": self.token_name().hex(),
        }


def decode_verdict(value: Any) -> IdentityVerdict:
    """Decode a verdict datum (CBOR bytes or hex, a tree, or a pycardano
    datum object). Raises ``IdentityDecodeError`` for another shape, for
    example the 4-field verdict of an older deployment."""
    try:
        tree = vi.data_from(value)
    except vi.IdentityDecodeError:
        raise
    except Exception as exc:  # cbor2 errors, bad hex
        raise vi.IdentityDecodeError(f"verdict datum is not CBOR: {exc}") from exc
    index, fields = vi.unconstr(tree)
    if index != 0 or len(fields) != 6:
        raise vi.IdentityDecodeError("IdentityVerdict: expected constructor 0 with 6 fields")
    agent, action, duration, challenge, deposit_to, expires_at = fields
    action_index, action_fields = vi.unconstr(action)
    ref_index, ref_fields = vi.unconstr(challenge)
    if not isinstance(agent, (bytes, bytearray)):
        raise vi.IdentityDecodeError("IdentityVerdict.agent: expected a byte string")
    if action_index not in (0, 1) or action_fields:
        raise vi.IdentityDecodeError("IdentityVerdict.action: not a VerdictAction")
    if not _is_int(duration):
        raise vi.IdentityDecodeError("IdentityVerdict.duration: expected an integer")
    if (
        ref_index != 0
        or len(ref_fields) != 2
        or not isinstance(ref_fields[0], (bytes, bytearray))
        or not _is_int(ref_fields[1])
    ):
        raise vi.IdentityDecodeError("IdentityVerdict.challenge: not an OutputReference")
    if not _is_int(expires_at):
        raise vi.IdentityDecodeError("IdentityVerdict.expires_at: expected an integer")
    return IdentityVerdict(
        agent=bytes(agent),
        action=VerdictAction(action_index),
        duration=duration,
        challenge_tx_id=bytes(ref_fields[0]),
        challenge_index=ref_fields[1],
        deposit_to=decode_address(deposit_to, "IdentityVerdict.deposit_to"),
        expires_at=expires_at,
    )


def resolution_verdict(
    policy: VerdictPolicy,
    outcome: str,
    *,
    resolution: str,
    claimer_did: bytes,
    auditor_did: bytes,
    challenge_tx_id: bytes,
    challenge_index: int,
    deposit_to: PlutusAddress,
    hi: int,
) -> Optional[IdentityVerdict]:
    """The verdict of a challenge resolution (``resolution_verdict`` of
    identity_party.ak), or None. Only an oracle resolution
    (``resolution == ORACLE_RESOLVE``) gives a verdict; a jury resolution
    and a timeout give None. An oracle resolution gives None when the policy
    gives no rule for the outcome.

    The agent is the losing side: the claimer for AuditorWins, the auditor
    for ClaimerWins. ``challenge_tx_id`` and ``challenge_index`` are the
    output reference of the challenge input that the resolution spends.
    ``deposit_to`` is the address of the wallet that funds the verdict
    output. ``hi`` is the upper bound (POSIX ms) of the validity range of the
    resolution; ``expires_at = hi + 30 days``."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"unknown resolution {resolution!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown challenge outcome {outcome!r}")
    if resolution != ORACLE_RESOLVE:
        return None
    rule = policy.rule(outcome)
    if rule is None:
        return None
    agent = claimer_did if outcome == AUDITOR_WINS else auditor_did
    return IdentityVerdict(
        agent=vi.normalize_agent_id(bytes(agent)),
        action=rule.action,
        duration=rule.duration,
        challenge_tx_id=bytes(challenge_tx_id),
        challenge_index=int(challenge_index),
        deposit_to=deposit_to,
        expires_at=int(hi) + VERDICT_LIFETIME_MS,
    )


def check_reclaim(verdict: IdentityVerdict, lo: int, hi: int) -> None:
    """The time rule of a reclaim (``reclaim_ok``): the verdict expired
    before the lower bound (``expires_at < lo``), and the range has at most
    one hour. Raises ``IdentityError`` otherwise."""
    vi.check_window(lo, hi)
    if not verdict.expires_at < lo:
        raise vi.IdentityError(
            f"the verdict expires at {verdict.expires_at}; a reclaim needs a "
            f"lower bound after it (lower bound {lo})"
        )


def verdict_from_output(datum: Any, token_name: bytes) -> IdentityVerdict:
    """The verdict of a verdict output: decode the inline datum and check
    that it hashes to the token name of the output. Raises
    ``IdentityError`` when the datum does not hash to the token name."""
    if datum is None:
        raise vi.IdentityError("the verdict output has no inline datum")
    verdict = decode_verdict(datum)
    if verdict.token_name() != bytes(token_name):
        raise vi.IdentityError(
            f"the verdict datum hashes to {verdict.token_name().hex()}, "
            f"not to the token name {bytes(token_name).hex()}"
        )
    return verdict


# ── Redeemers ───────────────────────────────────────────────────────────────


def mint_verdict_redeemer() -> cbor2.CBORTag:
    """``VerdictMint.MintVerdict`` (Constr 0)."""
    return vi.constr(0, [])


def burn_verdict_redeemer() -> cbor2.CBORTag:
    """``VerdictMint.BurnVerdict`` (Constr 1): in a verdict application
    (with the withdrawal of ``Script(M1)``) and in a reclaim after
    ``expires_at`` (without that withdrawal)."""
    return vi.constr(1, [])


def consent_redeemer() -> cbor2.CBORTag:
    """``PartyApproval.Consent`` (Constr 0)."""
    return vi.constr(0, [])


def apply_verdict_redeemer() -> cbor2.CBORTag:
    """``PartyApproval.ApplyVerdict`` (Constr 1)."""
    return vi.constr(1, [])


def verdict_spend_redeemer() -> cbor2.CBORTag:
    """The spend redeemer of a verdict output. The spend handler ignores
    it; Constr 0 is used."""
    return vi.constr(0, [])


# ── Module-1 party ──────────────────────────────────────────────────────────


def module1_party(m1: bytes, role: vi.Role, not_before: int) -> vi.Party:
    """A new Module-1 party with the terms that Consent accepts: role
    Accountable (rights 3) or Responsible (rights 1), signer
    ``Script { M1 }``, ``not_before``, no ``not_after`` and no
    ``removal_after``."""
    role = vi.Role(role)
    if role not in MODULE1_RIGHTS:
        raise ValueError("Module-1 can only be an Accountable or a Responsible party")
    return vi.Party(
        role=role,
        signer=vi.Signer.script(m1),
        organisation=None,
        not_before=int(not_before),
        not_after=None,
        rights=MODULE1_RIGHTS[role],
        removal_after=None,
    )


def has_module1_party(record: vi.IdentityDatum, m1: bytes) -> bool:
    """The record lists a party with the signer ``Script { M1 }``."""
    signer = vi.Signer.script(m1)
    return any(p.signer == signer for p in record.parties)


def consent_parties(
    record: vi.IdentityDatum, m1: bytes, role: vi.Role, lo: int
) -> Tuple[vi.Party, ...]:
    """The parties of a Consent Update: the parties of the record, then the
    new Module-1 party last (Part 2 §3.6 "new parties come last"). Raises
    ``IdentityError`` when Module-1 already has this role."""
    party = module1_party(m1, role, lo)
    if any(p.signer == party.signer and p.role == party.role for p in record.parties):
        raise vi.IdentityError(
            f"Module-1 is already a {party.role.name.lower()} party of the record"
        )
    return tuple(record.parties) + (party,)


def verdict_party_index(
    record: vi.IdentityDatum, m1: bytes, right: int, lo: int, hi: int
) -> int:
    """The index of the first Module-1 party that has ``right`` and covers
    ``[lo, hi]`` (the ``ByParty`` rules of Suspend and Revoke). Raises
    ``IdentityError`` when no such party exists."""
    signer = vi.Signer.script(m1)
    for index, party in enumerate(record.parties):
        if party.signer == signer and (party.rights // right) % 2 == 1 and vi.covers(party, lo, hi):
            return index
    name = "suspend" if right == vi.RIGHT_SUSPEND else "revoke"
    raise vi.IdentityError(
        f"the record has no Module-1 party with the {name} right that covers "
        f"the window [{lo}, {hi}]"
    )


# ── Verdict application ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerdictApplication:
    """The record change of a verdict application (``apply_verdict_ok``):
    the identity spend redeemer, the new record datum, the index of the
    Module-1 party, and the end of the Module-1 hold (None for a
    revocation)."""

    verdict: IdentityVerdict
    party_index: int
    redeemer: cbor2.CBORTag
    record: vi.IdentityDatum
    until: Optional[int]


def verdict_application(
    verdict: IdentityVerdict,
    record: vi.IdentityRecord,
    m1: bytes,
    lo: int,
    hi: int,
) -> VerdictApplication:
    """The application of ``verdict`` to the agent's record over the window
    ``[lo, hi]``. Raises ``IdentityError`` when the verdict expired before
    ``hi`` (an application needs ``hi <= expires_at``), when the record is
    not the verdict's agent, when it is a tombstone, when it has no Module-1
    party with the right that the action needs, or when a suspension would
    end before ``hi``.

    - Suspend: redeemer ``Suspend { ByParty { index } }``; the new record
      holds the Module-1 hold ``{ reason 64, since lo, until }`` with
      ``until`` = the later of ``lo + duration`` and the ``until`` of the
      Module-1 hold in force; ``hi < until`` (Part 2 §3.5).
    - Revoke: redeemer ``Revoke { ByParty { index } }``; the new record is
      the tombstone with ``Revoked { Script { M1 }, 64, lo }``.
    """
    vi.check_window(lo, hi)
    if hi > verdict.expires_at:
        raise vi.IdentityError(
            f"the verdict expires at {verdict.expires_at}, before the upper "
            f"bound {hi}; it cannot apply any more (reclaim the verdict output)"
        )
    if record.agent_id != verdict.agent:
        raise vi.IdentityError(
            f"the verdict names {vi.did_from_id(verdict.agent)}, "
            f"not {record.did}"
        )
    if vi.is_tombstone(record.datum):
        raise vi.IdentityError(f"{record.did} is a tombstone; a verdict cannot apply")
    author = vi.Signer.script(m1)
    if verdict.action is VerdictAction.SUSPEND:
        index = verdict_party_index(record.datum, m1, vi.RIGHT_SUSPEND, lo, hi)
        until = lo + verdict.duration
        current = vi.hold_of(record.datum, author, lo)
        if current is not None and current.until > until:
            until = current.until
        if until <= hi:
            raise vi.IdentityError(
                f"the hold would end at {until}, not after the upper bound {hi}"
            )
        if until > lo + vi.DAYS_30_MS:
            raise vi.IdentityError("a hold lasts at most 30 days from the lower bound")
        return VerdictApplication(
            verdict=verdict,
            party_index=index,
            redeemer=vi.suspend_redeemer(vi.by_party(index)),
            record=vi.suspended(record.datum, author, vi.REASON_DISPUTE_VERDICT, lo, until),
            until=until,
        )
    index = verdict_party_index(record.datum, m1, vi.RIGHT_REVOKE, lo, hi)
    return VerdictApplication(
        verdict=verdict,
        party_index=index,
        redeemer=vi.revoke_redeemer(vi.by_party(index)),
        record=vi.tombstone(
            record.datum, vi.Status.revoked(author, vi.REASON_DISPUTE_VERDICT, lo)
        ),
        until=None,
    )


__all__ = [
    "AUDITOR_WINS",
    "AuditingConfig",
    "CLAIMER_WINS",
    "CONFIG_TOKEN_NAME",
    "INCONCLUSIVE",
    "IdentityVerdict",
    "MODULE1_RIGHTS",
    "ORACLE_RESOLVE",
    "OUTCOMES",
    "PlutusAddress",
    "PlutusCredential",
    "RESOLUTIONS",
    "RESOLVE_JURY",
    "StakePointer",
    "TIMEOUT_RESOLVE",
    "VERDICT_LIFETIME_MS",
    "VerdictAction",
    "VerdictApplication",
    "VerdictPolicy",
    "VerdictRule",
    "apply_verdict_redeemer",
    "burn_verdict_redeemer",
    "check_reclaim",
    "consent_parties",
    "consent_redeemer",
    "decode_address",
    "decode_verdict",
    "has_module1_party",
    "identity_party_parameters",
    "mint_verdict_redeemer",
    "module1_party",
    "resolution_verdict",
    "verdict_application",
    "verdict_from_output",
    "verdict_party_index",
    "verdict_spend_redeemer",
]
