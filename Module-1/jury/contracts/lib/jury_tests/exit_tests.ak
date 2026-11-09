//// SPEC §6.8 juror `RequestExit`, `Exit` and `TopUp`: one passing and one
//// failing transaction per rule.

use aiken/interval
use cardano/assets
use cardano/transaction.{Transaction}
use jury/datum.{JurorFields}
use jury_tests/fixtures.{
  bond, case_token, committed_juror, exit_after, exit_tx, hour, juror,
  juror_output, juror_ref, juror_token, jury, leaving_juror, minute, opened_at,
  params, range, request_exit_tx, reveal_end, spend_ok, top_up_tx, with_entry,
}

fn juror_ok(tx: Transaction) -> Bool {
  spend_ok(tx, juror_ref(1))
}

// ## RequestExit

fn base_request() -> Transaction {
  request_exit_tx(1, juror(1), bond)
}

/// The base request with the juror output datum `j` and `lovelace`.
fn request_output(j: JurorFields, lovelace: Int) -> Transaction {
  Transaction { ..base_request(), outputs: [juror_output(1, j, lovelace)] }
}

test request_exit_ok_base() {
  juror_ok(base_request())
}

/// A juror with open entries may request the exit.
test request_exit_ok_with_open_entry() {
  juror_ok(request_exit_tx(1, committed_juror(1), bond))
}

test request_exit_r1_fails_without_owner_signature() fail {
  juror_ok(Transaction { ..base_request(), extra_signatories: [] })
}

test request_exit_r1_fails_without_upper_bound() fail {
  juror_ok(
    Transaction {
      ..base_request(),
      validity_range: interval.after(opened_at - 10 * minute),
    },
  )
}

test request_exit_r1_fails_with_range_over_one_hour() fail {
  juror_ok(
    Transaction {
      ..base_request(),
      validity_range: range(opened_at - hour - 1, opened_at),
    },
  )
}

test request_exit_r2_fails_after_request() fail {
  let j = JurorFields { ..juror(1), exit_after: Some(exit_after) }
  juror_ok(request_exit_tx(1, j, bond))
}

test request_exit_r3_fails_with_less_lovelace() fail {
  juror_ok(
    request_output(
      JurorFields {
        ..juror(1),
        exit_after: Some(opened_at + params().exit_notice),
      },
      bond - 1,
    ),
  )
}

test request_exit_r3_fails_with_early_exit_time() fail {
  juror_ok(
    request_output(
      JurorFields {
        ..juror(1),
        exit_after: Some(opened_at + params().exit_notice - 1),
      },
      bond,
    ),
  )
}

test request_exit_r3_fails_without_exit_time() fail {
  juror_ok(request_output(juror(1), bond))
}

// ## Exit

fn base_exit() -> Transaction {
  exit_tx(1, leaving_juror(1), bond)
}

test exit_ok_base() {
  juror_ok(base_exit())
}

test exit_r1_fails_without_owner_signature() fail {
  juror_ok(Transaction { ..base_exit(), extra_signatories: [] })
}

test exit_r2_fails_without_request() fail {
  juror_ok(exit_tx(1, juror(1), bond))
}

/// "Exit before exit_after".
test exit_r2_fails_before_exit_time() fail {
  juror_ok(
    Transaction {
      ..base_exit(),
      validity_range: range(exit_after - 1, exit_after + minute),
    },
  )
}

test exit_r2_fails_without_lower_bound() fail {
  juror_ok(
    Transaction {
      ..base_exit(),
      validity_range: interval.before(exit_after + minute),
    },
  )
}

/// "Exit with open entries".
test exit_r3_fails_with_open_entry() fail {
  juror_ok(
    exit_tx(1, with_entry(leaving_juror(1), case_token(), reveal_end), bond),
  )
}

test exit_r4_fails_without_burn() fail {
  juror_ok(Transaction { ..base_exit(), mint: assets.zero })
}

test exit_r4_fails_with_burn_of_other_token() fail {
  juror_ok(
    Transaction {
      ..base_exit(),
      mint: assets.from_asset(jury, juror_token(2), -1),
    },
  )
}

/// The token goes to a juror output again instead of the burn.
test exit_r4_fails_when_token_stays() fail {
  juror_ok(
    Transaction {
      ..base_exit(),
      outputs: [juror_output(1, leaving_juror(1), bond - 1)],
      mint: assets.zero,
    },
  )
}

// ## TopUp

fn base_top_up() -> Transaction {
  top_up_tx(1, juror(1), bond, 1)
}

test top_up_ok_base() {
  juror_ok(base_top_up())
}

test top_up_ok_with_open_entry_and_exit_request() {
  let j = with_entry(leaving_juror(1), case_token(), reveal_end)
  juror_ok(top_up_tx(1, j, bond, 100))
}

test top_up_r1_fails_without_owner_signature() fail {
  juror_ok(Transaction { ..base_top_up(), extra_signatories: [] })
}

test top_up_r2_fails_with_same_lovelace() fail {
  juror_ok(top_up_tx(1, juror(1), bond, 0))
}

test top_up_r2_fails_with_less_lovelace() fail {
  juror_ok(top_up_tx(1, juror(1), bond, -1))
}

/// A top-up cannot cancel an exit request.
test top_up_r2_fails_when_exit_request_goes() fail {
  juror_ok(
    Transaction {
      ..top_up_tx(1, leaving_juror(1), bond, 1),
      outputs: [juror_output(1, juror(1), bond + 1)],
    },
  )
}

/// A top-up cannot remove an open entry.
test top_up_r2_fails_when_entry_goes() fail {
  juror_ok(
    Transaction {
      ..top_up_tx(1, committed_juror(1), bond, 1),
      outputs: [juror_output(1, juror(1), bond + 1)],
    },
  )
}
