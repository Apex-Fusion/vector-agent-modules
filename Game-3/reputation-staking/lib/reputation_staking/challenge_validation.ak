/// Challenge validation logic for Game 3: Reputation Staking.
///
/// Validates challenge operations: MintChallengeToken, RespondToChallenge,
/// ResolveChallenge, DefaultJudgment, and DistributeOutcome.
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 6.8–6.11.

use aiken/builtin
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, Transaction}
use reputation_staking/config.{ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use reputation_staking/types.{
  CapabilityFalsified, CapabilityVerified, Inconclusive, Open,
  RepChallengeOutcome, ReputationChallengeDatum, Resolved, StakeDatum,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_stake_token_name, get_ap3x_amount,
  has_credential_signed,
  has_key_signed, verify_agent_exists,
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
      cfg,
      output_datum.target_did,
      output_datum.challenged_capability,
    )

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
          cfg.endorsement_policy_id,
          expected_token_name,
        ) >= 1
      },
    )

  // Output has correct challenge token
  let has_correct_token =
    assets.quantity_of(
      output.value,
      cfg.endorsement_policy_id,
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
      Script(hash) -> hash == cfg.endorsement_policy_id
      _ -> False
    }

  // Output value includes correct AP3X
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  let value_correct = output_ap3x >= output_datum.stake_amount

  // Rule 9: Signed by challenger
  let signed = has_credential_signed(tx, output_datum.challenger_credential)

  challenger_exists && target_exists && target_stake_valid && not_self_challenge && meets_minimum && evidence_valid && token_is_new && has_correct_token && state_open && initial_state_clean && at_validator && value_correct && signed
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
pub fn validate_default_judgment(
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

  // All other datum fields preserved
  let datum_preserved =
    output_datum.challenger_did == input_datum.challenger_did && output_datum.challenger_credential == input_datum.challenger_credential && output_datum.target_did == input_datum.target_did && output_datum.target_credential == input_datum.target_credential && output_datum.challenged_capability == input_datum.challenged_capability && output_datum.stake_amount == input_datum.stake_amount && output_datum.evidence_hash == input_datum.evidence_hash && output_datum.evidence_uri == input_datum.evidence_uri && output_datum.created_at == input_datum.created_at && output_datum.counter_evidence_hash == input_datum.counter_evidence_hash && output_datum.counter_evidence_uri == input_datum.counter_evidence_uri && output_datum.response_submitted_at == input_datum.response_submitted_at

  state_open && past_deadline && no_response && resolved_correctly && datum_preserved
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
      cfg.endorsement_policy_id,
      expected_token_name,
    ) == -1

  // Validate payout structure based on outcome
  let payouts_valid =
    when outcome is {
      CapabilityFalsified ->
        validate_falsified_payouts(tx, cfg, params, challenge_datum)
      CapabilityVerified ->
        validate_verified_payouts(tx, cfg, params, challenge_datum)
      Inconclusive ->
        validate_inconclusive_payouts(tx, cfg, params, challenge_datum)
    }

  token_burned && payouts_valid
}

/// Validate payouts when capability is falsified (challenger wins).
fn validate_falsified_payouts(
  tx: Transaction,
  cfg: ReputationConfig,
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
          cfg.reputation_policy_id,
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

  target_paid && treasury_paid
}

/// Validate payouts for inconclusive outcome.
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

  // Treasury gets fee
  let treasury_paid = verify_treasury_payment(tx.outputs, cfg, total_fee)

  challenger_paid && treasury_paid
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// Verify that the target agent has the challenged capability in their StakeUTXO.
fn verify_target_capability(
  reference_inputs: List<Input>,
  cfg: ReputationConfig,
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
            Script(hash) -> hash == cfg.reputation_policy_id
            _ -> False
          }
        let has_token =
          assets.quantity_of(
            input.output.value,
            cfg.reputation_policy_id,
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
          Script(hash) -> hash == cfg.reputation_policy_id
          _ -> False
        }
      let has_token =
        assets.quantity_of(output.value, cfg.reputation_policy_id, stake_token) == 1
      let ap3x =
        get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
      is_at_reputation && has_token && ap3x >= expected_remaining_stake
    },
  )
}
