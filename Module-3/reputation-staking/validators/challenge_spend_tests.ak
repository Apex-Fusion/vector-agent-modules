/// Challenge spend tests (validator level).
///
/// Each test builds a full transaction and calls the spend handler of the
/// endorsement validator for a challenge output. Times are POSIX
/// milliseconds; the test parameters use the deployment values.
///
/// The paths that close a challenge (WithdrawChallenge, ExpireChallenge)
/// also spend the target's StakeUTXO, which removes the lock of the
/// challenge (W4 item 1).

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use endorsement
use reputation_staking/challenge_validation.{challenge_timeout}
use reputation_staking/test_helpers.{
  base_tx, key_address, make_identity_ref_input, make_input, make_output,
  make_output_no_datum, make_params_ref_input, script_address, test_agent_did,
  test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id, test_config,
  test_controller_hash, test_endorsement_policy_id, test_owner_key_hash,
  test_owner_key_hash_2, test_params, test_refs, test_refs_token_policy,
  test_reputation_policy_id, test_tx_hash,
}
use reputation_staking/types.{
  CapabilityFalsified, Challenge, ChallengeSpend, DefaultJudgment,
  ExpireChallenge, Open, ReputationChallengeDatum, Resolved,
  RespondToChallenge, StakeDatum, WithdrawChallenge,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name,
}

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

const created_at: Int = 1_000

/// created_at + challenge_response_deadline (12 h)
const response_deadline: Int = 43_201_000

const challenge_stake: Int = 25_000_000

const counter_hash: ByteArray =
  #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

fn challenge_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"7777777777777777777777777777777777777777777777777777777777777777",
    output_index: 0,
  }
}

fn open_challenge() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did,
    challenger_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    target_credential: VerificationKey(test_owner_key_hash_2),
    challenged_capability: "code_review",
    stake_amount: challenge_stake,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at,
    counter_evidence_hash: "",
    counter_evidence_uri: "",
    response_submitted_at: 0,
    state: Open,
  }
}

fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

fn challenge_value(ap3x: Int) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, ap3x)
    |> assets.add(test_endorsement_policy_id, challenge_token(), 1)
}

fn challenge_output(datum: ReputationChallengeDatum, ap3x: Int) -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    challenge_value(ap3x),
    Challenge(datum),
  )
}

fn challenge_input(datum: ReputationChallengeDatum) -> Input {
  Input {
    output_reference: challenge_ref(),
    output: challenge_output(datum, challenge_stake),
  }
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

fn ap3x_to(key: ByteArray, amount: Int) -> Output {
  make_output_no_datum(
    key_address(key),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

/// The target's StakeUTXO with the lock of the test challenge.
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
        staked_capabilities: ["code_review", "testing"],
        last_updated: 500,
        history_points: 0,
        challenge_locks: [challenge_token()],
      },
    ),
  )
}

fn challenge_tx(
  datum: ReputationChallengeDatum,
  current_slot: Int,
  outputs: List<Output>,
  signers: List<ByteArray>,
) -> Transaction {
  Transaction {
    ..base_tx(current_slot),
    inputs: [challenge_input(datum)],
    reference_inputs: [make_params_ref_input(), refs_ref_input()],
    outputs: outputs,
    extra_signatories: signers,
  }
}

fn spend(
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

fn respond_action() -> types.RepChallengeAction {
  RespondToChallenge {
    counter_evidence_hash: counter_hash,
    counter_evidence_uri: "ipfs://counter-evidence",
  }
}

fn responded(at: Int) -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    ..open_challenge(),
    counter_evidence_hash: counter_hash,
    counter_evidence_uri: "ipfs://counter-evidence",
    response_submitted_at: at,
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// RespondToChallenge: the continuing output keeps the challenge stake
// ──────────────────────────────────────────────────────────────────────────────

test respond_keeps_challenge_stake() {
  let now = created_at + 3_600_000
  let tx =
    challenge_tx(
      open_challenge(),
      now,
      [challenge_output(responded(now), challenge_stake)],
      [test_owner_key_hash_2],
    )
  spend(open_challenge(), respond_action(), tx)
}

/// The old rule did not check the value of the continuing challenge output.
test respond_with_less_challenge_stake_fails() fail {
  let now = created_at + 3_600_000
  let tx =
    challenge_tx(
      open_challenge(),
      now,
      [
        challenge_output(responded(now), 1),
        ap3x_to(test_owner_key_hash_2, challenge_stake - 1),
      ],
      [test_owner_key_hash_2],
    )
  spend(open_challenge(), respond_action(), tx)
}

/// The continuing output stays at the address of the challenge.
test respond_with_output_at_other_address_fails() fail {
  let now = created_at + 3_600_000
  let moved =
    Output {
      ..challenge_output(responded(now), challenge_stake),
      address: script_address(test_endorsement_policy_id)
        |> fn(a) {
            address.Address {
              ..a,
              stake_credential: Some(
                address.Inline(VerificationKey(test_owner_key_hash_2)),
              ),
            }
          },
    }
  let tx = challenge_tx(open_challenge(), now, [moved], [test_owner_key_hash_2])
  spend(open_challenge(), respond_action(), tx)
}

// ──────────────────────────────────────────────────────────────────────────────
// DefaultJudgment: only the fee leaves the challenge output
// ──────────────────────────────────────────────────────────────────────────────

/// fee = 25_000_000 * 100 / 10_000
const default_judgment_fee: Int = 250_000

const submitter_key: ByteArray =
  #"5500000000000000000000000000000000000000000000000000000000000055"

fn judged() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    ..open_challenge(),
    state: Resolved { outcome: CapabilityFalsified },
  }
}

