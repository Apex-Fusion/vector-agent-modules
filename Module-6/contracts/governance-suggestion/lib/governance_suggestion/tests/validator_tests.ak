// Oracle: Self-Improvement Module — Validator-Level Mock Transaction Tests
//
// These tests call the validation functions from proposal_validation.ak and
// critique_validation.ak with fully constructed transaction arguments (inputs,
// outputs, reference inputs, mint values, signatories), simulating what the
// validator entry points pass through.
//
// Each test constructs the minimum viable transaction data for a specific
// action and verifies both positive (valid tx) and negative (invalid tx) cases.

use aiken/crypto.{blake2b_256}
use cardano/address.{Address, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, NoDatum, Output, OutputReference}

use governance_suggestion/critique_validation
use governance_suggestion/params
use governance_suggestion/proposal_validation
use governance_suggestion/types.{
  Amended, CritiqueDatum, Emergency, GeneralSuggestion, GovernanceEndorsementDatum,
  Open, ParameterChange, ProposalDatum, Standard, Supportive,
}
use governance_suggestion/utils

use governance_suggestion/tests/test_helpers.{
  activity_input, activity_output, activity_token_mint, critic_cred,
  critic_did, critic_key_hash, cross_refs_ref_input, default_proposal_datum,
  oracle_cred, oracle_key_hash, oracle_ref_input, oracle_ref_inputs,
  oracle_script_hash, params_ref_input, params_script_hash, payment_output,
  proposal_input, proposal_mint_hash, proposal_output, proposal_own_ref,
  proposal_ref_input, proposal_token_mint, proposal_validator_hash,
  proposer_cred, proposer_did, proposer_key_hash, protocol_params_ref_input,
  registry_ref_input, standard_ref_inputs, test_config, test_cross_refs,
  critique_validator_hash, critique_own_ref, critique_mint_hash,
}

// ============================================================================
// §5.1 SubmitProposal Tests
// ============================================================================

test submit_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  // Inputs: activity UTXO (first-time proposer uses InitActivity, so no activity input needed)
  // For first-time: no activity input, outputs must contain activity with count=1
  let reference_inputs = standard_ref_inputs(proposer_did)

  // Outputs: proposal UTXO + activity UTXO (count=1)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]

  // Mint: proposal token + activity token (first time)
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))

  let signatories = [proposer_key_hash]
  let inputs = []

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_missing_did() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  // No registry reference input — DID check will fail
  let reference_inputs = [cross_refs_ref_input(), params_ref_input()]

  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_insufficient_stake() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  // Stake below min (25 AP3X = 25_000_000)
  let datum =
    ProposalDatum { ..default_proposal_datum(), stake_amount: 1_000_000 }
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_bad_hash_length() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  // Hash is 16 bytes instead of 32
  let datum =
    ProposalDatum {
      ..default_proposal_datum(),
      proposal_hash: #"aabbccddaabbccddaabbccddaabbccdd",
    }
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_empty_uri() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = ProposalDatum { ..default_proposal_datum(), storage_uri: "" }
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_wrong_signer() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  // Wrong signer — critic instead of proposer
  let signatories = [critic_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_window_too_short() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  // Review window below min (259_200_000 = ~3 days)
  let datum =
    ProposalDatum { ..default_proposal_datum(), review_window: 1_000 }
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_window_too_long() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  // Review window above max (2_419_200_000 = ~28 days)
  let datum =
    ProposalDatum {
      ..default_proposal_datum(),
      review_window: 9_999_999_999,
    }
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

test submit_proposal_non_open_state() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum =
    ProposalDatum {
      ..default_proposal_datum(),
      state: Amended {
        previous_hash: blake2b_256("old"),
      },
    }
  let own_ref = proposal_own_ref()
  let current_slot = 2_000_000_000

  let reference_inputs = standard_ref_inputs(proposer_did)
  let outputs =
    [
      proposal_output(datum),
      activity_output(proposer_did, 1, current_slot),
    ]
  let mint =
    proposal_token_mint(own_ref, 1)
      |> assets.merge(activity_token_mint(proposer_did, 1))
  let signatories = [proposer_key_hash]

  proposal_validation.validate_submit_proposal(
    config,
    refs,
    gov_params,
    datum,
    current_slot,
    reference_inputs,
    [],
    outputs,
    signatories,
    mint,
    own_ref,
  )
}

