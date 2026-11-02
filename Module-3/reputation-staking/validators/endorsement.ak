/// Endorsement Validator — Multi-Validator
///
/// Controls endorsement and challenge lifecycle:
///   - Minting policy: MintEndorsementToken, BurnEndorsementToken,
///                     MintChallengeToken, BurnChallengeToken
///   - Spend validator: Endorsement actions + Challenge actions
///
/// Because both handlers share the same script hash:
///   policy_id == script_hash == payment_credential of the endorsement address

use aiken/interval.{Finite}
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Input, Output, OutputReference, Transaction}
use reputation_staking/challenge_validation
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig, get_cross_refs}
use reputation_staking/endorsement_validation
use reputation_staking/params.{ProtocolParams, find_protocol_params}
use reputation_staking/time
use reputation_staking/types.{
  BurnChallengeToken, BurnEndorsementToken, CapabilityFalsified,
  Challenge, ChallengeSpend, DefaultJudgment,
  DistributeOutcome, Endorsement, EndorsementDatum,
  EndorsementMintAction,
  EndorsementSpend, EndorsementValidatorAction, EndorsementValidatorDatum,
  Escalated, EscalateToAudit, ExpireChallenge, IncreaseEndorsement,
  MintChallengeToken, MintEndorsementToken, Open, ReputationChallengeDatum,
  ResolveChallenge, ResolveEscalation, Resolved, RespondToChallenge,
  SlashEndorsement,
  WithdrawChallenge, WithdrawEndorsement, challenge_token_prefix,
  endorsement_token_prefix,
}
use reputation_staking/utils.{
  count_script_inputs, derive_challenge_token_name, derive_endorsement_token_name,
  get_ap3x_amount, has_credential_signed, has_script_input, starts_with,
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers (must come before validator for Aiken parser)
// ──────────────────────────────────────────────────────────────────────────────

/// Extract the lower bound of the validity range.
fn get_validity_lower_bound(tx: Transaction) -> Option<Int> {
  when tx.validity_range.lower_bound.bound_type is {
    Finite(slot) -> Some(slot)
    _ -> None
  }
}

/// Find an endorsement output at the endorsement validator.
fn find_endorsement_output(
  tx: Transaction,
  endorsement_policy_id: PolicyId,
) -> Option<Output> {
  list.find(
    tx.outputs,
    fn(output) {
      let is_at_endorsement =
        when output.address.payment_credential is {
          Script(hash) -> hash == endorsement_policy_id
          _ -> False
        }
      let has_endorsement_token =
        list.any(
          assets.flatten(output.value),
          fn(entry) {
            let (pid, name, qty) = entry
            pid == endorsement_policy_id && starts_with(name,endorsement_token_prefix) && qty == 1
          },
        )
      is_at_endorsement && has_endorsement_token
    },
  )
}

/// Find a challenge output at the endorsement validator.
fn find_challenge_output(
  tx: Transaction,
  endorsement_policy_id: PolicyId,
) -> Option<Output> {
  list.find(
    tx.outputs,
    fn(output) {
      let is_at_endorsement =
        when output.address.payment_credential is {
          Script(hash) -> hash == endorsement_policy_id
          _ -> False
        }
      let has_challenge_token =
        list.any(
          assets.flatten(output.value),
          fn(entry) {
            let (pid, name, qty) = entry
            pid == endorsement_policy_id && starts_with(name,challenge_token_prefix) && qty == 1
          },
        )
      is_at_endorsement && has_challenge_token
    },
  )
}

/// Find the continuing endorsement output for a specific endorser-target pair.
fn find_continuing_endorsement(
  tx: Transaction,
  endorsement_policy_id: PolicyId,
  endorser_did: ByteArray,
  target_did: ByteArray,
) -> Option<Output> {
  let token_name = derive_endorsement_token_name(endorser_did, target_did)
  list.find(
    tx.outputs,
    fn(output) {
      let is_at_endorsement =
        when output.address.payment_credential is {
          Script(hash) -> hash == endorsement_policy_id
          _ -> False
        }
      let has_token =
        assets.quantity_of(output.value, endorsement_policy_id, token_name) == 1
      is_at_endorsement && has_token
    },
  )
}

/// Find the continuing challenge output for a specific challenge.
fn find_continuing_challenge(
  tx: Transaction,
  endorsement_policy_id: PolicyId,
  challenger_did: ByteArray,
  target_did: ByteArray,
  capability: ByteArray,
) -> Option<Output> {
  let token_name =
    derive_challenge_token_name(challenger_did, target_did, capability)
  list.find(
    tx.outputs,
    fn(output) {
      let is_at_endorsement =
        when output.address.payment_credential is {
          Script(hash) -> hash == endorsement_policy_id
          _ -> False
        }
      let has_token =
        assets.quantity_of(output.value, endorsement_policy_id, token_name) == 1
      is_at_endorsement && has_token
    },
  )
}

/// The input that the transaction spends at `own_ref`.
fn find_own_input(own_ref: OutputReference, tx: Transaction) -> Input {
  expect Some(own_input) =
    list.find(tx.inputs, fn(i) { i.output_reference == own_ref })
  own_input
}

/// True when the continuing challenge output stays at the address of the
/// spent challenge and keeps its AP3X, less at most `allowed_decrease`.
fn keeps_challenge_value(
  cfg: ReputationConfig,
  own_input: Input,
  output: Output,
  allowed_decrease: Int,
) -> Bool {
  let input_ap3x =
    get_ap3x_amount(
      own_input.output.value,
      cfg.ap3x_policy_id,
      cfg.ap3x_asset_name,
    )
  let output_ap3x =
    get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
  output.address == own_input.output.address && output_ap3x >= input_ap3x - allowed_decrease
}

/// The AP3X that the outputs to `credential` hold in total.
fn ap3x_paid_to(
  cfg: ReputationConfig,
  outputs: List<Output>,
  credential: address.Credential,
) -> Int {
  list.foldl(
    outputs,
    0,
    fn(output, total) {
      if output.address.payment_credential == credential {
        total + get_ap3x_amount(
          output.value,
          cfg.ap3x_policy_id,
          cfg.ap3x_asset_name,
        )
      } else {
        total
      }
    },
  )
}

/// The AP3X that the outputs at the protocol treasury hold in total.
fn ap3x_paid_to_treasury(cfg: ReputationConfig, outputs: List<Output>) -> Int {
  ap3x_paid_to(cfg, outputs, Script(cfg.treasury_script_hash))
}

/// Validate endorsement slashing triggered by a resolved challenge.
///
/// F-A1 fix: Read challenge from REFERENCE INPUTS (not consumed inputs) to
/// prevent fake-datum exploit. Verify the challenge UTxO is at the endorsement
/// validator address and carries a valid challenge token. Parse as the
/// EndorsementValidatorDatum::Challenge wrapper for type safety.
///
/// slash = stake_amount × slash_rate_endorser, remaining = stake_amount − slash,
/// fee = slash × protocol_fee_rate.
///   - The challenger receives at least slash − fee AP3X and the treasury at
///     least fee AP3X (MODULE-3 spec: the challenger receives the endorser
///     slashes less the protocol fee).
///   - remaining >= min_endorsement: one output continues the endorsement at
///     the same address with the endorsement token, the same datum except
///     stake_amount = remaining, and at least remaining AP3X.
///   - remaining < min_endorsement: the endorsement token is burned and the
///     endorser receives at least remaining AP3X.
/// When the challenger and the endorser have the same credential, the outputs
/// to that credential hold both amounts in total.
fn validate_endorsement_slash(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  own_input: Input,
  endorsement_datum: EndorsementDatum,
  challenge_ref: OutputReference,
) -> Bool {
  // F-A1 fix: Read challenge from reference inputs (not consumed inputs)
  expect Some(challenge_input) =
    list.find(
      tx.reference_inputs,
      fn(input) { input.output_reference == challenge_ref },
    )

  // F-A1 fix: Verify challenge is at endorsement validator address
  expect Script(challenge_hash) =
    challenge_input.output.address.payment_credential
  let at_endorsement = challenge_hash == refs.endorsement_policy_id

  // F-A1 fix: Verify challenge token present (proves legitimate minting flow)
  let has_challenge_token =
    list.any(
      assets.flatten(challenge_input.output.value),
      fn(entry) {
        let (pid, name, _qty) = entry
        pid == refs.endorsement_policy_id && starts_with(
          name,
          challenge_token_prefix,
        )
      },
    )

  // F-A1 fix: Parse as EndorsementValidatorDatum wrapper for type safety
  expect InlineDatum(raw_challenge) = challenge_input.output.datum
  expect Challenge(challenge_datum): EndorsementValidatorDatum = raw_challenge

  let same_target = challenge_datum.target_did == endorsement_datum.target_did
  let is_falsified =
    challenge_datum.state == Resolved { outcome: CapabilityFalsified }
  let capability_endorsed =
    list.has(
      endorsement_datum.endorsed_capabilities,
      challenge_datum.challenged_capability,
    )

  let slash_amount =
    endorsement_datum.stake_amount * params.slash_rate_endorser / 10_000
  let remaining = endorsement_datum.stake_amount - slash_amount
  let fee = slash_amount * params.protocol_fee_rate / 10_000
  let token_name =
    derive_endorsement_token_name(
      endorsement_datum.endorser_did,
      endorsement_datum.target_did,
    )
  let continues = remaining >= params.min_endorsement

  let output_valid =
    if continues {
      let expected_datum: Data =
        Endorsement(EndorsementDatum { ..endorsement_datum, stake_amount: remaining })
      list.any(
        tx.outputs,
        fn(output) {
          // F-NEW-4 fix: Verify AP3X value matches datum claim
          let ap3x =
            get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
          output.address == own_input.output.address && assets.quantity_of(
            output.value,
            refs.endorsement_policy_id,
            token_name,
          ) == 1 && output.datum == InlineDatum(expected_datum) && ap3x >= remaining
        },
      )
    } else {
      assets.quantity_of(tx.mint, refs.endorsement_policy_id, token_name) == -1
    }

  // Slashed AP3X to the challenger (less the fee) and the fee to the treasury.
  // When the endorsement closes, the endorser receives the remaining AP3X.
  let challenger_due = slash_amount - fee
  let endorser_due =
    if continues {
      0
    } else {
      remaining
    }
  let payouts_valid =
    if challenge_datum.challenger_credential == endorsement_datum.endorser_credential {
      ap3x_paid_to(cfg, tx.outputs, challenge_datum.challenger_credential) >= challenger_due + endorser_due
    } else {
      ap3x_paid_to(cfg, tx.outputs, challenge_datum.challenger_credential) >= challenger_due && ap3x_paid_to(
        cfg,
        tx.outputs,
        endorsement_datum.endorser_credential,
      ) >= endorser_due
    }
  let treasury_paid = fee <= 0 || ap3x_paid_to_treasury(cfg, tx.outputs) >= fee

  at_endorsement && has_challenge_token && same_target && is_falsified && capability_endorsed && output_valid && payouts_valid && treasury_paid
}

/// Validate challenge withdrawal.
///
/// The upper bound of the validity range is before
/// created_at + challenge_response_deadline.
fn validate_challenge_withdrawal(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  challenge_datum: ReputationChallengeDatum,
) -> Bool {
  let state_open = challenge_datum.state == Open
  // F-NEW-8 fix: Strict less-than to avoid race with respond at deadline slot
  let before_deadline =
    when time.upper_bound(tx) is {
      Some(hi) ->
        hi < challenge_datum.created_at + params.challenge_response_deadline
      None -> False
    }
  let no_response = challenge_datum.response_submitted_at == 0
  let token_name =
    derive_challenge_token_name(
      challenge_datum.challenger_did,
      challenge_datum.target_did,
      challenge_datum.challenged_capability,
    )
  let token_burned =
    assets.quantity_of(
      tx.mint,
      refs.endorsement_policy_id,
      token_name,
    ) == -1
  let signed = has_credential_signed(tx, challenge_datum.challenger_credential)

  // Verify AP3X returned to challenger
  let challenger_paid =
    list.any(
      tx.outputs,
      fn(output) {
        let is_to_challenger =
          output.address.payment_credential == challenge_datum.challenger_credential
        let ap3x =
          get_ap3x_amount(output.value, cfg.ap3x_policy_id, cfg.ap3x_asset_name)
        is_to_challenger && ap3x >= challenge_datum.stake_amount
      },
    )

  state_open && before_deadline && no_response && token_burned && signed && challenger_paid
}

// ──────────────────────────────────────────────────────────────────────────────
// Validator
// ──────────────────────────────────────────────────────────────────────────────

validator endorsement(cfg: ReputationConfig) {
  /// Minting policy: controls endorsement and challenge token lifecycle.
  mint(
    redeemer: EndorsementMintAction,
    policy_id: PolicyId,
    tx: Transaction,
  ) {
    let params =
      find_protocol_params(
        tx.reference_inputs,
        cfg.params_script_hash,
        cfg.params_token_policy,
      )
    let flat_mint = assets.flatten(tx.mint)

    // Look up cross-validator refs for cross-reference checks
    let refs = get_cross_refs(cfg, tx.reference_inputs)

    // F1 fix: Burn handlers require a script input to prevent orphan burns
    let script_input_exists = has_script_input(tx, policy_id)

    when redeemer is {
      MintEndorsementToken -> {
        // Use policy_id (== own hash) for self-referential checks
        expect Some(output) =
          find_endorsement_output(tx, policy_id)
        expect InlineDatum(raw_out) = output.datum
        expect Endorsement(out_datum): EndorsementValidatorDatum = raw_out
        endorsement_validation.validate_mint_endorsement(
          tx, cfg, refs, params, out_datum, output,
        )
      }
      BurnEndorsementToken ->
        // F1 fix: Require script input to prevent orphan burns
        script_input_exists && list.any(
          flat_mint,
          fn(entry) {
            let (pid, name, qty) = entry
            pid == policy_id && starts_with(name,endorsement_token_prefix) && qty == -1
          },
        )
      MintChallengeToken -> {
        expect Some(output) =
          find_challenge_output(tx, policy_id)
        expect InlineDatum(raw_out) = output.datum
        expect Challenge(out_datum): EndorsementValidatorDatum = raw_out
        challenge_validation.validate_mint_challenge(
          tx, cfg, refs, params, out_datum, output,
        )
      }
      BurnChallengeToken ->
        // F1 fix: Require script input to prevent orphan burns
        script_input_exists && list.any(
          flat_mint,
          fn(entry) {
            let (pid, name, qty) = entry
            pid == policy_id && starts_with(name,challenge_token_prefix) && qty == -1
          },
        )
    }
  }

  /// Spend validator: controls endorsement and challenge UTXO lifecycle.
  spend(
    datum: Option<EndorsementValidatorDatum>,
    redeemer: EndorsementValidatorAction,
    own_utxo: OutputReference,
    tx: Transaction,
  ) {
    expect Some(validator_datum) = datum
    let params =
      find_protocol_params(
        tx.reference_inputs,
        cfg.params_script_hash,
        cfg.params_token_policy,
      )
    expect Some(current_slot) = get_validity_lower_bound(tx)

    // Derive own script hash at runtime (no circular dependency)
    let own_input = find_own_input(own_utxo, tx)
    expect Script(own_hash) = own_input.output.address.payment_credential

    // Look up cross-validator refs from authenticated reference input
    let refs = get_cross_refs(cfg, tx.reference_inputs)

    // F-NEW-1 fix: Prevent double satisfaction — only one script input allowed
    expect count_script_inputs(tx, own_hash) == 1

    when (validator_datum, redeemer) is {
      (Endorsement(endorsement_datum), EndorsementSpend(action)) ->
        when action is {
          IncreaseEndorsement -> {
            expect Some(output) =
              find_continuing_endorsement(
                tx,
                own_hash,
                endorsement_datum.endorser_did,
                endorsement_datum.target_did,
              )
            expect InlineDatum(raw_out) = output.datum
            expect Endorsement(out_datum): EndorsementValidatorDatum = raw_out
            endorsement_validation.validate_increase_endorsement(
              tx, cfg, refs, endorsement_datum, out_datum, output,
            )
          }
          WithdrawEndorsement ->
            endorsement_validation.validate_withdraw_endorsement(
              tx, cfg, refs, params, endorsement_datum, current_slot,
            )
          SlashEndorsement { challenge_ref } ->
            validate_endorsement_slash(
              tx, cfg, refs, params, own_input, endorsement_datum, challenge_ref,
            )
        }

      (Challenge(challenge_datum), ChallengeSpend(action)) -> {
        // The challenge output that continues this challenge, when the action
        // keeps the challenge open. It stays at the same address and keeps
        // the challenge stake.
        let continuing_challenge =
          fn(allowed_decrease: Int) {
            expect Some(output) =
              find_continuing_challenge(
                tx,
                own_hash,
                challenge_datum.challenger_did,
                challenge_datum.target_did,
                challenge_datum.challenged_capability,
              )
            expect keeps_challenge_value(cfg, own_input, output, allowed_decrease)
            expect InlineDatum(raw_out) = output.datum
            expect Challenge(out_datum): EndorsementValidatorDatum = raw_out
            out_datum
          }

        when action is {
          WithdrawChallenge ->
            validate_challenge_withdrawal(tx, cfg, refs, params, challenge_datum)
          RespondToChallenge { counter_evidence_hash, counter_evidence_uri } ->
            challenge_validation.validate_respond_to_challenge(
              tx, params, challenge_datum, continuing_challenge(0),
              counter_evidence_hash, counter_evidence_uri,
            )
          EscalateToAudit -> {
            let out_datum = continuing_challenge(0)
            // Extract the audit_claim_ref from the output's Escalated state
            expect Escalated { audit_claim_ref } = out_datum.state
            challenge_validation.validate_escalate_to_audit(
              tx, cfg, params, challenge_datum, out_datum, audit_claim_ref,
            )
          }
          ResolveEscalation ->
            challenge_validation.validate_resolve_escalation(
              tx, cfg, challenge_datum, continuing_challenge(0),
            )
          ResolveChallenge { outcome } ->
            challenge_validation.validate_resolve_challenge(
              tx, cfg, challenge_datum, continuing_challenge(0), outcome,
            )
          DefaultJudgment -> {
            // The default judgment fee may leave the challenge output.
            let fee =
              challenge_datum.stake_amount * params.default_judgment_fee / 10_000
            challenge_validation.validate_default_judgment(
              tx, cfg, challenge_datum, continuing_challenge(fee), params,
              current_slot,
            )
          }
          DistributeOutcome ->
            challenge_validation.validate_distribute_outcome(
              tx, cfg, refs, params, challenge_datum,
            )
          ExpireChallenge ->
            challenge_validation.validate_expire_challenge(
              tx, cfg, refs, params, challenge_datum,
            )
        }
      }

      _ -> fail @"Datum and redeemer type mismatch"
    }
  }

  else(_ctx) {
    fail @"Unsupported script purpose"
  }
}
