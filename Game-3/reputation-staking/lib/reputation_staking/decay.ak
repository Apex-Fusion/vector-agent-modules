/// Decay calculation and validation for Game 3: Reputation Staking.
///
/// Reputation decays if an agent is inactive. Decay processing is permissionless —
/// any agent can earn a collector fee by processing decay on inactive agents.
///
/// Phase 1.0: Foundation oracle attests inactivity.
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 5.2 and 6.6.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Output, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{StakeDatum}
use reputation_staking/utils.{
  derive_stake_token_name, get_ap3x_amount, has_key_signed,
}

// ──────────────────────────────────────────────────────────────────────────────
// Decay Calculation (Section 5.2)
// ──────────────────────────────────────────────────────────────────────────────

/// Calculate the decay amount for an inactive agent.
///
/// decay_amount = min(stake × DECAY_RATE × inactive_epochs, stake)
/// where inactive_epochs = epochs since last activity - ACTIVITY_WINDOW
pub fn calculate_decay(
  stake_amount: Int,
  inactive_epochs: Int,
  decay_rate: Int,
) -> Int {
  // decay_rate is in basis points (100 = 1%)
  let raw_decay = stake_amount * decay_rate * inactive_epochs / 10_000
  // Cannot decay more than the total stake
  if raw_decay > stake_amount {
    stake_amount
  } else {
    raw_decay
  }
}

/// Calculate the collector fee from the decay amount.
/// collector_fee = decay_amount × DECAY_COLLECTOR_FEE / 10_000
pub fn calculate_collector_fee(decay_amount: Int, collector_fee_rate: Int) -> Int {
  decay_amount * collector_fee_rate / 10_000
}

// ──────────────────────────────────────────────────────────────────────────────
// ClaimDecayRefund (Section 6.6)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate decay processing on an inactive agent's stake.
///
/// Phase 1.0: Foundation oracle attests inactivity via signature.
///
/// MUST:
///   1. Target StakeUTXO consumed (with stake token)
///   2. Foundation oracle attests inactivity (signed by oracle_credential)
///   3. decay_amount correctly calculated
///   4. collector_fee = decay_amount × DECAY_COLLECTOR_FEE
///   5. treasury_amount = decay_amount - collector_fee
///   6. Outputs:
///      - Continuing StakeUTXO with reduced stake (if above MIN_SELF_STAKE)
///      - Collector payout
///      - Treasury payout
///   7. If stake drops below MIN_SELF_STAKE: stake token burned
pub fn validate_claim_decay_refund(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  input_datum: StakeDatum,
  inactive_epochs: Int,
) -> Bool {
  // Phase 1.0: Oracle must sign to attest inactivity
  let oracle_signed = has_key_signed(tx, cfg.oracle_credential)

  // Calculate expected amounts
  let decay_amount =
    calculate_decay(input_datum.stake_amount, inactive_epochs, params.decay_rate)
  let collector_fee =
    calculate_collector_fee(decay_amount, params.decay_collector_fee)
  let treasury_amount = decay_amount - collector_fee
  let remaining_stake = input_datum.stake_amount - decay_amount

  // Decay must be positive
  let positive_decay = decay_amount > 0
  let valid_inactive_epochs = inactive_epochs > 0

  // Verify outputs based on whether stake survives
  let outputs_valid =
    if remaining_stake >= params.min_self_stake {
      // Stake continues with reduced amount
      validate_decay_continuing(
        tx,
        cfg,
        refs,
        input_datum,
        remaining_stake,
        collector_fee,
        treasury_amount,
      )
    } else {
      // Stake fully consumed — token must be burned
      validate_decay_full_consumption(
        tx,
        cfg,
        refs,
        input_datum,
        collector_fee,
        treasury_amount,
      )
    }

  oracle_signed && positive_decay && valid_inactive_epochs && outputs_valid
}

