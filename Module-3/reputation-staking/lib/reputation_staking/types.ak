/// On-chain types for Module 3: Reputation Staking.
///
/// Defines all datum and redeemer types for the reputation and endorsement
/// multi-validators. See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 4.1–4.5.

use cardano/address.{Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference}

// ──────────────────────────────────────────────────────────────────────────────
// 4.1  Self-Stake Types
// ──────────────────────────────────────────────────────────────────────────────

/// An agent's self-stake backing their claimed capabilities.
/// One StakeUTXO per agent. Updated via IncreaseStake/DecreaseStake.
pub type StakeDatum {
  /// DID of the staking agent (NFT asset name from Agent Registry)
  agent_did: ByteArray,
  /// Payment credential of the agent owner
  owner_credential: Credential,
  /// Total AP3X self-staked (in DFM — smallest unit)
  stake_amount: Int,
  /// Capabilities this stake backs (must match registry capabilities)
  staked_capabilities: List<ByteArray>,
  /// Slot when stake was created or last modified
  last_updated: Int,
  /// Accumulated history bonus points (from winning challenges, cross-module bonuses)
  history_points: Int,
}

/// Redeemer for the reputation validator spend handler.
pub type StakeAction {
  /// Create initial self-stake (first time staking)
  CreateStake
  /// Add more AP3X to existing stake
  IncreaseStake
  /// Remove AP3X from stake (subject to cooldown and minimum)
  DecreaseStake { amount: Int }
  /// Update which capabilities are backed by the stake
  UpdateCapabilities { new_capabilities: List<ByteArray> }
  /// Process decay on an inactive agent's stake (callable by anyone)
  ClaimDecayRefund
}

/// Redeemer for the reputation validator mint handler.
pub type StakeMintAction {
  /// Mint a stake tracking token when an agent first stakes
  MintStakeToken
  /// Burn a stake tracking token when stake drops below minimum
  BurnStakeToken
  /// Mint a history bonus token for a qualifying cross-module event
  MintHistoryBonus
  /// Burn an expired genesis bonus token (permissionless cleanup)
  BurnGenesisBonus
}

// ──────────────────────────────────────────────────────────────────────────────
// 4.2  Endorsement Types
// ──────────────────────────────────────────────────────────────────────────────

/// An endorsement: Agent B stakes AP3X vouching for Agent A's capabilities.
/// One EndorsementUTXO per endorser-endorsee pair.
pub type EndorsementDatum {
  /// DID of the endorsing agent
  endorser_did: ByteArray,
  /// Payment credential of endorser
  endorser_credential: Credential,
  /// DID of the agent being endorsed
  target_did: ByteArray,
  /// AP3X staked as endorsement
  stake_amount: Int,
  /// Which specific capabilities are endorsed (subset of target's capabilities)
  endorsed_capabilities: List<ByteArray>,
  /// Slot when endorsement was created
  created_at: Int,
}

/// Redeemer for endorsement spend actions.
pub type EndorsementAction {
  /// Increase existing endorsement amount
  IncreaseEndorsement
  /// Withdraw endorsement (subject to cooldown)
  WithdrawEndorsement
  /// Slash endorsement (triggered by successful challenge against target)
  SlashEndorsement { challenge_ref: OutputReference }
}

/// Redeemer for the endorsement validator mint handler.
pub type EndorsementMintAction {
  /// Mint a new endorsement token
  MintEndorsementToken
  /// Burn an endorsement token on withdrawal
  BurnEndorsementToken
  /// Mint a new challenge token
  MintChallengeToken
  /// Burn a challenge token on resolution
  BurnChallengeToken
}

// ──────────────────────────────────────────────────────────────────────────────
// 4.3  Challenge Types
// ──────────────────────────────────────────────────────────────────────────────

/// A challenge against a specific capability claim.
/// Agent C stakes AP3X claiming Agent A cannot perform capability X.
pub type ReputationChallengeDatum {
  /// DID of the challenger
  challenger_did: ByteArray,
  /// Payment credential of challenger
  challenger_credential: Credential,
  /// DID of the agent being challenged
  target_did: ByteArray,
  /// Payment credential of target (needed for slashing/reward distribution)
  target_credential: Credential,
  /// The specific capability being challenged
  challenged_capability: ByteArray,
  /// AP3X staked by challenger
  stake_amount: Int,
  /// Evidence hash (blake2b_256 of off-chain evidence)
  evidence_hash: ByteArray,
  /// Off-chain storage URI for evidence
  evidence_uri: ByteArray,
  /// Slot when challenge was created
  created_at: Int,
  /// Counter-evidence hash (empty ByteArray initially)
  counter_evidence_hash: ByteArray,
  /// Off-chain storage URI for counter-evidence
  counter_evidence_uri: ByteArray,
  /// Slot when target responded (0 if no response yet)
  response_submitted_at: Int,
  /// Current state
  state: RepChallengeState,
}

