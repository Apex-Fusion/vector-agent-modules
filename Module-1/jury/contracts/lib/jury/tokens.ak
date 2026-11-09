//// Jury token names (SPEC §4).

use aiken/cbor
use aiken/crypto.{blake2b_256}
use aiken/primitive/bytearray
use cardano/transaction.{OutputReference}

/// Prefix of a juror token name: ASCII `jrr_` (hex 6a72725f).
pub const juror_prefix: ByteArray = "jrr_"

/// Prefix of a case token name: ASCII `cas_` (hex 6361735f).
pub const case_prefix: ByteArray = "cas_"

/// A 32-byte token name: `prefix` (4 bytes), then the first 28 bytes of
/// `blake2b_256(serialiseData(seed))`.
pub fn token_name(prefix: ByteArray, seed: OutputReference) -> ByteArray {
  bytearray.concat(
    prefix,
    bytearray.take(blake2b_256(cbor.serialise(seed)), 28),
  )
}

// ## Tests
//
// The expected names come from an independent Python computation over the
// bytes `d8799f 5820 <txid> <index> ff` (Module-1/simulation/chain.py
// `derive_token_name`).

const vector_txid: ByteArray =
  #"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"

test prefixes_are_ascii() {
  and {
    juror_prefix == #"6a72725f",
    case_prefix == #"6361735f",
  }
}

test seed_serialisation() {
  cbor.serialise(
    OutputReference { transaction_id: vector_txid, output_index: 0 },
  ) == #"d8799f5820000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f00ff"
}

test juror_name_vector_index_0() {
  token_name(
    juror_prefix,
    OutputReference { transaction_id: vector_txid, output_index: 0 },
  ) == #"6a72725f43ad75d45f7db29680da9255037d1efc023caed83a69a1d6f7b3a330"
}

test case_name_vector_index_0() {
  token_name(
    case_prefix,
    OutputReference { transaction_id: vector_txid, output_index: 0 },
  ) == #"6361735f43ad75d45f7db29680da9255037d1efc023caed83a69a1d6f7b3a330"
}

test juror_name_vector_index_24() {
  token_name(
    juror_prefix,
    OutputReference { transaction_id: vector_txid, output_index: 24 },
  ) == #"6a72725ffc9ea1787d6e27c1479c1e065670c92a89b7d48cdbd5f3e64c0908ce"
}

test case_name_vector_index_300() {
  token_name(
    case_prefix,
    OutputReference { transaction_id: vector_txid, output_index: 300 },
  ) == #"6361735fa6d9454422bf877a4136865f89733ab975f0bd175da96c8e76a2247d"
}

test token_name_is_32_bytes() {
  bytearray.length(
    token_name(
      juror_prefix,
      OutputReference { transaction_id: vector_txid, output_index: 1 },
    ),
  ) == 32
}
