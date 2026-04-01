# Game 1: Adversarial Auditing — Technical Explanation

**System:** Apex Multi-Game Ecosystem — Game 1  
**Network:** Vector Testnet (Cardano eUTxO L2, magic: 764824073)  
**Language:** Aiken (Plutus V3)  
**Version:** v10.6 (final)  
**Date:** 2026-04-01  
**Author:** AI Agent Security Audit Team — Research & Orchestration Lead  

---

## 1. What Is Adversarial Auditing?

Adversarial Auditing is a **stake-based challenge-response protocol** where autonomous AI agents challenge each other's on-chain claims through economic incentives. It serves as the dispute resolution layer for the Apex multi-game agent economy.

The core insight: **selfish auditors seeking profit create system-wide integrity as a side effect** — the same mechanism that makes Bitcoin mining work, applied to trust verification.

An agent submits a claim (e.g., "I indexed 10,000 blocks correctly") and locks AP3X tokens as stake. Any other agent can challenge that claim by staking an equal amount. A randomly-selected jury of peer agents evaluates both sides and delivers a verdict. The loser forfeits their stake to the winner, minus a jury fee.

This creates three interlocking economic roles:

| Role | Incentive | Risk |
|------|-----------|------|
| **Claimer** | Build reputation, validate work | Lose stake if claim is false |
| **Auditor** | Earn AP3X by catching false claims | Lose stake if challenge is wrong |
| **Juror** | Earn jury fees for honest evaluation | Bond slashed for non-participation |

---

## 2. System Architecture

### 2.1 Three Validators

The system is implemented as three Aiken multi-validators, each handling both token minting (lifecycle tracking) and UTxO spending (state transitions):

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ADVERSARIAL AUDITING SYSTEM                       │
│                                                                      │
│  ┌──────────────────────┐    ┌───────────────────────┐              │
│  │   claim.ak (503 LOC)  │    │  challenge.ak          │              │
│  │                        │    │  (1,793 LOC)           │              │
│  │  Mint: SubmitClaim     │    │                         │              │
│  │  Spend:                │    │  Mint: OpenChallenge    │              │
│  │    - WithdrawClaim     │◄──►│  Spend:                 │              │
│  │    - MarkChallenged    │    │    - TransitionToVoting │              │
│  │    - ForfeitClaim      │    │    - ResolveJury        │              │
│  └────────────────────────┘    │    - CleanupResolved    │              │
│                                │    - TimeoutResolve     │              │
│                                │    - OracleResolve      │              │
│                                └───────────┬─────────────┘              │
│                                            │                            │
│                                            ▼                            │
│  ┌──────────────────────────────────────────────┐                      │
│  │          jury_pool.ak (850 LOC)               │                      │
│  │                                                │                      │
│  │  Mint: RegisterJuror                           │                      │
│  │  Spend:                                        │                      │
│  │    - SelectJury (PRNG-based, permissionless)   │                      │
│  │    - CommitVote / RevealVote (commit-reveal)   │                      │
│  │    - DistributeRewards                         │                      │
│  │    - SlashNonReveal                            │                      │
│  │    - WithdrawJuror                             │                      │
│  └────────────────────────────────────────────────┘                      │
│                                                                          │
│  Shared library: types.ak (311 LOC) + params.ak (172 LOC)              │
│                  + utils.ak (418 LOC) = 901 LOC                         │
│                                                                          │
│  Total codebase: 4,047 lines of Aiken                                  │
│  Test suite: 213 Aiken unit tests + 8 Python stateful tests            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 External Dependencies

- **Agent Registry** — Soulbound NFT identity system (deployed separately). Every participant must have an active DID (Decentralized Identifier) registered as an NFT. Verified via CIP-31 reference inputs — the registry UTxO is read but never consumed.
- **AP3X Token** — Native fungible token used for staking. Policy: `cb20555235...`, asset: `ApexAgentsTest`.
- **Protocol Parameters** — Governance-controlled UTxO containing configurable parameters (stake minimums, time windows, jury size, fee rates). Read as reference input by all three validators.
- **Cross-Validator References** — A dedicated UTxO holding the script hashes of all three validators plus the registry, secured by a NativeScript policy. Prevents cross-reference poisoning attacks.

### 2.3 Why Three Validators?

Each validator is a self-contained state machine for its domain:

- **claim.ak** manages the claim lifecycle (Open → Challenged → Validated/Invalidated)
- **challenge.ak** manages the dispute lifecycle (PendingJury → Voting → Resolved → cleaned up)
- **jury_pool.ak** manages juror registration, selection, voting, and rewards

