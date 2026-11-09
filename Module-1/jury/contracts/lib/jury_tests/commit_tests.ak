//// SPEC §6.4 juror `Commit { case }` and §6.9 case `CaseCommit { juror,
//// commit }`: one passing and one failing transaction per rule. The base
//// transaction: juror 1 commits to `AuditorWins` in the new test case.

use aiken/crypto.{blake2b_224}
use aiken/interval
use aiken/primitive/bytearray
use cardano/transaction.{Input, Transaction}
use jury/datum.{CaseFields, JurorFields, case_data, juror_data, with_ballots}
use jury/redeemers.{CaseCommit, Commit, OnCase, OnJuror, Reveal}
use jury/types.{AuditorWins, Ballot, ClaimerWins, Committed, OpenVote, Settling}
use jury_tests/fixtures.{
  ballot_lovelace, base_commit, bond, case_input, case_lovelace, case_ref,
  case_token, case_token_b, commit_case_out, commit_end, commit_juror_out,
  commit_range, commit_tx, commit_tx_with, commitment, committed, juror,
  juror_agent, juror_input, juror_record, juror_ref, juror_token, minute,
  new_case, opened_at, params, range, record_input, registered_at, reveal_end,
  spend_ok, stranger, with_entry,
}
use vector_identity/types.{
  ControllerSet, Ed25519, Hold, IdentityDatum, Key, Subagent, Suspended,
} as vi

fn juror_ok(tx: Transaction) -> Bool {
  spend_ok(tx, juror_ref(1))
}

fn case_ok(tx: Transaction) -> Bool {
  spend_ok(tx, case_ref())
}

/// The commitment of juror 1 in the base commit.
fn base_commitment() -> ByteArray {
  commitment(case_token(), 1, AuditorWins)
}

/// A commit of juror 1 with the juror datum `j` and `lovelace`.
fn commit_of(j: JurorFields, lovelace: Int) -> Transaction {
  commit_tx(1, j, lovelace, new_case(), case_lovelace, base_commitment())
}

/// A commit of juror 1 to the case `c`.
fn commit_to(c: CaseFields) -> Transaction {
  commit_tx(1, juror(1), bond, c, case_lovelace, base_commitment())
}

/// The base commit with the outputs `juror_out` and `case_out`.
fn commit_outputs(juror_out, case_out) -> Transaction {
  commit_tx_with(
    1,
    juror(1),
    bond,
    new_case(),
    case_lovelace,
    base_commitment(),
    [juror_out, case_out],
  )
}

/// The honest juror output of the base commit.
fn base_juror_out() {
  commit_juror_out(1, juror(1), bond, new_case())
}

/// The honest case output of the base commit.
fn base_case_out() {
  commit_case_out(
    1,
    juror_agent(1),
    new_case(),
    case_lovelace + ballot_lovelace,
    base_commitment(),
  )
}

/// `j` with `count` open entries for other cases.
fn with_entries(j: JurorFields, count: Int) -> JurorFields {
  if count <= 0 {
    j
  } else {
    with_entries(with_entry(j, other_case(count), reveal_end), count - 1)
  }
}

/// The name of another case: the second test case name with the first byte
/// `n`.
fn other_case(n: Int) -> ByteArray {
  bytearray.push(bytearray.drop(case_token_b(), 1), n)
}

test commit_ok_base() {
  let tx = base_commit()
  juror_ok(tx) && case_ok(tx)
}

test commit_ok_with_other_ballots_and_entries() {
  let c =
    with_ballots(
      new_case(),
      [committed(2, ClaimerWins), committed(3, AuditorWins)],
    )
  let j = with_entries(juror(1), 2)
  let tx = commit_tx(1, j, bond, c, case_lovelace, base_commitment())
  juror_ok(tx) && case_ok(tx)
}

// ## §6.4 rule 1: the owner signs.

test commit_r1_fails_without_owner_signature() fail {
  juror_ok(Transaction { ..base_commit(), extra_signatories: [] })
}

// ## §6.4 rule 2: the counterpart case input and its redeemer.

test commit_r2_fails_without_case_input() fail {
  let tx = base_commit()
  juror_ok(
    Transaction {
      ..tx,
      inputs: [juror_input(1, juror(1), bond), fixtures.fee_input()],
    },
  )
}

test commit_r2_fails_when_case_names_other_juror() fail {
  juror_ok(
    fixtures.with_redeemer(
      base_commit(),
      case_ref(),
      OnCase(CaseCommit { juror: juror_token(2), commit: base_commitment() }),
    ),
  )
}

