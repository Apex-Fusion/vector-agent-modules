// Game 6: Governance Suggestion Engine — Proposal Multi-Validator
// Handles both minting (proposal tokens, activity tokens) and spending (proposal lifecycle)

use aiken/collection/list
use aiken/interval.{Finite}
use cardano/address.{Script as ScriptCredential}
use cardano/transaction.{OutputReference, Transaction}

use governance_suggestion/params
use governance_suggestion/proposal_validation
use governance_suggestion/types.{
  AdoptProposal, AmendProposal, ExpireProposal, ExpireStaleProposal,
  ExtendReview, GovernanceConfig, GovernanceCrossRefs, ProposalAction,
  ProposalDatum, RejectProposal, SpendActivity, SubmitProposal,
  WithdrawProposal,
}
use governance_suggestion/utils

// ============================================================================
// Multi-Validator: Minting Policy
// ============================================================================

/// Minting policy for proposal tokens and activity tracking tokens.
/// Same policy ID as the spend validator (multi-validator pattern).
/// Guarded: requires a spend from the proposal validator in the same tx.
validator proposal_mint(config: GovernanceConfig) {
  mint(redeemer: ProposalAction, _policy_id: ByteArray, tx: Transaction) {
    // Guard: a UTXO at the proposal spend validator must be spent in this tx.
    // This ensures the spend validator's full validation runs.
    // Uses GovernanceCrossRefs to find the proposal_spend hash (different from
    // this mint policy's own hash since they are separate validators).
    let refs = utils.get_cross_refs(config, tx.reference_inputs)
    let has_proposal_spend =
      list.any(
        tx.inputs,
        fn(input) {
          when input.output.address.payment_credential is {
            ScriptCredential(hash) -> hash == refs.proposal_validator_hash
            _ -> False
          }
        },
      )

    when redeemer is {
      SubmitProposal -> {
        // For submit, the proposal validator spend is consuming the proposer's
        // activity UTXO or a locked ProposalDatum UTXO.
        has_proposal_spend
      }
      _ -> {
        // Burning during finalization: spend validator must be running
        has_proposal_spend
      }
    }
  }

  else(_) {
    fail
  }
}

// ============================================================================
// Multi-Validator: Spend Validator
// ============================================================================

/// Spend validator for proposal UTXOs and activity tracking UTXOs.
validator proposal_spend(config: GovernanceConfig) {
  spend(
    datum_opt: Option<Data>,
    redeemer: ProposalAction,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    trace @"ENTER:proposal_spend"
    let gov_params =
      params.read_governance_params(config, tx.reference_inputs)
    trace @"OK:read_params"
    let refs =
      utils.get_cross_refs(config, tx.reference_inputs)
    trace @"OK:get_cross_refs"

    // Extract the current slot from validity range
    expect Some(current_slot) = get_lower_bound_slot(tx)
    trace @"OK:get_slot"

    when redeemer is {
      SubmitProposal -> {
        trace @"OK:redeemer_match"
        expect Some(raw_datum) = datum_opt
        trace @"OK:datum_some"
        expect datum: ProposalDatum = raw_datum
        trace @"OK:datum_parse"

        proposal_validation.validate_submit_proposal(
          config,
          refs,
          gov_params,
          datum,
          current_slot,
          tx.reference_inputs,
          tx.inputs,
          tx.outputs,
          tx.extra_signatories,
          tx.mint,
          own_ref,
        )
      }

      WithdrawProposal -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_withdraw_proposal(
          config,
          refs,
          datum,
          tx.inputs,
          tx.outputs,
          tx.extra_signatories,
          tx.mint,
        )
      }

      AmendProposal { new_hash, new_uri, incorporated } -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_amend_proposal(
          config,
          refs,
          gov_params,
          datum,
          new_hash,
          new_uri,
          incorporated,
          tx.outputs,
          tx.extra_signatories,
        )
      }

      AdoptProposal { reasoning_hash, reward_amount } -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_adopt_proposal(
          config,
          refs,
          gov_params,
          datum,
          reasoning_hash,
          reward_amount,
          tx.reference_inputs,
          tx.inputs,
          tx.outputs,
          tx.extra_signatories,
          tx.mint,
        )
      }

      RejectProposal { reasoning_hash } -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_reject_proposal(
          config,
          refs,
          datum,
          reasoning_hash,
          tx.reference_inputs,
          tx.inputs,
          tx.outputs,
          tx.extra_signatories,
          tx.mint,
        )
      }

      ExpireProposal -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_expire_proposal(
          config,
          refs,
          datum,
          current_slot,
          tx.inputs,
          tx.outputs,
          tx.mint,
        )
      }

      ExpireStaleProposal -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_expire_stale_proposal(
          config,
          refs,
          datum,
          tx.reference_inputs,
          tx.inputs,
          tx.outputs,
          tx.mint,
        )
      }

      ExtendReview { additional_slots } -> {
        expect Some(raw_datum) = datum_opt
        expect datum: ProposalDatum = raw_datum

        proposal_validation.validate_extend_review(
          config,
          gov_params,
          datum,
          additional_slots,
          tx.reference_inputs,
          tx.outputs,
          tx.extra_signatories,
        )
      }

      SpendActivity -> {
        // Activity UTXOs are validated by the proposal UTXO's evaluation
        // (via validate_finalize_activity in the Withdraw/Adopt/Reject/Expire case).
        // Guard: another input at the proposal validator address must also be spent.
        list.any(
          tx.inputs,
          fn(input) {
            input.output_reference != own_ref && when
              input.output.address.payment_credential
            is {
              ScriptCredential(hash) -> hash == refs.proposal_validator_hash
              _ -> False
            }
          },
        )
      }
    }
  }

  else(_) {
    fail
  }
}

// ============================================================================
// Helpers
// ============================================================================

/// Extract the lower bound slot from the transaction validity range.
/// Used for temporal checks (expiration, cooldown).
fn get_lower_bound_slot(tx: Transaction) -> Option<Int> {
  when tx.validity_range.lower_bound.bound_type is {
    Finite(slot) -> Some(slot)
    _ -> None
  }
}

