//// SPEC §6.11 case `Finalize { result }` and §6.13 case `Close` (anybody):
//// one passing and one failing transaction per rule.
////
//// The base finalization: the case of `finalize_case` (tally 1/2/0, no fee)
//// is finalized one minute after `reveal_end` with `AuditorWins`; the case
//// token is burned and the refund output gets all lovelace. The base close:
//// the case of `paid_case` (all revealed ballots paid) is closed one day
//// after `reveal_end`.

use aiken/interval
use cardano/assets
use cardano/transaction.{DatumHash, NoDatum, Output, Transaction}
use jury/datum.{CaseFields}
use jury/redeemers.{Burn, Close, Finalize, OnCase}
use jury/types.{AuditorWins, ClaimerWins, Inconclusive, Settling, Tally, Vote}
use jury_tests/fixtures.{
  burn_tx, case_input, case_input_at, case_lovelace, case_output, case_ref,
  case_ref_b, case_token, case_token_b, close_tx, day, fee_input, finalize_case,
  finalize_tx, late_range, minute, new_case, paid, paid_case, params, range,
  refund_address, reveal_end, revealed, settling, share, spend_ok, stranger,
  tagged_output,
}

fn case_ok(tx: Transaction) -> Bool {
  spend_ok(tx, case_ref())
}

fn base_finalize() -> Transaction {
  finalize_tx(finalize_case(), case_lovelace, AuditorWins)
}

/// The finalization of a case with this tally and fee.
fn finalize_with(tally: Tally, fee: Int, result: Vote) -> Transaction {
  finalize_tx(
    CaseFields { ..finalize_case(), tally, fee },
    case_lovelace,
    result,
  )
}

/// The base finalization with these outputs.
fn finalize_outputs(outputs: List<Output>) -> Transaction {
  Transaction { ..base_finalize(), outputs }
}

/// The case of a finalization with a fee of 15 AP3X: `Settling` with a
/// share of 5 AP3X.
fn fee_case() -> CaseFields {
  CaseFields { ..finalize_case(), fee: 15_000_000 }
}

fn base_settle() -> Transaction {
  finalize_tx(fee_case(), 25_000_000, AuditorWins)
}

test finalize_ok_base() {
  case_ok(base_finalize())
}

// ## Rule 1: `Open` and `lo >= reveal_end`.

test finalize_r1_ok_at_reveal_end() {
  case_ok(
    Transaction {
      ..base_finalize(),
      validity_range: range(reveal_end, reveal_end + minute),
    },
  )
}

test finalize_r1_fails_before_reveal_end() fail {
  case_ok(
    Transaction {
      ..base_finalize(),
      validity_range: range(reveal_end - 1, reveal_end + minute),
    },
  )
}

test finalize_r1_fails_without_lower_bound() fail {
  case_ok(
    Transaction {
      ..base_finalize(),
      validity_range: interval.before(reveal_end + minute),
    },
  )
}

test finalize_r1_fails_when_settling() fail {
  case_ok(
    finalize_tx(settling(finalize_case(), share), case_lovelace, AuditorWins),
  )
}

// ## Rule 2: `result == outcome(c.tally)`.

test finalize_r2_ok_claimer_wins() {
  case_ok(
    finalize_with(
      Tally { claimer: 2, auditor: 1, inconclusive: 0 },
      0,
      ClaimerWins,
    ),
  )
}

test finalize_r2_ok_inconclusive_below_quorum() {
  case_ok(
    finalize_with(
      Tally { claimer: 0, auditor: 2, inconclusive: 0 },
      0,
      Inconclusive,
    ),
  )
}

test finalize_r2_ok_inconclusive_without_majority() {
  case_ok(
    finalize_with(
      Tally { claimer: 1, auditor: 1, inconclusive: 1 },
      0,
      Inconclusive,
    ),
  )
}

test finalize_r2_fails_with_other_result() fail {
  case_ok(finalize_tx(finalize_case(), case_lovelace, ClaimerWins))
}

test finalize_r2_fails_with_inconclusive_for_majority() fail {
  case_ok(finalize_tx(finalize_case(), case_lovelace, Inconclusive))
}

