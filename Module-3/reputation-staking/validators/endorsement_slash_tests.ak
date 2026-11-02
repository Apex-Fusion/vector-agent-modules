/// Endorsement slash tests (validator level).
///
/// After a challenge resolves with CapabilityFalsified, SlashEndorsement
/// moves the endorser slash (less the protocol fee) to the challenger and the
/// fee to the treasury. The endorsement continues with the remaining stake,
/// or closes and returns the remaining stake to the endorser.
///
/// R4 P-3: a resolved challenge slashes an endorsement at most once. The
/// continuing endorsement records the challenge token name in slashed_by,
/// and a slash with a recorded name fails.
///
/// The bound stake of the endorsement (target agent 2, stake_id
/// test_stake_id) is a reference input and holds the lock of the challenge.
/// The payouts go to the payees of the challenger (agent 1) and of the
/// endorser (agent 3); their records are reference inputs.

use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use endorsement
use reputation_staking/test_helpers.{
  base_tx, key_address, make_identity_ref_input, make_input, make_output,
  make_output_no_datum, make_params_ref_input, script_address,
  stake_ref_input, test_agent_did, test_agent_did_2, test_ap3x_asset_name,
  test_ap3x_policy_id, test_capabilities, test_config,
  test_endorsement_policy_id, test_owner_key_hash, test_owner_key_hash_2,
  test_refs, test_refs_token_policy, test_stake_id, test_treasury_script_hash,
  test_tx_hash,
}
use reputation_staking/types.{
  CapabilityFalsified, Challenge, Endorsement, EndorsementDatum,
  EndorsementSpend, ReputationChallengeDatum, Resolved, SlashEndorsement,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
}
use shared/identity_fixtures.{
  controller_hash, ed_key_hash, record, record_input, with_transaction_key,
}

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

const endorser_did: ByteArray = "agent_did_003"

/// The endorser's key: the listed transaction key 3 of the endorser's record.
fn endorser_key() -> ByteArray {
  ed_key_hash(3)
}

/// The record of the endorser (controller 3, listed transaction key 3).
fn endorser_record_input() -> Input {
  record_input(endorser_did, with_transaction_key(record(3, test_capabilities), 3))
}

const other_key: ByteArray =
  #"aabb00000000000000000000000000000000000000000000000000000000000d"

fn endorsement_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"8888888888888888888888888888888888888888888888888888888888888888",
    output_index: 0,
  }
}

fn challenge_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"9999999999999999999999999999999999999999999999999999999999999999",
    output_index: 0,
  }
}

/// The challenge token of the falsified challenge (challenger agent 1,
/// target agent 2, code_review).
fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

/// An endorsement of agent 2 that no challenge slashed yet.
fn endorsement_datum(stake: Int) -> EndorsementDatum {
  EndorsementDatum {
    endorser_did,
    endorser_credential: VerificationKey(endorser_key()),
    target_did: test_agent_did_2,
    stake_amount: stake,
    endorsed_capabilities: ["code_review"],
    created_at: 1_000,
    slashed_by: [],
    target_stake: test_stake_id,
  }
}

/// The endorsement after the falsified challenge slashed it.
fn slashed_datum(stake: Int) -> EndorsementDatum {
  EndorsementDatum { ..endorsement_datum(stake), slashed_by: [challenge_token()] }
}

fn endorsement_token() -> ByteArray {
  derive_endorsement_token_name(endorser_did, test_agent_did_2)
}

fn endorsement_value(ap3x: Int) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, ap3x)
    |> assets.add(test_endorsement_policy_id, endorsement_token(), 1)
}

fn endorsement_input(datum: EndorsementDatum) -> Input {
  Input {
    output_reference: endorsement_ref(),
    output: make_output(
      script_address(test_endorsement_policy_id),
      endorsement_value(datum.stake_amount),
      Endorsement(datum),
    ),
  }
}

/// A challenge against the target that resolved with CapabilityFalsified.
fn falsified_challenge_ref_input() -> Input {
  let challenge =
    ReputationChallengeDatum {
      challenger_did: test_agent_did,
      challenger_credential: VerificationKey(test_owner_key_hash),
      target_did: test_agent_did_2,
      target_credential: VerificationKey(test_owner_key_hash_2),
      challenged_capability: "code_review",
      stake_amount: 25_000_000,
      evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      evidence_uri: "ipfs://evidence",
      created_at: 1_000,
      counter_evidence_hash: "",
      counter_evidence_uri: "",
      response_submitted_at: 0,
      state: Resolved { outcome: CapabilityFalsified },
    }
  Input {
    output_reference: challenge_ref(),
    output: make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
        |> assets.add(test_endorsement_policy_id, challenge_token(), 1),
      Challenge(challenge),
    ),
  }
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

