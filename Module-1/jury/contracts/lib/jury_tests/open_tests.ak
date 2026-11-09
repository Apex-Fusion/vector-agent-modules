//// SPEC §6.2 `Open { seed }`: one passing and one failing transaction per
//// rule. The base transaction opens the test case (no fee, 10 AP3X) for the
//// test challenge (`PendingOracle`) of the test claim (`Challenged`).

use aiken/interval
use cardano/address.{Address, Inline, Pointer, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{DatumHash, Input, Output, Transaction}
use jury/datum.{CaseFields, juror_data, with_ballots}
use jury/module1 as m1
use jury/redeemers
use jury/tokens.{juror_prefix, token_name}
use jury/types.{AuditorWins, Settling, Tally}
use jury_tests/fixtures.{
  base_open, case_lovelace, case_output, case_seed, case_token, challenge_datum,
  challenge_input, challenge_token, claim_datum, claim_input, claim_token,
  committed, deadline, hour, juror, jury, m1_challenge, m1_claim, m1_refs,
  new_case, open_range, open_tx, opened_at, other_policy, other_script, out_ref,
  params, script_address, stranger, wallet_input,
}

fn open_ok(tx: Transaction) -> Bool {
  fixtures.mint_ok(tx, redeemers.Open { seed: case_seed() })
}

/// The base opening with the case datum `c` and 10 AP3X.
fn open_with(c: CaseFields) -> Transaction {
  open_tx(c, case_lovelace, m1_refs())
}

/// The base opening with the reference inputs `refs`.
fn open_refs(refs: List<Input>) -> Transaction {
  open_tx(new_case(), case_lovelace, refs)
}

/// The base opening with the case output `output`.
fn open_output(output: Output) -> Transaction {
  Transaction { ..base_open(), outputs: [output] }
}

fn base_output() -> Output {
  case_output(new_case(), case_lovelace)
}

/// The base claim input (`Challenged`).
fn claim_ref() -> Input {
  claim_input(claim_datum(m1.Challenged))
}

/// The base challenge input (`PendingOracle`).
fn challenge_ref() -> Input {
  challenge_input(challenge_datum(m1.PendingOracle))
}

test open_ok_base() {
  open_ok(base_open())
}

test open_ok_with_reference_inputs_in_other_order() {
  open_ok(open_refs([claim_ref(), challenge_ref()]))
}

// ## Rule 1: the transaction spends `seed`.

test open_r1_fails_without_seed() fail {
  open_ok(
    Transaction { ..base_open(), inputs: [wallet_input(out_ref(0x77, 0))] },
  )
}

// ## Rule 2: exactly one jury token, named `cas_` + hash of `seed`.

test open_r2_fails_with_quantity_2() fail {
  open_ok(
    Transaction {
      ..base_open(),
      mint: assets.from_asset(jury, case_token(), 2),
    },
  )
}

test open_r2_fails_with_a_second_token() fail {
  open_ok(
    Transaction {
      ..base_open(),
      mint: assets.from_asset(jury, case_token(), 1)
        |> assets.add(jury, fixtures.case_token_b(), 1),
    },
  )
}

/// The mint field has the `jrr_` name of the seed instead of the `cas_`
/// name.
test open_r2_fails_with_juror_prefix() fail {
  open_ok(
    Transaction {
      ..base_open(),
      mint: assets.from_asset(jury, token_name(juror_prefix, case_seed()), 1),
    },
  )
}

// ## Rule 3: one output at the jury address with lovelace and the token
// only, an inline datum `Case` and no reference script.

test open_r3_fails_at_other_script() fail {
  open_ok(
    open_output(
      Output { ..base_output(), address: script_address(other_script) },
    ),
  )
}

test open_r3_fails_with_stake_credential() fail {
  open_ok(
    open_output(
      Output {
        ..base_output(),
        address: Address {
          payment_credential: Script(jury),
          stake_credential: Some(Inline(Script(other_script))),
        },
      },
    ),
  )
}

test open_r3_fails_with_extra_token() fail {
  open_ok(
    open_output(
      fixtures.plus(base_output(), assets.from_asset(other_policy, "x", 1)),
    ),
  )
}

test open_r3_fails_with_datum_hash() fail {
  open_ok(open_output(Output { ..base_output(), datum: DatumHash(stranger) }))
}

test open_r3_fails_with_reference_script() fail {
  open_ok(
    open_output(
      Output { ..base_output(), reference_script: Some(other_script) },
    ),
  )
}

test open_r3_fails_with_juror_datum() fail {
  open_ok(
    open_output(
      fixtures.jury_output(case_token(), juror_data(juror(1)), case_lovelace),
    ),
  )
}

// ## Rule 4: the challenge reference input.

test open_r4_fails_without_challenge() fail {
  open_ok(open_refs([claim_ref()]))
}

/// "Wrong address": the challenge token and datum at another script.
test open_r4_fails_with_challenge_at_other_script() fail {
  let ch = challenge_ref()
  open_ok(
    open_refs(
      [
        Input {
          ..ch,
          output: Output { ..ch.output, address: script_address(other_script) },
        },
        claim_ref(),
      ],
    ),
  )
}

/// "Wrong policy": the challenge address holds a token with the challenge
/// name under another policy.
test open_r4_fails_with_token_of_other_policy() fail {
  let ch = challenge_ref()
  open_ok(
    open_refs(
      [
        Input {
          ..ch,
          output: Output {
            ..ch.output,
            value: assets.from_lovelace(50_000_000)
              |> assets.add(other_policy, challenge_token, 1),
          },
        },
        claim_ref(),
      ],
    ),
  )
}

test open_r4_fails_with_two_challenge_tokens() fail {
  let ch = challenge_ref()
  open_ok(
    open_refs(
      [
        Input {
          ..ch,
          output: fixtures.plus(
            ch.output,
            assets.from_asset(m1_challenge, stranger, 1),
          ),
        },
        claim_ref(),
      ],
    ),
  )
}

test open_r4_fails_with_challenge_of_other_name() fail {
  open_ok(open_with(CaseFields { ..new_case(), challenge: stranger }))
}

/// "Wrong state": the challenge is `PendingJury`.
test open_r4_fails_when_challenge_is_pending_jury() fail {
  open_ok(
    open_refs([challenge_input(challenge_datum(m1.PendingJury)), claim_ref()]),
  )
}

test open_r4_fails_when_challenge_is_resolved() fail {
  open_ok(
    open_refs(
      [
        challenge_input(
          challenge_datum(
            m1.Resolved {
              verdict: m1.AuditorWins,
              refund_to: fixtures.refund_address(),
            },
          ),
        ),
        claim_ref(),
      ],
    ),
  )
}

test open_r4_fails_with_challenge_datum_hash() fail {
  let ch = challenge_ref()
  open_ok(
    open_refs(
      [
        Input {
          ..ch,
          output: Output { ..ch.output, datum: DatumHash(stranger) },
        },
        claim_ref(),
      ],
    ),
  )
}

test open_r4_fails_with_challenge_as_spent_input() fail {
  open_ok(
    Transaction {
      ..open_refs([claim_ref()]),
      inputs: [wallet_input(case_seed()), challenge_ref()],
    },
  )
}

// ## Rule 5: the claim reference input.

test open_r5_fails_without_claim() fail {
  open_ok(open_refs([challenge_ref()]))
}

test open_r5_fails_with_claim_at_other_script() fail {
  let cl = claim_ref()
  open_ok(
    open_refs(
      [
        challenge_ref(),
        Input {
          ..cl,
          output: Output { ..cl.output, address: script_address(other_script) },
        },
      ],
    ),
  )
}

/// "Claim token mismatch": the claim input holds another claim token.
test open_r5_fails_with_claim_token_mismatch() fail {
  let cl = claim_ref()
  open_ok(
    open_refs(
      [
        challenge_ref(),
        Input {
          ..cl,
          output: Output {
            ..cl.output,
            value: assets.from_lovelace(50_000_000)
              |> assets.add(m1_claim, stranger, 1),
          },
        },
      ],
    ),
  )
}

test open_r5_fails_when_claim_is_open() fail {
  open_ok(open_refs([challenge_ref(), claim_input(claim_datum(m1.Open))]))
}

test open_r5_fails_when_claim_is_validated() fail {
  open_ok(open_refs([challenge_ref(), claim_input(claim_datum(m1.Validated))]))
}

// ## Rule 6: the copied fields.

test open_r6_fails_with_other_claim() fail {
  open_ok(open_with(CaseFields { ..new_case(), claim: stranger }))
}

test open_r6_fails_with_other_claimer() fail {
  open_ok(open_with(CaseFields { ..new_case(), claimer: stranger }))
}

test open_r6_fails_with_other_auditor() fail {
  open_ok(open_with(CaseFields { ..new_case(), auditor: stranger }))
}

test open_r6_fails_with_other_claim_hash() fail {
  open_ok(open_with(CaseFields { ..new_case(), claim_hash: stranger }))
}

/// `challenge_out` must be the reference of the challenge input.
test open_r6_fails_with_other_challenge_out() fail {
  open_ok(
    open_with(CaseFields { ..new_case(), challenge_out: out_ref(0xc8, 7) }),
  )
}

test open_r6_fails_with_other_evidence_hash() fail {
  open_ok(open_with(CaseFields { ..new_case(), evidence_hash: stranger }))
}

/// `claim_out` must be the reference of the claim input.
test open_r6_fails_with_other_claim_out() fail {
  open_ok(open_with(CaseFields { ..new_case(), claim_out: out_ref(0xc8, 0) }))
}

test open_r6_fails_with_other_deadline() fail {
  open_ok(open_with(CaseFields { ..new_case(), deadline: deadline + 1 }))
}

/// Module-1 does not check the length of the DIDs and hashes that it
/// stores; the case needs 32 bytes (SPEC amendment 4).
test open_r6_fails_with_31_byte_claimer() fail {
  let short = #"c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1"
  open_ok(
    open_tx(
      CaseFields { ..new_case(), claimer: short },
      case_lovelace,
      [
        challenge_ref(),
        claim_input(
          m1.ClaimDatum { ..claim_datum(m1.Challenged), claimer_did: short },
        ),
      ],
    ),
  )
}

test open_r6_fails_with_31_byte_evidence_hash() fail {
  let short = #"d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"
  open_ok(
    open_tx(
      CaseFields { ..new_case(), evidence_hash: short },
      case_lovelace,
      [
        challenge_input(
          m1.ChallengeDatum {
            ..challenge_datum(m1.PendingOracle),
            evidence_hash: short,
          },
        ),
        claim_ref(),
      ],
    ),
  )
}

/// A huge `resolution_deadline` Int.
const huge: Int = 1_000_000_000_000_000_000_000_000

/// The case of a challenge with the huge `resolution_deadline`.
fn huge_deadline(case_deadline: Int) -> Transaction {
  open_tx(
    CaseFields { ..new_case(), deadline: case_deadline },
    case_lovelace,
    [
      challenge_input(
        m1.ChallengeDatum {
          ..challenge_datum(m1.PendingOracle),
          resolution_deadline: huge,
        },
      ),
      claim_ref(),
    ],
  )
}

/// The case deadline is capped at 365 days after the opening.
test open_r7_ok_with_capped_deadline() {
  open_ok(huge_deadline(opened_at + 31_536_000_000))
}

test open_r7_fails_with_uncapped_deadline() fail {
  open_ok(huge_deadline(fixtures.challenged_at + huge))
}

// ## Rule 7: time.

test open_r7_fails_with_unbounded_range() fail {
  open_ok(
    Transaction { ..base_open(), validity_range: interval.before(opened_at) },
  )
}

test open_r7_fails_with_range_over_one_hour() fail {
  open_ok(
    Transaction {
      ..base_open(),
      validity_range: interval.between(opened_at - hour - 1, opened_at),
    },
  )
}

test open_r7_fails_when_opened_at_is_not_hi() fail {
  open_ok(
    Transaction {
      ..base_open(),
      validity_range: interval.between(opened_at - hour, opened_at - 1),
    },
  )
}

test open_r7_fails_with_other_commit_end() fail {
  open_ok(
    open_with(CaseFields { ..new_case(), commit_end: fixtures.commit_end + 1 }),
  )
}

test open_r7_fails_with_other_reveal_end() fail {
  open_ok(
    open_with(CaseFields { ..new_case(), reveal_end: fixtures.reveal_end + 1 }),
  )
}

/// A challenge whose deadline is `opened_at + left`.
fn time_left(left: Int) -> Transaction {
  let ch = challenge_datum(m1.PendingOracle)
  let d = opened_at + left
  open_tx(
    CaseFields { ..new_case(), deadline: d },
    case_lovelace,
    [
      challenge_input(
        m1.ChallengeDatum { ..ch, resolution_deadline: d - ch.challenged_at },
      ),
      claim_ref(),
    ],
  )
}

/// Exactly 6 hours left: `reveal_end + resolve_margin == deadline`.
test open_r7_ok_with_six_hours_left() {
  open_ok(time_left(6 * hour))
}

/// Less than 6 hours left.
test open_r7_fails_with_less_than_six_hours_left() fail {
  open_ok(time_left(6 * hour - 1))
}

// ## Rule 8: start values.

test open_r8_ok_with_fee() {
  open_ok(
    open_tx(
      CaseFields { ..new_case(), fee: 5_000_000 },
      case_lovelace,
      m1_refs(),
    ),
  )
}

/// `lovelace(output) == fee`.
test open_r8_ok_with_fee_equal_to_lovelace() {
  open_ok(
    open_tx(
      CaseFields { ..new_case(), fee: case_lovelace },
      case_lovelace,
      m1_refs(),
    ),
  )
}

test open_r8_fails_with_fee_above_lovelace() fail {
  open_ok(
    open_tx(
      CaseFields { ..new_case(), fee: case_lovelace + 1 },
      case_lovelace,
      m1_refs(),
    ),
  )
}

test open_r8_fails_with_negative_fee() fail {
  open_ok(open_with(CaseFields { ..new_case(), fee: -1 }))
}

test open_r8_fails_with_a_ballot() fail {
  open_ok(open_with(with_ballots(new_case(), [committed(1, AuditorWins)])))
}

test open_r8_fails_with_tally() fail {
  open_ok(
    open_with(
      CaseFields {
        ..new_case(),
        tally: Tally { claimer: 0, auditor: 1, inconclusive: 0 },
      },
    ),
  )
}

test open_r8_fails_when_settling() fail {
  open_ok(open_with(CaseFields { ..new_case(), state: Settling { share: 1 } }))
}

/// The ballots must be the empty list: any other data fails.
test open_r8_fails_with_ballots_not_a_list() fail {
  let zero: Data = 0
  open_ok(open_with(CaseFields { ..new_case(), ballots: zero }))
}

// ## Rule 9: `refund_to` is a valid ledger address, not at the jury.

fn open_refund(refund_to: Address) -> Transaction {
  open_with(CaseFields { ..new_case(), refund_to })
}

test open_r9_ok_with_script_refund_address() {
  open_ok(open_refund(script_address(other_script)))
}

test open_r9_ok_with_stake_key() {
  open_ok(
    open_refund(
      Address {
        payment_credential: VerificationKey(fixtures.refund_key_hash),
        stake_credential: Some(
          Inline(VerificationKey(fixtures.wallet_key_hash)),
        ),
      },
    ),
  )
}

test open_r9_ok_with_stake_script() {
  open_ok(
    open_refund(
      Address {
        payment_credential: VerificationKey(fixtures.refund_key_hash),
        stake_credential: Some(Inline(Script(other_script))),
      },
    ),
  )
}

test open_r9_fails_at_jury_address() fail {
  open_ok(open_refund(script_address(jury)))
}

test open_r9_fails_at_jury_script_with_stake_key() fail {
  open_ok(
    open_refund(
      Address {
        payment_credential: Script(jury),
        stake_credential: Some(
          Inline(VerificationKey(fixtures.wallet_key_hash)),
        ),
      },
    ),
  )
}

test open_r9_fails_with_short_payment_hash() fail {
  open_ok(
    open_refund(
      fixtures.key_address(
        #"5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f",
      ),
    ),
  )
}

test open_r9_fails_with_long_script_hash() fail {
  open_ok(open_refund(script_address(stranger)))
}

test open_r9_fails_with_pointer_stake() fail {
  open_ok(
    open_refund(
      Address {
        payment_credential: VerificationKey(fixtures.refund_key_hash),
        stake_credential: Some(
          Pointer { slot_number: 1, transaction_index: 2, certificate_index: 3 },
        ),
      },
    ),
  )
}

/// `refund_to` must not use the payment credential of the claimer (SPEC
/// amendment 7).
test open_r9_fails_with_claimer_credential() fail {
  open_ok(open_refund(fixtures.key_address(fixtures.owner(201))))
}

/// Also with another stake credential.
test open_r9_fails_with_claimer_credential_and_stake_key() fail {
  open_ok(
    open_refund(
      Address {
        payment_credential: VerificationKey(fixtures.owner(201)),
        stake_credential: Some(
          Inline(VerificationKey(fixtures.wallet_key_hash)),
        ),
      },
    ),
  )
}

/// `refund_to` must not use the payment credential of the auditor.
test open_r9_fails_with_auditor_credential() fail {
  open_ok(open_refund(fixtures.key_address(fixtures.owner(202))))
}

/// The credentials of the parties differ from the base `refund_to`.
test open_r9_ok_with_other_key_than_parties() {
  and {
    claim_datum(m1.Challenged).claimer_credential == VerificationKey(
      fixtures.owner(201),
    ),
    challenge_datum(m1.PendingOracle).auditor_credential == VerificationKey(
      fixtures.owner(202),
    ),
    open_ok(open_refund(fixtures.key_address(fixtures.owner(203)))),
  }
}

test open_r9_fails_with_short_stake_hash() fail {
  open_ok(
    open_refund(
      Address {
        payment_credential: VerificationKey(fixtures.refund_key_hash),
        stake_credential: Some(Inline(VerificationKey(#"3e3e"))),
      },
    ),
  )
}

test open_ok_has_opening_window_of_ten_minutes() {
  open_range() == interval.between(opened_at - 600_000, opened_at)
}

test open_ok_params_are_mainnet_values() {
  params().commit_window + params().reveal_window + params().resolve_margin == 6 * hour
}

test open_ok_claim_token_is_in_challenge() {
  challenge_datum(m1.PendingOracle).claim_token == claim_token
}
