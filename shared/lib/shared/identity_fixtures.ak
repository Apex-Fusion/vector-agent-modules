//// Test fixtures for identity registry records (Part 1 §5). Only tests use
//// this module; no validator imports it.
////
//// A record input is a reference input at the exact identity address
//// (`identity_hash`, no stake credential) that holds 10 AP3X and exactly one
//// token `(identity_hash, id)`, with the record as inline datum.

use aiken/crypto.{blake2b_224}
use aiken/primitive/bytearray
use cardano/address.{Address, Inline, Script, VerificationKey}
use cardano/assets.{Value}
use cardano/transaction.{InlineDatum, Input, Output, OutputReference}
use vector_identity.{
  purpose_authentication, purpose_transaction, right_revoke, right_suspend,
}
use vector_identity/types.{
  Accountable, Active, ControllerSet, Ed25519, Hold, IdentityDatum, Key, KeyType,
  P256, Party, Retired, Revoked, Root, Status, Subagent, Suspended,
}

/// Script hash (= policy ID) of the identity registry in tests.
pub const identity_hash: ByteArray =
  #"1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d"

/// Another script hash (a wrong address or a fake token policy).
pub const other_hash: ByteArray =
  #"2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e"

/// Reason code 6 (hold, Part 2 Appendix A).
pub const reason_hold: Int = 6

/// Reason code 5 (cessationOfOperation, Part 2 Appendix A).
pub const reason_cessation: Int = 5

/// The identifier of test agent `n` (32 bytes).
pub fn agent_id(n: Int) -> ByteArray {
  bytearray.push(
    #"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
    n,
  )
}

/// The Ed25519 controller key of test agent `n` (32 bytes).
pub fn controller_key(n: Int) -> ByteArray {
  bytearray.push(
    #"c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
    n,
  )
}

/// The key hash (`blake2b_224`) of the controller key of test agent `n`.
pub fn controller_hash(n: Int) -> ByteArray {
  blake2b_224(controller_key(n))
}

/// Ed25519 key `n` that a record can list (32 bytes).
pub fn ed_key(n: Int) -> ByteArray {
  bytearray.push(
    #"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1",
    n,
  )
}

/// The key hash of Ed25519 key `n`.
pub fn ed_key_hash(n: Int) -> ByteArray {
  blake2b_224(ed_key(n))
}

/// P-256 key `n` (33 bytes, compressed point form).
pub fn p256_key(n: Int) -> ByteArray {
  bytearray.push(
    #"02a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2",
    n,
  )
}

/// The key hash of P-256 key `n`.
pub fn p256_key_hash(n: Int) -> ByteArray {
  blake2b_224(p256_key(n))
}

/// The controller as Accountable Party, from time 0 without end.
pub fn accountable_controller() -> Party {
  Party {
    role: Accountable,
    signer: ControllerSet,
    organisation: None,
    not_before: 0,
    not_after: None,
    rights: right_suspend + right_revoke,
    removal_after: None,
  }
}

/// An Active root record of test agent `n` with `capabilities`. The
/// controller is `controller_key(n)` and the Accountable Party. It lists no
/// keys. `created_at` is 0.
pub fn record(n: Int, capabilities: List<ByteArray>) -> IdentityDatum {
  IdentityDatum {
    controller: controller_key(n),
    keys: [],
    parties: [accountable_controller()],
    guardian_threshold: 0,
    pending_recovery: None,
    lineage: Root,
    subagent_epoch: 0,
    name: "test agent",
    description: "",
    framework: "",
    endpoint: "https://agent.example",
    capabilities,
    created_at: 0,
    status: Active,
  }
}

/// The record with `created_at = at`.
pub fn created_at(record: IdentityDatum, at: Int) -> IdentityDatum {
  IdentityDatum { ..record, created_at: at }
}

/// The record with one more listed key.
pub fn with_key(
  record: IdentityDatum,
  key_type: KeyType,
  key: ByteArray,
  purposes: Int,
) -> IdentityDatum {
  let pop =
    when key_type is {
      Ed25519 -> None
      P256 ->
        Some(
          #"5050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050",
        )
    }
  IdentityDatum {
    ..record,
    keys: [Key { key_type, key, purposes, pop }, ..record.keys],
  }
}

/// The record with listed Ed25519 key `ed_key(n)` with the transaction
/// purpose (and the authentication purpose).
pub fn with_transaction_key(record: IdentityDatum, n: Int) -> IdentityDatum {
  with_key(
    record,
    Ed25519,
    ed_key(n),
    purpose_authentication + purpose_transaction,
  )
}

