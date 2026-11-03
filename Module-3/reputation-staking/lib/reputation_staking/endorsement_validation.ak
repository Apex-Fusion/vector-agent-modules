/// Endorsement validation logic for Module 3: Reputation Staking.
///
/// Validates endorsement operations: MintEndorsementToken, IncreaseEndorsement,
/// and WithdrawEndorsement.
/// See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.3, 6.3b, 6.4.
///
/// Identity registry checks (Part 2 §7 of `docs/IDENTITY_REGISTRY.md` in
/// agents-marketplace, Module-3 row): creating or increasing an endorsement
/// needs the endorser's record with effective status Active and the
/// endorser's controller signature (in addition to the endorser_credential
/// signature), and the target's record with effective status Active that
/// lists each endorsed capability. At creation, endorser_credential is a
/// declared credential of the endorser's record. Withdrawing (an exit) needs
/// only the endorser's controller signature; the record can have any status,
/// and the payout goes to a key that the record trusts (`utils.exit_paid`).
/// Each of these actions has a validity range of at most one hour.
///
/// Bound stake: an endorsement backs one stake of the target, the stake
/// whose stake_id is target_stake (`utils.bound_stake`). Creation reads the
/// endorsed capabilities from it. A withdrawal needs it as a reference input
/// without challenge locks. A challenge against that stake adds its lock to
/// the stake when it opens and removes it when it closes, and at most one
/// output carries the stake_id, so the transaction cannot show another
/// version of the stake: no endorsement of a challenged stake can be
/// withdrawn while the challenge is open.

use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{Output, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{EndorsementDatum}
use reputation_staking/utils.{
  bound_stake, derive_endorsement_token_name, exit_paid, get_ap3x_amount,
  has_credential_signed, is_non_empty, is_subset, opens_with,
}
use shared/identity
use shared/identity.{Window}

// ──────────────────────────────────────────────────────────────────────────────
// MintEndorsementToken (Section 6.3)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate creation of a new endorsement.
///
/// MUST:
///   1. The endorser's identity record is Active over the validity range,
///      the endorser's controller signed, endorser_credential is a declared
///      credential of the record, and endorser_credential signed
///   2. The target's identity record is Active over the validity range and
///      lists each endorsed capability; the bound stake (the target stake
///      whose stake_id is target_stake) is a reference input
///   3. endorser_did != target_did (cannot self-endorse)
///   4. Endorsement token asset name = deterministic hash of pair, and the
///      transaction mints exactly that one token of the endorsement policy
///   5. stake_amount >= MIN_ENDORSEMENT
///   6. endorsed_capabilities is non-empty and a subset of the staked
///      capabilities of the bound stake
///   7. EndorsementUTXO created at endorsement validator address
///   8. The validity range is at most one hour long
///   9. created_at is the lower bound of the validity range
///  10. slashed_by is empty
pub fn validate_mint_endorsement(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  output_datum: EndorsementDatum,
  output: Output,
) -> Bool {
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

  // Rules 2 and 6: Capabilities non-empty and subset of the staked
  // capabilities of the bound stake (a reference input; the function fails
  // when there is none)
  let has_capabilities = is_non_empty(output_datum.endorsed_capabilities)
  let capabilities_valid =
    is_subset(
      output_datum.endorsed_capabilities,
      bound_stake(
        tx.reference_inputs,
        refs.reputation_policy_id,
        output_datum.target_did,
        output_datum.target_stake,
      ).staked_capabilities,
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

  // Rules 1, 2, 8 and 9: identity records, signature and created_at
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        opens_with(
          tx,
          cfg.identity_hash,
          output_datum.endorser_did,
          w,
          [],
          output_datum.endorser_credential,
        ),
        target_active_with(tx, cfg, output_datum, w),
        output_datum.created_at == w.lo,
        not_self_endorse,
        has_correct_token,
        dict.to_pairs(assets.tokens(tx.mint, refs.endorsement_policy_id)) == [
          Pair(expected_token_name, 1),
        ],
        token_is_new,
        meets_minimum,
        has_capabilities,
        capabilities_valid,
        at_validator,
        value_correct,
        has_credential_signed(tx, output_datum.endorser_credential),
        output_datum.slashed_by == [],
      }
    None -> False
  }
}

/// The target of an endorsement: its record is Active over `w` and lists
/// each endorsed capability. The target does not sign.
fn target_active_with(
  tx: Transaction,
  cfg: ReputationConfig,
  datum: EndorsementDatum,
  w: Window,
) -> Bool {
  identity.active_with_capabilities(
    tx.reference_inputs,
    cfg.identity_hash,
    datum.target_did,
    w,
    datum.endorsed_capabilities,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseEndorsement (Section 6.3b)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate increasing an existing endorsement.
///
/// MUST:
///   1. Existing EndorsementUTXO consumed (with endorsement token)
///   2. The continuing datum is the spent datum with a larger stake_amount
///      (endorser, credential, target, capabilities, created_at and
///      slashed_by stay)
///   3. Additional AP3X provided
///   4. The endorser's record is Active and its controller signed; the
///      target's record is Active and lists each endorsed capability; the
///      validity range is at most one hour long
///   5. Transaction signed by endorser_credential
pub fn validate_increase_endorsement(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: EndorsementDatum,
  output_datum: EndorsementDatum,
  output: Output,
) -> Bool {
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

  // Rule 4: identity records and the endorser's controller signature
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        identity.controller_opens(
          tx,
          cfg.identity_hash,
          input_datum.endorser_did,
          w,
          [],
        ),
        target_active_with(tx, cfg, input_datum, w),
        // Rule 2
        output_datum == EndorsementDatum {
          ..input_datum,
          stake_amount: output_datum.stake_amount,
        },
        output_datum.stake_amount > input_datum.stake_amount,
        value_correct,
        has_token,
        at_validator,
        has_credential_signed(tx, input_datum.endorser_credential),
      }
    None -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawEndorsement (Section 6.4)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate withdrawal of an endorsement (an exit).
///
/// MUST:
///   1. Endorsement exists (endorsement token present in input)
///   2. created_at + ENDORSEMENT_COOLDOWN <= current slot
///   3. The bound stake (`utils.bound_stake`: the target stake whose
///      stake_id is target_stake) is a reference input and no challenge
///      locks it (challenge_locks is empty). A challenge against the bound
///      stake keeps its lock in the stake from its opening to its closing,
///      so the withdrawal waits until every such challenge is closed
///   4. The outputs to the payee hold at least stake_amount AP3X in total.
///      The payee is endorser_credential when the endorser's record still
///      trusts it, else the controller key of the record
///      (`utils.exit_paid`)
///   5. Endorsement token burned
///   6. Transaction signed by the controller of the endorser's identity
///      record. The record can have any status (also a revoked or retired
///      record), and the validity range is at most one hour.
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

  // Rules 4 and 6: signed by the endorser's controller (no status gate),
  // paid to a key that the record trusts
  when identity.window(tx.validity_range) is {
    Some(_) -> and {
        exit_paid(
          tx,
          cfg,
          input_datum.endorser_did,
          input_datum.endorser_credential,
          input_datum.stake_amount,
        ),
        cooldown_met,
        // Rule 3: no open challenge against the bound stake
        bound_stake(
          tx.reference_inputs,
          refs.reputation_policy_id,
          input_datum.target_did,
          input_datum.target_stake,
        ).challenge_locks == [],
        token_burned,
      }
    None -> False
  }
}
