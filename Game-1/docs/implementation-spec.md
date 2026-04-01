# Game 1: Adversarial Auditing — Implementation Specification

**Status**: DRAFT v0.3
**Author**: Apex Fusion Team, with AI-assisted design
**Date**: 2026-03-20
**Dependencies**: Agent Registry contract (deployed), AP3X native token
**Phase**: 1 (Traction — requires ~10 active agents)
**Target**: Vector eUTXO L2

---

## 1. Executive Summary

Adversarial Auditing is a stake-based challenge-response game where agents stake AP3X to challenge the correctness of other agents' on-chain claims. It serves as the **dispute resolution layer** for the entire Vector agent economy (Core Stack: Games 1 + 3 + 5 + 12).

Selfish auditors seeking profit create system-wide integrity as a side effect — the Bitcoin analogy applied to trust verification.

---

## 2. System Architecture

### 2.1 Contract Topology

Three Aiken multi-validators, each with mint + spend handlers:

```
┌─────────────────────────────────────────────────────────────┐
│                    ADVERSARIAL AUDITING                      │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐                 │
│  │  Claim Validator  │  │ Challenge        │                 │
│  │  (claim.ak)       │  │ Validator        │                 │
│  │                    │  │ (challenge.ak)   │                 │
│  │  - SubmitClaim     │  │  - OpenChallenge │                 │
│  │  - WithdrawClaim   │  │  - SubmitEvidence│                 │
│  │  - ForfeitClaim    │  │  - ResolveJury   │                 │
│  └────────┬───────────┘  │  - TimeoutResolve│                 │
│           │              └────────┬─────────┘                 │
│           │                       │                           │
│           └───────┬───────────────┘                           │
│                   ▼                                           │
│  ┌──────────────────────────────────┐                        │
│  │  Jury Pool Validator              │                        │
│  │  (jury_pool.ak)                   │                        │
│  │                                    │                        │
│  │  - RegisterJuror                   │                        │
│  │  - SelectJury (VRF-seeded random)  │                        │
│  │  - CastVote                        │                        │
│  │  - DistributeRewards               │                        │
│  └────────────────────────────────────┘                        │
│                                                              │
│  External dependency: Agent Registry (existing)              │
│  - Claimer must have active DID                              │
│  - Auditor must have active DID                              │
│  - Jurors must have active DID + minimum reputation          │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 UTXO Flow

Each claim and each challenge is an **independent UTXO** — fully parallelizable. No global state contention. Thousands of audits run simultaneously without interference.

```
HAPPY PATH (no challenge):
  Agent A submits claim → ClaimUTXO created (locked with stake)
  ... W blocks pass (challenge window) ...
  No challenge → Agent A withdraws claim + stake

CHALLENGE PATH:
  Agent A submits claim → ClaimUTXO created
  Agent B opens challenge → ChallengeUTXO created (references ClaimUTXO)
  Both submit evidence → Evidence attached to ChallengeUTXO
  Jury selected (VRF random from pool) → JuryUTXOs created
  Jurors vote → Votes recorded on JuryUTXOs
  Resolution → Winner takes loser's stake (minus jury fee)

TIMEOUT PATH:
  Challenge opened → ... deadline passes, no jury resolution ...
  → Stakes returned to both parties (no winner)

CONTENTION PATH (multiple auditors target same claim):
  Agent A submits claim → ClaimUTXO created (state: Open)
  Agent B builds challenge tx consuming ClaimUTXO
  Agent C builds challenge tx consuming same ClaimUTXO
  → Only ONE succeeds (eUTXO guarantees: UTXO can only be consumed once)
  → Loser's tx fails deterministically at zero cost (no gas wasted)
  → This is a FEATURE: first-challenger-wins creates a race to audit, not congestion

FOUNDATION ORACLE PATH (Phase 1.0 — before jury pool exists):
  Agent A submits claim → ClaimUTXO created
  Agent B opens challenge → ChallengeUTXO created (state: PendingOracle)
  Foundation reviews evidence off-chain
  Foundation submits OracleResolve tx → stakes distributed per verdict
  → Single trusted resolver, simple, works with 2 agents
  → Transition trigger: jury pool reaches MIN_JURY_POOL_SIZE (10 jurors)
```

---

## 3. On-Chain Types

### 3.1 Claim Types

```aiken
/// A claim submitted by an agent asserting it performed some task
pub type ClaimDatum {
  /// DID of the claiming agent (policy_id of their registry NFT)
  claimer_did: PolicyId,
  /// Payment credential of claimer (for stake return)
  claimer_credential: Credential,
  /// blake2b_256 hash of the off-chain claim data
  /// The full claim data is stored off-chain (OriginTrail or IPFS)
  claim_hash: ByteArray,
  /// Category tag for claim routing (e.g. "task_completion", "capability", "data_integrity")
  claim_type: ByteArray,
  /// Off-chain storage URI for full evidence (OriginTrail UAL or IPFS CID)
  /// Not validated on-chain — used by auditors/jurors to retrieve evidence
  storage_uri: ByteArray,
  /// AP3X staked by claimer (in lovelace-equivalent DFM units)
  stake_amount: Int,
  /// Slot number when claim was submitted
  submitted_at: Int,
  /// Challenge window: number of slots after submission during which challenges are accepted
  challenge_window: Int,
  /// Current state of this claim
  state: ClaimState,
}

pub type ClaimState {
  /// Open for challenges
  Open
  /// Challenge has been filed — locked until resolution
  Challenged
  /// Claim validated (no challenge or won challenge)
  Validated
  /// Claim invalidated (lost challenge)
  Invalidated
}

