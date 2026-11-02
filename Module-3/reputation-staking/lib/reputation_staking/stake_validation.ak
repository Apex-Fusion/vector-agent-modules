/// Stake validation logic for Module 3: Reputation Staking.
///
/// Validates all self-stake operations: CreateStake, IncreaseStake,
/// DecreaseStake, UpdateCapabilities, SlashStake, LockForChallenge and
/// ReleaseChallenge.
/// See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.1, 6.2, 6.5.
///
/// Identity registry checks (Part 2 §7 of `docs/IDENTITY_REGISTRY.md` in
/// agents-marketplace, Module-3 row): CreateStake, IncreaseStake and
/// UpdateCapabilities need the agent's record with effective status Active
/// over the validity range, each staked capability in the record, and the
/// controller's signature, in addition to the owner_credential signature.
/// At CreateStake, owner_credential is a declared credential of the record.
/// DecreaseStake (an exit) needs only the controller's signature; the record
/// can have any status, and the payout goes to a key that the record trusts
/// (`utils.exit_paid`). Each of these actions has a validity range of at
/// most one hour (shared/identity.window).
///
/// Time rule of the stake: an owner action records the upper bound `hi` of
/// its validity range in last_updated, and the cooldown compares
/// last_updated with the lower bound `lo` of a later transaction. So the
/// cooldown cannot be shorter than `stake_cooldown`.
///
/// Challenge locks: a challenge against the stake adds the name of its
/// challenge token to challenge_locks when it opens (LockForChallenge) and
/// removes it when it closes (ReleaseChallenge or SlashStake). DecreaseStake,
/// UpdateCapabilities and ClaimDecayRefund need an empty list, and the owner
/// actions cannot open or close a challenge in the same transaction. The
/// spender cannot hide a lock, because the lock is in the stake datum.

use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, OutputReference, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{
  CapabilityFalsified, Challenge, EndorsementValidatorDatum, RepChallengeState,
  Resolved, StakeDatum, challenge_token_prefix,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name, exact_address,
  exit_paid, get_ap3x_amount, has_credential_signed, holds_single_token,
  is_non_empty, no_challenge_change, opens_with, starts_with,
}
use shared/identity

// ──────────────────────────────────────────────────────────────────────────────
// CreateStake (Section 6.1)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate creation of a new self-stake.
///
/// MUST:
///   1. The agent's identity record (agent_did) is Active over the validity
///      range, which is at most one hour long, it lists each staked
///      capability, and its controller signed
///   2. owner_credential is a declared credential of the record (the
///      controller key or a listed key with the transaction purpose) and
///      signed the transaction
///   3. The spent input holds no reputation token. The transaction mints
///      exactly one reputation token, the stake token of agent_did, and the
///      output holds exactly that reputation token
///   4. stake_amount >= MIN_SELF_STAKE and staked_capabilities is non-empty
///   5. The output is at the exact reputation address and holds at least
///      stake_amount AP3X
///   6. last_updated = upper bound of the validity range
///   7. history_points = 0 and challenge_locks is empty
pub fn validate_create_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  own_input: Input,
  output_datum: StakeDatum,
  output: Output,
) -> Bool {
  let stake_token = derive_stake_token_name(output_datum.agent_did)
  let reputation = refs.reputation_policy_id

  // Rules 1, 2 and 6: identity record, capabilities, signatures and time
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        opens_with(
          tx,
          cfg.identity_hash,
          output_datum.agent_did,
          w,
          output_datum.staked_capabilities,
          output_datum.owner_credential,
        ),
        has_credential_signed(tx, output_datum.owner_credential),
        output_datum.last_updated == w.hi,
        // Rule 3: a new stake token, not a spent stake
        dict.is_empty(assets.tokens(own_input.output.value, reputation)),
        dict.to_pairs(assets.tokens(tx.mint, reputation)) == [
          Pair(stake_token, 1),
        ],
        holds_single_token(output.value, reputation, stake_token),
        // Rule 4
        output_datum.stake_amount >= params.min_self_stake,
        is_non_empty(output_datum.staked_capabilities),
        // Rule 5
        output.address == exact_address(reputation),
        get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name) >= output_datum.stake_amount,
        // Rule 7
        output_datum.history_points == 0,
        output_datum.challenge_locks == [],
      }
    None -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseStake (Section 6.2)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate increasing an existing self-stake.
///
/// MUST:
///   1. Existing StakeUTXO consumed (with stake token)
///   2. The continuing datum is the spent datum with a larger stake_amount
///      and last_updated = upper bound of the validity range (the challenge
///      locks stay)
///   3. The output holds at least stake_amount AP3X and the stake token
///   4. The agent's identity record is Active over the validity range (at
///      most one hour), lists each staked capability, and its controller
///      signed
///   5. Transaction signed by owner_credential
///   6. The transaction opens or closes no challenge
pub fn validate_increase_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
) -> Bool {
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        identity.controller_opens(
          tx,
          cfg.identity_hash,
          input_datum.agent_did,
          w,
          input_datum.staked_capabilities,
        ),
        output_datum == StakeDatum {
          ..input_datum,
          stake_amount: output_datum.stake_amount,
          last_updated: w.hi,
        },
        output_datum.stake_amount > input_datum.stake_amount,
        continuing_stake_value(cfg, refs, input_datum, output, output_datum),
        has_credential_signed(tx, input_datum.owner_credential),
        no_challenge_change(tx, refs.endorsement_policy_id),
      }
    None -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// DecreaseStake (Section 6.2)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate decreasing an existing self-stake (an exit).
