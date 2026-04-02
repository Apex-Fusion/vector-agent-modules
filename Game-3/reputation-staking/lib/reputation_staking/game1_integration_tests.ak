/// Integration tests for Game 1 <-> Game 3 cross-contract interactions.
///
/// Tests escalation to Game 1, resolution from Game 1 verdict,
/// and history bonus minting from Game 1 events.

use aiken/builtin
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use reputation_staking/challenge_validation
use reputation_staking/config.{ReputationConfig}
use reputation_staking/test_helpers.{
  base_tx, make_input, make_output,
  script_address, test_agent_did, test_agent_did_2,
  test_config, test_oracle_credential,
  test_owner_key_hash, test_owner_key_hash_2, test_params,
}
use reputation_staking/types.{
  AuditChallengeDatum, AuditClaimDatum, AuditInconclusive, AuditJurorDatum,
  AuditOpen, AuditResolved, AuditValidated, AuditorWins, CapabilityFalsified,
  CapabilityVerified, ClaimerWins, Escalated, Inconclusive, Open,
  ReputationChallengeDatum, Resolved,
}

// ──────────────────────────────────────────────────────────────────────────────
// Test helpers for Game 1 integration
// ──────────────────────────────────────────────────────────────────────────────

fn test_challenge_datum() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did_2,
    challenger_credential: VerificationKey(test_owner_key_hash_2),
    target_did: test_agent_did,
    target_credential: VerificationKey(test_owner_key_hash),
    challenged_capability: "code_review",
    stake_amount: 25_000_000,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at: 1_000,
    counter_evidence_hash: #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    counter_evidence_uri: "ipfs://counter-evidence",
    response_submitted_at: 2_000,
    state: Open,
  }
}

fn test_audit_claim_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"1111111111111111111111111111111111111111111111111111111111111111",
    output_index: 0,
  }
}

fn make_game1_claim_output(
  cfg: ReputationConfig,
  expected_claim_hash: ByteArray,
) -> Output {
  let claim_datum =
    AuditClaimDatum {
      claimer_did: test_agent_did,
      claimer_credential: VerificationKey(test_owner_key_hash),
      claim_hash: expected_claim_hash,
      claim_type: "reputation_challenge",
      storage_uri: "ipfs://claim",
      stake_amount: 50_000_000,
      submitted_at: 3_000,
      challenge_window: 1_800_000,
      state: AuditOpen,
    }
  let value = assets.from_lovelace(52_000_000)
  make_output(script_address(cfg.audit_claim_validator_hash), value, claim_datum)
}

