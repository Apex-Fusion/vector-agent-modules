//// SPEC §6.7 juror `Slash { case }` (anybody): one passing and one failing
//// transaction per rule. The base transaction slashes juror 1, which did
//// not reveal in the test case, one minute after `reveal_end`: the juror
//// output loses 25 AP3X and a treasury output tagged with the juror input
//// gets them.

use aiken/interval
use cardano/address.{Address, Inline, Script, VerificationKey}
use cardano/transaction.{Input, NoDatum, Output, Transaction}
use jury/datum.{JurorFields}
use jury_tests/fixtures.{
  ap3x, base_slash, bond, case_token, case_token_b, committed_juror, fee_input,
  juror, juror_input, juror_output, juror_ref, jury, minute, params,
  plain_output, range, reveal_end, slash_amount, slash_tx, slash_tx_with,
  spend_ok, stranger, tagged_output, treasury, treasury_hash, with_entry,
}

fn slash_ok(tx: Transaction) -> Bool {
  spend_ok(tx, juror_ref(1))
}

fn full() -> Int {
  params().slash
}

/// The base slash with these outputs.
fn slash_outputs(outputs: List<Output>) -> Transaction {
  slash_tx_with(1, committed_juror(1), bond, case_token(), outputs)
}

/// The honest juror output of the base slash.
fn base_juror_out() -> Output {
  juror_output(1, juror(1), bond - full())
}

/// The honest treasury output of the base slash.
fn base_treasury_out() -> Output {
  tagged_output(treasury(), juror_ref(1), full())
}

test slash_ok_base() {
  slash_ok(base_slash())
}

test slash_ok_keeps_other_entries() {
  let j =
    with_entry(committed_juror(1), case_token_b(), reveal_end + fixtures.day)
  slash_ok(slash_tx(1, j, bond, case_token()))
}

// ## Rule 1: exactly one entry `e` for `case`, and `lo >= e.reveal_end`.

test slash_r1_ok_at_reveal_end() {
  slash_ok(
    Transaction {
      ..base_slash(),
      validity_range: range(reveal_end, reveal_end + minute),
    },
  )
}

test slash_r1_fails_before_reveal_end() fail {
  slash_ok(
    Transaction {
      ..base_slash(),
      validity_range: range(reveal_end - 1, reveal_end + minute),
    },
  )
}

test slash_r1_fails_without_lower_bound() fail {
  slash_ok(
    Transaction {
      ..base_slash(),
      validity_range: interval.before(reveal_end + minute),
    },
  )
}

test slash_r1_fails_without_entry() fail {
  slash_ok(
    slash_tx_with(
      1,
      juror(1),
      bond,
      case_token(),
      [base_juror_out(), base_treasury_out()],
    ),
  )
}

test slash_r1_fails_with_two_entries() fail {
  let j = with_entry(committed_juror(1), case_token(), reveal_end)
  slash_ok(
    slash_tx_with(
      1,
      j,
      bond,
      case_token(),
      [juror_output(1, committed_juror(1), bond - full()), base_treasury_out()],
    ),
  )
}

// ## Rule 2: `amount = min(slash, lovelace - juror_floor)`.

/// 10 AP3X above the floor: the amount is 10 AP3X.
test slash_r2_ok_with_partial_amount() {
  let lovelace = params().juror_floor + 10 * ap3x
  let tx = slash_tx(1, committed_juror(1), lovelace, case_token())
  and {
    slash_amount(lovelace) == 10 * ap3x,
    tx.outputs == [
      juror_output(1, juror(1), params().juror_floor),
      tagged_output(treasury(), juror_ref(1), 10 * ap3x),
    ],
    slash_ok(tx),
  }
}

/// At the floor: the amount is 0, no treasury output is needed.
test slash_r2_ok_at_floor_without_treasury_output() {
  let lovelace = params().juror_floor
  let tx = slash_tx(1, committed_juror(1), lovelace, case_token())
  tx.outputs == [juror_output(1, juror(1), lovelace)] && slash_ok(tx)
}

