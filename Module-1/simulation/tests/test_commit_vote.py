"""
RED-phase tests for `simulation.tx_builder.build_commit_vote`.

Function under test (NOT YET IMPLEMENTED):
    build_commit_vote(
        context, deployment,
        skey, vkey, wallet_addr,
        juror_utxo_ref: str,         # "<txid>#<idx>" of the JurorDatum UTxO
        challenge_utxo_ref: str,     # "<txid>#<idx>" of the Voting ChallengeDatum UTxO
        verdict_byte: int,           # 0x00 = ClaimerWins, 0x01 = AuditorWins
        *,
        salt: bytes | None = None,   # when None, builder generates os.urandom(32)
        commit_window_ms: int = 1_800_000,
    ) -> dict                         # MUST include {"salt": bytes} for caller
                                      # persistence (see v13 salt-loss incident)

Contract reference — researched by reading the validator directly:

    contracts/lib/adversarial_auditing/types.ak (lines 179-278)
        JurorDatum fields (9):
            [0] juror_did          PolicyId (28 B)
            [1] juror_credential   Credential (CBORTag(121,[vkh]))
            [2] bond_amount        Int
            [3] cases_resolved     Int
            [4] majority_votes     Int
            [5] registered_at      Int (POSIX ms)
            [6] active_case        Option<ByteArray>
                                    None     = CBORTag(122, [])
                                    Some(tn) = CBORTag(121, [tn])
            [7] vote_commitment    Option<ByteArray>
                                    None       = CBORTag(122, [])
                                    Some(hash) = CBORTag(121, [hash])
            [8] revealed_verdict   Option<Verdict>

        JuryAction enum order (Constr index = CBOR tag - 121):
            RegisterJuror          Constr0 -> CBORTag(121, [])
            SelectJury             Constr1 -> CBORTag(122, [...])
            CommitVote             Constr2 -> CBORTag(123, [challenge_ref, commitment_hash])
            RevealVote             Constr3 -> CBORTag(124, [...])
            DistributeRewards      Constr4 -> CBORTag(125, [...])
            WithdrawJuror          Constr5 -> CBORTag(126, [])
            ReceiveJuryFee         Constr6 -> CBORTag(127, [])
            SlashNonReveal         Constr7 -> CBORTag(128, [...])
            ResetStaleActiveCase   Constr8 -> CBORTag(129, [])

        >>> NOTE for Catherine: the task brief assigned CommitVote to
        >>> Constr4 / CBORTag 125; that is INCORRECT. The correct tag is
        >>> 123 (Constr2), confirmed both by the source enumeration in
        >>> types.ak and by the v13 testnet script's step6a_commit_votes
        >>> (deploy_and_run_v13.py:1264 — `cbor2.CBORTag(123, ...)`).

    contracts/validators/jury_pool.ak :: validate_commit_vote
    (lines 437-522) — full invariant list:

        1. `expect Some(active_token_name) = juror.active_case` — juror
           MUST be assigned to a challenge (line 447).
        2. `not_committed` — juror.vote_commitment MUST be None
           (lines 450-454).
        3. `hash_ok` — commitment_hash MUST be 32 bytes (line 457).
        4. `signed` — tx signed by juror.juror_credential's vkh
           (line 460).
        5. `challenge_ok` — some reference input at
           `refs.challenge_validator_hash` must:
             - carry a token named `active_token_name` (qty=1),
             - have an InlineDatum decoding to ChallengeDatum,
             - ch.state must be `Voting { .. }` (not PendingJury / etc.),
             - `tx_ends_before(tx, ch.challenged_at + params.commit_window)`
                i.e. TTL-as-ms strictly less than commit deadline.
           (lines 463-487)
        6. `output_ok` — some continuing output at `refs.jury_pool_hash`
           with InlineDatum where:
             - juror_did, juror_credential, bond_amount, cases_resolved,
               majority_votes, registered_at, active_case all equal to
               juror.* (fields 0-6 preserved),
             - vote_commitment == Some(commitment_hash),
             - revealed_verdict == None,
             - assets.lovelace_of(out.value) == juror.bond_amount.
           (lines 489-513)

    Commitment formula (jury_pool.ak:559-562, used by RevealVote):
        commitment = blake2b_256(serialize_verdict_index(verdict) || salt)
            ClaimerWins  -> #"00"
            AuditorWins  -> #"01"
            Inconclusive -> #"02"
        salt: caller-provided ByteArray.
    Build_commit_vote MUST produce `commitment = blake2b(bytes([verdict_byte]) + salt)`
    so that a subsequent RevealVote with (verdict, salt) satisfies the
    on-chain check.

Reference implementation we mirror:
    /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
    lines 1219-1320 (step6a_commit_votes — post-salt-loss-incident fix).
    Our build_commit_vote is ONE ITERATION of the v13 for-loop: it
    commits a single juror's vote.

────────────────────────────────────────────────────────────────────────
CRITICAL SESSION CONTEXT — salt persistence
────────────────────────────────────────────────────────────────────────
During v12 deploy, CommitVote salts were generated ephemerally via
`os.urandom(32)` and never persisted to disk. When commit 5/5 crashed
(collateral failure), the first 4 salts were lost, making those 4
votes PERMANENTLY UNREVEALABLE on-chain (blake2b is one-way; without
the salt we cannot reconstruct the original verdict for RevealVote).

The v13 script fixes this by writing the salt to a JSONL file BEFORE
moving on to the next iteration — see deploy_and_run_v13.py:1312-1316
(`with open(salt_log_path, "a") ... sf.flush(); os.fsync(sf.fileno())`).

Our sim-layer `build_commit_vote` MUST support salt persistence by
design. Two patterns — tests enforce BOTH so Catherine has flexibility:

  (A) Caller supplies `salt=<bytes>` kwarg — test-deterministic. The
      builder uses the provided salt verbatim. This is the preferred
      pattern in tests because it makes commitment equality assertable.

  (B) Caller passes `salt=None` — builder generates `os.urandom(32)`.

  In BOTH cases, the returned dict MUST include the salt so the caller
  can persist it immediately after build_commit_vote returns and
  BEFORE any subsequent builder call can cause orphan-salt risk.

All tests in this file MUST fail against the current tree because
`build_commit_vote` does NOT yet exist in `tx_builder.py`.  The RED
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
    """Return the spend Redeemer attached to the given juror UTxO.
    Mirrors the cross-version probing from test_transition_to_voting.py.
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


