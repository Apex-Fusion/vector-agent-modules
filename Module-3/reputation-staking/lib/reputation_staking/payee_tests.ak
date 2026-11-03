//// The payee rule for permissionless payouts (W4-MODFIX2 item 5), library
//// level.
////
//// ExpireChallenge and DistributeOutcome need no signature. Their payouts
//// go to the payee of the party: the stored credential (challenger_credential
//// or target_credential) while the party's identity record declares it, else
//// the record's controller key. The record is a reference input with any
//// status. Before, these payouts went to the stored credential, also after
//// the record removed that key. The exits (with the controller's signature)
//// use the same payee (exit_rule_tests.ak).
////
//// SlashEndorsement (validator level) is in
//// validators/endorsement_slash_tests.ak.

use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use reputation_staking/challenge_validation.{
  challenge_timeout, validate_distribute_outcome, validate_expire_challenge,
}
use reputation_staking/test_helpers.{
  base_tx, make_output, make_output_no_datum, script_address, stake_ref_input,
  test_agent_did, test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id,
  test_capabilities, test_config, test_endorsement_policy_id, test_params,
  test_refs, test_stake_id, test_treasury_script_hash, test_tx_hash,
}
use reputation_staking/types.{
  CapabilityFalsified, CapabilityVerified, Challenge, Inconclusive,
  RepChallengeOutcome, ReputationChallengeDatum, Resolved,
}
use reputation_staking/utils.{derive_challenge_token_name}
use shared/identity_fixtures.{
  controller_hash, ed_key_hash, record, record_input, revoked,
  with_transaction_key,
}

const created_at: Int = 1_700_000_000_000

/// The stored keys of the parties: the listed transaction keys of their
/// records (agent 1 is the challenger, agent 2 the target).
fn challenger_key() -> ByteArray {
  ed_key_hash(1)
}

fn target_key() -> ByteArray {
  ed_key_hash(2)
}

/// The records of the parties, which list the stored keys.
fn challenger_record() -> Input {
  record_input(test_agent_did, with_transaction_key(record(1, test_capabilities), 1))
}

fn target_record() -> Input {
  record_input(test_agent_did_2, with_transaction_key(record(2, test_capabilities), 2))
}

/// The records after the removal of the stored keys.
fn challenger_record_without_key() -> Input {
  record_input(test_agent_did, record(1, test_capabilities))
}

fn target_record_without_key() -> Input {
  record_input(test_agent_did_2, record(2, test_capabilities))
}

fn challenge(state: types.RepChallengeState) -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did,
    challenger_credential: VerificationKey(challenger_key()),
    target_did: test_agent_did_2,
    target_credential: VerificationKey(target_key()),
    challenged_capability: "code_review",
    stake_amount: 25_000_000,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at,
    counter_evidence_hash: #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    counter_evidence_uri: "ipfs://counter",
    response_submitted_at: created_at + 1_000,
    state,
  }
}

fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

fn burn() -> assets.Value {
  assets.from_asset(test_endorsement_policy_id, challenge_token(), -1)
}

