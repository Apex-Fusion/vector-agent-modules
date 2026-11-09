//// Property tests (aiken-lang/fuzz): the result rule (§8), the commit hash
//// (§7) and the juror lovelace invariant (§6.7: a juror output never holds
//// less than `juror_floor`).

use aiken/collection/list
use aiken/fuzz
use aiken/primitive/bytearray
use cardano/transaction.{Input, OutputReference, Transaction, placeholder}
use jury/commit.{commit_hash, preimage}
use jury/datum.{CaseFields, JurorFields}
use jury/outcome.{add_vote, outcome, votes}
use jury/redeemers.{
  CaseClaim, CaseCommit, CaseReveal, Claim, Commit, OnCase, OnJuror, RequestExit,
  Reveal, Slash, SpendAction, TopUp,
}
use jury/spend_rules
use jury/tokens.{case_prefix, token_name}
use jury/types.{AuditorWins, ClaimerWins, Inconclusive, JuryParams, Tally, Vote}
use jury_tests/fixtures.{
  ap3x, case_input_at, commit_range, fee_input, hour, juror, juror_input,
  juror_output, juror_record_input, juror_ref, juror_token, minute, new_case,
  out_ref, owner, params, range, record_hash, request_range, reveal_end, salt,
  spend_pair, treasury, with_entry,
}

// ## Generators

fn tally() -> Fuzzer<Tally> {
  fuzz.map3(
    fuzz.int_between(0, 40),
    fuzz.int_between(0, 40),
    fuzz.int_between(0, 40),
    fn(claimer, auditor, inconclusive) {
      Tally { claimer, auditor, inconclusive }
    },
  )
}

fn vote() -> Fuzzer<Vote> {
  fuzz.one_of([ClaimerWins, AuditorWins, Inconclusive])
}

/// Parameters with `quorum` in 1..6 and `1/2 < majority_num / majority_den
/// < 1`.
fn strict_params() -> Fuzzer<JuryParams> {
  fuzz.map3(
    fuzz.int_between(1, 6),
    fuzz.int_between(1, 9),
    fuzz.int_between(1, 9),
    fn(quorum, a, b) {
      // num in 2..10 and den in num+1 .. 2*num-1, so 1/2 < num/den < 1.
      let num = a + 1
      let den = num + 1 + b % ( num - 1 )
      JuryParams { ..params(), quorum, majority_num: num, majority_den: den }
    },
  )
}

// ## §8: the result rule

/// The rule of §8, written out.
fn reference_outcome(p: JuryParams, t: Tally) -> Vote {
  let n = t.claimer + t.auditor + t.inconclusive
  let claimer_wins =
    n >= p.quorum && t.claimer * p.majority_den >= n * p.majority_num
  let auditor_wins =
    n >= p.quorum && t.auditor * p.majority_den >= n * p.majority_num
  if claimer_wins {
    ClaimerWins
  } else if auditor_wins {
    AuditorWins
  } else {
    Inconclusive
  }
}

test prop_outcome_matches_rule(t via tally()) {
  outcome(params(), t) == reference_outcome(params(), t)
}

test prop_outcome_matches_rule_for_any_parameters(
  input via fuzz.both(strict_params(), tally()),
) {
  let (p, t) = input
  outcome(p, t) == reference_outcome(p, t)
}

/// Never both sides: with `majority_num / majority_den > 1/2`, both sides
/// cannot reach the majority together.
test prop_outcome_never_both_sides(
  input via fuzz.both(strict_params(), tally()),
) {
  let (p, t) = input
  let n = votes(t)
  !and {
    n > 0,
    t.claimer * p.majority_den >= n * p.majority_num,
    t.auditor * p.majority_den >= n * p.majority_num,
  }
}

test prop_outcome_needs_quorum(t via tally()) {
  if votes(t) < params().quorum {
    outcome(params(), t) == Inconclusive
  } else {
    True
  }
}

