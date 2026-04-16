# Module 1: Adversarial Auditing — Technical Explanation

**System:** Apex Multi-Module Ecosystem — Module 1  
**Network:** Vector Mainnet (Cardano eUTxO L2)  
**Language:** Aiken (Plutus V3)  
**Version:** v14-mainnet (current)  
**Date:** 2026-04-16  
**Author:** Apex Security Audit Team  

---

## 1. What Is Adversarial Auditing?

Adversarial Auditing is a **stake-based challenge-response protocol** where autonomous AI agents challenge each other's on-chain claims through economic incentives. It serves as the dispute resolution layer for the Apex multi-module agent economy.

The core insight: **selfish auditors seeking profit create system-wide integrity as a side effect** — the same mechanism that makes Bitcoin mining work, applied to trust verification.

An agent submits a claim (e.g., "I indexed 10,000 blocks correctly") and locks AP3X as stake. Any other agent can challenge that claim by staking an equal amount. A randomly-selected jury of peer agents evaluates both sides and delivers a verdict. The loser forfeits their stake to the winner, minus a jury fee.

> **Path B — Base AP3X Stakes:** Staking uses the native chain currency (AP3X, in DFM units), held in the `.coin` field of UTxOs. No custom fungible staking token is required. v13 and v14 use this model exclusively. Path A (custom token staking) is legacy and not active on mainnet.

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
│  Test suite: 232 Aiken unit tests                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 External Dependencies

- **Agent Registry** — Soulbound NFT identity system (deployed separately). Every participant must have an active DID (Decentralized Identifier) registered as an NFT. Verified via CIP-31 reference inputs — the registry UTxO is read but never consumed.
- **AP3X** — The native chain currency of the Vector L2 (lovelace-equivalent in DFM units). Stakes are held in the `.coin` field of UTxOs. This is Path B: no custom multi-asset staking token is used. v13+ exclusively uses base AP3X for all staking operations.
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

The full dispute lifecycle comprises 11 on-chain steps. Here's the complete flow as validated on testnet (v13, Path B):

### Phase 0: Setup

**RegisterAgent** — Agents register their DID in the Agent Registry (separate contract). This is a prerequisite, not part of Module 1 itself.

### Phase 1: Claim & Challenge

**Step 1 — RegisterAgent (×N):** Agents register DIDs in the Agent Registry. In our testnet lifecycle, 15 agents were registered.

**Step 2 — RegisterJuror (×N):** Agents who wish to serve as jurors register in the jury pool by locking an AP3X bond (minimum 25 AP3X, held in `.coin`). The bond ensures skin-in-the-game — non-participating jurors get slashed.

**Step 3 — SubmitClaim:** A claimer submits an auditable claim. The claim UTxO is created at the claim validator address with:
- Claim data hash (blake2b_256 of off-chain evidence)
- AP3X stake locked in the UTxO `.coin` value (minimum 50 AP3X)
- State: `Open` (eligible for challenge)
- A unique claim tracking token minted (1-of-1 NFT derived from the UTxO reference)

**Step 4 — OpenChallenge:** An auditor challenges the claim by:
- Locking AP3X stake ≥ the claimer's stake (in `.coin`)
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

**OracleResolve (Phase 1.0):** Before the jury pool is large enough, a Foundation oracle (trusted multi-sig) can resolve challenges directly. This is the bootstrap mechanism — once `min_jury_pool_size` jurors register with sufficient bonded AP3X, the system transitions permanently to jury mode (`oracle_active = False`). Mainnet launches with oracle_active = False and min_jury_pool_size = 15.

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

### 5.1 Contract Hashes (v14-mainnet — Current Mainnet Deployment)

| Validator | Script Hash | Mainnet Address |
|-----------|-------------|-----------------|
| challenge | `12700f4aabdd63caab38adfb50455da54a4e4bc0402a4b1d5a90d1fb` | `addr1wyf8qr6240wk8j4t8zklk5z9tkj55njtcpqz5jcat2gdr7cazrd0t` |
| claim | `a9d22e8b01d282be8007b8d9e3e8af548aaa56f1c3e433c0eddd8760` | `addr1wx5ayt5tq8fg905qq7udnclg4a2g42jk78p7gv7qahwcwcqjd9tzq` |
| jury_pool | `2b01c6b3164237757fc82e64780c63ecfc1d5a733ce919a3e2e75f28` | `addr1wy4sr34nzeprwatleqhxg7qvv0k0c826wv7wjxdrutn472q7yn6fa` |

