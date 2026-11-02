/// Tests for stake validation logic.
///
/// Time rule (W4 item 2): an owner action records the upper bound of its
/// validity range in last_updated, and the cooldown compares last_updated
/// with the lower bound. The transactions here have the validity range
/// [lo, lo + 100].
///
/// CreateStake (R4 P-1): the spent input holds no reputation token, the
/// transaction mints exactly the stake token of the new datum, and the
/// output holds exactly that reputation token.

use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, Transaction}
use reputation_staking/stake_validation.{
  validate_create_stake, validate_decrease_stake, validate_increase_stake,
  validate_update_capabilities,
}
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_identity_ref_input, make_input,
  make_output, make_output_no_datum, make_params_ref_input, script_address,
  test_agent_did, test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id,
  test_config, test_owner_key_hash, test_params, test_refs,
  test_reputation_policy_id, test_tx_hash,
}
use reputation_staking/types.{StakeDatum}
use reputation_staking/utils.{derive_stake_token_name}

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

/// The stake cooldown of the test parameters (24 h).
const cooldown: Int = 86_400_000

/// A stake of test_agent_did (owner key 1) without challenge locks.
fn stake(
  amount: Int,
  capabilities: List<ByteArray>,
  last_updated: Int,
) -> StakeDatum {
  StakeDatum {
    agent_did: test_agent_did,
    owner_credential: VerificationKey(test_owner_key_hash),
    stake_amount: amount,
    staked_capabilities: capabilities,
    last_updated,
    history_points: 0,
    challenge_locks: [],
  }
}

/// 2 ADA, the AP3X of the datum and the agent's stake token.
fn stake_value(datum: StakeDatum) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, datum.stake_amount)
    |> assets.add(
        test_reputation_policy_id,
        derive_stake_token_name(datum.agent_did),
        1,
      )
}

fn stake_output(datum: StakeDatum) -> Output {
  make_output(
    script_address(test_reputation_policy_id),
    stake_value(datum),
    datum,
  )
}

fn ap3x_to_owner(amount: Int) -> Output {
  make_output_no_datum(
    key_address(test_owner_key_hash),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

/// A transaction with the validity range [lo, lo + 100], the agent's record
/// and the params record, signed by the owner key and the controller.
fn stake_tx(lo: Int, outputs: List<Output>) -> Transaction {
  Transaction {
    ..base_tx(lo),
    reference_inputs: [
      make_identity_ref_input(test_agent_did, 1),
      make_params_ref_input(),
    ],
    outputs,
    extra_signatories: agent_signers(),
  }
}

/// A seed input at the reputation address (no token).
fn seed_input() -> Input {
  make_input(
    test_tx_hash,
    7,
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000),
      stake(10_000_000, ["code_review"], 0),
    ),
  )
}

/// A CreateStake transaction that spends `own_input` and mints `mint`.
fn create_with(
  own_input: Input,
  datum: StakeDatum,
  output: Output,
  mint: assets.Value,
) -> Bool {
  let tx =
    Transaction { ..stake_tx(1000, [output]), inputs: [own_input], mint }
  validate_create_stake(
    tx,
    test_config(),
    test_refs(),
    test_params(),
    own_input,
    datum,
    output,
  )
}

fn stake_mint(agent_did: ByteArray) -> assets.Value {
  assets.from_asset(test_reputation_policy_id, derive_stake_token_name(agent_did), 1)
}

fn create(datum: StakeDatum) -> Bool {
  create_with(seed_input(), datum, stake_output(datum), stake_mint(test_agent_did))
}

// ──────────────────────────────────────────────────────────────────────────────
// CreateStake tests
// ──────────────────────────────────────────────────────────────────────────────

test create_stake_happy_path() {
  // last_updated is the upper bound of [1000, 1100]
  create(stake(10_000_000, ["code_review", "testing"], 1100))
}

test create_stake_below_minimum_fails() fail {
  // Below minimum (10_000_000)
  create(stake(5_000_000, ["code_review"], 1100))
}

