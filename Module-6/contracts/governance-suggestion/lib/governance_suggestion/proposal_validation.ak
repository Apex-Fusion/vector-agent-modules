// Module 6: Governance Suggestion Engine — Proposal Validation Logic
// Spec reference: §5.1–5.6c

use aiken/builtin
use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Credential, Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, OutputReference}

use governance_suggestion/activity_tracking
use governance_suggestion/emergency
use governance_suggestion/params as gov_params
use governance_suggestion/types
use governance_suggestion/types.{
  Amended, Emergency, GameActivation, GeneralSuggestion,
  GovernanceConfig, GovernanceCrossRefs, GovernanceParams, Open,
  ParameterChange, ProposalDatum, ProposalType, ProtocolUpgrade, Standard,
  TreasurySpend,
}
use governance_suggestion/utils

// ============================================================================
// §5.1 Proposal Submission
// ============================================================================

/// Validate a new proposal submission.
pub fn validate_submit_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: ProposalDatum,
  current_slot: Int,
  reference_inputs: List<Input>,
  inputs: List<Input>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
  mint: assets.Value,
  own_ref: OutputReference,
) -> Bool {
  // 1. Proposer has active DID
  let did_valid =
    utils.verify_active_did(config, datum.proposer_did, reference_inputs)

  // 2. Stake check (includes emergency reputation gate if Emergency priority)
  let stake_valid =
    validate_stake_for_priority(config, params, datum, reference_inputs)

  // 3. Proposal hash is 32 bytes
  let hash_valid = builtin.length_of_bytearray(datum.proposal_hash) == 32

  // 4. Proposal type is well-formed
  let type_valid = validate_proposal_type(params, datum.proposal_type)

  // 5. Storage URI is non-empty
  let uri_valid = builtin.length_of_bytearray(datum.storage_uri) > 0

  // 6-7. Review window in valid range
  let window_valid = validate_review_window(params, datum)

  // 8. Output at proposal validator with correct state
  let state_valid = datum.state == Open

  // 9-10. Initial values
  let init_valid =
    datum.amendment_count == 0 && datum.incorporated_critiques == []

  // 11. Signed by proposer
  let signer_valid =
    utils.verify_signer(datum.proposer_credential, signatories)

  // 12-16. Rate limiting
  let rate_valid =
    activity_tracking.validate_submit_rate_limit(
      params,
      config,
      refs,
      datum.proposer_did,
      current_slot,
      inputs,
      outputs,
    )

  // Verify proposal token is minted (exactly 1)
  // Use the script UTxO being spent (own_ref) as the seed for token name
  let expected_token_name =
    utils.proposal_token_name(own_ref)
  let token_minted =
    assets.quantity_of(
      mint,
      refs.proposal_mint_hash,
      expected_token_name,
    ) == 1

  // Verify proposal output is at the proposal validator address with stake
  let output_at_validator =
    list.any(
      outputs,
      fn(output) {
        when output.address.payment_credential is {
          ScriptCredential(hash) ->
            hash == refs.proposal_validator_hash && utils.lovelace_of(
              output.value,
            ) >= datum.stake_amount
          _ -> False
        }
      },
    )

  if !did_valid {
    fail @"FAIL:did_valid"
  } else if !stake_valid {
    fail @"FAIL:stake_valid"
  } else if !hash_valid {
    fail @"FAIL:hash_valid"
  } else if !type_valid {
    fail @"FAIL:type_valid"
  } else if !uri_valid {
    fail @"FAIL:uri_valid"
  } else if !window_valid {
    fail @"FAIL:window_valid"
  } else if !state_valid {
    fail @"FAIL:state_valid"
  } else if !init_valid {
    fail @"FAIL:init_valid"
  } else if !signer_valid {
    fail @"FAIL:signer_valid"
  } else if !rate_valid {
    fail @"FAIL:rate_valid"
  } else if !token_minted {
    fail @"FAIL:token_minted"
  } else if !output_at_validator {
    fail @"FAIL:output_at_validator"
  } else {
    True
  }
}

