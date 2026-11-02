/// Challenge validation logic for Module 3: Reputation Staking.
///
/// Validates challenge operations: MintChallengeToken, RespondToChallenge,
/// ResolveChallenge, DefaultJudgment, DistributeOutcome and ExpireChallenge.
/// See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.8–6.11.
///
/// Times are POSIX milliseconds; see reputation_staking/time for the rules
/// on validity range bounds.
///
/// Identity registry checks (Part 2 §7 of `docs/IDENTITY_REGISTRY.md` in
/// agents-marketplace, Module-3 row): opening a challenge needs the
/// challenger's record with effective status Active, old enough by its
/// `created_at`, and the challenger's controller signature (in addition to
/// the challenger_credential signature); challenger_credential is a declared
/// credential of that record. The validity range is at most one hour long.
/// The target's identity record is not read: the target's StakeUTXO decides
/// (rule D-dodge).
///
/// Challenge locks: opening a challenge spends the target's StakeUTXO, which
/// adds the challenge token name to its challenge_locks (LockForChallenge).
/// Every path that closes a challenge (WithdrawChallenge, ExpireChallenge,
/// DistributeOutcome) also spends the target's StakeUTXO, which removes the
/// lock (ReleaseChallenge, or SlashStake for CapabilityFalsified).

use aiken/builtin
use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, Transaction}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/time
use reputation_staking/types.{
  AuditChallengeDatum, AuditClaimDatum, AuditJurorDatum,
  AuditResolved, AuditValidated, AuditVerdict,
  AuditorWins, ClaimerWins, AuditInconclusive,
  CapabilityFalsified, CapabilityVerified, Escalated, Inconclusive, Open,
  RepChallengeOutcome, ReputationChallengeDatum, Resolved, StakeDatum,
}
use reputation_staking/utils.{
  ap3x_paid_to, derive_challenge_token_name, derive_stake_token_name,
  exact_address, get_ap3x_amount, has_credential_signed, has_key_signed,
  treasury_paid,
}
use shared/identity
use shared/identity.{Window}
use vector_identity.{controller_signed}

// ──────────────────────────────────────────────────────────────────────────────
// MintChallengeToken (Section 6.8)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate creation of a new reputation challenge.
///
/// MUST:
///   1. The challenger's identity record is Active over the validity range,
///      its controller signed, it was created at least MIN_AGENT_AGE before
///      the lower bound of the validity range (`created_at`), and
///      challenger_credential is a declared credential of the record
///   2. The transaction spends the target's StakeUTXO (the input at the
///      exact reputation address with the target's stake token), which adds
///      the lock of this challenge (LockForChallenge). The target's identity
///      record is not read: a suspended, revoked or retired target, or a
///      target that removed the capability from its record, can be
///      challenged while its stake lists the capability
///   3. challenger_did != target_did
///   4. challenged_capability is in the stake's staked_capabilities, and
///      target_credential is the owner_credential of the stake
///   5. stake_amount >= MIN_CHALLENGE_STAKE
///   6. evidence_hash is exactly 32 bytes
///   7. No existing open challenge from this challenger for same capability
///      (the stake refuses a second lock with the same token name)
///   8. ChallengeUTXO created with state = Open, and the transaction mints
///      exactly its challenge token and no other token of the policy
///   9. Transaction signed by challenger_credential
///  10. created_at is the lower bound of a validity range of at most one hour
pub fn validate_mint_challenge(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  output_datum: ReputationChallengeDatum,
  output: Output,
) -> Bool {
  // Rules 2 and 4: the target's stake decides
  let target_stake = spent_target_stake(tx, refs, output_datum.target_did)

  // Rule 3: No self-challenge
  let not_self_challenge =
    output_datum.challenger_did != output_datum.target_did
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

  // Rules 1 and 10: the challenger's record, age, signature and declared
  // credential, and created_at (the deadlines of the challenge count from
  // created_at)
  when identity.window(tx.validity_range) is {
    Some(w) -> and {
        challenger_ok(tx, cfg, params, output_datum, w),
        output_datum.created_at == w.lo,
        list.has(
          target_stake.staked_capabilities,
          output_datum.challenged_capability,
        ),
        target_stake.owner_credential == output_datum.target_credential,
        not_self_challenge,
        meets_minimum,
        evidence_valid,
        token_is_new,
        has_correct_token,
        dict.to_pairs(assets.tokens(tx.mint, refs.endorsement_policy_id)) == [
          Pair(expected_token_name, 1),
        ],
        state_open,
        initial_state_clean,
        at_validator,
        value_correct,
        signed,
      }
    None -> False
  }
}

