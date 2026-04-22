"""
RED-phase tests for `simulation.tx_builder.build_distribute_rewards`.

Function under test (NOT YET IMPLEMENTED):
    build_distribute_rewards(
        context, deployment,
        skey, vkey, wallet_addr,
        juror_utxo_ref: str,                     # "<txid>#<idx>" of post-reveal JurorDatum UTxO
        resolved_challenge_utxo_ref: str,        # "<txid>#<idx>" of the Resolved ChallengeDatum UTxO
        *,
        jury_size: int = 5,
        jury_fee_rate: int = 1000,               # basis points (10% = 1000)
    ) -> dict                                    # MUST include:
                                                 #   tx_hash: str
                                                 #   juror_utxo_ref_next: str
                                                 #   fee_per_juror: int

════════════════════════════════════════════════════════════════════════
VALIDATOR GROUND TRUTH — researched by reading the source directly
════════════════════════════════════════════════════════════════════════

Researched files (all paths absolute):
    /home/jelisaveta/github/vector-agent-modules/Module-1/contracts/
        validators/jury_pool.ak                  lines 86-156, 737-856
        lib/adversarial_auditing/types.ak        JurorDatum + JuryAction
        lib/adversarial_auditing/params.ak       juror_fee_share
    /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py::step8_distribute_rewards
                                                 lines 1562-1636

JuryAction Constr index → CBOR tag (verified in types.ak):

    RegisterJuror        Constr0 -> CBORTag(121, [])
    SelectJury           Constr1 -> CBORTag(122, [...])
    CommitVote           Constr2 -> CBORTag(123, [...])
    RevealVote           Constr3 -> CBORTag(124, [...])
    DistributeRewards    Constr4 -> CBORTag(125, [challenge_ref])   <-- HERE
    WithdrawJuror        Constr5 -> CBORTag(126, [])
    ReceiveJuryFee       Constr6 -> CBORTag(127, [])
    SlashNonReveal       Constr7 -> CBORTag(128, [...])
    ResetStaleActiveCase Constr8 -> CBORTag(129, [])

ChallengeState::Resolved = Constr3 -> CBORTag(124, [verdict])   <-- REF INPUT must be this

Verdict enum (inside the Resolved wrapper):
    ClaimerWins   Constr0 -> CBORTag(121, [])
    AuditorWins   Constr1 -> CBORTag(122, [])
    Inconclusive  Constr2 -> CBORTag(123, [])

JurorDatum field order (9 fields, per types.ak):
    [0] juror_did          PolicyId (28 B)
    [1] juror_credential   Credential
    [2] bond_amount        Int
    [3] cases_resolved     Int       <-- +1 on continuing output (L830)
    [4] majority_votes     Int       <-- preserved (L835)
    [5] registered_at      Int
    [6] active_case        Option<ByteArray>   Some(tn) -> None
    [7] vote_commitment    Option<ByteArray>   MUST be None on output (L837)
    [8] revealed_verdict   Option<Verdict>     MUST be None on output (L838)

Findings vs. the task brief (brief-first — each confirmation / correction):

    * CORRECTION — "Required signers includes oracle_vkh" — WRONG for
      DistributeRewards. `validate_distribute_rewards` (jury_pool.ak
      L737-856) performs NO signature check. The function is EXPLICITLY
      PERMISSIONLESS per the doc comment on L735:
        "Permissionless — anyone can trigger for a juror once the
         challenge is resolved."
      The oracle-sig requirement the brief cited ONLY applies to
      `validate_receive_jury_fee` (L177-184), which is a SEPARATE
      JuryAction (ReceiveJuryFee = Constr6 -> CBORTag 127) for draining
      admin fee-pool UTxOs. DistributeRewards does NOT invoke that
      codepath. Tests assert NO oracle sig is in required_signers.

    * CORRECTION — "consumes the jury_fee UTxO at jury_pool_addr
      (created by ResolveJury), pays the juror their share" — WRONG.
      v13 step8 (L1562-1636) does NOT touch any jury-fee UTxO. Instead,
      `fee_per_juror` is added to the continuing juror output from the
      WALLET's funds (L1606:
         out_value = Value(current_lovelace + fee_per_juror, out_ma)).
      The validator checks `ap3x_out == juror.bond_amount + fee_per_juror`
      (L841) — it does NOT check for any consumed fee-pool UTxO. In the
      current Phase-1.0 design, the ResolveJury-created fee-pool UTxOs
      at jury_pool_addr are drained SEPARATELY via ReceiveJuryFee (the
      admin oracle-signed operation); DistributeRewards' only on-chain
      effect on the juror UTxO is (a) +fee_per_juror in coin and
      (b) datum transition.
      Tests assert NO fee-pool UTxO is consumed as a script input.

    * Confirmed — DistributeRewards Constr4 = CBORTag(125, [chal_ref]).
      v13 L1595-1596:
         challenge_ref_cbor = CBORTag(121, [bytes.fromhex(res_txid), int(res_idx)])
         dist_redeemer_cbor = RawCBOR(cbor2.dumps(CBORTag(125, [challenge_ref_cbor])))
      Note the inner challenge_ref is CBORTag(121, [txid, idx]) — NOT a
      plain list. Tests decode the redeemer and assert that outer tag
      == 125 and inner is a Constr0 (tag 121) with two fields.

    * Confirmed — Juror UTxO is SPENT (not a reference input). v13
      L1613: `b.add_script_input(juror_utxo, jury_pool_ref_utxo,
      redeemer=dist_redeemer)`. Validator dispatches through the spend
      handler which calls validate_distribute_rewards.

    * Confirmed — Challenge UTxO is a REFERENCE INPUT (not spent).
      v13 L1615: `b.reference_inputs.add(resolved_utxo)`. Validator
      L753-763 searches tx.reference_inputs for the matching token.

    * Confirmed — EXACTLY ONE juror script input per TX. The dispatch
      site (jury_pool.ak L134-136) enforces
      `count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1`.
      Test verifies only one jury_pool-script spend appears.

    * Confirmed — Challenge MUST be in Resolved state (tag 124). The
      validator's `challenge_resolved` predicate (L767-781) fails if
      ch.state is anything other than Resolved. Client guard refuses
      early if the passed-in challenge UTxO has a different state tag.

    * Confirmed — fee_per_juror = `ch.stake_amount * rate / 10000 //
      jury_size`. Validator L791-811 computes via juror_fee_share; v13
      L1577 confirms: `STAKE_AMOUNT * 1000 // 10000 // 5`. For the
      default fixtures (stake=50_000_000, rate=1000, size=5) this is:
          50_000_000 * 1000 // 10000 // 5 = 1_000_000
      Tests verify the builder-computed fee_per_juror matches this
      formula exactly.

    * Confirmed — Juror datum transition (L822-842, v13 L1588-1593):
        [0] juror_did       preserved (must match output DID, L824)
        [1] juror_credential preserved (L828)
        [2] bond_amount      preserved (L834)
        [3] cases_resolved   +1 (L830)
        [4] majority_votes   preserved (L835)
        [5] registered_at    preserved (L829)
        [6] active_case      Some(tn) -> None (L831)
        [7] vote_commitment  MUST be None on output (L837)
        [8] revealed_verdict MUST be None on output (L838) — CLEARED
                             even though v13's reveal step set it to
                             Some(Verdict). v13 L1592 confirms the
                             clear: `fields[8] = CBORTag(122, [])`.

    * Confirmed — Continuing juror output value:
      `coin == bond_amount + fee_per_juror` (L841), multi_asset
      preserves the juror NFT qty=1 (v13 L1601-1605: out_ma reuses the
      juror's existing policy entry).

    * Confirmed — NO mint / burn on DistributeRewards. v13 step8
      does not construct a mint MultiAsset; builder.mint is empty /
      unset. Tests assert the mint field is empty.

    * Phase-1.0 fee-formula quirk — validator uses ch.stake_amount
      (AUDITOR stake) unconditionally, independent of the verdict.
      The doc comment L783-790 flags this as a Phase-1.1 TODO (claim
      stake not stored in ChallengeDatum). For our RED tests we mirror
      the actual on-chain behaviour — fee_per_juror is the SAME whether
      the verdict is ClaimerWins, AuditorWins, or Inconclusive, because
      it is derived from the constant ch.stake_amount.

Builder signature decisions (where spec ambiguity existed):

    * Return shape — we adopt the task-brief proposal BUT omit the
      `remaining_fee_utxo_ref` field because no fee UTxO is consumed
      (see the correction above). Specifically:
         - tx_hash: str
         - juror_utxo_ref_next: str  (f"{tx_hash}#0" — the continuing
           juror UTxO, always at index 0 per v13 L1633)
         - fee_per_juror: int        (useful for reconciliation assertions
           in downstream simulation tests)

    * Client-side guards (raise BEFORE submit):
         - Resolved challenge datum in a non-Resolved state → ValueError
         - Juror.active_case == None (unassigned) → ValueError
         - Juror.active_case != challenge's token name → ValueError
         - Challenge UTxO missing a qty=1 token under challenge policy
           → ValueError
         - Missing revealed_verdict (field[8] == None)? NOT a guard —
           the on-chain validator clears field[8] on the output, so the
           reveal state is orthogonal. We do NOT add a guard for this.

════════════════════════════════════════════════════════════════════════
Source refs
════════════════════════════════════════════════════════════════════════

    validators/jury_pool.ak::validate_distribute_rewards  lines 737-856
    validators/jury_pool.ak::validate_receive_jury_fee    lines 177-185
                                                          (DIFFERENT action)
    lib/adversarial_auditing/types.ak                     JurorDatum, JuryAction
    lib/adversarial_auditing/params.ak                    juror_fee_share

    Reference impl in v13:
        /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py::step8_distribute_rewards   lines 1562-1636

All tests in this file MUST fail against the current tree because
`build_distribute_rewards` does NOT yet exist in `tx_builder.py`. The
RED signal is `ImportError` on the function; Charlotte implements the
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
# Helpers (local to this test file; mirror test_resolve_jury.py style)
# ═════════════════════════════════════════════════════════════════════════


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


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
    redeemer attached. Same shape as test_resolve_jury.py's helper.
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


def _rewire_distribute_resolve_utxo(juror_utxo, challenge_utxo):
    """Re-install tx_builder.resolve_utxo dispatch for a test override.

    Tests that swap in a non-default juror or challenge fixture call
    this to keep the canonical patched_network_for_distribute_rewards
    wiring while routing resolve_utxo to the variant fixture.
    """
    import simulation.tx_builder as tx_mod

    juror_hex = bytes(juror_utxo.input.transaction_id).hex()
    chal_hex = bytes(challenge_utxo.input.transaction_id).hex()

    def _dispatch(
        txid_hex, idx,
        _ju=juror_utxo, _ch=challenge_utxo,
        _ju_hex=juror_hex, _ch_hex=chal_hex,
    ):
        if txid_hex == _ju_hex:
            return _ju
        if txid_hex == _ch_hex:
            return _ch
        raise AssertionError(
            f"override resolve_utxo: unexpected txid {txid_hex}#{idx}"
        )

    tx_mod.resolve_utxo = _dispatch


def _run_build_distribute_rewards(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
    *,
    juror_utxo_override=None,
    challenge_utxo_override=None,
    juror_ref_override=None,
    challenge_ref_override=None,
):
    """Invoke build_distribute_rewards with canonical fixture inputs.

    Any of the overrides re-installs `resolve_utxo` dispatching. The
    `*_ref_override` args let tests pass a bad ref string to exercise
    early client guards (e.g. wrong-format refs).
    """
    from simulation.tx_builder import build_distribute_rewards

    skey, vkey, wallet_addr = sample_juror_wallet
    juror_utxo = juror_utxo_override or sample_juror_utxo_revealed_for_distribute
    challenge_utxo = challenge_utxo_override or sample_resolved_challenge_utxo

    if juror_utxo_override is not None or challenge_utxo_override is not None:
        _rewire_distribute_resolve_utxo(juror_utxo, challenge_utxo)

    juror_ref = juror_ref_override or _utxo_ref_str(juror_utxo)
    chal_ref = challenge_ref_override or _utxo_ref_str(challenge_utxo)

    result = build_distribute_rewards(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        juror_ref,
        chal_ref,
        jury_size=default_jury_size,
        jury_fee_rate=default_jury_fee_rate,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_distribute_rewards did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# A shared deployment fixture — DistributeRewards ONLY spends a jury_pool
# input (challenge is a reference input). Reuse the existing deployment
# fixture that has only jury_pool_ref populated.
# ═════════════════════════════════════════════════════════════════════════
# (uses sample_deployment_with_jury_pool_ref from conftest.py; no
# additional fixtures needed at the deployment level.)


# ═════════════════════════════════════════════════════════════════════════
# Signature / importability
# ═════════════════════════════════════════════════════════════════════════


def test_build_distribute_rewards_is_importable():
    """Charlotte MUST expose `build_distribute_rewards` at module scope
    in simulation/tx_builder.py. All other tests in this file will fail
    with ImportError until this is satisfied — but we keep a dedicated
    test so the RED signal is crystal clear.
    """
    from simulation.tx_builder import build_distribute_rewards  # noqa: F401
    assert callable(build_distribute_rewards), (
        "simulation.tx_builder.build_distribute_rewards must be callable"
    )


def test_build_distribute_rewards_returns_dict_with_required_keys(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """The return dict MUST include tx_hash (str), juror_utxo_ref_next
    (str), fee_per_juror (int). Downstream sim iterates per-juror and
    chains the next juror_utxo_ref_next into the next TX — it must be
    present without re-querying the chain.
    """
    result, _ = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    required = {
        "tx_hash": str,
        "juror_utxo_ref_next": str,
        "fee_per_juror": int,
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
    # juror_utxo_ref_next format check
    assert "#" in result["juror_utxo_ref_next"], (
        f"juror_utxo_ref_next must be '<txid>#<idx>'; "
        f"got {result['juror_utxo_ref_next']!r}"
    )


def test_juror_utxo_ref_next_points_at_output_zero(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """v13 L1633 uses `f"{tx_hash}#0"` — the continuing juror output is
    always at output index 0. Downstream code assumes this. Assert the
    returned ref ends with `#0`.
    """
    result, _ = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    assert result["juror_utxo_ref_next"].endswith("#0"), (
        f"juror_utxo_ref_next must reference output index 0 "
        f"(v13 step8 L1633). Got {result['juror_utxo_ref_next']!r}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer correctness — DistributeRewards Constr4 (CBORTag 125)
# ═════════════════════════════════════════════════════════════════════════


def test_juror_input_carries_DistributeRewards_redeemer_tag_125(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """The juror script input's redeemer MUST decode to a CBORTag with
    `tag == 125` and a single-element value list (the challenge_ref).
    This is JuryAction::DistributeRewards per types.ak ordering and
    matches v13 L1596.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )

    spend_reds = _collect_all_spend_redeemers(builder)
    assert spend_reds, "No spend redeemers attached to the builder."

    juror_key = (
        bytes(sample_juror_utxo_revealed_for_distribute.input.transaction_id),
        sample_juror_utxo_revealed_for_distribute.input.index,
    )
    assert juror_key in spend_reds, (
        f"Juror UTxO {juror_key[0].hex()}#{juror_key[1]} must carry a "
        f"redeemer. Got keys: "
        + ", ".join(f"{k[0].hex()[:8]}#{k[1]}" for k in spend_reds)
    )

    red_cbor = _redeemer_cbor_bytes(spend_reds[juror_key])
    decoded = cbor2.loads(red_cbor)
    assert hasattr(decoded, "tag"), (
        f"Redeemer must decode to a CBORTag; got {decoded!r}."
    )
    assert decoded.tag == 125, (
        f"Juror redeemer tag must be 125 (Constr4 = DistributeRewards); "
        f"got tag={decoded.tag}."
    )
    assert isinstance(decoded.value, list) and len(decoded.value) == 1, (
        f"Redeemer payload must be [challenge_ref] (single element); "
        f"got {decoded.value!r}."
    )


