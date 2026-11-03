//// Rule D-exit for WithdrawChallenge (W4 item 5), at validator level.
////
//// The controller of the challenger's record signs (any status). The
//// challenge stake goes to challenger_credential while the record trusts it,
//// else to the controller key of the record. The library cases
//// (DecreaseStake, WithdrawEndorsement and the openings) are in
//// lib/reputation_staking/exit_rule_tests.ak.

use aiken/interval
use cardano/address.{Credential, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use endorsement
use reputation_staking/test_helpers.{
  base_tx, key_address, make_input, make_output, make_output_no_datum,
  make_params_ref_input, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_capabilities, test_config,
  test_endorsement_policy_id, test_owner_key_hash, test_owner_key_hash_2,
  test_refs, test_refs_token_policy, test_reputation_policy_id, test_stake_id,
  test_tx_hash,
}
use reputation_staking/types.{
  Challenge, ChallengeSpend, Open, ReputationChallengeDatum, StakeDatum,
  WithdrawChallenge,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name,
}
use shared/identity_fixtures.{
  controller_hash, controller_key, ed_key_hash, record, record_input,
  with_transaction_key,
}
use vector_identity/types.{IdentityDatum} as identity_types

const t0: Int = 1_700_000_000_000

const challenge_stake: Int = 25_000_000

fn challenge_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"7777777777777777777777777777777777777777777777777777777777777777",
    output_index: 0,
  }
}

fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

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

fn ap3x_to(key: ByteArray, amount: Int) -> Output {
  make_output_no_datum(
    key_address(key),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

/// The target's StakeUTXO with the lock of the challenge.
fn locked_target_stake() -> Input {
  make_input(
    test_tx_hash,
    50,
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did_2),
            1,
          ),
      StakeDatum {
        agent_did: test_agent_did_2,
        owner_credential: VerificationKey(test_owner_key_hash_2),
        stake_amount: 20_000_000,
        staked_capabilities: test_capabilities,
        last_updated: 500,
        history_points: 0,
        challenge_locks: [challenge_token()],
        stake_id: test_stake_id,
      },
    ),
  )
}

/// WithdrawChallenge of a challenge with challenger_credential `stored`. The
/// challenger's record is `r`, the key `signer` signs, and the challenge
/// stake goes to the key `payee`.
fn withdraw(
  r: IdentityDatum,
  stored: Credential,
  signer: ByteArray,
  payee: ByteArray,
) -> Bool {
  let datum =
    ReputationChallengeDatum {
      challenger_did: test_agent_did,
      challenger_credential: stored,
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
      state: Open,
    }
  let challenge_input =
    Input {
      output_reference: challenge_ref(),
      output: make_output(
        script_address(test_endorsement_policy_id),
        assets.from_lovelace(2_000_000)
          |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, challenge_stake)
          |> assets.add(test_endorsement_policy_id, challenge_token(), 1),
        Challenge(datum),
      ),
    }
  let tx =
    Transaction {
      ..base_tx(t0),
      validity_range: interval.between(t0 + 1_000, t0 + 2_000),
      inputs: [challenge_input, locked_target_stake()],
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        record_input(test_agent_did, r),
      ],
      outputs: [ap3x_to(payee, challenge_stake)],
      mint: assets.from_asset(test_endorsement_policy_id, challenge_token(), -1),
      extra_signatories: [signer],
    }
  endorsement.endorsement.spend(
    test_config(),
    Some(Challenge(datum)),
    ChallengeSpend(WithdrawChallenge),
    challenge_ref(),
    tx,
  )
}

fn listed_key() -> ByteArray {
  ed_key_hash(1)
}

fn agent_record() -> IdentityDatum {
  with_transaction_key(record(1, test_capabilities), 1)
}

fn changed_controller() -> IdentityDatum {
  IdentityDatum { ..record(1, test_capabilities), controller: controller_key(7) }
}

test withdraw_challenge_pays_trusted_stored_credential() {
  withdraw(
    agent_record(),
    VerificationKey(listed_key()),
    controller_hash(1),
    listed_key(),
  )
}

test withdraw_challenge_after_controller_change_pays_new_controller() {
  withdraw(
    changed_controller(),
    VerificationKey(controller_hash(1)),
    controller_hash(7),
    controller_hash(7),
  )
}

test withdraw_challenge_after_controller_change_paying_old_key_fails() {
  !withdraw(
    changed_controller(),
    VerificationKey(controller_hash(1)),
    controller_hash(7),
    controller_hash(1),
  )
}

test withdraw_challenge_after_key_removal_pays_controller() {
  withdraw(
    record(1, test_capabilities),
    VerificationKey(listed_key()),
    controller_hash(1),
    controller_hash(1),
  )
}

test withdraw_challenge_after_key_removal_paying_removed_key_fails() {
  !withdraw(
    record(1, test_capabilities),
    VerificationKey(listed_key()),
    controller_hash(1),
    listed_key(),
  )
}
