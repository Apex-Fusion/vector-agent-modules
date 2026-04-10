/// Utility functions for Module 3: Reputation Staking.
///
/// DID verification, capability matching, credential checks, and byte helpers
/// delegate to the shared library (shared/). Token name derivation and AP3X
/// helpers remain Module 3–specific.
///
/// This module re-exports shared functions with Module 3's config-based
/// signatures so that all validation code can import from a single place.

use aiken/builtin
use aiken/collection/list
use cardano/address.{Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, OutputReference, Transaction}
use reputation_staking/config.{ReputationConfig}
use reputation_staking/types.{
  challenge_token_prefix, endorsement_token_prefix, history_bonus_token_prefix,
  stake_token_prefix,
}
use shared/bytearray as shared_bytearray
use shared/credential as shared_credential
use shared/did_verification
use shared/token_naming
use shared/utxo

// ──────────────────────────────────────────────────────────────────────────────
// Agent DID verification (delegates to shared/did_verification)
// ──────────────────────────────────────────────────────────────────────────────

/// Agent Registry datum type — re-exported from shared for backwards compatibility.
pub type AgentDatum =
  did_verification.AgentDatum

/// Find and return the AgentDatum for a given DID from reference inputs.
pub fn find_agent_registry_datum(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> AgentDatum {
  did_verification.find_agent_registry_datum(
    reference_inputs,
    cfg.registry_script_hash,
    cfg.registry_policy_id,
    agent_did,
  )
}

/// Verify that an agent DID exists in the registry (boolean check).
pub fn verify_agent_exists(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> Bool {
  did_verification.verify_agent_exists(
    reference_inputs,
    cfg.registry_script_hash,
    cfg.registry_policy_id,
    agent_did,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Capability matching (delegates to shared/did_verification)
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that claimed capabilities are a subset of the agent's registered
/// capabilities in the Agent Registry.
pub fn verify_capabilities_match(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
  agent_did: ByteArray,
  claimed_capabilities: List<ByteArray>,
) -> Bool {
  did_verification.verify_capabilities_match(
    reference_inputs,
    cfg.registry_script_hash,
    cfg.registry_policy_id,
    agent_did,
    claimed_capabilities,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Credential / signature checks
// ──────────────────────────────────────────────────────────────────────────────

/// Check that the transaction is signed by the given credential.
/// Only supports VerificationKey credentials (not Script credentials for signing).
pub fn has_credential_signed(tx: Transaction, credential: Credential) -> Bool {
  when credential is {
    VerificationKey(hash) -> list.has(tx.extra_signatories, hash)
    Script(_) -> False
  }
}

/// Check that the transaction is signed by a specific verification key hash.
pub fn has_key_signed(tx: Transaction, key_hash: ByteArray) -> Bool {
  shared_credential.has_key_signed(tx.extra_signatories, key_hash)
}

// ──────────────────────────────────────────────────────────────────────────────
// ByteArray helpers (delegates to shared/bytearray)
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that a list is non-empty.
pub fn is_non_empty(items: List<a>) -> Bool {
  shared_bytearray.is_non_empty(items)
}

/// Verify that all items in `subset` also appear in `superset`.
pub fn is_subset(subset: List<ByteArray>, superset: List<ByteArray>) -> Bool {
  shared_bytearray.is_subset(subset, superset)
}

/// Check if a ByteArray starts with a given prefix.
pub fn starts_with(data: ByteArray, prefix: ByteArray) -> Bool {
  shared_bytearray.starts_with(data, prefix)
}

// ──────────────────────────────────────────────────────────────────────────────
// Token name derivation (Module 3–specific prefixes, uses shared/token_naming)
// ──────────────────────────────────────────────────────────────────────────────

/// Derive the stake token asset name: "rstk_" ++ blake2b_256(agent_did)[0..28]
pub fn derive_stake_token_name(agent_did: ByteArray) -> ByteArray {
  // 5-byte prefix + 27-byte hash slice = 32 bytes (Cardano max asset name)
  token_naming.derive_token_name(stake_token_prefix, agent_did, 27)
}

/// Derive the endorsement token asset name:
/// "rend_" ++ blake2b_256(endorser_did ++ target_did)[0..28]
pub fn derive_endorsement_token_name(
  endorser_did: ByteArray,
  target_did: ByteArray,
) -> ByteArray {
  let combined = builtin.append_bytearray(endorser_did, target_did)
  // 5-byte prefix + 27-byte hash slice = 32 bytes (Cardano max asset name)
  token_naming.derive_token_name(endorsement_token_prefix, combined, 27)
}

/// Derive the challenge token asset name:
/// "rchl_" ++ blake2b_256(challenger_did ++ target_did ++ capability)[0..24]
pub fn derive_challenge_token_name(
  challenger_did: ByteArray,
  target_did: ByteArray,
  capability: ByteArray,
) -> ByteArray {
  let combined =
    builtin.append_bytearray(
      builtin.append_bytearray(challenger_did, target_did),
      capability,
    )
  token_naming.derive_token_name(challenge_token_prefix, combined, 24)
}

/// Derive the history bonus token asset name:
/// "hbonus_" ++ blake2b_256(source_ref_serialized)[0..24]
pub fn derive_history_bonus_token_name(source_ref: OutputReference) -> ByteArray {
  let ref_bytes = token_naming.serialise_output_reference(source_ref)
  token_naming.derive_token_name(history_bonus_token_prefix, ref_bytes, 24)
}

// ──────────────────────────────────────────────────────────────────────────────
// AP3X value helpers (Module 3–specific)
// ──────────────────────────────────────────────────────────────────────────────

/// Get the AP3X amount in an output's value.
pub fn get_ap3x_amount(
  value: assets.Value,
  ap3x_policy_id: assets.PolicyId,
  ap3x_asset_name: ByteArray,
) -> Int {
  assets.quantity_of(value, ap3x_policy_id, ap3x_asset_name)
}

// ──────────────────────────────────────────────────────────────────────────────
// UTXO search helpers (delegates to shared/utxo)
// ──────────────────────────────────────────────────────────────────────────────

/// Find a UTXO in the transaction inputs that holds the specified token.
pub fn find_input_with_token(
  inputs: List<Input>,
  policy_id: assets.PolicyId,
  asset_name: ByteArray,
) -> Input {
  utxo.find_input_with_token(inputs, policy_id, asset_name)
}

/// Count outputs at a specific script address.
pub fn count_outputs_at_script(
  outputs: List<transaction.Output>,
  script_hash: ByteArray,
) -> Int {
  utxo.count_outputs_at_script(outputs, script_hash)
}

/// Check that at least one transaction input comes from the given script address.
pub fn has_script_input(tx: Transaction, script_hash: ByteArray) -> Bool {
  utxo.has_script_input(tx.inputs, script_hash)
}

/// Count how many transaction inputs come from the given script address.
pub fn count_script_inputs(tx: Transaction, script_hash: ByteArray) -> Int {
  utxo.count_script_inputs(tx.inputs, script_hash)
}