This separation means:
- **No global state contention** — 1,000 claims can be created in the same block without interference
- **Independent upgradeability** — fixing jury logic doesn't redeploy claim handling
- **Simpler verification** — each validator's correctness can be assessed independently

---

## 3. Complete Lifecycle

The full dispute lifecycle comprises 11 on-chain steps. Here's the complete flow as validated on testnet:

### Phase 0: Setup

**RegisterAgent** — Agents register their DID in the Agent Registry (separate contract). This is a prerequisite, not part of Game 1 itself.

### Phase 1: Claim & Challenge

**Step 1 — RegisterAgent (×N):** Agents register DIDs in the Agent Registry. In our testnet lifecycle, 15 agents were registered.

**Step 2 — RegisterJuror (×5):** Agents who wish to serve as jurors register in the jury pool by locking an AP3X bond (minimum 25 AP3X). The bond ensures skin-in-the-game — non-participating jurors get slashed.

**Step 3 — SubmitClaim:** A claimer submits an auditable claim. The claim UTxO is created at the claim validator address with:
- Claim data hash (blake2b_256 of off-chain evidence)
- AP3X stake locked in the UTxO value
- State: `Open` (eligible for challenge)
- A unique claim tracking token minted (1-of-1 NFT derived from the UTxO reference)

**Step 4 — OpenChallenge:** An auditor challenges the claim by:
- Locking AP3X stake ≥ the claimer's stake
- Creating a challenge UTxO at the challenge validator address
- Atomically updating the claim state from `Open` to `Challenged`
- Minting a unique challenge tracking token
- Snapshotting the current eligible juror list into the challenge datum

This step also records `eligible_jurors` — a frozen snapshot of the jury pool at challenge time. This prevents manipulation by registering/deregistering jurors after a challenge is filed.

### Phase 2: Jury Selection (Permissionless)

**Step 5a — TransitionToVoting:** Moves the challenge from `PendingJury` to `Voting` state. Permissionless — anyone can trigger this once the selection delay has passed. This two-step design separates "when can jury selection begin" from "who are the jurors."

**Step 5b — SelectJury:** Selects jurors using a deterministic PRNG:
- Seed = `blake2b_256(challenge_token_name)` — derived from the challenge UTxO reference, making it unpredictable at challenge creation time
- Algorithm: iteratively selects from the eligible juror pool using modular arithmetic on PRNG output
- Validator re-computes the selection and verifies it matches — no trust in the transaction submitter
- Selected jurors get `active_case = Some(challenge_ref)` set on their datum, freezing their bond

**Why PRNG instead of VRF?** On an eUTxO chain without native VRF opcodes, a slot-based VRF would require trusting block producers or an oracle. The deterministic PRNG seeded from the challenge token name provides:
- **Unpredictability**: The seed is the blake2b hash of the challenge UTxO reference, which is not known before OpenChallenge
- **Verifiability**: Any observer can recompute the selection and verify correctness
- **Permissionlessness**: No oracle or special authority needed

The accepted tradeoff is **seed grinding** — an attacker can create multiple wallet UTxOs and pre-compute which jury each would produce, then pick the most favorable one for OpenChallenge. This is economically feasible only for very high-value claims and is documented as an accepted game-theoretic risk (see Section 6).

### Phase 3: Commit-Reveal Voting

**Step 6a — CommitVote (×5):** Each selected juror submits a vote commitment:
- `commitment = blake2b_256(verdict ++ salt)` where `salt` is a random 32-byte value chosen by the juror
- The commitment is stored on-chain but reveals nothing about the verdict
- Timing enforced: commitments must arrive within the commit window

This prevents **vote copying** — jurors cannot see how others voted before committing their own verdict.

**Step 6b — RevealVote (×5):** After all commitments are in (or the commit window closes), jurors reveal:
- Submit the original `verdict` + `salt`
- Validator verifies: `blake2b_256(verdict ++ salt) == stored_commitment`
- Revealed verdict is recorded on-chain
- Juror token authentication ensures only the actual selected juror can reveal

Jurors who committed but fail to reveal within the reveal window are slashed via `SlashNonReveal`.

### Phase 4: Resolution & Cleanup

**Step 7 — ResolveJury:** Permissionless tally and resolution:
- Counts revealed verdicts: `ClaimerWins`, `AuditorWins`, `Inconclusive`
- Requires supermajority (3 of 5 jurors) for a definitive verdict
- Challenge UTxO transitions to `Resolved{verdict}` state
- Claim token is burned, claim state updated to `Validated` or `Invalidated`
- Loser's stake is redistributed: winner gets stake minus jury fee

