//// Case rules: SPEC §6.9 to §6.13. The own datum is the `Case` `c`.

use aiken/collection/list
use aiken/primitive/bytearray
use cardano/assets
use cardano/transaction.{Transaction}
use jury/ballots.{
  has_agent, is_revealed, pay_status, reveal_status, update_ballot,
}
use jury/commit.{commit_hash}
use jury/datum.{CaseFields, ballot_list, case_data, juror_fields, with_ballots}
use jury/outcome.{add_vote, outcome, votes}
use jury/redeemers.{
  CaseAction, CaseClaim, CaseCommit, CaseReveal, Claim, Close, Commit, Finalize,
  OnJuror, Reveal,
}
use jury/time.{lower, upper}
use jury/tx.{
  Own, burns, counterpart, inline_datum, pays_tagged, single_jury_input,
  token_output,
}
use jury/types.{Ballot, CaseState, Committed, JuryParams, Open, Settling, Vote}

/// The spend handler for a case output.
pub fn validate(
  params: JuryParams,
  own: Own,
  c: CaseFields,
  action: CaseAction,
  tx: Transaction,
) -> Bool {
  when action is {
    CaseCommit { juror, commit } -> case_commit(own, c, juror, commit, tx)
    CaseReveal { juror, vote, salt, record_hash } ->
      case_reveal(own, c, juror, vote, salt, record_hash, tx)
    Finalize { result } -> finalize(params, own, c, result, tx)
    CaseClaim { juror } -> case_claim(own, c, juror, tx)
    Close -> close(params, own, c, tx)
  }
}

fn is_open(state: CaseState) -> Bool {
  when state is {
    Open -> True
    Settling { .. } -> False
  }
}

/// §6.9 `CaseCommit { juror, commit }`.
fn case_commit(
  own: Own,
  c: CaseFields,
  juror: ByteArray,
  commit: ByteArray,
  tx: Transaction,
) -> Bool {
  // 2. The counterpart and its datum `Juror`.
  let counter = counterpart(tx, own.hash, juror)
  expect OnJuror(Commit { case }) = counter.action
  let j = juror_fields(inline_datum(counter.input))
  let current = ballot_list(c)
  let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
  and {
    // 1.
    is_open(c.state),
    upper(tx.validity_range) <= c.commit_end,
    // 2.
    case == own.name,
    // 3.
    j.agent != c.claimer,
    j.agent != c.auditor,
    // 4.
    !has_agent(current, j.agent),
    // 5.
    bytearray.length(commit) == 32,
    // 6.
    lovelace >= own.lovelace,
    datum == case_data(
      with_ballots(
        c,
        [Ballot { agent: j.agent, juror, commit, status: Committed }, ..current],
      ),
    ),
  }
}

/// §6.10 `CaseReveal { juror, vote, salt, record_hash }`.
fn case_reveal(
  own: Own,
  c: CaseFields,
  juror: ByteArray,
  vote: Vote,
  salt: ByteArray,
  record_hash: ByteArray,
  tx: Transaction,
) -> Bool {
  // 2.
  let counter = counterpart(tx, own.hash, juror)
  expect OnJuror(Reveal { case }) = counter.action
  // 3. Exactly one ballot `b` with `juror`, and it is `Committed`.
  when update_ballot(ballot_list(c), juror, reveal_status(vote)) is {
    None -> False
    Some((b, revealed)) -> {
      let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
      and {
        // 1.
        is_open(c.state),
        lower(tx.validity_range) >= c.commit_end,
        upper(tx.validity_range) <= c.reveal_end,
        // 2.
        case == own.name,
        // 4.
        bytearray.length(salt) == 32,
        bytearray.length(record_hash) == 32,
        // 5.
        b.commit == commit_hash(own.name, b.agent, vote, salt, record_hash),
        // 6.
        lovelace >= own.lovelace,
        datum == case_data(
          CaseFields {
            ..with_ballots(c, revealed),
            tally: add_vote(c.tally, vote),
          },
        ),
      }
    }
  }
}

/// §6.11 `Finalize { result }` (anybody). The rule reads the tally and not
/// the ballots.
fn finalize(
  params: JuryParams,
  own: Own,
  c: CaseFields,
  result: Vote,
  tx: Transaction,
) -> Bool {
  let n = votes(c.tally)
  and {
    // 1.
    is_open(c.state),
    lower(tx.validity_range) >= c.reveal_end,
    // 2.
    result == outcome(params, c.tally),
    // 3.
    single_jury_input(tx.inputs, own.hash),
    // 4.
    if c.fee == 0 || n == 0 || c.fee < n {
      and {
        burns(tx.mint, own.hash, own.name),
        pays_tagged(tx.outputs, c.refund_to, own.reference, own.lovelace),
      }
    } else {
      let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
      and {
        lovelace >= own.lovelace,
        datum == case_data(
          CaseFields { ..c, state: Settling { share: c.fee / n } },
        ),
      }
    },
  }
}

/// §6.12 `CaseClaim { juror }`.
fn case_claim(
  own: Own,
  c: CaseFields,
  juror: ByteArray,
  tx: Transaction,
) -> Bool {
  // 1.
  when c.state is {
    Open -> False
    Settling { share } -> {
      // 2.
      let counter = counterpart(tx, own.hash, juror)
      expect OnJuror(Claim { case }) = counter.action
      // 3. Exactly one ballot with `juror`, and it is `Revealed`.
      when update_ballot(ballot_list(c), juror, pay_status) is {
        None -> False
        Some((_, paid)) -> {
          let (lovelace, datum) = token_output(tx.outputs, own.hash, own.name)
          let (juror_lovelace, _) = token_output(tx.outputs, own.hash, juror)
          and {
            // 2.
            case == own.name,
            // 4.
            lovelace >= own.lovelace - share,
            datum == case_data(with_ballots(c, paid)),
            // 5.
            juror_lovelace >= assets.lovelace_of(counter.input.output.value) + share,
          }
        }
      }
    }
  }
}

/// §6.13 `Close` (anybody).
fn close(params: JuryParams, own: Own, c: CaseFields, tx: Transaction) -> Bool {
  // 1.
  when c.state is {
    Open -> False
    Settling { .. } -> and {
        // 2.
        or {
          !list.any(ballot_list(c), is_revealed),
          lower(tx.validity_range) >= c.reveal_end + params.claim_period,
        },
        // 3.
        single_jury_input(tx.inputs, own.hash),
        // 4.
        burns(tx.mint, own.hash, own.name),
        pays_tagged(tx.outputs, c.refund_to, own.reference, own.lovelace),
      }
  }
}
