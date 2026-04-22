"""Runtime ProtocolParams resolver — Option A refactor.

Reads the on-chain ``params_utxo`` datum (17-field CBORTag(121, [...]))
produced by the governance deployer and materialises it as a frozen
``ResolvedParams`` dataclass. Builders and scenario helpers thread this
through their call sites instead of carrying per-site hardcoded defaults,
so the same simulation code paths are portable between v15 sim-testnet
and v14/v15 mainnet without edits.

Field order mirrors ``contracts/lib/adversarial_auditing/params.ak`` lines
30-88. If the on-chain type gains / reorders a field, update both the
dataclass here AND the deployer in lockstep — the resolver verifies the
field count and raises ``ParamsUnavailable`` on drift.

Plutus Bool encoding note:
    On chain, ``Bool`` is a two-constructor type. Plutus CBOR convention:
        CBORTag(121, [])  -> Constr0 -> False
        CBORTag(122, [])  -> Constr1 -> True
    ``oracle_active`` is the only Bool field in ``ProtocolParams``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cbor2


class ParamsUnavailable(Exception):
    """Raised when the params_utxo datum is missing or malformed.

    Callers (builders, scenarios, CLI) catch this by name so the failure
    surfaces crisply rather than as a downstream ``KeyError`` /
    ``AttributeError`` in the builder math.
    """


@dataclass(frozen=True)
class ResolvedParams:
    """Materialised snapshot of the on-chain ``ProtocolParams`` datum.

    Frozen so a single instance can be safely cached on a
    ``DeploymentState`` and shared across lifecycle steps without fear
    of accidental mutation. Field order MUST mirror
    ``contracts/lib/adversarial_auditing/params.ak:30-88`` — the
    resolver reads positional CBOR and zips values against
    ``dataclasses.fields(ResolvedParams)`` in declaration order.
    """

    min_claim_stake: int
    min_challenge_window: int
    max_challenge_window: int
    jury_size: int
    min_juror_bond: int
    jury_fee_rate: int
    selection_delay: int
    resolution_deadline: int
    juror_slash_rate: int
    min_agent_age: int
    max_concurrent_cases: int
    min_jury_pool_size: int
    min_jury_pool_total: int
    oracle_active: bool
    commit_window: int
    reveal_window: int
    cleanup_buffer: int


# Number of fields we expect in the on-chain ProtocolParams CBORTag(121, …).
_EXPECTED_FIELD_COUNT = 17

# Outer constructor tag for a well-formed ProtocolParams datum. ProtocolParams
# is single-constructor in params.ak so the only valid outer tag is Constr0
# (CBOR tag 121). Anything else indicates structural corruption.
_CONSTR0_TAG = 121

# Plutus Bool convention — see module docstring.
_BOOL_FALSE_TAG = 121  # Constr0
_BOOL_TRUE_TAG = 122   # Constr1


def _decode_bool(field: Any, field_name: str) -> bool:
    """Decode a Plutus Bool CBORTag to a Python ``bool``.

    Accepts the two legal encodings and raises ``ParamsUnavailable``
    on anything else so the resolver treats malformed bools the same
    as any other structural corruption.
    """
    tag = getattr(field, "tag", None)
    value = getattr(field, "value", None)
    if tag == _BOOL_FALSE_TAG and value == []:
        return False
    if tag == _BOOL_TRUE_TAG and value == []:
        return True
    raise ParamsUnavailable(
        f"ProtocolParams field {field_name!r}: expected Plutus Bool "
        f"CBORTag(121, []) or CBORTag(122, []); got {field!r}."
    )


def resolve_protocol_params(deployment) -> ResolvedParams:
    """Read the deployment's ``params_utxo`` datum and return a
    ``ResolvedParams`` snapshot.

    Contract:
      * Reads ONLY ``deployment.params_utxo`` — no other attribute.
      * Expects the datum's CBOR to decode as ``CBORTag(121, [<17 fields>])``.
      * Bool field ``oracle_active`` is coerced per Plutus convention.
      * Any structural defect raises ``ParamsUnavailable`` — callers must
        handle this by name.

    Args:
        deployment: Anything with a ``params_utxo`` attribute whose value
            is either ``None`` (missing) or a ``UTxO`` whose
            ``.output.datum`` carries the ProtocolParams CBOR. Duck-typed
            so tests can pass a minimal stand-in.

    Returns:
        A frozen ``ResolvedParams`` instance.

    Raises:
        ParamsUnavailable: ``params_utxo`` is None, its datum is missing,
            the outer CBORTag is not Constr0, the field count is wrong,
            or the Bool field is malformed.
    """
    params_utxo = getattr(deployment, "params_utxo", None)
    if params_utxo is None:
        raise ParamsUnavailable(
            "deployment.params_utxo is None — cannot resolve ProtocolParams. "
            "Ensure the deployment JSON includes a params_utxo reference and "
            "that DeploymentState.resolve_refs() has been called."
        )

    output = getattr(params_utxo, "output", None)
    datum = getattr(output, "datum", None) if output is not None else None
    if datum is None:
        raise ParamsUnavailable(
            "params_utxo.output.datum is missing — the governance UTxO must "
            "carry an inline datum encoding ProtocolParams."
        )

    # RawCBOR exposes `.cbor`; PlutusData / bytes fall through to ``bytes()``.
    raw_bytes = datum.cbor if hasattr(datum, "cbor") else bytes(datum)

    try:
        decoded = cbor2.loads(raw_bytes)
    except Exception as exc:  # noqa: BLE001 — surface all decode failures.
        raise ParamsUnavailable(
            f"params_utxo datum failed CBOR decode: {exc!r}"
        ) from exc

    outer_tag = getattr(decoded, "tag", None)
    if outer_tag != _CONSTR0_TAG:
        raise ParamsUnavailable(
            f"ProtocolParams datum outer CBORTag must be "
            f"{_CONSTR0_TAG} (Constr0); got {outer_tag!r}. "
            f"ProtocolParams is a single-constructor type — any other "
            f"outer tag is structural corruption."
        )

    fields = list(decoded.value) if decoded.value is not None else []
    if len(fields) != _EXPECTED_FIELD_COUNT:
        raise ParamsUnavailable(
            f"ProtocolParams datum must have exactly "
            f"{_EXPECTED_FIELD_COUNT} fields (mirror of params.ak:30-88); "
            f"got {len(fields)}."
        )

    # Positional decode. Field order MUST mirror the dataclass declaration
    # (which in turn mirrors params.ak). The sole Bool slot gets coerced.
    try:
        min_claim_stake       = int(fields[0])
        min_challenge_window  = int(fields[1])
        max_challenge_window  = int(fields[2])
        jury_size             = int(fields[3])
        min_juror_bond        = int(fields[4])
        jury_fee_rate         = int(fields[5])
        selection_delay       = int(fields[6])
        resolution_deadline   = int(fields[7])
        juror_slash_rate      = int(fields[8])
        min_agent_age         = int(fields[9])
        max_concurrent_cases  = int(fields[10])
        min_jury_pool_size    = int(fields[11])
        min_jury_pool_total   = int(fields[12])
        oracle_active         = _decode_bool(fields[13], "oracle_active")
        commit_window         = int(fields[14])
        reveal_window         = int(fields[15])
        cleanup_buffer        = int(fields[16])
    except ParamsUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — any Int coercion failure.
        raise ParamsUnavailable(
            f"ProtocolParams datum has a malformed field: {exc!r}"
        ) from exc

    return ResolvedParams(
        min_claim_stake=min_claim_stake,
        min_challenge_window=min_challenge_window,
        max_challenge_window=max_challenge_window,
        jury_size=jury_size,
        min_juror_bond=min_juror_bond,
        jury_fee_rate=jury_fee_rate,
        selection_delay=selection_delay,
        resolution_deadline=resolution_deadline,
        juror_slash_rate=juror_slash_rate,
        min_agent_age=min_agent_age,
        max_concurrent_cases=max_concurrent_cases,
        min_jury_pool_size=min_jury_pool_size,
        min_jury_pool_total=min_jury_pool_total,
        oracle_active=oracle_active,
        commit_window=commit_window,
        reveal_window=reveal_window,
        cleanup_buffer=cleanup_buffer,
    )


__all__ = ["ResolvedParams", "ParamsUnavailable", "resolve_protocol_params"]