/// The record with a controller hold that is in force until `until`.
pub fn suspended(record: IdentityDatum, until: Int) -> IdentityDatum {
  IdentityDatum {
    ..record,
    status: Suspended {
      holds: [Hold { by: ControllerSet, reason: reason_hold, since: 0, until }],
    },
  }
}

/// The record without parties, so no Accountable Party covers any window.
pub fn unaccountable(record: IdentityDatum) -> IdentityDatum {
  IdentityDatum { ..record, parties: [] }
}

/// The record with an Accountable Party whose period ends at `not_after`.
pub fn accountable_until(record: IdentityDatum, not_after: Int) -> IdentityDatum {
  IdentityDatum {
    ..record,
    parties: [Party { ..accountable_controller(), not_after: Some(not_after) }],
  }
}

/// The tombstone of the record with `status` (Part 2 §3.7).
fn tombstone(record: IdentityDatum, status: Status) -> IdentityDatum {
  IdentityDatum {
    ..record,
    keys: [],
    parties: [],
    guardian_threshold: 0,
    pending_recovery: None,
    description: "",
    framework: "",
    endpoint: "",
    capabilities: [],
    status,
  }
}

/// The record revoked by its controller (a tombstone).
pub fn revoked(record: IdentityDatum) -> IdentityDatum {
  tombstone(
    record,
    Revoked {
      by: ControllerSet,
      reason: reason_cessation,
      at: 0,
      invalid_from: None,
      successor: None,
    },
  )
}

/// The record retired by its controller (a tombstone).
pub fn retired(record: IdentityDatum) -> IdentityDatum {
  tombstone(record, Retired { at: 0, successor: None })
}

/// The record as a subagent of `parent` created under `parent_epoch`, at
/// `depth`. A subagent has no endpoint.
pub fn subagent(
  record: IdentityDatum,
  parent: ByteArray,
  parent_epoch: Int,
  depth: Int,
) -> IdentityDatum {
  IdentityDatum {
    ..record,
    endpoint: "",
    lineage: Subagent { parent, parent_epoch, depth },
  }
}

/// The record with `subagent_epoch = epoch`.
pub fn with_subagent_epoch(record: IdentityDatum, epoch: Int) -> IdentityDatum {
  IdentityDatum { ..record, subagent_epoch: epoch }
}

/// The exact identity address: `Script(identity_hash)`, no stake credential.
pub fn identity_address() -> Address {
  Address { payment_credential: Script(identity_hash), stake_credential: None }
}

/// The identity script address with a stake credential (not the exact
/// identity address).
pub fn identity_address_with_stake() -> Address {
  Address {
    payment_credential: Script(identity_hash),
    stake_credential: Some(Inline(VerificationKey(controller_hash(0)))),
  }
}

/// 10 AP3X and the token `(identity_hash, id)`.
pub fn record_value(id: ByteArray) -> Value {
  assets.from_lovelace(10_000_000) |> assets.add(identity_hash, id, 1)
}

/// A reference input with `datum` at `address` that holds `value`.
pub fn input_at(address: Address, value: Value, datum: Data) -> Input {
  Input {
    output_reference: OutputReference {
      transaction_id: #"1d0000000000000000000000000000000000000000000000000000000000001d",
      output_index: 0,
    },
    output: Output {
      address,
      value,
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The authentic record input of `id`.
pub fn record_input(id: ByteArray, record: IdentityDatum) -> Input {
  input_at(identity_address(), record_value(id), record)
}

/// The record of `id` at the identity script address with a stake
/// credential (a wrong address).
pub fn record_input_with_stake(id: ByteArray, record: IdentityDatum) -> Input {
  input_at(identity_address_with_stake(), record_value(id), record)
}

/// The record of `id` at another script address (a wrong address).
pub fn record_input_at_other_script(
  id: ByteArray,
  record: IdentityDatum,
) -> Input {
  input_at(
    Address { payment_credential: Script(other_hash), stake_credential: None },
    record_value(id),
    record,
  )
}

/// The record of `id` at the identity address, with a token `id` of another
/// policy instead of the identity token (a fake token).
pub fn record_input_with_fake_token(
  id: ByteArray,
  record: IdentityDatum,
) -> Input {
  input_at(
    identity_address(),
    assets.from_lovelace(10_000_000) |> assets.add(other_hash, id, 1),
    record,
  )
}
