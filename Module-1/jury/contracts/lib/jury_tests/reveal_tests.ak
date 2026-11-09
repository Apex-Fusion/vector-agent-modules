//// SPEC §6.5 juror `Reveal { case }` and §6.10 case `CaseReveal { juror,
//// vote, salt, record_hash }`: one passing and one failing transaction per
//// rule. The base transaction: juror 1 reveals `AuditorWins` in the case of
//// `reveal_case` (juror 2 committed, juror 3 revealed `ClaimerWins`).

use aiken/interval
use aiken/primitive/bytearray
use cardano/transaction.{Output, Transaction}
use jury/commit.{commit_hash}
use jury/datum.{CaseFields, with_ballots}
use jury/redeemers.{CaseCommit, CaseReveal, Commit, OnJuror, Reveal}
use jury/types.{
  AuditorWins, Ballot, ClaimerWins, Inconclusive, Revealed, Settling, Tally,
}
use jury_tests/fixtures.{
  base_reveal, bond, case_input, case_lovelace, case_output, case_ref,
  case_token, case_token_b, commit_end, committed, committed_juror, juror_agent,
  juror_input, juror_output, juror_ref, juror_token, minute, range, record_hash,
  reveal_action, reveal_case, reveal_end, reveal_outputs, reveal_tx,
  reveal_tx_with, revealed, salt, spend_ok, stranger, with_entry, without_entry,
}

fn juror_ok(tx: Transaction) -> Bool {
  spend_ok(tx, juror_ref(1))
}

fn case_ok(tx: Transaction) -> Bool {
  spend_ok(tx, case_ref())
}

/// The base reveal with the case action `action` and the honest outputs for
/// `vote`.
fn reveal_with_action(action, vote) -> Transaction {
  reveal_tx_with(
    1,
    committed_juror(1),
    bond,
    reveal_case(),
    case_lovelace,
    action,
    reveal_outputs(
      1,
      committed_juror(1),
      bond,
      reveal_case(),
      case_lovelace,
      vote,
    ),
  )
}

/// The base reveal with these outputs.
fn reveal_outputs_as(outputs: List<Output>) -> Transaction {
  reveal_tx_with(
    1,
    committed_juror(1),
    bond,
    reveal_case(),
    case_lovelace,
    reveal_action(1, AuditorWins),
    outputs,
  )
}

/// The honest juror output of the base reveal.
fn base_juror_out() -> Output {
  juror_output(1, without_entry(committed_juror(1), case_token()), bond)
}

/// A case output of the base reveal with these ballots and this tally.
fn case_out(ballots: List<Ballot>, tally: Tally, lovelace: Int) -> Output {
  case_output(
    CaseFields { ..with_ballots(reveal_case(), ballots), tally },
    lovelace,
  )
}

/// The honest case output of the base reveal.
fn base_case_out() -> Output {
  case_out(
    [
      committed(2, ClaimerWins),
      revealed(1, AuditorWins),
      revealed(3, ClaimerWins),
    ],
    Tally { claimer: 1, auditor: 1, inconclusive: 0 },
    case_lovelace,
  )
}

test reveal_ok_base() {
  let tx = base_reveal()
  juror_ok(tx) && case_ok(tx)
}

test reveal_ok_inconclusive() {
  let c =
    with_ballots(
      reveal_case(),
      [committed(1, Inconclusive), committed(2, ClaimerWins)],
    )
  let tx =
    reveal_tx(1, committed_juror(1), bond, c, case_lovelace, Inconclusive)
  juror_ok(tx) && case_ok(tx)
}

test reveal_ok_with_other_open_entries() {
  let j =
    with_entry(committed_juror(1), case_token_b(), reveal_end + fixtures.hour)
  let tx = reveal_tx(1, j, bond, reveal_case(), case_lovelace, AuditorWins)
  juror_ok(tx) && case_ok(tx)
}

// ## §6.5 rule 1: the owner signs.

