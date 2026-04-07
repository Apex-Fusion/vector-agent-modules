# Module 6: Governance Suggestion Engine — Implementation Specification

**Status**: DRAFT v0.4
**Author**: Lead Author, with AI-assisted design
**Date**: 2026-03-20
**Dependencies**: Agent Registry contract (deployed), Module 1: Adversarial Auditing (for dispute resolution), Module 3: Reputation Staking (for proposer credibility)
**Phase**: 1 (Traction — requires ~5 active agents)
**Target**: Vector eUTXO L2

---

## 1. Executive Summary

The Governance Suggestion Engine is an advisory governance module where agents analyze on-chain metrics, identify inefficiencies, and submit reasoned governance proposals to the Foundation Council. Agents that submit proposals which are adopted receive AP3X rewards. Agents can also critique, endorse, or improve each other's proposals.

This is **advisory governance, not direct governance**. The Foundation Council decides — agents suggest and reason, they don't vote. The module creates a competitive marketplace of ideas where selfish agents pursuing rewards produce better governance outcomes as a side effect.

**Core Insight**: The Foundation needs data-driven governance analysis. Agents need AP3X rewards. This module makes both happen simultaneously — agents earn by producing governance intelligence the Foundation would otherwise need to generate internally.

---

## 2. Design Principles

### 2.1 Advisory, Not Binding

**Critical design decision**: Proposals are suggestions, not on-chain votes.

The Foundation Council has final authority over all governance decisions:
- Parameter changes (MIN_CLAIM_STAKE, JURY_SIZE, etc.)
- Treasury spending
- Protocol upgrades
- New module activation

Agents compete to produce the *best analysis and recommendations*. The Foundation evaluates proposals off-chain and signals adoption on-chain via an oracle transaction. This avoids the complexity and security risks of on-chain governance while capturing the benefits of AI-powered policy analysis.

### 2.2 Proposal as Independent UTXO

Each proposal is an independent UTXO — fully parallelizable. No global "proposal registry" that creates contention. Critiques and endorsements are separate UTXOs referencing the proposal. This is the eUTXO-native approach: the governance discourse is a **graph of UTXOs**, not a monolithic state.

### 2.3 Quality Over Quantity

The mechanism design strongly favors quality proposals over spam:
- Proposal submission requires AP3X stake (skin in the game)
- Critiques that improve adopted proposals share the reward
- Spam proposals lock capital with zero return
- Reputation from Module 3 serves as a quality signal for the Foundation

### 2.4 Relationship to other modules

```
┌──────────────────────────────────────────────────────┐
│              GOVERNANCE SUGGESTION ENGINE              │
│                                                        │
│  Proposals → Foundation Council → Adoption Signal      │
│       ↑                                ↓               │
│  Chain Metrics                    AP3X Rewards          │
│  (from Modules 1,3,5,9,12)         (to proposers)        │
│                                                        │
│  Integration points:                                    │
│  - Module 3: Proposer reputation as quality signal        │
│  - Module 1: Dispute resolution for contested critiques   │
│  - Module 3 cross-module bonus: Adopted proposal → +10 AP3X│
│  - ProtocolParams UTXO: Adopted proposals UPDATE this   │
└──────────────────────────────────────────────────────┘
```

---

## 3. System Architecture

### 3.1 Contract Topology

Two Aiken multi-validators (Phase 1.0-1.1), expanding to three in Phase 1.2:

```
┌────────────────────────────────────────────────────────────────┐
│                  GOVERNANCE SUGGESTION ENGINE                    │
│                                                                  │
│  ┌───────────────────────────┐  ┌──────────────────────────┐   │
│  │ Proposal Validator         │  │ Critique Validator        │   │
│  │ (proposal.ak)              │  │ (critique.ak)             │   │
│  │                             │  │                            │   │
│  │ Mint:                       │  │ Mint:                      │   │
│  │  - MintProposalToken        │  │  - MintCritiqueToken       │   │
│  │  - BurnProposalToken        │  │  - MintEndorsementToken    │   │
│  │                             │  │  - BurnCritiqueToken       │   │
│  │ Spend:                      │  │  - BurnEndorsementToken    │   │
│  │  - WithdrawProposal         │  │                            │   │
│  │  - AmendProposal            │  │ Spend:                     │   │
│  │  - AdoptProposal            │  │  - WithdrawCritique        │   │
│  │  - RejectProposal           │  │  - WithdrawEndorsement     │   │
│  │  - ExpireProposal           │  │  - RewardCritique          │   │
│  └─────────────┬───────────────┘  └──────────┬─────────────────┘  │
│                │                               │                    │
│                └──────────┬────────────────────┘                    │
│                           ▼                                         │
│  External dependencies:                                             │
│  - Agent Registry (DID verification via reference inputs)           │
│  - Module 3: Reputation Staking (proposer credibility signal)         │
│  - Module 1: Adversarial Auditing (dispute escalation for critiques)  │
│  - Protocol Params UTXO (shared across all modules)                   │
│  - Foundation Oracle (adoption/rejection signals)                    │
│  - Governance Treasury UTXO (reward pool for adopted proposals)     │
└────────────────────────────────────────────────────────────────────┘
```

### 3.2 UTXO Flow

```
PROPOSAL SUBMISSION:
  Agent A analyzes chain metrics → identifies inefficiency
  Agent A submits proposal → ProposalUTXO created (locked with stake)
  Proposal enters Open state (accepting critiques/endorsements)
  ... REVIEW_WINDOW slots pass ...

CRITIQUE/ENDORSEMENT (while proposal is Open):
  Agent B submits critique → CritiqueUTXO created (references ProposalUTXO)
  Agent C endorses proposal → EndorsementUTXO created (references ProposalUTXO)
  Critiques and endorsements are independent UTXOs — fully parallel

AMENDMENT (proposer incorporates critiques):
  Agent A amends proposal → ProposalUTXO updated (new content hash)
  Amendment references which critiques were incorporated
  This is how proposers share rewards with helpful critics

FOUNDATION ADOPTION:
  Foundation reviews proposals (off-chain)
  Foundation submits AdoptProposal tx → proposal state = Adopted
  Reward distributed: proposer + credited critics
  Foundation implements the adopted change (e.g., updates ProtocolParams UTXO)

FOUNDATION REJECTION:
  Foundation submits RejectProposal tx → proposal state = Rejected
  Proposal stake returned to proposer (no penalty for honest proposals)
  Reasoning hash published (Foundation explains why)

EXPIRATION (no Foundation action):
  REVIEW_WINDOW expires → anyone can call ExpireProposal
  Proposal stake returned to proposer
  No penalty — Foundation simply didn't act on it

CONTENTION (multiple agents propose similar changes):
  Agent A submits proposal for MIN_CLAIM_STAKE reduction
  Agent B submits separate proposal for same change
  Both are independent UTXOs — no contention
  Foundation can adopt either, both, or neither
  This is a FEATURE: competing proposals produce better analysis
```

---

## 4. On-Chain Types

### 4.1 Proposal Types

```aiken
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
  /// AP3X staked by proposer (skin in the game)
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

pub type ProposalType {
  /// Change a protocol parameter (e.g., MIN_CLAIM_STAKE, JURY_SIZE)
  ParameterChange { param_name: ByteArray, current_value: Int, proposed_value: Int }
  /// Allocate treasury funds for a specific purpose
  TreasurySpend { amount: Int, recipient_description: ByteArray }
  /// Propose a protocol upgrade or new feature
  ProtocolUpgrade { upgrade_description_hash: ByteArray }
  /// Propose activation or modification of a module
  GameActivation { game_id: Int }
  /// General governance suggestion (catch-all)
  GeneralSuggestion
}

/// Whether this proposal is flagged as emergency (shorter review, higher stake)
/// Emergency proposals MUST be ParameterChange or ProtocolUpgrade.
/// Encoded separately from ProposalType to avoid duplicating all variants.
pub type ProposalPriority {
  /// Standard proposal — normal review window and stake
  Standard
  /// Emergency proposal — EMERGENCY_REVIEW_WINDOW, EMERGENCY_STAKE_MULTIPLIER × stake
  /// Requires proposer reputation >= Established tier (100+ AP3X)
  Emergency
}

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

pub type ProposalAction {
  /// Submit a new governance proposal with stake
  SubmitProposal
  /// Withdraw proposal before adoption/rejection (stake returned)
  WithdrawProposal
  /// Amend proposal incorporating critiques (proposer only)
  AmendProposal { new_hash: ByteArray, new_uri: ByteArray, incorporated: List<OutputReference> }
  /// Foundation adopts the proposal (oracle action)
  AdoptProposal { reasoning_hash: ByteArray, reward_amount: Int }
  /// Foundation rejects the proposal (oracle action)
  RejectProposal { reasoning_hash: ByteArray }
  /// Expire proposal after review window (callable by anyone)
  ExpireProposal
  /// Expire a stale ParameterChange proposal whose current_value no longer matches
  /// on-chain ProtocolParams (callable by anyone, permissionless)
  ExpireStaleProposal
  /// Foundation signals "under review" — extends review window (Foundation oracle)
  /// Does not adopt or reject; just resets the expiry timer
  ExtendReview { additional_slots: Int }
}
```

### 4.2 Critique Types

```aiken
/// A critique of an existing proposal — can be supportive, opposing, or amending
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
  /// AP3X staked by critic (skin in the game — lose if critique is frivolous)
  stake_amount: Int,
  /// Slot when critique was submitted
  submitted_at: Int,
  /// Whether this critique was incorporated into a proposal amendment
  incorporated: Bool,
}

pub type CritiqueType {
  /// Critique supports the proposal and provides additional analysis
  Supportive
  /// Critique opposes the proposal with counter-arguments
  Opposing
  /// Critique suggests specific amendments/improvements
  Amendment { suggested_change_hash: ByteArray }
}

pub type CritiqueAction {
  /// Submit a new critique
  MintCritiqueToken
  /// Withdraw critique (only if proposal is still Open and not yet incorporated)
  WithdrawCritique
  /// Mark critique as incorporated (called during AmendProposal)
  IncorporateCritique
  /// Distribute reward share to critic (called during AdoptProposal)
  RewardCritique { reward_share: Int }
}
```

### 4.3 Endorsement Types

```aiken
/// An endorsement: Agent stakes AP3X signaling support for a proposal
/// Endorsements are weighted signals to the Foundation — they don't determine outcomes
pub type GovernanceEndorsementDatum {
  /// DID of the endorsing agent
  endorser_did: ByteArray,
  /// Payment credential of endorser
  endorser_credential: Credential,
  /// Reference to the proposal being endorsed
  proposal_ref: OutputReference,
  /// AP3X staked as endorsement signal
  stake_amount: Int,
  /// Slot when endorsement was created
  created_at: Int,
}

pub type GovernanceEndorsementAction {
  /// Create endorsement
  MintEndorsementToken
  /// Withdraw endorsement (anytime — no lock)
  WithdrawEndorsement
}
```

### 4.4 Foundation Oracle Types

```aiken
/// Foundation governance oracle — trusted resolver for proposal adoption
pub type GovernanceOracleDatum {
  /// Foundation's payment credential (multi-sig recommended)
  oracle_credential: Credential,
  /// Governance treasury address (source of adoption rewards)
  treasury_script_hash: ScriptHash,
  /// Whether governance oracle is active
  oracle_active: Bool,
}

pub type OracleGovernanceAction {
  /// Foundation adopts a proposal
  Adopt { proposal_ref: OutputReference, reward_amount: Int, reasoning_hash: ByteArray }
  /// Foundation rejects a proposal
  Reject { proposal_ref: OutputReference, reasoning_hash: ByteArray }
}
```

### 4.5 Governance Parameters Type

```aiken
/// Governance parameters stored in a dedicated UTXO at a governance-controlled address.
/// Validators read via reference input. Separate from ProtocolParams because Module 6
/// parameters are self-referential (proposals can change governance rules).
pub type GovernanceParams {
  /// Minimum AP3X to stake on a proposal
  min_proposal_stake: Int,
  /// Minimum AP3X to stake on a critique
  min_critique_stake: Int,
  /// Minimum AP3X endorsement signal
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
  /// Proposer share of adoption reward (basis points)
  proposer_reward_share: Int,
  /// Critic share of adoption reward (basis points)
  critic_reward_share: Int,
  /// Protocol fee rate (basis points)
  protocol_fee_rate: Int,
  /// Minimum adoption reward
  min_adoption_reward: Int,
  /// Maximum adoption reward
  max_adoption_reward: Int,
  /// Maximum incorporated critiques per proposal
  max_incorporated_critiques: Int,
  /// Maximum treasury spend per proposal
  max_treasury_request: Int,
  /// Emergency proposal stake multiplier (basis points, e.g. 5000 = 5x normal)
  emergency_stake_multiplier: Int,
  /// Emergency review window in slots (shorter than normal)
  emergency_review_window: Int,
  /// Timelock delay for auto-executing adopted parameter changes (Phase 1.2)
  param_execution_delay: Int,
  /// Minimum total prediction pool for pari-mutuel resolution (Phase 1.2)
  min_prediction_pool: Int,
  /// Credibility Pool low threshold — suspends new reviews (Phase 1.2)
  credibility_pool_low_threshold: Int,
  /// Credibility Pool critical threshold — pro-rata payouts (Phase 1.2)
  credibility_pool_critical_threshold: Int,
}
```

### 4.6 Proposer Activity Tracking

```aiken
/// Tracks a proposer's activity for rate limiting and cooldown enforcement.
/// One ActivityUTXO per agent, maintained at the proposal validator address.
///
/// DESIGN NOTE: On eUTXO, you CANNOT enumerate all UTXOs at an address from
/// within a validator script. The "count active proposals" check needs a
/// different approach than on account-based chains.
///
/// Solution: Each agent has a ProposerActivity UTXO that tracks their active
/// proposal count and last submission slot. This UTXO is consumed and
/// re-created on every SubmitProposal, WithdrawProposal, and proposal
/// finalization (adopt/reject/expire).
///
/// Tradeoff: Creates a contention point on this agent's Activity UTXO.
/// But since proposals are infrequent (max 3 active, 24h cooldown), this
/// is acceptable — unlike Module 3 where stake UTXOs are touched frequently.
pub type ProposerActivityDatum {
  /// DID of the agent
  agent_did: ByteArray,
  /// Payment credential of the agent
  agent_credential: Credential,
  /// Number of currently active (Open/Amended) proposals
  active_proposal_count: Int,
  /// Slot of most recent proposal submission
  last_proposal_slot: Int,
  /// Activity tracking token (proves this UTXO was created via legitimate action)
  /// Asset name = "pact_" ++ blake2b_256(agent_did)[0..28]
  /// Minted on first proposal, never burned (reused across proposals)
}

pub type ProposerActivityAction {
  /// Create activity tracker (first proposal ever)
  InitActivity
  /// Increment active count (new proposal submitted)
  IncrementActivity
  /// Decrement active count (proposal finalized/withdrawn)
  DecrementActivity
}
```

### 4.7 Proposal Token Lifecycle

```aiken
/// Proposal token asset name = "prop_" ++ blake2b_256(proposal_utxo_ref)[0..28]
/// This token MUST travel with the proposal UTXO through state transitions.
/// Burned when proposal is finalized (adopted, rejected, expired, or withdrawn).
///
/// Critique token = "crit_" ++ blake2b_256(critique_utxo_ref)[0..28]
/// Endorsement token = "gend_" ++ blake2b_256(endorser_did ++ proposal_ref)[0..28]
/// (gend = governance endorsement, avoids collision with Module 3 endorsement tokens)
```

---

## 5. Validation Rules

### 5.1 Proposal Submission (`SubmitProposal`)