pub type ClaimAction {
  /// Submit a new claim with stake
  SubmitClaim
  /// Withdraw a validated claim after challenge window (no challenge received)
  WithdrawClaim
  /// Forfeit claim after losing challenge (called by resolution logic)
  ForfeitClaim
}
```

### 3.2 Challenge Types

```aiken
/// A challenge to an existing claim
pub type ChallengeDatum {
  /// Reference to the claim UTXO being challenged
  claim_ref: OutputReference,
  /// DID of the challenging agent
  auditor_did: PolicyId,
  /// Payment credential of auditor
  auditor_credential: Credential,
  /// AP3X staked by auditor (must be >= claim stake)
  stake_amount: Int,
  /// blake2b_256 hash of auditor's counter-evidence (stored off-chain)
  evidence_hash: ByteArray,
  /// Off-chain storage URI for counter-evidence
  evidence_uri: ByteArray,
  /// Slot when challenge was opened
  challenged_at: Int,
  /// Resolution deadline: slots after challenge_at for jury to resolve
  resolution_deadline: Int,
  /// Current state
  state: ChallengeState,
}

pub type ChallengeState {
  /// Awaiting Foundation oracle resolution (Phase 1.0)
  PendingOracle
  /// Awaiting jury selection (Phase 1.1+)
  PendingJury
  /// Jury selected, awaiting votes
  Voting
  /// Resolved — verdict rendered
  Resolved { verdict: Verdict }
}

pub type Verdict {
  /// Claimer was correct — auditor loses stake
  ClaimerWins
  /// Auditor was correct — claimer loses stake
  AuditorWins
  /// Inconclusive — both stakes returned minus jury fee
  Inconclusive
}

pub type ChallengeAction {
  /// Open a new challenge against a claim
  OpenChallenge
  /// Submit additional evidence (before jury selection)
  SubmitEvidence
  /// Trigger jury resolution
  ResolveJury { jury_votes: List<JuryVote> }
  /// Resolve by timeout (no jury action within deadline)
  TimeoutResolve
}
```

### 3.3 Jury Types

```aiken
/// A juror registered in the jury pool
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
  /// Slot when juror registered
  registered_at: Int,
}

pub type JuryVote {
  /// Which juror cast this vote
  juror_did: PolicyId,
  /// The verdict this juror selected
  verdict: Verdict,
  /// blake2b_256 hash of juror's reasoning (stored off-chain)
  reasoning_hash: ByteArray,
}

pub type JuryAction {
  /// Register as a juror (stake bond)
  RegisterJuror
  /// Withdraw from jury pool (only if not assigned to active case)
  WithdrawJuror
  /// Cast vote on a challenge
  CastVote { challenge_ref: OutputReference, vote: Verdict }
  /// Claim jury reward after resolution
  ClaimReward
}
```

### 3.4 Foundation Oracle Types (Phase 1.0)

```aiken
/// Foundation oracle resolution — used before jury pool is active
/// The Foundation's signing key is the trusted resolver
pub type OracleDatum {
  /// Foundation's payment credential (multi-sig recommended)
  oracle_credential: Credential,
  /// Minimum number of jurors required to transition away from oracle mode
  min_jury_pool_size: Int,
  /// Whether oracle mode is active (false = jury mode)
  oracle_active: Bool,
}

pub type OracleAction {
  /// Foundation resolves a challenge
  OracleResolve { challenge_ref: OutputReference, verdict: Verdict, reasoning_hash: ByteArray }
  /// Deactivate oracle mode (triggered when jury pool reaches threshold)
  DeactivateOracle
}
```

### 3.5 Claim Token Lifecycle

Each claim mints a **claim tracking token** (CIP-68 pattern) that enforces lifecycle:

```aiken
/// Claim token asset name = "claim_" ++ blake2b_256(claim_utxo_ref)[0..28]
/// This token MUST travel with the claim UTXO through state transitions.
/// It is burned when the claim is finalized (withdrawn, forfeited, or resolved).
///
/// Why a token? Without it, anyone could create a fake "claim UTXO" at the
/// validator address. The token proves the UTXO was created via SubmitClaim.
///
/// Similarly, challenge tokens ("chal_" prefix) prove challenge legitimacy.
```

---

## 4. Validation Rules

### 4.1 Claim Submission (`SubmitClaim`)

```
MUST:
  1. Claimer has active DID in Agent Registry (NFT exists at registry address)
  2. stake_amount >= MIN_CLAIM_STAKE (parameter, initially 50 AP3X)
  3. claim_hash is exactly 32 bytes (blake2b_256)
  4. claim_type is non-empty
  5. challenge_window >= MIN_CHALLENGE_WINDOW (parameter, initially 1800 slots = ~2 hours)
  6. Output UTXO at claim validator address contains:
     - Inline datum with state = Open
     - Value includes stake_amount AP3X
  7. Transaction signed by claimer_credential
```

### 4.2 Claim Withdrawal (`WithdrawClaim`)

```
MUST:
  1. Claim state == Open (never challenged)
  2. Current slot > submitted_at + challenge_window (window expired)
  3. Stake returned to claimer_credential
  4. No continuing output at claim address (UTXO consumed)
  5. Transaction signed by claimer_credential
```

### 4.3 Open Challenge (`OpenChallenge`)

```
MUST:
  1. Referenced claim exists and state == Open
  2. Auditor has active DID in Agent Registry
  3. Auditor DID != Claimer DID (cannot self-audit)
  4. Current slot <= claim.submitted_at + claim.challenge_window
  5. auditor stake_amount >= claim stake_amount (must match or exceed)
  6. evidence_hash is exactly 32 bytes
  7. Claim UTXO updated: state = Open → Challenged (continuing output)
  8. Challenge UTXO created at challenge validator:
     - If oracle_active (Phase 1.0): state = PendingOracle
     - If jury mode (Phase 1.1+): state = PendingJury
  9. Transaction signed by auditor_credential

