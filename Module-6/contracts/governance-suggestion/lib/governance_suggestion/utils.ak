// Module 6: Governance Suggestion Engine — Utilities
// DID verification, oracle checks, and token name derivation.
// Delegates to shared library (vector/shared) for cross-module functions.

use aiken/builtin
use cardano/address.{Credential}
use cardano/assets
use aiken/collection/list
use cardano/transaction.{InlineDatum, Input, OutputReference}

use governance_suggestion/types.{GovernanceConfig, GovernanceCrossRefs}

use shared/credential
use shared/did_verification
use shared/oracle
use shared/token_naming

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
// DID Verification (delegates to shared)
// ============================================================================

/// Verify that a DID is active in the Agent Registry.
/// Wraps shared/did_verification.verify_agent_exists with GovernanceConfig.
pub fn verify_active_did(
  config: GovernanceConfig,
  agent_did: ByteArray,
  reference_inputs: List<Input>,
) -> Bool {
  did_verification.verify_agent_exists(
    reference_inputs,
    config.registry_script_hash,
    config.registry_policy_id,
    agent_did,
  )
}

// ============================================================================
// Oracle Verification (delegates to shared)
// ============================================================================

/// Verify that the Foundation oracle is active and has signed the transaction.
/// Wraps shared/oracle.verify_oracle_signature with GovernanceConfig.
pub fn verify_oracle_signature(
  config: GovernanceConfig,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  oracle.verify_oracle_signature(
    config.governance_oracle_hash,
    reference_inputs,
    signatories,
  )
}

// ============================================================================
// Credential Helpers (delegates to shared)
// ============================================================================

/// Check that a payment credential is a VerificationKey and has signed.
pub fn verify_signer(
  cred: Credential,
  signatories: List<ByteArray>,
) -> Bool {
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

/// Derive activity tracking token asset name: "pact_" ++ blake2b_256(agent_did)[0..28]
pub fn activity_token_name(agent_did: ByteArray) -> ByteArray {
  token_naming.derive_token_name("pact_", agent_did, 27)
}

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
