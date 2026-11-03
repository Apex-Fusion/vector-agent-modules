/// Integration tests for Module 1 <-> Module 3 cross-contract interactions.
///
/// Tests escalation to Module 1, resolution from Module 1 verdict,
/// and history bonus minting from Module 1 events.
///
/// R4 P-2: the escalation binds the challenge to the Module 1 claim token
/// that the escalation transaction mints (Escalated { audit_claim_token }).
/// The resolution reads the Module 1 challenge only from a reference input
/// at the exact Module 1 challenge address that holds exactly one Module 1
/// challenge token and names that claim token.
///
/// W4-MODFIX3: the minted claim is a claim of the challenge target on the
/// challenged capability (claimer_did == target_did and claim_type ==
/// challenged_capability). ResolveEscalation maps ClaimerWins to
/// CapabilityVerified and AuditorWins to CapabilityFalsified, so a claim of
/// another agent or on another capability must not decide the challenge.

use aiken/builtin
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use reputation_staking/challenge_validation
use reputation_staking/config.{ReputationConfig}
use reputation_staking/test_helpers.{
  base_tx, key_address, make_input, make_output, script_address,
  test_agent_did, test_agent_did_2, test_config, test_oracle_credential,
  test_owner_key_hash, test_owner_key_hash_2, test_params,
}
use reputation_staking/types.{
  AuditChallengeDatum, AuditClaimDatum, AuditInconclusive, AuditJurorDatum,
  AuditOpen, AuditResolved, AuditValidated, AuditorWins, CapabilityFalsified,
  CapabilityVerified, ClaimerWins, Escalated, Inconclusive, Open,
  ReputationChallengeDatum, Resolved,
}

// ──────────────────────────────────────────────────────────────────────────────
// Test helpers for Module 1 integration
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

/// The name of the Module 1 claim token that the escalation mints.
const claim_token: ByteArray = "claim_token_001"

/// The name of the Module 1 challenge token of the Module 1 challenge.
const module1_challenge_token: ByteArray = "challenge_token_001"

/// The escalated challenge, bound to `claim_token`.
fn escalated_datum() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    ..test_challenge_datum(),
    state: Escalated { audit_claim_token: claim_token },
  }
}

/// The Module 1 claim output with the datum `claim` that holds `token` (a
/// claim token of the claim validator).
fn make_module1_claim_output(
  cfg: ReputationConfig,
  claim: AuditClaimDatum,
  token: ByteArray,
) -> Output {
  let value =
    assets.from_lovelace(52_000_000)
      |> assets.add(cfg.audit_claim_validator_hash, token, 1)
  make_output(script_address(cfg.audit_claim_validator_hash), value, claim)
}

/// The mint of one Module 1 claim token.
fn claim_mint(cfg: ReputationConfig, token: ByteArray) -> assets.Value {
  assets.from_asset(cfg.audit_claim_validator_hash, token, 1)
}

