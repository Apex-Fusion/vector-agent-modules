/// Reputation Validator — Multi-Validator
///
/// Controls self-stake lifecycle and history bonus tokens:
///   - Minting policy: MintStakeToken, MintHistoryBonus, BurnGenesisBonus
///   - Spend validator: CreateStake, IncreaseStake, DecreaseStake,
///     UpdateCapabilities, ClaimDecayRefund, SlashStake, LockForChallenge,
///     ReleaseChallenge
///
/// Because both handlers share the same script hash:
///   policy_id == script_hash == payment_credential of the reputation address
///
/// Each mint redeemer accepts exactly its own token set: one token of its
/// kind, and no other token of the policy. A stake token is minted only
/// together with the spend of a seed input with CreateStake, and no
/// redeemer burns it: every spend of a stake continues it.

use aiken/interval.{Finite}
use aiken/collection/list
use aiken/collection/pairs
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Spend, Transaction,
}
use reputation_staking/challenge_validation
use reputation_staking/config.{ReputationConfig, get_cross_refs}
use reputation_staking/decay
use reputation_staking/params.{ProtocolParams, find_protocol_params}
use reputation_staking/stake_validation
use reputation_staking/types.{
  AuditClaimWon, BurnGenesisBonus, ChallengeWon, ClaimDecayRefund, CreateStake,
  DecreaseStake, GenesisBonus, HistoryBonusDatum, IncreaseStake, JurorDuty,
  LockForChallenge, MintHistoryBonus, MintStakeToken, ReleaseChallenge,
  SlashStake, StakeAction, StakeDatum, StakeMintAction, UpdateCapabilities,
  genesis_bonus_token_prefix, history_bonus_token_prefix, stake_token_prefix,
}
use reputation_staking/utils.{
  count_script_inputs, derive_stake_token_name, exact_address, has_key_signed,
  has_script_input, single_token_change, starts_with,
}
use shared/identity

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Find the continuing output at the exact reputation address for a given
/// agent: it holds the agent's stake token. Every StakeUTXO stays at the
/// exact address, so that a challenge can find it (Part 2 §7, Module-3 row).
fn find_continuing_output(
  tx: Transaction,
  reputation_policy_id: PolicyId,
  agent_did: ByteArray,
) -> Option<Output> {
  let stake_token = derive_stake_token_name(agent_did)
  let address = exact_address(reputation_policy_id)
  list.find(
    tx.outputs,
    fn(output) {
      output.address == address && assets.quantity_of(
        output.value,
        reputation_policy_id,
        stake_token,
      ) == 1
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

/// The input that the transaction spends at `own_ref`.
fn find_own_input(own_ref: OutputReference, tx: Transaction) -> Input {
  expect Some(own_input) =
    list.find(tx.inputs, fn(i) { i.output_reference == own_ref })
  own_input
}

/// True when the transaction spends an input at the reputation script with
/// the redeemer CreateStake. The spend handler then checks the new stake and
/// the name of the minted stake token.
fn creates_stake(tx: Transaction, policy_id: PolicyId) -> Bool {
  let create: Data = CreateStake
  list.any(
    tx.inputs,
    fn(input) {
      input.output.address.payment_credential == Script(policy_id) && pairs.get_first(
        tx.redeemers,
        Spend(input.output_reference),
      ) == Some(create)
    },
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// History Bonus & Genesis Validation (F-NEW-6 fix)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the history bonus mint by checking the bonus output datum source.
/// Dispatches to source-specific validation (Module 1 claim won, jury duty, etc.)
///
/// The bonus agent's identity record is Active over the validity range (at
/// most one hour) and its controller signed the transaction (Part 2 §7 of
/// `docs/IDENTITY_REGISTRY.md` in agents-marketplace, Module-3 row).
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

  // The agent's identity record is Active and its controller signed
  let agent_ok =
    when identity.window(tx.validity_range) is {
      Some(w) ->
        identity.controller_opens(
          tx,
          cfg.identity_hash,
          bonus_datum.agent_did,
          w,
          [],
        )
      None -> False
    }

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
      // Other module sources: require oracle attestation (already enforced above)
      _ -> True
    }

  agent_ok && source_valid
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
  /// Each redeemer accepts exactly one token of its kind and no other token
  /// of the policy.
  mint(redeemer: StakeMintAction, policy_id: PolicyId, tx: Transaction) {
    let own_mint = assets.tokens(tx.mint, policy_id)

    when redeemer is {
      MintStakeToken ->
        // P-1: one stake token, minted only with a CreateStake spend, which
        // checks its name against the new stake datum
        single_token_change(own_mint, stake_token_prefix, 1) && creates_stake(
          tx,
          policy_id,
        )
      MintHistoryBonus -> {
        // F-NEW-6 fix: Require oracle signature AND source-specific validation
        let params =
          find_protocol_params(
            tx.reference_inputs,
            cfg.params_script_hash,
            cfg.params_token_policy,
          )
        let refs = get_cross_refs(cfg, tx.reference_inputs)
        let oracle_signed = has_key_signed(tx, cfg.oracle_credential)
        let has_valid_token =
          single_token_change(own_mint, history_bonus_token_prefix, 1)
        // History bonus is minted during DistributeOutcome, which spends an
        // endorsement UTXO (not a reputation UTXO). Check endorsement script
        // input instead of reputation script input.
        let endorsement_input_exists =
          has_script_input(tx, refs.endorsement_policy_id)
        // Find the history bonus output to determine source
        let bonus_valid =
          validate_history_bonus_mint(tx, cfg, params, policy_id)
        endorsement_input_exists && oracle_signed && has_valid_token && bonus_valid
      }
      BurnGenesisBonus -> {
        // F1 fix: Require script input to prevent orphan burns
        // Genesis burn: verify protection period has elapsed
        let params =
          find_protocol_params(
            tx.reference_inputs,
            cfg.params_script_hash,
            cfg.params_token_policy,
          )
        expect Some(current_slot) = get_validity_lower_bound(tx)
        let has_valid_burn =
          single_token_change(own_mint, genesis_bonus_token_prefix, -1)
        // Verify the genesis bonus being burned has passed protection period
        let protection_met =
          validate_genesis_burn(tx, policy_id, params, current_slot)
        has_script_input(tx, policy_id) && has_valid_burn && protection_met
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
      find_protocol_params(
        tx.reference_inputs,
        cfg.params_script_hash,
        cfg.params_token_policy,
      )

    // Derive own script hash at runtime (no circular dependency)
    let own_input = find_own_input(own_utxo, tx)
    expect Script(own_hash) = own_input.output.address.payment_credential

    // Look up cross-validator refs from authenticated reference input
    let refs = get_cross_refs(cfg, tx.reference_inputs)

    // F5 fix: Prevent double satisfaction — only one script input allowed
    expect count_script_inputs(tx, own_hash) == 1

    // The continuing StakeUTXO of this agent and its datum
    let continuing =
      fn() {
        expect Some(output) =
          find_continuing_output(tx, own_hash, stake_datum.agent_did)
        expect InlineDatum(raw) = output.datum
        expect out_datum: StakeDatum = raw
        (output, out_datum)
      }

    when redeemer is {
      CreateStake -> {
        let (output, out_datum) = continuing()
        stake_validation.validate_create_stake(
          tx, cfg, refs, params, own_input, out_datum, output,
        )
      }
      IncreaseStake -> {
        let (output, out_datum) = continuing()
        stake_validation.validate_increase_stake(
          tx, cfg, refs, stake_datum, out_datum, output,
        )
      }
      DecreaseStake { amount } -> {
        let (output, out_datum) = continuing()
        stake_validation.validate_decrease_stake(
          tx, cfg, refs, params, stake_datum, out_datum, output, amount,
        )
      }
      UpdateCapabilities { new_capabilities } -> {
        let (output, out_datum) = continuing()
        stake_validation.validate_update_capabilities(
          tx, cfg, refs, params, stake_datum, out_datum, output, new_capabilities,
        )
      }
      ClaimDecayRefund -> {
        // Derive current slot from validity range lower bound
        expect Some(current_slot) = get_validity_lower_bound(tx)
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
      SlashStake { challenge_ref } -> {
        // F-A2 fix: Permissionless stake slashing after CapabilityFalsified.
        // The challenge UTxO is in tx.inputs (consumed by DistributeOutcome
        // on the endorsement validator in the same transaction).
        let (output, out_datum) = continuing()
        stake_validation.validate_slash_stake(
          tx, cfg, refs, stake_datum, out_datum, output, challenge_ref,
        )
      }
      LockForChallenge -> {
        let (output, out_datum) = continuing()
        stake_validation.validate_lock_for_challenge(
          tx, refs, own_input, stake_datum, out_datum, output,
        )
      }
      ReleaseChallenge { challenge_ref } -> {
        let (output, out_datum) = continuing()
        stake_validation.validate_release_challenge(
          tx, refs, own_input, stake_datum, out_datum, output, challenge_ref,
        )
      }
    }
  }

  else(_ctx) {
    fail @"Unsupported script purpose"
  }
}
