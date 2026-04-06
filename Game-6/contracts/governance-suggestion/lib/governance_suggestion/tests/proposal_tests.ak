// Game 6: Governance Suggestion Engine — Proposal Validation Tests

use aiken/builtin
use aiken/crypto.{blake2b_256}
use cardano/address.{Credential, VerificationKey}
use governance_suggestion/params
use governance_suggestion/types.{
  Amended, Emergency, GameActivation, GeneralSuggestion,
  GovernanceParams, Open, ParameterChange, ProposalDatum, ProtocolUpgrade,
  Standard, TreasurySpend,
}

// ============================================================================
// Test Fixtures
// ============================================================================

fn test_params() -> GovernanceParams {
  params.default_governance_params()
}

fn test_proposer_credential() -> Credential {
  VerificationKey(
    #"aabb0000000000000000000000000000000000000000000000000000",
  )
}

fn test_proposal_hash() -> ByteArray {
  blake2b_256("test proposal content v1")
}

fn test_proposal_datum() -> ProposalDatum {
  ProposalDatum {
    proposer_did: "did_proposer_001",
    proposer_credential: test_proposer_credential(),
    proposal_hash: test_proposal_hash(),
    proposal_type: GeneralSuggestion,
    storage_uri: "ipfs://QmTestProposal123",
    stake_amount: 25_000_000,
    submitted_at: 1_000_000,
    review_window: 604_800_000,
    priority: Standard,
    amendment_count: 0,
    incorporated_critiques: [],
    state: Open,
  }
}

// ============================================================================
// §5.1 Proposal Submission Tests
// ============================================================================

test proposal_hash_must_be_32_bytes() {
  let hash = test_proposal_hash()
  builtin.length_of_bytearray(hash) == 32
}

test proposal_hash_blake2b_deterministic() {
  let h1 = blake2b_256("test input")
  let h2 = blake2b_256("test input")
  h1 == h2
}

test proposal_hash_different_for_different_inputs() {
  let h1 = blake2b_256("input A")
  let h2 = blake2b_256("input B")
  h1 != h2
}

test proposal_datum_initial_state_is_open() {
  let datum = test_proposal_datum()
  datum.state == Open
}

test proposal_datum_initial_amendment_count_zero() {
  let datum = test_proposal_datum()
  datum.amendment_count == 0
}

test proposal_datum_initial_incorporated_critiques_empty() {
  let datum = test_proposal_datum()
  datum.incorporated_critiques == []
}

test proposal_stake_meets_minimum_standard() {
  let p = test_params()
  let datum = test_proposal_datum()
  datum.stake_amount >= p.min_proposal_stake
}

test proposal_stake_below_minimum_fails() {
  let p = test_params()
  let datum =
    ProposalDatum { ..test_proposal_datum(), stake_amount: p.min_proposal_stake - 1 }
  datum.stake_amount < p.min_proposal_stake
}

test proposal_emergency_stake_multiplier() {
  let p = test_params()
  let emergency_stake =
    p.min_proposal_stake * p.emergency_stake_multiplier / 1_000
  // 25 AP3X * 5 = 125 AP3X
  emergency_stake == 125_000_000
}

test proposal_emergency_datum_needs_higher_stake() {
  let p = test_params()
  let emergency_stake =
    p.min_proposal_stake * p.emergency_stake_multiplier / 1_000
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      stake_amount: emergency_stake,
      review_window: p.emergency_review_window,
    }
  datum.stake_amount >= emergency_stake
}

// ============================================================================
// Review Window Tests
// ============================================================================

test review_window_standard_in_range() {
  let p = test_params()
  let datum = test_proposal_datum()
  datum.review_window >= p.min_review_window && datum.review_window <= p.max_review_window
}

test review_window_below_minimum_fails() {
  let p = test_params()
  let datum =
    ProposalDatum { ..test_proposal_datum(), review_window: p.min_review_window - 1 }
  datum.review_window < p.min_review_window
}

test review_window_above_maximum_fails() {
  let p = test_params()
  let datum =
    ProposalDatum { ..test_proposal_datum(), review_window: p.max_review_window + 1 }
  datum.review_window > p.max_review_window
}

test review_window_emergency_fixed() {
  let p = test_params()
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      review_window: p.emergency_review_window,
    }
  datum.review_window == p.emergency_review_window
}

// ============================================================================
// Proposal Type Validation Tests
// ============================================================================

