//// Challenge locks at validator level (W4 items 1 and 4).
////
//// Opening a challenge spends the target's StakeUTXO, which adds the name of
//// the challenge token to challenge_locks (LockForChallenge). Every path
//// that closes the challenge spends the StakeUTXO again and removes the lock
//// (ReleaseChallenge, or SlashStake for CapabilityFalsified). Each test runs
//// the handlers of both validators on the same transaction.
////
//// The challenger is test_agent_did (controller 1, owner key 1); the target
//// is test_agent_did_2 (owner key 2) with a stake of 20 AP3X for two
//// capabilities.

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use endorsement
use reputation
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_identity_ref_input, make_input,
  make_output, make_output_no_datum, make_params_ref_input, script_address,
  test_agent_did, test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id,
  test_config, test_controller_hash, test_endorsement_policy_id,
  test_owner_key_hash, test_owner_key_hash_2, test_refs,
  test_refs_token_policy, test_reputation_policy_id, test_treasury_script_hash,
  test_tx_hash,
}
use reputation_staking/types.{
  BurnChallengeToken, CapabilityFalsified, CapabilityVerified, Challenge,
  ChallengeSpend, DecreaseStake, DistributeOutcome, Inconclusive,
  LockForChallenge, MintChallengeToken, Open, RepChallengeState,
  ReputationChallengeDatum, ReleaseChallenge, Resolved, SlashStake, StakeDatum,
  WithdrawChallenge,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
  derive_stake_token_name,
}

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

const t0: Int = 1_700_000_000_000

const challenge_stake: Int = 25_000_000

fn stake_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"5050505050505050505050505050505050505050505050505050505050505050",
    output_index: 0,
  }
}

fn challenge_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"7777777777777777777777777777777777777777777777777777777777777777",
    output_index: 0,
  }
}

fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

/// The target's stake with the challenge locks `locks`.
fn stake_datum(locks: List<ByteArray>) -> StakeDatum {
  StakeDatum {
    agent_did: test_agent_did_2,
    owner_credential: VerificationKey(test_owner_key_hash_2),
    stake_amount: 20_000_000,
    staked_capabilities: ["code_review", "testing"],
    last_updated: 500,
    history_points: 0,
    challenge_locks: locks,
  }
}

fn stake_value(ap3x: Int) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, ap3x)
    |> assets.add(
        test_reputation_policy_id,
        derive_stake_token_name(test_agent_did_2),
        1,
      )
}

fn stake_output(datum: StakeDatum, ap3x: Int) -> Output {
  make_output(script_address(test_reputation_policy_id), stake_value(ap3x), datum)
}

fn stake_input(datum: StakeDatum) -> Input {
  Input {
    output_reference: stake_ref(),
    output: stake_output(datum, datum.stake_amount),
  }
}

fn challenge_datum(state: RepChallengeState) -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did,
    challenger_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    target_credential: VerificationKey(test_owner_key_hash_2),
    challenged_capability: "code_review",
    stake_amount: challenge_stake,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at: t0,
    counter_evidence_hash: "",
    counter_evidence_uri: "",
    response_submitted_at: 0,
    state,
  }
}

fn challenge_output(datum: ReputationChallengeDatum) -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, challenge_stake)
      |> assets.add(test_endorsement_policy_id, challenge_token(), 1),
    Challenge(datum),
  )
}

fn challenge_input(datum: ReputationChallengeDatum) -> Input {
  Input { output_reference: challenge_ref(), output: challenge_output(datum) }
}

/// The CrossValidatorRefs record (holds a refs_token_policy token).
fn refs_ref_input() -> Input {
  make_input(
    test_tx_hash,
    400,
    make_output(
      key_address(test_owner_key_hash),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_refs_token_policy, "reputation_refs", 1),
      test_refs(),
    ),
  )
}