/// A winning side has the quorum and at least 2/3 of the revealed votes.
test prop_outcome_winner_has_majority(t via tally()) {
  let n = votes(t)
  when outcome(params(), t) is {
    ClaimerWins -> n >= 3 && 3 * t.claimer >= 2 * n
    AuditorWins -> n >= 3 && 3 * t.auditor >= 2 * n
    Inconclusive ->
      !(n >= 3 && ( 3 * t.claimer >= 2 * n || 3 * t.auditor >= 2 * n ))
  }
}

test prop_add_vote_adds_one(input via fuzz.both(tally(), vote())) {
  let (t, v) = input
  let next = add_vote(t, v)
  and {
    votes(next) == votes(t) + 1,
    when v is {
      ClaimerWins -> next == Tally { ..t, claimer: t.claimer + 1 }
      AuditorWins -> next == Tally { ..t, auditor: t.auditor + 1 }
      Inconclusive -> next == Tally { ..t, inconclusive: t.inconclusive + 1 }
    },
  }
}

/// Why §3 requires `majority_den < 2 * majority_num`: at exactly 1/2 both
/// sides can reach the majority, and `ClaimerWins` would win only because
/// the rule tests it first.
test half_majority_shows_why_section_3_is_strict() {
  let p = JuryParams { ..params(), majority_num: 1, majority_den: 2 }
  let t = Tally { claimer: 2, auditor: 2, inconclusive: 0 }
  and {
    t.claimer * 2 >= votes(t) * 1,
    t.auditor * 2 >= votes(t) * 1,
    outcome(p, t) == ClaimerWins,
  }
}

/// The share of §6.11: `fee / n` is at least 1 and the shares never exceed
/// the fee.
test prop_share_fits_fee(
  input via fuzz.both(
    fuzz.int_between(1, 200),
    fuzz.int_between(0, 100_000_000),
  ),
) {
  let (n, fee) = input
  if fee == 0 || fee < n {
    True
  } else {
    let share = fee / n
    share >= 1 && share * n <= fee && fee - share * n < n
  }
}

// ## §7: the commit hash

/// The five fields of a commitment.
type Fields {
  case: ByteArray,
  agent: ByteArray,
  vote: Vote,
  salt: ByteArray,
  record: ByteArray,
}

fn fields() -> Fuzzer<Fields> {
  fuzz.map5(
    fuzz.bytearray_fixed(32),
    fuzz.bytearray_fixed(32),
    vote(),
    fuzz.bytearray_fixed(32),
    fuzz.bytearray_fixed(32),
    fn(case, agent, vote, salt, record) {
      Fields { case, agent, vote, salt, record }
    },
  )
}

fn hash_of(f: Fields) -> ByteArray {
  commit_hash(f.case, f.agent, f.vote, f.salt, f.record)
}

/// `bytes` with the byte at `index` changed by `delta` (1..255).
fn change_byte(bytes: ByteArray, index: Int, delta: Int) -> ByteArray {
  let i = index % bytearray.length(bytes)
  let byte = bytearray.at(bytes, i)
  bytearray.concat(
    bytearray.take(bytes, i),
    bytearray.push(bytearray.drop(bytes, i + 1), ( byte + delta ) % 256),
  )
}

fn other_vote(v: Vote, step: Int) -> Vote {
  let all = [ClaimerWins, AuditorWins, Inconclusive]
  let index =
    when v is {
      ClaimerWins -> 0
      AuditorWins -> 1
      Inconclusive -> 2
    }
  expect Some(other) = list.at(all, ( index + 1 + step % 2 ) % 3)
  other
}

/// Change exactly one field of `f`.
fn mutate(f: Fields, which: Int, index: Int, delta: Int) -> Fields {
  when which is {
    0 -> Fields { ..f, case: change_byte(f.case, index, delta) }
    1 -> Fields { ..f, agent: change_byte(f.agent, index, delta) }
    2 -> Fields { ..f, vote: other_vote(f.vote, delta) }
    3 -> Fields { ..f, salt: change_byte(f.salt, index, delta) }
    _ -> Fields { ..f, record: change_byte(f.record, index, delta) }
  }
}

