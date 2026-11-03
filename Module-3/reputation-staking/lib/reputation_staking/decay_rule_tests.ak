//// ClaimDecayRefund (W4-MODFIX2 items 2 and 5), library level.
////
//// The stake always continues: with stake_amount = remaining when remaining
//// is at least MIN_SELF_STAKE, else with stake_amount = 0. All other fields
//// stay (last_updated can change), so the stake keeps its stake_id and its
//// endorsements can read it. When the decay consumes the whole stake, the
//// owner's payee receives remaining: owner_credential while the agent's
//// record declares it, else the record's controller key. Before, a decay
//// that consumed the whole stake burned the stake token and sent remaining
//// nowhere.

use cardano/address.{Credential, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, Transaction}
use reputation_staking/decay
use reputation_staking/test_helpers.{
  base_tx, make_output, make_output_no_datum, script_address, test_agent_did,
  test_ap3x_asset_name, test_ap3x_policy_id, test_capabilities, test_config,
  test_oracle_credential, test_other_stake_id, test_params, test_refs,
  test_reputation_policy_id, test_stake_id, test_treasury_script_hash,
}
use reputation_staking/types.{StakeDatum}
use reputation_staking/utils.{derive_stake_token_name}
use shared/identity_fixtures.{
  controller_hash, ed_key_hash, record, record_input, with_transaction_key,
}

const t0: Int = 1_700_000_000_000

/// The owner key of the stake: the listed transaction key 1 of the record
/// of test_agent_did.
fn owner_key() -> ByteArray {
  ed_key_hash(1)
}

/// The record of test_agent_did that lists the owner key.
fn owner_record() -> Input {
  record_input(test_agent_did, with_transaction_key(record(1, test_capabilities), 1))
}

/// The record of test_agent_did after the removal of the owner key.
fn record_without_owner_key() -> Input {
  record_input(test_agent_did, record(1, test_capabilities))
}

fn stake(amount: Int) -> StakeDatum {
  StakeDatum {
    agent_did: test_agent_did,
    owner_credential: VerificationKey(owner_key()),
    stake_amount: amount,
    staked_capabilities: ["code_review"],
    last_updated: t0 - 2_000_000_000,
    history_points: 3,
    challenge_locks: [],
    stake_id: test_stake_id,
  }
}

fn stake_output(datum: StakeDatum, ap3x: Int) -> Output {
  make_output(
    script_address(test_reputation_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, ap3x)
      |> assets.add(
          test_reputation_policy_id,
          derive_stake_token_name(test_agent_did),
          1,
        ),
    datum,
  )
}