def _utxo_ref_str(utxo) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def _run_build_commit(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    verdict_byte: int,
    salt: bytes | None,
    default_commit_window_ms: int,
    *,
    juror_utxo_override=None,
    challenge_utxo_override=None,
):
    """Invoke build_commit_vote with canonical fixture inputs.

    `juror_utxo_override` / `challenge_utxo_override` let individual
    tests swap in a variant UTxO (e.g. the already-committed juror
    fixture) while keeping the rest of the wiring canonical.
    """
    from simulation.tx_builder import build_commit_vote

    skey, vkey, wallet_addr = sample_juror_wallet

    juror_utxo = juror_utxo_override or sample_juror_utxo_assigned
    challenge_utxo = challenge_utxo_override or sample_challenge_utxo_voting

    # Override resolve_utxo dispatch when the test supplies a variant.
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

        # Respect pytest fixture boundary: monkeypatch must be already
        # applied by patched_network_for_commit_vote. We override it
        # here via direct setattr — the `patched_network` fixture is
        # the one that set up the prior value.
        tx_mod.resolve_utxo = _dispatch

    result = build_commit_vote(
        mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        skey, vkey, wallet_addr,
        _utxo_ref_str(juror_utxo),
        _utxo_ref_str(challenge_utxo),
        verdict_byte,
        salt=salt,
        commit_window_ms=default_commit_window_ms,
    )
    assert captured_builder, (
        "No TransactionBuilder was passed to build_and_sign — "
        "build_commit_vote did not reach the build step."
    )
    return result, captured_builder[-1]


# ═════════════════════════════════════════════════════════════════════════
# Commitment correctness
# ═════════════════════════════════════════════════════════════════════════