ANTI-SYBIL:
  10. DID graph check (off-chain, enforced by jury selection):
      - Auditor and claimer DIDs must not share registration transaction inputs
      - Auditor must have been registered for >= MIN_AGENT_AGE slots (initially 21600 = ~24 hours)
```

### 4.4 Jury Selection

Jury selection is **pseudo-random and deterministic**, seeded by on-chain entropy.

**Critical design note**: eUTXO has no global state — you cannot "enumerate all jurors" in a validator script. The selection happens in two layers:

```
JURY_SIZE = 5 (parameter — must be odd for majority)

═══ LAYER 1: OFF-CHAIN (Indexer + SDK) ═══

The indexer maintains a view of all JurorDatum UTXOs at the jury_pool validator address.
Any agent (or the SDK automatically) can query the indexer to compute the jury:

  1. Query indexer: GET /v1/auditing/jurors?eligible=true
     Returns all juror UTXOs sorted by juror_did (deterministic ordering)
  2. Compute selection seed:
     seed = blake2b_256(challenge_utxo_ref ++ block_header_hash(challenge_slot + SELECTION_DELAY))
     SELECTION_DELAY = 10 slots (~40 seconds) — prevents block producer from
     manipulating jury by choosing which block to produce
  3. For i in 0..JURY_SIZE:
       index = int_from_bytes(blake2b_256(seed ++ i)) % eligible_count
       selected[i] = eligible[index]
       Remove selected juror from pool (no duplicates)
  4. Build the "activate jury" transaction with selected juror UTXOs as inputs

═══ LAYER 2: ON-CHAIN (Validator) ═══

The jury_pool validator verifies the selection was computed correctly:

  1. Receives the challenge UTXO ref and juror UTXO refs as inputs
  2. Reads block_header_hash from protocol parameters or transaction validity range
  3. Recomputes the selection seed and indices
  4. Verifies the provided juror UTXOs match the computed selection
  5. If mismatch → transaction fails (someone tried to pick their own jury)

This is the standard eUTXO pattern: compute off-chain, verify on-chain.
The validator doesn't enumerate — it CHECKS that the enumeration was done correctly.

Eligibility (checked by validator):
  - Juror UTXO exists at jury_pool validator address (not a fake UTXO)
  - Juror bond >= MIN_JUROR_BOND (initially 25 AP3X)
  - juror_did != claimer_did AND juror_did != auditor_did
  - cases_resolved >= 0 (any registered juror can serve initially)
```

### 4.5 Jury Voting (`CastVote`)

```
MUST:
  1. Voter is one of the selected jurors for this challenge
  2. Challenge state == Voting
  3. Current slot <= challenge.challenged_at + challenge.resolution_deadline
  4. Vote is one of: ClaimerWins, AuditorWins, Inconclusive
  5. Each juror votes exactly once (vote UTXO created, not updateable)
  6. Transaction signed by juror_credential

COMMIT-REVEAL (optional enhancement for Phase 1.1):
  - Phase A: jurors submit hash(vote ++ salt) within voting window
  - Phase B: jurors reveal vote + salt within reveal window
  - Prevents jurors from copying majority vote
```

### 4.6 Resolution (`ResolveJury`)

```
TRIGGERABLE BY: Any agent (permissionless — the math is deterministic)

MUST:
  1. All JURY_SIZE votes have been cast, OR resolution_deadline has passed
  2. Tally votes:
     - If >= SUPERMAJORITY (3 of 5) agree on ClaimerWins or AuditorWins → that verdict
     - If no supermajority → Inconclusive
  3. Distribute stakes:

     ClaimerWins:
       claimer receives: claim_stake + auditor_stake - jury_fee
       auditor receives: 0 (forfeits entire stake)
       jury_pool: jury_fee (split equally among jurors who voted with majority)

     AuditorWins:
       auditor receives: auditor_stake + claim_stake - jury_fee
       claimer receives: 0 (forfeits entire stake)
       jury_pool: jury_fee (split equally among jurors who voted with majority)

     Inconclusive:
       claimer receives: claim_stake - (jury_fee / 2)
       auditor receives: auditor_stake - (jury_fee / 2)
       jury_pool: jury_fee (split equally among ALL jurors)

  4. Update claim state: Validated or Invalidated
  5. Jurors who voted AGAINST majority receive no reward (but keep bond)
  6. Jurors who didn't vote at all: bond slashed by JUROR_SLASH_RATE (initially 10%)
```

### 4.7 Foundation Oracle Resolution (`OracleResolve` — Phase 1.0)

```
MUST:
  1. Oracle UTXO exists as reference input with oracle_active == True
  2. Transaction signed by oracle_credential (Foundation multi-sig)
  3. Challenge exists and state == PendingOracle
  4. Verdict is one of: ClaimerWins, AuditorWins, Inconclusive
  5. reasoning_hash is exactly 32 bytes (Foundation publishes reasoning off-chain)
  6. Stake distribution follows same rules as jury resolution (Section 4.6)
     EXCEPT: no jury fee — Foundation absorbs resolution cost
  7. Claim state updated to Validated or Invalidated
  8. Both claim token and challenge token burned

TRANSITION TO JURY MODE:
  9. Foundation can call DeactivateOracle when:
     - Number of registered jurors >= min_jury_pool_size (10)
     - Total juror bonds >= MIN_JURY_POOL_TOTAL (initially 250 AP3X)
  10. Once deactivated, OracleResolve transactions are rejected
  11. Deactivation is one-way (cannot re-enable oracle without contract upgrade)
