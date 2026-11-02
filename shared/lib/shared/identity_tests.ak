//// Tests of `shared/identity` with the fixtures of `shared/identity_fixtures`.

use aiken/interval
use cardano/address.{Script, VerificationKey}
use cardano/transaction.{Input, Transaction}
use shared/identity.{
  Window, active_record, active_with_capabilities, controller_exits,
  controller_opens, credential_opens, declared_credential, lists_capabilities,
  window,
}
use shared/identity_fixtures.{
  agent_id, controller_hash, ed_key, ed_key_hash, identity_hash, p256_key,
  p256_key_hash, record, record_input, record_input_at_other_script,
  record_input_with_fake_token, record_input_with_stake, retired, revoked,
  subagent, suspended, unaccountable, with_key, with_subagent_epoch,
  with_transaction_key,
}
use vector_identity.{purpose_assertion, purpose_authentication}
use vector_identity/types.{Ed25519, P256}

const lo: Int = 1_000_000_000

const hi: Int = 1_000_600_000

const w: Window = Window { lo, hi }

fn tx(refs: List<Input>, signers: List<ByteArray>) -> Transaction {
  Transaction {
    ..transaction.placeholder,
    reference_inputs: refs,
    extra_signatories: signers,
    validity_range: interval.between(lo, hi),
  }
}

// ## window

test window_accepts_one_hour() {
  window(interval.between(lo, lo + 3_600_000)) == Some(
    Window { lo, hi: lo + 3_600_000 },
  )
}

test window_rejects_more_than_one_hour() {
  window(interval.between(lo, lo + 3_600_001)) == None
}

test window_rejects_open_ranges() {
  and {
    window(interval.after(lo)) == None,
    window(interval.before(hi)) == None,
    window(interval.everything) == None,
  }
}

test window_rejects_hi_before_lo() {
  window(interval.between(hi, lo)) == None
}

// ## active_record

test active_record_active() {
  let r = record(1, ["cap"])
  active_record([record_input(agent_id(1), r)], identity_hash, agent_id(1), w) == Some(
    r,
  )
}

test active_record_not_active() {
  let r = record(1, ["cap"])
  let none_with =
    fn(input: Input) {
      active_record([input], identity_hash, agent_id(1), w) == None
    }
  and {
    none_with(record_input(agent_id(1), suspended(r, hi))),
    none_with(record_input(agent_id(1), unaccountable(r))),
    none_with(record_input(agent_id(1), revoked(r))),
    none_with(record_input(agent_id(1), retired(r))),
    none_with(record_input_with_stake(agent_id(1), r)),
    none_with(record_input_at_other_script(agent_id(1), r)),
    none_with(record_input_with_fake_token(agent_id(1), r)),
    none_with(record_input(agent_id(2), r)),
  }
}

test active_record_hold_ending_at_lo_not_in_force() {
  let r = suspended(record(1, ["cap"]), lo)
  active_record([record_input(agent_id(1), r)], identity_hash, agent_id(1), w) == Some(
    r,
  )
}

test active_record_subagent() {
  let parent = record(1, [])
  let child = subagent(record(2, ["cap"]), agent_id(1), 0, 1)
  let refs =
    fn(p) { [record_input(agent_id(2), child), record_input(agent_id(1), p)] }
  and {
    active_record(refs(parent), identity_hash, agent_id(2), w) == Some(child),
    active_record(refs(suspended(parent, hi)), identity_hash, agent_id(2), w) == None,
    active_record(refs(revoked(parent)), identity_hash, agent_id(2), w) == None,
    active_record(
      refs(with_subagent_epoch(parent, 1)),
      identity_hash,
      agent_id(2),
      w,
    ) == None,
  }
}

test active_record_subagent_without_parent_fails() fail {
  let child = subagent(record(2, ["cap"]), agent_id(1), 0, 1)
  active_record(
    [record_input(agent_id(2), child)],
    identity_hash,
    agent_id(2),
    w,
  ) != None
}

// ## capabilities

test lists_capabilities_cases() {
  let r = record(1, ["a", "b"])
  and {
    lists_capabilities(r, []),
    lists_capabilities(r, ["a"]),
    lists_capabilities(r, ["b", "a"]),
    !lists_capabilities(r, ["a", "c"]),
    !lists_capabilities(r, ["a.v1"]),
  }
}