/// Changing any one field changes the hash.
test prop_commit_binds_every_field(
  input via fuzz.tuple4(
    fields(),
    fuzz.int_between(0, 4),
    fuzz.int_between(0, 31),
    fuzz.int_between(1, 255),
  ),
) {
  let (f, which, index, delta) = input
  let g = mutate(f, which, index, delta)
  and {
    g != f,
    hash_of(g) != hash_of(f),
  }
}

/// The preimage is 129 bytes, and its parts are at fixed places.
test prop_commit_preimage_layout(f via fields()) {
  let p = preimage(f.case, f.agent, f.vote, f.salt, f.record)
  and {
    bytearray.length(p) == 129,
    bytearray.take(p, 32) == f.case,
    bytearray.slice(p, 32, 63) == f.agent,
    bytearray.slice(p, 65, 96) == f.salt,
    bytearray.drop(p, 97) == f.record,
    hash_of(f) == commit_hash(f.case, f.agent, f.vote, f.salt, f.record),
  }
}

// ## §6.4 to §6.8: the juror lovelace invariant
//
// A random sequence of attempts on the output of juror 1: commits to four
// cases, reveals, slashes, top-ups, claims and an exit request. Each attempt
// is a transaction for the juror spend handler, honest or with a wrong
// amount, a wrong time or a missing entry. The test checks that:
//
// - the handler accepts an attempt exactly when the model below accepts it;
// - after each accepted attempt, the juror output holds at least
//   `juror_floor`.

/// One attempt. `kind`: 0 commit, 1 reveal, 2 slash, 3 top-up, 4 claim,
/// 5 exit request. `case`: 0..3. `x`: the amount change (see `delta`).
type Attempt {
  kind: Int,
  case: Int,
  x: Int,
  early: Bool,
}

fn attempt() -> Fuzzer<Attempt> {
  fuzz.map4(
    fuzz.int_between(0, 5),
    fuzz.int_between(0, 3),
    fuzz.int_between(-3, 6),
    fuzz.bool(),
    fn(kind, case, x, early) { Attempt { kind, case, x, early } },
  )
}

/// The change of the juror output from the honest amount: 0 for half of
/// the values of `x`.
fn delta(x: Int) -> Int {
  if x > 2 {
    0
  } else {
    x
  }
}

type Juror {
  j: JurorFields,
  lovelace: Int,
}

const p: JuryParams = params()

const token_1: ByteArray = juror_token(1)

const ref_1: OutputReference = juror_ref(1)

const owner_1: ByteArray = owner(1)

const record_1: Input = juror_record_input(1)

const fee: Input = fee_input()

const case_names: List<ByteArray> =
  [
    token_name(case_prefix, out_ref(0x70, 0)),
    token_name(case_prefix, out_ref(0x71, 0)),
    token_name(case_prefix, out_ref(0x72, 0)),
    token_name(case_prefix, out_ref(0x73, 0)),
  ]

/// The case inputs: case `k` ends its reveal window `k` hours after the
/// test case.
const case_inputs: List<Input> =
  [
    case_input_at(
      out_ref(0x80, 0),
      token_name(case_prefix, out_ref(0x70, 0)),
      CaseFields { ..new_case(), reveal_end },
      10_000_000,
    ),
    case_input_at(
      out_ref(0x81, 0),
      token_name(case_prefix, out_ref(0x71, 0)),
      CaseFields { ..new_case(), reveal_end: reveal_end + hour },
      10_000_000,
    ),
    case_input_at(
      out_ref(0x82, 0),
      token_name(case_prefix, out_ref(0x72, 0)),
      CaseFields { ..new_case(), reveal_end: reveal_end + 2 * hour },
      10_000_000,
    ),
    case_input_at(
      out_ref(0x83, 0),
      token_name(case_prefix, out_ref(0x73, 0)),
      CaseFields { ..new_case(), reveal_end: reveal_end + 3 * hour },
      10_000_000,
    ),
  ]

fn case_name(k: Int) -> ByteArray {
  expect Some(name) = list.at(case_names, k)
  name
}