```

### 4.8 Timeout Resolution (`TimeoutResolve`)

```
MUST:
  1. Current slot > challenged_at + resolution_deadline
  2. Fewer than JURY_SIZE votes cast
  3. Both stakes returned in full (minus fee for any jurors who did vote)
  4. Non-voting jurors: bonds slashed by JUROR_SLASH_RATE
```

---

## 5. Parameters

All parameters are governance-adjustable (Game 6 pathway).

| Parameter | Initial Value | Unit | Rationale |
|-----------|--------------|------|-----------|
| `MIN_CLAIM_STAKE` | 50 | AP3X | Low enough for Type 1 agents, high enough to deter spam |
| `MIN_CHALLENGE_WINDOW` | 1,800 | slots (~2h) | Enough time for auditors to discover and evaluate claims |
| `MAX_CHALLENGE_WINDOW` | 64,800 | slots (~3d) | Prevents indefinite capital lockup |
| `JURY_SIZE` | 5 | agents | Odd number for majority; small enough to find jurors |
| `MIN_JUROR_BOND` | 25 | AP3X | Skin in the game for jurors |
| `JURY_FEE_RATE` | 10 | % of loser's stake | Compensates jurors; creates auditing demand |
| `SELECTION_DELAY` | 10 | slots (~40s) | Prevents block producer from manipulating jury seed |
| `RESOLUTION_DEADLINE` | 5,400 | slots (~6h) | Enough time for jury deliberation |
| `JUROR_SLASH_RATE` | 10 | % of bond | Deters non-participation after selection |
| `MIN_AGENT_AGE` | 21,600 | slots (~24h) | Anti-sybil: agents must exist before auditing |
| `MAX_CONCURRENT_CASES` | 5 | cases/juror | Prevents juror overload |
| `MIN_JURY_POOL_SIZE` | 10 | jurors | Threshold to transition from oracle to jury mode |
| `MIN_JURY_POOL_TOTAL` | 250 | AP3X | Minimum total bonds in jury pool for activation |

---

## 6. Anti-Collusion Mechanisms

### 6.1 Self-Auditing Prevention

**Problem**: Type 2 operator runs both claimer and auditor. They stage fake challenges to farm jury fees or manipulate reputation.

**Mitigations** (layered):

1. **On-chain rule**: `auditor_did != claimer_did` (prevents trivial self-audit)
2. **Randomized jury**: Even if claimer and auditor collude, the jury is randomly selected and cannot be controlled
3. **Stake symmetry**: Auditor must stake >= claimer's stake. Self-auditing is net-zero minus jury fees and tx costs — it's *unprofitable by design*
4. **DID graph analysis** (off-chain monitoring, Phase 1.1):
   - Track auditor-claimer pair frequency
   - Flag DIDs that audit each other with statistical anomaly detection
   - If flagged, require higher stakes for those pairs (dynamic risk pricing)
5. **Audit diversity requirement** (Phase 1.1):
   - No agent can be audited by the same auditor more than 20% of the time
   - Enforced by tracking audit history on-chain or via indexer

### 6.2 Jury Collusion Prevention

**Problem**: Jurors coordinate votes off-chain.

**Mitigations**:

1. **Random selection**: Jurors don't know they'll be selected until after `SELECTION_DELAY`
2. **Commit-reveal** (Phase 1.1): Jurors commit vote hash before seeing others' votes
3. **Minority penalty**: Jurors who vote against majority get no reward (incentivizes honest independent evaluation, not coordination)
4. **Bond at risk**: Non-participation is slashed, so jurors can't boycott unfavorable cases

### 6.3 Fake Claim Farming

**Problem**: Agent submits many low-quality claims hoping no one challenges them, building reputation for free.

**Mitigations**:

1. **Minimum stake**: 50 AP3X per claim locks capital
2. **Claim type validation** (Phase 1.1): Certain claim types require minimum evidence quality
3. **Reputation integration** (Game 3 synergy): Claims from low-reputation agents are flagged for priority auditing
4. **Economic deterrent**: If auditors exist, submitting false claims is -EV (you lose stake)

---

## 7. Contract Architecture (Aiken)

### 7.1 File Structure

```
contracts/adversarial-auditing/
├── aiken.toml
├── validators/
│   ├── claim.ak              # Claim multi-validator (mint claim token + spend claim UTXO)
│   ├── challenge.ak           # Challenge multi-validator
│   └── jury_pool.ak           # Jury registration + selection + voting
├── lib/
│   └── adversarial_auditing/
│       ├── types.ak           # All types from Section 3
│       ├── params.ak          # Protocol parameters (reference input)
│       ├── claim_validation.ak    # Claim validation logic
│       ├── challenge_validation.ak # Challenge validation logic
│       ├── jury_validation.ak     # Jury selection + voting logic
│       ├── jury_selection.ak      # VRF-seeded random selection algorithm
│       └── utils.ak           # Shared helpers (DID verification, stake math)
└── tests/
    ├── claim_tests.ak
    ├── challenge_tests.ak
    ├── jury_tests.ak
    └── integration_tests.ak
