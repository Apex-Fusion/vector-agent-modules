/// Tests for challenge validation logic.

use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Transaction}
use reputation_staking/challenge_validation.{
  validate_default_judgment, validate_respond_to_challenge,
  validate_resolve_challenge,
}
use reputation_staking/test_helpers.{
  base_tx, key_address, make_output,
  test_agent_did, test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id,
  test_config, test_oracle_credential, test_owner_key_hash,
  test_owner_key_hash_2, test_params,
}
use reputation_staking/types.{
  CapabilityFalsified, CapabilityVerified, Inconclusive, Open, Resolved,
  ReputationChallengeDatum,
}

// ──────────────────────────────────────────────────────────────────────────────
// Helper: make a base challenge datum
// ──────────────────────────────────────────────────────────────────────────────

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
    created_at: 1000,
    counter_evidence_hash: "",
    counter_evidence_uri: "",
    response_submitted_at: 0,
    state: Open,
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// RespondToChallenge tests
// ──────────────────────────────────────────────────────────────────────────────

test respond_to_challenge_happy_path() {
  let params = test_params()
  let current_slot = 1000 + 5000

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
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            extra_signatories: [test_owner_key_hash_2],
          }
        }

  validate_respond_to_challenge(
    tx, params, input_datum, output_datum,
    counter_hash, counter_uri, current_slot,
  )
}

test respond_to_challenge_past_deadline_fails() fail {
  let params = test_params()
  // Past response deadline (created_at=1000, deadline=10_800)
  let current_slot = 1000 + 10_800 + 1

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
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            extra_signatories: [test_owner_key_hash_2],
          }
        }

  validate_respond_to_challenge(
    tx, params, input_datum, output_datum,
    counter_hash, counter_uri, current_slot,
  )
}

test respond_to_challenge_bad_hash_fails() fail {
  let params = test_params()
  let current_slot = 1000 + 5000

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
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            extra_signatories: [test_owner_key_hash_2],
          }
        }

  validate_respond_to_challenge(
    tx, params, input_datum, output_datum,
    counter_hash, counter_uri, current_slot,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// DefaultJudgment tests
// ──────────────────────────────────────────────────────────────────────────────

test default_judgment_happy_path() {
  let cfg = test_config()
  let params = test_params()
  // Past response deadline, no response
  let current_slot = 1000 + 10_800 + 1

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
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            outputs: [fee_output],
            extra_signatories: [submitter_key],
          }
        }

  validate_default_judgment(tx, cfg, input_datum, output_datum, params, current_slot)
}

test default_judgment_before_deadline_fails() fail {
  let cfg = test_config()
  let params = test_params()
  // Before deadline
  let current_slot = 1000 + 5000

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
  let current_slot = 1000 + 10_800 + 1

  // Target already responded
  let input_datum =
    ReputationChallengeDatum {
      ..base_challenge_datum(),
      response_submitted_at: 5000,
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
    base_tx(5000)
      |> fn(t) {
          Transaction {
            ..t,
            extra_signatories: [test_oracle_credential],
          }
        }

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
    base_tx(5000)
      |> fn(t) {
          Transaction {
            ..t,
            extra_signatories: [test_oracle_credential],
          }
        }

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
    base_tx(5000)
      |> fn(t) {
          Transaction {
            ..t,
            extra_signatories: [test_oracle_credential],
          }
        }

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
  let tx = base_tx(5000)

  validate_resolve_challenge(tx, cfg, input_datum, output_datum, CapabilityVerified)
}