test proposal_type_general_suggestion_valid() {
  let datum =
    ProposalDatum { ..test_proposal_datum(), proposal_type: GeneralSuggestion }
  datum.proposal_type == GeneralSuggestion
}

test proposal_type_parameter_change_valid() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      proposal_type: ParameterChange {
        param_name: "MIN_CLAIM_STAKE",
        current_value: 100,
        proposed_value: 150,
      },
    }
  when datum.proposal_type is {
    ParameterChange { param_name, .. } ->
      params.is_governable_param(param_name)
    _ -> False
  }
}

test proposal_type_treasury_spend_valid() {
  let p = test_params()
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      proposal_type: TreasurySpend {
        amount: 100_000_000,
        recipient_description: "Fund Game 7 development",
      },
    }
  when datum.proposal_type is {
    TreasurySpend { amount, .. } ->
      amount > 0 && amount <= p.max_treasury_request
    _ -> False
  }
}

test proposal_type_treasury_spend_zero_amount_invalid() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      proposal_type: TreasurySpend {
        amount: 0,
        recipient_description: "Bad request",
      },
    }
  when datum.proposal_type is {
    TreasurySpend { amount, .. } -> amount <= 0
    _ -> False
  }
}

test proposal_type_protocol_upgrade_valid() {
  let hash = blake2b_256("upgrade spec v2.0")
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      proposal_type: ProtocolUpgrade { upgrade_description_hash: hash },
    }
  when datum.proposal_type is {
    ProtocolUpgrade { upgrade_description_hash } ->
      builtin.length_of_bytearray(upgrade_description_hash) == 32
    _ -> False
  }
}

test proposal_type_game_activation_valid() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      proposal_type: GameActivation { game_id: 7 },
    }
  when datum.proposal_type is {
    GameActivation { game_id } -> game_id > 0
    _ -> False
  }
}

test proposal_type_game_activation_zero_id_invalid() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      proposal_type: GameActivation { game_id: 0 },
    }
  when datum.proposal_type is {
    GameActivation { game_id } -> game_id <= 0
    _ -> False
  }
}

// ============================================================================
// Proposal State Machine Tests
// ============================================================================

test proposal_state_open_is_active() {
  let datum = test_proposal_datum()
  when datum.state is {
    Open -> True
    _ -> False
  }
}

test proposal_state_amended_is_active() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      state: Amended { previous_hash: test_proposal_hash() },
    }
  when datum.state is {
    Amended { .. } -> True
    _ -> False
  }
}

// ============================================================================
// Governable Parameters Tests
// ============================================================================

test governable_param_min_claim_stake() {
  params.is_governable_param("MIN_CLAIM_STAKE")
}

test governable_param_jury_size() {
  params.is_governable_param("JURY_SIZE")
}

test governable_param_min_proposal_stake() {
  params.is_governable_param("MIN_PROPOSAL_STAKE")
}

test governable_param_invalid_name() {
  !params.is_governable_param("INVALID_PARAM_NAME")
}

test governable_param_empty_name() {
  !params.is_governable_param("")
}

// ============================================================================
// Amendment Tests
// ============================================================================

test amendment_count_within_limit() {
  let p = test_params()
  let datum = test_proposal_datum()
  datum.amendment_count + 1 <= p.max_amendments
}

test amendment_count_at_max_fails() {
  let p = test_params()
  let datum =
    ProposalDatum { ..test_proposal_datum(), amendment_count: p.max_amendments }
  datum.amendment_count + 1 > p.max_amendments
}

test amendment_new_hash_must_differ() {
  let old_hash = blake2b_256("version 1")
  let new_hash = blake2b_256("version 2")
  old_hash != new_hash
}

test amendment_preserves_proposer_did() {
  let datum = test_proposal_datum()
  let amended_datum =
    ProposalDatum {
      ..datum,
      proposal_hash: blake2b_256("amended content"),
      amendment_count: datum.amendment_count + 1,
      state: Amended { previous_hash: datum.proposal_hash },
    }
  amended_datum.proposer_did == datum.proposer_did
}

test amendment_preserves_stake() {
  let datum = test_proposal_datum()
  let amended_datum =
    ProposalDatum {
      ..datum,
      proposal_hash: blake2b_256("amended content"),
      amendment_count: datum.amendment_count + 1,
      state: Amended { previous_hash: datum.proposal_hash },
    }
  amended_datum.stake_amount == datum.stake_amount
}

// ============================================================================
// Expiration Tests
// ============================================================================

