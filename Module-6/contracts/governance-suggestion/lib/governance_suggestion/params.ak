// Module 6: Self-Improvement Module — Parameter Reading
// Read GovernanceParams from reference input UTXO

use cardano/transaction.{Input}

use governance_suggestion/types.{GovernanceConfig, GovernanceParams}

use shared/record_auth

// ============================================================================
// GovernanceParams Reading
// ============================================================================

/// Read GovernanceParams from the params record: the reference input at the
/// governance params holder that holds the `params` record token
/// (config.record_token_policy). Fails if no such reference input exists.
pub fn read_governance_params(
  config: GovernanceConfig,
  reference_inputs: List<Input>,
) -> GovernanceParams {
  expect Some(raw_datum) =
    record_auth.find_record(
      reference_inputs,
      config.governance_params_hash,
      config.record_token_policy,
      record_auth.params_token_name,
    )
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

/// The value that the GovernanceParams record holds for a Module-6
/// parameter name, or None. 17 names map to the first 17 fields of
/// GovernanceParams. The other governable names (Module 1, Module 3, the
/// treasury batch names, ADOPTION_REWARD_BASE and GAME_ACTIVATION) have no
/// value in the record. ExpireStaleProposal compares this value with the
/// current_value of a ParameterChange proposal.
pub fn module6_param_value(
  params: GovernanceParams,
  param_name: ByteArray,
) -> Option<Int> {
  when param_name is {
    "MIN_PROPOSAL_STAKE" -> Some(params.min_proposal_stake)
    "MIN_CRITIQUE_STAKE" -> Some(params.min_critique_stake)
    "MIN_GOVERNANCE_ENDORSEMENT" -> Some(params.min_governance_endorsement)
    "MIN_REVIEW_WINDOW" -> Some(params.min_review_window)
    "MAX_REVIEW_WINDOW" -> Some(params.max_review_window)
    "MAX_AMENDMENTS" -> Some(params.max_amendments)
    "MAX_ACTIVE_PROPOSALS" -> Some(params.max_active_proposals)
    "COOLDOWN_BETWEEN_PROPOSALS" -> Some(params.proposal_cooldown)
    "PROPOSER_REWARD_SHARE" -> Some(params.proposer_reward_share)
    "CRITIC_REWARD_SHARE" -> Some(params.critic_reward_share)
    "PROTOCOL_FEE_RATE" -> Some(params.protocol_fee_rate)
    "MIN_ADOPTION_REWARD" -> Some(params.min_adoption_reward)
    "MAX_ADOPTION_REWARD" -> Some(params.max_adoption_reward)
    "MAX_INCORPORATED_CRITIQUES" -> Some(params.max_incorporated_critiques)
    "MAX_TREASURY_REQUEST" -> Some(params.max_treasury_request)
    "EMERGENCY_STAKE_MULTIPLIER" -> Some(params.emergency_stake_multiplier)
    "EMERGENCY_REVIEW_WINDOW" -> Some(params.emergency_review_window)
    _ -> None
  }
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