test finalize_r2_fails_with_side_below_quorum() fail {
  case_ok(
    finalize_with(
      Tally { claimer: 0, auditor: 2, inconclusive: 0 },
      0,
      AuditorWins,
    ),
  )
}

// ## Rule 3: exactly one input with payment credential `Script(J)`.

test finalize_r3_fails_with_second_jury_input() fail {
  let tx = base_finalize()
  case_ok(
    Transaction {
      ..tx,
      inputs: [
        case_input(finalize_case(), case_lovelace),
        case_input_at(case_ref_b(), case_token_b(), new_case(), case_lovelace),
        fee_input(),
      ],
    },
  )
}

// ## Rule 4, first case: `fee == 0`, `n == 0` or `fee < n`: burn and refund.

test finalize_r4_ok_without_votes() {
  case_ok(
    finalize_with(
      Tally { claimer: 0, auditor: 0, inconclusive: 0 },
      15_000_000,
      Inconclusive,
    ),
  )
}

/// `fee < n`: a share would be 0.
test finalize_r4_ok_with_fee_below_votes() {
  case_ok(
    finalize_with(
      Tally { claimer: 1, auditor: 2, inconclusive: 0 },
      2,
      AuditorWins,
    ),
  )
}

test finalize_r4_fails_without_burn() fail {
  case_ok(Transaction { ..base_finalize(), mint: assets.zero })
}

test finalize_r4_fails_with_refund_to_other_address() fail {
  case_ok(
    finalize_outputs(
      [tagged_output(fixtures.treasury(), case_ref(), case_lovelace)],
    ),
  )
}

/// "Refund output without the tag datum".
test finalize_r4_fails_with_refund_without_datum() fail {
  case_ok(
    finalize_outputs(
      [
        Output {
          ..tagged_output(refund_address(), case_ref(), case_lovelace),
          datum: NoDatum,
        },
      ],
    ),
  )
}

test finalize_r4_fails_with_tag_of_other_case() fail {
  case_ok(
    finalize_outputs(
      [tagged_output(refund_address(), case_ref_b(), case_lovelace)],
    ),
  )
}

test finalize_r4_fails_with_tag_as_datum_hash() fail {
  case_ok(
    finalize_outputs(
      [
        Output {
          ..tagged_output(refund_address(), case_ref(), case_lovelace),
          datum: DatumHash(case_token()),
        },
      ],
    ),
  )
}

test finalize_r4_fails_with_less_refund() fail {
  case_ok(
    finalize_outputs(
      [tagged_output(refund_address(), case_ref(), case_lovelace - 1)],
    ),
  )
}

/// The refund may come in more than one output: one tagged output with all
/// lovelace is enough.
test finalize_r4_ok_with_extra_outputs() {
  case_ok(
    finalize_outputs(
      [
        fixtures.plain_output(refund_address(), 1_000_000),
        tagged_output(refund_address(), case_ref(), case_lovelace),
      ],
    ),
  )
}

// ## Rule 4, second case: `Settling { share: fee / n }`.

test finalize_r4_ok_settling() {
  case_ok(base_settle())
}

/// `fee == n`: the share is 1.
test finalize_r4_ok_settling_with_share_1() {
  case_ok(
    finalize_with(
      Tally { claimer: 1, auditor: 2, inconclusive: 0 },
      3,
      AuditorWins,
    ),
  )
}

/// The share rounds down: 10 / 3 = 3.
test finalize_r4_ok_settling_rounds_down() {
  let c = CaseFields { ..finalize_case(), fee: 10 }
  let tx = finalize_tx(c, case_lovelace, AuditorWins)
  and {
    tx.outputs == [
      case_output(
        CaseFields { ..c, state: Settling { share: 3 } },
        case_lovelace,
      ),
    ],
    case_ok(tx),
  }
}

fn settle_output(state_share: Int, lovelace: Int) -> Output {
  case_output(
    CaseFields { ..fee_case(), state: Settling { share: state_share } },
    lovelace,
  )
}

test finalize_r4_fails_with_other_share() fail {
  case_ok(
    Transaction {
      ..base_settle(),
      outputs: [settle_output(5_000_001, 25_000_000)],
    },
  )
}

test finalize_r4_fails_with_share_of_pool() fail {
  // The pool is the declared fee only (SPEC amendment 1), not the lovelace.
  case_ok(
    Transaction {
      ..base_settle(),
      outputs: [settle_output(25_000_000 / 3, 25_000_000)],
    },
  )
}