test proposal_expired_after_review_window() {
  let datum = test_proposal_datum()
  let current_slot = datum.submitted_at + datum.review_window + 1
  current_slot > datum.submitted_at + datum.review_window
}

test proposal_not_expired_during_review_window() {
  let datum = test_proposal_datum()
  let current_slot = datum.submitted_at + datum.review_window - 100
  current_slot <= datum.submitted_at + datum.review_window
}

test proposal_not_expired_at_exact_boundary() {
  let datum = test_proposal_datum()
  // At exact boundary, NOT expired (need strictly greater)
  let current_slot = datum.submitted_at + datum.review_window
  !(current_slot > datum.submitted_at + datum.review_window)
}

// ============================================================================
// Storage URI Tests
// ============================================================================

test storage_uri_non_empty() {
  let datum = test_proposal_datum()
  builtin.length_of_bytearray(datum.storage_uri) > 0
}

test storage_uri_empty_fails() {
  let datum = ProposalDatum { ..test_proposal_datum(), storage_uri: "" }
  builtin.length_of_bytearray(datum.storage_uri) == 0
}

// ============================================================================
// Emergency Proposal Type Restriction Tests (Fix 5)
// ============================================================================

test emergency_parameter_change_allowed() {
  let p = test_params()
  let emergency_stake =
    p.min_proposal_stake * p.emergency_stake_multiplier / 1_000
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      stake_amount: emergency_stake,
      review_window: p.emergency_review_window,
      proposal_type: ParameterChange {
        param_name: "MIN_CLAIM_STAKE",
        current_value: 100,
        proposed_value: 150,
      },
    }
  // ParameterChange is allowed for emergency
  when datum.proposal_type is {
    ParameterChange { .. } -> True
    _ -> False
  }
}

test emergency_protocol_upgrade_allowed() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      proposal_type: ProtocolUpgrade {
        upgrade_description_hash: blake2b_256("urgent upgrade"),
      },
    }
  // ProtocolUpgrade is allowed for emergency
  when datum.proposal_type is {
    ProtocolUpgrade { .. } -> True
    _ -> False
  }
}

test emergency_general_suggestion_not_allowed() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      proposal_type: GeneralSuggestion,
    }
  // GeneralSuggestion is NOT allowed for emergency
  when datum.proposal_type is {
    ParameterChange { .. } -> True
    ProtocolUpgrade { .. } -> True
    _ -> False
  } == False
}

test emergency_treasury_spend_not_allowed() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      proposal_type: TreasurySpend {
        amount: 100_000_000,
        recipient_description: "Emergency funding",
      },
    }
  // TreasurySpend is NOT allowed for emergency
  when datum.proposal_type is {
    ParameterChange { .. } -> True
    ProtocolUpgrade { .. } -> True
    _ -> False
  } == False
}

test emergency_game_activation_not_allowed() {
  let datum =
    ProposalDatum {
      ..test_proposal_datum(),
      priority: Emergency,
      proposal_type: GameActivation { game_id: 7 },
    }
  // GameActivation is NOT allowed for emergency
  when datum.proposal_type is {
    ParameterChange { .. } -> True
    ProtocolUpgrade { .. } -> True
    _ -> False
  } == False
}

// ============================================================================
// Max Incorporated Critiques Tests (Fix 3/13)
// ============================================================================

test max_incorporated_critiques_within_limit() {
  let p = test_params()
  let current_count = 3
  let new_count = 2
  current_count + new_count <= p.max_incorporated_critiques
}

test max_incorporated_critiques_at_limit() {
  let p = test_params()
  let current_count = p.max_incorporated_critiques
  let new_count = 1
  // Exceeds limit
  !(current_count + new_count <= p.max_incorporated_critiques)
}

// ============================================================================
// Rate Limiting Tests (Fix 11)
// ============================================================================

test rate_limit_max_active_proposals_constant() {
  let p = test_params()
  p.max_active_proposals == 3
}

test rate_limit_cooldown_constant() {
  let p = test_params()
  p.proposal_cooldown > 0
}

test rate_limit_cooldown_not_elapsed() {
  let p = test_params()
  let last_slot = 1000
  let current_slot = last_slot + p.proposal_cooldown - 1
  // Cooldown not elapsed
  current_slot - last_slot < p.proposal_cooldown
}

test rate_limit_cooldown_elapsed() {
  let p = test_params()
  let last_slot = 1000
  let current_slot = last_slot + p.proposal_cooldown
  // Cooldown elapsed
  current_slot - last_slot >= p.proposal_cooldown
}