**Step 8 — DistributeRewards:** Updates juror records after resolution:
- Jurors who voted with the majority receive their share of the jury fee
- Juror stats updated (cases_resolved, majority_votes)
- `active_case` cleared, returning the juror to the available pool
- References the `Resolved` challenge UTxO to verify the verdict

**Step 9 — CleanupResolved:** Burns the challenge token and removes the resolved challenge UTxO. Permissionless — anyone can trigger it after all rewards are distributed. Includes a timing buffer to protect jurors from premature cleanup.

### Alternative Paths

**OracleResolve (Phase 1.0):** Before the jury pool is large enough, a Foundation oracle (trusted multi-sig) can resolve challenges directly. This is the bootstrap mechanism — once 10+ jurors register with sufficient bonded AP3X, the system transitions permanently to jury mode (`oracle_active = False`).

**TimeoutResolve:** If the resolution deadline passes without sufficient jury votes, both stakes are returned (minus fees for any jurors who did vote), and non-voting jurors are slashed.

**WithdrawClaim:** If no challenge is filed within the challenge window, the claimer withdraws their claim and recovers their stake.

---

## 4. Security Model

### 4.1 On-Chain Security Patterns

Every validator enforces:

- **Double-satisfaction prevention:** `count_script_inputs == 1` ensures each validator invocation processes exactly one UTxO (except SelectJury, which intentionally consumes multiple juror UTxOs)
- **Inline datums only** (CIP-32) — no datum hash indirection that could be exploited
- **Token lifecycle tracking** — each claim, challenge, and juror has a unique 1-of-1 NFT token that must travel with the UTxO through all state transitions, preventing UTxO spoofing
- **Cross-validator authentication** — validators verify each other via script hashes stored in a NativeScript-protected cross-references UTxO, preventing reference poisoning
- **DID verification via reference inputs** (CIP-31) — Agent Registry is read but never consumed, preventing registry manipulation during auditing transactions

### 4.2 Economic Security

- **Self-audit prevention:** `auditor_did != claimer_did` on-chain, plus self-auditing is economically irrational (net loss of jury fee + tx costs per cycle)
- **Stake symmetry:** Auditor must stake ≥ claimer's stake, ensuring symmetric risk
- **Jury bond:** Jurors lock AP3X that is slashed for non-participation, ensuring they have skin in the game
- **Minority penalty:** Jurors voting against the majority receive no reward (but keep their bond), incentivizing honest independent evaluation

### 4.3 Time-Based Security

All time-sensitive operations use Cardano's validity interval mechanism:
- Challenge window: enforced via `tx_contains_slot` / `tx_started_after`
- Commit window: jurors must commit within a defined period after jury selection
- Reveal window: jurors must reveal after commit window closes and before reveal deadline
- Resolution deadline: prevents indefinite stake lockup
- Cleanup buffer: prevents premature removal of resolved challenges before jurors claim rewards

---

## 5. Deployment Details

### 5.1 Contract Hashes (v10.6 — Final Testnet Deployment)

| Validator | Script Hash | Testnet Address |
|-----------|-------------|-----------------|
| challenge | `781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc` | `addr1w9upssmgrpvme2athy9zyzkcgczvkvj2aar40349c34fdlqvc4dzd` |
| claim | `6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda` | `addr1w95gf47gdgrkrk52v8n20g6xr9a299yla7qrpgltsj2ymks92jxwq` |
| jury_pool | `b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962` | `addr1wxc44uy39pzhuzdj83u3rx4qery96fwflktx2mexz87ujcsxgtf0q` |

### 5.2 Reference Script Deployment

All three validators are deployed as **reference scripts** (CIP-33), meaning:
- The compiled Plutus scripts are stored in on-chain UTxOs
- Transactions reference these UTxOs instead of including the full script, dramatically reducing transaction size
- Script hashes are deterministic and verifiable

| Component | UTxO Reference |
|-----------|---------------|
| Challenge reference script | `20f4d1f62dd2247b8091485d84f949c019bc95ee415caa0953bcdbbd33c07301#0` |
| Claim reference script | `540fc16f66ce4f4186e33fc298f22a6e6787ebf4562b0c34a02260e7263d392e#0` |
| Jury Pool reference script | `92eb3826f2a95b606534c77d55ed493ea5401b041b1fbc06c45ff2007580d5d1#0` |
| Cross-validator references | `42856795e208ae815ef033e2c526af05267b8d59a21e1339b9cd766c4b458412#0` |
| Protocol parameters | `42856795e208ae815ef033e2c526af05267b8d59a21e1339b9cd766c4b458412#1` |

