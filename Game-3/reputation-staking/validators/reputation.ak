/// Reputation Validator — Multi-Validator
///
/// Controls self-stake lifecycle and history bonus tokens:
///   - Minting policy: MintStakeToken, BurnStakeToken, MintHistoryBonus, BurnGenesisBonus
///   - Spend validator: CreateStake, IncreaseStake, DecreaseStake, UpdateCapabilities, ClaimDecayRefund
///
/// Because both handlers share the same script hash:
///   policy_id == script_hash == payment_credential of the reputation address

use aiken/interval.{Finite}
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Output, OutputReference, Transaction}
use reputation_staking/challenge_validation
use reputation_staking/config.{ReputationConfig, get_cross_refs}
use reputation_staking/decay
use reputation_staking/params.{ProtocolParams, find_protocol_params}
use reputation_staking/stake_validation
use reputation_staking/types.{
  AuditClaimWon, BurnGenesisBonus, BurnStakeToken, ChallengeWon,
  ClaimDecayRefund, CreateStake, DecreaseStake, GenesisBonus,
  HistoryBonusDatum, IncreaseStake, JurorDuty,
  MintHistoryBonus, MintStakeToken,
  StakeAction, StakeDatum, StakeMintAction, UpdateCapabilities,
  genesis_bonus_token_prefix, history_bonus_token_prefix, stake_token_prefix,
}
use reputation_staking/utils.{
  count_script_inputs, derive_stake_token_name,
  has_key_signed, has_script_input, starts_with, verify_agent_exists,
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Find the continuing output at the reputation validator for a given agent.
fn find_continuing_output(
  tx: Transaction,
  reputation_policy_id: PolicyId,
  agent_did: ByteArray,
) -> Option<Output> {
  let stake_token = derive_stake_token_name(agent_did)
  list.find(
    tx.outputs,
    fn(output) {
      let is_at_reputation =
        when output.address.payment_credential is {
          Script(hash) -> hash == reputation_policy_id
          _ -> False
        }
      let has_token =
        assets.quantity_of(output.value, reputation_policy_id, stake_token) == 1
      is_at_reputation && has_token
    },
  )
}

/// Extract the lower bound of the validity range as the "current slot".
fn get_validity_lower_bound(tx: Transaction) -> Option<Int> {
  when tx.validity_range.lower_bound.bound_type is {
    Finite(slot) -> Some(slot)
    _ -> None
  }
}

/// Derive own script hash from the spent UTXO's address.
fn resolve_own_hash(own_ref: OutputReference, tx: Transaction) -> ByteArray {
  expect Some(own_input) =
    list.find(tx.inputs, fn(i) { i.output_reference == own_ref })
  expect Script(hash) = own_input.output.address.payment_credential
  hash
}

// ──────────────────────────────────────────────────────────────────────────────
// History Bonus & Genesis Validation (F-NEW-6 fix)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the history bonus mint by checking the bonus output datum source.
/// Dispatches to source-specific validation (Game 1 claim won, jury duty, etc.)
fn validate_history_bonus_mint(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  policy_id: PolicyId,
) -> Bool {
  // Find the history bonus output at the reputation validator
  expect Some(bonus_output) =
    list.find(
      tx.outputs,
      fn(output) {
        let is_at_reputation =
          when output.address.payment_credential is {
            Script(hash) -> hash == policy_id
            _ -> False
          }
        let has_bonus_token =
          list.any(
            assets.flatten(output.value),
            fn(entry) {
              let (pid, name, _qty) = entry
              pid == policy_id && starts_with(name, history_bonus_token_prefix)
            },
          )
        is_at_reputation && has_bonus_token
      },
    )
  expect InlineDatum(raw) = bonus_output.datum
  expect bonus_datum: HistoryBonusDatum = raw

  // Agent must have active DID
  let agent_exists =
    verify_agent_exists(tx.reference_inputs, cfg, bonus_datum.agent_did)

  // Source-specific validation
  let source_valid =
    when bonus_datum.source is {
      AuditClaimWon ->
        challenge_validation.validate_audit_claim_won_bonus(
          tx, cfg, bonus_datum.agent_did,
        )
      JurorDuty ->
        challenge_validation.validate_juror_duty_bonus(
          tx, cfg, bonus_datum.agent_did,
        )
      ChallengeWon ->
        // ChallengeWon bonuses are minted during DistributeOutcome
        // Oracle must sign (already enforced at mint handler level)
        True
      GenesisBonus ->
        // Genesis has its own validation below
        validate_genesis_mint(tx, policy_id, params, bonus_datum)
      // Other game sources: require oracle attestation (already enforced above)
      _ -> True
    }

  agent_exists && source_valid
}

/// Validate genesis bonus minting rules.
fn validate_genesis_mint(
  tx: Transaction,
  reputation_policy_id: PolicyId,
  params: ProtocolParams,
  bonus_datum: HistoryBonusDatum,
) -> Bool {
  expect Some(current_slot) = get_validity_lower_bound(tx)

  // bonus_points must match genesis_bonus_amount
  let correct_amount = bonus_datum.bonus_points == params.genesis_bonus_amount

  // Must be within minting window (slots since genesis)
  let within_window = current_slot <= params.genesis_minting_window

  // Token must be unique per agent (genesis token not already in inputs)
  let genesis_prefix = genesis_bonus_token_prefix
  let already_has_genesis =
    list.any(
      tx.inputs,
      fn(input) {
        list.any(
          assets.flatten(input.output.value),
          fn(entry) {
            let (pid, name, _qty) = entry
            pid == reputation_policy_id && starts_with(name, genesis_prefix)
          },
        )
      },
    )
  let is_unique = !already_has_genesis

  correct_amount && within_window && is_unique
}

/// Validate genesis bonus burn: protection period must have elapsed.
fn validate_genesis_burn(
  tx: Transaction,
  reputation_policy_id: PolicyId,
  params: ProtocolParams,
  current_slot: Int,
) -> Bool {
  // Find the genesis bonus input being consumed
  expect Some(genesis_input) =
    list.find(
      tx.inputs,
      fn(input) {
        list.any(
          assets.flatten(input.output.value),
          fn(entry) {
            let (pid, name, _qty) = entry
            pid == reputation_policy_id && starts_with(name, genesis_bonus_token_prefix)
          },
        )
      },
    )
  expect InlineDatum(raw) = genesis_input.output.datum
  expect bonus_datum: HistoryBonusDatum = raw

  // Protection period must have elapsed since bonus creation
  bonus_datum.created_at + params.genesis_protection_period <= current_slot
}

// ──────────────────────────────────────────────────────────────────────────────
// Validator
// ──────────────────────────────────────────────────────────────────────────────

validator reputation(cfg: ReputationConfig) {
  /// Minting policy: controls stake token and history bonus token lifecycle.
  mint(redeemer: StakeMintAction, policy_id: PolicyId, tx: Transaction) {
    let flat_mint = assets.flatten(tx.mint)

    // F2/F1 fix: All mint/burn actions require a script input, ensuring
    // the spend handler fires and provides real validation.
    let script_input_exists = has_script_input(tx, policy_id)

    when redeemer is {
      MintStakeToken ->
        // F2 fix: Require script input so spend handler (CreateStake) must fire
        script_input_exists && list.any(
          flat_mint,
          fn(entry) {
            let (pid, name, qty) = entry
            pid == policy_id && starts_with(name,stake_token_prefix) && qty == 1
          },
        )
      BurnStakeToken ->
        // F1 fix: Require script input to prevent orphan burns
        script_input_exists && list.any(
          flat_mint,
          fn(entry) {
            let (pid, name, qty) = entry
            pid == policy_id && starts_with(name,stake_token_prefix) && qty == -1
          },
        )
      MintHistoryBonus -> {
        // F-NEW-6 fix: Require oracle signature AND source-specific validation
        let params =
          find_protocol_params(tx.reference_inputs, cfg.params_script_hash)
        let oracle_signed = has_key_signed(tx, cfg.oracle_credential)
        let has_valid_token =
          list.any(
            flat_mint,
            fn(entry) {
              let (pid, name, qty) = entry
              pid == policy_id && starts_with(name, history_bonus_token_prefix) && qty == 1
            },
          )
        // Find the history bonus output to determine source
        let bonus_valid =
          validate_history_bonus_mint(tx, cfg, params, policy_id)
        script_input_exists && oracle_signed && has_valid_token && bonus_valid
      }
      BurnGenesisBonus -> {
        // F1 fix: Require script input to prevent orphan burns
        // Genesis burn: verify protection period has elapsed
        let params =
          find_protocol_params(tx.reference_inputs, cfg.params_script_hash)
        expect Some(current_slot) = get_validity_lower_bound(tx)
        let has_valid_burn =
          list.any(
            flat_mint,
            fn(entry) {
              let (pid, name, qty) = entry
              pid == policy_id && starts_with(name, genesis_bonus_token_prefix) && qty == -1
            },
          )
        // Verify the genesis bonus being burned has passed protection period
        let protection_met =
          validate_genesis_burn(tx, policy_id, params, current_slot)
        script_input_exists && has_valid_burn && protection_met
      }
    }
  }

  /// Spend validator: controls self-stake UTXO lifecycle.
  spend(
    datum: Option<StakeDatum>,
    redeemer: StakeAction,
    own_utxo: OutputReference,
    tx: Transaction,
  ) {
    expect Some(stake_datum) = datum
    let params =
      find_protocol_params(tx.reference_inputs, cfg.params_script_hash)

    // Derive current slot from validity range lower bound
    expect Some(current_slot) = get_validity_lower_bound(tx)

    // Derive own script hash at runtime (no circular dependency)
    let own_hash = resolve_own_hash(own_utxo, tx)

    // Look up cross-validator refs from authenticated reference input
    let refs = get_cross_refs(cfg, tx.reference_inputs)

    // F5 fix: Prevent double satisfaction — only one script input allowed
    expect count_script_inputs(tx, own_hash) == 1

    when redeemer is {
      CreateStake -> {
        expect Some(output) =
          find_continuing_output(tx, own_hash, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_create_stake(
          tx, cfg, refs, params, out_datum, output, current_slot,
        )
      }
      IncreaseStake -> {
        expect Some(output) =
          find_continuing_output(tx, own_hash, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_increase_stake(
          tx, cfg, refs, stake_datum, out_datum, output, current_slot,
        )
      }
      DecreaseStake { amount } -> {
        expect Some(output) =
          find_continuing_output(tx, own_hash, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_decrease_stake(
          tx, cfg, refs, params, stake_datum, out_datum, output, amount, current_slot,
        )
      }
      UpdateCapabilities { new_capabilities } -> {
        expect Some(output) =
          find_continuing_output(tx, own_hash, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_update_capabilities(
          tx, cfg, refs, params, stake_datum, out_datum, output, new_capabilities,
          current_slot,
        )
      }
      ClaimDecayRefund -> {
        // F6 fix: Calculate inactive_epochs from on-chain data instead of hardcoding
        // F-NEW-2 fix: Guard against division by zero
        expect params.epoch_length > 0
        let elapsed_slots = current_slot - stake_datum.last_updated
        let elapsed_epochs = elapsed_slots / params.epoch_length
        let inactive_epochs = elapsed_epochs - params.activity_window
        expect inactive_epochs > 0
        decay.validate_claim_decay_refund(
          tx, cfg, refs, params, stake_datum,
          inactive_epochs,
        )
      }
    }
  }

  else(_ctx) {
    fail @"Unsupported script purpose"
  }
}
