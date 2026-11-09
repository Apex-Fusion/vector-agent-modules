//// The spend handler: the general rules of SPEC §6, then the rules of the
//// action.

use aiken/collection/dict
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, OutputReference, Transaction}
use jury/case_rules
use jury/datum.{case_fields, juror_fields}
use jury/juror_rules
use jury/redeemers.{OnCase, OnJuror, SpendAction}
use jury/tx.{Own}
use jury/types.{JuryParams}

/// The spend handler of the jury. It reads the own datum from the own input,
/// not from the datum argument of the handler:
///
/// - the own input has an inline datum;
/// - its value holds exactly one token of the policy `J` (its payment
///   credential `Script(J)`), with quantity 1: the own token;
/// - the datum matches the redeemer: `OnJuror` needs a `Juror` datum and
///   `OnCase` a `Case` datum.
pub fn validate(
  params: JuryParams,
  redeemer: SpendAction,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  expect Some(input) = transaction.find_input(tx.inputs, own_ref)
  let output = input.output
  expect InlineDatum(datum) = output.datum
  expect Script(hash) = output.address.payment_credential
  expect [Pair(name, 1)] = dict.to_pairs(assets.tokens(output.value, hash))
  let own =
    Own {
      hash,
      name,
      lovelace: assets.lovelace_of(output.value),
      datum,
      reference: own_ref,
    }
  when redeemer is {
    OnJuror(action) ->
      juror_rules.validate(params, own, juror_fields(datum), action, tx)
    OnCase(action) ->
      case_rules.validate(params, own, case_fields(datum), action, tx)
  }
}
