// Module 6: Governance Suggestion Engine — Reward Distribution
// Spec reference: §7.2, §11.5

use aiken/collection/list
use cardano/address.{Credential, Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{Output}

use governance_suggestion/types.{GovernanceConfig, GovernanceParams}

// ============================================================================
// Reward Calculation
// ============================================================================

/// Calculate the proposer's share of the adoption reward.
/// If no critics are incorporated, proposer gets 90% (proposer_share + critic_share).
pub fn proposer_reward(
  params: GovernanceParams,
  reward_amount: Int,
  num_incorporated_critiques: Int,
) -> Int {
  if num_incorporated_critiques > 0 {
    reward_amount * params.proposer_reward_share / 10_000
  } else {
    // Proposer absorbs unallocated critic share
    reward_amount - protocol_fee(params, reward_amount)
  }
}

/// Calculate the protocol fee from the adoption reward.
pub fn protocol_fee(params: GovernanceParams, reward_amount: Int) -> Int {
  reward_amount * params.protocol_fee_rate / 10_000
}

/// Calculate the total critic pool.
pub fn critic_pool(
  params: GovernanceParams,
  reward_amount: Int,
  num_incorporated_critiques: Int,
) -> Int {
  if num_incorporated_critiques > 0 {
    reward_amount * params.critic_reward_share / 10_000
  } else {
    0
  }
}

/// Calculate the per-critic reward (equal split among incorporated critics).
pub fn per_critic_reward(
  params: GovernanceParams,
  reward_amount: Int,
  num_incorporated_critiques: Int,
) -> Int {
  if num_incorporated_critiques > 0 {
    critic_pool(params, reward_amount, num_incorporated_critiques) / num_incorporated_critiques
  } else {
    0
  }
}

// ============================================================================
// Reward Distribution Validation
// ============================================================================

/// Validate that the reward distribution outputs are correct.
/// Called during AdoptProposal to verify all payouts.
pub fn validate_reward_distribution(
  config: GovernanceConfig,
  params: GovernanceParams,
  reward_amount: Int,
  num_incorporated_critiques: Int,
  proposer_credential: Credential,
  critic_credentials: List<Credential>,
  proposer_stake: Int,
  outputs: List<Output>,
) -> Bool {
  let prop_reward = proposer_reward(params, reward_amount, num_incorporated_critiques)
  let proto_fee = protocol_fee(params, reward_amount)
  let critic_reward = per_critic_reward(params, reward_amount, num_incorporated_critiques)

  // Verify proposer receives reward + stake return
  let proposer_paid =
    verify_output_to_credential(
      outputs,
      proposer_credential,
      prop_reward + proposer_stake,
    )

  // Verify protocol treasury receives fee
  let treasury_paid =
    verify_output_to_script(outputs, config.governance_treasury_hash, proto_fee)

  // Verify each incorporated critic receives their share
  let critics_paid =
    if num_incorporated_critiques > 0 {
      list.all(
        critic_credentials,
        fn(cred) {
          verify_output_to_credential(outputs, cred, critic_reward)
        },
      )
    } else {
      True
    }

  proposer_paid && treasury_paid && critics_paid
}

// ============================================================================
// Output Verification Helpers
// ============================================================================

/// Verify that an output exists paying at least `min_amount` lovelace to a credential.
fn verify_output_to_credential(
  outputs: List<Output>,
  credential: Credential,
  min_amount: Int,
) -> Bool {
  list.any(
    outputs,
    fn(output) {
      output.address.payment_credential == credential && assets.lovelace_of(
        output.value,
      ) >= min_amount
    },
  )
}

/// Verify that an output exists paying at least `min_amount` lovelace to a script hash.
fn verify_output_to_script(
  outputs: List<Output>,
  script_hash: ByteArray,
  min_amount: Int,
) -> Bool {
  list.any(
    outputs,
    fn(output) {
      when output.address.payment_credential is {
        ScriptCredential(hash) ->
          hash == script_hash && assets.lovelace_of(output.value) >= min_amount
        _ -> False
      }
    },
  )
}
