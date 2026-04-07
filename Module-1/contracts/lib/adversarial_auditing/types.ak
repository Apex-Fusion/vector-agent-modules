/// Shared types for Module 1: Adversarial Auditing (Vector/eUTXO)
///
/// Spec: game-1-adversarial-auditing-impl-spec.md (v0.3)
/// Author: Contract Author (Apex Security Audit Team)
///
/// VRF research incorporated: the VRF report 2026-03-23
///   - eligible_jurors snapshot added to ChallengeDatum (pool freeze at challenge time)
///   - SelectJury in Phase 1.0 is oracle-controlled; seed is cosmetic
///   - SELECTION_DELAY set to 30 slots minimum (up from spec's 10)
///
/// Three multi-validators:
///   claim.ak      — SubmitClaim / WithdrawClaim / MarkChallenged / ForfeitClaim
///   challenge.ak  — OpenChallenge / SubmitEvidence / OracleResolve / TimeoutResolve
///   jury_pool.ak  — RegisterJuror / SelectJury / CastVote / DistributeRewards
///
/// All datums use inline datum pattern (CIP-32). Tokens track lifecycle.

use cardano/address.{Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference}

// ══════════════════════════════════════════════════════════════════════
// CLAIM TYPES
// ══════════════════════════════════════════════════════════════════════

/// A claim submitted by an agent asserting it performed some task.
/// One UTXO per claim — fully parallelizable across thousands of audits.
pub type ClaimDatum {
  /// DID of the claiming agent (policy_id of their registry NFT)
  claimer_did: PolicyId,
  /// Payment credential of claimer (for stake return)
  claimer_credential: Credential,
  /// blake2b_256 hash of the off-chain claim data (exactly 32 bytes)
  /// Full claim data lives off-chain: OriginTrail UAL or IPFS CID
  claim_hash: ByteArray,
  /// Category tag for claim routing (e.g. "data_indexing", "task_completion")
  claim_type: ByteArray,
  /// Off-chain storage URI for full evidence (not validated on-chain)
  storage_uri: ByteArray,
  /// AP3X staked by claimer (in DFM units, 6 decimals)
  stake_amount: Int,
  /// POSIX time in milliseconds when claim was submitted.
  /// Must fall within the tx validity range (prevents backdating/future-dating).
  submitted_at: Int,
  /// Challenge window in POSIX milliseconds after submission during which
  /// challenges are accepted. Deadline = submitted_at + challenge_window.
  /// Must be within [params.min_challenge_window, params.max_challenge_window].
  /// v4: MUST be in ms (v3 bug used seconds → 1.8s instead of 30min).
  challenge_window: Int,
  /// Current state of this claim
  state: ClaimState,
}

/// Lifecycle state of a claim UTXO.
pub type ClaimState {
  /// Open for challenges — within challenge_window
  Open
  /// A challenge has been filed — locked until resolution
  Challenged
  /// Claim validated (no challenge, or won challenge)
  Validated
  /// Claim invalidated (lost challenge)
  Invalidated
}

/// Redeemer for claim validator (spend + mint).
pub type ClaimAction {
  /// Submit a new claim (mint handler: creates claim token + UTXO)
  SubmitClaim
  /// Withdraw a validated claim after challenge window expires (no challenge)
  WithdrawClaim
  /// Update claim state to Challenged (called atomically within OpenChallenge tx)
  MarkChallenged
  /// Finalize claim after oracle/jury resolution — burn token, distribute stake
  ForfeitClaim
}

// ══════════════════════════════════════════════════════════════════════
// CHALLENGE TYPES
// ══════════════════════════════════════════════════════════════════════

/// A challenge to an existing claim.
/// One UTXO per challenge — references the claim UTXO being contested.
///
/// Design note (VRF research 2026-03-23):
///   `eligible_jurors` is a snapshot of juror DIDs taken at OpenChallenge time.
///   This freezes the effective jury pool — prevents pool manipulation between
///   OpenChallenge and SelectJury. The SelectJury tx must use this snapshot,
///   not the live pool state.
pub type ChallengeDatum {
  /// Reference to the claim UTXO being challenged (cross-validator link)
  claim_ref: OutputReference,
  /// DID of the challenging agent
  auditor_did: PolicyId,
  /// Payment credential of auditor (for stake return or reward)
  auditor_credential: Credential,
  /// AP3X staked by auditor (must be >= claim stake)
  stake_amount: Int,
  /// blake2b_256 hash of auditor's counter-evidence (exactly 32 bytes)
  evidence_hash: ByteArray,
  /// Off-chain storage URI for counter-evidence (not validated on-chain)
  evidence_uri: ByteArray,
  /// POSIX time in milliseconds when challenge was opened.
  /// Must fall within the tx validity range (prevents backdating/future-dating).
  challenged_at: Int,
  /// POSIX milliseconds after challenged_at for jury to resolve.
  /// Deadline = challenged_at + resolution_deadline.
  /// v4: MUST be in ms (consistent with all time fields).
  resolution_deadline: Int,
  /// Snapshot of eligible juror DIDs at OpenChallenge time (sorted lexicographically).
  /// Freezes the pool — prevents registration/deregistration manipulation.
  /// Phase 1.0: may be empty list (oracle resolves without jury).
  /// Phase 1.1: must contain >= jury_size entries for SelectJury to proceed.
  eligible_jurors: List<PolicyId>,
  /// Current state of this challenge
  state: ChallengeState,
}