test commit_r2_fails_when_case_reveals() fail {
  juror_ok(
    fixtures.with_redeemer(
      base_commit(),
      case_ref(),
      OnCase(fixtures.reveal_action(1, AuditorWins)),
    ),
  )
}

/// The case token in an output with a `Juror` datum: not a `Case`.
test commit_r2_fails_when_counterpart_datum_is_not_case() fail {
  let tx = base_commit()
  let fake =
    Input {
      output_reference: case_ref(),
      output: fixtures.jury_output(
        case_token(),
        juror_data(juror(9)),
        case_lovelace,
      ),
    }
  juror_ok(
    Transaction {
      ..tx,
      inputs: [fake, juror_input(1, juror(1), bond), fixtures.fee_input()],
    },
  )
}

// ## §6.4 rule 3: `case` is not in `j.open`.

test commit_r3_fails_when_case_is_open() fail {
  juror_ok(commit_of(with_entry(juror(1), case_token(), reveal_end), bond))
}

// ## §6.4 rule 4: eligibility.

/// `lo == registered_at + eligibility_delay`.
test commit_r4_ok_at_end_of_eligibility_delay() {
  let j =
    JurorFields {
      ..juror(1),
      registered_at: opened_at + 10 * minute - params().eligibility_delay,
    }
  juror_ok(commit_of(j, bond))
}

test commit_r4_fails_before_end_of_eligibility_delay() fail {
  let j =
    JurorFields {
      ..juror(1),
      registered_at: opened_at + 10 * minute - params().eligibility_delay + 1,
    }
  juror_ok(commit_of(j, bond))
}

test commit_r4_fails_after_exit_request() fail {
  juror_ok(
    commit_of(
      JurorFields { ..juror(1), exit_after: Some(opened_at + fixtures.day) },
      bond,
    ),
  )
}

test commit_r4_ok_with_bond_min() {
  juror_ok(commit_of(juror(1), params().bond_min))
}

test commit_r4_fails_below_bond_min() fail {
  juror_ok(commit_of(juror(1), params().bond_min - 1))
}

/// No limit on open commitments (SPEC amendment 2): 30 open entries with
/// the minimum bond.
test commit_r4_ok_with_many_open_entries() {
  juror_ok(commit_of(with_entries(juror(1), 30), params().bond_min))
}

test commit_r4_ok_with_listed_transaction_key() {
  let key = #"e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"
  let record =
    IdentityDatum {
      ..juror_record(1),
      keys: [Key { key_type: Ed25519, key, purposes: 8, pop: None }],
    }
  let j = JurorFields { ..juror(1), owner: blake2b_224(key) }
  juror_ok(
    Transaction {
      ..commit_of(j, bond),
      reference_inputs: [record_input(0x91, juror_agent(1), record)],
      extra_signatories: [blake2b_224(key)],
    },
  )
}

test commit_r4_fails_without_record() fail {
  juror_ok(Transaction { ..base_commit(), reference_inputs: [] })
}

test commit_r4_fails_when_suspended() fail {
  let record =
    IdentityDatum {
      ..juror_record(1),
      status: Suspended {
        holds: [
          Hold {
            by: ControllerSet,
            reason: 64,
            since: opened_at - fixtures.day,
            until: opened_at + 7 * fixtures.day,
          },
        ],
      },
    }
  juror_ok(
    Transaction {
      ..base_commit(),
      reference_inputs: [record_input(0x91, juror_agent(1), record)],
    },
  )
}

test commit_r4_fails_for_subagent() fail {
  let record =
    IdentityDatum {
      ..juror_record(1),
      lineage: Subagent { parent: stranger, parent_epoch: 0, depth: 1 },
    }
  juror_ok(
    Transaction {
      ..base_commit(),
      reference_inputs: [record_input(0x91, juror_agent(1), record)],
    },
  )
}

test commit_r4_fails_when_controller_changed() fail {
  let record =
    IdentityDatum { ..juror_record(1), controller: fixtures.controller(7) }
  juror_ok(
    Transaction {
      ..base_commit(),
      reference_inputs: [record_input(0x91, juror_agent(1), record)],
    },
  )
}

test commit_r4_fails_with_range_over_one_hour() fail {
  juror_ok(
    Transaction {
      ..base_commit(),
      validity_range: range(
        opened_at + 20 * minute - fixtures.hour - 1,
        opened_at + 20 * minute,
      ),
    },
  )
}

