/// Tests for challenge validation logic.
///
/// Times are POSIX milliseconds. The test parameters use the deployment
/// values: challenge_response_deadline = 43_200_000 (12 h),
/// escalation_window = 21_600_000 (6 h), activity_window = 180 epochs of
/// 14_400_000 ms (30 days).

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Transaction}
use reputation_staking/challenge_validation.{
  challenge_timeout, validate_default_judgment, validate_expire_challenge,
  validate_mint_challenge, validate_respond_to_challenge,
  validate_resolve_challenge,
}
use reputation_staking/test_helpers.{
  base_tx, key_address, make_input, make_output, make_output_no_datum,
  make_registry_ref_input, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_config,
  test_endorsement_policy_id, test_oracle_credential, test_owner_key_hash,
  test_owner_key_hash_2, test_params, test_refs, test_reputation_policy_id,
  test_tx_hash,
}
use reputation_staking/types.{
  CapabilityFalsified, CapabilityVerified, Challenge, Escalated, Inconclusive,
  Open, ReputationChallengeDatum, Resolved, StakeDatum,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name,
}

// ──────────────────────────────────────────────────────────────────────────────
// Helper: make a base challenge datum
// ──────────────────────────────────────────────────────────────────────────────

const created_at: Int = 1_000

/// created_at + challenge_response_deadline
const response_deadline: Int = 43_201_000

fn base_challenge_datum() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did,
    challenger_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    target_credential: VerificationKey(test_owner_key_hash_2),
    challenged_capability: "code_review",
    stake_amount: 25_000_000,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at,
    counter_evidence_hash: "",
    counter_evidence_uri: "",
    response_submitted_at: 0,
    state: Open,
  }
}

fn respond_at(current_slot: Int) -> Bool {
  let params = test_params()
  let input_datum = base_challenge_datum()
  let counter_hash =
    #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  let counter_uri = "ipfs://counter-evidence"
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      counter_evidence_hash: counter_hash,
      counter_evidence_uri: counter_uri,
      response_submitted_at: current_slot,
    }
  let tx =
    Transaction {
      ..base_tx(current_slot),
      extra_signatories: [test_owner_key_hash_2],
    }
  validate_respond_to_challenge(
    tx, params, input_datum, output_datum, counter_hash, counter_uri,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// RespondToChallenge tests
// ──────────────────────────────────────────────────────────────────────────────

test respond_to_challenge_happy_path() {
  respond_at(created_at + 5_000)
}

/// The response deadline is 12 hours in milliseconds. A response one hour
/// after the challenge is in time (with the old slot value 10_800 the
/// deadline was 10.8 seconds after the challenge).
test respond_to_challenge_one_hour_later() {
  respond_at(created_at + 3_600_000)
}

test respond_to_challenge_past_deadline_fails() {
  !respond_at(response_deadline + 1)
}

/// The old rule compared the lower bound of the validity range with the
/// deadline. The new rule compares the upper bound: base_tx(t) ends at
/// t + 100, after the deadline.
test respond_to_challenge_with_upper_bound_after_deadline_fails() {
  !respond_at(response_deadline - 50)
}

/// The response time is the lower bound of a validity range of at most one
/// hour.
test respond_to_challenge_without_bounded_range_fails() {
  let params = test_params()
  let input_datum = base_challenge_datum()
  let counter_hash =
    #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  let counter_uri = "ipfs://counter-evidence"
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      counter_evidence_hash: counter_hash,
      counter_evidence_uri: counter_uri,
      response_submitted_at: 1,
    }
  // Lower bound 1, upper bound 1 + 1 day
  let tx =
    Transaction {
      ..base_tx(1),
      validity_range: interval.between(1, 86_400_001),
      extra_signatories: [test_owner_key_hash_2],
    }
  !validate_respond_to_challenge(
    tx, params, input_datum, output_datum, counter_hash, counter_uri,
  )
}

