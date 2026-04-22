"""
RED-phase tests for `simulation.tx_builder.build_resolve_jury`.

Function under test (NOT YET IMPLEMENTED):
    build_resolve_jury(
        context, deployment,
        skey, vkey, wallet_addr,
        challenge_utxo_ref: str,             # "<txid>#<idx>" — Voting ChallengeDatum UTxO
        claim_utxo_ref: str,                 # "<txid>#<idx>" — Challenged ClaimDatum UTxO
        revealed_juror_utxo_refs: list[str], # 5 juror UTxOs (post-reveal) as reference inputs
        *,
        jury_size: int = 5,
        jury_fee_rate: int = 1000,           # basis points (10% = 1000)
    ) -> dict                                # MUST include:
                                              #   - tx_hash: str
                                              #   - verdict: str in {"ClaimerWins",
                                              #                      "AuditorWins",
                                              #                      "Inconclusive"}
                                              #   - resolved_challenge_ref: str
                                              #   - jury_fee: int
                                              #   - claimer_payout: int | None
                                              #   - auditor_payout: int | None

════════════════════════════════════════════════════════════════════════
VALIDATOR GROUND TRUTH — researched by reading the source directly
════════════════════════════════════════════════════════════════════════

Researched files (all paths absolute):
    /home/jelisaveta/github/vector-agent-modules/Module-1/
        contracts/validators/challenge.ak          lines 460-642, 1021-1126
        contracts/validators/claim.ak              lines 419-495
        contracts/lib/adversarial_auditing/types.ak
        contracts/lib/adversarial_auditing/params.ak lines 137-172
    /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py::step7_resolve_jury  lines 1431-1555

Constructor index → CBOR tag (verified in types.ak):

    ChallengeAction:
        OpenChallenge       Constr0 -> CBORTag(121, [])
        SubmitEvidence      Constr1 -> CBORTag(122, [...])
        OracleResolve       Constr2 -> CBORTag(123, [...])
        ResolveJury         Constr3 -> CBORTag(124, [])      <-- HERE
        TimeoutResolve      Constr4 -> CBORTag(125, [])
        CleanupResolved     Constr5 -> CBORTag(126, [])
        TransitionToVoting  Constr6 -> CBORTag(127, [...])

    ClaimAction:
        SubmitClaim         Constr0 -> CBORTag(121, [])
        WithdrawClaim       Constr1 -> CBORTag(122, [])
        MarkChallenged      Constr2 -> CBORTag(123, [])
        ForfeitClaim        Constr3 -> CBORTag(124, [])      <-- HERE

    ChallengeState:
        PendingOracle       Constr0 -> CBORTag(121, [])
        PendingJury         Constr1 -> CBORTag(122, [])
        Voting              Constr2 -> CBORTag(123, [selected_jurors])
        Resolved            Constr3 -> CBORTag(124, [verdict])  <-- HERE

    Verdict (Constr index, inside Resolved wrapper):
        ClaimerWins         Constr0 -> CBORTag(121, [])
        AuditorWins         Constr1 -> CBORTag(122, [])
        Inconclusive        Constr2 -> CBORTag(123, [])

    ClaimState (for the Challenged input we're spending):
        Open                Constr0 -> CBORTag(121, [])
        Challenged          Constr1 -> CBORTag(122, [])      <-- INPUT must be this
        Validated           Constr2 -> CBORTag(123, [])
        Invalidated         Constr3 -> CBORTag(124, [])

Findings vs. the task brief (brief-first — each confirmation / correction):

    * Confirmed — ChallengeAction::ResolveJury = Constr3 = CBORTag(124, []).
      Brief's statement. Verified against types.ak ChallengeAction ordering
      and against v13 `resolve_redeemer_cbor = CBORTag(124, [])` at line 1461.

    * Confirmed — ClaimAction::ForfeitClaim = Constr3 = CBORTag(124, []).
      Brief's statement. Verified: types.ak enumerates SubmitClaim(0),
      WithdrawClaim(1), MarkChallenged(2), ForfeitClaim(3). v13 at line
      1462 constructs `forfeit_redeemer_cbor = CBORTag(124, [])`.

    * Confirmed — Resolved state tag = 124. Verified by types.ak ordering
      AND v13 patch at line 1493: `chal_fields[9] = CBORTag(124, [verdict])`.

    * Confirmed — Verdict variants are 121 (ClaimerWins), 122 (AuditorWins),
      123 (Inconclusive). Verified by types.ak ordering and v13 tally at
      lines 1490-1491 (121 if claimer >= auditor else 122). Inconclusive
      (123) is reached if neither side hits `supermajority_threshold` (3).

    * Confirmed — Path B exact-equality on outputs. Validator (lines
      1042, 1069, 1097, 1103, etc.) uses `assets.lovelace_of(o.value) ==
      expected` — NOT `>=`. Building outputs with padding / min-UTxO
      absorbers would fail. V13's final script uses pure lovelace
      `payout_value = claimer_payout` (line 1503). TESTS ENFORCE THIS.

    * Confirmed — No oracle signature required. Validator challenge.ak
      lines 629-630: "Phase 1.1: No oracle signature needed — votes are
      authenticated on-chain via commit-reveal. ResolveJury is fully
      permissionless." V13 at line 1528-1530 only attaches the wallet
      signer (the fee payer). Tests assert NO oracle vkh in
      required_signers.

    * Confirmed — Challenge continuing output preserves auditor stake.
      Validator line 587: `assets.lovelace_of(out.value) == ch.stake_amount`.
      The AUDITOR'S stake lives on the continuing challenge UTxO so that
      DistributeRewards (step 8) can consume it to pay jurors. V13 at
      line 1500: `resolved_value = Value(STAKE_AMOUNT, chl_nft_ma)`.

    * Confirmed — Claim token burn is mandatory. Validator line 601-605:
      `list.any(tx.mint tokens under claim_validator_hash, qty == -1)`.
      V13 builds `burn_ma[claim_policy_sh] = {token: -1}` (line 1478-1480).

    * Confirmed — Challenge token is NOT burned in this step. Validator
      lines 551-553 say the challenge continues with the Resolved state
      and the challenge token preserved (CleanupResolved burns it later).
      V13 does NOT add the challenge policy to its burn MultiAsset.

    * Confirmed — `single_challenge_output` guard (Finding-002-F3).
      Validator lines 624-627 enforce exactly 1 output at challenge
      script addr. Tests assert the builder emits exactly one such
      output (and check that no stray Resolved-lookalike outputs
      pollute the tx).

    * Confirmed — Challenge UTxO is SPENT (not a reference input).
      Validator reads `ch` from the spent own_input; `find_input(tx.inputs,
      own_ref)` at line 480-487. v13 uses `b.add_script_input(challenge_utxo,
      ..., redeemer=resolve_r)` at line 1512.

    * Confirmed — Claim UTxO is SPENT alongside. v13 line 1513
      `b.add_script_input(claim_utxo, claim_ref_utxo, redeemer=forfeit_r)`.
      The claim spend validator (validate_forfeit_claim, claim.ak:419-495)
      checks:
         - clm.state == Challenged    (line 427-430)
         - claim token burned         (line 433-446)
         - challenge_resolving: Resolved-state continuing output with
           legitimate challenge token (line 467-487) OR challenge token
           burned (line 456-460) — ResolveJury path is the former.

    * Confirmed — Juror UTxOs are REFERENCE INPUTS, not spent. Validator
      iterates `tx.reference_inputs` at line 491. v13 line 1517-1518
      `b.reference_inputs.add(jutxo)` in a loop over the 5 jurors.
      Tests verify the 5 juror UTxOs appear in `builder.reference_inputs`
      and NOT in the spend redeemer map.

    * Confirmed — `votes_complete`, `votes_not_empty`, `no_duplicates`,
      `votes_from_jurors`. Validator lines 534-546. Tests exercise each:
         - partial reveal set (only 4 of 5)  -> client-side guard raises
         - duplicate juror_did across ref UTxOs -> guard raises
         - wrong challenge-token binding (jurors from another case) ->
           validator filter_map skips them -> vote_count == 0 -> guard raises

    * Confirmed — Datum field 9 (state) is the ONLY field that changes
      on the continuing challenge output. Validator lines 569-577
      preserve fields 0-8 and 10? ACTUALLY wait — ChallengeDatum has
      10 fields indexed [0..9]. Validator preservation list:
          updated.claim_ref, .auditor_did, .auditor_credential,
          .stake_amount, .evidence_hash, .evidence_uri, .challenged_at,
          .resolution_deadline, .eligible_jurors  (= fields 0..8)
          AND updated.state == Resolved{verdict}   (= field 9 flips)
      Tests assert byte-equality for fields 0..8 and Constr equality
      for field 9.

    * Confirmed — Jury fee rate default 1000 bps (10%). Per params.ak:
      `compute_jury_fee(loser_stake, rate) = loser_stake * rate / 10000`.
      So rate=1000 gives loser_stake / 10. V13 inline at line 1483.

    * Confirmed — supermajority_threshold(5) == 3.
      `jury_size / 2 + 1` in params.ak. Tests use this to prove
      2/2/1 -> Inconclusive and 3/1/1 -> ClaimerWins.

    * Brief's "Constr3 = tag 124" claim for ChallengeState::Resolved
      and for ChallengeAction::ResolveJury and ClaimAction::ForfeitClaim
      — ALL CONFIRMED.

Builder signature decisions (where spec ambiguity existed):

    * Return shape — we adopt the same pattern as build_select_jury:
      a dict with tx_hash and enough downstream metadata for Catherine's
      sim to route to step 8 (DistributeRewards) without re-querying
      the chain. Specifically:
         - verdict: str — "ClaimerWins" / "AuditorWins" / "Inconclusive"
           (upper-camel). The sim uses string literals for branching.
         - resolved_challenge_ref: str — "<tx_hash>#0" (the continuing
           challenge output is always at index 0 in v13; see line 1521
           where it is the first add_output call).
         - jury_fee: int — raw jury_fee amount (useful for
           reconciliation assertions in downstream simulation tests).
         - claimer_payout: int | None — set when ClaimerWins or
           Inconclusive; None otherwise.
         - auditor_payout: int | None — set when AuditorWins or
           Inconclusive; None otherwise.

    * Verdict derivation — the BUILDER re-tallies the revealed votes
      (reads each referenced juror UTxO's revealed_verdict) to decide
      the verdict client-side, then writes that verdict into both the
      Resolved state datum and the distribution outputs. This mirrors
      v13 step7 which tallies in Python (lines 1488-1492). The client
      MUST agree with what the on-chain validator will recompute —
      otherwise verify_jury_distribution will fail. Tests assert the
      computed verdict matches the juror reveal tally.

    * Client-side guards (raise before submit):
         - Wrong number of juror refs (!= jury_size) -> ValueError
         - Duplicate juror_did in ref set             -> ValueError
         - Juror active_case doesn't match challenge  -> ValueError
         - Any juror revealed_verdict == None         -> ValueError
         - Challenge state != Voting                  -> ValueError
         - Claim state     != Challenged              -> ValueError
         - Juror_did not in selected_jurors           -> ValueError

════════════════════════════════════════════════════════════════════════
Source refs
════════════════════════════════════════════════════════════════════════

    validators/challenge.ak::validate_resolve_jury       lines 460-642
    validators/challenge.ak::verify_jury_distribution    lines 1021-1126
    validators/challenge.ak::tally_revealed_votes        lines 945-971
    validators/challenge.ak::find_claim_stake            lines 897-917
    validators/challenge.ak::find_claimer_cred           lines 923-940
    validators/claim.ak::validate_forfeit_claim          lines 419-495
    lib/adversarial_auditing/types.ak                    lines 28-260
    lib/adversarial_auditing/params.ak                   lines 137-172

    Reference impl in v13:
        /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py::step7_resolve_jury        lines 1431-1555

All tests in this file MUST fail against the current tree because
`build_resolve_jury` does NOT yet exist in `tx_builder.py`. The RED
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
# Helpers (local to this test file; mirror test_select_jury.py style)
# ═════════════════════════════════════════════════════════════════════════


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def _challenge_outputs(builder: TransactionBuilder):
    """Return all continuing outputs at challenge script addr."""
    from simulation.tests.conftest import V13_DEPLOYMENT
    chal_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    return [o for o in builder.outputs if o.address == chal_addr]


def _jury_pool_outputs(builder: TransactionBuilder):
    """Return all continuing outputs at jury_pool script addr."""
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
    """Return {(txid_bytes, idx): Redeemer} for every script-spend
    redeemer attached. Same shape as test_select_jury.py's helper.
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


