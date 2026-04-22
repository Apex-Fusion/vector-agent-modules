"""
RED-phase tests for `simulation.tx_builder.build_select_jury`.

Function under test (NOT YET IMPLEMENTED):
    build_select_jury(
        context, deployment,
        skey, vkey, wallet_addr,
        challenge_utxo_ref: str,             # "<txid>#<idx>" Voting ChallengeDatum UTxO
        juror_utxo_refs: list[str],          # ["<txid>#<idx>", ...] length==jury_size
        *,
        jury_size: int = 5,
    ) -> dict                                 # MUST include tx_hash + the
                                              # 5 continuing juror UTxO refs
                                              # + challenge_token_name bytes

────────────────────────────────────────────────────────────────────────
VALIDATOR GROUND TRUTH — researched by reading the source directly
────────────────────────────────────────────────────────────────────────
Findings corrected vs. the task brief:

    * Confirmed  — JuryAction::SelectJury is Constr1, CBOR tag 122.
      (The brief's Constr1=122 claim was correct.)

    * Confirmed  — Redeemer shape:
        SelectJury { challenge_ref, selection_seed, selected_jurors }
      Wire layout: CBORTag(122, [challenge_ref, selection_seed, selected_jurors])
      Source: types.ak:220-240.

    * Confirmed  — Datum transition per juror:
        field[6] active_case: None -> Some(challenge_token_name)
        field[7] vote_commitment: None -> None (unchanged but explicitly
          asserted == None by validator)
        field[8] revealed_verdict: None -> None (same)
        fields[0..5] preserved byte-identically.
      Source: jury_pool.ak:397-420.

    * Corrected — Selection determinism. The brief said "client MUST
      provide the exact PRNG-selected subset" and hinted the validator
      recomputes PRNG. That is INCORRECT as of the current tree.
      In Phase 1.1 the validator does NOT recompute the PRNG on
      SelectJury. Instead it reads the ALREADY-VERIFIED selected_jurors
      list from the challenge UTxO's Voting state datum
      (jury_pool.ak:370-387). The redeemer's `selected_jurors` field
      is accepted (no crash) but IGNORED for security — line 370
      comment: "Read selected_jurors from Voting state datum, not
      redeemer. This eliminates redeemer trust: the juror list is
      verified on-chain by TransitionToVoting."
      Implication for this builder: we still want to sanity-check the
      input DIDs match the on-chain Voting list (that's where the PRNG
      verification has already happened), but the PRNG recompute
      itself is optional for correctness — the Voting state is the
      source of truth.

    * Corrected — Time gate. The brief asked to verify. ANSWER: there
      is NO additional time constraint on SelectJury beyond the
      challenge being in Voting state (the `selection_delay` time
      gate sits on TransitionToVoting, not here). jury_pool.ak:325-427
      contains no `tx_ends_before` / `tx_started_after` calls.

    * Corrected — Signer. The brief asked "permissionless / oracle /
      jury pool admin?". ANSWER: PERMISSIONLESS. The validator does
      not call `credential_signed` or check any vkh — anyone can
      trigger SelectJury as long as the inputs are structurally valid.
      jury_pool.ak:334-335 comment: "Phase 1.1: permissionless — jury
      selection is deterministic ... Oracle signature no longer
      required." The builder's `required_signers` contains only the
      fee payer's vkh (matching v13 step5a line 1183).

    * Challenge UTxO is REFERENCE input only (not spent). The challenge
      UTxO is consulted as a reference input at the challenge validator
      address; its InlineDatum is parsed to read the Voting state.
      jury_pool.ak:354-368 uses list.find on tx.reference_inputs.

    * Reference inputs required:
        - cross_refs UTxO (for refs.challenge_validator_hash,
          refs.jury_pool_hash) — validator loads these via the refs NFT
        - params UTxO (loaded by the jury_pool validator for other
          actions; SelectJury body itself doesn't read params, but v13
          includes it at line 1180 for consistency with the shared
          builder shape and to satisfy the validator's params arg
          unpacking)
        - challenge UTxO (Voting state) — the source-of-truth datum
      Source: v13 step5a_select_jury lines 1178-1180.

    * Multi-input nature — each juror UTxO is added via
      `builder.add_script_input(juror_utxo, jury_pool_ref_utxo,
       redeemer=r)` in a loop (v13:1174-1176). Each juror input gets
      its OWN spend redeemer entry; the redeemers all carry the SAME
      payload (challenge_ref, selection_seed, selected_jurors). That
      is important — we treat the redeemer as tx-global metadata even
      though Cardano stores one per input.

    * Continuing outputs — one per juror input (v13:1181-1182),
      produced at `jury_pool_addr`. The builder recomputes each
      juror's datum by copying fields 0..5, patching field[6] to
      `Some(challenge_token_name)`, leaving fields[7..8] as None,
      and preserves the juror NFT (the v13 snippet builds a fresh
      MultiAsset with only the juror NFT — bond sits in the coin
      field, matching Path B).

────────────────────────────────────────────────────────────────────────
Source refs
────────────────────────────────────────────────────────────────────────
    contracts/lib/adversarial_auditing/types.ak           lines  220-240
    contracts/validators/jury_pool.ak :: validate_select_jury
                                                          lines  325-427
    Reference impl in v13:
        /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py :: step5a_select_jury       lines 1104-1212

All tests in this file MUST fail against the current tree because
`build_select_jury` does NOT yet exist in `tx_builder.py`. The RED
signal is `ImportError` on the function; Catherine implements the
function to turn the suite GREEN.
"""
from __future__ import annotations