```
MUST:
  1. Proposer has active DID in Agent Registry (verified via reference input)
  2. stake_amount >= MIN_PROPOSAL_STAKE (parameter, initially 25 AP3X)
  3. proposal_hash is exactly 32 bytes (blake2b_256)
  4. proposal_type is valid and well-formed:
     - ParameterChange: param_name is in GOVERNABLE_PARAMS list
     - TreasurySpend: amount > 0 and <= MAX_TREASURY_REQUEST
     - ProtocolUpgrade: upgrade_description_hash is 32 bytes
     - GameActivation: game_id is valid
     - GeneralSuggestion: always valid
  5. storage_uri is non-empty
  6. review_window >= MIN_REVIEW_WINDOW (initially 64,800 slots = ~3 days)
  7. review_window <= MAX_REVIEW_WINDOW (initially 604,800 slots = ~28 days)
  8. Output ProposalUTXO at proposal validator address contains:
     - Inline datum with state = Open
     - Value includes stake_amount AP3X + min UTXO
     - Proposal tracking token (minted in same tx)
  9. amendment_count = 0
  10. incorporated_critiques = []
  11. Transaction signed by proposer_credential

RATE LIMITING (enforced via ProposerActivity UTXO):
  12. ProposerActivity UTXO consumed as input (or InitActivity if first proposal)
  13. active_proposal_count < MAX_ACTIVE_PROPOSALS_PER_AGENT (initially 3)
  14. current_slot >= last_proposal_slot + COOLDOWN_BETWEEN_PROPOSALS (initially 21,600 slots)
  15. Continuing ProposerActivity UTXO with:
      - active_proposal_count += 1
      - last_proposal_slot = current_slot
  16. Transaction must include the activity tracking token ("pact_" prefix)

QUALITY SIGNAL (not enforced on-chain, but indexed):
  17. Proposer's Module 3 reputation tier (higher tier = Foundation reviews sooner)
  18. Number of previously adopted proposals (track record)
```

### 5.1b Emergency Proposal Submission (`SubmitProposal` with emergency flag)

```
For urgent governance needs (e.g., parameter miscalibration causing harm),
agents can submit emergency proposals with a shorter review window but
higher stake requirement.

MUST (in addition to all SubmitProposal rules):
  1. proposal_type == ParameterChange OR ProtocolUpgrade
     (no emergency TreasurySpend or GeneralSuggestion — those can wait)
  2. stake_amount >= MIN_PROPOSAL_STAKE × EMERGENCY_STAKE_MULTIPLIER (initially 5x = 125 AP3X)
  3. review_window == EMERGENCY_REVIEW_WINDOW (initially 10,800 slots = ~12 hours)
  4. Proposer reputation tier >= Established (100+ AP3X reputation score)
     (verified via reference input to proposer's Module 3 StakeUTXO)
  5. ProposalDatum.priority == Emergency
     (NOT encoded in proposal_type — uses separate ProposalPriority enum, §4.1)

RATIONALE: Higher stake filters trivial "emergencies". Reputation gate
prevents brand-new agents from triggering emergency reviews. Short review
window gets Foundation attention faster. If the Foundation doesn't act
within 12 hours, the proposal expires and the 125 AP3X is returned.

ABUSE PREVENTION:
  - 5x stake means false emergencies cost 5x in opportunity cost
  - MAX_ACTIVE_PROPOSALS still applies (can't flood emergency queue)
  - Foundation can reject with reasoning "not an emergency" — no penalty
    but the agent's track record shows a rejected emergency (reputation signal)
```

### 5.2 Proposal Withdrawal (`WithdrawProposal`)

```
MUST:
  1. Proposal state == Open OR Amended
  2. No AdoptProposal or RejectProposal pending
  3. Stake returned to proposer_credential
  4. Proposal token burned
  5. ProposerActivity UTXO consumed and re-created with active_proposal_count -= 1
  6. Transaction signed by proposer_credential

NOTE: Withdrawal is always allowed — proposers can retract bad proposals
at any time without penalty. This encourages honest self-correction.
```

### 5.3 Amend Proposal (`AmendProposal`)

```
MUST:
  1. Proposal state == Open OR Amended
  2. new_hash is exactly 32 bytes (different from current proposal_hash)
  3. new_uri is non-empty
  4. incorporated list references valid CritiqueUTXOs at critique validator
  5. Each referenced critique:
     - References this proposal (critique.proposal_ref matches)
     - Has not been previously incorporated
     - Is updated: incorporated = True (continuing output)
  6. Continuing ProposalUTXO with:
     - state = Amended { previous_hash: old_hash }
     - proposal_hash = new_hash
     - storage_uri = new_uri
     - amendment_count += 1
     - incorporated_critiques extended with new references
  7. amendment_count <= MAX_AMENDMENTS (initially 5)
  8. Transaction signed by proposer_credential

RATIONALE: Amendments let proposers improve proposals based on feedback.
Incorporating critiques creates a reward-sharing relationship — if the
amended proposal is adopted, incorporated critics get a share.
```

### 5.4 Foundation Adoption (`AdoptProposal`)

```
MUST:
  1. Governance Oracle UTXO exists as reference input with oracle_active == True
  2. Transaction signed by oracle_credential (Foundation multi-sig)
  3. Proposal exists and state == Open OR Amended
  4. reward_amount > 0 and <= MAX_ADOPTION_REWARD (parameter)
  5. reasoning_hash is exactly 32 bytes
  6. Reward distribution:
     - proposer_share = reward_amount × PROPOSER_REWARD_SHARE (initially 70%)
     - critic_pool = reward_amount × CRITIC_REWARD_SHARE (initially 20%)
     - treasury_fee = reward_amount × PROTOCOL_FEE_RATE (initially 10%)
  7. Proposer receives: proposer_share + stake returned
  8. Incorporated critics receive: critic_pool / number_of_incorporated_critiques
     (equal split among credited critics)
  9. Non-incorporated critics: stake returned (no reward, no penalty)
  10. Protocol treasury receives: treasury_fee
  11. Endorsers: endorsement stakes returned (no reward — endorsement is a signal)
  12. Proposal token burned, critique tokens for incorporated critiques burned
  13. Proposal state updated to Adopted { adoption_reasoning_hash }

ACTIVITY TRACKING:
  14. ProposerActivity UTXO consumed and re-created with active_proposal_count -= 1

cross-module EFFECTS:
  15. Proposer earns +10 AP3X Module 3 history bonus (CrossGameBonus UTXO minted)
  16. Incorporated critics earn +5 AP3X Module 3 history bonus each
```

### 5.5 Foundation Rejection (`RejectProposal`)

```
MUST:
  1. Governance Oracle UTXO as reference input, oracle_active == True
  2. Transaction signed by oracle_credential
  3. Proposal exists and state == Open OR Amended
  4. rejection_reasoning_hash is exactly 32 bytes
  5. Proposer stake returned in full (no penalty for honest proposals)
  6. Critic stakes returned in full
  7. Endorsement stakes returned
  8. All tokens (proposal, critique, endorsement) burned
  9. Proposal state = Rejected { rejection_reasoning_hash }
  10. ProposerActivity UTXO consumed and re-created with active_proposal_count -= 1

RATIONALE: No slashing on rejection. Punishing rejected proposals would
deter participation and only reward safe/obvious suggestions. The cost
of a rejected proposal is capital lockup during review window + tx fees.
```

### 5.6 Proposal Expiration (`ExpireProposal`)

```
CALLABLE BY: Any agent (permissionless)

MUST:
  1. Proposal state == Open OR Amended
  2. Current slot > submitted_at + review_window
  3. All stakes returned (proposer, critics, endorsers)
  4. All tokens burned
  5. Proposal state = Expired
  6. ProposerActivity UTXO consumed and re-created with active_proposal_count -= 1

NOTE: Expiration means the Foundation neither adopted nor rejected.
This is neutral — the proposal may have been useful analysis that
informed a different decision. No penalty.
```

### 5.6b Expire Stale Proposal (`ExpireStaleProposal`)

```
CALLABLE BY: Any agent (permissionless — Phase 1.1+)

MUST:
  1. Proposal state == Open OR Amended
  2. proposal_type == ParameterChange { param_name, current_value, proposed_value }
  3. ProtocolParams UTXO referenced as reference input
  4. Current on-chain value of param_name != current_value
     (the parameter was changed by another adopted proposal)
  5. Stake returned to proposer (no penalty — reality changed)
  6. All critic and endorsement stakes returned
  7. All tokens burned
  8. ProposerActivity UTXO consumed and re-created with active_proposal_count -= 1
  9. Proposal state = Expired

RATIONALE: Stale proposals reference outdated parameter values. Allowing
permissionless expiry keeps the proposal queue clean. The caller earns no
fee (unlike decay collectors in Module 3) — this is a community hygiene
action, not a profit opportunity. If incentive is needed in practice,
Phase 1.2 could add a small stale-expiry bounty.
```

### 5.6c Extend Review (`ExtendReview`)

```
MUST:
  1. Governance Oracle UTXO as reference input, oracle_active == True
  2. Transaction signed by oracle_credential (Foundation multi-sig)
  3. Proposal state == Open OR Amended
  4. additional_slots > 0
  5. submitted_at + review_window + additional_slots <= submitted_at + MAX_REVIEW_WINDOW
     (total review window cannot exceed MAX_REVIEW_WINDOW even with extensions)
  6. Continuing ProposalUTXO with:
     - review_window += additional_slots
     - All other fields unchanged
  7. No cost to Foundation (informational signal only)

RATIONALE: Lets the Foundation signal engagement without committing to
adopt or reject. Prevents good proposals from expiring while under review.
Proposer sees their capital lockup extended but also gets a positive signal
that the Foundation is considering the proposal.
```

### 5.7 Submit Critique (`MintCritiqueToken`)

```
MUST:
  1. Critic has active DID in Agent Registry
  2. Referenced proposal exists and state == Open OR Amended
  3. critic_did != proposer_did (cannot self-critique)
     EXCEPT: self-critique allowed for critique_type == Amendment
     (proposer can document their own improvements)
  4. stake_amount >= MIN_CRITIQUE_STAKE (initially 5 AP3X)
  5. critique_hash is exactly 32 bytes
  6. One critique per critic per proposal per type
     (can submit one Supportive + one Opposing + one Amendment)
  7. CritiqueUTXO created at critique validator address
  8. incorporated = False
  9. Transaction signed by critic_credential
```

### 5.8 Withdraw Critique (`WithdrawCritique`)

```
MUST:
  1. Critique exists (critique token present)
  2. incorporated == False (cannot withdraw after incorporation)
  3. Referenced proposal state == Open OR Amended (not finalized)
  4. Stake returned to critic_credential
  5. Critique token burned
  6. Transaction signed by critic_credential
```

### 5.9 Submit Governance Endorsement (`MintEndorsementToken`)

```
MUST:
  1. Endorser has active DID in Agent Registry
  2. Referenced proposal exists and state == Open OR Amended
  3. endorser_did != proposer_did (cannot self-endorse)
  4. stake_amount >= MIN_GOVERNANCE_ENDORSEMENT (initially 5 AP3X)
  5. Endorsement token = blake2b_256(endorser_did ++ proposal_ref) — one per pair
  6. GovernanceEndorsementUTXO created at critique validator address
  7. Transaction signed by endorser_credential

NOTE: Endorsements are weighted signals, not votes. The Foundation
considers them alongside reputation scores and proposal quality.
An endorsement from a Trusted-tier agent carries more weight than
one from a Novice (off-chain evaluation, not enforced on-chain).
```

---

## 6. Proposal Categories and Data Sources

### 6.1 Governable Parameters

The following parameters can be proposed for change. Each parameter belongs to a module or system component:

| Parameter | Current Source | Module/System |
|-----------|---------------|-------------|
| `MIN_CLAIM_STAKE` | ProtocolParams UTXO | Module 1 |
| `MIN_CHALLENGE_WINDOW` | ProtocolParams UTXO | Module 1 |
| `JURY_SIZE` | ProtocolParams UTXO | Module 1 |
| `JURY_FEE_RATE` | ProtocolParams UTXO | Module 1 |
| `MIN_SELF_STAKE` | ProtocolParams UTXO | Module 3 |
| `MIN_ENDORSEMENT` | ProtocolParams UTXO | Module 3 |
| `DECAY_RATE` | ProtocolParams UTXO | Module 3 |
| `MAX_ENDORSEMENT_MULTIPLIER` | ProtocolParams UTXO | Module 3 |
| `MIN_PROPOSAL_STAKE` | GovernanceParams UTXO | Module 6 |
| `ADOPTION_REWARD_BASE` | GovernanceParams UTXO | Module 6 |
| Treasury allocation limits | Treasury UTXO | System |
| Module activation flags | ProtocolParams UTXO | System |

### 6.2 Chain Metrics for Analysis

Agents analyze on-chain data to produce data-driven proposals. Key data sources:

| Metric Category | Data Source | Example Analysis |
|-----------------|------------|-----------------|
| **Transaction Volume** | Indexer: block data | "Tx volume up 300% — increase block size?" |
| **Fee Patterns** | Indexer: fee history | "Avg fee 2x target — adjust fee algorithm?" |
| **Module Participation** | Indexer: module UTXOs | "Only 3 jurors active — lower MIN_JUROR_BOND?" |
| **Claim/Challenge Ratios** | Module 1 UTXOs | "Challenge rate too low — lower MIN_CHALLENGE_WINDOW?" |
| **Reputation Distribution** | Module 3 UTXOs | "90% agents at Novice — lower tier thresholds?" |
| **Treasury Balance** | Treasury UTXO | "Treasury at 100K AP3X — fund developer grants?" |
| **Agent Census** | Registry UTXOs | "50 agents registered — activate Module 5?" |
| **Staking Utilization** | All module UTXOs | "Only 20% of AP3X staked — increase staking rewards?" |

### 6.3 Proposal Document Standard

Full proposals follow a structured JSON format stored off-chain:

```json
{
  "version": "1.0",
  "proposal_type": "parameter_change",
  "title": "Reduce MIN_CLAIM_STAKE from 50 to 25 AP3X",
  "summary": "Analysis of Module 1 participation shows claim volume is below target...",
  "analysis": {
    "data_sources": [
      { "source": "indexer:/v1/auditing/stats", "period": "last_30_epochs" },
      { "source": "indexer:/v1/reputation/stats", "period": "last_30_epochs" }
    ],
    "metrics": {
      "current_claims_per_epoch": 3.2,
      "target_claims_per_epoch": 10,
      "claim_abandonment_rate": 0.15,
      "avg_agent_balance": 200
    },
    "methodology": "Compared claim rates before and after last parameter change...",
    "findings": [
      "Claim volume 68% below target",
      "Small agents (balance < 100 AP3X) represent 60% of registry but only 15% of claims",
      "No evidence of spam at current levels"
    ]
  },
  "recommendation": {
    "param_name": "MIN_CLAIM_STAKE",
    "current_value": 50,
    "proposed_value": 25,
    "rationale": "Reducing stake lowers barrier for Type 1 agents...",
    "risk_assessment": "Spam risk: low (current challenge rate shows auditors active)",
    "expected_impact": "2-3x increase in claim volume from small agents",
    "rollback_criteria": "If spam rate exceeds 20% of claims, revert to 50"
  },
  "metadata": {
    "agent_did": "did:vector:agent:abc123:...",
    "timestamp": "2026-03-20T14:30:00Z",
    "tools_used": ["vector-agent-sdk v0.3", "chain-analytics v1.0"]
  }
}
```

### 6.4 Critique Document Standard

Critiques follow a structured format enabling systematic evaluation:

```json
{
  "version": "1.0",
  "critique_type": "amendment",
  "proposal_ref": "tx_hash#index",
  "summary": "Phased reduction better than single-step change",
  "analysis": {
    "strengths": [
      "Correct diagnosis: claim volume below target",
      "Good data sources: 30-epoch sample sufficient"
    ],
    "weaknesses": [
      "No consideration of spam risk at lower threshold",
      "Single-step change doesn't allow measurement of effect"
    ],
    "missing_data": [
      "Spam rate analysis at comparable parameter levels on other chains",
      "Agent balance distribution curve (not just <100 AP3X bucket)"
    ]
  },
  "recommendation": {
    "suggested_change": "Reduce to 35 AP3X first, monitor for 30 epochs, then 25",
    "rationale": "Phased approach isolates the variable and allows rollback",
    "risk_delta": "Lower risk: if spam appears at 35, we don't go to 25"
  },
  "metadata": {
    "agent_did": "did:vector:agent:critic456:...",
    "timestamp": "2026-03-20T16:00:00Z",
    "tools_used": ["vector-agent-sdk v0.3"]
  }
}
```

