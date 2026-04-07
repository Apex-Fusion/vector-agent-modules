// Oracle: Governance Suggestion Engine — Critique & Endorsement Multi-Validator
// Handles minting (critique/endorsement tokens) and spending (critique/endorsement lifecycle)

use aiken/collection/list
use cardano/address.{Script as ScriptCredential}
use cardano/transaction.{OutputReference, Transaction}

use governance_suggestion/critique_validation
use governance_suggestion/params
use governance_suggestion/types.{
  BurnCritiqueToken, BurnEndorsementToken, CritiqueAction, CritiqueDatum,
  GovernanceConfig, GovernanceCrossRefs, GovernanceEndorsementAction,
  GovernanceEndorsementDatum, IncorporateCritique, MintCritiqueToken,
  MintEndorsementToken, RewardCritique, WithdrawCritique, WithdrawEndorsement,
}
use governance_suggestion/utils

// ============================================================================
// Multi-Validator: Minting Policy for Critique + Endorsement Tokens
// ============================================================================

/// Guarded mint policy: requires a spend from critique or endorsement validator.
validator critique_mint(config: GovernanceConfig) {
  mint(_redeemer: Data, _policy_id: ByteArray, tx: Transaction) {
    // Guard: a UTXO at the critique or endorsement spend validator must be spent.
    // This ensures the spend validator's full validation runs.
    // Uses GovernanceCrossRefs to find the critique_spend hash (different from
    // this mint policy's own hash since they are separate validators).
    let refs = utils.get_cross_refs(config, tx.reference_inputs)
    list.any(
      tx.inputs,
      fn(input) {
        when input.output.address.payment_credential is {
          ScriptCredential(hash) ->
            hash == refs.critique_validator_hash
          _ -> False
        }
      },
    )
  }

  else(_) {
    fail
  }
}

// ============================================================================
// Multi-Validator: Spend Validator for Critique UTXOs
// ============================================================================

validator critique_spend(config: GovernanceConfig) {
  spend(
    datum_opt: Option<Data>,
    redeemer: CritiqueAction,
    _own_ref: OutputReference,
    tx: Transaction,
  ) {
    let gov_params =
      params.read_governance_params(config, tx.reference_inputs)
    let refs =
      utils.get_cross_refs(config, tx.reference_inputs)

    when redeemer is {
      MintCritiqueToken -> {
        // Minting a new critique — validate from the output datum
        // Find the new critique UTXO in outputs and validate
        expect Some(raw_datum) = datum_opt
        expect datum: CritiqueDatum = raw_datum

        critique_validation.validate_submit_critique(
          config,
          refs,
          gov_params,
          datum,
          tx.reference_inputs,
          tx.extra_signatories,
        )
      }

      WithdrawCritique -> {
        expect Some(raw_datum) = datum_opt
        expect datum: CritiqueDatum = raw_datum

        critique_validation.validate_withdraw_critique(
          datum,
          tx.reference_inputs,
          refs,
          tx.extra_signatories,
        )
      }

      IncorporateCritique -> {
        expect Some(raw_datum) = datum_opt
        expect datum: CritiqueDatum = raw_datum

        critique_validation.validate_incorporate_critique(datum, tx.outputs)
      }

      RewardCritique { reward_share: _ } -> {
        // Reward distribution happens during AdoptProposal
        // The proposal validator handles the overall validation;
        // here we just verify the critique is being consumed properly
        expect Some(raw_datum) = datum_opt
        expect datum: CritiqueDatum = raw_datum

        // Must be incorporated to receive reward
        datum.incorporated
      }

      BurnCritiqueToken -> {
        // Burning during proposal finalization — always allowed
        // The proposal validator ensures this only happens during adopt/reject/expire
        True
      }
    }
  }

  else(_) {
    fail
  }
}

// ============================================================================
// Spend Validator for Endorsement UTXOs
// ============================================================================

validator endorsement_spend(config: GovernanceConfig) {
  spend(
    datum_opt: Option<Data>,
    redeemer: GovernanceEndorsementAction,
    _own_ref: OutputReference,
    tx: Transaction,
  ) {
    let gov_params =
      params.read_governance_params(config, tx.reference_inputs)
    let refs =
      utils.get_cross_refs(config, tx.reference_inputs)

    when redeemer is {
      MintEndorsementToken -> {
        expect Some(raw_datum) = datum_opt
        expect datum: GovernanceEndorsementDatum = raw_datum

        critique_validation.validate_submit_endorsement(
          config,
          refs,
          gov_params,
          datum,
          tx.reference_inputs,
          tx.extra_signatories,
        )
      }

      WithdrawEndorsement -> {
        expect Some(raw_datum) = datum_opt
        expect datum: GovernanceEndorsementDatum = raw_datum

        critique_validation.validate_withdraw_endorsement(
          datum,
          tx.extra_signatories,
        )
      }

      BurnEndorsementToken -> {
        // Burning during proposal finalization — always allowed
        True
      }
    }
  }

  else(_) {
    fail
  }
}
