"""
RED-phase tests for `simulation.tx_builder.build_open_challenge`.

Function under test (NOT YET IMPLEMENTED):
    build_open_challenge(
        context, deployment,
        auditor_skey, auditor_vkey, auditor_wallet_addr,
        auditor_did_hex: str,
        claim_utxo_ref: str,               # "<txid>#<idx>"
        eligible_jurors: list[bytes],      # 28-byte DIDs (sorted)
        *,
        stake_amount: int,
        evidence_hash: bytes,              # 32 B
        evidence_uri: bytes,
        resolution_deadline_ms: int,
        jury_size: int = 5,
        oracle_active: bool = False,
    ) -> dict

Contract reference:
    - Datum shape: `contracts/lib/adversarial_auditing/types.ak`
      ChallengeDatum — 10 fields in this order (see the task brief):
        [0] claim_ref              OutputReference  CBORTag(121, [txid, idx])
        [1] auditor_did            PolicyId (28 B)
        [2] auditor_credential     Credential       CBORTag(121, [vkh])
        [3] stake_amount           Int
        [4] evidence_hash          ByteArray (32 B)
        [5] evidence_uri           ByteArray
        [6] challenged_at          Int (POSIX ms)
        [7] resolution_deadline    Int (ms)
        [8] eligible_jurors        List<PolicyId>   sorted bytewise
        [9] state                  ChallengeState   PendingJury=Constr1=CBORTag(122, [])

    - Validator invariants: `contracts/validators/challenge.ak`
        - Path B stake: out.coin == stake_amount (exact equality)
        - single_output: exactly one output at challenge script addr
        - has_token: challenge NFT minted (1 qty)
        - no_self_audit: auditor_did != claim.claimer_did
        - within_window: tx ends before claim.submitted_at+challenge_window
        - stake_sufficient: challenge.stake_amount >= claim.stake_amount
        - state_ok: oracle inactive → state = PendingJury
        - jurors_sorted: eligible_jurors == sort_dids(eligible_jurors)
        - pool_large_enough: len(eligible_jurors) >= 3 * jury_size
        - evidence_hash_ok: 32 bytes
        - claim_updated: continuing claim output's state = Challenged
        - Atomic: spends claim UTxO with MarkChallenged redeemer
                  (CBORTag(123, []) = Constr2 of ClaimAction)

Reference implementation we mirror:
    `/home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py`
    lines 853-995 (step4_open_challenge).

All tests in this file MUST fail against the current tree because
`build_open_challenge` does NOT yet exist in `tx_builder.py`.  The RED
signal is `ImportError` / `AttributeError` on the function; Catherine
implements the function to turn the suite GREEN.

Two tests specifically enforce CLIENT-SIDE GUARDS — not on-chain checks:
    - test_no_self_audit_raises_when_auditor_equals_claimer_did
    - test_pool_large_enough_raises_when_fewer_than_3x_jury_size
These MUST raise `ValueError` BEFORE reaching chain-effort territory —
failing fast at the builder is cheaper than submitting a doomed TX.
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


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════

def _challenge_output(builder: TransactionBuilder):
    """Return the builder's (single) output routed to the challenge script
    address.  build_open_challenge MUST produce exactly one such output —
    the `single_output` validator invariant.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    chal_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    hits = [o for o in builder.outputs if o.address == chal_addr]
    assert len(hits) == 1, (
        f"Expected exactly one output at challenge script addr "
        f"{chal_addr}; got {len(hits)}. All outputs: "
        f"{[str(o.address) for o in builder.outputs]}"
    )
    return hits[0]


def _continuing_claim_output(builder: TransactionBuilder):
    """Return the builder output that is the CONTINUING claim output —
    i.e., the updated claim UTxO that routes back to the claim script
    address carrying state=Challenged.
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    claim_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"])
    hits = [o for o in builder.outputs if o.address == claim_addr]
    assert len(hits) == 1, (
        f"Expected exactly one CONTINUING claim output at claim addr "
        f"{claim_addr}; got {len(hits)}"
    )
    return hits[0]


def _decode_datum(output) -> list:
    """Unpack the CBORTag-wrapped Plutus datum on a TransactionOutput."""
    datum = output.datum
    if hasattr(datum, "cbor"):
        raw = datum.cbor
    else:
        raw = bytes(datum)
    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag"), f"Datum outer is not a CBORTag: {decoded!r}"
    return decoded


def _collect_mint_redeemers(builder: TransactionBuilder):
    """Collect mint Redeemers across PyCardano versions — mirrors the
    helper in test_submit_claim.py.
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