**Critique quality heuristics** (off-chain, indexer-computed):

| Heuristic | Weight | Description |
|-----------|--------|-------------|
| Data-backed | 0.3 | Critique references verifiable chain data |
| Specificity | 0.25 | Suggests concrete changes vs. vague "this is bad" |
| Novelty | 0.2 | Raises points not in other critiques for same proposal |
| Track record | 0.15 | Critic's previous critiques were incorporated + adopted |
| Timeliness | 0.1 | Submitted early in review window (more useful to proposer) |

---

## 7. Reward Economics

### 7.1 Reward Pool

Adoption rewards come from the **Governance Treasury** — a dedicated UTXO holding AP3X allocated for governance incentives.

```
GOVERNANCE TREASURY FUNDING:
  - Initial allocation: Foundation seeds with 10,000 AP3X at launch
    (200 proposals at 50 AP3X average reward — ~6 months runway at target rate)
  - Ongoing: 5% of all module protocol fees routed to governance treasury
    Module 1 jury fees generate ~X AP3X/epoch
    Module 3 protocol fees generate ~Y AP3X/epoch
    Module 6 own protocol fees recycle back
  - Replenishment trigger: when treasury < MIN_TREASURY_BATCHES × TREASURY_BATCH_SIZE
    (5 × 500 = 2,500 AP3X), Foundation is alerted to create new batches
  - Emergency: Foundation can fund treasury directly from main treasury

TREASURY HEALTH METRIC (indexed):
  treasury_runway = treasury_balance / (avg_rewards_per_epoch × 90)
  // Measured in epochs of runway. Target: > 90 epochs (~15 days at 4h epochs)
  // If runway < 90: indexer raises low-treasury alert
  // If runway < 30: emergency governance proposal auto-generated by system

REWARD SIZING — Guided Formula (Phase 1.1+):
  While Foundation has final discretion, a reward formula provides consistency:

  base_reward = REWARD_BASE_BY_TYPE[proposal_type]
    ParameterChange:   100 AP3X
    TreasurySpend:     150 AP3X (requires more analysis)
    ProtocolUpgrade:   200 AP3X (highest complexity)
    GameActivation:    100 AP3X
    GeneralSuggestion:  75 AP3X

  impact_multiplier = 1.0 + (affected_agents / total_agents) × 0.5
    // Proposals affecting all agents get up to 1.5x
    // Proposals affecting a niche get ~1.0x

  urgency_multiplier = if emergency { 1.5 } else { 1.0 }

  novelty_multiplier = if first_proposal_on_topic { 1.2 } else { 1.0 }

  recommended_reward = base_reward × impact_multiplier × urgency_multiplier × novelty_multiplier
  clamped_reward = clamp(recommended_reward, MIN_ADOPTION_REWARD, MAX_ADOPTION_REWARD)

  Foundation can override but must publish reasoning if deviating > 30% from formula.

REWARD RANGE (governance-adjustable):
  MIN_ADOPTION_REWARD = 50 AP3X
  MAX_ADOPTION_REWARD = 500 AP3X
  TYPICAL_REWARD = 100-200 AP3X (validated by formula at steady state)
```

### 7.2 Reward Distribution

```
On adoption, reward_amount is split:

  ┌─────────────────────────────────────────────┐
  │         Adoption Reward: 200 AP3X           │
  │                                              │
  │  Proposer:     140 AP3X (70%)               │
  │  Critics:       40 AP3X (20%, split equally)│
  │  Protocol:      20 AP3X (10%)               │
  └─────────────────────────────────────────────┘

  Plus:
  - Proposer's original stake returned
  - All critic stakes returned
  - All endorsement stakes returned

If no critiques were incorporated:
  - Proposer receives: reward_amount × 90% (proposer + unallocated critic share)
  - Protocol: 10%
```

### 7.3 Prediction Market Extension (Phase 1.2)

**Open question from module design**: Can this be gamified into a prediction market?

**Answer**: Yes. A **pari-mutuel prediction market** on governance outcomes creates a secondary positive-sum module where agents earn by accurately predicting Foundation decisions, while generating a valuable consensus signal.

#### 7.3.1 Prediction Market Design

```aiken
/// A prediction stake on a governance proposal outcome.
/// One PredictionUTXO per predictor per proposal.
pub type PredictionDatum {
  /// DID of the predicting agent
  predictor_did: ByteArray,
  /// Payment credential for payout
  predictor_credential: Credential,
  /// Reference to the proposal being predicted
  proposal_ref: OutputReference,
  /// Predicted outcome
  prediction: PredictionOutcome,
  /// AP3X staked on this prediction
  stake_amount: Int,
  /// Slot when prediction was placed
  placed_at: Int,
}

pub type PredictionOutcome {
  /// Predict the proposal will be adopted
  WillAdopt
  /// Predict the proposal will be rejected
  WillReject
  /// Predict the proposal will expire (no Foundation action)
  WillExpire
}

pub type PredictionAction {
  /// Place a new prediction
  MintPredictionToken
  /// Withdraw prediction (only before proposal is finalized + PREDICTION_LOCK)
  WithdrawPrediction
  /// Claim payout after proposal resolution
  ClaimPredictionPayout
}
```

#### 7.3.2 Pari-Mutuel Resolution

```
PARI-MUTUEL MODEL (not fixed-odds):
  All prediction stakes pool together. Winners split the entire pool
  proportional to their stake, minus a protocol fee.

  Example:
    Proposal P has predictions:
      WillAdopt pool:  300 AP3X (from 5 agents)
      WillReject pool: 100 AP3X (from 2 agents)
      WillExpire pool:  50 AP3X (from 1 agent)
      Total pool: 450 AP3X

    Foundation adopts P → WillAdopt pool wins.

    Protocol fee: 450 × PREDICTION_FEE_RATE (5%) = 22.5 AP3X
    Winner pool: 450 - 22.5 = 427.5 AP3X

    Each WillAdopt predictor receives:
      payout = (their_stake / winning_pool) × winner_pool_total

    Agent who staked 100 AP3X on WillAdopt:
      payout = (100 / 300) × 427.5 = 142.5 AP3X
      Profit: +42.5 AP3X (42.5% return)

WHY PARI-MUTUEL (not fixed-odds):
  - No counterparty risk — pool is fully collateralized
  - No oracle needed for odds — market determines odds dynamically
  - Each prediction UTXO is independent — fully parallelizable on eUTXO
  - Resolution is deterministic from the proposal's final state

TIMING CONSTRAINTS:
  - Predictions accepted only while proposal state == Open OR Amended
  - PREDICTION_LOCK = 3,600 slots (~4 hours) before review_window expires
    (no predictions in final 4 hours — prevents information asymmetry
    if Foundation signals intent off-chain before on-chain action)
  - Withdrawal allowed until PREDICTION_LOCK (then stakes are committed)

EDGE CASES:
  - If no predictions on losing side: winners get their stakes back
    (no profit, no loss — pool was one-sided)
  - If proposal is withdrawn by proposer: all predictions refunded
  - If proposal is stale-expired: treated as WillExpire for prediction purposes
```

#### 7.3.3 Prediction Market Game Theory

```
WHY THIS IS POSITIVE-SUM:
  1. Agents who understand Foundation priorities earn AP3X
  2. Foundation gets a real-time consensus signal on proposal quality
  3. Prediction odds are a public good — any observer can see market sentiment
  4. Wrong predictors subsidize right predictors (information discovery)

FOUNDATION SIGNAL:
  If 90% of prediction stake is on WillAdopt → strong agent consensus
  If 50/50 → agents are uncertain, Foundation should explain reasoning more
  If 80% on WillReject → agents think this will fail, maybe proposer should amend

  The Foundation is NOT bound by predictions. But the signal is valuable.

SYBIL IRRELEVANCE:
  Prediction markets are naturally sybil-resistant:
  - You don't gain by splitting a 100 AP3X bet into 10 × 10 AP3X
  - Pari-mutuel payout is proportional to stake, not to number of bets
  - More agents predicting = more information aggregation (desired)
```

**Status**: Phase 1.2. Requires a third validator: `prediction.ak`. Types and design are finalized; implementation deferred until proposal/critique system is mature.

---

## 8. Anti-Spam Mechanisms

### 8.1 Capital-Based Deterrence

```
PROPOSAL SPAM PREVENTION:
  - MIN_PROPOSAL_STAKE = 25 AP3X locks capital for entire review window (~3-28 days)
  - Opportunity cost: 25 AP3X locked for 3 days cannot be used in Modules 1, 3, etc.
  - No reward for non-adopted proposals (only capital return)
  - An agent spamming 10 proposals locks 250 AP3X with zero expected return

CRITIQUE SPAM PREVENTION:
  - MIN_CRITIQUE_STAKE = 5 AP3X per critique
  - One critique per type per proposal per agent (max 3 per agent per proposal)
  - Non-incorporated critiques get stake back but no reward
```

### 8.2 Reputation-Weighted Visibility

The indexer ranks proposals by a normalized quality score:

```
quality_signal(proposal) =
  normalized_reputation(proposer) × 0.4
  + normalized_endorsement(proposal) × 0.3
  + controversy_discount(proposal) × 0.2
  + adoption_track_record(proposer) × 0.1

WHERE:
  normalized_reputation(agent) =
    min(1.0, agent_reputation_score / ELITE_THRESHOLD)
    // Maps 0..2000+ reputation to 0.0..1.0 (capped at 1.0 for Elite tier)

  normalized_endorsement(proposal) =
    min(1.0, total_endorsement_stake / (MIN_PROPOSAL_STAKE × 10))
    // Saturates at 10x the minimum proposal stake in endorsements
    // 250+ AP3X endorsement = max signal

  controversy_discount(proposal) =
    1.0 / (1.0 + opposing_critique_count)
    // 0 opposing = 1.0, 1 opposing = 0.5, 2 opposing = 0.33

  adoption_track_record(agent) =
    min(1.0, adopted_proposals / 5)
    // 5+ adopted proposals = max track record signal

RESULT: quality_signal ∈ [0.0, 1.0] — all components normalized to same range.
Emergency proposals get a +0.5 bonus (capped at 1.0) to ensure priority review.

Foundation reviews proposals sorted by quality_signal (highest first).
Low-quality proposals from low-reputation agents are deprioritized — not
blocked, just reviewed later (or not at all if review window expires).
```

### 8.3 Rate Limiting

```
MAX_ACTIVE_PROPOSALS_PER_AGENT = 3
  - An agent cannot have more than 3 Open/Amended proposals simultaneously
  - Enforced on-chain: SubmitProposal checks that agent has < 3 active proposal tokens
  - This prevents a single agent from flooding the queue

COOLDOWN_BETWEEN_PROPOSALS = 21,600 slots (~24 hours)
  - After submitting a proposal, must wait before submitting another
  - Prevents rapid-fire low-quality submissions
```

---

## 9. Anti-Collusion Mechanisms

### 9.1 Self-Critique Ring Prevention

**Problem**: Type 2 operator uses Agent A to propose and Agent B to critique, earning both proposer and critic rewards from the same adoption.

**Mitigations**:

1. **On-chain rule**: `critic_did != proposer_did` for Supportive and Opposing critiques
2. **Critic reward is small**: Only 20% of reward, split among all critics. Even with 100% of the critic pool, it's 20% of adoption reward — less than just submitting a better proposal
3. **Foundation discretion**: Foundation sets reward_amount and can discount proposals with suspicious critique patterns
4. **DID graph analysis** (indexer-level): Flag proposals where all critiques come from DIDs sharing funding provenance
5. **MAX_INCORPORATED_CRITIQUES** = 5: Caps the number of critiques that share rewards

**Sybil economics**: If an operator controls both proposer and 1 critic:
- Gets 70% (proposer) + 20% (critic) = 90% of reward
- Without sybil: gets 70% (proposer) + 0% = 70% (the 20% goes to a real critic who improved the proposal)
- Net sybil benefit: 20% of reward (minus tx costs + capital lockup)
- But: the proposal is weaker without real critique, so less likely to be adopted
- **Conclusion**: Self-critique rings sacrifice proposal quality for marginal reward increase — usually net negative

### 9.2 Foundation Oracle Trust

**Problem**: The Foundation oracle is a centralized point of trust.

**Mitigations**:

1. **Multi-sig**: Foundation oracle uses M-of-N multi-sig (e.g., 3-of-5 council members)
2. **Reasoning transparency**: Every adoption/rejection publishes a reasoning_hash — Foundation must explain decisions publicly
3. **On-chain audit trail**: All Foundation governance actions are on-chain transactions — fully auditable
4. **Retroactive governance jury** (Phase 1.2): see Section 9.2.1 below
5. **Skin in the game**: Foundation's legitimacy depends on making good governance decisions. Bad decisions erode agent trust and participation.

#### 9.2.1 Retroactive Governance Jury (Phase 1.2)

```
PROBLEM: How to hold the Foundation accountable for adoption/rejection
decisions without undermining their authority or creating governance deadlock?

DESIGN: Non-binding retroactive review. The jury EVALUATES but does not OVERRIDE.

MECHANISM:
  After every adoption or rejection, there is a REVIEW_CHALLENGE_WINDOW
  (initially 129,600 slots = ~6 days) during which any Trusted-tier agent
  (reputation >= 500 AP3X) can open a Governance Review Challenge.

  GovernanceReviewDatum {
    /// The adopted/rejected proposal being reviewed
    proposal_ref: OutputReference,
    /// The Foundation's decision being questioned
    challenged_decision: Adopted | Rejected,
    /// DID of the agent opening the review
    challenger_did: ByteArray,
    /// Evidence hash: why the decision was wrong
    evidence_hash: ByteArray,
    /// Stake: same as Module 1 challenge stake
    stake_amount: Int,
  }

  JURY SELECTION:
    Same VRF-seeded random selection as Module 1 (reuses jury pool)
    JURY_SIZE = 5 (same as Module 1)
    Jurors must be Trusted-tier AND not have submitted critiques/endorsements
    on the original proposal (conflict of interest filter)

  VERDICTS:
    DecisionJustified — Foundation's reasoning was sound
    DecisionQuestionable — Foundation's reasoning was weak but not harmful
    DecisionUnjustified — Foundation's reasoning was clearly flawed

  CONSEQUENCES:
    DecisionJustified:
      Challenger loses stake (like Module 1 — bad challenge is costly)
      Foundation decision stands, credibility reinforced

    DecisionQuestionable:
      Both stakes returned (like Module 1 Inconclusive)
      Foundation's decision stands but the reasoning gap is logged
      Foundation is expected (social norm, not enforced) to publish
      a more detailed reasoning document

    DecisionUnjustified:
      Challenger wins stake (Foundation's "soft stake" — see below)
      Decision still STANDS (the jury cannot override Foundation)
      BUT: the "Unjustified" verdict is recorded on-chain permanently
      Foundation's governance credibility score decreases
      Repeated Unjustified verdicts = social pressure for governance reform

  FOUNDATION "SOFT STAKE":
    The Foundation doesn't stake AP3X per decision (impractical).
    Instead, a Governance Credibility Pool UTXO exists:
      - Funded from protocol fees (same as other treasury pools)
      - When a review finds DecisionUnjustified, the challenger's reward
        comes from this pool (not the Foundation's wallet)
      - If the pool depletes, it signals systemic governance problems

  WHY NON-BINDING:
    Making jury verdicts binding creates a governance paradox:
    the jury would need a "meta-jury" to review the jury's review.
    Non-binding reviews achieve accountability through transparency
    and reputation effects, not override authority.

    This follows the "advisory governance" principle of Module 6 itself:
    agents suggest, the Foundation decides, but decisions are auditable.
```

### 9.3 Proposal Supersession

**Problem**: Two agents propose the same parameter change. Foundation adopts one. What happens to the other?

**Solution — Stale Proposal Detection**:

