/// Shared types for Game 1: Adversarial Auditing (Vector/eUTXO)
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

use cardano/address.{Address, Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference}

// ══════════════════════════════════════════════════════════════════════
// CLAIM TYPES
// ══════════════════════════════════════════════════════════════════════

/// A claim submitted by an agent asserting it performed some task.
/// One UTXO per claim — fully parallelizable across thousands of audits.
pub type ClaimDatum {
  /// Identity registry identifier of the claiming agent (did:vector, 32 bytes).
  /// SubmitClaim checks the record (Part 2 §7, Module-1 row).
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
  /// Identity registry identifier of the challenging agent (did:vector,
  /// 32 bytes). OpenChallenge checks the record (Part 2 §7, Module-1 row).
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
  /// Asset name of the claim token of the challenged claim. OpenChallenge
  /// checks that the consumed claim input holds this token, and every
  /// continuing challenge output keeps it. It binds the challenge to its
  /// claim, so a verdict names the agent of that claim.
  claim_token: ByteArray,
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
  /// Resolved — verdict rendered, stakes distributed. ResolveJury writes
  /// this state. The Resolved output keeps AP3X equal to the auditor stake;
  /// the ResolveJury transaction funds it, because the payouts of that
  /// transaction already use both stakes. `refund_to` is the address that
  /// gets this AP3X back when CleanupResolved removes the output. The
  /// ResolveJury transaction sets it to the address of its funder.
  Resolved { verdict: Verdict, refund_to: Address }
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
  /// Requires state == Resolved. Anybody can submit it after the cleanup
  /// time. The AP3X of the Resolved output goes to its `refund_to`; the
  /// transaction spends no other script input.
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
  /// Identity registry identifier of the juror agent (did:vector, 32 bytes).
  /// RegisterJuror checks the record (Part 2 §7, Module-1 row).
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
  /// Escape hatch: reset a juror's stale active_case when the referenced
  /// challenge token no longer exists on-chain (e.g., the challenge was
  /// burned via TimeoutResolve but the juror was never reached by
  /// DistributeRewards — which would require a Resolved challenge).
  /// Juror signs; validator verifies the active_case token is NOT held by
  /// any reference input at the challenge address (i.e., burned/absent).
  /// Continuing output: active_case=None, bond preserved, everything else
  /// unchanged. vote_commitment and revealed_verdict also reset to None.
  ResetStaleActiveCase
}

// ══════════════════════════════════════════════════════════════════════
// AUDITING CONFIG (deployment parameters, baked into validators)
// ══════════════════════════════════════════════════════════════════════

/// Compile-time configuration — baked into all validators via blueprint apply.
/// Contains ONLY fields that do NOT create circular deployment dependencies.
/// The three cross-validator script hashes (which change after apply) are
/// intentionally absent — they live in CrossValidatorRefs (a runtime UTxO datum).
/// The hash of `identity_party` is also absent: the challenge validator reads
/// it from the identity configuration record (Part 2 §3.8).
pub type AuditingConfig {
  /// Script hash (= policy ID) of the identity registry contract. Agent
  /// records are reference inputs at its exact address (vector_identity).
  identity_hash: ByteArray,
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
  /// Policy ID of the identity configuration token (`config_seal`).
  config_policy: PolicyId,
  /// Script hash of `config_holder`. The configuration record is the output
  /// at its exact address that holds the token `identity-config`.
  config_holder: ByteArray,
  /// Verdict policy, fixed at deployment (Part 2 §3.8). It applies only to
  /// oracle resolutions (OracleResolve). The values are an open item (Part 2
  /// §13.4).
  verdict_policy: VerdictPolicy,
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

// ══════════════════════════════════════════════════════════════════════
// IDENTITY PARTY TYPES (Part 2 §3.8)
// ══════════════════════════════════════════════════════════════════════

/// The action of a verdict.
pub type VerdictAction {
  /// A hold with reason 64 that lasts `duration` milliseconds.
  VerdictSuspend
  /// A revocation with reason 64, without "invalid from" time and successor.
  VerdictRevoke
}

/// The action and the duration that the verdict policy gives for one
/// oracle outcome.
pub type VerdictRule {
  action: VerdictAction,
  /// Milliseconds: 1 to 30 days for VerdictSuspend, 0 for VerdictRevoke.
  duration: Int,
}

/// Which oracle outcome (OracleResolve) gives which verdict. The agent of a
/// verdict is the side that loses the challenge. Inconclusive outcomes, jury
/// resolutions (ResolveJury) and timeouts give no verdict: the auditor names
/// the eligible jurors of a challenge, so a jury outcome is not safe for an
/// identity verdict.
pub type VerdictPolicy {
  /// The rule for AuditorWins. The verdict names the claimer.
  auditor_wins: Option<VerdictRule>,
  /// The rule for ClaimerWins. The verdict names the auditor.
  claimer_wins: Option<VerdictRule>,
}

/// The inline datum of a verdict output. The verdict token name is
/// blake2b_256 of the CBOR serialisation (serialiseData) of this value, with
/// all fields.
pub type IdentityVerdict {
  /// Identity registry identifier of the agent.
  agent: ByteArray,
  action: VerdictAction,
  /// Milliseconds; 0 for VerdictRevoke.
  duration: Int,
  /// The challenge UTxO that the resolution transaction spends.
  challenge: OutputReference,
  /// The address that gets the AP3X of the verdict output when the verdict
  /// expires without an application. The resolution transaction names it in
  /// the verdict output; its funder pays that AP3X.
  deposit_to: Address,
  /// POSIX milliseconds: the upper bound of the resolution transaction plus
  /// 30 days. A verdict application needs `hi <= expires_at`. After
  /// `expires_at` (`expires_at < lo`), anybody can spend the verdict output
  /// without an application: its token is burned and its AP3X goes to
  /// `deposit_to`.
  expires_at: Int,
}

/// Mint redeemer of `identity_party`.
pub type VerdictMint {
  /// Mint one verdict token in the resolution of one challenge.
  MintVerdict
  /// Burn one verdict token in a verdict application.
  BurnVerdict
}

/// Withdraw redeemer of `identity_party`: the two kinds of identity
/// transaction that Module-1 approves.
pub type PartyApproval {
  /// Module-1 becomes a new party in an identity Register, Adopt or Update.
  Consent
  /// One verdict becomes a Suspend or a Revoke by the Module-1 party.
  ApplyVerdict
}