// ============================================================================
// §5.2 WithdrawProposal Tests
// ============================================================================

test withdraw_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()

  // Inputs: proposal UTXO + activity UTXO
  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]

  // Outputs: stake return + activity UTXO (count decremented)
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]

  // Burn the proposal token
  let mint = proposal_token_mint(own_ref, -1)
  let signatories = [proposer_key_hash]

  proposal_validation.validate_withdraw_proposal(
    config,
    refs,
    datum,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

test withdraw_proposal_wrong_signer() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()

  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)
  // Wrong signer
  let signatories = [critic_key_hash]

  proposal_validation.validate_withdraw_proposal(
    config,
    refs,
    datum,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

// ============================================================================
// §5.6 ExpireProposal Tests
// ============================================================================

test expire_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()

  // Current slot past submitted_at + review_window
  let current_slot = datum.submitted_at + datum.review_window + 1

  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)

  proposal_validation.validate_expire_proposal(
    config,
    refs,
    datum,
    current_slot,
    inputs,
    outputs,
    mint,
  )
}

test expire_proposal_too_early() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()

  // Current slot BEFORE window expiry
  let current_slot = datum.submitted_at + datum.review_window - 1_000

  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)

  proposal_validation.validate_expire_proposal(
    config,
    refs,
    datum,
    current_slot,
    inputs,
    outputs,
    mint,
  )
}

// ============================================================================
// §5.4 AdoptProposal Tests
// ============================================================================

test adopt_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let reasoning_hash = blake2b_256("adoption reasoning")
  let reward_amount = 100_000_000
  // 100 AP3X

  let reference_inputs = oracle_ref_inputs(proposer_did)

  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]

  // Outputs: proposer reward + activity decremented
  // No critics incorporated → proposer gets 90% (reward - protocol fee)
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]

  let mint = proposal_token_mint(own_ref, -1)
  // Oracle must sign
  let signatories = [oracle_key_hash]

  proposal_validation.validate_adopt_proposal(
    config,
    refs,
    gov_params,
    datum,
    reasoning_hash,
    reward_amount,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

test adopt_proposal_no_oracle_sig() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let reasoning_hash = blake2b_256("adoption reasoning")
  let reward_amount = 100_000_000

  let reference_inputs = oracle_ref_inputs(proposer_did)

  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)
  // No oracle signature — regular proposer signed instead
  let signatories = [proposer_key_hash]

  proposal_validation.validate_adopt_proposal(
    config,
    refs,
    gov_params,
    datum,
    reasoning_hash,
    reward_amount,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

test adopt_proposal_reward_too_low() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let reasoning_hash = blake2b_256("adoption reasoning")
  // Below min_adoption_reward (50_000_000)
  let reward_amount = 1_000_000

  let reference_inputs = oracle_ref_inputs(proposer_did)
  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)
  let signatories = [oracle_key_hash]

  proposal_validation.validate_adopt_proposal(
    config,
    refs,
    gov_params,
    datum,
    reasoning_hash,
    reward_amount,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

test adopt_proposal_reward_too_high() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let reasoning_hash = blake2b_256("adoption reasoning")
  // Above max_adoption_reward (500_000_000)
  let reward_amount = 999_000_000

  let reference_inputs = oracle_ref_inputs(proposer_did)
  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)
  let signatories = [oracle_key_hash]

  proposal_validation.validate_adopt_proposal(
    config,
    refs,
    gov_params,
    datum,
    reasoning_hash,
    reward_amount,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

// ============================================================================
// §5.5 RejectProposal Tests
// ============================================================================

test reject_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let reasoning_hash = blake2b_256("rejection reasoning")

  let reference_inputs = oracle_ref_inputs(proposer_did)
  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)
  let signatories = [oracle_key_hash]

  proposal_validation.validate_reject_proposal(
    config,
    refs,
    datum,
    reasoning_hash,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

test reject_proposal_no_oracle_sig() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  let reasoning_hash = blake2b_256("rejection reasoning")

  let reference_inputs = oracle_ref_inputs(proposer_did)
  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)
  // No oracle sig
  let signatories = [proposer_key_hash]

  proposal_validation.validate_reject_proposal(
    config,
    refs,
    datum,
    reasoning_hash,
    reference_inputs,
    inputs,
    outputs,
    signatories,
    mint,
  )
}

