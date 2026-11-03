//// Identity registry checks for the module contracts (Part 2 §7 of
//// `docs/IDENTITY_REGISTRY.md` in agents-marketplace).
////
//// The modules read identity records only through the vendored library
//// `vector_identity` (see `shared/VENDORED.md`). An agent identifier in a
//// module datum is the identifier of an identity record (did:vector).
////
//// - Every action that reads an identity record has a validity range with
////   finite bounds and `hi - lo <= max_range_ms` (one hour): see `window`.
//// - Ages and other elapsed times use `lo`. The effective status covers the
////   whole window `[lo, hi]`.

use aiken/collection/list
use aiken/crypto.{blake2b_224}
use aiken/interval.{Finite}
use cardano/address.{Credential, Script, VerificationKey}
use cardano/transaction.{Input, Transaction, ValidityRange}
use vector_identity.{
  active_key, controller_signed, effective, find_record, has_capability,
  max_range_ms, purpose_transaction,
}
use vector_identity/types.{EffActive, IdentityDatum}

/// The finite bounds of the validity range of a transaction.
pub type Window {
  lo: Int,
  hi: Int,
}

/// The window of a transaction that reads identity records: both bounds are
/// finite, `lo <= hi` and `hi - lo <= max_range_ms`. Else `None`. `lo` and
/// `hi` are the values of the bounds; the inclusive flags are ignored, as in
/// `identity_marketplace/common.window`.
pub fn window(range: ValidityRange) -> Option<Window> {
  when range.lower_bound.bound_type is {
    Finite(lo) ->
      when range.upper_bound.bound_type is {
        Finite(hi) ->
          if lo <= hi && hi - lo <= max_range_ms {
            Some(Window { lo, hi })
          } else {
            None
          }
        _ -> None
      }
    _ -> None
  }
}

/// The record of `id` when it is a reference input (`find_record`: the exact
/// identity address and exactly one token `(identity_hash, id)`) and its
/// effective status over `w` is Active. Else `None`. The function fails when
/// an ancestor of a subagent is not a reference input (`effective`).
pub fn active_record(
  refs: List<Input>,
  identity_hash: ByteArray,
  id: ByteArray,
  w: Window,
) -> Option<IdentityDatum> {
  when find_record(refs, identity_hash, id) is {
    Some(record) ->
      if effective(record, refs, identity_hash, w.lo, w.hi) == EffActive {
        Some(record)
      } else {
        None
      }
    None -> None
  }
}

/// True when the record lists each of `capabilities` (exact match).
pub fn lists_capabilities(
  record: IdentityDatum,
  capabilities: List<ByteArray>,
) -> Bool {
  list.all(capabilities, fn(capability) { has_capability(record, capability) })
}

/// True when the record of `id` is Active over `w` (`active_record`) and lists
/// each of `capabilities`. No signature is needed: use it for an agent that
/// an action names but that does not act, such as the target of an
/// endorsement or a challenge.
pub fn active_with_capabilities(
  refs: List<Input>,
  identity_hash: ByteArray,
  id: ByteArray,
  w: Window,
  capabilities: List<ByteArray>,
) -> Bool {
  when active_record(refs, identity_hash, id, w) is {
    Some(record) -> lists_capabilities(record, capabilities)
    None -> False
  }
}

/// An action by agent `id` that opens or extends a position (Part 2 §7,
/// Module-3 row): the record of `id` is Active over `w`, it lists each of
/// `capabilities`, and its controller signed the transaction
/// (`controller_signed`).
pub fn controller_opens(
  tx: Transaction,
  identity_hash: ByteArray,
  id: ByteArray,
  w: Window,
  capabilities: List<ByteArray>,
) -> Bool {
  when active_record(tx.reference_inputs, identity_hash, id, w) is {
    Some(record) ->
      lists_capabilities(record, capabilities) && controller_signed(record, tx)
    None -> False
  }
}

/// An exit by agent `id` (Part 2 §7, Module-3 row): the record of `id` is a
/// reference input and its controller signed the transaction. A tombstone
/// counts and the status does not matter, so a revoked or retired agent can
/// exit.
pub fn controller_exits(
  tx: Transaction,
  identity_hash: ByteArray,
  id: ByteArray,
) -> Bool {
  when find_record(tx.reference_inputs, identity_hash, id) is {
    Some(record) -> controller_signed(record, tx)
    None -> False
  }
}

/// The declared credential is a verification key whose hash is the hash of
/// the record's controller, or of a listed key with the transaction purpose
/// (`active_key(record, hash, purpose_transaction)`). A script credential
/// never passes. Part 2 §7, Module-1 and Module-6 rows.
pub fn declared_credential(
  record: IdentityDatum,
  credential: Credential,
) -> Bool {
  when credential is {
    VerificationKey(key_hash) ->
      key_hash == blake2b_224(record.controller) || active_key(
        record,
        key_hash,
        purpose_transaction,
      )
    Script(_) -> False
  }
}

/// An action by agent `id` with a declared credential that opens a position
/// (Part 2 §7, Module-6 row): the record of `id` is Active over `w`, the
/// credential is a declared credential of the record (`declared_credential`)
/// and the key of the credential signed the transaction.
pub fn credential_opens(
  tx: Transaction,
  identity_hash: ByteArray,
  id: ByteArray,
  credential: Credential,
  w: Window,
) -> Bool {
  when active_record(tx.reference_inputs, identity_hash, id, w) is {
    Some(record) ->
      when credential is {
        VerificationKey(key_hash) ->
          declared_credential(record, credential) && list.has(
            tx.extra_signatories,
            key_hash,
          )
        Script(_) -> False
      }
    None -> False
  }
}
