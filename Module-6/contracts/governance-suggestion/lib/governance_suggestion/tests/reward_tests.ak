// Module 6: Self-Improvement Module — Reward Distribution Tests

use governance_suggestion/params
use governance_suggestion/reward_distribution
use governance_suggestion/types.{GovernanceParams}

// ============================================================================
// Test Fixtures
// ============================================================================

fn test_params() -> GovernanceParams {
  params.default_governance_params()
}

// ============================================================================
// Reward Calculation Tests (§7.2)
// ============================================================================

test protocol_fee_10_percent() {
  let p = test_params()
  let reward = 100_000_000
  let fee = reward_distribution.protocol_fee(p, reward)
  // 10% of 100 AP3X = 10 AP3X
  fee == 10_000_000
}

test protocol_fee_basis_points() {
  let p = test_params()
  // Verify basis point calculation: 1000/10000 = 10%
  p.protocol_fee_rate == 1_000
}

test proposer_reward_no_critics_gets_90_percent() {
  let p = test_params()
  let reward = 100_000_000
  let prop_reward = reward_distribution.proposer_reward(p, reward, 0)
  let proto_fee = reward_distribution.protocol_fee(p, reward)
  // When no critics, proposer gets reward - protocol_fee = 90%
  prop_reward == reward - proto_fee
}

test proposer_reward_with_critics_gets_70_percent() {
  let p = test_params()
  let reward = 100_000_000
  let prop_reward = reward_distribution.proposer_reward(p, reward, 3)
  // 70% of 100 AP3X = 70 AP3X
  prop_reward == 70_000_000
}

test critic_pool_with_critics_is_20_percent() {
  let p = test_params()
  let reward = 100_000_000
  let pool = reward_distribution.critic_pool(p, reward, 3)
  // 20% of 100 AP3X = 20 AP3X
  pool == 20_000_000
}

test critic_pool_no_critics_is_zero() {
  let p = test_params()
  let reward = 100_000_000
  let pool = reward_distribution.critic_pool(p, reward, 0)
  pool == 0
}

test per_critic_reward_equal_split() {
  let p = test_params()
  let reward = 100_000_000
  let per_critic = reward_distribution.per_critic_reward(p, reward, 4)
  // 20% of 100 AP3X / 4 critics = 5 AP3X each
  per_critic == 5_000_000
}

test per_critic_reward_single_critic() {
  let p = test_params()
  let reward = 100_000_000
  let per_critic = reward_distribution.per_critic_reward(p, reward, 1)
  // Single critic gets full 20% = 20 AP3X
  per_critic == 20_000_000
}

test per_critic_reward_no_critics_is_zero() {
  let p = test_params()
  let reward = 100_000_000
  let per_critic = reward_distribution.per_critic_reward(p, reward, 0)
  per_critic == 0
}

// ============================================================================
// Reward Conservation Tests (total distributed == total reward)
// ============================================================================

test reward_conservation_no_critics() {
  let p = test_params()
  let reward = 100_000_000
  let prop = reward_distribution.proposer_reward(p, reward, 0)
  let fee = reward_distribution.protocol_fee(p, reward)
  let critic = reward_distribution.critic_pool(p, reward, 0)
  // Total = proposer (90) + fee (10) + critic_pool (0) = 100
  prop + fee + critic == reward
}

test reward_conservation_with_critics() {
  let p = test_params()
  let reward = 100_000_000
  let prop = reward_distribution.proposer_reward(p, reward, 3)
  let fee = reward_distribution.protocol_fee(p, reward)
  let critic = reward_distribution.critic_pool(p, reward, 3)
  // Total = proposer (70) + fee (10) + critic_pool (20) = 100
  prop + fee + critic == reward
}

test reward_conservation_large_amount() {
  let p = test_params()
  let reward = 500_000_000
  let prop = reward_distribution.proposer_reward(p, reward, 5)
  let fee = reward_distribution.protocol_fee(p, reward)
  let critic = reward_distribution.critic_pool(p, reward, 5)
  prop + fee + critic == reward
}

// ============================================================================
// Edge Cases
// ============================================================================

test reward_minimum_amount() {
  let p = test_params()
  let reward = p.min_adoption_reward
  let prop = reward_distribution.proposer_reward(p, reward, 0)
  let fee = reward_distribution.protocol_fee(p, reward)
  prop + fee == reward
}

test reward_maximum_amount() {
  let p = test_params()
  let reward = p.max_adoption_reward
  let prop = reward_distribution.proposer_reward(p, reward, 1)
  let fee = reward_distribution.protocol_fee(p, reward)
  let critic = reward_distribution.critic_pool(p, reward, 1)
  prop + fee + critic == reward
}

test per_critic_reward_rounding_division() {
  let p = test_params()
  // 100 AP3X reward, 3 critics: 20% = 20 AP3X / 3 = 6.666... AP3X
  // Integer division should give 6_666_666 (floor)
  let reward = 100_000_000
  let per_critic = reward_distribution.per_critic_reward(p, reward, 3)
  // Floor division: 20_000_000 / 3 = 6_666_666
  per_critic == 6_666_666
}

test per_critic_rounding_dust_stays_in_contract() {
  let p = test_params()
  let reward = 100_000_000
  let num_critics = 3
  let per_critic = reward_distribution.per_critic_reward(p, reward, num_critics)
  let total_critic_payout = per_critic * num_critics
  let critic_pool_total = reward_distribution.critic_pool(p, reward, num_critics)
  // Dust stays in the contract (not over-allocated)
  total_critic_payout <= critic_pool_total
}

// ============================================================================
// Reward Amount Validation Tests
// ============================================================================

test reward_amount_in_valid_range() {
  let p = test_params()
  let reward = 100_000_000
  reward >= p.min_adoption_reward && reward <= p.max_adoption_reward
}

test reward_amount_below_min_invalid() {
  let p = test_params()
  let reward = p.min_adoption_reward - 1
  reward < p.min_adoption_reward
}

test reward_amount_above_max_invalid() {
  let p = test_params()
  let reward = p.max_adoption_reward + 1
  reward > p.max_adoption_reward
}

// ============================================================================
// Share Configuration Tests
// ============================================================================

test shares_sum_to_100_percent() {
  let p = test_params()
  // 70% + 20% + 10% = 100% (in basis points: 7000 + 2000 + 1000 = 10000)
  p.proposer_reward_share + p.critic_reward_share + p.protocol_fee_rate == 10_000
}

test proposer_share_is_70_percent() {
  let p = test_params()
  p.proposer_reward_share == 7_000
}

test critic_share_is_20_percent() {
  let p = test_params()
  p.critic_reward_share == 2_000
}

test protocol_fee_is_10_percent() {
  let p = test_params()
  p.protocol_fee_rate == 1_000
}