// ============================================================================
// §5.6c ExtendReview Tests
// ============================================================================

test extend_review_valid() {
  let config = test_config()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let additional_slots = 100_000_000

  let reference_inputs = oracle_ref_inputs(proposer_did)

  // Continuing output with updated review_window
  let updated_datum =
    ProposalDatum {
      ..datum,
      review_window: datum.review_window + additional_slots,
    }
  let outputs = [proposal_output(updated_datum)]
  let signatories = [oracle_key_hash]

  proposal_validation.validate_extend_review(
    config,
    gov_params,
    datum,
    additional_slots,
    reference_inputs,
    outputs,
    signatories,
  )
}

test extend_review_exceeds_max() fail {
  let config = test_config()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  // Push total past max_review_window (2_419_200_000)
  let additional_slots = 9_999_999_999

  let reference_inputs = oracle_ref_inputs(proposer_did)

  let updated_datum =
    ProposalDatum {
      ..datum,
      review_window: datum.review_window + additional_slots,
    }
  let outputs = [proposal_output(updated_datum)]
  let signatories = [oracle_key_hash]

  proposal_validation.validate_extend_review(
    config,
    gov_params,
    datum,
    additional_slots,
    reference_inputs,
    outputs,
    signatories,
  )
}

test extend_review_no_oracle_sig() fail {
  let config = test_config()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let additional_slots = 100_000_000

  let reference_inputs = oracle_ref_inputs(proposer_did)

  let updated_datum =
    ProposalDatum {
      ..datum,
      review_window: datum.review_window + additional_slots,
    }
  let outputs = [proposal_output(updated_datum)]
  // No oracle signature
  let signatories = [proposer_key_hash]

  proposal_validation.validate_extend_review(
    config,
    gov_params,
    datum,
    additional_slots,
    reference_inputs,
    outputs,
    signatories,
  )
}

// ============================================================================
// §5.3 AmendProposal Tests
// ============================================================================

test amend_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let datum = default_proposal_datum()
  let new_hash = blake2b_256("amended proposal content v2")
  let new_uri = "ipfs://QmAmendedProposal"

  // No incorporated critiques for simplicity
  let incorporated_refs = []

  // Continuing output with Amended state
  let amended_datum =
    ProposalDatum {
      ..datum,
      state: Amended { previous_hash: datum.proposal_hash },
      proposal_hash: new_hash,
      storage_uri: new_uri,
      amendment_count: 1,
    }
  let outputs = [proposal_output(amended_datum)]
  let signatories = [proposer_key_hash]

  proposal_validation.validate_amend_proposal(
    config,
    refs,
    gov_params,
    datum,
    new_hash,
    new_uri,
    incorporated_refs,
    outputs,
    signatories,
  )
}

test amend_proposal_max_amendments() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  // Already at max amendments (5)
  let datum =
    ProposalDatum { ..default_proposal_datum(), amendment_count: 5 }
  let new_hash = blake2b_256("amended proposal content v7")
  let new_uri = "ipfs://QmAmended7"
  let incorporated_refs = []

  let amended_datum =
    ProposalDatum {
      ..datum,
      state: Amended { previous_hash: datum.proposal_hash },
      proposal_hash: new_hash,
      storage_uri: new_uri,
      amendment_count: 6,
    }
  let outputs = [proposal_output(amended_datum)]
  let signatories = [proposer_key_hash]

  proposal_validation.validate_amend_proposal(
    config,
    refs,
    gov_params,
    datum,
    new_hash,
    new_uri,
    incorporated_refs,
    outputs,
    signatories,
  )
}

// ============================================================================
// §5.6b ExpireStaleProposal Tests
// ============================================================================

test expire_stale_proposal_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let datum =
    ProposalDatum {
      ..default_proposal_datum(),
      proposal_type: ParameterChange {
        param_name: "MIN_CLAIM_STAKE",
        current_value: 50_000_000,
        proposed_value: 25_000_000,
      },
    }
  let own_ref = proposal_own_ref()

  // Reference inputs must include ProtocolParams at protocol_params_hash
  let reference_inputs =
    [cross_refs_ref_input(), protocol_params_ref_input()]

  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)

  proposal_validation.validate_expire_stale_proposal(
    config,
    refs,
    datum,
    reference_inputs,
    inputs,
    outputs,
    mint,
  )
}

