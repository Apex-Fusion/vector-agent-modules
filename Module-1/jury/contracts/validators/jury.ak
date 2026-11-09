//// The Module-1 jury validator (`Module-1/jury/SPEC.md`).
////
//// One multi-validator: `J` is its script hash, the policy id of the juror
//// and case tokens, and the payment credential of the jury address (§4).
//// The rules are in `lib/jury`: `mint_rules` (§6.1 to §6.3) and
//// `spend_rules` with `juror_rules` (§6.4 to §6.8) and `case_rules` (§6.9 to
//// §6.13). The tests are in `lib/jury_tests`.
////
//// The spend handler reads the own datum from the own input (it must be an
//// inline `JuryDatum`, §6), so the datum argument is not decoded here.

use cardano/address.{VerificationKey}
use cardano/assets.{PolicyId}
use cardano/script_context.{ScriptContext, Withdrawing}
use cardano/transaction.{OutputReference, Transaction, placeholder}
use jury/mint_rules
use jury/redeemers.{
  CaseCommit, Commit, MintAction, OnCase, OnJuror, Register, SpendAction, TopUp,
}
use jury/spend_rules
use jury/types.{AuditorWins, JuryParams}
use jury_tests/fixtures

validator jury(params: JuryParams) {
  mint(redeemer: MintAction, policy_id: PolicyId, tx: Transaction) {
    mint_rules.validate(params, redeemer, policy_id, tx)
  }

  spend(
    _datum: Option<Data>,
    redeemer: SpendAction,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    spend_rules.validate(params, redeemer, own_ref, tx)
  }

  else(_) {
    fail
  }
}

// ## Wiring tests: the handlers of the validator run the rules.

test validator_register() {
  jury.mint(
    fixtures.params(),
    Register { seed: fixtures.juror_seed(1) },
    fixtures.jury,
    fixtures.base_register(),
  )
}

test validator_register_fails_with_other_policy() fail {
  jury.mint(
    fixtures.params(),
    Register { seed: fixtures.juror_seed(1) },
    fixtures.other_policy,
    fixtures.base_register(),
  )
}

test validator_commit() {
  let tx = fixtures.base_commit()
  and {
    jury.spend(
      fixtures.params(),
      None,
      OnJuror(Commit { case: fixtures.case_token() }),
      fixtures.juror_ref(1),
      tx,
    ),
    jury.spend(
      fixtures.params(),
      None,
      OnCase(
        CaseCommit {
          juror: fixtures.juror_token(1),
          commit: fixtures.commitment(fixtures.case_token(), 1, AuditorWins),
        },
      ),
      fixtures.case_ref(),
      tx,
    ),
  }
}

/// The datum argument is not used: the own datum comes from the own input.
test validator_ignores_datum_argument() {
  let datum: Data = 42
  jury.spend(
    fixtures.params(),
    Some(datum),
    OnJuror(TopUp),
    fixtures.juror_ref(1),
    fixtures.top_up_tx(1, fixtures.juror(1), fixtures.bond, 1),
  )
}

test validator_top_up_fails_with_other_params() fail {
  jury.spend(
    JuryParams { ..fixtures.params(), bond_min: 1 },
    None,
    OnJuror(TopUp),
    fixtures.juror_ref(1),
    Transaction {
      ..fixtures.top_up_tx(1, fixtures.juror(1), fixtures.bond, 1),
      extra_signatories: [],
    },
  )
}

test validator_rejects_other_purposes() fail {
  jury.else(
    fixtures.params(),
    ScriptContext {
      transaction: placeholder,
      redeemer: Void,
      info: Withdrawing(VerificationKey(fixtures.wallet_key_hash)),
    },
  )
}
