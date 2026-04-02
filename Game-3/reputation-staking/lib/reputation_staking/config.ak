/// Cross-validator configuration for Game 3: Reputation Staking.
/// Passed as a parameter to the multi-validators at compile/deploy time.
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Section 11.2.

use cardano/assets.{PolicyId}

/// Configuration linking all external contracts and shared infrastructure.
pub type ReputationConfig {
  /// Policy ID of the reputation validator (self-referential for mint checks)
  reputation_policy_id: PolicyId,
  /// Policy ID of the endorsement validator
  endorsement_policy_id: PolicyId,
  /// Policy ID of the Agent Registry (NFT policy for DID verification)
  registry_policy_id: PolicyId,
  /// Script hash of the Agent Registry validator (for address verification)
  registry_script_hash: ByteArray,
  /// Script hash of Game 1 claim validator (= claim token policy_id)
  audit_claim_validator_hash: ByteArray,
  /// Script hash of Game 1 challenge validator (= challenge token policy_id)
  audit_challenge_validator_hash: ByteArray,
  /// Script hash of Game 1 jury pool validator (= juror token policy_id)
  audit_jury_pool_hash: ByteArray,
  /// Policy ID of the NFT that authenticates the Game 1 CrossValidatorRefs UTxO.
  /// Used to locate and trust Game 1 cross-validator references in reference inputs.
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
}