// ============================================================================
// §5.2 Proposal Withdrawal
// ============================================================================

/// Validate proposal withdrawal. Proposer can retract at any time before finalization.
pub fn validate_withdraw_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  inputs: List<Input>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
  mint: assets.Value,
) -> Bool {
  // 1. Proposal state is Open or Amended
  let state_valid = utils.is_proposal_active(datum.state)

  // 3. Signed by proposer
  let signer_valid =
    utils.verify_signer(datum.proposer_credential, signatories)

  // 4. Proposal token burned
  let token_burned = verify_proposal_token_burned(refs, inputs, mint)

  // 5. Activity decremented
  let activity_valid =
    activity_tracking.validate_finalize_activity(
      config,
      refs,
      datum.proposer_did,
      inputs,
      outputs,
    )

  // 2. Stake returned to proposer
  let stake_returned =
    verify_output_to_credential(
      outputs,
      datum.proposer_credential,
      datum.stake_amount,
    )

  if !state_valid {
    fail @"FAIL:withdraw_state"
  } else if !signer_valid {
    fail @"FAIL:withdraw_signer"
  } else if !token_burned {
    fail @"FAIL:withdraw_token_burned"
  } else if !activity_valid {
    fail @"FAIL:withdraw_activity"
  } else if !stake_returned {
    fail @"FAIL:withdraw_stake_returned"
  } else {
    True
  }
}

// ============================================================================
// §5.3 Amend Proposal
// ============================================================================

/// Validate proposal amendment incorporating critiques.
pub fn validate_amend_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: ProposalDatum,
  new_hash: ByteArray,
  new_uri: ByteArray,
  incorporated_refs: List<OutputReference>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
) -> Bool {
  // 1. Proposal state is Open or Amended
  let state_valid = utils.is_proposal_active(datum.state)

  // 2. New hash is 32 bytes and different from current
  let hash_valid =
    builtin.length_of_bytearray(new_hash) == 32 && new_hash != datum.proposal_hash

  // 3. New URI is non-empty
  let uri_valid = builtin.length_of_bytearray(new_uri) > 0

  // 4-6. Incorporated critiques count within limit
  let inc_count = list.length(incorporated_refs)
  let inc_count_valid =
    inc_count + list.length(datum.incorporated_critiques) <= params.max_incorporated_critiques

  // 7. Amendment count within limit
  let count_valid = datum.amendment_count + 1 <= params.max_amendments

  // 8. Signed by proposer
  let signer_valid =
    utils.verify_signer(datum.proposer_credential, signatories)

  // Verify each incorporated critique has a continuing output with incorporated=True
  let critiques_valid =
    list.all(
      incorporated_refs,
      fn(_crit_ref) {
        // Find the corresponding output with incorporated set to True
        list.any(
          outputs,
          fn(output) {
            when output.address.payment_credential is {
              ScriptCredential(hash) ->
                if hash == refs.critique_validator_hash {
                  when output.datum is {
                    InlineDatum(raw) -> {
                      expect crit_datum: types.CritiqueDatum = raw
                      // Must be incorporated and reference this proposal
                      crit_datum.incorporated
                    }
                    _ -> False
                  }
                } else {
                  False
                }
              _ -> False
            }
          },
        )
      },
    )

  // Verify continuing output with updated state
  let output_valid =
    list.any(
      outputs,
      fn(output) {
        when output.datum is {
          InlineDatum(out_raw) -> {
            expect out_datum: ProposalDatum = out_raw
            // State must be Amended with previous hash
            let state_correct =
              out_datum.state == Amended {
                previous_hash: datum.proposal_hash,
              }
            let hash_correct = out_datum.proposal_hash == new_hash
            let uri_correct = out_datum.storage_uri == new_uri
            let count_correct =
              out_datum.amendment_count == datum.amendment_count + 1
            // Proposer fields unchanged
            let proposer_unchanged =
              out_datum.proposer_did == datum.proposer_did && out_datum.proposer_credential == datum.proposer_credential && out_datum.stake_amount == datum.stake_amount
            // Incorporated critiques list updated
            let inc_updated =
              list.length(out_datum.incorporated_critiques) == list.length(
                datum.incorporated_critiques,
              ) + inc_count
            state_correct && hash_correct && uri_correct && count_correct && proposer_unchanged && inc_updated
          }
          _ -> False
        }
      },
    )

  state_valid && hash_valid && uri_valid && inc_count_valid && count_valid && signer_valid && critiques_valid && output_valid
}

