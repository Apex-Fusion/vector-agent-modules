"""
RED-phase tests for `simulation.tx_builder.build_transition_to_voting`.

Function under test (NOT YET IMPLEMENTED):
    build_transition_to_voting(
        context, deployment,
        skey, vkey, wallet_addr,
        challenge_utxo_ref: str,           # "<txid>#<idx>" of PendingJury UTxO
        *,
        jury_size: int = 5,
        selection_delay_ms: int = 30_000,
    ) -> dict

Contract reference — researched by reading the validator directly:

    contracts/lib/adversarial_auditing/types.ak
        ChallengeAction variants (Constr index = CBOR tag):
            OpenChallenge         Constr0 -> CBORTag(121, [])
            SubmitEvidence        Constr1 -> CBORTag(122, [...])
            OracleResolve         Constr2 -> CBORTag(123, [...])
            ResolveJury           Constr3 -> CBORTag(124, [])
            TimeoutResolve        Constr4 -> CBORTag(125, [])
            CleanupResolved       Constr5 -> CBORTag(126, [])
            TransitionToVoting    Constr6 -> CBORTag(127, [selected_jurors])

        ChallengeState variants:
            PendingOracle  Constr0 -> CBORTag(121, [])
            PendingJury    Constr1 -> CBORTag(122, [])
            Voting         Constr2 -> CBORTag(123, [selected_jurors])
            Resolved       Constr3 -> CBORTag(124, [verdict])

    contracts/validators/challenge.ak :: validate_transition_to_voting
    (lines 739-823) — full invariant list:

        1. ch.state must be PendingJury (line 750-753).
        2. tx_started_after(ch.challenged_at + params.selection_delay) —
           validity_start must be > challenged_at + selection_delay
           (line 757-758).
        3. PRNG-deterministic selection: the validator recomputes
           select_jurors_prng(challenge_token_name, ch.eligible_jurors,
           params.jury_size) and requires
           sort_dids(selected_jurors) == sort_dids(expected)
           (lines 769-775).  Client CANNOT pick an arbitrary subset of
           eligible_jurors — the selection is fixed by the challenge
           token name (the PRNG seed, set at OpenChallenge time).
        4. Continuing output at challenge script addr preserves fields
           0-8 byte-for-byte; only field[9] flips to
           Voting { selected_jurors } (lines 778-810).
        5. Challenge NFT preserved in continuing output (qty == 1)
           (lines 798-802).
        6. AP3X stake preserved (coin == ch.stake_amount) (line 804).
        7. Exactly one output at the challenge script address
           (line 812-814).
        8. NO ORACLE SIGNATURE REQUIRED — this is a departure from
           earlier plans.  Phase 1.1 made TransitionToVoting
           PERMISSIONLESS; the comment at challenge.ak:755 reads
           "permissionless — anyone can trigger after selection_delay".
           v13's step5b_transition_to_voting sets required_signers =
           [wallet_vkh] only because the wallet is paying fees — it is
           NOT a validator requirement.

Reference implementation we mirror:
    /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
    lines 1015-1097 (step5b_transition_to_voting).  The PRNG helper is
    defined at lines 1002-1012.

All tests in this file MUST fail against the current tree because
`build_transition_to_voting` does NOT yet exist in `tx_builder.py`.
The RED signal is `ImportError` on the function; Catherine implements
the function to turn the suite GREEN.

Three tests specifically enforce CLIENT-SIDE GUARDS — not on-chain
checks — so the builder fails fast instead of submitting a doomed TX:
    - test_selected_jurors_subset_of_eligible_raises_if_not
      (builder must refuse if caller somehow ended up with a
      selected_juror outside the eligible snapshot)
    - test_selected_jurors_count_matches_jury_size
      (builder must refuse if the PRNG-selected count diverges from
      jury_size — guard against eligible_jurors being shorter than
      jury_size)
    - test_challenge_state_must_be_PendingJury_or_raises
      (builder must refuse if the challenge UTxO it was pointed at
      has moved past PendingJury — e.g. already Voting or Resolved)
"""
from __future__ import annotations

import hashlib

import cbor2
import pytest

from pycardano import (
    Address,
    TransactionBuilder,
)


# ═════════════════════════════════════════════════════════════════════════
# Helpers (local to this test file; mirror test_open_challenge.py style)
# ═════════════════════════════════════════════════════════════════════════

