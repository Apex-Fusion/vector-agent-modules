//// The identity check of a DID (SPEC §5.8).
////
//// The jury reads identity records only through the vendored library
//// `vector_identity` (see contracts/VENDORED.md). The owner rule follows
//// `declared_credential_ok` of Module-1 `adversarial_auditing/identity_checks`
//// for a verification key hash.

use aiken/collection/list
use aiken/crypto.{blake2b_224}
use cardano/transaction.{Transaction}
use jury/time.{Window}
use vector_identity.{
  active_key, effective, find_record, independent, purpose_transaction,
}
use vector_identity/types.{EffActive, IdentityDatum}

/// Rule 4 of §5.8: `owner` is the hash of the record's controller, or of a
/// listed key with the transaction purpose.
pub fn owner_ok(record: IdentityDatum, owner: ByteArray) -> Bool {
  owner == blake2b_224(record.controller) || active_key(
    record,
    owner,
    purpose_transaction,
  )
}

/// The identity check of §5.8 for the DID `agent` with the owner `owner`.
/// `w` must be `time.window(tx.validity_range)`: that function fails unless
/// rule 1 holds. Rules 2 to 6:
///
/// 2. the record of `agent` is a reference input (`find_record`);
/// 3. the record is not a subagent (`independent`);
/// 4. `owner_ok`;
/// 5. the effective status over `[w.lo, w.hi]` is `EffActive`;
/// 6. `owner` signs the transaction.
pub fn identity_ok(
  identity_hash: ByteArray,
  agent: ByteArray,
  owner: ByteArray,
  w: Window,
  tx: Transaction,
) -> Bool {
  when find_record(tx.reference_inputs, identity_hash, agent) is {
    None -> False
    Some(record) -> and {
        independent(record),
        owner_ok(record, owner),
        list.has(tx.extra_signatories, owner),
        when
          effective(record, tx.reference_inputs, identity_hash, w.lo, w.hi)
        is {
          EffActive -> True
          _ -> False
        },
      }
  }
}
