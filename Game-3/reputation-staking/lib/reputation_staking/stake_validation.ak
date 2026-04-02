/// Stake validation logic for Game 3: Reputation Staking.
///
/// Validates all self-stake operations: CreateStake, IncreaseStake,
/// DecreaseStake, and UpdateCapabilities.
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.1, 6.2, 6.5.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{
  EndorsementValidatorDatum, Challenge, Open, StakeDatum,
}
use reputation_staking/utils.{
  derive_stake_token_name, find_agent_registry_datum, get_ap3x_amount,
  has_credential_signed, is_non_empty, starts_with, verify_capabilities_match,
}

// ──────────────────────────────────────────────────────────────────────────────
// CreateStake (Section 6.1)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate creation of a new self-stake.
///
/// MUST:
///   1. Agent has active DID in Agent Registry (verified via reference input)
///   2. No existing StakeUTXO for this agent_did (enforced by mint: token must not exist in inputs)
///   3. stake_amount >= MIN_SELF_STAKE
///   4. staked_capabilities is non-empty and subset of registry capabilities
///   5. Output StakeUTXO at reputation validator with correct datum + value + token
///   6. last_updated = current slot (validated via validity range)
///   7. Transaction signed by owner_credential
pub fn validate_create_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  output_datum: StakeDatum,
  output: Output,
  current_slot: Int,
) -> Bool {
  // Rule 1: Agent exists in registry (also validates capabilities — Rule 4)
  let capabilities_valid =
    verify_capabilities_match(
      tx.reference_inputs,
      cfg,
      output_datum.agent_did,
      output_datum.staked_capabilities,
    )

  // F3 fix: Verify owner_credential matches registry owner
  let agent_datum =
    find_agent_registry_datum(tx.reference_inputs, cfg, output_datum.agent_did)
  let owner_matches = output_datum.owner_credential == agent_datum.owner

  // Rule 3: Minimum stake
  let meets_minimum = output_datum.stake_amount >= params.min_self_stake

  // Rule 4: Non-empty capabilities
  let has_capabilities = is_non_empty(output_datum.staked_capabilities)

  // Rule 5: Output value includes correct AP3X amount
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Rule 5: Output has the stake tracking token
  let expected_token_name = derive_stake_token_name(output_datum.agent_did)
  let has_token =
    assets.quantity_of(output.value, refs.reputation_policy_id, expected_token_name) == 1

  // Rule 5: Output is at the reputation validator address
  let at_validator =
    when output.address.payment_credential is {
      Script(hash) -> hash == refs.reputation_policy_id
      _ -> False
    }

  // Rule 6: last_updated is current
  let timestamp_valid = output_datum.last_updated == current_slot

  // Rule: history_points starts at 0
  let history_clean = output_datum.history_points == 0

  // Rule 7: Signed by owner
  let signed = has_credential_signed(tx, output_datum.owner_credential)

  capabilities_valid && owner_matches && meets_minimum && has_capabilities && value_correct && has_token && at_validator && timestamp_valid && history_clean && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseStake (Section 6.2)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate increasing an existing self-stake.
///
/// MUST:
///   1. Existing StakeUTXO consumed (with stake token)
///   2. Continuing output with same agent_did, increased stake_amount
///   3. Value difference matches the increase
///   4. last_updated = current slot
///   5. Transaction signed by owner_credential
pub fn validate_increase_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
  current_slot: Int,
) -> Bool {
  // Rule 2: Same agent, increased amount
  let same_agent = input_datum.agent_did == output_datum.agent_did
  let same_owner = input_datum.owner_credential == output_datum.owner_credential
  let amount_increased = output_datum.stake_amount > input_datum.stake_amount
  let same_capabilities =
    input_datum.staked_capabilities == output_datum.staked_capabilities
  let same_history = input_datum.history_points == output_datum.history_points

  // Rule 3: Output value matches new stake amount
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Rule: Stake token travels with UTXO
  let expected_token_name = derive_stake_token_name(input_datum.agent_did)
  let has_token =
    assets.quantity_of(output.value, refs.reputation_policy_id, expected_token_name) == 1

  // Rule 4: Timestamp updated
  let timestamp_valid = output_datum.last_updated == current_slot

  // Rule 5: Signed by owner
  let signed = has_credential_signed(tx, input_datum.owner_credential)

  same_agent && same_owner && amount_increased && same_capabilities && same_history && value_correct && has_token && timestamp_valid && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// DecreaseStake (Section 6.2)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate decreasing an existing self-stake.
///
/// MUST:
///   1. Existing StakeUTXO consumed (with stake token)
///   2. new_amount = stake_amount - decrease_amount
///   3. new_amount >= MIN_SELF_STAKE
///   4. COOLDOWN: last_updated + STAKE_COOLDOWN <= current slot
///   5. No active challenges against this agent
///   6. Continuing output with decreased stake_amount
///   7. Difference returned to owner_credential
///   8. Transaction signed by owner_credential
pub fn validate_decrease_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
  decrease_amount: Int,
  current_slot: Int,
) -> Bool {
  // Rule 2: Correct decrease
  let expected_new_amount = input_datum.stake_amount - decrease_amount
  let amount_correct = output_datum.stake_amount == expected_new_amount
  let positive_decrease = decrease_amount > 0

  // Rule 3: Above minimum
  let meets_minimum = output_datum.stake_amount >= params.min_self_stake

  // Rule 4: Cooldown elapsed
  let cooldown_met =
    input_datum.last_updated + params.stake_cooldown <= current_slot

  // Rule 5: No active challenges (checked by verifying no challenge tokens
  // targeting this agent exist in the tx reference inputs or inputs).
  // In practice, we check that no ChallengeUTXO with target_did == agent_did
  // is referenced. For Phase 1.0, we verify via reference inputs.
  let no_active_challenges =
    verify_no_active_challenges(
      tx.reference_inputs,
      refs,
      input_datum.agent_did,
    )

  // Rule 6: Datum continuity
  let same_agent = input_datum.agent_did == output_datum.agent_did
  let same_owner = input_datum.owner_credential == output_datum.owner_credential
  let same_capabilities =
    input_datum.staked_capabilities == output_datum.staked_capabilities
  let same_history = input_datum.history_points == output_datum.history_points

  // Output value matches new stake
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Stake token travels with UTXO
  let expected_token_name = derive_stake_token_name(input_datum.agent_did)
  let has_token =
    assets.quantity_of(output.value, refs.reputation_policy_id, expected_token_name) == 1

  // Timestamp updated
  let timestamp_valid = output_datum.last_updated == current_slot

  // Rule 8: Signed by owner
  let signed = has_credential_signed(tx, input_datum.owner_credential)

  // F8 fix: Verify withdrawn AP3X is sent to the owner's address
  let owner_receives_withdrawal =
    list.any(
      tx.outputs,
      fn(output) {
        let is_to_owner =
          output.address.payment_credential == input_datum.owner_credential
        let ap3x =
          get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
        is_to_owner && ap3x >= decrease_amount
      },
    )

  amount_correct && positive_decrease && meets_minimum && cooldown_met && no_active_challenges && same_agent && same_owner && same_capabilities && same_history && value_correct && has_token && timestamp_valid && signed && owner_receives_withdrawal
}

