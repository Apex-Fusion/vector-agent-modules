/// Shared utility functions for Module 1: Adversarial Auditing
///
/// Helpers for DID verification, stake math, token naming, datum access,
/// signing checks, and the Phase 1.1 PRNG jury selection algorithm.
///
/// VRF research incorporated (VRF research 2026-03-23):
///   - Juror selection uses slot-number-based seed (Option C from report)
///   - Validator sorts juror inputs internally — never trusts submitter ordering
///   - Block hash NOT accepted from redeemer without oracle authentication
///   - SELECTION_DELAY minimum is 30 slots

use aiken/builtin
use aiken/cbor
use aiken/collection/dict
use aiken/collection/list
use aiken/crypto
use aiken/interval
use aiken/primitive/bytearray
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets.{PolicyId}
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction,
}
use adversarial_auditing/types.{AuditingConfig, CrossValidatorRefs}

// ══════════════════════════════════════════════════════════════════════
// TOKEN NAMING
// ══════════════════════════════════════════════════════════════════════

/// Derive claim token asset name from the seed UTXO reference.
/// Format: "clm_" (4 bytes) ++ blake2b_256(cbor(seed_ref))[0..28]
/// Total: 32 bytes — maximum AssetName length.
/// The prefix makes token purpose evident in block explorers.
pub fn claim_token_name(seed: OutputReference) -> ByteArray {
  let hash = crypto.blake2b_256(cbor.serialise(seed))
  // "clm_" = 0x636c6d5f
  builtin.append_bytearray(#"636c6d5f", builtin.slice_bytearray(0, 28, hash))
}

/// Derive challenge token asset name from the seed UTXO reference.
/// Format: "chl_" (4 bytes) ++ blake2b_256(cbor(seed_ref))[0..28]
pub fn challenge_token_name(seed: OutputReference) -> ByteArray {
  let hash = crypto.blake2b_256(cbor.serialise(seed))
  // "chl_" = 0x63686c5f
  builtin.append_bytearray(#"63686c5f", builtin.slice_bytearray(0, 28, hash))
}

/// Derive juror token asset name from the seed UTXO reference.
/// Format: "jur_" (4 bytes) ++ blake2b_256(cbor(seed_ref))[0..28]
pub fn juror_token_name(seed: OutputReference) -> ByteArray {
  let hash = crypto.blake2b_256(cbor.serialise(seed))
  // "jur_" = 0x6a75725f
  builtin.append_bytearray(#"6a75725f", builtin.slice_bytearray(0, 28, hash))
}

// ══════════════════════════════════════════════════════════════════════
// DID VERIFICATION
// ══════════════════════════════════════════════════════════════════════

/// Verify that a DID is active in the Agent Registry via reference input.
/// Uses CIP-31 reference inputs — does not consume the registry UTXO.
///
/// Checks: the UTXO contains the agent's identity NFT under the registry policy.
/// Note: registry_script_hash check removed — it was part of AuditingConfig but
/// registry_script_hash is not circular and could be added to CrossValidatorRefs
/// in a future upgrade if needed.
pub fn verify_active_did(
  config: AuditingConfig,
  agent_did: PolicyId,
  reference_inputs: List<Input>,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      // The registry NFT asset name equals the agent's DID (28-byte policy id)
      assets.quantity_of(
        input.output.value,
        config.registry_policy_id,
        agent_did,
      ) == 1
    },
  )
}

// ══════════════════════════════════════════════════════════════════════
// SIGNING HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Check if a Credential has signed the transaction.
/// Only VerificationKey credentials can appear in extra_signatories.
pub fn credential_signed(tx: Transaction, credential: Credential) -> Bool {
  when credential is {
    VerificationKey(vkh) ->
      list.any(tx.extra_signatories, fn(s) { s == vkh })
    Script(_) -> False
  }
}

/// Check if a raw VerificationKeyHash has signed the transaction.
pub fn vkh_signed(tx: Transaction, vkh: ByteArray) -> Bool {
  list.any(tx.extra_signatories, fn(s) { s == vkh })
}

// ══════════════════════════════════════════════════════════════════════
// UTXO HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Find a specific input by output reference.
pub fn find_input(
  inputs: List<Input>,
  ref: OutputReference,
) -> Option<Input> {
  list.find(inputs, fn(i) { i.output_reference == ref })
}

/// Check that an output carries an inline datum.
pub fn has_inline_datum(output: Output) -> Bool {
  when output.datum is {
    InlineDatum(_) -> True
    _ -> False
  }
}

/// Count inputs at a given script hash.
/// Used for double-satisfaction prevention: must be 1 in most spend paths.
pub fn count_script_inputs(inputs: List<Input>, script_hash: ByteArray) -> Int {
  list.count(
    inputs,
    fn(i) {
      when i.output.address.payment_credential is {
        Script(h) -> h == script_hash
        _ -> False
      }
    },
  )
}

/// Find the first output at a given script hash.
pub fn find_output_at_script(
  outputs: List<Output>,
  script_hash: ByteArray,
) -> Option<Output> {
  list.find(
    outputs,
    fn(o) {
      when o.address.payment_credential is {
        Script(h) -> h == script_hash
        _ -> False
      }
    },
  )
}

/// Find all outputs at a given script hash.
pub fn outputs_at_script(
  outputs: List<Output>,
  script_hash: ByteArray,
) -> List<Output> {
  list.filter(
    outputs,
    fn(o) {
      when o.address.payment_credential is {
        Script(h) -> h == script_hash
        _ -> False
      }
    },
  )
}

// ══════════════════════════════════════════════════════════════════════
// AP3X VALUE HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Get the AP3X quantity from a value.
pub fn ap3x_of(
  value: assets.Value,
  ap3x_policy: PolicyId,
  ap3x_name: ByteArray,
) -> Int {
  assets.quantity_of(value, ap3x_policy, ap3x_name)
}

/// Check that an output carries at least `expected` AP3X.
pub fn output_has_min_ap3x(
  output: Output,
  ap3x_policy: PolicyId,
  ap3x_name: ByteArray,
  expected: Int,
) -> Bool {
  assets.quantity_of(output.value, ap3x_policy, ap3x_name) >= expected
}

/// Check that an output's payment credential matches the given one.
pub fn output_to_credential(output: Output, cred: Credential) -> Bool {
  output.address.payment_credential == cred
}

// ══════════════════════════════════════════════════════════════════════
// VALIDITY RANGE HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Check that the tx validity range is entirely after `slot`.
/// Used to confirm: challenge_window has passed, selection_delay elapsed, etc.
pub fn tx_started_after(tx: Transaction, slot: Int) -> Bool {
  interval.is_entirely_after(tx.validity_range, slot)
}

/// Check that the tx validity range is entirely before `slot`.
/// Used to confirm: within challenge window, within resolution deadline, etc.
pub fn tx_ends_before(tx: Transaction, slot: Int) -> Bool {
  interval.is_entirely_before(tx.validity_range, slot)
}

/// Check that `slot` is contained within the tx validity range.
/// Used to validate claimer/auditor-supplied timestamps (submitted_at, challenged_at).
/// Prevents backdating (slot < lower_bound) and future-dating (slot > upper_bound).
pub fn tx_contains_slot(tx: Transaction, slot: Int) -> Bool {
  !interval.is_entirely_after(tx.validity_range, slot) && !interval.is_entirely_before(
    tx.validity_range,
    slot,
  )
}

// ══════════════════════════════════════════════════════════════════════
// HASH VALIDATION
// ══════════════════════════════════════════════════════════════════════

/// Validate that a hash field is exactly 32 bytes (blake2b_256 output).
pub fn is_valid_hash(hash: ByteArray) -> Bool {
  builtin.length_of_bytearray(hash) == 32
}

// ══════════════════════════════════════════════════════════════════════
// TOKEN MINT/BURN HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Verify exactly one token is minted: policy + name + qty == +1.
pub fn exactly_one_minted(
  tx: Transaction,
  policy_id: PolicyId,
  token_name: ByteArray,
) -> Bool {
  let tokens = assets.tokens(tx.mint, policy_id)
  when dict.to_pairs(tokens) is {
    [Pair(name, qty)] -> name == token_name && qty == 1
    _ -> False
  }
}

/// Verify exactly one token is burned: policy + name + qty == -1.
pub fn exactly_one_burned(
  tx: Transaction,
  policy_id: PolicyId,
  token_name: ByteArray,
) -> Bool {
  let tokens = assets.tokens(tx.mint, policy_id)
  when dict.to_pairs(tokens) is {
    [Pair(name, qty)] -> name == token_name && qty == -1
    _ -> False
  }
}

// ══════════════════════════════════════════════════════════════════════
// CROSS-VALIDATOR REFS
// ══════════════════════════════════════════════════════════════════════

/// Look up the CrossValidatorRefs from reference inputs.
/// Authenticates by finding a reference input whose value contains at least one
/// token under config.refs_token_policy. Parses its inline datum as CrossValidatorRefs.
///
/// Fails (expect) if no such input is found — callers MUST include the refs UTxO
/// as a reference input in every transaction that needs cross-validator hashes.
pub fn get_cross_refs(
  config: AuditingConfig,
  reference_inputs: List<Input>,
) -> CrossValidatorRefs {
  expect Some(refs_input) =
    list.find(reference_inputs, fn(inp) {
      // Authenticated refs UTxO: carries at least one token under refs_token_policy
      !dict.is_empty(assets.tokens(inp.output.value, config.refs_token_policy))
    })
  expect InlineDatum(raw) = refs_input.output.datum
  expect refs: CrossValidatorRefs = raw
  refs
}

// ══════════════════════════════════════════════════════════════════════
// PHASE 1.1 PRNG JURY SELECTION
// ══════════════════════════════════════════════════════════════════════
//
// Design notes (VRF research 2026-03-23):
//
//   NEVER trust a redeemer-supplied block hash without oracle authentication.
//   In Phase 1.0, the oracle controls selection entirely (signs the tx).
//   In Phase 1.1, we use a slot-number-based seed (Option C from report):
//
//     seed = blake2b_256(
//       cbor(challenge_utxo_ref) ++
//       cbor(challenge_datum.challenged_at + params.selection_delay)
//     )
//
//   The slot component is fixed (challenge slot is immutable in the datum),
//   so the seed is deterministic and cannot be manipulated after challenge creation.
//   Weakness: predictable before the selection slot arrives. This is honest and
//   acceptable for Phase 1.1. Phase 1.2+ will add a block hash oracle (Option B).
//
//   CRITICAL: The validator must sort juror_did inputs lexicographically
//   before running selection — do NOT trust the submitter's ordering.

/// Compute the jury selection seed from a challenge UTXO ref and the
/// fixed selection slot (challenged_at + selection_delay).
///
/// Phase 1.1+: slot-number entropy (Option C from the VRF report).
/// Phase 1.2+: replace slot_number with authenticated oracle block hash.
pub fn compute_selection_seed(
  challenge_ref: OutputReference,
  selection_slot: Int,
) -> ByteArray {
  // Mix the fixed UTXO ref with the fixed slot number.
  // Both are determined at OpenChallenge time and immutable afterward.
  crypto.blake2b_256(
    builtin.append_bytearray(
      cbor.serialise(challenge_ref),
      cbor.serialise(selection_slot),
    ),
  )
}

/// Select n jurors from an eligible pool using hash-chain PRNG.
/// The pool MUST be sorted lexicographically by juror_did before calling.
///
/// Algorithm: for i in 0..n:
///   index_hash = blake2b_256(seed ++ cbor(i))
///   index = bytearray_to_int(index_hash[0..8]) % len(remaining)
///   selected[i] = remaining[index]
///   remaining.remove(index)
///
/// This is O(n * pool_size) — acceptable for JURY_SIZE=5, pool ≤ 50.
pub fn select_jurors_prng(
  seed: ByteArray,
  eligible: List<PolicyId>,
  n: Int,
) -> List<PolicyId> {
  select_loop(seed, eligible, n, 0, [])
}

fn select_loop(
  seed: ByteArray,
  remaining: List<PolicyId>,
  n: Int,
  i: Int,
  acc: List<PolicyId>,
) -> List<PolicyId> {
  if i >= n || list.is_empty(remaining) {
    acc
  } else {
    let index_hash =
      crypto.blake2b_256(
        builtin.append_bytearray(seed, cbor.serialise(i)),
      )
    // Use first 8 bytes as integer (big-endian)
    let raw_index =
      builtin.bytearray_to_integer(True, builtin.slice_bytearray(0, 8, index_hash))
    // Modulo: always positive since bytearray_to_integer returns non-negative
    let len = list.length(remaining)
    let index = raw_index % len
    expect Some(chosen) = list.at(remaining, index)
    let remaining2 = list_remove_at(remaining, index)
    select_loop(seed, remaining2, n, i + 1, [chosen, ..acc])
  }
}

/// Remove the element at a given index from a list.
fn list_remove_at(lst: List<a>, index: Int) -> List<a> {
  list_remove_at_loop(lst, index, 0, [])
}

fn list_remove_at_loop(
  lst: List<a>,
  target: Int,
  current: Int,
  acc: List<a>,
) -> List<a> {
  when lst is {
    [] -> list.reverse(acc)
    [head, ..tail] ->
      if current == target {
        list.concat(list.reverse(acc), tail)
      } else {
        list_remove_at_loop(tail, target, current + 1, [head, ..acc])
      }
  }
}

/// Sort a list of PolicyIds (ByteArrays) lexicographically.
/// Required by the PRNG selection: the pool ordering must be deterministic
/// and validator-derived — NEVER trust the submitter's ordering.
pub fn sort_dids(dids: List<PolicyId>) -> List<PolicyId> {
  list.sort(dids, bytearray.compare)
}

/// Verify that a list of selected juror DIDs matches the PRNG output
/// given the seed and eligible pool (already sorted by caller).
///
/// Both lists are sorted before comparison to be order-independent.
pub fn verify_juror_selection(
  seed: ByteArray,
  eligible_sorted: List<PolicyId>,
  claimed_selection: List<PolicyId>,
  jury_size: Int,
) -> Bool {
  let expected = select_jurors_prng(seed, eligible_sorted, jury_size)
  // Sort both before comparison: selection is a set, not an ordered list
  list.sort(expected, bytearray.compare) == list.sort(
    claimed_selection,
    bytearray.compare,
  )
}
