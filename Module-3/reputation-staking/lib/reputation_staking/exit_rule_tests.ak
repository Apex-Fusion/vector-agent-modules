//// Rule D-exit (W4 item 5): exits pay a key that the record trusts now, and
//// openings store only a declared credential.
////
//// - An exit (DecreaseStake, WithdrawEndorsement, WithdrawChallenge) needs
////   the controller's signature of the agent's record (any status). The
////   payout goes to the stored credential while the record trusts it
////   (`identity.declared_credential`: the controller key or a listed key
////   with the transaction purpose). Else it goes to the controller key of
////   the record. The payout check adds up all outputs to the payee.
//// - An opening (CreateStake, MintEndorsementToken, MintChallengeToken)
////   stores a payout credential only when it is a declared credential of
////   the acting agent's record.
////
//// WithdrawChallenge cases are in validators/exit_rule_tests.ak.

use aiken/interval
use cardano/address.{Credential, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use reputation_staking/challenge_validation.{validate_mint_challenge}
use reputation_staking/endorsement_validation.{
  validate_mint_endorsement, validate_withdraw_endorsement,
}
use reputation_staking/stake_validation.{
  validate_create_stake, validate_decrease_stake,
}
use reputation_staking/test_helpers.{
  base_tx, key_address, make_input, make_output, make_output_no_datum,
  script_address, stake_ref_input, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_capabilities, test_config,
  test_endorsement_policy_id, test_owner_key_hash_2, test_params, test_refs,
  test_reputation_policy_id, test_stake_id, test_tx_hash,
}
use reputation_staking/types.{
  Challenge, EndorsementDatum, Open, ReputationChallengeDatum, StakeDatum,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
  derive_stake_token_name,
}
use shared/identity_fixtures.{
  controller_hash, controller_key, ed_key_hash, record, record_input,
  with_transaction_key,
}
use vector_identity/types.{IdentityDatum} as identity_types

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

const t0: Int = 1_700_000_000_000

fn range() -> transaction.ValidityRange {
  interval.between(t0, t0 + 600_000)
}

/// The record of test_agent_did: controller 1 and the listed transaction
/// key 1.
fn agent_record() -> IdentityDatum {
  with_transaction_key(record(1, test_capabilities), 1)
}

/// The record after a controller change: the new controller key 7, no
/// listed key.
fn changed_controller() -> IdentityDatum {
  IdentityDatum { ..record(1, test_capabilities), controller: controller_key(7) }
}

/// The record after the removal of the listed key 1: controller 1, no
/// listed key.
fn removed_key() -> IdentityDatum {
  record(1, test_capabilities)
}

fn agent_ref(r: IdentityDatum) -> Input {
  record_input(test_agent_did, r)
}

fn ap3x_to(key: ByteArray, amount: Int) -> Output {
  make_output_no_datum(
    key_address(key),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

/// The stored key of the position: the listed transaction key 1.
fn listed_key() -> ByteArray {
  ed_key_hash(1)
}

// ──────────────────────────────────────────────────────────────────────────────
// DecreaseStake
// ──────────────────────────────────────────────────────────────────────────────

/// DecreaseStake of 5 AP3X from a stake with owner_credential `stored`. The
/// record is `r`, the controller key `signer` signs, and `outputs` pay out.
fn decrease(
  r: IdentityDatum,
  stored: Credential,
  signer: ByteArray,
  payouts: List<Output>,
) -> Bool {
  let input =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: stored,
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review"],
      last_updated: t0 - 86_400_000,
      history_points: 0,
      challenge_locks: [],
      stake_id: test_stake_id,
    }
  let datum =
    StakeDatum { ..input, stake_amount: 15_000_000, last_updated: t0 + 600_000 }
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 15_000_000)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did),
            1,
          ),
      datum,
    )
  validate_decrease_stake(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      reference_inputs: [agent_ref(r)],
      outputs: [output, ..payouts],
      extra_signatories: [signer],
    },
    test_config(),
    test_refs(),
    test_params(),
    input,
    datum,
    output,
    5_000_000,
  )
}

/// The record still lists the stored key: the exit pays it.
test decrease_stake_pays_trusted_stored_credential() {
  decrease(
    agent_record(),
    VerificationKey(listed_key()),
    controller_hash(1),
    [ap3x_to(listed_key(), 5_000_000)],
  )
}

