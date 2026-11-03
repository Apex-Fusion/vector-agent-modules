//// Record types of the did:vector method: Part 1 §5 (`docs/did-vector-method.md`).
////
//// The field order and the constructor order are part of the on-chain format.
//// A record type is `Constr 0` with its fields in the order shown. The
//// constructors of a sum type are numbered from 0 in the order shown (Plutus
//// Data tags 121, 122, ...). Do not reorder or insert fields or constructors.
////
//// The module contracts vendor this file unchanged. Keep it compatible with
//// aiken-lang/stdlib v3.0.0 and v3.1.0.

use cardano/address.{Credential}

/// The inline datum of an identity record: `Constr 0` with 14 fields.
pub type IdentityDatum {
  /// Ed25519 public key, 32 bytes.
  controller: ByteArray,
  /// 0..8 listed keys.
  keys: List<Key>,
  /// 0..8 parties.
  parties: List<Party>,
  /// 0 = no recovery.
  guardian_threshold: Int,
  pending_recovery: Option<PendingRecovery>,
  lineage: Lineage,
  subagent_epoch: Int,
  /// UTF-8 text.
  name: ByteArray,
  /// UTF-8 text.
  description: ByteArray,
  /// UTF-8 text.
  framework: ByteArray,
  /// UTF-8 URL.
  endpoint: ByteArray,
  /// UTF-8 text items.
  capabilities: List<ByteArray>,
  /// POSIX milliseconds.
  created_at: Int,
  status: Status,
}

/// A listed key. `purposes` is a sum of purpose values: authentication (1),
/// assertion (2), transaction (8), delegation (16).
pub type Key {
  key_type: KeyType,
  key: ByteArray,
  purposes: Int,
  pop: Option<ByteArray>,
}

/// `Ed25519`: 32-byte key, no `pop`. `P256`: 33-byte compressed point with a
/// 64-byte `pop`.
pub type KeyType {
  Ed25519
  P256
}

/// The signer of a party, a hold or a revocation.
pub type Signer {
  /// 28 bytes.
  KeyHash { hash: ByteArray }
  /// 28 bytes; only the approved Module-1 contract.
  Script { hash: ByteArray }
  /// Whoever is the record's controller.
  ControllerSet
}

pub type Role {
  Accountable
  Responsible
  Provider
  Distributor
  Guardian
}

/// A party. `rights` is a sum of right values: suspend (1), revoke (2),
/// recover (4).
pub type Party {
  role: Role,
  signer: Signer,
  /// UTF-8 DID or domain of an organisation, 1..64 bytes.
  organisation: Option<ByteArray>,
  not_before: Int,
  not_after: Option<Int>,
  rights: Int,
  removal_after: Option<Int>,
}

pub type Lineage {
  Root
  Subagent { parent: ByteArray, parent_epoch: Int, depth: Int }
}

pub type Status {
  Active
  /// 1..10 holds, at most one for each author.
  Suspended { holds: List<Hold> }
  Revoked {
    by: Signer,
    reason: Int,
    at: Int,
    invalid_from: Option<Int>,
    successor: Option<ByteArray>,
  }
  Retired { at: Int, successor: Option<ByteArray> }
}

pub type Hold {
  by: Signer,
  reason: Int,
  since: Int,
  until: Int,
}

pub type PendingRecovery {
  new_controller: ByteArray,
  effective_after: Int,
}

/// Legacy registry record: `Constr 0` with 7 fields.
pub type LegacyDatum {
  /// VerificationKey(hash) or Script(hash).
  owner: Credential,
  name: ByteArray,
  description: ByteArray,
  capabilities: List<ByteArray>,
  framework: ByteArray,
  endpoint: ByteArray,
  /// Self-declared; not used.
  registered_at: Int,
}

/// The effective status that `vector_identity.effective` computes. It is never
/// stored on-chain.
pub type Effective {
  EffActive
  EffSuspended
  EffRevoked
  EffRetired
}