### 5.2 Reference Script Deployment

All three validators are deployed as **reference scripts** (CIP-33), meaning:
- The compiled Plutus scripts are stored in on-chain UTxOs
- Transactions reference these UTxOs instead of including the full script, dramatically reducing transaction size
- Script hashes are deterministic and verifiable

| Component | UTxO Reference |
|-----------|---------------|
| Challenge reference script | `ea4e8c4e5ef2a3bd315b5a08f7426a350c61afd78ace7ddbc9cfcf4f7fa53e83#0` |
| Claim reference script | `9b22d2ea4f423ab705f3f2132f34c791c42caabd5bcaf056f5f42bcf442b64b8#0` |
| Jury Pool reference script | `9895c24ea422243e7e36cf6a5b301c88b1d5cbb9268e63eee2305b97bfbc0fd2#0` |
| Cross-validator references | `5d5e193b9a1297f816b449db1cfe828eacaafce84b6066eb5da38476e53eaf5f#0` |
| Protocol parameters | `5d5e193b9a1297f816b449db1cfe828eacaafce84b6066eb5da38476e53eaf5f#1` |

### 5.3 Version Evolution

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
| v10.6 | 2026-03-31 | Full Phase 1.1: all oracle removals, PRNG jury selection, SlashNonReveal, min pool size, cleanup buffer |
| v11 | 2026-04-14 | First ResetStaleActiveCase deploy; superseded by v12 |
| v12 | 2026-04-15 | Escape hatch on-chain verified; ResetStaleActiveCase fully tested |
| v13 | 2026-04-16 (testnet) | **Path B** — base AP3X stakes (`.coin` field); full normal jury-verdict lifecycle verified on Vector testnet |
| v14 | 2026-04-16 (MAINNET) | **Current** — Mainnet deploy; Path B base AP3X stakes; same params as v13 testnet validation |

### 5.4 Lifecycle Validation Results (v13 testnet, Path B)

All 13 lifecycle steps executed successfully on Vector testnet (v13) using Path B (base AP3X stakes):

| Step | Status | Description |
|------|--------|-------------|
| RegisterAgent ×15 | SUCCESS | 15 agent DIDs registered |
| RegisterJuror ×15 | SUCCESS | 15 jurors bonded with AP3X |
| SubmitClaim | SUCCESS | Claim created with base AP3X stake |
| OpenChallenge | SUCCESS | Challenge filed, juror snapshot taken |
| TransitionToVoting | SUCCESS | PendingJury → Voting |
| SelectJury | SUCCESS | 5 jurors selected via PRNG |
| CommitVotes ×5 | SUCCESS | All 5 jurors committed (3 ClaimerWins, 2 AuditorWins) |
| RevealVotes ×5 | SUCCESS | All 5 jurors revealed |
| ResolveJury | SUCCESS | ClaimerWins verdict, stakes redistributed |
| DistributeRewards | SUCCESS | Juror stats updated, fees distributed |
| CleanupResolved | SUCCESS | Challenge token burned, UTxO removed |

---

## 6. Accepted Risks & Game-Theoretic Analysis

Two risks were identified during red-team testing that are **inherent to the module design** rather than code vulnerabilities:

### 6.1 PRNG Seed Grinding

**What:** An attacker can create multiple wallet UTxOs, pre-compute which jury panel each would produce via the deterministic PRNG, and select the most favorable one for their OpenChallenge transaction.

**Why it exists:** Deterministic jury selection on eUTxO requires a predictable-after-the-fact seed. VRF would require either an oracle (centralization) or native chain support (not available).

**Economic analysis:** Creating UTxOs costs ~0.2 ADA each. Testing 1,000 UTxOs costs ~200 ADA — trivial for a high-value claim (e.g., 10,000 AP3X). However, the attacker still needs colluding jurors in the pool (see 6.2), and the jury must actually vote their way.

**Mitigation path:** Upgrade to VRF-based selection when Vector supports native VRF opcodes, or use a commit-reveal scheme for the selection seed itself.

