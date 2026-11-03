/// Holder validator for parameter, oracle and treasury records.
///
/// The parameters give each holder a distinct script address:
///   tag=1 → parameter records
///   tag=2 → oracle record (and the cross-validator references)
///   tag=3 → treasury
///
/// Only the owner key can spend an output at a holder address. The module
/// validators accept a parameter or oracle record only when the reference
/// input also holds the record token of `record_token.ak`.

use aiken/collection/list
use aiken/crypto.{VerificationKeyHash}
use cardano/transaction.{OutputReference, Transaction}

validator holder(tag: Int, owner: VerificationKeyHash) {
  spend(
    _datum: Option<Data>,
    _redeemer: Data,
    _own_utxo: OutputReference,
    tx: Transaction,
  ) {
    tag > 0 && list.has(tx.extra_signatories, owner)
  }

  else(_) {
    fail @"Unsupported script purpose"
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

const test_owner: VerificationKeyHash =
  #"0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a"

const test_other: VerificationKeyHash =
  #"0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"

const test_utxo: OutputReference =
  OutputReference {
    transaction_id: #"0000000000000000000000000000000000000000000000000000000000000001",
    output_index: 0,
  }

test holder_spend_with_owner_signature() {
  let tx = Transaction { ..transaction.placeholder, extra_signatories: [test_owner] }
  holder.spend(1, test_owner, None, Void, test_utxo, tx)
}

/// The old holder accepted every spend. The new holder needs the owner key.
test holder_spend_without_owner_signature_fails() {
  let tx = Transaction { ..transaction.placeholder, extra_signatories: [test_other] }
  !holder.spend(1, test_owner, None, Void, test_utxo, tx)
}

test holder_spend_without_signatures_fails() {
  !holder.spend(2, test_owner, None, Void, test_utxo, transaction.placeholder)
}

test holder_spend_with_zero_tag_fails() {
  let tx = Transaction { ..transaction.placeholder, extra_signatories: [test_owner] }
  !holder.spend(0, test_owner, None, Void, test_utxo, tx)
}