pub type RepChallengeState {
  /// Awaiting resolution
  Open
  /// Escalated to Module 1 Adversarial Auditing
  Escalated { audit_claim_ref: OutputReference }
  /// Resolved — capability verified or falsified
  Resolved { outcome: RepChallengeOutcome }
}

pub type RepChallengeOutcome {
  /// Agent's capability claim is valid — challenger loses stake
  CapabilityVerified
  /// Agent's capability claim is false — agent + endorsers slashed
  CapabilityFalsified
  /// Inconclusive — stakes returned minus fees
  Inconclusive
}

/// Redeemer for challenge spend actions.
pub type RepChallengeAction {
  /// Withdraw challenge (only if state == Open and before response deadline)
  WithdrawChallenge
  /// Target agent responds with counter-evidence
  RespondToChallenge {
    counter_evidence_hash: ByteArray,
    counter_evidence_uri: ByteArray,
  }
  /// Escalate to Module 1 for full adversarial resolution.
  /// Creates a Module 1 claim and transitions challenge to Escalated state.
  EscalateToAudit
  /// Resolve an escalated challenge by reading the Module 1 verdict.
  /// Reads the resolved Module 1 challenge via reference input, maps verdict
  /// to Module 3 outcome: ClaimerWins→CapabilityVerified, AuditorWins→CapabilityFalsified.
  ResolveEscalation
  /// Resolve directly (Foundation oracle in Phase 1.0)
  ResolveChallenge { outcome: RepChallengeOutcome }
  /// Auto-resolve after missed response deadline
  DefaultJudgment
  /// Distribute rewards/slashing after resolution
  DistributeOutcome
}

// ──────────────────────────────────────────────────────────────────────────────
// 4.4  History Bonus Types
// ──────────────────────────────────────────────────────────────────────────────

/// A history bonus UTXO — tracks permanent reputation earned from cross-module
/// events. Phase 1.0: tracked as separate UTXOs to avoid StakeDatum contention.
/// The indexer aggregates all HistoryBonusUTXOs for an agent when computing score.
pub type HistoryBonusDatum {
  /// DID of the agent receiving the bonus
  agent_did: ByteArray,
  /// Source of the bonus (which module/event generated it)
  source: HistoryBonusSource,
  /// Amount of bonus points (in AP3X-equivalent reputation units)
  bonus_points: Int,
  /// Reference to the originating event (for auditability)
  source_ref: OutputReference,
  /// Slot when bonus was created
  created_at: Int,
}

pub type HistoryBonusSource {
  /// Won a capability challenge in Module 3
  ChallengeWon
  /// Won an audit challenge as claimer in Module 1
  AuditClaimWon
  /// Served as juror and voted with majority in Module 1
  JurorDuty
  /// Governance proposal adopted in Module 6
  ProposalAdopted
  /// Governance critique incorporated in Module 6
  CritiqueIncorporated
  /// Escrow task completed successfully in Module 12
  EscrowCompleted
  /// Useful work verified in Module 9
  UsefulWorkVerified
  /// Validated security report in Module 11
  SecurityReportValidated
  /// Genesis agent bonus (one-time, at system launch)
  GenesisBonus
}

// ──────────────────────────────────────────────────────────────────────────────
// 4.5  Reputation Tiers (computed, not stored — for on-chain tier-gating)
// ──────────────────────────────────────────────────────────────────────────────

pub type ReputationTier {
  Unverified
  Novice
  Established
  Trusted
  Elite
}

// ──────────────────────────────────────────────────────────────────────────────
// Token name prefixes (Section 4.1b)
// ──────────────────────────────────────────────────────────────────────────────

pub const stake_token_prefix: ByteArray = "rstk_"

pub const endorsement_token_prefix: ByteArray = "rend_"

pub const challenge_token_prefix: ByteArray = "rchl_"

pub const history_bonus_token_prefix: ByteArray = "hbonus_"

