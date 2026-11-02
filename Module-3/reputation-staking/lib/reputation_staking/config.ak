/// Cross-validator configuration for Module 3: Reputation Staking.
/// Passed as a parameter to the multi-validators at compile/deploy time.
/// See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Section 11.2.
///
/// Design note: Cross-validator hashes (reputation_policy_id, endorsement_policy_id)
/// are intentionally absent from the compile-time config. Including them would
/// create a circular dependency with `aiken blueprint apply` (applying the config
/// changes the hash, which changes the config, ad infinitum). Instead, these live
/// in CrossValidatorRefs — a runtime datum stored in a reference input UTxO and
/// authenticated by refs_token_policy. This follows Module-1's AuditingConfig pattern.

use aiken/collection/list
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Input}

/// Compile-time configuration — baked into all validators via blueprint apply.
/// Contains ONLY fields that do NOT create circular deployment dependencies.
///
/// The Module-1 hashes come from the Module-1 deployment that this
/// deployment uses, so Module-1 is deployed first (Part 2 §7 of
/// `docs/IDENTITY_REGISTRY.md` in agents-marketplace).
pub type ReputationConfig {
  /// Policy ID of the NFT that authenticates the CrossValidatorRefs UTxO.
  /// Must be a NativeScript policy locked to oracle_credential (ScriptPubkey).
  refs_token_policy: PolicyId,
  /// Script hash of Module 1 claim validator (= claim token policy_id)
  audit_claim_validator_hash: ByteArray,
  /// Script hash of Module 1 challenge validator (= challenge token policy_id)
  audit_challenge_validator_hash: ByteArray,
  /// Script hash of Module 1 jury pool validator (= juror token policy_id)
  audit_jury_pool_hash: ByteArray,
  /// Policy ID of the NFT that authenticates the Module 1 CrossValidatorRefs UTxO.
  audit_refs_token_policy: PolicyId,
  /// Script hash of protocol params holder
  params_script_hash: ByteArray,
  /// Script hash of protocol treasury
  treasury_script_hash: ByteArray,
  /// Policy ID of the AP3X token
  ap3x_policy_id: PolicyId,
  /// AP3X token asset name
  ap3x_asset_name: ByteArray,
  /// Foundation oracle credential (for Phase 1.0 challenge/decay resolution)
  oracle_credential: ByteArray,
  /// Policy ID of the one-shot record token policy
  /// (shared/holder-scripts/validators/record_token.ak). The ProtocolParams
  /// record at params_script_hash holds its `params` token. Outputs at the
  /// params holder without that token are ignored.
  params_token_policy: PolicyId,
  /// Script hash (= policy ID) of the identity registry contract `identity`.
  /// An agent's record is a reference input at its exact address that holds
  /// the token named with the agent's identifier (`vector_identity`).
  identity_hash: ByteArray,
}

/// Runtime cross-validator references — stored as an inline datum on a UTxO
/// that is authenticated by the refs_token_policy NFT.
/// Deployed AFTER the two validators are on-chain (their hashes are known).
/// Included as a reference input in every transaction that needs cross-refs.
pub type CrossValidatorRefs {
  /// Policy ID / script hash of the reputation validator
  reputation_policy_id: PolicyId,
  /// Policy ID / script hash of the endorsement validator
  endorsement_policy_id: PolicyId,
}

/// Look up CrossValidatorRefs from a reference input authenticated by refs_token_policy.
pub fn get_cross_refs(
  cfg: ReputationConfig,
  reference_inputs: List<Input>,
) -> CrossValidatorRefs {
  expect Some(refs_input) =
    list.find(
      reference_inputs,
      fn(input) {
        list.any(
          assets.flatten(input.output.value),
          fn(entry) {
            let (pid, _name, qty) = entry
            pid == cfg.refs_token_policy && qty > 0
          },
        )
      },
    )
  expect InlineDatum(raw) = refs_input.output.datum
  expect refs: CrossValidatorRefs = raw
  refs
}
