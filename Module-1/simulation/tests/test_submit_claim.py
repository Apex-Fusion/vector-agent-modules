"""
RED-phase tests for `simulation.tx_builder.build_submit_claim`.

Contract under test:
    build_submit_claim must produce a Cardano TransactionBuilder whose
    state matches the v13 testnet `step3_submit_claim` reference
    (`/home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py`
    lines 744-850). The current in-tree implementation
    (`simulation/tx_builder.py:111`) is WRONG on two counts:

      1. It builds an 8-field ClaimDatum with the wrong field order
         (claim_hash / claim_type / storage_uri are all missing or
         mis-ordered).  The contract's `ClaimDatum` (see
         `contracts/lib/adversarial_auditing/types.ak:28`) has NINE
         fields in this exact order:

            [0] claimer_did         : PolicyId (28 B)
            [1] claimer_credential  : Credential   (CBORTag(121, [vkh]))
            [2] claim_hash          : ByteArray (32 B, blake2b_256)
            [3] claim_type          : ByteArray
            [4] storage_uri         : ByteArray
            [5] stake_amount        : Int
            [6] submitted_at        : Int (POSIX ms)
            [7] challenge_window    : Int (ms)
            [8] state               : ClaimState  (CBORTag(121, []) = Open)

      2. It uses Path A — builds a multi-asset stake output with an
         `ApexAgentsTest` custom token. v13/Path B stores the stake in
         the output's COIN field; the multi_asset contains ONLY the
         claim NFT (1 token).

Every test in this file must FAIL against the current implementation
(modulo a small number that may incidentally pass — e.g. signer check,
validity_start). Catherine rewrites `build_submit_claim` to make them
pass.
"""
from __future__ import annotations

import hashlib

import cbor2
import pytest