// ──────────────────────────────────────────────────────────────────────────────
// UpdateCapabilities (Section 6.5)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate updating the capabilities backed by a self-stake.
///
/// MUST:
///   1. Existing StakeUTXO consumed (with stake token)
///   2. new_capabilities is non-empty
///   3. new_capabilities is a subset of agent's registry capabilities
///   4. COOLDOWN: last_updated + STAKE_COOLDOWN <= current slot
///   5. No active challenges against capabilities being REMOVED
///   6. Continuing output with updated staked_capabilities, same stake_amount
///   7. last_updated = current slot
///   8. Transaction signed by owner_credential
pub fn validate_update_capabilities(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
  new_capabilities: List<ByteArray>,
  current_slot: Int,
) -> Bool {
  // Rule 2: Non-empty
  let has_capabilities = is_non_empty(new_capabilities)

  // Rule 3: Subset of registry capabilities
  let capabilities_valid =
    verify_capabilities_match(
      tx.reference_inputs,
      cfg,
      input_datum.agent_did,
      new_capabilities,
    )

  // Rule 4: Cooldown elapsed
  let cooldown_met =
    input_datum.last_updated + params.stake_cooldown <= current_slot

  // Rule 5: No active challenges on removed capabilities
  // (For Phase 1.0, simplified: no active challenges at all if removing any)
  let no_active_challenges =
    verify_no_active_challenges(
      tx.reference_inputs,
      refs,
      input_datum.agent_did,
    )

  // Rule 6: Datum continuity (same stake, same agent, only capabilities change)
  let same_agent = input_datum.agent_did == output_datum.agent_did
  let same_owner = input_datum.owner_credential == output_datum.owner_credential
  let same_stake = input_datum.stake_amount == output_datum.stake_amount
  let same_history = input_datum.history_points == output_datum.history_points
  let capabilities_updated = output_datum.staked_capabilities == new_capabilities

  // Stake token travels with UTXO
  let expected_token_name = derive_stake_token_name(input_datum.agent_did)
  let has_token =
    assets.quantity_of(output.value, refs.reputation_policy_id, expected_token_name) == 1

  // Rule 7: Timestamp updated
  let timestamp_valid = output_datum.last_updated == current_slot

  // Rule 8: Signed by owner
  let signed = has_credential_signed(tx, input_datum.owner_credential)

  has_capabilities && capabilities_valid && cooldown_met && no_active_challenges && same_agent && same_owner && same_stake && same_history && capabilities_updated && has_token && timestamp_valid && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that no active (Open) challenge exists against the given agent.
/// Checks reference inputs for ChallengeUTXOs at the endorsement validator
/// that target this agent.
///
/// F4 fix: (a) Parse as EndorsementValidatorDatum wrapper, match Challenge variant.
///         (b) Only check inputs with challenge token prefix to avoid crashing on
///             endorsement datums. Reference input bypass remains a Phase 1.0
///             limitation — Phase 2 should use oracle attestation.
fn verify_no_active_challenges(
  reference_inputs: List<Input>,
  refs: CrossValidatorRefs,
  agent_did: ByteArray,
) -> Bool {
  !list.any(
    reference_inputs,
    fn(input) {
      let is_at_endorsement =
        when input.output.address.payment_credential is {
          Script(hash) -> hash == refs.endorsement_policy_id
          _ -> False
        }
      if is_at_endorsement {
        // Only inspect inputs that carry a challenge token (rchl_ prefix).
        // This avoids crashing on endorsement datums.
        let has_challenge_token =
          list.any(
            assets.flatten(input.output.value),
            fn(entry) {
              let (pid, name, _qty) = entry
              pid == refs.endorsement_policy_id && starts_with(name, types.challenge_token_prefix)
            },
          )
        if has_challenge_token {
          when input.output.datum is {
            InlineDatum(raw) -> {
              expect wrapper: EndorsementValidatorDatum = raw
              when wrapper is {
                Challenge(challenge_datum) ->
                  challenge_datum.target_did == agent_did && challenge_datum.state == Open
                _ -> False
              }
            }
            _ -> False
          }
        } else {
          False
        }
      } else {
        False
      }
    },
  )
}