fn ap3x_output(address: address.Address, amount: Int) -> Output {
  make_output_no_datum(
    address,
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

/// SlashEndorsement of the endorsement with `input_datum`. The reference
/// inputs hold the bound stake with `locks` and the identity records
/// `records`.
fn slash_tx(
  input_datum: EndorsementDatum,
  locks: List<ByteArray>,
  records: List<Input>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  let tx =
    Transaction {
      ..base_tx(2_000),
      inputs: [endorsement_input(input_datum)],
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        falsified_challenge_ref_input(),
        stake_ref_input(test_agent_did_2, test_stake_id, locks),
        ..records
      ],
      outputs,
      mint,
    }
  endorsement.endorsement.spend(
    test_config(),
    Some(Endorsement(input_datum)),
    EndorsementSpend(SlashEndorsement { challenge_ref: challenge_ref() }),
    endorsement_ref(),
    tx,
  )
}

/// The records of the challenger (agent 1) and of the endorser (agent 3).
fn party_records() -> List<Input> {
  [make_identity_ref_input(test_agent_did, 1), endorser_record_input()]
}

/// SlashEndorsement with the bound stake `locks` and both party records.
fn slash_with_locks(
  input_datum: EndorsementDatum,
  locks: List<ByteArray>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  slash_tx(input_datum, locks, party_records(), outputs, mint)
}

/// SlashEndorsement of the endorsement with `input_datum`; the challenge
/// locks the bound stake.
fn slash_from(
  input_datum: EndorsementDatum,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  slash_with_locks(input_datum, [challenge_token()], outputs, mint)
}

/// SlashEndorsement of a new endorsement of `stake` AP3X.
fn slash(stake: Int, outputs: List<Output>, mint: assets.Value) -> Bool {
  slash_from(endorsement_datum(stake), outputs, mint)
}

fn continuing_endorsement(datum: EndorsementDatum) -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    endorsement_value(datum.stake_amount),
    Endorsement(datum),
  )
}

// stake 10 AP3X: slash 5, remaining 5 (= min_endorsement), fee 0.25
const stake_continue: Int = 10_000_000

// stake 6 AP3X: slash 3, remaining 3 (< min_endorsement), fee 0.15
const stake_close: Int = 6_000_000

// ──────────────────────────────────────────────────────────────────────────────
// Continuing endorsement
// ──────────────────────────────────────────────────────────────────────────────

test slash_endorsement_pays_challenger_and_treasury() {
  slash(
    stake_continue,
    [
      continuing_endorsement(slashed_datum(5_000_000)),
      ap3x_output(key_address(test_owner_key_hash), 4_750_000),
      ap3x_output(script_address(test_treasury_script_hash), 250_000),
    ],
    assets.zero,
  )
}

/// The old rule did not say where the slashed AP3X goes.
test slash_endorsement_without_payment_to_challenger_fails() {
  !slash(
    stake_continue,
    [
      continuing_endorsement(slashed_datum(5_000_000)),
      ap3x_output(key_address(other_key), 5_000_000),
    ],
    assets.zero,
  )
}

/// The old rule checked only the stake amount and the DIDs of the continuing
/// datum. The other fields stay the same.
test slash_endorsement_with_changed_endorser_credential_fails() {
  let changed =
    EndorsementDatum {
      ..slashed_datum(5_000_000),
      endorser_credential: VerificationKey(other_key),
    }
  !slash(
    stake_continue,
    [
      continuing_endorsement(changed),
      ap3x_output(key_address(test_owner_key_hash), 4_750_000),
      ap3x_output(script_address(test_treasury_script_hash), 250_000),
    ],
    assets.zero,
  )
}