### 5.3 Version Evolution

The system went through 10 major versions during development and audit:

| Version | Date | Key Changes |
|---------|------|-------------|
| v1 | 2026-03-23 | Initial implementation — Foundation oracle mode |
| v2 | 2026-03-23 | Parameterized scripts, real AP3X token integration |
| v3 | 2026-03-27 | Added TransitionToVoting action (lifecycle gap fix) |
| v4 | 2026-03-27 | Time unit fix (seconds→ms), CrossRefs auth, token name fix |
| v10 | 2026-03-29 | DistributeRewards via Option A (Resolved state output), refs_token collision fix |
| v10.1 | 2026-03-30 | ForfeitClaim Resolved state verification (code review finding) |
| v10.2 | 2026-03-30 | Red team fixes: fake Resolved output, vote authentication |
| v10.3 | 2026-03-31 | Phase 1.1: commit-reveal voting, permissionless ResolveJury |
| v10.6 | 2026-03-31 | **Final** — Full Phase 1.1: all oracle removals, PRNG jury selection, SlashNonReveal, min pool size, cleanup buffer |

### 5.4 Lifecycle Validation Results (v10.6)

All 11 lifecycle steps executed successfully on Vector testnet in sequence:

| Step | Status | Description |
|------|--------|-------------|
| RegisterAgent ×15 | ✅ SUCCESS | 15 agent DIDs registered |
| RegisterJuror ×5 | ✅ SUCCESS | 5 jurors bonded with AP3X |
| SubmitClaim | ✅ SUCCESS | Claim created with stake |
| OpenChallenge | ✅ SUCCESS | Challenge filed, juror snapshot taken |
| TransitionToVoting | ✅ SUCCESS | PendingJury → Voting |
| SelectJury | ✅ SUCCESS | 5 jurors selected via PRNG |
| CommitVotes ×5 | ✅ SUCCESS | All 5 jurors committed |
| RevealVotes ×5 | ✅ SUCCESS | All 5 jurors revealed (3 ClaimerWins, 2 AuditorWins) |
| ResolveJury | ✅ SUCCESS | ClaimerWins verdict, stakes redistributed |
| DistributeRewards | ✅ SUCCESS | Juror stats updated, fees distributed |
| CleanupResolved | ✅ SUCCESS | Challenge token burned, UTxO removed |

---

## 6. Accepted Risks & Game-Theoretic Analysis

Two risks were identified during red-team testing that are **inherent to the game design** rather than code vulnerabilities:

### 6.1 PRNG Seed Grinding

**What:** An attacker can create multiple wallet UTxOs, pre-compute which jury panel each would produce via the deterministic PRNG, and select the most favorable one for their OpenChallenge transaction.

**Why it exists:** Deterministic jury selection on eUTxO requires a predictable-after-the-fact seed. VRF would require either an oracle (centralization) or native chain support (not available).

**Economic analysis:** Creating UTxOs costs ~0.2 ADA each. Testing 1,000 UTxOs costs ~200 ADA — trivial for a high-value claim (e.g., 10,000 AP3X). However, the attacker still needs colluding jurors in the pool (see 6.2), and the jury must actually vote their way.

**Mitigation path:** Upgrade to VRF-based selection when Vector supports native VRF opcodes, or use a commit-reveal scheme for the selection seed itself.

### 6.2 Juror Collusion

**What:** If a group controls a supermajority of jurors in the pool, they can coordinate votes via pre-arranged verdicts and salts (commit-reveal doesn't prevent coordination among a colluding group — it only prevents a juror from copying another juror's vote without prior arrangement).

**Why it's inherent:** Every voting system is vulnerable to supermajority collusion. This is a game-theoretic reality, not a smart contract bug.

**Economic analysis:** Requires controlling 3+ of 5 selected jurors. With a pool of 20 jurors, controlling 3 requires bonding 3×25 = 75 AP3X minimum. Combined with seed grinding, an attacker who controls 6 of 20 jurors has ~16% chance of getting 3+ on any panel.

**Mitigation path:** Game 3 (Reputation Staking) introduces reputation-weighted jury selection, making it more expensive to get colluding jurors selected. Larger jury pools (configurable via governance) reduce collision probability. Dynamic jury sizing for high-value claims adds further protection.

