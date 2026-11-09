//// Juror rules: SPEC §6.4 to §6.8. The own datum is the `Juror` `j`.

use aiken/math
use cardano/transaction.{Transaction}
use jury/ballots.{has_entry, take_entry}
use jury/datum.{JurorFields, case_fields, juror_data}
use jury/identity.{identity_ok}
use jury/redeemers.{
  CaseClaim, CaseCommit, CaseReveal, Claim, Commit, Exit, JurorAction, OnCase,
  RequestExit, Reveal, Slash, TopUp,
}
use jury/time.{lower, window}
use jury/tx.{
  Own, burns, counterpart, inline_datum, pays_tagged, signed_by,
  single_jury_input, token_output,
}
use jury/types.{JuryParams, OpenVote}

/// The spend handler for a juror output.
pub fn validate(
  params: JuryParams,
  own: Own,
  j: JurorFields,
  action: JurorAction,
  tx: Transaction,
) -> Bool {
  when action is {
    Commit { case } -> commit(params, own, j, case, tx)
    Reveal { case } -> reveal(own, j, case, tx)
    Claim { case } -> claim(own, j, case, tx)
    Slash { case } -> slash(params, own, j, case, tx)
    RequestExit -> request_exit(params, own, j, tx)
    Exit -> exit(own, j, tx)
    TopUp -> top_up(own, j, tx)
  }
}

/// §6.4 `Commit { case }`.
fn commit(
  params: JuryParams,
  own: Own,
  j: JurorFields,
  case: ByteArray,
  tx: Transaction,
) -> Bool {
  // 2. The counterpart and its datum `Case`.
  let counter = counterpart(tx, own.hash, case)
  expect OnCase(CaseCommit { juror, .. }) = counter.action
  let c = case_fields(inline_datum(counter.input))
  // 4. `lo` and step 1 of the identity check (§5.8).
  let w = window(tx.validity_range)
  let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
  and {
    // 1.
    signed_by(tx, j.owner),
    // 2.
    juror == own.name,
    // 3.
    !has_entry(j.open, case),
    // 4.
    w.lo >= j.registered_at + params.eligibility_delay,
    j.exit_after == None,
    own.lovelace >= params.bond_min,
    // 5.
    lovelace == own.lovelace,
    datum == juror_data(
      JurorFields {
        ..j,
        open: [OpenVote { case, reveal_end: c.reveal_end }, ..j.open],
      },
    ),
    // 4. The identity check, last because it costs the most.
    identity_ok(params.identity_hash, j.agent, j.owner, w, tx),
  }
}

/// §6.5 `Reveal { case }`.
fn reveal(own: Own, j: JurorFields, case: ByteArray, tx: Transaction) -> Bool {
  // 2.
  let counter = counterpart(tx, own.hash, case)
  expect OnCase(CaseReveal { juror, .. }) = counter.action
  // 3.
  when take_entry(j.open, case) is {
    None -> False
    Some((_, rest)) -> {
      let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
      and {
        // 1.
        signed_by(tx, j.owner),
        // 2.
        juror == own.name,
        // 4.
        lovelace == own.lovelace,
        datum == juror_data(JurorFields { ..j, open: rest }),
      }
    }
  }
}

/// §6.6 `Claim { case }`.
fn claim(own: Own, j: JurorFields, case: ByteArray, tx: Transaction) -> Bool {
  // 2.
  let counter = counterpart(tx, own.hash, case)
  expect OnCase(CaseClaim { juror }) = counter.action
  let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
  and {
    // 1.
    signed_by(tx, j.owner),
    // 2.
    juror == own.name,
    // 3. The case handler checks the amount (§6.12 rule 5).
    lovelace >= own.lovelace,
    datum == own.datum,
  }
}

/// §6.7 `Slash { case }` (anybody).
fn slash(
  params: JuryParams,
  own: Own,
  j: JurorFields,
  case: ByteArray,
  tx: Transaction,
) -> Bool {
  // 1. Exactly one entry `e`.
  when take_entry(j.open, case) is {
    None -> False
    Some((e, rest)) -> {
      // 2. The slash never takes the juror output below `juror_floor`.
      let amount = math.min(params.slash, own.lovelace - params.juror_floor)
      let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
      and {
        // 1.
        lower(tx.validity_range) >= e.reveal_end,
        // 3.
        lovelace >= own.lovelace - amount,
        datum == juror_data(JurorFields { ..j, open: rest }),
        // 4.
        or {
          amount <= 0,
          pays_tagged(tx.outputs, params.treasury, own.reference, amount),
        },
        // 5.
        single_jury_input(tx.inputs, own.hash),
      }
    }
  }
}

/// §6.8 `RequestExit`.
fn request_exit(
  params: JuryParams,
  own: Own,
  j: JurorFields,
  tx: Transaction,
) -> Bool {
  // 1. Finite bounds and `hi - lo <= 3,600,000`.
  let w = window(tx.validity_range)
  let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
  and {
    // 1.
    signed_by(tx, j.owner),
    // 2.
    j.exit_after == None,
    // 3.
    lovelace == own.lovelace,
    datum == juror_data(
      JurorFields { ..j, exit_after: Some(w.hi + params.exit_notice) },
    ),
  }
}

/// §6.8 `Exit`.
fn exit(own: Own, j: JurorFields, tx: Transaction) -> Bool {
  and {
    // 1.
    signed_by(tx, j.owner),
    // 2.
    when j.exit_after is {
      Some(t) -> lower(tx.validity_range) >= t
      None -> False
    },
    // 3.
    j.open == [],
    // 4.
    burns(tx.mint, own.hash, own.name),
  }
}

/// §6.8 `TopUp`.
fn top_up(own: Own, j: JurorFields, tx: Transaction) -> Bool {
  let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
  and {
    // 1.
    signed_by(tx, j.owner),
    // 2.
    lovelace > own.lovelace,
    datum == own.datum,
  }
}
