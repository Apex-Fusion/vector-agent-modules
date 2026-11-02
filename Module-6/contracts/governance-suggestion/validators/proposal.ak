// Module 6: Self-Improvement Module — Proposal Multi-Validator
// Handles both minting (proposal tokens, activity tokens) and spending (proposal lifecycle)

use aiken/interval.{Finite}
use cardano/transaction.{OutputReference, Transaction}
use governance_suggestion/params
use governance_suggestion/proposal_validation
use governance_suggestion/types.{
  AdoptProposal, AmendProposal, ExpireProposal, ExpireStaleProposal,
  ExtendReview, GovernanceConfig, ProposalAction, ProposalDatum, RejectProposal,
  SpendActivity, SubmitProposal, WithdrawProposal,
}
use governance_suggestion/utils

// ============================================================================
// Multi-Validator: Minting Policy
// ============================================================================

/// Minting policy for proposal tokens and activity tracking tokens.
///
/// MUST: the transaction has exactly one proposal action
/// (proposal_validation.one_proposal_action): one input at the proposal
/// spend validator spent with an action other than SpendActivity. The
/// proposal spend validator runs for that input, and the action fixes the
/// exact set of tokens that the transaction mints or burns under this policy:
///   - SubmitProposal: utils.proposal_token_name(seed) and, for the first
///     proposal of the agent (the insertion of its node in the activity
///     list), utils.activity_token_name(proposer_did), each with quantity 1
///     (activity_tracking.validate_submit_activity);
///   - a finalisation: the burn of the token of the finalised proposal, and
///     nothing else;
///   - AmendProposal and ExtendReview: nothing.
/// The proposal_spend hash comes from GovernanceCrossRefs, because the two
/// validators have different hashes.
validator proposal_mint(config: GovernanceConfig) {
  mint(_redeemer: ProposalAction, _policy_id: ByteArray, tx: Transaction) {
    let refs = utils.get_cross_refs(config, tx.reference_inputs)
    proposal_validation.one_proposal_action(refs, tx)
  }

  else(_) {
    fail
  }
}

// ============================================================================
// Multi-Validator: Spend Validator
// ============================================================================

/// Spend validator for proposal UTXOs and the entries of the activity list
/// (the head and the nodes, activity_tracking).
///
/// Every redeemer needs a finite lower bound of the validity range and
/// exactly one proposal action in the transaction
/// (proposal_validation.one_proposal_action). Both rules come before the
/// redeemer branch.
validator proposal_spend(config: GovernanceConfig) {
  spend(
    datum_opt: Option<Data>,
    redeemer: ProposalAction,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    trace @"ENTER:proposal_spend"
    let gov_params = params.read_governance_params(config, tx.reference_inputs)
    let refs = utils.get_cross_refs(config, tx.reference_inputs)

    // Extract the current slot from validity range
    expect Some(current_slot) = get_lower_bound_slot(tx)
    expect Some(raw_datum) = datum_opt

    and {
      // One proposal action per transaction, for every redeemer
      proposal_validation.one_proposal_action(refs, tx),
      when redeemer is {
        SubmitProposal -> {
          expect spent: ProposalDatum = raw_datum
          proposal_validation.validate_submit_proposal(
            config,
            refs,
            gov_params,
            spent,
            own_ref,
            tx,
          )
        }

        WithdrawProposal -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_withdraw_proposal(
            refs,
            datum,
            own_ref,
            tx,
          )
        }

        AmendProposal { new_hash, new_uri, incorporated } -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_amend_proposal(
            config,
            refs,
            gov_params,
            datum,
            new_hash,
            new_uri,
            incorporated,
            own_ref,
            tx,
          )
        }

        AdoptProposal { reasoning_hash, reward_amount } -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_adopt_proposal(
            config,
            refs,
            gov_params,
            datum,
            reasoning_hash,
            reward_amount,
            own_ref,
            tx,
          )
        }

        RejectProposal { reasoning_hash } -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_reject_proposal(
            config,
            refs,
            datum,
            reasoning_hash,
            own_ref,
            tx,
          )
        }

        ExpireProposal -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_expire_proposal(
            refs,
            datum,
            current_slot,
            own_ref,
            tx,
          )
        }

        ExpireStaleProposal -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_expire_stale_proposal(
            refs,
            gov_params,
            datum,
            own_ref,
            tx,
          )
        }

        ExtendReview { additional_slots } -> {
          expect datum: ProposalDatum = raw_datum
          proposal_validation.validate_extend_review(
            config,
            refs,
            gov_params,
            datum,
            additional_slots,
            own_ref,
            tx,
          )
        }

        SpendActivity ->
          proposal_validation.validate_spend_activity(
            config,
            refs,
            raw_datum,
            own_ref,
            tx,
          )
      },
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
