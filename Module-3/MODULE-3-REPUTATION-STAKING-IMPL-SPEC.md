# Module 3: Reputation Staking — Implementation Specification

**Status**: DRAFT v0.4
**Author**: Lead Author, with AI-assisted design
**Date**: 2026-03-20
**Dependencies**: Agent Registry contract (deployed), Module 1: Adversarial Auditing (for dispute escalation)
**Phase**: 1 (Traction — requires ~5 active agents)
**Target**: Vector eUTXO L2

---

## 1. Executive Summary

Reputation Staking is an economically-secured curation module where agents stake AP3X proportional to their claimed capabilities, and other agents endorse or challenge those claims. The result is a **self-curating directory** of trustworthy AI agents — the canonical place to discover reliable agents on Vector.

Your stake IS your reputation. Unstaking means losing credibility. This creates the **trust layer** in the Core Stack (Modules 3 → 1 → 12 → 5).

---

## 2. Design Principles

### 2.1 Reputation as Emergent Property, Not Stored State

**Critical design decision**: The reputation score is NOT stored as a single on-chain number.

On eUTXO, there is no global state to increment. Instead, reputation is **computed from the set of all UTXOs** associated with a DID:

```
reputation(agent_i) = self_stake(i) + Σ endorsements(i) - Σ challenges(i) + history_bonus(i) - decay(i)
```

Each component is a separate UTXO at the reputation validator address. The indexer aggregates them into a score. This is the eUTXO-native approach: reputation is a **view over UTXOs**, not a stored variable.

**Why this matters**:
- No contention: endorsing Agent A and endorsing Agent B are independent UTXOs
- No bottleneck: 1,000 agents can all receive endorsements in the same block
- No admin key: there is no "update reputation" function that could be exploited
- Composable: any contract can read reputation by counting UTXOs via reference inputs

### 2.2 Relationship to Agent Registry

Module 3 **extends** the existing Agent Registry — it does not replace it. The registry handles identity (DID, soulbound NFT, profile). Module 3 adds an economic layer on top:

```
┌───────────────────────────────────────────┐
│              Agent Discovery              │
│  (Indexer computes: profile + reputation) │
├───────────────────────────────────────────┤
│  Module 3: Reputation Layer                 │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐  │
│  │Self-Stake│ │Endorsement│ │Challenge │  │
│  │UTXOs     │ │UTXOs      │ │UTXOs     │  │
│  └─────────┘ └──────────┘ └──────────┘  │
├───────────────────────────────────────────┤
│  Agent Registry (existing)                │
│  ┌──────────────────────────────────┐    │
│  │ AgentDatum + Soulbound NFT       │    │
│  │ (DID, name, capabilities, etc.)  │    │
│  └──────────────────────────────────┘    │
└───────────────────────────────────────────┘
```

---

## 3. System Architecture

### 3.1 Contract Topology

Two Aiken multi-validators:

```
┌──────────────────────────────────────────────────────────────┐
│                    REPUTATION STAKING                          │
│                                                                │
│  ┌───────────────────────────┐  ┌──────────────────────────┐  │
│  │ Reputation Validator       │  │ Endorsement Validator     │  │
│  │ (reputation.ak)            │  │ (endorsement.ak)          │  │
│  │                             │  │                            │  │
│  │ Mint:                       │  │ Mint:                      │  │
│  │  - MintStakeToken           │  │  - MintEndorsementToken    │  │
│  │  - BurnStakeToken           │  │  - MintChallengeToken      │  │
│  │                             │  │  - BurnEndorsementToken    │  │
│  │ Spend:                      │  │  - BurnChallengeToken      │  │
│  │  - IncreaseStake            │  │                            │  │
│  │  - DecreaseStake            │  │ Spend:                     │  │
│  │  - UpdateCapabilities       │  │  - WithdrawEndorsement     │  │
│  │  - ClaimDecayRefund         │  │  - WithdrawChallenge       │  │
│  │                             │  │  - SlashEndorsement        │  │
│  └─────────────┬───────────────┘  │  - RewardChallenger        │  │
│                │                   │  - EscalateToAudit         │  │
│                │                   └──────────┬─────────────────┘  │
│                └──────────┬──────────────────┘                    │
│                           ▼                                       │
│  External dependencies:                                           │
│  - Agent Registry (DID verification via reference inputs)         │
│  - Module 1: Adversarial Auditing (dispute escalation)              │
│  - Protocol Params UTXO (shared with Module 1)                      │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 UTXO Flow

```
SELF-STAKING (agent backs its own claims):
  Agent registers in Registry → has DID + capabilities list
  Agent stakes AP3X → StakeUTXO created at reputation validator
  Agent can increase/decrease stake over time
  Stake amount signals confidence: "I'm willing to risk X AP3X on my claims"

ENDORSEMENT (agent vouches for another):
  Agent B endorses Agent A → EndorsementUTXO created
  Locks e_stake AP3X referencing Agent A's DID
  Agent B can withdraw endorsement at any time (with cooldown)
  If Agent A is slashed, endorsers lose proportional stake

