// Module 6: Self-Improvement Module — Parameter Reading
// Read GovernanceParams from reference input UTXO

use aiken/collection/list
use cardano/address.{Script as ScriptCredential}
use cardano/transaction.{InlineDatum, Input}

use governance_suggestion/types.{GovernanceConfig, GovernanceParams}

// ============================================================================
// GovernanceParams Reading
// ============================================================================

/// Read GovernanceParams from a reference input at the governance params address.
/// Returns the params or fails if not found.
pub fn read_governance_params(
  config: GovernanceConfig,
  reference_inputs: List<Input>,
) -> GovernanceParams {
  expect Some(params_input) =
    list.find(
      reference_inputs,
      fn(input) {
        when input.output.address.payment_credential is {
          ScriptCredential(hash) -> hash == config.governance_params_hash
          _ -> False
        }
      },
    )
  expect InlineDatum(raw_datum) = params_input.output.datum
  expect params: GovernanceParams = raw_datum
  params
}

// ============================================================================
// Parameter Name Validation
// ============================================================================

/// List of governable parameter names that can be proposed for change.
/// This is the on-chain validation that a ParameterChange proposal
/// references a real parameter.
pub fn is_governable_param(param_name: ByteArray) -> Bool {
  // Module 1 parameters
  param_name == "MIN_CLAIM_STAKE" || param_name == "MIN_CHALLENGE_WINDOW" || param_name == "JURY_SIZE" || param_name == "JURY_FEE_RATE" || // Module 3 parameters
  param_name == "MIN_SELF_STAKE" || param_name == "MIN_ENDORSEMENT" || param_name == "DECAY_RATE" || param_name == "MAX_ENDORSEMENT_MULTIPLIER" || // Module 6 parameters (self-referential)
  param_name == "MIN_PROPOSAL_STAKE" || param_name == "MIN_CRITIQUE_STAKE" || param_name == "MIN_GOVERNANCE_ENDORSEMENT" || param_name == "MIN_REVIEW_WINDOW" || param_name == "MAX_REVIEW_WINDOW" || param_name == "MAX_AMENDMENTS" || param_name == "MAX_ACTIVE_PROPOSALS" || param_name == "COOLDOWN_BETWEEN_PROPOSALS" || param_name == "PROPOSER_REWARD_SHARE" || param_name == "CRITIC_REWARD_SHARE" || param_name == "PROTOCOL_FEE_RATE" || param_name == "MIN_ADOPTION_REWARD" || param_name == "MAX_ADOPTION_REWARD" || param_name == "MAX_INCORPORATED_CRITIQUES" || param_name == "MAX_TREASURY_REQUEST" || param_name == "EMERGENCY_STAKE_MULTIPLIER" || param_name == "EMERGENCY_REVIEW_WINDOW" || param_name == "TREASURY_BATCH_SIZE" || param_name == "MIN_TREASURY_BATCHES" || param_name == "ADOPTION_REWARD_BASE" || // System parameters
  param_name == "GAME_ACTIVATION"
}

// ============================================================================
// Default Parameter Values (for testnet initialization)
// ============================================================================

/// Default GovernanceParams for testnet deployment.
/// All stake amounts in lovelace (1 AP3X = 1,000,000 lovelace).
pub fn default_governance_params() -> GovernanceParams {
  GovernanceParams {
    min_proposal_stake: 25_000_000,
    // 25 AP3X
    min_critique_stake: 5_000_000,
    // 5 AP3X
    min_governance_endorsement: 5_000_000,
    // 5 AP3X
    min_review_window: 259_200_000,
    // ~3 days in POSIX ms
    max_review_window: 2_419_200_000,
    // ~28 days in POSIX ms
    max_amendments: 5,
    max_active_proposals: 3,
    proposal_cooldown: 86_400_000,
    // ~24 hours in POSIX ms
    proposer_reward_share: 7_000,
    // 70% in basis points
    critic_reward_share: 2_000,
    // 20% in basis points
    protocol_fee_rate: 1_000,
    // 10% in basis points
    min_adoption_reward: 50_000_000,
    // 50 AP3X
    max_adoption_reward: 500_000_000,
    // 500 AP3X
    max_incorporated_critiques: 5,
    max_treasury_request: 10_000_000_000,
    // 10,000 AP3X
    emergency_stake_multiplier: 5_000,
    // 5x in basis points
    emergency_review_window: 43_200_000,
    // ~12 hours in POSIX ms
    param_execution_delay: 604_800_000,
    // ~7 days in POSIX ms (Phase 1.2)
    min_prediction_pool: 100_000_000,
    // 100 AP3X (Phase 1.2)
    credibility_pool_low_threshold: 500_000_000,
    // 500 AP3X (Phase 1.2)
    credibility_pool_critical_threshold: 200_000_000,
    // 200 AP3X (Phase 1.2)
  }
}