fn case_input(k: Int) -> Input {
  expect Some(input) = list.at(case_inputs, k)
  input
}

fn reveal_end_of(k: Int) -> Int {
  reveal_end + k * hour
}

fn floor(_s: Juror) -> Int {
  p.juror_floor
}

/// The slash amount of §6.7.
fn amount_of(s: Juror) -> Int {
  if p.slash < s.lovelace - p.juror_floor {
    p.slash
  } else {
    s.lovelace - p.juror_floor
  }
}

fn entries(s: Juror, case: ByteArray) -> Int {
  list.count(s.j.open, fn(e) { e.case == case })
}

fn without(s: Juror, case: ByteArray) -> JurorFields {
  JurorFields { ..s.j, open: list.filter(s.j.open, fn(e) { e.case != case }) }
}

/// The model of the juror rules: the next state, when the attempt is valid.
fn model(s: Juror, a: Attempt) -> Option<Juror> {
  let case = case_name(a.case)
  let d = delta(a.x)
  when a.kind is {
    0 ->
      if and {
        entries(s, case) == 0,
        s.j.exit_after == None,
        s.lovelace >= p.bond_min,
        d == 0,
      } {
        Some(Juror { ..s, j: with_entry(s.j, case, reveal_end_of(a.case)) })
      } else {
        None
      }
    1 ->
      if entries(s, case) == 1 && d == 0 {
        Some(Juror { ..s, j: without(s, case) })
      } else {
        None
      }
    2 ->
      if entries(s, case) == 1 && d == 0 && !a.early {
        Some(Juror { j: without(s, case), lovelace: s.lovelace - amount_of(s) })
      } else {
        None
      }
    3 ->
      if a.x > 0 {
        Some(Juror { ..s, lovelace: s.lovelace + a.x * ap3x })
      } else {
        None
      }
    4 ->
      if a.x >= 0 {
        Some(Juror { ..s, lovelace: s.lovelace + a.x * ap3x })
      } else {
        None
      }
    _ ->
      if s.j.exit_after == None && d == 0 {
        Some(Juror { ..s, j: exit_requested(s.j) })
      } else {
        None
      }
  }
}

fn exit_requested(j: JurorFields) -> JurorFields {
  JurorFields { ..j, exit_after: Some(request_hi + p.exit_notice) }
}

/// `hi` of `request_range`.
const request_hi: Int = 1_793_577_600_000