test commit_r4_fails_with_unbounded_lower_bound() fail {
  juror_ok(
    Transaction {
      ..base_commit(),
      validity_range: interval.before(opened_at + 20 * minute),
    },
  )
}

// ## §6.4 rule 5: the juror output.

test commit_r5_fails_with_less_lovelace() fail {
  juror_ok(
    commit_outputs(
      commit_juror_out(1, juror(1), bond - 1, new_case()),
      base_case_out(),
    ),
  )
}

test commit_r5_fails_with_more_lovelace() fail {
  juror_ok(
    commit_outputs(
      commit_juror_out(1, juror(1), bond + 1, new_case()),
      base_case_out(),
    ),
  )
}

test commit_r5_fails_with_other_reveal_end() fail {
  juror_ok(
    commit_outputs(
      fixtures.juror_output(
        1,
        with_entry(juror(1), case_token(), reveal_end + 1),
        bond,
      ),
      base_case_out(),
    ),
  )
}

test commit_r5_fails_without_entry() fail {
  juror_ok(
    commit_outputs(fixtures.juror_output(1, juror(1), bond), base_case_out()),
  )
}

/// The new entry must come first.
test commit_r5_fails_with_entry_last() fail {
  let j = with_entries(juror(1), 1)
  let appended =
    JurorFields {
      ..j,
      open: list_append(j.open, OpenVote { case: case_token(), reveal_end }),
    }
  juror_ok(
    commit_tx_with(
      1,
      j,
      bond,
      new_case(),
      case_lovelace,
      base_commitment(),
      [
        fixtures.juror_output(1, appended, bond),
        commit_case_out(
          1,
          juror_agent(1),
          new_case(),
          case_lovelace,
          base_commitment(),
        ),
      ],
    ),
  )
}

fn list_append(xs: List<OpenVote>, x: OpenVote) -> List<OpenVote> {
  when xs is {
    [] -> [x]
    [y, ..rest] -> [y, ..list_append(rest, x)]
  }
}

test commit_r5_fails_with_other_prompt_hash() fail {
  juror_ok(
    commit_outputs(
      fixtures.juror_output(
        1,
        JurorFields {
          ..with_entry(juror(1), case_token(), reveal_end),
          prompt_hash: stranger,
        },
        bond,
      ),
      base_case_out(),
    ),
  )
}

// ## §6.9 rule 1: `c.state == Open` and `hi <= c.commit_end`.

test case_commit_r1_ok_at_commit_end() {
  case_ok(
    Transaction {
      ..base_commit(),
      validity_range: range(commit_end - 10 * minute, commit_end),
    },
  )
}

test case_commit_r1_fails_after_commit_end() fail {
  case_ok(
    Transaction {
      ..base_commit(),
      validity_range: range(commit_end - 10 * minute, commit_end + 1),
    },
  )
}

test case_commit_r1_fails_without_upper_bound() fail {
  case_ok(
    Transaction {
      ..base_commit(),
      validity_range: interval.after(commit_end - 10 * minute),
    },
  )
}

test case_commit_r1_fails_when_settling() fail {
  case_ok(commit_to(CaseFields { ..new_case(), state: Settling { share: 1 } }))
}

// ## §6.9 rule 2: the counterpart juror input and its redeemer.

test case_commit_r2_fails_without_juror_input() fail {
  case_ok(
    Transaction {
      ..base_commit(),
      inputs: [case_input(new_case(), case_lovelace), fixtures.fee_input()],
    },
  )
}

test case_commit_r2_fails_when_juror_names_other_case() fail {
  case_ok(
    fixtures.with_redeemer(
      base_commit(),
      juror_ref(1),
      OnJuror(Commit { case: case_token_b() }),
    ),
  )
}

test case_commit_r2_fails_when_juror_reveals() fail {
  case_ok(
    fixtures.with_redeemer(
      base_commit(),
      juror_ref(1),
      OnJuror(Reveal { case: case_token() }),
    ),
  )
}

/// The juror token in an output with a `Case` datum: not a `Juror`.
test case_commit_r2_fails_when_counterpart_datum_is_not_juror() fail {
  let fake =
    Input {
      output_reference: juror_ref(1),
      output: fixtures.jury_output(juror_token(1), case_data(new_case()), bond),
    }
  case_ok(
    Transaction {
      ..base_commit(),
      inputs: [
        case_input(new_case(), case_lovelace),
        fake,
        fixtures.fee_input(),
      ],
    },
  )
}