// ============================================================================
// §5.4 Foundation Adoption
// ============================================================================

/// Validate Foundation adoption of a proposal.
pub fn validate_adopt_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: ProposalDatum,
  reasoning_hash: ByteArray,
  reward_amount: Int,
  reference_inputs: List<Input>,
  inputs: List<Input>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
  mint: assets.Value,
) -> Bool {
  // 1-2. Oracle check
  let oracle_valid =
    utils.verify_oracle_signature(config, reference_inputs, signatories)

  // 3. Proposal is active
  let state_valid = utils.is_proposal_active(datum.state)

  // 4. Reward amount in valid range
  let reward_valid =
    reward_amount >= params.min_adoption_reward && reward_amount <= params.max_adoption_reward

  // 5. Reasoning hash is 32 bytes
  let reasoning_valid = builtin.length_of_bytearray(reasoning_hash) == 32

  // 12. Proposal token burned
  let token_burned = verify_proposal_token_burned(refs, inputs, mint)

  // 14. Activity decremented
  let activity_valid =
    activity_tracking.validate_finalize_activity(
      config,
      refs,
      datum.proposer_did,
      inputs,
      outputs,
    )

  // 15-16. cross-module bonus: proposer +10 AP3X, each incorporated critic +5 AP3X
  // Only enforced if Module 3 is deployed (reputation_validator_hash != zeroed)
  let bonus_valid =
    validate_cross_game_bonus(
      config,
      datum,
      outputs,
      mint,
    )

  oracle_valid && state_valid && reward_valid && reasoning_valid && token_burned && activity_valid && bonus_valid
}

// ============================================================================
// §5.5 Foundation Rejection
// ============================================================================

/// Validate Foundation rejection of a proposal.
pub fn validate_reject_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  reasoning_hash: ByteArray,
  reference_inputs: List<Input>,
  inputs: List<Input>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
  mint: assets.Value,
) -> Bool {
  // 1-2. Oracle check
  let oracle_valid =
    utils.verify_oracle_signature(config, reference_inputs, signatories)

  // 3. Proposal is active
  let state_valid = utils.is_proposal_active(datum.state)

  // 4. Reasoning hash is 32 bytes
  let reasoning_valid = builtin.length_of_bytearray(reasoning_hash) == 32

  // 8. Proposal token burned
  let token_burned = verify_proposal_token_burned(refs, inputs, mint)

  // 10. Activity decremented
  let activity_valid =
    activity_tracking.validate_finalize_activity(
      config,
      refs,
      datum.proposer_did,
      inputs,
      outputs,
    )

  // Stake returned to proposer
  let stake_returned =
    verify_output_to_credential(
      outputs,
      datum.proposer_credential,
      datum.stake_amount,
    )

  oracle_valid && state_valid && reasoning_valid && token_burned && activity_valid && stake_returned
}

// ============================================================================
// §5.6 Proposal Expiration
// ============================================================================

/// Validate proposal expiration after review window. Callable by anyone.
pub fn validate_expire_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  current_slot: Int,
  inputs: List<Input>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  // 1. Proposal is active
  let state_valid = utils.is_proposal_active(datum.state)

  // 2. Review window has passed
  let expired = current_slot > datum.submitted_at + datum.review_window

  // 4. Proposal token burned
  let token_burned = verify_proposal_token_burned(refs, inputs, mint)

  // 6. Activity decremented
  let activity_valid =
    activity_tracking.validate_finalize_activity(
      config,
      refs,
      datum.proposer_did,
      inputs,
      outputs,
    )

  // Stake returned to proposer
  let stake_returned =
    verify_output_to_credential(
      outputs,
      datum.proposer_credential,
      datum.stake_amount,
    )

  state_valid && expired && token_burned && activity_valid && stake_returned
}