import hashlib

import cbor2
import pytest

from pycardano import (
    Address,
    AssetName,
    ScriptHash,
    TransactionBuilder,
)


# ═════════════════════════════════════════════════════════════════════════
# Helpers (local to this test file; mirror test_commit_vote.py style)
# ═════════════════════════════════════════════════════════════════════════


def _jury_pool_outputs(builder: TransactionBuilder):
    """Return all continuing outputs at jury_pool script addr — should
    be exactly 5 (one per spent juror UTxO)."""
    from simulation.tests.conftest import V13_DEPLOYMENT
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    return [o for o in builder.outputs if o.address == jury_addr]


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


def _collect_all_spend_redeemers(builder: TransactionBuilder):
    """Return {(txid_bytes, idx): Redeemer} for every script spend
    redeemer attached to the builder. Probes PyCardano's internal
    attribute shapes the same way the other test files do — see
    test_transition_to_voting.py:_collect_spend_redeemers_for_input.
    """
    out: dict = {}
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
                    out[key] = v
        elif isinstance(mapping, list):
            for item in mapping:
                if isinstance(item, tuple) and len(item) == 2:
                    k, v = item
                    k_inp = k.input if hasattr(k, "input") else k
                    if hasattr(k_inp, "transaction_id"):
                        key = (bytes(k_inp.transaction_id), k_inp.index)
                        out[key] = v
    return out


