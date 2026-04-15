/// reset.ak — Shared logic for ResetStaleActiveCase
///
/// This module hosts the authoritative body of the ResetStaleActiveCase
/// rule. The jury_pool spend handler calls `check_reset_stale_active_case`
/// directly, so tests that exercise this function exercise the SAME code
/// path that the validator's redeemer dispatch invokes — closing the gap
/// between e2e tests and the validator's private handler logic.
///
/// DO NOT reimplement this logic elsewhere. If the rule changes, change it
/// here and both the validator dispatch and tests stay in sync.

use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, OutputReference, Transaction}
use adversarial_auditing/params.{ProtocolParams}
use adversarial_auditing/types.{
  AuditingConfig, CrossValidatorRefs, JurorDatum,
}
use adversarial_auditing/utils.{
  credential_signed, find_input, get_cross_refs,
}

/// Public wrapper used by both the jury_pool validator's spend dispatch
/// and by e2e tests. Resolves cross-validator refs from tx reference
/// inputs (same way the validator does) and runs the authoritative checks.
///
/// `_params` is accepted for forward-compatibility (param-dependent
/// extensions can land here without changing the validator signature).
pub fn check_reset_stale_active_case(
  config: AuditingConfig,
  _params: ProtocolParams,
  juror: JurorDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let refs = get_cross_refs(config, tx.reference_inputs)
  check_reset_stale_active_case_with_refs(refs, juror, own_ref, tx)
}

/// Variant that accepts pre-resolved CrossValidatorRefs. The validator
/// already resolves refs once in its spend branch, so it calls this form
/// directly to avoid double-resolution.
pub fn check_reset_stale_active_case_with_refs(
  refs: CrossValidatorRefs,
  juror: JurorDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // (1) must currently be stuck
  expect Some(active_token_name) = juror.active_case

  // (2) no live challenge UTxO with this token is referenced
  let challenge_still_live =
    list.any(tx.reference_inputs, fn(inp) {
      let at_challenge_addr =
        when inp.output.address.payment_credential is {
          Script(h) -> h == refs.challenge_validator_hash
          _ -> False
        }
      let has_token =
        assets.quantity_of(inp.output.value, refs.challenge_validator_hash, active_token_name) == 1
      at_challenge_addr && has_token
    })
  let challenge_absent = !challenge_still_live

  // (3) juror signs
  let signed = credential_signed(tx, juror.juror_credential)

  // Find this juror's input
  let this_input = find_input(tx.inputs, own_ref)

  // Expect single juror NFT on input
  let juror_token_name =
    when this_input is {
      Some(inp) ->
        when dict.to_pairs(assets.tokens(inp.output.value, refs.jury_pool_hash)) is {
          [Pair(tn, 1)] -> Some(tn)
          _ -> None
        }
      None -> None
    }

  // Continuing output at jury_pool: same juror NFT, active_case cleared,
  // commitment + revealed cleared, bond preserved, other fields unchanged.
  let output_ok =
    when juror_token_name is {
      None -> False
      Some(tn) ->
        list.any(tx.outputs, fn(o) {
          let at_pool =
            when o.address.payment_credential is {
              Script(h) -> h == refs.jury_pool_hash
              _ -> False
            }
          let has_this_juror_token =
            assets.quantity_of(o.value, refs.jury_pool_hash, tn) == 1
          when o.datum is {
            InlineDatum(raw) -> {
              expect updated: JurorDatum = raw
              and {
                at_pool,
                has_this_juror_token,
                updated.juror_did == juror.juror_did,
                updated.juror_credential == juror.juror_credential,
                updated.bond_amount == juror.bond_amount,
                updated.cases_resolved == juror.cases_resolved,
                updated.majority_votes == juror.majority_votes,
                updated.registered_at == juror.registered_at,
                updated.active_case == None,
                updated.vote_commitment == None,
                updated.revealed_verdict == None,
              }
            }
            _ -> False
          }
        })
    }

  and {
    challenge_absent,
    signed,
    output_ok,
  }
}