def test_commitment_equals_blake2b_of_verdict_concat_salt(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """The commitment hash stored in the output datum and embedded in
    the redeemer MUST equal blake2b_256(bytes([verdict_byte]) || salt).

    Per jury_pool.ak:559-562 (RevealVote's verification):
        computed_hash == blake2b_256(serialize_verdict_index(verdict) || salt)
    Whatever build_commit_vote publishes now must match what a future
    RevealVote will recompute — otherwise the vote is orphaned.
    """
    result, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    expected_commitment = hashlib.blake2b(
        bytes([sample_verdict_byte]) + sample_commitment_salt,
        digest_size=32,
    ).digest()

    # Must appear in the result dict (for downstream persistence).
    assert "commitment" in result, (
        "build_commit_vote result dict must include 'commitment' so "
        "callers can log/replay it."
    )
    assert result["commitment"] == expected_commitment, (
        f"result['commitment'] must equal "
        f"blake2b_256(bytes([{sample_verdict_byte:#x}]) || salt). "
        f"Expected {expected_commitment.hex()}; got "
        f"{result['commitment'].hex() if isinstance(result['commitment'], (bytes, bytearray)) else result['commitment']}"
    )

    # And the same commitment must be in the continuing datum field[7].
    out_fields = _decode_datum(_jury_pool_output(builder)).value
    vote_commitment_field = out_fields[7]
    assert hasattr(vote_commitment_field, "tag") and vote_commitment_field.tag == 121, (
        f"field[7] vote_commitment must be Some(h) = CBORTag(121,[h]); "
        f"got {vote_commitment_field!r}"
    )
    stored_commitment = bytes(vote_commitment_field.value[0])
    assert stored_commitment == expected_commitment, (
        f"Continuing datum field[7] commitment mismatch. "
        f"Expected {expected_commitment.hex()}; got {stored_commitment.hex()}"
    )


def test_commit_vote_accepts_caller_provided_salt(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    default_commit_window_ms,
):
    """When the caller supplies `salt=<bytes>`, the builder MUST use it
    verbatim (not generate a fresh one). Tested by feeding a specific
    salt and asserting both that (a) result['salt'] == that salt, and
    (b) the commitment matches blake2b(verdict || that-salt).
    """
    caller_salt = hashlib.blake2b(b"caller-salt-xyz", digest_size=32).digest()
    result, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        caller_salt,
        default_commit_window_ms,
    )
    assert "salt" in result, (
        "build_commit_vote result must contain 'salt' key (persistence "
        "contract — see v13 salt-loss incident)."
    )
    assert bytes(result["salt"]) == caller_salt, (
        f"When caller provides salt, builder must use it verbatim. "
        f"Expected {caller_salt.hex()}; got "
        f"{bytes(result['salt']).hex() if isinstance(result['salt'], (bytes, bytearray)) else result['salt']}"
    )
    expected_commitment = hashlib.blake2b(
        bytes([sample_verdict_byte]) + caller_salt, digest_size=32,
    ).digest()
    assert bytes(result["commitment"]) == expected_commitment, (
        f"Commitment must be derived from the caller-supplied salt. "
        f"Expected {expected_commitment.hex()}; got "
        f"{bytes(result['commitment']).hex()}"
    )


def test_commit_vote_returns_salt_for_persistence_with_caller_salt(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """The returned dict MUST contain the salt (32 bytes) so the caller
    can persist it before a subsequent TX can orphan it. This is a
    behavioural fix for the v12 incident (see module docstring). The
    salt must be the EXACT bytes used in the commitment — not a copy
    derivation.
    """
    result, _ = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    assert "salt" in result, "result must expose 'salt'"
    assert isinstance(result["salt"], (bytes, bytearray)), (
        f"result['salt'] must be bytes-like; got {type(result['salt']).__name__}"
    )
    assert len(result["salt"]) == 32, (
        f"salt must be exactly 32 bytes (blake2b_256 input convention); "
        f"got {len(result['salt'])} bytes"
    )
    assert bytes(result["salt"]) == sample_commitment_salt


def test_commit_vote_generates_salt_when_absent_and_returns_it(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    default_commit_window_ms,
):
    """When salt=None (caller did not supply), the builder MUST generate
    a 32-byte salt AND return it. The commitment must be consistent
    with the generated salt. Two invocations with salt=None MUST NOT
    produce the same salt (i.e. actual randomness, not a constant).
    """
    verdict_byte = 0x00  # fixed so this test doesn't depend on parametrisation

    # First invocation with salt=None.
    result1, _ = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        verdict_byte,
        None,  # <-- no caller salt; builder must generate.
        default_commit_window_ms,
    )
    assert "salt" in result1 and result1["salt"] is not None, (
        "When salt=None, builder MUST still return a generated salt."
    )
    salt1 = bytes(result1["salt"])
    assert len(salt1) == 32, f"generated salt must be 32 bytes; got {len(salt1)}"

    # Commitment must match salt1.
    expected1 = hashlib.blake2b(bytes([verdict_byte]) + salt1, digest_size=32).digest()
    assert bytes(result1["commitment"]) == expected1, (
        "commitment must be consistent with the generated salt."
    )

    # Second invocation — distinct salt proves the generator is not a constant.
    captured_builder.clear()
    result2, _ = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        verdict_byte,
        None,
        default_commit_window_ms,
    )
    salt2 = bytes(result2["salt"])
    assert salt2 != salt1, (
        "Two calls with salt=None MUST produce different salts; got same "
        f"{salt1.hex()} — the builder is emitting a constant salt (critical bug)."
    )