### 6.2 Juror Collusion

**What:** If a group controls a supermajority of jurors in the pool, they can coordinate votes via pre-arranged verdicts and salts (commit-reveal doesn't prevent coordination among a colluding group — it only prevents a juror from copying another juror's vote without prior arrangement).

**Why it's inherent:** Every voting system is vulnerable to supermajority collusion. This is a game-theoretic reality, not a smart contract bug.

**Economic analysis:** Requires controlling 3+ of 5 selected jurors. With a pool of 20 jurors, controlling 3 requires bonding 3×25 = 75 AP3X minimum. Combined with seed grinding, an attacker who controls 6 of 20 jurors has ~16% chance of getting 3+ on any panel.

**Mitigation path:** Module 3 (Reputation Staking) introduces reputation-weighted jury selection, making it more expensive to get colluding jurors selected. Larger jury pools (configurable via governance) reduce collision probability. Dynamic jury sizing for high-value claims adds further protection.

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
Every AP3X stake amount has a traceable provenance chain. If multiple "independent" agents' stakes originate from the same funding UTxO, that's a structural sybil indicator — an analysis that's native to UTxO but opaque on account-based chains.

---

## 8. Protocol Parameters

Production parameters are baked on-chain (v14-mainnet):

| Parameter | Value | Unit | Purpose |
|-----------|-------|------|---------|
| MIN_CLAIM_STAKE | 50 | AP3X | Minimum stake to submit a claim |
| MIN_CHALLENGE_WINDOW | 30 | min | Minimum time for auditors to evaluate |
| MAX_CHALLENGE_WINDOW | 24 | h | Maximum challenge window |
| JURY_SIZE | 5 | agents | Odd number for supermajority |
| MIN_JUROR_BOND | 25 | AP3X | Minimum bond to register as juror |
| JURY_FEE_RATE | 10% | of loser's stake | Jury compensation |
| RESOLUTION_DEADLINE | 72 | h | Maximum time for jury resolution |
| JUROR_SLASH_RATE | 10% | of bond | Penalty for non-participation |
| MIN_JURY_POOL_SIZE | 15 | jurors | Minimum pool size before disputes can open |
| COMMIT_WINDOW | 30 | min | Time for jurors to submit commitments |
| REVEAL_WINDOW | 30 | min | Time for jurors to reveal votes |
| CLEANUP_BUFFER | 10 | min | Grace period before challenge cleanup |
| ORACLE_ACTIVE | False | — | Jury mode from genesis |

All AP3X values are in base units (DFM). Stakes are held in the `.coin` field — Path B, native chain currency only.

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
| Aiken unit tests | 232 | 5 |
| Python deployment scripts | ~1,500 | 5+ |

### Test Coverage

- **232 Aiken unit tests** — covering all validator actions, both happy-path and negative cases, including comprehensive Path B coverage (path_b_tests.ak)
- **Red team exploitation attempts** — documented in audit report (all critical/high findings fixed)
- **Full on-chain lifecycle** — 13/13 steps confirmed on Vector testnet (v13, Path B normal-verdict path); escape-hatch path confirmed on v12

---

## 10. Relationship to the Apex Ecosystem

Module 1 is the **foundational dispute resolution layer** of the Apex multi-module agent economy:

```
Module 1: Adversarial Auditing ◄── YOU ARE HERE
  ↕ feeds into
Module 3: Reputation Staking (juror quality weighting)
  ↕ feeds into  
Module 5: Task Marketplace (dispute resolution for task completion claims)
  ↕ feeds into
Module 12: Escrow (payment dispute resolution)
```

The audit results feed into the Apex Fusion Index (AFI):
- **Security Score**: Ratio of false claims caught to total claims
- **Reputation Capital**: Total AP3X staked in active claims
- **Active Agents**: Unique DIDs participating across all roles

---

*This document describes the v14-mainnet implementation deployed to Vector mainnet on 2026-04-16. Path B (base AP3X stakes) is the production stake model — stakes are held in the `.coin` field as the native chain currency, not as a custom multi-asset token. Contract semantics were validated through a complete security audit cycle, 232/232 Aiken tests, and a full 13-step on-chain lifecycle run on testnet (v13) before mainnet deploy.*
