"""
RED-phase tests for `simulation.tx_builder.build_reveal_vote`.

Function under test (NOT YET IMPLEMENTED):
    build_reveal_vote(
        context, deployment,
        skey, vkey, wallet_addr,
        juror_utxo_ref: str,          # "<txid>#<idx>" of the committed JurorDatum UTxO
        challenge_utxo_ref: str,      # "<txid>#<idx>" of the Voting ChallengeDatum UTxO
        verdict_byte: int,            # 0x00 = ClaimerWins, 0x01 = AuditorWins
        salt: bytes,                  # 32 B salt reconstructed from commit-time persistence
        *,
        commit_window_ms: int = 1_800_000,
        reveal_window_ms: int = 1_800_000,
    ) -> dict                          # includes {"tx_hash", "juror_utxo_ref", ...}

Contract reference — researched by reading the validator directly:

    contracts/lib/adversarial_auditing/types.ak
        JurorDatum fields (9):
            [0] juror_did          PolicyId (28 B)
            [1] juror_credential   Credential (CBORTag(121,[vkh]))
            [2] bond_amount        Int
            [3] cases_resolved     Int
            [4] majority_votes     Int
            [5] registered_at      Int (POSIX ms)
            [6] active_case        Option<ByteArray>
            [7] vote_commitment    Option<ByteArray>
                INPUT  = Some(commitment_hash) (the stored blake2b)
                OUTPUT = None                   ← CLEARED on reveal
            [8] revealed_verdict   Option<Verdict>
                INPUT  = None
                OUTPUT = Some(verdict_cbor)     ← verdict is the Verdict ENUM,
                                                  NOT the Int verdict_byte

        Verdict enum (Constr index = CBOR tag - 121):
            ClaimerWins  -> CBORTag(121, [])   ↔ verdict_byte 0x00
            AuditorWins  -> CBORTag(122, [])   ↔ verdict_byte 0x01
            Inconclusive -> CBORTag(123, [])   ↔ verdict_byte 0x02

        JuryAction enum order (Constr index = CBOR tag - 121):
            RegisterJuror          Constr0 -> CBORTag(121, [])
            SelectJury             Constr1 -> CBORTag(122, [...])
            CommitVote             Constr2 -> CBORTag(123, [...])
            RevealVote             Constr3 -> CBORTag(124, [...])   ← THIS

        >>> NOTE for Catherine: task brief stated "RevealVote stores
        >>> verdict_byte: Int directly in the redeemer and revealed_verdict
        >>> holds Some(verdict_byte)" — that is INCORRECT. The redeemer's
        >>> 3rd field is a `Verdict` enum CONSTRUCTOR (CBORTag 121/122/123
        >>> with empty payload), not an Int. Same for the datum field[8]:
        >>> Some(Verdict) = CBORTag(121, [CBORTag(121, [])]) for ClaimerWins,
        >>> NOT CBORTag(121, [0x00]).
        >>>
        >>> Also: brief said field[7] vote_commitment "stays Some(commitment)"
        >>> — that is INCORRECT. Per jury_pool.ak:618 the validator REQUIRES
        >>> updated.vote_commitment == None, i.e. the commitment MUST be
        >>> cleared on the continuing output. v13 line 1382 confirms:
        >>>   fields[7] = cbor2.CBORTag(122, [])   # Option None
        >>>   fields[8] = cbor2.CBORTag(121, [verdict_cbor])   # Some(Verdict)

    contracts/validators/jury_pool.ak :: validate_reveal_vote
    (lines 542-634) — full invariant list:

        1. `expect Some(active_token_name) = juror.active_case` (line 553)
           — juror MUST be assigned to a challenge.
        2. `expect Some(expected_hash) = juror.vote_commitment` (line 556)
           — juror MUST have a stored commitment.
        3. `hash_matches` (lines 559-563) — blake2b_256(
                serialize_verdict_index(verdict) || salt
            ) == expected_hash. The on-chain binding check.
        4. `signed` (line 566) — tx signed by juror's vkh.
        5. `challenge_ok` (lines 569-600) — some reference input at
           refs.challenge_validator_hash carries a token with AssetName
           == active_token_name (qty=1), has InlineDatum decoding to
           ChallengeDatum whose state is Voting, AND the TX's validity
           window satisfies:
             `tx_started_after(tx, challenged_at + commit_window)` AND
             `tx_ends_before(tx, challenged_at + commit_window + reveal_window)`
           i.e. validity_start > commit_deadline AND ttl < reveal_deadline.
        6. `output_ok` (lines 603-626) — some continuing output at
           refs.jury_pool_hash with InlineDatum where:
             - fields 0..6 are byte-identical to the input juror datum,
             - vote_commitment == None           (CLEARED),
             - revealed_verdict == Some(verdict) (Verdict enum),
             - assets.lovelace_of(out.value) == juror.bond_amount.

Reference implementation we mirror:
    /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
    lines 1327-1424 (step6b_reveal_votes). Our build_reveal_vote is ONE
    ITERATION of that for-loop: reveals a single juror's vote.

    Key v13 cues (authoritative — successful on-chain reveals):
        L1357  challenge_ref_cbor = cbor2.CBORTag(121, [txid_bytes, idx])
        L1358-1362
               verdict_cbor_map = {
                   0x00: cbor2.CBORTag(121, []),  # ClaimerWins
                   0x01: cbor2.CBORTag(122, []),  # AuditorWins
                   0x02: cbor2.CBORTag(123, []),  # Inconclusive
               }
        L1377  reveal_redeemer_cbor = cbor2.CBORTag(
                   124, [challenge_ref_cbor, verdict_cbor, salt])
        L1382  fields[7] = cbor2.CBORTag(122, [])                # None
        L1383  fields[8] = cbor2.CBORTag(121, [verdict_cbor])    # Some(Verdict)
        L1398  validity_start = max(current_slot - 30, commit_deadline_slot + 1)
        L1399  ttl            = min(current_slot + 120, reveal_deadline_slot - 1)
        L1400-1401
               if ttl <= validity_start: raise "reveal deadline passed"

────────────────────────────────────────────────────────────────────────
CRITICAL BINDING INVARIANT — commit↔reveal salt/verdict round-trip
────────────────────────────────────────────────────────────────────────
The reveal operation is the OTHER HALF of the commit-reveal scheme. The
commitment stored on-chain was computed (by build_commit_vote) as:
    commitment = blake2b_256(bytes([verdict_byte]) || salt)

For the reveal to succeed on-chain, the (verdict_byte, salt) passed to
build_reveal_vote MUST recompute to the SAME commitment. Any mismatch
is permanently un-revealable. Our client-side guard verifies this
equality BEFORE submitting — better to raise ValueError locally than
burn fees on a doomed script validation.

All tests in this file MUST fail against the current tree because
`build_reveal_vote` does NOT yet exist in `tx_builder.py`. The RED
signal is `ImportError` on the function; Catherine implements the
function to turn the suite GREEN.
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
# Helpers (local to this test file)
# ═════════════════════════════════════════════════════════════════════════


def _jury_pool_output(builder: TransactionBuilder):
    """Return the single continuing output at jury_pool script addr."""
    from simulation.tests.conftest import V13_DEPLOYMENT
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    hits = [o for o in builder.outputs if o.address == jury_addr]
    assert len(hits) == 1, (
        f"Expected exactly ONE continuing output at jury_pool script addr "
        f"{jury_addr}; got {len(hits)}. All outputs: "
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


def _collect_spend_redeemer_for_input(builder: TransactionBuilder, utxo):
    """Return the spend Redeemer attached to the given juror UTxO."""
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


def _extract_redeemer_cbor(redeemer):
    """Decode the CBORTag payload of a PyCardano Redeemer.

    Accommodates the same variance we handle in test_commit_vote.py —
    PyCardano has shifted how Redeemer.data is represented across
    versions (RawCBOR, PlutusData, raw bytes, object with to_cbor()).
    """
    data = getattr(redeemer, "data", None)
    if data is None:
        data = redeemer
    raw = getattr(data, "cbor", None)
    if raw is None:
        raw = bytes(data) if hasattr(data, "__bytes__") else None
    if raw is None:
        raw_try = getattr(data, "to_cbor", None)
        if callable(raw_try):
            raw = raw_try()
    assert raw is not None, (
        f"Could not extract CBOR bytes from redeemer {redeemer!r}"
    )
    return cbor2.loads(raw)


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


# Mapping verdict_byte (juror-facing Int) → Verdict CBOR constructor
# (on-chain representation). Mirrors v13 line 1358-1362.
_VERDICT_CBOR = {
    0x00: (121, "ClaimerWins"),
    0x01: (122, "AuditorWins"),
    0x02: (123, "Inconclusive"),
}


def _run_build_reveal(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    verdict_byte: int,
    salt: bytes,
    default_commit_window_ms: int,
    default_reveal_window_ms: int,
    *,
    juror_utxo_override=None,
    challenge_utxo_override=None,
):
    """Invoke build_reveal_vote with canonical fixture inputs.

    `juror_utxo_override` / `challenge_utxo_override` let individual
    tests swap in a variant UTxO (e.g. already-revealed fixture) while
    keeping the rest of the wiring canonical.
    """
    from simulation.tx_builder import build_reveal_vote

    skey, vkey, wallet_addr = sample_juror_wallet

    juror_utxo = juror_utxo_override or sample_juror_utxo_with_known_commitment
    challenge_utxo = challenge_utxo_override or sample_challenge_utxo_voting_for_reveal

    if juror_utxo_override is not None or challenge_utxo_override is not None:
        import simulation.tx_builder as tx_mod
        ju_txid_hex = bytes(juror_utxo.input.transaction_id).hex()
        ch_txid_hex = bytes(challenge_utxo.input.transaction_id).hex()

        def _dispatch(txid_hex, idx, _ju=juror_utxo, _ch=challenge_utxo,
                      _jutxid=ju_txid_hex, _chtxid=ch_txid_hex):
            if txid_hex == _jutxid:
                return _ju
            if txid_hex == _chtxid:
                return _ch
            raise AssertionError(f"unexpected resolve_utxo({txid_hex}#{idx})")

        tx_mod.resolve_utxo = _dispatch

    result = build_reveal_vote(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        _utxo_ref_str(juror_utxo),
        _utxo_ref_str(challenge_utxo),
        verdict_byte,
        salt,
        commit_window_ms=default_commit_window_ms,
        reveal_window_ms=default_reveal_window_ms,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_reveal_vote did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# Importability / signature / return shape
# ═════════════════════════════════════════════════════════════════════════


def test_build_reveal_vote_exists_and_importable():
    """build_reveal_vote MUST be importable from simulation.tx_builder.
    This is the canonical RED failure mode — ImportError until Catherine
    implements the function.
    """
    from simulation.tx_builder import build_reveal_vote  # noqa: F401
    assert callable(build_reveal_vote), (
        "build_reveal_vote must be a callable (function)."
    )


def test_return_dict_includes_tx_hash_and_continuing_ref(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Result MUST expose at minimum:
      - 'tx_hash': str — for wait_confirm / logging
      - 'juror_utxo_ref': '<tx_hash>#0' — for downstream DistributeRewards
      - 'verdict_byte': int — for correlating persistence records
    """
    result, _ = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    assert "tx_hash" in result, "result must include 'tx_hash'"
    assert isinstance(result["tx_hash"], str) and len(result["tx_hash"]) > 0, (
        f"tx_hash must be a non-empty string; got {result['tx_hash']!r}"
    )
    assert "juror_utxo_ref" in result, "result must include 'juror_utxo_ref'"
    assert "#" in result["juror_utxo_ref"], (
        f"juror_utxo_ref must be 'txid#idx'; got {result['juror_utxo_ref']!r}"
    )
    assert "verdict_byte" in result, "result must include 'verdict_byte'"
    assert result["verdict_byte"] == sample_known_commit_salt_pair["verdict_byte"]