from pycardano import (
    Address,
    AssetName,
    MultiAsset,
    ScriptHash,
    TransactionBuilder,
    UTxO,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _claim_output(builder: TransactionBuilder):
    """Return the builder's output that lands at the claim script address.

    build_submit_claim creates exactly one such output (the claim UTxO).
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    claim_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"])
    for out in builder.outputs:
        if out.address == claim_addr:
            return out
    pytest.fail(
        f"No builder output routed to claim script address. "
        f"Got {[str(o.address) for o in builder.outputs]}"
    )


def _decode_datum(output) -> list:
    """Unpack the CBORTag-wrapped Plutus datum on a TransactionOutput."""
    datum = output.datum
    # RawCBOR wraps raw bytes; RawPlutusData wraps the already-decoded value.
    if hasattr(datum, "cbor"):
        raw = datum.cbor
    else:
        raw = bytes(datum)
    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag"), f"Datum outer is not a CBORTag: {decoded!r}"
    assert decoded.tag == 121, f"Datum outer tag must be 121 (Constr0), got {decoded.tag}"
    assert isinstance(decoded.value, list), f"Datum payload must be list, got {type(decoded.value)}"
    return decoded.value


def _run_build_submit_claim(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    """Invoke build_submit_claim and return (result, builder)."""
    from simulation.tx_builder import build_submit_claim

    skey, vkey, wallet_addr = sample_wallet
    result = build_submit_claim(
        mock_ogmios_context,
        sample_deployment,
        skey, vkey, wallet_addr,
        sample_did_hex,
        default_stake_amount,
        challenge_window_ms=default_challenge_window_ms,
        claim_hash=sample_claim_hash,
        claim_type=b"data_indexing",
        storage_uri=b"ipfs://test-claim-cid",
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_submit_claim did not reach the build step."
    )
    return result, captured_builder[-1]


# ─────────────────────────────────────────────────────────────────────────
# Datum structure tests
# ─────────────────────────────────────────────────────────────────────────

def test_submit_claim_datum_has_9_fields(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert len(fields) == 9, (
        f"ClaimDatum must have 9 fields (see types.ak:28); got {len(fields)}. "
        f"Fields: {fields!r}"
    )


def test_submit_claim_datum_claimer_did_at_index_0(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert fields[0] == bytes.fromhex(sample_did_hex), (
        f"Field[0] must be claimer_did bytes ({sample_did_hex}); "
        f"got {fields[0]!r}"
    )


def test_submit_claim_datum_claimer_credential_at_index_1(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    _, vkey, _ = sample_wallet
    cred = fields[1]
    assert hasattr(cred, "tag") and cred.tag == 121, (
        f"Field[1] must be Credential CBORTag(121, [vkh]) = VerificationKey variant; "
        f"got {cred!r}"
    )
    assert cred.value == [bytes(vkey.hash())], (
        f"Credential payload must be [vkey.hash()] = [{bytes(vkey.hash()).hex()}]; "
        f"got {cred.value!r}"
    )


def test_submit_claim_datum_claim_hash_at_index_2(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert fields[2] == sample_claim_hash, (
        f"Field[2] must be the 32-byte claim_hash; got {fields[2]!r}"
    )
    assert isinstance(fields[2], (bytes, bytearray))
    assert len(fields[2]) == 32, f"claim_hash must be exactly 32 bytes; got {len(fields[2])}"


def test_submit_claim_datum_claim_type_at_index_3(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert isinstance(fields[3], (bytes, bytearray)), (
        f"Field[3] claim_type must be ByteArray; got {type(fields[3]).__name__}"
    )
    assert fields[3] == b"data_indexing", (
        f"Field[3] must equal the claim_type arg (b'data_indexing'); got {fields[3]!r}"
    )


def test_submit_claim_datum_storage_uri_at_index_4(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert isinstance(fields[4], (bytes, bytearray)), (
        f"Field[4] storage_uri must be ByteArray; got {type(fields[4]).__name__}"
    )
    assert fields[4] == b"ipfs://test-claim-cid", (
        f"Field[4] must equal the storage_uri arg; got {fields[4]!r}"
    )


def test_submit_claim_datum_stake_amount_at_index_5(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert isinstance(fields[5], int), (
        f"Field[5] stake_amount must be Int; got {type(fields[5]).__name__}"
    )
    assert fields[5] == default_stake_amount, (
        f"Field[5] must equal stake_amount arg ({default_stake_amount}); got {fields[5]}"
    )


def test_submit_claim_datum_submitted_at_at_index_6(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    from simulation.tests.conftest import CANNED_SLOT
    from simulation.config import SYSTEM_START_UNIX

    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert isinstance(fields[6], int), (
        f"Field[6] submitted_at must be Int; got {type(fields[6]).__name__}"
    )
    # submitted_at should be posix ms anchored on current_slot. v13 uses
    # (SYSTEM_START_UNIX + current_slot - 30) * 1000; allow some tolerance.
    expected_ms = (SYSTEM_START_UNIX + CANNED_SLOT) * 1000
    assert abs(fields[6] - expected_ms) < 120_000, (  # within 2 min
        f"Field[6] submitted_at should be ~{expected_ms} ms (current_slot "
        f"as POSIX ms); got {fields[6]}"
    )


def test_submit_claim_datum_challenge_window_at_index_7(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    assert isinstance(fields[7], int), (
        f"Field[7] challenge_window must be Int; got {type(fields[7]).__name__}"
    )
    assert fields[7] == default_challenge_window_ms, (
        f"Field[7] must equal challenge_window_ms arg "
        f"({default_challenge_window_ms}); got {fields[7]}"
    )


def test_submit_claim_datum_state_at_index_8_is_Open(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    fields = _decode_datum(_claim_output(builder))
    state = fields[8]
    assert hasattr(state, "tag") and state.tag == 121, (
        f"Field[8] state must be CBORTag(121, []) = Open (Constr0); got {state!r}"
    )
    assert state.value == [], (
        f"Open state payload must be []; got {state.value!r}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Output value / Path B stake tests
# ─────────────────────────────────────────────────────────────────────────

def test_submit_claim_stake_in_coin_not_multi_asset(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    """Path B: stake goes in the coin field. The output's multi_asset
    must contain ONLY the claim NFT (1 token) — no ApexAgentsTest /
    legacy AP3X asset name."""
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    out = _claim_output(builder)
    val = out.amount
    assert val.coin == default_stake_amount, (
        f"Claim output coin field must equal stake_amount ({default_stake_amount}); "
        f"got {val.coin}. This indicates Path A (legacy multi-asset stake) is "
        f"still in use."
    )
    # Multi-asset must have exactly one policy with exactly one asset, qty 1.
    ma = val.multi_asset
    assert len(ma) == 1, (
        f"Claim output multi_asset must contain exactly one policy (the claim "
        f"NFT policy); got {len(ma)}: {ma!r}"
    )
    (only_policy, only_assets), = ma.data.items() if hasattr(ma, "data") else list(ma.items())
    assert len(only_assets) == 1, (
        f"The claim NFT policy must carry exactly one token; got {len(only_assets)}"
    )
    qty = list(only_assets.values())[0]
    assert qty == 1, f"Claim NFT quantity must be 1; got {qty}"

    # And that sole policy must equal the deployment's claim policy.
    claim_policy = ScriptHash(bytes.fromhex(sample_deployment.claim_hash))
    assert only_policy == claim_policy, (
        f"Multi-asset policy must equal claim policy {claim_policy}; "
        f"got {only_policy}"
    )


def test_submit_claim_mints_exactly_one_claim_nft(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    mint_ma = builder.mint
    assert mint_ma is not None, "builder.mint must be set"
    claim_policy = ScriptHash(bytes.fromhex(sample_deployment.claim_hash))
    ma_items = dict(mint_ma.data.items()) if hasattr(mint_ma, "data") else dict(mint_ma.items())
    assert claim_policy in ma_items, (
        f"Mint must include the claim policy {claim_policy}; got policies "
        f"{list(ma_items.keys())}"
    )
    claim_assets = ma_items[claim_policy]
    assert len(claim_assets) == 1, (
        f"Exactly one claim-NFT asset must be minted; got {len(claim_assets)}"
    )
    qty = list(claim_assets.values())[0]
    assert qty == 1, f"Claim NFT mint quantity must be 1; got {qty}"


def test_submit_claim_claim_nft_token_name_derived_from_seed(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    sample_wallet_utxo_base_ap3x,
):
    """Claim NFT token name is b'clm_' || blake2b_256(cbor(seed_ref))[:28].

    Reference: simulation/chain.py:derive_token_name and
    contracts/lib/adversarial_auditing/utils.ak."""
    from simulation.chain import claim_token_name

    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    seed_tx_hash = bytes(sample_wallet_utxo_base_ap3x.input.transaction_id)
    seed_idx = sample_wallet_utxo_base_ap3x.input.index
    expected_name = claim_token_name(seed_tx_hash, seed_idx)

    claim_policy = ScriptHash(bytes.fromhex(sample_deployment.claim_hash))
    mint_ma = builder.mint
    ma_items = dict(mint_ma.data.items()) if hasattr(mint_ma, "data") else dict(mint_ma.items())
    claim_assets = ma_items[claim_policy]
    an_items = dict(claim_assets.data.items()) if hasattr(claim_assets, "data") else dict(claim_assets.items())
    assert any(bytes(an) == expected_name for an in an_items.keys()), (
        f"Claim NFT token name must be derive_token_name(b'clm_', seed_tx, seed_idx) "
        f"= {expected_name.hex()}; got {[bytes(an).hex() for an in an_items.keys()]}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Mint redeemer
# ─────────────────────────────────────────────────────────────────────────

def test_submit_claim_mint_redeemer_is_nullary_constr0(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    """Mint redeemer must be the nullary Constr0 = CBORTag(121, []).

    The Aiken type `ClaimAction.SubmitClaim`
    (`contracts/lib/adversarial_auditing/types.ak:69`) is a nullary
    constructor — it takes ZERO fields. Canonical Plutus Data encoding
    for a nullary Constr0 is CBORTag(121, []) i.e. raw bytes 0xd8 0x79
    0x80.

    The NFT token name is derived inside the claim validator from
    `tx.inputs[0].output_reference`
    (`contracts/validators/claim.ak:127-129`) — the redeemer value is
    never read. Putting anything inside the redeemer's arg list (e.g. a
    nested seed_ref) causes the Aiken `expect redeemer: ClaimAction =
    raw` decode to crash at evaluateTransaction time (Ogmios HTTP 400
    code 3012 with trace "redeemer: ClaimAction") before any validator
    body trace fires.

    The v13 mainnet reference
    (`/home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py:812`)
    uses the correct empty form.
    """
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    # Find the mint redeemer on the builder's minting scripts.
    # PyCardano exposes minting redeemers via builder._minting_script_to_redeemers
    # or builder.redeemers / builder.witness_set. We introspect generically.
    redeemers = _collect_mint_redeemers(builder)
    assert redeemers, "No mint redeemers found on builder."
    assert len(redeemers) == 1, (
        f"Expected exactly one mint redeemer; got {len(redeemers)}"
    )
    red = redeemers[0]
    raw = red.data.cbor if hasattr(red.data, "cbor") else bytes(red.data)
    # Strong assertion: exact canonical byte encoding of CBORTag(121, []).
    assert raw == b"\xd8\x79\x80", (
        f"Mint redeemer must be the nullary Constr0 CBORTag(121, []) "
        f"= 0xd87980; got {raw.hex()}. Any non-empty arg list crashes "
        f"the Aiken `expect redeemer: ClaimAction = raw` decode."
    )
    # Structural assertion (mirrors style of test_submit_claim_datum_state_at_index_8_is_Open).
    outer = cbor2.loads(raw)
    assert hasattr(outer, "tag") and outer.tag == 121, (
        f"Mint redeemer must be CBORTag(121, ...); got {outer!r}"
    )
    assert outer.value == [], (
        f"SubmitClaim is nullary — payload must be []; got {outer.value!r}"
    )


def _collect_mint_redeemers(builder: TransactionBuilder):
    """Return a list of mint Redeemers on the builder.

    PyCardano's TransactionBuilder stores minting-script redeemers in
    `_minting_script_to_redeemers` as `List[Tuple[script_or_utxo, Redeemer]]`.
    This internal API differs slightly across releases; walk anything
    that looks like it.
    """
    mint_reds = []
    candidates = (
        "_minting_script_to_redeemers",
        "_script_to_redeemers",
        "_redeemers",
    )
    for attr in candidates:
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


# ─────────────────────────────────────────────────────────────────────────
# Output destination / reference inputs / signer / validity
# ─────────────────────────────────────────────────────────────────────────

def test_submit_claim_output_at_claim_script_addr(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    out = _claim_output(builder)
    assert out.address == sample_deployment.claim_addr, (
        f"Claim output address must be deployment.claim_addr "
        f"({sample_deployment.claim_addr}); got {out.address}"
    )


def test_submit_claim_reference_inputs_include_cross_refs_and_params_and_registry(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    sample_cross_refs_utxo, sample_params_utxo, sample_registry_did_utxo,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    ref_inputs_set = builder.reference_inputs
    # reference_inputs may store UTxOs or TransactionInputs depending on
    # pycardano version. Normalise to TransactionInput for comparison.
    seen_refs = set()
    for r in ref_inputs_set:
        if hasattr(r, "input"):  # UTxO
            seen_refs.add((bytes(r.input.transaction_id), r.input.index))
        else:  # TransactionInput
            seen_refs.add((bytes(r.transaction_id), r.index))

    for name, utxo in [
        ("cross_refs", sample_cross_refs_utxo),
        ("params", sample_params_utxo),
        ("registry_did", sample_registry_did_utxo),
    ]:
        key = (bytes(utxo.input.transaction_id), utxo.input.index)
        assert key in seen_refs, (
            f"reference_inputs must contain {name} UTxO "
            f"{utxo.input.transaction_id.payload.hex()}#{utxo.input.index}; "
            f"got {[(k[0].hex(), k[1]) for k in seen_refs]}"
        )


def test_submit_claim_signer_is_claimer_vkh(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    _, vkey, _ = sample_wallet
    required = builder.required_signers or []
    hashes = [bytes(h) for h in required]
    assert hashes == [bytes(vkey.hash())], (
        f"required_signers must be [vkey.hash()] = [{bytes(vkey.hash()).hex()}]; "
        f"got {[h.hex() for h in hashes]}"
    )


def test_submit_claim_validity_start_is_current_slot_minus_60(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    from simulation.tests.conftest import CANNED_SLOT

    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    assert builder.validity_start is not None, "validity_start must be set"
    # Allow a small slop — v13 uses current_slot - 60.
    assert abs(builder.validity_start - (CANNED_SLOT - 60)) <= 5, (
        f"validity_start must be ~current_slot-60 ({CANNED_SLOT - 60}); "
        f"got {builder.validity_start}"
    )


def test_submit_claim_ttl_current_slot_plus_3600(
    patched_network, captured_builder, mock_ogmios_context,
    sample_deployment, sample_wallet, sample_did_hex,
    sample_claim_hash, default_stake_amount, default_challenge_window_ms,
):
    from simulation.tests.conftest import CANNED_SLOT

    _, builder = _run_build_submit_claim(
        patched_network, captured_builder, mock_ogmios_context,
        sample_deployment, sample_wallet, sample_did_hex,
        sample_claim_hash, default_stake_amount, default_challenge_window_ms,
    )
    assert builder.ttl is not None, "ttl must be set"
    assert abs(builder.ttl - (CANNED_SLOT + 3600)) <= 5, (
        f"ttl must be ~current_slot+3600 ({CANNED_SLOT + 3600}); got {builder.ttl}"
    )
