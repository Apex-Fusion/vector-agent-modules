//// SPEC §6.1 `Register { seed }`: one passing and one failing transaction
//// per rule. The base transaction registers juror 1 with the minimum bond.

use aiken/crypto.{blake2b_224}
use aiken/interval
use cardano/address.{Address, Inline, Script}
use cardano/assets
use cardano/transaction.{DatumHash, Output, Transaction}
use jury/datum.{JurorFields}
use jury/redeemers.{Register}
use jury/tokens.{case_prefix, token_name}
use jury/types.{OpenVote}
use jury_tests/fixtures.{
  base_register, case_output, juror, juror_record, juror_seed, juror_token, jury,
  new_case, new_juror, other_policy, other_script, out_ref, owner, params,
  record_input, register_range, register_tx, registered_at, script_address,
  stranger, wallet_input,
}
use vector_identity/types.{
  ControllerSet, Ed25519, Hold, IdentityDatum, Key, Revoked, Subagent, Suspended,
} as vi

fn register_ok(tx: Transaction) -> Bool {
  fixtures.mint_ok(tx, Register { seed: juror_seed(1) })
}

/// The base registration with the output `output`.
fn with_output(output: Output) -> Transaction {
  Transaction { ..base_register(), outputs: [output] }
}

/// The juror output of the base registration.
fn base_output() -> Output {
  fixtures.juror_output(1, new_juror(1), params().bond_min)
}

/// Juror 1 with the datum `j` and the minimum bond.
fn register_with(j: JurorFields) -> Transaction {
  register_tx(1, j, params().bond_min)
}

/// The base registration with the record `record` of juror 1.
fn with_record(record: IdentityDatum) -> Transaction {
  Transaction {
    ..base_register(),
    reference_inputs: [record_input(0x91, fixtures.juror_agent(1), record)],
  }
}

test register_ok_base() {
  register_ok(base_register())
}

// ## Rule 1: the transaction spends `seed`.

test register_r1_fails_without_seed() fail {
  register_ok(
    Transaction { ..base_register(), inputs: [wallet_input(out_ref(0x77, 0))] },
  )
}

// ## Rule 2: exactly one jury token, named `jrr_` + hash of `seed`.

/// The second unit goes to a wallet output, so that only the mint field
/// breaks the rule.
test register_r2_fails_with_quantity_2() fail {
  register_ok(
    Transaction {
      ..base_register(),
      outputs: [base_output(), wallet_with(juror_token(1))],
      mint: assets.from_asset(jury, juror_token(1), 2),
    },
  )
}

test register_r2_fails_with_a_second_token() fail {
  register_ok(
    Transaction {
      ..base_register(),
      outputs: [base_output(), wallet_with(juror_token(2))],
      mint: assets.from_asset(jury, juror_token(1), 1)
        |> assets.add(jury, juror_token(2), 1),
    },
  )
}

/// The mint field has the `cas_` name of the seed instead of the `jrr_`
/// name.
test register_r2_fails_with_case_prefix() fail {
  register_ok(
    Transaction {
      ..base_register(),
      mint: assets.from_asset(jury, token_name(case_prefix, juror_seed(1)), 1),
    },
  )
}

/// A wallet output with one jury token `name`.
fn wallet_with(name: ByteArray) -> Output {
  fixtures.plus(
    fixtures.plain_output(
      fixtures.key_address(fixtures.wallet_key_hash),
      2_000_000,
    ),
    assets.from_asset(jury, name, 1),
  )
}

// ## Rule 3: one output at the jury address with lovelace and the token
// only, an inline datum `Juror` and no reference script.

test register_r3_fails_at_other_script() fail {
  register_ok(
    with_output(
      Output { ..base_output(), address: script_address(other_script) },
    ),
  )
}