fn ap3x_to(credential: Credential, amount: Int) -> Output {
  make_output_no_datum(
    address_of(credential),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

fn address_of(credential: Credential) -> address.Address {
  address.Address { payment_credential: credential, stake_credential: None }
}

fn treasury(amount: Int) -> Output {
  ap3x_to(address.Script(test_treasury_script_hash), amount)
}

/// The collector: the oracle key in these tests.
fn collector(amount: Int) -> Output {
  ap3x_to(VerificationKey(test_oracle_credential), amount)
}

/// ClaimDecayRefund of `input` by 10 inactive epochs with `outputs`, `mint`
/// and the reference inputs `refs`.
fn decay_with(
  input: StakeDatum,
  refs: List<Input>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  decay.validate_claim_decay_refund(
    Transaction {
      ..base_tx(t0),
      reference_inputs: refs,
      outputs,
      mint,
      extra_signatories: [test_oracle_credential],
    },
    test_config(),
    test_refs(),
    test_params(),
    input,
    10,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// The stake continues (remaining >= MIN_SELF_STAKE)
// ──────────────────────────────────────────────────────────────────────────────

// 20 AP3X, 10 epochs at 1%: decay 2 AP3X, collector 0.1, treasury 1.9,
// remaining 18 AP3X.

fn partial(continuing: StakeDatum) -> Bool {
  decay_with(
    stake(20_000_000),
    [],
    [stake_output(continuing, 18_000_000), collector(100_000), treasury(1_900_000)],
    assets.zero,
  )
}

test decay_keeps_stake_with_remaining_amount() {
  partial(StakeDatum { ..stake(20_000_000), stake_amount: 18_000_000 })
}

/// The oracle can set last_updated (it marks the processed decay).
test decay_can_set_last_updated() {
  partial(
    StakeDatum {
      ..stake(20_000_000),
      stake_amount: 18_000_000,
      last_updated: t0,
    },
  )
}

/// The old rule compared only agent_did, owner_credential, stake_amount
/// and challenge_locks of the continuing datum.
test decay_with_changed_stake_id_fails() {
  !partial(
    StakeDatum {
      ..stake(20_000_000),
      stake_amount: 18_000_000,
      stake_id: test_other_stake_id,
    },
  )
}

test decay_with_changed_capabilities_fails() {
  !partial(
    StakeDatum {
      ..stake(20_000_000),
      stake_amount: 18_000_000,
      staked_capabilities: ["code_review", "testing"],
    },
  )
}

test decay_with_changed_history_points_fails() {
  !partial(
    StakeDatum { ..stake(20_000_000), stake_amount: 18_000_000, history_points: 0 },
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// The decay consumes the whole stake (remaining < MIN_SELF_STAKE)
// ──────────────────────────────────────────────────────────────────────────────

// 10 AP3X, 10 epochs at 1%: decay 1 AP3X, collector 0.05, treasury 0.95,
// remaining 9 AP3X < 10 AP3X: the stake continues with 0, the owner's payee
// receives 9 AP3X.

fn zero_stake() -> StakeDatum {
  StakeDatum { ..stake(10_000_000), stake_amount: 0 }
}

fn full(refs: List<Input>, owner_payout: List<Output>) -> Bool {
  decay_with(
    stake(10_000_000),
    refs,
    [
      stake_output(zero_stake(), 0),
      collector(50_000),
      treasury(950_000),
      ..owner_payout
    ],
    assets.zero,
  )
}

test decay_of_whole_stake_pays_owner() {
  full([owner_record()], [ap3x_to(VerificationKey(owner_key()), 9_000_000)])
}

test decay_of_whole_stake_without_owner_payout_fails() {
  !full([owner_record()], [])
}

test decay_of_whole_stake_underpaying_owner_fails() {
  !full([owner_record()], [ap3x_to(VerificationKey(owner_key()), 8_999_999)])
}

/// The owner's record no longer lists the owner key: the payout goes to the
/// record's controller key.
test decay_of_whole_stake_after_key_removal_pays_controller() {
  full(
    [record_without_owner_key()],
    [ap3x_to(VerificationKey(controller_hash(1)), 9_000_000)],
  )
}

test decay_of_whole_stake_after_key_removal_paying_removed_key_fails() {
  !full(
    [record_without_owner_key()],
    [ap3x_to(VerificationKey(owner_key()), 9_000_000)],
  )
}

/// The payee needs the owner's record.
test decay_of_whole_stake_without_owner_record_fails() {
  !full([], [ap3x_to(VerificationKey(owner_key()), 9_000_000)])
}

/// The old rule burned the stake token and checked no owner payout. The
/// stake must continue (with stake_amount 0).
test decay_of_whole_stake_with_token_burn_fails() {
  !decay_with(
    stake(10_000_000),
    [owner_record()],
    [collector(50_000), treasury(950_000)],
    assets.from_asset(
      test_reputation_policy_id,
      derive_stake_token_name(test_agent_did),
      -1,
    ),
  )
}

/// The zero stake keeps all other fields.
test decay_of_whole_stake_with_changed_stake_id_fails() {
  !decay_with(
    stake(10_000_000),
    [owner_record()],
    [
      stake_output(StakeDatum { ..zero_stake(), stake_id: test_other_stake_id }, 0),
      collector(50_000),
      treasury(950_000),
      ap3x_to(VerificationKey(owner_key()), 9_000_000),
    ],
    assets.zero,
  )
}

/// A stake of 0 cannot decay again.
test decay_of_zero_stake_fails() {
  !decay_with(
    zero_stake(),
    [owner_record()],
    [stake_output(zero_stake(), 0)],
    assets.zero,
  )
}