fn ap3x_to(address: address.Address, amount: Int) -> Output {
  make_output_no_datum(
    address,
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

fn spend_stake(
  datum: StakeDatum,
  redeemer: types.StakeAction,
  tx: Transaction,
) -> Bool {
  reputation.reputation.spend(test_config(), Some(datum), redeemer, stake_ref(), tx)
}

fn spend_challenge(
  datum: ReputationChallengeDatum,
  action: types.RepChallengeAction,
  tx: Transaction,
) -> Bool {
  endorsement.endorsement.spend(
    test_config(),
    Some(Challenge(datum)),
    ChallengeSpend(action),
    challenge_ref(),
    tx,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Opening a challenge locks the target's stake
// ──────────────────────────────────────────────────────────────────────────────

/// A transaction that opens the test challenge: it spends the target's stake
/// (`stake_in`), outputs the challenge and `stake_out`, and mints `mint`.
fn open_tx(stake_in: StakeDatum, stake_out: Output, mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    validity_range: interval.between(t0, t0 + 600_000),
    inputs: [stake_input(stake_in)],
    reference_inputs: [
      make_params_ref_input(),
      refs_ref_input(),
      make_identity_ref_input(test_agent_did, 1),
    ],
    outputs: [challenge_output(challenge_datum(Open)), stake_out],
    mint,
    extra_signatories: agent_signers(),
  }
}

fn challenge_mint() -> assets.Value {
  assets.from_asset(test_endorsement_policy_id, challenge_token(), 1)
}

test open_challenge_locks_target_stake() {
  let tx =
    open_tx(
      stake_datum([]),
      stake_output(stake_datum([challenge_token()]), 20_000_000),
      challenge_mint(),
    )
  and {
    endorsement.endorsement.mint(
      test_config(),
      MintChallengeToken,
      test_endorsement_policy_id,
      tx,
    ),
    spend_stake(stake_datum([]), LockForChallenge, tx),
  }
}

/// The continuing stake must hold the new lock.
test open_challenge_without_lock_fails() {
  let tx =
    open_tx(
      stake_datum([]),
      stake_output(stake_datum([]), 20_000_000),
      challenge_mint(),
    )
  !spend_stake(stake_datum([]), LockForChallenge, tx)
}

/// One open challenge per challenger, target and capability: the stake
/// refuses a second lock with the same name.
test lock_with_name_already_locked_fails() {
  let locked = stake_datum([challenge_token()])
  let tx =
    open_tx(
      locked,
      stake_output(
        stake_datum([challenge_token(), challenge_token()]),
        20_000_000,
      ),
      challenge_mint(),
    )
  !spend_stake(locked, LockForChallenge, tx)
}

/// LockForChallenge needs a challenge token minted in the same transaction.
test lock_without_challenge_mint_fails() {
  let tx =
    open_tx(
      stake_datum([]),
      stake_output(stake_datum([challenge_token()]), 20_000_000),
      assets.zero,
    )
  !spend_stake(stake_datum([]), LockForChallenge, tx)
}

/// A minted endorsement token is not a challenge lock.
test lock_with_endorsement_token_mint_fails() {
  let endorsement_token =
    derive_endorsement_token_name(test_agent_did, test_agent_did_2)
  let tx =
    open_tx(
      stake_datum([]),
      stake_output(stake_datum([endorsement_token]), 20_000_000),
      assets.from_asset(test_endorsement_policy_id, endorsement_token, 1),
    )
  !spend_stake(stake_datum([]), LockForChallenge, tx)
}

/// The lock moves no AP3X out of the stake.
test lock_taking_ap3x_from_stake_fails() {
  let tx =
    open_tx(
      stake_datum([]),
      stake_output(stake_datum([challenge_token()]), 19_000_000),
      challenge_mint(),
    )
  !spend_stake(stake_datum([]), LockForChallenge, tx)
}

/// The owner cannot exit in the transaction that opens the challenge: the
/// owner actions refuse a challenge mint.
test decrease_stake_in_challenge_opening_fails() {
  let decreased =
    StakeDatum { ..stake_datum([]), stake_amount: 10_000_000, last_updated: t0 + 600_000 }
  let tx =
    Transaction {
      ..open_tx(stake_datum([]), stake_output(decreased, 10_000_000), challenge_mint()),
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        make_identity_ref_input(test_agent_did, 1),
        make_identity_ref_input(test_agent_did_2, 2),
      ],
      outputs: [
        challenge_output(challenge_datum(Open)),
        stake_output(decreased, 10_000_000),
        ap3x_to(key_address(test_owner_key_hash_2), 10_000_000),
      ],
      extra_signatories: [test_controller_hash(2), ..agent_signers()],
    }
  !spend_stake(stake_datum([]), DecreaseStake { amount: 10_000_000 }, tx)
}

// ──────────────────────────────────────────────────────────────────────────────
// Closing a challenge releases the lock
// ──────────────────────────────────────────────────────────────────────────────

/// A transaction at [lo, lo + 600_000] that spends `challenge` and the
/// target's stake with `stake_in`, burns the challenge token, and outputs
/// `outputs`.
fn close_tx(
  challenge: ReputationChallengeDatum,
  stake_in: StakeDatum,
  lo: Int,
  outputs: List<Output>,
) -> Transaction {
  Transaction {
    ..base_tx(lo),
    validity_range: interval.between(lo, lo + 600_000),
    inputs: [challenge_input(challenge), stake_input(stake_in)],
    reference_inputs: [
      make_params_ref_input(),
      refs_ref_input(),
      make_identity_ref_input(test_agent_did, 1),
    ],
    outputs,
    mint: assets.from_asset(test_endorsement_policy_id, challenge_token(), -1),
    extra_signatories: [test_controller_hash(1)],
  }
}

fn locked() -> StakeDatum {
  stake_datum([challenge_token()])
}

fn released() -> Output {
  stake_output(stake_datum([]), 20_000_000)
}

fn burn_challenge(tx: Transaction) -> Bool {
  endorsement.endorsement.mint(
    test_config(),
    BurnChallengeToken,
    test_endorsement_policy_id,
    tx,
  )
}

test withdraw_challenge_releases_lock() {
  let challenge = challenge_datum(Open)
  let tx =
    close_tx(
      challenge,
      locked(),
      t0 + 1_000,
      [ap3x_to(key_address(test_owner_key_hash), challenge_stake), released()],
    )
  and {
    spend_challenge(challenge, WithdrawChallenge, tx),
    spend_stake(locked(), ReleaseChallenge { challenge_ref: challenge_ref() }, tx),
    burn_challenge(tx),
  }
}

/// Inconclusive: the challenger gets its stake less half the fee, the
/// treasury half the fee, and the target's stake keeps its AP3X.
test distribute_inconclusive_releases_lock() {
  let challenge = challenge_datum(Resolved { outcome: Inconclusive })
  let tx =
    close_tx(
      challenge,
      locked(),
      t0 + 100_000_000,
      [
        ap3x_to(key_address(test_owner_key_hash), 24_375_000),
        ap3x_to(script_address(test_treasury_script_hash), 625_000),
        released(),
      ],
    )
  and {
    spend_challenge(challenge, DistributeOutcome, tx),
    spend_stake(locked(), ReleaseChallenge { challenge_ref: challenge_ref() }, tx),
    burn_challenge(tx),
  }
}

/// CapabilityVerified: the target receives the challenge stake less the fee.
test distribute_verified_releases_lock() {
  let challenge = challenge_datum(Resolved { outcome: CapabilityVerified })
  let tx =
    close_tx(
      challenge,
      locked(),
      t0 + 100_000_000,
      [
        ap3x_to(key_address(test_owner_key_hash_2), 23_750_000),
        ap3x_to(script_address(test_treasury_script_hash), 1_250_000),
        released(),
      ],
    )
  and {
    spend_challenge(challenge, DistributeOutcome, tx),
    spend_stake(locked(), ReleaseChallenge { challenge_ref: challenge_ref() }, tx),
  }
}

/// CapabilityFalsified: SlashStake takes stake / 2 capabilities = 10 AP3X
/// and removes the lock; the challenger receives 25 + 10 - 0.5 AP3X.
fn falsified_tx(stake_out: Output) -> Transaction {
  close_tx(
    challenge_datum(Resolved { outcome: CapabilityFalsified }),
    locked(),
    t0 + 100_000_000,
    [
      ap3x_to(key_address(test_owner_key_hash), 34_500_000),
      ap3x_to(script_address(test_treasury_script_hash), 500_000),
      stake_out,
    ],
  )
}

fn slashed_stake() -> Output {
  stake_output(
    StakeDatum { ..stake_datum([]), stake_amount: 10_000_000 },
    10_000_000,
  )
}

test distribute_falsified_slashes_and_releases_lock() {
  let challenge = challenge_datum(Resolved { outcome: CapabilityFalsified })
  let tx = falsified_tx(slashed_stake())
  and {
    spend_challenge(challenge, DistributeOutcome, tx),
    spend_stake(locked(), SlashStake { challenge_ref: challenge_ref() }, tx),
  }
}

/// A falsified challenge closes only through SlashStake: ReleaseChallenge
/// (no slash) fails.
test distribute_falsified_with_release_fails() {
  !spend_stake(
    locked(),
    ReleaseChallenge { challenge_ref: challenge_ref() },
    falsified_tx(released()),
  )
}

/// SlashStake needs the outcome CapabilityFalsified.
test slash_stake_with_verified_challenge_fails() fail {
  let challenge = challenge_datum(Resolved { outcome: CapabilityVerified })
  let tx =
    close_tx(
      challenge,
      locked(),
      t0 + 100_000_000,
      [slashed_stake()],
    )
  spend_stake(locked(), SlashStake { challenge_ref: challenge_ref() }, tx)
}

/// The released stake must have held the lock of the closing challenge.
test release_of_stake_without_lock_fails() fail {
  let challenge = challenge_datum(Open)
  let tx =
    close_tx(
      challenge,
      stake_datum([]),
      t0 + 1_000,
      [ap3x_to(key_address(test_owner_key_hash), challenge_stake), released()],
    )
  spend_stake(
    stake_datum([]),
    ReleaseChallenge { challenge_ref: challenge_ref() },
    tx,
  )
}

/// ReleaseChallenge needs the burn of the challenge token.
test release_without_challenge_burn_fails() fail {
  let challenge = challenge_datum(Open)
  let tx =
    Transaction {
      ..close_tx(
        challenge,
        locked(),
        t0 + 1_000,
        [ap3x_to(key_address(test_owner_key_hash), challenge_stake), released()],
      ),
      mint: assets.zero,
    }
  spend_stake(locked(), ReleaseChallenge { challenge_ref: challenge_ref() }, tx)
}

/// A release keeps all AP3X in the stake.
test release_taking_ap3x_fails() {
  let challenge = challenge_datum(Open)
  let tx =
    close_tx(
      challenge,
      locked(),
      t0 + 1_000,
      [
        ap3x_to(key_address(test_owner_key_hash), challenge_stake),
        stake_output(stake_datum([]), 19_000_000),
      ],
    )
  !spend_stake(locked(), ReleaseChallenge { challenge_ref: challenge_ref() }, tx)
}

/// W4 item 1: the stake cannot exit while a challenge locks it, and the
/// transaction does not need to name the challenge.
test decrease_locked_stake_fails() {
  let lo = 500 + 86_400_000
  let decreased =
    StakeDatum { ..locked(), stake_amount: 15_000_000, last_updated: lo + 600_000 }
  let tx =
    Transaction {
      ..base_tx(lo),
      validity_range: interval.between(lo, lo + 600_000),
      inputs: [stake_input(locked())],
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        make_identity_ref_input(test_agent_did_2, 2),
      ],
      outputs: [
        stake_output(decreased, 15_000_000),
        ap3x_to(key_address(test_owner_key_hash_2), 5_000_000),
      ],
      extra_signatories: [test_controller_hash(2)],
    }
  !spend_stake(locked(), DecreaseStake { amount: 5_000_000 }, tx)
}

/// Positive control: without the lock the same exit passes.
test decrease_unlocked_stake() {
  let lo = 500 + 86_400_000
  let decreased =
    StakeDatum {
      ..stake_datum([]),
      stake_amount: 15_000_000,
      last_updated: lo + 600_000,
    }
  let tx =
    Transaction {
      ..base_tx(lo),
      validity_range: interval.between(lo, lo + 600_000),
      inputs: [stake_input(stake_datum([]))],
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        make_identity_ref_input(test_agent_did_2, 2),
      ],
      outputs: [
        stake_output(decreased, 15_000_000),
        ap3x_to(key_address(test_owner_key_hash_2), 5_000_000),
      ],
      extra_signatories: [test_controller_hash(2)],
    }
  spend_stake(stake_datum([]), DecreaseStake { amount: 5_000_000 }, tx)
}