def _collect_spend_redeemers_for_input(builder: TransactionBuilder, utxo: UTxO):
    """Return the spend Redeemer attached to the given claim UTxO input.

    PyCardano stores script-input redeemers in
    `_inputs_to_redeemers` / `_input_to_redeemer` dict-like structures;
    we walk common attr names and match on TransactionInput equality.
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
    # Fallback: scan list-of-tuples form
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


def _run_build_open_challenge(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    *,
    auditor_did_override: str | None = None,
    eligible_override: list | None = None,
):
    """Invoke build_open_challenge with the canonical fixture inputs."""
    from simulation.tx_builder import build_open_challenge

    skey, vkey, auditor_addr = sample_auditor_wallet
    claim_ref = (
        f"{bytes(sample_claim_utxo.input.transaction_id).hex()}"
        f"#{sample_claim_utxo.input.index}"
    )
    auditor_did = auditor_did_override or sample_auditor_did_hex
    eligible = eligible_override if eligible_override is not None else sample_eligible_jurors

    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    result = build_open_challenge(
        mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        skey, vkey, auditor_addr,
        auditor_did,
        claim_ref,
        eligible,
        stake_amount=default_stake_amount,
        evidence_hash=evidence_hash,
        evidence_uri=b"ipfs://claire-test-evidence-uri",
        resolution_deadline_ms=default_resolution_deadline_ms,
        jury_size=default_jury_size,
        oracle_active=False,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_open_challenge did not reach the build step."
    )
    return result, captured_builder[-1], evidence_hash


# ═════════════════════════════════════════════════════════════════════════
# Datum structure tests — ChallengeDatum has 10 fields
# ═════════════════════════════════════════════════════════════════════════

def test_challenge_datum_has_10_fields(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    decoded = _decode_datum(_challenge_output(builder))
    assert decoded.tag == 121, (
        f"ChallengeDatum outer tag must be 121 (Constr0); got {decoded.tag}"
    )
    fields = decoded.value
    assert len(fields) == 10, (
        f"ChallengeDatum must have 10 fields (see types.ak ChallengeDatum); "
        f"got {len(fields)}. Fields: {fields!r}"
    )


def test_challenge_datum_claim_ref_at_index_0_is_Constr0_with_txid_and_idx(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    claim_ref = fields[0]
    assert hasattr(claim_ref, "tag") and claim_ref.tag == 121, (
        f"Field[0] claim_ref must be CBORTag(121, [txid, idx]) "
        f"(OutputReference Constr0); got {claim_ref!r}"
    )
    assert isinstance(claim_ref.value, list) and len(claim_ref.value) == 2, (
        f"claim_ref payload must be [txid, idx]; got {claim_ref.value!r}"
    )
    txid, idx = claim_ref.value
    assert txid == bytes(sample_claim_utxo.input.transaction_id), (
        f"claim_ref txid must equal the spent claim UTxO's txid "
        f"({bytes(sample_claim_utxo.input.transaction_id).hex()}); "
        f"got {txid.hex() if isinstance(txid, (bytes, bytearray)) else txid!r}"
    )
    assert idx == sample_claim_utxo.input.index, (
        f"claim_ref idx must equal the spent claim UTxO's index "
        f"({sample_claim_utxo.input.index}); got {idx}"
    )


def test_challenge_datum_auditor_did_at_index_1(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    assert fields[1] == bytes.fromhex(sample_auditor_did_hex), (
        f"Field[1] must be auditor_did bytes ({sample_auditor_did_hex}); "
        f"got {fields[1]!r}"
    )
    assert isinstance(fields[1], (bytes, bytearray)) and len(fields[1]) == 28, (
        f"auditor_did must be 28-byte PolicyId; got "
        f"{len(fields[1]) if isinstance(fields[1], (bytes, bytearray)) else 'non-bytes'}"
    )


def test_challenge_datum_auditor_credential_at_index_2_is_VerificationKey_variant(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    _, vkey, _ = sample_auditor_wallet
    cred = fields[2]
    assert hasattr(cred, "tag") and cred.tag == 121, (
        f"Field[2] auditor_credential must be CBORTag(121, [vkh]) = "
        f"VerificationKey Credential variant; got {cred!r}"
    )
    assert cred.value == [bytes(vkey.hash())], (
        f"Credential payload must be [auditor_vkey.hash()] = "
        f"[{bytes(vkey.hash()).hex()}]; got {cred.value!r}"
    )


def test_challenge_datum_stake_amount_at_index_3_equals_param(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    assert isinstance(fields[3], int), (
        f"Field[3] stake_amount must be Int; got {type(fields[3]).__name__}"
    )
    assert fields[3] == default_stake_amount, (
        f"Field[3] must equal stake_amount arg ({default_stake_amount}); "
        f"got {fields[3]}"
    )


def test_challenge_datum_evidence_hash_at_index_4_is_32_bytes(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, evidence_hash = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    assert isinstance(fields[4], (bytes, bytearray)), (
        f"Field[4] evidence_hash must be ByteArray; got {type(fields[4]).__name__}"
    )
    assert len(fields[4]) == 32, (
        f"evidence_hash must be exactly 32 bytes (blake2b_256); got {len(fields[4])}"
    )
    assert fields[4] == evidence_hash, (
        f"evidence_hash must equal the param bytes; got mismatch"
    )


def test_challenge_datum_evidence_uri_at_index_5_is_bytearray(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    assert isinstance(fields[5], (bytes, bytearray)), (
        f"Field[5] evidence_uri must be ByteArray; got {type(fields[5]).__name__}"
    )
    assert fields[5] == b"ipfs://claire-test-evidence-uri", (
        f"Field[5] must equal evidence_uri arg; got {fields[5]!r}"
    )


def test_challenge_datum_challenged_at_at_index_6_within_validity_window(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    from simulation.tests.conftest import CANNED_SLOT
    from simulation.config import SYSTEM_START_UNIX

    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    challenged_at = fields[6]
    assert isinstance(challenged_at, int), (
        f"Field[6] challenged_at must be Int; got {type(challenged_at).__name__}"
    )
    # v13 reference uses (SYSTEM_START_UNIX + current_slot - 15) * 1000.
    # Allow tolerance — any value anchored on the current slot within
    # ±2 min of that expectation is acceptable.
    expected = (SYSTEM_START_UNIX + CANNED_SLOT) * 1000
    assert abs(challenged_at - expected) < 120_000, (
        f"Field[6] challenged_at should be ~(SYSTEM_START_UNIX + "
        f"current_slot) * 1000 ≈ {expected}; got {challenged_at}. "
        f"Must fall within the TX validity window."
    )
    # Also verify it lies inside the builder's validity_start..ttl window
    # (converted to POSIX ms).
    validity_start_ms = (SYSTEM_START_UNIX + builder.validity_start) * 1000
    ttl_ms = (SYSTEM_START_UNIX + builder.ttl) * 1000
    assert validity_start_ms <= challenged_at <= ttl_ms, (
        f"challenged_at {challenged_at} must be within tx validity window "
        f"[{validity_start_ms}..{ttl_ms}]"
    )


def test_challenge_datum_resolution_deadline_at_index_7_equals_param(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    assert isinstance(fields[7], int), (
        f"Field[7] resolution_deadline must be Int; got {type(fields[7]).__name__}"
    )
    assert fields[7] == default_resolution_deadline_ms, (
        f"Field[7] must equal resolution_deadline_ms arg "
        f"({default_resolution_deadline_ms}); got {fields[7]}"
    )


def test_challenge_datum_eligible_jurors_at_index_8_sorted_bytewise(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """`jurors_sorted` validator check: eligible_jurors must equal
    sort_dids(eligible_jurors) — bytewise ascending sort."""
    # Even if the caller passes an unsorted list, the builder MUST sort
    # it before embedding in the datum.  Force unsorted by reversing.
    unsorted = list(reversed(sample_eligible_jurors))
    assert unsorted != sorted(unsorted), (
        "Test setup bug: reversed fixture happens to be sorted — "
        "choose different test data."
    )
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
        eligible_override=unsorted,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    jurors = fields[8]
    assert isinstance(jurors, list), (
        f"Field[8] eligible_jurors must be List; got {type(jurors).__name__}"
    )
    assert jurors == sorted(jurors), (
        f"eligible_jurors must be bytewise-sorted ascending (per the "
        f"`jurors_sorted` validator); got {[d.hex() for d in jurors]}"
    )


def test_challenge_datum_eligible_jurors_at_index_8_length_matches_input(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    jurors = fields[8]
    assert len(jurors) == len(sample_eligible_jurors), (
        f"Field[8] length must match input ({len(sample_eligible_jurors)}); "
        f"got {len(jurors)} — builder must not drop or duplicate entries."
    )
    assert set(jurors) == set(sample_eligible_jurors), (
        "Juror set must equal the input set (no silent substitutions)."
    )


def test_challenge_datum_state_at_index_9_is_PendingJury_when_oracle_inactive(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """`state_ok` invariant: when oracle_active=False, state = PendingJury
    which serialises as Constr1 = CBORTag(122, [])."""
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    fields = _decode_datum(_challenge_output(builder)).value
    state = fields[9]
    assert hasattr(state, "tag") and state.tag == 122, (
        f"Field[9] state must be CBORTag(122, []) = Constr1 = PendingJury "
        f"when oracle_active=False; got {state!r}"
    )
    assert state.value == [], (
        f"PendingJury payload must be []; got {state.value!r}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Path B stake: challenge output value layout
# ═════════════════════════════════════════════════════════════════════════

def test_challenge_output_stake_in_coin_not_multi_asset(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """Path B: auditor stake goes in the output's COIN field.  The
    challenge output's multi_asset must contain ONLY the challenge NFT
    (exactly one policy, one asset name, qty 1).  No Path-A
    ApexAgentsTest asset.
    """
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    out = _challenge_output(builder)
    val = out.amount
    assert val.coin == default_stake_amount, (
        f"Challenge output coin field must equal stake_amount "
        f"({default_stake_amount}); got {val.coin}. Path A leakage suspected."
    )
    ma = val.multi_asset
    assert len(ma) == 1, (
        f"Challenge output multi_asset must contain exactly one policy "
        f"(the challenge NFT policy); got {len(ma)} policies."
    )
    items = list(ma.data.items()) if hasattr(ma, "data") else list(ma.items())
    only_policy, only_assets = items[0]
    assert len(only_assets) == 1, (
        f"The challenge NFT policy must carry exactly one token; "
        f"got {len(only_assets)}"
    )
    qty = list(only_assets.values())[0]
    assert qty == 1, f"Challenge NFT quantity must be 1; got {qty}"

    challenge_policy = ScriptHash(
        bytes.fromhex(sample_deployment_with_challenge_ref.challenge_hash)
    )
    assert only_policy == challenge_policy, (
        f"Multi-asset policy must equal challenge policy "
        f"{challenge_policy}; got {only_policy}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Minting: exactly 1 challenge NFT, correct token name, correct redeemer
# ═════════════════════════════════════════════════════════════════════════

def test_challenge_mints_exactly_one_challenge_nft(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    mint_ma = builder.mint
    assert mint_ma is not None, "builder.mint must be set"
    challenge_policy = ScriptHash(
        bytes.fromhex(sample_deployment_with_challenge_ref.challenge_hash)
    )
    items = dict(mint_ma.data.items()) if hasattr(mint_ma, "data") else dict(mint_ma.items())
    assert challenge_policy in items, (
        f"Mint must include the challenge policy {challenge_policy}; "
        f"got policies {list(items.keys())}"
    )
    challenge_assets = items[challenge_policy]
    assert len(challenge_assets) == 1, (
        f"Exactly one challenge-NFT asset must be minted; "
        f"got {len(challenge_assets)}"
    )
    qty = list(challenge_assets.values())[0]
    assert qty == 1, f"Challenge NFT mint quantity must be 1; got {qty}"


def test_challenge_nft_token_name_derived_from_seed(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    sample_challenge_token_name_deriver,
    sample_auditor_wallet_utxo_base_ap3x,
):
    """Challenge NFT token name is `b"chl_" || blake2b_256(cbor(seed_ref))[:28]`.

    The "seed" is the smallest-sort-key (txid, idx) among the TX's
    auditor wallet inputs AND the spent claim UTxO (mirrors v13
    lines 895-900: `sorted_inputs = sorted(all_inputs, key=...)`).
    The test asserts the NFT name is one of the two possible seeds
    (auditor wallet UTxO OR the claim UTxO), because the builder's
    exact choice depends on which has the lower bytewise txid — and the
    sample_claim_utxo / sample_auditor_wallet_utxo fixtures are
    deterministic, so either option is reproducible.
    """
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )

    # Compute the canonical seed exactly as v13 does:
    all_inputs = [sample_auditor_wallet_utxo_base_ap3x, sample_claim_utxo]
    sorted_inputs = sorted(
        all_inputs,
        key=lambda u: (bytes(u.input.transaction_id).hex(), u.input.index),
    )
    seed = sorted_inputs[0]
    expected_name = sample_challenge_token_name_deriver(
        bytes(seed.input.transaction_id), seed.input.index
    )

    challenge_policy = ScriptHash(
        bytes.fromhex(sample_deployment_with_challenge_ref.challenge_hash)
    )
    mint_ma = builder.mint
    items = dict(mint_ma.data.items()) if hasattr(mint_ma, "data") else dict(mint_ma.items())
    challenge_assets = items[challenge_policy]
    an_items = (dict(challenge_assets.data.items())
                if hasattr(challenge_assets, "data")
                else dict(challenge_assets.items()))
    actual_names = [bytes(an) for an in an_items.keys()]
    assert expected_name in actual_names, (
        f"Challenge NFT token name must be derive_token_name(b'chl_', "
        f"sorted_inputs[0].txid, sorted_inputs[0].idx) = "
        f"{expected_name.hex()}; got {[n.hex() for n in actual_names]}"
    )


def test_mint_redeemer_is_OpenChallenge_Constr0(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """Mint redeemer for the challenge policy is CBORTag(121, []) —
    Constr0 of the ChallengeAction enum (OpenChallenge).  v13
    reference: deploy_and_run_v13.py:938.
    """
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    reds = _collect_mint_redeemers(builder)
    assert reds, "No mint redeemers found on builder — challenge NFT mint is missing."
    assert len(reds) == 1, (
        f"Expected exactly one mint redeemer (the challenge NFT mint); "
        f"got {len(reds)}"
    )
    red = reds[0]
    raw = red.data.cbor if hasattr(red.data, "cbor") else bytes(red.data)
    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag") and decoded.tag == 121, (
        f"Mint redeemer must be CBORTag(121, []) = Constr0 OpenChallenge; "
        f"got {decoded!r}"
    )
    assert decoded.value == [], (
        f"OpenChallenge redeemer payload must be []; got {decoded.value!r}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Atomic claim-state transition: spend with MarkChallenged, continue Challenged
# ═════════════════════════════════════════════════════════════════════════

def test_claim_spent_with_MarkChallenged_redeemer(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """The spent claim input's redeemer must be CBORTag(123, []) =
    Constr2 of ClaimAction = MarkChallenged.  v13 reference:
    deploy_and_run_v13.py:939.
    """
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    red = _collect_spend_redeemers_for_input(builder, sample_claim_utxo)
    assert red is not None, (
        f"No spend redeemer found for claim UTxO "
        f"{bytes(sample_claim_utxo.input.transaction_id).hex()}#"
        f"{sample_claim_utxo.input.index}. The claim MUST be spent by "
        f"this TX (atomic state transition)."
    )
    raw = red.data.cbor if hasattr(red.data, "cbor") else bytes(red.data)
    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag") and decoded.tag == 123, (
        f"Claim spend redeemer must be CBORTag(123, []) = Constr2 "
        f"MarkChallenged; got {decoded!r}"
    )
    assert decoded.value == [], (
        f"MarkChallenged redeemer payload must be []; got {decoded.value!r}"
    )


def test_claim_continuing_output_state_is_Challenged(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """The continuing claim output's datum must have state field (index 8)
    equal to Constr1 = CBORTag(122, []) = Challenged.  All other fields
    must be preserved from the original claim datum.
    """
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    cont_out = _continuing_claim_output(builder)
    fields = _decode_datum(cont_out).value
    assert len(fields) == 9, (
        f"Continuing claim datum must still have 9 fields (only state "
        f"changes); got {len(fields)}"
    )
    state = fields[8]
    assert hasattr(state, "tag") and state.tag == 122, (
        f"Continuing claim state must be CBORTag(122, []) = Constr1 "
        f"Challenged; got {state!r}"
    )
    assert state.value == [], (
        f"Challenged state payload must be []; got {state.value!r}"
    )

    # Other fields unchanged vs. the original claim datum:
    orig_fields = _decode_datum(sample_claim_utxo.output).value
    for i in range(8):
        assert fields[i] == orig_fields[i], (
            f"Continuing claim datum field[{i}] must be unchanged; "
            f"orig={orig_fields[i]!r}, got={fields[i]!r}"
        )


def test_claim_continuing_output_preserves_value(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """Value on the continuing claim output must equal the original
    claim UTxO's value — claim NFT + claim stake are carried forward
    intact.  v13 reference: deploy_and_run_v13.py:959-960."""
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    cont_out = _continuing_claim_output(builder)
    orig_val = sample_claim_utxo.output.amount
    new_val = cont_out.amount
    assert new_val.coin == orig_val.coin, (
        f"Continuing claim output coin must equal original "
        f"({orig_val.coin}); got {new_val.coin}"
    )
    # multi_asset must contain the same claim NFT (same policy + asset name + qty).
    orig_ma = orig_val.multi_asset
    new_ma = new_val.multi_asset
    orig_items = (dict(orig_ma.data.items()) if hasattr(orig_ma, "data")
                  else dict(orig_ma.items()))
    new_items = (dict(new_ma.data.items()) if hasattr(new_ma, "data")
                 else dict(new_ma.items()))
    assert set(new_items.keys()) == set(orig_items.keys()), (
        f"Continuing claim multi_asset policies must match: "
        f"orig={list(orig_items.keys())}, got={list(new_items.keys())}"
    )
    for policy in orig_items:
        orig_assets = (dict(orig_items[policy].data.items())
                       if hasattr(orig_items[policy], "data")
                       else dict(orig_items[policy].items()))
        new_assets = (dict(new_items[policy].data.items())
                      if hasattr(new_items[policy], "data")
                      else dict(new_items[policy].items()))
        assert {bytes(k): v for k, v in orig_assets.items()} == \
               {bytes(k): v for k, v in new_assets.items()}, (
            f"Assets under policy {policy} must be preserved on the "
            f"continuing claim output."
        )


# ═════════════════════════════════════════════════════════════════════════
# Cross-validator references + signers + validity window
# ═════════════════════════════════════════════════════════════════════════

def test_reference_inputs_include_cross_refs_params_and_both_registry_dids(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    sample_cross_refs_utxo, sample_params_utxo,
    sample_registry_did_utxo, sample_auditor_registry_did_utxo,
):
    """reference_inputs must include: cross_refs_utxo, params_utxo,
    claimer's registry DID UTxO, auditor's registry DID UTxO.

    The challenge validator reads the claimer's DID from the spent
    claim datum AND the auditor's DID for auth via verify_active_did.
    v13 reference: deploy_and_run_v13.py:949-952.
    """
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    seen = set()
    for r in builder.reference_inputs:
        if hasattr(r, "input"):
            seen.add((bytes(r.input.transaction_id), r.input.index))
        else:
            seen.add((bytes(r.transaction_id), r.index))
    for name, u in [
        ("cross_refs", sample_cross_refs_utxo),
        ("params", sample_params_utxo),
        ("claimer_registry_did", sample_registry_did_utxo),
        ("auditor_registry_did", sample_auditor_registry_did_utxo),
    ]:
        key = (bytes(u.input.transaction_id), u.input.index)
        assert key in seen, (
            f"reference_inputs must contain {name} UTxO "
            f"{u.input.transaction_id.payload.hex()}#{u.input.index}. "
            f"Seen: {[(k[0].hex(), k[1]) for k in seen]}"
        )


def test_required_signer_is_auditor_vkh(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    _, vkey, _ = sample_auditor_wallet
    required = builder.required_signers or []
    hashes = [bytes(h) for h in required]
    assert hashes == [bytes(vkey.hash())], (
        f"required_signers must be [auditor_vkey.hash()] = "
        f"[{bytes(vkey.hash()).hex()}]; got {[h.hex() for h in hashes]}"
    )


def test_validity_window_bounded_correctly(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    default_challenge_window_ms,
):
    """validity_start ~= current_slot - 60; ttl == min(current_slot+3600,
    window_end_slot - 1).

    The tx's upper validity bound must strictly precede the claim's
    challenge-window deadline; see `tx_ends_before` at
    `contracts/lib/adversarial_auditing/utils.ak:203`. The builder caps
    ttl at `window_end_slot - 1` whenever the claim's challenge window
    would close before `current_slot + 3600`.

    With the canonical fixture:
      submitted_at_ms = (SYSTEM_START_UNIX + CANNED_SLOT - 120) * 1000
      challenge_window_ms = 1_800_000 (30 min)
    so `window_end_slot = submitted_at_s + 1800 - SYSTEM_START_UNIX
    = CANNED_SLOT + 1680`, and the cap applies.

    Allow ±5-slot tolerance on validity_start as test_submit_claim does.
    """
    from simulation.config import SYSTEM_START_UNIX
    from simulation.tests.conftest import CANNED_SLOT

    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )
    assert builder.validity_start is not None, "validity_start must be set"
    assert abs(builder.validity_start - (CANNED_SLOT - 60)) <= 5, (
        f"validity_start must be ~current_slot-60 ({CANNED_SLOT - 60}); "
        f"got {builder.validity_start}"
    )

    # Compute expected ttl upper bound from the fixture's claim datum.
    submitted_at_ms = (SYSTEM_START_UNIX + CANNED_SLOT - 120) * 1000
    window_end_slot = (
        (submitted_at_ms + default_challenge_window_ms) // 1000
        - SYSTEM_START_UNIX
    )
    expected_ttl = min(CANNED_SLOT + 3600, window_end_slot - 1)

    assert builder.ttl is not None, "ttl must be set"
    assert builder.ttl == expected_ttl, (
        f"ttl must be min(current_slot+3600, window_end_slot-1) = "
        f"min({CANNED_SLOT + 3600}, {window_end_slot - 1}) = {expected_ttl}; "
        f"got {builder.ttl}"
    )


def test_ttl_caps_at_claim_challenge_window_deadline(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    default_challenge_window_ms,
):
    """ttl is capped at `window_end_slot - 1` when the claim's challenge
    window closes BEFORE `current_slot + 3600`.

    Why: the challenge validator enforces `tx_ends_before(submitted_at
    + challenge_window)` via `tx_ends_before` at
    `contracts/lib/adversarial_auditing/utils.ak:203`. A hardcoded
    `ttl = current_slot + 3600` would overshoot short windows and cause
    the TX to be rejected on-chain. Catherine's fix caps ttl at
    `window_end_slot - 1` in exactly this case.

    The canonical `sample_claim_utxo` already sets:
      submitted_at_ms = (SYSTEM_START_UNIX + CANNED_SLOT - 120) * 1000
      challenge_window_ms = 1_800_000 (30 min)
    So window_end_slot = CANNED_SLOT + 1680, which closes 1920 slots
    BEFORE current_slot + 3600 — exactly the scenario we want to assert.
    """
    from simulation.config import SYSTEM_START_UNIX
    from simulation.tests.conftest import CANNED_SLOT

    submitted_at_ms = (SYSTEM_START_UNIX + CANNED_SLOT - 120) * 1000
    window_end_slot = (
        (submitted_at_ms + default_challenge_window_ms) // 1000
        - SYSTEM_START_UNIX
    )
    # Sanity: the fixture actually triggers the cap (window ends first).
    assert window_end_slot - 1 < CANNED_SLOT + 3600, (
        "Precondition: fixture must set a challenge window that closes "
        "BEFORE current_slot+3600 to exercise the cap path. "
        f"window_end_slot-1={window_end_slot - 1}, "
        f"current_slot+3600={CANNED_SLOT + 3600}."
    )

    _, builder, _ = _run_build_open_challenge(
        patched_network_for_challenge, captured_builder, mock_ogmios_context,
        sample_deployment_with_challenge_ref,
        sample_auditor_wallet, sample_auditor_did_hex,
        sample_claim_utxo, sample_eligible_jurors,
        default_stake_amount, default_resolution_deadline_ms, default_jury_size,
    )

    assert builder.ttl == window_end_slot - 1, (
        f"ttl must be capped at window_end_slot - 1 = {window_end_slot - 1}; "
        f"got {builder.ttl}. The old hardcoded current_slot+3600 = "
        f"{CANNED_SLOT + 3600} would violate within_window on-chain."
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guard rails — raise ValueError BEFORE building the TX
# ═════════════════════════════════════════════════════════════════════════

def test_no_self_audit_raises_when_auditor_equals_claimer_did(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """`no_self_audit` invariant — builder must FAIL-FAST with
    ValueError when the caller tries to audit their own claim.

    Why client-side: submitting a TX that will be rejected on-chain
    wastes collateral and makes runbook analysis noisy.  The v13 smoke
    assumed distinct DIDs; the simulation must not.  Catherine should
    validate `auditor_did_hex != claim.claimer_did` before constructing
    any datum.
    """
    from simulation.tx_builder import build_open_challenge

    skey, vkey, auditor_addr = sample_auditor_wallet
    claim_ref = (
        f"{bytes(sample_claim_utxo.input.transaction_id).hex()}"
        f"#{sample_claim_utxo.input.index}"
    )
    evidence_hash = hashlib.blake2b(b"evidence", digest_size=32).digest()

    with pytest.raises(ValueError, match=r"(?i)self[\- ]?audit|auditor.*claimer"):
        build_open_challenge(
            mock_ogmios_context,
            sample_deployment_with_challenge_ref,
            skey, vkey, auditor_addr,
            sample_did_hex,  # SAME as the claimer's DID
            claim_ref,
            sample_eligible_jurors,
            stake_amount=default_stake_amount,
            evidence_hash=evidence_hash,
            evidence_uri=b"ipfs://x",
            resolution_deadline_ms=default_resolution_deadline_ms,
            jury_size=default_jury_size,
            oracle_active=False,
        )


def test_pool_large_enough_raises_when_fewer_than_3x_jury_size(
    patched_network_for_challenge, captured_builder, mock_ogmios_context,
    sample_deployment_with_challenge_ref,
    sample_auditor_wallet, sample_auditor_did_hex,
    sample_claim_utxo, sample_eligible_jurors,
    default_stake_amount, default_resolution_deadline_ms, default_jury_size,
):
    """`pool_large_enough` invariant — jury mode requires
    len(eligible_jurors) >= 3 * jury_size.  Default jury_size=5 ⇒ need
    at least 15 DIDs in the pool.  Passing 14 must raise ValueError
    BEFORE the TX is built.
    """
    from simulation.tx_builder import build_open_challenge

    skey, vkey, auditor_addr = sample_auditor_wallet
    claim_ref = (
        f"{bytes(sample_claim_utxo.input.transaction_id).hex()}"
        f"#{sample_claim_utxo.input.index}"
    )
    evidence_hash = hashlib.blake2b(b"evidence", digest_size=32).digest()
    too_small = sample_eligible_jurors[:14]  # 14 < 3*5=15
    assert len(too_small) == 14, "Test setup: expected 14-DID undersized pool"

    with pytest.raises(ValueError, match=r"(?i)pool|eligible|jury.*size"):
        build_open_challenge(
            mock_ogmios_context,
            sample_deployment_with_challenge_ref,
            skey, vkey, auditor_addr,
            sample_auditor_did_hex,
            claim_ref,
            too_small,
            stake_amount=default_stake_amount,
            evidence_hash=evidence_hash,
            evidence_uri=b"ipfs://x",
            resolution_deadline_ms=default_resolution_deadline_ms,
            jury_size=default_jury_size,
            oracle_active=False,
        )
