//// Challenge locks in the stake datum (W4 items 1 and 4), library level.
////
//// While challenge_locks is not empty, the owner cannot decrease the stake
//// or change its capabilities, and decay cannot lower it. The owner actions
//// keep the locks and cannot open or close a challenge in the same
//// transaction. The validator-level flows (open, release, slash) are in
//// validators/lock_tests.ak.

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Output, Transaction}
use reputation_staking/decay
use reputation_staking/stake_validation.{
  validate_increase_stake, validate_update_capabilities,
}
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_identity_ref_input, make_output,
  make_output_no_datum, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_config,
  test_endorsement_policy_id, test_oracle_credential, test_owner_key_hash,
  test_params, test_refs, test_reputation_policy_id, test_stake_id,
  test_treasury_script_hash,
}
use reputation_staking/types.{StakeDatum}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name,
}

const t0: Int = 1_700_000_000_000

fn lock() -> ByteArray {
  derive_challenge_token_name(test_agent_did_2, test_agent_did, "code_review")
}

fn stake(capabilities: List<ByteArray>, locks: List<ByteArray>) -> StakeDatum {
  StakeDatum {
    agent_did: test_agent_did,
    owner_credential: VerificationKey(test_owner_key_hash),
    stake_amount: 20_000_000,
    staked_capabilities: capabilities,
    last_updated: t0 - 86_400_000,
    history_points: 0,
    challenge_locks: locks,
    stake_id: test_stake_id,
  }
}

fn stake_output(datum: StakeDatum) -> Output {
  make_output(
    script_address(test_reputation_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, datum.stake_amount)
      |> assets.add(
          test_reputation_policy_id,
          derive_stake_token_name(test_agent_did),
          1,
        ),
    datum,
  )
}

fn owner_tx(outputs: List<Output>, mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    validity_range: interval.between(t0, t0 + 600_000),
    reference_inputs: [make_identity_ref_input(test_agent_did, 1)],
    outputs,
    mint,
    extra_signatories: agent_signers(),
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// UpdateCapabilities
// ──────────────────────────────────────────────────────────────────────────────

fn update(input_datum: StakeDatum, new_caps: List<ByteArray>) -> Bool {
  let output_datum =
    StakeDatum {
      ..input_datum,
      staked_capabilities: new_caps,
      last_updated: t0 + 600_000,
    }
  let output = stake_output(output_datum)
  validate_update_capabilities(
    owner_tx([output], assets.zero),
    test_config(),
    test_refs(),
    test_params(),
    input_datum,
    output_datum,
    output,
    new_caps,
  )
}

test update_capabilities_without_lock() {
  update(stake(["code_review"], []), ["testing"])
}

/// W4 item 1: removing a capability while a challenge locks the stake fails
/// (the transaction names no challenge).
test update_capabilities_removing_capability_with_lock_fails() {
  !update(stake(["code_review", "testing"], [lock()]), ["testing"])
}

/// W4 item 4: adding a capability while a challenge locks the stake fails:
/// it would lower the slash (stake / number of capabilities).
test update_capabilities_adding_capability_with_lock_fails() {
  !update(
    stake(["code_review"], [lock()]),
    ["code_review", "testing", "deployment"],
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseStake keeps the locks
// ──────────────────────────────────────────────────────────────────────────────

fn increase(
  input_datum: StakeDatum,
  output_datum: StakeDatum,
  mint: assets.Value,
) -> Bool {
  let output = stake_output(output_datum)
  validate_increase_stake(
    owner_tx([output], mint),
    test_config(),
    test_refs(),
    input_datum,
    output_datum,
    output,
  )
}

test increase_stake_keeps_challenge_lock() {
  let input_datum = stake(["code_review"], [lock()])
  increase(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 30_000_000, last_updated: t0 + 600_000 },
    assets.zero,
  )
}

test increase_stake_dropping_challenge_lock_fails() {
  let input_datum = stake(["code_review"], [lock()])
  !increase(
    input_datum,
    StakeDatum {
      ..input_datum,
      stake_amount: 30_000_000,
      last_updated: t0 + 600_000,
      challenge_locks: [],
    },
    assets.zero,
  )
}

/// An owner action cannot close a challenge: the lock would stay in the
/// stake without a challenge.
test increase_stake_with_challenge_burn_fails() {
  let input_datum = stake(["code_review"], [lock()])
  !increase(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 30_000_000, last_updated: t0 + 600_000 },
    assets.from_asset(test_endorsement_policy_id, lock(), -1),
  )
}

/// An owner action cannot open a challenge against its own stake without
/// the lock.
test increase_stake_with_challenge_mint_fails() {
  let input_datum = stake(["code_review"], [])
  !increase(
    input_datum,
    StakeDatum { ..input_datum, stake_amount: 30_000_000, last_updated: t0 + 600_000 },
    assets.from_asset(test_endorsement_policy_id, lock(), 1),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// ClaimDecayRefund
// ──────────────────────────────────────────────────────────────────────────────

/// Decay of a 20 AP3X stake by 10 epochs at 1% (2 AP3X): 0.1 AP3X to the
/// collector, 1.9 AP3X to the treasury, 18 AP3X stay.
fn decay_of(input_datum: StakeDatum) -> Bool {
  let continuing =
    stake_output(
      StakeDatum { ..input_datum, stake_amount: 18_000_000, challenge_locks: [] },
    )
  let tx =
    Transaction {
      ..base_tx(t0),
      outputs: [
        continuing,
        make_output_no_datum(
          key_address(test_oracle_credential),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 100_000),
        ),
        make_output_no_datum(
          script_address(test_treasury_script_hash),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 1_900_000),
        ),
      ],
      extra_signatories: [test_oracle_credential],
    }
  decay.validate_claim_decay_refund(
    tx,
    test_config(),
    test_refs(),
    test_params(),
    input_datum,
    10,
  )
}

test claim_decay_refund_without_lock() {
  decay_of(stake(["code_review"], []))
}

/// W4 item 4: decay cannot lower a stake that an open challenge can slash.
test claim_decay_refund_with_challenge_lock_fails() {
  !decay_of(stake(["code_review"], [lock()]))
}