test create_stake_empty_capabilities_fails() fail {
  create(stake(10_000_000, [], 1100))
}

test create_stake_recording_lower_bound_fails() {
  !create(stake(10_000_000, ["code_review"], 1000))
}

test create_stake_with_challenge_lock_fails() {
  !create(
    StakeDatum {
      ..stake(10_000_000, ["code_review"], 1100),
      challenge_locks: ["rchl_x"],
    },
  )
}

/// R4 P-1: CreateStake on another agent's StakeUTXO keeps the minimum stake
/// in a new StakeUTXO for the spender's agent and takes the rest.
test create_stake_spending_a_stake_fails() {
  let victim =
    StakeDatum {
      ..stake(1_000_000_000, ["code_review"], 0),
      agent_did: test_agent_did_2,
    }
  let victim_input =
    make_input(
      #"9191919191919191919191919191919191919191919191919191919191919191",
      0,
      stake_output(victim),
    )
  let datum = stake(10_000_000, ["code_review"], 1100)
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      stake_value(datum)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did_2),
            1,
          ),
      datum,
    )
  !create_with(victim_input, datum, output, stake_mint(test_agent_did))
}

test create_stake_output_with_second_stake_token_fails() {
  let datum = stake(10_000_000, ["code_review"], 1100)
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      stake_value(datum)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did_2),
            1,
          ),
      datum,
    )
  !create_with(
    seed_input(),
    datum,
    output,
    assets.merge(stake_mint(test_agent_did), stake_mint(test_agent_did_2)),
  )
}

test create_stake_without_minting_the_stake_token_fails() {
  let datum = stake(10_000_000, ["code_review"], 1100)
  !create_with(seed_input(), datum, stake_output(datum), assets.zero)
}

test create_stake_minting_the_token_of_another_agent_fails() {
  let datum = stake(10_000_000, ["code_review"], 1100)
  !create_with(
    seed_input(),
    datum,
    stake_output(datum),
    stake_mint(test_agent_did_2),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseStake tests
// ──────────────────────────────────────────────────────────────────────────────

fn increase(input_datum: StakeDatum, output_datum: StakeDatum) -> Bool {
  let output = stake_output(output_datum)
  validate_increase_stake(
    stake_tx(5000, [output]),
    test_config(),
    test_refs(),
    input_datum,
    output_datum,
    output,
  )
}

test increase_stake_happy_path() {
  let input_datum =
    StakeDatum { ..stake(10_000_000, ["code_review", "testing"], 1000), history_points: 50 }
  increase(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 20_000_000, last_updated: 5100 },
  )
}

test increase_stake_decrease_fails() fail {
  // Trying to decrease via IncreaseStake — should fail
  let input_datum = stake(20_000_000, ["code_review"], 1000)
  increase(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 10_000_000, last_updated: 5100 },
  )
}

test increase_stake_recording_lower_bound_fails() {
  let input_datum = stake(10_000_000, ["code_review"], 1000)
  !increase(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 20_000_000, last_updated: 5000 },
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// DecreaseStake tests
// ──────────────────────────────────────────────────────────────────────────────

/// DecreaseStake of `decrease_amount` at lower bound `lo`; the owner key
/// receives the decrease.
fn decrease(
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  decrease_amount: Int,
  lo: Int,
) -> Bool {
  let output = stake_output(output_datum)
  validate_decrease_stake(
    stake_tx(lo, [output, ap3x_to_owner(decrease_amount)]),
    test_config(),
    test_refs(),
    test_params(),
    input_datum,
    output_datum,
    output,
    decrease_amount,
  )
}

test decrease_stake_happy_path() {
  // Cooldown is 86_400_000 ms (24 h) — last_updated old enough
  let lo = 1000 + cooldown
  let input_datum = stake(20_000_000, ["code_review"], 1000)
  decrease(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 15_000_000, last_updated: lo + 100 },
    5_000_000,
    lo,
  )
}

