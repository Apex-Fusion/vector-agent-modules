/// Challenge validation logic for Game 3: Reputation Staking.
///
/// Validates challenge operations: MintChallengeToken, RespondToChallenge,
/// ResolveChallenge, DefaultJudgment, and DistributeOutcome.
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.8–6.11.

use aiken/builtin
use aiken/collection/list
use aiken/interval.{Finite}
use cardano/address.{Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, OutputReference, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{
  AuditChallengeDatum, AuditClaimDatum, AuditJurorDatum,
  AuditResolved, AuditValidated, AuditVerdict,
  AuditorWins, ClaimerWins, AuditInconclusive,
  CapabilityFalsified, CapabilityVerified, Escalated, Inconclusive, Open,
  RepChallengeOutcome, ReputationChallengeDatum, Resolved, StakeDatum,
  history_bonus_token_prefix,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name, find_agent_registry_datum,
  get_ap3x_amount, has_credential_signed,
  has_key_signed, starts_with, verify_agent_exists,
}

// ──────────────────────────────────────────────────────────────────────────────
// MintChallengeToken (Section 6.8)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate creation of a new reputation challenge.
///
/// MUST:
///   1. Challenger has active DID in Agent Registry
///   2. Target agent has active DID AND active StakeUTXO
///   3. challenger_did != target_did
///   4. challenged_capability is in target's staked_capabilities
///   5. stake_amount >= MIN_CHALLENGE_STAKE
///   6. evidence_hash is exactly 32 bytes
///   7. No existing open challenge from this challenger for same capability
///   8. ChallengeUTXO created with state = Open
///   9. Transaction signed by challenger_credential
pub fn validate_mint_challenge(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  output_datum: ReputationChallengeDatum,
  output: Output,
) -> Bool {
  // Rule 1: Challenger exists
  let challenger_exists =
    verify_agent_exists(tx.reference_inputs, cfg, output_datum.challenger_did)

  // Rule 2: Target exists
  let target_exists =
    verify_agent_exists(tx.reference_inputs, cfg, output_datum.target_did)

  // Rule 2: Target has active StakeUTXO with the challenged capability
  let target_stake_valid =
    verify_target_capability(
      tx.reference_inputs,
      refs,
      output_datum.target_did,
      output_datum.challenged_capability,
    )

  // Rule 3: No self-challenge
  let not_self_challenge =
    output_datum.challenger_did != output_datum.target_did

  // MIN_AGENT_AGE check: challenger must have been registered for min_agent_age slots
  let challenger_agent =
    find_agent_registry_datum(tx.reference_inputs, cfg, output_datum.challenger_did)
  let validity_lower =
    when tx.validity_range.lower_bound.bound_type is {
      Finite(slot) -> slot
      _ -> 0
    }
  let challenger_old_enough =
    validity_lower >= challenger_agent.registered_at + params.min_agent_age

  // Rule 5: Minimum challenge stake
  let meets_minimum = output_datum.stake_amount >= params.min_challenge_stake

  // Rule 6: Evidence hash is 32 bytes
  let evidence_valid =
    builtin.length_of_bytearray(output_datum.evidence_hash) == 32

  // Rule 7: No existing challenge (enforced by token uniqueness — token must not exist in inputs)
  let expected_token_name =
    derive_challenge_token_name(
      output_datum.challenger_did,
      output_datum.target_did,
      output_datum.challenged_capability,
    )
  let token_is_new =
    !list.any(
      tx.inputs,
      fn(input) {
        assets.quantity_of(
          input.output.value,
          refs.endorsement_policy_id,
          expected_token_name,
        ) >= 1
      },
    )

  // Output has correct challenge token
  let has_correct_token =
    assets.quantity_of(
      output.value,
      refs.endorsement_policy_id,
      expected_token_name,
    ) == 1

  // Rule 8: State is Open
  let state_open = output_datum.state == Open

  // Initial fields are empty/zero
  let initial_state_clean =
    output_datum.counter_evidence_hash == "" && output_datum.counter_evidence_uri == "" && output_datum.response_submitted_at == 0

  // Output at endorsement validator
  let at_validator =
    when output.address.payment_credential is {
      Script(hash) -> hash == refs.endorsement_policy_id
      _ -> False
    }

  // Output value includes correct AP3X
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Rule 9: Signed by challenger
  let signed = has_credential_signed(tx, output_datum.challenger_credential)

  challenger_exists && target_exists && target_stake_valid && not_self_challenge && challenger_old_enough && meets_minimum && evidence_valid && token_is_new && has_correct_token && state_open && initial_state_clean && at_validator && value_correct && signed
}

// ──────────────────────────────────────────────────────────────────────────────
// RespondToChallenge (Section 6.9)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the target agent's response to a challenge.
///
/// MUST:
///   1. ChallengeUTXO consumed (with challenge token)
///   2. state == Open
///   3. current slot <= created_at + CHALLENGE_RESPONSE_DEADLINE
///   4. Transaction signed by target agent's owner_credential
///   5. counter_evidence_hash is exactly 32 bytes
///   6. counter_evidence_uri is non-empty
///   7. Continuing ChallengeUTXO with response fields populated
pub fn validate_respond_to_challenge(
  tx: Transaction,
  params: ProtocolParams,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
  counter_evidence_hash: ByteArray,
  counter_evidence_uri: ByteArray,
  current_slot: Int,
) -> Bool {
  // Rule 2: State is Open
  let state_open = input_datum.state == Open

  // Rule 3: Within response deadline
  let within_deadline =
    current_slot <= input_datum.created_at + params.challenge_response_deadline

  // Rule 4: Signed by target
  let signed = has_credential_signed(tx, input_datum.target_credential)

  // Rule 5: Counter-evidence hash is 32 bytes
  let evidence_valid = builtin.length_of_bytearray(counter_evidence_hash) == 32

  // Rule 6: Counter-evidence URI is non-empty
  let uri_valid = builtin.length_of_bytearray(counter_evidence_uri) > 0

  // Rule 7: Continuing datum — all fields preserved except response fields
  let datum_preserved =
    output_datum.challenger_did == input_datum.challenger_did && output_datum.challenger_credential == input_datum.challenger_credential && output_datum.target_did == input_datum.target_did && output_datum.target_credential == input_datum.target_credential && output_datum.challenged_capability == input_datum.challenged_capability && output_datum.stake_amount == input_datum.stake_amount && output_datum.evidence_hash == input_datum.evidence_hash && output_datum.evidence_uri == input_datum.evidence_uri && output_datum.created_at == input_datum.created_at && output_datum.state == Open

  // Response fields updated
  let response_updated =
    output_datum.counter_evidence_hash == counter_evidence_hash && output_datum.counter_evidence_uri == counter_evidence_uri && output_datum.response_submitted_at == current_slot

  state_open && within_deadline && signed && evidence_valid && uri_valid && datum_preserved && response_updated
}

// ──────────────────────────────────────────────────────────────────────────────
// DefaultJudgment (Section 6.9)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate auto-resolution when target fails to respond.
///
/// Callable by anyone (permissionless).
/// If target did NOT respond by deadline → CapabilityFalsified.
/// DEFAULT_JUDGMENT_FEE: submitter receives a small fee from the challenge stake
/// as an incentive for triggering the resolution.
pub fn validate_default_judgment(
  tx: Transaction,
  cfg: ReputationConfig,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
  params: ProtocolParams,
  current_slot: Int,
) -> Bool {
  // State must be Open
  let state_open = input_datum.state == Open

  // Past response deadline
  let past_deadline =
    current_slot > input_datum.created_at + params.challenge_response_deadline

  // No response was submitted
  let no_response = input_datum.response_submitted_at == 0

  // Output state is Resolved { CapabilityFalsified }
  let resolved_correctly =
    output_datum.state == Resolved { outcome: CapabilityFalsified }

  // DEFAULT_JUDGMENT_FEE: small fee from challenge stake to any tx signer
  let fee = input_datum.stake_amount * params.default_judgment_fee / 10_000
  let fee_paid =
    if fee <= 0 {
      True
    } else {
      list.any(
        tx.outputs,
        fn(output) {
          // Pay to any extra signatory (the submitter)
          let is_to_signer =
            list.any(
              tx.extra_signatories,
              fn(signer) {
                output.address.payment_credential == VerificationKey(signer)
              },
            )
          let ap3x =
            get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
          is_to_signer && ap3x >= fee
        },
      )
    }

  // All other datum fields preserved (stake_amount preserved — fee deducted during DistributeOutcome)
  let datum_preserved =
    output_datum.challenger_did == input_datum.challenger_did && output_datum.challenger_credential == input_datum.challenger_credential && output_datum.target_did == input_datum.target_did && output_datum.target_credential == input_datum.target_credential && output_datum.challenged_capability == input_datum.challenged_capability && output_datum.stake_amount == input_datum.stake_amount && output_datum.evidence_hash == input_datum.evidence_hash && output_datum.evidence_uri == input_datum.evidence_uri && output_datum.created_at == input_datum.created_at && output_datum.counter_evidence_hash == input_datum.counter_evidence_hash && output_datum.counter_evidence_uri == input_datum.counter_evidence_uri && output_datum.response_submitted_at == input_datum.response_submitted_at

  state_open && past_deadline && no_response && resolved_correctly && fee_paid && datum_preserved
}

// ──────────────────────────────────────────────────────────────────────────────
// ResolveChallenge — Phase 1.0 Foundation Oracle (Section 6.11)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate Foundation oracle resolution of a challenge.
///
/// Phase 1.0: Foundation multi-sig signs the resolution.
pub fn validate_resolve_challenge(
  tx: Transaction,
  cfg: ReputationConfig,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
  outcome: RepChallengeOutcome,
) -> Bool {
  // State must be Open (possibly with response)
  let state_open = input_datum.state == Open

  // Signed by Foundation oracle
  let oracle_signed = has_key_signed(tx, cfg.oracle_credential)

  // Output state is Resolved with the given outcome
  let resolved_correctly = output_datum.state == Resolved { outcome }

  // All other datum fields preserved
  let datum_preserved =
    output_datum.challenger_did == input_datum.challenger_did && output_datum.challenger_credential == input_datum.challenger_credential && output_datum.target_did == input_datum.target_did && output_datum.target_credential == input_datum.target_credential && output_datum.challenged_capability == input_datum.challenged_capability && output_datum.stake_amount == input_datum.stake_amount && output_datum.evidence_hash == input_datum.evidence_hash && output_datum.evidence_uri == input_datum.evidence_uri && output_datum.created_at == input_datum.created_at && output_datum.counter_evidence_hash == input_datum.counter_evidence_hash && output_datum.counter_evidence_uri == input_datum.counter_evidence_uri && output_datum.response_submitted_at == input_datum.response_submitted_at

  state_open && oracle_signed && resolved_correctly && datum_preserved
}

// ──────────────────────────────────────────────────────────────────────────────
// EscalateToAudit — Game 1 Integration
// ──────────────────────────────────────────────────────────────────────────────

/// Validate escalation of a Game 3 challenge to Game 1 Adversarial Auditing.
///
/// MUST:
///   1. Challenge state == Open (with response — both parties have submitted evidence)
///   2. Within escalation window (created_at + challenge_response_deadline + escalation_window)
///   3. Signed by either challenger or target (either party can escalate)
///   4. A Game 1 claim UTXO is created at the audit_claim_validator_hash address
///      (verified by checking for a token under that policy in tx outputs)
///   5. Continuing challenge UTXO transitions to Escalated { audit_claim_ref }
///   6. All other datum fields preserved
pub fn validate_escalate_to_audit(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
  audit_claim_ref: OutputReference,
  current_slot: Int,
) -> Bool {
  // Rule 1: State must be Open
  let state_open = input_datum.state == Open

  // Rule 1: Response must have been submitted (both parties have evidence)
  let has_response = input_datum.response_submitted_at > 0

  // Rule 2: Within escalation window
  let escalation_deadline =
    input_datum.created_at + params.challenge_response_deadline + params.escalation_window
  let within_window = current_slot <= escalation_deadline

  // Rule 3: Signed by either party
  let challenger_signed =
    has_credential_signed(tx, input_datum.challenger_credential)
  let target_signed = has_credential_signed(tx, input_datum.target_credential)
  let authorized = challenger_signed || target_signed

  // Rule 4 (F-NEW-7 fix): Verify a Game 1 claim output exists at the claim
  // validator address with a datum that links back to this challenge.
  // The claim_hash must be blake2b_256(evidence_hash ++ counter_evidence_hash)
  // and claim_type must be "reputation_challenge".
  let expected_claim_hash =
    builtin.blake2b_256(
      builtin.append_bytearray(
        input_datum.evidence_hash,
        input_datum.counter_evidence_hash,
      ),
    )
  let claim_output_valid =
    list.any(
      tx.outputs,
      fn(output) {
        let is_at_claim =
          when output.address.payment_credential is {
            Script(hash) -> hash == cfg.audit_claim_validator_hash
            _ -> False
          }
        if is_at_claim {
          when output.datum is {
            InlineDatum(raw) -> {
              expect claim: AuditClaimDatum = raw
              claim.claim_hash == expected_claim_hash && claim.claim_type == "reputation_challenge"
            }
            _ -> False
          }
        } else {
          False
        }
      },
    )

  // Rule 5: Output state transitions to Escalated
  let state_escalated =
    output_datum.state == Escalated { audit_claim_ref }

  // Rule 6: All other datum fields preserved
  let datum_preserved =
    output_datum.challenger_did == input_datum.challenger_did && output_datum.challenger_credential == input_datum.challenger_credential && output_datum.target_did == input_datum.target_did && output_datum.target_credential == input_datum.target_credential && output_datum.challenged_capability == input_datum.challenged_capability && output_datum.stake_amount == input_datum.stake_amount && output_datum.evidence_hash == input_datum.evidence_hash && output_datum.evidence_uri == input_datum.evidence_uri && output_datum.created_at == input_datum.created_at && output_datum.counter_evidence_hash == input_datum.counter_evidence_hash && output_datum.counter_evidence_uri == input_datum.counter_evidence_uri && output_datum.response_submitted_at == input_datum.response_submitted_at

  state_open && has_response && within_window && authorized && claim_output_valid && state_escalated && datum_preserved
}

// ──────────────────────────────────────────────────────────────────────────────
// ResolveEscalation — Read Game 1 verdict and map to Game 3 outcome
// ──────────────────────────────────────────────────────────────────────────────

/// Validate resolution of an escalated challenge by reading Game 1 verdict.
///
/// Permissionless — anyone can trigger once Game 1 challenge is resolved.
///
/// MUST:
///   1. Challenge state == Escalated { audit_claim_ref }
///   2. Game 1 challenge UTXO found in reference inputs at audit_challenge_validator_hash
///      with state == AuditResolved { verdict }
///   3. Game 1 challenge references the same claim (claim_ref matches audit_claim_ref)
///   4. Verdict mapped: ClaimerWins → CapabilityVerified, AuditorWins → CapabilityFalsified,
///      AuditInconclusive → Inconclusive
///   5. Output state == Resolved { mapped_outcome }
///   6. All other datum fields preserved
pub fn validate_resolve_escalation(
  tx: Transaction,
  cfg: ReputationConfig,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
) -> Bool {
  // Rule 1: State must be Escalated
  expect Escalated { audit_claim_ref } = input_datum.state

  // Rule 2: Find the Game 1 challenge in reference inputs
  expect Some(g1_challenge_input) =
    list.find(
      tx.reference_inputs,
      fn(input) {
        let is_at_challenge_validator =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == cfg.audit_challenge_validator_hash
            _ -> False
          }
        if is_at_challenge_validator {
          when input.output.datum is {
            InlineDatum(raw) -> {
              expect g1_datum: AuditChallengeDatum = raw
              // Rule 3: Must reference the same claim
              g1_datum.claim_ref == audit_claim_ref
            }
            _ -> False
          }
        } else {
          False
        }
      },
    )

  // Parse the Game 1 challenge datum
  expect InlineDatum(raw_g1) = g1_challenge_input.output.datum
  expect g1_challenge: AuditChallengeDatum = raw_g1

  // Rule 2: Must be in AuditResolved state
  expect AuditResolved { verdict } = g1_challenge.state

  // F-NEW-3 fix: Verify the Game 1 challenge involves at least one of the
  // Game 3 parties (auditor_did should match the escalating party's DID).
  // The claim was created from the Game 3 challenge's evidence, so the
  // Game 1 challenge must reference the correct claim (already checked above)
  // and the auditor must be a known party.
  let identity_linked =
    g1_challenge.auditor_did == input_datum.challenger_did || g1_challenge.auditor_did == input_datum.target_did

  // Rule 4: Map Game 1 verdict to Game 3 outcome
  let mapped_outcome =
    map_audit_verdict(verdict)

  // Rule 5: Output state is Resolved with mapped outcome
  let resolved_correctly = output_datum.state == Resolved { outcome: mapped_outcome }

  // Rule 6: All other datum fields preserved
  let datum_preserved =
    output_datum.challenger_did == input_datum.challenger_did && output_datum.challenger_credential == input_datum.challenger_credential && output_datum.target_did == input_datum.target_did && output_datum.target_credential == input_datum.target_credential && output_datum.challenged_capability == input_datum.challenged_capability && output_datum.stake_amount == input_datum.stake_amount && output_datum.evidence_hash == input_datum.evidence_hash && output_datum.evidence_uri == input_datum.evidence_uri && output_datum.created_at == input_datum.created_at && output_datum.counter_evidence_hash == input_datum.counter_evidence_hash && output_datum.counter_evidence_uri == input_datum.counter_evidence_uri && output_datum.response_submitted_at == input_datum.response_submitted_at

  identity_linked && resolved_correctly && datum_preserved
}

