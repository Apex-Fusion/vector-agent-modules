// Module 6: Self-Improvement Module — On-Chain Types
// Spec reference: §4.1–4.7

use cardano/address.{Credential}
use cardano/transaction.{OutputReference}

// ============================================================================
// §4.1 Proposal Types
// ============================================================================

/// A governance proposal submitted by an agent
pub type ProposalDatum {
  /// Identifier of the proposing agent: the identifier of its identity record
  /// (did:vector, 32 bytes; config.identity_hash)
  proposer_did: ByteArray,
  /// Payment credential of proposer (for stake return + rewards). Submission
  /// and amendment need a declared credential of the proposer's record: a
  /// verification key whose hash is the hash of the controller or of a listed
  /// key with the transaction purpose (utils.verify_active_agent)
  proposer_credential: Credential,
  /// blake2b_256 hash of the full proposal document (stored off-chain)
  proposal_hash: ByteArray,
  /// Category tag for proposal routing
  proposal_type: ProposalType,
  /// Off-chain storage URI for the full proposal (OriginTrail UAL or IPFS CID)
  storage_uri: ByteArray,
  /// AP3X staked by proposer (skin in the game), in lovelace
  stake_amount: Int,
  /// Time of the submission (POSIX ms): the lower bound of the validity range
  /// of the submission transaction, which is at most one hour long
  submitted_at: Int,
  /// Review window: slots after submission during which Foundation can act
  review_window: Int,
  /// Priority level (Standard or Emergency)
  priority: ProposalPriority,
  /// Number of amendments made (tracks revision history)
  amendment_count: Int,
  /// Output references of the critiques that amendments incorporated, in the
  /// order of the amendments (for reward sharing). Each critique names this
  /// proposal (its proposal_ref is the seed of this proposal)
  incorporated_critiques: List<OutputReference>,
  /// Current state
  state: ProposalState,
}

/// Category of governance proposal
pub type ProposalType {
  /// Change a protocol parameter (e.g., MIN_CLAIM_STAKE, JURY_SIZE)
  ParameterChange {
    param_name: ByteArray,
    current_value: Int,
    proposed_value: Int,
  }
  /// Allocate treasury funds for a specific purpose
  TreasurySpend { amount: Int, recipient_description: ByteArray }
  /// Propose a protocol upgrade or new feature
  ProtocolUpgrade { upgrade_description_hash: ByteArray }
  /// Propose activation or modification of a module
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
  /// Expire a stale ParameterChange proposal of a Module-6 parameter: the
  /// GovernanceParams record holds a value for param_name that differs from
  /// current_value (callable by anyone, permissionless;
  /// proposal_validation.validate_expire_stale_proposal)
  ExpireStaleProposal
  /// Foundation signals "under review" — extends review window
  ExtendReview { additional_slots: Int }
  /// Spend an entry of the activity list: the node of an agent or the head
  /// (§4.6). Only together with the one proposal action of the transaction:
  /// SubmitProposal (any entry; the submission checks the continuing entry),
  /// or a finalisation of a proposal of the agent of the node (never the
  /// head). That action checks the continuing output
  /// (proposal_validation.validate_spend_activity).
  SpendActivity
}

// ============================================================================
// §4.2 Critique Types
// ============================================================================

