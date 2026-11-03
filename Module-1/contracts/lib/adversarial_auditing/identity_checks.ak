//// Identity registry checks for Module-1 (Part 2 §7, Module-1 row) and the
//// reader of the identity configuration record (Part 2 §3.8).
////
//// Module-1 reads identity records only through the vendored library
//// `vector_identity` (see Module-1/VENDORED.md).

use aiken/collection/dict
use aiken/collection/list
use aiken/crypto.{blake2b_224}
use aiken/interval.{Finite}
use aiken/primitive/bytearray
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, Transaction, ValidityRange}
use vector_identity.{
  active_key, effective, find_record, max_range_ms, purpose_transaction,
}
use vector_identity/types.{EffActive, IdentityDatum}

/// Asset name of the identity configuration token: UTF-8 `identity-config`
/// (hex 6964656e746974792d636f6e666967).
pub const config_token_name: ByteArray = "identity-config"

/// Reason code 64 (disputeVerdict, Part 2 Appendix A).
pub const reason_dispute_verdict: Int = 64

/// The finite bounds of a validity range.
pub type Window {
  lo: Int,
  hi: Int,
}

/// The window of a transaction that reads identity data (Part 2 §7, Time).
/// `lo` and `hi` are the values of the finite bounds; the inclusive flags are
/// ignored. The function fails when a bound is infinite, when `hi < lo` or
/// when `hi - lo > max_range_ms` (one hour). This is the convention of
/// `identity_marketplace/common.window`.
pub fn window(range: ValidityRange) -> Window {
  expect Finite(lo) = range.lower_bound.bound_type
  expect Finite(hi) = range.upper_bound.bound_type
  expect lo <= hi
  expect hi - lo <= max_range_ms
  Window { lo, hi }
}

/// The identity check of a Module-1 action that opens a position (claim
/// submission, challenge opening, juror registration):
///
/// - the transaction window satisfies `window` (at most one hour);
/// - the record of `id` is a reference input (`find_record`);
/// - the declared credential is a verification key whose hash is the hash of
///   the record's controller, or of a listed key with the transaction purpose;
/// - the effective status over the window is Active.
pub fn identity_ok(
  identity_hash: ByteArray,
  id: ByteArray,
  credential: Credential,
  tx: Transaction,
) -> Bool {
  let Window { lo, hi } = window(tx.validity_range)
  // `lo <= hi` comes first, so the window rule runs on every path.
  and {
    lo <= hi,
    when find_record(tx.reference_inputs, identity_hash, id) is {
      None -> False
      Some(record) -> and {
          declared_credential_ok(record, credential),
          when
            effective(record, tx.reference_inputs, identity_hash, lo, hi)
          is {
            EffActive -> True
            _ -> False
          },
        }
    },
  }
}

/// The declared credential is a verification key whose hash is the hash of
/// the record's controller, or of a listed key with the transaction purpose.
pub fn declared_credential_ok(
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

/// `M1` of Part 2 §3.2: the script hash in the inline datum of the reference
/// input at the exact `config_holder` address (no stake credential) that holds
/// the `config_policy` token `identity-config`. The datum is a 28-byte byte
/// string. The function fails when no such reference input exists or when the
/// datum is not a 28-byte byte string.
pub fn config_record_hash(
  refs: List<Input>,
  config_policy: ByteArray,
  config_holder: ByteArray,
) -> ByteArray {
  let holder =
    Address {
      payment_credential: Script(config_holder),
      stake_credential: None,
    }
  expect Some(record) =
    list.find(
      refs,
      fn(input) {
        and {
          input.output.address == holder,
          assets.quantity_of(
            input.output.value,
            config_policy,
            config_token_name,
          ) == 1,
        }
      },
    )
  expect InlineDatum(data) = record.output.datum
  expect hash: ByteArray = data
  expect bytearray.length(hash) == 28
  hash
}

/// True when the transaction mints exactly one token under `policy`, with
/// quantity 1.
pub fn mints_one_token(mint: assets.Value, policy: ByteArray) -> Bool {
  when dict.to_pairs(assets.tokens(mint, policy)) is {
    [Pair(_, 1)] -> True
    _ -> False
  }
}

/// True when the output pays to the exact enterprise address of the script
/// `hash`.
pub fn at_script_address(output: Output, hash: ByteArray) -> Bool {
  output.address == Address {
    payment_credential: Script(hash),
    stake_credential: None,
  }
}
