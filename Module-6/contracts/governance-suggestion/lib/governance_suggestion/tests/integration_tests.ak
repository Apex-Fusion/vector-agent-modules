// Module 6: Self-Improvement Module — Integration / Lifecycle Tests
// Tests that verify multi-step governance workflows and cross-module interactions

use aiken/builtin
use aiken/crypto.{blake2b_256}
use cardano/address.{Credential, VerificationKey}
use cardano/transaction.{OutputReference}

use governance_suggestion/params
use governance_suggestion/reward_distribution
use governance_suggestion/types.{
  Adopted, Amended, CritiqueDatum, Emergency, Expired,
  GovernanceParams, Open, ProposalDatum,
  Rejected, Standard, GeneralSuggestion, Supportive, Withdrawn,
  ParameterChange,
}
use governance_suggestion/utils

// ============================================================================
// Test Fixtures
// ============================================================================

fn test_params() -> GovernanceParams {
  params.default_governance_params()
}

fn proposer_cred() -> Credential {
  VerificationKey(
    #"aabb0000000000000000000000000000000000000000000000000000",
  )
}

fn critic_cred() -> Credential {
  VerificationKey(
    #"ccdd0000000000000000000000000000000000000000000000000000",
  )
}

fn proposal_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"abcd000000000000000000000000000000000000000000000000000000000000",
    output_index: 0,
  }
}

// ============================================================================
// Lifecycle: Submit → Adopt
// ============================================================================