fn ap3x_to(credential: Credential, amount: Int) -> Output {
  make_output_no_datum(
    Address { payment_credential: credential, stake_credential: None },
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

fn to_key(key: ByteArray, amount: Int) -> Output {
  ap3x_to(VerificationKey(key), amount)
}

fn to_treasury(amount: Int) -> Output {
  ap3x_to(Script(test_treasury_script_hash), amount)
}

/// The target's stake (agent 2) with the lock of the challenge, spent in
/// the transaction.
fn locked_stake() -> Input {
  stake_ref_input(test_agent_did_2, test_stake_id, [challenge_token()])
}

// ──────────────────────────────────────────────────────────────────────────────
// ExpireChallenge
// ──────────────────────────────────────────────────────────────────────────────

fn expire(refs: List<Input>, payout: Output) -> Bool {
  let datum = challenge(types.Open)
  validate_expire_challenge(
    Transaction {
      ..base_tx(challenge_timeout(test_params(), created_at) + 1),
      inputs: [locked_stake()],
      reference_inputs: refs,
      outputs: [payout],
      mint: burn(),
    },
    test_config(),
    test_refs(),
    test_params(),
    datum,
  )
}

test expire_challenge_pays_declared_stored_key() {
  expire([challenger_record()], to_key(challenger_key(), 25_000_000))
}

test expire_challenge_after_key_removal_pays_controller() {
  expire(
    [challenger_record_without_key()],
    to_key(controller_hash(1), 25_000_000),
  )
}

/// The old rule paid the stored key, also after the record removed it.
test expire_challenge_after_key_removal_paying_removed_key_fails() {
  !expire(
    [challenger_record_without_key()],
    to_key(challenger_key(), 25_000_000),
  )
}

test expire_challenge_without_challenger_record_fails() {
  !expire([], to_key(challenger_key(), 25_000_000))
}

/// A revoked record (a tombstone) still names the payee: the controller key.
test expire_challenge_of_revoked_challenger_pays_controller() {
  expire(
    [record_input(test_agent_did, revoked(record(1, test_capabilities)))],
    to_key(controller_hash(1), 25_000_000),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// DistributeOutcome
// ──────────────────────────────────────────────────────────────────────────────

fn challenge_input(outcome: RepChallengeOutcome) -> Input {
  Input {
    output_reference: OutputReference { transaction_id: test_tx_hash, output_index: 70 },
    output: make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
        |> assets.add(test_endorsement_policy_id, challenge_token(), 1),
      Challenge(challenge(Resolved { outcome })),
    ),
  }
}

fn distribute(
  outcome: RepChallengeOutcome,
  refs: List<Input>,
  outputs: List<Output>,
) -> Bool {
  let own = challenge_input(outcome)
  validate_distribute_outcome(
    Transaction {
      ..base_tx(5_000),
      inputs: [own, locked_stake()],
      reference_inputs: refs,
      outputs,
      mint: burn(),
    },
    test_config(),
    test_refs(),
    test_params(),
    own,
    challenge(Resolved { outcome }),
  )
}

// CapabilityVerified: the target's payee receives 25 - 1.25 AP3X.

test distribute_verified_pays_declared_target_key() {
  distribute(
    CapabilityVerified,
    [target_record()],
    [to_key(target_key(), 23_750_000), to_treasury(1_250_000)],
  )
}

test distribute_verified_after_key_removal_pays_target_controller() {
  distribute(
    CapabilityVerified,
    [target_record_without_key()],
    [to_key(controller_hash(2), 23_750_000), to_treasury(1_250_000)],
  )
}

test distribute_verified_after_key_removal_paying_removed_key_fails() {
  !distribute(
    CapabilityVerified,
    [target_record_without_key()],
    [to_key(target_key(), 23_750_000), to_treasury(1_250_000)],
  )
}

test distribute_verified_without_target_record_fails() {
  !distribute(
    CapabilityVerified,
    [],
    [to_key(target_key(), 23_750_000), to_treasury(1_250_000)],
  )
}

// CapabilityFalsified: the stake (20 AP3X, 3 capabilities) loses 6.666666
// AP3X; the challenger's payee receives 25 + slash - fee.

fn falsified_payout() -> Int {
  let slash = 20_000_000 / 3
  25_000_000 + slash - slash * 500 / 10_000
}

fn falsified_fee() -> Int {
  20_000_000 / 3 * 500 / 10_000
}

test distribute_falsified_after_key_removal_pays_challenger_controller() {
  distribute(
    CapabilityFalsified,
    [challenger_record_without_key()],
    [to_key(controller_hash(1), falsified_payout()), to_treasury(falsified_fee())],
  )
}

test distribute_falsified_after_key_removal_paying_removed_key_fails() {
  !distribute(
    CapabilityFalsified,
    [challenger_record_without_key()],
    [to_key(challenger_key(), falsified_payout()), to_treasury(falsified_fee())],
  )
}

// Inconclusive: the challenger's payee receives 25 - 0.625 AP3X.

test distribute_inconclusive_after_key_removal_pays_challenger_controller() {
  distribute(
    Inconclusive,
    [challenger_record_without_key()],
    [to_key(controller_hash(1), 24_375_000), to_treasury(625_000)],
  )
}

test distribute_inconclusive_after_key_removal_paying_removed_key_fails() {
  !distribute(
    Inconclusive,
    [challenger_record_without_key()],
    [to_key(challenger_key(), 24_375_000), to_treasury(625_000)],
  )
}
