//// Redeemers of the identity contract: Part 2 §3.1 (`docs/IDENTITY_REGISTRY.md`).
////
//// The constructor order is part of the on-chain format (Plutus Data tags
//// 121, 122, ... in the order shown). Do not reorder or insert constructors.
////
//// These types live beside the record types so that the module contracts,
//// which vendor `lib/vector_identity/` unchanged, can decode identity
//// redeemers (Module-1 `identity_party`, Part 2 §3.8).

use cardano/transaction.{OutputReference}
use vector_identity/types.{Signer}

/// Mint redeemer of the identity policy.
pub type IdentityMint {
  Register { seed: OutputReference }
  /// Merkle proof against `legacy_root` (Part 2 §3.4).
  Adopt { proof: List<ByteArray> }
}

/// The author of a `Suspend` or a `Revoke`.
pub type Actor {
  ByController
  /// Index of the acting party in the spent record's `parties`.
  ByParty { index: Int }
}

/// Spend redeemer of the identity contract.
pub type IdentitySpend {
  Update
  ChangeController
  Suspend { actor: Actor }
  Unsuspend { author: Signer }
  Revoke { actor: Actor }
  Retire
  Reclaim
  SetSuccessor
  BumpEpoch
  RecoverInit { guardians: List<Int> }
  RecoverCancel { guardians: List<Int> }
  RecoverFinalize
}