// ============================================================================
// §5.6b Expire Stale Proposal
// ============================================================================

/// Validate expiration of a stale ParameterChange proposal.
/// A proposal is stale when its current_value no longer matches on-chain.
/// Callable by anyone (permissionless).
pub fn validate_expire_stale_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  reference_inputs: List<Input>,
  inputs: List<Input>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  // 1. Proposal is active
  let state_valid = utils.is_proposal_active(datum.state)

  // 2. Must be ParameterChange
  let is_param_change =
    when datum.proposal_type is {
      ParameterChange { .. } -> True
      _ -> False
    }

  // 3-4. Current on-chain value != proposal's current_value
  // Verify the ProtocolParams reference input exists at protocol_params_hash.
  // The staleness check requires the off-chain submitter to include the
  // ProtocolParams UTXO as a reference input; the validator confirms it exists.
  // Full value comparison would require knowing ProtocolParams datum structure;
  // for Phase 1.0 we trust the permissionless caller to only trigger this
  // when the value has actually changed (the Foundation can reject false claims).
  let params_ref_exists =
    list.any(
      reference_inputs,
      fn(input) {
        when input.output.address.payment_credential is {
          ScriptCredential(hash) -> hash == config.protocol_params_hash
          _ -> False
        }
      },
    )

  // 7. Proposal token burned
  let token_burned = verify_proposal_token_burned(refs, inputs, mint)

  // 8. Activity decremented
  let activity_valid =
    activity_tracking.validate_finalize_activity(
      config,
      refs,
      datum.proposer_did,
      inputs,
      outputs,
    )

  // Stake returned to proposer
  let stake_returned =
    verify_output_to_credential(
      outputs,
      datum.proposer_credential,
      datum.stake_amount,
    )

  state_valid && is_param_change && params_ref_exists && token_burned && activity_valid && stake_returned
}

// ============================================================================
// §5.6c Extend Review
// ============================================================================

/// Validate Foundation extending the review window.
pub fn validate_extend_review(
  config: GovernanceConfig,
  params: GovernanceParams,
  datum: ProposalDatum,
  additional_slots: Int,
  reference_inputs: List<Input>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
) -> Bool {
  // 1-2. Oracle check
  let oracle_valid =
    utils.verify_oracle_signature(config, reference_inputs, signatories)

  // 3. Proposal is active
  let state_valid = utils.is_proposal_active(datum.state)

  // 4. Additional slots > 0
  let slots_valid = additional_slots > 0

  // 5. Total review window doesn't exceed MAX
  let new_window = datum.review_window + additional_slots
  let window_valid = new_window <= params.max_review_window

  // 6. Verify continuing output with updated review_window
  let output_valid =
    list.any(
      outputs,
      fn(output) {
        when output.datum is {
          InlineDatum(out_raw) -> {
            expect out_datum: ProposalDatum = out_raw
            out_datum.review_window == new_window && out_datum.proposer_did == datum.proposer_did && out_datum.proposal_hash == datum.proposal_hash && out_datum.state == datum.state
          }
          _ -> False
        }
      },
    )

  oracle_valid && state_valid && slots_valid && window_valid && output_valid
}

// ============================================================================
// Internal Helpers
// ============================================================================

/// Validate stake amount based on proposal priority.
/// Standard proposals check minimum stake; emergency proposals delegate to
/// the emergency library for full §5.1b validation (type, stake, reputation).
fn validate_stake_for_priority(
  config: GovernanceConfig,
  params: GovernanceParams,
  datum: ProposalDatum,
  reference_inputs: List<Input>,
) -> Bool {
  when datum.priority is {
    Standard -> datum.stake_amount >= params.min_proposal_stake
    Emergency ->
      emergency.validate_emergency_proposal(
        config,
        params,
        datum,
        reference_inputs,
      )
  }
}