fn make_module1_validated_claim_ref_input(
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

/// The resolved Module 1 challenge of the claim with the claim token
/// `claim`, with the verdict `verdict` (Module 1 layout:
/// Resolved { verdict, refund_to }).
fn module1_challenge_datum(
  claim: ByteArray,
  verdict: types.AuditVerdict,
) -> AuditChallengeDatum {
  AuditChallengeDatum {
    claim_ref: OutputReference {
      transaction_id: #"1111111111111111111111111111111111111111111111111111111111111111",
      output_index: 0,
    },
    auditor_did: test_agent_did_2,
    auditor_credential: VerificationKey(test_owner_key_hash_2),
    stake_amount: 25_000_000,
    evidence_hash: #"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    evidence_uri: "ipfs://audit-evidence",
    challenged_at: 5_000,
    resolution_deadline: 5_400_000,
    eligible_jurors: [],
    state: AuditResolved {
      verdict,
      refund_to: key_address(test_owner_key_hash_2),
    },
    claim_token: claim,
  }
}

/// A reference input at `address` that holds `value` and `datum`.
fn module1_challenge_input(address: Address, value: assets.Value, datum: Data) -> Input {
  make_input(
    #"3333333333333333333333333333333333333333333333333333333333333333",
    0,
    make_output(address, value, datum),
  )
}

/// 27 ADA and one Module 1 challenge token.
fn module1_challenge_value(cfg: ReputationConfig) -> assets.Value {
  assets.from_lovelace(27_000_000)
    |> assets.add(cfg.audit_challenge_validator_hash, module1_challenge_token, 1)
}

fn make_module1_resolved_challenge_ref_input(
  cfg: ReputationConfig,
  claim: ByteArray,
  verdict: types.AuditVerdict,
) -> Input {
  module1_challenge_input(
    script_address(cfg.audit_challenge_validator_hash),
    module1_challenge_value(cfg),
    module1_challenge_datum(claim, verdict),
  )
}

fn make_module1_juror_ref_input(
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

/// The Module 1 claim of the escalation of `challenge`: the target claims
/// the challenged capability (claim_type), and claim_hash links the claim
/// to the evidence of both parties.
fn escalation_claim(challenge: ReputationChallengeDatum) -> AuditClaimDatum {
  AuditClaimDatum {
    claimer_did: challenge.target_did,
    claimer_credential: challenge.target_credential,
    claim_hash: expected_claim_hash_for(challenge),
    claim_type: challenge.challenged_capability,
    storage_uri: "ipfs://claim",
    stake_amount: 50_000_000,
    submitted_at: 3_000,
    challenge_window: 1_800_000,
    state: AuditOpen,
  }
}

/// A claim in the shape that the rule before W4-MODFIX3 accepted: the
/// claim_type is the tag "reputation_challenge", not a capability.
fn tagged_claim(claimer_did: ByteArray, claimer_key: ByteArray) -> AuditClaimDatum {
  AuditClaimDatum {
    ..escalation_claim(test_challenge_datum()),
    claimer_did: claimer_did,
    claimer_credential: VerificationKey(claimer_key),
    claim_type: "reputation_challenge",
  }
}

/// EscalateToAudit of `input_datum` to `output_datum` at the validity range
/// [lo, lo + 100], signed by `signers`, with `outputs` and `mint`.
fn escalate(
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
  lo: Int,
  signers: List<ByteArray>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  let tx =
    Transaction {
      ..base_tx(lo),
      outputs,
      mint,
      extra_signatories: signers,
    }
  challenge_validation.validate_escalate_to_audit(
    tx,
    test_config(),
    test_params(),
    input_datum,
    output_datum,
  )
}

/// The escalation of the test challenge at `lo`, signed by `signers`, with
/// the claim output and the claim token mint.
fn escalate_at(lo: Int, signers: List<ByteArray>) -> Bool {
  let cfg = test_config()
  let input_datum = test_challenge_datum()
  escalate(
    input_datum,
    escalated_datum(),
    lo,
    signers,
    [make_module1_claim_output(cfg, escalation_claim(input_datum), claim_token)],
    claim_mint(cfg, claim_token),
  )
}

/// The escalation of the test challenge at 3_000, signed by the challenger,
/// whose minted claim token is in a claim output with the datum `claim`.
fn escalate_with_claim(claim: AuditClaimDatum) -> Bool {
  let cfg = test_config()
  escalate(
    test_challenge_datum(),
    escalated_datum(),
    3_000,
    [test_owner_key_hash_2],
    [make_module1_claim_output(cfg, claim, claim_token)],
    claim_mint(cfg, claim_token),
  )
}

test escalate_to_audit_happy_path() {
  escalate_at(3_000, [test_owner_key_hash_2])
}

test escalate_to_audit_by_target() {
  // Target signs instead of challenger
  escalate_at(3_000, [test_owner_key_hash])
}

test escalate_to_audit_no_response_fails() fail {
  let cfg = test_config()
  // Challenge with no response
  let input_datum =
    ReputationChallengeDatum {
      ..test_challenge_datum(),
      counter_evidence_hash: "",
      counter_evidence_uri: "",
      response_submitted_at: 0,
    }
  escalate(
    input_datum,
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_token: claim_token },
    },
    3_000,
    [test_owner_key_hash_2],
    [make_module1_claim_output(cfg, escalation_claim(input_datum), claim_token)],
    claim_mint(cfg, claim_token),
  )
}

test escalate_to_audit_past_window_fails() fail {
  // Validity range starts at the escalation deadline
  // deadline = created_at(1_000) + challenge_response_deadline(43_200_000)
  //   + escalation_window(21_600_000) = 64_801_000
  escalate_at(64_801_000, [test_owner_key_hash_2])
}

