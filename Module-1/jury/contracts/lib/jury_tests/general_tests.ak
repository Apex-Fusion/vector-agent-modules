//// SPEC §6, the general rules for every spend, and §6.3 `Burn`.
////
//// The base transactions: a top-up of juror 1 (a juror spend without a
//// counterpart) and the base commit (a juror spend and a case spend).

use cardano/address.{Address, Inline, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  DatumHash, Input, NoDatum, Output, OutputReference, Transaction,
}
use jury/datum.{case_data}
use jury/mint_rules
use jury/redeemers.{Burn, CaseCommit, Commit, OnCase, OnJuror, Register, TopUp}
use jury/spend_rules
use jury/types.{AuditorWins}
use jury_tests/fixtures.{
  base_commit, bond, case_ref, case_token, commitment, exit_tx, fee_input, juror,
  juror_input, juror_output, juror_ref, juror_token, jury, jury_output,
  leaving_juror, merge, new_case, other_policy, other_script, params, plus,
  spend_ok, stranger, top_up_tx,
}

fn base_top_up() -> Transaction {
  top_up_tx(1, juror(1), bond, 1_000_000)
}

/// The base top-up with the juror input `input`.
fn with_juror_input(input: Input) -> Transaction {
  Transaction { ..base_top_up(), inputs: [input, fee_input()] }
}

/// The juror input of juror 1 with the output `output`.
fn juror_input_with(output: Output) -> Input {
  Input { output_reference: juror_ref(1), output }
}

/// The output of the base top-up with `change` applied.
fn top_up_output() -> Output {
  juror_output(1, juror(1), bond + 1_000_000)
}

fn top_up_ok(tx: Transaction) -> Bool {
  spend_ok(tx, juror_ref(1))
}

test general_ok_top_up() {
  top_up_ok(base_top_up())
}

// ## The own input has an inline `JuryDatum`.

test general_fails_with_own_datum_hash() fail {
  let input = juror_input(1, juror(1), bond)
  top_up_ok(
    with_juror_input(
      juror_input_with(Output { ..input.output, datum: DatumHash(stranger) }),
    ),
  )
}

test general_fails_without_own_datum() fail {
  let input = juror_input(1, juror(1), bond)
  top_up_ok(
    with_juror_input(
      juror_input_with(Output { ..input.output, datum: NoDatum }),
    ),
  )
}

test general_fails_with_malformed_own_datum() fail {
  let datum: Data = [1, 2, 3]
  top_up_ok(
    Transaction {
      ..with_juror_input(
        juror_input_with(jury_output(juror_token(1), datum, bond)),
      ),
      outputs: [jury_output(juror_token(1), datum, bond + 1_000_000)],
    },
  )
}

test general_fails_when_own_input_is_missing() fail {
  spend_rules.validate(
    params(),
    OnJuror(TopUp),
    OutputReference { transaction_id: stranger, output_index: 0 },
    base_top_up(),
  )
}

// ## The own value holds exactly one jury token, quantity 1.

test general_fails_without_jury_token() fail {
  let input = juror_input(1, juror(1), bond)
  top_up_ok(
    with_juror_input(
      juror_input_with(
        Output { ..input.output, value: assets.from_lovelace(bond) },
      ),
    ),
  )
}

test general_fails_with_two_jury_tokens() fail {
  let input = juror_input(1, juror(1), bond)
  top_up_ok(
    with_juror_input(
      juror_input_with(
        plus(input.output, assets.from_asset(jury, juror_token(2), 1)),
      ),
    ),
  )
}

test general_fails_with_quantity_2() fail {
  let input = juror_input(1, juror(1), bond)
  top_up_ok(
    with_juror_input(
      juror_input_with(
        plus(input.output, assets.from_asset(jury, juror_token(1), 1)),
      ),
    ),
  )
}

/// Other tokens in the own input do not matter for this rule.
test general_ok_with_other_token_in_own_input() {
  let input = juror_input(1, juror(1), bond)
  top_up_ok(
    with_juror_input(
      juror_input_with(
        plus(input.output, assets.from_asset(other_policy, "x", 1)),
      ),
    ),
  )
}

// ## The datum and the redeemer match.