test reveal_r1_fails_without_owner_signature() fail {
  juror_ok(Transaction { ..base_reveal(), extra_signatories: [] })
}

// ## §6.5 rule 2: the counterpart case input and its redeemer.

test reveal_r2_fails_without_case_input() fail {
  juror_ok(
    Transaction {
      ..base_reveal(),
      inputs: [juror_input(1, committed_juror(1), bond), fixtures.fee_input()],
    },
  )
}

test reveal_r2_fails_when_case_reveals_other_juror() fail {
  juror_ok(reveal_with_action(reveal_action(2, ClaimerWins), AuditorWins))
}

test reveal_r2_fails_when_case_commits() fail {
  juror_ok(
    reveal_with_action(
      CaseCommit { juror: juror_token(1), commit: stranger },
      AuditorWins,
    ),
  )
}

// ## §6.5 rule 3: exactly one entry for `case`.

test reveal_r3_fails_without_entry() fail {
  let j = fixtures.juror(1)
  juror_ok(
    reveal_tx_with(
      1,
      j,
      bond,
      reveal_case(),
      case_lovelace,
      reveal_action(1, AuditorWins),
      [juror_output(1, j, bond), base_case_out()],
    ),
  )
}

test reveal_r3_fails_with_two_entries() fail {
  let j = with_entry(committed_juror(1), case_token(), reveal_end)
  juror_ok(
    reveal_tx_with(
      1,
      j,
      bond,
      reveal_case(),
      case_lovelace,
      reveal_action(1, AuditorWins),
      [juror_output(1, committed_juror(1), bond), base_case_out()],
    ),
  )
}

// ## §6.5 rule 4: the juror output.

test reveal_r4_fails_with_less_lovelace() fail {
  juror_ok(
    reveal_outputs_as(
      [juror_output(1, fixtures.juror(1), bond - 1), base_case_out()],
    ),
  )
}

test reveal_r4_fails_with_more_lovelace() fail {
  juror_ok(
    reveal_outputs_as(
      [juror_output(1, fixtures.juror(1), bond + 1), base_case_out()],
    ),
  )
}

test reveal_r4_fails_when_entry_stays() fail {
  juror_ok(
    reveal_outputs_as(
      [juror_output(1, committed_juror(1), bond), base_case_out()],
    ),
  )
}

test reveal_r4_fails_when_other_entry_goes() fail {
  let j = with_entry(committed_juror(1), case_token_b(), reveal_end)
  juror_ok(
    reveal_tx_with(
      1,
      j,
      bond,
      reveal_case(),
      case_lovelace,
      reveal_action(1, AuditorWins),
      [juror_output(1, committed_juror(1), bond), base_case_out()],
    ),
  )
}

// ## §6.10 rule 1: `Open`, `lo >= commit_end`, `hi <= reveal_end`.

test case_reveal_r1_ok_at_window_bounds() {
  case_ok(
    Transaction {
      ..base_reveal(),
      validity_range: range(commit_end, reveal_end),
    },
  )
}

test case_reveal_r1_fails_before_commit_end() fail {
  case_ok(
    Transaction {
      ..base_reveal(),
      validity_range: range(commit_end - 1, commit_end + minute),
    },
  )
}

test case_reveal_r1_fails_after_reveal_end() fail {
  case_ok(
    Transaction {
      ..base_reveal(),
      validity_range: range(reveal_end - minute, reveal_end + 1),
    },
  )
}

test case_reveal_r1_fails_without_lower_bound() fail {
  case_ok(
    Transaction { ..base_reveal(), validity_range: interval.before(reveal_end) },
  )
}

test case_reveal_r1_fails_without_upper_bound() fail {
  case_ok(
    Transaction { ..base_reveal(), validity_range: interval.after(commit_end) },
  )
}

test case_reveal_r1_fails_when_settling() fail {
  let c = CaseFields { ..reveal_case(), state: Settling { share: 1 } }
  case_ok(reveal_tx(1, committed_juror(1), bond, c, case_lovelace, AuditorWins))
}