// ## §6.9 rule 3: the juror is not a party.

test case_commit_r3_fails_for_claimer() fail {
  let j = JurorFields { ..juror(1), agent: fixtures.claimer_did }
  case_ok(commit_of(j, bond))
}

test case_commit_r3_fails_for_auditor() fail {
  let j = JurorFields { ..juror(1), agent: fixtures.auditor_did }
  case_ok(commit_of(j, bond))
}

// ## §6.9 rule 4: one ballot per DID.

/// Juror 2 has the DID of juror 1 and committed first.
test case_commit_r4_fails_for_second_ballot_of_did() fail {
  let first = Ballot { ..committed(2, ClaimerWins), agent: juror_agent(1) }
  case_ok(commit_to(with_ballots(new_case(), [first])))
}

test case_commit_r4_ok_for_other_did() {
  case_ok(commit_to(with_ballots(new_case(), [committed(2, ClaimerWins)])))
}

// ## §6.9 rule 5: `commit` is 32 bytes.

test case_commit_r5_fails_with_31_bytes() fail {
  case_ok(
    commit_tx(
      1,
      juror(1),
      bond,
      new_case(),
      case_lovelace,
      bytearray.drop(base_commitment(), 1),
    ),
  )
}

test case_commit_r5_fails_with_33_bytes() fail {
  case_ok(
    commit_tx(
      1,
      juror(1),
      bond,
      new_case(),
      case_lovelace,
      bytearray.push(base_commitment(), 0),
    ),
  )
}

// ## §6.9 rule 6: the case output.

test case_commit_r6_ok_with_same_lovelace() {
  case_ok(
    commit_outputs(
      base_juror_out(),
      commit_case_out(
        1,
        juror_agent(1),
        new_case(),
        case_lovelace,
        base_commitment(),
      ),
    ),
  )
}

test case_commit_r6_fails_with_less_lovelace() fail {
  case_ok(
    commit_outputs(
      base_juror_out(),
      commit_case_out(
        1,
        juror_agent(1),
        new_case(),
        case_lovelace - 1,
        base_commitment(),
      ),
    ),
  )
}

test case_commit_r6_fails_without_ballot() fail {
  case_ok(
    commit_outputs(
      base_juror_out(),
      fixtures.case_output(new_case(), case_lovelace),
    ),
  )
}

/// The new ballot must come first.
test case_commit_r6_fails_with_ballot_last() fail {
  let c = with_ballots(new_case(), [committed(2, ClaimerWins)])
  let new =
    Ballot {
      agent: juror_agent(1),
      juror: juror_token(1),
      commit: base_commitment(),
      status: Committed,
    }
  case_ok(
    commit_tx_with(
      1,
      juror(1),
      bond,
      c,
      case_lovelace,
      base_commitment(),
      [
        commit_juror_out(1, juror(1), bond, c),
        fixtures.case_output(
          with_ballots(c, [committed(2, ClaimerWins), new]),
          case_lovelace,
        ),
      ],
    ),
  )
}

test case_commit_r6_fails_with_revealed_status() fail {
  let new =
    Ballot {
      agent: juror_agent(1),
      juror: juror_token(1),
      commit: base_commitment(),
      status: fixtures.revealed(1, AuditorWins).status,
    }
  case_ok(
    commit_outputs(
      base_juror_out(),
      fixtures.case_output(with_ballots(new_case(), [new]), case_lovelace),
    ),
  )
}

test case_commit_r6_fails_with_other_agent_in_ballot() fail {
  case_ok(
    commit_outputs(
      base_juror_out(),
      commit_case_out(1, stranger, new_case(), case_lovelace, base_commitment()),
    ),
  )
}

test case_commit_r6_fails_with_changed_fee() fail {
  let new =
    Ballot {
      agent: juror_agent(1),
      juror: juror_token(1),
      commit: base_commitment(),
      status: Committed,
    }
  case_ok(
    commit_outputs(
      base_juror_out(),
      fixtures.case_output(
        CaseFields { ..with_ballots(new_case(), [new]), fee: 1 },
        case_lovelace,
      ),
    ),
  )
}

test commit_base_range_is_in_commit_window() {
  commit_range() == range(opened_at + 10 * minute, opened_at + 20 * minute) && registered_at + params().eligibility_delay <= opened_at
}