/// The stored key is trusted, so the controller key is not the payee.
test decrease_stake_paying_controller_instead_of_trusted_key_fails() {
  !decrease(
    agent_record(),
    VerificationKey(listed_key()),
    controller_hash(1),
    [ap3x_to(controller_hash(1), 5_000_000)],
  )
}

/// After a controller change the stored credential (the old controller key)
/// is not trusted: the exit pays the new controller key.
test decrease_stake_after_controller_change_pays_new_controller() {
  decrease(
    changed_controller(),
    VerificationKey(controller_hash(1)),
    controller_hash(7),
    [ap3x_to(controller_hash(7), 5_000_000)],
  )
}

test decrease_stake_after_controller_change_paying_old_key_fails() {
  !decrease(
    changed_controller(),
    VerificationKey(controller_hash(1)),
    controller_hash(7),
    [ap3x_to(controller_hash(1), 5_000_000)],
  )
}

/// After the removal of the listed key that is the stored credential, the
/// exit pays the controller key.
test decrease_stake_after_key_removal_pays_controller() {
  decrease(
    removed_key(),
    VerificationKey(listed_key()),
    controller_hash(1),
    [ap3x_to(controller_hash(1), 5_000_000)],
  )
}

test decrease_stake_after_key_removal_paying_removed_key_fails() {
  !decrease(
    removed_key(),
    VerificationKey(listed_key()),
    controller_hash(1),
    [ap3x_to(listed_key(), 5_000_000)],
  )
}

/// The payout check adds up all outputs to the payee.
test decrease_stake_payout_in_two_outputs() {
  decrease(
    agent_record(),
    VerificationKey(listed_key()),
    controller_hash(1),
    [ap3x_to(listed_key(), 2_000_000), ap3x_to(listed_key(), 3_000_000)],
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawEndorsement
// ──────────────────────────────────────────────────────────────────────────────

/// WithdrawEndorsement of 5 AP3X with endorser_credential `stored`.
fn withdraw(
  r: IdentityDatum,
  stored: Credential,
  signer: ByteArray,
  payee: ByteArray,
) -> Bool {
  let input =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: stored,
      target_did: test_agent_did_2,
      stake_amount: 5_000_000,
      endorsed_capabilities: ["code_review"],
      created_at: t0 - 172_800_000,
      slashed_by: [],
      target_stake: test_stake_id,
    }
  validate_withdraw_endorsement(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      reference_inputs: [
        agent_ref(r),
        stake_ref_input(test_agent_did_2, test_stake_id, []),
      ],
      outputs: [ap3x_to(payee, 5_000_000)],
      mint: assets.from_asset(
        test_endorsement_policy_id,
        derive_endorsement_token_name(test_agent_did, test_agent_did_2),
        -1,
      ),
      extra_signatories: [signer],
    },
    test_config(),
    test_refs(),
    test_params(),
    input,
    t0,
  )
}

test withdraw_endorsement_pays_trusted_stored_credential() {
  withdraw(
    agent_record(),
    VerificationKey(listed_key()),
    controller_hash(1),
    listed_key(),
  )
}

test withdraw_endorsement_after_controller_change_pays_new_controller() {
  withdraw(
    changed_controller(),
    VerificationKey(controller_hash(1)),
    controller_hash(7),
    controller_hash(7),
  )
}

test withdraw_endorsement_after_controller_change_paying_old_key_fails() {
  !withdraw(
    changed_controller(),
    VerificationKey(controller_hash(1)),
    controller_hash(7),
    controller_hash(1),
  )
}

test withdraw_endorsement_after_key_removal_pays_controller() {
  withdraw(
    removed_key(),
    VerificationKey(listed_key()),
    controller_hash(1),
    controller_hash(1),
  )
}

