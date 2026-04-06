// Game 6: Governance Suggestion Engine — On-Chain Types
// Spec reference: §4.1–4.7

use cardano/address.{Credential}
use cardano/transaction.{OutputReference}

// ============================================================================
// §4.1 Proposal Types
// ============================================================================

/// A governance proposal submitted by an agent
pub type ProposalDatum {
  /// DID of the proposing agent (policy_id of registry NFT)
  proposer_did: ByteArray,
  /// Payment credential of proposer (for stake return + rewards)
  proposer_credential: Credential,
  /// blake2b_256 hash of the full proposal document (stored off-chain)
  proposal_hash: ByteArray,
  /// Category tag for proposal routing
  proposal_type: ProposalType,
  /// Off-chain storage URI for the full proposal (OriginTrail UAL or IPFS CID)
  storage_uri: ByteArray,
  /// AP3X staked by proposer (skin in the game), in lovelace
  stake_amount: Int,
  /// Slot when proposal was submitted
  submitted_at: Int,
  /// Review window: slots after submission during which Foundation can act
  review_window: Int,
  /// Priority level (Standard or Emergency)
  priority: ProposalPriority,
  /// Number of amendments made (tracks revision history)
  amendment_count: Int,
  /// List of critique UTXOs incorporated in amendments (for reward sharing)
  incorporated_critiques: List<OutputReference>,
  /// Current state
  state: ProposalState,
}

/// Category of governance proposal
pub type ProposalType {
  /// Change a protocol parameter (e.g., MIN_CLAIM_STAKE, JURY_SIZE)
  ParameterChange { param_name: ByteArray, current_value: Int, proposed_value: Int }
  /// Allocate treasury funds for a specific purpose
  TreasurySpend { amount: Int, recipient_description: ByteArray }
  /// Propose a protocol upgrade or new feature
  ProtocolUpgrade { upgrade_description_hash: ByteArray }
  /// Propose activation or modification of a game
  GameActivation { game_id: Int }
  /// General governance suggestion (catch-all)
  GeneralSuggestion
}

/// Whether this proposal is standard or emergency priority.
/// Emergency proposals require higher stake and have shorter review windows.
pub type ProposalPriority {
  /// Standard proposal — normal review window and stake
  Standard
  /// Emergency proposal — EMERGENCY_REVIEW_WINDOW, EMERGENCY_STAKE_MULTIPLIER × stake
  /// Requires proposer reputation >= Established tier (100+ AP3X)
  Emergency
}

/// Lifecycle state of a proposal
pub type ProposalState {
  /// Open for critiques and endorsements
  Open
  /// Proposal has been amended (new version, still open)
  Amended { previous_hash: ByteArray }
  /// Foundation has adopted the proposal
  Adopted { adoption_reasoning_hash: ByteArray }
  /// Foundation has rejected the proposal
  Rejected { rejection_reasoning_hash: ByteArray }
  /// Review window expired without Foundation action
  Expired
  /// Proposer withdrew the proposal
  Withdrawn
}

/// Actions (redeemers) for the proposal validator
pub type ProposalAction {
  /// Submit a new governance proposal with stake (mint action)
  SubmitProposal
  /// Withdraw proposal before adoption/rejection (stake returned)
  WithdrawProposal
  /// Amend proposal incorporating critiques (proposer only)
  AmendProposal {
    new_hash: ByteArray,
    new_uri: ByteArray,
    incorporated: List<OutputReference>,
  }
  /// Foundation adopts the proposal (oracle action)
  AdoptProposal { reasoning_hash: ByteArray, reward_amount: Int }
  /// Foundation rejects the proposal (oracle action)
  RejectProposal { reasoning_hash: ByteArray }
  /// Expire proposal after review window (callable by anyone)
  ExpireProposal
  /// Expire a stale ParameterChange proposal whose current_value no longer matches
  /// on-chain ProtocolParams (callable by anyone, permissionless)
  ExpireStaleProposal
  /// Foundation signals "under review" — extends review window
  ExtendReview { additional_slots: Int }
  /// Allow activity UTXO to be consumed alongside a proposal finalization.
  /// Validation is done by the proposal UTXO's evaluation (via validate_finalize_activity).
  SpendActivity
}

// ============================================================================
// §4.2 Critique Types
// ============================================================================

/// A critique of an existing proposal
pub type CritiqueDatum {
  /// DID of the critiquing agent
  critic_did: ByteArray,
  /// Payment credential of critic (for potential reward sharing)
  critic_credential: Credential,
  /// Reference to the proposal being critiqued
  proposal_ref: OutputReference,
  /// blake2b_256 hash of the critique document (stored off-chain)
  critique_hash: ByteArray,
  /// Off-chain storage URI for critique
  storage_uri: ByteArray,
  /// Type of critique
  critique_type: CritiqueType,
  /// AP3X staked by critic (skin in the game), in lovelace
  stake_amount: Int,
  /// Slot when critique was submitted
  submitted_at: Int,
  /// Whether this critique was incorporated into a proposal amendment
  incorporated: Bool,
}