test default_judgment_keeps_stake_less_fee() {
  let tx =
    challenge_tx(
      open_challenge(),
      response_deadline + 1,
      [
        challenge_output(judged(), challenge_stake - default_judgment_fee),
        ap3x_to(submitter_key, default_judgment_fee),
      ],
      [submitter_key],
    )
  spend(open_challenge(), DefaultJudgment, tx)
}

/// The old rule did not check the value of the continuing challenge output.
test default_judgment_taking_more_than_fee_fails() fail {
  let tx =
    challenge_tx(
      open_challenge(),
      response_deadline + 1,
      [
        challenge_output(judged(), 1),
        ap3x_to(submitter_key, challenge_stake - 1),
      ],
      [submitter_key],
    )
  spend(open_challenge(), DefaultJudgment, tx)
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawChallenge: the deadline check uses the upper bound
// ──────────────────────────────────────────────────────────────────────────────

/// A challenge withdrawal: the challenger's identity record is a reference
/// input and its controller signs.
fn withdraw_tx(validity_range: interval.Interval) -> Transaction {
  let tx =
    challenge_tx(
      open_challenge(),
      0,
      [ap3x_to(test_owner_key_hash, challenge_stake)],
      [test_controller_hash(1)],
    )
  Transaction {
    ..tx,
    inputs: [locked_target_stake(), ..tx.inputs],
    reference_inputs: [
      make_identity_ref_input(test_agent_did, 1),
      ..tx.reference_inputs
    ],
    validity_range,
    mint: assets.from_asset(test_endorsement_policy_id, challenge_token(), -1),
  }
}

test withdraw_challenge_before_deadline() {
  let tx = withdraw_tx(interval.between(created_at + 1_000, created_at + 2_000))
  spend(open_challenge(), WithdrawChallenge, tx)
}

/// The old rule compared the lower bound with the deadline. A range that
/// starts before the deadline and ends after it does not pass.
test withdraw_challenge_with_upper_bound_after_deadline_fails() {
  let tx =
    withdraw_tx(
      interval.between(response_deadline - 1_000, response_deadline + 1_000),
    )
  !spend(open_challenge(), WithdrawChallenge, tx)
}

// ──────────────────────────────────────────────────────────────────────────────
// ExpireChallenge: the challenge timeout returns the challenge stake
// ──────────────────────────────────────────────────────────────────────────────

fn expire_tx(current_slot: Int) -> Transaction {
  let tx =
    challenge_tx(
      responded(5_000),
      current_slot,
      [ap3x_to(test_owner_key_hash, challenge_stake)],
      [],
    )
  Transaction {
    ..tx,
    inputs: [locked_target_stake(), ..tx.inputs],
    mint: assets.from_asset(test_endorsement_policy_id, challenge_token(), -1),
  }
}

test expire_responded_challenge_after_timeout() {
  let tx = expire_tx(challenge_timeout(test_params(), created_at) + 1)
  spend(responded(5_000), ExpireChallenge, tx)
}

test expire_responded_challenge_before_timeout_fails() {
  let tx = expire_tx(challenge_timeout(test_params(), created_at))
  !spend(responded(5_000), ExpireChallenge, tx)
}

/// W4 item 1: the withdrawal also spends the target's StakeUTXO, so that the
/// lock of the challenge goes. Before, the stake was not an input.
test withdraw_challenge_without_target_stake_fails() {
  let tx = withdraw_tx(interval.between(created_at + 1_000, created_at + 2_000))
  !spend(
    open_challenge(),
    WithdrawChallenge,
    Transaction {
      ..tx,
      inputs: [challenge_input(open_challenge())],
    },
  )
}