/// The old rule compared the lower bound of the validity range with the
/// escalation deadline. The new rule compares the upper bound, so a range
/// that starts before the deadline and ends after it does not pass.
test escalate_to_audit_with_upper_bound_after_window_fails() {
  // lower bound 64_800_950 <= deadline 64_801_000 < upper bound 64_801_050
  !escalate_at(64_800_950, [test_owner_key_hash_2])
}

test escalate_to_audit_unsigned_fails() fail {
  // No signatories
  escalate_at(3_000, [])
}

/// R4 P-2: the escalating party cannot choose the claim that the challenge
/// waits for. The Escalated state must name the claim token that the
/// transaction mints.
test escalate_to_audit_naming_another_claim_fails() {
  let cfg = test_config()
  let input_datum = test_challenge_datum()
  !escalate(
    input_datum,
    ReputationChallengeDatum {
      ..input_datum,
      state: Escalated { audit_claim_token: "claim_token_of_other_claim" },
    },
    3_000,
    [test_owner_key_hash_2],
    [make_module1_claim_output(cfg, escalation_claim(input_datum), claim_token)],
    claim_mint(cfg, claim_token),
  )
}

/// R4 P-2: a claim output without a claim token minted in the transaction
/// does not count.
test escalate_to_audit_without_claim_token_mint_fails() fail {
  let cfg = test_config()
  let input_datum = test_challenge_datum()
  escalate(
    input_datum,
    escalated_datum(),
    3_000,
    [test_owner_key_hash_2],
    [make_module1_claim_output(cfg, escalation_claim(input_datum), claim_token)],
    assets.zero,
  )
}

/// R4 P-2: the minted claim token must be in the linked claim output.
test escalate_to_audit_claim_output_without_minted_token_fails() {
  let cfg = test_config()
  let input_datum = test_challenge_datum()
  !escalate(
    input_datum,
    escalated_datum(),
    3_000,
    [test_owner_key_hash_2],
    [
      make_module1_claim_output(
        cfg,
        escalation_claim(input_datum),
        "claim_token_002",
      ),
    ],
    claim_mint(cfg, claim_token),
  )
}

// W4-MODFIX3: the claim is a claim of the target on the challenged
// capability. The three tests below use the claim shape that the rule
// before W4-MODFIX3 accepted (claim_type "reputation_challenge"): that rule
// read neither the claimer nor the capability, so it accepted them.

/// A claim of the challenger: ClaimerWins would mean that the challenger
/// was right, but ResolveEscalation maps it to CapabilityVerified.
test escalate_to_audit_with_claim_of_challenger_fails() {
  !escalate_with_claim(tagged_claim(test_agent_did_2, test_owner_key_hash_2))
}

/// A claim of an agent that is neither party: its verdict says nothing
/// about the target.
test escalate_to_audit_with_claim_of_other_agent_fails() {
  !escalate_with_claim(tagged_claim("agent_did_003", test_owner_key_hash_2))
}

/// A claim of the target that is not on the challenged capability.
test escalate_to_audit_with_claim_without_capability_fails() {
  !escalate_with_claim(tagged_claim(test_agent_did, test_owner_key_hash))
}

// Each test below changes one field of a valid claim.

/// The valid claim with the challenger as claimer.
test escalate_to_audit_with_capability_claim_of_challenger_fails() {
  !escalate_with_claim(
    AuditClaimDatum {
      ..escalation_claim(test_challenge_datum()),
      claimer_did: test_agent_did_2,
      claimer_credential: VerificationKey(test_owner_key_hash_2),
    },
  )
}

/// The valid claim on another capability that the target also stakes.
test escalate_to_audit_with_claim_on_other_capability_fails() {
  !escalate_with_claim(
    AuditClaimDatum {
      ..escalation_claim(test_challenge_datum()),
      claim_type: "testing",
    },
  )
}