```

### 7.2 Cross-Validator References

The three auditing validators need to reference each other and the Agent Registry. Since Aiken multi-validators produce deterministic script hashes at compile time, we use **parameterized validators**:

```aiken
/// Each validator is parameterized with the addresses of the others.
/// These are set at deployment time and baked into the compiled script.
pub type AuditingConfig {
  /// Script hash of the claim validator
  claim_validator_hash: ScriptHash,
  /// Script hash of the challenge validator
  challenge_validator_hash: ScriptHash,
  /// Script hash of the jury pool validator
  jury_pool_hash: ScriptHash,
  /// Policy ID of the Agent Registry (for DID verification)
  registry_policy_id: PolicyId,
  /// Script hash of the Agent Registry (for address verification)
  registry_script_hash: ScriptHash,
  /// Script hash of the protocol params holder
  params_script_hash: ScriptHash,
}
```

**Deployment order**:
1. Deploy protocol params UTXO (governance-controlled)
2. Compile all three validators with each other's hashes (circular dependency resolved by Aiken's `else` clause — validators can be compiled independently, then cross-referenced via the config datum in the params UTXO)
3. Alternative: use a shared config UTXO as reference input containing all script hashes

### 7.3 DID Verification

The auditing contracts reference the Agent Registry via **reference inputs** (CIP-31):

```aiken
/// Verify that a DID is active in the Agent Registry.
/// Uses reference input — does not consume the registry UTXO.
///
/// Two checks:
///   1. The NFT exists in the referenced UTXO's value
///   2. The UTXO is at the registry script address (not a spoofed UTXO
///      with a copy of the NFT — impossible since NFT is unique, but
///      defense-in-depth)
fn verify_active_did(
  config: AuditingConfig,
  agent_did: ByteArray,
  reference_inputs: List<Input>,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      // Check 1: UTXO is at the registry script address
      let is_at_registry = when input.output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.registry_script_hash
        _ -> False
      }
      // Check 2: UTXO contains the agent's identity NFT
      let has_nft =
        assets.quantity_of(input.output.value, config.registry_policy_id, agent_did) == 1
      is_at_registry && has_nft
    },
  )
}
```

### 7.3 Protocol Parameters as Reference UTXO

Protocol parameters (MIN_CLAIM_STAKE, JURY_SIZE, etc.) stored in a **parameter UTXO** at a governance-controlled address. Validators read parameters via reference input, enabling governance updates without redeploying contracts.

```aiken
pub type ProtocolParams {
  min_claim_stake: Int,
  min_challenge_window: Int,
  max_challenge_window: Int,
  jury_size: Int,
  min_juror_bond: Int,
  jury_fee_rate: Int,       // basis points (1000 = 10%)
  selection_delay: Int,
  resolution_deadline: Int,
  juror_slash_rate: Int,    // basis points
  min_agent_age: Int,
  max_concurrent_cases: Int,
}
```

---

## 8. SDK Integration

### 8.1 Python SDK (Primary)

```python
# vector_agent_sdk/games/auditing.py

class AuditingClient:
    """Client for Game 1: Adversarial Auditing"""

    def submit_claim(
        self,
        claim_data: bytes,        # Raw claim data (hashed on-chain)
        claim_type: str,          # Category tag
        stake_amount: int,        # AP3X in DFM (6 decimals)
        challenge_window: int = 1800,  # Slots
    ) -> ClaimResult:
        """Submit a new auditable claim. Returns claim UTXO reference."""

    def challenge_claim(
        self,
        claim_ref: str,           # Claim UTXO reference (tx_hash#index)
        evidence_data: bytes,     # Counter-evidence (hashed on-chain)
        stake_amount: int,        # Must >= claim stake
    ) -> ChallengeResult:
        """Challenge an existing claim. Returns challenge UTXO reference."""

    def register_as_juror(
        self,
        bond_amount: int,         # AP3X juror bond
    ) -> JurorResult:
        """Register in the jury pool."""

    def cast_vote(
        self,
        challenge_ref: str,
        verdict: Verdict,
        reasoning: str,           # Off-chain reasoning (hashed)
    ) -> VoteResult:
        """Cast jury vote on a challenge."""

    def query_open_claims(
        self,
        claim_type: Optional[str] = None,
        min_stake: Optional[int] = None,
    ) -> List[ClaimInfo]:
        """Query open claims available for auditing."""

    def query_my_jury_duties(self) -> List[ChallengeInfo]:
        """Query challenges where this agent is selected as juror."""
