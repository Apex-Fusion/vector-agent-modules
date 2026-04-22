"""
RED-phase tests for `simulation.tx_builder.build_cleanup_resolved`.

Function under test (NOT YET IMPLEMENTED):
    build_cleanup_resolved(
        context, deployment,
        skey, vkey, wallet_addr,
        resolved_challenge_utxo_ref: str,            # "<txid>#<idx>"
        *,
        resolution_deadline_ms: int = 259_200_000,   # 72h mainnet
        cleanup_buffer_ms: int = 600_000,            # 10 min mainnet
    ) -> dict                                        # MUST include:
                                                     #   tx_hash: str
                                                     #   recovered_coin: int

════════════════════════════════════════════════════════════════════════
VALIDATOR GROUND TRUTH — researched by reading the source directly
════════════════════════════════════════════════════════════════════════

Researched files (all paths absolute):
    /home/jelisaveta/github/vector-agent-modules/Module-1/contracts/
        validators/challenge.ak                  lines 837-883
        lib/adversarial_auditing/types.ak        ChallengeAction, ChallengeState
    /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py::step9_cleanup_resolved
                                                 lines 1643-1715

ChallengeAction Constr index -> CBOR tag (verified in types.ak L144-169):

    OpenChallenge        Constr0 -> CBORTag(121, [])
    SubmitEvidence       Constr1 -> CBORTag(122, [...])
    OracleResolve        Constr2 -> CBORTag(123, [...])
    ResolveJury          Constr3 -> CBORTag(124, [])
    TimeoutResolve       Constr4 -> CBORTag(125, [])
    CleanupResolved      Constr5 -> CBORTag(126, [])   <-- HERE
    TransitionToVoting   Constr6 -> CBORTag(127, [...])

CONFIRMED against v13 reference (L1680):
    cleanup_redeemer_cbor = RawCBOR(cbor2.dumps(cbor2.CBORTag(126, [])))
The CleanupResolved redeemer is a ZERO-argument constructor — the tag
value is the empty list. The SAME redeemer bytes are used for BOTH the
spend (of the Resolved challenge UTxO) and the mint (of the -1 qty
challenge token). v13 L1700-1701 demonstrates this duplication:
    spend_r = Redeemer(cleanup_redeemer_cbor, ExecutionUnits(...))
    mint_r  = Redeemer(cleanup_redeemer_cbor, ExecutionUnits(...))

ChallengeState::Resolved = Constr3 -> CBORTag(124, [verdict])

Findings vs. the task brief (brief-first — each confirmation / correction):

    * CONFIRMED — CleanupResolved is Constr5 = tag 126. Types.ak order:
      OpenChallenge(0), SubmitEvidence(1), OracleResolve(2),
      ResolveJury(3), TimeoutResolve(4), CleanupResolved(5),
      TransitionToVoting(6). Brief prediction was correct.

    * CONFIRMED — Permissionless in Phase 1.1. challenge.ak L857:
        "Phase 1.1: permissionless — no oracle needed."
      The earlier Phase-1.0 doc comment (L834) mentioned an oracle
      requirement, but the active code path does NOT invoke any signer
      check. Tests assert required_signers == {fee_payer_vkh} ONLY —
      no oracle vkh, no auditor vkh beyond the wallet's own.

    * CONFIRMED — Time gate uses ch.resolution_deadline (from datum,
      not a builder kwarg) + params.cleanup_buffer. challenge.ak L854-855:
        tx_started_after(tx, ch.challenged_at + ch.resolution_deadline
                             + params.cleanup_buffer)
      The builder's `resolution_deadline_ms` / `cleanup_buffer_ms` kwargs
      therefore exist only for CLIENT-SIDE pre-flight gating; the
      validator reads resolution_deadline from the datum (field[7]) and
      cleanup_buffer from ProtocolParams (reference input). This means
      the builder MUST do its own time-gate check or else it will
      submit TXs guaranteed to fail — wasting collateral. Tests verify
      both (a) the client-side guard AND (b) validity_start strictly
      past the cutoff when the gate has passed.

    * CONFIRMED — Challenge NFT burned (qty = -1). challenge.ak L860-866:
        challenge_burned =
          when find_input(tx.inputs, own_ref) is {
            Some(inp) ->
              when dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash)) is {
                [Pair(token_name, 1)] ->
                  exactly_one_burned(tx, refs.challenge_validator_hash, token_name)
                _ -> False
              }
            None -> False
          }
      v13 L1681-1683 mirrors this by constructing a burn MultiAsset with
      qty = -1 under the challenge policy. Tests verify builder.mint
      has the correct policy + token + qty=-1 and nothing else.

    * CONFIRMED — NO continuing output at challenge script address.
      challenge.ak L873-875:
        no_challenge_outputs =
          list.length(outputs_at_script(tx.outputs, refs.challenge_validator_hash)) == 0
      This is finding-002-F4 (prevents UTxO pollution). Tests verify
      that NONE of builder.outputs are at V13_DEPLOYMENT["addresses"]["challenge"].

    * CONFIRMED — Challenge UTxO is SPENT as a script input (not ref).
      v13 L1690:
        b.add_script_input(resolved_utxo, challenge_ref_utxo, redeemer=spend_r)
      The validator dispatches through the spend handler which calls
      validate_cleanup_resolved with own_ref = the spent input's ref.

    * CONFIRMED — Wallet recovers the auditor_stake coin that was
      preserved through ResolveJury (i.e. the challenge UTxO's `coin`
      field at cleanup time). v13 L1691: `for u in wallet_utxos: b.add_input(u)`
      together with `change_address=wallet_addr` (L1710) means any
      residual lovelace — including the spent Resolved UTxO's coin —
      flows to the wallet as change. The builder reports this as
      `recovered_coin`.

    * CONFIRMED — NO reference input to the challenge UTxO. v13 step9
      does NOT call `b.reference_inputs.add(resolved_utxo)`; it adds
      only cross_refs_utxo (L1692). The challenge UTxO is consumed,
      not referenced — adding it to reference_inputs would be a bug
      (and Cardano rejects same-UTxO-in-both-sets at the ledger level).

Client-side guards (raise BEFORE submit):
    - Challenge datum state != Resolved  -> ValueError
    - current_slot too early for time gate -> ValueError
      (the validator WILL fail-fast; the builder must not waste a TX)
    - Challenge UTxO missing a qty=1 token under challenge policy
      -> ValueError (nothing to burn; on-chain validator would reject)

════════════════════════════════════════════════════════════════════════
Source refs
════════════════════════════════════════════════════════════════════════

    validators/challenge.ak::validate_cleanup_resolved     L837-883
    lib/adversarial_auditing/types.ak                      L144-169

    Reference impl in v13:
        /home/jelisaveta/.openclaw/workspace-apex/testnet/
        deploy_and_run_v13.py::step9_cleanup_resolved      L1643-1715

All tests in this file MUST fail against the current tree because
`build_cleanup_resolved` does NOT yet exist in `tx_builder.py`. The
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
# Helpers (local to this test file; mirror test_distribute_rewards.py style)
# ═════════════════════════════════════════════════════════════════════════


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def _challenge_outputs(builder: TransactionBuilder):
    """Return all outputs at the challenge script address."""
    from simulation.tests.conftest import V13_DEPLOYMENT
    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    return [o for o in builder.outputs if o.address == challenge_addr]


def _collect_all_spend_redeemers(builder: TransactionBuilder):
    """Return {(txid_bytes, idx): Redeemer} for every script-spend
    redeemer attached. Same shape as test_distribute_rewards.py helper.
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