fn make_game1_validated_claim_ref_input(
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> Input {
  let claim_datum =
    AuditClaimDatum {
      claimer_did: agent_did,
      claimer_credential: VerificationKey(test_owner_key_hash),
      claim_hash: #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      claim_type: "capability_claim",
      storage_uri: "ipfs://claim",
      stake_amount: 50_000_000,
      submitted_at: 1_000,
      challenge_window: 1_800_000,
      state: AuditValidated,
    }
  let value = assets.from_lovelace(52_000_000)
  let output =
    make_output(script_address(cfg.audit_claim_validator_hash), value, claim_datum)
  make_input(
    #"2222222222222222222222222222222222222222222222222222222222222222",
    0,
    output,
  )
}

fn make_game1_resolved_challenge_ref_input(
  cfg: ReputationConfig,
  claim_ref: OutputReference,
  verdict: types.AuditVerdict,
) -> Input {
  let challenge_datum =
    AuditChallengeDatum {
      claim_ref,
      auditor_did: test_agent_did_2,
      auditor_credential: VerificationKey(test_owner_key_hash_2),
      stake_amount: 25_000_000,
      evidence_hash: #"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      evidence_uri: "ipfs://audit-evidence",
      challenged_at: 5_000,
      resolution_deadline: 5_400_000,
      eligible_jurors: [],
      state: AuditResolved { verdict },
    }
  let value = assets.from_lovelace(27_000_000)
  let output =
    make_output(
      script_address(cfg.audit_challenge_validator_hash),
      value,
      challenge_datum,
    )
  make_input(
    #"3333333333333333333333333333333333333333333333333333333333333333",
    0,
    output,
  )
}

fn make_game1_juror_ref_input(
  cfg: ReputationConfig,
  agent_did: ByteArray,
  cases_resolved: Int,
  majority_votes: Int,
) -> Input {
  let juror_datum =
    AuditJurorDatum {
      juror_did: agent_did,
      juror_credential: VerificationKey(test_owner_key_hash),
      bond_amount: 25_000_000,
      cases_resolved,
      majority_votes,
      registered_at: 500,
      active_case: None,
      vote_commitment: None,
      revealed_verdict: None,
    }
  let value = assets.from_lovelace(27_000_000)
  let output =
    make_output(
      script_address(cfg.audit_jury_pool_hash),
      value,
      juror_datum,
    )
  make_input(
    #"4444444444444444444444444444444444444444444444444444444444444444",
    0,
    output,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// EscalateToAudit tests
// ──────────────────────────────────────────────────────────────────────────────

fn expected_claim_hash_for(datum: ReputationChallengeDatum) -> ByteArray {
  builtin.blake2b_256(
    builtin.append_bytearray(datum.evidence_hash, datum.counter_evidence_hash),
  )
}

test escalate_to_audit_happy_path() {
  let cfg = test_config()
  let params = test_params()
  let input_datum = test_challenge_datum()
  let audit_claim_ref = test_audit_claim_ref()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_ref },
    }

  let claim_hash = expected_claim_hash_for(input_datum)
  let claim_output = make_game1_claim_output(cfg, claim_hash)

  let tx =
    Transaction {
      ..base_tx(3_000),
      outputs: [claim_output],
      extra_signatories: [test_owner_key_hash_2],
    }

  challenge_validation.validate_escalate_to_audit(
    tx,
    cfg,
    params,
    input_datum,
    output_datum,
    audit_claim_ref,
    3_000,
  )
}

test escalate_to_audit_by_target() {
  let cfg = test_config()
  let params = test_params()
  let input_datum = test_challenge_datum()
  let audit_claim_ref = test_audit_claim_ref()

  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_ref },
    }

  let claim_hash = expected_claim_hash_for(input_datum)
  let claim_output = make_game1_claim_output(cfg, claim_hash)

  // Target signs instead of challenger
  let tx =
    Transaction {
      ..base_tx(3_000),
      outputs: [claim_output],
      extra_signatories: [test_owner_key_hash],
    }

  challenge_validation.validate_escalate_to_audit(
    tx,
    cfg,
    params,
    input_datum,
    output_datum,
    audit_claim_ref,
    3_000,
  )
}

test escalate_to_audit_no_response_fails() fail {
  let cfg = test_config()
  let params = test_params()
  // Challenge with no response
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      counter_evidence_hash: "",
      counter_evidence_uri: "",
      response_submitted_at: 0,
    }
  let audit_claim_ref = test_audit_claim_ref()
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_ref },
    }

  let claim_hash = expected_claim_hash_for(input_datum)
  let claim_output = make_game1_claim_output(cfg, claim_hash)
  let tx =
    Transaction {
      ..base_tx(3_000),
      outputs: [claim_output],
      extra_signatories: [test_owner_key_hash_2],
    }

  challenge_validation.validate_escalate_to_audit(
    tx, cfg, params, input_datum, output_datum, audit_claim_ref, 3_000,
  )
}

test escalate_to_audit_past_window_fails() fail {
  let cfg = test_config()
  let params = test_params()
  let input_datum = test_challenge_datum()
  let audit_claim_ref = test_audit_claim_ref()
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_ref },
    }

  let claim_hash = expected_claim_hash_for(input_datum)
  let claim_output = make_game1_claim_output(cfg, claim_hash)
  // current_slot far past escalation deadline
  // deadline = created_at(1000) + challenge_response_deadline(10800) + escalation_window(5400) = 17200
  let tx =
    Transaction {
      ..base_tx(20_000),
      outputs: [claim_output],
      extra_signatories: [test_owner_key_hash_2],
    }

  challenge_validation.validate_escalate_to_audit(
    tx, cfg, params, input_datum, output_datum, audit_claim_ref, 20_000,
  )
}

