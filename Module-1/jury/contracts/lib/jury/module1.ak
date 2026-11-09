//// Module-1 data that the jury reads (SPEC §5.7).
////
//// Copied from `Module-1/contracts/lib/adversarial_auditing/types.ak`: the
//// types `ClaimDatum`, `ClaimState`, `ChallengeDatum`, `ChallengeState` and
//// `Verdict`, with the same field order and the same constructors. The
//// deployed Module-1 fixes this order. Do not change it: `expect x: T = data`
//// checks the full structure, so a difference makes the decoding fail.
////
//// The jury uses only some fields (§5.7), but it decodes all of them.

use cardano/address.{Address, Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference}

/// A Module-1 claim: `Constr 0` with 9 fields.
pub type ClaimDatum {
  /// Field 0: the claimer DID (32 bytes).
  claimer_did: PolicyId,
  claimer_credential: Credential,
  /// Field 2: the claim document hash.
  claim_hash: ByteArray,
  claim_type: ByteArray,
  /// Field 4: the claim document URI.
  storage_uri: ByteArray,
  stake_amount: Int,
  submitted_at: Int,
  challenge_window: Int,
  /// Field 8.
  state: ClaimState,
}

pub type ClaimState {
  Open
  /// Constr 1.
  Challenged
  Validated
  Invalidated
}

/// A Module-1 challenge: `Constr 0` with 11 fields.
pub type ChallengeDatum {
  claim_ref: OutputReference,
  /// Field 1: the auditor DID (32 bytes).
  auditor_did: PolicyId,
  auditor_credential: Credential,
  stake_amount: Int,
  /// Field 4: the evidence document hash.
  evidence_hash: ByteArray,
  /// Field 5: the evidence document URI.
  evidence_uri: ByteArray,
  /// Field 6: POSIX milliseconds.
  challenged_at: Int,
  /// Field 7: milliseconds after `challenged_at`.
  resolution_deadline: Int,
  eligible_jurors: List<PolicyId>,
  /// Field 9.
  state: ChallengeState,
  /// Field 10: the claim token name of the challenged claim.
  claim_token: ByteArray,
}

pub type ChallengeState {
  /// Constr 0.
  PendingOracle
  PendingJury
  Voting { selected_jurors: List<PolicyId> }
  Resolved { verdict: Verdict, refund_to: Address }
}

pub type Verdict {
  ClaimerWins
  AuditorWins
  Inconclusive
}