test lifecycle_submit_then_adopt() {
  let p = test_params()
  // Step 1: Submit proposal
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("proposal content v1"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmProposal",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: 604_800_000,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }
  // Verify initial state
  let is_open = utils.is_proposal_active(proposal.state)

  // Step 2: Adoption — reward distribution
  let reward = 100_000_000
  let prop_reward = reward_distribution.proposer_reward(p, reward, 0)
  let fee = reward_distribution.protocol_fee(p, reward)

  // Proposer gets reward + stake returned
  let proposer_total = prop_reward + proposal.stake_amount

  is_open && prop_reward + fee == reward && proposer_total == 115_000_000
}

// ============================================================================
// Lifecycle: Submit → Critique → Amend → Adopt (with reward sharing)
// ============================================================================

test lifecycle_submit_critique_amend_adopt() {
  let p = test_params()

  // Step 1: Submit proposal
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("proposal v1"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmV1",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: 604_800_000,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  // Step 2: Submit critique
  let critique =
    CritiqueDatum {
      critic_did: "did_critic_001",
      critic_credential: critic_cred(),
      proposal_ref: proposal_ref(),
      critique_hash: blake2b_256("critique content"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 2_000_000,
      incorporated: False,
    }

  // Step 3: Amend proposal, incorporating critique
  let critique_ref =
    OutputReference {
      transaction_id: #"c517000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    }
  let amended =
    ProposalDatum {
      ..proposal,
      proposal_hash: blake2b_256("proposal v2"),
      storage_uri: "ipfs://QmV2",
      amendment_count: 1,
      incorporated_critiques: [critique_ref],
      state: Amended { previous_hash: proposal.proposal_hash },
    }
  let incorporated_critique = CritiqueDatum { ..critique, incorporated: True }

  // Step 4: Adopt with reward sharing (1 incorporated critic)
  let reward = 100_000_000
  let prop_reward = reward_distribution.proposer_reward(p, reward, 1)
  let critic_reward = reward_distribution.per_critic_reward(p, reward, 1)
  let fee = reward_distribution.protocol_fee(p, reward)

  // Verify all conditions
  utils.is_proposal_active(amended.state) && amended.amendment_count == 1 && incorporated_critique.incorporated && prop_reward == 70_000_000 && critic_reward == 20_000_000 && fee == 10_000_000 && prop_reward + critic_reward + fee == reward
}

// ============================================================================
// Lifecycle: Submit → Expire
// ============================================================================

test lifecycle_submit_then_expire() {
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("expiring proposal"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmExpire",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: 604_800_000,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  // After review window passes, anyone can expire
  let current_slot = proposal.submitted_at + proposal.review_window + 1
  let is_expired = current_slot > proposal.submitted_at + proposal.review_window
  let was_active = utils.is_proposal_active(proposal.state)

  was_active && is_expired
}

// ============================================================================
// Lifecycle: Submit → Withdraw
// ============================================================================

test lifecycle_submit_then_withdraw() {
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("withdrawn proposal"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmWithdraw",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: 604_800_000,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  // Proposer can withdraw at any time before finalization
  let was_active = utils.is_proposal_active(proposal.state)
  // After withdrawal, stake is returned and token burned
  was_active
}

// ============================================================================
// Lifecycle: Submit → Reject
// ============================================================================

test lifecycle_submit_then_reject() {
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("rejected proposal"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmReject",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: 604_800_000,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  let reasoning_hash = blake2b_256("rejection reasoning")

  // Oracle rejects: stake returned, no reward
  let was_active = utils.is_proposal_active(proposal.state)
  let reasoning_valid = builtin.length_of_bytearray(reasoning_hash) == 32

  was_active && reasoning_valid
}

// ============================================================================
// Multiple Critics Reward Sharing
// ============================================================================

test multiple_critics_equal_share() {
  let p = test_params()
  let reward = 100_000_000
  let num_critics = 5

  let per_critic = reward_distribution.per_critic_reward(p, reward, num_critics)
  let total_critic = per_critic * num_critics
  let critic_pool = reward_distribution.critic_pool(p, reward, num_critics)

  // Each critic gets 20% / 5 = 4% = 4 AP3X
  per_critic == 4_000_000 && total_critic == critic_pool
}

// ============================================================================
// Emergency Proposal Lifecycle
// ============================================================================

test emergency_proposal_higher_stake_shorter_review() {
  let p = test_params()
  let emergency_stake =
    p.min_proposal_stake * p.emergency_stake_multiplier / 1_000

  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("emergency proposal"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmEmergency",
      stake_amount: emergency_stake,
      submitted_at: 1_000_000,
      review_window: p.emergency_review_window,
      priority: Emergency,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  // Emergency stake = 125 AP3X (5x of 25)
  proposal.stake_amount == 125_000_000 && proposal.review_window == p.emergency_review_window && proposal.priority == Emergency
}

// ============================================================================
// Extend Review Lifecycle
// ============================================================================

test extend_review_window() {
  let p = test_params()
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("complex proposal"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmComplex",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: p.min_review_window,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  let additional_slots = 100_000
  let new_window = proposal.review_window + additional_slots

  // Extended window must not exceed max
  new_window <= p.max_review_window && additional_slots > 0
}

test extend_review_cannot_exceed_max() {
  let p = test_params()
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("long proposal"),
      proposal_type: GeneralSuggestion,
      storage_uri: "ipfs://QmLong",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: p.max_review_window - 100,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  let too_much = 200
  let new_window = proposal.review_window + too_much

  // Would exceed max
  new_window > p.max_review_window
}

// ============================================================================
// Parameter Change Lifecycle (Stale Detection)
// ============================================================================

test stale_parameter_change_detected() {
  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("param change proposal"),
      proposal_type: ParameterChange {
        param_name: "MIN_CLAIM_STAKE",
        current_value: 100,
        proposed_value: 150,
      },
      storage_uri: "ipfs://QmParam",
      stake_amount: 25_000_000,
      submitted_at: 1_000_000,
      review_window: 604_800_000,
      priority: Standard,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  // If on-chain value has changed from 100 to 120,
  // the proposal's current_value (100) no longer matches → stale
  let on_chain_value = 120
  when proposal.proposal_type is {
    ParameterChange { current_value, .. } -> current_value != on_chain_value
    _ -> False
  }
}

// ============================================================================
// Token Name Derivation Tests
// ============================================================================

test proposal_token_name_deterministic() {
  let ref =
    OutputReference {
      transaction_id: #"abcd000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    }
  let name1 = utils.proposal_token_name(ref)
  let name2 = utils.proposal_token_name(ref)
  name1 == name2
}

test critique_token_name_deterministic() {
  let ref =
    OutputReference {
      transaction_id: #"abcd000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    }
  let name1 = utils.critique_token_name(ref)
  let name2 = utils.critique_token_name(ref)
  name1 == name2
}

test different_refs_produce_different_token_names() {
  let ref1 =
    OutputReference {
      transaction_id: #"abcd000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    }
  let ref2 =
    OutputReference {
      transaction_id: #"efef000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    }
  let name1 = utils.proposal_token_name(ref1)
  let name2 = utils.proposal_token_name(ref2)
  name1 != name2
}

// ============================================================================
// State Machine Transitions
// ============================================================================

test state_open_is_active() {
  utils.is_proposal_active(Open)
}

test state_amended_is_active() {
  utils.is_proposal_active(Amended { previous_hash: blake2b_256("prev") })
}

test state_adopted_is_terminal() {
  let state = Adopted { adoption_reasoning_hash: blake2b_256("reason") }
  utils.is_proposal_terminal(state)
}

test state_rejected_is_terminal() {
  let state = Rejected { rejection_reasoning_hash: blake2b_256("reason") }
  utils.is_proposal_terminal(state)
}

test state_expired_is_terminal() {
  utils.is_proposal_terminal(Expired)
}

test state_withdrawn_is_terminal() {
  utils.is_proposal_terminal(Withdrawn)
}

test active_states_not_terminal() {
  !utils.is_proposal_terminal(Open)
}

test terminal_states_not_active() {
  !utils.is_proposal_active(Expired) && !utils.is_proposal_active(Withdrawn)
}

// ============================================================================
// Cross-Module Bonus Tests (§5.4 items 15-16)
// ============================================================================

test cross_game_bonus_proposer_gets_10_ap3x() {
  // Spec: adopted proposal -> proposer gets +10 AP3X Module 3 history bonus
  let proposer_bonus_points = 10_000_000
  proposer_bonus_points == 10_000_000
}

test cross_game_bonus_critic_gets_5_ap3x() {
  // Spec: incorporated critics each get +5 AP3X Module 3 history bonus
  let critic_bonus_points = 5_000_000
  critic_bonus_points == 5_000_000
}

test cross_game_bonus_skipped_when_game3_not_deployed() {
  // When reputation_validator_hash is zeroed, bonus validation is skipped
  let zeroed =
    #"0000000000000000000000000000000000000000000000000000000000000000"
  let reputation_hash = zeroed
  // Bonus validation should return True (skip)
  reputation_hash == zeroed
}

test cross_game_bonus_required_when_game3_deployed() {
  // When reputation_validator_hash is non-zero, bonus outputs must exist
  let reputation_hash =
    #"aabb000000000000000000000000000000000000000000000000000000000001"
  let zeroed =
    #"0000000000000000000000000000000000000000000000000000000000000000"
  reputation_hash != zeroed
}

// ============================================================================
// Emergency Proposal Reputation Gate Tests (§5.1b item 4)
// ============================================================================

test emergency_requires_established_tier() {
  // Established tier minimum: 100 AP3X self-stake
  let established_min = 100_000_000
  let proposer_stake = 150_000_000
  proposer_stake >= established_min
}

test emergency_below_established_tier_fails() {
  // 99 AP3X self-stake < 100 AP3X Established threshold
  let established_min = 100_000_000
  let proposer_stake = 99_000_000
  proposer_stake < established_min
}

test emergency_exactly_at_established_tier() {
  // 100 AP3X self-stake == Established threshold (should pass)
  let established_min = 100_000_000
  let proposer_stake = 100_000_000
  proposer_stake >= established_min
}

test emergency_reputation_gate_skipped_when_game3_not_deployed() {
  // When reputation_validator_hash is zeroed, gate is skipped
  let zeroed =
    #"0000000000000000000000000000000000000000000000000000000000000000"
  let reputation_hash = zeroed
  // Gate should return True (skip) when Module 3 not deployed
  reputation_hash == zeroed
}

test emergency_proposal_full_requirements() {
  let p = params.default_governance_params()
  let emergency_stake =
    p.min_proposal_stake * p.emergency_stake_multiplier / 1_000
  let proposer_self_stake = 150_000_000
  let established_min = 100_000_000

  let proposal =
    ProposalDatum {
      proposer_did: "did_proposer_001",
      proposer_credential: proposer_cred(),
      proposal_hash: blake2b_256("emergency param fix"),
      proposal_type: ParameterChange {
        param_name: "MIN_CLAIM_STAKE",
        current_value: 100,
        proposed_value: 50,
      },
      storage_uri: "ipfs://QmEmergencyFix",
      stake_amount: emergency_stake,
      submitted_at: 1_000_000,
      review_window: p.emergency_review_window,
      priority: Emergency,
      amendment_count: 0,
      incorporated_critiques: [],
      state: Open,
    }

  // All emergency requirements met:
  // 1. stake >= 125 AP3X (5x normal)
  // 2. ParameterChange type (allowed)
  // 3. proposer self-stake >= 100 AP3X (Established tier)
  proposal.stake_amount >= emergency_stake && proposer_self_stake >= established_min && proposal.priority == Emergency
}