test active_with_capabilities_cases() {
  let refs = [record_input(agent_id(1), record(1, ["a"]))]
  and {
    active_with_capabilities(refs, identity_hash, agent_id(1), w, ["a"]),
    !active_with_capabilities(refs, identity_hash, agent_id(1), w, ["b"]),
    !active_with_capabilities(
      [record_input(agent_id(1), suspended(record(1, ["a"]), hi))],
      identity_hash,
      agent_id(1),
      w,
      ["a"],
    ),
  }
}

// ## controller_opens and controller_exits

test controller_opens_cases() {
  let refs = [record_input(agent_id(1), record(1, ["a"]))]
  and {
    controller_opens(
      tx(refs, [controller_hash(1)]),
      identity_hash,
      agent_id(1),
      w,
      ["a"],
    ),
    !controller_opens(
      tx(refs, [controller_hash(2)]),
      identity_hash,
      agent_id(1),
      w,
      ["a"],
    ),
    !controller_opens(
      tx(refs, [controller_hash(1)]),
      identity_hash,
      agent_id(1),
      w,
      ["b"],
    ),
    !controller_opens(
      tx(
        [record_input(agent_id(1), suspended(record(1, ["a"]), hi))],
        [controller_hash(1)],
      ),
      identity_hash,
      agent_id(1),
      w,
      ["a"],
    ),
    !controller_opens(
      tx([], [controller_hash(1)]),
      identity_hash,
      agent_id(1),
      w,
      ["a"],
    ),
  }
}

test controller_exits_any_status() {
  let r = record(1, ["a"])
  let exits =
    fn(input: Input, signer: ByteArray) {
      controller_exits(tx([input], [signer]), identity_hash, agent_id(1))
    }
  and {
    exits(record_input(agent_id(1), r), controller_hash(1)),
    exits(record_input(agent_id(1), suspended(r, hi)), controller_hash(1)),
    exits(record_input(agent_id(1), unaccountable(r)), controller_hash(1)),
    exits(record_input(agent_id(1), revoked(r)), controller_hash(1)),
    exits(record_input(agent_id(1), retired(r)), controller_hash(1)),
    !exits(record_input(agent_id(1), r), controller_hash(2)),
    !exits(record_input_with_stake(agent_id(1), r), controller_hash(1)),
    !exits(record_input_with_fake_token(agent_id(1), r), controller_hash(1)),
    !controller_exits(tx([], [controller_hash(1)]), identity_hash, agent_id(1)),
  }
}

// ## declared credentials

test declared_credential_cases() {
  let r =
    record(1, [])
      |> with_transaction_key(1)
      |> with_key(
          Ed25519,
          ed_key(2),
          purpose_authentication + purpose_assertion,
        )
      |> with_key(P256, p256_key(3), purpose_authentication)
  and {
    declared_credential(r, VerificationKey(controller_hash(1))),
    declared_credential(r, VerificationKey(ed_key_hash(1))),
    !declared_credential(r, VerificationKey(ed_key_hash(2))),
    !declared_credential(r, VerificationKey(p256_key_hash(3))),
    !declared_credential(r, VerificationKey(ed_key_hash(4))),
    !declared_credential(r, VerificationKey(controller_hash(2))),
    !declared_credential(r, Script(controller_hash(1))),
  }
}

test credential_opens_cases() {
  let r = record(1, []) |> with_transaction_key(1)
  let refs = [record_input(agent_id(1), r)]
  let opens =
    fn(refs: List<Input>, credential, signers) {
      credential_opens(
        tx(refs, signers),
        identity_hash,
        agent_id(1),
        credential,
        w,
      )
    }
  and {
    opens(refs, VerificationKey(controller_hash(1)), [controller_hash(1)]),
    opens(refs, VerificationKey(ed_key_hash(1)), [ed_key_hash(1)]),
    !opens(refs, VerificationKey(ed_key_hash(1)), [controller_hash(1)]),
    !opens(refs, VerificationKey(ed_key_hash(2)), [ed_key_hash(2)]),
    !opens(refs, Script(controller_hash(1)), [controller_hash(1)]),
    !opens(
      [record_input(agent_id(1), suspended(r, hi))],
      VerificationKey(controller_hash(1)),
      [controller_hash(1)],
    ),
    !opens(
      [record_input(agent_id(1), revoked(r))],
      VerificationKey(controller_hash(1)),
      [controller_hash(1)],
    ),
  }
}
