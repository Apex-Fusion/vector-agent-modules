/// Endorsement validation logic for Module 3: Reputation Staking.
///
/// Validates endorsement operations: MintEndorsementToken, IncreaseEndorsement,
/// and WithdrawEndorsement.
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.3, 6.3b, 6.4.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{
  Challenge, EndorsementDatum, EndorsementValidatorDatum, Open, StakeDatum,
}
use reputation_staking/utils.{
  derive_endorsement_token_name, derive_stake_token_name, get_ap3x_amount,
  has_credential_signed, is_non_empty, is_subset, starts_with, verify_agent_exists,
}

// ──────────────────────────────────────────────────────────────────────────────
// MintEndorsementToken (Section 6.3)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate creation of a new endorsement.
///
/// MUST:
///   1. Endorser has active DID in Agent Registry
///   2. Target agent has active DID AND active StakeUTXO
///   3. endorser_did != target_did (cannot self-endorse)
///   4. Endorsement token asset name = deterministic hash of pair
///   5. stake_amount >= MIN_ENDORSEMENT
///   6. endorsed_capabilities is non-empty and subset of target's staked_capabilities
///   7. EndorsementUTXO created at endorsement validator address
///   8. Transaction signed by endorser_credential
pub fn validate_mint_endorsement(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  output_datum: EndorsementDatum,
  output: Output,
) -> Bool {
  // Rule 1: Endorser exists in registry
  let endorser_exists =
    verify_agent_exists(tx.reference_inputs, cfg, output_datum.endorser_did)

  // Rule 2: Target exists in registry
  let target_exists =
    verify_agent_exists(tx.reference_inputs, cfg, output_datum.target_did)

  // Rule 2: Target has active StakeUTXO (verified via reference input)
  let target_has_stake =
    verify_target_has_stake(
      tx.reference_inputs,
      refs,
      output_datum.target_did,
    )

  // Rule 3: No self-endorsement
  let not_self_endorse = output_datum.endorser_did != output_datum.target_did

  // Rule 4: Correct token name
  let expected_token_name =
    derive_endorsement_token_name(
      output_datum.endorser_did,
      output_datum.target_did,
    )
  let has_correct_token =
    assets.quantity_of(
      output.value,
      refs.endorsement_policy_id,
      expected_token_name,
    ) == 1

  // Rule 4: Token must not appear in any input (proving it doesn't already exist)
  let token_is_new =
    !list.any(
      tx.inputs,
      fn(input) {
        assets.quantity_of(
          input.output.value,
          refs.endorsement_policy_id,
          expected_token_name,
        ) >= 1
      },
    )

  // Rule 5: Minimum endorsement
  let meets_minimum = output_datum.stake_amount >= params.min_endorsement

  // Rule 6: Capabilities non-empty and subset of target's staked capabilities
  let has_capabilities = is_non_empty(output_datum.endorsed_capabilities)
  let capabilities_valid =
    verify_endorsed_capabilities(
      tx.reference_inputs,
      refs,
      output_datum.target_did,
      output_datum.endorsed_capabilities,
    )

  // Rule 7: Output is at endorsement validator address
  let at_validator =
    when output.address.payment_credential is {
      Script(hash) -> hash == refs.endorsement_policy_id
      _ -> False
    }

  // Output value includes correct AP3X
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Rule 8: Signed by endorser
  let signed = has_credential_signed(tx, output_datum.endorser_credential)

  endorser_exists && target_exists && target_has_stake && not_self_endorse && has_correct_token && token_is_new && meets_minimum && has_capabilities && capabilities_valid && at_validator && value_correct && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseEndorsement (Section 6.3b)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate increasing an existing endorsement.
///
/// MUST:
///   1. Existing EndorsementUTXO consumed (with endorsement token)
///   2. endorser_did and target_did unchanged
///   3. new_stake_amount > current stake_amount
///   4. Additional AP3X provided
///   5. endorsed_capabilities may be updated (cannot remove challenged ones)
///   6. Continuing EndorsementUTXO with updated amount, preserved created_at
///   7. Transaction signed by endorser_credential
pub fn validate_increase_endorsement(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: EndorsementDatum,
  output_datum: EndorsementDatum,
  output: Output,
) -> Bool {
  // Rule 2: Same pair
  let same_endorser = input_datum.endorser_did == output_datum.endorser_did
  let same_target = input_datum.target_did == output_datum.target_did
  let same_credential =
    input_datum.endorser_credential == output_datum.endorser_credential

  // Rule 3: Amount increased
  let amount_increased = output_datum.stake_amount > input_datum.stake_amount

  // Rule 5: Capabilities must remain unchanged during increase
  // (endorser must withdraw and re-create to change capability scope)
  let capabilities_unchanged =
    input_datum.endorsed_capabilities == output_datum.endorsed_capabilities

  // Rule 6: created_at preserved
  let created_at_preserved = input_datum.created_at == output_datum.created_at

  // Output value matches new stake
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Endorsement token travels with UTXO
  let expected_token_name =
    derive_endorsement_token_name(
      input_datum.endorser_did,
      input_datum.target_did,
    )
  let has_token =
    assets.quantity_of(
      output.value,
      refs.endorsement_policy_id,
      expected_token_name,
    ) == 1

  // Output at correct address
  let at_validator =
    when output.address.payment_credential is {
      Script(hash) -> hash == refs.endorsement_policy_id
      _ -> False
    }

  // Rule 7: Signed by endorser
  let signed = has_credential_signed(tx, input_datum.endorser_credential)

  same_endorser && same_target && same_credential && amount_increased && capabilities_unchanged && created_at_preserved && value_correct && has_token && at_validator && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawEndorsement (Section 6.4)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate withdrawal of an endorsement.
///
/// MUST:
///   1. Endorsement exists (endorsement token present in input)
///   2. created_at + ENDORSEMENT_COOLDOWN <= current slot
///   3. Target agent has no active challenges
///   4. Stake returned to endorser_credential
///   5. Endorsement token burned
///   6. Transaction signed by endorser_credential
pub fn validate_withdraw_endorsement(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  input_datum: EndorsementDatum,
  current_slot: Int,
) -> Bool {
  // Rule 2: Cooldown elapsed
  let cooldown_met =
    input_datum.created_at + params.endorsement_cooldown <= current_slot

  // Rule 3: No active challenges against target
  // Phase 1.0: check reference inputs for open challenges
  let no_active_challenges =
    verify_no_target_challenges(
      tx.reference_inputs,
      refs,
      input_datum.target_did,
    )

  // Rule 4 (F-NEW-5 fix): Verify AP3X returned to endorser
  let endorser_paid =
    list.any(
      tx.outputs,
      fn(output) {
        let is_to_endorser =
          output.address.payment_credential == input_datum.endorser_credential
        let ap3x =
          get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
        is_to_endorser && ap3x >= input_datum.stake_amount
      },
    )

  // Rule 5: Endorsement token is burned in this tx
  let expected_token_name =
    derive_endorsement_token_name(
      input_datum.endorser_did,
      input_datum.target_did,
    )
  let token_burned =
    assets.quantity_of(
      tx.mint,
      refs.endorsement_policy_id,
      expected_token_name,
    ) == -1

  // Rule 6: Signed by endorser
  let signed = has_credential_signed(tx, input_datum.endorser_credential)

  cooldown_met && no_active_challenges && endorser_paid && token_burned && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that the target agent has an active StakeUTXO (via reference inputs).
fn verify_target_has_stake(
  reference_inputs: List<Input>,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
) -> Bool {
  let expected_token_name = derive_stake_token_name(target_did)
  list.any(
    reference_inputs,
    fn(input) {
      let is_at_reputation =
        when input.output.address.payment_credential is {
          Script(hash) -> hash == refs.reputation_policy_id
          _ -> False
        }
      let has_stake_token =
        assets.quantity_of(
          input.output.value,
          refs.reputation_policy_id,
          expected_token_name,
        ) == 1
      is_at_reputation && has_stake_token
    },
  )
}

/// Verify that endorsed capabilities are a subset of the target's staked capabilities.
/// Reads the target's StakeDatum from reference inputs.
fn verify_endorsed_capabilities(
  reference_inputs: List<Input>,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
  endorsed_capabilities: List<ByteArray>,
) -> Bool {
  let expected_token_name = derive_stake_token_name(target_did)
  expect Some(stake_input) =
    list.find(
      reference_inputs,
      fn(input) {
        let is_at_reputation =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == refs.reputation_policy_id
            _ -> False
          }
        let has_stake_token =
          assets.quantity_of(
            input.output.value,
            refs.reputation_policy_id,
            expected_token_name,
          ) == 1
        is_at_reputation && has_stake_token
      },
    )
  expect InlineDatum(raw_stake) = stake_input.output.datum
  expect stake_datum: StakeDatum = raw_stake
  is_subset(endorsed_capabilities, stake_datum.staked_capabilities)
}

/// Verify that no open challenges exist against the target agent.
/// F4 fix: Parse as EndorsementValidatorDatum wrapper, only inspect challenge tokens.
fn verify_no_target_challenges(
  reference_inputs: List<Input>,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
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
        // Only inspect inputs with challenge token prefix
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
                  challenge_datum.target_did == target_did && challenge_datum.state == Open
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