test expire_stale_proposal_not_param_change() fail {
  let config = test_config()
  let refs = test_cross_refs()
  // GeneralSuggestion — not a ParameterChange, should fail
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()

  let reference_inputs =
    [cross_refs_ref_input(), protocol_params_ref_input()]
  let inputs =
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ]
  let outputs =
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ]
  let mint = proposal_token_mint(own_ref, -1)

  proposal_validation.validate_expire_stale_proposal(
    config,
    refs,
    datum,
    reference_inputs,
    inputs,
    outputs,
    mint,
  )
}

// ============================================================================
// §5.7 SubmitCritique Tests
// ============================================================================

test submit_critique_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique content"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  // Reference inputs: cross_refs, params, critic's DID, and the proposal
  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      registry_ref_input(critic_did),
      proposal_ref_input(proposal_datum),
    ]

  let signatories = [critic_key_hash]

  critique_validation.validate_submit_critique(
    config,
    refs,
    gov_params,
    critique_datum,
    reference_inputs,
    signatories,
  )
}

test submit_critique_self_critique_supportive() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  // Critic DID == Proposer DID with Supportive type — should fail
  let critique_datum =
    CritiqueDatum {
      critic_did: proposer_did,
      critic_credential: proposer_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("self critique"),
      storage_uri: "ipfs://QmSelfCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      registry_ref_input(proposer_did),
      proposal_ref_input(proposal_datum),
    ]
  let signatories = [proposer_key_hash]

  critique_validation.validate_submit_critique(
    config,
    refs,
    gov_params,
    critique_datum,
    reference_inputs,
    signatories,
  )
}

test submit_critique_self_amendment_allowed() {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  // Self-critique with Amendment type — should be allowed
  let critique_datum =
    CritiqueDatum {
      critic_did: proposer_did,
      critic_credential: proposer_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("self amendment"),
      storage_uri: "ipfs://QmSelfAmendment",
      critique_type: types.Amendment {
        suggested_change_hash: blake2b_256("improvement"),
      },
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      registry_ref_input(proposer_did),
      proposal_ref_input(proposal_datum),
    ]
  let signatories = [proposer_key_hash]

  critique_validation.validate_submit_critique(
    config,
    refs,
    gov_params,
    critique_datum,
    reference_inputs,
    signatories,
  )
}

test submit_critique_missing_did() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  // No critic DID in registry
  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      proposal_ref_input(proposal_datum),
    ]
  let signatories = [critic_key_hash]

  critique_validation.validate_submit_critique(
    config,
    refs,
    gov_params,
    critique_datum,
    reference_inputs,
    signatories,
  )
}

test submit_critique_insufficient_stake() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      // Below min_critique_stake (5_000_000)
      stake_amount: 1_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      registry_ref_input(critic_did),
      proposal_ref_input(proposal_datum),
    ]
  let signatories = [critic_key_hash]

  critique_validation.validate_submit_critique(
    config,
    refs,
    gov_params,
    critique_datum,
    reference_inputs,
    signatories,
  )
}

// ============================================================================
// §5.8 WithdrawCritique Tests
// ============================================================================

test withdraw_critique_valid() {
  let refs = test_cross_refs()
  let proposal_datum = default_proposal_datum()

  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  // Need proposal as reference input to check it's still active
  let reference_inputs = [proposal_ref_input(proposal_datum)]
  let signatories = [critic_key_hash]

  critique_validation.validate_withdraw_critique(
    critique_datum,
    reference_inputs,
    refs,
    signatories,
  )
}

test withdraw_critique_incorporated() fail {
  let refs = test_cross_refs()
  let proposal_datum = default_proposal_datum()

  // Already incorporated — can't withdraw
  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: True,
    }

  let reference_inputs = [proposal_ref_input(proposal_datum)]
  let signatories = [critic_key_hash]

  critique_validation.validate_withdraw_critique(
    critique_datum,
    reference_inputs,
    refs,
    signatories,
  )
}