test register_r3_fails_with_stake_credential() fail {
  register_ok(
    with_output(
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

test register_r3_fails_with_extra_token() fail {
  register_ok(
    with_output(
      fixtures.plus(base_output(), assets.from_asset(other_policy, "x", 1)),
    ),
  )
}

test register_r3_fails_with_datum_hash() fail {
  register_ok(
    with_output(Output { ..base_output(), datum: DatumHash(stranger) }),
  )
}

test register_r3_fails_with_reference_script() fail {
  register_ok(
    with_output(
      Output { ..base_output(), reference_script: Some(other_script) },
    ),
  )
}

test register_r3_fails_with_case_datum() fail {
  register_ok(
    with_output(
      Output {
        ..case_output(new_case(), params().bond_min),
        value: base_output().value,
      },
    ),
  )
}

test register_r3_fails_with_malformed_datum() fail {
  let datum: Data = (fixtures.juror_agent(1), owner(1))
  register_ok(
    with_output(fixtures.jury_output(juror_token(1), datum, params().bond_min)),
  )
}

// ## Rule 4: the datum fields.

test register_r4_fails_with_short_agent() fail {
  register_ok(register_with(JurorFields { ..new_juror(1), agent: #"d1d1" }))
}

test register_r4_fails_with_long_owner() fail {
  register_ok(
    Transaction {
      ..register_with(
        JurorFields { ..new_juror(1), owner: fixtures.controller(1) },
      ),
      extra_signatories: [fixtures.controller(1)],
    },
  )
}

test register_r4_fails_with_short_prompt_hash() fail {
  register_ok(register_with(JurorFields { ..new_juror(1), prompt_hash: #"9b" }))
}

test register_r4_fails_when_registered_at_is_not_hi() fail {
  register_ok(
    register_with(
      JurorFields { ..new_juror(1), registered_at: registered_at - 1 },
    ),
  )
}

test register_r4_fails_with_exit_request() fail {
  register_ok(
    register_with(
      JurorFields { ..new_juror(1), exit_after: Some(registered_at) },
    ),
  )
}

test register_r4_fails_with_open_entry() fail {
  register_ok(
    register_with(
      JurorFields {
        ..new_juror(1),
        open: [
          OpenVote { case: fixtures.case_token(), reveal_end: registered_at },
        ],
      },
    ),
  )
}

// ## Rule 5: at least `bond_min`.

test register_r5_ok_above_bond_min() {
  register_ok(register_tx(1, new_juror(1), params().bond_min + 1))
}

test register_r5_fails_below_bond_min() fail {
  register_ok(register_tx(1, new_juror(1), params().bond_min - 1))
}

// ## Rule 6: the identity check (§5.8).

/// Rule 6 with a listed key with the transaction purpose as owner.
test register_r6_ok_with_listed_transaction_key() {
  let key = #"e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"
  let record =
    IdentityDatum {
      ..juror_record(1),
      keys: [Key { key_type: Ed25519, key, purposes: 9, pop: None }],
    }
  register_ok(
    Transaction {
      ..register_with(JurorFields { ..new_juror(1), owner: blake2b_224(key) }),
      reference_inputs: [record_input(0x91, fixtures.juror_agent(1), record)],
      extra_signatories: [blake2b_224(key)],
    },
  )
}

test register_r6_fails_with_key_without_transaction_purpose() fail {
  let key = #"e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6"
  let record =
    IdentityDatum {
      ..juror_record(1),
      keys: [Key { key_type: Ed25519, key, purposes: 3, pop: None }],
    }
  register_ok(
    Transaction {
      ..register_with(JurorFields { ..new_juror(1), owner: blake2b_224(key) }),
      reference_inputs: [record_input(0x91, fixtures.juror_agent(1), record)],
      extra_signatories: [blake2b_224(key)],
    },
  )
}

test register_r6_fails_without_record() fail {
  register_ok(Transaction { ..base_register(), reference_inputs: [] })
}

test register_r6_fails_with_record_of_other_agent() fail {
  register_ok(
    Transaction {
      ..base_register(),
      reference_inputs: [record_input(0x91, stranger, juror_record(1))],
    },
  )
}

test register_r6_fails_for_subagent() fail {
  register_ok(
    with_record(
      IdentityDatum {
        ..juror_record(1),
        lineage: Subagent { parent: stranger, parent_epoch: 0, depth: 1 },
      },
    ),
  )
}

test register_r6_fails_when_owner_is_not_controller() fail {
  register_ok(
    Transaction {
      ..register_with(JurorFields { ..new_juror(1), owner: owner(2) }),
      extra_signatories: [owner(2)],
    },
  )
}

test register_r6_fails_when_suspended() fail {
  register_ok(
    with_record(
      IdentityDatum {
        ..juror_record(1),
        status: Suspended {
          holds: [
            Hold {
              by: ControllerSet,
              reason: 6,
              since: registered_at - fixtures.day,
              until: registered_at + fixtures.day,
            },
          ],
        },
      },
    ),
  )
}

test register_r6_fails_when_revoked() fail {
  register_ok(
    with_record(
      IdentityDatum {
        ..juror_record(1),
        status: Revoked {
          by: ControllerSet,
          reason: 5,
          at: registered_at - fixtures.day,
          invalid_from: None,
          successor: None,
        },
      },
    ),
  )
}

test register_r6_fails_without_accountable_party() fail {
  register_ok(with_record(IdentityDatum { ..juror_record(1), parties: [] }))
}

test register_r6_fails_without_owner_signature() fail {
  register_ok(Transaction { ..base_register(), extra_signatories: [] })
}

test register_r6_fails_with_range_over_one_hour() fail {
  register_ok(
    Transaction {
      ..base_register(),
      validity_range: interval.between(registered_at - 3_600_001, registered_at),
    },
  )
}

test register_r6_fails_with_unbounded_range() fail {
  register_ok(
    Transaction {
      ..base_register(),
      validity_range: interval.before(registered_at),
    },
  )
}

test register_base_window_is_one_hour_or_less() {
  register_range() == interval.between(registered_at - 600_000, registered_at)
}

test register_base_datum_is_juror_1() {
  new_juror(1) == juror(1)
}
