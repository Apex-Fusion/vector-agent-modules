/// Decay calculation and validation for Module 3: Reputation Staking.
///
/// Reputation decays if an agent is inactive. Decay processing is permissionless —
/// any agent can earn a collector fee by processing decay on inactive agents.
///
/// Phase 1.0: Foundation oracle attests inactivity.
/// See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 5.2 and 6.6.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Output, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{StakeDatum}
use reputation_staking/utils.{
  derive_stake_token_name, exact_address, get_ap3x_amount, has_key_signed,
  no_challenge_change, paid,
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
/// remaining = stake_amount - decay_amount. When remaining is at least
/// MIN_SELF_STAKE, the stake continues with stake_amount = remaining. Else
/// the decay consumes the whole stake: the stake continues with
/// stake_amount = 0 and its owner receives remaining (owner_due). The stake
/// token is never burned, so the stake (and its stake_id) stays readable
/// for the endorsements that back it.
///
/// MUST:
///   1. Target StakeUTXO consumed (with stake token)
///   2. Foundation oracle attests inactivity (signed by oracle_credential)
///   3. decay_amount correctly calculated and positive
///   4. collector_fee = decay_amount × DECAY_COLLECTOR_FEE
///   5. treasury_amount = decay_amount - collector_fee
///   6. Outputs:
///      - The continuing StakeUTXO at the exact reputation address, with
///        exactly one stake token of the agent, at least the new
///        stake_amount AP3X, and the spent datum with the new stake_amount
///        (last_updated can change; all other fields stay, also stake_id)
///      - Collector payout
///      - Treasury payout
///      - When owner_due > 0: the payee of the owner (`utils.paid`: the
///        owner_credential while the agent's record declares it, else the
///        record's controller key) receives at least owner_due AP3X. The
///        agent's record is a reference input (any status)
///   7. No challenge locks the stake (challenge_locks is empty), and the
///      transaction opens or closes no challenge. Decay cannot lower the
///      stake that an open challenge can slash
pub fn validate_claim_decay_refund(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  input_datum: StakeDatum,
  inactive_epochs: Int,
) -> Bool {
  // Calculate expected amounts
  let decay_amount =
    calculate_decay(input_datum.stake_amount, inactive_epochs, params.decay_rate)
  let collector_fee =
    calculate_collector_fee(decay_amount, params.decay_collector_fee)
  let treasury_amount = decay_amount - collector_fee
  let remaining = input_datum.stake_amount - decay_amount
  let new_amount =
    if remaining >= params.min_self_stake {
      remaining
    } else {
      0
    }
  let owner_due = remaining - new_amount

  and {
    // Phase 1.0: Oracle must sign to attest inactivity
    has_key_signed(tx, cfg.oracle_credential),
    decay_amount > 0,
    inactive_epochs > 0,
    input_datum.challenge_locks == [],
    no_challenge_change(tx, refs.endorsement_policy_id),
    stake_continues(tx, cfg, refs, input_datum, new_amount),
    verify_treasury_payment(tx.outputs, cfg, treasury_amount),
    collector_fee <= 0 || verify_collector_payment(tx, cfg, refs, collector_fee),
    owner_due <= 0 || paid(
      cfg,
      tx,
      input_datum.agent_did,
      input_datum.owner_credential,
      owner_due,
    ),
  }
}

/// True when an output continues the stake after a decay: at the exact
/// reputation address, with exactly one stake token of the agent, at least
/// `new_amount` AP3X, and the datum of the spent stake with
/// stake_amount = new_amount. Only last_updated can change.
fn stake_continues(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  new_amount: Int,
) -> Bool {
  let stake_token = derive_stake_token_name(input_datum.agent_did)
  let address = exact_address(refs.reputation_policy_id)
  list.any(
    tx.outputs,
    fn(output) {
      if and {
        output.address == address,
        assets.quantity_of(output.value, refs.reputation_policy_id, stake_token) == 1,
      } {
        expect InlineDatum(raw_out) = output.datum
        expect out_datum: StakeDatum = raw_out
        and {
          out_datum == StakeDatum {
            ..input_datum,
            stake_amount: new_amount,
            last_updated: out_datum.last_updated,
          },
          get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name) >= new_amount,
        }
      } else {
        False
      }
    },
  )
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