// ## §6.10 rule 2: the counterpart juror input and its redeemer.

test case_reveal_r2_fails_without_juror_input() fail {
  case_ok(
    Transaction {
      ..base_reveal(),
      inputs: [case_input(reveal_case(), case_lovelace), fixtures.fee_input()],
    },
  )
}

test case_reveal_r2_fails_when_juror_names_other_case() fail {
  case_ok(
    fixtures.with_redeemer(
      base_reveal(),
      juror_ref(1),
      OnJuror(Reveal { case: case_token_b() }),
    ),
  )
}

test case_reveal_r2_fails_when_juror_commits() fail {
  case_ok(
    fixtures.with_redeemer(
      base_reveal(),
      juror_ref(1),
      OnJuror(Commit { case: case_token() }),
    ),
  )
}

// ## §6.10 rule 3: exactly one ballot with `juror`, and it is `Committed`.

/// Juror 3 revealed already.
test case_reveal_r3_fails_when_revealed() fail {
  case_ok(
    reveal_tx_with(
      3,
      committed_juror(3),
      bond,
      reveal_case(),
      case_lovelace,
      reveal_action(3, ClaimerWins),
      [
        juror_output(3, fixtures.juror(3), bond),
        case_out(
          [
            committed(2, ClaimerWins),
            fixtures.committed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 2, auditor: 0, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

/// Juror 5 has no ballot.
test case_reveal_r3_fails_without_ballot() fail {
  case_ok(
    reveal_tx_with(
      5,
      committed_juror(5),
      bond,
      reveal_case(),
      case_lovelace,
      reveal_action(5, AuditorWins),
      [
        juror_output(5, fixtures.juror(5), bond),
        case_out(
          [
            committed(2, ClaimerWins),
            fixtures.committed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

/// Two ballots with the juror token of juror 1.
test case_reveal_r3_fails_with_two_ballots_of_juror() fail {
  let twice =
    [fixtures.committed(1, AuditorWins), fixtures.committed(1, AuditorWins)]
  case_ok(
    reveal_tx_with(
      1,
      committed_juror(1),
      bond,
      with_ballots(reveal_case(), twice),
      case_lovelace,
      reveal_action(1, AuditorWins),
      [
        base_juror_out(),
        case_out(
          [revealed(1, AuditorWins), fixtures.committed(1, AuditorWins)],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

// ## §6.10 rule 4: 32-byte salt and record hash.

/// A juror that committed with a 31-byte salt can never reveal.
test case_reveal_r4_fails_with_short_salt() fail {
  let short = bytearray.drop(salt(1), 1)
  let commit =
    commit_hash(case_token(), juror_agent(1), AuditorWins, short, record_hash)
  let c =
    with_ballots(
      reveal_case(),
      [Ballot { ..fixtures.committed(1, AuditorWins), commit }],
    )
  case_ok(
    reveal_tx_with(
      1,
      committed_juror(1),
      bond,
      c,
      case_lovelace,
      CaseReveal {
        juror: juror_token(1),
        vote: AuditorWins,
        salt: short,
        record_hash,
      },
      reveal_outputs(1, committed_juror(1), bond, c, case_lovelace, AuditorWins),
    ),
  )
}

test case_reveal_r4_ok_with_32_bytes() {
  case_ok(base_reveal())
}

test case_reveal_r4_fails_with_long_record_hash() fail {
  let long = bytearray.push(record_hash, 0)
  let commit =
    commit_hash(case_token(), juror_agent(1), AuditorWins, salt(1), long)
  let c =
    with_ballots(
      reveal_case(),
      [Ballot { ..fixtures.committed(1, AuditorWins), commit }],
    )
  case_ok(
    reveal_tx_with(
      1,
      committed_juror(1),
      bond,
      c,
      case_lovelace,
      CaseReveal {
        juror: juror_token(1),
        vote: AuditorWins,
        salt: salt(1),
        record_hash: long,
      },
      reveal_outputs(1, committed_juror(1), bond, c, case_lovelace, AuditorWins),
    ),
  )
}

// ## §6.10 rule 5: the commitment.

/// Committed to `AuditorWins`, reveals `ClaimerWins`.
test case_reveal_r5_fails_with_other_vote() fail {
  case_ok(reveal_with_action(reveal_action(1, ClaimerWins), ClaimerWins))
}

test case_reveal_r5_fails_with_other_salt() fail {
  case_ok(
    reveal_with_action(
      CaseReveal {
        juror: juror_token(1),
        vote: AuditorWins,
        salt: salt(9),
        record_hash,
      },
      AuditorWins,
    ),
  )
}

test case_reveal_r5_fails_with_other_record_hash() fail {
  case_ok(
    reveal_with_action(
      CaseReveal {
        juror: juror_token(1),
        vote: AuditorWins,
        salt: salt(1),
        record_hash: stranger,
      },
      AuditorWins,
    ),
  )
}

/// The commitment was made for another case.
test case_reveal_r5_fails_with_commitment_of_other_case() fail {
  let commit =
    commit_hash(
      case_token_b(),
      juror_agent(1),
      AuditorWins,
      salt(1),
      record_hash,
    )
  let c =
    with_ballots(
      reveal_case(),
      [Ballot { ..fixtures.committed(1, AuditorWins), commit }],
    )
  case_ok(reveal_tx(1, committed_juror(1), bond, c, case_lovelace, AuditorWins))
}

/// The commitment was made with another DID.
test case_reveal_r5_fails_with_commitment_of_other_agent() fail {
  let commit =
    commit_hash(case_token(), juror_agent(2), AuditorWins, salt(1), record_hash)
  let c =
    with_ballots(
      reveal_case(),
      [Ballot { ..fixtures.committed(1, AuditorWins), commit }],
    )
  case_ok(reveal_tx(1, committed_juror(1), bond, c, case_lovelace, AuditorWins))
}

// ## §6.10 rule 6: the case output.

/// The reveal drops the commit (SPEC amendment 8): a case output that keeps
/// it fails.
test case_reveal_r6_fails_when_commit_stays() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            Ballot {
              ..fixtures.committed(1, AuditorWins),
              status: Revealed { vote: AuditorWins },
            },
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

/// The honest reveal output has the empty commit.
test case_reveal_r6_ok_with_empty_commit() {
  let tx = base_reveal()
  expect [_, case_output] = tx.outputs
  and {
    case_output == base_case_out(),
    revealed(1, AuditorWins).commit == #"",
    case_ok(tx),
  }
}

test case_reveal_r6_ok_with_more_lovelace() {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            revealed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace + 50_000,
        ),
      ],
    ),
  )
}

test case_reveal_r6_fails_with_less_lovelace() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            revealed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace - 1,
        ),
      ],
    ),
  )
}

test case_reveal_r6_fails_without_tally() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            revealed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 0, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

test case_reveal_r6_fails_with_tally_of_other_vote() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            revealed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 2, auditor: 0, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

test case_reveal_r6_fails_with_status_unchanged() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            fixtures.committed(1, AuditorWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

test case_reveal_r6_fails_with_ballot_moved() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            revealed(1, AuditorWins),
            committed(2, ClaimerWins),
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}

test case_reveal_r6_fails_with_other_vote_in_status() fail {
  case_ok(
    reveal_outputs_as(
      [
        base_juror_out(),
        case_out(
          [
            committed(2, ClaimerWins),
            Ballot {
              ..fixtures.committed(1, AuditorWins),
              status: Revealed { vote: ClaimerWins },
            },
            revealed(3, ClaimerWins),
          ],
          Tally { claimer: 1, auditor: 1, inconclusive: 0 },
          case_lovelace,
        ),
      ],
    ),
  )
}