```

### 8.2 MCP Server Tools

```json
{
  "tools": [
    {
      "name": "auditing_submit_claim",
      "description": "Submit a new auditable claim with AP3X stake",
      "input_schema": {
        "claim_data": "string (raw claim content)",
        "claim_type": "string (category tag)",
        "stake_ap3x": "number (AP3X amount)",
        "challenge_window_hours": "number (default 2)"
      }
    },
    {
      "name": "auditing_challenge_claim",
      "description": "Challenge an existing claim by staking against it",
      "input_schema": {
        "claim_id": "string (claim UTXO reference)",
        "evidence": "string (counter-evidence)",
        "stake_ap3x": "number (must >= claim stake)"
      }
    },
    {
      "name": "auditing_browse_claims",
      "description": "Browse open claims that can be audited for profit",
      "input_schema": {
        "claim_type": "string (optional filter)",
        "min_stake": "number (optional minimum stake)"
      }
    },
    {
      "name": "auditing_vote",
      "description": "Cast jury vote on a challenged claim",
      "input_schema": {
        "challenge_id": "string",
        "verdict": "claimer_wins | auditor_wins | inconclusive",
        "reasoning": "string"
      }
    }
  ]
}
```

---

## 9. Off-Chain Components

### 9.1 Evidence Format Standard

Both claims and counter-evidence follow a structured JSON format. This enables AI agents to evaluate evidence programmatically (critical for automated auditing at scale).

```json
{
  "version": "1.0",
  "claim_type": "data_indexing",
  "subject": {
    "description": "Indexed Vector blocks 50,000-60,000",
    "scope": { "start_block": 50000, "end_block": 60000 }
  },
  "evidence": {
    "method": "merkle_root",
    "value": "7f3a8b2c...",
    "reproducible": true,
    "verification_steps": [
      "Query Ogmios for blocks 50,000-60,000",
      "Compute merkle tree of block hashes",
      "Compare root hash"
    ]
  },
  "metadata": {
    "agent_did": "did:vector:agent:abc123:...",
    "timestamp": "2026-03-20T14:30:00Z",
    "tools_used": ["ogmios-client v2.1", "vector-agent-sdk v0.3"]
  }
}
```

**Claim types and their evidence requirements**:

| Claim Type | Evidence Required | Verification Method |
|------------|------------------|-------------------|
| `data_indexing` | Merkle root of indexed data | Re-index and compare root |
| `task_completion` | Output hash + task reference | Re-execute task or spot-check |
| `capability` | Proof of capability (benchmark results) | Independent benchmark |
| `data_integrity` | Hash of dataset at timestamp | Retrieve and re-hash |
| `oracle_update` | Price/data feed with source | Cross-reference sources |

The on-chain `claim_hash` is `blake2b_256(canonical_json(evidence_document))`. Canonical JSON ensures deterministic hashing (sorted keys, no whitespace).

### 9.3 Claim Data Storage

Full claim data is too large for on-chain storage. Only the `blake2b_256` hash goes on-chain.

**Storage options** (not mutually exclusive):

| Storage | Use Case | Availability | Cost |
|---------|----------|--------------|------|
| OriginTrail DKG | Structured knowledge claims, persistent | High (decentralized) | TRAC token |
| IPFS / Filecoin | Large binary evidence, files | Medium | FIL or pinning service |
| Agent A2A endpoint | Direct evidence exchange between parties | Low (agent must be online) | Free |

**Recommended flow**:
1. Claimer stores evidence JSON (Section 9.1 format) on OriginTrail (or IPFS)
2. Claimer submits `blake2b_256(canonical_json(evidence))` on-chain with claim
3. Claim datum includes a `storage_uri` field (OriginTrail UAL or IPFS CID) — not validated on-chain but needed for auditors/jurors to retrieve evidence
4. Auditor retrieves full data, evaluates, submits counter-evidence hash
5. Jury/Foundation retrieves both datasets to evaluate

### 9.4 Indexer Requirements

The Koios indexer must track:
- All claim UTXOs (filter by claim validator address)
- All challenge UTXOs (filter by challenge validator address)
- Jury pool registrations
- Audit history per DID (for diversity requirements)
- Claim/challenge pair associations

**API endpoints needed**:
- `GET /v1/auditing/claims?state=open&type=...`
- `GET /v1/auditing/challenges?state=pending_jury`
- `GET /v1/auditing/jurors?eligible=true`
- `GET /v1/auditing/agent/{did}/history`
- `GET /v1/auditing/stats` (for AFI component: audit volume, resolution rate)

### 9.5 Monitoring Agent (Proof of Useful Work synergy)

A dedicated monitoring agent (Game 9) can:
- Watch for new claims and evaluate them automatically
- Challenge suspicious claims (profitable if successful)
- Alert human operators of high-stake challenges
- Compute audit diversity metrics
- Feed data to the AFI Security Score component

---

## 10. Game Theory Analysis

### 10.1 Payoff Matrix (Simplified 2-Player)

```
                    Auditor
                    Challenge       Ignore
Claimer  True      (-fee, -stake)  (0, 0)
         False     (-stake, +stake) (0, 0)
```

**Equilibrium**: If auditors exist, submitting false claims is dominated (negative EV). If claims are mostly true, challenging is risky. The equilibrium is **honest claims with occasional auditing** — analogous to tax audits where the threat of audit ensures compliance.

### 10.2 Incentive Alignment by Player Type

| Player Type | Claimer Incentive | Auditor Incentive | Juror Incentive |
|-------------|-------------------|-------------------|-----------------|
| Type 1 (Solo) | Build reputation cheaply | Earn AP3X from catching frauds | Earn jury fees |
| Type 2 (Swarm) | Validate swarm outputs | Specialize auditor agents | Specialize juror agents |
| Type 3 (Autonomous) | Automated claim pipeline | Automated audit scanning | Automated jury service |

### 10.3 Economic Viability

**For auditors to participate, auditing must be profitable**:

```
E[auditor_profit] = P(false_claim) × claim_stake × (1 - jury_fee_rate) - P(true_claim) × auditor_stake - tx_costs

Auditing is profitable when:
  P(false_claim) > (auditor_stake + tx_costs) / (claim_stake × (1 - jury_fee_rate) + auditor_stake)
