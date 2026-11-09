//// Library `vector_identity`: Part 2 §3.10 (`docs/IDENTITY_REGISTRY.md`).
////
//// Every contract that needs identity uses only these functions. The record
//// format is Part 1 §5 (`docs/did-vector-method.md`), and the effective status
//// follows Part 1 §7.3 for on-chain use.
////
//// The module contracts vendor this file and `lib/vector_identity/` unchanged.
//// Keep them compatible with aiken-lang/stdlib v3.0.0 and v3.1.0.

use aiken/collection/dict
use aiken/collection/list
use aiken/crypto.{blake2b_224}
use aiken/primitive/bytearray
use cardano/address.{Address, Script, VerificationKey}
use cardano/assets.{Value}
use cardano/transaction.{
  DatumHash, InlineDatum, Input, NoDatum, Output, OutputReference, Transaction,
}
use vector_identity/types.{
  Accountable, Active, ControllerSet, Ed25519, EffActive, EffRetired, EffRevoked,
  EffSuspended, Effective, Hold, IdentityDatum, Key, KeyHash, P256, Party,
  Responsible, Retired, Revoked, Root, Status, Subagent, Suspended,
}

// ## Constants

/// Key purpose: authentication.
pub const purpose_authentication: Int = 1

/// Key purpose: assertion.
pub const purpose_assertion: Int = 2

/// Key purpose: transaction. Ed25519 keys only.
pub const purpose_transaction: Int = 8

/// Key purpose: delegation. Ed25519 keys only.
pub const purpose_delegation: Int = 16

/// Party right: suspend.
pub const right_suspend: Int = 1

/// Party right: revoke.
pub const right_revoke: Int = 2

/// Party right: recover.
pub const right_recover: Int = 4

/// The longest validity range (`hi - lo`) of a transaction that reads or
/// changes identity data: one hour.
pub const max_range_ms: Int = 3_600_000

/// The latest time value: 9999-12-31T23:59:59.999Z in POSIX milliseconds.
pub const max_time_ms: Int = 253_402_300_799_999

/// One day in milliseconds.
pub const day_ms: Int = 86_400_000

/// 7 days in milliseconds.
pub const days_7_ms: Int = 604_800_000

/// 30 days in milliseconds.
pub const days_30_ms: Int = 2_592_000_000

/// 90 days in milliseconds.
pub const days_90_ms: Int = 7_776_000_000

/// 366 days in milliseconds.
pub const days_366_ms: Int = 31_622_400_000

/// The greatest subagent depth, and the most ancestors that `effective` reads.
pub const max_depth: Int = 4

// ## Functions

/// The record of `id`: a reference input at the exact identity address (payment
/// credential `Script(identity_hash)`, no stake credential) whose value holds
/// exactly one token of the policy `identity_hash` in total, named `id` with
/// quantity 1, and whose datum is inline. The datum must decode as
/// `IdentityDatum`; the function fails when it does not. Live records and
/// tombstones count. Else `None`. Pass the transaction's `reference_inputs`
/// as `refs`.
pub fn find_record(
  refs: List<Input>,
  identity_hash: ByteArray,
  id: ByteArray,
) -> Option<IdentityDatum> {
  when refs is {
    [] -> None
    [input, ..rest] -> {
      let output = input.output
      if is_record_output(output, identity_hash, id) {
        when output.datum is {
          InlineDatum(data) -> {
            expect record: IdentityDatum = data
            Some(record)
          }
          _ -> find_record(rest, identity_hash, id)
        }
      } else {
        find_record(rest, identity_hash, id)
      }
    }
  }
}

/// The effective status of `record` over the window `[lo, hi]`. The first rule
/// that applies gives the result:
///
/// 1. The record is a tombstone: `EffRevoked` or `EffRetired`.
/// 2. The record is a subagent. Its parent must be a reference input
///    (`find_record`); the function fails when it is missing. When the
///    parent's `subagent_epoch` is greater than `parent_epoch`: `EffRevoked`.
///    Else the parent's effective status over the same window, when it is not
///    `EffActive`. Each ancestor is checked in turn, at most `max_depth` of them.
/// 3. A hold is in force (`until > lo`): `EffSuspended`.
/// 4. No Accountable Party covers the window (`not_before <= lo`, and
///    `not_after` is empty or `>= hi`): `EffSuspended`.
/// 5. Else `EffActive`.
pub fn effective(
  record: IdentityDatum,
  refs: List<Input>,
  identity_hash: ByteArray,
  lo: Int,
  hi: Int,
) -> Effective {
  effective_within(record, refs, identity_hash, lo, hi, max_depth)
}