def test_redeemer_inner_challenge_ref_matches_resolved_utxo(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """The inner element of the DistributeRewards redeemer payload is
    a CBORTag(121, [txid_bytes, idx]) — the OutputReference of the
    Resolved challenge. Verified against v13 L1595:
        CBORTag(121, [bytes.fromhex(res_txid), int(res_idx)]).
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )

    spend_reds = _collect_all_spend_redeemers(builder)
    juror_key = (
        bytes(sample_juror_utxo_revealed_for_distribute.input.transaction_id),
        sample_juror_utxo_revealed_for_distribute.input.index,
    )
    red_cbor = _redeemer_cbor_bytes(spend_reds[juror_key])
    decoded = cbor2.loads(red_cbor)
    inner = decoded.value[0]

    assert hasattr(inner, "tag"), (
        f"Inner challenge_ref must be a CBORTag; got {inner!r}."
    )
    assert inner.tag == 121, (
        f"Inner challenge_ref tag must be 121 (Constr0 = OutputReference); "
        f"got tag={inner.tag}."
    )
    assert isinstance(inner.value, list) and len(inner.value) == 2, (
        f"OutputReference must be [txid_bytes, idx]; got {inner.value!r}."
    )

    expected_txid = bytes(
        sample_resolved_challenge_utxo.input.transaction_id,
    )
    expected_idx = sample_resolved_challenge_utxo.input.index
    assert bytes(inner.value[0]) == expected_txid, (
        f"Redeemer challenge_ref txid mismatch: "
        f"expected {expected_txid.hex()}, got {bytes(inner.value[0]).hex()}."
    )
    assert int(inner.value[1]) == expected_idx, (
        f"Redeemer challenge_ref idx mismatch: expected {expected_idx}, "
        f"got {inner.value[1]}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Input / reference topology
# ═════════════════════════════════════════════════════════════════════════


def test_exactly_one_jury_pool_script_input(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """Validator dispatch (jury_pool.ak L134-136) enforces
    `count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1` for
    DistributeRewards. The builder must emit EXACTLY ONE script-spend
    against the jury_pool hash.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    spend_reds = _collect_all_spend_redeemers(builder)
    # The only script spend we expect is the juror UTxO.
    assert len(spend_reds) == 1, (
        f"expected exactly 1 script-spend redeemer (the juror input); "
        f"got {len(spend_reds)}. Keys: "
        + ", ".join(f"{k[0].hex()[:8]}#{k[1]}" for k in spend_reds)
    )


def test_resolved_challenge_utxo_is_reference_input_not_spent(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """The Resolved challenge UTxO is consulted via tx.reference_inputs
    by `list.find(...)` at jury_pool.ak L754. It must NOT appear in
    tx.inputs (which would re-spend the challenge — destroying the
    ability of OTHER jurors to also find + DistributeRewards).
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )

    chal_txid_bytes = bytes(sample_resolved_challenge_utxo.input.transaction_id)
    chal_idx = sample_resolved_challenge_utxo.input.index

    # Must appear in reference_inputs.
    ref_inputs = list(builder.reference_inputs)
    ref_keys = {
        (bytes(r.input.transaction_id), r.input.index) for r in ref_inputs
    }
    assert (chal_txid_bytes, chal_idx) in ref_keys, (
        f"Resolved challenge UTxO {chal_txid_bytes.hex()}#{chal_idx} must "
        f"appear in builder.reference_inputs. Got refs: "
        + ", ".join(f"{r[0].hex()[:8]}#{r[1]}" for r in ref_keys)
    )

    # Must NOT appear in tx.inputs (script-spent).
    spend_reds = _collect_all_spend_redeemers(builder)
    for txid_b, idx in spend_reds.keys():
        assert not (txid_b == chal_txid_bytes and idx == chal_idx), (
            "Resolved challenge UTxO must NOT be spent (it is a "
            "reference input only). Spending it would burn the Resolved "
            "state needed by the other 4 jurors' DistributeRewards TXs."
        )


def test_cross_refs_and_params_utxo_in_reference_inputs(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
    sample_cross_refs_utxo, sample_params_utxo,
):
    """Validator uses `get_cross_refs(config, tx.reference_inputs)` at
    jury_pool.ak L100 — cross_refs and params UTxOs must be referenced.
    Mirrors v13 L1616: `b.reference_inputs.add(cross_refs_utxo)`.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    ref_keys = {
        (bytes(r.input.transaction_id), r.input.index)
        for r in builder.reference_inputs
    }
    cross_key = (
        bytes(sample_cross_refs_utxo.input.transaction_id),
        sample_cross_refs_utxo.input.index,
    )
    params_key = (
        bytes(sample_params_utxo.input.transaction_id),
        sample_params_utxo.input.index,
    )
    assert cross_key in ref_keys, (
        "cross_refs_utxo must appear in reference_inputs "
        "(validator reads refs via get_cross_refs at jury_pool.ak L100)."
    )
    assert params_key in ref_keys, (
        "params_utxo must appear in reference_inputs so the validator "
        "can consult jury_fee_rate / jury_size from ProtocolParams."
    )


# ═════════════════════════════════════════════════════════════════════════
# No mint / burn (DistributeRewards does not create / destroy tokens)
# ═════════════════════════════════════════════════════════════════════════


def test_no_mint_or_burn_on_distribute_rewards(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """v13 step8 does not construct any mint MultiAsset. The juror NFT
    is preserved (qty=1 in, qty=1 out) and the challenge NFT is
    untouched. Builder.mint must be empty / None.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    mint = getattr(builder, "mint", None) or getattr(builder, "_mint", None)
    # Accept None / empty MultiAsset / empty dict as "no mint".
    if mint is None:
        return
    # MultiAsset supports bool(): False if empty.
    assert not mint, (
        f"DistributeRewards must NOT mint or burn any tokens; "
        f"builder.mint is non-empty: {mint!r}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Oracle signature — MUST NOT be required (brief premise corrected)
# ═════════════════════════════════════════════════════════════════════════


def test_required_signers_does_not_include_oracle(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """DistributeRewards is PERMISSIONLESS per jury_pool.ak L735.
    required_signers should contain ONLY the fee-payer's vkh (the
    wallet signer). Adding an oracle_vkh here would be a bug —
    unnecessary signatures are a chain-of-custody red flag and force
    downstream sim code to collect oracle keys it does not need.

    The brief's "required signers includes oracle_vkh" premise
    CONFLATED DistributeRewards with ReceiveJuryFee (a different
    JuryAction that IS oracle-gated). See file docstring for the
    correction trace.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    required = builder.required_signers or []
    required_bytes = {bytes(s) for s in required}

    _, vkey, _ = sample_juror_wallet
    fee_payer_vkh = bytes(vkey.hash())
    assert fee_payer_vkh in required_bytes, (
        "fee-payer's vkh MUST appear in required_signers; got "
        f"{[h.hex()[:8] for h in required_bytes]}."
    )
    # Must be EXACTLY the fee-payer — no oracle, no extra juror keys.
    assert required_bytes == {fee_payer_vkh}, (
        f"required_signers must contain ONLY the fee-payer vkh; "
        f"got {[h.hex()[:8] for h in required_bytes]}. "
        f"DistributeRewards is permissionless — no oracle sig needed."
    )


# ═════════════════════════════════════════════════════════════════════════
# No jury_fee pool UTxO consumed as a script input (brief premise corrected)
# ═════════════════════════════════════════════════════════════════════════


def test_no_fee_pool_utxo_is_script_spent(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """The brief claimed DistributeRewards "consumes the jury_fee UTxO
    at jury_pool_addr" — this is WRONG. v13 step8 touches no fee-pool
    UTxO; the fee_per_juror comes from the wallet's base coin. If the
    builder tried to consume a fee-pool UTxO, it would hit
    `validate_receive_jury_fee` (oracle-gated, different action) or
    simply fail because no such UTxO is wired in.

    Assert that the ONLY script input is the juror UTxO — no second
    jury_pool spend.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    spend_reds = _collect_all_spend_redeemers(builder)
    juror_key = (
        bytes(sample_juror_utxo_revealed_for_distribute.input.transaction_id),
        sample_juror_utxo_revealed_for_distribute.input.index,
    )
    assert set(spend_reds.keys()) == {juror_key}, (
        f"Only the juror UTxO should be script-spent; extraneous "
        f"script inputs found: "
        + ", ".join(
            f"{k[0].hex()[:8]}#{k[1]}"
            for k in spend_reds
            if k != juror_key
        )
    )


# ═════════════════════════════════════════════════════════════════════════
# Continuing juror output — datum transition per validator L822-842
# ═════════════════════════════════════════════════════════════════════════


def _find_juror_continuing_output(builder, juror_did_bytes):
    """Return the single continuing output at jury_pool_addr whose
    datum's field[0] (juror_did) matches the given DID bytes.
    """
    outs = _jury_pool_outputs(builder)
    for out in outs:
        decoded = _decode_datum(out)
        fields = list(decoded.value)
        if bytes(fields[0]) == juror_did_bytes:
            return out, fields
    raise AssertionError(
        f"No jury_pool output matching juror_did={juror_did_bytes.hex()[:16]}... "
        f"found among {len(outs)} outputs at jury_pool_addr."
    )


def test_continuing_juror_output_exists_exactly_once(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """Exactly one continuing output at jury_pool_addr (the updated
    juror UTxO). No stray outputs to that script address.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    outs = _jury_pool_outputs(builder)
    assert len(outs) == 1, (
        f"Expected exactly 1 output at jury_pool_addr (the continuing "
        f"juror UTxO); got {len(outs)}."
    )


def test_continuing_output_is_at_index_zero(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """v13 L1617-1618 emits the juror continuing output as the FIRST
    (and only) output — so `juror_utxo_ref_next = f"{tx_hash}#0"` is
    consistent with on-chain indexing. Test the builder's first output
    is at jury_pool_addr.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    from simulation.tests.conftest import V13_DEPLOYMENT
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    assert builder.outputs, "builder has no outputs"
    assert builder.outputs[0].address == jury_addr, (
        f"Output 0 must be at jury_pool_addr (v13 step8 L1617 adds the "
        f"juror continuing output first). Got address "
        f"{builder.outputs[0].address!r}."
    )


def test_datum_field_0_juror_did_preserved(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[0] juror_did MUST equal the input datum's juror_did
    (validator L824 matches output by DID). A mismatch would mean the
    builder produced an output for a different juror.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    assert bytes(fields[0]) == sample_juror_did, (
        f"field[0] juror_did mismatch: expected {sample_juror_did.hex()[:16]}..., "
        f"got {bytes(fields[0]).hex()[:16]}..."
    )


def test_datum_field_1_juror_credential_preserved(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[1] juror_credential must be byte-identical to the input
    (validator L828). The credential is CBORTag(121, [vkh]).
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    cred = fields[1]
    assert getattr(cred, "tag", None) == 121, (
        f"field[1] juror_credential outer tag must be 121 (Credential "
        f"= VerificationKey); got {cred!r}."
    )
    _, juror_vkey, _ = sample_juror_wallet
    expected_vkh = bytes(juror_vkey.hash())
    assert bytes(cred.value[0]) == expected_vkh, (
        f"field[1] vkh mismatch: expected {expected_vkh.hex()[:16]}..., "
        f"got {bytes(cred.value[0]).hex()[:16]}..."
    )


def test_datum_field_2_bond_amount_preserved(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did, default_bond_amount,
    default_jury_size, default_jury_fee_rate,
):
    """field[2] bond_amount must equal the input's bond (validator L834
    'updated.bond_amount == juror.bond_amount'). A juror could not
    inflate their bond via DistributeRewards.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    assert int(fields[2]) == default_bond_amount, (
        f"field[2] bond_amount mismatch: expected {default_bond_amount}, "
        f"got {int(fields[2])}."
    )


def test_datum_field_3_cases_resolved_incremented(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[3] cases_resolved MUST be `input.cases_resolved + 1`
    (validator L830). Input fixture has field[3]=0 so output should be 1.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    assert int(fields[3]) == 1, (
        f"field[3] cases_resolved must be input+1 = 1 (input fixture has "
        f"cases_resolved=0); got {int(fields[3])}."
    )


def test_datum_field_4_majority_votes_preserved(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[4] majority_votes MUST equal the input's (validator L835).
    DistributeRewards does NOT increment this — only a separate
    bookkeeping step would. Input fixture has 0 so output is 0.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    assert int(fields[4]) == 0, (
        f"field[4] majority_votes must equal input (0); "
        f"got {int(fields[4])}."
    )


def test_datum_field_5_registered_at_preserved(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[5] registered_at must be byte-identical to input
    (validator L829 via implicit field preservation — the authoritative
    check is `updated.registered_at == juror.registered_at`).
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    # Read input datum's registered_at for comparison.
    inp_raw = sample_juror_utxo_revealed_for_distribute.output.datum
    inp_cbor = inp_raw.cbor if hasattr(inp_raw, "cbor") else bytes(inp_raw)
    inp_decoded = cbor2.loads(inp_cbor)
    inp_fields = list(inp_decoded.value)
    expected_registered_at = int(inp_fields[5])

    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    assert int(fields[5]) == expected_registered_at, (
        f"field[5] registered_at mismatch: expected "
        f"{expected_registered_at}, got {int(fields[5])}."
    )


def test_datum_field_6_active_case_cleared_to_none(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[6] active_case Some(tn) -> None (validator L831).
    On-chain None is encoded as CBORTag(122, []) (Option::None
    constructor). v13 L1590 confirms: `fields[6] = CBORTag(122, [])`.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    active = fields[6]
    assert getattr(active, "tag", None) == 122, (
        f"field[6] active_case must be None (CBORTag 122) after "
        f"DistributeRewards; got {active!r}."
    )
    assert list(active.value) == [], (
        f"Option::None payload must be empty list; got {active.value!r}."
    )


def test_datum_field_7_vote_commitment_is_none(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[7] vote_commitment MUST be None on the continuing output
    (validator L837). Input is already None (post-reveal), so this
    predominantly tests that the builder doesn't accidentally re-add
    a commitment when rebuilding the datum.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    commitment = fields[7]
    assert getattr(commitment, "tag", None) == 122, (
        f"field[7] vote_commitment must be None (CBORTag 122); "
        f"got {commitment!r}."
    )


def test_datum_field_8_revealed_verdict_cleared_to_none(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_jury_size, default_jury_fee_rate,
):
    """field[8] revealed_verdict MUST be None on the continuing output
    (validator L838). Input fixture has Some(ClaimerWins), so the
    builder MUST actively clear this. v13 L1592 confirms:
        `fields[8] = CBORTag(122, [])`.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    _, fields = _find_juror_continuing_output(builder, sample_juror_did)
    verdict_opt = fields[8]
    assert getattr(verdict_opt, "tag", None) == 122, (
        f"field[8] revealed_verdict must be CLEARED to None "
        f"(CBORTag 122) on the continuing output; got {verdict_opt!r}. "
        f"Do not copy the input's Some(Verdict) to the output — "
        f"validator L838 rejects it."
    )


# ═════════════════════════════════════════════════════════════════════════
# Continuing juror output — VALUE (bond + fee_per_juror, NFT preserved)
# ═════════════════════════════════════════════════════════════════════════


def test_continuing_juror_output_coin_equals_bond_plus_fee(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did,
    default_bond_amount, default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Validator L841 requires:
        lovelace_of(out.value) == juror.bond_amount + fee_per_juror
    where:
        fee_per_juror = (ch.stake_amount * rate / 10000) // jury_size

    For the default fixtures:
        stake_amount = 50_000_000  (default_stake_amount)
        rate         = 1000         (default_jury_fee_rate)
        jury_size    = 5
        => fee_per_juror = 50_000_000 * 1000 // 10000 // 5 = 1_000_000
        => expected coin = bond + 1_000_000 = 26_000_000
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    out, _ = _find_juror_continuing_output(builder, sample_juror_did)
    coin = (
        out.amount.coin
        if hasattr(out.amount, "coin")
        else int(out.amount)
    )
    expected_fee = default_stake_amount * default_jury_fee_rate // 10000 // default_jury_size
    expected_coin = default_bond_amount + expected_fee
    assert coin == expected_coin, (
        f"Continuing juror output coin must be bond ({default_bond_amount}) "
        f"+ fee_per_juror ({expected_fee}) = {expected_coin}; got {coin}."
    )


def test_continuing_juror_output_preserves_juror_nft(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    sample_juror_did, sample_juror_token_bytes,
    default_jury_size, default_jury_fee_rate,
):
    """The juror NFT (qty=1 under jury_pool policy, AssetName =
    b'jur_' || blake2b_256(did)[:28]) MUST be carried forward on the
    continuing output. v13 L1601-1605 re-adds the policy entry to
    out_ma exactly. Burning or dropping the NFT would make the next
    DistributeRewards cycle (across cases) impossible to authenticate.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    out, _ = _find_juror_continuing_output(builder, sample_juror_did)
    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    ma = out.amount.multi_asset
    assert ma is not None and jury_policy in ma, (
        "Continuing juror output must carry a juror NFT under "
        f"jury_pool policy {V13_DEPLOYMENT['hashes']['jury_pool']}; "
        f"got multi_asset={ma!r}."
    )
    token_an = AssetName(sample_juror_token_bytes)
    qty = ma[jury_policy].get(token_an, 0)
    assert qty == 1, (
        f"Juror NFT {sample_juror_token_bytes.hex()[:16]}... must have "
        f"qty=1 on the continuing output; got qty={qty}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Result: fee_per_juror matches the formula
# ═════════════════════════════════════════════════════════════════════════


def test_result_fee_per_juror_matches_formula(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_stake_amount,
    default_jury_size, default_jury_fee_rate,
):
    """Result dict's fee_per_juror MUST equal
    `ch.stake_amount * rate / 10000 // jury_size` (v13 L1577 formula).

    For defaults: 50_000_000 * 1000 // 10000 // 5 = 1_000_000.
    """
    result, _ = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    expected = default_stake_amount * default_jury_fee_rate // 10000 // default_jury_size
    assert result["fee_per_juror"] == expected, (
        f"result['fee_per_juror'] mismatch: expected {expected} "
        f"(stake={default_stake_amount}, rate={default_jury_fee_rate}, "
        f"jury_size={default_jury_size}); got {result['fee_per_juror']}."
    )


def test_fee_per_juror_invariant_across_verdicts(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo_auditor_wins,
    default_jury_size, default_jury_fee_rate,
    sample_resolved_challenge_utxo,
):
    """Per the Phase-1.0 fee formula (validator L791-811), fee_per_juror
    depends ONLY on ch.stake_amount, NOT the verdict. Resolving the
    same challenge stake with AuditorWins should yield the SAME
    fee_per_juror as ClaimerWins. This locks in the current behaviour
    so a Phase-1.1 rewrite that changes the formula will surface here.
    """
    # Run 1: canonical (ClaimerWins) fixture
    result_cw, _ = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    # Clear captured_builder list so run 2 captures its own builder.
    captured_builder.clear()

    # Run 2: AuditorWins verdict variant (same stake_amount)
    result_aw, _ = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo_auditor_wins,
        default_jury_size, default_jury_fee_rate,
        challenge_utxo_override=sample_resolved_challenge_utxo_auditor_wins,
    )

    assert result_cw["fee_per_juror"] == result_aw["fee_per_juror"], (
        f"fee_per_juror must be verdict-invariant in Phase-1.0 "
        f"(validator uses ch.stake_amount unconditionally). "
        f"ClaimerWins gave {result_cw['fee_per_juror']}, "
        f"AuditorWins gave {result_aw['fee_per_juror']}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards (fail FAST with ValueError before builder runs)
# ═════════════════════════════════════════════════════════════════════════


def test_guard_challenge_not_resolved_raises_value_error(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_challenge_utxo_still_voting_for_distribute,
    default_jury_size, default_jury_fee_rate,
):
    """Passing a challenge UTxO that is still in Voting state (tag 123)
    instead of Resolved (tag 124) must raise ValueError. Validator
    L767-781 checks `challenge_resolved`; a non-Resolved challenge
    would fail on-chain. Client refuses early for a cleaner error.
    """
    with pytest.raises(ValueError, match=r"(?i)resolved|voting|state"):
        _run_build_distribute_rewards(
            patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_revealed_for_distribute,
            sample_challenge_utxo_still_voting_for_distribute,
            default_jury_size, default_jury_fee_rate,
            challenge_utxo_override=sample_challenge_utxo_still_voting_for_distribute,
        )


def test_guard_juror_unassigned_raises_value_error(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_unassigned_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """A juror UTxO with active_case=None cannot be paid (validator L747
    fails at `expect Some(active_token_name) = juror.active_case`).
    Client must raise ValueError before submit.
    """
    with pytest.raises(ValueError, match=r"(?i)active_case|assigned|unassigned"):
        _run_build_distribute_rewards(
            patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_unassigned_for_distribute,
            sample_resolved_challenge_utxo,
            default_jury_size, default_jury_fee_rate,
            juror_utxo_override=sample_juror_utxo_unassigned_for_distribute,
        )


def test_guard_juror_wrong_challenge_raises_value_error(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_wrong_challenge_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """A juror whose active_case token name does NOT match the
    challenge's token name cannot be resolved against this challenge
    (validator L760-763 filters by token-name match). Client raises
    ValueError before building.
    """
    with pytest.raises(
        ValueError, match=r"(?i)active_case|challenge.*token|mismatch"
    ):
        _run_build_distribute_rewards(
            patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_wrong_challenge_for_distribute,
            sample_resolved_challenge_utxo,
            default_jury_size, default_jury_fee_rate,
            juror_utxo_override=sample_juror_utxo_wrong_challenge_for_distribute,
        )


def test_guard_malformed_juror_ref_raises_value_error(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """A ref string missing `#<idx>` is a programming error — surface
    it with a clear ValueError instead of an obscure KeyError / split
    failure. (Python's str.split("#", 1) would still produce a 1-list
    that crashes at `int(...)`.)
    """
    with pytest.raises((ValueError, IndexError)):
        _run_build_distribute_rewards(
            patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_revealed_for_distribute,
            sample_resolved_challenge_utxo,
            default_jury_size, default_jury_fee_rate,
            juror_ref_override="not-a-valid-ref-no-hash",
        )


# ═════════════════════════════════════════════════════════════════════════
# Validity window & signer wiring (permissionless fee-payer only)
# ═════════════════════════════════════════════════════════════════════════


def test_validity_window_has_both_start_and_ttl(
    patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    default_jury_size, default_jury_fee_rate,
):
    """v13 L1619-1620 sets validity_start and ttl. There is no on-chain
    time gate on DistributeRewards (validator L737-856 performs no
    tx_ends_before / tx_started_after check), but a valid TX still
    needs a reasonable window set. Smoke-check that both are present
    and ordered.
    """
    _, builder = _run_build_distribute_rewards(
        patched_network_for_distribute_rewards, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_revealed_for_distribute,
        sample_resolved_challenge_utxo,
        default_jury_size, default_jury_fee_rate,
    )
    assert builder.validity_start is not None, "validity_start must be set"
    assert builder.ttl is not None, "ttl must be set"
    assert builder.validity_start < builder.ttl, (
        f"validity_start ({builder.validity_start}) must be < "
        f"ttl ({builder.ttl})."
    )
