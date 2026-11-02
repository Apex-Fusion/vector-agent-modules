//// Tests of the Module-1 identity checks (Part 2 §7, Module-1 row) and of
//// the configuration record reader (Part 2 §3.8). The validator tests of
//// claim.ak, challenge.ak and jury_pool.ak run the same checks inside full
//// SubmitClaim, OpenChallenge and RegisterJuror transactions.

use adversarial_auditing/identity_checks.{
  Window, config_record_hash, declared_credential_ok, identity_ok, window,
}
use adversarial_auditing_tests/identity_fixtures.{
  accountable_key_party, agent_a, agent_b, auth_key_a, config_holder,
  config_input, config_policy, controller_a, controller_b, hi, identity_hash,
  key_address, key_credential, lo, module1_hash, out_ref, p256_key_a, record_a,
  record_b, record_input, script_address, stranger_key, tx_key_a, window_range,
}
use aiken/crypto.{blake2b_224}
use aiken/interval
use cardano/address.{Address, Credential, Inline, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  DatumHash, InlineDatum, Input, NoDatum, Output, Transaction, placeholder,
}
use vector_identity.{day_ms, max_range_ms}
use vector_identity/types.{
  Active, ControllerSet, Hold, IdentityDatum, Party, Retired, Revoked, Subagent,
  Suspended,
} as vi

/// Script hash and policy ID of the legacy registry.
const legacy_hash: ByteArray =
  #"be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"

fn check_tx(refs: List<Input>) -> Transaction {
  Transaction {
    ..placeholder,
    reference_inputs: refs,
    validity_range: window_range(),
  }
}

fn record_of_a(record: IdentityDatum) -> Input {
  record_input(out_ref(0xa0, 0), agent_a, record)
}

fn check_a(credential: Credential, refs: List<Input>) -> Bool {
  identity_ok(identity_hash, agent_a, credential, check_tx(refs))
}

fn hold_until(until: Int) -> Hold {
  Hold { by: ControllerSet, reason: 6, since: lo - day_ms, until }
}

// ## Declared credential

test identity_ok_with_controller_credential() {
  check_a(key_credential(controller_a), [record_of_a(record_a(Active))])
}

test identity_ok_with_listed_transaction_key() {
  check_a(key_credential(tx_key_a), [record_of_a(record_a(Active))])
}

test identity_fails_with_key_without_transaction_purpose() {
  !check_a(key_credential(auth_key_a), [record_of_a(record_a(Active))])
}

test identity_fails_with_p256_key() {
  !check_a(key_credential(p256_key_a), [record_of_a(record_a(Active))])
}

test identity_fails_with_unlisted_key() {
  !check_a(key_credential(stranger_key), [record_of_a(record_a(Active))])
}

test identity_fails_with_script_credential() {
  !check_a(Script(blake2b_224(controller_a)), [record_of_a(record_a(Active))])
}

test identity_fails_with_controller_of_other_agent() {
  !check_a(key_credential(controller_b), [record_of_a(record_a(Active))])
}

test declared_credential_rules() {
  let record = record_a(Active)
  and {
    declared_credential_ok(record, key_credential(controller_a)),
    declared_credential_ok(record, key_credential(tx_key_a)),
    !declared_credential_ok(record, key_credential(auth_key_a)),
    !declared_credential_ok(record, VerificationKey(controller_a)),
    !declared_credential_ok(record, Script(blake2b_224(tx_key_a))),
  }
}

// ## Effective status

test identity_fails_when_suspended() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      status: Suspended { holds: [hold_until(lo + 1)] },
    }
  !check_a(key_credential(tx_key_a), [record_of_a(record)])
}

test identity_ok_after_hold_ends() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      status: Suspended { holds: [hold_until(lo)] },
    }
  check_a(key_credential(tx_key_a), [record_of_a(record)])
}

test identity_fails_when_revoked() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      status: Revoked {
        by: ControllerSet,
        reason: 5,
        at: lo - day_ms,
        invalid_from: None,
        successor: None,
      },
    }
  !check_a(key_credential(controller_a), [record_of_a(record)])
}

test identity_fails_when_retired() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      status: Retired { at: lo - day_ms, successor: None },
    }
  !check_a(key_credential(controller_a), [record_of_a(record)])
}

test identity_fails_when_unaccountable() {
  let record = IdentityDatum { ..record_a(Active), parties: [] }
  !check_a(key_credential(tx_key_a), [record_of_a(record)])
}

test identity_fails_when_accountable_party_ends_before_hi() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      parties: [Party { ..accountable_key_party(), not_after: Some(hi - 1) }],
    }
  !check_a(key_credential(tx_key_a), [record_of_a(record)])
}

test identity_fails_when_accountable_party_starts_after_lo() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      parties: [Party { ..accountable_key_party(), not_before: lo + 1 }],
    }
  !check_a(key_credential(tx_key_a), [record_of_a(record)])
}

// ## Subagents

fn subagent_of_b(parent_epoch: Int) -> IdentityDatum {
  IdentityDatum {
    ..record_a(Active),
    endpoint: "",
    lineage: Subagent { parent: agent_b, parent_epoch, depth: 1 },
  }
}

test identity_subagent_with_active_parent() {
  check_a(
    key_credential(tx_key_a),
    [
      record_of_a(subagent_of_b(0)),
      record_input(out_ref(0xb1, 0), agent_b, record_b(Active)),
    ],
  )
}

test identity_subagent_with_suspended_parent_fails() {
  let parent =
    IdentityDatum {
      ..record_b(Active),
      status: Suspended { holds: [hold_until(lo + 1)] },
    }
  !check_a(
    key_credential(tx_key_a),
    [
      record_of_a(subagent_of_b(0)),
      record_input(out_ref(0xb1, 0), agent_b, parent),
    ],
  )
}