def _challenge_output(builder: TransactionBuilder):
    """Return the builder's single continuing output at challenge addr."""
    from simulation.tests.conftest import V13_DEPLOYMENT
    chal_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    hits = [o for o in builder.outputs if o.address == chal_addr]
    assert len(hits) == 1, (
        f"Expected exactly ONE continuing output at challenge script addr "
        f"{chal_addr} (validator requires single_challenge_output, "
        f"challenge.ak:813-814); got {len(hits)}. All outputs: "
        f"{[str(o.address) for o in builder.outputs]}"
    )
    return hits[0]


def _decode_datum(output):
    """Unpack the CBORTag-wrapped Plutus datum on a TransactionOutput."""
    datum = output.datum
    if hasattr(datum, "cbor"):
        raw = datum.cbor
    else:
        raw = bytes(datum)
    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag"), f"Datum outer is not a CBORTag: {decoded!r}"
    return decoded


def _collect_spend_redeemers_for_input(builder: TransactionBuilder, utxo):
    """Return the spend Redeemer attached to the given challenge UTxO.

    Copies the cross-version attribute-probing strategy from
    test_open_challenge.py to survive PyCardano's internal shape drift.
    """
    target_key = (bytes(utxo.input.transaction_id), utxo.input.index)
    for attr in ("_inputs_to_redeemers", "_input_to_redeemer",
                 "_script_to_redeemers"):
        mapping = getattr(builder, attr, None)
        if mapping is None:
            continue
        if hasattr(mapping, "items"):
            for k, v in mapping.items():
                k_inp = k.input if hasattr(k, "input") else k
                if hasattr(k_inp, "transaction_id"):
                    key = (bytes(k_inp.transaction_id), k_inp.index)
                    if key == target_key:
                        return v
    for attr in ("_inputs_to_redeemers", "_script_to_redeemers"):
        val = getattr(builder, attr, None)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, tuple) and len(item) == 2:
                    k, v = item
                    k_inp = k.input if hasattr(k, "input") else k
                    if hasattr(k_inp, "transaction_id"):
                        key = (bytes(k_inp.transaction_id), k_inp.index)
                        if key == target_key:
                            return v
    return None