/// "Slash below the floor": the slasher takes the full `slash` from a juror
/// output 10 AP3X above the floor.
test slash_r2_fails_below_floor() fail {
  let lovelace = params().juror_floor + 10 * ap3x
  slash_ok(
    slash_tx_with(
      1,
      committed_juror(1),
      lovelace,
      case_token(),
      [
        juror_output(1, juror(1), lovelace - full()),
        tagged_output(treasury(), juror_ref(1), full()),
      ],
    ),
  )
}

// ## Rule 3: the juror output loses `e` and at least the amount.

/// The slasher may add lovelace for the ledger minimum (SPEC amendment 4).
test slash_r3_ok_when_slasher_adds_lovelace() {
  slash_ok(
    slash_outputs(
      [juror_output(1, juror(1), bond - full() + 1), base_treasury_out()],
    ),
  )
}

test slash_r3_fails_when_juror_loses_more() fail {
  slash_ok(
    slash_outputs(
      [juror_output(1, juror(1), bond - full() - 1), base_treasury_out()],
    ),
  )
}

test slash_r3_fails_when_entry_stays() fail {
  slash_ok(
    slash_outputs(
      [juror_output(1, committed_juror(1), bond - full()), base_treasury_out()],
    ),
  )
}

test slash_r3_fails_when_datum_changes() fail {
  slash_ok(
    slash_outputs(
      [
        juror_output(
          1,
          JurorFields { ..juror(1), exit_after: Some(reveal_end) },
          bond - full(),
        ),
        base_treasury_out(),
      ],
    ),
  )
}

// ## Rule 4: a tagged output at the treasury gets at least the amount.

test slash_r4_ok_with_more_to_treasury() {
  slash_ok(
    slash_outputs(
      [base_juror_out(), tagged_output(treasury(), juror_ref(1), full() + 1)],
    ),
  )
}

test slash_r4_fails_with_less_to_treasury() fail {
  slash_ok(
    slash_outputs(
      [base_juror_out(), tagged_output(treasury(), juror_ref(1), full() - 1)],
    ),
  )
}

test slash_r4_fails_without_tag() fail {
  slash_ok(slash_outputs([base_juror_out(), plain_output(treasury(), full())]))
}

test slash_r4_fails_with_tag_of_other_input() fail {
  slash_ok(
    slash_outputs(
      [base_juror_out(), tagged_output(treasury(), juror_ref(2), full())],
    ),
  )
}

test slash_r4_fails_with_treasury_stake_credential() fail {
  let address =
    Address {
      payment_credential: Script(treasury_hash),
      stake_credential: Some(Inline(VerificationKey(stranger))),
    }
  slash_ok(
    slash_outputs(
      [base_juror_out(), tagged_output(address, juror_ref(1), full())],
    ),
  )
}

test slash_r4_fails_when_slasher_takes_it() fail {
  slash_ok(
    slash_outputs(
      [
        base_juror_out(),
        tagged_output(
          fixtures.key_address(fixtures.wallet_key_hash),
          juror_ref(1),
          full(),
        ),
      ],
    ),
  )
}

// ## Rule 5: exactly one input with payment credential `Script(J)`.

test slash_r5_fails_with_second_juror_input() fail {
  slash_ok(
    Transaction {
      ..base_slash(),
      inputs: [
        juror_input(1, committed_juror(1), bond),
        juror_input(2, juror(2), bond),
        fee_input(),
      ],
    },
  )
}

/// Any input at the jury payment credential counts, also with a stake
/// credential and without a jury token.
test slash_r5_fails_with_other_jury_address_input() fail {
  let junk =
    Input {
      output_reference: fixtures.out_ref(0x99, 0),
      output: Output {
        ..plain_output(
          Address {
            payment_credential: Script(jury),
            stake_credential: Some(Inline(VerificationKey(stranger))),
          },
          2_000_000,
        ),
        datum: NoDatum,
      },
    }
  slash_ok(
    Transaction {
      ..base_slash(),
      inputs: [juror_input(1, committed_juror(1), bond), junk, fee_input()],
    },
  )
}