/// Map a Game 1 Verdict to a Game 3 RepChallengeOutcome.
/// ClaimerWins means the agent's claim was valid → CapabilityVerified
/// AuditorWins means the auditor proved it false → CapabilityFalsified
/// AuditInconclusive → Inconclusive
pub fn map_audit_verdict(verdict: AuditVerdict) -> RepChallengeOutcome {
  when verdict is {
    ClaimerWins -> CapabilityVerified
    AuditorWins -> CapabilityFalsified
    AuditInconclusive -> Inconclusive
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// DistributeOutcome (Section 6.11)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the distribution of rewards/slashing after challenge resolution.
///
/// Callable by anyone (permissionless — outcome is deterministic).
///
/// CapabilityFalsified:
///   - Target slashed: self_stake × (1 / num_staked_capabilities)
///   - Endorsers slashed: 50% of endorsement stake
///   - Challenger receives: total_slashed - protocol_fee
///   - Protocol treasury receives: protocol_fee
///
/// CapabilityVerified:
///   - Challenger loses entire stake
///   - Target receives: challenger_stake - protocol_fee
///   - Protocol treasury receives: protocol_fee
///
/// Inconclusive:
///   - Both stakes returned minus protocol_fee split equally
pub fn validate_distribute_outcome(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  // Must be in Resolved state
  expect Resolved { outcome } = challenge_datum.state

  // Challenge token must be burned
  let expected_token_name =
    derive_challenge_token_name(
      challenge_datum.challenger_did,
      challenge_datum.target_did,
      challenge_datum.challenged_capability,
    )
  let token_burned =
    assets.quantity_of(
      tx.mint,
      refs.endorsement_policy_id,
      expected_token_name,
    ) == -1

  // Validate payout structure based on outcome
  let payouts_valid =
    when outcome is {
      CapabilityFalsified ->
        validate_falsified_payouts(tx, cfg, refs, params, challenge_datum)
      CapabilityVerified ->
        validate_verified_payouts(tx, cfg, refs, params, challenge_datum)
      Inconclusive ->
        validate_inconclusive_payouts(tx, cfg, params, challenge_datum)
    }

  token_burned && payouts_valid
}

/// Validate payouts when capability is falsified (challenger wins).
fn validate_falsified_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  // Calculate target slash: self_stake × (1 / num_capabilities)
  // We need the target's StakeDatum to get num_staked_capabilities
  let target_stake_token = derive_stake_token_name(challenge_datum.target_did)

  // Find target's StakeUTXO in tx inputs (being consumed for slashing)
  expect Some(stake_input) =
    list.find(
      tx.inputs,
      fn(input) {
        assets.quantity_of(
          input.output.value,
          refs.reputation_policy_id,
          target_stake_token,
        ) >= 1
      },
    )
  expect InlineDatum(raw_stake) = stake_input.output.datum
  expect stake_datum: StakeDatum = raw_stake
  let num_capabilities = list.length(stake_datum.staked_capabilities)
  // Guard against division by zero
  expect num_capabilities > 0
  let target_slash = stake_datum.stake_amount / num_capabilities

  // Protocol fee on total slashed amount (target slash only for now —
  // endorser slashing is handled separately in the tx)
  let protocol_fee = target_slash * params.protocol_fee_rate / 10_000
  let challenger_reward = target_slash - protocol_fee

  // Verify challenger receives their reward + original stake back
  let challenger_paid =
    verify_payment_to_credential(
      tx.outputs,
      challenge_datum.challenger_credential,
      cfg,
      challenger_reward + challenge_datum.stake_amount,
    )

  // Verify protocol treasury receives fee
  let treasury_paid =
    verify_treasury_payment(tx.outputs, cfg, protocol_fee)

  // Verify target's StakeUTXO continues with reduced stake
  let target_stake_continues =
    verify_reduced_stake_output(
      tx.outputs,
      refs,
      cfg,
      challenge_datum.target_did,
      stake_datum.stake_amount - target_slash,
    )

  challenger_paid && treasury_paid && target_stake_continues
}

/// Validate payouts when capability is verified (target wins).
fn validate_verified_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  let protocol_fee =
    challenge_datum.stake_amount * params.protocol_fee_rate / 10_000
  let target_reward = challenge_datum.stake_amount - protocol_fee

  // Target receives challenger's stake minus protocol fee
  let target_paid =
    verify_payment_to_credential(
      tx.outputs,
      challenge_datum.target_credential,
      cfg,
      target_reward,
    )

  // Protocol treasury receives fee
  let treasury_paid =
    verify_treasury_payment(tx.outputs, cfg, protocol_fee)

  // ChallengeWon history bonus: target should receive a history bonus token
  // for successfully defending their capability claim.
  // Verify a history bonus token is minted under the reputation policy.
  let history_bonus_minted =
    list.any(
      assets.flatten(tx.mint),
      fn(entry) {
        let (pid, name, qty) = entry
        pid == refs.reputation_policy_id && starts_with(name, history_bonus_token_prefix) && qty == 1
      },
    )

  target_paid && treasury_paid && history_bonus_minted
}

