//// SPEC §6.6 juror `Claim { case }` and §6.12 case `CaseClaim { juror }`:
//// one passing and one failing transaction per rule. The base transaction:
//// juror 1 claims its share (5 AP3X) from the case of `claim_case` (fee
//// 15 AP3X, three revealed ballots, `Settling { share: 5 AP3X }`).

use cardano/transaction.{Output, Transaction}
use jury/datum.{CaseFields, with_ballots}
use jury/redeemers.{CaseClaim, Claim, Close, OnCase, OnJuror, TopUp}
use jury/types.{AuditorWins, Ballot, ClaimerWins, Open, Paid}
use jury_tests/fixtures.{
  base_claim, bond, case_input, case_output, case_ref, case_token_b, claim_case,
  claim_case_lovelace, claim_outputs, claim_tx, claim_tx_with, committed, juror,
  juror_input, juror_output, juror_ref, juror_token, paid, revealed, share,
  spend_ok, with_entry,
}

fn juror_ok(tx: Transaction) -> Bool {
  spend_ok(tx, juror_ref(1))
}

fn case_ok(tx: Transaction) -> Bool {
  spend_ok(tx, case_ref())
}

/// The case output of a claim of juror 1 with `ballots` and `lovelace`.
fn case_out(ballots: List<Ballot>, lovelace: Int) -> Output {
  case_output(with_ballots(claim_case(), ballots), lovelace)
}

/// The honest case output of the base claim.
fn base_case_out() -> Output {
  case_out(
    [paid(1, AuditorWins), revealed(2, AuditorWins), revealed(3, ClaimerWins)],
    claim_case_lovelace - share,
  )
}

/// The base claim with these outputs.
fn claim_outputs_as(juror_out: Output, case_out: Output) -> Transaction {
  claim_tx_with(
    1,
    juror(1),
    bond,
    claim_case(),
    claim_case_lovelace,
    [juror_out, case_out],
  )
}

/// The base claim with the case datum `c`; the outputs are those of the
/// base claim with the datum `c` and juror 1 paid.
fn claim_from(c: CaseFields, ballots_after: List<Ballot>) -> Transaction {
  claim_tx_with(
    1,
    juror(1),
    bond,
    c,
    claim_case_lovelace,
    [
      juror_output(1, juror(1), bond + share),
      case_output(with_ballots(c, ballots_after), claim_case_lovelace - share),
    ],
  )
}

test claim_ok_base() {
  let tx = base_claim()
  juror_ok(tx) && case_ok(tx)
}

test claim_ok_for_last_ballot() {
  let tx = claim_tx(3, juror(3), bond, claim_case(), claim_case_lovelace, share)
  spend_ok(tx, juror_ref(3)) && case_ok(tx)
}

// ## §6.6 rule 1: the owner signs.

test claim_r1_fails_without_owner_signature() fail {
  juror_ok(Transaction { ..base_claim(), extra_signatories: [] })
}

// ## §6.6 rule 2: the counterpart case input and its redeemer.

test claim_r2_fails_without_case_input() fail {
  juror_ok(
    Transaction {
      ..base_claim(),
      inputs: [juror_input(1, juror(1), bond), fixtures.fee_input()],
    },
  )
}

test claim_r2_fails_when_case_pays_other_juror() fail {
  juror_ok(
    fixtures.with_redeemer(
      base_claim(),
      case_ref(),
      OnCase(CaseClaim { juror: juror_token(2) }),
    ),
  )
}

test claim_r2_fails_when_case_closes() fail {
  juror_ok(fixtures.with_redeemer(base_claim(), case_ref(), OnCase(Close)))
}

// ## §6.6 rule 3: the juror output keeps the datum and at least the
// lovelace.

test claim_r3_ok_with_more_than_share() {
  juror_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share + 1),
      base_case_out(),
    ),
  )
}

test claim_r3_fails_with_less_lovelace() fail {
  juror_ok(
    claim_outputs_as(juror_output(1, juror(1), bond - 1), base_case_out()),
  )
}

test claim_r3_fails_with_changed_datum() fail {
  juror_ok(
    claim_outputs_as(
      juror_output(1, with_entry(juror(1), case_token_b(), 1), bond + share),
      base_case_out(),
    ),
  )
}

// ## §6.12 rule 1: `Settling { share }`.

test case_claim_r1_fails_when_open() fail {
  let c = CaseFields { ..claim_case(), state: Open }
  case_ok(
    claim_from(
      c,
      [paid(1, AuditorWins), revealed(2, AuditorWins), revealed(3, ClaimerWins)],
    ),
  )
}

