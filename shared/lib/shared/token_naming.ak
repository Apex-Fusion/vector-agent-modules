/// Shared token name derivation utilities.
///
/// Generic pattern: prefix + blake2b_256(data)[0..slice_len]
/// Each game defines its own prefixes and calls these helpers.

use aiken/builtin
use aiken/crypto.{blake2b_256}
use cardano/transaction.{OutputReference}

/// Derive a token name: prefix ++ blake2b_256(data)[0..slice_len]
///
/// This is the generic pattern used across all Vector games:
/// - Game 3: "rstk_", "rend_", "rchl_", "hbonus_"
/// - Game 6: "prop_", "crit_", "gend_", "pact_", "pred_"
pub fn derive_token_name(
  prefix: ByteArray,
  data: ByteArray,
  slice_len: Int,
) -> ByteArray {
  let hash = blake2b_256(data)
  builtin.append_bytearray(prefix, builtin.slice_bytearray(0, slice_len, hash))
}

/// Serialise an OutputReference for hashing.
/// Uses CBOR serialisation via builtin for Conway era compatibility.
pub fn serialise_output_reference(oref: OutputReference) -> ByteArray {
  builtin.serialise_data(oref)
}