test respond_to_challenge_bad_hash_fails() fail {
  let params = test_params()
  let current_slot = created_at + 5_000

  let input_datum = base_challenge_datum()

  // Hash not 32 bytes
  let counter_hash = #"aabb"
  let counter_uri = "ipfs://counter-evidence"

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      counter_evidence_hash: counter_hash,
      counter_evidence_uri: counter_uri,
      response_submitted_at: current_slot,
    }

  let tx =
    Transaction {
      ..base_tx(current_slot),
      extra_signatories: [test_owner_key_hash_2],
    }

  validate_respond_to_challenge(
    tx, params, input_datum, output_datum, counter_hash, counter_uri,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// DefaultJudgment tests
// ──────────────────────────────────────────────────────────────────────────────

test default_judgment_happy_path() {
  let cfg = test_config()
  let params = test_params()
  // Past response deadline, no response
  let current_slot = response_deadline + 1

  let input_datum = base_challenge_datum()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityFalsified },
    }

  // DEFAULT_JUDGMENT_FEE: fee = 25_000_000 * 100 / 10_000 = 250_000
  let submitter_key =
    #"5500000000000000000000000000000000000000000000000000000000000055"
  let fee_output =
    make_output(
      key_address(submitter_key),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 250_000),
      0,
    )

  let tx =
    Transaction {
      ..base_tx(current_slot),
      outputs: [fee_output],
      extra_signatories: [submitter_key],
    }

  validate_default_judgment(tx, cfg, input_datum, output_datum, params, current_slot)
}

test default_judgment_before_deadline_fails() fail {
  let cfg = test_config()
  let params = test_params()
  // Before deadline
  let current_slot = created_at + 5_000

  let input_datum = base_challenge_datum()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityFalsified },
    }

  let tx = base_tx(current_slot)

  validate_default_judgment(tx, cfg, input_datum, output_datum, params, current_slot)
}

test default_judgment_with_response_fails() fail {
  let cfg = test_config()
  let params = test_params()
  let current_slot = response_deadline + 1

  // Target already responded
  let input_datum =
    ReputationChallengeDatum {
      ..base_challenge_datum(),
      response_submitted_at: 5_000,
    }

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityFalsified },
    }

  let tx = base_tx(current_slot)

  validate_default_judgment(tx, cfg, input_datum, output_datum, params, current_slot)
}

// ──────────────────────────────────────────────────────────────────────────────
// ResolveChallenge (Oracle) tests
// ──────────────────────────────────────────────────────────────────────────────

test resolve_challenge_verified() {
  let cfg = test_config()

  let input_datum = base_challenge_datum()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityVerified },
    }

  let tx =
    Transaction { ..base_tx(5_000), extra_signatories: [test_oracle_credential] }

  validate_resolve_challenge(tx, cfg, input_datum, output_datum, CapabilityVerified)
}

test resolve_challenge_falsified() {
  let cfg = test_config()

  let input_datum = base_challenge_datum()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityFalsified },
    }

  let tx =
    Transaction { ..base_tx(5_000), extra_signatories: [test_oracle_credential] }

  validate_resolve_challenge(tx, cfg, input_datum, output_datum, CapabilityFalsified)
}

test resolve_challenge_inconclusive() {
  let cfg = test_config()

  let input_datum = base_challenge_datum()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: Inconclusive },
    }

  let tx =
    Transaction { ..base_tx(5_000), extra_signatories: [test_oracle_credential] }

  validate_resolve_challenge(tx, cfg, input_datum, output_datum, Inconclusive)
}

test resolve_challenge_without_oracle_fails() fail {
  let cfg = test_config()

  let input_datum = base_challenge_datum()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityVerified },
    }

  // No oracle signature
  let tx = base_tx(5_000)

  validate_resolve_challenge(tx, cfg, input_datum, output_datum, CapabilityVerified)
}

// ──────────────────────────────────────────────────────────────────────────────
// ExpireChallenge tests (challenge timeout)
// ──────────────────────────────────────────────────────────────────────────────

/// A challenge with a response that nobody resolved.
fn responded_challenge() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    ..base_challenge_datum(),
    counter_evidence_hash: #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    counter_evidence_uri: "ipfs://counter-evidence",
    response_submitted_at: 5_000,
  }
}

fn challenge_token_burn(datum: ReputationChallengeDatum) -> assets.Value {
  assets.from_asset(
    test_endorsement_policy_id,
    derive_challenge_token_name(
      datum.challenger_did,
      datum.target_did,
      datum.challenged_capability,
    ),
    -1,
  )
}