def _collect_all_mint_redeemers(builder: TransactionBuilder):
    """Return a list of mint redeemers attached. PyCardano exposes mint
    redeemers via various private attrs depending on version; we probe a
    few and return whichever is non-empty.
    """
    for attr in ("_minting_script_to_redeemers", "_mint_to_redeemer",
                 "_minting_redeemers"):
        mapping = getattr(builder, attr, None)
        if mapping is None:
            continue
        if hasattr(mapping, "values"):
            vals = list(mapping.values())
            if vals:
                return vals
        elif isinstance(mapping, list):
            if mapping:
                return [x[1] if isinstance(x, tuple) else x for x in mapping]
    return []


def _redeemer_cbor_bytes(redeemer) -> bytes:
    """Normalise a Redeemer's payload to CBOR bytes."""
    data = redeemer.data if hasattr(redeemer, "data") else redeemer
    if hasattr(data, "cbor"):
        return bytes(data.cbor)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return cbor2.dumps(data)


def _rewire_cleanup_resolve_utxo(challenge_utxo):
    """Re-install tx_builder.resolve_utxo to route to a variant fixture.

    Tests that swap in a non-default challenge fixture (too-early,
    no-NFT, Voting-state) call this to keep the canonical
    patched_network_for_cleanup_resolved wiring while routing
    resolve_utxo to the variant fixture.
    """
    import simulation.tx_builder as tx_mod

    chal_hex = bytes(challenge_utxo.input.transaction_id).hex()

    def _dispatch(txid_hex, idx, _ch=challenge_utxo, _ch_hex=chal_hex):
        if txid_hex == _ch_hex:
            return _ch
        raise AssertionError(
            f"override resolve_utxo: unexpected txid {txid_hex}#{idx}"
        )

    tx_mod.resolve_utxo = _dispatch


