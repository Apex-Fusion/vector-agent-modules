/// Shared credential and signature verification utilities.
///
/// Helpers for checking transaction signatures against credentials.
/// Used by Modules 3, 6, and future modules.

use aiken/collection/list
use cardano/address.{Credential, Script, VerificationKey}

/// Check if a credential holder has signed the transaction.
/// For VerificationKey credentials, checks the signatories list.
/// For Script credentials, returns True (script execution is verified by the ledger).
pub fn has_credential_signed(
  credential: Credential,
  signatories: List<ByteArray>,
) -> Bool {
  when credential is {
    VerificationKey(hash) -> list.any(signatories, fn(s) { s == hash })
    Script(_) -> True
  }
}

/// Check that a payment credential is a VerificationKey and has signed.
/// Returns False for Script credentials (stricter than has_credential_signed).
pub fn verify_signer(
  credential: Credential,
  signatories: List<ByteArray>,
) -> Bool {
  when credential is {
    VerificationKey(hash) -> list.any(signatories, fn(s) { s == hash })
    _ -> False
  }
}

/// Check that the transaction is signed by a specific verification key hash.
pub fn has_key_signed(signatories: List<ByteArray>, key_hash: ByteArray) -> Bool {
  list.has(signatories, key_hash)
}