# ═════════════════════════════════════════════════════════════════════════
# Datum correctness (continuing output)
# ═════════════════════════════════════════════════════════════════════════


def test_juror_datum_field_7_updated_to_Some_commitment(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Continuing datum field[7] (vote_commitment) MUST be
    Some(commitment) = CBORTag(121, [commitment_bytes]). Validator
    line 505: updated.vote_commitment == Some(commitment_hash).
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    out_fields = _decode_datum(_jury_pool_output(builder)).value
    assert len(out_fields) == 9, (
        f"JurorDatum must have 9 fields; got {len(out_fields)}"
    )
    field7 = out_fields[7]
    assert hasattr(field7, "tag"), f"field[7] must be a CBORTag; got {field7!r}"
    assert field7.tag == 121, (
        f"field[7] vote_commitment must be Some(...) = CBORTag(121, [h]); "
        f"got CBORTag({field7.tag}, {field7.value!r})"
    )
    assert isinstance(field7.value, list) and len(field7.value) == 1, (
        f"Some payload must be a single-element list; got {field7.value!r}"
    )
    stored = bytes(field7.value[0])
    assert len(stored) == 32, (
        f"commitment_hash must be 32 bytes (jury_pool.ak:457 is_valid_hash); "
        f"got {len(stored)} bytes"
    )


def test_juror_datum_field_8_stays_None(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Continuing datum field[8] (revealed_verdict) MUST stay None =
    CBORTag(122, []). Validator line 506: updated.revealed_verdict == None.
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    out_fields = _decode_datum(_jury_pool_output(builder)).value
    field8 = out_fields[8]
    assert hasattr(field8, "tag"), f"field[8] must be a CBORTag; got {field8!r}"
    assert field8.tag == 122, (
        f"field[8] revealed_verdict must remain None = CBORTag(122,[]); "
        f"got CBORTag({field8.tag}, {field8.value!r})"
    )
    assert field8.value == [], (
        f"None payload must be empty list; got {field8.value!r}"
    )


def test_juror_datum_field_6_active_case_preserved(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_challenge_token_bytes,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """field[6] (active_case) MUST remain Some(challenge_token_bytes)
    across the commit — CommitVote does NOT clear the assignment.
    Validator line 504: updated.active_case == juror.active_case.
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    out_fields = _decode_datum(_jury_pool_output(builder)).value
    field6 = out_fields[6]
    assert hasattr(field6, "tag") and field6.tag == 121, (
        f"field[6] active_case must remain Some(...) = CBORTag(121, [tn]); "
        f"got {field6!r}"
    )
    assert bytes(field6.value[0]) == bytes(sample_challenge_token_bytes), (
        f"active_case token name must be preserved byte-for-byte across "
        f"commit. Expected {bytes(sample_challenge_token_bytes).hex()}; "
        f"got {bytes(field6.value[0]).hex()}"
    )


def test_juror_datum_all_other_fields_preserved(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Fields [0] juror_did, [1] juror_credential, [2] bond_amount,
    [3] cases_resolved, [4] majority_votes, [5] registered_at MUST be
    byte-identical across the commit. Validator lines 498-503 enforce
    pointwise equality on these.
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    in_fields = _decode_datum(sample_juror_utxo_assigned.output).value
    out_fields = _decode_datum(_jury_pool_output(builder)).value

    for idx in (0, 1, 2, 3, 4, 5):
        assert out_fields[idx] == in_fields[idx], (
            f"field[{idx}] must be preserved across CommitVote "
            f"(jury_pool.ak:498-503). Input: {in_fields[idx]!r}; "
            f"Output: {out_fields[idx]!r}"
        )


# ═════════════════════════════════════════════════════════════════════════
# Redeemer structure
# ═════════════════════════════════════════════════════════════════════════


def test_redeemer_is_CommitVote_Constr2_with_challenge_ref_and_commitment(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Spend redeemer MUST be CBORTag(123, [CBORTag(121, [txid, idx]),
    commitment_bytes]).

    NOTE: Task brief said Constr4/tag 125 — that is wrong. Correct
    value per types.ak enum order is Constr2 / CBORTag 123, confirmed
    by v13 step6a line 1264.
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    red = _collect_spend_redeemer_for_input(builder, sample_juror_utxo_assigned)
    assert red is not None, (
        "No spend redeemer attached to the juror UTxO. "
        "build_commit_vote must call add_script_input(juror_utxo, "
        "jury_pool_ref, redeemer=...)."
    )

    # PyCardano Redeemer: decode its data via .data or .to_cbor()
    data = getattr(red, "data", None)
    if data is None:
        data = red
    # RawCBOR object?
    raw = getattr(data, "cbor", None)
    if raw is None:
        raw = bytes(data) if hasattr(data, "__bytes__") else None
    if raw is None:
        raw_try = getattr(data, "to_cbor", None)
        if callable(raw_try):
            raw = raw_try()
    assert raw is not None, (
        f"Could not extract CBOR bytes from redeemer {red!r}"
    )

    decoded = cbor2.loads(raw)
    assert hasattr(decoded, "tag") and decoded.tag == 123, (
        f"CommitVote redeemer outer tag MUST be 123 (Constr2). "
        f"Got CBORTag({getattr(decoded, 'tag', None)}, ...). "
        f"NOTE: task brief claimed 125 — that was incorrect."
    )
    assert isinstance(decoded.value, list) and len(decoded.value) == 2, (
        f"CommitVote payload must be [challenge_ref, commitment]; "
        f"got {decoded.value!r}"
    )

    challenge_ref_field, commitment_field = decoded.value
    assert hasattr(challenge_ref_field, "tag") and challenge_ref_field.tag == 121, (
        f"challenge_ref must be CBORTag(121, [txid, idx]); "
        f"got {challenge_ref_field!r}"
    )
    assert len(challenge_ref_field.value) == 2, (
        f"challenge_ref payload must be [txid, idx]; "
        f"got {challenge_ref_field.value!r}"
    )
    txid_bytes, idx_val = challenge_ref_field.value
    assert bytes(txid_bytes) == bytes(sample_challenge_utxo_voting.input.transaction_id), (
        f"challenge_ref.txid must point at the challenge UTxO "
        f"({bytes(sample_challenge_utxo_voting.input.transaction_id).hex()}); "
        f"got {bytes(txid_bytes).hex()}"
    )
    assert idx_val == sample_challenge_utxo_voting.input.index

    assert isinstance(commitment_field, (bytes, bytearray)), (
        f"commitment must be ByteArray; got {type(commitment_field).__name__}"
    )
    assert len(commitment_field) == 32, (
        f"commitment must be 32 bytes (blake2b_256); "
        f"got {len(commitment_field)}"
    )
    expected = hashlib.blake2b(
        bytes([sample_verdict_byte]) + sample_commitment_salt, digest_size=32,
    ).digest()
    assert bytes(commitment_field) == expected, (
        f"Redeemer commitment must match blake2b(verdict || salt). "
        f"Expected {expected.hex()}; got {bytes(commitment_field).hex()}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Value preservation on continuing output
# ═════════════════════════════════════════════════════════════════════════


def test_continuing_juror_output_preserves_bond_and_nft(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_juror_token_bytes,
    default_bond_amount,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Continuing juror UTxO MUST carry:
      - coin == bond_amount (validator line 507)
      - juror NFT (qty=1) under the jury_pool policy
      - no extra unexpected assets (commit does NOT mint or burn)
    """
    from simulation.tests.conftest import V13_DEPLOYMENT
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    out = _jury_pool_output(builder)
    value = out.amount

    assert value.coin == default_bond_amount, (
        f"Continuing output coin must equal bond_amount "
        f"({default_bond_amount}); got {value.coin}. "
        f"Validator: jury_pool.ak:507 — "
        f"assets.lovelace_of(out.value) == juror.bond_amount."
    )

    # Multi-asset: exactly one policy (jury_pool), one token (juror NFT), qty=1.
    from pycardano import ScriptHash
    jury_policy_hex = V13_DEPLOYMENT["hashes"]["jury_pool"]
    jury_policy = ScriptHash(bytes.fromhex(jury_policy_hex))
    ma = value.multi_asset
    assert jury_policy in ma, (
        f"Continuing output multi_asset must include jury_pool policy "
        f"{jury_policy_hex}; got policies {[p.payload.hex() for p in ma]}"
    )
    asset_map = ma[jury_policy]
    # Find our juror NFT
    from pycardano import AssetName
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


def test_reference_inputs_include_cross_refs_params_and_challenge(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_cross_refs_utxo,
    sample_params_utxo,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Reference inputs MUST include:
      - cross_refs UTxO (for refs.challenge_validator_hash + jury_pool_hash)
      - params UTxO (for params.commit_window)
      - challenge UTxO (for ch.state + ch.challenged_at)
    See jury_pool.ak:463-487 — validator iterates over tx.reference_inputs
    looking for the challenge UTxO.
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
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
    assert _key(sample_challenge_utxo_voting) in ref_input_keys, (
        f"challenge (Voting) UTxO must be a reference input — validator "
        f"iterates tx.reference_inputs to check state/deadline "
        f"(jury_pool.ak:464). Builder refs: "
        f"{[(tid.hex(), i) for tid, i in ref_input_keys]}"
    )


def test_required_signer_is_juror_vkh(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """required_signers MUST include the juror's vkey hash (NOT the
    claimer's and NOT the auditor's). Validator line 460:
    credential_signed(tx, juror.juror_credential).
    """
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    _, juror_vkey, _ = sample_juror_wallet
    juror_vkh = juror_vkey.hash()
    signer_hashes = [bytes(s) for s in (builder.required_signers or [])]
    assert bytes(juror_vkh) in signer_hashes, (
        f"juror vkh ({bytes(juror_vkh).hex()}) must be in required_signers; "
        f"got {[h.hex() for h in signer_hashes]}"
    )


def test_ttl_before_commit_deadline(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_verdict_byte,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """builder.ttl (upper bound on validity) MUST be strictly less than
    the commit deadline expressed as a slot. The validator uses
    `tx_ends_before(tx, challenged_at + commit_window)` which means
    the TX's upper validity bound (ttl, in slots) must correspond to
    a POSIX time STRICTLY LESS THAN the commit deadline.

    Formula:
        commit_deadline_slot = (challenged_at_ms + commit_window_ms) // 1000
                                - SYSTEM_START_UNIX
        builder.ttl < commit_deadline_slot
    """
    from simulation.config import SYSTEM_START_UNIX
    _, builder = _run_build_commit(
        patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
        sample_deployment_with_jury_pool_ref,
        sample_juror_wallet,
        sample_juror_utxo_assigned,
        sample_challenge_utxo_voting,
        sample_verdict_byte,
        sample_commitment_salt,
        default_commit_window_ms,
    )
    # Read challenged_at from the challenge UTxO we set up in the fixture.
    chal_fields = _decode_datum(sample_challenge_utxo_voting.output).value
    challenged_at_ms = chal_fields[6]
    commit_deadline_slot = (
        (challenged_at_ms + default_commit_window_ms) // 1000 - SYSTEM_START_UNIX
    )

    assert builder.ttl is not None, "builder.ttl must be set"
    assert builder.ttl < commit_deadline_slot, (
        f"builder.ttl ({builder.ttl}) must be strictly less than "
        f"commit_deadline_slot ({commit_deadline_slot}) so the on-chain "
        f"tx_ends_before(commit_deadline) check passes "
        f"(jury_pool.ak:478). "
        f"challenged_at_ms={challenged_at_ms}, "
        f"commit_window_ms={default_commit_window_ms}, "
        f"SYSTEM_START_UNIX={SYSTEM_START_UNIX}"
    )


# ═════════════════════════════════════════════════════════════════════════
# Client-side guards (fail-fast BEFORE builder work)
# ═════════════════════════════════════════════════════════════════════════


def test_raises_if_juror_active_case_is_None(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_unassigned,
    sample_challenge_utxo_voting,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Client-side guard: if juror.active_case == None, refuse to submit.
    Validator would reject via `expect Some(active_token_name) =
    juror.active_case` (line 447) — but failing fast is cheaper and
    gives a clearer error than a script failure.
    """
    with pytest.raises(ValueError, match=r"(?i)active.?case|not.*assigned"):
        _run_build_commit(
            patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_unassigned,       # unassigned
            sample_challenge_utxo_voting,
            0x00,
            sample_commitment_salt,
            default_commit_window_ms,
            juror_utxo_override=sample_juror_utxo_unassigned,
        )


def test_raises_if_juror_active_case_mismatches_challenge_token(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_mismatched_challenge,
    sample_challenge_utxo_voting,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Client-side guard: juror.active_case must match the challenge
    token being voted on. If it points at a different challenge, the
    on-chain `challenge_ok` check would fail (no reference input
    carries a token name matching juror.active_case). Refuse fast.
    """
    with pytest.raises(ValueError, match=r"(?i)active.?case|challenge.*token|mismatch"):
        _run_build_commit(
            patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_mismatched_challenge,
            sample_challenge_utxo_voting,
            0x00,
            sample_commitment_salt,
            default_commit_window_ms,
            juror_utxo_override=sample_juror_utxo_mismatched_challenge,
        )


def test_raises_if_juror_already_committed(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_already_committed,
    sample_challenge_utxo_voting,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Client-side guard: if juror.vote_commitment is Some(_), the
    validator's `not_committed` predicate (jury_pool.ak:450-454) will
    reject. Refuse fast — double-commit is a caller bug.
    """
    with pytest.raises(ValueError, match=r"(?i)already.*commit|double.*commit|vote_commitment"):
        _run_build_commit(
            patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_already_committed,
            sample_challenge_utxo_voting,
            0x00,
            sample_commitment_salt,
            default_commit_window_ms,
            juror_utxo_override=sample_juror_utxo_already_committed,
        )


def test_raises_if_commit_deadline_passed(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_commitment_salt,
):
    """Client-side guard: if current_slot * 1000 already exceeds
    `challenged_at + commit_window`, the validator's
    tx_ends_before(commit_deadline) check is impossible to satisfy.
    Refuse fast with a clear message.

    We simulate a "deadline passed" scenario by passing a TINY
    commit_window_ms — 1 ms — which makes the deadline lie well before
    the mock context's canned slot.
    """
    tiny_window = 1  # 1 millisecond — guaranteed already past at CANNED_SLOT
    with pytest.raises(ValueError, match=r"(?i)commit.*deadline|window.*closed"):
        _run_build_commit(
            patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
            sample_deployment_with_jury_pool_ref,
            sample_juror_wallet,
            sample_juror_utxo_assigned,
            sample_challenge_utxo_voting,
            0x00,
            sample_commitment_salt,
            tiny_window,
        )


def test_raises_if_verdict_byte_invalid(
    patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
    sample_deployment_with_jury_pool_ref,
    sample_juror_wallet,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    sample_commitment_salt,
    default_commit_window_ms,
):
    """Client-side guard: only verdict bytes 0x00 (ClaimerWins) and
    0x01 (AuditorWins) are valid juror commit inputs. 0x02 is
    Inconclusive, which is reserved for oracle fallback — a juror
    submitting 0x02 would produce a commitment the RevealVote
    verification could validate but is semantically illegal at the
    sim level. Anything outside {0x00, 0x01} MUST raise.

    We test representative invalid values spanning the surrounding
    space: 0x02 (Inconclusive, not permitted for jurors), 0xFF (top of
    byte range), and a non-byte int.
    """
    for bad in (0x02, 0xFF, -1, 256):
        with pytest.raises(ValueError, match=r"(?i)verdict"):
            _run_build_commit(
                patched_network_for_commit_vote, captured_builder, mock_ogmios_context,
                sample_deployment_with_jury_pool_ref,
                sample_juror_wallet,
                sample_juror_utxo_assigned,
                sample_challenge_utxo_voting,
                bad,
                sample_commitment_salt,
                default_commit_window_ms,
            )