CHALLENGE (agent disputes another's claims):
  Agent C challenges Agent A's capability claim → ChallengeUTXO created
  Locks ch_stake AP3X against Agent A's specific capability
  Resolution: automated verification, peer jury, or escalation to Module 1
  If challenge succeeds: Agent A + endorsers slashed, challenger rewarded
  If challenge fails: challenger forfeits stake to Agent A

DECAY (inactive agents lose reputation):
  Every DECAY_PERIOD epochs, inactive agents' stakes become partially withdrawable
  by a "decay collector" (any agent) who earns a small fee
  This creates a Module 9 (Proof of Useful Work) synergy: agents can earn AP3X
  by processing reputation decay
```

---

## 4. On-Chain Types

### 4.1 Self-Stake Types

```aiken
/// An agent's self-stake backing their claimed capabilities.
/// One StakeUTXO per agent. Updated via IncreaseStake/DecreaseStake.
pub type StakeDatum {
  /// DID of the staking agent (policy_id from registry NFT)
  agent_did: ByteArray,
  /// Payment credential of the agent owner
  owner_credential: Credential,
  /// Total AP3X self-staked (in DFM)
  stake_amount: Int,
  /// Capabilities this stake backs (must match registry capabilities)
  staked_capabilities: List<ByteArray>,
  /// Slot when stake was created or last modified
  last_updated: Int,
  /// Accumulated history bonus points (from winning challenges, cross-module bonuses)
  /// Updated only when this UTXO is consumed and re-created (e.g., IncreaseStake, AddHistoryBonus)
  history_points: Int,
}

/// DESIGN NOTE on activity tracking for decay:
///
/// On eUTXO, you CANNOT update a UTXO via reference input — only by consuming
/// and re-creating it. This means `last_active` cannot live in the StakeDatum
/// (it would require consuming the stake UTXO every time the agent transacts
/// in any module, creating contention on the stake UTXO).
///
/// Instead, activity tracking uses the INDEXER:
///   - The indexer monitors all module validator addresses
///   - When it sees a transaction signed by an agent's credential, it records
///     the slot as `last_active` in its database
///   - Decay eligibility is computed off-chain by the indexer
///   - The decay collector transaction includes the indexer's activity report
///     as a redeemer argument, and the validator verifies it against on-chain
///     evidence (the agent's most recent tx in any module contract, provable via
///     reference inputs)
///
/// Alternative: a lightweight "heartbeat" UTXO that agents touch periodically.
/// But this adds unnecessary tx cost. The indexer approach is preferred.

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
```

### 4.1b Token Naming Conventions

```aiken
/// STAKE TOKEN:
///   Asset name = "rstk_" ++ blake2b_256(agent_did)[0..28]
///   Policy ID = reputation_validator_hash (minted by reputation multi-validator)
///   Exactly 1 per agent. Minted on CreateStake, burned when stake drops below
///   MIN_SELF_STAKE (via decay or explicit unstake to zero).
///   MUST travel with the StakeUTXO through all state transitions.
///
/// ENDORSEMENT TOKEN:
///   Asset name = "rend_" ++ blake2b_256(endorser_did ++ target_did)[0..28]
///   Policy ID = endorsement_validator_hash
///   Exactly 1 per endorser-target pair. (Already specified in §6.3)
///
/// CHALLENGE TOKEN:
///   Asset name = "rchl_" ++ blake2b_256(challenger_did ++ target_did ++ challenged_capability)[0..24]
///   Policy ID = endorsement_validator_hash (same multi-validator handles challenges)
///   Exactly 1 per challenger-target-capability triple.
///
/// HISTORY BONUS TOKEN:
///   Asset name = "hbonus_" ++ blake2b_256(source_ref)[0..24]
///   Policy ID = reputation_validator_hash
///   One per qualifying event. (Defined in §4.4)
///
/// GENESIS BONUS TOKEN:
///   Asset name = "genesis_" ++ blake2b_256(agent_did)[0..24]
///   Policy ID = reputation_validator_hash
///   Maximum 1 per agent, maximum 20 total. (Defined in §4.5)
```

### 4.2 Endorsement Types

```aiken
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

pub type EndorsementAction {
  /// Create a new endorsement
  MintEndorsementToken
  /// Increase existing endorsement amount (consumes and re-creates with more AP3X)
  IncreaseEndorsement
  /// Withdraw endorsement (subject to ENDORSEMENT_COOLDOWN)
  WithdrawEndorsement
  /// Slash endorsement (triggered by successful challenge against target)
  SlashEndorsement { challenge_ref: OutputReference }
}
```

### 4.3 Challenge Types

```aiken
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
  /// Counter-evidence from target (populated by RespondToChallenge)
  /// Initially empty ByteArray; set when target responds
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

pub type RepChallengeAction {
  /// Create a new reputation challenge
  MintChallengeToken
  /// Withdraw challenge (only if state == Open and before response deadline)
  WithdrawChallenge
  /// Target agent responds with counter-evidence
  RespondToChallenge { counter_evidence_hash: ByteArray, counter_evidence_uri: ByteArray }
  /// Escalate to Module 1 for full adversarial resolution
  EscalateToAudit
  /// Resolve directly (automated verification or Foundation oracle Phase 1.0)
  ResolveChallenge { outcome: RepChallengeOutcome }
  /// Distribute rewards/slashing after resolution
  DistributeOutcome
}
```

### 4.4 History Bonus Types

```aiken
/// A history bonus UTXO — tracks permanent reputation earned from cross-module
/// events (challenge wins, adopted proposals, escrow completions, etc.).
///
/// Phase 1.0: History bonuses are tracked as SEPARATE UTXOs (not in StakeDatum)
/// to avoid contention on the StakeUTXO. The indexer aggregates all
/// HistoryBonusUTXOs for an agent when computing reputation score.
///
/// Each bonus is an independent UTXO at the reputation validator address.
/// Cannot be transferred or withdrawn — permanent reputation.
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

pub type HistoryBonusAction {
  /// Mint a new history bonus (called by module validators via cross-module bonus)
  MintHistoryBonus
  /// Burn expired genesis bonuses (permissionless, after genesis period ends)
  BurnGenesisBonus
}

/// History bonus token asset name = "hbonus_" ++ blake2b_256(source_ref)[0..24]
/// Minted by the reputation validator policy when a qualifying event occurs.
/// The minting transaction must include a reference input or consumed input
/// proving the qualifying event (e.g., resolved ChallengeUTXO, adopted ProposalUTXO).
```

### 4.5 Genesis Bonus Types

```aiken
/// Genesis bonus — a special HistoryBonusDatum with source = GenesisBonus.
/// Minted once per Genesis Agent at system launch. Grants 100 AP3X equivalent
/// reputation points. Subject to normal decay. Cannot be transferred.
///
/// MINTING RULES:
///   1. Only Foundation oracle can mint GenesisBonus tokens (oracle_credential signature)
///   2. Maximum 20 genesis bonuses total (GENESIS_AGENT_CAP)
///   3. Each agent_did can receive at most 1 genesis bonus
///   4. Minting window: only valid within GENESIS_MINTING_WINDOW slots of system launch
///      (initially 604,800 slots = ~28 days after launch)
///   5. bonus_points = GENESIS_BONUS_AMOUNT (100 AP3X equivalent)
///   6. Agent must have active DID in Agent Registry
///
/// DECAY:
///   Genesis bonuses decay normally (same rate as stake-based reputation).
///   After ~100 inactive epochs at 1% decay, the bonus approaches zero.
///   This prevents genesis agents from coasting indefinitely.
///
/// BURN:
///   After GENESIS_PROTECTION_PERIOD (initially 129,600 slots = ~60 days),
///   anyone can burn expired genesis bonus UTXOs where the bonus has fully
///   decayed to 0 (UTXO cleanup).
///
/// Genesis bonus token asset name = "genesis_" ++ blake2b_256(agent_did)[0..24]
```

---

## 5. Reputation Score Computation

### 5.1 Score Formula

The reputation score is computed **off-chain by the indexer** from on-chain UTXOs:

```
R(agent_i, t) = S(i) + E(i) - C(i) + H(i) - D(i, t)

Where:
  S(i) = self_stake amount (from StakeUTXO)
  E(i) = Σ endorsement amounts (from all EndorsementUTXOs targeting agent_i)
  C(i) = Σ active challenge amounts (from all open ChallengeUTXOs against agent_i)
  H(i) = history bonus (accumulated from resolved challenges won)
  D(i, t) = decay penalty (grows with inactivity)
```

### 5.2 Decay Mechanism

Reputation decays if an agent is inactive. "Active" means the agent submitted any on-chain transaction (in any module) within the last `ACTIVITY_WINDOW` epochs.

```
D(i, t) = 0                                        if active in last ACTIVITY_WINDOW
D(i, t) = stake(i) × DECAY_RATE × inactive_epochs  if inactive

Where:
  inactive_epochs = (current_epoch - last_active_epoch) - ACTIVITY_WINDOW
  DECAY_RATE = 1% per epoch (parameter)
  ACTIVITY_WINDOW = 180 epochs (~30 days at 4h epochs)
```

**Decay processing** is permissionless — any agent can submit a `ClaimDecayRefund` transaction:

```
decay_amount = min(stake(i) × DECAY_RATE × inactive_epochs, stake(i))
collector_fee = decay_amount × DECAY_COLLECTOR_FEE (5%)
treasury_amount = decay_amount - collector_fee

The decay collector (the agent processing the decay) earns collector_fee.
The rest goes to a protocol treasury UTXO.
```

This creates a Module 9 synergy: monitoring agents earn AP3X by processing reputation decay — useful work that maintains directory quality.

### 5.3 History Bonus

Agents earn non-decaying reputation points for successfully defending against challenges:

```
H(i) = Σ (challenge_stake_won × HISTORY_MULTIPLIER)
HISTORY_MULTIPLIER = 0.1 (10% of won challenge stakes as permanent bonus)
```

The history bonus is tracked via a `history_points` field in the StakeDatum, updated when challenges resolve. This creates path-dependent reputation — agents that have survived scrutiny are worth more than agents with the same stake but no challenge history.

### 5.4 Reputation Tiers

The indexer maps raw scores to human-readable tiers:

| Tier | Score Range | Meaning | Unlocks |
|------|-------------|---------|---------|
| Unverified | 0 | Just registered, no stake | Basic registry listing |
| Novice | 1–99 AP3X | Minimal skin in the game | Can be endorsed |
| Established | 100–499 AP3X | Meaningful stake | Eligible for Task Marketplace (Module 5) |
| Trusted | 500–1,999 AP3X | Significant economic backing | Eligible as juror (Module 1) |
| Elite | 2,000+ AP3X | Heavily vetted and endorsed | Priority in agent discovery |

Tiers are computed, not stored. Any contract can derive tier from the reputation score.

---

## 6. Validation Rules

### 6.1 Create Self-Stake (`CreateStake`)

```
MUST:
  1. Agent has active DID in Agent Registry (verified via reference input)
  2. No existing StakeUTXO for this agent_did (first-time stake)
  3. stake_amount >= MIN_SELF_STAKE (initially 10 AP3X — matches registration deposit)
  4. staked_capabilities is non-empty and is a subset of registry capabilities
     (verified: reference input of agent's registry UTXO, check capabilities list)
  5. Output StakeUTXO at reputation validator address contains:
     - Inline datum with correct fields
     - Value includes stake_amount AP3X + min UTXO ADA
     - Stake tracking token (minted in same tx)
  6. last_active = current slot
  7. Transaction signed by owner_credential
```

### 6.2 Increase/Decrease Stake

```
IncreaseStake MUST:
  1. Existing StakeUTXO consumed (with stake token)
  2. Continuing output with same agent_did, increased stake_amount
  3. Value difference matches the increase
  4. last_updated = current slot
  5. Transaction signed by owner_credential

DecreaseStake MUST:
  1. Existing StakeUTXO consumed (with stake token)
  2. new_amount = stake_amount - decrease_amount
  3. new_amount >= MIN_SELF_STAKE (cannot unstake below minimum)
  4. COOLDOWN: last_updated + STAKE_COOLDOWN <= current slot
     (prevents rapid stake-then-unstake manipulation)
  5. No active challenges against this agent (cannot unstake while challenged)
  6. Continuing output with decreased stake_amount
  7. Difference returned to owner_credential
  8. Transaction signed by owner_credential
```

### 6.3 Create Endorsement (`MintEndorsementToken`)

```
MUST:
  1. Endorser has active DID in Agent Registry
  2. Target agent has active DID AND active StakeUTXO (must have self-staked first)
  3. endorser_did != target_did (cannot self-endorse)
  4. Endorsement token asset name = blake2b_256(endorser_did ++ target_did)
     This is DETERMINISTIC per pair — the mint policy rejects if this token
     already exists in circulation (checked via mint quantity: must be exactly +1,
     and the token must not appear in any input, proving it doesn't already exist).
     To increase endorsement amount, use a separate IncreaseEndorsement action
     that consumes and re-creates the existing EndorsementUTXO.
  5. stake_amount >= MIN_ENDORSEMENT (initially 5 AP3X)
  6. endorsed_capabilities is non-empty and subset of target's staked_capabilities
  7. EndorsementUTXO created at endorsement validator address
  8. Transaction signed by endorser_credential
```

### 6.3b Increase Endorsement (`IncreaseEndorsement`)

```
MUST:
  1. Existing EndorsementUTXO consumed (with endorsement token)
  2. endorser_did and target_did unchanged (same pair)
  3. new_stake_amount > current stake_amount (must be an increase)
  4. Additional AP3X provided in transaction inputs (difference)
  5. endorsed_capabilities may be updated (add new capabilities, but
     cannot REMOVE capabilities while target has active challenges
     on those capabilities)
  6. Continuing EndorsementUTXO at endorsement validator address with:
     - stake_amount = new_stake_amount
     - endorsed_capabilities = updated list (if changed)
     - created_at UNCHANGED (preserves cooldown eligibility from original)
     - Endorsement token travels with UTXO (not burned/re-minted)
  7. Transaction signed by endorser_credential

NOTE: IncreaseEndorsement does NOT reset the cooldown timer.
      The original created_at is preserved, so if an endorser
      already waited through the cooldown, they can increase and
      withdraw in the same session. This incentivizes gradual
      commitment increases over time.

NOTE: There is no DecreaseEndorsement — to reduce endorsement,
      the endorser must WithdrawEndorsement entirely and re-create
      a new endorsement at the lower amount (which resets cooldown).
      This asymmetry is intentional: increasing trust is easy,
      reducing trust should have friction.
```

### 6.4 Withdraw Endorsement (`WithdrawEndorsement`)

```
MUST:
  1. Endorsement exists (endorsement token present)
  2. created_at + ENDORSEMENT_COOLDOWN <= current slot
     (prevents flash-endorse-then-withdraw manipulation)
  3. Target agent has no active challenges (cannot withdraw during dispute)
  4. Stake returned to endorser_credential
  5. Endorsement token burned
  6. Transaction signed by endorser_credential
```

### 6.5 Update Capabilities (`UpdateCapabilities`)

```
MUST:
  1. Existing StakeUTXO consumed (with stake token)
  2. new_capabilities is non-empty
  3. new_capabilities is a subset of agent's registry capabilities
     (verified via reference input to Agent Registry UTXO)
  4. COOLDOWN: last_updated + STAKE_COOLDOWN <= current slot
  5. No active challenges against capabilities being REMOVED
     (cannot drop a capability while it's being challenged)
  6. Continuing output with updated staked_capabilities, same stake_amount
  7. last_updated = current slot
  8. Transaction signed by owner_credential

NOTE: Adding new capabilities costs nothing extra (just a datum update).
      Removing capabilities while unchallenged is allowed — this is how
      an agent gracefully drops a service it no longer provides.
```

### 6.6 Process Decay (`ClaimDecayRefund`)

```
CALLABLE BY: Any agent (permissionless — incentivized by collector fee)

MUST:
  1. Target StakeUTXO consumed (with stake token)
  2. Target agent is inactive — verified by one of:
     a. No transaction from target's credential appears in any module validator
        within ACTIVITY_WINDOW epochs (proven via reference inputs showing
        most recent module UTXOs for this agent are all older than threshold)
     b. OR Foundation oracle attests inactivity (Phase 1.0 simplification)
  3. decay_amount = min(stake × DECAY_RATE × inactive_epochs, stake)
     where inactive_epochs = epochs since last activity - ACTIVITY_WINDOW
  4. collector_fee = decay_amount × DECAY_COLLECTOR_FEE
  5. treasury_amount = decay_amount - collector_fee
  6. Outputs:
     - Continuing StakeUTXO with stake_amount reduced by decay_amount
       (if remaining > MIN_SELF_STAKE; otherwise UTXO consumed entirely)
     - Collector payout UTXO to decay collector's credential
     - Treasury payout UTXO to protocol treasury address
  7. If stake drops below MIN_SELF_STAKE: stake token is burned,
     agent reverts to Unverified tier

PHASE 1.0 SIMPLIFICATION:
  Decay is processed via Foundation oracle attestation (same signing key
  as Module 1 oracle). The Foundation periodically publishes an "inactivity
  list" UTXO. Decay collectors reference this list instead of proving
  inactivity from raw on-chain data.

PHASE 1.1+:
  Trustless decay verification via indexer Merkle proofs or by requiring
  the decay collector to provide reference inputs showing the agent's
  most recent UTXOs across all module validators are older than threshold.
```

### 6.7 Add History Bonus (`AddHistoryBonus`)

```
CALLABLE BY: Module validators (Module 1 challenge resolver, Module 9 work verifier, etc.)

MUST:
  1. Target StakeUTXO consumed (with stake token)
  2. A qualifying event occurred in a module validator (proven by consuming
     or referencing the module's resolution UTXO)
  3. bonus_amount calculated per cross-module bonus table (Section 8.3)
  4. Continuing StakeUTXO with history_points increased by bonus_amount
  5. No change to stake_amount or staked_capabilities

IMPLEMENTATION NOTE:
  This creates a contention point — the StakeUTXO must be consumed to
  update history_points. To minimize contention:
  - cross-module bonuses can be BATCHED: a "bonus accumulator" UTXO
    collects pending bonuses, and the agent claims them all at once
    via a single StakeUTXO update
  - Alternative: history bonus tracked as separate UTXOs (like endorsements)
    and aggregated by the indexer. Avoids contention entirely.
  - Phase 1.0: use separate bonus UTXOs (no contention)
  - Phase 1.1: evaluate whether StakeDatum update is worth the contention
```

### 6.8 Create Reputation Challenge (`MintChallengeToken`)

```
MUST:
  1. Challenger has active DID in Agent Registry
  2. Target agent has active DID AND active StakeUTXO
  3. challenger_did != target_did (cannot self-challenge)
  4. challenged_capability is in target's staked_capabilities list
  5. stake_amount >= MIN_CHALLENGE_STAKE (initially 25 AP3X)
  6. evidence_hash is exactly 32 bytes
  7. No existing open challenge from this challenger against this target
     for the same capability (one challenge per capability per pair)
  8. ChallengeUTXO created with state = Open
  9. Transaction signed by challenger_credential
```

### 6.9 Respond to Challenge (`RespondToChallenge`)

```
MUST:
  1. ChallengeUTXO consumed (with challenge token)
  2. state == Open
  3. current slot <= created_at + CHALLENGE_RESPONSE_DEADLINE
  4. Transaction signed by target agent's owner_credential
     (only the challenged agent can respond)
  5. counter_evidence_hash is exactly 32 bytes
  6. counter_evidence_uri is non-empty
  7. Continuing ChallengeUTXO with:
     - Same state (Open) — response doesn't resolve, just adds evidence
     - New fields: counter_evidence_hash, counter_evidence_uri
     - response_submitted_at = current slot

DEFAULT JUDGMENT:
  If target does NOT respond by created_at + CHALLENGE_RESPONSE_DEADLINE:
  - Challenge auto-resolves as CapabilityFalsified
  - Callable by anyone (permissionless)
  - Rationale: silence implies inability to defend the claim
  - INCENTIVE: The agent who submits the default judgment tx earns a
    DEFAULT_JUDGMENT_FEE = 1% of the challenger's stake (deducted from
    the challenger's eventual reward). This covers tx fees and incentivizes
    timely default processing, similar to the DECAY_COLLECTOR_FEE pattern.
    If the challenger submits the default judgment themselves, they pay no
    fee (they are the beneficiary — no need for third-party incentive).
```

### 6.10 Escalate to Audit (`EscalateToAudit`)

```
CALLABLE BY: Either party (challenger or target)

MUST:
  1. ChallengeUTXO consumed (with challenge token)
  2. state == Open AND response has been submitted (both sides have evidence)
  3. current slot <= created_at + CHALLENGE_RESPONSE_DEADLINE + ESCALATION_WINDOW
     (ESCALATION_WINDOW = 5,400 slots, ~6 hours after response deadline)
  4. Creates a Module 1 Adversarial Auditing claim:
     - claim_hash = blake2b_256(challenge evidence + counter-evidence)
     - claim_type = "reputation_challenge"
     - stake = forwarded from both parties' stakes
  5. ChallengeUTXO updated: state = Escalated { audit_claim_ref }
  6. Resolution handled entirely by Module 1 system
  7. Module 1 verdict maps back:
     - ClaimerWins (target wins) → CapabilityVerified
     - AuditorWins (challenger wins) → CapabilityFalsified
     - Inconclusive → Inconclusive

IF NEITHER PARTY ESCALATES:
  Foundation oracle resolves directly (Phase 1.0)
  or challenge expires as Inconclusive (stakes returned minus fees)
```

### 6.11 Resolve Challenge

```
Phase 1.0 — Foundation Oracle:
  Same pattern as Module 1: Foundation multi-sig resolves.

Phase 1.1 — Automated Verification:
  For objectively verifiable capabilities (data_indexing, oracle_update):
  1. Verification agent runs the capability test
  2. Submits proof on-chain
  3. If proof matches claim → CapabilityVerified
  4. If proof contradicts → CapabilityFalsified

Phase 1.1+ — Escalation to Module 1:
  For subjective capabilities (research, analysis):
  1. Either party calls EscalateToAudit
  2. Creates a claim in Module 1's Adversarial Auditing system
  3. Module 1 jury resolves the dispute
  4. Result feeds back to Module 3 for slashing/reward

SLASHING (CapabilityFalsified):
  Target agent: loses stake proportional to challenged capability
    slash_amount = self_stake × (1 / num_staked_capabilities)
  Endorsers of that capability: lose 50% of their endorsement stake
  Challenger receives: target_slash + endorser_slashes - protocol_fee (5%)
  Protocol treasury receives: protocol_fee

REWARD (CapabilityVerified):
  Challenger: loses entire stake
  Target agent: receives challenger's stake minus protocol_fee
  Endorsers: no change (vindicated)
  history_bonus += challenger_stake × HISTORY_MULTIPLIER
```

---

## 7. Capability Taxonomy (Phase 1.0)

Capabilities are free-text in the Agent Registry, but Module 3 needs structured capabilities for matching and challenges. Phase 1.0 uses a **curated vocabulary** stored in the ProtocolParams UTXO:

### 7.1 Phase 1.0 Curated Capabilities

| Category | Capability Tag | Verification Method | Objective? |
|----------|---------------|-------------------|-----------|
| **Data** | `data_indexing` | Re-index and compare merkle root | Yes |
| **Data** | `data_curation` | Spot-check curated records | Partially |
| **Oracle** | `oracle_price_feed` | Cross-reference price sources | Yes |
| **Oracle** | `oracle_data_feed` | Cross-reference data sources | Yes |
| **Infrastructure** | `api_hosting` | Uptime check (ping endpoint) | Yes |
| **Infrastructure** | `chain_monitoring` | Verify alert accuracy | Partially |
| **Compute** | `ml_inference` | Run benchmark task, compare output | Yes |
| **Compute** | `data_analysis` | Evaluate analysis quality | No (subjective) |
| **Research** | `vulnerability_research` | Verify reported vulnerability | Yes |
| **Research** | `governance_analysis` | Evaluate proposal quality | No (subjective) |
| **DeFi** | `arbitrage` | Verify trade profitability on-chain | Yes |
| **DeFi** | `liquidity_provision` | Verify LP positions on-chain | Yes |

**Objective capabilities** can be verified automatically (Phase 1.1).
**Subjective capabilities** require jury/oracle resolution.
**Custom capabilities** allowed in Phase 1.2 after taxonomy stabilizes.

### 7.2 Capability Verification Profiles

Each curated capability has a verification profile defining how challenges are resolved:

```aiken
pub type VerificationProfile {
  /// Whether automated verification is possible
  automatable: Bool,
  /// Time required for verification (affects challenge deadlines)
  verification_complexity: VerificationComplexity,
  /// Minimum challenge stake (harder-to-verify capabilities cost more to challenge)
  min_challenge_multiplier: Int,  // basis points relative to MIN_CHALLENGE_STAKE
}

pub type VerificationComplexity {
  /// Result computable in < 1 minute (data hash comparison)
  Trivial
  /// Result computable in < 1 hour (re-indexing, benchmark)
  Moderate
  /// Requires human or jury evaluation (research quality)
  Subjective
}
```

---

## 8. Cold Start Bootstrap

The cold start problem: new agents have zero reputation, making the directory useless initially.

### 8.1 Genesis Agent Program

The Foundation designates **Genesis Agents** — the first 20 agents on Vector. These agents receive:
- `genesis_bonus`: 100 AP3X equivalent in reputation history points (non-transferable, decays normally)
- Priority listing in the directory for the first 90 days
- Foundation endorsement (Foundation itself is an endorser, signaling trust)

Genesis bonus is implemented as a special `GenesisBonus` UTXO at the reputation validator, minted once at system launch and referenceable by the indexer.

### 8.2 Graduated Minimum Stakes

Early agents face lower barriers:

| Agent Count on Network | MIN_SELF_STAKE | MIN_ENDORSEMENT | MIN_CHALLENGE_STAKE |
|------------------------|----------------|-----------------|---------------------|
| 0–20 agents | 10 AP3X | 5 AP3X | 10 AP3X |
| 21–100 agents | 25 AP3X | 10 AP3X | 25 AP3X |
| 100+ agents | 50 AP3X | 25 AP3X | 50 AP3X |

Implemented via the ProtocolParams UTXO (governance-adjustable).

### 8.3 Automatic Reputation from other modules

Agents earn implicit reputation through participation in other modules:

| Module Activity | Reputation Effect |
|---------------|-------------------|
| Module 1: Won audit challenge (as claimer) | +10% of challenge stake as history bonus |
| Module 1: Served as juror (voted with majority) | +2 AP3X equivalent history bonus |
| Module 9: Verified useful work accepted | +5 AP3X equivalent history bonus |
| Module 6: Governance proposal adopted | +10 AP3X equivalent history bonus |
| Module 12: Escrow task completed successfully | +3 AP3X equivalent history bonus |

These cross-module bonuses are recorded via a `CrossGameBonus` UTXO minted by each module's validator when the qualifying event occurs. The reputation indexer includes them in the history bonus calculation.

---

## 9. Anti-Sybil Mechanisms

### 9.1 Self-Endorsement Ring Prevention

**Problem**: Type 2 operator creates agents A, B, C. A endorses B, B endorses C, C endorses A. All gain reputation without external validation.

**Mitigations**:

1. **Self-endorsement blocked**: `endorser_did != target_did` (on-chain rule)
2. **Endorsement doesn't count toward endorser's own reputation**: Endorsing someone else doesn't raise your score
3. **Ring detection** (off-chain, indexer-level):
   - DID graph analysis: if A→B→C→A forms a cycle, flag all three
   - Endorsed-by-same-cluster penalty: if >50% of an agent's endorsements come from DIDs that also endorse each other, discount their endorsement weight
4. **UTXO provenance analysis**: If endorsement stakes originate from the same funding UTXO, flag as potential sybil cluster
5. **Endorsement diversity requirement** (Phase 1.1):
   - An agent's effective endorsed reputation is capped at `self_stake × MAX_ENDORSEMENT_MULTIPLIER` (initially 3x)
   - Even if you get 10,000 AP3X in endorsements, it only counts as 3x your self-stake
   - This means self-stake is the anchor — you can't fake reputation without real capital

### 9.2 Stake Manipulation Prevention

**Problem**: Agent stakes, gets endorsements, then immediately unstakes.

**Mitigations**:
- `STAKE_COOLDOWN`: 21,600 slots (~24 hours) between stake changes
- Cannot decrease stake while any challenge is open
- Endorsement withdrawal also has cooldown: `ENDORSEMENT_COOLDOWN` = 43,200 slots (~48 hours)

### 9.3 Challenge Spam Prevention

**Problem**: Agent files many cheap challenges to drain targets' time/resources.

**Mitigations**:
- `MIN_CHALLENGE_STAKE` = 25 AP3X (expensive to spam)
- One challenge per capability per pair (can't challenge same thing twice)
- Failed challenges lose entire stake (high cost of being wrong)
- Challenger must have been registered for `MIN_AGENT_AGE` (24 hours)

---

## 10. Parameters

All parameters governance-adjustable via ProtocolParams UTXO (shared with Module 1).

| Parameter | Initial Value | Unit | Rationale |
|-----------|--------------|------|-----------|
| `MIN_SELF_STAKE` | 10 | AP3X | Low entry for early adopters; matches registration deposit |
| `MIN_ENDORSEMENT` | 5 | AP3X | Low enough to encourage endorsement activity |
| `MIN_CHALLENGE_STAKE` | 25 | AP3X | High enough to deter spam; must risk real capital |
| `STAKE_COOLDOWN` | 21,600 | slots (~24h) | Prevents rapid stake manipulation |
| `ENDORSEMENT_COOLDOWN` | 43,200 | slots (~48h) | Prevents flash-endorse-withdraw |
| `DECAY_RATE` | 100 | basis points/epoch (1%) | Gentle decay; inactive agents lose ~30% over 30 inactive epochs |
| `ACTIVITY_WINDOW` | 180 | epochs (~30 days) | Grace period before decay kicks in |
| `DECAY_COLLECTOR_FEE` | 500 | basis points (5%) | Incentivizes decay processing |
| `HISTORY_MULTIPLIER` | 1,000 | basis points (10%) | Permanent bonus from winning challenges |
| `MAX_ENDORSEMENT_MULTIPLIER` | 3 | multiplier | Caps endorsement effect relative to self-stake |
| `SLASH_RATE_ENDORSER` | 5,000 | basis points (50%) | Endorsers lose 50% when target is falsified |
| `PROTOCOL_FEE_RATE` | 500 | basis points (5%) | Cut to protocol treasury on resolutions |
| `CHALLENGE_RESPONSE_DEADLINE` | 10,800 | slots (~12h) | Target must respond or face default |
| `MIN_AGENT_AGE` | 21,600 | slots (~24h) | Anti-sybil: must exist before challenging |
| `ESCALATION_WINDOW` | 5,400 | slots (~6h) | Window after response deadline to escalate to Module 1 |
| `DEFAULT_JUDGMENT_FEE` | 100 | basis points (1%) | Fee paid to default judgment submitter from challenger reward |
| `GENESIS_AGENT_CAP` | 20 | agents | Maximum genesis bonus recipients |
| `GENESIS_BONUS_AMOUNT` | 100 | AP3X equivalent | History points per genesis agent |
| `GENESIS_MINTING_WINDOW` | 604,800 | slots (~28d) | Window after launch for minting genesis bonuses |
| `GENESIS_PROTECTION_PERIOD` | 129,600 | slots (~60d) | Period before expired genesis UTXOs can be burned |

---

## 11. Contract Architecture (Aiken)

### 11.1 File Structure

```
contracts/reputation-staking/
├── aiken.toml
├── validators/
│   ├── reputation.ak          # Self-stake multi-validator (mint stake token + spend stake UTXO)
│   └── endorsement.ak         # Endorsement + challenge multi-validator
├── lib/
│   └── reputation_staking/
│       ├── types.ak            # All types from Sections 4.1–4.5 (incl. HistoryBonusDatum, GenesisBonusDatum)
│       ├── params.ak           # Protocol parameters (reference input, shared with Module 1)
│       ├── stake_validation.ak     # Self-stake validation logic
│       ├── endorsement_validation.ak # Endorsement validation logic
│       ├── challenge_validation.ak   # Challenge validation + resolution logic
│       ├── decay.ak            # Decay calculation and processing logic
│       ├── scoring.ak          # On-chain score helpers (for tier-gating)
│       └── utils.ak            # Shared helpers (DID verification, capability matching)
└── tests/
    ├── stake_tests.ak
    ├── endorsement_tests.ak
    ├── challenge_tests.ak
    ├── decay_tests.ak
    └── integration_tests.ak
```

### 11.2 Cross-Validator References

```aiken
pub type ReputationConfig {
  /// Script hash of the reputation validator
  reputation_validator_hash: ScriptHash,
  /// Script hash of the endorsement validator
  endorsement_validator_hash: ScriptHash,
  /// Policy ID of the Agent Registry
  registry_policy_id: PolicyId,
  /// Script hash of the Agent Registry (for address verification)
  registry_script_hash: ScriptHash,
  /// Script hash of Module 1 claim validator (for escalation)
  audit_claim_validator_hash: ScriptHash,
  /// Script hash of protocol params holder
  params_script_hash: ScriptHash,
  /// Script hash of protocol treasury
  treasury_script_hash: ScriptHash,
}

/// TREASURY MANAGEMENT:
/// The protocol treasury (treasury_script_hash) is a SHARED treasury used
/// across all modules (Module 1, 3, 6, 11). It is NOT module-specific.
///
/// Module 3 sends AP3X to treasury in two scenarios:
///   1. Decay processing: treasury_amount = decay_amount - collector_fee (§5.2)
///   2. Challenge resolution: protocol_fee = outcome_value × PROTOCOL_FEE_RATE (§6.11)
///
/// Treasury UTXO structure:
///   Address: ScriptCredential(treasury_script_hash)
///   Value: accumulated AP3X protocol fees + min UTXO ADA
///   Datum: TreasuryDatum { source_module: Int, last_deposit_slot: Int }
///
/// Treasury governance:
///   - Foundation controls withdrawals via multi-sig oracle
///   - Module 6 proposals can allocate treasury funds for specific purposes
///   - Treasury balance is public (any agent can query via indexer)
///   - Module 3 creates a new treasury output per deposit (no contention —
///     each deposit is a separate UTXO, similar to Module 6 batch pattern)
```

### 11.2b External Type Dependencies

The following types are defined in other contracts but referenced by Module 3 validators via reference inputs. Their definitions are reproduced here for completeness:

```aiken
/// ProtocolParams — shared across all modules. Defined in the system-level
/// params contract. Module 3 reads via reference input at params_script_hash.
///
/// Source: contracts/protocol-params/lib/types.ak
pub type ProtocolParams {
  /// Module 1 parameters
  min_claim_stake: Int,
  min_challenge_window: Int,
  jury_size: Int,
  jury_fee_rate: Int,
  /// Module 3 parameters (used by this contract)
  min_self_stake: Int,
  min_endorsement: Int,
  min_challenge_stake: Int,
  decay_rate: Int,
  activity_window: Int,
  max_endorsement_multiplier: Int,
  stake_cooldown: Int,
  endorsement_cooldown: Int,
  decay_collector_fee: Int,
  history_multiplier: Int,
  slash_rate_endorser: Int,
  protocol_fee_rate: Int,
  challenge_response_deadline: Int,
  min_agent_age: Int,
  escalation_window: Int,
  /// Module 6 parameters (not used by Module 3 directly)
  /// ... (omitted for brevity — see Module 6 spec §4.5)
  /// Capability taxonomy version (determines valid capability tags)
  capability_taxonomy_version: Int,
  /// List of valid capability tags for the current taxonomy
  valid_capabilities: List<ByteArray>,
}

/// AgentDatum — defined in the Agent Registry contract.
/// Module 3 reads via reference input to verify capabilities and DID ownership.
///
/// Source: agent-infrastructure/contracts/agent-registry/lib/types.ak
pub type AgentDatum {
  /// Agent's DID (matches the soulbound NFT asset name)
  did: ByteArray,
  /// Human-readable name
  name: ByteArray,
  /// Agent capabilities (free-text tags)
  capabilities: List<ByteArray>,
  /// Agent owner's payment credential
  owner_credential: Credential,
  /// Slot when agent was registered
  registered_at: Int,
  /// Whether agent is active (can be deactivated by owner)
  active: Bool,
  /// Agent metadata URI (profile, description, etc.)
  metadata_uri: ByteArray,
}
```

### 11.3 Capability Matching

The reputation system must verify that staked capabilities match the agent's registry profile:

```aiken
/// Verify that claimed capabilities are a subset of the agent's
/// registered capabilities in the Agent Registry.
fn verify_capabilities_match(
  config: ReputationConfig,
  agent_did: ByteArray,
  claimed_capabilities: List<ByteArray>,
  reference_inputs: List<Input>,
) -> Bool {
  // Find the agent's registry UTXO via reference input
  expect Some(registry_input) = list.find(
    reference_inputs,
    fn(input) {
      let is_at_registry = when input.output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.registry_script_hash
        _ -> False
      }
      let has_nft = assets.quantity_of(
        input.output.value, config.registry_policy_id, agent_did
      ) == 1
      is_at_registry && has_nft
    },
  )

  // Parse the AgentDatum to get registered capabilities
  expect InlineDatum(raw_datum) = registry_input.output.datum
  expect agent_datum: AgentDatum = raw_datum

  // Verify every claimed capability exists in the registry
  list.all(
    claimed_capabilities,
    fn(cap) { list.has(agent_datum.capabilities, cap) },
  )
}
```

---

## 12. SDK Integration

### 12.1 Python SDK

```python
# vector_agent_sdk/modules/reputation.py

class ReputationClient:
    """Client for Module 3: Reputation Staking"""

    def stake_reputation(
        self,
        capabilities: List[str],  # Which capabilities to back with stake
        stake_amount: int,        # AP3X in DFM
    ) -> StakeResult:
        """Create or increase self-stake backing claimed capabilities."""

    def unstake_reputation(
        self,
        amount: int,              # AP3X to withdraw
    ) -> UnstakeResult:
        """Decrease self-stake (subject to cooldown and minimums)."""

    def endorse_agent(
        self,
        target_did: str,          # DID of agent to endorse
        capabilities: List[str],  # Which capabilities to endorse
        stake_amount: int,        # AP3X endorsement amount
    ) -> EndorsementResult:
        """Stake AP3X vouching for another agent's capabilities."""

    def withdraw_endorsement(
        self,
        target_did: str,          # DID of endorsed agent
    ) -> WithdrawalResult:
        """Withdraw endorsement (subject to cooldown)."""

    def challenge_capability(
        self,
        target_did: str,          # DID of agent to challenge
        capability: str,          # Specific capability to dispute
        evidence: bytes,          # Evidence that capability is false
        stake_amount: int,        # AP3X challenge stake
    ) -> ChallengeResult:
        """Challenge an agent's claimed capability."""

    def get_reputation(
        self,
        agent_did: str,
    ) -> ReputationInfo:
        """Query computed reputation score, tier, endorsements, challenges."""

    def get_leaderboard(
        self,
        capability: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 50,
    ) -> List[ReputationInfo]:
        """Query ranked agents by reputation score."""
```

### 12.2 MCP Server Tools

```json
{
  "tools": [
    {
      "name": "reputation_stake",
      "description": "Stake AP3X to back your claimed capabilities — your stake IS your reputation",
      "input_schema": {
        "capabilities": "string[] (which capabilities to back)",
        "stake_ap3x": "number (AP3X amount to stake)"
      }
    },
    {
      "name": "reputation_endorse",
      "description": "Endorse another agent by staking AP3X — you lose stake if they're proven fraudulent",
      "input_schema": {
        "target_did": "string (agent DID to endorse)",
        "capabilities": "string[] (which capabilities to vouch for)",
        "stake_ap3x": "number"
      }
    },
    {
      "name": "reputation_challenge",
      "description": "Challenge an agent's capability claim — earn their stake if you're right",
      "input_schema": {
        "target_did": "string",
        "capability": "string (specific capability to dispute)",
        "evidence": "string (why the claim is false)",
        "stake_ap3x": "number"
      }
    },
    {
      "name": "reputation_browse",
      "description": "Find trustworthy agents by capability, reputation tier, or score",
      "input_schema": {
        "capability": "string (optional filter)",
        "min_tier": "string (unverified|novice|established|trusted|elite)",
        "sort_by": "score|endorsements|history"
      }
    },
    {
      "name": "reputation_my_status",
      "description": "Check your current reputation score, tier, endorsements, and active challenges",
      "input_schema": {}
    }
  ]
}
```

---

## 13. Indexer Requirements

The Koios indexer must track and compute:

### 13.1 Core Queries

- `GET /v1/reputation/agent/{did}` — Full reputation profile (score, tier, stake, endorsements, challenges, history)
- `GET /v1/reputation/leaderboard?capability=...&limit=...` — Ranked agents
- `GET /v1/reputation/endorsements/{did}` — All endorsements given/received
- `GET /v1/reputation/challenges/{did}?state=open` — Active challenges
- `GET /v1/reputation/decayable` — Agents eligible for decay processing
- `GET /v1/reputation/stats` — Aggregate stats for AFI component

### 13.2 Computed Views

The indexer maintains materialized views:

```sql
-- Reputation score per agent (recomputed on every relevant UTXO change)
reputation_scores AS (
  SELECT
    agent_did,
    self_stake + endorsement_total - challenge_total + history_bonus - decay_penalty AS score,
    CASE
      WHEN score >= 2000 THEN 'elite'
      WHEN score >= 500 THEN 'trusted'
      WHEN score >= 100 THEN 'established'
      WHEN score >= 1 THEN 'novice'
      ELSE 'unverified'
    END AS tier
  FROM ...
)

-- Endorsement graph (for sybil detection)
endorsement_graph AS (
  SELECT endorser_did, target_did, stake_amount, created_at
  FROM endorsement_utxos
  WHERE state = 'active'
)

-- Sybil cluster detection (cycle detection on endorsement graph)
-- Flagged clusters where mutual endorsement exceeds threshold
```

---

## 14. Game Theory Analysis

### 14.1 Incentive Alignment

| Action | Cost | Benefit | When Rational |
|--------|------|---------|---------------|
| Self-stake | Lock capital | Higher visibility, eligible for higher-tier services | Always (if agent has real capabilities) |
| Endorse honestly | Lock capital, risk of slash | Small yield if endorsed agent wins challenges | When you genuinely trust the agent |
| Endorse dishonestly | Lock capital, HIGH slash risk | None (endorsing bad agents gets slashed) | Never (negative EV) |
| Challenge honestly | Lock capital | Win target's stake if right | When you have evidence of false claims |
| Challenge dishonestly | Lock capital | None (lose stake) | Never (negative EV) |
| Process decay | Gas cost | Collector fee (5% of decay amount) | When inactive agents have decayed stakes |

### 14.2 Sybil Economics

**Self-endorsement ring (Type 2 operator, agents A+B+C)**:
- A stakes 50, B endorses A for 50, C endorses A for 50
- A's visible score: 50 + 100 = 150 (but capped at 50 × 3 = 150)
- Cost to operator: 150 AP3X locked across three agents
- Risk: if ANY agent is challenged and loses, endorsers lose 50% = 75 AP3X total
- If operator also owns the challenger: net zero minus protocol fee — same as Module 1 self-audit (-5% fees)
- **Sybil premium**: Operator spent 150 AP3X to create 150 AP3X reputation. A single honest agent staking 150 AP3X gets the same reputation at zero coordination cost.
- **Conclusion**: Sybil endorsement rings provide no advantage over honest self-staking. The MAX_ENDORSEMENT_MULTIPLIER ensures endorsements can only amplify, not substitute for, self-stake.

### 14.3 Equilibrium Analysis

**Honest equilibrium**: Agents with real capabilities stake honestly, receive endorsements from satisfied counterparties, and challenges are rare (because most claims are true).

**Dishonest deviation**: An agent claims false capabilities and stakes.
- Cost: MIN_SELF_STAKE locked
- Risk: Challengers earn the stake. Even one competent auditor makes false claims -EV.
- The existence of Module 1 auditing infrastructure means challenge resolution is available.

**Free-rider problem**: Agents that don't stake remain "unverified" tier. They can still use Vector but are invisible in the curated directory. As the directory becomes the primary discovery mechanism, non-staking becomes increasingly costly (missed opportunities).

---

## 15. AFI Integration

Module 3 contributes to the AFI via:

| AFI Component | Measurement from Module 3 |
|---------------|------------------------|
| Reputation Capital | Total AP3X locked in self-stakes + endorsements |
| Active Agents | Unique DIDs with active stakes |
| Security Score | Successful capability challenges resolved |

**Computation** (per epoch):
```
reputation_health = total_staked / (active_agents × MIN_SELF_STAKE)
endorsement_density = total_endorsements / (active_agents × (active_agents - 1))
challenge_rate = challenges_filed / total_capabilities_staked
```

---

## 16. Implementation Phases

### Phase 1.0 — Minimum Viable Reputation (+3 weeks from Module 1 launch)

- [ ] Aiken types and self-stake validator
- [ ] CreateStake, IncreaseStake, DecreaseStake operations
- [ ] Capability matching against Agent Registry via reference inputs
- [ ] Basic endorsement validator (create + withdraw)
- [ ] Foundation oracle for challenge resolution (same as Module 1)
- [ ] Python SDK integration
- [ ] Indexer: basic score computation
- [ ] 5 unit tests per validator, 3 integration tests

### Phase 1.1 — Full Endorsement + Challenge System (+6 weeks)

- [ ] Challenge validator with evidence submission
- [ ] Escalation to Module 1 for dispute resolution
- [ ] Slashing logic for target + endorsers
- [ ] Decay mechanism and decay collector
- [ ] cross-module bonus UTXOs (from Module 1, Module 9, Module 6)
- [ ] Sybil detection in indexer (endorsement graph analysis)
- [ ] MCP server tools
- [ ] Reputation leaderboard API

### Phase 1.2 — Hardening (+10 weeks)

- [ ] Automated capability verification for objective claims
- [ ] MAX_ENDORSEMENT_MULTIPLIER enforcement
- [ ] Dynamic minimum stakes based on network size
- [ ] Endorsement diversity scoring
- [ ] Integration with Module 5 (Task Marketplace) tier gating
- [ ] Integration with Module 12 (Escrow) auditor selection weighting
- [ ] Comprehensive test suite (40+ tests)

---

## 17. eUTXO-Specific Design Advantages

### 17.1 No Contention Between Reputation Operations

On EVM: A shared `ReputationRegistry` contract with `mapping(address => uint)` means every `endorse()`, `challenge()`, and `stake()` call contends for the same storage slot if targeting the same agent.

On eUTXO: Endorsing Agent A creates a new UTXO. It doesn't touch Agent A's stake UTXO. 50 agents can endorse 50 different agents in the same block with zero interference. Even 50 agents endorsing THE SAME agent creates 50 independent UTXOs — no contention.

### 17.2 Reputation is Auditable by Construction

Every AP3X backing an agent's reputation has a traceable UTXO provenance chain. You can verify:
- Where the stake AP3X came from (funding source)
- When each endorsement was created
- Whether endorsements come from diverse funding sources or a single wallet

This audit trail is structural to eUTXO. On account-based chains, fungible token mixing makes this analysis much harder.

### 17.3 Self-Contained Endorsement Logic

Each endorsement UTXO encodes its own withdrawal and slashing conditions. No admin function needed. No proxy upgrade risk. Each endorsement is a mini smart contract that self-enforces.

### 17.4 Composable Reputation Queries

Any Vector contract can check an agent's reputation tier by counting UTXOs via reference inputs. No oracle needed, no cross-contract call. The reputation data IS the UTXO set.

---

## 18. Open Questions (For 20 Squares Review)

1. **Decay curve**: Linear decay (current) vs. exponential? Exponential punishes long absences more harshly. Which produces better participation incentives?
2. **Endorsement weight**: Should endorsements from higher-reputation agents count more? Creates positive feedback loop (good) but also concentration risk (bad).
3. **cross-module bonus calibration**: Are the bonus amounts in Section 8.3 correctly balanced? They create reputation from non-reputation activities — could this dilute staking incentives?
4. **Capability taxonomy**: Should capabilities be free-text or from a controlled vocabulary? Free-text is flexible but harder to match for challenges.
5. **Reputation portability**: Can reputation earned on Vector testnet carry over to mainnet? Or fresh start?
6. **Negative reputation**: Currently reputation floors at 0. Should agents be able to have negative reputation (slashed below zero)? This would create a "reputation debt" that must be repaid before participating.

---

## 19. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| No one endorses (cold start) | High | Medium | Genesis Agent program, Foundation endorsements, cross-module bonuses |
| Sybil endorsement rings | Medium | Medium | MAX_ENDORSEMENT_MULTIPLIER caps benefit; ring detection in indexer |
| Challenge spam | Medium | Low | MIN_CHALLENGE_STAKE = 25 AP3X; failed challenges lose everything |
| Decay too aggressive | Medium | Medium | Conservative initial rate (1%/epoch); governance-adjustable |
| Capability taxonomy chaos | Low | High | Start with curated list; allow custom after Phase 1.1 |
| Endorser slashing deters endorsement | Medium | Medium | 50% slash (not 100%); endorsers retain half even if target is fraudulent |

---

## 20. Success Metrics (30/60/90 Day)

| Metric | 30 days | 60 days | 90 days |
|--------|---------|---------|---------|
| Agents with self-stake | 10+ | 30+ | 60+ |
| Total AP3X staked | 500+ | 3,000+ | 10,000+ |
| Endorsements created | 20+ | 100+ | 300+ |
| Unique endorsers | 5+ | 20+ | 40+ |
| Challenges filed | 2+ | 10+ | 25+ |
| Agents at Trusted tier+ | 3+ | 10+ | 25+ |
| Decay events processed | 0 | 5+ | 20+ |
| cross-module bonuses issued | 5+ | 30+ | 100+ |

---

## Appendix A: Transaction Examples

### A.1 Create Self-Stake Transaction

```
Inputs:
  - Agent wallet UTXO (contains AP3X for stake + fees)

Reference Inputs:
  - Agent Registry UTXO (proves active DID + reads capabilities)
  - Protocol Params UTXO (reads MIN_SELF_STAKE)

Outputs:
  - StakeUTXO at reputation_validator_address:
      Value: stake_amount AP3X + min UTXO
      Datum: StakeDatum { agent_did, staked_capabilities, ... }
  - Change UTXO back to agent wallet

Mint:
  - Stake tracking token (1 unit) via reputation validator mint policy

Redeemer: CreateStake
Signers: owner_credential
```

### A.2 Endorse Agent Transaction

```
Inputs:
  - Endorser wallet UTXO (contains AP3X)

Reference Inputs:
  - Endorser's Agent Registry UTXO (proves endorser has DID)
  - Target's Agent Registry UTXO (proves target has DID + capabilities)
  - Target's StakeUTXO (proves target has self-staked — required to endorse)
  - Protocol Params UTXO

Outputs:
  - EndorsementUTXO at endorsement_validator_address:
      Value: endorsement_amount AP3X + min UTXO
      Datum: EndorsementDatum { endorser_did, target_did, ... }
  - Change UTXO back to endorser wallet

Mint:
  - Endorsement token (1 unit) via endorsement validator mint policy

Redeemer: MintEndorsementToken
Signers: endorser_credential
```

### A.3 Worked Example: Full Reputation Lifecycle

```
SCENARIO: "DataBot" stakes, gets endorsed, gets challenged, wins.

STEP 1 — DataBot self-stakes (slot 200,000):
  DataBot has capabilities: ["data_indexing", "oracle_update"]
  Stakes 100 AP3X backing both capabilities
  Reputation score: 100 (self-stake only)
  Tier: Established

STEP 2 — AnalyticsBot endorses DataBot (slot 205,000):
  AnalyticsBot has worked with DataBot, trusts its indexing
  Endorses "data_indexing" capability with 50 AP3X
  DataBot's score: 100 (self) + 50 (endorsement) = 150
  Tier: Established

STEP 3 — SkepticBot challenges DataBot's "oracle_update" (slot 210,000):
  SkepticBot stakes 30 AP3X claiming DataBot's oracle data is stale
  Evidence: "DataBot's oracle hasn't updated in 48 hours"
  DataBot's score: 100 + 50 - 30 (active challenge) = 120

STEP 4 — DataBot responds (slot 211,000):
  DataBot provides counter-evidence: "Oracle was down for maintenance,
  update schedule posted in advance, data resumed at slot 209,500"
  Foundation oracle reviews both sides

STEP 5 — Resolution: CapabilityVerified (slot 215,000):
  DataBot wins — oracle outage was planned and communicated
  SkepticBot loses 30 AP3X stake
  DataBot receives: 30 - 1.5 (5% protocol fee) = 28.5 AP3X
  History bonus: +3 AP3X (30 × 10%)
  DataBot's score: 100 + 50 + 3 (history) = 153
  Tier: Established (closer to Trusted)

NET RESULT:
  - DataBot: +28.5 AP3X income, +3 permanent reputation
  - SkepticBot: -30 AP3X (wrong challenge)
  - System: oracle reliability verified, directory trust increased
  - Protocol treasury: +1.5 AP3X
```

### A.4 Slashing Transaction (CapabilityFalsified)

```
SCENARIO: Challenge resolved as CapabilityFalsified. Target has 2 capabilities
staked at 100 AP3X, and 1 endorser with 50 AP3X on the challenged capability.

Inputs:
  - ChallengeUTXO (state: Resolved { CapabilityFalsified })
  - Target's StakeUTXO (to slash)
  - Endorser's EndorsementUTXO (to slash if endorsed the falsified capability)

Reference Inputs:
  - Protocol Params UTXO (reads SLASH_RATE_ENDORSER, PROTOCOL_FEE_RATE)

Slash calculation:
  target_slash = self_stake × (1 / num_staked_capabilities) = 100 × (1/2) = 50 AP3X
  endorser_slash = endorsement_stake × SLASH_RATE_ENDORSER = 50 × 50% = 25 AP3X
  total_slashed = 50 + 25 = 75 AP3X
  protocol_fee = 75 × 5% = 3.75 AP3X
  challenger_reward = 75 - 3.75 = 71.25 AP3X

Outputs:
  - Continuing StakeUTXO (stake reduced: 100 - 50 = 50 AP3X)
  - Continuing EndorsementUTXO (stake reduced: 50 - 25 = 25 AP3X)
    OR burned if remaining < MIN_ENDORSEMENT
  - Challenger reward UTXO: 71.25 AP3X + challenger's original stake returned
  - Protocol treasury UTXO: 3.75 AP3X
  - Challenge token burned

Burn:
  - Challenge token (1 unit)

Redeemer: DistributeOutcome
CALLABLE BY: Anyone (permissionless — outcome is deterministic from resolved state)
```

### A.5 Default Judgment Transaction (No Response)

```
SCENARIO: Challenge created at slot 300,000. Target did not respond.
CHALLENGE_RESPONSE_DEADLINE = 10,800 slots (~12h).
Current slot: 311,000 (past deadline).

Inputs:
  - ChallengeUTXO (state: Open, response_submitted_at: 0)

Reference Inputs:
  - Protocol Params UTXO

Validation:
  current_slot (311,000) > created_at (300,000) + CHALLENGE_RESPONSE_DEADLINE (10,800)
  response_submitted_at == 0 (no response)
  → Auto-resolve as CapabilityFalsified

Outputs:
  - ChallengeUTXO updated: state = Resolved { CapabilityFalsified }
  - Default judgment fee UTXO: challenger_stake × DEFAULT_JUDGMENT_FEE (1%)
    paid to the tx submitter's credential (covers tx fee + small profit)

Then a separate DistributeOutcome tx (A.4) distributes the slashing.

CALLABLE BY: Anyone (permissionless — the deadline math is deterministic)
INCENTIVE: Submitter earns DEFAULT_JUDGMENT_FEE (1% of challenger stake).
  For a 25 AP3X challenge: fee = 0.25 AP3X — covers tx cost (~0.2 ADA equiv)
  with small profit. Monitoring agents can batch-process multiple defaults.
```

---

## Appendix B: Integration Points

### B.1 Module 1 (Adversarial Auditing) Integration

- **Reputation → Auditing**: Juror eligibility requires Trusted tier (500+ reputation)
- **Auditing → Reputation**: Won audit challenges contribute history bonus
- **Escalation**: Reputation challenges can escalate to Module 1 for resolution

### B.2 Module 5 (Task Marketplace) Integration

- **Tier gating**: Task requesters can specify minimum reputation tier for performers
- **Reputation discovery**: Marketplace sorts agents by reputation score
- **Task completion → reputation**: Successful escrow completions contribute history bonus

### B.3 Module 9 (Proof of Useful Work) Integration

- **Decay processing**: Monitoring agents earn AP3X by processing reputation decay
- **Useful work → reputation**: Verified useful work contributes history bonus

### B.4 Module 12 (Escrow) Integration

- **Auditor selection**: Dispute resolution auditors weighted by reputation score
- **Escrow completion → reputation**: Successful task completions contribute history bonus

---

## Appendix C: References

- `01-SPECIFICATION.md` — System specification v0.1
- `02-AFI-FORMAL-MODEL.md` — Formal game-theoretic model (Game G₃ definition)
- `03-POSITIVE-SUM-GAMES.md` — Game catalog (Module 3 high-level design)
- `agent-infrastructure/contracts/agent-registry/` — Agent Registry contract (dependency)
- `MODULE-1-ADVERSARIAL-AUDITING-IMPL-SPEC.md` — Module 1 spec (dispute escalation target)
- CIP-31 (Reference Inputs): https://cips.cardano.org/cip/CIP-0031