def _run_build_cleanup_resolved(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
    *,
    challenge_utxo_override=None,
    challenge_ref_override=None,
    resolution_deadline_ms_override=None,
    cleanup_buffer_ms_override=None,
):
    """Invoke build_cleanup_resolved with canonical fixture inputs.

    Any override re-installs the `resolve_utxo` dispatcher so the
    variant fixture is returned. The `*_ref_override` argument lets
    tests pass a bad ref string to exercise early client guards.
    """
    from simulation.tx_builder import build_cleanup_resolved

    skey, vkey, wallet_addr = sample_auditor_wallet
    challenge_utxo = (
        challenge_utxo_override or sample_resolved_challenge_utxo_for_cleanup
    )

    if challenge_utxo_override is not None:
        _rewire_cleanup_resolve_utxo(challenge_utxo)

    chal_ref = challenge_ref_override or _utxo_ref_str(challenge_utxo)
    res_deadline = (
        resolution_deadline_ms_override
        if resolution_deadline_ms_override is not None
        else default_resolution_deadline_ms
    )
    cleanup_buf = (
        cleanup_buffer_ms_override
        if cleanup_buffer_ms_override is not None
        else default_cleanup_buffer_ms
    )

    result = build_cleanup_resolved(
        mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        skey, vkey, wallet_addr,
        chal_ref,
        resolution_deadline_ms=res_deadline,
        cleanup_buffer_ms=cleanup_buf,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_cleanup_resolved did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# Signature / importability
# ═════════════════════════════════════════════════════════════════════════


def test_build_cleanup_resolved_is_importable():
    """Charlotte MUST expose `build_cleanup_resolved` at module scope
    in simulation/tx_builder.py. All other tests in this file will fail
    with ImportError until this is satisfied — but we keep a dedicated
    test so the RED signal is crystal clear.
    """
    from simulation.tx_builder import build_cleanup_resolved  # noqa: F401
    assert callable(build_cleanup_resolved), (
        "simulation.tx_builder.build_cleanup_resolved must be callable"
    )


def test_build_cleanup_resolved_returns_dict_with_required_keys(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """The return dict MUST include tx_hash (str) and recovered_coin (int).
    Downstream sim uses recovered_coin to reconcile wallet balance vs.
    the auditor's original stake post-lifecycle.
    """
    result, _ = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    required = {
        "tx_hash": str,
        "recovered_coin": int,
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


def test_recovered_coin_matches_resolved_challenge_utxo_coin(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_stake_amount,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """recovered_coin MUST equal the coin value sitting in the spent
    Resolved challenge UTxO (the auditor's preserved stake). This is
    what flows back to the wallet via change after cleanup.
    """
    result, _ = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    expected_coin = sample_resolved_challenge_utxo_for_cleanup.output.amount.coin
    assert result["recovered_coin"] == expected_coin == default_stake_amount, (
        f"recovered_coin must equal the Resolved challenge UTxO's coin "
        f"(the auditor's preserved stake). Expected {expected_coin}, "
        f"got {result['recovered_coin']}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer correctness — CleanupResolved Constr5 (CBORTag 126)
# ═════════════════════════════════════════════════════════════════════════


def test_spend_redeemer_tag_is_126(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """The challenge script input's redeemer MUST decode to a CBORTag
    with `tag == 126` (ChallengeAction::CleanupResolved = Constr5).
    v13 L1680 confirms: `CBORTag(126, [])`.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )

    spend_reds = _collect_all_spend_redeemers(builder)
    assert spend_reds, "No spend redeemers attached to the builder."

    chal_key = (
        bytes(sample_resolved_challenge_utxo_for_cleanup.input.transaction_id),
        sample_resolved_challenge_utxo_for_cleanup.input.index,
    )
    assert chal_key in spend_reds, (
        f"Resolved challenge UTxO {chal_key[0].hex()}#{chal_key[1]} must "
        f"carry a spend redeemer. Got keys: "
        + ", ".join(f"{k[0].hex()[:8]}#{k[1]}" for k in spend_reds)
    )

    red_cbor = _redeemer_cbor_bytes(spend_reds[chal_key])
    decoded = cbor2.loads(red_cbor)
    assert hasattr(decoded, "tag"), (
        f"Redeemer must decode to a CBORTag; got {decoded!r}."
    )
    assert decoded.tag == 126, (
        f"Challenge spend redeemer tag must be 126 "
        f"(Constr5 = CleanupResolved); got tag={decoded.tag}."
    )
    # Payload MUST be the empty list — CleanupResolved is a zero-arg
    # constructor. v13 L1680: `CBORTag(126, [])`.
    assert decoded.value == [], (
        f"CleanupResolved redeemer payload must be [] (zero-arg "
        f"constructor); got {decoded.value!r}."
    )


def test_mint_redeemer_tag_is_126(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """The mint redeemer for the challenge-token burn MUST ALSO decode
    to CBORTag(126, []). v13 L1700-1701 uses the SAME cleanup_redeemer_cbor
    for both spend and mint — the validator shares one action code path.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    mint_reds = _collect_all_mint_redeemers(builder)
    assert mint_reds, (
        "No mint redeemer attached — CleanupResolved burns the challenge "
        "NFT and MUST attach a mint redeemer. v13 L1694: "
        "`b.add_minting_script(challenge_ref_utxo, mint_r)`."
    )
    red_cbor = _redeemer_cbor_bytes(mint_reds[0])
    decoded = cbor2.loads(red_cbor)
    assert hasattr(decoded, "tag"), (
        f"Mint redeemer must decode to a CBORTag; got {decoded!r}."
    )
    assert decoded.tag == 126, (
        f"Mint redeemer tag must be 126 (CleanupResolved); "
        f"got tag={decoded.tag}."
    )
    assert decoded.value == [], (
        f"Mint redeemer payload must be [] (zero-arg constructor); "
        f"got {decoded.value!r}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Spend + Burn atomicity
# ═════════════════════════════════════════════════════════════════════════


def test_resolved_challenge_utxo_is_script_spent(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """The Resolved challenge UTxO MUST be a SCRIPT-SPEND input (not a
    reference input). challenge.ak L862-870 calls find_input(tx.inputs,
    own_ref) — the validator expects it in tx.inputs. v13 L1690 confirms:
    `b.add_script_input(resolved_utxo, ...)`.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )

    chal_txid_bytes = bytes(
        sample_resolved_challenge_utxo_for_cleanup.input.transaction_id,
    )
    chal_idx = sample_resolved_challenge_utxo_for_cleanup.input.index

    # MUST appear as a script-spend.
    spend_reds = _collect_all_spend_redeemers(builder)
    assert (chal_txid_bytes, chal_idx) in spend_reds, (
        f"Resolved challenge UTxO {chal_txid_bytes.hex()}#{chal_idx} must "
        f"be a script-spend input. Got spend keys: "
        + ", ".join(f"{k[0].hex()[:8]}#{k[1]}" for k in spend_reds)
    )

    # MUST NOT appear in reference_inputs.
    ref_inputs = list(builder.reference_inputs)
    ref_keys = {
        (bytes(r.input.transaction_id), r.input.index) for r in ref_inputs
    }
    assert (chal_txid_bytes, chal_idx) not in ref_keys, (
        "Resolved challenge UTxO MUST NOT appear in reference_inputs "
        "(it is consumed, not referenced). Cardano rejects same-UTxO-in-"
        "both-sets at the ledger level."
    )


def test_exactly_one_challenge_script_input(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """CleanupResolved expects exactly ONE script-spend input — the
    Resolved challenge UTxO. No other script inputs should be attached
    (no claim, no jury_pool, no other challenge). v13 step9 consumes
    only the resolved_utxo as a script input.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    spend_reds = _collect_all_spend_redeemers(builder)
    assert len(spend_reds) == 1, (
        f"expected exactly 1 script-spend redeemer (the Resolved "
        f"challenge UTxO); got {len(spend_reds)}. Keys: "
        + ", ".join(f"{k[0].hex()[:8]}#{k[1]}" for k in spend_reds)
    )


def test_mint_burns_exactly_one_challenge_token(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    sample_challenge_token_bytes,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """builder.mint MUST contain EXACTLY {challenge_policy:
    {challenge_token: -1}} — one burn, no other assets. v13 L1681-1683:
        burn_ma = MultiAsset()
        chal_burn = Asset(); chal_burn[chal_token_an] = -1
        burn_ma[challenge_policy_sh] = chal_burn
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    mint = getattr(builder, "mint", None) or getattr(builder, "_mint", None)
    assert mint is not None, (
        "CleanupResolved MUST mint (burn -1) the challenge NFT. "
        "builder.mint is None."
    )

    challenge_policy = ScriptHash(
        bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"])
    )
    # Iterate policy entries
    mint_policies = list(mint.data.keys()) if hasattr(mint, "data") else list(mint.keys())
    assert challenge_policy in mint_policies, (
        f"builder.mint must contain the challenge policy "
        f"{bytes(challenge_policy).hex()}; got policies: "
        + ", ".join(bytes(p).hex()[:8] for p in mint_policies)
    )
    # Exactly one policy — no stray mints/burns elsewhere.
    assert len(mint_policies) == 1, (
        f"builder.mint must contain ONLY the challenge policy; got "
        f"{len(mint_policies)} policies."
    )

    asset_dict = mint[challenge_policy]
    asset_items = (
        list(asset_dict.data.items()) if hasattr(asset_dict, "data")
        else list(asset_dict.items())
    )
    assert len(asset_items) == 1, (
        f"Challenge-policy mint entry must burn exactly one token "
        f"(the challenge NFT); got {len(asset_items)} entries."
    )
    an, qty = asset_items[0]
    expected_an = AssetName(sample_challenge_token_bytes)
    assert an == expected_an, (
        f"Burned token name must be the challenge NFT's name "
        f"{sample_challenge_token_bytes.hex()}; got {bytes(an).hex()}."
    )
    assert int(qty) == -1, (
        f"Challenge NFT must be BURNED (qty=-1); got qty={qty}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Continuing output — MUST be NONE at challenge addr
# ═════════════════════════════════════════════════════════════════════════


def test_no_continuing_output_at_challenge_addr(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """challenge.ak L873-875 (`no_challenge_outputs`) requires zero
    outputs at the challenge script address. The lifecycle is CLOSING
    out — the challenge is permanently removed from chain state. Any
    stray output at challenge_addr would fail validation AND pollute
    the UTxO set with a tokenless junk entry.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    chal_outs = _challenge_outputs(builder)
    assert len(chal_outs) == 0, (
        f"CleanupResolved MUST NOT emit any output at the challenge "
        f"script address (challenge.ak L873-875). Got {len(chal_outs)} "
        f"outputs there."
    )


# ═════════════════════════════════════════════════════════════════════════
# Reference-input topology
# ═════════════════════════════════════════════════════════════════════════


def test_cross_refs_and_params_utxo_in_reference_inputs(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
    sample_cross_refs_utxo, sample_params_utxo,
):
    """The validator calls get_cross_refs(config, tx.reference_inputs)
    at the spend-handler entry and reads ProtocolParams from params_utxo
    (for cleanup_buffer). Both MUST appear in reference_inputs.
    v13 L1692: `b.reference_inputs.add(cross_refs_utxo)`.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
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
        "cross_refs_utxo MUST appear in reference_inputs (validator "
        "reads refs via get_cross_refs). v13 step9 L1692."
    )
    assert params_key in ref_keys, (
        "params_utxo MUST appear in reference_inputs so the validator "
        "can read cleanup_buffer from ProtocolParams."
    )


# ═════════════════════════════════════════════════════════════════════════
# Signature — permissionless, wallet vkh ONLY
# ═════════════════════════════════════════════════════════════════════════


def test_required_signers_is_only_fee_payer(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """CleanupResolved is PERMISSIONLESS (challenge.ak L857). The only
    signature required is the fee payer's. No oracle, no auditor
    (beyond the fact that the fee-payer's wallet IS the auditor by
    convention in v13), no extra keys.

    If the brief's Phase-1.0 residual doc-comment claimed an oracle
    sig is needed, that is stale — Phase 1.1 removed it (L857).
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    required = builder.required_signers or []
    required_bytes = {bytes(s) for s in required}

    _, vkey, _ = sample_auditor_wallet
    fee_payer_vkh = bytes(vkey.hash())

    assert fee_payer_vkh in required_bytes, (
        f"fee-payer's vkh MUST appear in required_signers; got "
        f"{[h.hex()[:8] for h in required_bytes]}."
    )
    assert required_bytes == {fee_payer_vkh}, (
        f"required_signers must contain ONLY the fee-payer vkh "
        f"(CleanupResolved is permissionless). Got "
        f"{[h.hex()[:8] for h in required_bytes]}."
    )


# ═════════════════════════════════════════════════════════════════════════
# Time gate — validity_start strictly past cleanup cutoff
# ═════════════════════════════════════════════════════════════════════════


def test_validity_start_strictly_past_cleanup_cutoff(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """challenge.ak L854-855 requires
        tx_started_after(tx, challenged_at + resolution_deadline + cleanup_buffer).
    `tx_started_after` uses tx.validity_range.lower_bound and requires
    it to be STRICTLY > the cutoff. The builder's validity_start (slot)
    must correspond to a POSIX-ms value > cutoff_ms.

    Cutoff in slots = (challenged_at_ms + resolution_deadline_ms +
                       cleanup_buffer_ms) // 1000 - SYSTEM_START_UNIX.
    SYSTEM_START_UNIX = 1752057484 (v13 testnet constant).

    v13 L1696: `b.validity_start = max(current_slot - 60, cleanup_after_slot + 1)`
    — i.e. strictly ABOVE cleanup_after_slot.
    """
    from simulation.tests.conftest import CANNED_SLOT
    SYSTEM_START_UNIX = 1752057484

    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    # Extract the datum's challenged_at field[6]
    raw = sample_resolved_challenge_utxo_for_cleanup.output.datum.cbor
    decoded = cbor2.loads(bytes(raw))
    challenged_at_ms = decoded.value[6]
    cutoff_ms = (
        challenged_at_ms
        + default_resolution_deadline_ms
        + default_cleanup_buffer_ms
    )
    cutoff_slot = cutoff_ms // 1000 - SYSTEM_START_UNIX

    validity_start = builder.validity_start
    assert validity_start is not None, (
        "builder.validity_start MUST be set — the validator rejects TXs "
        "without a lower-bound validity (open-ended starts mean no "
        "tx_started_after check can succeed)."
    )
    assert validity_start > cutoff_slot, (
        f"validity_start ({validity_start}) must be STRICTLY greater "
        f"than cleanup cutoff slot ({cutoff_slot}). challenge.ak L854-855."
    )
    # And it must be <= current slot so the network accepts the TX.
    assert validity_start <= CANNED_SLOT, (
        f"validity_start ({validity_start}) must be <= current slot "
        f"({CANNED_SLOT}) — else no block can include the TX. "
        f"v13 L1696 caps it at `current_slot - 60`."
    )


def test_ttl_set_reasonably_after_validity_start(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """builder.ttl MUST be set and strictly greater than validity_start
    (otherwise the TX is born expired). v13 L1697: `b.ttl = current_slot + 3600`.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    assert builder.ttl is not None, (
        "builder.ttl MUST be set — unbounded-above TXs are not accepted."
    )
    assert builder.ttl > (builder.validity_start or 0), (
        f"ttl ({builder.ttl}) must be strictly greater than "
        f"validity_start ({builder.validity_start}). v13 L1696-1697."
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards (raise BEFORE submit)
# ═════════════════════════════════════════════════════════════════════════


def test_raises_if_challenge_state_is_voting(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    sample_challenge_utxo_voting_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """If the passed challenge UTxO is still in Voting state (validator
    would reject with `state_ok == False` at L846-850), the builder MUST
    raise ValueError before submit. Wasting collateral on a guaranteed-
    fail TX is a bug.
    """
    with pytest.raises(ValueError, match=r"(?i)(state|resolved|voting)"):
        _run_build_cleanup_resolved(
            patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
            sample_deployment_with_challenge_ref_only,
            sample_auditor_wallet,
            sample_resolved_challenge_utxo_for_cleanup,
            default_resolution_deadline_ms, default_cleanup_buffer_ms,
            challenge_utxo_override=sample_challenge_utxo_voting_for_cleanup,
        )


def test_raises_if_time_gate_not_yet_passed(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    sample_resolved_challenge_utxo_for_cleanup_too_early,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """If `current_slot <= cleanup_after_slot` the validator's time gate
    is closed. Builder MUST raise ValueError up-front — submitting an
    early cleanup burns the collateral for no benefit, and (because the
    Resolved UTxO is still referenceable) gives the caller the false
    impression something transient went wrong.
    """
    with pytest.raises(ValueError, match=r"(?i)(time|early|cleanup|deadline|slot)"):
        _run_build_cleanup_resolved(
            patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
            sample_deployment_with_challenge_ref_only,
            sample_auditor_wallet,
            sample_resolved_challenge_utxo_for_cleanup,
            default_resolution_deadline_ms, default_cleanup_buffer_ms,
            challenge_utxo_override=sample_resolved_challenge_utxo_for_cleanup_too_early,
        )


def test_raises_if_challenge_utxo_missing_nft(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    sample_resolved_challenge_utxo_for_cleanup_no_nft,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """Defensive guard — if the Resolved challenge UTxO somehow lacks
    the challenge NFT (should never happen in a well-formed lifecycle;
    the token survives every spend until cleanup), the builder CANNOT
    construct a valid burn and MUST raise ValueError. Validator L861-870
    would reject a cleanup TX whose own_ref input has no policy token.
    """
    with pytest.raises(ValueError, match=r"(?i)(token|nft|challenge|policy|burn)"):
        _run_build_cleanup_resolved(
            patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
            sample_deployment_with_challenge_ref_only,
            sample_auditor_wallet,
            sample_resolved_challenge_utxo_for_cleanup,
            default_resolution_deadline_ms, default_cleanup_buffer_ms,
            challenge_utxo_override=sample_resolved_challenge_utxo_for_cleanup_no_nft,
        )


# ═════════════════════════════════════════════════════════════════════════
# Wallet inputs — at least one covers fees, and the wallet receives change
# ═════════════════════════════════════════════════════════════════════════


def test_wallet_input_added_for_fees(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_auditor_wallet_utxo_base_ap3x,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """v13 step9 L1691 iterates `wallet_utxos` and calls `b.add_input(u)`
    so the TX has enough lovelace to pay fees. The builder MUST attach
    at least the auditor's base-AP3X wallet UTxO as a plain (non-script)
    input.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    wallet_txid = bytes(
        sample_auditor_wallet_utxo_base_ap3x.input.transaction_id,
    )
    wallet_idx = sample_auditor_wallet_utxo_base_ap3x.input.index

    input_keys = {
        (bytes(i.input.transaction_id), i.input.index)
        for i in builder.inputs
    }
    assert (wallet_txid, wallet_idx) in input_keys, (
        f"Auditor wallet UTxO {wallet_txid.hex()}#{wallet_idx} must be "
        f"attached to the builder for fee payment (v13 L1691). "
        f"Got input keys: "
        + ", ".join(f"{k[0].hex()[:8]}#{k[1]}" for k in input_keys)
    )


# ═════════════════════════════════════════════════════════════════════════
# Result tx_hash plumbing
# ═════════════════════════════════════════════════════════════════════════


def test_tx_hash_comes_from_submit_tx_stub(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """The returned tx_hash MUST come from submit_tx (the patched stub
    returns a known sentinel prefix). Confirms the builder actually
    reaches the submit step — not aborting early after build_and_sign.
    """
    result, _ = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    assert result["tx_hash"].startswith("fake_cleanup_resolved_tx_hash_"), (
        f"tx_hash must come from the submit_tx stub "
        f"('fake_cleanup_resolved_tx_hash_...'); got {result['tx_hash']!r}. "
        f"If the value differs, submit_tx may not have been invoked."
    )


# ═════════════════════════════════════════════════════════════════════════
# Mint-script wiring — challenge ref utxo attaches the mint script
# ═════════════════════════════════════════════════════════════════════════


def test_challenge_ref_utxo_used_for_spend_and_mint_scripts(
    patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref_only,
    sample_auditor_wallet,
    sample_resolved_challenge_utxo_for_cleanup,
    sample_challenge_ref_script_utxo,
    default_resolution_deadline_ms, default_cleanup_buffer_ms,
):
    """The challenge ref-script UTxO hosts BOTH the spend script (for
    spending the Resolved UTxO) AND the mint script (for burning the
    NFT). It MUST appear in reference_inputs. v13 L1690 passes it as
    the ref for add_script_input; L1694 passes it to add_minting_script.
    """
    _, builder = _run_build_cleanup_resolved(
        patched_network_for_cleanup_resolved, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref_only,
        sample_auditor_wallet,
        sample_resolved_challenge_utxo_for_cleanup,
        default_resolution_deadline_ms, default_cleanup_buffer_ms,
    )
    chal_ref_txid = bytes(
        sample_challenge_ref_script_utxo.input.transaction_id,
    )
    chal_ref_idx = sample_challenge_ref_script_utxo.input.index

    ref_keys = {
        (bytes(r.input.transaction_id), r.input.index)
        for r in builder.reference_inputs
    }
    assert (chal_ref_txid, chal_ref_idx) in ref_keys, (
        f"challenge_ref_script_utxo {chal_ref_txid.hex()}#{chal_ref_idx} "
        f"must appear in reference_inputs (hosts both spend + mint "
        f"scripts for CleanupResolved). v13 L1690+L1694."
    )