/// The nature of a critique
pub type CritiqueType {
  /// Critique supports the proposal and provides additional analysis
  Supportive
  /// Critique opposes the proposal with counter-arguments
  Opposing
  /// Critique suggests specific amendments/improvements
  Amendment { suggested_change_hash: ByteArray }
}

/// Actions (redeemers) for the critique validator
pub type CritiqueAction {
  /// Submit a new critique (mint)
  MintCritiqueToken
  /// Withdraw critique (only if not yet incorporated)
  WithdrawCritique
  /// Mark critique as incorporated (called during AmendProposal)
  IncorporateCritique
  /// Distribute reward share to critic (called during AdoptProposal)
  RewardCritique { reward_share: Int }
  /// Burn critique token on proposal finalization
  BurnCritiqueToken
}

// ============================================================================
// §4.3 Endorsement Types
// ============================================================================

/// An endorsement: Agent stakes AP3X signaling support for a proposal.
/// Endorsements are weighted signals to the Foundation — they don't determine outcomes.
pub type GovernanceEndorsementDatum {
  /// DID of the endorsing agent
  endorser_did: ByteArray,
  /// Payment credential of endorser
  endorser_credential: Credential,
  /// Reference to the proposal being endorsed
  proposal_ref: OutputReference,
  /// AP3X staked as endorsement signal, in lovelace
  stake_amount: Int,
  /// Slot when endorsement was created
  created_at: Int,
}

/// Actions for endorsements
pub type GovernanceEndorsementAction {
  /// Create endorsement (mint)
  MintEndorsementToken
  /// Withdraw endorsement (anytime — no lock)
  WithdrawEndorsement
  /// Burn endorsement token on proposal finalization
  BurnEndorsementToken
}

// ============================================================================
// §4.4 Foundation Oracle Types
// ============================================================================

/// Foundation governance oracle — trusted resolver for proposal adoption
pub type GovernanceOracleDatum {
  /// Foundation's payment credential (multi-sig recommended)
  oracle_credential: Credential,
  /// Governance treasury script hash
  treasury_script_hash: ByteArray,
  /// Whether governance oracle is active
  oracle_active: Bool,
}

/// Oracle actions
pub type OracleGovernanceAction {
  /// Foundation adopts a proposal
  Adopt {
    proposal_ref: OutputReference,
    reward_amount: Int,
    reasoning_hash: ByteArray,
  }
  /// Foundation rejects a proposal
  Reject { proposal_ref: OutputReference, reasoning_hash: ByteArray }
}

// ============================================================================
// §4.5 Governance Parameters
// ============================================================================

/// Governance parameters stored in a dedicated UTXO.
/// Validators read via reference input.
pub type GovernanceParams {
  /// Minimum AP3X to stake on a proposal (lovelace)
  min_proposal_stake: Int,
  /// Minimum AP3X to stake on a critique (lovelace)
  min_critique_stake: Int,
  /// Minimum AP3X endorsement signal (lovelace)
  min_governance_endorsement: Int,
  /// Minimum review window in slots
  min_review_window: Int,
  /// Maximum review window in slots
  max_review_window: Int,
  /// Maximum amendments per proposal
  max_amendments: Int,
  /// Maximum active proposals per agent
  max_active_proposals: Int,
  /// Cooldown between proposals in slots
  proposal_cooldown: Int,
  /// Proposer share of adoption reward (basis points, e.g. 7000 = 70%)
  proposer_reward_share: Int,
  /// Critic share of adoption reward (basis points, e.g. 2000 = 20%)
  critic_reward_share: Int,
  /// Protocol fee rate (basis points, e.g. 1000 = 10%)
  protocol_fee_rate: Int,
  /// Minimum adoption reward (lovelace)
  min_adoption_reward: Int,
  /// Maximum adoption reward (lovelace)
  max_adoption_reward: Int,
  /// Maximum incorporated critiques per proposal
  max_incorporated_critiques: Int,
  /// Maximum treasury spend per proposal (lovelace)
  max_treasury_request: Int,
  /// Emergency proposal stake multiplier (basis points, e.g. 5000 = 5x)
  emergency_stake_multiplier: Int,
  /// Emergency review window in slots
  emergency_review_window: Int,
  /// Timelock delay for auto-executing adopted parameter changes (Phase 1.2)
  param_execution_delay: Int,
  /// Minimum total prediction pool for pari-mutuel resolution (Phase 1.2)
  min_prediction_pool: Int,
  /// Credibility Pool low threshold (Phase 1.2)
  credibility_pool_low_threshold: Int,
  /// Credibility Pool critical threshold (Phase 1.2)
  credibility_pool_critical_threshold: Int,
}

// ============================================================================
// §11.2 Cross-Validator Configuration
// ============================================================================