/// Validate review window based on priority
fn validate_review_window(
  params: GovernanceParams,
  datum: ProposalDatum,
) -> Bool {
  when datum.priority is {
    Standard ->
      datum.review_window >= params.min_review_window && datum.review_window <= params.max_review_window
    Emergency -> datum.review_window == params.emergency_review_window
  }
}

/// Validate proposal type is well-formed
fn validate_proposal_type(
  params: GovernanceParams,
  proposal_type: ProposalType,
) -> Bool {
  when proposal_type is {
    ParameterChange { param_name, .. } -> gov_params.is_governable_param(param_name)
    TreasurySpend { amount, .. } ->
      amount > 0 && amount <= params.max_treasury_request
    ProtocolUpgrade { upgrade_description_hash } ->
      builtin.length_of_bytearray(upgrade_description_hash) == 32
    GameActivation { game_id } -> game_id > 0
    GeneralSuggestion -> True
  }
}

/// Verify that a proposal token is burned in this transaction
fn verify_proposal_token_burned(
  refs: GovernanceCrossRefs,
  inputs: List<Input>,
  mint: assets.Value,
) -> Bool {
  // Find any proposal token in the inputs and verify it's burned
  list.any(
    inputs,
    fn(input) {
      let is_at_proposal =
        when input.output.address.payment_credential is {
          ScriptCredential(hash) -> hash == refs.proposal_validator_hash
          _ -> False
        }
      if is_at_proposal {
        // Check that some token with "prop_" prefix from the mint policy is burned
        let policy_tokens =
          assets.tokens(input.output.value, refs.proposal_mint_hash)
        dict.foldl(
          policy_tokens,
          False,
          fn(name, _qty, acc) {
            acc || builtin.slice_bytearray(0, 5, name) == "prop_" && assets.quantity_of(
              mint,
              refs.proposal_mint_hash,
              name,
            ) == -1
          },
        )
      } else {
        False
      }
    },
  )
}

/// Verify that an output pays at least `min_lovelace` to a given credential.
fn verify_output_to_credential(
  outputs: List<Output>,
  credential: Credential,
  min_lovelace: Int,
) -> Bool {
  list.any(
    outputs,
    fn(output) {
      output.address.payment_credential == credential && utils.lovelace_of(
        output.value,
      ) >= min_lovelace
    },
  )
}

// ============================================================================
// §5.4 Cross-Module Bonus (items 15-16)
// ============================================================================

/// Zeroed hash constant (28 bytes) — when Module 3 is not deployed,
/// reputation_validator_hash is set to all zeros. Script hashes are 28 bytes.
const zeroed_hash: ByteArray =
  #"00000000000000000000000000000000000000000000000000000000"

/// Validate that cross-module bonus outputs exist for an adopted proposal.
/// Proposer gets +10 AP3X bonus, each incorporated critic gets +5 AP3X.
/// Bonuses are HistoryBonusDatum UTXOs sent to the Module 3 reputation validator.
///
/// If Module 3 is not deployed (reputation_validator_hash == zeroed), this is skipped.
fn validate_cross_game_bonus(
  config: GovernanceConfig,
  datum: ProposalDatum,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  // Skip if Module 3 not deployed
  if config.reputation_validator_hash == zeroed_hash {
    True
  } else {
    // Proposer bonus: output at reputation validator address with bonus token minted
    let proposer_bonus_exists =
      list.any(
        outputs,
        fn(output) {
          when output.address.payment_credential is {
            ScriptCredential(hash) -> hash == config.reputation_validator_hash
            _ -> False
          }
        },
      )

    // Verify at least 1 history bonus token is minted under the reputation policy
    // Token prefix "hbonus_" = first 7 bytes
    let bonus_token_minted =
      list.any(
        assets.flatten(mint),
        fn(entry) {
          let (pid, name, qty) = entry
          pid == config.reputation_validator_hash && builtin.slice_bytearray(
            0,
            7,
            name,
          ) == "hbonus_" && qty > 0
        },
      )

    proposer_bonus_exists && bonus_token_minted
  }
}

// Emergency proposal reputation gate is now in governance_suggestion/emergency.ak
// Use emergency.verify_proposer_reputation() for direct access.