/// True when the record lists a key with `blake2b_224(key) == key_hash` whose
/// `purposes` contain `purpose`. `purpose` must be one of the purpose values;
/// the transaction and delegation purposes count only for Ed25519 keys. The
/// controller key counts only when it is also a listed key.
pub fn active_key(
  record: IdentityDatum,
  key_hash: ByteArray,
  purpose: Int,
) -> Bool {
  list.any(
    record.keys,
    fn(key) {
      key_has_purpose(key, purpose) && blake2b_224(key.key) == key_hash
    },
  )
}

/// True when the record has no parent (lineage `Root`).
pub fn independent(record: IdentityDatum) -> Bool {
  when record.lineage is {
    Root -> True
    Subagent { .. } -> False
  }
}

/// True when `capability` is one of the record's capabilities (exact match).
pub fn has_capability(record: IdentityDatum, capability: ByteArray) -> Bool {
  list.has(record.capabilities, capability)
}

/// True when `blake2b_224(record.controller)` is in the transaction's
/// `extra_signatories`.
pub fn controller_signed(record: IdentityDatum, tx: Transaction) -> Bool {
  list.has(tx.extra_signatories, blake2b_224(record.controller))
}

// ## Internal helpers

fn is_record_output(
  output: Output,
  identity_hash: ByteArray,
  id: ByteArray,
) -> Bool {
  when output.address is {
    Address { payment_credential: Script(hash), stake_credential: None } ->
      and {
        hash == identity_hash,
        holds_single_token(output.value, identity_hash, id),
      }
    _ -> False
  }
}

/// Exactly one token of `policy` in total: `name` with quantity 1.
fn holds_single_token(value: Value, policy: ByteArray, name: ByteArray) -> Bool {
  when dict.to_pairs(assets.tokens(value, policy)) is {
    [Pair(asset_name, quantity)] -> asset_name == name && quantity == 1
    _ -> False
  }
}

fn effective_within(
  record: IdentityDatum,
  refs: List<Input>,
  identity_hash: ByteArray,
  lo: Int,
  hi: Int,
  ancestors_left: Int,
) -> Effective {
  when record.status is {
    Revoked { .. } -> EffRevoked
    Retired { .. } -> EffRetired
    _ -> {
      let inherited =
        when record.lineage is {
          Root -> EffActive
          Subagent { parent, parent_epoch, .. } -> {
            expect ancestors_left > 0
            expect Some(parent_record) =
              find_record(refs, identity_hash, parent)
            if parent_record.subagent_epoch > parent_epoch {
              EffRevoked
            } else {
              effective_within(
                parent_record,
                refs,
                identity_hash,
                lo,
                hi,
                ancestors_left - 1,
              )
            }
          }
        }
      when inherited is {
        EffActive ->
          if hold_in_force(record.status, lo) {
            EffSuspended
          } else if accountable(record, lo, hi) {
            EffActive
          } else {
            EffSuspended
          }
        _ -> inherited
      }
    }
  }
}

fn hold_in_force(status: Status, lo: Int) -> Bool {
  when status is {
    Suspended { holds } -> list.any(holds, fn(h: Hold) { h.until > lo })
    _ -> False
  }
}

/// `accountable(D)` of Part 2 §3.2: some party with role Accountable covers
/// `[lo, hi]` (`not_before <= lo`, and `not_after` is empty or `>= hi`).
fn accountable(record: IdentityDatum, lo: Int, hi: Int) -> Bool {
  list.any(record.parties, fn(party) { covers(party, lo, hi) })
}

/// An Accountable Party that covers `[lo, hi]`.
fn covers(party: Party, lo: Int, hi: Int) -> Bool {
  when party.role is {
    Accountable -> {
      let ends_in_time =
        when party.not_after is {
          None -> True
          Some(not_after) -> not_after >= hi
        }
      party.not_before <= lo && ends_in_time
    }
    _ -> False
  }
}

/// `purpose` is one purpose value (a power of two), and `key.purposes` is a
/// sum of distinct purpose values. The bit test uses division and modulo.
/// Purpose values other than 1, 2, 8 and 16 give `False`, so the division
/// never divides by zero.
fn key_has_purpose(key: Key, purpose: Int) -> Bool {
  let allowed =
    if purpose == purpose_authentication || purpose == purpose_assertion {
      True
    } else if purpose == purpose_transaction || purpose == purpose_delegation {
      when key.key_type is {
        Ed25519 -> True
        P256 -> False
      }
    } else {
      False
    }
  allowed && key.purposes / purpose % 2 == 1
}