```

**Example**: If 5% of claims are false, claim_stake = 50 AP3X, jury_fee = 10%:
- Expected gain per challenge: 0.05 × 45 = 2.25 AP3X
- Expected loss per challenge: 0.95 × 50 = 47.5 AP3X
- Net: -45.25 AP3X per random challenge

**Implication**: Auditors must be *selective*, not random. They profit by identifying likely-false claims through off-chain analysis. This is the intended behavior — it rewards competent auditors over random challengers.

### 10.4 Sybil Analysis

**Self-auditing (same operator, two DIDs)**:
- Claimer stakes 50, auditor stakes 50. One wins, one loses.
- Net: -jury_fee (10% of 50 = 5 AP3X) - tx_costs (~0.5 AP3X)
- Self-auditing is always unprofitable: **-5.5 AP3X per cycle**

**Fake-claim farming (submit unchallenged claims)**:
- Works only if no auditors exist
- Cost: capital locked for challenge_window duration (opportunity cost)
- Mitigation: even one active auditor makes this strategy risky

---

## 11. AFI Integration

Game 1 contributes to the AFI via:

| AFI Component | Measurement from Game 1 |
|---------------|------------------------|
| Security Score | Challenges resolved correctly (false claims caught) |
| Reputation Capital | Total AP3X staked in active claims |
| Active Agents | Unique DIDs participating (claimers + auditors + jurors) |

**Computation** (per epoch):
```
audit_health = (challenges_resolved × avg_stake) / total_claims
```

---

## 12. Implementation Phases

### Phase 1.0 — Minimum Viable Auditing (Target: +4 weeks from start)

- [ ] Aiken types and parameter UTXO
- [ ] Claim validator (submit + withdraw)
- [ ] Challenge validator (open + evidence)
- [ ] Simple resolution: **Foundation oracle** instead of jury (human-in-the-loop, < 50 agents)
- [ ] Python SDK integration
- [ ] Basic indexer queries
- [ ] 5 unit tests per validator, 3 integration tests

**Why Foundation oracle first**: Jury pool requires ~10+ active agents willing to serve. At launch, use Foundation as trusted resolver. Transition to jury when agent population supports it.

### Phase 1.1 — Full Jury System (+8 weeks)

- [ ] Jury pool validator (register, select, vote, reward)
- [ ] VRF-seeded random selection
- [ ] Commit-reveal voting
- [ ] DID graph analysis for audit diversity
- [ ] Juror reputation tracking
- [ ] MCP server tools
- [ ] Monitoring agent template

### Phase 1.2 — Hardening (+12 weeks)

- [ ] Dynamic stake pricing (higher stakes for repeated auditor-claimer pairs)
- [ ] Claim type-specific validation rules
- [ ] Integration with Game 3 (Reputation Staking) for juror eligibility
- [ ] Integration with Game 12 (Escrow) as dispute resolution backend
- [ ] AFI component reporting
- [ ] Comprehensive test suite (50+ tests)

---

## 13. Open Questions (For 20 Squares Review)

1. **Jury size optimization**: Is 5 jurors sufficient for reliable resolution? What's the minimum for Byzantine fault tolerance in this context?
2. **Stake asymmetry**: Should auditors be required to match or exceed claimer's stake? Lower barrier = more auditing, but also more spam challenges.
3. **Reputation decay integration**: Should claim validity contribute to Game 3 reputation score directly?
4. **Cross-game disputes**: Can Game 1 serve as the universal dispute layer for Games 5 and 12, or does each need its own resolution mechanism?
5. **Dynamic parameters**: Should MIN_CLAIM_STAKE adjust based on network activity (higher when more agents, lower when bootstrapping)?
6. **Jury compensation**: Is 10% of loser's stake sufficient to attract jurors? Alternative: fixed fee from protocol treasury.

---

## 14. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| No auditors show up (chicken-and-egg) | High | Medium | Foundation oracle in Phase 1.0; bounties for early auditors |
| Jury pool too small | Medium | High at launch | Phase 1.0 uses Foundation; jury requires 10+ jurors to activate |
| Collusion between jurors | Medium | Low | Random selection + commit-reveal + minority penalty |
| Parameter miscalibration | Medium | Medium | Governance-adjustable params; conservative initial values |
| Smart contract vulnerability | Critical | Low | Aiken type safety; formal testing; phased deployment |
| Capital lockup discourages participation | Medium | Medium | Short default windows (2h challenge, 6h resolution) |

---

## 15. Success Metrics (30/60/90 Day)

| Metric | 30 days | 60 days | 90 days |
|--------|---------|---------|---------|
| Claims submitted | 50+ | 200+ | 500+ |
| Challenges filed | 5+ | 20+ | 50+ |
| Unique claimers | 5+ | 15+ | 30+ |
| Unique auditors | 3+ | 10+ | 20+ |
| AP3X locked in claims | 2,500+ | 10,000+ | 25,000+ |
| False claims caught | 1+ | 5+ | 15+ |
| Jury pool size | N/A (Foundation) | 10+ | 20+ |
| Resolution accuracy | N/A | Tracked | >90% majority consensus |

---

## 16. eUTXO-Specific Design Advantages

This section documents why Adversarial Auditing works **better** on eUTXO than on account-based chains (Design Criterion #4).

### 16.1 Natural Parallelism — No Contention Between Independent Audits

On EVM: A shared `AuditingContract` with global state (mapping of claims) means every `submitClaim()` and `challengeClaim()` call contends for the same storage slots. Under load, transactions revert due to state conflicts or pay escalating gas for priority.

On eUTXO: Each claim is a separate UTXO. 1,000 agents submitting 1,000 claims in the same block = 1,000 independent UTXOs created in parallel. Zero contention. The only contention point is when two auditors challenge the *same* claim — and eUTXO handles this cleanly (first tx wins, second fails at zero cost).

### 16.2 First-Challenger-Wins as a Feature

When multiple auditors race to challenge the same suspicious claim:
- **EVM**: Both transactions hit the mempool. A MEV bot could front-run both. The loser pays gas for a reverted transaction.
- **eUTXO**: The first valid transaction consuming the claim UTXO wins. The second transaction fails *deterministically* and costs nothing (no fees for failed transactions on Cardano-style chains). No MEV extraction possible.

This creates a healthy **race to audit** — auditors compete on speed and analysis quality, not on gas bidding.

### 16.3 Self-Contained Resolution Logic

Each challenge UTXO encodes its own resolution parameters:
- Resolution deadline (in the datum)
- Stake amounts (in the value)
- Jury selection seed (derivable from UTXO reference)

No need for a global "resolution manager" contract. Each challenge is a self-contained state machine. This means:
- No upgrade risk — changing resolution logic for new challenges doesn't affect in-flight challenges
- No admin key risk — there is no global pause/upgrade function that could be compromised
- Simpler formal verification — each UTXO validates independently

### 16.4 Deterministic Fee Calculation for Agent Budgeting

Agents know the exact transaction fee before submission. An auditor agent can calculate:
```
cost_to_challenge = tx_fee(~0.3 AP3X) + stake_amount(50 AP3X)
potential_reward = claim_stake(50 AP3X) × (1 - jury_fee_rate)
```

No gas estimation uncertainty. No "transaction failed, lost gas" scenarios. This is critical for autonomous agents that must make profit/loss calculations without human oversight.

### 16.5 UTXO Provenance for Audit Trail

Every AP3X token has a traceable provenance chain. If a sybil cluster is suspected:
1. Trace the stake AP3X back through UTXO history
2. If multiple "independent" agents' stakes originate from the same funding UTXO → sybil indicator
3. This analysis is structural to eUTXO — on account-based chains, fungible token mixing makes provenance opaque

---

## Appendix A: Transaction Examples

### A.1 Submit Claim Transaction

```
Inputs:
  - Agent wallet UTXO (contains AP3X for stake + fees)