/// The transaction of the attempt, and the redeemer of the juror output.
fn transaction(s: Juror, a: Attempt) -> (SpendAction, Transaction) {
  let case = case_name(a.case)
  let d = delta(a.x)
  let own = juror_input(1, s.j, s.lovelace)
  let base =
    Transaction {
      ..placeholder,
      inputs: [own, fee],
      extra_signatories: [owner_1],
      validity_range: range(
        reveal_end + 5 * hour,
        reveal_end + 5 * hour + minute,
      ),
    }
  when a.kind is {
    0 -> {
      let action = OnJuror(Commit { case })
      let tx =
        Transaction {
          ..base,
          inputs: [case_input(a.case), own, fee],
          reference_inputs: [record_1],
          outputs: [
            juror_output(
              1,
              with_entry(s.j, case, reveal_end_of(a.case)),
              s.lovelace + d,
            ),
          ],
          redeemers: [
            spend_pair(
              case_input(a.case).output_reference,
              OnCase(CaseCommit { juror: token_1, commit: record_hash }),
            ),
            spend_pair(ref_1, action),
          ],
          validity_range: commit_range(),
        }
      (action, tx)
    }
    1 -> {
      let action = OnJuror(Reveal { case })
      let tx =
        Transaction {
          ..base,
          inputs: [case_input(a.case), own, fee],
          outputs: [juror_output(1, without(s, case), s.lovelace + d)],
          redeemers: [
            spend_pair(
              case_input(a.case).output_reference,
              OnCase(
                CaseReveal {
                  juror: token_1,
                  vote: AuditorWins,
                  salt: salt(1),
                  record_hash,
                },
              ),
            ),
            spend_pair(ref_1, action),
          ],
        }
      (action, tx)
    }
    2 -> {
      let action = OnJuror(Slash { case })
      let lo =
        if a.early {
          reveal_end_of(a.case) - 1
        } else {
          reveal_end_of(a.case)
        }
      let tx =
        Transaction {
          ..base,
          outputs: [
            juror_output(1, without(s, case), s.lovelace - amount_of(s) + d),
            fixtures.tagged_output(treasury(), ref_1, p.slash),
          ],
          redeemers: [spend_pair(ref_1, action)],
          extra_signatories: [],
          validity_range: range(lo, lo + minute),
        }
      (action, tx)
    }
    3 -> {
      let action = OnJuror(TopUp)
      let tx =
        Transaction {
          ..base,
          outputs: [juror_output(1, s.j, s.lovelace + a.x * ap3x)],
          redeemers: [spend_pair(ref_1, action)],
        }
      (action, tx)
    }
    4 -> {
      let action = OnJuror(Claim { case })
      let tx =
        Transaction {
          ..base,
          inputs: [case_input(a.case), own, fee],
          outputs: [juror_output(1, s.j, s.lovelace + a.x * ap3x)],
          redeemers: [
            spend_pair(
              case_input(a.case).output_reference,
              OnCase(CaseClaim { juror: token_1 }),
            ),
            spend_pair(ref_1, action),
          ],
        }
      (action, tx)
    }
    _ -> {
      let action = OnJuror(RequestExit)
      let tx =
        Transaction {
          ..base,
          outputs: [juror_output(1, exit_requested(s.j), s.lovelace + d)],
          redeemers: [spend_pair(ref_1, action)],
          validity_range: request_range(),
        }
      (action, tx)
    }
  }
}

/// One attempt: the handler must agree with the model, and the invariant
/// must hold after an accepted attempt.
fn step(s: Juror, a: Attempt) -> Juror {
  let (action, tx) = transaction(s, a)
  let accepted = spend_rules.validate(p, action, ref_1, tx)
  when model(s, a) is {
    Some(next) -> {
      expect accepted
      expect next.lovelace >= floor(next)
      next
    }
    None -> {
      expect !accepted
      s
    }
  }
}

test prop_juror_lovelace_invariant(
  input via fuzz.both(
    fuzz.int_between(250, 320),
    fuzz.list_between(attempt(), 1, 8),
  ),
) {
  let (start, attempts) = input
  let s0 = Juror { j: juror(1), lovelace: start * ap3x }
  let last = list.foldl(attempts, s0, fn(a, s) { step(s, a) })
  last.lovelace >= floor(last)
}

/// A long honest life: commit to the four cases, slash two, reveal two,
/// claim, top up, commit again. The invariant holds at each step.
test juror_invariant_on_a_long_sequence() {
  let s0 = Juror { j: juror(1), lovelace: 250 * ap3x }
  let plan =
    [
      Attempt { kind: 0, case: 0, x: 0, early: False },
      Attempt { kind: 0, case: 1, x: 0, early: False },
      Attempt { kind: 0, case: 2, x: 0, early: False },
      Attempt { kind: 0, case: 3, x: 0, early: False },
      Attempt { kind: 2, case: 0, x: 0, early: False },
      Attempt { kind: 2, case: 1, x: 0, early: False },
      Attempt { kind: 1, case: 2, x: 0, early: False },
      Attempt { kind: 1, case: 3, x: 0, early: False },
      Attempt { kind: 4, case: 2, x: 3, early: False },
      Attempt { kind: 0, case: 0, x: 0, early: False },
      Attempt { kind: 3, case: 0, x: 50, early: False },
      Attempt { kind: 0, case: 0, x: 0, early: False },
    ]
  let last = list.foldl(plan, s0, fn(a, s) { step(s, a) })
  and {
    // 250 - 2 * 25 + 3 = 203 AP3X is below bond_min, so the second commit
    // to case 0 fails. After the top-up of 50 AP3X it passes.
    last.lovelace == 253 * ap3x,
    list.length(last.j.open) == 1,
  }
}