def _redeemer_cbor_bytes(redeemer) -> bytes:
    """Normalise a Redeemer's payload to CBOR bytes."""
    data = redeemer.data if hasattr(redeemer, "data") else redeemer
    if hasattr(data, "cbor"):
        return bytes(data.cbor)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return cbor2.dumps(data)


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def _run_build_select_jury(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
    *,
    juror_utxos_override=None,
    challenge_utxo_override=None,
    juror_refs_override=None,
):
    """Invoke build_select_jury with canonical fixture inputs.

    `juror_utxos_override` / `challenge_utxo_override` let tests
    swap in a variant set while keeping the rest of the wiring
    canonical. When provided, the resolve_utxo dispatcher is
    re-installed on top of the base `patched_network_for_select_jury`
    patch.

    `juror_refs_override` lets tests pass a custom ref list (e.g.
    4-element list to check the length guard).
    """
    from simulation.tx_builder import build_select_jury

    skey, vkey, wallet_addr = sample_juror_wallet
    juror_utxos = juror_utxos_override or sample_5_juror_utxos_unassigned
    challenge_utxo = challenge_utxo_override or sample_challenge_utxo_voting

    # Rewire resolve_utxo when a variant is active.
    if juror_utxos_override is not None or challenge_utxo_override is not None:
        import simulation.tx_builder as tx_mod
        txid_map = {
            bytes(u.input.transaction_id).hex(): u for u in juror_utxos
        }
        ch_txid_hex = bytes(challenge_utxo.input.transaction_id).hex()

        def _dispatch(txid_hex, idx, _m=txid_map, _ch=challenge_utxo,
                      _cht=ch_txid_hex):
            if txid_hex in _m:
                return _m[txid_hex]
            if txid_hex == _cht:
                return _ch
            raise AssertionError(
                f"override resolve_utxo: unexpected txid {txid_hex}#{idx}"
            )
        tx_mod.resolve_utxo = _dispatch

    juror_refs = juror_refs_override or [_utxo_ref_str(u) for u in juror_utxos]
    result = build_select_jury(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        _utxo_ref_str(challenge_utxo),
        juror_refs,
        jury_size=default_jury_size,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_select_jury did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# Signature / importability
# ═════════════════════════════════════════════════════════════════════════


def test_build_select_jury_is_importable():
    """Catherine MUST expose `build_select_jury` at module scope in
    simulation/tx_builder.py. All other tests in this file will fail
    with ImportError until this is satisfied — but we also keep a
    dedicated test so the RED signal is crystal clear."""
    from simulation.tx_builder import build_select_jury  # noqa: F401
    assert callable(build_select_jury), (
        "simulation.tx_builder.build_select_jury must be callable"
    )


def test_build_select_jury_returns_dict_with_tx_hash_and_juror_refs(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """The return dict MUST include:
        - tx_hash: str
        - juror_utxo_refs: list[str] of length jury_size
          (so the caller can pass them to downstream build_commit_vote)
        - challenge_token_name: bytes (the token name bound to each
          juror's new active_case — callers need it for auditing)
    """
    result, _ = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    assert "tx_hash" in result, (
        "result must include 'tx_hash' (str) so callers can track the "
        "submitted tx. Got keys: " + ", ".join(sorted(result.keys()))
    )
    assert isinstance(result["tx_hash"], str), (
        f"tx_hash must be str; got {type(result['tx_hash']).__name__}"
    )

    assert "juror_utxo_refs" in result, (
        "result must include 'juror_utxo_refs' (list[str]) — the 5 new "
        "continuing juror UTxO references (so the caller can immediately "
        "feed them into build_commit_vote without re-querying the chain)"
    )
    refs = result["juror_utxo_refs"]
    assert isinstance(refs, list) and len(refs) == default_jury_size, (
        f"juror_utxo_refs must be a list of length {default_jury_size}; "
        f"got {type(refs).__name__} with len={len(refs) if isinstance(refs, list) else 'n/a'}"
    )
    for r in refs:
        assert isinstance(r, str) and "#" in r, (
            f"each juror_utxo_ref must be a '<txid>#<idx>' string; got {r!r}"
        )

    assert "challenge_token_name" in result, (
        "result must include 'challenge_token_name' (bytes) — the token "
        "name bound into each juror's new active_case field, needed by "
        "callers that re-derive juror identities for downstream steps"
    )
    tn = result["challenge_token_name"]
    assert isinstance(tn, (bytes, bytearray)) and len(tn) == 32, (
        f"challenge_token_name must be 32 bytes; got "
        f"{type(tn).__name__} len={len(tn) if hasattr(tn, '__len__') else 'n/a'}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer correctness
# ═════════════════════════════════════════════════════════════════════════


def test_redeemer_is_SelectJury_Constr1_tag_122(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """Each of the 5 juror inputs must carry a spend redeemer whose
    outermost CBOR tag is 122 — JuryAction::SelectJury (Constr1).
    Per types.ak:220-240 and v13 deploy_and_run_v13.py:1146.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    red_map = _collect_all_spend_redeemers(builder)
    assert red_map, (
        "No spend redeemers attached to the builder — "
        "build_select_jury must call add_script_input(...) for each "
        "juror UTxO."
    )
    for juror_utxo in sample_5_juror_utxos_unassigned:
        key = (bytes(juror_utxo.input.transaction_id), juror_utxo.input.index)
        assert key in red_map, (
            f"Missing spend redeemer for juror UTxO "
            f"{key[0].hex()}#{key[1]}. Got keys: "
            f"{[(k[0].hex()[:12], k[1]) for k in red_map.keys()]}"
        )
        raw = _redeemer_cbor_bytes(red_map[key])
        decoded = cbor2.loads(raw)
        assert hasattr(decoded, "tag") and decoded.tag == 122, (
            f"Juror {key[0].hex()[:12]}#{key[1]} redeemer must be "
            f"CBORTag(122, [..]) = JuryAction::SelectJury (Constr1); "
            f"got {decoded!r}"
        )


def test_redeemer_payload_has_challenge_ref_seed_and_selected_jurors(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    sample_selected_jurors,
    default_jury_size,
):
    """Redeemer payload MUST have 3 fields:
        [0] challenge_ref   CBORTag(121, [txid_bytes, idx])
        [1] selection_seed  ByteArray (32 B — cosmetic)
        [2] selected_jurors list[ByteArray] of length jury_size
    Mirrors types.ak:220-240 and v13:1146.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    red_map = _collect_all_spend_redeemers(builder)
    # Any one redeemer is fine — they all carry the same payload.
    sample_red = next(iter(red_map.values()))
    decoded = cbor2.loads(_redeemer_cbor_bytes(sample_red))
    assert isinstance(decoded.value, list) and len(decoded.value) == 3, (
        f"Redeemer payload must be [challenge_ref, seed, selected_jurors] "
        f"(3 items); got {decoded.value!r}"
    )
    chal_ref, seed, selected = decoded.value

    # chal_ref is OutputReference = CBORTag(121, [txid_bytes, idx])
    assert hasattr(chal_ref, "tag") and chal_ref.tag == 121, (
        f"challenge_ref must be CBORTag(121, [txid, idx]); got {chal_ref!r}"
    )
    assert isinstance(chal_ref.value, list) and len(chal_ref.value) == 2, (
        f"challenge_ref payload must be [txid_bytes, idx]; got {chal_ref.value!r}"
    )
    expected_txid = bytes(sample_challenge_utxo_voting.input.transaction_id)
    actual_txid = bytes(chal_ref.value[0])
    assert actual_txid == expected_txid, (
        f"challenge_ref.txid must match the input challenge UTxO. "
        f"Expected {expected_txid.hex()}; got {actual_txid.hex()}"
    )
    assert chal_ref.value[1] == sample_challenge_utxo_voting.input.index

    assert isinstance(seed, (bytes, bytearray)) and len(seed) == 32, (
        f"selection_seed must be 32-byte ByteArray; got "
        f"{type(seed).__name__} len={len(seed) if hasattr(seed,'__len__') else 'n/a'}"
    )

    assert isinstance(selected, list) and len(selected) == default_jury_size, (
        f"selected_jurors must be list of length {default_jury_size}; "
        f"got len={len(selected) if isinstance(selected, list) else 'n/a'}"
    )
    selected_bytes = [bytes(d) for d in selected]
    assert sorted(selected_bytes) == sorted(sample_selected_jurors), (
        f"selected_jurors in redeemer must match the PRNG-derived set "
        f"(== sample_selected_jurors). Expected (sorted) "
        f"{[b.hex()[:12] for b in sorted(sample_selected_jurors)]}; got "
        f"{[b.hex()[:12] for b in sorted(selected_bytes)]}"
    )


def test_all_juror_inputs_share_the_same_redeemer_payload(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """All 5 juror inputs attach DIFFERENT Redeemer objects (one per
    input is how Cardano works) but the PAYLOAD bytes must be
    byte-identical — the redeemer is tx-global metadata.
    Mirrors v13 where a single `select_jury_redeemer_cbor` is
    instantiated once then used for each add_script_input call
    (deploy_and_run_v13.py:1174-1176).
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    red_map = _collect_all_spend_redeemers(builder)
    payloads = {_redeemer_cbor_bytes(r) for r in red_map.values()}
    assert len(payloads) == 1, (
        f"All {len(red_map)} juror spend redeemers must share the same "
        f"CBOR payload; got {len(payloads)} distinct payloads."
    )


# ═════════════════════════════════════════════════════════════════════════
# Per-juror datum transition
# ═════════════════════════════════════════════════════════════════════════


def test_five_continuing_juror_outputs_produced(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """There MUST be exactly jury_size (5) continuing outputs at the
    jury_pool script address — one per spent juror UTxO. Validator
    enforces a continuing output exists for each consumed juror
    (jury_pool.ak:397-420 `list.any` per juror).
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    outs = _jury_pool_outputs(builder)
    assert len(outs) == default_jury_size, (
        f"Expected exactly {default_jury_size} continuing juror outputs "
        f"at jury_pool addr; got {len(outs)}. "
        f"All outputs: {[str(o.address) for o in builder.outputs]}"
    )


def test_each_continuing_juror_datum_field_6_active_case_is_challenge_token(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    sample_challenge_token_bytes,
    default_jury_size,
):
    """Each of the 5 continuing outputs must have field[6] =
    Some(challenge_token_name), i.e. CBORTag(121, [challenge_token_bytes]).
    Validator line 411: `updated.active_case == Some(challenge_token_name)`.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    outs = _jury_pool_outputs(builder)
    for i, out in enumerate(outs):
        fields = _decode_datum(out).value
        active_case = fields[6]
        assert hasattr(active_case, "tag") and active_case.tag == 121, (
            f"juror output[{i}] field[6] active_case must be Some(h) = "
            f"CBORTag(121,[h]); got {active_case!r}"
        )
        assert isinstance(active_case.value, list) and len(active_case.value) == 1
        tn = bytes(active_case.value[0])
        assert tn == sample_challenge_token_bytes, (
            f"juror output[{i}] active_case token mismatch. "
            f"Expected {sample_challenge_token_bytes.hex()}; "
            f"got {tn.hex()}"
        )


def test_each_continuing_juror_datum_field_7_and_8_are_None(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """Each continuing output must have field[7] vote_commitment = None
    AND field[8] revealed_verdict = None — CBORTag(122, []).
    Validator lines 412-413: `updated.vote_commitment == None`,
    `updated.revealed_verdict == None`.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    outs = _jury_pool_outputs(builder)
    for i, out in enumerate(outs):
        fields = _decode_datum(out).value
        vc = fields[7]
        rv = fields[8]
        assert hasattr(vc, "tag") and vc.tag == 122 and list(vc.value) == [], (
            f"juror output[{i}] field[7] vote_commitment must be "
            f"None = CBORTag(122, []); got {vc!r}"
        )
        assert hasattr(rv, "tag") and rv.tag == 122 and list(rv.value) == [], (
            f"juror output[{i}] field[8] revealed_verdict must be "
            f"None = CBORTag(122, []); got {rv!r}"
        )


def test_each_continuing_juror_datum_preserves_fields_0_through_5(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """Fields 0-5 (juror_did, juror_credential, bond_amount,
    cases_resolved, majority_votes, registered_at) MUST be preserved
    byte-identically per juror. Validator lines 405-410.

    Matching input-juror to output-juror is done via juror_did (field[0]).
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    # Build a DID -> input-datum-fields index.
    in_by_did = {}
    for u in sample_5_juror_utxos_unassigned:
        fields = _decode_datum(u.output).value
        in_by_did[bytes(fields[0])] = fields

    outs = _jury_pool_outputs(builder)
    matched = 0
    for out in outs:
        out_fields = _decode_datum(out).value
        did = bytes(out_fields[0])
        assert did in in_by_did, (
            f"Continuing output juror_did {did.hex()[:12]} was not an "
            f"input juror. Inputs: "
            f"{[b.hex()[:12] for b in in_by_did.keys()]}"
        )
        in_fields = in_by_did[did]
        for idx in range(6):  # 0..5
            assert out_fields[idx] == in_fields[idx], (
                f"Juror {did.hex()[:12]} field[{idx}] not preserved. "
                f"Input: {in_fields[idx]!r}. Output: {out_fields[idx]!r}"
            )
        matched += 1
    assert matched == default_jury_size, (
        f"Every input juror must have a matching continuing output; "
        f"matched {matched}/{default_jury_size}"
    )


def test_continuing_juror_dids_match_input_juror_dids(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """The set of DIDs across continuing outputs must exactly equal the
    set across input juror UTxOs (bijection via field[0]).
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    input_dids = sorted(
        bytes(_decode_datum(u.output).value[0])
        for u in sample_5_juror_utxos_unassigned
    )
    output_dids = sorted(
        bytes(_decode_datum(o).value[0])
        for o in _jury_pool_outputs(builder)
    )
    assert input_dids == output_dids, (
        f"Continuing-output DIDs must match input DIDs bijectively. "
        f"Inputs: {[d.hex()[:12] for d in input_dids]}; "
        f"Outputs: {[d.hex()[:12] for d in output_dids]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Selection correctness (PRNG ↔ input DIDs)
# ═════════════════════════════════════════════════════════════════════════


def test_selected_dids_match_prng_output(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    sample_selected_jurors,
    default_jury_size,
):
    """The set of juror DIDs consumed by build_select_jury MUST equal
    the PRNG-selected subset (`sample_selected_jurors`). Same set the
    on-chain Voting datum stores under field[9].value[0].
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    # Inputs attached to the builder should be the 5 juror UTxOs.
    # We look them up by txid against the fixture list.
    fixture_txids = {bytes(u.input.transaction_id) for u in sample_5_juror_utxos_unassigned}
    attached_inputs = list(builder.inputs) if hasattr(builder, "inputs") else []
    attached_script_inputs = [
        i for i in attached_inputs
        if bytes(getattr(i, "input", i).transaction_id) in fixture_txids
    ]
    assert len(attached_script_inputs) == default_jury_size, (
        f"Expected {default_jury_size} juror script inputs attached to "
        f"builder; got {len(attached_script_inputs)}"
    )
    # Verify via continuing outputs — those carry the input DIDs directly.
    output_dids = sorted(
        bytes(_decode_datum(o).value[0])
        for o in _jury_pool_outputs(builder)
    )
    assert output_dids == sorted(sample_selected_jurors), (
        f"Consumed juror DIDs must match PRNG selection. "
        f"Expected (sorted): {[b.hex()[:12] for b in sorted(sample_selected_jurors)]}; "
        f"got: {[b.hex()[:12] for b in output_dids]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Value preservation (bond + juror NFT)
# ═════════════════════════════════════════════════════════════════════════


def test_each_continuing_juror_output_preserves_bond_coin(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_bond_amount,
    default_jury_size,
):
    """Each continuing output MUST have coin == juror.bond_amount
    (Path B — bond is base lovelace). Validator line 414.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    outs = _jury_pool_outputs(builder)
    for i, out in enumerate(outs):
        assert out.amount.coin == default_bond_amount, (
            f"juror output[{i}] coin must equal bond_amount "
            f"({default_bond_amount}); got {out.amount.coin}"
        )


def test_each_continuing_juror_output_preserves_juror_nft(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    sample_5_juror_tokens_from_dids,
    default_jury_size,
):
    """Each continuing output MUST carry the SAME juror NFT (qty=1)
    under jury_pool policy as the input juror UTxO. Token name
    = b'jur_' || blake2b_256(did)[:28] (utils.ak:50-54).
    Implicit validator requirement — if the NFT were dropped the
    juror would have no continuing identity.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT

    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    outs = _jury_pool_outputs(builder)
    for i, out in enumerate(outs):
        fields = _decode_datum(out).value
        did = bytes(fields[0])
        expected_token_bytes = sample_5_juror_tokens_from_dids(did)
        expected_an = AssetName(expected_token_bytes)
        ma = out.amount.multi_asset
        assert jury_policy in ma, (
            f"juror output[{i}] multi_asset missing jury_pool policy "
            f"{jury_policy}. Got: {list(ma.keys())}"
        )
        assets = ma[jury_policy]
        assert expected_an in assets, (
            f"juror output[{i}] missing expected juror token "
            f"{expected_token_bytes.hex()[:16]}..."
        )
        assert assets[expected_an] == 1, (
            f"juror output[{i}] juror NFT qty must be 1; "
            f"got {assets[expected_an]}"
        )


# ═════════════════════════════════════════════════════════════════════════
# Builder topology — reference inputs, signer, validity window
# ═════════════════════════════════════════════════════════════════════════


def test_reference_inputs_include_cross_refs_params_and_challenge(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    sample_cross_refs_utxo,
    sample_params_utxo,
    default_jury_size,
):
    """The TX MUST include three reference inputs:
        - cross_refs UTxO   (refs NFT)
        - params UTxO       (v13:1180 adds it for shared builder shape)
        - challenge UTxO    (the Voting-state challenge; validator
                             reads selected_jurors from its datum)
    Mirrors v13 step5a_select_jury lines 1178-1180.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    ref_ids = {
        (bytes(getattr(r, "input", r).transaction_id),
         getattr(r, "input", r).index)
        for r in builder.reference_inputs
    }
    wanted = {
        (bytes(sample_cross_refs_utxo.input.transaction_id),
         sample_cross_refs_utxo.input.index),
        (bytes(sample_params_utxo.input.transaction_id),
         sample_params_utxo.input.index),
        (bytes(sample_challenge_utxo_voting.input.transaction_id),
         sample_challenge_utxo_voting.input.index),
    }
    missing = wanted - ref_ids
    assert not missing, (
        f"reference_inputs missing: "
        f"{[(t.hex()[:12], i) for (t, i) in missing]}. Got: "
        f"{[(t.hex()[:12], i) for (t, i) in ref_ids]}"
    )


def test_five_script_inputs_attached_one_per_juror(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """Every one of the 5 juror UTxOs MUST be attached as a script
    input (i.e. with a spend redeemer). We verify via the redeemer
    map since the low-level input collection shape varies between
    PyCardano versions.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    red_map = _collect_all_spend_redeemers(builder)
    script_input_keys = set(red_map.keys())
    expected_keys = {
        (bytes(u.input.transaction_id), u.input.index)
        for u in sample_5_juror_utxos_unassigned
    }
    assert expected_keys == script_input_keys, (
        f"Expected exactly 5 script inputs (one per juror UTxO). "
        f"Missing: {[(k[0].hex()[:12], k[1]) for k in expected_keys - script_input_keys]}; "
        f"Unexpected: {[(k[0].hex()[:12], k[1]) for k in script_input_keys - expected_keys]}"
    )


def test_permissionless_no_extra_oracle_signer(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """SelectJury is PERMISSIONLESS in Phase 1.1 (jury_pool.ak:334-335).
    The builder's required_signers should contain ONLY the fee-payer's
    vkh — no oracle vkh, no per-juror vkh pile-up. Mirrors v13:1183
    where `required_signers = [vkey.hash()]`.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    rs = builder.required_signers or []
    _, vkey, _ = sample_juror_wallet
    wallet_vkh = bytes(vkey.hash())
    extras = [bytes(s) for s in rs if bytes(s) != wallet_vkh]
    assert len(extras) == 0, (
        f"SelectJury is permissionless — required_signers should be "
        f"{{wallet_vkh}}. Unexpected extra signers: "
        f"{[s.hex() for s in extras]}"
    )


def test_ttl_and_validity_start_set(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """The builder should set sensible validity_start and ttl. v13
    uses `validity_start = current_slot - 60` and `ttl = current_slot
    + 3600`. We assert both are set and ttl > validity_start. No time
    GATE exists on SelectJury itself — this is only a sanity bound.
    """
    _, builder = _run_build_select_jury(
        patched_network_for_select_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_5_juror_utxos_unassigned,
        sample_challenge_utxo_voting,
        default_jury_size,
    )
    assert builder.validity_start is not None, (
        "builder.validity_start must be set for submission safety"
    )
    assert builder.ttl is not None, "builder.ttl must be set"
    assert builder.ttl > builder.validity_start, (
        f"ttl ({builder.ttl}) must be > validity_start "
        f"({builder.validity_start})"
    )
    # v13 uses a 3660-slot window; allow up to 7200 for slack.
    assert builder.ttl - builder.validity_start <= 7200, (
        f"validity window of {builder.ttl - builder.validity_start} "
        f"slots is unreasonably wide (v13 uses ~3660)"
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards — builder must fail fast before submitting a
# doomed TX
# ═════════════════════════════════════════════════════════════════════════


def test_raises_if_juror_utxo_count_not_equal_to_jury_size(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """If the caller passes a juror-utxo list of wrong length, the
    builder MUST raise ValueError before doing anything else. On-chain
    the validator would still pass (it checks each juror
    independently) but the challenge would remain under-filled, which
    violates the implicit whole-tx invariant that exactly jury_size
    jurors are assigned in one atomic TX.
    """
    from simulation.tx_builder import build_select_jury

    skey, vkey, wallet_addr = sample_juror_wallet
    short_refs = [
        _utxo_ref_str(u) for u in sample_5_juror_utxos_unassigned[:4]
    ]  # 4 < 5
    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)

    with pytest.raises(ValueError, match=r"(?i)jury.?size|juror.*count|len\(|length"):
        build_select_jury(
            mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            skey, vkey, wallet_addr,
            chal_ref,
            short_refs,
            jury_size=default_jury_size,
        )


def test_raises_if_a_juror_already_has_active_case(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_one_already_assigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """If ANY of the 5 juror UTxOs has active_case=Some(...), the
    builder MUST raise ValueError — on-chain the `juror_available`
    predicate would fail (jury_pool.ak:343-347). Client-side guard
    means we do not waste a fee on a guaranteed-to-fail TX.
    """
    from simulation.tx_builder import build_select_jury
    import simulation.tx_builder as tx_mod

    skey, vkey, wallet_addr = sample_juror_wallet

    # Rewire resolve_utxo to serve the bad juror set.
    txid_map = {
        bytes(u.input.transaction_id).hex(): u
        for u in sample_5_juror_utxos_one_already_assigned
    }
    ch_txid_hex = bytes(sample_challenge_utxo_voting.input.transaction_id).hex()

    def _dispatch(txid_hex, idx, _m=txid_map, _ch=sample_challenge_utxo_voting,
                  _cht=ch_txid_hex):
        if txid_hex in _m:
            return _m[txid_hex]
        if txid_hex == _cht:
            return _ch
        raise AssertionError(f"unexpected resolve_utxo({txid_hex}#{idx})")
    tx_mod.resolve_utxo = _dispatch

    juror_refs = [
        _utxo_ref_str(u) for u in sample_5_juror_utxos_one_already_assigned
    ]
    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)

    with pytest.raises(ValueError, match=r"(?i)active.?case|already.*assigned|juror.*available"):
        build_select_jury(
            mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            skey, vkey, wallet_addr,
            chal_ref,
            juror_refs,
            jury_size=default_jury_size,
        )


def test_raises_if_juror_dids_do_not_match_prng_selection(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_wrong_dids,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """If the caller feeds 5 juror UTxOs whose DIDs are eligible but
    NOT the PRNG-selected subset (i.e. not the ones stored in the
    challenge's Voting.selected_jurors), the builder MUST raise
    ValueError. On-chain the validator would reject each juror via
    `list.has(on_chain_jurors, juror.juror_did)` (jury_pool.ak:380).
    """
    from simulation.tx_builder import build_select_jury
    import simulation.tx_builder as tx_mod

    skey, vkey, wallet_addr = sample_juror_wallet

    txid_map = {
        bytes(u.input.transaction_id).hex(): u
        for u in sample_5_juror_utxos_wrong_dids
    }
    ch_txid_hex = bytes(sample_challenge_utxo_voting.input.transaction_id).hex()

    def _dispatch(txid_hex, idx, _m=txid_map, _ch=sample_challenge_utxo_voting,
                  _cht=ch_txid_hex):
        if txid_hex in _m:
            return _m[txid_hex]
        if txid_hex == _cht:
            return _ch
        raise AssertionError(f"unexpected resolve_utxo({txid_hex}#{idx})")
    tx_mod.resolve_utxo = _dispatch

    juror_refs = [_utxo_ref_str(u) for u in sample_5_juror_utxos_wrong_dids]
    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)

    with pytest.raises(
        ValueError,
        match=r"(?i)selected|prng|voting.*state|did.*mismatch|not.*in.*selected",
    ):
        build_select_jury(
            mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            skey, vkey, wallet_addr,
            chal_ref,
            juror_refs,
            jury_size=default_jury_size,
        )


def test_raises_if_challenge_not_in_voting_state(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_pending_jury_for_select,
    default_jury_size,
):
    """If the challenge UTxO the builder was pointed at is NOT in
    state Voting (e.g. still PendingJury because TransitionToVoting
    hasn't happened yet), the builder MUST raise ValueError.
    Matches validator invariant (jury_pool.ak:372-387): the
    challenge_in_voting predicate requires ch.state == Voting{..}.
    """
    from simulation.tx_builder import build_select_jury
    import simulation.tx_builder as tx_mod

    skey, vkey, wallet_addr = sample_juror_wallet
    bad_challenge = sample_challenge_utxo_pending_jury_for_select

    txid_map = {
        bytes(u.input.transaction_id).hex(): u
        for u in sample_5_juror_utxos_unassigned
    }
    ch_txid_hex = bytes(bad_challenge.input.transaction_id).hex()

    def _dispatch(txid_hex, idx, _m=txid_map, _ch=bad_challenge,
                  _cht=ch_txid_hex):
        if txid_hex in _m:
            return _m[txid_hex]
        if txid_hex == _cht:
            return _ch
        raise AssertionError(f"unexpected resolve_utxo({txid_hex}#{idx})")
    tx_mod.resolve_utxo = _dispatch

    juror_refs = [_utxo_ref_str(u) for u in sample_5_juror_utxos_unassigned]
    chal_ref = _utxo_ref_str(bad_challenge)

    with pytest.raises(ValueError, match=r"(?i)voting|pending.?jury|challenge.*state"):
        build_select_jury(
            mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            skey, vkey, wallet_addr,
            chal_ref,
            juror_refs,
            jury_size=default_jury_size,
        )


def test_raises_if_duplicate_juror_utxo_refs(
    patched_network_for_select_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    default_jury_size,
):
    """If the caller passes the SAME juror UTxO ref twice (and drops a
    different one to keep len==5), the builder MUST raise ValueError.
    Consuming the same juror UTxO twice in one TX is impossible on
    Cardano (a UTxO is spent at most once) but a bug in the caller
    could produce such a list — fail fast with a clear error instead
    of letting PyCardano's deep error path fire.
    """
    from simulation.tx_builder import build_select_jury

    skey, vkey, wallet_addr = sample_juror_wallet
    u0 = sample_5_juror_utxos_unassigned[0]
    # Duplicate u0, drop u4; total still == 5.
    dup_refs = [
        _utxo_ref_str(u0),
        _utxo_ref_str(sample_5_juror_utxos_unassigned[1]),
        _utxo_ref_str(sample_5_juror_utxos_unassigned[2]),
        _utxo_ref_str(sample_5_juror_utxos_unassigned[3]),
        _utxo_ref_str(u0),  # dup
    ]
    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)

    with pytest.raises(ValueError, match=r"(?i)duplicate|unique|same.*juror|repeated"):
        build_select_jury(
            mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            skey, vkey, wallet_addr,
            chal_ref,
            dup_refs,
            jury_size=default_jury_size,
        )