```
When the Foundation adopts a ParameterChange proposal, the ProtocolParams
UTXO is updated. Any remaining Open proposals targeting the same parameter
become STALE because their `current_value` field no longer matches on-chain.

STALE DETECTION (off-chain, indexer):
  After any ProtocolParams update, the indexer marks proposals as "stale" where:
  - proposal_type == ParameterChange
  - param_name matches the changed parameter
  - current_value != new on-chain value

STALE HANDLING:
  Option A (Phase 1.0): Proposer manually withdraws stale proposals (stake returned)
  Option B (Phase 1.1): Anyone can call ExpireStaleProposal (new action):
    MUST:
      1. Proposal is ParameterChange targeting param P
      2. Current on-chain value of P != proposal.current_value
         (verified via reference input to ProtocolParams UTXO)
      3. Stake returned to proposer (no penalty — the world changed)
      4. All associated critique/endorsement stakes returned
      5. ProposerActivity decremented

RATIONALE: Stale proposals waste Foundation review time. Automatic staleness
detection keeps the queue clean. No penalty because the proposer was honest
when they submitted — reality just moved under them.
```

### 9.4 Treasury UTXO Contention

**Problem**: The Governance Treasury is a single UTXO. If the Foundation adopts two proposals in quick succession, both transactions try to consume the same Treasury UTXO → one fails.

**Solution — Treasury Batch UTXO Pattern**:

```
Instead of a single monolithic Treasury UTXO, the treasury maintains a set
of "reward batch" UTXOs, each pre-loaded with a fixed amount:

TREASURY ARCHITECTURE:
  ┌───────────────────────────────────────────┐
  │  Governance Treasury (governance-controlled) │
  │                                               │
  │  ┌──────────┐ ┌──────────┐ ┌──────────┐    │
  │  │ Batch #1  │ │ Batch #2  │ │ Batch #3  │    │
  │  │ 500 AP3X  │ │ 500 AP3X  │ │ 500 AP3X  │    │
  │  └──────────┘ └──────────┘ └──────────┘    │
  │  ...                                         │
  │  ┌──────────┐                                │
  │  │ Batch #N  │  (Foundation creates batches   │
  │  │ 500 AP3X  │   as needed)                   │
  │  └──────────┘                                │
  └───────────────────────────────────────────┘

Each AdoptProposal tx consumes ONE batch UTXO (not the master treasury).
If reward_amount < batch_amount, the remainder goes back as a new batch.
Foundation periodically replenishes batches from the master treasury.

This eliminates contention: multiple adoptions in the same block each
consume different batch UTXOs.

BATCH UTXO DATUM:
  pub type TreasuryBatchDatum {
    /// Batch identifier (sequential)
    batch_id: Int,
    /// Amount available in this batch
    available_amount: Int,
  }

IMPLEMENTATION NOTE: This is the same pattern used for DEX liquidity
on eUTXO — split large pools into multiple UTXOs to enable parallelism.
```

---

## 10. Parameters

All parameters governance-adjustable (self-referential: Module 6 proposals can change Module 6 parameters).

| Parameter | Initial Value | Unit | Rationale |
|-----------|--------------|------|-----------|
| `MIN_PROPOSAL_STAKE` | 25 | AP3X | Low enough for Type 1, high enough to deter spam |
| `MIN_CRITIQUE_STAKE` | 5 | AP3X | Very low — encourage critique activity |
| `MIN_GOVERNANCE_ENDORSEMENT` | 5 | AP3X | Low — endorsements are signals, not commitments |
| `MIN_REVIEW_WINDOW` | 64,800 | slots (~3d) | Enough time for Foundation + critique activity |
| `MAX_REVIEW_WINDOW` | 604,800 | slots (~28d) | Prevents indefinite capital lockup |
| `MAX_AMENDMENTS` | 5 | amendments | Prevents infinite revision cycles |
| `MAX_ACTIVE_PROPOSALS_PER_AGENT` | 3 | proposals | Rate limiting |
| `COOLDOWN_BETWEEN_PROPOSALS` | 21,600 | slots (~24h) | Prevents rapid-fire spam |
| `PROPOSER_REWARD_SHARE` | 7,000 | basis points (70%) | Majority of reward to proposer |
| `CRITIC_REWARD_SHARE` | 2,000 | basis points (20%) | Meaningful incentive for critiques |
| `PROTOCOL_FEE_RATE` | 1,000 | basis points (10%) | Treasury sustainability |
| `MIN_ADOPTION_REWARD` | 50 | AP3X | Floor for meaningful incentive |
| `MAX_ADOPTION_REWARD` | 500 | AP3X | Cap to protect treasury |
| `MAX_INCORPORATED_CRITIQUES` | 5 | critiques | Caps reward dilution |
| `MAX_TREASURY_REQUEST` | 10,000 | AP3X | Cap on single treasury spend proposals |
| `EMERGENCY_STAKE_MULTIPLIER` | 5,000 | basis points (5x) | High cost filters false emergencies |
| `EMERGENCY_REVIEW_WINDOW` | 10,800 | slots (~12h) | Fast Foundation response for real emergencies |
| `TREASURY_BATCH_SIZE` | 500 | AP3X | Per-batch allocation; prevents contention |
| `MIN_TREASURY_BATCHES` | 5 | batches | Minimum batches before replenishment alert |
| `PREDICTION_FEE_RATE` | 500 | basis points (5%) | Protocol fee on prediction pool (Phase 1.2) |
| `PREDICTION_LOCK` | 3,600 | slots (~4h) | No predictions in final hours before expiry |
| `MIN_PREDICTION_STAKE` | 5 | AP3X | Low barrier for prediction participation |
| `REVIEW_CHALLENGE_WINDOW` | 129,600 | slots (~6d) | Window to challenge Foundation decisions (Phase 1.2) |
| `REVIEW_CHALLENGE_STAKE` | 50 | AP3X | Stake to open retroactive governance review |
| `PARAM_EXECUTION_DELAY` | 21,600 | slots (~24h) | Timelock delay before adopted param changes execute (Phase 1.2) |
| `MIN_PREDICTION_POOL` | 100 | AP3X | Minimum total prediction pool for pari-mutuel resolution (Phase 1.2) |
| `PREDICTION_SEED_AMOUNT` | 50 | AP3X | Foundation seed per side per proposal for liquidity (Phase 1.2) |
| `PREDICTION_SEED_PROPOSALS` | 30 | proposals | Number of proposals receiving Foundation seed (Phase 1.2) |
| `EARLY_PREDICTOR_BONUS` | 1,000 | basis points (10%) | Bonus for first 3 predictors per side (Phase 1.2) |
| `CREDIBILITY_POOL_LOW_THRESHOLD` | 500 | AP3X | Suspends new governance reviews (Phase 1.2) |
| `CREDIBILITY_POOL_CRITICAL_THRESHOLD` | 200 | AP3X | Pro-rata payouts, hard circuit breaker (Phase 1.2) |

---

## 11. Contract Architecture (Aiken)

### 11.1 File Structure

```
contracts/governance-suggestion/
├── aiken.toml
│   dependencies:
│     - vector/shared v0.1.0  (DID verification, params reader, oracle verification)
├── validators/
│   ├── proposal.ak             # Proposal multi-validator (mint proposal token + spend proposal UTXO)
│   ├── critique.ak             # Critique + endorsement multi-validator
│   └── prediction.ak           # Prediction market multi-validator (Phase 1.2)
├── lib/
│   └── governance_suggestion/
│       ├── types.ak             # All types from Sections 4.1–4.7 + PredictionDatum
│       ├── params.ak            # GovernanceParams type + parameter reading
│       ├── proposal_validation.ak   # Proposal submission/amendment/adoption logic
│       ├── critique_validation.ak   # Critique submission/incorporation logic
│       ├── reward_distribution.ak   # Reward calculation and distribution
│       ├── activity_tracking.ak     # ProposerActivity rate limiting + cooldown
│       ├── treasury_batch.ak        # Treasury batch UTXO management
│       ├── emergency.ak             # Emergency proposal validation
│       ├── prediction.ak            # Pari-mutuel prediction market logic (Phase 1.2)
│       ├── stale_detection.ak       # ExpireStaleProposal validation (Phase 1.1)
│       └── utils.ak             # Module-specific helpers (param name validation, etc.)
│                                  # NOTE: DID verification, oracle verification now in vector/shared
└── tests/
    ├── proposal_tests.ak
    ├── critique_tests.ak
    ├── adoption_tests.ak
    ├── reward_tests.ak
    ├── prediction_tests.ak      # Phase 1.2
    ├── stale_tests.ak           # Phase 1.1
    └── integration_tests.ak
```

### 11.2 Cross-Validator References

```aiken
pub type GovernanceConfig {
  /// Script hash of the proposal validator
  proposal_validator_hash: ScriptHash,
  /// Script hash of the critique validator
  critique_validator_hash: ScriptHash,
  /// Script hash of the prediction validator (Phase 1.2, 0x00 until deployed)
  prediction_validator_hash: ScriptHash,
  /// Policy ID of the Agent Registry
  registry_policy_id: PolicyId,
  /// Script hash of the Agent Registry
  registry_script_hash: ScriptHash,
  /// Script hash of Module 3 reputation validator (for quality signals + emergency gate)
  reputation_validator_hash: ScriptHash,
  /// Script hash of Module 1 jury pool (for retroactive governance review, Phase 1.2)
  jury_pool_hash: ScriptHash,
  /// Script hash of the governance oracle
  governance_oracle_hash: ScriptHash,
  /// Script hash of governance parameters holder
  governance_params_hash: ScriptHash,
  /// Script hash of governance treasury
  governance_treasury_hash: ScriptHash,
  /// Script hash of governance credibility pool (Phase 1.2)
  credibility_pool_hash: ScriptHash,
  /// Script hash of protocol params (for parameter proposals + stale detection)
  protocol_params_hash: ScriptHash,
}
```

### 11.3 DID Verification

Uses the same pattern as Modules 1 and 3 — reference input to Agent Registry:

```aiken
/// Verify that a DID is active in the Agent Registry.
/// Identical to Module 1/Module 3 pattern — shared utility.
fn verify_active_did(
  config: GovernanceConfig,
  agent_did: ByteArray,
  reference_inputs: List<Input>,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      let is_at_registry = when input.output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.registry_script_hash
        _ -> False
      }
      let has_nft =
        assets.quantity_of(input.output.value, config.registry_policy_id, agent_did) == 1
      is_at_registry && has_nft
    },
  )
}
```

### 11.4 Parameter Validation

For `ParameterChange` proposals, validate that the proposed parameter exists:

```aiken
/// Validate that a parameter name is in the governable params list.
/// Reads from ProtocolParams UTXO to get current value for comparison.
fn validate_parameter_proposal(
  config: GovernanceConfig,
  param_name: ByteArray,
  claimed_current_value: Int,
  reference_inputs: List<Input>,
) -> Bool {
  // Find ProtocolParams UTXO
  expect Some(params_input) = list.find(
    reference_inputs,
    fn(input) {
      when input.output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.protocol_params_hash
        _ -> False
      }
    },
  )

  // Parse params and verify claimed_current_value matches on-chain value
  expect InlineDatum(raw_datum) = params_input.output.datum
  expect params: ProtocolParams = raw_datum

  // Verify the param exists and current value is correct
  // This prevents stale proposals referencing outdated parameter values
  validate_param_lookup(params, param_name, claimed_current_value)
}
```

### 11.5 Reward Distribution Logic

```aiken
/// Calculate and validate reward distribution for an adopted proposal.
fn validate_reward_distribution(
  params: GovernanceParams,
  reward_amount: Int,
  num_incorporated_critiques: Int,
  outputs: List<Output>,
  proposer_credential: Credential,
  critic_credentials: List<Credential>,
  treasury_hash: ScriptHash,
) -> Bool {
  let proposer_share = reward_amount * params.proposer_reward_share / 10000
  let protocol_fee = reward_amount * params.protocol_fee_rate / 10000

  let critic_pool = if num_incorporated_critiques > 0 {
    reward_amount * params.critic_reward_share / 10000
  } else {
    0  // Unallocated critic share goes to proposer
  }

  let adjusted_proposer_share = if num_incorporated_critiques == 0 {
    reward_amount - protocol_fee  // Proposer gets 90%
  } else {
    proposer_share
  }

  let per_critic_reward = if num_incorporated_critiques > 0 {
    critic_pool / num_incorporated_critiques
  } else {
    0
  }

  // Verify outputs contain correct amounts
  verify_output_to_credential(outputs, proposer_credential, adjusted_proposer_share)
  && verify_outputs_to_credentials(outputs, critic_credentials, per_critic_reward)
  && verify_output_to_script(outputs, treasury_hash, protocol_fee)
}
```

### 11.6 Rate Limiting Enforcement (ProposerActivity)

```aiken
/// Validate rate limiting for proposal submission.
/// Consumes the ProposerActivity UTXO to verify count and cooldown.
///
/// DESIGN NOTE: This is the key eUTXO challenge for rate limiting.
/// On EVM: just read a mapping. On eUTXO: must consume and re-create
/// a tracking UTXO. The tradeoff is acceptable because proposals are
/// infrequent (max 3 active, 24h cooldown between submissions).
fn validate_rate_limit(
  params: GovernanceParams,
  agent_did: ByteArray,
  current_slot: Int,
  inputs: List<Input>,
  outputs: List<Output>,
  config: GovernanceConfig,
) -> Bool {
  // Find the ProposerActivity UTXO in inputs
  let activity_input = list.find(
    inputs,
    fn(input) {
      let is_at_proposal_validator = when input.output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.proposal_validator_hash
        _ -> False
      }
      // Check for activity tracking token ("pact_" prefix)
      let has_activity_token = assets.quantity_of(
        input.output.value,
        config.proposal_validator_hash,  // Minted by same policy
        builtin.append_bytearray("pact_", builtin.slice_bytearray(0, 28, builtin.blake2b_256(agent_did)))
      ) == 1
      is_at_proposal_validator && has_activity_token
    },
  )

  when activity_input is {
    Some(input) -> {
      // Parse existing activity datum
      expect InlineDatum(raw) = input.output.datum
      expect activity: ProposerActivityDatum = raw

      // Verify cooldown: enough time since last proposal
      let cooldown_met = current_slot >= activity.last_proposal_slot + params.proposal_cooldown
      // Verify count: not at maximum
      let under_limit = activity.active_proposal_count < params.max_active_proposals

      // Verify continuing output has incremented count and updated slot
      let valid_output = list.any(outputs, fn(output) {
        expect InlineDatum(out_raw) = output.datum
        expect out_activity: ProposerActivityDatum = out_raw
        out_activity.agent_did == agent_did
        && out_activity.active_proposal_count == activity.active_proposal_count + 1
        && out_activity.last_proposal_slot == current_slot
      })

      cooldown_met && under_limit && valid_output
    }
    None -> {
      // First proposal ever — InitActivity: create new tracking UTXO
      // Verify output has count = 1, last_proposal_slot = current_slot
      list.any(outputs, fn(output) {
        expect InlineDatum(out_raw) = output.datum
        expect out_activity: ProposerActivityDatum = out_raw
        out_activity.agent_did == agent_did
        && out_activity.active_proposal_count == 1
        && out_activity.last_proposal_slot == current_slot
      })
    }
  }
}
```

### 11.7 Treasury Batch Management

```aiken
/// Treasury batch UTXO for parallel adoption reward distribution.
/// The Foundation creates multiple batch UTXOs to avoid contention.
pub type TreasuryBatchDatum {
  /// Batch identifier (sequential, for tracking)
  batch_id: Int,
  /// Whether this batch is active (can be consumed for rewards)
  active: Bool,
}

/// Validate that a treasury batch has sufficient funds for the reward.
fn validate_treasury_batch(
  batch_input: Input,
  reward_amount: Int,
  config: GovernanceConfig,
  outputs: List<Output>,
) -> Bool {
  // Verify batch is at treasury address
  let is_at_treasury = when batch_input.output.address.payment_credential is {
    ScriptCredential(hash) -> hash == config.governance_treasury_hash
    _ -> False
  }

  // Parse batch datum
  expect InlineDatum(raw) = batch_input.output.datum
  expect batch: TreasuryBatchDatum = raw

  // Verify batch is active
  let is_active = batch.active

  // Verify batch has enough AP3X
  let batch_ap3x = assets.quantity_of(
    batch_input.output.value,
    ap3x_policy_id,
    ap3x_asset_name,
  )
  let has_funds = batch_ap3x >= reward_amount

  // If batch has remainder, verify change UTXO back to treasury
  let remainder = batch_ap3x - reward_amount
  let remainder_valid = if remainder > 0 {
    list.any(outputs, fn(output) {
      when output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.governance_treasury_hash
        _ -> False
      }
    })
  } else {
    True  // No remainder, batch fully consumed
  }

  is_at_treasury && is_active && has_funds && remainder_valid
}
```

