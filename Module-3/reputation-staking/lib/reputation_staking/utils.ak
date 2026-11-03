/// Utility functions for Module 3: Reputation Staking.
///
/// Credential checks and byte helpers delegate to the shared library
/// (shared/). Token name derivation and AP3X helpers remain Module 3–specific.
/// Identity registry checks are in shared/identity; the exit and opening
/// rules of Module 3 (`exit_paid`, `opens_with`) use them.
///
/// Validation code imports these helpers from this single place.

use aiken/builtin
use aiken/collection/dict.{Dict}
use aiken/collection/list
use aiken/crypto.{blake2b_224}
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, OutputReference, Transaction}
use reputation_staking/config.{ReputationConfig}
use reputation_staking/types.{
  StakeDatum, challenge_token_prefix, endorsement_token_prefix,
  history_bonus_token_prefix, stake_token_prefix,
}
use shared/bytearray as shared_bytearray
use shared/credential as shared_credential
use shared/identity.{Window}
use shared/token_naming
use shared/utxo
use vector_identity.{controller_signed, find_record}
use vector_identity/types.{IdentityDatum} as identity_types

// ──────────────────────────────────────────────────────────────────────────────
// Credential / signature checks
// ──────────────────────────────────────────────────────────────────────────────

/// Check that the transaction is signed by the given credential.
/// Only supports VerificationKey credentials (not Script credentials for signing).
pub fn has_credential_signed(tx: Transaction, credential: Credential) -> Bool {
  when credential is {
    VerificationKey(hash) -> list.has(tx.extra_signatories, hash)
    Script(_) -> False
  }
}

/// Check that the transaction is signed by a specific verification key hash.
pub fn has_key_signed(tx: Transaction, key_hash: ByteArray) -> Bool {
  shared_credential.has_key_signed(tx.extra_signatories, key_hash)
}

// ──────────────────────────────────────────────────────────────────────────────
// Identity rules of positions (Part 2 §7, Module-3 row)
// ──────────────────────────────────────────────────────────────────────────────

/// An action by agent `id` that opens a position with the payout credential
/// `credential`: the record of `id` is Active over `w`, it lists each of
/// `capabilities`, its controller signed the transaction, and `credential`
/// is a declared credential of the record (`identity.declared_credential`:
/// the controller key or a listed key with the transaction purpose).
pub fn opens_with(
  tx: Transaction,
  identity_hash: ByteArray,
  id: ByteArray,
  w: Window,
  capabilities: List<ByteArray>,
  credential: Credential,
) -> Bool {
  when identity.active_record(tx.reference_inputs, identity_hash, id, w) is {
    Some(record) -> and {
        identity.lists_capabilities(record, capabilities),
        controller_signed(record, tx),
        identity.declared_credential(record, credential),
      }
    None -> False
  }
}

/// The payee of a payout to a position whose stored credential is `stored`,
/// for the record of the position's agent (the payee rule): `stored` when
/// the record still declares it (`identity.declared_credential`: the
/// controller key or a listed key with the transaction purpose), else the
/// controller key of the record. So after a controller change, a recovery
/// or a key removal, the funds go to a key that the record trusts now.
fn record_payee(record: IdentityDatum, stored: Credential) -> Credential {
  if identity.declared_credential(record, stored) {
    stored
  } else {
    VerificationKey(blake2b_224(record.controller))
  }
}

/// The payee of a payout to a position of agent `id` whose stored credential
/// is `stored` (`record_payee`). The record of `id` is a reference input
/// (`find_record`, any status, also a tombstone); None without it.
pub fn payee(
  cfg: ReputationConfig,
  tx: Transaction,
  id: ByteArray,
  stored: Credential,
) -> Option<Credential> {
  when find_record(tx.reference_inputs, cfg.identity_hash, id) is {
    Some(record) -> Some(record_payee(record, stored))
    None -> None
  }
}

/// True when the outputs to the payee of agent `id` and the stored
/// credential `stored` (`payee`) hold at least `amount` AP3X in total. False
/// without the record of `id`. The permissionless payouts of positions use
/// it (ExpireChallenge, DistributeOutcome, SlashEndorsement,
/// ClaimDecayRefund); the exits use `exit_paid`.
pub fn paid(
  cfg: ReputationConfig,
  tx: Transaction,
  id: ByteArray,
  stored: Credential,
  amount: Int,
) -> Bool {
  when payee(cfg, tx, id, stored) is {
    Some(to) -> ap3x_paid_to(cfg, tx.outputs, to) >= amount
    None -> False
  }
}