/// Lifecycle state of a challenge UTXO.
pub type ChallengeState {
  /// Awaiting Foundation oracle resolution (Phase 1.0)
  /// Foundation reviews evidence off-chain and submits OracleResolve tx.
  PendingOracle
  /// Awaiting jury selection (Phase 1.1+)
  /// Triggered when jury pool reaches MIN_JURY_POOL_SIZE threshold.
  PendingJury
  /// Jury selected, awaiting votes — holds the selected juror DIDs
  Voting { selected_jurors: List<PolicyId> }
  /// Resolved — verdict rendered, stakes distributed
  Resolved { verdict: Verdict }
}

/// Outcome of a challenge resolution.
pub type Verdict {
  /// Claimer was correct — auditor loses stake
  ClaimerWins
  /// Auditor was correct — claimer loses stake
  AuditorWins
  /// Inconclusive — both stakes returned minus jury fee
  Inconclusive
}

/// Redeemer for challenge validator (spend + mint).
pub type ChallengeAction {
  /// Open a new challenge against a claim (also updates claim state atomically)
  OpenChallenge
  /// Submit additional evidence before jury selection
  SubmitEvidence { new_evidence_hash: ByteArray, new_evidence_uri: ByteArray }
  /// Phase 1.0: Foundation oracle resolves challenge with a verdict.
  /// Requires oracle_credential signature. No jury fee — Foundation absorbs cost.
  OracleResolve {
    verdict: Verdict,
    /// blake2b_256 hash of Foundation's public reasoning (stored off-chain)
    reasoning_hash: ByteArray,
  }
  /// Phase 1.1+: Trigger jury resolution after all votes are revealed on-chain.
  /// Permissionless — reads revealed_verdict from juror reference inputs.
  ResolveJury
  /// Timeout: deadline passed without full jury resolution; both stakes returned.
  TimeoutResolve
  /// Phase 1.0+: Burn the challenge UTxO after resolution.
  /// Requires state == Resolved. Oracle must sign (Phase 1.0 simplicity).
  /// The challenge token is burned here (deferred from ResolveJury to allow
  /// DistributeRewards to reference the Resolved challenge UTxO).
  CleanupResolved
  /// Phase 1.1: Transition challenge from PendingJury → Voting after jury selection.
  /// Called atomically with jury_pool.ak SelectJury. Oracle must sign.
  /// selected_jurors must be a subset of eligible_jurors snapshot and match jury_size.
  TransitionToVoting { selected_jurors: List<PolicyId> }
}

// ══════════════════════════════════════════════════════════════════════
// JURY TYPES
// ══════════════════════════════════════════════════════════════════════

/// A juror registered in the jury pool.
/// One UTXO per juror — bond (AP3X) held in UTXO value.
/// `active_case`: when Some, juror cannot withdraw until case resolves.
pub type JurorDatum {
  /// DID of the juror agent
  juror_did: PolicyId,
  /// Payment credential for reward distribution
  juror_credential: Credential,
  /// AP3X staked as juror bond (slashed for non-participation)
  bond_amount: Int,
  /// Number of challenges this juror has resolved
  cases_resolved: Int,
  /// Number of times this juror voted with the majority
  majority_votes: Int,
  /// POSIX time in milliseconds when juror registered.
  /// v4: stored in ms (consistent with all time fields).
  registered_at: Int,
  /// Active case assignment: None = available, Some(token_name) = assigned to this challenge.
  /// Stores the challenge token name (ByteArray), which is immutable across the challenge
  /// lifecycle (derived at OpenChallenge mint, survives all spends including ResolveJury).
  /// fix: CR-002-F1 — changed from Option<OutputReference> to Option<ByteArray>
  /// because OutputReference mutates on every spend, breaking cross-validator lookups
  /// after ResolveJury consumes and re-outputs the challenge UTxO.
  active_case: Option<ByteArray>,
  /// Commit-reveal voting: blake2b_256 hash of (verdict_index_byte || salt).
  /// Set during CommitVote, cleared during RevealVote.
  /// None = no commitment yet (or already revealed).
  vote_commitment: Option<ByteArray>,
  /// Commit-reveal voting: revealed verdict after RevealVote.
  /// None = not yet revealed. Some(verdict) = juror has revealed their vote.
  revealed_verdict: Option<Verdict>,
}