def _collect_mint_redeemers(builder: TransactionBuilder):
    """Collect mint Redeemers across PyCardano versions — mirrors the
    helper in test_open_challenge.py.
    """
    mint_reds = []
    for attr in ("_minting_script_to_redeemers", "_script_to_redeemers",
                 "_redeemers"):
        val = getattr(builder, attr, None)
        if val is None:
            continue
        if isinstance(val, list):
            for item in val:
                if isinstance(item, tuple) and len(item) == 2:
                    mint_reds.append(item[1])
                elif hasattr(item, "tag") and hasattr(item, "data"):
                    mint_reds.append(item)
        if mint_reds:
            break
    return mint_reds


def _redeemer_cbor_bytes(redeemer) -> bytes:
    """Normalise a Redeemer's payload to CBOR bytes."""
    data = redeemer.data if hasattr(redeemer, "data") else redeemer
    if hasattr(data, "cbor"):
        return bytes(data.cbor)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return cbor2.dumps(data)


def _rewire_resolve_utxo(
    challenge_utxo,
    claim_utxo,
    juror_utxos,
):
    """Re-install tx_builder.resolve_utxo dispatch for a test override.

    Tests that swap in a non-default juror set (AuditorWins / Inconclusive /
    partial / duplicate / wrong_challenge) call this to keep the canonical
    patched_network_for_resolve_jury wiring while routing resolve_utxo
    to the variant fixture set.
    """
    import simulation.tx_builder as tx_mod

    chal_hex = bytes(challenge_utxo.input.transaction_id).hex()
    claim_hex = bytes(claim_utxo.input.transaction_id).hex()
    juror_txid_map = {
        bytes(u.input.transaction_id).hex(): u
        for u in juror_utxos
    }

    def _dispatch(
        txid_hex, idx,
        _ch=challenge_utxo, _cl=claim_utxo, _jm=juror_txid_map,
        _ch_hex=chal_hex, _cl_hex=claim_hex,
    ):
        if txid_hex == _ch_hex:
            return _ch
        if txid_hex == _cl_hex:
            return _cl
        if txid_hex in _jm:
            return _jm[txid_hex]
        raise AssertionError(
            f"override resolve_utxo: unexpected txid {txid_hex}#{idx}"
        )

    tx_mod.resolve_utxo = _dispatch


