/// Simple always-succeeds holder validator, parameterized by a tag.
/// Used to create distinct script addresses for governance infrastructure:
///   tag=1 → GovernanceParams holder
///   tag=2 → Governance Oracle holder
///   tag=3 → Governance Treasury holder
///
/// These are intentionally minimal — anyone can spend from them.
/// The security model relies on the governance validators checking
/// that the correct datums exist at these addresses, not on the
/// holder scripts preventing unauthorized access.

use cardano/transaction.{OutputReference, Transaction}

validator holder(tag: Int) {
  spend(
    _datum: Option<Data>,
    _redeemer: Data,
    _own_utxo: OutputReference,
    _tx: Transaction,
  ) {
    tag > 0
  }

  else(_) {
    fail @"Unsupported script purpose"
  }
}