/// The old rule did not check the address of the continuing endorsement.
test slash_endorsement_with_output_at_other_address_fails() {
  let moved =
    Output {
      ..continuing_endorsement(slashed_datum(5_000_000)),
      address: key_address(other_key),
    }
  !slash(
    stake_continue,
    [
      moved,
      ap3x_output(key_address(test_owner_key_hash), 4_750_000),
      ap3x_output(script_address(test_treasury_script_hash), 250_000),
    ],
    assets.zero,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// R4 P-3: one slash per challenge and endorsement
// ──────────────────────────────────────────────────────────────────────────────

/// The R4 PoC: the same resolved challenge slashes the continuing
/// endorsement again (20 AP3X to 10 AP3X), with the continuing datum that
/// the old rule accepted. The endorsement records the challenge, so the
/// second slash fails, whatever the continuing datum is.
test slash_endorsement_twice_with_same_challenge_fails() {
  !slash_from(
    slashed_datum(20_000_000),
    [
      continuing_endorsement(slashed_datum(10_000_000)),
      ap3x_output(key_address(test_owner_key_hash), 9_500_000),
      ap3x_output(script_address(test_treasury_script_hash), 500_000),
    ],
    assets.zero,
  )
}

test slash_endorsement_twice_recording_challenge_again_fails() {
  !slash_from(
    slashed_datum(20_000_000),
    [
      continuing_endorsement(
        EndorsementDatum {
          ..slashed_datum(10_000_000),
          slashed_by: [challenge_token(), challenge_token()],
        },
      ),
      ap3x_output(key_address(test_owner_key_hash), 9_500_000),
      ap3x_output(script_address(test_treasury_script_hash), 500_000),
    ],
    assets.zero,
  )
}

/// The continuing endorsement must record the challenge.
test slash_endorsement_without_recording_challenge_fails() {
  !slash(
    stake_continue,
    [
      continuing_endorsement(endorsement_datum(5_000_000)),
      ap3x_output(key_address(test_owner_key_hash), 4_750_000),
      ap3x_output(script_address(test_treasury_script_hash), 250_000),
    ],
    assets.zero,
  )
}

/// Another challenge can still slash an endorsement that one challenge
/// slashed before.
test slash_endorsement_after_slash_by_other_challenge() {
  let other =
    derive_challenge_token_name(test_agent_did_2, test_agent_did_2, "testing")
  let input_datum =
    EndorsementDatum { ..endorsement_datum(stake_continue), slashed_by: [other] }
  slash_from(
    input_datum,
    [
      continuing_endorsement(
        EndorsementDatum {
          ..input_datum,
          stake_amount: 5_000_000,
          slashed_by: [challenge_token(), other],
        },
      ),
      ap3x_output(key_address(test_owner_key_hash), 4_750_000),
      ap3x_output(script_address(test_treasury_script_hash), 250_000),
    ],
    assets.zero,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Closing endorsement (remaining below min_endorsement)
// ──────────────────────────────────────────────────────────────────────────────

fn endorsement_burn() -> assets.Value {
  assets.from_asset(test_endorsement_policy_id, endorsement_token(), -1)
}

test slash_endorsement_closes_and_returns_remaining() {
  slash(
    stake_close,
    [
      ap3x_output(key_address(endorser_key()), 3_000_000),
      ap3x_output(key_address(test_owner_key_hash), 2_850_000),
      ap3x_output(script_address(test_treasury_script_hash), 150_000),
    ],
    endorsement_burn(),
  )
}

/// The old rule only required the token burn.
test slash_endorsement_closing_without_payment_to_endorser_fails() {
  !slash(
    stake_close,
    [
      ap3x_output(key_address(other_key), 3_000_000),
      ap3x_output(key_address(test_owner_key_hash), 2_850_000),
      ap3x_output(script_address(test_treasury_script_hash), 150_000),
    ],
    endorsement_burn(),
  )
}

/// R4 P-3: a closing slash with a challenge that already slashed the
/// endorsement fails too.
test slash_endorsement_closing_twice_with_same_challenge_fails() {
  !slash_from(
    slashed_datum(stake_close),
    [
      ap3x_output(key_address(endorser_key()), 3_000_000),
      ap3x_output(key_address(test_owner_key_hash), 2_850_000),
      ap3x_output(script_address(test_treasury_script_hash), 150_000),
    ],
    endorsement_burn(),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// The challenge locks the bound stake (W4-MODFIX2 item 1)
// ──────────────────────────────────────────────────────────────────────────────

fn continuing_outputs() -> List<Output> {
  [
    continuing_endorsement(slashed_datum(5_000_000)),
    ap3x_output(key_address(test_owner_key_hash), 4_750_000),
    ap3x_output(script_address(test_treasury_script_hash), 250_000),
  ]
}

/// The bound stake holds no lock of the challenge: the challenge did not
/// challenge the stake that this endorsement backs. The old rule did not
/// read the bound stake.
test slash_endorsement_without_lock_on_bound_stake_fails() {
  !slash_with_locks(
    endorsement_datum(stake_continue),
    [],
    continuing_outputs(),
    assets.zero,
  )
}

test slash_endorsement_with_lock_of_other_challenge_fails() {
  !slash_with_locks(
    endorsement_datum(stake_continue),
    [derive_challenge_token_name(endorser_did, test_agent_did_2, "code_review")],
    continuing_outputs(),
    assets.zero,
  )
}

test slash_endorsement_without_bound_stake_fails() fail {
  let datum = endorsement_datum(stake_continue)
  let tx =
    Transaction {
      ..base_tx(2_000),
      inputs: [endorsement_input(datum)],
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        falsified_challenge_ref_input(),
        ..party_records()
      ],
      outputs: continuing_outputs(),
    }
  endorsement.endorsement.spend(
    test_config(),
    Some(Endorsement(datum)),
    EndorsementSpend(SlashEndorsement { challenge_ref: challenge_ref() }),
    endorsement_ref(),
    tx,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Payees (W4-MODFIX2 item 5)
// ──────────────────────────────────────────────────────────────────────────────

/// The record of the endorser after the removal of its listed key.
fn endorser_record_without_key() -> Input {
  record_input(endorser_did, record(3, test_capabilities))
}

fn closing_outputs(endorser_payee: ByteArray, challenger_payee: ByteArray) -> List<Output> {
  [
    ap3x_output(key_address(endorser_payee), 3_000_000),
    ap3x_output(key_address(challenger_payee), 2_850_000),
    ap3x_output(script_address(test_treasury_script_hash), 150_000),
  ]
}

/// The endorser's record no longer lists its key: the remaining stake goes
/// to the record's controller key.
test slash_endorsement_closing_after_endorser_key_removal_pays_controller() {
  slash_tx(
    endorsement_datum(stake_close),
    [challenge_token()],
    [make_identity_ref_input(test_agent_did, 1), endorser_record_without_key()],
    closing_outputs(controller_hash(3), test_owner_key_hash),
    endorsement_burn(),
  )
}

/// The old rule paid the stored endorser key.
test slash_endorsement_closing_after_endorser_key_removal_paying_removed_key_fails() {
  !slash_tx(
    endorsement_datum(stake_close),
    [challenge_token()],
    [make_identity_ref_input(test_agent_did, 1), endorser_record_without_key()],
    closing_outputs(endorser_key(), test_owner_key_hash),
    endorsement_burn(),
  )
}

/// The challenger's record no longer lists its key: the slash share goes to
/// the challenger's controller key.
test slash_endorsement_after_challenger_key_removal_pays_controller() {
  slash_tx(
    endorsement_datum(stake_continue),
    [challenge_token()],
    [
      record_input(test_agent_did, record(1, test_capabilities)),
      endorser_record_input(),
    ],
    [
      continuing_endorsement(slashed_datum(5_000_000)),
      ap3x_output(key_address(controller_hash(1)), 4_750_000),
      ap3x_output(script_address(test_treasury_script_hash), 250_000),
    ],
    assets.zero,
  )
}

test slash_endorsement_after_challenger_key_removal_paying_removed_key_fails() {
  !slash_tx(
    endorsement_datum(stake_continue),
    [challenge_token()],
    [
      record_input(test_agent_did, record(1, test_capabilities)),
      endorser_record_input(),
    ],
    continuing_outputs(),
    assets.zero,
  )
}

test slash_endorsement_without_challenger_record_fails() fail {
  slash_tx(
    endorsement_datum(stake_continue),
    [challenge_token()],
    [endorser_record_input()],
    continuing_outputs(),
    assets.zero,
  )
}

/// A continuing endorsement pays nothing to the endorser, so the endorser's
/// record is not needed.
test slash_endorsement_continuing_without_endorser_record() {
  slash_tx(
    endorsement_datum(stake_continue),
    [challenge_token()],
    [make_identity_ref_input(test_agent_did, 1)],
    continuing_outputs(),
    assets.zero,
  )
}