def _run_build_transition(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
    *,
    challenge_utxo_ref_override: str | None = None,
):
    """Invoke build_transition_to_voting with canonical fixture inputs."""
    from simulation.tx_builder import build_transition_to_voting

    skey, vkey, wallet_addr = sample_auditor_wallet
    if challenge_utxo_ref_override is not None:
        chal_ref = challenge_utxo_ref_override
    else:
        chal_ref = (
            f"{bytes(sample_challenge_utxo_pending_jury.input.transaction_id).hex()}"
            f"#{sample_challenge_utxo_pending_jury.input.index}"
        )

    result = build_transition_to_voting(
        mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        skey, vkey, wallet_addr,
        chal_ref,
        jury_size=default_jury_size,
        selection_delay_ms=default_selection_delay_ms,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_transition_to_voting did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# Datum correctness (continuing output)
# ═════════════════════════════════════════════════════════════════════════

def test_challenge_datum_preserves_fields_0_through_8(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
):
    """Continuing output's datum fields[0..8] MUST be byte-identical to
    the input ChallengeDatum.  Validator enforces this field-by-field
    (challenge.ak:788-796)."""
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    input_fields = _decode_datum(sample_challenge_utxo_pending_jury.output).value
    output_fields = _decode_datum(_challenge_output(builder)).value
    assert len(input_fields) == 10, (
        f"Input ChallengeDatum should have 10 fields; got {len(input_fields)}"
    )
    assert len(output_fields) == 10, (
        f"Continuing output datum must have 10 fields; got {len(output_fields)}"
    )
    for idx in range(9):
        assert output_fields[idx] == input_fields[idx], (
            f"Field[{idx}] must be preserved across TransitionToVoting "
            f"(challenge.ak:788-796). Input: {input_fields[idx]!r}. "
            f"Output: {output_fields[idx]!r}"
        )


def test_challenge_datum_state_at_index_9_is_Voting_with_selected_jurors(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_selected_jurors,
    default_jury_size, default_selection_delay_ms,
):
    """Field[9] (state) MUST become Voting { selected_jurors } —
    ChallengeState::Voting is Constr2 → CBORTag(123, [selected_jurors]).
    Per validator line 786: updated.state == Voting { selected_jurors }.
    """
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    output_fields = _decode_datum(_challenge_output(builder)).value
    state = output_fields[9]
    assert hasattr(state, "tag") and state.tag == 123, (
        f"Field[9] state must be CBORTag(123, [selected_jurors]) = "
        f"ChallengeState::Voting (Constr2); got {state!r}"
    )
    assert isinstance(state.value, list) and len(state.value) == 1, (
        f"Voting variant payload must be [selected_jurors]; "
        f"got {state.value!r}"
    )
    payload = state.value[0]
    assert isinstance(payload, list), (
        f"Voting.selected_jurors must be a list of PolicyIds; "
        f"got {payload!r}"
    )
    # selected_jurors must match the PRNG-derived selection (Catherine
    # must recompute select_jurors_prng from the consumed token name).
    assert sorted(payload) == sorted(sample_selected_jurors), (
        f"selected_jurors must match the deterministic PRNG output. "
        f"Expected (sorted) {[b.hex() for b in sorted(sample_selected_jurors)]}; "
        f"got (sorted) {[b.hex() if isinstance(b, (bytes, bytearray)) else b for b in sorted(payload)]}"
    )


def test_challenge_datum_selected_jurors_count_equals_jury_size(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
):
    """The Voting variant must carry exactly `jury_size` DIDs — validator
    invariant is implicit via the PRNG equality check (the PRNG always
    yields `jury_size` entries when eligible >= jury_size)."""
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    output_fields = _decode_datum(_challenge_output(builder)).value
    selected = output_fields[9].value[0]
    assert len(selected) == default_jury_size, (
        f"Voting.selected_jurors length must equal jury_size "
        f"({default_jury_size}); got {len(selected)}"
    )


def test_challenge_datum_selected_jurors_are_subset_of_eligible(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_eligible_jurors,
    default_jury_size, default_selection_delay_ms,
):
    """Every DID in Voting.selected_jurors must appear in the
    eligible_jurors snapshot (field[8]).  The validator enforces this
    indirectly — the PRNG draws from `ch.eligible_jurors` — but the
    constructed datum should trivially witness the subset property."""
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    output_fields = _decode_datum(_challenge_output(builder)).value
    selected = [bytes(x) for x in output_fields[9].value[0]]
    eligible_set = {bytes(x) for x in sample_eligible_jurors}
    for did in selected:
        assert did in eligible_set, (
            f"Selected juror {did.hex()} is NOT in eligible_jurors snapshot "
            f"— would violate PRNG invariant (challenge.ak:774)"
        )


def test_challenge_datum_preserves_eligible_jurors_list(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
):
    """Field[8] (eligible_jurors snapshot) must be byte-identical
    between input and continuing output (challenge.ak:796).  Separate
    test from the fields-0-8 loop because this list is the load-bearing
    subset-of-which-we-sample invariant."""
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    input_fields = _decode_datum(sample_challenge_utxo_pending_jury.output).value
    output_fields = _decode_datum(_challenge_output(builder)).value
    in_elig = [bytes(x) for x in input_fields[8]]
    out_elig = [bytes(x) for x in output_fields[8]]
    assert out_elig == in_elig, (
        f"eligible_jurors (field[8]) must be preserved. "
        f"Input head: {[b.hex()[:12] for b in in_elig[:3]]}... "
        f"Output head: {[b.hex()[:12] for b in out_elig[:3]]}..."
    )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer correctness
# ═════════════════════════════════════════════════════════════════════════

def test_spend_redeemer_is_TransitionToVoting_with_selected_jurors(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_selected_jurors,
    default_jury_size, default_selection_delay_ms,
):
    """The spend redeemer on the challenge UTxO input MUST be
    CBORTag(127, [selected_jurors]) = ChallengeAction::TransitionToVoting
    (Constr6).  Counted from types.ak:144-170:
        OpenChallenge(0), SubmitEvidence(1), OracleResolve(2),
        ResolveJury(3), TimeoutResolve(4), CleanupResolved(5),
        TransitionToVoting(6)
    Alonzo constr encoding: Constr6 uses general tag (102) with [index,
    fields] OR a dedicated tag. Constr0..6 map to tags 121..127; Constr7+
    map to 1280 + (n - 7). So 6 -> 127.
    """
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    redeemer = _collect_spend_redeemers_for_input(
        builder, sample_challenge_utxo_pending_jury,
    )
    assert redeemer is not None, (
        "Could not locate a spend redeemer attached to the challenge "
        "UTxO input. build_transition_to_voting MUST call "
        "builder.add_script_input(challenge_utxo, ref, redeemer=...)"
    )
    # Redeemer wraps a PlutusData-serialisable object (RawCBOR, or
    # CBORTag directly). Normalise to bytes, then decode.
    data = redeemer.data if hasattr(redeemer, "data") else redeemer
    if hasattr(data, "cbor"):
        raw = bytes(data.cbor)
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        # PyCardano may hand back a PlutusData subclass — serialise.
        raw = cbor2.dumps(data)
    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag") and decoded.tag == 127, (
        f"Spend redeemer must be CBORTag(127, [selected_jurors]) = "
        f"ChallengeAction::TransitionToVoting (Constr6); got {decoded!r}"
    )
    assert isinstance(decoded.value, list) and len(decoded.value) == 1, (
        f"Redeemer payload must be [selected_jurors]; got {decoded.value!r}"
    )
    payload = [bytes(x) for x in decoded.value[0]]
    assert sorted(payload) == sorted(sample_selected_jurors), (
        f"Redeemer selected_jurors must match the PRNG-derived set. "
        f"Expected (sorted) {[b.hex()[:12] for b in sorted(sample_selected_jurors)]}; "
        f"got (sorted) {[b.hex()[:12] for b in sorted(payload)]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Value preservation (NFT + stake)
# ═════════════════════════════════════════════════════════════════════════

def test_continuing_challenge_output_preserves_nft_and_stake(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_challenge_token_bytes,
    default_stake_amount,
    default_jury_size, default_selection_delay_ms,
):
    """The continuing output MUST carry:
        - Challenge NFT (challenge_policy, challenge_token_name) qty=1
        - coin == input stake_amount (Path B — stake is base lovelace)
    Per validator lines 798-804."""
    from simulation.tests.conftest import V13_DEPLOYMENT
    from pycardano import AssetName, ScriptHash

    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    out = _challenge_output(builder)

    assert out.amount.coin == default_stake_amount, (
        f"Continuing output coin MUST equal stake_amount "
        f"({default_stake_amount}); got {out.amount.coin}"
    )

    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    ma = out.amount.multi_asset
    assert challenge_policy in ma, (
        f"Continuing output multi_asset must contain challenge_policy "
        f"{challenge_policy}. Got policies: {list(ma.keys())}"
    )
    token_assets = ma[challenge_policy]
    token_an = AssetName(sample_challenge_token_bytes)
    assert token_an in token_assets, (
        f"Continuing output must carry challenge token "
        f"{sample_challenge_token_bytes.hex()}"
    )
    assert token_assets[token_an] == 1, (
        f"Challenge NFT qty must be 1; got {token_assets[token_an]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Builder topology
# ═════════════════════════════════════════════════════════════════════════

def test_reference_inputs_include_cross_refs(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_cross_refs_utxo,
    default_jury_size, default_selection_delay_ms,
):
    """The TransitionToVoting TX MUST include the cross_refs UTxO in
    reference_inputs so the validator can load `refs` (jury_pool hash,
    claim hash, etc.).  v13 line 1070: `b.reference_inputs.add(
    cross_refs_utxo)`.
    """
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    ref_inputs = list(builder.reference_inputs)
    ref_ids = {(bytes(getattr(r, "input", r).transaction_id),
                getattr(r, "input", r).index) for r in ref_inputs}
    wanted = (
        bytes(sample_cross_refs_utxo.input.transaction_id),
        sample_cross_refs_utxo.input.index,
    )
    assert wanted in ref_ids, (
        f"reference_inputs must include cross_refs UTxO "
        f"{wanted[0].hex()}#{wanted[1]}. Got: "
        f"{[(t.hex()[:12], i) for (t, i) in ref_ids]}"
    )


def test_validity_window_respects_selection_delay(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
):
    """The validator requires
        tx_started_after(ch.challenged_at + params.selection_delay)
    so the builder's `validity_start` slot MUST correspond to a POSIX
    time strictly greater than `challenged_at + selection_delay_ms`.

    Per v13 line 1075 the builder computes
        validity_start = max(current_slot - 60, selection_deadline_slot + 1)
    """
    from simulation.tx_builder import SYSTEM_START_UNIX

    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    input_fields = _decode_datum(sample_challenge_utxo_pending_jury.output).value
    challenged_at_ms = input_fields[6]

    validity_start = builder.validity_start
    assert validity_start is not None, (
        "builder.validity_start must be set — validator enforces a "
        "strict time lower bound (challenge.ak:757-758)."
    )
    vs_ms = (SYSTEM_START_UNIX + validity_start) * 1000
    deadline_ms = challenged_at_ms + default_selection_delay_ms
    assert vs_ms > deadline_ms, (
        f"validity_start ({validity_start} slots -> {vs_ms} ms) must be "
        f"strictly after challenged_at+selection_delay "
        f"({deadline_ms} ms). Validator invariant "
        f"challenge.ak:757-758."
    )


def test_ttl_reasonable(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
):
    """TTL should be a sensible upper bound — v13 uses `current_slot +
    3600`.  Assert TTL is set and lives in the future relative to
    validity_start."""
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    assert builder.ttl is not None, "builder.ttl must be set (validity_end)"
    assert builder.validity_start is not None
    assert builder.ttl > builder.validity_start, (
        f"ttl ({builder.ttl}) must be > validity_start "
        f"({builder.validity_start})"
    )
    # Reasonable upper bound — v13 uses +3600, so <= +7200 gives slack
    # for future slot-drift without permitting absurdly long windows.
    assert builder.ttl - builder.validity_start <= 7200, (
        f"validity window of {builder.ttl - builder.validity_start} "
        f"slots exceeds a sensible ~2h upper bound (v13 uses 3600)"
    )


def test_no_oracle_signature_required_on_builder(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
):
    """TransitionToVoting is PERMISSIONLESS in Phase 1.1 (challenge.ak
    line 755: 'permissionless — anyone can trigger after
    selection_delay').  The builder's required_signers should therefore
    NOT include an additional oracle vkh — only the fee-payer's vkh.

    If there is a required_signers list, it should contain at most one
    entry (the wallet vkh) — not two (wallet + oracle)."""
    _, builder = _run_build_transition(
        patched_network_for_transition, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_challenge_utxo_pending_jury,
        default_jury_size, default_selection_delay_ms,
    )
    rs = builder.required_signers or []
    _, vkey, _ = sample_auditor_wallet
    wallet_vkh = bytes(vkey.hash())
    non_wallet_signers = [bytes(s) for s in rs if bytes(s) != wallet_vkh]
    assert len(non_wallet_signers) == 0, (
        f"TransitionToVoting is permissionless — required_signers should "
        f"contain only the fee-payer's vkh ({wallet_vkh.hex()}). "
        f"Unexpected extra signers: {[s.hex() for s in non_wallet_signers]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards — builder must fail fast before submitting a
# doomed TX.
# ═════════════════════════════════════════════════════════════════════════

def test_selected_jurors_count_matches_jury_size(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_eligible_jurors,
    default_selection_delay_ms,
    monkeypatch,
):
    """If the eligible_jurors snapshot is shorter than jury_size, the
    PRNG can only draw `len(eligible)` jurors — the validator's
    selection_matches check would then fail because the validator
    computes jury_size entries from params but the builder produced
    fewer.  The builder MUST raise ValueError before reaching submit.
    """
    from simulation.tx_builder import build_transition_to_voting

    # Patch resolve_utxo to return a clone of the pending-jury UTxO with
    # only 3 eligible jurors (< jury_size=5).
    _, _, _ = sample_auditor_wallet
    from pycardano import RawCBOR, TransactionInput, TransactionOutput, TransactionId, UTxO as _UTxO

    raw = bytes(sample_challenge_utxo_pending_jury.output.datum.cbor)
    decoded = cbor2.loads(raw)
    fields = list(decoded.value)
    fields[8] = list(sample_eligible_jurors[:3])  # only 3 < jury_size 5
    shrunken_datum = RawCBOR(cbor2.dumps(cbor2.CBORTag(121, fields)))
    new_txid = hashlib.blake2b(b"short-elig", digest_size=32).digest()
    shrunken = _UTxO(
        TransactionInput(TransactionId(new_txid), 0),
        TransactionOutput(
            sample_challenge_utxo_pending_jury.output.address,
            sample_challenge_utxo_pending_jury.output.amount,
            datum=shrunken_datum,
        ),
    )

    import simulation.tx_builder as tx_mod
    monkeypatch.setattr(tx_mod, "resolve_utxo", lambda t, i: shrunken)

    skey, vkey, wallet_addr = sample_auditor_wallet
    chal_ref = f"{new_txid.hex()}#0"

    with pytest.raises(ValueError, match=r"(?i)jury.?size|juror.*count|eligible"):
        build_transition_to_voting(
            mock_ogmios_context,
            sample_deployment_with_challenge_ref_only,
            skey, vkey, wallet_addr,
            chal_ref,
            jury_size=5,
            selection_delay_ms=default_selection_delay_ms,
        )


def test_challenge_state_must_be_PendingJury_or_raises(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_voting_state,
    default_jury_size, default_selection_delay_ms,
    monkeypatch,
):
    """If the challenge UTxO the builder was pointed at is NOT in state
    PendingJury (e.g. someone already transitioned it, or it is
    Resolved), the builder MUST refuse with ValueError.  Matches
    validator invariant 1 (state_ok = PendingJury).
    """
    from simulation.tx_builder import build_transition_to_voting
    import simulation.tx_builder as tx_mod

    # Route resolve_utxo to the Voting-state variant.
    monkeypatch.setattr(
        tx_mod, "resolve_utxo",
        lambda t, i: sample_challenge_utxo_voting_state,
    )

    skey, vkey, wallet_addr = sample_auditor_wallet
    chal_ref = (
        f"{bytes(sample_challenge_utxo_voting_state.input.transaction_id).hex()}"
        f"#{sample_challenge_utxo_voting_state.input.index}"
    )

    with pytest.raises(ValueError, match=r"(?i)pending.?jury|state"):
        build_transition_to_voting(
            mock_ogmios_context,
            sample_deployment_with_challenge_ref_only,
            skey, vkey, wallet_addr,
            chal_ref,
            jury_size=default_jury_size,
            selection_delay_ms=default_selection_delay_ms,
        )


def test_selected_jurors_subset_of_eligible_raises_if_not(
    patched_network_for_transition, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    default_jury_size, default_selection_delay_ms,
    monkeypatch,
):
    """Defensive client-side check: if the builder internally computes a
    PRNG selection and then (for any reason — bug, stale snapshot,
    misconfiguration) ends up with a DID outside `eligible_jurors`, it
    MUST raise ValueError before attempting to submit.

    We force the failure by monkey-patching a hypothetical internal
    `select_jurors_prng` the builder will import from its own module —
    if Catherine ships it under a different name, the import will
    succeed but our patch will no-op.  We fall back to asserting that
    *when* such a divergence is detectable (via an all-zero DID
    injected into the selected list via a patched helper), the builder
    raises.  Catherine: expose the PRNG helper at module scope as
    `_select_jurors_prng` so this test can exercise the guard.
    """
    from simulation.tx_builder import build_transition_to_voting
    import simulation.tx_builder as tx_mod

    # Force PRNG to return a DID that is NOT in eligible_jurors.
    bogus = [b"\x00" * 28] * default_jury_size

    def _bad_prng(seed, eligible, n):
        return list(bogus)

    # Catherine MUST expose the PRNG as a module-level callable named
    # `select_jurors_prng` (or `_select_jurors_prng`) so the builder is
    # testable.  We set both common names; the real one will take
    # effect.
    monkeypatch.setattr(tx_mod, "select_jurors_prng", _bad_prng, raising=False)
    monkeypatch.setattr(tx_mod, "_select_jurors_prng", _bad_prng, raising=False)

    skey, vkey, wallet_addr = sample_auditor_wallet
    chal_ref = (
        f"{bytes(sample_challenge_utxo_pending_jury.input.transaction_id).hex()}"
        f"#{sample_challenge_utxo_pending_jury.input.index}"
    )

    with pytest.raises(
        ValueError,
        match=r"(?i)subset|selected.*eligible|selected.*not.*eligible",
    ):
        build_transition_to_voting(
            mock_ogmios_context,
            sample_deployment_with_challenge_ref_only,
            skey, vkey, wallet_addr,
            chal_ref,
            jury_size=default_jury_size,
            selection_delay_ms=default_selection_delay_ms,
        )