// ## Tests

const test_identity_hash: ByteArray =
  #"1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d"

const test_other_hash: ByteArray =
  #"2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e"

const test_controller: ByteArray =
  #"c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"

const test_ed_key: ByteArray =
  #"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"

const test_p256_key: ByteArray =
  #"02a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2"

const test_pop: ByteArray =
  #"50505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050"

/// 2026-11-02T00:00:00Z.
const test_lo: Int = 1_793_577_600_000

/// `test_lo` + 10 minutes.
const test_hi: Int = 1_793_578_200_000

/// The identifier of the n-th test record (32 bytes).
fn test_id(n: Int) -> ByteArray {
  bytearray.push(
    #"00000000000000000000000000000000000000000000000000000000000000",
    n,
  )
}

fn accountable_party(not_before: Int, not_after: Option<Int>) -> Party {
  Party {
    role: Accountable,
    signer: ControllerSet,
    organisation: None,
    not_before,
    not_after,
    rights: right_suspend + right_revoke,
    removal_after: None,
  }
}

fn root_record(status: Status) -> IdentityDatum {
  IdentityDatum {
    controller: test_controller,
    keys: [],
    parties: [accountable_party(test_lo - day_ms, None)],
    guardian_threshold: 0,
    pending_recovery: None,
    lineage: Root,
    subagent_epoch: 0,
    name: "test agent",
    description: "",
    framework: "",
    endpoint: "https://agent.example",
    capabilities: ["ocr.extract.v1"],
    created_at: test_lo - days_30_ms,
    status,
  }
}

fn subagent_record(
  parent: ByteArray,
  parent_epoch: Int,
  depth: Int,
) -> IdentityDatum {
  IdentityDatum {
    ..root_record(Active),
    endpoint: "",
    lineage: Subagent { parent, parent_epoch, depth },
  }
}

fn with_parties(record: IdentityDatum, parties: List<Party>) -> IdentityDatum {
  IdentityDatum { ..record, parties }
}

fn with_keys(keys: List<Key>) -> IdentityDatum {
  IdentityDatum { ..root_record(Active), keys }
}

fn hold(by_hash: ByteArray, until: Int) -> Hold {
  Hold {
    by: KeyHash { hash: by_hash },
    reason: 6,
    since: until - day_ms,
    until,
  }
}

fn test_ref(n: Int) -> OutputReference {
  OutputReference {
    transaction_id: #"7878787878787878787878787878787878787878787878787878787878787878",
    output_index: n,
  }
}

fn identity_address() -> Address {
  Address {
    payment_credential: Script(test_identity_hash),
    stake_credential: None,
  }
}

fn record_value(id: ByteArray) -> Value {
  assets.from_lovelace(10_000_000) |> assets.add(test_identity_hash, id, 1)
}