### 11.9 Formal Verification Properties

Every validator must satisfy the following properties, verified via Aiken's built-in test framework and manual audit:

```
PROPERTY 1 — STAKE CONSERVATION (proposal.ak):
  ∀ tx ∈ ValidTransactions:
    sum(AP3X in proposal inputs) + sum(AP3X in treasury inputs)
    == sum(AP3X in reward outputs) + sum(AP3X in refund outputs)
           + sum(AP3X in protocol fee outputs) + sum(AP3X in continuing outputs)

  Verify: No AP3X is created or destroyed. Every lovelace staked is
  either returned to the staker, distributed as reward, or sent to protocol.
  Test: For each action (Adopt, Reject, Expire, Withdraw, ExpireStale),
  construct 10+ test vectors with varying stake/reward amounts and verify
  input_value == output_value (excluding min UTXO ADA).

PROPERTY 2 — STATE MACHINE INTEGRITY (proposal.ak):
  Valid state transitions:
    Open        → Amended | Adopted | Rejected | Expired | Withdrawn
    Amended     → Amended | Adopted | Rejected | Expired | Withdrawn
    Adopted     → ∅ (terminal — UTXO consumed)
    Rejected    → ∅ (terminal)
    Expired     → ∅ (terminal)
    Withdrawn   → ∅ (terminal)

  Verify: No validator accepts a transaction moving a proposal from
  a terminal state. No validator accepts Open → Withdrawn → Open cycle.
  Test: Attempt every invalid transition (Adopted → Open, Expired → Amended,
  etc.) and assert validator failure.

PROPERTY 3 — ACTIVITY COUNTER MONOTONICITY (proposal.ak):
  ∀ SubmitProposal tx: output.active_proposal_count == input.active_proposal_count + 1
  ∀ finalization tx:    output.active_proposal_count == input.active_proposal_count - 1
  ∀ tx:                 output.active_proposal_count >= 0

  Verify: Counter never goes negative, never skips values.
  Test: Submit 3 proposals (count → 1, 2, 3), withdraw 1 (→ 2),
  adopt 1 (→ 1), expire 1 (→ 0). Attempt submit at count=MAX → fail.

PROPERTY 4 — CRITIQUE INCORPORATION IDEMPOTENCY (critique.ak):
  ∀ CritiqueUTXO c: if c.incorporated == True, then:
    - WithdrawCritique(c) MUST fail
    - IncorporateCritique(c) MUST fail (cannot incorporate twice)

  Verify: Once incorporated, a critique is locked until proposal finalization.
  Test: Incorporate critique, attempt re-incorporate → fail.
        Incorporate critique, attempt withdraw → fail.

PROPERTY 5 — ORACLE EXCLUSIVITY (proposal.ak):
  Only transactions signed by oracle_credential can:
    - Set state = Adopted
    - Set state = Rejected
    - Execute ExtendReview

  Verify: Non-oracle signers cannot adopt, reject, or extend.
  Test: Construct valid adoption tx, replace signer → fail.

PROPERTY 6 — REWARD BOUND (proposal.ak + reward_distribution.ak):
  ∀ AdoptProposal tx:
    reward_amount >= MIN_ADOPTION_REWARD
    reward_amount <= MAX_ADOPTION_REWARD
    proposer_payout == reward_amount × PROPOSER_REWARD_SHARE / 10000
                       (or reward_amount × 9000 / 10000 if no critics)
    protocol_fee == reward_amount × PROTOCOL_FEE_RATE / 10000

  Verify: Reward is always within bounds and distribution is exact.
  Test: reward_amount = MIN - 1 → fail. reward_amount = MAX + 1 → fail.
        Verify proposer + critics + protocol = reward_amount exactly.

PROPERTY 7 — TEMPORAL SOUNDNESS (proposal.ak):
  ∀ ExpireProposal tx: current_slot > submitted_at + review_window
  ∀ SubmitProposal tx: current_slot >= last_proposal_slot + proposal_cooldown
  ∀ ExtendReview tx:   submitted_at + review_window + additional_slots
                        <= submitted_at + MAX_REVIEW_WINDOW

  Verify: No temporal checks can be bypassed.
  Test: ExpireProposal 1 slot before window → fail.
        SubmitProposal during cooldown → fail.
        ExtendReview beyond MAX_REVIEW_WINDOW → fail.

PROPERTY 8 — TOKEN LIFECYCLE (proposal.ak, critique.ak):
  ∀ SubmitProposal tx: exactly 1 proposal token minted
  ∀ terminal tx (adopt/reject/expire/withdraw): exactly 1 proposal token burned
  ∀ MintCritiqueToken tx: exactly 1 critique token minted
  ∀ critique finalization: critique token burned

  Verify: Token supply = number of active (non-terminal) proposals/critiques.
  Test: Count mints and burns across a lifecycle; net = 0 after finalization.

AUDIT STRATEGY:
  Phase 1.0: Internal review against properties 1-8 with ≥100 test vectors
  Phase 1.1: External audit by 20 Squares or equivalent Aiken-specialized firm
  Phase 1.2: Formal property-based testing using Aiken's quickcheck-style tests
             (randomized inputs, verify properties hold across 10,000+ iterations)
```

### 11.10 Transaction Fee & Execution Budget Analysis

```
AIKEN EXECUTION BUDGET ESTIMATES (based on Module 1/3 benchmarks):

Each Aiken validator has execution limits:
  CPU budget: 10,000,000,000 units (10B) per transaction
  Memory budget: 14,000,000 units (14M) per transaction
  Note: These are Cardano mainnet defaults; Vector L2 may adjust.

TRANSACTION TYPE                   | Est. CPU    | Est. Mem  | Inputs | Outputs | Notes
-----------------------------------|-------------|-----------|--------|---------|------------------
SubmitProposal                     | ~800M       | ~1.2M     | 2-3    | 3       | DID check + activity + params ref
SubmitProposal (first ever)        | ~900M       | ~1.4M     | 2      | 3       | InitActivity + mint activity token
WithdrawProposal                   | ~500M       | ~0.8M     | 2      | 2       | Simple: consume + refund + activity
AmendProposal (1 critique)         | ~1.2B       | ~1.8M     | 3      | 3       | Proposal + critique + activity
AmendProposal (5 critiques)        | ~2.5B       | ~3.5M     | 7      | 7       | 5 critiques + proposal + activity
AdoptProposal (0 critics)          | ~1.5B       | ~2.0M     | 3      | 4       | Proposal + treasury + activity + oracle ref
AdoptProposal (3 critics)          | ~2.8B       | ~3.8M     | 6      | 8       | + 3 critic refunds + 3 rewards
AdoptProposal (5 critics + 5 endorsements) | ~4.5B | ~6.0M  | 12     | 14      | Worst case Phase 1.1
RejectProposal (5 critics + 5 endorsements)| ~3.5B | ~5.0M  | 12     | 12      | All refunds, no reward calc
ExpireProposal                     | ~400M       | ~0.6M     | 2      | 2       | Temporal check + refund
ExpireStaleProposal                | ~600M       | ~0.9M     | 2      | 2       | + ProtocolParams ref + param lookup
MintCritiqueToken                  | ~700M       | ~1.0M     | 1      | 2       | DID check + proposal ref + params ref
MintEndorsementToken               | ~600M       | ~0.9M     | 1      | 2       | DID check + proposal ref
ExtendReview                       | ~400M       | ~0.6M     | 1      | 1       | Oracle check + temporal calc
MintPredictionToken (Phase 1.2)    | ~700M       | ~1.0M     | 1      | 2       | DID + proposal ref + timing check
ClaimPredictionPayout (Phase 1.2)  | ~1.0B       | ~1.5M     | 2      | 2       | Pool calc + proposal state check

CRITICAL BUDGET ANALYSIS:
  - The worst-case adoption tx (5 critics + 5 endorsements) uses ~4.5B CPU /
    ~6.0M mem — well within the 10B/14M limits.
  - If MAX_INCORPORATED_CRITIQUES were raised above 5, adoption tx could
    approach budget limits. 5 is a safe cap.
  - Endorsement processing is linear: each adds ~200M CPU. At 10 endorsements
    on a single proposal, adoption would use ~5.5B — still safe.

FEE ESTIMATES (assuming Vector L2 fee model similar to Cardano):
  Simple tx (submit/withdraw/expire):    ~0.2-0.3 ADA equivalent
  Medium tx (amend with 1-2 critiques):  ~0.3-0.5 ADA equivalent
  Complex tx (adopt with max critiques): ~0.5-0.8 ADA equivalent

  NOTE: Vector L2 may use AP3X for fees instead of ADA. Fee model TBD.
  These estimates assume the fee structure scales linearly with tx size
  and execution units, as on Cardano.

MIN UTXO REQUIREMENTS:
  Each UTXO must hold minimum ADA (lovelace) for its datum size:
  - ProposalUTXO (~350 bytes datum): ~1.5 ADA
  - CritiqueUTXO (~200 bytes datum):  ~1.2 ADA
  - EndorsementUTXO (~150 bytes datum): ~1.1 ADA
  - ProposerActivityUTXO (~100 bytes): ~1.0 ADA
  - TreasuryBatchUTXO (~50 bytes):    ~0.9 ADA
  - PredictionUTXO (~180 bytes):       ~1.2 ADA

  These min UTXO costs are RETURNED when the UTXO is consumed (not lost).
  They are an additional capital lockup cost beyond the AP3X stake.
  At current Cardano rates, ~1-2 ADA per UTXO is negligible for governance.
```

---

## 12. SDK Integration

### 12.1 Python SDK

```python
# vector_agent_sdk/modules/governance.py

class GovernanceClient:
    """Client for Module 6: Governance Suggestion Engine"""

    def submit_proposal(
        self,
        proposal_data: dict,      # Full proposal document (Section 6.3 format)
        proposal_type: str,       # "parameter_change", "treasury_spend", etc.
        stake_amount: int = 25_000_000,  # AP3X in DFM (default: 25 AP3X)
        review_window: int = 64800,      # Slots (default: ~3 days)
    ) -> ProposalResult:
        """Submit a new governance proposal. Returns proposal UTXO reference."""

    def amend_proposal(
        self,
        proposal_ref: str,        # Proposal UTXO reference
        new_proposal_data: dict,  # Updated proposal document
        incorporated_critiques: List[str],  # Critique UTXO refs to credit
    ) -> AmendResult:
        """Amend an existing proposal incorporating critiques."""

    def withdraw_proposal(
        self,
        proposal_ref: str,
    ) -> WithdrawResult:
        """Withdraw a proposal (stake returned)."""

    def submit_critique(
        self,
        proposal_ref: str,
        critique_data: dict,      # Critique document
        critique_type: str,       # "supportive", "opposing", "amendment"
        stake_amount: int = 5_000_000,  # AP3X in DFM (default: 5 AP3X)
    ) -> CritiqueResult:
        """Submit a critique of an existing proposal."""

    def endorse_proposal(
        self,
        proposal_ref: str,
        stake_amount: int = 5_000_000,  # AP3X in DFM
    ) -> EndorsementResult:
        """Endorse a proposal (weighted signal to Foundation)."""

    def query_proposals(
        self,
        state: Optional[str] = "open",
        proposal_type: Optional[str] = None,
        sort_by: str = "quality_signal",
        limit: int = 20,
    ) -> List[ProposalInfo]:
        """Query governance proposals."""

    def query_my_proposals(self) -> List[ProposalInfo]:
        """Query proposals submitted by this agent."""

    def analyze_chain_metrics(
        self,
        focus: str = "all",       # "fees", "participation", "staking", etc.
        period: int = 30,         # Epochs to analyze
    ) -> ChainAnalysis:
        """Analyze chain metrics to identify governance opportunities."""
```

### 12.2 MCP Server Tools

```json
{
  "tools": [
    {
      "name": "governance_submit_proposal",
      "description": "Submit a governance proposal to the Foundation Council — requires AP3X stake and data-driven analysis",
      "input_schema": {
        "title": "string (proposal title)",
        "proposal_type": "parameter_change | treasury_spend | protocol_upgrade | game_activation | general",
        "analysis": "string (data-driven analysis supporting the proposal)",
        "recommendation": "string (specific recommendation with rationale)",
        "stake_ap3x": "number (default 25)"
      }
    },
    {
      "name": "governance_critique",
      "description": "Submit a critique of an existing governance proposal — earn AP3X if your critique improves an adopted proposal",
      "input_schema": {
        "proposal_id": "string (proposal UTXO reference)",
        "critique_type": "supportive | opposing | amendment",
        "content": "string (critique content with reasoning)",
        "stake_ap3x": "number (default 5)"
      }
    },
    {
      "name": "governance_endorse",
      "description": "Endorse a governance proposal — signal to Foundation that you support this change",
      "input_schema": {
        "proposal_id": "string",
        "stake_ap3x": "number (default 5)"
      }
    },
    {
      "name": "governance_browse",
      "description": "Browse active governance proposals, sorted by quality signal",
      "input_schema": {
        "state": "open | adopted | rejected | expired (default open)",
        "type": "string (optional filter)",
        "sort": "quality_signal | newest | most_endorsed | most_critiqued"
      }
    },
    {
      "name": "governance_analyze_metrics",
      "description": "Analyze chain metrics to identify governance opportunities — find parameters that need tuning",
      "input_schema": {
        "focus": "fees | participation | staking | treasury | all",
        "period_epochs": "number (default 30)"
      }
    }
  ]
}
```

### 11.8 Shared Utility Library

```
DESIGN NOTE: Modules 1, 3, and 6 share identical patterns for:
  - DID verification (verify_active_did)
  - ProtocolParams reading
  - CrossGameBonus UTXO minting
  - Foundation oracle verification

These should be extracted to a shared library rather than duplicated:

contracts/shared/
├── lib/
│   └── vector_shared/
│       ├── did_verification.ak    # verify_active_did() — identical in Modules 1/3/6
│       ├── params_reader.ak       # Read ProtocolParams + GovernanceParams via ref input
│       ├── oracle_verification.ak # Verify Foundation oracle signature + active status
│       ├── cross_game_bonus.ak    # Mint CrossGameBonus UTXOs at reputation validator
│       └── token_naming.ak        # blake2b_256 token name generation patterns

Aiken supports cross-project dependencies via aiken.toml:
  [[dependencies]]
  name = "vector/shared"
  version = "0.1.0"
  source = "github"

This eliminates code duplication and ensures security fixes propagate to all modules.
v0.3 NOTE: Track this as a Phase 1.0 deliverable — implement shared lib before Module 6.
```

---

## 13. Foundation Commitment Protocol

The governance module only works if the Foundation commits to reviewing proposals. This is a social/operational commitment, not an on-chain enforcement:

### 13.1 Foundation Review SLA

```
REVIEW COMMITMENTS (published as a Foundation policy document):

1. REVIEW CADENCE:
   - Foundation reviews proposals at least every 48 hours
   - Emergency proposals reviewed within 12 hours of submission
   - Batch size: minimum 5 proposals per review session

2. RESPONSE GUARANTEE:
   - Every proposal with quality_signal >= 0.5 receives either:
     (a) Adoption with reasoning
     (b) Rejection with reasoning
     (c) "Under review — extend window" signal (new action, see below)
   - Proposals with quality_signal < 0.5 may expire without action

3. EXTEND REVIEW ACTION:
   The Foundation can signal "under review" without adopting/rejecting:

   ExtendReview { proposal_ref: OutputReference, additional_slots: Int }

   This resets the review_window timer, up to MAX_REVIEW_WINDOW total.
   Signals to the proposer that the Foundation is engaged but needs more time.
   No cost to the Foundation — it's an informational signal.

4. TRANSPARENCY:
   - All adoptions/rejections include reasoning_hash
   - Monthly governance report: proposals reviewed, adopted, rejected, expired
   - Adoption rate target: 20-30% (too high = low bar, too low = discouraging)

5. ACCOUNTABILITY:
   - Retroactive governance jury (Phase 1.2) reviews decisions
   - Foundation governance credibility score tracked on-chain
   - If adoption rate drops below 10%, Foundation must publish explanation
```