test withdraw_endorsement_after_key_removal_paying_removed_key_fails() {
  !withdraw(
    removed_key(),
    VerificationKey(listed_key()),
    controller_hash(1),
    listed_key(),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Openings store only a declared credential
// ──────────────────────────────────────────────────────────────────────────────

/// A key hash that the record does not list.
const undeclared_key: ByteArray =
  #"0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"

/// CreateStake with owner_credential `credential`; the controller and the
/// credential's key sign.
fn create_with_owner(credential: Credential, key: ByteArray) -> Bool {
  let datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: credential,
      stake_amount: 10_000_000,
      staked_capabilities: ["code_review"],
      last_updated: t0 + 600_000,
      history_points: 0,
      challenge_locks: [],
      // The seed below
      stake_id: OutputReference { transaction_id: test_tx_hash, output_index: 9 },
    }
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did),
            1,
          ),
      datum,
    )
  let seed =
    make_input(
      test_tx_hash,
      9,
      make_output(
        script_address(test_reputation_policy_id),
        assets.from_lovelace(2_000_000),
        datum,
      ),
    )
  validate_create_stake(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      inputs: [seed],
      reference_inputs: [agent_ref(agent_record())],
      outputs: [output],
      mint: assets.from_asset(
        test_reputation_policy_id,
        derive_stake_token_name(test_agent_did),
        1,
      ),
      extra_signatories: [controller_hash(1), key],
    },
    test_config(),
    test_refs(),
    test_params(),
    seed,
    datum,
    output,
  )
}

test create_stake_with_listed_key_as_owner() {
  create_with_owner(VerificationKey(listed_key()), listed_key())
}

test create_stake_with_controller_key_as_owner() {
  create_with_owner(VerificationKey(controller_hash(1)), controller_hash(1))
}

/// The undeclared key signs, but the record does not trust it.
test create_stake_with_undeclared_owner_fails() {
  !create_with_owner(VerificationKey(undeclared_key), undeclared_key)
}

/// The StakeUTXO of the endorsement and challenge target (test_agent_did_2).
fn target_stake() -> Input {
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
        challenge_locks: [],
        stake_id: test_stake_id,
      },
    ),
  )
}

/// MintEndorsementToken with endorser_credential `credential`.
fn endorse_with(credential: Credential, key: ByteArray) -> Bool {
  let datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: credential,
      target_did: test_agent_did_2,
      stake_amount: 5_000_000,
      endorsed_capabilities: ["code_review"],
      created_at: t0,
      slashed_by: [],
      target_stake: test_stake_id,
    }
  let token = derive_endorsement_token_name(test_agent_did, test_agent_did_2)
  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 5_000_000)
        |> assets.add(test_endorsement_policy_id, token, 1),
      datum,
    )
  validate_mint_endorsement(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      reference_inputs: [
        agent_ref(agent_record()),
        record_input(
          test_agent_did_2,
          with_transaction_key(record(2, test_capabilities), 2),
        ),
        target_stake(),
      ],
      outputs: [output],
      mint: assets.from_asset(test_endorsement_policy_id, token, 1),
      extra_signatories: [controller_hash(1), key],
    },
    test_config(),
    test_refs(),
    test_params(),
    datum,
    output,
  )
}

test mint_endorsement_with_listed_key() {
  endorse_with(VerificationKey(listed_key()), listed_key())
}

test mint_endorsement_with_undeclared_credential_fails() {
  !endorse_with(VerificationKey(undeclared_key), undeclared_key)
}

/// MintChallengeToken with challenger_credential `credential`.
fn challenge_with(credential: Credential, key: ByteArray) -> Bool {
  let datum =
    ReputationChallengeDatum {
      challenger_did: test_agent_did,
      challenger_credential: credential,
      target_did: test_agent_did_2,
      target_credential: VerificationKey(test_owner_key_hash_2),
      challenged_capability: "code_review",
      stake_amount: 25_000_000,
      evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      evidence_uri: "ipfs://evidence",
      created_at: t0,
      counter_evidence_hash: "",
      counter_evidence_uri: "",
      response_submitted_at: 0,
      state: Open,
    }
  let token =
    derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
        |> assets.add(test_endorsement_policy_id, token, 1),
      Challenge(datum),
    )
  validate_mint_challenge(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      inputs: [target_stake()],
      reference_inputs: [agent_ref(agent_record())],
      outputs: [output],
      mint: assets.from_asset(test_endorsement_policy_id, token, 1),
      extra_signatories: [controller_hash(1), key],
    },
    test_config(),
    test_refs(),
    test_params(),
    datum,
    output,
  )
}

test mint_challenge_with_listed_key() {
  challenge_with(VerificationKey(listed_key()), listed_key())
}

test mint_challenge_with_undeclared_credential_fails() {
  !challenge_with(VerificationKey(undeclared_key), undeclared_key)
}