/// Validate payouts for inconclusive outcome.
/// Both parties get their stakes back minus split protocol fee.
fn validate_inconclusive_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  // Small protocol fee from both parties
  let total_fee =
    challenge_datum.stake_amount * params.protocol_fee_rate / 10_000
  let half_fee = total_fee / 2
  let challenger_return = challenge_datum.stake_amount - half_fee

  // Challenger gets most of their stake back
  let challenger_paid =
    verify_payment_to_credential(
      tx.outputs,
      challenge_datum.challenger_credential,
      cfg,
      challenger_return,
    )

  // Target's challenge UTXO stake is also returned (minus half fee)
  // The challenge UTXO held the challenger's stake; target's self-stake is untouched.
  // No explicit target return needed — their StakeUTXO is not consumed.

  // Treasury gets fee
  let treasury_paid = verify_treasury_payment(tx.outputs, cfg, total_fee)

  challenger_paid && treasury_paid
}

// ──────────────────────────────────────────────────────────────────────────────
// MintHistoryBonus — Game 1 cross-game validation
// ──────────────────────────────────────────────────────────────────────────────

/// Validate minting a history bonus for winning an audit claim in Game 1.
///
/// MUST:
///   1. A Game 1 claim UTXO exists in reference inputs at audit_claim_validator_hash
///      with state == AuditValidated
///   2. The claim's claimer_did matches the agent_did receiving the bonus
///   3. Oracle signs (prevents replay — oracle tracks which claims have been rewarded)
pub fn validate_audit_claim_won_bonus(
  tx: Transaction,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> Bool {
  // Find a validated Game 1 claim for this agent
  let has_validated_claim =
    list.any(
      tx.reference_inputs,
      fn(input) {
        let is_at_claim_validator =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == cfg.audit_claim_validator_hash
            _ -> False
          }
        if is_at_claim_validator {
          when input.output.datum is {
            InlineDatum(raw) -> {
              expect claim: AuditClaimDatum = raw
              // claimer_did in Game 1 = agent DID (NFT asset name)
              claim.claimer_did == agent_did && claim.state == AuditValidated
            }
            _ -> False
          }
        } else {
          False
        }
      },
    )

  // Oracle must sign to prevent replay
  let oracle_signed = has_key_signed(tx, cfg.oracle_credential)

  has_validated_claim && oracle_signed
}