///
/// MUST:
///   1. Existing StakeUTXO consumed (with stake token)
///   2. The continuing datum is the spent datum with stake_amount reduced by
///      decrease_amount (> 0) and last_updated = upper bound of the validity
///      range; the new stake_amount is at least MIN_SELF_STAKE
///   3. COOLDOWN: last_updated + STAKE_COOLDOWN <= lower bound of the
///      validity range
///   4. No challenge locks the stake (challenge_locks is empty), and the
///      transaction opens or closes no challenge
///   5. The output holds at least the new stake_amount AP3X and the stake
///      token
///   6. The controller of the agent's identity record signed (the record can
///      have any status, also a revoked or retired record; the validity
///      range is at most one hour), and the outputs to the payee hold at
///      least decrease_amount AP3X in total. The payee is owner_credential
///      when the record still trusts it, else the controller key
///      (`utils.exit_paid`)
pub fn validate_decrease_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
  decrease_amount: Int,
) -> Bool {
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        exit_paid(
          tx,
          cfg,
          input_datum.agent_did,
          input_datum.owner_credential,
          decrease_amount,
        ),
        decrease_amount > 0,
        output_datum == StakeDatum {
          ..input_datum,
          stake_amount: input_datum.stake_amount - decrease_amount,
          last_updated: w.hi,
        },
        output_datum.stake_amount >= params.min_self_stake,
        input_datum.last_updated + params.stake_cooldown <= w.lo,
        input_datum.challenge_locks == [],
        no_challenge_change(tx, refs.endorsement_policy_id),
        continuing_stake_value(cfg, refs, input_datum, output, output_datum),
      }
    None -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// UpdateCapabilities (Section 6.5)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate updating the capabilities backed by a self-stake.
///
/// MUST:
///   1. Existing StakeUTXO consumed (with stake token)
///   2. new_capabilities is non-empty and each one is in the agent's
///      identity record
///   3. COOLDOWN: last_updated + STAKE_COOLDOWN <= lower bound of the
///      validity range
///   4. No challenge locks the stake (challenge_locks is empty), and the
///      transaction opens or closes no challenge. Adding a capability would
///      also lower the slash of an open challenge (stake / capabilities)
///   5. The continuing datum is the spent datum with staked_capabilities =
///      new_capabilities and last_updated = upper bound of the validity range
///   6. The output holds at least stake_amount AP3X and the stake token
///   7. The record is Active over the validity range (at most one hour) and
///      its controller signed
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
) -> Bool {
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        identity.controller_opens(
          tx,
          cfg.identity_hash,
          input_datum.agent_did,
          w,
          new_capabilities,
        ),
        is_non_empty(new_capabilities),
        input_datum.last_updated + params.stake_cooldown <= w.lo,
        input_datum.challenge_locks == [],
        no_challenge_change(tx, refs.endorsement_policy_id),
        output_datum == StakeDatum {
          ..input_datum,
          staked_capabilities: new_capabilities,
          last_updated: w.hi,
        },
        continuing_stake_value(cfg, refs, input_datum, output, output_datum),
        has_credential_signed(tx, input_datum.owner_credential),
      }
    None -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// SlashStake (F-A2 fix: permissionless stake slashing after CapabilityFalsified)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate slashing of an agent's stake after their capability was falsified.
