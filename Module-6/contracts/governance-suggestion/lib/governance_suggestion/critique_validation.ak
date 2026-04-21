// Module 6: Self-Improvement Module — Critique & Endorsement Validation
// Spec reference: §5.7–5.9

use aiken/builtin
use aiken/collection/list
use cardano/address.{Script as ScriptCredential}
use cardano/transaction.{InlineDatum, Input, Output, OutputReference}

use governance_suggestion/types.{
  Amendment, CritiqueDatum, GovernanceConfig, GovernanceCrossRefs,
  GovernanceParams, GovernanceEndorsementDatum, ProposalDatum,
}
use governance_suggestion/utils

// ============================================================================
// §5.7 Submit Critique
// ============================================================================

/// Validate a new critique submission.
pub fn validate_submit_critique(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: CritiqueDatum,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  // 1. Critic has active DID
  let did_valid =
    utils.verify_active_did(config, datum.critic_did, reference_inputs)

  // 2. Referenced proposal exists and is active (Open or Amended)
  let proposal_valid =
    verify_proposal_is_active(refs, datum.proposal_ref, reference_inputs)

  // 3. No self-critique (except Amendment type — proposer can document own improvements)
  let self_critique_check =
    verify_no_self_critique(refs, datum, reference_inputs)

  // 4. Stake meets minimum
  let stake_valid = datum.stake_amount >= params.min_critique_stake

  // 5. Critique hash is 32 bytes
  let hash_valid = builtin.length_of_bytearray(datum.critique_hash) == 32

  // 8. incorporated = False
  let not_incorporated = !datum.incorporated

  // 9. Signed by critic
  let signer_valid =
    utils.verify_signer(datum.critic_credential, signatories)

  did_valid && proposal_valid && self_critique_check && stake_valid && hash_valid && not_incorporated && signer_valid
}

// ============================================================================
// §5.8 Withdraw Critique
// ============================================================================

/// Validate critique withdrawal.
pub fn validate_withdraw_critique(
  datum: CritiqueDatum,
  reference_inputs: List<Input>,
  refs: GovernanceCrossRefs,
  signatories: List<ByteArray>,
) -> Bool {
  // 2. Not incorporated
  let not_incorporated = !datum.incorporated

  // 3. Referenced proposal is still active
  let proposal_active =
    verify_proposal_is_active(refs, datum.proposal_ref, reference_inputs)

  // 6. Signed by critic
  let signer_valid =
    utils.verify_signer(datum.critic_credential, signatories)

  not_incorporated && proposal_active && signer_valid
}

// ============================================================================
// §5.9 Submit Endorsement
// ============================================================================

/// Validate a new endorsement submission.
pub fn validate_submit_endorsement(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: GovernanceEndorsementDatum,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  // 1. Endorser has active DID
  let did_valid =
    utils.verify_active_did(config, datum.endorser_did, reference_inputs)

  // 2. Referenced proposal exists and is active
  let proposal_valid =
    verify_proposal_is_active(refs, datum.proposal_ref, reference_inputs)

  // 3. No self-endorsement
  let no_self_endorse =
    verify_no_self_endorsement(refs, datum, reference_inputs)

  // 4. Stake meets minimum
  let stake_valid = datum.stake_amount >= params.min_governance_endorsement

  // 7. Signed by endorser
  let signer_valid =
    utils.verify_signer(datum.endorser_credential, signatories)

  did_valid && proposal_valid && no_self_endorse && stake_valid && signer_valid
}

// ============================================================================
// Withdraw Endorsement
// ============================================================================

/// Validate endorsement withdrawal. Anytime — no lock.
pub fn validate_withdraw_endorsement(
  datum: GovernanceEndorsementDatum,
  signatories: List<ByteArray>,
) -> Bool {
  utils.verify_signer(datum.endorser_credential, signatories)
}

// ============================================================================
// Incorporate Critique (called during AmendProposal)
// ============================================================================

/// Validate that a critique is being incorporated into a proposal amendment.
/// The critique's `incorporated` field must be set to True in the continuing output.
pub fn validate_incorporate_critique(
  datum: CritiqueDatum,
  outputs: List<Output>,
) -> Bool {
  // Must not already be incorporated
  let not_yet = !datum.incorporated

  // Verify continuing output with incorporated = True
  let output_valid =
    list.any(
      outputs,
      fn(output) {
        when output.datum is {
          InlineDatum(out_raw) -> {
            expect out_datum: CritiqueDatum = out_raw
            // Same critique but now incorporated
            out_datum.critic_did == datum.critic_did && out_datum.proposal_ref == datum.proposal_ref && out_datum.critique_hash == datum.critique_hash && out_datum.incorporated == True
          }
          _ -> False
        }
      },
    )

  not_yet && output_valid
}

// ============================================================================
// Internal Helpers
// ============================================================================

/// Verify that a proposal referenced by OutputReference is active (Open or Amended).
/// Checks reference inputs for the proposal UTXO.
fn verify_proposal_is_active(
  refs: GovernanceCrossRefs,
  _proposal_ref: OutputReference,
  reference_inputs: List<Input>,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      let is_at_proposal =
        when input.output.address.payment_credential is {
          ScriptCredential(hash) -> hash == refs.proposal_validator_hash
          _ -> False
        }
      if is_at_proposal {
        when input.output.datum is {
          InlineDatum(raw) -> {
            expect datum: ProposalDatum = raw
            utils.is_proposal_active(datum.state)
          }
          _ -> False
        }
      } else {
        False
      }
    },
  )
}

/// Verify no self-critique (critic_did != proposer_did).
/// Exception: Amendment type allows self-critique (proposer documenting own improvements).
fn verify_no_self_critique(
  refs: GovernanceCrossRefs,
  critique: CritiqueDatum,
  reference_inputs: List<Input>,
) -> Bool {
  // If it's an Amendment type, self-critique is allowed
  when critique.critique_type is {
    Amendment { .. } -> True
    _ -> {
      // Find the proposal and check the proposer DID
      let proposer_did =
        get_proposal_proposer_did(refs, critique.proposal_ref, reference_inputs)
      when proposer_did is {
        Some(did) -> critique.critic_did != did
        None -> False
      }
    }
  }
}

/// Verify no self-endorsement (endorser_did != proposer_did).
fn verify_no_self_endorsement(
  refs: GovernanceCrossRefs,
  endorsement: GovernanceEndorsementDatum,
  reference_inputs: List<Input>,
) -> Bool {
  let proposer_did =
    get_proposal_proposer_did(
      refs,
      endorsement.proposal_ref,
      reference_inputs,
    )
  when proposer_did is {
    Some(did) -> endorsement.endorser_did != did
    None -> False
  }
}

/// Get the proposer DID from a proposal referenced by OutputReference.
fn get_proposal_proposer_did(
  refs: GovernanceCrossRefs,
  _proposal_ref: OutputReference,
  reference_inputs: List<Input>,
) -> Option<ByteArray> {
  let found =
    list.find(
      reference_inputs,
      fn(input) {
        when input.output.address.payment_credential is {
          ScriptCredential(hash) -> hash == refs.proposal_validator_hash
          _ -> False
        }
      },
    )
  when found is {
    Some(input) ->
      when input.output.datum is {
        InlineDatum(raw) -> {
          expect datum: ProposalDatum = raw
          Some(datum.proposer_did)
        }
        _ -> None
      }
    None -> None
  }
}