pub const genesis_bonus_token_prefix: ByteArray = "genesis_"

// ──────────────────────────────────────────────────────────────────────────────
// Endorsement Validator Wrapper Types
// ──────────────────────────────────────────────────────────────────────────────

/// The endorsement validator can hold either endorsement or challenge datums.
/// Defined here (not in validators/) so library modules can import it.
pub type EndorsementValidatorDatum {
  Endorsement(EndorsementDatum)
  Challenge(ReputationChallengeDatum)
}

/// Redeemer for the endorsement validator spend handler.
pub type EndorsementValidatorAction {
  EndorsementSpend(EndorsementAction)
  ChallengeSpend(RepChallengeAction)
}

// ──────────────────────────────────────────────────────────────────────────────
// Module 1 Mirror Types (for cross-contract reads via reference inputs)
// ──────────────────────────────────────────────────────────────────────────────

/// Mirror of Module 1 ClaimDatum — used to verify escalation claim existence.
/// Only the fields needed for Module 3 cross-reads are included.
pub type AuditClaimDatum {
  /// DID of the claiming agent (policy_id of their registry NFT)
  claimer_did: PolicyId,
  /// Payment credential of claimer
  claimer_credential: Credential,
  /// blake2b_256 hash of the off-chain claim data
  claim_hash: ByteArray,
  /// Category tag for claim routing
  claim_type: ByteArray,
  /// Off-chain storage URI
  storage_uri: ByteArray,
  /// AP3X staked by claimer
  stake_amount: Int,
  /// POSIX time in milliseconds when claim was submitted
  submitted_at: Int,
  /// Challenge window in POSIX milliseconds
  challenge_window: Int,
  /// Current state
  state: AuditClaimState,
}

/// Mirror of Module 1 ClaimState.
pub type AuditClaimState {
  /// Open for challenges
  AuditOpen
  /// A challenge has been filed
  AuditChallenged
  /// Claim validated
  AuditValidated
  /// Claim invalidated
  AuditInvalidated
}

/// Mirror of Module 1 ChallengeDatum — used to read verdict after resolution.
pub type AuditChallengeDatum {
  /// Reference to the claim UTXO being challenged
  claim_ref: OutputReference,
  /// DID of the challenging agent
  auditor_did: PolicyId,
  /// Payment credential of auditor
  auditor_credential: Credential,
  /// AP3X staked by auditor
  stake_amount: Int,
  /// blake2b_256 hash of auditor's counter-evidence
  evidence_hash: ByteArray,
  /// Off-chain storage URI for counter-evidence
  evidence_uri: ByteArray,
  /// POSIX time in milliseconds when challenge was opened
  challenged_at: Int,
  /// POSIX milliseconds after challenged_at for resolution
  resolution_deadline: Int,
  /// Snapshot of eligible juror DIDs at OpenChallenge time
  eligible_jurors: List<PolicyId>,
  /// Current state
  state: AuditChallengeState,
}

/// Mirror of Module 1 ChallengeState.
pub type AuditChallengeState {
  /// Awaiting Foundation oracle resolution
  PendingOracle
  /// Awaiting jury selection
  PendingJury
  /// Jury selected, awaiting votes
  AuditVoting { selected_jurors: List<PolicyId> }
  /// Resolved — verdict rendered
  AuditResolved { verdict: AuditVerdict }
}

/// Mirror of Module 1 Verdict.
pub type AuditVerdict {
  /// Claimer was correct — auditor loses stake
  ClaimerWins
  /// Auditor was correct — claimer loses stake
  AuditorWins
  /// Inconclusive — both stakes returned minus jury fee
  AuditInconclusive
}

/// Mirror of Module 1 JurorDatum — used to verify jury duty for history bonuses.
pub type AuditJurorDatum {
  /// DID of the juror agent
  juror_did: PolicyId,
  /// Payment credential for reward distribution
  juror_credential: Credential,
  /// AP3X staked as juror bond
  bond_amount: Int,
  /// Number of challenges this juror has resolved
  cases_resolved: Int,
  /// Number of times this juror voted with the majority
  majority_votes: Int,
  /// POSIX time in milliseconds when juror registered
  registered_at: Int,
  /// Active case assignment
  active_case: Option<ByteArray>,
  /// Commit-reveal voting: commitment hash
  vote_commitment: Option<ByteArray>,
  /// Commit-reveal voting: revealed verdict
  revealed_verdict: Option<AuditVerdict>,
}
