/// On-chain types for Module 3: Reputation Staking.
///
/// Defines all datum and redeemer types for the reputation and endorsement
/// multi-validators. See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 4.1–4.5.

use cardano/address.{Address, Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference}

// ──────────────────────────────────────────────────────────────────────────────
// 4.1  Self-Stake Types
// ──────────────────────────────────────────────────────────────────────────────

/// An agent's self-stake backing their claimed capabilities.
/// One StakeUTXO per agent. Updated via IncreaseStake/DecreaseStake.
///
/// Field order (deployment and off-chain encoders): agent_did,
/// owner_credential, stake_amount, staked_capabilities, last_updated,
/// history_points, challenge_locks.
pub type StakeDatum {
  /// Identity registry identifier of the staking agent (did:vector, the
  /// asset name of its record token)
  agent_did: ByteArray,
  /// Payment credential of the agent owner. At creation it is a declared
  /// credential of the agent's record (the controller key or a listed key
  /// with the transaction purpose).
  owner_credential: Credential,
  /// Total AP3X self-staked (in DFM — smallest unit)
  stake_amount: Int,
  /// Capabilities this stake backs (must match registry capabilities)
  staked_capabilities: List<ByteArray>,
  /// POSIX ms of the last change by the owner (CreateStake, IncreaseStake,
  /// DecreaseStake, UpdateCapabilities): the upper bound of the validity
  /// range of that transaction, which is at most one hour long. The
  /// cooldown compares it with the lower bound of a later transaction.
  last_updated: Int,
  /// Accumulated history bonus points (from winning challenges, cross-module bonuses)
  history_points: Int,
  /// The asset names of the challenge tokens of the open challenges against
  /// this stake. A challenge adds its token name when it opens
  /// (LockForChallenge) and removes it when it closes (ReleaseChallenge or
  /// SlashStake). While the list is not empty, the stake cannot decrease,
  /// change its capabilities or decay. It is empty at creation.
  challenge_locks: List<ByteArray>,
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
  /// Slash stake after a capability is falsified (permissionless — outcome
  /// is deterministic). It also removes the lock of that challenge.
  SlashStake { challenge_ref: OutputReference }
  /// Add the lock of a challenge that opens against this stake in the same
  /// transaction (the transaction mints the challenge token). Permissionless:
  /// the endorsement policy checks the challenge.
  LockForChallenge
  /// Remove the lock of a challenge that closes in the same transaction
  /// without the outcome CapabilityFalsified (the transaction burns the
  /// challenge token). Permissionless.
  ReleaseChallenge { challenge_ref: OutputReference }
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
///
/// Field order (deployment and off-chain encoders): endorser_did,
/// endorser_credential, target_did, stake_amount, endorsed_capabilities,
/// created_at, slashed_by.
pub type EndorsementDatum {
  /// DID of the endorsing agent
  endorser_did: ByteArray,
  /// Payment credential of endorser. At creation it is a declared credential
  /// of the endorser's record.
  endorser_credential: Credential,
  /// DID of the agent being endorsed
  target_did: ByteArray,
  /// AP3X staked as endorsement
  stake_amount: Int,
  /// Which specific capabilities are endorsed (subset of target's capabilities)
  endorsed_capabilities: List<ByteArray>,
  /// POSIX ms when the endorsement was created (lower bound of a validity
  /// range of at most one hour)
  created_at: Int,
  /// The asset names of the challenge tokens of the resolved challenges that
  /// slashed this endorsement. A challenge slashes an endorsement at most
  /// once. It is empty at creation.
  slashed_by: List<ByteArray>,
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
  /// Payment credential of challenger. At creation it is a declared
  /// credential of the challenger's record.
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
  /// POSIX ms when the challenge was created (lower bound of a validity range
  /// of at most one hour)
  created_at: Int,
  /// Counter-evidence hash (empty ByteArray initially)
  counter_evidence_hash: ByteArray,
  /// Off-chain storage URI for counter-evidence
  counter_evidence_uri: ByteArray,
  /// POSIX ms when the target responded (0 if no response yet)
  response_submitted_at: Int,
  /// Current state
  state: RepChallengeState,
}

pub type RepChallengeState {
  /// Awaiting resolution
  Open
  /// Escalated to Module 1 Adversarial Auditing. `audit_claim_token` is the
  /// asset name of the Module 1 claim token that the escalation transaction
  /// minted (policy audit_claim_validator_hash). The output reference of
  /// that claim cannot be stored here: it contains the id of the escalation
  /// transaction, and a datum cannot hold the id of its own transaction.
  Escalated { audit_claim_token: ByteArray }
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
  /// Close a challenge that nobody resolved before the challenge timeout
  /// (see challenge_validation.challenge_timeout). The challenger receives
  /// the challenge stake; the target's stake keeps its AP3X (the transaction
  /// only removes the lock of the challenge).
  ExpireChallenge
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
  /// POSIX ms when the bonus was created
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
  /// Identity registry identifier of the claiming agent
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
/// The field order and the constructors follow the Module-1 deployment that
/// this deployment uses (Module-1 adds `claim_token` as the last field).
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
  /// Asset name of the claim token of the challenged claim
  claim_token: ByteArray,
}

/// Mirror of Module 1 ChallengeState. The field order of AuditResolved
/// follows Module 1 `Resolved { verdict, refund_to }`.
pub type AuditChallengeState {
  /// Awaiting Foundation oracle resolution
  PendingOracle
  /// Awaiting jury selection
  PendingJury
  /// Jury selected, awaiting votes
  AuditVoting { selected_jurors: List<PolicyId> }
  /// Resolved — verdict rendered. `refund_to` is the address that Module 1
  /// records for the refund; Module 3 does not read it.
  AuditResolved { verdict: AuditVerdict, refund_to: Address }
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