test decrease_stake_cooldown_not_met_fails() fail {
  // Cooldown NOT met
  let lo = 1000 + 100
  let input_datum = stake(20_000_000, ["code_review"], 1000)
  decrease(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 15_000_000, last_updated: lo + 100 },
    5_000_000,
    lo,
  )
}

test decrease_stake_cooldown_one_millisecond_short_fails() {
  // The cooldown compares last_updated with the lower bound; the upper bound
  // is already after the cooldown
  let lo = 1000 + cooldown - 1
  let input_datum = stake(20_000_000, ["code_review"], 1000)
  !decrease(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 15_000_000, last_updated: lo + 100 },
    5_000_000,
    lo,
  )
}

test decrease_stake_below_minimum_fails() fail {
  // Decrease so much that remaining < min_self_stake (10_000_000)
  let lo = 1000 + cooldown
  let input_datum = stake(15_000_000, ["code_review"], 1000)
  decrease(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 9_000_000, last_updated: lo + 100 },
    6_000_000,
    lo,
  )
}

test decrease_stake_recording_lower_bound_fails() {
  let lo = 1000 + cooldown
  let input_datum = stake(20_000_000, ["code_review"], 1000)
  !decrease(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 15_000_000, last_updated: lo },
    5_000_000,
    lo,
  )
}

/// W4 item 2: the earlier action recorded the upper bound `hi` of its range
/// (one hour after its lower bound). The cooldown runs from `hi`, so a
/// decrease one cooldown after the earlier lower bound is too early.
test decrease_stake_one_cooldown_after_earlier_lower_bound_fails() {
  let earlier_lo = 1000
  let earlier_hi = earlier_lo + 3_600_000
  let lo = earlier_lo + cooldown
  let input_datum = stake(20_000_000, ["code_review"], earlier_hi)
  !decrease(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 15_000_000, last_updated: lo + 100 },
    5_000_000,
    lo,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// UpdateCapabilities tests
// ──────────────────────────────────────────────────────────────────────────────

fn update(
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  new_caps: List<ByteArray>,
  lo: Int,
) -> Bool {
  let output = stake_output(output_datum)
  validate_update_capabilities(
    stake_tx(lo, [output]),
    test_config(),
    test_refs(),
    test_params(),
    input_datum,
    output_datum,
    output,
    new_caps,
  )
}

test update_capabilities_happy_path() {
  let lo = 1000 + cooldown
  let new_caps = ["testing", "deployment"]
  let input_datum = stake(10_000_000, ["code_review", "testing"], 1000)
  update(
    input_datum,
    StakeDatum { ..input_datum, staked_capabilities: new_caps, last_updated: lo + 100 },
    new_caps,
    lo,
  )
}

test update_capabilities_recording_lower_bound_fails() {
  let lo = 1000 + cooldown
  let new_caps = ["testing", "deployment"]
  let input_datum = stake(10_000_000, ["code_review", "testing"], 1000)
  !update(
    input_datum,
    StakeDatum { ..input_datum, staked_capabilities: new_caps, last_updated: lo },
    new_caps,
    lo,
  )
}

/// The output must keep the AP3X of the stake (W4 item 4: the target cannot
/// empty its StakeUTXO while the datum keeps the stake amount).
test update_capabilities_taking_ap3x_fails() {
  let lo = 1000 + cooldown
  let new_caps = ["testing"]
  let input_datum = stake(10_000_000, ["code_review", "testing"], 1000)
  let output_datum =
    StakeDatum { ..input_datum, staked_capabilities: new_caps, last_updated: lo + 100 }
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      stake_value(StakeDatum { ..output_datum, stake_amount: 1 }),
      output_datum,
    )
  !validate_update_capabilities(
    stake_tx(lo, [output, ap3x_to_owner(9_999_999)]),
    test_config(),
    test_refs(),
    test_params(),
    input_datum,
    output_datum,
    output,
    new_caps,
  )
}
