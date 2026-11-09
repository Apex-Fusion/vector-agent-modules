//// Execution units of the handlers. The transactions are constants, built
//// at compile time, so the units of each test are the units of one handler
//// run (the rules in `lib/jury`). They do not include the decoding of the
//// script context by the validator wrapper.
////
//// A transaction with a counterpart runs two handlers: the juror handler and
//// the case handler. Each has its own test.

use cardano/transaction.{OutputReference, Transaction}
use jury/datum.{CaseFields, with_ballots}
use jury/mint_rules
use jury/redeemers.{
  Burn, CaseClaim, CaseCommit, Claim, Close, Commit, Exit, Finalize, OnCase,
  OnJuror, Register, RequestExit, Reveal, Slash, SpendAction, TopUp,
}
use jury/spend_rules
use jury/types.{AuditorWins, Ballot, JuryParams, Tally}
use jury_tests/fixtures.{
  base_commit, base_open, base_register, bond, case_lovelace, case_ref,
  case_seed, case_token, claim_case_lovelace, claim_tx, close_tx, commit_tx,
  commitment, committed, committed_juror, exit_tx, finalize_tx, juror, juror_ref,
  juror_seed, juror_token, jury, leaving_juror, new_case, other_ballots, paid,
  params, range, request_exit_tx, reveal_end, reveal_tx, revealed, settling,
  share, slash_tx, top_up_tx,
}

const p: JuryParams = params()

fn spend(action: SpendAction, own: OutputReference, tx: Transaction) -> Bool {
  spend_rules.validate(p, action, own, tx)
}

// ## Register and Open

const register_tx: Transaction = base_register()

test bench_register() {
  mint_rules.validate(p, Register { seed: juror_seed(1) }, jury, register_tx)
}

const open_tx: Transaction = base_open()

test bench_open() {
  mint_rules.validate(p, redeemers.Open { seed: case_seed() }, jury, open_tx)
}

// ## Commit with 0 and 20 existing ballots

const commit_0: Transaction = base_commit()

const juror_1: ByteArray = juror_token(1)

const case_name: ByteArray = case_token()

const commit_1: ByteArray = commitment(case_token(), 1, AuditorWins)

test bench_commit_0_juror() {
  spend(OnJuror(Commit { case: case_name }), juror_ref(1), commit_0)
}

test bench_commit_0_case() {
  spend(
    OnCase(CaseCommit { juror: juror_1, commit: commit_1 }),
    case_ref(),
    commit_0,
  )
}

const case_20: CaseFields = with_ballots(new_case(), other_ballots(20))

const commit_20: Transaction =
  commit_tx(1, juror(1), bond, case_20, case_lovelace + 20 * 470_000, commit_1)

test bench_commit_20_juror() {
  spend(OnJuror(Commit { case: case_name }), juror_ref(1), commit_20)
}

test bench_commit_20_case() {
  spend(
    OnCase(CaseCommit { juror: juror_1, commit: commit_1 }),
    case_ref(),
    commit_20,
  )
}

// ## Reveal with 20 ballots (the revealing ballot last)

const reveal_case_20: CaseFields =
  with_ballots(
    new_case(),
    list_append(other_ballots(19), committed(1, AuditorWins)),
  )

fn list_append(xs: List<Ballot>, x: Ballot) -> List<Ballot> {
  when xs is {
    [] -> [x]
    [y, ..rest] -> [y, ..list_append(rest, x)]
  }
}

const reveal_20: Transaction =
  reveal_tx(
    1,
    committed_juror(1),
    bond,
    reveal_case_20,
    case_lovelace + 20 * 470_000,
    AuditorWins,
  )

test bench_reveal_20_juror() {
  spend(OnJuror(Reveal { case: case_name }), juror_ref(1), reveal_20)
}

test bench_reveal_20_case() {
  spend(OnCase(fixtures.reveal_action(1, AuditorWins)), case_ref(), reveal_20)
}

// ## Finalize: burn path and Settling path, with 0 and 20 ballots

const tally_3: Tally = Tally { claimer: 1, auditor: 2, inconclusive: 0 }

const finalize_burn_0: Transaction =
  finalize_tx(
    CaseFields { ..new_case(), tally: tally_3 },
    case_lovelace,
    AuditorWins,
  )

const finalize_burn_20: Transaction =
  finalize_tx(
    CaseFields { ..with_ballots(new_case(), other_ballots(20)), tally: tally_3 },
    case_lovelace,
    AuditorWins,
  )

const finalize_settle_20: Transaction =
  finalize_tx(
    CaseFields {
      ..with_ballots(new_case(), other_ballots(20)),
      tally: tally_3,
      fee: 15_000_000,
    },
    25_000_000,
    AuditorWins,
  )

test bench_finalize_burn_0() {
  spend(OnCase(Finalize { result: AuditorWins }), case_ref(), finalize_burn_0)
}

test bench_finalize_burn_20() {
  spend(OnCase(Finalize { result: AuditorWins }), case_ref(), finalize_burn_20)
}

test bench_finalize_settle_20() {
  spend(
    OnCase(Finalize { result: AuditorWins }),
    case_ref(),
    finalize_settle_20,
  )
}

// ## Claim with 20 ballots (the claiming ballot last)

const claim_case_20: CaseFields =
  settling(
    CaseFields {
      ..with_ballots(
        new_case(),
        list_append(other_ballots(19), revealed(1, AuditorWins)),
      ),
      fee: 15_000_000,
      tally: tally_3,
    },
    share,
  )

const claim_20: Transaction =
  claim_tx(1, juror(1), bond, claim_case_20, claim_case_lovelace, share)

test bench_claim_20_juror() {
  spend(OnJuror(Claim { case: case_name }), juror_ref(1), claim_20)
}

test bench_claim_20_case() {
  spend(OnCase(CaseClaim { juror: juror_1 }), case_ref(), claim_20)
}

// ## Slash, RequestExit, Exit, TopUp, Close

const slash_1: Transaction = slash_tx(1, committed_juror(1), bond, case_token())

test bench_slash() {
  spend(OnJuror(Slash { case: case_name }), juror_ref(1), slash_1)
}

const request_1: Transaction = request_exit_tx(1, juror(1), bond)

test bench_request_exit() {
  spend(OnJuror(RequestExit), juror_ref(1), request_1)
}

const exit_1: Transaction = exit_tx(1, leaving_juror(1), bond)

test bench_exit() {
  spend(OnJuror(Exit), juror_ref(1), exit_1)
}

test bench_exit_burn_policy() {
  mint_rules.validate(p, Burn, jury, exit_1)
}

const top_up_1: Transaction = top_up_tx(1, juror(1), bond, 1_000_000)

test bench_top_up() {
  spend(OnJuror(TopUp), juror_ref(1), top_up_1)
}

const close_20: Transaction =
  close_tx(
    settling(
      CaseFields {
        ..with_ballots(
          new_case(),
          list_append(other_ballots(19), paid(1, AuditorWins)),
        ),
        fee: 15_000_000,
        tally: tally_3,
      },
      share,
    ),
    case_lovelace,
    range(reveal_end + 86_400_000, reveal_end + 86_460_000),
  )

test bench_close_20() {
  spend(OnCase(Close), case_ref(), close_20)
}
