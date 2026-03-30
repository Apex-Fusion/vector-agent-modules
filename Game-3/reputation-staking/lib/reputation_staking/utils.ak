/// Shared utility functions for Game 3: Reputation Staking.
///
/// DID verification, capability matching, token name derivation, and
/// common validation helpers used across all validators.

use aiken/builtin
use aiken/collection/list
use cardano/address.{Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, OutputReference, Transaction}
use reputation_staking/config.{ReputationConfig}
use reputation_staking/types.{
  challenge_token_prefix, endorsement_token_prefix, history_bonus_token_prefix,
  stake_token_prefix,
}

// ──────────────────────────────────────────────────────────────────────────────
// Agent DID verification
// ──────────────────────────────────────────────────────────────────────────────

/// Agent Registry datum — mirrors the deployed AgentDatum type.
/// Defined here to avoid a cross-project dependency.
pub type AgentDatum {
  owner: Credential,
  name: ByteArray,
  description: ByteArray,
  capabilities: List<ByteArray>,
  framework: ByteArray,
  endpoint: ByteArray,
  registered_at: Int,
}

/// Verify that an agent with the given DID (NFT asset name) exists in the
/// Agent Registry. Returns the AgentDatum if found.
///
/// The DID is the NFT asset name at the registry script address.
/// An agent is "active" if the UTXO exists (no `active` field in datum).
pub fn find_agent_registry_datum(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> AgentDatum {
  expect Some(registry_input) =
    list.find(
      reference_inputs,
      fn(input) {
        let is_at_registry =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == cfg.registry_script_hash
            _ -> False
          }
        let has_nft =
          assets.quantity_of(
            input.output.value,
            cfg.registry_policy_id,
            agent_did,
          ) == 1
        is_at_registry && has_nft
      },
    )
  expect InlineDatum(raw) = registry_input.output.datum
  expect datum: AgentDatum = raw
  datum
}

/// Verify that an agent DID exists in the registry (boolean check).
pub fn verify_agent_exists(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      let is_at_registry =
        when input.output.address.payment_credential is {
          Script(hash) -> hash == cfg.registry_script_hash
          _ -> False
        }
      let has_nft =
        assets.quantity_of(
          input.output.value,
          cfg.registry_policy_id,
          agent_did,
        ) == 1
      is_at_registry && has_nft
    },
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Capability matching
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that claimed capabilities are a subset of the agent's registered
/// capabilities in the Agent Registry.
pub fn verify_capabilities_match(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
  agent_did: ByteArray,
  claimed_capabilities: List<ByteArray>,
) -> Bool {
  let agent_datum = find_agent_registry_datum(reference_inputs, cfg, agent_did)
  // Every claimed capability must exist in the registry
  list.all(
    claimed_capabilities,
    fn(cap) { list.has(agent_datum.capabilities, cap) },
  )
}

/// Verify that a list is non-empty.
pub fn is_non_empty(items: List<a>) -> Bool {
  when items is {
    [] -> False
    _ -> True
  }
}

/// Verify that all items in `subset` also appear in `superset`.
pub fn is_subset(subset: List<ByteArray>, superset: List<ByteArray>) -> Bool {
  list.all(subset, fn(item) { list.has(superset, item) })
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
  list.has(tx.extra_signatories, key_hash)
}

// ──────────────────────────────────────────────────────────────────────────────
// Token name derivation (Section 4.1b)
// ──────────────────────────────────────────────────────────────────────────────

/// Derive the stake token asset name: "rstk_" ++ blake2b_256(agent_did)[0..28]
pub fn derive_stake_token_name(agent_did: ByteArray) -> ByteArray {
  let hash = builtin.blake2b_256(agent_did)
  builtin.append_bytearray(stake_token_prefix, builtin.slice_bytearray(0, 28, hash))
}

/// Derive the endorsement token asset name:
/// "rend_" ++ blake2b_256(endorser_did ++ target_did)[0..28]
pub fn derive_endorsement_token_name(
  endorser_did: ByteArray,
  target_did: ByteArray,
) -> ByteArray {
  let combined = builtin.append_bytearray(endorser_did, target_did)
  let hash = builtin.blake2b_256(combined)
  builtin.append_bytearray(
    endorsement_token_prefix,
    builtin.slice_bytearray(0, 28, hash),
  )
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
  let hash = builtin.blake2b_256(combined)
  builtin.append_bytearray(
    challenge_token_prefix,
    builtin.slice_bytearray(0, 24, hash),
  )
}

/// Derive the history bonus token asset name:
/// "hbonus_" ++ blake2b_256(source_ref_serialized)[0..24]
pub fn derive_history_bonus_token_name(source_ref: OutputReference) -> ByteArray {
  let ref_bytes =
    builtin.append_bytearray(
      source_ref.transaction_id,
      builtin.integer_to_bytearray(True, 0, source_ref.output_index),
    )
  let hash = builtin.blake2b_256(ref_bytes)
  builtin.append_bytearray(
    history_bonus_token_prefix,
    builtin.slice_bytearray(0, 24, hash),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// ByteArray helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Check if a ByteArray starts with a given prefix.
pub fn starts_with(data: ByteArray, prefix: ByteArray) -> Bool {
  let prefix_len = builtin.length_of_bytearray(prefix)
  let data_len = builtin.length_of_bytearray(data)
  if data_len < prefix_len {
    False
  } else {
    builtin.slice_bytearray(0, prefix_len, data) == prefix
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// AP3X value helpers
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
// UTXO search helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Find a UTXO in the transaction inputs that holds the specified token.
pub fn find_input_with_token(
  inputs: List<Input>,
  policy_id: assets.PolicyId,
  asset_name: ByteArray,
) -> Input {
  expect Some(input) =
    list.find(
      inputs,
      fn(input) {
        assets.quantity_of(input.output.value, policy_id, asset_name) >= 1
      },
    )
  input
}

/// Count outputs at a specific script address.
pub fn count_outputs_at_script(
  outputs: List<transaction.Output>,
  script_hash: ByteArray,
) -> Int {
  list.count(
    outputs,
    fn(output) {
      when output.address.payment_credential is {
        Script(hash) -> hash == script_hash
        _ -> False
      }
    },
  )
}

/// Check that at least one transaction input comes from the given script address.
/// Used to couple mint handlers to spend handlers (F1/F2 fix).
pub fn has_script_input(tx: Transaction, script_hash: ByteArray) -> Bool {
  list.any(
    tx.inputs,
    fn(input) {
      when input.output.address.payment_credential is {
        Script(hash) -> hash == script_hash
        _ -> False
      }
    },
  )
}

/// Count how many transaction inputs come from the given script address.
/// Used for single-script-input guards (F5 fix).
pub fn count_script_inputs(tx: Transaction, script_hash: ByteArray) -> Int {
  list.count(
    tx.inputs,
    fn(input) {
      when input.output.address.payment_credential is {
        Script(hash) -> hash == script_hash
        _ -> False
      }
    },
  )
}