/// The valid claim with a claim_hash that does not link to the evidence.
test escalate_to_audit_with_claim_of_other_evidence_fails() {
  !escalate_with_claim(
    AuditClaimDatum {
      ..escalation_claim(test_challenge_datum()),
      claim_hash: #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    },
  )
}

/// Module 3 binds the claimer by its DID only. The Module 1 claim rules
/// check the claimer credential: a declared key of the target's record
/// that signs. Here the credential is not target_credential.
test escalate_to_audit_with_other_claimer_key_of_target() {
  escalate_with_claim(
    AuditClaimDatum {
      ..escalation_claim(test_challenge_datum()),
      claimer_credential: VerificationKey(test_owner_key_hash_2),
    },
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// ResolveEscalation tests
// ──────────────────────────────────────────────────────────────────────────────

/// ResolveEscalation of the escalated test challenge to `outcome` with the
/// reference inputs `refs`.
fn resolve_escalation(
  outcome: types.RepChallengeOutcome,
  refs: List<Input>,
) -> Bool {
  let input_datum = escalated_datum()
  let output_datum =
    ReputationChallengeDatum { ..input_datum, state: Resolved { outcome } }
  let tx = Transaction { ..base_tx(10_000), reference_inputs: refs }
  challenge_validation.validate_resolve_escalation(
    tx,
    test_config(),
    input_datum,
    output_datum,
  )
}

test resolve_escalation_claimer_wins() {
  resolve_escalation(
    CapabilityVerified,
    [
      make_module1_resolved_challenge_ref_input(
        test_config(),
        claim_token,
        ClaimerWins,
      ),
    ],
  )
}

test resolve_escalation_auditor_wins() {
  resolve_escalation(
    CapabilityFalsified,
    [
      make_module1_resolved_challenge_ref_input(
        test_config(),
        claim_token,
        AuditorWins,
      ),
    ],
  )
}

test resolve_escalation_inconclusive() {
  resolve_escalation(
    Inconclusive,
    [
      make_module1_resolved_challenge_ref_input(
        test_config(),
        claim_token,
        AuditInconclusive,
      ),
    ],
  )
}

test resolve_escalation_wrong_verdict_mapping_fails() fail {
  // ClaimerWins should map to CapabilityVerified, not CapabilityFalsified
  resolve_escalation(
    CapabilityFalsified,
    [
      make_module1_resolved_challenge_ref_input(
        test_config(),
        claim_token,
        ClaimerWins,
      ),
    ],
  )
}

test resolve_escalation_wrong_claim_token_fails() fail {
  // Module 1 challenge references a DIFFERENT claim
  resolve_escalation(
    CapabilityVerified,
    [
      make_module1_resolved_challenge_ref_input(
        test_config(),
        "claim_token_of_other_claim",
        ClaimerWins,
      ),
    ],
  )
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

/// R4 P-2: a plain output at the Module 1 challenge address, without a
/// Module 1 challenge token, with a datum that the challenger wrote.
test resolve_escalation_with_forged_module1_challenge_fails() fail {
  let cfg = test_config()
  resolve_escalation(
    CapabilityFalsified,
    [
      module1_challenge_input(
        script_address(cfg.audit_challenge_validator_hash),
        assets.from_lovelace(2_000_000),
        module1_challenge_datum(claim_token, AuditorWins),
      ),
    ],
  )
}

/// R4 P-2: exactly one Module 1 challenge token.
test resolve_escalation_with_two_module1_tokens_fails() fail {
  let cfg = test_config()
  resolve_escalation(
    CapabilityFalsified,
    [
      module1_challenge_input(
        script_address(cfg.audit_challenge_validator_hash),
        module1_challenge_value(cfg)
          |> assets.add(cfg.audit_challenge_validator_hash, "another_token", 1),
        module1_challenge_datum(claim_token, AuditorWins),
      ),
    ],
  )
}

/// R4 P-2: the exact Module 1 challenge address (no stake credential).
test resolve_escalation_module1_challenge_with_stake_credential_fails() fail {
  let cfg = test_config()
  resolve_escalation(
    CapabilityFalsified,
    [
      module1_challenge_input(
        Address {
          payment_credential: Script(cfg.audit_challenge_validator_hash),
          stake_credential: Some(
            address.Inline(VerificationKey(test_owner_key_hash_2)),
          ),
        },
        module1_challenge_value(cfg),
        module1_challenge_datum(claim_token, AuditorWins),
      ),
    ],
  )
}

/// The Module-1 ChallengeDatum of the previous Module-1 deployment: the
/// current fields without `claim_token`.
type PreviousAuditChallengeDatum {
  claim_ref: OutputReference,
  auditor_did: ByteArray,
  auditor_credential: Credential,
  stake_amount: Int,
  evidence_hash: ByteArray,
  evidence_uri: ByteArray,
  challenged_at: Int,
  resolution_deadline: Int,
  eligible_jurors: List<ByteArray>,
  state: types.AuditChallengeState,
}

/// A challenge of the previous Module-1 layout does not decode as the mirror
/// of the Module-1 deployment that Module-3 uses.
test resolve_escalation_previous_module1_layout_fails() fail {
  let cfg = test_config()
  let current = module1_challenge_datum(claim_token, ClaimerWins)
  let previous_datum =
    PreviousAuditChallengeDatum {
      claim_ref: current.claim_ref,
      auditor_did: current.auditor_did,
      auditor_credential: current.auditor_credential,
      stake_amount: current.stake_amount,
      evidence_hash: current.evidence_hash,
      evidence_uri: current.evidence_uri,
      challenged_at: current.challenged_at,
      resolution_deadline: current.resolution_deadline,
      eligible_jurors: [],
      state: current.state,
    }
  resolve_escalation(
    CapabilityVerified,
    [
      module1_challenge_input(
        script_address(cfg.audit_challenge_validator_hash),
        module1_challenge_value(cfg),
        previous_datum,
      ),
    ],
  )
}

/// The Module-1 ChallengeDatum with a raw state, to write the state layout
/// before W4.
type RawStateAuditChallengeDatum {
  claim_ref: OutputReference,
  auditor_did: ByteArray,
  auditor_credential: Credential,
  stake_amount: Int,
  evidence_hash: ByteArray,
  evidence_uri: ByteArray,
  challenged_at: Int,
  resolution_deadline: Int,
  eligible_jurors: List<ByteArray>,
  state: Data,
  claim_token: ByteArray,
}

/// W4 item 8: a Module 1 challenge whose Resolved state has no refund_to
/// (Resolved { verdict }, constructor 3 with one field: the layout before
/// W4) does not decode.
test resolve_escalation_resolved_without_refund_to_fails() fail {
  let cfg = test_config()
  let current = module1_challenge_datum(claim_token, ClaimerWins)
  let verdict: Data = ClaimerWins
  let datum =
    RawStateAuditChallengeDatum {
      claim_ref: current.claim_ref,
      auditor_did: current.auditor_did,
      auditor_credential: current.auditor_credential,
      stake_amount: current.stake_amount,
      evidence_hash: current.evidence_hash,
      evidence_uri: current.evidence_uri,
      challenged_at: current.challenged_at,
      resolution_deadline: current.resolution_deadline,
      eligible_jurors: [],
      state: builtin.constr_data(3, [verdict]),
      claim_token: current.claim_token,
    }
  resolve_escalation(
    CapabilityVerified,
    [
      module1_challenge_input(
        script_address(cfg.audit_challenge_validator_hash),
        module1_challenge_value(cfg),
        datum,
      ),
    ],
  )
}

/// W4 item 8: the Module 1 layout Resolved { verdict, refund_to } decodes,
/// and Module 3 reads the verdict.
test resolve_escalation_reads_verdict_next_to_refund_to() {
  resolve_escalation(
    CapabilityFalsified,
    [
      make_module1_resolved_challenge_ref_input(
        test_config(),
        claim_token,
        AuditorWins,
      ),
    ],
  )
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
  let claim_ref = make_module1_validated_claim_ref_input(cfg, test_agent_did)

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
  let claim_ref = make_module1_validated_claim_ref_input(cfg, test_agent_did)

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
  let claim_ref = make_module1_validated_claim_ref_input(cfg, test_agent_did)

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
  let juror_ref = make_module1_juror_ref_input(cfg, test_agent_did, 3, 2)

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
  let juror_ref = make_module1_juror_ref_input(cfg, test_agent_did, 0, 0)

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
  let juror_ref = make_module1_juror_ref_input(cfg, test_agent_did, 3, 2)

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
  let juror_ref = make_module1_juror_ref_input(cfg, test_agent_did, 3, 2)

  // No oracle signature
  let tx =
    Transaction {
      ..base_tx(10_000),
      reference_inputs: [juror_ref],
      extra_signatories: [],
    }

  challenge_validation.validate_juror_duty_bonus(tx, cfg, test_agent_did)
}