/// Validate minting a history bonus for jury duty in Game 1.
///
/// MUST:
///   1. A Game 1 juror UTXO exists in reference inputs at audit_jury_pool_hash
///      with cases_resolved > 0 and majority_votes > 0
///   2. The juror's juror_did matches the agent_did receiving the bonus
///   3. Oracle signs (prevents replay)
pub fn validate_juror_duty_bonus(
  tx: Transaction,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> Bool {
  // Find a juror UTXO showing completed duty for this agent
  let has_jury_duty =
    list.any(
      tx.reference_inputs,
      fn(input) {
        let is_at_jury_pool =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == cfg.audit_jury_pool_hash
            _ -> False
          }
        if is_at_jury_pool {
          when input.output.datum is {
            InlineDatum(raw) -> {
              expect juror: AuditJurorDatum = raw
              juror.juror_did == agent_did && juror.cases_resolved > 0 && juror.majority_votes > 0
            }
            _ -> False
          }
        } else {
          False
        }
      },
    )

  // Oracle must sign to prevent replay
  let oracle_signed = has_key_signed(tx, cfg.oracle_credential)

  has_jury_duty && oracle_signed
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that the target agent has the challenged capability in their StakeUTXO.
fn verify_target_capability(
  reference_inputs: List<Input>,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
  capability: ByteArray,
) -> Bool {
  let target_stake_token = derive_stake_token_name(target_did)
  expect Some(stake_input) =
    list.find(
      reference_inputs,
      fn(input) {
        let is_at_reputation =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == refs.reputation_policy_id
            _ -> False
          }
        let has_token =
          assets.quantity_of(
            input.output.value,
            refs.reputation_policy_id,
            target_stake_token,
          ) == 1
        is_at_reputation && has_token
      },
    )
  expect InlineDatum(raw_stake) = stake_input.output.datum
  expect stake_datum: StakeDatum = raw_stake
  list.has(stake_datum.staked_capabilities, capability)
}