/// An exit of a position of agent `id` that returns `amount` AP3X to its
/// owner at the owner's request (rule D-exit): the record of `id` is a
/// reference input (any status, also a tombstone), its controller signed
/// the transaction, and the outputs to the payee (`record_payee`) hold at
/// least `amount` AP3X in total.
pub fn exit_paid(
  tx: Transaction,
  cfg: ReputationConfig,
  id: ByteArray,
  stored: Credential,
  amount: Int,
) -> Bool {
  when find_record(tx.reference_inputs, cfg.identity_hash, id) is {
    Some(record) ->
      controller_signed(record, tx) && ap3x_paid_to(
        cfg,
        tx.outputs,
        record_payee(record, stored),
      ) >= amount
    None -> False
  }
}

/// The bound stake of an endorsement: the datum of the reference input at
/// the exact reputation address that holds the stake token of `target_did`
/// and whose datum has `stake_id`. At most one output carries a stake_id
/// with the stake token, so the transaction cannot name another version of
/// the stake. Fails when there is no such reference input.
pub fn bound_stake(
  reference_inputs: List<Input>,
  reputation_policy: ByteArray,
  target_did: ByteArray,
  stake_id: OutputReference,
) -> StakeDatum {
  let address = exact_address(reputation_policy)
  let token = derive_stake_token_name(target_did)
  expect Some(stake) =
    list.find_map(
      reference_inputs,
      fn(input) {
        if and {
          input.output.address == address,
          assets.quantity_of(input.output.value, reputation_policy, token) == 1,
        } {
          expect InlineDatum(raw) = input.output.datum
          expect stake: StakeDatum = raw
          if stake.stake_id == stake_id {
            Some(stake)
          } else {
            None
          }
        } else {
          None
        }
      },
    )
  stake
}

// ──────────────────────────────────────────────────────────────────────────────
// ByteArray helpers (delegates to shared/bytearray)
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that a list is non-empty.
pub fn is_non_empty(items: List<a>) -> Bool {
  shared_bytearray.is_non_empty(items)
}

/// Verify that all items in `subset` also appear in `superset`.
pub fn is_subset(subset: List<ByteArray>, superset: List<ByteArray>) -> Bool {
  shared_bytearray.is_subset(subset, superset)
}

/// Check if a ByteArray starts with a given prefix.
pub fn starts_with(data: ByteArray, prefix: ByteArray) -> Bool {
  shared_bytearray.starts_with(data, prefix)
}

// ──────────────────────────────────────────────────────────────────────────────
// Addresses, tokens and payouts
// ──────────────────────────────────────────────────────────────────────────────

/// The exact address of a script: payment credential `Script(hash)` and no
/// stake credential (the address that the deployment uses).
pub fn exact_address(hash: ByteArray) -> Address {
  Address { payment_credential: Script(hash), stake_credential: None }
}

/// True when the value holds exactly one token of `policy` in total: `name`
/// with quantity 1.
pub fn holds_single_token(
  value: assets.Value,
  policy: ByteArray,
  name: ByteArray,
) -> Bool {
  dict.to_pairs(assets.tokens(value, policy)) == [Pair(name, 1)]
}

/// True when `tokens` (the tokens of one policy) is exactly one token whose
/// name starts with `prefix`, with quantity `quantity`. A mint redeemer uses
/// it so that the transaction mints or burns no other token of the policy.
pub fn single_token_change(
  tokens: Dict<ByteArray, Int>,
  prefix: ByteArray,
  quantity: Int,
) -> Bool {
  when dict.to_pairs(tokens) is {
    [Pair(name, q)] -> q == quantity && starts_with(name, prefix)
    _ -> False
  }
}

/// True when the transaction mints or burns no challenge token (a token of
/// `endorsement_policy` whose name starts with the challenge prefix).
pub fn no_challenge_change(
  tx: Transaction,
  endorsement_policy: ByteArray,
) -> Bool {
  list.all(
    dict.keys(assets.tokens(tx.mint, endorsement_policy)),
    fn(name) { !starts_with(name, challenge_token_prefix) },
  )
}