fn input_with(address: Address, value: Value, datum: Data) -> Input {
  Input {
    output_reference: test_ref(0),
    output: Output {
      address,
      value,
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

fn record_input(id: ByteArray, record: IdentityDatum) -> Input {
  input_with(identity_address(), record_value(id), record)
}

fn tx_signed_by(signers: List<ByteArray>) -> Transaction {
  Transaction { ..transaction.placeholder, extra_signatories: signers }
}

fn effective_now(record: IdentityDatum, refs: List<Input>) -> Effective {
  effective(record, refs, test_identity_hash, test_lo, test_hi)
}

/// Records 0 (root) to `n` (depth `n`): record k > 0 is a subagent of record k - 1.
fn chain_refs(n: Int, root: IdentityDatum) -> List<Input> {
  if n == 0 {
    [record_input(test_id(0), root)]
  } else {
    [
      record_input(test_id(n), subagent_record(test_id(n - 1), 0, n)),
      ..chain_refs(n - 1, root)
    ]
  }
}

// ### find_record

test find_record_finds_live_record() {
  let record = root_record(Active)
  find_record(
    [record_input(test_id(1), record)],
    test_identity_hash,
    test_id(1),
  ) == Some(record)
}

test find_record_finds_tombstone() {
  let record =
    root_record(
      Revoked {
        by: ControllerSet,
        reason: 5,
        at: test_lo,
        invalid_from: None,
        successor: None,
      },
    )
  find_record(
    [record_input(test_id(1), record)],
    test_identity_hash,
    test_id(1),
  ) == Some(record)
}

test find_record_skips_other_inputs() {
  let wanted = root_record(Active)
  let other = root_record(Retired { at: test_lo, successor: None })
  let refs =
    [
      record_input(test_id(2), other),
      input_with(identity_address(), assets.from_lovelace(5_000_000), wanted),
      record_input(test_id(1), wanted),
    ]
  find_record(refs, test_identity_hash, test_id(1)) == Some(wanted)
}

test find_record_none_without_refs() {
  find_record([], test_identity_hash, test_id(1)) == None
}

test find_record_ignores_stake_credential() {
  let address =
    Address {
      payment_credential: Script(test_identity_hash),
      stake_credential: Some(address.Inline(VerificationKey(test_other_hash))),
    }
  let refs =
    [input_with(address, record_value(test_id(1)), root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_other_script_address() {
  let address =
    Address {
      payment_credential: Script(test_other_hash),
      stake_credential: None,
    }
  let refs =
    [input_with(address, record_value(test_id(1)), root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_key_address() {
  let address =
    Address {
      payment_credential: VerificationKey(test_identity_hash),
      stake_credential: None,
    }
  let refs =
    [input_with(address, record_value(test_id(1)), root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_two_identity_tokens() {
  let value =
    record_value(test_id(1)) |> assets.add(test_identity_hash, test_id(2), 1)
  let refs = [input_with(identity_address(), value, root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_quantity_two() {
  let value =
    assets.from_lovelace(10_000_000)
      |> assets.add(test_identity_hash, test_id(1), 2)
  let refs = [input_with(identity_address(), value, root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_missing_token() {
  let refs =
    [
      input_with(
        identity_address(),
        assets.from_lovelace(10_000_000),
        root_record(Active),
      ),
    ]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_other_identifier() {
  let refs = [record_input(test_id(2), root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_token_of_other_policy() {
  let value =
    assets.from_lovelace(10_000_000)
      |> assets.add(test_other_hash, test_id(1), 1)
  let refs = [input_with(identity_address(), value, root_record(Active))]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_datum_hash() {
  let input = record_input(test_id(1), root_record(Active))
  let refs =
    [
      Input {
        ..input,
        output: Output { ..input.output, datum: DatumHash(test_controller) },
      },
    ]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_ignores_missing_datum() {
  let input = record_input(test_id(1), root_record(Active))
  let refs =
    [Input { ..input, output: Output { ..input.output, datum: NoDatum } }]
  find_record(refs, test_identity_hash, test_id(1)) == None
}

test find_record_fails_on_invalid_datum() fail {
  let refs = [input_with(identity_address(), record_value(test_id(1)), 42)]
  find_record(refs, test_identity_hash, test_id(1)) != None
}

// ### effective

test effective_active_root() {
  effective_now(root_record(Active), []) == EffActive
}

test effective_revoked_tombstone() {
  let record =
    root_record(
      Revoked {
        by: KeyHash { hash: test_other_hash },
        reason: 3,
        at: test_lo,
        invalid_from: None,
        successor: None,
      },
    )
  effective_now(with_parties(record, []), []) == EffRevoked
}

test effective_retired_tombstone() {
  let record = root_record(Retired { at: test_lo, successor: Some(test_id(9)) })
  effective_now(with_parties(record, []), []) == EffRetired
}

test effective_tombstone_needs_no_parent() {
  let record =
    IdentityDatum {
      ..subagent_record(test_id(0), 0, 1),
      status: Retired { at: test_lo, successor: None },
    }
  effective_now(record, []) == EffRetired
}

test effective_current_epoch_subagent_active() {
  let parent = IdentityDatum { ..root_record(Active), subagent_epoch: 3 }
  let child = subagent_record(test_id(0), 3, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffActive
}

test effective_epoch_end_revokes_subagent() {
  let parent = IdentityDatum { ..root_record(Active), subagent_epoch: 1 }
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffRevoked
}

test effective_epoch_end_before_parent_status() {
  let parent =
    IdentityDatum {
      ..root_record(Retired { at: test_lo, successor: None }),
      subagent_epoch: 1,
    }
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffRevoked
}

test effective_ancestor_revoked() {
  let parent =
    root_record(
      Revoked {
        by: ControllerSet,
        reason: 1,
        at: test_lo,
        invalid_from: Some(test_lo - day_ms),
        successor: None,
      },
    )
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffRevoked
}

test effective_ancestor_retired() {
  let parent = root_record(Retired { at: test_lo, successor: None })
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffRetired
}

test effective_ancestor_suspended_by_hold() {
  let parent =
    root_record(Suspended { holds: [hold(test_other_hash, test_lo + day_ms)] })
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffSuspended
}

test effective_ancestor_unaccountable() {
  let parent = with_parties(root_record(Active), [])
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(0), parent)]) == EffSuspended
}

test effective_ancestor_status_before_own_hold() {
  let parent = root_record(Retired { at: test_lo, successor: None })
  let child =
    IdentityDatum {
      ..subagent_record(test_id(0), 0, 1),
      status: Suspended { holds: [hold(test_other_hash, test_hi + day_ms)] },
    }
  effective_now(child, [record_input(test_id(0), parent)]) == EffRetired
}

test effective_missing_ancestor_fails() fail {
  let child = subagent_record(test_id(0), 0, 1)
  effective_now(child, [record_input(test_id(5), root_record(Active))]) == EffActive
}

test effective_ancestor_at_other_address_fails() fail {
  let address =
    Address {
      payment_credential: Script(test_identity_hash),
      stake_credential: Some(address.Inline(VerificationKey(test_other_hash))),
    }
  let child = subagent_record(test_id(0), 0, 1)
  let refs =
    [input_with(address, record_value(test_id(0)), root_record(Active))]
  effective_now(child, refs) == EffActive
}

test effective_hold_until_lo_not_in_force() {
  let record =
    root_record(Suspended { holds: [hold(test_other_hash, test_lo)] })
  effective_now(record, []) == EffActive
}

test effective_hold_in_force() {
  let record =
    root_record(Suspended { holds: [hold(test_other_hash, test_lo + 1)] })
  effective_now(record, []) == EffSuspended
}

test effective_one_hold_of_two_in_force() {
  let record =
    root_record(
      Suspended {
        holds: [
          hold(test_other_hash, test_lo - day_ms),
          hold(test_identity_hash, test_hi + 1),
        ],
      },
    )
  effective_now(record, []) == EffSuspended
}

test effective_not_before_at_lo_covers() {
  let record =
    with_parties(root_record(Active), [accountable_party(test_lo, None)])
  effective_now(record, []) == EffActive
}

test effective_not_before_after_lo_suspends() {
  let record =
    with_parties(root_record(Active), [accountable_party(test_lo + 1, None)])
  effective_now(record, []) == EffSuspended
}

test effective_not_after_at_hi_covers() {
  let record =
    with_parties(
      root_record(Active),
      [accountable_party(test_lo, Some(test_hi))],
    )
  effective_now(record, []) == EffActive
}

test effective_not_after_before_hi_suspends() {
  let record =
    with_parties(
      root_record(Active),
      [accountable_party(test_lo, Some(test_hi - 1))],
    )
  effective_now(record, []) == EffSuspended
}

test effective_any_accountable_party_covers() {
  let record =
    with_parties(
      root_record(Active),
      [
        accountable_party(test_lo, Some(test_lo)),
        accountable_party(test_lo - 1, None),
      ],
    )
  effective_now(record, []) == EffActive
}

test effective_responsible_party_does_not_count() {
  let responsible =
    Party { ..accountable_party(test_lo - day_ms, None), role: Responsible }
  let record = with_parties(root_record(Active), [responsible])
  effective_now(record, []) == EffSuspended
}

test effective_no_parties_suspends() {
  effective_now(with_parties(root_record(Active), []), []) == EffSuspended
}

test effective_depth_4_chain_active() {
  let refs = chain_refs(3, root_record(Active))
  effective_now(subagent_record(test_id(3), 0, 4), refs) == EffActive
}

test effective_depth_4_chain_root_suspended() {
  let root =
    root_record(Suspended { holds: [hold(test_other_hash, test_hi + day_ms)] })
  let refs = chain_refs(3, root)
  effective_now(subagent_record(test_id(3), 0, 4), refs) == EffSuspended
}

test effective_depth_4_chain_root_epoch_end() {
  let root = IdentityDatum { ..root_record(Active), subagent_epoch: 1 }
  let refs = chain_refs(3, root)
  effective_now(subagent_record(test_id(3), 0, 4), refs) == EffRevoked
}

test effective_five_ancestors_fail() fail {
  let refs = chain_refs(4, root_record(Active))
  effective_now(subagent_record(test_id(4), 0, 5), refs) == EffActive
}

// ### active_key

test active_key_purpose_bits() {
  let record =
    with_keys(
      [
        Key {
          key_type: Ed25519,
          key: test_ed_key,
          purposes: purpose_authentication + purpose_transaction,
          pop: None,
        },
      ],
    )
  let h = blake2b_224(test_ed_key)
  and {
    active_key(record, h, purpose_authentication),
    !active_key(record, h, purpose_assertion),
    active_key(record, h, purpose_transaction),
    !active_key(record, h, purpose_delegation),
  }
}

test active_key_all_ed25519_purposes() {
  let record =
    with_keys(
      [Key { key_type: Ed25519, key: test_ed_key, purposes: 27, pop: None }],
    )
  let h = blake2b_224(test_ed_key)
  and {
    active_key(record, h, purpose_authentication),
    active_key(record, h, purpose_assertion),
    active_key(record, h, purpose_transaction),
    active_key(record, h, purpose_delegation),
  }
}

test active_key_p256_never_transaction_or_delegation() {
  let record =
    with_keys(
      [
        Key {
          key_type: P256,
          key: test_p256_key,
          purposes: 27,
          pop: Some(test_pop),
        },
      ],
    )
  let h = blake2b_224(test_p256_key)
  and {
    active_key(record, h, purpose_authentication),
    active_key(record, h, purpose_assertion),
    !active_key(record, h, purpose_transaction),
    !active_key(record, h, purpose_delegation),
  }
}

test active_key_p256_assertion() {
  let record =
    with_keys(
      [
        Key {
          key_type: Ed25519,
          key: test_ed_key,
          purposes: purpose_authentication,
          pop: None,
        },
        Key {
          key_type: P256,
          key: test_p256_key,
          purposes: purpose_assertion,
          pop: Some(test_pop),
        },
      ],
    )
  and {
    active_key(record, blake2b_224(test_p256_key), purpose_assertion),
    !active_key(record, blake2b_224(test_p256_key), purpose_authentication),
    !active_key(record, blake2b_224(test_ed_key), purpose_assertion),
  }
}

test active_key_wrong_hash() {
  let record =
    with_keys(
      [Key { key_type: Ed25519, key: test_ed_key, purposes: 27, pop: None }],
    )
  and {
    !active_key(record, blake2b_224(test_controller), purpose_authentication),
    !active_key(record, test_ed_key, purpose_authentication),
  }
}

test active_key_other_purpose_values() {
  let record =
    with_keys(
      [Key { key_type: Ed25519, key: test_ed_key, purposes: 31, pop: None }],
    )
  let h = blake2b_224(test_ed_key)
  and {
    !active_key(record, h, 0),
    !active_key(record, h, 3),
    !active_key(record, h, 4),
    !active_key(record, h, 32),
    !active_key(record, h, -1),
  }
}

test active_key_ignores_unlisted_controller() {
  !active_key(
    root_record(Active),
    blake2b_224(test_controller),
    purpose_transaction,
  )
}

// ### independent, has_capability, controller_signed

test independent_root() {
  independent(root_record(Active))
}

test independent_not_subagent() {
  !independent(subagent_record(test_id(0), 0, 1))
}

test has_capability_exact() {
  has_capability(root_record(Active), "ocr.extract.v1")
}

test has_capability_no_prefix_match() {
  let record = root_record(Active)
  and {
    !has_capability(record, "ocr.extract"),
    !has_capability(record, "ocr.extract.v12"),
    !has_capability(record, ""),
  }
}

test controller_signed_with_key_hash() {
  controller_signed(
    root_record(Active),
    tx_signed_by([test_other_hash, blake2b_224(test_controller)]),
  )
}

test controller_signed_not_signed() {
  !controller_signed(root_record(Active), tx_signed_by([test_other_hash]))
}

test controller_signed_needs_hash() {
  !controller_signed(root_record(Active), tx_signed_by([test_controller]))
}

// ### Constants

test time_constants() {
  and {
    days_7_ms == 7 * day_ms,
    days_30_ms == 30 * day_ms,
    days_90_ms == 90 * day_ms,
    days_366_ms == 366 * day_ms,
    max_range_ms == 60 * 60 * 1000,
    max_time_ms + 1 == 253_402_300_800_000,
  }
}