/// The challenger's identity record is Active over `w`, its controller
/// signed, it was created at least `min_agent_age` before `w.lo`, and
/// challenger_credential is a declared credential of the record. The age
/// comes from the record's `created_at` (the upper bound of the validity
/// range of the registering transaction).
fn challenger_ok(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  datum: ReputationChallengeDatum,
  w: Window,
) -> Bool {
  when
    identity.active_record(
      tx.reference_inputs,
      cfg.identity_hash,
      datum.challenger_did,
      w,
    )
  is {
    Some(record) -> and {
        controller_signed(record, tx),
        record.created_at + params.min_agent_age <= w.lo,
        identity.declared_credential(record, datum.challenger_credential),
      }
    None -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// RespondToChallenge (Section 6.9)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the target agent's response to a challenge.
///
/// MUST:
///   1. ChallengeUTXO consumed (with challenge token)
///   2. state == Open
///   3. The upper bound of the validity range is at most
///      created_at + CHALLENGE_RESPONSE_DEADLINE
///   4. Transaction signed by target agent's owner_credential
///   5. counter_evidence_hash is exactly 32 bytes
///   6. counter_evidence_uri is non-empty
///   7. Continuing ChallengeUTXO with response fields populated;
///      response_submitted_at is the lower bound of a validity range of at
///      most one hour
pub fn validate_respond_to_challenge(
  tx: Transaction,
  params: ProtocolParams,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
  counter_evidence_hash: ByteArray,
  counter_evidence_uri: ByteArray,
) -> Bool {
  // Rule 2: State is Open
  let state_open = input_datum.state == Open

  // Rule 3: Before the response deadline
  let within_deadline =
    when time.upper_bound(tx) is {
      Some(hi) ->
        hi <= input_datum.created_at + params.challenge_response_deadline
      None -> False
    }

  // Rule 4: Signed by target
  let signed = has_credential_signed(tx, input_datum.target_credential)

  // Rule 5: Counter-evidence hash is 32 bytes
  let evidence_valid = builtin.length_of_bytearray(counter_evidence_hash) == 32

  // Rule 6: Counter-evidence URI is non-empty
  let uri_valid = builtin.length_of_bytearray(counter_evidence_uri) > 0

  // Rule 7: Continuing datum — all fields preserved except response fields
  let datum_preserved =
    same_terms(output_datum, input_datum) && output_datum.state == Open

  // Response fields updated
  let response_updated =
    when time.bounded_lower_bound(tx) is {
      Some(lo) ->
        output_datum.counter_evidence_hash == counter_evidence_hash && output_datum.counter_evidence_uri == counter_evidence_uri && output_datum.response_submitted_at == lo
      None -> False
    }

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
  let datum_preserved = same_terms_and_response(output_datum, input_datum)

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
  let datum_preserved = same_terms_and_response(output_datum, input_datum)

  state_open && oracle_signed && resolved_correctly && datum_preserved
}

// ──────────────────────────────────────────────────────────────────────────────
// EscalateToAudit — Module 1 Integration
// ──────────────────────────────────────────────────────────────────────────────

/// Validate escalation of a Module 3 challenge to Module 1 Adversarial Auditing.
///
/// MUST:
///   1. Challenge state == Open (with response — both parties have submitted evidence)
///   2. Within escalation window: the upper bound of the validity range is at
///      most created_at + challenge_response_deadline + escalation_window
///   3. Signed by either challenger or target (either party can escalate)
///   4. The transaction mints exactly one Module 1 claim token (policy
///      audit_claim_validator_hash), and a Module 1 claim output at the
///      claim validator holds it, with a datum that links back to this
///      challenge
///   5. Continuing challenge UTXO transitions to
///      Escalated { audit_claim_token } with the name of that claim token.
///      (The output reference of the claim cannot be in the datum: it
///      contains the id of this transaction.)
///   6. All other datum fields preserved
pub fn validate_escalate_to_audit(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
) -> Bool {
  // Rule 1: State must be Open
  let state_open = input_datum.state == Open

  // Rule 1: Response must have been submitted (both parties have evidence)
  let has_response = input_datum.response_submitted_at > 0

  // Rule 2: Within escalation window
  let escalation_deadline =
    input_datum.created_at + params.challenge_response_deadline + params.escalation_window
  let within_window =
    when time.upper_bound(tx) is {
      Some(hi) -> hi <= escalation_deadline
      None -> False
    }

  // Rule 3: Signed by either party
  let challenger_signed =
    has_credential_signed(tx, input_datum.challenger_credential)
  let target_signed = has_credential_signed(tx, input_datum.target_credential)
  let authorized = challenger_signed || target_signed

  // Rule 4: the one Module 1 claim token that this transaction mints
  expect [Pair(claim_token, 1)] =
    dict.to_pairs(assets.tokens(tx.mint, cfg.audit_claim_validator_hash))

  // Rule 4 (F-NEW-7 fix): Verify a Module 1 claim output exists at the claim
  // validator address with the claim token and a datum that links back to
  // this challenge. The claim_hash must be
  // blake2b_256(evidence_hash ++ counter_evidence_hash) and claim_type must
  // be "reputation_challenge".
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
        let is_claim =
          output.address.payment_credential == Script(
            cfg.audit_claim_validator_hash,
          ) && assets.quantity_of(
            output.value,
            cfg.audit_claim_validator_hash,
            claim_token,
          ) == 1
        if is_claim {
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
    output_datum.state == Escalated { audit_claim_token: claim_token }

  // Rule 6: All other datum fields preserved
  let datum_preserved = same_terms_and_response(output_datum, input_datum)

  state_open && has_response && within_window && authorized && claim_output_valid && state_escalated && datum_preserved
}

// ──────────────────────────────────────────────────────────────────────────────
// ResolveEscalation — Read Module 1 verdict and map to Module 3 outcome
// ──────────────────────────────────────────────────────────────────────────────

/// Validate resolution of an escalated challenge by reading Module 1 verdict.
///
/// Permissionless — anyone can trigger once Module 1 challenge is resolved.
///
/// MUST:
///   1. Challenge state == Escalated { audit_claim_token }
///   2. The Module 1 challenge is a reference input at the exact
///      audit_challenge_validator_hash address that holds exactly one token
///      of the Module 1 challenge policy, whose datum names the claim token
///      (claim_token == audit_claim_token), with state
///      AuditResolved { verdict, .. }
///   3. Verdict mapped: ClaimerWins → CapabilityVerified, AuditorWins → CapabilityFalsified,
///      AuditInconclusive → Inconclusive
///   4. Output state == Resolved { mapped_outcome }
///   5. All other datum fields preserved
pub fn validate_resolve_escalation(
  tx: Transaction,
  cfg: ReputationConfig,
  input_datum: ReputationChallengeDatum,
  output_datum: ReputationChallengeDatum,
) -> Bool {
  // Rule 1: State must be Escalated
  expect Escalated { audit_claim_token } = input_datum.state

  // Rule 2: Find the Module 1 challenge in reference inputs. Its challenge
  // token shows that the Module 1 challenge validator created it.
  let challenge_policy = cfg.audit_challenge_validator_hash
  let challenge_address = exact_address(challenge_policy)
  expect Some(g1_challenge) =
    list.find_map(
      tx.reference_inputs,
      fn(input) {
        let is_module1_challenge =
          input.output.address == challenge_address && when
            dict.to_pairs(assets.tokens(input.output.value, challenge_policy))
          is {
            [Pair(_, 1)] -> True
            _ -> False
          }
        if is_module1_challenge {
          expect InlineDatum(raw) = input.output.datum
          expect g1_datum: AuditChallengeDatum = raw
          if g1_datum.claim_token == audit_claim_token {
            Some(g1_datum)
          } else {
            None
          }
        } else {
          None
        }
      },
    )

  // Rule 2: Must be in AuditResolved state
  expect AuditResolved { verdict, .. } = g1_challenge.state

  // F-NEW-3 fix: Verify the Module 1 challenge involves at least one of the
  // Module 3 parties (auditor_did should match the escalating party's DID).
  // The claim was created from the Module 3 challenge's evidence, so the
  // Module 1 challenge must reference the correct claim (already checked above)
  // and the auditor must be a known party.
  let identity_linked =
    g1_challenge.auditor_did == input_datum.challenger_did || g1_challenge.auditor_did == input_datum.target_did

  // Rule 3: Map Module 1 verdict to Module 3 outcome
  let mapped_outcome =
    map_audit_verdict(verdict)

  // Rule 4: Output state is Resolved with mapped outcome
  let resolved_correctly = output_datum.state == Resolved { outcome: mapped_outcome }

  // Rule 5: All other datum fields preserved
  let datum_preserved = same_terms_and_response(output_datum, input_datum)

  identity_linked && resolved_correctly && datum_preserved
}

/// Map a Module 1 Verdict to a Module 3 RepChallengeOutcome.
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
// ExpireChallenge — timeout when nobody resolves the challenge
// ──────────────────────────────────────────────────────────────────────────────

/// The POSIX ms time after which an unresolved challenge can be closed.
///
/// The timeout starts when the escalation window closes
/// (created_at + challenge_response_deadline + escalation_window) and lasts
/// one activity window (activity_window × epoch_length). With the deployment
/// values this is 12 h + 6 h + 180 × 4 h = 30 days and 18 hours after
/// created_at. The timeout comes from existing parameters only.
pub fn challenge_timeout(params: ProtocolParams, created_at: Int) -> Int {
  created_at + params.challenge_response_deadline + params.escalation_window + params.activity_window * params.epoch_length
}

/// Validate the closing of a challenge that nobody resolved before the
/// challenge timeout.
///
/// Callable by anyone (permissionless — the outcome is fixed).
///
/// MUST:
///   1. The challenge is Open with a response, or Escalated. (An Open
///      challenge without a response is closed by DefaultJudgment instead.)
///   2. The lower bound of the validity range is after challenge_timeout.
///   3. The challenge token is burned.
///   4. The outputs to challenger_credential hold at least stake_amount AP3X
///      in total.
///   5. The transaction spends the target's StakeUTXO, which removes the
///      lock of the challenge (ReleaseChallenge) and keeps all its AP3X. No
///      fee is taken.
pub fn validate_expire_challenge(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  // Rule 1: Unresolved after a response
  let unresolved =
    when challenge_datum.state is {
      Open -> challenge_datum.response_submitted_at > 0
      Escalated { .. } -> True
      Resolved { .. } -> False
    }

  // Rule 2: After the challenge timeout
  let timed_out =
    when time.lower_bound(tx) is {
      Some(lo) -> lo > challenge_timeout(params, challenge_datum.created_at)
      None -> False
    }

  // Rule 3: Challenge token burned
  let expected_token_name =
    derive_challenge_token_name(
      challenge_datum.challenger_did,
      challenge_datum.target_did,
      challenge_datum.challenged_capability,
    )
  let token_burned =
    assets.quantity_of(tx.mint, refs.endorsement_policy_id, expected_token_name) == -1

  and {
    unresolved,
    timed_out,
    token_burned,
    // Rule 4: Challenger receives the challenge stake
    ap3x_paid_to(cfg, tx.outputs, challenge_datum.challenger_credential) >= challenge_datum.stake_amount,
    // Rule 5: the lock of the challenge goes
    spends_target_stake(tx, refs, challenge_datum.target_did),
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// DistributeOutcome (Section 6.11)
// ──────────────────────────────────────────────────────────────────────────────

/// Validate the distribution of rewards/slashing after challenge resolution.
///
/// Callable by anyone (permissionless — outcome is deterministic).
///
/// Every outcome burns the challenge token and spends the target's
/// StakeUTXO, which removes the lock of the challenge (SlashStake for
/// CapabilityFalsified, ReleaseChallenge for the other outcomes). The
/// locked stake always exists: DecreaseStake, UpdateCapabilities and decay
/// need a stake without locks. So the challenger's own stake can always be
/// settled. All payout checks add up the outputs to the payee.
///
/// CapabilityFalsified:
///   - Target slashed: self_stake × (1 / num_staked_capabilities)
///   - Endorsers slashed: 50% of endorsement stake (SlashEndorsement)
///   - Challenger receives: the AP3X that the challenge holds + slash -
///     protocol_fee
///   - Protocol treasury receives: protocol_fee
///
/// CapabilityVerified:
///   - Challenger loses entire stake
///   - Target receives: challenger_stake - protocol_fee
///   - Protocol treasury receives: protocol_fee
///
/// Inconclusive:
///   - Challenger receives: challenger_stake - half of protocol_fee
///   - Protocol treasury receives: half of protocol_fee
///   - The target's stake keeps its AP3X
pub fn validate_distribute_outcome(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  own_input: Input,
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

  // The target's StakeUTXO that this transaction spends
  let stake = spent_target_stake(tx, refs, challenge_datum.target_did)

  // The AP3X that the challenge holds: the challenge stake, less the
  // DefaultJudgment fee when that fee left the challenge output
  let held =
    get_ap3x_amount(
      own_input.output.value,
      cfg.ap3x_policy_id,
      cfg.ap3x_asset_name,
    )

  and {
    assets.quantity_of(tx.mint, refs.endorsement_policy_id, expected_token_name) == -1,
    // Used for every outcome, so the stake input is needed for each one
    stake.agent_did == challenge_datum.target_did,
    when outcome is {
      CapabilityFalsified ->
        validate_falsified_payouts(tx, cfg, params, challenge_datum, stake, held)
      CapabilityVerified ->
        validate_verified_payouts(tx, cfg, params, challenge_datum)
      Inconclusive ->
        validate_inconclusive_payouts(tx, cfg, params, challenge_datum)
    },
  }
}

/// Validate payouts when capability is falsified (challenger wins).
///
/// slash = stake_amount / num_staked_capabilities of the target's stake
/// (SlashStake takes it from the stake), protocol_fee = slash ×
/// protocol_fee_rate.
///   - The outputs to challenger_credential hold at least
///     held + slash - protocol_fee AP3X, where held is the AP3X that the
///     challenge holds.
///   - The treasury receives at least protocol_fee.
/// The payouts add up to held + slash: the funds of the challenge and the
/// slash, and nothing more.
fn validate_falsified_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
  stake: StakeDatum,
  held: Int,
) -> Bool {
  let num_capabilities = list.length(stake.staked_capabilities)
  // Guard against division by zero
  expect num_capabilities > 0
  let target_slash = stake.stake_amount / num_capabilities

  // Protocol fee on total slashed amount (target slash only for now —
  // endorser slashing is handled separately in the tx)
  let protocol_fee = target_slash * params.protocol_fee_rate / 10_000

  and {
    ap3x_paid_to(cfg, tx.outputs, challenge_datum.challenger_credential) >= held + target_slash - protocol_fee,
    treasury_paid(cfg, tx.outputs, protocol_fee),
  }
}

/// Validate payouts when capability is verified (target wins).
///
/// The ChallengeWon history bonus of the target is not a condition. A
/// history bonus needs the target's identity record with effective status
/// Active and the target's controller signature (MintHistoryBonus), so a
/// required bonus would lock the challenge stake while the target is
/// suspended, revoked or retired, or when its controller does not sign. The
/// transaction can still mint the bonus (MintHistoryBonus).
fn validate_verified_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  let protocol_fee =
    challenge_datum.stake_amount * params.protocol_fee_rate / 10_000

  and {
    // Target receives challenger's stake minus protocol fee
    ap3x_paid_to(cfg, tx.outputs, challenge_datum.target_credential) >= challenge_datum.stake_amount - protocol_fee,
    // Protocol treasury receives fee
    treasury_paid(cfg, tx.outputs, protocol_fee),
  }
}

/// Validate payouts for inconclusive outcome.
///
/// The challenge holds only the challenger's stake, and the target's stake
/// keeps its AP3X (ReleaseChallenge). So the challenger pays only its half
/// of the protocol fee, and the target pays nothing:
/// half_fee = (stake_amount × protocol_fee_rate) / 2.
///   - The outputs to challenger_credential hold at least
///     stake_amount - half_fee AP3X.
///   - The treasury receives at least half_fee.
/// The payouts add up to stake_amount, which the challenge holds.
fn validate_inconclusive_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  let total_fee =
    challenge_datum.stake_amount * params.protocol_fee_rate / 10_000
  let half_fee = total_fee / 2

  and {
    ap3x_paid_to(cfg, tx.outputs, challenge_datum.challenger_credential) >= challenge_datum.stake_amount - half_fee,
    treasury_paid(cfg, tx.outputs, half_fee),
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// MintHistoryBonus — Module 1 cross-module validation
// ──────────────────────────────────────────────────────────────────────────────

/// Validate minting a history bonus for winning an audit claim in Module 1.
///
/// MUST:
///   1. A Module 1 claim UTXO exists in reference inputs at audit_claim_validator_hash
///      with state == AuditValidated
///   2. The claim's claimer_did matches the agent_did receiving the bonus
///   3. Oracle signs (prevents replay — oracle tracks which claims have been rewarded)
pub fn validate_audit_claim_won_bonus(
  tx: Transaction,
  cfg: ReputationConfig,
  agent_did: ByteArray,
) -> Bool {
  // Find a validated Module 1 claim for this agent
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
              // claimer_did in Module 1 = the agent's identity identifier
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

/// Validate minting a history bonus for jury duty in Module 1.
///
/// MUST:
///   1. A Module 1 juror UTXO exists in reference inputs at audit_jury_pool_hash
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

/// The input that spends the target's StakeUTXO: the input at the exact
/// reputation address that holds the stake token of `target_did`.
fn target_stake_input(
  tx: Transaction,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
) -> Option<Input> {
  let address = exact_address(refs.reputation_policy_id)
  let token = derive_stake_token_name(target_did)
  list.find(
    tx.inputs,
    fn(input) {
      input.output.address == address && assets.quantity_of(
        input.output.value,
        refs.reputation_policy_id,
        token,
      ) == 1
    },
  )
}

/// True when the transaction spends the target's StakeUTXO. The reputation
/// validator then removes the lock of the closing challenge
/// (ReleaseChallenge or SlashStake).
pub fn spends_target_stake(
  tx: Transaction,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
) -> Bool {
  when target_stake_input(tx, refs, target_did) is {
    Some(_) -> True
    None -> False
  }
}

/// The datum of the target's StakeUTXO that the transaction spends. Fails
/// when the transaction does not spend it. At challenge creation the
/// reputation validator adds the lock of the challenge to this stake
/// (LockForChallenge); the payouts to the target (CapabilityVerified) and
/// the signature of RespondToChallenge use target_credential, so it must be
/// the owner_credential of this stake.
fn spent_target_stake(
  tx: Transaction,
  refs: CrossValidatorRefs,
  target_did: ByteArray,
) -> StakeDatum {
  expect Some(stake_input) = target_stake_input(tx, refs, target_did)
  expect InlineDatum(raw_stake) = stake_input.output.datum
  expect stake_datum: StakeDatum = raw_stake
  stake_datum
}

/// True when the two challenges have the same terms: the fields from
/// challenger_did to created_at are equal.
fn same_terms(a: ReputationChallengeDatum, b: ReputationChallengeDatum) -> Bool {
  and {
    a.challenger_did == b.challenger_did,
    a.challenger_credential == b.challenger_credential,
    a.target_did == b.target_did,
    a.target_credential == b.target_credential,
    a.challenged_capability == b.challenged_capability,
    a.stake_amount == b.stake_amount,
    a.evidence_hash == b.evidence_hash,
    a.evidence_uri == b.evidence_uri,
    a.created_at == b.created_at,
  }
}

/// True when the two challenges have the same terms and the same response:
/// all fields except `state` are equal.
fn same_terms_and_response(
  a: ReputationChallengeDatum,
  b: ReputationChallengeDatum,
) -> Bool {
  and {
    same_terms(a, b),
    a.counter_evidence_hash == b.counter_evidence_hash,
    a.counter_evidence_uri == b.counter_evidence_uri,
    a.response_submitted_at == b.response_submitted_at,
  }
}