fn stake_to_challenger(amount: Int) -> transaction.Output {
  make_output_no_datum(
    key_address(test_owner_key_hash),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

fn expire_at(datum: ReputationChallengeDatum, current_slot: Int) -> Bool {
  let tx =
    Transaction {
      ..base_tx(current_slot),
      outputs: [stake_to_challenger(datum.stake_amount)],
      mint: challenge_token_burn(datum),
    }
  validate_expire_challenge(tx, test_config(), test_refs(), test_params(), datum)
}

/// The timeout is created_at + 12 h + 6 h + 180 × 4 h.
test challenge_timeout_uses_existing_parameters() {
  challenge_timeout(test_params(), created_at) == created_at + 43_200_000 + 21_600_000 + 180 * 14_400_000
}

/// The old validator had no path that ends a responded challenge without the
/// oracle or an escalation. After the timeout anybody can close it, and the
/// challenger receives the challenge stake.
test expire_responded_challenge_after_timeout() {
  let datum = responded_challenge()
  expire_at(datum, challenge_timeout(test_params(), created_at) + 1)
}

test expire_escalated_challenge_after_timeout() {
  let datum =
    ReputationChallengeDatum {
      ..responded_challenge(),
      state: Escalated {
        audit_claim_ref: transaction.OutputReference {
          transaction_id: #"0000000000000000000000000000000000000000000000000000000000000001",
          output_index: 0,
        },
      },
    }
  expire_at(datum, challenge_timeout(test_params(), created_at) + 1)
}

test expire_challenge_before_timeout_fails() {
  let datum = responded_challenge()
  !expire_at(datum, challenge_timeout(test_params(), created_at))
}

/// A challenge without a response ends with DefaultJudgment, not with the
/// timeout.
test expire_challenge_without_response_fails() {
  let datum = base_challenge_datum()
  !expire_at(datum, challenge_timeout(test_params(), created_at) + 1)
}

test expire_resolved_challenge_fails() {
  let datum =
    ReputationChallengeDatum {
      ..responded_challenge(),
      state: Resolved { outcome: Inconclusive },
    }
  !expire_at(datum, challenge_timeout(test_params(), created_at) + 1)
}

test expire_challenge_without_payment_to_challenger_fails() {
  let datum = responded_challenge()
  let tx =
    Transaction {
      ..base_tx(challenge_timeout(test_params(), created_at) + 1),
      outputs: [stake_to_challenger(datum.stake_amount - 1)],
      mint: challenge_token_burn(datum),
    }
  !validate_expire_challenge(tx, test_config(), test_refs(), test_params(), datum)
}

test expire_challenge_without_token_burn_fails() {
  let datum = responded_challenge()
  let tx =
    Transaction {
      ..base_tx(challenge_timeout(test_params(), created_at) + 1),
      outputs: [stake_to_challenger(datum.stake_amount)],
    }
  !validate_expire_challenge(tx, test_config(), test_refs(), test_params(), datum)
}

// ──────────────────────────────────────────────────────────────────────────────
// MintChallengeToken: created_at is the time of the transaction
// ──────────────────────────────────────────────────────────────────────────────

/// Time of the challenge transactions below. The challenger registered at 0,
/// so it is older than min_agent_age (24 h).
const challenge_time: Int = 1_700_000_000_000

fn target_stake_ref() -> transaction.Input {
  let stake_datum =
    StakeDatum {
      agent_did: test_agent_did_2,
      owner_credential: VerificationKey(test_owner_key_hash_2),
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review", "testing"],
      last_updated: 500,
      history_points: 0,
    }
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
      stake_datum,
    ),
  )
}

/// A new challenge with the given created_at, in a transaction with the
/// given validity range.
fn mint_challenge_with(
  created_at: Int,
  validity_range: interval.Interval,
) -> Bool {
  let output_datum = ReputationChallengeDatum { ..base_challenge_datum(), created_at }
  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
        |> assets.add(
            test_endorsement_policy_id,
            derive_challenge_token_name(
              test_agent_did,
              test_agent_did_2,
              "code_review",
            ),
            1,
          ),
      Challenge(output_datum),
    )
  let tx =
    Transaction {
      ..base_tx(0),
      validity_range,
      reference_inputs: [
        make_registry_ref_input(test_agent_did, test_owner_key_hash),
        make_registry_ref_input(test_agent_did_2, test_owner_key_hash_2),
        target_stake_ref(),
      ],
      outputs: [output],
      extra_signatories: [test_owner_key_hash],
    }
  validate_mint_challenge(tx, test_config(), test_refs(), test_params(), output_datum, output)
}

test mint_challenge_created_at_lower_bound() {
  mint_challenge_with(
    challenge_time,
    interval.between(challenge_time, challenge_time + 600_000),
  )
}

/// The old rule did not check created_at. The response deadline counts from
/// created_at, so an earlier created_at would shorten or remove the time the
/// target has to respond.
test mint_challenge_with_earlier_created_at_fails() {
  !mint_challenge_with(
    challenge_time - 43_200_000,
    interval.between(challenge_time, challenge_time + 600_000),
  )
}

/// created_at is the lower bound of a validity range of at most one hour.
test mint_challenge_with_long_validity_range_fails() {
  !mint_challenge_with(
    challenge_time,
    interval.between(challenge_time, challenge_time + 3_600_001),
  )
}