test identity_subagent_after_epoch_increase_fails() {
  let parent = IdentityDatum { ..record_b(Active), subagent_epoch: 1 }
  !check_a(
    key_credential(tx_key_a),
    [
      record_of_a(subagent_of_b(0)),
      record_input(out_ref(0xb1, 0), agent_b, parent),
    ],
  )
}

test identity_subagent_without_parent_fails() fail {
  check_a(key_credential(tx_key_a), [record_of_a(subagent_of_b(0))])
}

// ## The record: only an authentic identity record counts

test identity_fails_without_record() {
  !check_a(key_credential(tx_key_a), [])
}

test identity_fails_with_legacy_registry_token() {
  // The old check accepted any reference input with a registry token named
  // after the agent. It no longer counts.
  let legacy =
    Input {
      output_reference: out_ref(0x1e, 0),
      output: Output {
        address: script_address(legacy_hash),
        value: assets.from_lovelace(2_000_000)
          |> assets.add(legacy_hash, agent_a, 1),
        datum: NoDatum,
        reference_script: None,
      },
    }
  !check_a(key_credential(tx_key_a), [legacy])
}

test identity_fails_with_record_of_other_agent() {
  !check_a(
    key_credential(tx_key_a),
    [record_input(out_ref(0xa0, 0), agent_b, record_a(Active))],
  )
}

test identity_fails_with_record_at_staked_address() {
  let input = record_of_a(record_a(Active))
  let staked =
    Input {
      ..input,
      output: Output {
        ..input.output,
        address: Address {
          payment_credential: Script(identity_hash),
          stake_credential: Some(
            Inline(VerificationKey(blake2b_224(controller_a))),
          ),
        },
      },
    }
  !check_a(key_credential(tx_key_a), [staked])
}

test identity_fails_with_record_at_key_address() {
  let input = record_of_a(record_a(Active))
  let moved =
    Input {
      ..input,
      output: Output {
        ..input.output,
        address: key_address(blake2b_224(controller_a)),
      },
    }
  !check_a(key_credential(tx_key_a), [moved])
}

test identity_fails_with_token_of_other_policy() {
  let input = record_of_a(record_a(Active))
  let fake =
    Input {
      ..input,
      output: Output {
        ..input.output,
        value: assets.from_lovelace(10_000_000)
          |> assets.add(module1_hash, agent_a, 1),
      },
    }
  !check_a(key_credential(tx_key_a), [fake])
}

test identity_fails_with_datum_hash() {
  let input = record_of_a(record_a(Active))
  let hashed =
    Input {
      ..input,
      output: Output { ..input.output, datum: DatumHash(agent_a) },
    }
  !check_a(key_credential(tx_key_a), [hashed])
}

// ## Time

test identity_with_range_of_one_hour() {
  identity_ok(
    identity_hash,
    agent_a,
    key_credential(tx_key_a),
    Transaction {
      ..check_tx([record_of_a(record_a(Active))]),
      validity_range: interval.between(lo, lo + max_range_ms),
    },
  )
}

test identity_with_range_over_one_hour_fails() fail {
  identity_ok(
    identity_hash,
    agent_a,
    key_credential(tx_key_a),
    Transaction {
      ..check_tx([record_of_a(record_a(Active))]),
      validity_range: interval.between(lo, lo + max_range_ms + 1),
    },
  )
}

test identity_with_unbounded_range_fails() fail {
  identity_ok(
    identity_hash,
    agent_a,
    key_credential(tx_key_a),
    Transaction {
      ..check_tx([record_of_a(record_a(Active))]),
      validity_range: interval.after(lo),
    },
  )
}

test identity_range_rule_applies_without_record() fail {
  // The window rule runs first, also when the result would be False: without
  // the window failure this test would return True.
  !identity_ok(
    identity_hash,
    agent_a,
    key_credential(tx_key_a),
    Transaction { ..check_tx([]), validity_range: interval.before(hi) },
  )
}

test window_values() {
  window(window_range()) == Window { lo, hi }
}

test window_fails_when_hi_is_before_lo() fail {
  window(interval.between(hi, lo)).lo == hi
}

// ## Configuration record

test config_record_hash_reads_module1() {
  config_record_hash([config_input(module1_hash)], config_policy, config_holder) == module1_hash
}

test config_record_hash_skips_other_inputs() {
  config_record_hash(
    [record_of_a(record_a(Active)), config_input(module1_hash)],
    config_policy,
    config_holder,
  ) == module1_hash
}

test config_record_hash_fails_without_record() fail {
  config_record_hash([], config_policy, config_holder) == module1_hash
}

test config_record_hash_fails_with_other_policy() fail {
  config_record_hash([config_input(module1_hash)], module1_hash, config_holder) == module1_hash
}

test config_record_hash_fails_with_datum_hash() fail {
  let input = config_input(module1_hash)
  config_record_hash(
    [
      Input {
        ..input,
        output: Output { ..input.output, datum: DatumHash(module1_hash) },
      },
    ],
    config_policy,
    config_holder,
  ) == module1_hash
}

test config_record_hash_fails_with_long_datum() fail {
  config_record_hash(
    [
      config_input(
        #"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1",
      ),
    ],
    config_policy,
    config_holder,
  ) == module1_hash
}

test config_record_hash_fails_with_integer_datum() fail {
  let input = config_input(module1_hash)
  config_record_hash(
    [
      Input {
        ..input,
        output: Output { ..input.output, datum: InlineDatum(28) },
      },
    ],
    config_policy,
    config_holder,
  ) == module1_hash
}
