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
use reputation_staking/config.{ReputationConfig}
use reputation_staking/decay
use reputation_staking/params.{find_protocol_params}
use reputation_staking/stake_validation
use reputation_staking/types.{
  BurnGenesisBonus, BurnStakeToken, ClaimDecayRefund, CreateStake, DecreaseStake,
  IncreaseStake, MintHistoryBonus, MintStakeToken,
  StakeAction, StakeDatum, StakeMintAction, UpdateCapabilities,
  genesis_bonus_token_prefix, history_bonus_token_prefix, stake_token_prefix,
}
use reputation_staking/utils.{
  count_script_inputs, derive_stake_token_name, has_script_input, starts_with,
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
      MintHistoryBonus ->
        script_input_exists && list.any(
          flat_mint,
          fn(entry) {
            let (pid, name, qty) = entry
            pid == policy_id && starts_with(name,history_bonus_token_prefix) && qty == 1
          },
        )
      BurnGenesisBonus ->
        // F1 fix: Require script input to prevent orphan burns
        script_input_exists && list.any(
          flat_mint,
          fn(entry) {
            let (pid, name, qty) = entry
            pid == policy_id && starts_with(name,genesis_bonus_token_prefix) && qty == -1
          },
        )
    }
  }

  /// Spend validator: controls self-stake UTXO lifecycle.
  spend(
    datum: Option<StakeDatum>,
    redeemer: StakeAction,
    _own_utxo: OutputReference,
    tx: Transaction,
  ) {
    expect Some(stake_datum) = datum
    let params =
      find_protocol_params(tx.reference_inputs, cfg.params_script_hash)

    // Derive current slot from validity range lower bound
    expect Some(current_slot) = get_validity_lower_bound(tx)

    // F5 fix: Prevent double satisfaction — only one script input allowed
    expect count_script_inputs(tx, cfg.reputation_policy_id) == 1

    when redeemer is {
      CreateStake -> {
        expect Some(output) =
          find_continuing_output(tx, cfg.reputation_policy_id, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_create_stake(
          tx, cfg, params, out_datum, output, current_slot,
        )
      }
      IncreaseStake -> {
        expect Some(output) =
          find_continuing_output(tx, cfg.reputation_policy_id, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_increase_stake(
          tx, cfg, stake_datum, out_datum, output, current_slot,
        )
      }
      DecreaseStake { amount } -> {
        expect Some(output) =
          find_continuing_output(tx, cfg.reputation_policy_id, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_decrease_stake(
          tx, cfg, params, stake_datum, out_datum, output, amount, current_slot,
        )
      }
      UpdateCapabilities { new_capabilities } -> {
        expect Some(output) =
          find_continuing_output(tx, cfg.reputation_policy_id, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        stake_validation.validate_update_capabilities(
          tx, cfg, params, stake_datum, out_datum, output, new_capabilities,
          current_slot,
        )
      }
      ClaimDecayRefund -> {
        // F6 fix: Calculate inactive_epochs from on-chain data instead of hardcoding
        let elapsed_slots = current_slot - stake_datum.last_updated
        let elapsed_epochs = elapsed_slots / params.epoch_length
        let inactive_epochs = elapsed_epochs - params.activity_window
        expect inactive_epochs > 0
        decay.validate_claim_decay_refund(
          tx, cfg, params, stake_datum,
          inactive_epochs,
        )
      }
    }
  }

  else(_ctx) {
    fail @"Unsupported script purpose"
  }
}