/// Validate decay when stake continues (remaining > MIN_SELF_STAKE).
fn validate_decay_continuing(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  remaining_stake: Int,
  collector_fee: Int,
  treasury_amount: Int,
) -> Bool {
  let stake_token = derive_stake_token_name(input_datum.agent_did)

  // Continuing StakeUTXO with reduced stake
  let stake_continues =
    list.any(
      tx.outputs,
      fn(output) {
        let is_at_reputation =
          when output.address.payment_credential is {
            Script(hash) -> hash == refs.reputation_policy_id
            _ -> False
          }
        let has_token =
          assets.quantity_of(output.value, refs.reputation_policy_id, stake_token) == 1
        let ap3x =
          get_ap3x_amount(
            output.value,
            cfg.ap3x_policy_id,
            cfg.ap3x_asset_name,
          )
        if is_at_reputation && has_token {
          expect InlineDatum(raw_out) = output.datum
          expect out_datum: StakeDatum = raw_out
          out_datum.agent_did == input_datum.agent_did && out_datum.owner_credential == input_datum.owner_credential && out_datum.stake_amount == remaining_stake && ap3x >= remaining_stake
        } else {
          False
        }
      },
    )

  // Treasury payment
  let treasury_paid = verify_treasury_payment(tx.outputs, cfg, treasury_amount)

  // Collector payment (any output not at reputation/treasury/endorsement validators)
  // The collector fee just needs to exist as an output with sufficient AP3X
  let collector_paid = collector_fee <= 0 || verify_collector_payment(tx, cfg, refs, collector_fee)

  stake_continues && treasury_paid && collector_paid
}

/// Validate decay when stake is fully consumed (burn token).
fn validate_decay_full_consumption(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  collector_fee: Int,
  treasury_amount: Int,
) -> Bool {
  let stake_token = derive_stake_token_name(input_datum.agent_did)

  // Stake token must be burned
  let token_burned =
    assets.quantity_of(
      tx.mint,
      refs.reputation_policy_id,
      stake_token,
    ) == -1

  // Treasury payment
  let treasury_paid = verify_treasury_payment(tx.outputs, cfg, treasury_amount)

  // Collector payment
  let collector_paid = collector_fee <= 0 || verify_collector_payment(tx, cfg, refs, collector_fee)

  token_burned && treasury_paid && collector_paid
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

fn verify_treasury_payment(
  outputs: List<Output>,
  cfg: ReputationConfig,
  min_amount: Int,
) -> Bool {
  if min_amount <= 0 {
    True
  } else {
    list.any(
      outputs,
      fn(output) {
        let is_treasury =
          when output.address.payment_credential is {
            Script(hash) -> hash == cfg.treasury_script_hash
            _ -> False
          }
        let ap3x =
          get_ap3x_amount(
            output.value,
            cfg.ap3x_policy_id,
            cfg.ap3x_asset_name,
          )
        is_treasury && ap3x >= min_amount
      },
    )
  }
}

/// Verify that a collector fee is paid out. The collector is any address
/// that is not the reputation validator, endorsement validator, or treasury.
fn verify_collector_payment(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  min_amount: Int,
) -> Bool {
  // Sum AP3X in outputs not going to protocol addresses
  let total_external_ap3x =
    list.foldl(
      tx.outputs,
      0,
      fn(output, acc) {
        let is_protocol =
          when output.address.payment_credential is {
            Script(hash) ->
              hash == refs.reputation_policy_id || hash == refs.endorsement_policy_id || hash == cfg.treasury_script_hash
            _ -> False
          }
        if !is_protocol {
          acc + get_ap3x_amount(
            output.value,
            cfg.ap3x_policy_id,
            cfg.ap3x_asset_name,
          )
        } else {
          acc
        }
      },
    )
  // At least the collector fee should go to non-protocol addresses
  // (this is a loose check — the tx builder determines who the collector is)
  total_external_ap3x >= min_amount
}