# ═════════════════════════════════════════════════════════════════════════
# Redeemer structure — RevealVote = Constr3 = CBORTag(124, [...])
# ═════════════════════════════════════════════════════════════════════════


def test_redeemer_is_RevealVote_Constr3_tag_124(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Spend redeemer's outer CBOR tag MUST be 124 (Constr3 of JuryAction).
    Confirmed both by the enum order in types.ak and by v13 step6b
    (deploy_and_run_v13.py:1377).
    """
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    red = _collect_spend_redeemer_for_input(
        builder, sample_juror_utxo_with_known_commitment
    )
    assert red is not None, (
        "No spend redeemer attached to the juror UTxO. "
        "build_reveal_vote must call add_script_input(juror_utxo, "
        "jury_pool_ref, redeemer=...)."
    )
    decoded = _extract_redeemer_cbor(red)
    assert hasattr(decoded, "tag") and decoded.tag == 124, (
        f"RevealVote redeemer outer tag MUST be 124 (Constr3). "
        f"Got CBORTag({getattr(decoded, 'tag', None)}, ...)."
    )


def test_redeemer_carries_challenge_ref_verdict_byte_and_salt(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """RevealVote payload MUST be a 3-tuple:
        [0] challenge_ref: CBORTag(121, [txid_bytes, idx])
        [1] verdict:       Verdict enum = CBORTag(121|122|123, [])
                           ^ NOT an Int — task brief was wrong.
        [2] salt:          ByteArray (exactly 32 bytes)
    Verdict tag is derived from verdict_byte via:
        0x00 -> 121 (ClaimerWins)
        0x01 -> 122 (AuditorWins)
        0x02 -> 123 (Inconclusive)
    """
    verdict_byte = sample_known_commit_salt_pair["verdict_byte"]
    salt = sample_known_commit_salt_pair["salt"]
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        verdict_byte,
        salt,
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    red = _collect_spend_redeemer_for_input(
        builder, sample_juror_utxo_with_known_commitment
    )
    decoded = _extract_redeemer_cbor(red)
    payload = decoded.value
    assert isinstance(payload, list) and len(payload) == 3, (
        f"RevealVote payload must be [challenge_ref, verdict, salt]; "
        f"got {payload!r}"
    )
    challenge_ref_field, verdict_field, salt_field = payload

    # Challenge ref
    assert hasattr(challenge_ref_field, "tag") and challenge_ref_field.tag == 121, (
        f"challenge_ref must be CBORTag(121, [txid, idx]); "
        f"got {challenge_ref_field!r}"
    )
    assert len(challenge_ref_field.value) == 2
    txid_bytes, idx_val = challenge_ref_field.value
    assert bytes(txid_bytes) == bytes(
        sample_challenge_utxo_voting_for_reveal.input.transaction_id
    ), "challenge_ref.txid must point at the challenge UTxO"
    assert idx_val == sample_challenge_utxo_voting_for_reveal.input.index

    # Verdict (ENUM, not Int)
    expected_tag, _name = _VERDICT_CBOR[verdict_byte]
    assert hasattr(verdict_field, "tag"), (
        f"verdict redeemer field MUST be a Verdict enum constructor "
        f"(CBORTag), NOT a raw int. Got {verdict_field!r} (type "
        f"{type(verdict_field).__name__}). Task brief claim that verdict "
        f"is stored as Int is INCORRECT — see v13 line 1375."
    )
    assert verdict_field.tag == expected_tag, (
        f"verdict_byte {verdict_byte:#x} must CBOR-encode as "
        f"CBORTag({expected_tag}, []); got CBORTag({verdict_field.tag}, ...)"
    )
    assert verdict_field.value == [], (
        f"Verdict constructors have no fields — payload must be []; "
        f"got {verdict_field.value!r}"
    )

    # Salt
    assert isinstance(salt_field, (bytes, bytearray)), (
        f"salt must be ByteArray; got {type(salt_field).__name__}"
    )
    assert bytes(salt_field) == salt, (
        f"salt in redeemer must equal caller-supplied salt verbatim. "
        f"Expected {salt.hex()}; got {bytes(salt_field).hex()}"
    )
    assert len(salt_field) == 32, (
        f"salt length mismatch — must be 32 B (matches blake2b_256 input "
        f"convention); got {len(salt_field)}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Datum correctness (continuing output)
# ═════════════════════════════════════════════════════════════════════════


def test_juror_datum_field_7_cleared_to_None(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Continuing datum field[7] (vote_commitment) MUST be cleared to
    None = CBORTag(122, []). Validator line 618:
        updated.vote_commitment == None.

    >>> NOTE for Catherine: task brief said "field[7] stays Some(commitment)"
    >>> — that is WRONG. On reveal, the commitment is CLEARED. Confirmed
    >>> by v13 line 1382: fields[7] = cbor2.CBORTag(122, []).
    """
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    out_fields = _decode_datum(_jury_pool_output(builder)).value
    assert len(out_fields) == 9, (
        f"JurorDatum must have 9 fields; got {len(out_fields)}"
    )
    field7 = out_fields[7]
    assert hasattr(field7, "tag"), f"field[7] must be a CBORTag; got {field7!r}"
    assert field7.tag == 122, (
        f"field[7] vote_commitment must be CLEARED to None = "
        f"CBORTag(122, []) on reveal; got CBORTag({field7.tag}, {field7.value!r})"
    )
    assert field7.value == [], (
        f"None payload must be empty list; got {field7.value!r}"
    )


def test_juror_datum_field_8_updated_to_Some_verdict(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Continuing datum field[8] (revealed_verdict) MUST be
    Some(verdict) where verdict is the Verdict ENUM constructor (NOT
    the Int verdict_byte). Validator line 619:
        updated.revealed_verdict == Some(verdict).

    Layout:
        Some(ClaimerWins) = CBORTag(121, [CBORTag(121, [])])
        Some(AuditorWins) = CBORTag(121, [CBORTag(122, [])])

    >>> NOTE for Catherine: task brief said revealed_verdict = Some(verdict_byte)
    >>> where verdict_byte: Int — that is INCORRECT. The inner payload is
    >>> a Verdict CONSTRUCTOR, not an int. v13 line 1383 confirms:
    >>>   fields[8] = cbor2.CBORTag(121, [verdict_cbor])
    >>> where verdict_cbor is CBORTag(121|122|123, []).
    """
    verdict_byte = sample_known_commit_salt_pair["verdict_byte"]
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        verdict_byte,
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    out_fields = _decode_datum(_jury_pool_output(builder)).value
    field8 = out_fields[8]
    assert hasattr(field8, "tag"), f"field[8] must be a CBORTag; got {field8!r}"
    assert field8.tag == 121, (
        f"field[8] revealed_verdict must be Some(_) = CBORTag(121, [_]); "
        f"got CBORTag({field8.tag}, {field8.value!r})"
    )
    assert isinstance(field8.value, list) and len(field8.value) == 1, (
        f"Some payload must be a single-element list; got {field8.value!r}"
    )
    inner = field8.value[0]
    assert hasattr(inner, "tag"), (
        f"inner value of revealed_verdict must be a Verdict ENUM "
        f"(CBORTag 121/122/123 with empty payload), NOT a raw Int. "
        f"Got {inner!r} (type {type(inner).__name__})."
    )
    expected_tag, name = _VERDICT_CBOR[verdict_byte]
    assert inner.tag == expected_tag, (
        f"verdict_byte {verdict_byte:#x} ({name}) must CBOR-encode as "
        f"CBORTag({expected_tag}, []); got CBORTag({inner.tag}, ...)"
    )
    assert inner.value == [], (
        f"Verdict constructors have no fields; got {inner.value!r}"
    )


def test_juror_datum_fields_0_through_6_preserved(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Fields [0] juror_did, [1] juror_credential, [2] bond_amount,
    [3] cases_resolved, [4] majority_votes, [5] registered_at,
    [6] active_case MUST be byte-identical across the reveal.
    Validator lines 611-617 enforce pointwise equality.

    Note: This is STRICTER than commit-vote's preservation test, which
    allowed field[6] to change; reveal preserves field[6] too because
    cases_resolved only increments at DistributeRewards time.
    """
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    in_fields = _decode_datum(sample_juror_utxo_with_known_commitment.output).value
    out_fields = _decode_datum(_jury_pool_output(builder)).value

    for idx in (0, 1, 2, 3, 4, 5, 6):
        assert out_fields[idx] == in_fields[idx], (
            f"field[{idx}] must be preserved across RevealVote "
            f"(jury_pool.ak:611-617). Input: {in_fields[idx]!r}; "
            f"Output: {out_fields[idx]!r}"
        )


# ═════════════════════════════════════════════════════════════════════════
# Commit↔reveal binding — client-side pre-flight check
# ═════════════════════════════════════════════════════════════════════════


def test_reveal_rejects_wrong_salt(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Client guard: if blake2b(bytes([verdict_byte]) || salt) does NOT
    match the stored commitment in field[7], the builder MUST refuse
    BEFORE submitting — the TX would fail the on-chain hash_matches
    check (jury_pool.ak:559-563) and burn fees for nothing.
    """
    wrong_salt = hashlib.blake2b(b"WRONG-SALT-bytes", digest_size=32).digest()
    # Sanity: wrong_salt MUST be different from the genuine salt, else
    # this test is vacuous (the fixture salt is `sample_commitment_salt`).
    assert wrong_salt != sample_known_commit_salt_pair["salt"]

    with pytest.raises(ValueError, match=r"(?i)commit|salt|binding"):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_with_known_commitment,
            sample_challenge_utxo_voting_for_reveal,
            sample_known_commit_salt_pair["verdict_byte"],
            wrong_salt,
            default_commit_window_ms,
            default_reveal_window_ms,
        )


def test_reveal_rejects_wrong_verdict_byte(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Client guard: if the caller passes the SAME salt but a DIFFERENT
    verdict_byte from the one that was committed, blake2b yields a
    different hash → commitment mismatch. Builder must fail fast.
    """
    genuine_byte = sample_known_commit_salt_pair["verdict_byte"]
    # Toggle between ClaimerWins and AuditorWins for the "wrong" verdict.
    wrong_byte = 0x01 if genuine_byte == 0x00 else 0x00
    assert wrong_byte != genuine_byte

    with pytest.raises(ValueError, match=r"(?i)commit|salt|binding"):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_with_known_commitment,
            sample_challenge_utxo_voting_for_reveal,
            wrong_byte,
            sample_known_commit_salt_pair["salt"],
            default_commit_window_ms,
            default_reveal_window_ms,
        )


# ═════════════════════════════════════════════════════════════════════════
# Value preservation on continuing output
# ═════════════════════════════════════════════════════════════════════════


def test_continuing_juror_output_preserves_bond_and_nft(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_juror_token_bytes,
    default_bond_amount,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Continuing juror UTxO MUST carry:
      - coin == bond_amount (validator line 620)
      - juror NFT (qty=1) under the jury_pool policy (preserved by
        convention — v13 line 1396 copies ju.output.amount verbatim)
      - no extra unexpected assets (reveal does NOT mint or burn)
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    out = _jury_pool_output(builder)
    value = out.amount

    assert value.coin == default_bond_amount, (
        f"Continuing output coin must equal bond_amount "
        f"({default_bond_amount}); got {value.coin}. "
        f"Validator: jury_pool.ak:620 — "
        f"assets.lovelace_of(out.value) == juror.bond_amount."
    )

    from pycardano import ScriptHash, AssetName
    jury_policy_hex = V13_DEPLOYMENT["hashes"]["jury_pool"]
    jury_policy = ScriptHash(bytes.fromhex(jury_policy_hex))
    ma = value.multi_asset
    assert jury_policy in ma, (
        f"Continuing output multi_asset must include jury_pool policy "
        f"{jury_policy_hex}; got policies {[p.payload.hex() for p in ma]}"
    )
    asset_map = ma[jury_policy]
    juror_an = AssetName(sample_juror_token_bytes)
    assert juror_an in asset_map, (
        f"juror NFT ({sample_juror_token_bytes.hex()}) missing from "
        f"continuing output."
    )
    assert asset_map[juror_an] == 1, (
        f"juror NFT qty must be 1; got {asset_map[juror_an]}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Builder topology — reference inputs, signers, timing
# ═════════════════════════════════════════════════════════════════════════


def test_reference_inputs_include_cross_refs_params_challenge(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_cross_refs_utxo,
    sample_params_utxo,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Reference inputs MUST include:
      - cross_refs UTxO (for refs.challenge_validator_hash + jury_pool_hash)
      - params UTxO (for params.commit_window + params.reveal_window)
      - challenge UTxO (for ch.state + ch.challenged_at)
    Validator lines 569-600 iterate tx.reference_inputs looking for the
    challenge UTxO.
    """
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    ref_input_keys = {
        (bytes(u.input.transaction_id), u.input.index)
        for u in builder.reference_inputs
    }

    def _key(utxo):
        return (bytes(utxo.input.transaction_id), utxo.input.index)

    assert _key(sample_cross_refs_utxo) in ref_input_keys, (
        f"cross_refs UTxO must be a reference input. Builder refs: "
        f"{[(tid.hex(), i) for tid, i in ref_input_keys]}"
    )
    assert _key(sample_params_utxo) in ref_input_keys, (
        f"params UTxO must be a reference input. Builder refs: "
        f"{[(tid.hex(), i) for tid, i in ref_input_keys]}"
    )
    assert _key(sample_challenge_utxo_voting_for_reveal) in ref_input_keys, (
        f"challenge (Voting) UTxO must be a reference input — validator "
        f"iterates tx.reference_inputs to check state/deadline "
        f"(jury_pool.ak:570-600). Builder refs: "
        f"{[(tid.hex(), i) for tid, i in ref_input_keys]}"
    )


def test_required_signer_is_juror_vkh(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """required_signers MUST include the juror's vkey hash.
    Validator line 566: credential_signed(tx, juror.juror_credential).
    """
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    _, juror_vkey, _ = sample_juror_wallet
    juror_vkh = juror_vkey.hash()
    signer_hashes = [bytes(s) for s in (builder.required_signers or [])]
    assert bytes(juror_vkh) in signer_hashes, (
        f"juror vkh ({bytes(juror_vkh).hex()}) must be in required_signers; "
        f"got {[h.hex() for h in signer_hashes]}"
    )


def test_validity_window_respects_commit_and_reveal_deadlines(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Validity window MUST be a SQUEEZE between the commit deadline
    (strictly after) and the reveal deadline (strictly before):

        validity_start > commit_deadline_slot      (after_commit)
        ttl            < reveal_deadline_slot      (before_reveal)

    Validator (jury_pool.ak:588-590):
        after_commit   = tx_started_after(tx, commit_deadline)
        before_reveal  = tx_ends_before(tx, reveal_deadline)
        after_commit && before_reveal

    v13 reference (lines 1398-1399):
        validity_start = max(current_slot - 30, commit_deadline_slot + 1)
        ttl            = min(current_slot + 120, reveal_deadline_slot - 1)
    """
    from simulation.config import SYSTEM_START_UNIX
    _, builder = _run_build_reveal(
        patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_with_known_commitment,
        sample_challenge_utxo_voting_for_reveal,
        sample_known_commit_salt_pair["verdict_byte"],
        sample_known_commit_salt_pair["salt"],
        default_commit_window_ms,
        default_reveal_window_ms,
    )
    chal_fields = _decode_datum(sample_challenge_utxo_voting_for_reveal.output).value
    challenged_at_ms = chal_fields[6]
    commit_deadline_slot = (
        (challenged_at_ms + default_commit_window_ms) // 1000 - SYSTEM_START_UNIX
    )
    reveal_deadline_slot = (
        (challenged_at_ms + default_commit_window_ms + default_reveal_window_ms) // 1000
        - SYSTEM_START_UNIX
    )

    assert builder.validity_start is not None, "builder.validity_start must be set"
    assert builder.ttl is not None, "builder.ttl must be set"
    assert builder.validity_start > commit_deadline_slot, (
        f"builder.validity_start ({builder.validity_start}) must be "
        f"strictly GREATER than commit_deadline_slot "
        f"({commit_deadline_slot}) so the on-chain tx_started_after check "
        f"passes (jury_pool.ak:588)."
    )
    assert builder.ttl < reveal_deadline_slot, (
        f"builder.ttl ({builder.ttl}) must be strictly LESS than "
        f"reveal_deadline_slot ({reveal_deadline_slot}) so the on-chain "
        f"tx_ends_before check passes (jury_pool.ak:589)."
    )
    assert builder.validity_start < builder.ttl, (
        f"builder.validity_start ({builder.validity_start}) must be "
        f"strictly less than builder.ttl ({builder.ttl}) — otherwise the "
        f"TX has no valid execution window."
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards (fail-fast BEFORE builder work)
# ═════════════════════════════════════════════════════════════════════════


def test_raises_if_juror_not_committed(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_not_committed_for_reveal,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Client-side guard: if juror.vote_commitment is None (never
    committed), the validator rejects via
    `expect Some(expected_hash) = juror.vote_commitment` (line 556).
    Client must refuse fast.
    """
    with pytest.raises(ValueError, match=r"(?i)not.*commit|commit.*missing"):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_not_committed_for_reveal,
            sample_challenge_utxo_voting_for_reveal,
            sample_known_commit_salt_pair["verdict_byte"],
            sample_known_commit_salt_pair["salt"],
            default_commit_window_ms,
            default_reveal_window_ms,
            juror_utxo_override=sample_juror_utxo_not_committed_for_reveal,
        )


def test_raises_if_juror_already_revealed(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_already_revealed,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Client-side guard: if juror.revealed_verdict is Some(_) already,
    the validator's output_ok check fails (we cannot set revealed_verdict
    to a new value while the input is already Some). Client refuses.
    """
    with pytest.raises(ValueError, match=r"(?i)already.*reveal|double.*reveal"):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_already_revealed,
            sample_challenge_utxo_voting_for_reveal,
            sample_known_commit_salt_pair["verdict_byte"],
            sample_known_commit_salt_pair["salt"],
            default_commit_window_ms,
            default_reveal_window_ms,
            juror_utxo_override=sample_juror_utxo_already_revealed,
        )


def test_raises_if_active_case_mismatches_challenge(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_mismatched_challenge_committed,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Client-side guard: juror.active_case MUST match the challenge
    token being revealed against. If they differ, on-chain challenge_ok
    fails (no ref input carries a token matching active_case). Refuse.

    Uses `sample_juror_utxo_mismatched_challenge_committed` which has
    BOTH a stored commitment AND a mismatched active_case — so the
    guard being tested here is unambiguously the active_case check
    (not the not-committed check, which has its own test).
    """
    with pytest.raises(
        ValueError, match=r"(?i)active.?case|challenge.*token|mismatch"
    ):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_mismatched_challenge_committed,
            sample_challenge_utxo_voting_for_reveal,
            sample_known_commit_salt_pair["verdict_byte"],
            sample_known_commit_salt_pair["salt"],
            default_commit_window_ms,
            default_reveal_window_ms,
            juror_utxo_override=sample_juror_utxo_mismatched_challenge_committed,
        )


def test_raises_if_before_commit_deadline(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting,            # <-- USES regular challenge UTxO
    sample_known_commit_salt_pair,           #     where commit_deadline is
    default_commit_window_ms,                #     30 min in the FUTURE
    default_reveal_window_ms,
):
    """Client-side guard: if current_slot * 1000 is still BEFORE
    challenged_at + commit_window (commit window not yet closed), the
    reveal is PREMATURE. Validator fix CR-CR-01 (lines 586-588):
        after_commit = tx_started_after(tx, commit_deadline)
    is impossible to satisfy when now < commit_deadline.

    This uses `sample_challenge_utxo_voting` (not the _for_reveal
    variant) because that fixture has challenged_at only 60 s before
    "now" — meaning the commit deadline is still ~29 min in the
    FUTURE, which is exactly the condition this test exercises.
    """
    with pytest.raises(ValueError, match=r"(?i)commit.*window|too.*early|not.*yet"):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_with_known_commitment,
            sample_challenge_utxo_voting,        # commit deadline still open!
            sample_known_commit_salt_pair["verdict_byte"],
            sample_known_commit_salt_pair["salt"],
            default_commit_window_ms,
            default_reveal_window_ms,
            challenge_utxo_override=sample_challenge_utxo_voting,
        )


def test_raises_if_after_reveal_deadline(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
):
    """Client-side guard: if current_slot * 1000 is already past
    challenged_at + commit_window + reveal_window, the reveal is too
    LATE. Validator (line 589):
        before_reveal = tx_ends_before(tx, reveal_deadline)
    is impossible to satisfy when now > reveal_deadline.

    We simulate "already past reveal deadline" by passing a TINY
    reveal_window_ms = 1 ms (so reveal_deadline = commit_deadline + 1ms,
    which is in the past given our canned slot is well past
    commit_deadline).
    """
    tiny_reveal_window = 1  # 1 ms — reveal deadline already passed
    with pytest.raises(
        ValueError, match=r"(?i)reveal.*deadline|window.*closed|too.*late"
    ):
        _run_build_reveal(
            patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_with_known_commitment,
            sample_challenge_utxo_voting_for_reveal,
            sample_known_commit_salt_pair["verdict_byte"],
            sample_known_commit_salt_pair["salt"],
            default_commit_window_ms,
            tiny_reveal_window,
        )


def test_raises_if_verdict_byte_invalid(
    patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    sample_known_commit_salt_pair,
    default_commit_window_ms,
    default_reveal_window_ms,
):
    """Client-side guard: only verdict bytes {0x00, 0x01} are valid
    juror reveals. 0x02 (Inconclusive) is reserved for oracle fallback
    and a juror submitting it is a caller bug. Anything outside
    {0x00, 0x01} MUST raise BEFORE the commit-binding check (so we get
    a clear 'verdict byte' error rather than an opaque 'hash mismatch').

    Representative invalid values: 0x02, 0xFF, negative, over-byte-range.
    Note: the reveal's binding check with an out-of-range byte would
    anyway fail (no stored commitment matches 0x02||salt), but surfacing
    a verdict-range error gives the caller a clearer diagnostic.
    """
    for bad in (0x02, 0xFF, -1, 256):
        with pytest.raises(ValueError, match=r"(?i)verdict"):
            _run_build_reveal(
                patched_network_for_reveal_vote, captured_builder, mock_ogmios_context,
                sample_deployment_with_jury_pool_ref,
                sample_juror_wallet,
                sample_juror_utxo_with_known_commitment,
                sample_challenge_utxo_voting_for_reveal,
                bad,
                sample_known_commit_salt_pair["salt"],
                default_commit_window_ms,
                default_reveal_window_ms,
            )