/// The AP3X that the outputs to `credential` hold in total. Payout checks
/// add up all outputs to the payee.
pub fn ap3x_paid_to(
  cfg: ReputationConfig,
  outputs: List<Output>,
  credential: Credential,
) -> Int {
  list.foldl(
    outputs,
    0,
    fn(output, total) {
      if output.address.payment_credential == credential {
        total + get_ap3x_amount(
          output.value,
          cfg.ap3x_policy_id,
          cfg.ap3x_asset_name,
        )
      } else {
        total
      }
    },
  )
}

/// True when the outputs at the protocol treasury hold at least `amount` AP3X
/// in total (always true for an amount of zero or less).
pub fn treasury_paid(
  cfg: ReputationConfig,
  outputs: List<Output>,
  amount: Int,
) -> Bool {
  amount <= 0 || ap3x_paid_to(cfg, outputs, Script(cfg.treasury_script_hash)) >= amount
}

// ──────────────────────────────────────────────────────────────────────────────
// Token name derivation (Module 3–specific prefixes, uses shared/token_naming)
// ──────────────────────────────────────────────────────────────────────────────

/// Derive the stake token asset name: "rstk_" ++ blake2b_256(agent_did)[0..28]
pub fn derive_stake_token_name(agent_did: ByteArray) -> ByteArray {
  // 5-byte prefix + 27-byte hash slice = 32 bytes (Cardano max asset name)
  token_naming.derive_token_name(stake_token_prefix, agent_did, 27)
}

/// Derive the endorsement token asset name:
/// "rend_" ++ blake2b_256(endorser_did ++ target_did)[0..28]
pub fn derive_endorsement_token_name(
  endorser_did: ByteArray,
  target_did: ByteArray,
) -> ByteArray {
  let combined = builtin.append_bytearray(endorser_did, target_did)
  // 5-byte prefix + 27-byte hash slice = 32 bytes (Cardano max asset name)
  token_naming.derive_token_name(endorsement_token_prefix, combined, 27)
}

/// Derive the challenge token asset name:
/// "rchl_" ++ blake2b_256(challenger_did ++ target_did ++ capability)[0..24]
pub fn derive_challenge_token_name(
  challenger_did: ByteArray,
  target_did: ByteArray,
  capability: ByteArray,
) -> ByteArray {
  let combined =
    builtin.append_bytearray(
      builtin.append_bytearray(challenger_did, target_did),
      capability,
    )
  token_naming.derive_token_name(challenge_token_prefix, combined, 24)
}

/// Derive the history bonus token asset name:
/// "hbonus_" ++ blake2b_256(source_ref_serialized)[0..24]
pub fn derive_history_bonus_token_name(source_ref: OutputReference) -> ByteArray {
  let ref_bytes = token_naming.serialise_output_reference(source_ref)
  token_naming.derive_token_name(history_bonus_token_prefix, ref_bytes, 24)
}

// ──────────────────────────────────────────────────────────────────────────────
// AP3X value helpers (Module 3–specific)
// ──────────────────────────────────────────────────────────────────────────────

/// Get the AP3X amount in an output's value.
pub fn get_ap3x_amount(
  value: assets.Value,
  ap3x_policy_id: assets.PolicyId,
  ap3x_asset_name: ByteArray,
) -> Int {
  assets.quantity_of(value, ap3x_policy_id, ap3x_asset_name)
}

// ──────────────────────────────────────────────────────────────────────────────
// UTXO search helpers (delegates to shared/utxo)
// ──────────────────────────────────────────────────────────────────────────────

/// Find a UTXO in the transaction inputs that holds the specified token.
pub fn find_input_with_token(
  inputs: List<Input>,
  policy_id: assets.PolicyId,
  asset_name: ByteArray,
) -> Input {
  utxo.find_input_with_token(inputs, policy_id, asset_name)
}

/// Count outputs at a specific script address.
pub fn count_outputs_at_script(
  outputs: List<transaction.Output>,
  script_hash: ByteArray,
) -> Int {
  utxo.count_outputs_at_script(outputs, script_hash)
}

/// Check that at least one transaction input comes from the given script address.
pub fn has_script_input(tx: Transaction, script_hash: ByteArray) -> Bool {
  utxo.has_script_input(tx.inputs, script_hash)
}

/// Count how many transaction inputs come from the given script address.
pub fn count_script_inputs(tx: Transaction, script_hash: ByteArray) -> Int {
  utxo.count_script_inputs(tx.inputs, script_hash)
}