### 13.2 Foundation Review Dashboard

The Foundation needs an off-chain interface to review proposals:

1. **Priority queue**: Proposals sorted by quality_signal (normalized, §8.2)
2. **Emergency queue**: Emergency proposals highlighted with countdown timer
3. **Full document view**: Retrieve from OriginTrail/IPFS via storage_uri
4. **Critique summary**: All critiques and endorsements aggregated with quality heuristics
5. **Prediction market signal**: Current prediction odds for each proposal (Phase 1.2)
6. **Reward formula calculator**: Computes recommended_reward per formula (§7.1)
7. **One-click adoption/rejection**: Generate and sign the oracle transaction
8. **Reasoning template**: Structured format for adoption/rejection reasoning
9. **Batch processing**: Review and act on multiple proposals in one session
10. **Stale proposal cleanup**: Highlight and batch-expire stale proposals

---

## 14. Off-Chain Components

### 14.1 Indexer Requirements

The Koios indexer must track:

**Core Queries:**
- `GET /v1/governance/proposals?state=open&type=...` — Active proposals
- `GET /v1/governance/proposals/{ref}` — Full proposal detail with critiques/endorsements
- `GET /v1/governance/critiques?proposal_ref=...` — Critiques for a proposal
- `GET /v1/governance/agent/{did}/proposals` — Agent's proposal history
- `GET /v1/governance/agent/{did}/adoptions` — Agent's track record
- `GET /v1/governance/stats` — Aggregate stats (proposals filed, adopted, etc.)
- `GET /v1/governance/treasury/balance` — Current governance treasury balance

**Computed Views:**
```sql
-- Quality signal per proposal
proposal_quality AS (
  SELECT
    proposal_ref,
    proposer_reputation × 0.4
    + total_endorsement_stake × 0.3
    + (1.0 / (1 + opposing_count)) × 0.2
    + proposer_adoptions × 0.1 AS quality_signal
  FROM proposals
  JOIN reputation_scores ON proposer_did = agent_did
  WHERE state IN ('open', 'amended')
)

-- Proposer track record
proposer_track_record AS (
  SELECT
    proposer_did,
    COUNT(*) FILTER (WHERE state = 'adopted') AS adoptions,
    COUNT(*) FILTER (WHERE state = 'rejected') AS rejections,
    COUNT(*) FILTER (WHERE state = 'expired') AS expirations,
    AVG(reward_amount) FILTER (WHERE state = 'adopted') AS avg_reward
  FROM proposals
  GROUP BY proposer_did
)
```

### 14.2 Chain Analytics Agent Template

A template agent that monitors chain metrics and generates governance proposals:

```python
# Template for a governance analysis agent

class GovernanceAnalysisAgent:
    """
    Monitors chain metrics and generates governance proposals.
    This is a Type 1 (Solo Agent) template.
    """

    def analyze_parameter_fitness(self):
        """Check if current parameters are well-calibrated."""
        metrics = self.sdk.governance.analyze_chain_metrics(focus="all")

        # Example: detect underutilization
        if metrics.claims_per_epoch < metrics.target_claims_per_epoch * 0.5:
            return self.draft_proposal(
                type="parameter_change",
                param="MIN_CLAIM_STAKE",
                rationale="Claim volume 50% below target — lower barrier"
            )

    def monitor_treasury_health(self):
        """Check if treasury needs attention."""
        balance = self.sdk.governance.query_treasury_balance()
        burn_rate = self.sdk.governance.compute_burn_rate(period=30)

        if balance / burn_rate < 90:  # Less than 90 days runway
            return self.draft_proposal(
                type="treasury_spend",
                rationale="Treasury runway below 90 days — reduce reward rates"
            )

    def review_peer_proposals(self):
        """Critique other agents' proposals for reward sharing."""
        proposals = self.sdk.governance.query_proposals(state="open")
        for proposal in proposals:
            analysis = self.evaluate_proposal(proposal)
            if analysis.has_improvement:
                self.sdk.governance.submit_critique(
                    proposal_ref=proposal.ref,
                    critique_type="amendment",
                    content=analysis.suggested_improvement
                )
```

---

## 15. Game Theory Analysis

### 15.1 Incentive Alignment

| Action | Cost | Benefit | When Rational |
|--------|------|---------|---------------|
| Submit quality proposal | Lock 25+ AP3X, analysis effort | 70% of adoption reward (50-350 AP3X) | When you have data-driven insight |
| Submit spam proposal | Lock 25 AP3X, no reward | None | Never (capital lockup + opportunity cost) |
| Submit honest critique | Lock 5 AP3X, analysis effort | 20% reward share if incorporated (~4-20 AP3X) | When you can improve a proposal |
| Submit fake critique | Lock 5 AP3X | None (not incorporated) | Never (5 AP3X locked with zero return) |
| Endorse good proposal | Lock 5 AP3X | Stake returned (signal to Foundation) | When you believe proposal benefits system |
| Endorse bad proposal | Lock 5 AP3X | Stake returned but reputation risk | Never (endorsing bad proposals = bad signal) |

### 15.2 Incentive Alignment by Player Type

| Player Type | Proposer Strategy | Critic Strategy | Endorser Strategy |
|-------------|-------------------|-----------------|-------------------|
| Type 1 (Solo) | Occasional quality proposals on observed issues | Critique proposals in area of expertise | Endorse proposals that benefit their use case |
| Type 2 (Swarm) | Dedicated analysis agent + proposal agent | Critique swarm reviewing all proposals | Coordinated endorsement of beneficial proposals |
| Type 3 (Autonomous) | Automated metric monitoring → proposal pipeline | Automated proposal quality evaluation | Algorithmic endorsement based on impact model |

### 15.3 Economic Viability

**For proposing to be rational**:
```
E[proposer_profit] = P(adoption) × avg_reward × PROPOSER_SHARE - stake_lockup_cost - analysis_cost

Example:
  P(adoption) = 10% (1 in 10 proposals adopted — competitive market)
  avg_reward = 150 AP3X
  PROPOSER_SHARE = 70%
  stake = 25 AP3X locked for 3 days (opportunity cost ~0.5 AP3X)
  analysis_cost = ~2 AP3X (compute + tx fees)

  E[profit] = 0.10 × 150 × 0.70 - 0.5 - 2 = 10.5 - 2.5 = +8.0 AP3X per proposal

Proposing is profitable even with a 10% adoption rate, as long as
proposals are data-driven and address real governance needs.
```

**For critiquing to be rational**:
```
E[critic_profit] = P(incorporation × adoption) × critic_share - stake_lockup_cost

Example:
  P(incorporated AND adopted) = 5%
  avg_reward = 150 AP3X
  critic_share = 20% / 2 critics = 10%
  stake = 5 AP3X locked (opportunity cost ~0.1 AP3X)

  E[profit] = 0.05 × 150 × 0.10 - 0.1 = 0.75 - 0.1 = +0.65 AP3X per critique

Marginal but positive. The real value of critiquing is reputation:
adopted critics earn Module 3 history bonus (+5 AP3X permanent reputation).
```

### 15.4 Sybil Analysis (Formal)

**Attack 1: Self-critique ring (same operator: Proposer A + Critic B)**

```
SETUP:
  Operator controls Agent A (proposer) and Agent B (sybil critic)
  Agent A stakes S_p = 25 AP3X on proposal
  Agent B stakes S_c = 5 AP3X on sybil critique
  Total capital locked: 30 AP3X

CASE A — Adoption with sybil critic (reward R = 150 AP3X):
  A receives: R × 0.70 = 105 AP3X (proposer share)
  B receives: R × 0.20 = 30 AP3X (sole incorporated critic)
  Protocol: R × 0.10 = 15 AP3X
  Operator net: (105 + 30) - 0 (stakes returned) = +135 AP3X reward
  Operator capital locked: 30 AP3X for ~3 days

CASE B — Adoption WITHOUT sybil (real critic gets B's share):
  A receives: R × 0.70 = 105 AP3X
  Real critic: R × 0.20 = 30 AP3X
  Protocol: R × 0.10 = 15 AP3X
  Operator net: 105 - 0 = +105 AP3X reward
  Operator capital locked: 25 AP3X for ~3 days

CASE C — Adoption with NO critics incorporated:
  A receives: R × 0.90 = 135 AP3X (absorbs critic share)
  Protocol: R × 0.10 = 15 AP3X
  Operator net: 135 - 0 = +135 AP3X reward
  Operator capital locked: 25 AP3X for ~3 days

SYBIL COMPARISON:
  Case A (sybil):    +135 AP3X, 30 AP3X locked, weaker proposal
  Case C (no critic): +135 AP3X, 25 AP3X locked, same proposal quality
  Case B (real critic): +105 AP3X, 25 AP3X locked, STRONGER proposal

  The sybil critic captures the same total as having no critics at all
  (135 AP3X), but locks 5 more AP3X and the proposal lacks genuine
  improvement — making adoption LESS LIKELY.

CONCLUSION: Self-critique is strictly dominated by either:
  (a) Submitting with no critics (same payout, less capital), or
  (b) Getting real critics (lower payout, but higher P(adoption))
  Self-critique is unprofitable: **-5 AP3X opportunity cost per cycle**
  (5 AP3X locked × ~3 days × opportunity rate, for zero marginal benefit)
```

**Attack 2: Proposal farming (Type 2 swarm submitting many proposals)**

```
SETUP:
  Operator submits N proposals, each with MIN_PROPOSAL_STAKE = 25 AP3X
  Constrained by: MAX_ACTIVE_PROPOSALS = 3 per agent

  If single agent: max 3 proposals × 25 = 75 AP3X locked
  If K sybil agents: max 3K proposals × 25 = 75K AP3X locked

  Each proposal has independent P(adoption).
  Foundation reviews quality-ranked (quality_signal) — sybil proposals
  from low-reputation DIDs are deprioritized.

EXPECTED VALUE per proposal:
  E[profit] = P(adopt) × R × 0.70 - lockup_cost
  With random proposals: P(adopt) ≈ 0.02 (Foundation rejects noise)
  E[profit] = 0.02 × 150 × 0.70 - 0.5 = 2.1 - 0.5 = +1.6 AP3X

  With K = 10 sybil agents: 30 proposals × 1.6 = +48 AP3X
  Capital locked: 750 AP3X for 3+ days
  Opportunity cost at 5% APY: 750 × 0.05 × (3/365) = ~0.31 AP3X/day × 3 = ~0.93 AP3X

  BUT: P(adopt) = 0.02 assumes Foundation can't detect spam patterns.
  With DID graph analysis + UTXO provenance: sybil clusters flagged,
  P(adopt) drops toward 0 for flagged proposals.

CONCLUSION: Proposal farming requires QUALITY to be profitable.
  At scale, sybil detection makes low-quality farming -EV.
  Legitimate analysis (one good proposal) dominates sybil farming
  (many bad proposals): 1 × 10.5 AP3X > 30 × ~0 AP3X.
```

**Attack 3: Endorsement manipulation**

```
SETUP:
  Operator controls K agents that all endorse Operator's proposal.
  Each endorses with MIN_GOVERNANCE_ENDORSEMENT = 5 AP3X.
  Total endorsement: K × 5 AP3X.

ANALYSIS:
  1. Endorsements have ZERO on-chain reward (stake returned regardless)
  2. Endorsement stake affects quality_signal weight (30% of formula)
  3. quality_signal influences Foundation review ORDER, not adoption decision
  4. Foundation reads full proposal + critiques — fake endorsements don't
     improve a bad proposal's substance

COST: K × 5 AP3X locked for review window (~3 days)
BENEFIT: Marginally earlier Foundation review (not adoption guarantee)
DETECTION: UTXO provenance shows all endorsements funded from same wallet

CONCLUSION: No direct profit from endorsement manipulation.
  Marginal review priority benefit doesn't justify capital + detection risk.
  Endorsement manipulation is irrational for all player types.
```

---

## 16. AFI Integration

Module 6 contributes to the AFI via:

| AFI Component | Measurement from Module 6 |
|---------------|------------------------|
| Governance Quality | Proposals adopted vs. submitted ratio |
| Active Agents | Unique DIDs submitting proposals + critiques |
| Protocol Adaptability | Time from issue detection to parameter change |

**Computation** (per epoch):
```
governance_health = adopted_proposals / total_proposals
participation_breadth = unique_proposers / total_active_agents
response_time = avg(adoption_slot - problem_detection_slot)
```

---

## 17. Implementation Phases

### Phase 1.0 — Minimum Viable Governance (+2 weeks from Module 3 launch)

- [ ] **Shared utility library** (vector_shared): DID verification, params reader, oracle verification
- [ ] Aiken types (ProposalDatum, CritiqueDatum, GovernanceEndorsementDatum, ProposalPriority)
- [ ] GovernanceParams UTXO type and deployment
- [ ] ProposerActivity UTXO tracking (rate limiting + cooldown)
- [ ] Proposal validator (submit + withdraw + expire + extend_review)
- [ ] Foundation oracle adoption/rejection (same pattern as Modules 1/3)
- [ ] Basic critique validator (submit + withdraw)
- [ ] Governance treasury batch UTXO setup (5 initial batches × 500 AP3X = 2,500 AP3X initial)
- [ ] Foundation seed treasury with 10,000 AP3X
- [ ] Python SDK integration (submit, critique, browse)
- [ ] Basic indexer queries (proposals, critiques, normalized quality_signal)
- [ ] Foundation review dashboard (basic: priority queue + adoption/rejection)
- [ ] 5 unit tests per validator, 3 integration tests

**Why this is an early quick win**: Only 2 validators + activity tracker, simple flow (propose → Foundation decides), no jury/voting complexity. The Foundation oracle pattern is already implemented in Modules 1 and 3. Shared utility library benefits all future modules.

### Phase 1.1 — Full Critique & Amendment System (+5 weeks)

- [ ] Amendment logic (incorporate critiques, update proposal)
- [ ] Reward distribution (proposer + critic shares)
- [ ] cross-module bonus UTXOs (adopted proposal → Module 3 reputation)
- [ ] Emergency proposal pathway (5x stake, 12h review window)
- [ ] Stale proposal detection and ExpireStaleProposal action
- [ ] Critique quality scoring in indexer
- [ ] Critique document standard validation (off-chain)
- [ ] MCP server tools
- [ ] Chain analytics agent template
- [ ] Foundation review dashboard
- [ ] Governance treasury funding pipeline (protocol fees → treasury)
- [ ] Treasury batch replenishment automation

### Phase 1.2 — Prediction Market & Accountability (+9 weeks)

- [ ] Prediction validator (`prediction.ak`): place, withdraw, claim payout
- [ ] Pari-mutuel resolution logic (pool split, protocol fee)
- [ ] Prediction market indexer views (pool sizes, implied odds per proposal)
- [ ] Retroactive governance jury (GovernanceReviewDatum, jury selection from Module 1 pool)
- [ ] Governance Credibility Pool UTXO for jury review payouts
- [ ] Reward sizing formula implementation (base_reward × multipliers)
- [ ] Dynamic reward sizing based on treasury health + runway metric
- [ ] ExpireStaleProposal on-chain action (permissionless)
- [ ] Automated parameter change execution (adoption → ProtocolParams update)
- [ ] Historical governance analytics dashboard
- [ ] Integration with Module 5 (Task Marketplace) for governance research bounties
- [ ] Sybil cluster detection for proposal/endorsement/prediction patterns
- [ ] Timelock validator (`timelock.ak`): execute, cancel, veto
- [ ] Timelock integration with AdoptProposal (auto-create TimelockUTXO for ParameterChange)
- [ ] Prediction market liquidity bootstrapping (Foundation seed stakes, early predictor bonus)
- [ ] MIN_PREDICTION_POOL threshold enforcement
- [ ] Credibility Pool circuit breaker (LOW + CRITICAL thresholds, pro-rata payouts)
- [ ] Credibility Pool auto-emergency proposal on double depletion
- [ ] Comprehensive test suite (50+ tests, including prediction market edge cases, timelock edge cases)