test escalate_to_audit_unsigned_fails() fail {
  let cfg = test_config()
  let params = test_params()
  let input_datum = test_challenge_datum()
  let audit_claim_ref = test_audit_claim_ref()
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_ref },
    }

  let claim_hash = expected_claim_hash_for(input_datum)
  let claim_output = make_game1_claim_output(cfg, claim_hash)
  // No signatories
  let tx =
    Transaction {
      ..base_tx(3_000),
      outputs: [claim_output],
      extra_signatories: [],
    }

  challenge_validation.validate_escalate_to_audit(
    tx, cfg, params, input_datum, output_datum, audit_claim_ref, 3_000,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// ResolveEscalation tests
// ──────────────────────────────────────────────────────────────────────────────

test resolve_escalation_claimer_wins() {
  let cfg = test_config()
  let audit_claim_ref = test_audit_claim_ref()
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      state: Escalated { audit_claim_ref },
    }
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityVerified },
    }

  let g1_challenge_ref =
    make_game1_resolved_challenge_ref_input(cfg, audit_claim_ref, ClaimerWins)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [g1_challenge_ref],
    }

  challenge_validation.validate_resolve_escalation(tx, cfg, input_datum, output_datum)
}

test resolve_escalation_auditor_wins() {
  let cfg = test_config()
  let audit_claim_ref = test_audit_claim_ref()
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      state: Escalated { audit_claim_ref },
    }
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityFalsified },
    }

  let g1_challenge_ref =
    make_game1_resolved_challenge_ref_input(cfg, audit_claim_ref, AuditorWins)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [g1_challenge_ref],
    }

  challenge_validation.validate_resolve_escalation(tx, cfg, input_datum, output_datum)
}

test resolve_escalation_inconclusive() {
  let cfg = test_config()
  let audit_claim_ref = test_audit_claim_ref()
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      state: Escalated { audit_claim_ref },
    }
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: Inconclusive },
    }

  let g1_challenge_ref =
    make_game1_resolved_challenge_ref_input(
      cfg,
      audit_claim_ref,
      AuditInconclusive,
    )

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [g1_challenge_ref],
    }

  challenge_validation.validate_resolve_escalation(tx, cfg, input_datum, output_datum)
}

test resolve_escalation_wrong_verdict_mapping_fails() fail {
  let cfg = test_config()
  let audit_claim_ref = test_audit_claim_ref()
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      state: Escalated { audit_claim_ref },
    }
  // ClaimerWins should map to CapabilityVerified, not CapabilityFalsified
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityFalsified },
    }

  let g1_challenge_ref =
    make_game1_resolved_challenge_ref_input(cfg, audit_claim_ref, ClaimerWins)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [g1_challenge_ref],
    }

  challenge_validation.validate_resolve_escalation(tx, cfg, input_datum, output_datum)
}

test resolve_escalation_wrong_claim_ref_fails() fail {
  let cfg = test_config()
  let audit_claim_ref = test_audit_claim_ref()
  let wrong_claim_ref =
    OutputReference {
      transaction_id: #"9999999999999999999999999999999999999999999999999999999999999999",
      output_index: 5,
    }
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      state: Escalated { audit_claim_ref },
    }
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityVerified },
    }

  // Game 1 challenge references a DIFFERENT claim
  let g1_challenge_ref =
    make_game1_resolved_challenge_ref_input(cfg, wrong_claim_ref, ClaimerWins)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [g1_challenge_ref],
    }

  challenge_validation.validate_resolve_escalation(tx, cfg, input_datum, output_datum)
}