/// A critique of an existing proposal
pub type CritiqueDatum {
  /// Identifier of the critiquing agent: the identifier of its identity record
  /// (did:vector, 32 bytes; config.identity_hash)
  critic_did: ByteArray,
  /// Payment credential of critic (for potential reward sharing). Submission
  /// needs a declared credential of the critic's record
  /// (utils.verify_active_agent)
  critic_credential: Credential,
  /// The proposal that is critiqued: the seed of the proposal, the output
  /// reference that its SubmitProposal spent. The proposal holds the token
  /// utils.proposal_token_name(proposal_ref)
  proposal_ref: OutputReference,
  /// blake2b_256 hash of the critique document (stored off-chain)
  critique_hash: ByteArray,
  /// Off-chain storage URI for critique
  storage_uri: ByteArray,
  /// Type of critique
  critique_type: CritiqueType,
  /// AP3X staked by critic (skin in the game), in lovelace
  stake_amount: Int,
  /// Time of the submission (POSIX ms): the lower bound of the validity range
  /// of the submission transaction, which is at most one hour long
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
  /// Identifier of the endorsing agent: the identifier of its identity record
  /// (did:vector, 32 bytes; config.identity_hash)
  endorser_did: ByteArray,
  /// Payment credential of endorser. Submission needs a declared credential
  /// of the endorser's record (utils.verify_active_agent)
  endorser_credential: Credential,
  /// The proposal that is endorsed: the seed of the proposal (see
  /// CritiqueDatum.proposal_ref)
  proposal_ref: OutputReference,
  /// AP3X staked as endorsement signal, in lovelace
  stake_amount: Int,
  /// Time of the endorsement (POSIX ms): the lower bound of the validity
  /// range of the endorsement transaction, which is at most one hour long
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
  /// Script hash of Module 3 reputation validator (zeroed if not available)
  reputation_validator_hash: ByteArray,
  /// Script hash of Module 1 jury pool (zeroed if not available)
  jury_pool_hash: ByteArray,
  /// Script hash of the governance oracle holder
  governance_oracle_hash: ByteArray,
  /// Script hash of governance parameters holder
  governance_params_hash: ByteArray,
  /// Script hash of governance treasury
  governance_treasury_hash: ByteArray,
  /// Script hash of governance credibility pool (Phase 1.2, zeroed until deployed)
  credibility_pool_hash: ByteArray,
  /// Script hash of a protocol params holder. No validator reads it:
  /// ExpireStaleProposal reads the GovernanceParams record. The field stays,
  /// so the GovernanceConfig layout (11 fields) does not change.
  protocol_params_hash: ByteArray,
  /// Policy ID of the one-shot record token policy
  /// (shared/holder-scripts/validators/record_token.ak). The GovernanceParams
  /// record holds its `params` token, the oracle record holds its `oracle`
  /// token and the head of the activity list holds its `activity` token
  /// (utils.activity_head_token). Records without the token are ignored.
  record_token_policy: ByteArray,
  /// Script hash (= policy ID) of the identity registry contract `identity`
  /// (did:vector, Part 2 §2 of docs/IDENTITY_REGISTRY.md in agents-marketplace).
  /// An agent's record is the reference input at the exact identity address
  /// (Script(identity_hash), no stake credential) that holds exactly one token
  /// of this policy, named with the agent's identifier (vector_identity). The
  /// hash is known after the identity deployment, which comes before the
  /// Module-6 deployment (Part 2 §7).
  identity_hash: ByteArray,
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
  /// Script hash of the endorsement spend validator. The critique mint policy
  /// uses it to find the endorsement inputs of a transaction
  endorsement_validator_hash: ByteArray,
}

// ============================================================================
// §4.6 Proposer Activity Tracking
// ============================================================================

/// Tracks a proposer's activity for rate limiting and cooldown enforcement.
///
/// The activity records form one list at the proposal validator, sorted by
/// agent_did in ascending byte order (builtin.less_than_bytearray):
///   - The head: the UTxO that holds the record token
///     (config.record_token_policy, utils.activity_head_token) and no token
///     of the proposal policy. Its datum has agent_did = #"",
///     active_proposal_count = 0 and last_proposal_slot = 0. The deploy
///     transaction of the records creates it.
///   - A node: the UTxO of one agent. It holds exactly one token of the
///     proposal policy: the activity token
///     utils.activity_token_name(agent_did), quantity 1.
/// next_did links each entry to the next node. The first submission of an
/// agent inserts its node after its predecessor
/// (activity_tracking.validate_submit_activity), so each agent has at most
/// one node.
pub type ProposerActivityDatum {
  /// DID of the agent (#"" for the head)
  agent_did: ByteArray,
  /// Payment credential of the agent
  agent_credential: Credential,
  /// Number of currently active (Open/Amended) proposals
  active_proposal_count: Int,
  /// Time of the most recent proposal submission (POSIX ms): the lower bound
  /// of the validity range of that submission
  last_proposal_slot: Int,
  /// agent_did of the next node of the list, or #"" for the last entry
  next_did: ByteArray,
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