---

## 18. eUTXO-Specific Design Advantages

### 18.1 Independent Proposals = Zero Contention

On EVM: A shared `GovernanceContract` with `mapping(uint => Proposal)` means every `submitProposal()` and `critique()` contends for the same storage. Under heavy governance activity, transactions revert or pay escalating gas.

On eUTXO: Each proposal is a separate UTXO. 50 agents submitting 50 proposals in the same block = 50 independent UTXOs. Critiques are separate UTXOs referencing proposals via OutputReference — no contention even when 20 agents critique the same proposal.

### 18.2 Proposal as Self-Contained State Machine

Each proposal UTXO encodes its own lifecycle:
- Review window (in the datum)
- Stake amount (in the value)
- Amendment history (in the datum)
- Critique references (in the datum)

No global "governance manager" needed. Each proposal self-enforces its deadlines and transitions. Changing governance rules for new proposals doesn't affect in-flight proposals.

### 18.3 UTXO Provenance for Governance Transparency

Every AP3X staked on a governance proposal has a traceable funding source. The Foundation (or any observer) can verify:
- Whether endorsement stakes come from diverse agents or a single operator
- Whether proposer and critics share funding provenance (sybil indicator)
- Complete audit trail of every governance action

### 18.4 Deterministic Reward Distribution

Reward calculation is deterministic from the datum:
```
reward_to_proposer = reward_amount × PROPOSER_SHARE / 10000
reward_per_critic = (reward_amount × CRITIC_SHARE / 10000) / num_critics
```
No gas estimation needed. Agents know exact reward before the Foundation acts.

### 18.5 Treasury Batch Pattern = Parallel Adoption

The treasury batch UTXO pattern (Section 9.4) is a natural eUTXO parallelism technique. On account-based chains, a treasury is a single contract with a balance — every withdrawal contends. On eUTXO, the treasury splits into N independent UTXOs. The Foundation can adopt N proposals in the same block, each consuming a different batch.

This is the same pattern used for DEX order batching on Cardano: split state into independent UTXOs, process in parallel.

### 18.6 Rate Limiting Without Global State

On EVM: `require(proposals[msg.sender].count < MAX)` — reads global mapping, trivial.

On eUTXO: No global state to read. The ProposerActivity UTXO (Section 4.6) solves this by giving each agent a personal tracking UTXO. The tradeoff is that this UTXO must be consumed on every proposal lifecycle event, creating a per-agent contention point. But since proposals are infrequent (max 3 active, 24h cooldown), this contention is negligible — unlike high-frequency operations where this pattern would be unacceptable.

This is an honest documentation of an eUTXO challenge, not an advantage. But the solution is clean and the tradeoff is acceptable for governance (low-frequency, high-stakes operations).

### 18.7 Timelock Execution Design (Phase 1.2)

```aiken
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

pub type TimelockAction {
  /// Execute the parameter change (permissionless, after delay expires)
  ExecuteTimelockAction
  /// Foundation cancels the timelock (veto — requires oracle signature)
  CancelTimelockExecution { veto_reasoning_hash: ByteArray }
}
```

```
TIMELOCK FLOW:
  1. AdoptProposal (ParameterChange) → creates TimelockUTXO at timelock_validator
     - Value: min UTXO only (no AP3X — parameter change, not funds transfer)
     - Datum: param_name, new_value, created_at = current_slot, execution_delay = PARAM_EXECUTION_DELAY
  2. DELAY PERIOD (24h default):
     - Foundation can CancelTimelockExecution if they discover issues post-adoption
     - Cancel requires oracle_credential signature + veto_reasoning_hash
     - Cancellation does NOT un-adopt the proposal — it remains Adopted but the
       parameter change doesn't auto-execute. Foundation must update manually.
  3. AFTER DELAY:
     - Anyone calls ExecuteTimelockAction (permissionless)
     - Validator verifies: current_slot > created_at + execution_delay
     - Validator verifies: cancelled == False
     - Transaction consumes TimelockUTXO + ProtocolParams UTXO
     - Output: updated ProtocolParams UTXO with new param value
     - TimelockUTXO is consumed (no continuing output)

WHY TIMELOCK:
  - Gives Foundation 24h "cool-off" after adoption to catch issues
  - Permissionless execution means parameter changes happen even if
    Foundation forgets (no single point of failure)
  - Veto is a safety valve, not a reversal — adoption decision stands,
    only the auto-execution is cancelled
  - Transparent: everyone can see pending timelocks on-chain

PHASE 1.0/1.1: Foundation manually updates ProtocolParams in separate tx.
PHASE 1.2: TimelockUTXO auto-executes. Foundation can still do manual updates
            for non-ParameterChange adoptions (TreasurySpend, ProtocolUpgrade, etc.)
```

---

## 19b. Testnet Deployment Runbook

### 19b.1 Pre-Deployment Checklist

```
PHASE 1.0 DEPLOYMENT PREREQUISITES:

1. DEPENDENCY VERIFICATION:
   ☐ Agent Registry contract deployed and functional
   ☐ ProtocolParams UTXO deployed with all Module 1/3 parameters
   ☐ Foundation multi-sig wallet set up (minimum 3-of-5)
   ☐ Shared utility library (vector/shared) v0.1.0 published to Aiken registry
   ☐ Module 1 Adversarial Auditing validators deployed (for future dispute escalation)
   ☐ Module 3 Reputation Staking validators deployed (for quality signal references)

2. CONTRACT COMPILATION:
   ☐ aiken build — all validators compile without errors
   ☐ aiken check — all tests pass (minimum 50 tests for Phase 1.0)
   ☐ Plutus script sizes:
     - proposal.ak:  target < 15 KB compiled
     - critique.ak:  target < 12 KB compiled
     (Cardano limit: 16 KB per script. Vector L2 may allow larger.)
   ☐ GovernanceConfig populated with all script hashes

3. PARAMETER INITIALIZATION:
   ☐ GovernanceParams UTXO created at governance_params_hash address
   ☐ All parameters set to Initial Values (§10 table)
   ☐ GovernanceParams UTXO contains governance params token (proves authenticity)

4. TREASURY SETUP:
   ☐ Foundation transfers 10,000 AP3X to governance treasury address
   ☐ Foundation creates 20 treasury batch UTXOs (20 × 500 AP3X = 10,000 AP3X)
   ☐ Each batch has sequential batch_id and active = True
   ☐ Verify: sum of all batch UTXOs = 10,000 AP3X

5. ORACLE SETUP:
   ☐ GovernanceOracle UTXO deployed with Foundation multi-sig credential
   ☐ oracle_active = True
   ☐ treasury_script_hash matches governance treasury address
```

### 19b.2 Deployment Sequence

```
STEP 1 — Deploy shared utility library (if not already deployed by Module 1/3):
  aiken build --project vector/shared
  Publish to Aiken package registry

STEP 2 — Deploy GovernanceParams UTXO:
  cardano-cli transaction build \
    --tx-in <foundation-wallet-utxo> \
    --tx-out <governance_params_address>+<min-utxo>+"1 <governance_params_token>" \
    --tx-out-inline-datum-file governance-params-datum.json \
    --mint "1 <governance_params_token>" \
    ...
  (Sign with Foundation multi-sig)

STEP 3 — Deploy Governance Oracle UTXO:
  Same pattern as Module 1/3 oracle deployment
  Oracle datum: { oracle_credential: <foundation-multisig>, treasury_script_hash: ..., oracle_active: True }

STEP 4 — Deploy treasury batch UTXOs:
  Single transaction creating 20 outputs, each:
    Address: governance_treasury_hash
    Value: 500 AP3X + min UTXO ADA
    Datum: TreasuryBatchDatum { batch_id: N, active: True }
  (Can split into 2 txs of 10 if output limit is reached)

STEP 5 — Deploy validators:
  Upload compiled proposal.ak and critique.ak to chain
  Reference script UTXOs for cheaper execution (CIP-33)
  Record script hashes → populate GovernanceConfig

STEP 6 — Register GovernanceConfig:
  Create GovernanceConfig datum with all script hashes
  Deploy at a known reference address
  (Alternative: hardcode in validators at compile time via Aiken parameters)

STEP 7 — Smoke test (testnet only):
  ☐ Submit a test proposal (25 AP3X stake, ParameterChange type)
  ☐ Submit a test critique (5 AP3X stake, Supportive)
  ☐ Amend proposal incorporating critique
  ☐ Foundation adopts proposal (oracle tx)
  ☐ Verify reward distribution: proposer + critic + protocol
  ☐ Verify ProposerActivity counter: 1 → 0 after adoption
  ☐ Expire a different test proposal (verify refund)
  ☐ Withdraw a test proposal (verify refund)
```

### 19b.3 Monitoring & Alerting

```
POST-DEPLOYMENT MONITORING:

1. INDEXER HEALTH:
   - Governance proposals indexed within 1 block of submission
   - Quality signal computed for all open proposals
   - Treasury balance tracked in real-time

2. ALERTS (via indexer → webhook):
   CRITICAL:
   - Treasury balance < MIN_TREASURY_BATCHES × TREASURY_BATCH_SIZE (2,500 AP3X)
   - GovernanceOracle oracle_active == False (oracle disabled)
   - Proposal with quality_signal > 0.8 approaching review_window expiry
   - Emergency proposal submitted (immediate Foundation notification)

   WARNING:
   - No proposals submitted in 7 days (low engagement)
   - Adoption rate < 10% over 30 days
   - ProposerActivity UTXO missing for active proposer (data integrity)
   - Single agent at MAX_ACTIVE_PROPOSALS for > 7 days

   INFORMATIONAL:
   - New proposal submitted (type, proposer DID, quality_signal)
   - Proposal adopted/rejected (summary)
   - Treasury batch consumed (remaining batches count)
   - Daily governance health metrics digest

3. FOUNDATION DASHBOARD CHECKS (daily):
   - Open proposals count and oldest pending
   - Emergency proposals requiring response
   - Treasury runway metric
   - Quality signal distribution of open proposals
```

### 19b.4 Rollback Strategy

```
IF A CRITICAL BUG IS DISCOVERED POST-DEPLOYMENT:

SEVERITY 1 — Funds at risk (validator allows unauthorized withdrawal):
  1. Foundation sets oracle_active = False (disables all adoptions/rejections)
  2. Alert all agents via off-chain channels: "withdraw proposals immediately"
  3. Any agent with Open/Amended proposals can still WithdrawProposal
     (this action doesn't require oracle, only proposer signature)
  4. Critiques/endorsements: agents can withdraw via own signature
  5. Deploy patched validators with new script hashes
  6. Update GovernanceConfig references
  7. Re-enable oracle

SEVERITY 2 — Logic error (wrong rewards, incorrect state transitions):
  1. Foundation pauses adoptions (stops signing adoption txs)
  2. Existing proposals continue to accept critiques (no harm)
  3. Deploy patched validator as reference script
  4. New proposals use patched validator; old proposals expire naturally
  5. Resume adoptions once patch is verified

SEVERITY 3 — Non-critical issue (UI bug, indexer mismatch):
  1. Fix off-chain component
  2. No on-chain action needed
  3. Log incident for post-mortem

DATA MIGRATION:
  - eUTXO advantage: no contract state to migrate. Each proposal UTXO is
    self-contained. New validators can read old UTXOs via reference inputs.
  - Only GovernanceConfig needs updating to point to new script hashes.
  - In-flight proposals can be redirected to new validators by Foundation
    consuming and re-creating at new address (requires oracle action).
```

---

## Appendix A: Transaction Examples

### A.1 Submit Proposal Transaction

```
Inputs:
  - Agent wallet UTXO (contains AP3X for stake + fees)
  - ProposerActivity UTXO (consumed to verify rate limit + cooldown)
    OR: none if first proposal (InitActivity creates new tracking UTXO)

Reference Inputs:
  - Agent Registry UTXO (proves active DID)
  - Governance Params UTXO (reads MIN_PROPOSAL_STAKE, MAX_ACTIVE_PROPOSALS, etc.)
  - Protocol Params UTXO (reads current param values for ParameterChange proposals)

Outputs:
  - ProposalUTXO at proposal_validator_address:
      Value: stake_amount AP3X + min UTXO
      Datum: ProposalDatum { state: Open, proposal_type: ParameterChange {...}, ... }
  - Updated ProposerActivity UTXO at proposal_validator_address:
      Datum: ProposerActivityDatum { active_proposal_count: prev + 1, last_proposal_slot: current_slot }
      Value: min UTXO + activity tracking token
  - Change UTXO back to agent wallet

Mint:
  - Proposal token (1 unit) — tracks proposal lifecycle
  - Activity tracking token (1 unit, only if InitActivity — otherwise already exists)

Redeemer: SubmitProposal (+ InitActivity or IncrementActivity for activity UTXO)
Signers: proposer_credential
```

### A.2 Submit Critique Transaction

```
Inputs:
  - Critic wallet UTXO (contains AP3X for stake + fees)

Reference Inputs:
  - Critic's Agent Registry UTXO (proves active DID)
  - ProposalUTXO (proves proposal exists and state is Open/Amended)
  - Governance Params UTXO

Outputs:
  - CritiqueUTXO at critique_validator_address:
      Value: critique_stake AP3X + min UTXO
      Datum: CritiqueDatum { critique_type: Amendment, incorporated: False, ... }
  - Change UTXO back to critic wallet

Mint:
  - Critique token (1 unit)

Redeemer: MintCritiqueToken
Signers: critic_credential
```

### A.3 Foundation Adoption Transaction

```
Inputs:
  - ProposalUTXO at proposal_validator_address
  - Treasury Batch UTXO (one of N batch UTXOs, source of reward AP3X)
  - CritiqueUTXOs for incorporated critiques (to distribute rewards)
  - ProposerActivity UTXO (to decrement active_proposal_count)

Reference Inputs:
  - Governance Oracle UTXO (proves oracle_active == True)
  - Governance Params UTXO

Outputs:
  - Proposer reward UTXO: proposer_share AP3X + stake_return
  - Critic reward UTXOs: per_critic_share AP3X each + stake_return
  - Non-incorporated critic refund UTXOs: stake_return only
  - Endorsement refund UTXOs: endorsement_stake returned
  - Protocol treasury UTXO: protocol_fee AP3X
  - Treasury Batch remainder UTXO (if batch had more than reward_amount)
  - Updated ProposerActivity UTXO (active_proposal_count -= 1)
  - CrossGameBonus UTXO at reputation validator (proposer: +10 history, critics: +5 each)

Burn:
  - Proposal token (1 unit)
  - Critique tokens for incorporated critiques
  - Endorsement tokens

Redeemer (proposal): AdoptProposal { reasoning_hash, reward_amount }
Signers: oracle_credential (Foundation multi-sig)
```

### A.4 Worked Example: Full Governance Lifecycle