/// A single juror's vote on a challenge.
pub type JuryVote {
  /// Which juror cast this vote
  juror_did: PolicyId,
  /// The verdict this juror selected
  verdict: Verdict,
  /// blake2b_256 hash of juror's reasoning (stored off-chain)
  reasoning_hash: ByteArray,
}

/// Redeemer for jury_pool validator (spend + mint).
pub type JuryAction {
  /// Register as a juror — stake bond, mint juror token
  RegisterJuror
  /// Phase 1.0: Foundation oracle selects jury.
  ///   - oracle_credential signs the tx
  ///   - selection_seed is cosmetic (used for ordering only; validator does NOT
  ///     trust it for security — oracle controls selection in Phase 1.0)
  ///
  /// Phase 1.1: replace oracle-signed selection with slot-based entropy
  /// once pool is large enough (see VRF research 2026-03-23).
  /// // Phase 1.1: replace with VRF-based selection once research complete
  SelectJury {
    /// OutputReference of the challenge UTXO this jury serves
    challenge_ref: OutputReference,
    /// Cosmetic ordering seed (Phase 1.0: oracle-provided, not security-critical)
    selection_seed: ByteArray,
    /// Pre-computed list of selected juror DIDs
    /// Phase 1.0: oracle picks; validator checks oracle sig only
    /// Phase 1.1: validator re-derives from slot entropy and verifies
    selected_jurors: List<PolicyId>,
  }
  /// Commit a vote hash on an active challenge (Phase 1.1 commit-reveal)
  /// Replaces CastVote — juror submits blake2b_256(verdict_index_byte || salt)
  CommitVote {
    challenge_ref: OutputReference,
    /// blake2b_256(verdict_index_byte || salt) — 32 bytes
    commitment_hash: ByteArray,
  }
  /// Reveal a previously committed vote (Phase 1.1 commit-reveal)
  RevealVote {
    challenge_ref: OutputReference,
    verdict: Verdict,
    salt: ByteArray,
  }
  /// Distribute rewards and update stats after challenge resolution
  DistributeRewards { challenge_ref: OutputReference }
  /// Withdraw juror registration (only if active_case == None)
  WithdrawJuror
  /// fix: NEW-FINAL-02 — accept jury fee UTXOs at jury_pool_hash.
  /// A ResolveJury tx routes the jury fee to jury_pool_hash with a FeeAccumulator datum.
  /// This action allows that UTXO to be consumed later (e.g., via DistributeRewards
  /// or a future FeeDistribute action in Phase 1.1).
  /// Requires oracle signature to prevent unauthorized drainage.
  ReceiveJuryFee
  /// Slash a juror who committed but did not reveal within the reveal window.
  /// Oracle-gated to prevent griefing. Bond reduced by slash_rate.
  SlashNonReveal {
    challenge_ref: OutputReference,
  }
}

// ══════════════════════════════════════════════════════════════════════
// AUDITING CONFIG (deployment parameters, baked into validators)
// ══════════════════════════════════════════════════════════════════════

/// Compile-time configuration — baked into all validators via blueprint apply.
/// Contains ONLY fields that do NOT create circular deployment dependencies.
/// The three cross-validator script hashes (which change after apply) are
/// intentionally absent — they live in CrossValidatorRefs (a runtime UTxO datum).
pub type AuditingConfig {
  /// Policy ID of the Agent Registry (for DID verification)
  registry_policy_id: PolicyId,
  /// Policy ID of the AP3X token (the staking currency)
  ap3x_policy_id: PolicyId,
  /// Asset name of the AP3X token
  ap3x_asset_name: ByteArray,
  /// Foundation oracle verification key hash (Phase 1.0 dispute resolution)
  oracle_vkh: ByteArray,
  /// Policy ID of the NFT that authenticates the CrossValidatorRefs UTxO.
  /// Any reference input carrying a token under this policy is the trusted
  /// refs UTxO — validators parse its inline datum as CrossValidatorRefs.
  ///
  /// v4 FIX (CRITICAL-02): This MUST be a dedicated NativeScript policy
  /// locked to oracle_vkh (ScriptPubkey(oracle_vkh)), NOT the AP3X policy.
  /// Using AP3X policy allowed attackers to create fake refs UTxOs with
  /// poisoned hashes (any UTxO holding AP3X tokens would pass the check).
  /// The NativeScript policy ensures only the oracle can mint the refs NFT.
  refs_token_policy: PolicyId,
}

/// Runtime cross-validator references — stored as an inline datum on a UTxO
/// that is authenticated by the refs_token_policy NFT.
/// Deployed AFTER the three validators are on-chain (their hashes are known).
/// Included as a reference input in every transaction that needs cross-refs.
pub type CrossValidatorRefs {
  /// Script hash of the claim validator (= claim token policy_id)
  claim_validator_hash: ByteArray,
  /// Script hash of the challenge validator (= challenge token policy_id)
  challenge_validator_hash: ByteArray,
  /// Script hash of the jury pool validator (= juror token policy_id)
  jury_pool_hash: ByteArray,
}
