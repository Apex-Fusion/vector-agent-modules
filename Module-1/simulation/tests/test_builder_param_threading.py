"""
RED tests for Option-A ProtocolParams threading through every builder in
``simulation.tx_builder``.

Contract under test:
    Catherine will add a new kwarg ``resolved_params: ResolvedParams | None``
    to each affected builder. When None and a param-tied value is needed,
    the builder must look it up via ``deployment.resolved_params`` (a new
    lazy property on ``DeploymentState``). When provided explicitly, the
    stub's values must thread into the TransactionBuilder's datum / ttl
    / timing fields — **replacing** the current hardcoded defaults.

Affected builders (from Catherine's audit):
    build_submit_claim        challenge_window_ms  <- pick in range of
                                                     (min, max) challenge_window
    build_transition_to_voting selection_delay_ms  <- resolved.selection_delay
    build_commit_vote          commit_window_ms    <- resolved.commit_window
    build_reveal_vote          commit/reveal_window_ms -> resolved.*
    build_resolve_jury         jury_fee_rate       <- resolved.jury_fee_rate
    build_distribute_rewards   jury_fee_rate       <- resolved.jury_fee_rate
    build_cleanup_resolved     resolution_deadline/cleanup_buffer_ms

These tests will all RED on the current codebase because:
    (a) ``simulation.params`` / ResolvedParams does not exist yet, OR
    (b) the builders do not yet accept ``resolved_params`` as a kwarg.

They become GREEN once Catherine lands Option A end-to-end.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import cbor2
import pytest

from pycardano import TransactionBuilder


# ═════════════════════════════════════════════════════════════════════════
# Sentinel stub — distinct values per field so threading is unambiguous.
# ═════════════════════════════════════════════════════════════════════════

SENTINELS = {
    "min_claim_stake":       77_000_001,
    "min_challenge_window":       91_234,
    "max_challenge_window":      199_876,
    "jury_size":                       5,   # keep validator-plausible for fixtures
    "min_juror_bond":         26_000_001,
    "jury_fee_rate":              1_500,   # 15% — differs from default 1000
    "selection_delay":           88_888,
    "resolution_deadline":      555_555,
    "juror_slash_rate":            1_111,
    "min_agent_age":          12_345_678,
    "max_concurrent_cases":            5,
    "min_jury_pool_size":             15,
    "min_jury_pool_total":   380_000_000,
    "oracle_active":               False,
    "commit_window":              99_999,
    "reveal_window":              77_777,
    "cleanup_buffer":             33_333,
}


@pytest.fixture
def stub_resolved_params():
    """A ResolvedParams instance with distinct sentinel values per field.

    Requires simulation.params.ResolvedParams to exist; collection-time
    ImportError is the RED signal when it doesn't.
    """
    from simulation.params import ResolvedParams
    return ResolvedParams(**SENTINELS)


def _patch_resolve_everywhere(monkeypatch, stub):
    """Patch resolve_protocol_params at every likely import site so
    DeploymentState.resolved_params (whose implementation detail is
    Catherine's) sees the stub regardless of where she imports the
    resolver. Safe to call even before Catherine lands her code:
    non-existent attributes are ignored.
    """
    import simulation.params as params_mod
    monkeypatch.setattr(
        params_mod, "resolve_protocol_params", lambda dep: stub,
        raising=False,
    )
    # Also patch any re-imports into sibling modules.
    import simulation.tx_builder as txb_mod
    if hasattr(txb_mod, "resolve_protocol_params"):
        monkeypatch.setattr(
            txb_mod, "resolve_protocol_params", lambda dep: stub,
            raising=False,
        )


# ═════════════════════════════════════════════════════════════════════════
# Deployment.resolved_params property
# ═════════════════════════════════════════════════════════════════════════


def test_deployment_state_has_resolved_params_property(
    sample_deployment, monkeypatch, stub_resolved_params,
):
    """DeploymentState must expose a ``resolved_params`` attribute/property.

    Catherine's design makes it a lazy property backed by
    resolve_protocol_params(self) — from the test's perspective, it
    must (a) exist and (b) yield the stub value when the resolver is
    patched to return it.
    """
    _patch_resolve_everywhere(monkeypatch, stub_resolved_params)
    assert hasattr(sample_deployment, "resolved_params"), (
        "DeploymentState must expose a `resolved_params` attribute "
        "(Option A lazy property — see Catherine's design)."
    )
    got = sample_deployment.resolved_params
    assert got is stub_resolved_params or got == stub_resolved_params, (
        f"DeploymentState.resolved_params should resolve to the "
        f"stubbed ResolvedParams; got {got!r}"
    )


def test_deployment_state_resolved_params_is_cached(
    sample_deployment, monkeypatch, stub_resolved_params,
):
    """Repeated access to ``.resolved_params`` MUST NOT re-run
    resolve_protocol_params — the brief says "one call per deployment
    instance" (avoids O(N) Ogmios round-trips across a scenario run).
    """
    import simulation.params as params_mod

    call_count = {"n": 0}

    def _counting_resolver(dep):
        call_count["n"] += 1
        return stub_resolved_params

    monkeypatch.setattr(
        params_mod, "resolve_protocol_params", _counting_resolver, raising=False,
    )
    import simulation.tx_builder as txb_mod
    if hasattr(txb_mod, "resolve_protocol_params"):
        monkeypatch.setattr(
            txb_mod, "resolve_protocol_params", _counting_resolver, raising=False,
        )

    # Access twice.
    _ = sample_deployment.resolved_params
    _ = sample_deployment.resolved_params
    assert call_count["n"] <= 1, (
        f"resolve_protocol_params must be called at most once per "
        f"DeploymentState instance; got {call_count['n']} calls across "
        f"2 property reads"
    )


# ═════════════════════════════════════════════════════════════════════════
# build_submit_claim — challenge_window_ms from max_challenge_window
# ═════════════════════════════════════════════════════════════════════════


def _decode_datum_fields(output) -> list:
    datum = output.datum
    raw = datum.cbor if hasattr(datum, "cbor") else bytes(datum)
    decoded = cbor2.loads(raw)
    return list(decoded.value)


def test_build_submit_claim_threads_resolved_params_challenge_window(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, stub_resolved_params,
):
    """When ``resolved_params`` is threaded in, build_submit_claim's
    ClaimDatum field[7] (challenge_window) must pick a value in
    [min_challenge_window, max_challenge_window]. The brief suggests
    80% of max, but any in-range value is acceptable.

    This test explicitly does NOT pass challenge_window_ms as a kwarg —
    the resolved_params path must be the source of truth.
    """
    from simulation.tx_builder import build_submit_claim

    skey, vkey, wallet_addr = sample_wallet
    build_submit_claim(
        mock_ogmios_context,
        sample_deployment,
        skey, vkey, wallet_addr,
        sample_did_hex,
        50_000_000,
        claim_hash=sample_claim_hash,
        claim_type=b"data_indexing",
        storage_uri=b"ipfs://test-claim-cid",
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_submit_claim never reached build_and_sign"
    builder = captured_builder[-1]

    # Locate the claim output (the one at the claim script addr).
    from simulation.tests.conftest import V13_DEPLOYMENT
    from pycardano import Address
    claim_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"])
    claim_out = None
    for o in builder.outputs:
        if o.address == claim_addr:
            claim_out = o
            break
    assert claim_out is not None, "no output at claim script address"

    fields = _decode_datum_fields(claim_out)
    field_7 = fields[7]
    assert isinstance(field_7, int), (
        f"ClaimDatum field[7] must be Int; got {type(field_7).__name__}"
    )
    # Must be in [min_challenge_window, max_challenge_window] — NOT the
    # previous hardcoded 1_800_000 default.
    assert stub_resolved_params.min_challenge_window <= field_7 <= \
        stub_resolved_params.max_challenge_window, (
            f"ClaimDatum field[7] challenge_window={field_7} must lie in "
            f"[{stub_resolved_params.min_challenge_window}, "
            f"{stub_resolved_params.max_challenge_window}] "
            f"(from resolved_params). This fails if the builder still "
            f"uses the hardcoded 1_800_000 ms default."
        )
    # Sanity: must NOT equal the stale hardcoded default that builder
    # previously baked in unless it happens to lie inside the stub
    # range (our sentinel range excludes 1_800_000 by construction).
    assert field_7 != 1_800_000, (
        "ClaimDatum field[7] still equals the hardcoded 1_800_000 ms "
        "default. Threading resolved_params has not taken effect."
    )


# ═════════════════════════════════════════════════════════════════════════
# build_transition_to_voting — selection_delay_ms from resolved_params
# ═════════════════════════════════════════════════════════════════════════


def test_build_transition_to_voting_threads_resolved_params_selection_delay(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size,
    stub_resolved_params,
):
    """selection_delay must flow from resolved_params, not the kwarg
    default. The validity_start slot must be > (challenged_at +
    stub.selection_delay) — i.e. computed from the stub value.
    """
    from simulation.tx_builder import build_transition_to_voting, SYSTEM_START_UNIX

    skey, vkey, wallet_addr = sample_auditor_wallet
    chal_ref = (
        f"{bytes(sample_challenge_utxo_pending_jury.input.transaction_id).hex()}"
        f"#{sample_challenge_utxo_pending_jury.input.index}"
    )

    build_transition_to_voting(
        mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        skey, vkey, wallet_addr,
        chal_ref,
        jury_size=default_jury_size,
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_transition_to_voting never reached build_and_sign"
    builder = captured_builder[-1]

    # challenged_at from the input datum
    input_datum = sample_challenge_utxo_pending_jury.output.datum
    raw = input_datum.cbor if hasattr(input_datum, "cbor") else bytes(input_datum)
    input_fields = list(cbor2.loads(raw).value)
    challenged_at_ms = input_fields[6]

    vs_ms = (SYSTEM_START_UNIX + builder.validity_start) * 1000
    stub_deadline_ms = challenged_at_ms + stub_resolved_params.selection_delay
    assert vs_ms > stub_deadline_ms, (
        f"validity_start_ms={vs_ms} must be > challenged_at+stub.selection_delay"
        f"={stub_deadline_ms} (stub.selection_delay="
        f"{stub_resolved_params.selection_delay}). If this still passes "
        f"against the hardcoded 30_000 default, the sentinel value may "
        f"accidentally match; our SENTINELS keep them distinct."
    )
    # Sanity: if the builder fell back to the hardcoded 30_000 default
    # instead of using the stub's 88_888, vs_ms would be LESS than
    # challenged_at + stub.selection_delay (because stub > default).
    default_deadline_ms = challenged_at_ms + 30_000
    assert stub_deadline_ms > default_deadline_ms, (
        "stub.selection_delay must exceed the hardcoded 30_000 default "
        "so this test is sensitive to the threading (test fixture bug "
        "if this fires)"
    )


# ═════════════════════════════════════════════════════════════════════════
# build_commit_vote — commit_window_ms from resolved_params
# ═════════════════════════════════════════════════════════════════════════


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def test_build_commit_vote_threads_resolved_params_commit_window(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    stub_resolved_params,
):
    """commit_window must flow from resolved_params. The builder caps
    its ttl so (ttl * 1000 + SYSTEM_START_UNIX * 1000) is strictly less
    than (challenged_at + commit_window) — so the stub's commit_window
    must drive the commit_deadline_slot the builder reports in its
    result dict.
    """
    from simulation.tx_builder import build_commit_vote, SYSTEM_START_UNIX

    skey, vkey, wallet_addr = sample_juror_wallet
    result = build_commit_vote(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        _utxo_ref_str(sample_juror_utxo_assigned),
        _utxo_ref_str(sample_challenge_utxo_voting),
        0x00,
        salt=b"\x11" * 32,
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_commit_vote never reached build_and_sign"

    # challenged_at from input challenge datum
    ch_datum = sample_challenge_utxo_voting.output.datum
    raw = ch_datum.cbor if hasattr(ch_datum, "cbor") else bytes(ch_datum)
    challenged_at_ms = list(cbor2.loads(raw).value)[6]

    expected_commit_deadline_ms = challenged_at_ms + stub_resolved_params.commit_window
    expected_slot = (expected_commit_deadline_ms // 1000) - SYSTEM_START_UNIX
    assert result["commit_deadline_slot"] == expected_slot, (
        f"commit_deadline_slot={result['commit_deadline_slot']} must be "
        f"derived from stub.commit_window={stub_resolved_params.commit_window}. "
        f"Expected slot={expected_slot}. "
        f"If still passing with hardcoded 1_800_000 default, the test "
        f"sentinel may collide; SENTINELS keep them distinct."
    )


# ═════════════════════════════════════════════════════════════════════════
# build_reveal_vote — commit_window / reveal_window from resolved_params
# ═════════════════════════════════════════════════════════════════════════


def test_build_reveal_vote_threads_resolved_params_windows(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    stub_resolved_params,
    monkeypatch,
):
    """reveal_deadline_slot must derive from challenged_at + commit_window
    + reveal_window — both from the stub, not the hardcoded defaults.

    The builder must also validate that current_slot > commit_deadline
    (commit_window from stub) and current_slot < reveal_deadline. The
    reveal fixture's `challenged_at` is calibrated to the 30-min default
    windows, so with the stub's sub-second windows the fixture slot
    would miss the reveal-open interval. Patch mock_ogmios_context._slot
    to land inside the stub-implied window.
    """
    from simulation.tx_builder import build_reveal_vote, SYSTEM_START_UNIX

    # Extract challenged_at from the fixture's challenge datum.
    ch_datum = sample_challenge_utxo_voting_for_reveal.output.datum
    raw = ch_datum.cbor if hasattr(ch_datum, "cbor") else bytes(ch_datum)
    challenged_at_ms = list(cbor2.loads(raw).value)[6]

    expected_commit_deadline_ms = (
        challenged_at_ms + stub_resolved_params.commit_window
    )
    expected_reveal_deadline_ms = (
        expected_commit_deadline_ms + stub_resolved_params.reveal_window
    )
    expected_commit_slot = (expected_commit_deadline_ms // 1000) - SYSTEM_START_UNIX
    expected_reveal_slot = (expected_reveal_deadline_ms // 1000) - SYSTEM_START_UNIX

    # Slot inside the reveal window — strictly after commit_deadline, strictly
    # before reveal_deadline.
    target_slot = (expected_commit_slot + expected_reveal_slot) // 2
    monkeypatch.setattr(mock_ogmios_context, "_slot", target_slot)

    skey, vkey, wallet_addr = sample_juror_wallet
    result = build_reveal_vote(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        _utxo_ref_str(sample_juror_utxo_with_known_commitment),
        _utxo_ref_str(sample_challenge_utxo_voting_for_reveal),
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_reveal_vote never reached build_and_sign"

    assert result["commit_deadline_slot"] == expected_commit_slot, (
        f"commit_deadline_slot={result['commit_deadline_slot']} must be "
        f"derived from stub.commit_window={stub_resolved_params.commit_window}. "
        f"Expected slot={expected_commit_slot}"
    )
    assert result["reveal_deadline_slot"] == expected_reveal_slot, (
        f"reveal_deadline_slot={result['reveal_deadline_slot']} must be "
        f"derived from stub.reveal_window={stub_resolved_params.reveal_window}. "
        f"Expected slot={expected_reveal_slot}"
    )


# ═════════════════════════════════════════════════════════════════════════
# build_resolve_jury — jury_fee_rate from resolved_params
# ═════════════════════════════════════════════════════════════════════════


def test_build_resolve_jury_threads_resolved_params_fee_rate(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size,
    stub_resolved_params,
):
    """jury_fee for a ClaimerWins verdict should equal
    auditor_stake * stub.jury_fee_rate / 10000.
    The stub's 1500 (15%) must replace the baked-in 1000 (10%) default.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    juror_refs = [
        _utxo_ref_str(u) for u in sample_revealed_juror_utxos_claimer_wins
    ]
    result = build_resolve_jury(
        mock_ogmios_context,
        sample_deployment_with_all_refs,
        skey, vkey, wallet_addr,
        _utxo_ref_str(sample_challenge_utxo_voting),
        _utxo_ref_str(sample_claim_utxo_challenged),
        juror_refs,
        jury_size=default_jury_size,
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_resolve_jury never reached build_and_sign"

    # Extract auditor_stake from the challenge datum (field[3]).
    ch_raw = sample_challenge_utxo_voting.output.datum
    raw_bytes = ch_raw.cbor if hasattr(ch_raw, "cbor") else bytes(ch_raw)
    auditor_stake = int(list(cbor2.loads(raw_bytes).value)[3])

    # For a ClaimerWins verdict, jury_fee = auditor_stake * rate // 10000
    expected_fee = auditor_stake * stub_resolved_params.jury_fee_rate // 10000
    assert result["jury_fee"] == expected_fee, (
        f"jury_fee={result['jury_fee']} must equal auditor_stake "
        f"({auditor_stake}) * stub.jury_fee_rate "
        f"({stub_resolved_params.jury_fee_rate}) // 10000 = "
        f"{expected_fee}. If it still matches the 10% (rate 1000) "
        f"default, resolved_params is not threading."
    )


# ═════════════════════════════════════════════════════════════════════════
# build_distribute_rewards — jury_fee_rate from resolved_params
# ═════════════════════════════════════════════════════════════════════════


def test_build_distribute_rewards_threads_resolved_params_fee_rate(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size,
    stub_resolved_params,
):
    """fee_per_juror formula (Phase-1.0):
        fee_per_juror = challenge_stake * jury_fee_rate / 10000 / jury_size
    With the stub threaded in, the rate must come from the stub (1500),
    not the hardcoded 1000.
    """
    from simulation.tx_builder import build_distribute_rewards

    skey, vkey, wallet_addr = sample_juror_wallet
    result = build_distribute_rewards(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        _utxo_ref_str(sample_juror_utxo_revealed_for_distribute),
        _utxo_ref_str(sample_resolved_challenge_utxo),
        jury_size=default_jury_size,
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_distribute_rewards never reached build_and_sign"

    ch_raw = sample_resolved_challenge_utxo.output.datum
    raw_bytes = ch_raw.cbor if hasattr(ch_raw, "cbor") else bytes(ch_raw)
    challenge_stake = int(list(cbor2.loads(raw_bytes).value)[3])

    expected_fee = (
        challenge_stake
        * stub_resolved_params.jury_fee_rate
        // 10000
        // default_jury_size
    )
    assert result["fee_per_juror"] == expected_fee, (
        f"fee_per_juror={result['fee_per_juror']} must derive from stub's "
        f"jury_fee_rate ({stub_resolved_params.jury_fee_rate}), not the "
        f"hardcoded 1000 default. Expected {expected_fee}."
    )


# ═════════════════════════════════════════════════════════════════════════
# build_cleanup_resolved — resolution_deadline + cleanup_buffer from
# resolved_params
# ═════════════════════════════════════════════════════════════════════════


def test_build_cleanup_resolved_threads_resolved_params_cleanup_buffer(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    stub_resolved_params,
    monkeypatch,
):
    """The validity_start slot in the built tx must be > cleanup_after_slot
    where cleanup_after_ms = challenged_at + resolution_deadline +
    cleanup_buffer — all three pulled from the stub.

    For the fixture's current_slot to be past that cutoff, we patch the
    fake context's `last_block_slot` to sit comfortably after the
    stub-implied cutoff.
    """
    from simulation.tx_builder import build_cleanup_resolved, SYSTEM_START_UNIX

    # The resolved-challenge fixture's datum carries challenged_at in
    # field[6] and resolution_deadline in field[7]. We'll drive the
    # builder's resolution_deadline via the stub, not the datum — the
    # brief says the builder uses resolved_params for its internal
    # pre-flight math, not the datum's authoritative value.
    ch_raw = sample_resolved_challenge_utxo_for_cleanup.output.datum
    raw_bytes = ch_raw.cbor if hasattr(ch_raw, "cbor") else bytes(ch_raw)
    challenged_at_ms = int(list(cbor2.loads(raw_bytes).value)[6])

    cleanup_after_ms = (
        challenged_at_ms
        + stub_resolved_params.resolution_deadline
        + stub_resolved_params.cleanup_buffer
    )
    cleanup_after_slot = (cleanup_after_ms // 1000) - SYSTEM_START_UNIX
    # Give the mock context a current_slot a few minutes past that cutoff
    # so the builder doesn't raise the "cleanup not yet" ValueError.
    monkeypatch.setattr(
        mock_ogmios_context, "_slot", cleanup_after_slot + 120,
    )

    chal_ref = _utxo_ref_str(sample_resolved_challenge_utxo_for_cleanup)
    build_cleanup_resolved(
        mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        skey=sample_auditor_wallet[0],
        vkey=sample_auditor_wallet[1],
        wallet_addr=sample_auditor_wallet[2],
        resolved_challenge_utxo_ref=chal_ref,
        resolved_params=stub_resolved_params,
    )
    assert captured_builder, "build_cleanup_resolved never reached build_and_sign"
    builder = captured_builder[-1]

    assert builder.validity_start > cleanup_after_slot, (
        f"validity_start={builder.validity_start} must be > "
        f"cleanup_after_slot={cleanup_after_slot} derived from stub "
        f"resolution_deadline={stub_resolved_params.resolution_deadline} "
        f"+ cleanup_buffer={stub_resolved_params.cleanup_buffer}. If "
        f"the builder still uses the hardcoded 259_200_000 + 600_000 "
        f"defaults, the computed cutoff differs and this fails."
    )