// ## §6.12 rule 2: the counterpart juror input and its redeemer.

test case_claim_r2_fails_without_juror_input() fail {
  case_ok(
    Transaction {
      ..base_claim(),
      inputs: [
        case_input(claim_case(), claim_case_lovelace),
        fixtures.fee_input(),
      ],
    },
  )
}

test case_claim_r2_fails_when_juror_claims_other_case() fail {
  case_ok(
    fixtures.with_redeemer(
      base_claim(),
      juror_ref(1),
      OnJuror(Claim { case: case_token_b() }),
    ),
  )
}

test case_claim_r2_fails_when_juror_tops_up() fail {
  case_ok(fixtures.with_redeemer(base_claim(), juror_ref(1), OnJuror(TopUp)))
}

// ## §6.12 rule 3: exactly one ballot with `juror`, and it is `Revealed`.

/// "Claim by a non-revealer": the ballot of juror 1 is `Committed`.
test case_claim_r3_fails_for_committed_ballot() fail {
  let before =
    [
      committed(1, AuditorWins),
      revealed(2, AuditorWins),
      revealed(3, ClaimerWins),
    ]
  case_ok(
    claim_from(
      with_ballots(claim_case(), before),
      [paid(1, AuditorWins), revealed(2, AuditorWins), revealed(3, ClaimerWins)],
    ),
  )
}

/// "Settling claim twice": the ballot of juror 1 is `Paid`.
test case_claim_r3_fails_for_paid_ballot() fail {
  let before =
    [paid(1, AuditorWins), revealed(2, AuditorWins), revealed(3, ClaimerWins)]
  case_ok(claim_from(with_ballots(claim_case(), before), before))
}

test case_claim_r3_fails_without_ballot() fail {
  let before = [revealed(2, AuditorWins), revealed(3, ClaimerWins)]
  case_ok(claim_from(with_ballots(claim_case(), before), before))
}

// ## §6.12 rule 4: the case output.

/// The claimer adds lovelace for the ledger minimum (SPEC amendment 1).
test case_claim_r4_ok_with_more_than_lovelace_minus_share() {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share),
      case_out(
        [
          paid(1, AuditorWins),
          revealed(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        claim_case_lovelace - share + 400_000,
      ),
    ),
  )
}

test case_claim_r4_fails_with_less_than_lovelace_minus_share() fail {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share + 1),
      case_out(
        [
          paid(1, AuditorWins),
          revealed(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        claim_case_lovelace - share - 1,
      ),
    ),
  )
}

test case_claim_r4_fails_when_ballot_stays_revealed() fail {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share),
      case_out(
        [
          revealed(1, AuditorWins),
          revealed(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        claim_case_lovelace - share,
      ),
    ),
  )
}

test case_claim_r4_fails_with_other_vote_in_paid() fail {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share),
      case_out(
        [
          Ballot {
            ..revealed(1, AuditorWins),
            status: Paid { vote: ClaimerWins },
          },
          revealed(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        claim_case_lovelace - share,
      ),
    ),
  )
}

test case_claim_r4_fails_with_other_ballot_paid() fail {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share),
      case_out(
        [
          revealed(1, AuditorWins),
          paid(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        claim_case_lovelace - share,
      ),
    ),
  )
}

// ## §6.12 rule 5: the juror output gets the share.

test case_claim_r5_ok_with_more_than_share() {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share + 1),
      base_case_out(),
    ),
  )
}

test case_claim_r5_fails_with_less_than_share() fail {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share - 1),
      base_case_out(),
    ),
  )
}

/// The claim keeps the empty commit: a case output that sets a commit
/// fails.
test case_claim_r4_fails_when_commit_changes() fail {
  case_ok(
    claim_outputs_as(
      juror_output(1, juror(1), bond + share),
      case_out(
        [
          Ballot {
            ..paid(1, AuditorWins),
            commit: fixtures.commitment(fixtures.case_token(), 1, AuditorWins),
          },
          revealed(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        claim_case_lovelace - share,
      ),
    ),
  )
}

test case_claim_r4_ok_keeps_empty_commit() {
  and {
    paid(1, AuditorWins).commit == #"",
    case_ok(base_claim()),
  }
}

/// The fixture builds the outputs of the base claim as this file expects.
test claim_base_outputs_are_honest() {
  claim_outputs(1, juror(1), bond, claim_case(), claim_case_lovelace, share) == [
    juror_output(1, juror(1), bond + share),
    base_case_out(),
  ]
}