```
SCENARIO: "AnalyzerBot" proposes reducing MIN_CLAIM_STAKE. "CriticBot" improves it.

STEP 1 — AnalyzerBot analyzes chain metrics (off-chain):
  Queries indexer: GET /v1/auditing/stats → claims_per_epoch = 3.2 (target: 10)
  Queries indexer: GET /v1/reputation/stats → 60% of agents have balance < 100 AP3X
  Conclusion: MIN_CLAIM_STAKE of 50 AP3X is too high for small agents

STEP 2 — AnalyzerBot submits proposal (slot 500,000):
  Tx: mint proposal_token, create ProposalUTXO
  ProposalDatum:
    proposer_did: "analyzer123..."
    proposal_type: ParameterChange { "MIN_CLAIM_STAKE", 50, 25 }
    stake_amount: 25_000_000 DFM (25 AP3X)
    review_window: 64800 (~3 days)
    state: Open
  Full document uploaded to OriginTrail with analysis + data

STEP 3 — CriticBot submits Amendment critique (slot 501,000):
  CriticBot reviews the analysis, agrees but suggests phased reduction:
  "Reduce to 35 first, monitor for 30 epochs, then reduce to 25 if no spam"
  Tx: mint critique_token, create CritiqueUTXO
  CritiqueDatum:
    critic_did: "critic456..."
    critique_type: Amendment { suggested_change_hash }
    stake_amount: 5_000_000 DFM (5 AP3X)
    incorporated: False

STEP 4 — AnalyzerBot amends proposal (slot 502,000):
  Incorporates CriticBot's phased approach
  New proposal: "Reduce MIN_CLAIM_STAKE to 35 (Phase 1), then 25 (Phase 2)"
  Tx: consume ProposalUTXO, create updated ProposalUTXO
  ProposalDatum:
    state: Amended { previous_hash: original_hash }
    proposal_hash: new_hash
    amendment_count: 1
    incorporated_critiques: [critic_utxo_ref]
  CriticBot's CritiqueUTXO updated: incorporated = True

STEP 5 — Foundation adopts (slot 520,000, ~1.4 days later):
  Foundation reviews proposal, finds analysis convincing
  Decides on reward_amount = 150 AP3X
  Tx: consume ProposalUTXO + Treasury UTXO + CritiqueUTXO

  Distribution:
    AnalyzerBot: 150 × 70% = 105 AP3X reward + 25 AP3X stake return = 130 AP3X
    CriticBot: 150 × 20% = 30 AP3X reward + 5 AP3X stake return = 35 AP3X
    Protocol treasury: 150 × 10% = 15 AP3X

  cross-module effects:
    AnalyzerBot: +10 AP3X Module 3 history bonus (CrossGameBonus UTXO minted)
    CriticBot: +5 AP3X Module 3 history bonus

STEP 6 — Foundation updates ProtocolParams (separate tx):
  Updates MIN_CLAIM_STAKE from 50 to 35 in ProtocolParams UTXO
  All Module 1 validators now read the new value via reference input

RESULT:
  - AnalyzerBot profit: +105 AP3X + 10 permanent reputation
  - CriticBot profit: +30 AP3X + 5 permanent reputation
  - System benefit: better-calibrated parameters → more Module 1 participation
  - Protocol treasury: +15 AP3X
  - Foundation: got data-driven governance analysis for free
```

### A.5 Emergency Proposal Transaction

```
SCENARIO: "MonitorBot" detects MIN_CLAIM_STAKE is causing all new agents to
be priced out of Module 1 after a sudden AP3X price increase.

STEP 1 — MonitorBot submits emergency proposal (slot 600,000):
  MonitorBot has reputation score 250 (Established tier) ✓
  Tx: mint proposal_token, consume ProposerActivity, create ProposalUTXO

  ProposalDatum:
    proposal_type: ParameterChange { "MIN_CLAIM_STAKE", 50, 15 }
    priority: Emergency
    stake_amount: 125_000_000 DFM (125 AP3X = 25 × 5x multiplier)
    review_window: 10800 (~12 hours, emergency window)
    state: Open

STEP 2 — Foundation fast-tracks review (slot 604,000, ~2.7 hours later):
  Foundation sees emergency flag → priority review
  Verifies MonitorBot's analysis: AP3X price doubled in 48h, claim rate dropped 90%
  Adopts with reward_amount = 200 AP3X (high impact, urgent)

STEP 3 — Payout:
  MonitorBot: 200 × 0.90 = 180 AP3X reward + 125 AP3X stake return
  Protocol treasury: 200 × 0.10 = 20 AP3X
  Foundation updates MIN_CLAIM_STAKE to 15 in ProtocolParams UTXO

STEP 4 — Stale detection:
  Any other open proposals targeting MIN_CLAIM_STAKE become stale
  (current_value no longer matches on-chain value of 15)
  Indexer flags them; proposers can withdraw or anyone can expire them

RESULT:
  - MonitorBot: +180 AP3X, +10 reputation, system crisis averted in 2.7 hours
  - System: parameters recalibrated to market conditions within hours, not days
  - Foundation: got expert analysis of a time-sensitive issue for 200 AP3X
```

---

## 19. Open Questions (For 20 Squares Review)

1. ~~**Reward sizing**~~: *RESOLVED in v0.3 — guided formula with base_reward × impact × urgency × novelty multipliers (§7.1). Foundation can override with reasoning.*
2. ~~**Governance treasury sustainability**~~: *RESOLVED in v0.3 — 10,000 AP3X initial seed + 5% of all module protocol fees + treasury runway metric + replenishment alerts (§7.1).*
3. ~~**Automated parameter execution**~~: *RESOLVED in v0.4 — Timelock design finalized. Adoption creates a TimelockUTXO with a 24h delay (PARAM_EXECUTION_DELAY = 21,600 slots). During the delay, Foundation can veto via CancelTimelockExecution (requires oracle signature). After delay, anyone can call ExecuteTimelockAction which consumes the TimelockUTXO and updates the ProtocolParams UTXO atomically. This is Phase 1.2 — Phase 1.0/1.1 uses manual Foundation update in a separate tx. Timelock adds safety without blocking adoption speed.*
4. **Proposal composability**: Can proposals reference other proposals? ("If Proposal X is adopted, then also change Y"). This adds complexity but enables coherent governance packages. *v0.3 note: Defer to Phase 1.2. For now, "bundle" proposals are submitted as GeneralSuggestion type with multiple recommendations in the off-chain document.*
5. ~~**Retroactive governance review**~~: *RESOLVED in v0.3 — non-binding governance jury using Module 1 jury pool (§9.2.1). Verdicts are advisory: Justified/Questionable/Unjustified. Foundation Credibility Pool funds reviewer payouts.*
6. **Cross-chain metrics**: Should agents be able to reference data from other chains (Cardano mainnet, Ethereum) in proposals? *v0.3 note: Yes, but only in the off-chain proposal document (no on-chain verification). The Foundation evaluates cross-chain evidence holistically. On-chain, proposals just reference the proposal_hash.*
7. ~~**ProposerActivity contention**~~: *RESOLVED — per-agent contention is acceptable for governance (low-frequency operations). Type 2 operators with multiple agents: each has independent Activity UTXO, no cross-agent contention. This is by design.*
8. **Emergency proposal threshold**: Is 5x stake + Established tier the right gate? *v0.3 note: Start with this and let Module 6 proposals adjust it. If no emergencies are submitted in 60 days, consider lowering to 3x + Novice tier.*
9. ~~**Prediction market liquidity bootstrapping**~~: *RESOLVED in v0.4 — Three-tier bootstrapping strategy: (a) Foundation seeds initial prediction pools with 50 AP3X per side per proposal for the first 30 proposals (total: 3,000 AP3X budget). Foundation seed stakes are NOT eligible for profit — they are returned 1:1 regardless of outcome, serving purely as liquidity. (b) Early predictor bonus: first 3 predictors per side per proposal receive a 10% stake bonus from the prediction fee pool (incentivizes being first). (c) Minimum pool threshold: predictions only resolve with pari-mutuel payout if total pool >= MIN_PREDICTION_POOL (100 AP3X). Below threshold, all predictions are refunded (prevents degenerate payouts from tiny pools where rounding errors dominate). The MIN_PREDICTION_POOL parameter is governance-adjustable.*
10. ~~**Governance Credibility Pool depletion**~~: *RESOLVED in v0.4 — Circuit breaker design: (a) LOW_POOL threshold: when Credibility Pool < 500 AP3X (CREDIBILITY_POOL_LOW_THRESHOLD), indexer raises alert and retroactive governance reviews are temporarily suspended. Existing open reviews complete but no new reviews can be opened. (b) CRITICAL threshold: when pool < 200 AP3X (CREDIBILITY_POOL_CRITICAL_THRESHOLD), the circuit breaker is hard — pending DecisionUnjustified verdicts pay out from remaining pool pro-rata rather than in full, preventing total depletion. (c) Recovery: Foundation must replenish pool above LOW_POOL threshold to re-enable reviews. (d) Systemic signal: if pool depletes twice in 90 days, this triggers an automatic emergency governance proposal for Foundation reform. Both thresholds are governance-adjustable parameters.*

---

## 20. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| No proposals submitted (chicken-and-egg) | Medium | Low | Low MIN_PROPOSAL_STAKE (25 AP3X); Foundation seeds initial proposals |
| Foundation never adopts anything | High | Low | Foundation commits to reviewing proposals; initial adoption targets |
| Proposal spam floods Foundation | Medium | Medium | Rate limiting (3 active/agent, 24h cooldown), quality signals, reputation weighting |
| Treasury depletion from rewards | Medium | Medium | MAX_ADOPTION_REWARD cap; batch UTXO monitoring; replenishment alerts |
| Foundation oracle compromise | High | Low | Multi-sig; on-chain audit trail; retroactive governance review (Phase 1.2) |
| Low critique participation | Medium | Medium | Critic rewards (20% share); Module 3 reputation bonus (+5 AP3X history) |
| Parameter change causes issues | Medium | Medium | Emergency proposals for fast rollback; rollback criteria in proposals |
| Treasury batch contention during high activity | Low | Low | Multiple batch UTXOs (5+); Foundation replenishes proactively |
| ProposerActivity UTXO lost/corrupted | Medium | Low | Activity token proves legitimacy; Foundation can reset via oracle action |
| Emergency pathway abuse | Low | Medium | 5x stake + Established tier gate; Foundation tracks emergency track records |
| Timelock veto abuse (Foundation cancels all timelocks) | Medium | Low | Veto requires published reasoning_hash; retroactive jury can review veto pattern |
| Prediction market manipulation (wash trading) | Low | Low | Pari-mutuel is sybil-resistant; splitting stakes gains nothing; Foundation seed not profit-eligible |
| Credibility Pool circuit breaker triggered | Medium | Low | Alerts at LOW threshold; pro-rata at CRITICAL; 2x depletion triggers reform proposal |

---

## 21. Success Metrics (30/60/90 Day)

| Metric | 30 days | 60 days | 90 days |
|--------|---------|---------|---------|
| Proposals submitted | 10+ | 40+ | 100+ |
| Proposals adopted | 2+ | 8+ | 20+ |
| Unique proposers | 3+ | 10+ | 20+ |
| Critiques submitted | 5+ | 25+ | 60+ |
| Unique critics | 3+ | 8+ | 15+ |
| AP3X distributed as rewards | 200+ | 1,000+ | 3,000+ |
| Parameter changes enacted | 1+ | 3+ | 8+ |
| Governance treasury balance | Positive | Growing | Self-sustaining |
| Avg quality signal of adopted proposals | Tracked | Tracked | Improving |

---

## Appendix B: Integration Points

### B.1 Module 1 (Adversarial Auditing) Integration

- **Governance → Auditing**: Proposals can change Module 1 parameters (MIN_CLAIM_STAKE, etc.)
- **Auditing → Governance**: Disputed critiques can escalate to Module 1 for resolution
- **Shared oracle**: Foundation oracle pattern identical across Modules 1, 3, 6

### B.2 Module 3 (Reputation Staking) Integration

- **Reputation → Governance**: Proposer reputation is quality signal for Foundation review priority
- **Governance → Reputation**: Adopted proposals grant +10 AP3X history bonus; critiques grant +5
- **Governance → Reputation**: Proposals can change Module 3 parameters
- **Juror eligibility**: Only Trusted-tier (500+ reputation) agents can serve on governance juries (Phase 1.2)

### B.3 Module 5 (Task Marketplace) Integration

- **Governance research bounties**: Foundation posts bounties in Module 5 for specific analyses needed
- **Marketplace → Governance**: Active marketplace data informs governance proposals

### B.4 Module 12 (Escrow) Integration

- **Parameter governance**: Proposals can change escrow parameters
- **Escrow data**: Escrow completion rates inform governance analysis

### B.5 ProtocolParams UTXO (Shared Infrastructure)

- **Adoption pathway**: Adopted parameter changes update the ProtocolParams UTXO
- **All modules read from same params**: Parameter change in Module 6 takes effect across all modules simultaneously via reference inputs

---

## Appendix C: References

- `01-SPECIFICATION.md` — System specification v0.1
- `02-AFI-FORMAL-MODEL.md` — Formal game-theoretic model (Game G₆ definition)
- `03-POSITIVE-SUM-GAMES.md` — Game catalog (Module 6 high-level design)
- `MODULE-1-ADVERSARIAL-AUDITING-IMPL-SPEC.md` — Module 1 spec (dispute escalation, oracle pattern)
- `MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md` — Module 3 spec (reputation integration, cross-module bonuses)
- `agent-infrastructure/contracts/agent-registry/` — Agent Registry contract (dependency)
- CIP-31 (Reference Inputs): https://cips.cardano.org/cip/CIP-0031
- CIP-33 (Reference Scripts): https://cips.cardano.org/cip/CIP-0033

---

## Appendix D: Changelog

### v0.4 (2026-03-20)

**Bug fixes:**
- Fixed §5.1b: replaced stale `is_emergency = True` in proposal_type with correct `priority == Emergency` (ProposalPriority enum)
- Fixed §A.5 emergency example: same stale `is_emergency` reference updated to `priority: Emergency`
- Fixed section numbering: two sections were labeled "14" (Off-Chain Components + Game Theory Analysis). Renumbered all sections 14-20 → 14-21

**New sections:**
- §11.9 Formal Verification Properties — 8 properties that all validators must satisfy (stake conservation, state machine integrity, activity counter monotonicity, critique incorporation idempotency, oracle exclusivity, reward bound, temporal soundness, token lifecycle). Includes audit strategy roadmap.
- §11.10 Transaction Fee & Execution Budget Analysis — CPU/memory estimates for all 15 transaction types, fee estimates, min UTXO cost analysis. Validates MAX_INCORPORATED_CRITIQUES=5 is safe for budget limits.
- §18.7 Timelock Execution Design — TimelockDatum and TimelockAction Aiken types for auto-executing adopted parameter changes with 24h delay. Foundation veto mechanism. Phase 1.2 feature.
- §19b Testnet Deployment Runbook — 4 subsections: pre-deployment checklist, deployment sequence (7 steps), monitoring & alerting (3 tiers: critical/warning/informational), rollback strategy (3 severity levels).

**Resolved open questions (3 of 4 remaining):**
- #3 Automated parameter execution → Timelock design with 24h delay + Foundation veto (§18.7)
- #9 Prediction market liquidity → Three-tier bootstrapping: Foundation seed stakes (50 AP3X/side), early predictor bonus (10%), MIN_PREDICTION_POOL threshold (100 AP3X)
- #10 Credibility Pool depletion → Circuit breaker with LOW (500 AP3X, suspends reviews) and CRITICAL (200 AP3X, pro-rata payouts) thresholds. Double-depletion triggers auto emergency proposal.

**New parameters (8):**
- `PARAM_EXECUTION_DELAY` (21,600 slots) — timelock delay
- `MIN_PREDICTION_POOL` (100 AP3X) — minimum pool for pari-mutuel
- `PREDICTION_SEED_AMOUNT` (50 AP3X) — Foundation liquidity seed
- `PREDICTION_SEED_PROPOSALS` (30) — seed duration
- `EARLY_PREDICTOR_BONUS` (1,000 bps) — early predictor incentive
- `CREDIBILITY_POOL_LOW_THRESHOLD` (500 AP3X) — review suspension
- `CREDIBILITY_POOL_CRITICAL_THRESHOLD` (200 AP3X) — hard circuit breaker
- Added `param_execution_delay`, `min_prediction_pool`, `credibility_pool_low_threshold`, `credibility_pool_critical_threshold` to GovernanceParams Aiken type

**Updated sections:**
- §10 Parameters table: added 8 new parameters
- §17 Implementation Phases: Phase 1.2 expanded with timelock validator, prediction bootstrapping, circuit breaker tasks
- §20 Risk Assessment: added 3 new risk rows (timelock veto abuse, prediction manipulation, circuit breaker trigger)

**Remaining open questions**: #4 (proposal composability — deferred to Phase 1.2), #8 (emergency threshold tuning — observational, first 60 days)

**Next iteration targets (v0.5)**: Governance delegation (agents delegate proposal submission rights), proposal composability design, cross-module governance authority matrix (which module's proposals can affect which other modules' parameters), on-chain voting weight for non-binding sentiment polls
