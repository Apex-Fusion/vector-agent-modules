"""
RED tests for ``simulation.params`` — the ProtocolParams resolver.

Contract under test:
    Catherine will implement ``simulation.params`` with:

    - A frozen dataclass ``ResolvedParams`` carrying the 17 fields of the
      on-chain ``ProtocolParams`` (``contracts/lib/adversarial_auditing/params.ak:30-88``).

    - ``resolve_protocol_params(deployment) -> ResolvedParams``: reads
      ``deployment.params_utxo.output.datum`` as CBOR, parses
      ``CBORTag(121, [...17 fields...])``, coerces the ``oracle_active``
      Bool (``CBORTag(121, []) -> False`` / ``CBORTag(122, []) -> True``),
      and raises ``ParamsUnavailable`` on missing / malformed datums.

Every test in this file MUST fail against the current repo (the
``simulation/params`` module does not exist yet) — the failures surface
as ``ImportError`` on the first test, then as ``ModuleNotFoundError``
/ ``AttributeError`` across the rest. Catherine's GREEN makes them pass.

Golden values for the v15-sim-testnet test come from
``/home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v15_sim_testnet.py``
lines 387-405. Any accidental drift between that deployer and the
ResolvedParams type will surface here.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import cbor2
import pytest

from pycardano import (
    Address,
    RawCBOR,
    ScriptHash,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)


# ═════════════════════════════════════════════════════════════════════════
# Expected field order — mirrors contracts/lib/adversarial_auditing/params.ak
# lines 30-88. DO NOT reorder without updating params.ak in lockstep.
# ═════════════════════════════════════════════════════════════════════════

EXPECTED_FIELD_ORDER = (
    "min_claim_stake",
    "min_challenge_window",
    "max_challenge_window",
    "jury_size",
    "min_juror_bond",
    "jury_fee_rate",
    "selection_delay",
    "resolution_deadline",
    "juror_slash_rate",
    "min_agent_age",
    "max_concurrent_cases",
    "min_jury_pool_size",
    "min_jury_pool_total",
    "oracle_active",
    "commit_window",
    "reveal_window",
    "cleanup_buffer",
)

# v15 sim-testnet deploy values — mirror of
# /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v15_sim_testnet.py
# lines 387-405. `oracle_active` stored as CBORTag(121, []) on-chain, which
# per Plutus Bool convention decodes to False ("jury mode").
V15_SIM_VALUES = {
    "min_claim_stake":       50_000_000,
    "min_challenge_window":       60_000,
    "max_challenge_window":      300_000,
    "jury_size":                      5,
    "min_juror_bond":         25_000_000,
    "jury_fee_rate":               1_000,
    "selection_delay":            30_000,
    "resolution_deadline":       600_000,
    "juror_slash_rate":            1_000,
    "min_agent_age":          21_600_000,
    "max_concurrent_cases":           5,
    "min_jury_pool_size":            15,
    "min_jury_pool_total":   375_000_000,
    "oracle_active":              False,      # CBORTag(121, []) on-chain
    "commit_window":             180_000,
    "reveal_window":             180_000,
    "cleanup_buffer":             60_000,
}


# ═════════════════════════════════════════════════════════════════════════
# Helpers for building a stand-in DeploymentState with a params_utxo that
# carries a given CBORTag datum.
# ═════════════════════════════════════════════════════════════════════════


def _make_params_utxo_with_datum(datum_cbor_bytes: bytes | None) -> UTxO:
    """Build a UTxO whose .output.datum carries the given CBOR payload.

    Pass None to build a UTxO with no datum (exercises the missing-datum
    error path).
    """
    ti = TransactionInput(TransactionId(b"\x00" * 32), 0)
    # Any script-ish address works — the resolver never inspects it.
    script_hash = ScriptHash(b"\xaa" * 28)
    addr = Address(script_hash)
    if datum_cbor_bytes is None:
        to = TransactionOutput(addr, Value(coin=2_000_000))
    else:
        to = TransactionOutput(
            addr, Value(coin=2_000_000), datum=RawCBOR(datum_cbor_bytes),
        )
    return UTxO(ti, to)


class _FakeDeployment:
    """Minimal stand-in for DeploymentState exposing just params_utxo.

    Catherine's resolver reads deployment.params_utxo — it should NOT
    touch any other attribute during resolve. Missing params_utxo is
    exercised by setting the attribute to None.
    """

    def __init__(self, params_utxo: UTxO | None):
        self.params_utxo = params_utxo


def _build_full_cbor_tag(values: dict[str, Any]) -> bytes:
    """Encode a CBORTag(121, [...17 fields in canonical order...]) payload.

    Bool is encoded Plutus-style: False -> CBORTag(121, []), True ->
    CBORTag(122, []). All other fields are passed through as-is.
    """
    fields: list[Any] = []
    for name in EXPECTED_FIELD_ORDER:
        v = values[name]
        if name == "oracle_active":
            v = cbor2.CBORTag(122, []) if v else cbor2.CBORTag(121, [])
        fields.append(v)
    return cbor2.dumps(cbor2.CBORTag(121, fields))


# ═════════════════════════════════════════════════════════════════════════
# Dataclass shape
# ═════════════════════════════════════════════════════════════════════════


def test_resolved_params_is_importable():
    """ResolvedParams MUST be exposed at simulation.params.ResolvedParams.

    First RED fence — if the module doesn't exist at all, everything below
    fails with ImportError and this test gives the crispest signal.
    """
    from simulation.params import ResolvedParams  # noqa: F401
    assert ResolvedParams is not None


def test_resolve_protocol_params_is_importable():
    """resolve_protocol_params MUST be exposed as a callable."""
    from simulation.params import resolve_protocol_params
    assert callable(resolve_protocol_params)


def test_params_unavailable_is_importable():
    """ParamsUnavailable MUST be an exception class exported by
    simulation.params — callers catch it by name.
    """
    from simulation.params import ParamsUnavailable
    assert isinstance(ParamsUnavailable, type)
    assert issubclass(ParamsUnavailable, Exception)


def test_resolved_params_is_frozen_dataclass():
    """ResolvedParams must be a frozen dataclass (immutability — callers
    cache a single instance per deployment).
    """
    from simulation.params import ResolvedParams
    assert dataclasses.is_dataclass(ResolvedParams), (
        "ResolvedParams must be a dataclass"
    )
    # frozen=True sets this flag on the dataclass params.
    params = ResolvedParams.__dataclass_params__
    assert params.frozen is True, (
        "ResolvedParams must be declared with frozen=True so instances "
        "can be cached without accidental mutation"
    )


def test_resolved_params_has_17_fields_in_expected_order():
    """ResolvedParams MUST expose exactly the 17 fields from params.ak in
    that exact declaration order.
    """
    from simulation.params import ResolvedParams
    fields = dataclasses.fields(ResolvedParams)
    assert len(fields) == 17, (
        f"ResolvedParams must have 17 fields (one per ProtocolParams entry "
        f"in params.ak:30-88); got {len(fields)}"
    )
    names = tuple(f.name for f in fields)
    assert names == EXPECTED_FIELD_ORDER, (
        f"ResolvedParams field order must mirror params.ak. "
        f"Expected: {EXPECTED_FIELD_ORDER}\n"
        f"Got:      {names}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Happy-path decoding
# ═════════════════════════════════════════════════════════════════════════


def test_resolve_protocol_params_decodes_all_17_fields():
    """Given a hand-built CBORTag(121, [...17 fields...]) datum with
    distinct sentinel values per field, resolve_protocol_params must
    return a ResolvedParams with those values at the right field names.
    """
    from simulation.params import ResolvedParams, resolve_protocol_params

    # Distinct sentinel per Int field; False for the Bool (the False path
    # is the v15-sim + mainnet default).
    sentinels = {
        "min_claim_stake":        1001,
        "min_challenge_window":   1002,
        "max_challenge_window":   1003,
        "jury_size":              1004,
        "min_juror_bond":         1005,
        "jury_fee_rate":          1006,
        "selection_delay":        1007,
        "resolution_deadline":    1008,
        "juror_slash_rate":       1009,
        "min_agent_age":          1010,
        "max_concurrent_cases":   1011,
        "min_jury_pool_size":     1012,
        "min_jury_pool_total":    1013,
        "oracle_active":          False,
        "commit_window":          1015,
        "reveal_window":          1016,
        "cleanup_buffer":         1017,
    }
    datum_cbor = _build_full_cbor_tag(sentinels)
    dep = _FakeDeployment(_make_params_utxo_with_datum(datum_cbor))

    resolved = resolve_protocol_params(dep)
    assert isinstance(resolved, ResolvedParams)
    for name, expected in sentinels.items():
        got = getattr(resolved, name)
        assert got == expected, (
            f"field {name!r}: expected {expected!r}, got {got!r}"
        )


def test_resolve_protocol_params_int_fields_decode_to_python_ints():
    """Every integer field of ResolvedParams must decode as Python int
    (not a subclass, and certainly not bytes or CBORTag).
    """
    from simulation.params import resolve_protocol_params

    values = dict(V15_SIM_VALUES)
    datum_cbor = _build_full_cbor_tag(values)
    dep = _FakeDeployment(_make_params_utxo_with_datum(datum_cbor))

    resolved = resolve_protocol_params(dep)
    for name in EXPECTED_FIELD_ORDER:
        if name == "oracle_active":
            continue
        v = getattr(resolved, name)
        assert isinstance(v, int) and not isinstance(v, bool), (
            f"field {name!r}: expected plain Python int, got "
            f"{type(v).__name__} ({v!r})"
        )


def test_resolve_protocol_params_oracle_active_false_is_constr0():
    """Plutus Bool convention: CBORTag(121, []) = Constr0 = False.

    This is the v15 sim + mainnet default ("jury mode") — see
    deploy_and_run_v15_sim_testnet.py:401 comment.
    """
    from simulation.params import resolve_protocol_params
    values = dict(V15_SIM_VALUES, oracle_active=False)
    datum_cbor = _build_full_cbor_tag(values)
    dep = _FakeDeployment(_make_params_utxo_with_datum(datum_cbor))
    resolved = resolve_protocol_params(dep)
    assert resolved.oracle_active is False, (
        "CBORTag(121, []) at the Bool slot must decode to False "
        "(Plutus Constr0 convention). Got "
        f"{resolved.oracle_active!r}"
    )


def test_resolve_protocol_params_oracle_active_true_is_constr1():
    """Plutus Bool convention: CBORTag(122, []) = Constr1 = True
    ("oracle mode").
    """
    from simulation.params import resolve_protocol_params
    values = dict(V15_SIM_VALUES, oracle_active=True)
    datum_cbor = _build_full_cbor_tag(values)
    dep = _FakeDeployment(_make_params_utxo_with_datum(datum_cbor))
    resolved = resolve_protocol_params(dep)
    assert resolved.oracle_active is True, (
        "CBORTag(122, []) at the Bool slot must decode to True "
        "(Plutus Constr1 convention). Got "
        f"{resolved.oracle_active!r}"
    )


def test_resolve_protocol_params_matches_v15_sim_testnet_values():
    """Regression test: feeding the exact CBOR the v15 sim deployer writes
    (deploy_and_run_v15_sim_testnet.py:387-405) must reproduce the
    expected ResolvedParams field-by-field.

    If anyone edits the deployer without updating this test (or vice
    versa), the test surfaces the drift immediately.
    """
    from simulation.params import resolve_protocol_params

    datum_cbor = _build_full_cbor_tag(V15_SIM_VALUES)
    dep = _FakeDeployment(_make_params_utxo_with_datum(datum_cbor))
    resolved = resolve_protocol_params(dep)

    for name, expected in V15_SIM_VALUES.items():
        got = getattr(resolved, name)
        assert got == expected, (
            f"v15-sim drift at field {name!r}: deployer writes "
            f"{expected!r}, resolver returns {got!r}. If the deployer "
            f"changed on purpose, update V15_SIM_VALUES in this test "
            f"in the same PR."
        )


# ═════════════════════════════════════════════════════════════════════════
# Error paths
# ═════════════════════════════════════════════════════════════════════════


def test_resolve_protocol_params_raises_on_missing_params_utxo():
    """deployment.params_utxo is None -> ParamsUnavailable."""
    from simulation.params import ParamsUnavailable, resolve_protocol_params

    dep = _FakeDeployment(None)
    with pytest.raises(ParamsUnavailable):
        resolve_protocol_params(dep)


def test_resolve_protocol_params_raises_on_missing_datum():
    """params_utxo has no datum attached -> ParamsUnavailable."""
    from simulation.params import ParamsUnavailable, resolve_protocol_params

    dep = _FakeDeployment(_make_params_utxo_with_datum(None))
    with pytest.raises(ParamsUnavailable):
        resolve_protocol_params(dep)


def test_resolve_protocol_params_raises_on_wrong_constr_tag():
    """Outer CBORTag that is NOT 121 (e.g. 122) -> ParamsUnavailable.

    ProtocolParams is declared as a single-constructor type in params.ak
    so the only valid outer tag is 121 (Constr0). Anything else is
    structural corruption.
    """
    from simulation.params import ParamsUnavailable, resolve_protocol_params

    # Build a CBORTag(122, ...) with 17 otherwise-valid fields.
    fields = []
    for name in EXPECTED_FIELD_ORDER:
        v = V15_SIM_VALUES[name]
        if name == "oracle_active":
            v = cbor2.CBORTag(121, []) if v is False else cbor2.CBORTag(122, [])
        fields.append(v)
    bad_cbor = cbor2.dumps(cbor2.CBORTag(122, fields))
    dep = _FakeDeployment(_make_params_utxo_with_datum(bad_cbor))
    with pytest.raises(ParamsUnavailable):
        resolve_protocol_params(dep)


def test_resolve_protocol_params_raises_on_too_few_fields():
    """16 fields (one short) -> ParamsUnavailable."""
    from simulation.params import ParamsUnavailable, resolve_protocol_params

    values = dict(V15_SIM_VALUES)
    full_cbor = _build_full_cbor_tag(values)
    # Re-decode, drop the last field, re-encode.
    decoded = cbor2.loads(full_cbor)
    truncated = cbor2.dumps(cbor2.CBORTag(121, list(decoded.value)[:-1]))
    dep = _FakeDeployment(_make_params_utxo_with_datum(truncated))
    with pytest.raises(ParamsUnavailable):
        resolve_protocol_params(dep)


def test_resolve_protocol_params_raises_on_too_many_fields():
    """18 fields (one extra) -> ParamsUnavailable."""
    from simulation.params import ParamsUnavailable, resolve_protocol_params

    values = dict(V15_SIM_VALUES)
    full_cbor = _build_full_cbor_tag(values)
    decoded = cbor2.loads(full_cbor)
    extended = cbor2.dumps(
        cbor2.CBORTag(121, list(decoded.value) + [0xDEAD])
    )
    dep = _FakeDeployment(_make_params_utxo_with_datum(extended))
    with pytest.raises(ParamsUnavailable):
        resolve_protocol_params(dep)
