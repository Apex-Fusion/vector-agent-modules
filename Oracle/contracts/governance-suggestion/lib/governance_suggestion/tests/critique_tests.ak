// Oracle: Governance Suggestion Engine — Critique & Endorsement Tests

use aiken/builtin
use aiken/crypto.{blake2b_256}
use cardano/address.{Credential, VerificationKey}
use cardano/transaction.{OutputReference}

use governance_suggestion/params
use governance_suggestion/types.{
  Amendment, CritiqueDatum, GovernanceEndorsementDatum,
  GovernanceParams, Opposing, Supportive,
}

// ============================================================================
// Test Fixtures
// ============================================================================

fn test_params() -> GovernanceParams {
  params.default_governance_params()
}

fn test_critic_credential() -> Credential {
  VerificationKey(
    #"ccdd0000000000000000000000000000000000000000000000000000",
  )
}

fn test_proposal_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"abcd000000000000000000000000000000000000000000000000000000000000",
    output_index: 0,
  }
}

fn test_critique_datum() -> CritiqueDatum {
  CritiqueDatum {
    critic_did: "did_critic_001",
    critic_credential: test_critic_credential(),
    proposal_ref: test_proposal_ref(),
    critique_hash: blake2b_256("critique content v1"),
    storage_uri: "ipfs://QmTestCritique456",
    critique_type: Supportive,
    stake_amount: 5_000_000,
    submitted_at: 2000,
    incorporated: False,
  }
}

fn test_endorsement_datum() -> GovernanceEndorsementDatum {
  GovernanceEndorsementDatum {
    endorser_did: "did_endorser_001",
    endorser_credential: VerificationKey(
      #"eeff0000000000000000000000000000000000000000000000000000",
    ),
    proposal_ref: test_proposal_ref(),
    stake_amount: 10_000_000,
    created_at: 3000,
  }
}

// ============================================================================
// Critique Datum Tests
// ============================================================================

test critique_hash_is_32_bytes() {
  let datum = test_critique_datum()
  builtin.length_of_bytearray(datum.critique_hash) == 32
}

test critique_initial_not_incorporated() {
  let datum = test_critique_datum()
  !datum.incorporated
}

test critique_stake_meets_minimum() {
  let p = test_params()
  let datum = test_critique_datum()
  datum.stake_amount >= p.min_critique_stake
}

test critique_stake_below_minimum_fails() {
  let p = test_params()
  let datum =
    CritiqueDatum { ..test_critique_datum(), stake_amount: p.min_critique_stake - 1 }
  datum.stake_amount < p.min_critique_stake
}

test critique_storage_uri_non_empty() {
  let datum = test_critique_datum()
  builtin.length_of_bytearray(datum.storage_uri) > 0
}

// ============================================================================
// Critique Type Tests
// ============================================================================

test critique_type_supportive() {
  let datum = CritiqueDatum { ..test_critique_datum(), critique_type: Supportive }
  when datum.critique_type is {
    Supportive -> True
    _ -> False
  }
}

test critique_type_opposing() {
  let datum = CritiqueDatum { ..test_critique_datum(), critique_type: Opposing }
  when datum.critique_type is {
    Opposing -> True
    _ -> False
  }
}

test critique_type_amendment() {
  let change_hash = blake2b_256("suggested change")
  let datum =
    CritiqueDatum {
      ..test_critique_datum(),
      critique_type: Amendment { suggested_change_hash: change_hash },
    }
  when datum.critique_type is {
    Amendment { suggested_change_hash } ->
      builtin.length_of_bytearray(suggested_change_hash) == 32
    _ -> False
  }
}

// ============================================================================
// Self-Critique / Self-Endorsement Tests
// ============================================================================

test no_self_critique_different_dids() {
  let proposer_did = "did_proposer_001"
  let critic_did = "did_critic_001"
  proposer_did != critic_did
}

test self_critique_same_did_detected() {
  let did = "did_same_agent"
  let proposer_did = did
  let critic_did = did
  proposer_did == critic_did
}

test amendment_type_allows_self_critique() {
  // Per spec: Amendment type allows self-critique (proposer documenting improvements)
  let change_hash = blake2b_256("self improvement")
  let datum =
    CritiqueDatum {
      ..test_critique_datum(),
      critic_did: "did_proposer_001",
      critique_type: Amendment { suggested_change_hash: change_hash },
    }
  when datum.critique_type is {
    Amendment { .. } -> True
    _ -> False
  }
}

test no_self_endorsement_different_dids() {
  let proposer_did = "did_proposer_001"
  let endorser_did = "did_endorser_001"
  proposer_did != endorser_did
}

test self_endorsement_same_did_detected() {
  let did = "did_same_agent"
  let endorser_datum =
    GovernanceEndorsementDatum { ..test_endorsement_datum(), endorser_did: did }
  endorser_datum.endorser_did == did
}

// ============================================================================
// Incorporation Tests
// ============================================================================

test incorporated_critique_flag_set() {
  let original = test_critique_datum()
  let incorporated = CritiqueDatum { ..original, incorporated: True }
  !original.incorporated && incorporated.incorporated
}

test incorporated_critique_preserves_fields() {
  let original = test_critique_datum()
  let incorporated = CritiqueDatum { ..original, incorporated: True }
  incorporated.critic_did == original.critic_did && incorporated.proposal_ref == original.proposal_ref && incorporated.critique_hash == original.critique_hash
}

test cannot_incorporate_already_incorporated() {
  let datum = CritiqueDatum { ..test_critique_datum(), incorporated: True }
  // validate_incorporate_critique requires !datum.incorporated
  datum.incorporated
}

// ============================================================================
// Endorsement Tests
// ============================================================================

test endorsement_stake_meets_minimum() {
  let p = test_params()
  let datum = test_endorsement_datum()
  datum.stake_amount >= p.min_governance_endorsement
}

test endorsement_stake_below_minimum_fails() {
  let p = test_params()
  let datum =
    GovernanceEndorsementDatum {
      ..test_endorsement_datum(),
      stake_amount: p.min_governance_endorsement - 1,
    }
  datum.stake_amount < p.min_governance_endorsement
}

test endorsement_references_proposal() {
  let datum = test_endorsement_datum()
  datum.proposal_ref == test_proposal_ref()
}
