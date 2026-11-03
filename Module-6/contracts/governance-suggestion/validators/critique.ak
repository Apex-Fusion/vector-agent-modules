// Module 6: Self-Improvement Module — Critique & Endorsement Multi-Validator
// Handles minting (critique/endorsement tokens) and spending (critique/endorsement lifecycle)

use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference, Transaction}
use governance_suggestion/critique_validation
use governance_suggestion/params
use governance_suggestion/types.{
  BurnCritiqueToken, BurnEndorsementToken, CritiqueAction, CritiqueDatum,
  GovernanceConfig, GovernanceEndorsementAction, GovernanceEndorsementDatum,
  IncorporateCritique, MintCritiqueToken, MintEndorsementToken, RewardCritique,
  WithdrawCritique, WithdrawEndorsement,
}
use governance_suggestion/utils

// ============================================================================
// Multi-Validator: Minting Policy for Critique + Endorsement Tokens
// ============================================================================

/// Mint policy of the critique and endorsement tokens
/// (critique_validation.validate_critique_mint): the transaction spends a
/// critique or endorsement input, and it mints exactly the derived tokens of
/// the critiques and endorsements that it creates. The spend validator hashes
/// come from GovernanceCrossRefs, because they differ from this policy's hash.
validator critique_mint(config: GovernanceConfig) {
  mint(_redeemer: Data, policy_id: PolicyId, tx: Transaction) {
    let refs = utils.get_cross_refs(config, tx.reference_inputs)
    critique_validation.validate_critique_mint(refs, policy_id, tx)
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
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    let gov_params = params.read_governance_params(config, tx.reference_inputs)
    let refs = utils.get_cross_refs(config, tx.reference_inputs)
    expect Some(raw_datum) = datum_opt
    expect datum: CritiqueDatum = raw_datum

    when redeemer is {
      MintCritiqueToken ->
        // A new critique: the checks read the datum of the created output;
        // `datum` (the spent UTxO) only binds its owner.
        critique_validation.validate_submit_critique(
          config,
          refs,
          gov_params,
          datum,
          own_ref,
          tx,
        )

      WithdrawCritique ->
        critique_validation.validate_withdraw_critique(
          datum,
          tx.reference_inputs,
          refs,
          tx.extra_signatories,
        )

      IncorporateCritique ->
        // Only inside an amendment of the proposal that the critique names,
        // which lists this critique; the critique continues unchanged except
        // incorporated = True.
        critique_validation.validate_incorporate_critique(
          refs,
          datum,
          own_ref,
          tx,
        )

      RewardCritique { reward_share: _ } ->
        // Only inside the adoption of the proposal that the critique names;
        // the critique tokens are burned and the critic receives at least the
        // returned lovelace.
        critique_validation.validate_reward_critique(refs, datum, own_ref, tx)

      BurnCritiqueToken ->
        // Only inside the finalisation of the proposal that the critique
        // names, or of any proposal when the critic signs; the critique tokens
        // are burned and the critic receives at least the returned lovelace.
        critique_validation.validate_burn_critique(refs, datum, own_ref, tx)
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
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    let gov_params = params.read_governance_params(config, tx.reference_inputs)
    let refs = utils.get_cross_refs(config, tx.reference_inputs)
    expect Some(raw_datum) = datum_opt
    expect datum: GovernanceEndorsementDatum = raw_datum

    when redeemer is {
      MintEndorsementToken ->
        // A new endorsement: the checks read the datum of the created
        // output; `datum` (the spent UTxO) only binds its owner.
        critique_validation.validate_submit_endorsement(
          config,
          refs,
          gov_params,
          datum,
          own_ref,
          tx,
        )

      WithdrawEndorsement ->
        critique_validation.validate_withdraw_endorsement(
          datum,
          tx.extra_signatories,
        )

      BurnEndorsementToken ->
        // Only inside the finalisation of the proposal that the endorsement
        // names, or of any proposal when the endorser signs; the endorsement
        // tokens are burned and the endorser receives at least the returned
        // lovelace.
        critique_validation.validate_burn_endorsement(refs, datum, own_ref, tx)
    }
  }

  else(_) {
    fail
  }
}