test withdraw_critique_wrong_signer() fail {
  let refs = test_cross_refs()
  let proposal_datum = default_proposal_datum()

  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  let reference_inputs = [proposal_ref_input(proposal_datum)]
  // Wrong signer
  let signatories = [proposer_key_hash]

  critique_validation.validate_withdraw_critique(
    critique_datum,
    reference_inputs,
    refs,
    signatories,
  )
}

// ============================================================================
// §5.9 SubmitEndorsement Tests
// ============================================================================

test submit_endorsement_valid() {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  let endorsement_datum =
    GovernanceEndorsementDatum {
      endorser_did: critic_did,
      endorser_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      stake_amount: 5_000_000,
      created_at: 1_500_000_000,
    }

  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      registry_ref_input(critic_did),
      proposal_ref_input(proposal_datum),
    ]
  let signatories = [critic_key_hash]

  critique_validation.validate_submit_endorsement(
    config,
    refs,
    gov_params,
    endorsement_datum,
    reference_inputs,
    signatories,
  )
}

test submit_endorsement_self_endorse() fail {
  let config = test_config()
  let refs = test_cross_refs()
  let gov_params = params.default_governance_params()
  let proposal_datum = default_proposal_datum()

  // Endorser DID == Proposer DID — not allowed
  let endorsement_datum =
    GovernanceEndorsementDatum {
      endorser_did: proposer_did,
      endorser_credential: proposer_cred(),
      proposal_ref: proposal_own_ref(),
      stake_amount: 5_000_000,
      created_at: 1_500_000_000,
    }

  let reference_inputs =
    [
      cross_refs_ref_input(),
      params_ref_input(),
      registry_ref_input(proposer_did),
      proposal_ref_input(proposal_datum),
    ]
  let signatories = [proposer_key_hash]

  critique_validation.validate_submit_endorsement(
    config,
    refs,
    gov_params,
    endorsement_datum,
    reference_inputs,
    signatories,
  )
}

// ============================================================================
// WithdrawEndorsement Tests
// ============================================================================

test withdraw_endorsement_valid() {
  let endorsement_datum =
    GovernanceEndorsementDatum {
      endorser_did: critic_did,
      endorser_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      stake_amount: 5_000_000,
      created_at: 1_500_000_000,
    }
  let signatories = [critic_key_hash]

  critique_validation.validate_withdraw_endorsement(
    endorsement_datum,
    signatories,
  )
}

test withdraw_endorsement_wrong_signer() fail {
  let endorsement_datum =
    GovernanceEndorsementDatum {
      endorser_did: critic_did,
      endorser_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      stake_amount: 5_000_000,
      created_at: 1_500_000_000,
    }
  // Wrong signer
  let signatories = [proposer_key_hash]

  critique_validation.validate_withdraw_endorsement(
    endorsement_datum,
    signatories,
  )
}

// ============================================================================
// IncorporateCritique Tests
// ============================================================================

test incorporate_critique_valid() {
  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }

  // Output must have same critique but incorporated = True
  let incorporated_datum = CritiqueDatum { ..critique_datum, incorporated: True }
  let outputs =
    [
      Output {
        address: Address {
          payment_credential: Script(critique_validator_hash),
          stake_credential: None,
        },
        value: assets.from_lovelace(5_000_000),
        datum: InlineDatum(incorporated_datum),
        reference_script: None,
      },
    ]

  critique_validation.validate_incorporate_critique(
    critique_datum,
    outputs,
  )
}

test incorporate_critique_already_incorporated() fail {
  // Already incorporated — can't incorporate again
  let critique_datum =
    CritiqueDatum {
      critic_did: critic_did,
      critic_credential: critic_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("critique"),
      storage_uri: "ipfs://QmCritique",
      critique_type: Supportive,
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: True,
    }

  let incorporated_datum = CritiqueDatum { ..critique_datum, incorporated: True }
  let outputs =
    [
      Output {
        address: Address {
          payment_credential: Script(critique_validator_hash),
          stake_credential: None,
        },
        value: assets.from_lovelace(5_000_000),
        datum: InlineDatum(incorporated_datum),
        reference_script: None,
      },
    ]

  critique_validation.validate_incorporate_critique(
    critique_datum,
    outputs,
  )
}