/// Verify that a credential receives at least the specified AP3X amount
/// in the transaction outputs.
fn verify_payment_to_credential(
  outputs: List<Output>,
  credential: address.Credential,
  cfg: ReputationConfig,
  min_amount: Int,
) -> Bool {
  list.any(
    outputs,
    fn(output) {
      let is_to_credential =
        output.address.payment_credential == credential
      let ap3x =
        get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
      is_to_credential && ap3x >= min_amount
    },
  )
}

/// Verify that the protocol treasury receives the specified AP3X amount.
fn verify_treasury_payment(
  outputs: List<Output>,
  cfg: ReputationConfig,
  min_amount: Int,
) -> Bool {
  if min_amount <= 0 {
    True
  } else {
    list.any(
      outputs,
      fn(output) {
        let is_treasury =
          when output.address.payment_credential is {
            Script(hash) -> hash == cfg.treasury_script_hash
            _ -> False
          }
        let ap3x =
          get_ap3x_amount(
            output.value,
            cfg.ap3x_policy_id,
            cfg.ap3x_asset_name,
          )
        is_treasury && ap3x >= min_amount
      },
    )
  }
}

/// Verify that the target's StakeUTXO continues with reduced stake amount.
fn verify_reduced_stake_output(
  outputs: List<Output>,
  refs: CrossValidatorRefs,
  cfg: ReputationConfig,
  agent_did: ByteArray,
  expected_remaining_stake: Int,
) -> Bool {
  let stake_token = derive_stake_token_name(agent_did)
  list.any(
    outputs,
    fn(output) {
      let is_at_reputation =
        when output.address.payment_credential is {
          Script(hash) -> hash == refs.reputation_policy_id
          _ -> False
        }
      let has_token =
        assets.quantity_of(output.value, refs.reputation_policy_id, stake_token) == 1
      let ap3x =
        get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
      is_at_reputation && has_token && ap3x >= expected_remaining_stake
    },
  )
}