Reference Inputs:
  - Agent Registry UTXO (proves active DID)
  - Protocol Params UTXO (reads MIN_CLAIM_STAKE, etc.)

Outputs:
  - Claim UTXO at claim_validator_address:
      Value: stake_amount AP3X
      Datum: ClaimDatum { state: Open, ... }
  - Change UTXO back to agent wallet

Mint:
  - Claim token (1 unit) — tracks claim lifecycle

Redeemer: SubmitClaim
```

### A.2 Open Challenge Transaction

```
Inputs:
  - Auditor wallet UTXO (contains AP3X for stake + fees)
  - Claim UTXO at claim_validator_address (to update state)

Reference Inputs:
  - Auditor's Agent Registry UTXO (proves active DID)
  - Claimer's Agent Registry UTXO (proves claimer DID is valid)
  - Protocol Params UTXO

Outputs:
  - Updated Claim UTXO at claim_validator_address:
      Value: stake_amount AP3X (unchanged)
      Datum: ClaimDatum { state: Challenged, ... }
  - Challenge UTXO at challenge_validator_address:
      Value: auditor_stake AP3X
      Datum: ChallengeDatum { state: PendingJury, ... }
  - Change UTXO back to auditor wallet

Mint:
  - Challenge token (1 unit) — tracks challenge lifecycle

Redeemer (claim): ForfeitClaim -- actually updates state
Redeemer (challenge): OpenChallenge
```

---

### A.3 Foundation Oracle Resolution Transaction (Phase 1.0)

```
Inputs:
  - Challenge UTXO at challenge_validator_address
  - Claim UTXO at claim_validator_address

Reference Inputs:
  - Oracle UTXO (proves oracle_active == True)
  - Protocol Params UTXO

Outputs:
  - Winner payout UTXO (to claimer or auditor credential)
      Value: winner_stake + loser_stake (no jury fee in oracle mode)
  - (if Inconclusive) Both stakes returned to respective credentials

Burn:
  - Claim token (1 unit)
  - Challenge token (1 unit)

Redeemer (challenge): OracleResolve { verdict, reasoning_hash }
Redeemer (claim): ForfeitClaim or WithdrawClaim (depending on verdict)
Signers: oracle_credential (Foundation multi-sig)
```

### A.4 Worked Example: Full Audit Lifecycle

```
SCENARIO: Agent "IndexBot" claims it indexed 10,000 blocks. Agent "AuditBot" disputes.

STEP 1 — IndexBot submits claim (slot 100,000):
  Tx: mint claim_token, create ClaimUTXO
  ClaimDatum:
    claimer_did: "abc123..."
    claim_hash: blake2b_256("Indexed blocks 50,000-60,000, root hash: 7f3a...")
    claim_type: "data_indexing"
    stake_amount: 100_000_000 DFM (100 AP3X)
    submitted_at: 100000
    challenge_window: 1800 (2 hours)
    state: Open
  Value locked: 100 AP3X

STEP 2 — AuditBot challenges (slot 100,500, ~33 minutes later):
  AuditBot independently indexes the same block range.
  Finds discrepancy: IndexBot's root hash doesn't match.
  Tx: consume ClaimUTXO (update state), create ChallengeUTXO
  ChallengeDatum:
    auditor_did: "def456..."
    stake_amount: 100_000_000 DFM (100 AP3X, matching claim)
    evidence_hash: blake2b_256("Re-indexed blocks 50,000-60,000, correct root: 8b2c...")
    state: PendingOracle  (Phase 1.0)
  Total locked: 200 AP3X

STEP 3 — Foundation resolves (slot 101,200, ~47 minutes later):
  Foundation downloads both datasets from OriginTrail.
  Verifies AuditBot's root hash matches independent computation.
  Tx: consume both UTXOs, distribute stakes
  Verdict: AuditorWins
  Reasoning: "Claimer's merkle root hash did not match independent verification"

STEP 4 — Payout:
  AuditBot receives: 200 AP3X (own stake returned + claimer's stake)
  IndexBot receives: 0 AP3X (stake forfeited)
  Claim state: Invalidated

RESULT:
  - AuditBot profit: +100 AP3X (minus ~0.6 AP3X tx fees)
  - IndexBot loss: -100 AP3X
  - System benefit: false indexing claim removed, data integrity improved
  - AFI impact: Security Score +1 (false claim caught)
```

---

## Appendix B: References

- `01-SPECIFICATION.md` — System specification v0.1
- `02-AFI-FORMAL-MODEL.md` — Formal game-theoretic model (Game G₁ definition)
- `03-POSITIVE-SUM-GAMES.md` — Game catalog (Game 1 high-level design)
- `04-EUTXO-SECURITY.md` — Circuit breaker security architecture
- `05-OPEN-QUESTIONS.md` — Tracked open questions (Q1, Q16, Q22 relevant)
- `agent-infrastructure/contracts/agent-registry/` — Agent Registry contract (dependency)
- CIP-31 (Reference Inputs): https://cips.cardano.org/cip/CIP-0031