test resolve_escalation_not_escalated_fails() fail {
  let cfg = test_config()
  // State is Open, not Escalated
  let input_datum = test_challenge_datum()
  let output_datum =
    ReputationChallengeDatum {
      ..input_datum,
      state: Resolved { outcome: CapabilityVerified },
    }

  let tx =
    Transaction { ..base_tx(10_000), reference_inputs: [] }

  challenge_validation.validate_resolve_escalation(tx, cfg, input_datum, output_datum)
}

// ──────────────────────────────────────────────────────────────────────────────
// Verdict mapping tests
// ──────────────────────────────────────────────────────────────────────────────

test map_verdict_claimer_wins() {
  challenge_validation.map_audit_verdict(ClaimerWins) == CapabilityVerified
}

test map_verdict_auditor_wins() {
  challenge_validation.map_audit_verdict(AuditorWins) == CapabilityFalsified
}

test map_verdict_inconclusive() {
  challenge_validation.map_audit_verdict(AuditInconclusive) == Inconclusive
}

// ──────────────────────────────────────────────────────────────────────────────
// History bonus — AuditClaimWon tests
// ──────────────────────────────────────────────────────────────────────────────

test audit_claim_won_bonus_happy_path() {
  let cfg = test_config()
  let claim_ref = make_game1_validated_claim_ref_input(cfg, test_agent_did)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [claim_ref],
      extra_signatories: [test_oracle_credential],
    }

  challenge_validation.validate_audit_claim_won_bonus(tx, cfg, test_agent_did)
}

test audit_claim_won_bonus_wrong_agent_fails() fail {
  let cfg = test_config()
  // Claim is for agent_did, but we're requesting bonus for agent_did_2
  let claim_ref = make_game1_validated_claim_ref_input(cfg, test_agent_did)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [claim_ref],
      extra_signatories: [test_oracle_credential],
    }

  challenge_validation.validate_audit_claim_won_bonus(tx, cfg, test_agent_did_2)
}

test audit_claim_won_bonus_no_oracle_fails() fail {
  let cfg = test_config()
  let claim_ref = make_game1_validated_claim_ref_input(cfg, test_agent_did)

  // No oracle signature
  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [claim_ref],
      extra_signatories: [],
    }

  challenge_validation.validate_audit_claim_won_bonus(tx, cfg, test_agent_did)
}

// ──────────────────────────────────────────────────────────────────────────────
// History bonus — JurorDuty tests
// ──────────────────────────────────────────────────────────────────────────────

test juror_duty_bonus_happy_path() {
  let cfg = test_config()
  let juror_ref = make_game1_juror_ref_input(cfg, test_agent_did, 3, 2)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [juror_ref],
      extra_signatories: [test_oracle_credential],
    }

  challenge_validation.validate_juror_duty_bonus(tx, cfg, test_agent_did)
}

test juror_duty_bonus_no_cases_fails() fail {
  let cfg = test_config()
  // Juror has 0 cases resolved
  let juror_ref = make_game1_juror_ref_input(cfg, test_agent_did, 0, 0)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [juror_ref],
      extra_signatories: [test_oracle_credential],
    }

  challenge_validation.validate_juror_duty_bonus(tx, cfg, test_agent_did)
}

test juror_duty_bonus_wrong_agent_fails() fail {
  let cfg = test_config()
  // Juror is agent_did, bonus requested for agent_did_2
  let juror_ref = make_game1_juror_ref_input(cfg, test_agent_did, 3, 2)

  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [juror_ref],
      extra_signatories: [test_oracle_credential],
    }

  challenge_validation.validate_juror_duty_bonus(tx, cfg, test_agent_did_2)
}

test juror_duty_bonus_no_oracle_fails() fail {
  let cfg = test_config()
  let juror_ref = make_game1_juror_ref_input(cfg, test_agent_did, 3, 2)

  // No oracle signature
  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [juror_ref],
      extra_signatories: [],
    }

  challenge_validation.validate_juror_duty_bonus(tx, cfg, test_agent_did)
}