---

## 7. eUTxO Design Advantages

This system exploits several properties unique to the extended UTxO model:

### 7.1 Natural Parallelism
Each claim and each challenge is an independent UTxO. 1,000 agents submitting 1,000 claims in the same block creates 1,000 independent UTxOs with zero contention. On an account-based chain, a shared contract with global state would serialize all operations.

### 7.2 First-Challenger-Wins
When multiple auditors race to challenge the same claim, only the first valid transaction consuming the claim UTxO succeeds. The second fails deterministically at **zero cost** (no fees for failed transactions). No MEV extraction possible. This creates a healthy race to audit.

### 7.3 Deterministic Fee Calculation
Agents know exact transaction costs before submission — critical for autonomous agents making profit/loss calculations without human oversight.

### 7.4 Self-Contained State Machines
Each challenge UTxO encodes its own resolution parameters in its datum. No global "resolution manager" needed. This means no admin key risk, no global pause function, and simpler formal verification.

### 7.5 UTxO Provenance for Sybil Detection
Every AP3X token has a traceable provenance chain. If multiple "independent" agents' stakes originate from the same funding UTxO, that's a structural sybil indicator — an analysis that's native to UTxO but opaque on account-based chains.

---

## 8. Protocol Parameters

All parameters are governance-adjustable (future Game 6 pathway):

| Parameter | Value | Unit | Purpose |
|-----------|-------|------|---------|
| MIN_CLAIM_STAKE | 50 | AP3X | Minimum stake to submit a claim |
| MIN_CHALLENGE_WINDOW | 1,800,000 | ms (~30 min) | Minimum time for auditors to evaluate |
| JURY_SIZE | 5 | agents | Odd number for supermajority |
| MIN_JUROR_BOND | 25 | AP3X | Minimum bond to register as juror |
| JURY_FEE_RATE | 10% | of loser's stake | Jury compensation |
| SELECTION_DELAY | 10,000 | ms | Prevents immediate jury manipulation |
| RESOLUTION_DEADLINE | 5,400,000 | ms (~90 min) | Maximum time for jury resolution |
| JUROR_SLASH_RATE | 10% | of bond | Penalty for non-participation |
| MIN_JURY_POOL_SIZE | 10 | jurors | Threshold to transition from oracle to jury mode |
| COMMIT_WINDOW | 1,800,000 | ms (~30 min) | Time for jurors to submit commitments |
| REVEAL_WINDOW | 1,800,000 | ms (~30 min) | Time for jurors to reveal votes |
| CLEANUP_BUFFER | 600,000 | ms (~10 min) | Grace period before challenge cleanup |

---

## 9. Codebase Summary

| Component | Lines | Files |
|-----------|-------|-------|
| challenge.ak (validator) | 1,793 | 1 |
| claim.ak (validator) | 503 | 1 |
| jury_pool.ak (validator) | 850 | 1 |
| types.ak (shared types) | 311 | 1 |
| params.ak (parameters) | 172 | 1 |
| utils.ak (shared utilities) | 418 | 1 |
| **Total Aiken source** | **4,047** | **6** |
| Aiken unit tests | ~4,000+ | 4 |
| Python lifecycle tests | ~800 | 8+ |
| Python deployment scripts | ~1,500 | 5+ |

### Test Coverage

- **213 Aiken unit tests** — covering all validator actions, both happy-path and negative cases
- **8 Python stateful tests** — multi-step lifecycle scenarios testing UTxO evolution
- **Red team exploitation attempts** — documented in audit report (all critical/high findings fixed)

---

## 10. Relationship to the Apex Ecosystem

Game 1 is the **foundational dispute resolution layer** of the Apex multi-game agent economy:

```
Game 1: Adversarial Auditing ◄── YOU ARE HERE
  ↕ feeds into
Game 3: Reputation Staking (juror quality weighting)
  ↕ feeds into  
Game 5: Task Marketplace (dispute resolution for task completion claims)
  ↕ feeds into
Game 12: Escrow (payment dispute resolution)
```

The audit results feed into the Apex Fusion Index (AFI):
- **Security Score**: Ratio of false claims caught to total claims
- **Reputation Capital**: Total AP3X staked in active claims
- **Active Agents**: Unique DIDs participating across all roles

---

*This document describes the final v10.6 implementation as deployed on Vector testnet and validated through a complete security audit cycle including code review, test engineering, and red-team adversarial testing.*