/// Cross-validator configuration — baked into all validators via blueprint apply.
/// Contains ONLY fields that do NOT create circular deployment dependencies.
/// proposal_validator_hash and critique_validator_hash live in GovernanceCrossRefs
/// (a runtime datum) to avoid the circular dependency with `aiken blueprint apply`.
pub type GovernanceConfig {
  /// Policy ID of the NFT that authenticates the GovernanceCrossRefs UTxO.
  /// Must be a NativeScript policy locked to oracle_credential (ScriptPubkey).
  refs_token_policy: ByteArray,
  /// Script hash of the prediction validator (Phase 1.2, zeroed until deployed)
  prediction_validator_hash: ByteArray,
  /// Policy ID of the Agent Registry
  registry_policy_id: ByteArray,
  /// Script hash of the Agent Registry
  registry_script_hash: ByteArray,
  /// Script hash of Game 3 reputation validator (zeroed if not available)
  reputation_validator_hash: ByteArray,
  /// Script hash of Game 1 jury pool (zeroed if not available)
  jury_pool_hash: ByteArray,
  /// Script hash of the governance oracle
  governance_oracle_hash: ByteArray,
  /// Script hash of governance parameters holder
  governance_params_hash: ByteArray,
  /// Script hash of governance treasury
  governance_treasury_hash: ByteArray,
  /// Script hash of governance credibility pool (Phase 1.2, zeroed until deployed)
  credibility_pool_hash: ByteArray,
  /// Script hash of protocol params (for parameter proposals + stale detection)
  protocol_params_hash: ByteArray,
}

/// Runtime cross-validator references — stored as inline datum on a UTxO
/// authenticated by refs_token_policy. Deployed AFTER validators are on-chain.
pub type GovernanceCrossRefs {
  /// Script hash of the proposal spend validator
  proposal_validator_hash: ByteArray,
  /// Script hash of the critique spend validator
  critique_validator_hash: ByteArray,
  /// Policy ID (script hash) of the proposal mint policy (for token minting/burning)
  proposal_mint_hash: ByteArray,
  /// Policy ID (script hash) of the critique mint policy (for token minting/burning)
  critique_mint_hash: ByteArray,
}

// ============================================================================
// §4.6 Proposer Activity Tracking
// ============================================================================

/// Tracks a proposer's activity for rate limiting and cooldown enforcement.
/// One ActivityUTXO per agent, consumed and re-created on every proposal lifecycle event.
pub type ProposerActivityDatum {
  /// DID of the agent
  agent_did: ByteArray,
  /// Payment credential of the agent
  agent_credential: Credential,
  /// Number of currently active (Open/Amended) proposals
  active_proposal_count: Int,
  /// Slot of most recent proposal submission
  last_proposal_slot: Int,
}

/// Activity tracking actions
pub type ProposerActivityAction {
  /// Create activity tracker (first proposal ever)
  InitActivity
  /// Increment active count (new proposal submitted)
  IncrementActivity
  /// Decrement active count (proposal finalized/withdrawn)
  DecrementActivity
}

// ============================================================================
// §9.4 Treasury Batch
// ============================================================================

/// Treasury batch UTXO for parallel adoption reward distribution.
/// Multiple batch UTXOs avoid contention when Foundation adopts multiple proposals.
pub type TreasuryBatchDatum {
  /// Batch identifier (sequential, for tracking)
  batch_id: Int,
  /// Whether this batch is active (can be consumed for rewards)
  active: Bool,
}

// ============================================================================
// Phase 1.2: Prediction Market Types (§7.3)
// ============================================================================

/// A prediction stake on a governance proposal outcome
pub type PredictionDatum {
  /// DID of the predicting agent
  predictor_did: ByteArray,
  /// Payment credential for payout
  predictor_credential: Credential,
  /// Reference to the proposal being predicted
  proposal_ref: OutputReference,
  /// Predicted outcome
  prediction: PredictionOutcome,
  /// AP3X staked on this prediction (lovelace)
  stake_amount: Int,
  /// Slot when prediction was placed
  placed_at: Int,
}

/// Predicted outcome for a proposal
pub type PredictionOutcome {
  /// Predict the proposal will be adopted
  WillAdopt
  /// Predict the proposal will be rejected
  WillReject
  /// Predict the proposal will expire (no Foundation action)
  WillExpire
}

/// Prediction market actions
pub type PredictionAction {
  /// Place a new prediction (mint)
  MintPredictionToken
  /// Withdraw prediction (only before PREDICTION_LOCK)
  WithdrawPrediction
  /// Claim payout after proposal resolution
  ClaimPredictionPayout
}

// ============================================================================
// Phase 1.2: Timelock Types (§18.7)
// ============================================================================

/// Timelock UTXO created by AdoptProposal for ParameterChange proposals.
/// Holds the adopted parameter change and executes after PARAM_EXECUTION_DELAY.
pub type TimelockDatum {
  /// Reference to the adopted proposal that created this timelock
  proposal_ref: OutputReference,
  /// Parameter to change
  param_name: ByteArray,
  /// New value to set
  new_value: Int,
  /// Slot when timelock was created (= adoption slot)
  created_at: Int,
  /// Delay in slots before execution is permitted
  execution_delay: Int,
  /// Whether the timelock has been cancelled by Foundation veto
  cancelled: Bool,
}

/// Timelock actions
pub type TimelockAction {
  /// Execute the parameter change (permissionless, after delay expires)
  ExecuteTimelockAction
  /// Foundation cancels the timelock (veto — requires oracle signature)
  CancelTimelockExecution { veto_reasoning_hash: ByteArray }
}
