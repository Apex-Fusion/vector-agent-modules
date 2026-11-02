// Module 6: Self-Improvement Module — Utilities
// Identity checks, oracle checks, and token name derivation.
// Delegates to shared library (vector/shared) for cross-module functions.

use aiken/builtin
use aiken/collection/list
use cardano/address.{Credential}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, OutputReference}
use governance_suggestion/types.{GovernanceConfig, GovernanceCrossRefs}
use shared/credential
use shared/identity.{Window}
use shared/oracle
use shared/token_naming
use vector_identity/types.{IdentityDatum} as identity_types

// ============================================================================
// Cross-Validator References
// ============================================================================

/// Look up GovernanceCrossRefs from a reference input authenticated by refs_token_policy.
pub fn get_cross_refs(
  config: GovernanceConfig,
  reference_inputs: List<Input>,
) -> GovernanceCrossRefs {
  expect Some(refs_input) =
    list.find(
      reference_inputs,
      fn(input) {
        list.any(
          assets.flatten(input.output.value),
          fn(entry) {
            let (pid, _name, qty) = entry
            pid == config.refs_token_policy && qty > 0
          },
        )
      },
    )
  expect InlineDatum(raw) = refs_input.output.datum
  expect refs: GovernanceCrossRefs = raw
  refs
}

// ============================================================================
// Identity Check (Part 2 §7 of docs/IDENTITY_REGISTRY.md, Module-6 row)
// ============================================================================

/// The identity check of an action by an agent that opens or extends a
/// position: proposal submission and amendment, critique submission and
/// endorsement submission. Exits and settlements have no identity check.
///
/// `w` is the window of the transaction (shared/identity.window): finite
/// bounds, lo <= hi and hi - lo <= one hour. The caller gets it first and
/// fails without it.
///
/// MUST:
///   1. The record of `agent_did` is a reference input at the exact identity
///      address that holds exactly one token (config.identity_hash,
///      agent_did), and its effective status over the whole window [lo, hi]
///      is Active (shared/identity.active_record). The transaction fails when
///      a subagent's ancestor record is not a reference input.
///   2. `agent_credential` is a declared credential of the record: a
///      verification key whose hash is the hash of the controller or of a
///      listed Ed25519 key with the transaction purpose
///      (shared/identity.declared_credential). A script credential never
///      passes.
///   3. The key of `agent_credential` signed the transaction.
///
/// Returns the record when all hold, else None.
pub fn agent_record(
  config: GovernanceConfig,
  agent_did: ByteArray,
  agent_credential: Credential,
  w: Window,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Option<IdentityDatum> {
  when
    identity.active_record(reference_inputs, config.identity_hash, agent_did, w)
  is {
    Some(record) ->
      if and {
        identity.declared_credential(record, agent_credential),
        credential.verify_signer(agent_credential, signatories),
      } {
        Some(record)
      } else {
        None
      }
    None -> None
  }
}

/// True when the identity check of `agent_record` holds. With the
/// transaction's reference inputs, signatories and window, this is
/// shared/identity.credential_opens.
pub fn verify_active_agent(
  config: GovernanceConfig,
  agent_did: ByteArray,
  agent_credential: Credential,
  w: Window,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  when
    agent_record(
      config,
      agent_did,
      agent_credential,
      w,
      reference_inputs,
      signatories,
    )
  is {
    Some(_) -> True
    None -> False
  }
}

// ============================================================================
// Oracle Verification (delegates to shared)
// ============================================================================

/// Verify that the Foundation oracle is active and has signed the transaction.
/// The oracle record must hold the `oracle` record token
/// (config.record_token_policy), and its credential must be a verification key.
pub fn verify_oracle_signature(
  config: GovernanceConfig,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  oracle.verify_oracle_signature(
    config.governance_oracle_hash,
    config.record_token_policy,
    reference_inputs,
    signatories,
  )
}

// ============================================================================
// Credential Helpers (delegates to shared)
// ============================================================================

/// Check that a payment credential is a VerificationKey and has signed.
pub fn verify_signer(cred: Credential, signatories: List<ByteArray>) -> Bool {
  credential.verify_signer(cred, signatories)
}

// ============================================================================
// Token Name Derivation (module-specific prefixes, shared helpers)
// ============================================================================

/// Derive proposal token asset name: "prop_" ++ blake2b_256(utxo_ref)[0..28]
pub fn proposal_token_name(utxo_ref: OutputReference) -> ByteArray {
  let ref_bytes = token_naming.serialise_output_reference(utxo_ref)
  token_naming.derive_token_name("prop_", ref_bytes, 27)
}

/// Derive critique token asset name: "crit_" ++ blake2b_256(utxo_ref)[0..28]
pub fn critique_token_name(utxo_ref: OutputReference) -> ByteArray {
  let ref_bytes = token_naming.serialise_output_reference(utxo_ref)
  token_naming.derive_token_name("crit_", ref_bytes, 27)
}

/// Derive endorsement token asset name: "gend_" ++ blake2b_256(endorser_did ++ proposal_ref)[0..28]
/// Uses "gend_" (governance endorsement) to avoid collision with Module 3 endorsement tokens
pub fn endorsement_token_name(
  endorser_did: ByteArray,
  proposal_ref: OutputReference,
) -> ByteArray {
  let ref_bytes = token_naming.serialise_output_reference(proposal_ref)
  let combined = builtin.append_bytearray(endorser_did, ref_bytes)
  token_naming.derive_token_name("gend_", combined, 27)
}

/// Derive activity tracking token asset name: "pact_" ++ blake2b_256(agent_did)[0..27].
/// The node of the agent in the activity list holds it (§4.6).
pub fn activity_token_name(agent_did: ByteArray) -> ByteArray {
  token_naming.derive_token_name("pact_", agent_did, 27)
}

/// Asset name of the record token of the head of the activity list (§4.6),
/// under config.record_token_policy. The one-shot record token transaction
/// of the deployment mints it together with the `params` and `oracle`
/// tokens (shared/record_auth), so at most one output holds it.
pub const activity_head_token: ByteArray = "activity"

/// Derive prediction token asset name: "pred_" ++ blake2b_256(utxo_ref)[0..28]
pub fn prediction_token_name(utxo_ref: OutputReference) -> ByteArray {
  let ref_bytes = token_naming.serialise_output_reference(utxo_ref)
  token_naming.derive_token_name("pred_", ref_bytes, 27)
}

// ============================================================================
// Proposal State Helpers
// ============================================================================

/// Check if a proposal state allows critiques/endorsements (Open or Amended)
pub fn is_proposal_active(state: types.ProposalState) -> Bool {
  when state is {
    types.Open -> True
    types.Amended { .. } -> True
    _ -> False
  }
}

/// Check if a proposal state is terminal (cannot be further modified)
pub fn is_proposal_terminal(state: types.ProposalState) -> Bool {
  when state is {
    types.Adopted { .. } -> True
    types.Rejected { .. } -> True
    types.Expired -> True
    types.Withdrawn -> True
    _ -> False
  }
}

// ============================================================================
// Value Helpers
// ============================================================================

/// Get the lovelace amount from a value (AP3X = native lovelace on Vector)
pub fn lovelace_of(value: assets.Value) -> Int {
  assets.lovelace_of(value)
}