/// A `Juror` datum with an `OnCase` redeemer ("a juror datum used as a
/// case").
test general_fails_with_juror_datum_and_case_action() fail {
  let tx = base_commit()
  spend_ok(
    fixtures.with_redeemer(
      tx,
      juror_ref(1),
      OnCase(
        CaseCommit {
          juror: juror_token(1),
          commit: commitment(case_token(), 1, AuditorWins),
        },
      ),
    ),
    juror_ref(1),
  )
}

/// A `Case` datum with an `OnJuror` redeemer.
test general_fails_with_case_datum_and_juror_action() fail {
  let tx = base_commit()
  spend_ok(
    fixtures.with_redeemer(
      tx,
      case_ref(),
      OnJuror(Commit { case: case_token() }),
    ),
    case_ref(),
  )
}

/// A juror token in an output with a `Case` datum cannot use `OnJuror`.
test general_fails_with_juror_token_and_case_datum() fail {
  top_up_ok(
    Transaction {
      ..with_juror_input(
        juror_input_with(
          jury_output(juror_token(1), case_data(new_case()), bond),
        ),
      ),
      outputs: [
        jury_output(juror_token(1), case_data(new_case()), bond + 1_000_000),
      ],
    },
  )
}

// ## The continuing output.

fn with_output(output: Output) -> Transaction {
  Transaction { ..base_top_up(), outputs: [output] }
}

test general_fails_with_output_at_other_script() fail {
  top_up_ok(
    with_output(
      Output {
        ..top_up_output(),
        address: fixtures.script_address(other_script),
      },
    ),
  )
}

test general_fails_with_output_stake_credential() fail {
  top_up_ok(
    with_output(
      Output {
        ..top_up_output(),
        address: Address {
          payment_credential: Script(jury),
          stake_credential: Some(Inline(VerificationKey(stranger))),
        },
      },
    ),
  )
}

test general_fails_with_extra_token_in_output() fail {
  top_up_ok(
    with_output(plus(top_up_output(), assets.from_asset(other_policy, "x", 1))),
  )
}

test general_fails_with_output_datum_hash() fail {
  top_up_ok(
    with_output(Output { ..top_up_output(), datum: DatumHash(stranger) }),
  )
}

test general_fails_with_output_reference_script() fail {
  top_up_ok(
    with_output(
      Output { ..top_up_output(), reference_script: Some(other_script) },
    ),
  )
}

test general_fails_when_token_leaves() fail {
  top_up_ok(
    with_output(
      plus(
        fixtures.plain_output(fixtures.key_address(stranger), bond + 1_000_000),
        assets.from_asset(jury, juror_token(1), 1),
      ),
    ),
  )
}

/// The continuing output may come after other outputs.
test general_ok_with_continuing_output_last() {
  top_up_ok(
    Transaction {
      ..base_top_up(),
      outputs: [
        fixtures.plain_output(fixtures.key_address(stranger), 2_000_000),
        top_up_output(),
      ],
    },
  )
}

// ## §6.3 `Burn`: every quantity of policy J in the mint field is negative.

fn burn_ok(tx: Transaction) -> Bool {
  mint_rules.validate(params(), Burn, jury, tx)
}

test burn_ok_one_token() {
  burn_ok(exit_tx(1, leaving_juror(1), bond))
}

test burn_ok_two_tokens() {
  burn_ok(
    merge(
      exit_tx(1, leaving_juror(1), bond),
      exit_tx(2, leaving_juror(2), bond),
    ),
  )
}

test burn_fails_with_a_mint() fail {
  burn_ok(
    Transaction {
      ..exit_tx(1, leaving_juror(1), bond),
      mint: assets.from_asset(jury, juror_token(1), -1)
        |> assets.add(jury, juror_token(9), 1),
    },
  )
}

test burn_fails_with_only_a_mint() fail {
  burn_ok(
    Transaction {
      ..exit_tx(1, leaving_juror(1), bond),
      mint: assets.from_asset(jury, juror_token(9), 1),
    },
  )
}

/// Other policies in the mint field do not matter.
test burn_ok_with_other_policy_minted() {
  burn_ok(
    Transaction {
      ..exit_tx(1, leaving_juror(1), bond),
      mint: assets.from_asset(jury, juror_token(1), -1)
        |> assets.add(other_policy, "x", 5),
    },
  )
}

/// A `Register` redeemer cannot burn.
test register_fails_for_a_burn() fail {
  mint_rules.validate(
    params(),
    Register { seed: fixtures.juror_seed(1) },
    jury,
    exit_tx(1, leaving_juror(1), bond),
  )
}