def _run_build_resolve_jury(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
    *,
    challenge_utxo_override=None,
    claim_utxo_override=None,
    juror_utxos_override=None,
    juror_refs_override=None,
):
    """Invoke build_resolve_jury with canonical fixture inputs.

    Any of the three utxo overrides re-installs `resolve_utxo`
    dispatching. `juror_refs_override` lets tests pass a wrong-length
    (or otherwise malformed) ref list to exercise guards.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    challenge_utxo = challenge_utxo_override or sample_challenge_utxo_voting
    claim_utxo = claim_utxo_override or sample_claim_utxo_challenged
    juror_utxos = juror_utxos_override or sample_revealed_juror_utxos_claimer_wins

    if (challenge_utxo_override is not None
            or claim_utxo_override is not None
            or juror_utxos_override is not None):
        _rewire_resolve_utxo(challenge_utxo, claim_utxo, juror_utxos)

    juror_refs = juror_refs_override or [_utxo_ref_str(u) for u in juror_utxos]

    result = build_resolve_jury(
        mock_ogmios_context,
        sample_deployment_with_all_refs,
        skey, vkey, wallet_addr,
        _utxo_ref_str(challenge_utxo),
        _utxo_ref_str(claim_utxo),
        juror_refs,
        jury_size=default_jury_size,
        jury_fee_rate=default_jury_fee_rate,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_resolve_jury did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# Signature / importability
# ═════════════════════════════════════════════════════════════════════════


def test_build_resolve_jury_is_importable():
    """Catherine MUST expose `build_resolve_jury` at module scope in
    simulation/tx_builder.py. All other tests in this file will fail
    with ImportError until this is satisfied — but we also keep a
    dedicated test so the RED signal is crystal clear.
    """
    from simulation.tx_builder import build_resolve_jury  # noqa: F401
    assert callable(build_resolve_jury), (
        "simulation.tx_builder.build_resolve_jury must be callable"
    )


def test_build_resolve_jury_returns_dict_with_required_keys(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The return dict MUST include tx_hash (str), verdict (str),
    resolved_challenge_ref (str), jury_fee (int), claimer_payout,
    auditor_payout. Downstream step8 (DistributeRewards) and step9
    (CleanupResolved) both reach for these fields without re-querying.
    """
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    required = {
        "tx_hash": str,
        "verdict": str,
        "resolved_challenge_ref": str,
        "jury_fee": int,
    }
    for key, typ in required.items():
        assert key in result, (
            f"result must include {key!r}. Got keys: "
            + ", ".join(sorted(result.keys()))
        )
        assert isinstance(result[key], typ), (
            f"{key!r} must be {typ.__name__}; got "
            f"{type(result[key]).__name__}"
        )
    # Optional-nullable payout keys MUST be present (at least None).
    assert "claimer_payout" in result, (
        "result must include 'claimer_payout' (int or None) so callers "
        "can reconcile the claimer-leg output size"
    )
    assert "auditor_payout" in result, (
        "result must include 'auditor_payout' (int or None) so callers "
        "can reconcile the auditor-leg output size"
    )
    # verdict string must be one of three canonical values
    assert result["verdict"] in {"ClaimerWins", "AuditorWins", "Inconclusive"}, (
        f"verdict must be one of ClaimerWins/AuditorWins/Inconclusive; "
        f"got {result['verdict']!r}"
    )
    # resolved_challenge_ref format
    assert "#" in result["resolved_challenge_ref"], (
        f"resolved_challenge_ref must be '<txid>#<idx>'; "
        f"got {result['resolved_challenge_ref']!r}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer correctness — challenge spend (ResolveJury, Constr3, tag 124)
# ═════════════════════════════════════════════════════════════════════════


def test_challenge_input_carries_ResolveJury_redeemer_tag_124(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The challenge-spend redeemer MUST be CBORTag(124, []) =
    ChallengeAction::ResolveJury (Constr3). Per types.ak ordering and
    v13 line 1461: `resolve_redeemer_cbor = CBORTag(124, [])`.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    red_map = _collect_all_spend_redeemers(builder)
    key = (bytes(sample_challenge_utxo_voting.input.transaction_id),
           sample_challenge_utxo_voting.input.index)
    assert key in red_map, (
        f"Missing spend redeemer for challenge UTxO {key[0].hex()[:12]}"
        f"#{key[1]}. Got keys: "
        f"{[(k[0].hex()[:12], k[1]) for k in red_map.keys()]}"
    )
    decoded = cbor2.loads(_redeemer_cbor_bytes(red_map[key]))
    assert hasattr(decoded, "tag") and decoded.tag == 124, (
        f"Challenge-spend redeemer must be CBORTag(124, []) = "
        f"ResolveJury (Constr3); got {decoded!r}"
    )
    assert list(decoded.value) == [], (
        f"ResolveJury redeemer has no payload (empty Constr); "
        f"got payload {decoded.value!r}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer correctness — claim spend (ForfeitClaim, Constr3, tag 124)
# ═════════════════════════════════════════════════════════════════════════


def test_claim_input_carries_ForfeitClaim_redeemer_tag_124(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The claim-spend redeemer MUST be CBORTag(124, []) =
    ClaimAction::ForfeitClaim (Constr3). V13 line 1462:
    `forfeit_redeemer_cbor = CBORTag(124, [])`. types.ak ClaimAction
    ordering: SubmitClaim(0), WithdrawClaim(1), MarkChallenged(2),
    ForfeitClaim(3).
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    red_map = _collect_all_spend_redeemers(builder)
    key = (bytes(sample_claim_utxo_challenged.input.transaction_id),
           sample_claim_utxo_challenged.input.index)
    assert key in red_map, (
        f"Missing spend redeemer for claim UTxO {key[0].hex()[:12]}"
        f"#{key[1]}. Got keys: "
        f"{[(k[0].hex()[:12], k[1]) for k in red_map.keys()]}"
    )
    decoded = cbor2.loads(_redeemer_cbor_bytes(red_map[key]))
    assert hasattr(decoded, "tag") and decoded.tag == 124, (
        f"Claim-spend redeemer must be CBORTag(124, []) = "
        f"ForfeitClaim (Constr3); got {decoded!r}"
    )
    assert list(decoded.value) == [], (
        f"ForfeitClaim redeemer has no payload (empty Constr); "
        f"got payload {decoded.value!r}"
    )


def test_exactly_two_script_spend_inputs(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """ResolveJury tx spends EXACTLY 2 script inputs: the challenge UTxO
    and the claim UTxO. Juror UTxOs are reference inputs, not spends.
    Both challenge & claim validators enforce `count_script_inputs(.,
    own_hash) == 1` for their respective hashes (challenge.ak:120-122,
    claim.ak:92-94) — so exactly 1 challenge-script spend + exactly 1
    claim-script spend = 2 total.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    red_map = _collect_all_spend_redeemers(builder)
    assert len(red_map) == 2, (
        f"Expected exactly 2 script spend inputs (challenge + claim); "
        f"got {len(red_map)} with keys "
        f"{[(k[0].hex()[:12], k[1]) for k in red_map.keys()]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Mint — claim burn (tag 124 = ForfeitClaim on the claim policy)
# ═════════════════════════════════════════════════════════════════════════


def test_claim_token_burned_exactly_one(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The claim NFT MUST be burned (qty = -1) under the claim policy.
    Validator challenge.ak:601-605 uses `list.any(tokens, qty == -1)`;
    claim.ak:442 requires `exactly_one_burned`. V13 lines 1478-1480 set
    `burn_ma[claim_policy] = {token: -1}`.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    mint_ma = builder.mint
    assert mint_ma is not None, (
        "builder.mint must be set — ResolveJury burns the claim NFT"
    )
    claim_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["claim"]))
    items = dict(mint_ma.data.items()) if hasattr(mint_ma, "data") else dict(mint_ma.items())
    assert claim_policy in items, (
        f"Mint must include the claim policy {claim_policy}; "
        f"got policies {list(items.keys())}"
    )
    claim_assets = items[claim_policy]
    assert len(claim_assets) == 1, (
        f"Exactly one claim NFT asset must be burned; got {len(claim_assets)}"
    )
    qty = list(claim_assets.values())[0]
    assert qty == -1, f"Claim NFT mint quantity must be -1 (burn); got {qty}"


def test_challenge_token_NOT_burned(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The challenge NFT MUST be PRESERVED (not burned) during
    ResolveJury — it stays on the continuing Resolved challenge UTxO
    for DistributeRewards to reference. CleanupResolved (step 9) burns
    it later. Validator challenge.ak:71-73 fix comment:
    "ResolveJury no longer burns the challenge token."
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    mint_ma = builder.mint
    if mint_ma is None:
        return  # no mint at all is fine
    challenge_policy = ScriptHash(
        bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"])
    )
    items = dict(mint_ma.data.items()) if hasattr(mint_ma, "data") else dict(mint_ma.items())
    assert challenge_policy not in items, (
        f"Challenge policy MUST NOT appear in tx.mint. Challenge token "
        f"is preserved on continuing Resolved UTxO; CleanupResolved "
        f"burns it later. Got: {list(items.keys())}"
    )


def test_claim_mint_redeemer_is_ForfeitClaim_tag_124(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The claim-policy mint redeemer (for the burn) must be
    CBORTag(124, []) = ForfeitClaim — the same tag the claim spend
    uses. V13 reuses `forfeit_redeemer_cbor` for both spend and mint
    (lines 1529-1530).
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    mint_reds = _collect_mint_redeemers(builder)
    assert mint_reds, (
        "Expected at least one mint redeemer (the claim-burn redeemer); "
        "got none attached to the builder."
    )
    # At least one of the mint redeemers must have tag 124.
    tags = []
    for r in mint_reds:
        try:
            decoded = cbor2.loads(_redeemer_cbor_bytes(r))
            if hasattr(decoded, "tag"):
                tags.append(decoded.tag)
        except Exception:
            continue
    assert 124 in tags, (
        f"At least one mint redeemer must be CBORTag(124, ..) = "
        f"ForfeitClaim (the claim-burn redeemer); got tags {tags}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Continuing challenge output — state transition + preservation
# ═════════════════════════════════════════════════════════════════════════


def test_exactly_one_continuing_output_at_challenge_addr(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """`single_challenge_output` guard (Finding-002-F3, challenge.ak:
    624-627). Exactly one output MAY appear at the challenge script
    address — extra outputs there (even valueless) fail validation.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    outs = _challenge_outputs(builder)
    assert len(outs) == 1, (
        f"Exactly ONE continuing output must appear at challenge script "
        f"addr (single_challenge_output guard, challenge.ak:624-627); "
        f"got {len(outs)}. All output addrs: "
        f"{[str(o.address) for o in builder.outputs]}"
    )


def test_continuing_challenge_datum_state_is_Resolved_tag_124(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """field[9] of the continuing challenge datum must be
    CBORTag(124, [verdict]) = Resolved { verdict }. With the all-CW
    juror set, verdict should be ClaimerWins = CBORTag(121, []).
    Validator line 567: `updated.state == Resolved { verdict }`.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    (chal_out,) = _challenge_outputs(builder)
    fields = _decode_datum(chal_out).value
    state = fields[9]
    assert hasattr(state, "tag") and state.tag == 124, (
        f"Continuing challenge datum field[9] must be Resolved "
        f"= CBORTag(124, [verdict]); got {state!r}"
    )
    assert isinstance(state.value, list) and len(state.value) == 1, (
        f"Resolved payload must be [verdict]; got {state.value!r}"
    )
    verdict = state.value[0]
    assert hasattr(verdict, "tag") and verdict.tag == 121, (
        f"Verdict in all-ClaimerWins scenario must be "
        f"CBORTag(121, []) = ClaimerWins; got {verdict!r}"
    )


def test_continuing_challenge_datum_fields_0_through_8_preserved(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Fields 0-8 of the continuing challenge datum MUST be preserved
    byte-identically from the input challenge datum. Only field[9]
    (state) flips from Voting -> Resolved. Validator lines 569-577.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    (chal_out,) = _challenge_outputs(builder)
    out_fields = _decode_datum(chal_out).value
    in_fields = _decode_datum(sample_challenge_utxo_voting.output).value
    for idx in range(9):  # 0..8
        assert out_fields[idx] == in_fields[idx], (
            f"Continuing challenge datum field[{idx}] not preserved. "
            f"Input: {in_fields[idx]!r}. Output: {out_fields[idx]!r}"
        )


def test_continuing_challenge_output_preserves_challenge_nft(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    sample_challenge_token_bytes,
    default_jury_size, default_jury_fee_rate,
):
    """The challenge NFT must be preserved on the continuing Resolved
    output (qty == 1 under challenge policy). Validator lines 579-583:
    `assets.quantity_of(out.value, refs.challenge_validator_hash, tn) == 1`.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    (chal_out,) = _challenge_outputs(builder)
    challenge_policy = ScriptHash(
        bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"])
    )
    ma = chal_out.amount.multi_asset
    assert challenge_policy in ma, (
        f"Continuing challenge output missing challenge policy "
        f"{challenge_policy}; got policies {list(ma.keys())}"
    )
    expected_an = AssetName(sample_challenge_token_bytes)
    assets = ma[challenge_policy]
    assert expected_an in assets, (
        f"Continuing challenge output missing challenge token "
        f"{sample_challenge_token_bytes.hex()[:16]}..."
    )
    assert assets[expected_an] == 1, (
        f"Challenge NFT qty must be 1; got {assets[expected_an]}"
    )


def test_continuing_challenge_output_coin_equals_auditor_stake(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """The continuing challenge output's coin MUST equal ch.stake_amount
    (the AUDITOR's stake). Validator line 587:
    `assets.lovelace_of(out.value) == ch.stake_amount`. The auditor
    stake rides on the continuing Resolved UTxO so DistributeRewards
    can consume it to pay jurors in step 8.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    (chal_out,) = _challenge_outputs(builder)
    assert chal_out.amount.coin == default_stake_amount, (
        f"Continuing challenge output coin must equal auditor stake "
        f"({default_stake_amount}); got {chal_out.amount.coin}. "
        f"Validator challenge.ak:587 uses == not >=, so padding fails."
    )


# ═════════════════════════════════════════════════════════════════════════
# Per-outcome stake distribution — ClaimerWins
# ═════════════════════════════════════════════════════════════════════════


def test_claimer_wins_produces_claimer_payout_output(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet, sample_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """ClaimerWins: exactly one output at claimer_credential with
    coin == claim_stake + auditor_stake - jury_fee.
    Validator line 1042: `lovelace_of(o.value) == claimer_payout`.
    V13 line 1484: `claimer_payout = STAKE_AMOUNT + STAKE_AMOUNT - jury_fee`.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    _, claimer_vkey, claimer_addr = sample_wallet
    expected_jury_fee = default_stake_amount * default_jury_fee_rate // 10000
    expected_claimer_payout = (
        default_stake_amount + default_stake_amount - expected_jury_fee
    )
    # Find an output whose payment credential matches claimer's vkh,
    # with EXACT lovelace equality to claimer_payout.
    claimer_vkh = bytes(claimer_vkey.hash())
    hits = []
    for o in builder.outputs:
        pc = o.address.payment_part
        # pc may be VerificationKeyHash or similar; compare bytes.
        if pc is None:
            continue
        pc_bytes = bytes(pc) if not isinstance(pc, (bytes, bytearray)) else bytes(pc)
        if pc_bytes == claimer_vkh and o.amount.coin == expected_claimer_payout:
            hits.append(o)
    assert len(hits) >= 1, (
        f"Expected at least one output at claimer vkh "
        f"{claimer_vkh.hex()[:16]}... with EXACT coin == "
        f"{expected_claimer_payout} (= {default_stake_amount} + "
        f"{default_stake_amount} - {expected_jury_fee}). "
        f"Got outputs: "
        f"{[(str(o.address)[:30], o.amount.coin) for o in builder.outputs]}"
    )


def test_claimer_wins_jury_fee_routed_to_jury_pool_hash(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """ClaimerWins: jury_fee goes to an output at jury_pool_hash.
    jury_fee = auditor_stake * rate / 10000 (the LOSER's stake).
    Validator lines 1046-1056 (`fee_routed`). NEW-R2-03 patch.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    jury_pool_hash = ScriptHash(
        bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"])
    )
    expected_fee = default_stake_amount * default_jury_fee_rate // 10000
    hits = [
        o for o in builder.outputs
        if isinstance(o.address.payment_part, ScriptHash)
        and o.address.payment_part == jury_pool_hash
        and o.amount.coin == expected_fee
    ]
    assert len(hits) >= 1, (
        f"Expected at least one output at jury_pool script hash "
        f"{jury_pool_hash} with EXACT coin == {expected_fee} "
        f"(auditor_stake * {default_jury_fee_rate} / 10000). "
        f"Got outputs: "
        f"{[(str(o.address)[:30], o.amount.coin) for o in builder.outputs]}"
    )


def test_claimer_wins_no_padding_exact_equality(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet, sample_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Validator uses `==` not `>=`. Padding the claimer_payout output
    with even 1 extra lovelace (e.g. to satisfy min-UTxO) would fail
    on-chain. V13 patched an earlier padding bug; the final v13 script
    uses pure lovelace `payout_value = claimer_payout`. Test enforces
    byte-level equality and rejects ANY adjacent claimer output
    with a different coin.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    _, claimer_vkey, _ = sample_wallet
    claimer_vkh = bytes(claimer_vkey.hash())
    expected_jury_fee = default_stake_amount * default_jury_fee_rate // 10000
    expected_claimer_payout = (
        default_stake_amount + default_stake_amount - expected_jury_fee
    )
    claimer_outs = [
        o for o in builder.outputs
        if o.address.payment_part is not None
        and bytes(o.address.payment_part) == claimer_vkh
    ]
    # Every claimer-addressed output (ignoring wallet change)
    # must EITHER match expected_claimer_payout exactly or be
    # clearly flagged as something else (change). We require at
    # least one exact match AND disallow a near-miss (e.g. +1200000
    # min-UTxO padding).
    exacts = [o for o in claimer_outs if o.amount.coin == expected_claimer_payout]
    near_misses = [
        o for o in claimer_outs
        if abs(o.amount.coin - expected_claimer_payout) in range(1, 5_000_001)
    ]
    assert len(exacts) == 1, (
        f"Expected EXACTLY one claimer-addressed output with coin "
        f"== {expected_claimer_payout}; got {len(exacts)}."
    )
    assert not near_misses, (
        f"No claimer-addressed output may have coin within 5M lovelace "
        f"of {expected_claimer_payout} (padding indicates ==/>= bug). "
        f"Near-misses: {[o.amount.coin for o in near_misses]}"
    )


def test_claimer_wins_no_auditor_payout_output(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet, sample_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """In ClaimerWins, the auditor LOSES their stake — the builder MUST
    NOT emit an output at the auditor's credential carrying any of the
    distribution amounts (claim_stake, auditor_stake, half_fee-style,
    etc). Validator's verify_jury_distribution ClaimerWins branch
    (lines 1034-1060) checks only claimer + jury_pool — extra auditor
    payouts would leave the tx imbalanced.

    Note: the auditor MAY appear in builder.outputs as the fee-payer /
    change address (they're also the wallet paying TX fees in our
    fixture). The assertion here is that there is NO auditor-addressed
    output whose coin matches any distribution amount the auditor
    would receive in a different branch.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    _, auditor_vkey, _ = sample_auditor_wallet
    auditor_vkh = bytes(auditor_vkey.hash())
    # Amounts the auditor should NEVER receive in ClaimerWins:
    jury_fee = default_stake_amount * default_jury_fee_rate // 10000
    half_total_fee = (2 * default_stake_amount * default_jury_fee_rate // 10000) // 2
    forbidden_amounts = {
        default_stake_amount + default_stake_amount - jury_fee,  # auditor-wins payout
        default_stake_amount - half_total_fee,                   # inconclusive auditor share
        default_stake_amount,                                    # bare auditor-stake return
    }
    for o in builder.outputs:
        pc = o.address.payment_part
        if pc is None:
            continue
        if bytes(pc) == auditor_vkh and o.amount.coin in forbidden_amounts:
            raise AssertionError(
                f"ClaimerWins tx contains an auditor-addressed output "
                f"with coin {o.amount.coin} matching a distribution "
                f"amount that only applies in AuditorWins / Inconclusive."
            )


# ═════════════════════════════════════════════════════════════════════════
# Per-outcome stake distribution — AuditorWins
# ═════════════════════════════════════════════════════════════════════════


def test_auditor_wins_produces_auditor_payout_output(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet, sample_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_auditor_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """AuditorWins: exactly one output at auditor_credential with
    coin == auditor_stake + claim_stake - jury_fee, where jury_fee
    = claim_stake * rate / 10000. Validator lines 1065-1070.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,  # shadowed by override below
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_auditor_wins,
    )
    _, auditor_vkey, _ = sample_auditor_wallet
    expected_jury_fee = default_stake_amount * default_jury_fee_rate // 10000
    expected_auditor_payout = (
        default_stake_amount + default_stake_amount - expected_jury_fee
    )
    auditor_vkh = bytes(auditor_vkey.hash())
    hits = [
        o for o in builder.outputs
        if o.address.payment_part is not None
        and bytes(o.address.payment_part) == auditor_vkh
        and o.amount.coin == expected_auditor_payout
    ]
    assert len(hits) >= 1, (
        f"AuditorWins: expected an output at auditor vkh with EXACT "
        f"coin == {expected_auditor_payout}. "
        f"Got outputs: "
        f"{[(str(o.address)[:30], o.amount.coin) for o in builder.outputs]}"
    )


def test_auditor_wins_result_verdict_is_AuditorWins(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_auditor_wins,
    default_jury_size, default_jury_fee_rate,
):
    """The builder-returned verdict string must equal "AuditorWins"
    when all 5 jurors revealed AuditorWins.
    """
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_auditor_wins,
    )
    assert result["verdict"] == "AuditorWins", (
        f"All-AuditorWins juror set must produce verdict='AuditorWins'; "
        f"got {result['verdict']!r}"
    )


def test_auditor_wins_datum_state_is_Resolved_AuditorWins_tag_122(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_auditor_wins,
    default_jury_size, default_jury_fee_rate,
):
    """AuditorWins path: continuing challenge datum[9] = Resolved(AuditorWins)
    = CBORTag(124, [CBORTag(122, [])]).
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_auditor_wins,
    )
    (chal_out,) = _challenge_outputs(builder)
    state = _decode_datum(chal_out).value[9]
    assert state.tag == 124, (
        f"field[9] must be Resolved (tag 124); got tag {state.tag}"
    )
    verdict = state.value[0]
    assert verdict.tag == 122, (
        f"Verdict must be AuditorWins (tag 122); got tag {verdict.tag}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Per-outcome stake distribution — Inconclusive
# ═════════════════════════════════════════════════════════════════════════


def test_inconclusive_produces_claimer_and_auditor_outputs(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet, sample_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_inconclusive,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Inconclusive: BOTH claimer and auditor get their stake back,
    each minus half of the total jury fee. Validator lines 1089-1124:
        total_jury_fee = (claim_stake + auditor_stake) * rate / 10000
        half_fee = total_jury_fee / 2
        claimer_out == claim_stake - half_fee
        auditor_out == auditor_stake - half_fee
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_inconclusive,
    )
    _, claimer_vkey, _ = sample_wallet
    _, auditor_vkey, _ = sample_auditor_wallet
    total_jury_fee = (
        (default_stake_amount + default_stake_amount)
        * default_jury_fee_rate // 10000
    )
    half_fee = total_jury_fee // 2
    expected_claimer_coin = default_stake_amount - half_fee
    expected_auditor_coin = default_stake_amount - half_fee

    claimer_vkh = bytes(claimer_vkey.hash())
    auditor_vkh = bytes(auditor_vkey.hash())
    claimer_hits = [
        o for o in builder.outputs
        if o.address.payment_part is not None
        and bytes(o.address.payment_part) == claimer_vkh
        and o.amount.coin == expected_claimer_coin
    ]
    auditor_hits = [
        o for o in builder.outputs
        if o.address.payment_part is not None
        and bytes(o.address.payment_part) == auditor_vkh
        and o.amount.coin == expected_auditor_coin
    ]
    assert claimer_hits, (
        f"Inconclusive: expected a claimer output with coin == "
        f"{expected_claimer_coin} (claim_stake - half_fee = "
        f"{default_stake_amount} - {half_fee})"
    )
    assert auditor_hits, (
        f"Inconclusive: expected an auditor output with coin == "
        f"{expected_auditor_coin}"
    )


def test_inconclusive_total_fee_routed_to_jury_pool(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_inconclusive,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Inconclusive: the FULL total_jury_fee (not half!) lands at
    jury_pool_hash. Validator lines 1108-1118: the fee output holds
    `== total_jury_fee`, not half. Only the claimer/auditor outputs
    are halved.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_inconclusive,
    )
    jury_pool_hash = ScriptHash(
        bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"])
    )
    total_jury_fee = (
        (default_stake_amount + default_stake_amount)
        * default_jury_fee_rate // 10000
    )
    hits = [
        o for o in builder.outputs
        if isinstance(o.address.payment_part, ScriptHash)
        and o.address.payment_part == jury_pool_hash
        and o.amount.coin == total_jury_fee
    ]
    assert hits, (
        f"Inconclusive: expected jury_pool output with coin == "
        f"total_jury_fee ({total_jury_fee}). Got jury_pool outputs: "
        f"{[o.amount.coin for o in builder.outputs if isinstance(o.address.payment_part, ScriptHash) and o.address.payment_part == jury_pool_hash]}"
    )


def test_inconclusive_result_verdict_is_Inconclusive(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_inconclusive,
    default_jury_size, default_jury_fee_rate,
):
    """2 CW + 2 AW + 1 IC -> tally = Inconclusive (neither hits 3).
    Validator tally_revealed_votes falls through to Inconclusive
    (challenge.ak:968-970).
    """
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_inconclusive,
    )
    assert result["verdict"] == "Inconclusive", (
        f"2/2/1 split must tally to Inconclusive; got {result['verdict']!r}"
    )


def test_inconclusive_datum_state_is_Resolved_Inconclusive_tag_123(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_inconclusive,
    default_jury_size, default_jury_fee_rate,
):
    """Inconclusive path: continuing datum[9] = Resolved(Inconclusive)
    = CBORTag(124, [CBORTag(123, [])]).
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=sample_revealed_juror_utxos_inconclusive,
    )
    (chal_out,) = _challenge_outputs(builder)
    state = _decode_datum(chal_out).value[9]
    assert state.tag == 124
    verdict = state.value[0]
    assert verdict.tag == 123, (
        f"Verdict must be Inconclusive (tag 123); got tag {verdict.tag}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Verdict result key matches distribution
# ═════════════════════════════════════════════════════════════════════════


def test_claimer_wins_result_verdict_is_ClaimerWins(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """All-ClaimerWins juror set -> result["verdict"] == "ClaimerWins"."""
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    assert result["verdict"] == "ClaimerWins"


def test_claimer_wins_result_jury_fee_is_correct(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Builder-returned jury_fee in ClaimerWins branch:
    result["jury_fee"] == auditor_stake * rate / 10000.
    """
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    expected = default_stake_amount * default_jury_fee_rate // 10000
    assert result["jury_fee"] == expected, (
        f"ClaimerWins: jury_fee result field must equal "
        f"auditor_stake*rate/10000 = {expected}; got {result['jury_fee']}"
    )


def test_claimer_wins_result_claimer_payout_is_correct(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Builder-returned claimer_payout matches claim + auditor - fee."""
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    expected_fee = default_stake_amount * default_jury_fee_rate // 10000
    expected_payout = default_stake_amount + default_stake_amount - expected_fee
    assert result["claimer_payout"] == expected_payout


def test_claimer_wins_result_auditor_payout_is_None(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Claimer-wins: auditor gets NOTHING. result["auditor_payout"] is None."""
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    assert result["auditor_payout"] is None, (
        f"ClaimerWins: auditor_payout must be None (auditor loses "
        f"stake entirely); got {result['auditor_payout']!r}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Reference inputs — cross_refs, params, 5 revealed jurors
# ═════════════════════════════════════════════════════════════════════════


def test_reference_inputs_include_cross_refs_and_params(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    sample_cross_refs_utxo,
    sample_params_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """cross_refs UTxO and params UTxO MUST appear as reference inputs.
    V13 lines 1515-1516: `b.reference_inputs.add(cross_refs_utxo);
    b.reference_inputs.add(params_utxo)`.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
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
    }
    missing = wanted - ref_ids
    assert not missing, (
        f"reference_inputs missing: "
        f"{[(t.hex()[:12], i) for (t, i) in missing]}. "
        f"Got: {[(t.hex()[:12], i) for (t, i) in ref_ids]}"
    )


def test_reference_inputs_include_all_five_juror_utxos(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """All 5 revealed juror UTxOs MUST appear as reference_inputs.
    Validator iterates tx.reference_inputs at challenge.ak:491 to tally
    votes. V13 line 1517-1518 loops over juror_ref_utxos.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    ref_ids = {
        (bytes(getattr(r, "input", r).transaction_id),
         getattr(r, "input", r).index)
        for r in builder.reference_inputs
    }
    wanted = {
        (bytes(u.input.transaction_id), u.input.index)
        for u in sample_revealed_juror_utxos_claimer_wins
    }
    missing = wanted - ref_ids
    assert not missing, (
        f"reference_inputs missing juror UTxOs: "
        f"{[(t.hex()[:12], i) for (t, i) in missing]}. Got ref_ids: "
        f"{[(t.hex()[:12], i) for (t, i) in ref_ids]}"
    )


def test_juror_utxos_are_NOT_in_spend_inputs(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Juror UTxOs are REFERENCE inputs, not spent. None of them
    should appear in the spend-redeemer map. Spending them would
    cause the jury_pool validator to fire (it isn't prepared for a
    ResolveJury context) and also destroy the juror identities needed
    for step 8 DistributeRewards.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    red_map = _collect_all_spend_redeemers(builder)
    red_keys = set(red_map.keys())
    for u in sample_revealed_juror_utxos_claimer_wins:
        key = (bytes(u.input.transaction_id), u.input.index)
        assert key not in red_keys, (
            f"Juror UTxO {key[0].hex()[:12]}#{key[1]} should be a "
            f"REFERENCE input, not a spend input. Got spend inputs: "
            f"{[(k[0].hex()[:12], k[1]) for k in red_keys]}"
        )


# ═════════════════════════════════════════════════════════════════════════
# Builder topology — signer, validity window
# ═════════════════════════════════════════════════════════════════════════


def test_permissionless_no_oracle_signer(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """ResolveJury is PERMISSIONLESS in Phase 1.1 (challenge.ak:629-630).
    Required_signers MUST NOT include an oracle vkh — only the fee-
    payer's vkh is permitted. V13 attaches only the wallet signer
    (via build_and_sign's signing_keys arg, not required_signers).
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    rs = builder.required_signers or []
    _, vkey, _ = sample_auditor_wallet
    wallet_vkh = bytes(vkey.hash())
    extras = [bytes(s) for s in rs if bytes(s) != wallet_vkh]
    assert len(extras) == 0, (
        f"ResolveJury is permissionless — no extra (oracle / juror) "
        f"signers should appear. Got extras: {[s.hex() for s in extras]}"
    )


def test_ttl_and_validity_start_set(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Builder must set validity_start and ttl for submission safety.
    No time GATE exists on ResolveJury itself; bounds only need to be
    sensible. V13 uses ~3660-slot window (validity_start - 60 .. +3600).
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    assert builder.validity_start is not None, (
        "builder.validity_start must be set for submission safety"
    )
    assert builder.ttl is not None, "builder.ttl must be set"
    assert builder.ttl > builder.validity_start, (
        f"ttl ({builder.ttl}) must be > validity_start "
        f"({builder.validity_start})"
    )
    assert builder.ttl - builder.validity_start <= 7200, (
        f"validity window of {builder.ttl - builder.validity_start} "
        f"slots is unreasonably wide (v13 uses ~3660)"
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards — builder must fail fast
# ═════════════════════════════════════════════════════════════════════════


def test_raises_if_juror_ref_count_not_equal_to_jury_size(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """On-chain `votes_complete` (challenge.ak:537) requires
    vote_count == jury_size. The builder MUST catch a wrong-length
    juror ref list before submitting.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    short_refs = [
        _utxo_ref_str(u) for u in sample_revealed_juror_utxos_claimer_wins[:4]
    ]
    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)
    claim_ref = _utxo_ref_str(sample_claim_utxo_challenged)

    with pytest.raises(
        ValueError,
        match=r"(?i)jury.?size|juror.*count|len\(|length|votes.*complete",
    ):
        build_resolve_jury(
            mock_ogmios_context,
            sample_deployment_with_all_refs,
            skey, vkey, wallet_addr,
            chal_ref, claim_ref,
            short_refs,
            jury_size=default_jury_size,
            jury_fee_rate=default_jury_fee_rate,
        )


def test_raises_if_challenge_state_not_voting(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_pending_jury,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Validator challenge.ak:470-472 fails with
    @"challenge: ResolveJury requires Voting state" if ch.state !=
    Voting. Client MUST refuse fast. We pass the PendingJury-state
    challenge UTxO fixture.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    _rewire_resolve_utxo(
        sample_challenge_utxo_pending_jury,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
    )

    chal_ref = _utxo_ref_str(sample_challenge_utxo_pending_jury)
    claim_ref = _utxo_ref_str(sample_claim_utxo_challenged)
    juror_refs = [_utxo_ref_str(u) for u in sample_revealed_juror_utxos_claimer_wins]

    with pytest.raises(
        ValueError,
        match=r"(?i)voting|challenge.*state|pending.?jury|not.*in.*voting",
    ):
        build_resolve_jury(
            mock_ogmios_context,
            sample_deployment_with_all_refs,
            skey, vkey, wallet_addr,
            chal_ref, claim_ref,
            juror_refs,
            jury_size=default_jury_size,
            jury_fee_rate=default_jury_fee_rate,
        )


def test_raises_if_claim_state_not_challenged(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo,  # Open-state claim UTxO
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Validator claim.ak:427-430 requires clm.state == Challenged.
    Client MUST refuse fast if pointed at an Open (or Validated /
    Invalidated) claim UTxO.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    _rewire_resolve_utxo(
        sample_challenge_utxo_voting,
        sample_claim_utxo,
        sample_revealed_juror_utxos_claimer_wins,
    )

    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)
    claim_ref = _utxo_ref_str(sample_claim_utxo)  # Open state, wrong
    juror_refs = [_utxo_ref_str(u) for u in sample_revealed_juror_utxos_claimer_wins]

    with pytest.raises(
        ValueError,
        match=r"(?i)challenged|claim.*state|open|forfeit",
    ):
        build_resolve_jury(
            mock_ogmios_context,
            sample_deployment_with_all_refs,
            skey, vkey, wallet_addr,
            chal_ref, claim_ref,
            juror_refs,
            jury_size=default_jury_size,
            jury_fee_rate=default_jury_fee_rate,
        )


def test_raises_if_duplicate_juror_did(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_duplicate_did,
    default_jury_size, default_jury_fee_rate,
):
    """Validator challenge.ak:544-546 `no_duplicates` check: two juror
    refs with the same juror_did would pass the per-juror filter but
    fail the set-size guard. Client MUST refuse fast.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    _rewire_resolve_utxo(
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_duplicate_did,
    )

    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)
    claim_ref = _utxo_ref_str(sample_claim_utxo_challenged)
    juror_refs = [_utxo_ref_str(u) for u in sample_revealed_juror_utxos_duplicate_did]

    with pytest.raises(
        ValueError,
        match=r"(?i)duplicate|unique|same.*juror|no.?duplicates|repeated.*did",
    ):
        build_resolve_jury(
            mock_ogmios_context,
            sample_deployment_with_all_refs,
            skey, vkey, wallet_addr,
            chal_ref, claim_ref,
            juror_refs,
            jury_size=default_jury_size,
            jury_fee_rate=default_jury_fee_rate,
        )


def test_raises_if_juror_active_case_mismatches_challenge(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_wrong_challenge,
    default_jury_size, default_jury_fee_rate,
):
    """Juror UTxOs whose active_case is bound to a DIFFERENT challenge
    will be SILENTLY SKIPPED by the validator's filter_map (line 512-
    524), so vote_count == 0 and votes_not_empty (line 536) fails.
    Client MUST detect the mismatch and refuse fast with a clear
    error — don't wait for an opaque on-chain failure.
    """
    from simulation.tx_builder import build_resolve_jury

    skey, vkey, wallet_addr = sample_auditor_wallet
    _rewire_resolve_utxo(
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_wrong_challenge,
    )

    chal_ref = _utxo_ref_str(sample_challenge_utxo_voting)
    claim_ref = _utxo_ref_str(sample_claim_utxo_challenged)
    juror_refs = [_utxo_ref_str(u) for u in sample_revealed_juror_utxos_wrong_challenge]

    with pytest.raises(
        ValueError,
        match=r"(?i)active.?case|challenge.*token|wrong.*challenge|mismatch",
    ):
        build_resolve_jury(
            mock_ogmios_context,
            sample_deployment_with_all_refs,
            skey, vkey, wallet_addr,
            chal_ref, claim_ref,
            juror_refs,
            jury_size=default_jury_size,
            jury_fee_rate=default_jury_fee_rate,
        )


# ═════════════════════════════════════════════════════════════════════════
# Input topology — challenge and claim are SPENT (not reference)
# ═════════════════════════════════════════════════════════════════════════


def test_challenge_utxo_is_a_spend_input(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Challenge UTxO MUST be a spend input (so the ResolveJury
    validator runs). Reference-only would skip validation entirely.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    red_map = _collect_all_spend_redeemers(builder)
    key = (bytes(sample_challenge_utxo_voting.input.transaction_id),
           sample_challenge_utxo_voting.input.index)
    assert key in red_map, (
        f"Challenge UTxO {key[0].hex()[:12]}#{key[1]} must be a spend "
        f"input (not reference). Got spend keys: "
        f"{[(k[0].hex()[:12], k[1]) for k in red_map.keys()]}"
    )


def test_claim_utxo_is_a_spend_input(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Claim UTxO MUST be a spend input so validate_forfeit_claim runs."""
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    red_map = _collect_all_spend_redeemers(builder)
    key = (bytes(sample_claim_utxo_challenged.input.transaction_id),
           sample_claim_utxo_challenged.input.index)
    assert key in red_map, (
        f"Claim UTxO {key[0].hex()[:12]}#{key[1]} must be a spend "
        f"input. Got spend keys: "
        f"{[(k[0].hex()[:12], k[1]) for k in red_map.keys()]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Returned tx_hash threading
# ═════════════════════════════════════════════════════════════════════════


def test_resolved_challenge_ref_uses_returned_tx_hash(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """resolved_challenge_ref must be '<tx_hash>#0' — the continuing
    challenge output is the FIRST output added (v13 line 1521). That
    convention lets step 8 DistributeRewards and step 9 CleanupResolved
    locate the Resolved UTxO by concatenating the tx_hash with #0.
    """
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    assert result["resolved_challenge_ref"] == f"{result['tx_hash']}#0", (
        f"resolved_challenge_ref must be '{{tx_hash}}#0'; got "
        f"{result['resolved_challenge_ref']!r} vs tx_hash "
        f"{result['tx_hash']!r}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Parametrised sanity — all three verdicts produce sensible outputs
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "jurors_fixture_name, expected_verdict",
    [
        ("sample_revealed_juror_utxos_claimer_wins", "ClaimerWins"),
        ("sample_revealed_juror_utxos_auditor_wins", "AuditorWins"),
        ("sample_revealed_juror_utxos_inconclusive", "Inconclusive"),
    ],
)
def test_verdict_tallied_correctly_from_juror_reveals(
    jurors_fixture_name, expected_verdict,
    request,
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    default_jury_size, default_jury_fee_rate,
):
    """Parametrised: every juror-fixture variant -> expected verdict.
    Defends against a bug where the tally logic reads the wrong
    Verdict tag (e.g. off-by-one vs. Constr ordering).
    """
    jurors = request.getfixturevalue(jurors_fixture_name)
    result, _ = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=jurors,
    )
    assert result["verdict"] == expected_verdict, (
        f"Juror fixture {jurors_fixture_name} should tally to "
        f"{expected_verdict!r}; got {result['verdict']!r}"
    )


@pytest.mark.parametrize(
    "jurors_fixture_name, expected_verdict_tag",
    [
        ("sample_revealed_juror_utxos_claimer_wins", 121),
        ("sample_revealed_juror_utxos_auditor_wins", 122),
        ("sample_revealed_juror_utxos_inconclusive", 123),
    ],
)
def test_resolved_datum_carries_correct_verdict_tag(
    jurors_fixture_name, expected_verdict_tag,
    request,
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    default_jury_size, default_jury_fee_rate,
):
    """Parametrised: the Resolved datum's inner Verdict tag MUST match
    the expected Constr index (121 / 122 / 123).
    """
    jurors = request.getfixturevalue(jurors_fixture_name)
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins=None,
        default_jury_size=default_jury_size, default_jury_fee_rate=default_jury_fee_rate,
        juror_utxos_override=jurors,
    )
    (chal_out,) = _challenge_outputs(builder)
    state = _decode_datum(chal_out).value[9]
    assert state.tag == 124, (
        f"state must be Resolved (tag 124); got {state.tag}"
    )
    verdict = state.value[0]
    assert verdict.tag == expected_verdict_tag, (
        f"{jurors_fixture_name}: verdict tag must be "
        f"{expected_verdict_tag}; got {verdict.tag}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Misc — fee payer and builder shape
# ═════════════════════════════════════════════════════════════════════════


def test_challenge_and_claim_ref_scripts_attached(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    sample_challenge_ref_script_utxo,
    sample_claim_ref_script_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """The challenge and claim ref-script UTxOs (they carry the
    PlutusV3Script bytes) must appear in the TX so PyCardano can wire
    up the reference-script input. V13 passes them into
    add_script_input as the second positional arg (line 1512, 1513).
    In PyCardano these show up either in reference_inputs or as
    inline script references; we assert they are at least present
    SOMEWHERE in the builder's input/ref sets.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    # Collect all txid/idx pairs from both inputs and reference_inputs.
    all_ids = set()
    for r in list(builder.inputs) + list(builder.reference_inputs):
        inp = getattr(r, "input", r)
        all_ids.add((bytes(inp.transaction_id), inp.index))
    wanted = {
        (bytes(sample_challenge_ref_script_utxo.input.transaction_id),
         sample_challenge_ref_script_utxo.input.index),
        (bytes(sample_claim_ref_script_utxo.input.transaction_id),
         sample_claim_ref_script_utxo.input.index),
    }
    missing = wanted - all_ids
    assert not missing, (
        f"Ref-script UTxOs must be attached as either inputs or "
        f"reference_inputs (pycardano dispatches script resolution "
        f"either way). Missing: "
        f"{[(t.hex()[:12], i) for (t, i) in missing]}"
    )


def test_fee_payer_wallet_utxo_included_as_non_script_input(
    patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
    sample_deployment_with_all_refs,
    sample_auditor_wallet,
    sample_auditor_wallet_utxo_base_ap3x,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    default_jury_size, default_jury_fee_rate,
):
    """Fee-payer wallet UTxO must be an input (for fees + change).
    V13 line 1514: `for u in wallet_utxos: b.add_input(u)`.
    """
    _, builder = _run_build_resolve_jury(
        patched_network_for_resolve_jury, captured_builder, mock_ogmios_context,
        sample_deployment_with_all_refs,
        sample_auditor_wallet,
        sample_challenge_utxo_voting,
        sample_claim_utxo_challenged,
        sample_revealed_juror_utxos_claimer_wins,
        default_jury_size, default_jury_fee_rate,
    )
    wallet_id = (bytes(sample_auditor_wallet_utxo_base_ap3x.input.transaction_id),
                 sample_auditor_wallet_utxo_base_ap3x.input.index)
    input_ids = {
        (bytes(getattr(i, "input", i).transaction_id),
         getattr(i, "input", i).index)
        for i in builder.inputs
    }
    assert wallet_id in input_ids, (
        f"Fee-payer wallet UTxO {wallet_id[0].hex()[:12]}#{wallet_id[1]} "
        f"must be an input. Got input_ids: "
        f"{[(t.hex()[:12], i) for (t, i) in input_ids]}"
    )