test finalize_r4_fails_with_less_lovelace() fail {
  case_ok(
    Transaction {
      ..base_settle(),
      outputs: [settle_output(5_000_000, 25_000_000 - 1)],
    },
  )
}

/// The finalizer may add lovelace for the ledger minimum (SPEC amendment 2).
test finalize_r4_ok_with_more_lovelace() {
  case_ok(
    Transaction {
      ..base_settle(),
      outputs: [settle_output(5_000_000, 25_000_000 + 1)],
    },
  )
}

/// With a fee to pay, the case cannot close at once.
test finalize_r4_fails_with_burn_when_fee_is_due() fail {
  case_ok(
    burn_tx(
      fee_case(),
      25_000_000,
      Finalize { result: AuditorWins },
      [tagged_output(refund_address(), case_ref(), 25_000_000)],
      late_range(),
    ),
  )
}

// ## Close: rule 1, `Settling`.

fn base_close() -> Transaction {
  close_tx(
    paid_case(),
    case_lovelace,
    range(reveal_end + day, reveal_end + day + minute),
  )
}

test close_ok_base() {
  case_ok(base_close())
}

test close_r1_fails_when_open() fail {
  case_ok(
    close_tx(
      finalize_case(),
      case_lovelace,
      range(reveal_end + day, reveal_end + day + minute),
    ),
  )
}

// ## Close: rule 2, no `Revealed` ballot, or `lo >= reveal_end + claim_period`.

/// Juror 3 did not claim yet.
fn unpaid_case() -> CaseFields {
  CaseFields {
    ..paid_case(),
    ballots: fixtures.ballots_data(
      [paid(1, AuditorWins), paid(2, AuditorWins), revealed(3, ClaimerWins)],
    ),
  }
}

/// "Close before all paid and before claim_period".
test close_r2_fails_with_unpaid_ballot_before_claim_period() fail {
  let end = reveal_end + params().claim_period
  case_ok(close_tx(unpaid_case(), case_lovelace, range(end - 1, end + minute)))
}

test close_r2_ok_with_unpaid_ballot_after_claim_period() {
  let end = reveal_end + params().claim_period
  case_ok(close_tx(unpaid_case(), case_lovelace, range(end, end + minute)))
}

test close_r2_fails_with_unpaid_ballot_without_lower_bound() fail {
  let end = reveal_end + params().claim_period
  case_ok(close_tx(unpaid_case(), case_lovelace, interval.before(end + minute)))
}

/// Without a `Revealed` ballot, the close needs no time bound.
test close_r2_ok_without_bounds() {
  case_ok(close_tx(paid_case(), case_lovelace, interval.everything))
}

// ## Close: rule 3, exactly one jury input.

test close_r3_fails_with_second_jury_input() fail {
  let tx = base_close()
  case_ok(
    Transaction {
      ..tx,
      inputs: [
        case_input(paid_case(), case_lovelace),
        case_input_at(case_ref_b(), case_token_b(), paid_case(), case_lovelace),
        fee_input(),
      ],
    },
  )
}

// ## Close: rule 4, burn and refund.

test close_r4_fails_without_burn() fail {
  case_ok(Transaction { ..base_close(), mint: assets.zero })
}

test close_r4_fails_without_refund() fail {
  case_ok(
    Transaction {
      ..base_close(),
      outputs: [fixtures.plain_output(refund_address(), case_lovelace)],
    },
  )
}

test close_r4_fails_with_less_refund() fail {
  case_ok(
    Transaction {
      ..base_close(),
      outputs: [tagged_output(refund_address(), case_ref(), case_lovelace - 1)],
    },
  )
}

test close_r4_fails_with_refund_to_other_address() fail {
  case_ok(
    Transaction {
      ..base_close(),
      outputs: [
        tagged_output(fixtures.key_address(stranger), case_ref(), case_lovelace),
      ],
    },
  )
}

/// The base close carries the spend and the burn redeemers.
test close_base_redeemers() {
  base_close().redeemers == [
    fixtures.spend_pair(case_ref(), OnCase(Close)),
    fixtures.mint_pair(Burn),
  ]
}