///
/// Permissionless — the resolved challenge is verified on-chain.
/// The challenge UTxO is in tx.inputs (being consumed by DistributeOutcome
/// on the endorsement validator in the same transaction).
///
/// MUST:
///   1. The challenge closes in this transaction (`closing_challenge`): it
///      is an input at the endorsement validator that holds its challenge
///      token, it names this stake's agent as target, and the transaction
///      burns its token
///   2. Challenge state == Resolved { CapabilityFalsified }
///   3. The challenge locks this stake (its token name is in
///      challenge_locks)
///   4. Slash amount = stake_amount / num_staked_capabilities
///   5. The continuing datum is the spent datum with stake_amount reduced by
///      the slash and without the lock of the challenge. last_updated does
///      not change: a slash is not an action of the owner
///   6. The output holds at least the remaining stake AP3X and the stake
///      token
pub fn validate_slash_stake(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
  challenge_ref: OutputReference,
) -> Bool {
  // Rules 1 and 2
  let (name, state) =
    closing_challenge(tx, refs, challenge_ref, input_datum.agent_did)

  // Rule 4: Calculate slash amount
  let num_capabilities = list.length(input_datum.staked_capabilities)
  expect num_capabilities > 0
  let remaining =
    input_datum.stake_amount - input_datum.stake_amount / num_capabilities

  and {
    state == Resolved { outcome: CapabilityFalsified },
    // Rules 3 and 5
    output_datum == StakeDatum {
      ..input_datum,
      stake_amount: remaining,
      challenge_locks: without_lock(input_datum.challenge_locks, name),
    },
    // Rule 6
    continuing_stake_value(cfg, refs, input_datum, output, output_datum),
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Challenge locks
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the lock of a challenge that opens against this stake.
///
/// Permissionless: the endorsement policy (MintChallengeToken) checks the
/// challenge and needs this stake as a spent input.
///
/// MUST:
///   1. The transaction mints exactly one token of the endorsement policy,
///      a challenge token
///   2. Its name is not a lock of the stake yet (one open challenge per
///      challenger, target and capability)
///   3. The continuing datum is the spent datum with the name added to
///      challenge_locks
///   4. The output holds the same tokens as the spent stake and at least
///      its lovelace
pub fn validate_lock_for_challenge(
  tx: Transaction,
  refs: CrossValidatorRefs,
  own_input: Input,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
) -> Bool {
  when dict.to_pairs(assets.tokens(tx.mint, refs.endorsement_policy_id)) is {
    [Pair(name, 1)] -> and {
        starts_with(name, challenge_token_prefix),
        !list.has(input_datum.challenge_locks, name),
        output_datum == StakeDatum {
          ..input_datum,
          challenge_locks: [name, ..input_datum.challenge_locks],
        },
        keeps_value(own_input, output),
      }
    _ -> False
  }
}

/// Validate the release of the lock of a challenge that closes without the
/// outcome CapabilityFalsified (WithdrawChallenge, ExpireChallenge, or
/// DistributeOutcome of CapabilityVerified or Inconclusive).
///
/// Permissionless.
///
/// MUST:
///   1. The challenge closes in this transaction (`closing_challenge`)
///   2. Its state is not Resolved { CapabilityFalsified } (that outcome
///      closes through SlashStake)
///   3. The continuing datum is the spent datum without the lock of the
///      challenge (the lock must be there)
///   4. The output holds the same tokens as the spent stake and at least
///      its lovelace
pub fn validate_release_challenge(
  tx: Transaction,
  refs: CrossValidatorRefs,
  own_input: Input,
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  output: Output,
  challenge_ref: OutputReference,
) -> Bool {
  let (name, state) =
    closing_challenge(tx, refs, challenge_ref, input_datum.agent_did)
  and {
    state != Resolved { outcome: CapabilityFalsified },
    output_datum == StakeDatum {
      ..input_datum,
      challenge_locks: without_lock(input_datum.challenge_locks, name),
    },
    keeps_value(own_input, output),
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// The name of the challenge token and the state of the challenge that
/// closes in this transaction: the input at `challenge_ref` is at the
/// endorsement validator, holds its challenge token, names `agent_did` as
/// target, and the transaction burns its challenge token. Fails otherwise.
fn closing_challenge(
  tx: Transaction,
  refs: CrossValidatorRefs,
  challenge_ref: OutputReference,
  agent_did: ByteArray,
) -> (ByteArray, RepChallengeState) {
  expect Some(input) =
    list.find(tx.inputs, fn(i) { i.output_reference == challenge_ref })
  expect Script(hash) = input.output.address.payment_credential
  expect InlineDatum(raw) = input.output.datum
  expect Challenge(challenge): EndorsementValidatorDatum = raw
  let name =
    derive_challenge_token_name(
      challenge.challenger_did,
      challenge.target_did,
      challenge.challenged_capability,
    )
  expect and {
      hash == refs.endorsement_policy_id,
      challenge.target_did == agent_did,
      assets.quantity_of(input.output.value, hash, name) == 1,
      assets.quantity_of(tx.mint, hash, name) == -1,
    }
  (name, challenge.state)
}

/// The locks without one occurrence of `name`. Fails when `name` is not a
/// lock.
fn without_lock(locks: List<ByteArray>, name: ByteArray) -> List<ByteArray> {
  expect list.has(locks, name)
  list.delete(locks, name)
}

/// True when the continuing stake output holds at least the stake_amount of
/// its datum in AP3X and exactly one stake token of the agent.
fn continuing_stake_value(
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  input_datum: StakeDatum,
  output: Output,
  output_datum: StakeDatum,
) -> Bool {
  and {
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name) >= output_datum.stake_amount,
    assets.quantity_of(
      output.value,
      refs.reputation_policy_id,
      derive_stake_token_name(input_datum.agent_did),
    ) == 1,
  }
}

/// True when the output holds the same tokens as the spent input and at
/// least its lovelace (the lock actions move no AP3X).
fn keeps_value(own_input: Input, output: Output) -> Bool {
  and {
    assets.without_lovelace(output.value) == assets.without_lovelace(
      own_input.output.value,
    ),
    assets.lovelace_of(output.value) >= assets.lovelace_of(
      own_input.output.value,
    ),
  }
}
