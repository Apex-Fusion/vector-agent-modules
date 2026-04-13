# Module 3: Reputation Staking — Security Audit Report

> **Chain:** Vector Testnet (Cardano Conway) | **Language:** Aiken v1.1.21+ / Plutus V3 | **Audit Date:** 2026-04-13
> **Tests:** 110/110 pass | **Status:** CONDITIONAL (pending on-chain falsified path test)

---

## 1. Contract Summary

Module 3 implements an economically-secured reputation staking system for AI agents on the Vector testnet. Two Aiken multi-validators (reputation + endorsement) manage the lifecycle of self-stakes, endorsements, challenges, history bonuses, and decay. Agents stake AP3X to back capability claims; other agents endorse or challenge those claims. Challenges are resolved by a Foundation oracle (Phase 1.0) or via escalation to Module 1 Adversarial Auditing. The protocol treasury collects fees from slashing and decay. The system produces a self-curating directory of trustworthy AI agents with reputation tiers (Unverified through Elite).

Parties: agents (stakers/endorsers/challengers), Foundation oracle, protocol treasury, permissionless decay collectors.

## 2. Audit Scope

- **Files reviewed:**
  - `validators/reputation.ak` — Self-stake multi-validator (mint + spend)
  - `validators/endorsement.ak` — Endorsement + challenge multi-validator (mint + spend)
  - `lib/reputation_staking/types.ak` — All datum and redeemer types
  - `lib/reputation_staking/stake_validation.ak` — Stake operation validation
  - `lib/reputation_staking/endorsement_validation.ak` — Endorsement operation validation
  - `lib/reputation_staking/challenge_validation.ak` — Challenge operation validation
  - `lib/reputation_staking/decay.ak` — Decay calculation and validation
  - `lib/reputation_staking/scoring.ak` — Tier computation
  - `lib/reputation_staking/params.ak` — Protocol parameters
  - `lib/reputation_staking/config.ak` — Cross-validator configuration
  - `lib/reputation_staking/utils.ak` — Module-specific utilities
  - `lib/shared/utxo.ak` — Shared UTXO utilities
  - `lib/shared/token_naming.ak` — Shared token name derivation
  - `lib/shared/did_verification.ak` — Shared DID verification
  - `lib/reputation_staking/audit_exploit_tests.ak` — Prior audit exploit tests
  - `lib/reputation_staking/test_helpers.ak` — Test scaffolding
  - All test files (`*_tests.ak`)
- **Lines of code:** ~6,600 (validators + library + shared + tests)
- **Redeemer actions:**
  - Reputation mint: MintStakeToken, BurnStakeToken, MintHistoryBonus, BurnGenesisBonus
  - Reputation spend: CreateStake, IncreaseStake, DecreaseStake, UpdateCapabilities, ClaimDecayRefund, SlashStake
  - Endorsement mint: MintEndorsementToken, BurnEndorsementToken, MintChallengeToken, BurnChallengeToken
  - Endorsement spend: IncreaseEndorsement, WithdrawEndorsement, SlashEndorsement, WithdrawChallenge, RespondToChallenge, EscalateToAudit, ResolveEscalation, ResolveChallenge, DefaultJudgment, DistributeOutcome
- **Methodology:** Static analysis (Apex v2, 6 first-pass + 4 full-scan categories) -> test verification -> report

---

## 3. Findings Summary

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| F-A1 | SlashEndorsement — fake challenge datum exploit + single-input guard conflict | Critical | Fixed ✅ |
| F-A2 | CapabilityFalsified distribution — StakeUTXO consumption without valid redeemer | High | Fixed ✅ |
| F-A3 | Active challenge bypass via reference input omission (F4b) | Low | Accepted (Phase 1.0) |
| F-A4 | Genesis bonus on-chain uniqueness check is ineffective | Low | Accepted (oracle mitigates) |
| F-A5 | Staking credential not validated on validator outputs | Low | Open |
| F-A6 | HistoryBonus source validation defaults to True for most sources | Info | Accepted (Phase 1.0) |
| F-A7 | DefaultJudgment fee extraction creates datum/value accounting mismatch | Info | Open |

---

### Finding Detail: F-A1 — SlashEndorsement Fake Challenge Exploit

**Severity:** Critical
**Status:** Fixed ✅ (2026-04-13)
**Location:** `validators/endorsement.ak`, called via `SlashEndorsement { challenge_ref }`

**Description:**

`validate_endorsement_slash` reads the challenge from `tx.inputs` using an attacker-controlled `challenge_ref` (from the redeemer), without verifying the challenge UTxO is at the endorsement validator address or carries a valid challenge token. This creates two compounding issues:

1. **Fake challenge exploit:** An attacker can create a UTxO at their own wallet address with a crafted `ReputationChallengeDatum` inline datum (state = `Resolved { CapabilityFalsified }`, target_did = victim). Since the UTxO is at a regular wallet address, no validator fires for it — it's just a standard input. All validation checks pass because the attacker controls every field of the fake datum.

2. **Real challenges are unusable:** If a legitimate challenge UTxO (at the endorsement validator address) is included in `tx.inputs`, it counts as a second endorsement script input. The `count_script_inputs(tx, own_hash) == 1` guard on line 357 rejects the transaction. This means `SlashEndorsement` can NEVER work with a real resolved challenge.

The result is that `SlashEndorsement` only succeeds with fake challenges — the action is both exploitable and non-functional for its intended purpose.

**Attack Scenario:**

```
1. Attacker identifies victim's endorsement UTxO (endorser A -> target B, 500 AP3X)

2. Attacker creates a wallet UTxO with inline datum:
   ReputationChallengeDatum {
     target_did: B,                              // matches endorsement
     state: Resolved { CapabilityFalsified },     // triggers slash
     challenged_capability: "code_review",        // in endorsed_capabilities
     ... (other fields: arbitrary)
   }

3. Attacker builds transaction:
   Inputs:  [victim_endorsement_utxo, attacker_fake_challenge_utxo]
   Redeemer: EndorsementSpend(SlashEndorsement { challenge_ref: attacker_utxo_ref })
   Outputs: [reduced_endorsement_utxo (250 AP3X), attacker_receives_250_AP3X]

4. Endorsement validator fires for victim_endorsement_utxo:
   - count_script_inputs == 1  (fake challenge is NOT at script address) ✓
   - validate_endorsement_slash:
     - challenge_input found in tx.inputs ✓ (the fake UTxO)
     - same_target: B == B ✓
     - is_falsified: Resolved(CapabilityFalsified) ✓
     - capability_endorsed: "code_review" in list ✓
     - output_valid: reduced endorsement exists ✓
   → PASSES. 250 AP3X stolen from endorser.
```

**Vulnerable Code:**

```aiken
// endorsement.ak:167-173 — No address verification on challenge input
expect Some(challenge_input) =
    list.find(
      tx.inputs,
      fn(input) { input.output_reference == challenge_ref },
    )
expect InlineDatum(raw_challenge) = challenge_input.output.datum
expect challenge_datum: ReputationChallengeDatum = raw_challenge
```

**Recommended Fix:**

Use `tx.reference_inputs` instead of `tx.inputs` to read the challenge (no consumption needed), and verify it's at the endorsement validator address with a valid challenge token:

```aiken
fn validate_endorsement_slash(
  tx: Transaction,
  cfg: ReputationConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  endorsement_datum: EndorsementDatum,
  challenge_ref: OutputReference,
) -> Bool {
  // Fix: Read challenge from REFERENCE inputs (not consumed inputs)
  expect Some(challenge_input) =
    list.find(
      tx.reference_inputs,
      fn(input) { input.output_reference == challenge_ref },
    )

  // Fix: Verify challenge UTxO is at the endorsement validator address
  let is_at_endorsement =
    when challenge_input.output.address.payment_credential is {
      Script(hash) -> hash == refs.endorsement_policy_id
      _ -> False
    }
  expect is_at_endorsement

  // Fix: Verify challenge token exists (proves it was minted through proper flow)
  let has_challenge_token =
    list.any(
      assets.flatten(challenge_input.output.value),
      fn(entry) {
        let (pid, name, _qty) = entry
        pid == refs.endorsement_policy_id && starts_with(name, challenge_token_prefix)
      },
    )
  expect has_challenge_token

  // Parse and validate as before
  expect InlineDatum(raw_challenge) = challenge_input.output.datum
  expect Challenge(challenge_datum): EndorsementValidatorDatum = raw_challenge
  // ... rest of validation unchanged
}
```

**Fix Applied:**

1. Changed `tx.inputs` to `tx.reference_inputs` for challenge lookup (no consumption needed, avoids single-input guard conflict)
2. Added address verification: challenge UTxO must be at the endorsement validator address (`Script(hash) == refs.endorsement_policy_id`)
3. Added challenge token verification: UTxO must carry a `rchl_`-prefixed token at the endorsement policy ID (proves legitimate mint)
4. Changed datum parsing from raw `ReputationChallengeDatum` to `Challenge(challenge_datum): EndorsementValidatorDatum` wrapper (correct Aiken nested Constr encoding)

**Verification:** 3 new unit tests in `audit_exploit_tests.ak`:
- `fa1_fake_challenge_at_wallet_address_rejected` (fail test) — fake datum at wallet address rejected
- `fa1_fake_challenge_without_token_detected` — missing challenge token rejected
- `fa1_real_challenge_slash_endorsement_passes` — legitimate slash with correct address + token + wrapper passes

---

### Finding Detail: F-A2 — CapabilityFalsified Distribution Path Non-Functional

**Severity:** High
**Status:** Fixed ✅ (2026-04-13)
**Location:** `lib/reputation_staking/challenge_validation.ak` (`validate_falsified_payouts`)

**Description:**

When a challenge is resolved as `CapabilityFalsified`, `validate_falsified_payouts` expects the target agent's StakeUTXO to be consumed via `tx.inputs` (line 536-546) so the target can be slashed. However, the StakeUTXO is at the reputation validator address, meaning the reputation validator's spend handler fires and must accept a valid `StakeAction` redeemer.

No `StakeAction` variant supports being slashed:
- `DecreaseStake` requires the **owner's** signature (adversarial target won't cooperate)
- `ClaimDecayRefund` requires oracle signature but uses a decay formula that produces different amounts than slashing
- `CreateStake`, `IncreaseStake`, `UpdateCapabilities` don't reduce stake

This means the CapabilityFalsified distribution transaction cannot be constructed — the reputation validator will reject it regardless of the redeemer used.

The on-chain smoke test (8/8 steps) only tests `CapabilityVerified` outcome, which does NOT consume the target's StakeUTXO. The falsified path has never been executed on-chain.

**Attack Scenario:**

This is not an exploit but a functionality gap:
```
1. Challenge filed: Agent C challenges Agent A's "code_review" capability
2. Oracle resolves: CapabilityFalsified — Agent A's claim was false
3. Anyone calls DistributeOutcome
4. validate_falsified_payouts tries to find Agent A's StakeUTXO in tx.inputs
5. Including Agent A's StakeUTXO triggers reputation validator spend handler
6. No valid StakeAction exists for slashing → transaction REJECTED
7. Challenge is permanently stuck in Resolved state — no distribution possible
```

**Vulnerable Code:**

```aiken
// challenge_validation.ak:536-546 — Requires consuming target's StakeUTXO
expect Some(stake_input) =
    list.find(
      tx.inputs,                    // <-- consumed, not referenced
      fn(input) {
        assets.quantity_of(
          input.output.value,
          refs.reputation_policy_id,
          target_stake_token,
        ) >= 1
      },
    )
```

**Recommended Fix:**

Add a new `StakeAction` variant for permissionless slashing, or restructure to use reference inputs:

Option A — Add `SlashStake` action to reputation validator:
```aiken
pub type StakeAction {
  CreateStake
  IncreaseStake
  DecreaseStake { amount: Int }
  UpdateCapabilities { new_capabilities: List<ByteArray> }
  ClaimDecayRefund
  SlashStake { challenge_ref: OutputReference }  // NEW
}
```

The `SlashStake` handler would:
1. Verify the challenge is resolved as CapabilityFalsified (via reference input)
2. Calculate slash amount: `stake_amount / num_capabilities`
3. Produce continuing StakeUTXO with reduced stake
4. Require oracle signature (or be permissionless like DistributeOutcome)

Option B — Use reference inputs for stake reading:
```aiken
// Read target's stake from reference inputs instead of consuming it
expect Some(stake_ref) =
    list.find(
      tx.reference_inputs,
      fn(input) { ... },
    )
```

This avoids consuming the StakeUTXO entirely but means the target's on-chain stake isn't actually reduced. The slash amount would come from the challenge stake redistribution instead, which changes the economic model.

Option A is recommended as it preserves the intended slashing semantics.

**Fix Applied (Option A):**

1. Added `SlashStake { challenge_ref: OutputReference }` variant to `StakeAction` in `types.ak`
2. Added `validate_slash_stake` function in `stake_validation.ak` (~70 lines):
   - Finds challenge in `tx.inputs` (consumed by DistributeOutcome on the endorsement validator in the same transaction)
   - Verifies challenge UTxO is at endorsement validator address
   - Verifies challenge token (`rchl_` prefix) exists
   - Parses as `Challenge(challenge_datum): EndorsementValidatorDatum` wrapper
   - Checks state is `Resolved { CapabilityFalsified }`
   - Checks `target_did` matches the stake being slashed
   - Calculates slash: `stake_amount / num_capabilities`
   - Verifies continuing output with reduced stake, same owner/capabilities/history, updated timestamp
3. Added `SlashStake` case to reputation validator spend handler in `reputation.ak`

Key design insight: DistributeOutcome (endorsement validator) + SlashStake (reputation validator) coexist in the same transaction because `count_script_inputs` counts per-address, and they're at different addresses.

**Verification:** 4 new unit tests in `audit_exploit_tests.ak`:
- `fa2_slash_stake_reduces_target_stake` — legitimate slash reduces stake correctly
- `fa2_slash_stake_rejects_fake_challenge_at_wallet` (fail test) — fake challenge at wallet address rejected
- `fa2_slash_stake_rejects_challenge_without_token` (fail test) — missing challenge token rejected
- `fa2_slash_stake_rejects_wrong_slash_amount` (fail test) — incorrect slash calculation rejected

---

### Finding Detail: F-A3 — Active Challenge Bypass via Reference Input Omission

**Severity:** Low
**Status:** Accepted (Phase 1.0 limitation)
**Location:** `lib/reputation_staking/stake_validation.ak` lines 313-357 (`verify_no_active_challenges`)

**Description:**

`verify_no_active_challenges` checks `tx.reference_inputs` for open challenges, but an attacker can simply omit the reference input containing the challenge UTxO. This allows an agent to decrease their stake or update capabilities even while actively challenged. Already documented as F4b in prior audit. Test `f4_decrease_stake_bypasses_challenge_check` demonstrates the bypass.

Phase 2 will use oracle attestation instead of reference input checks.

**Verification:** Existing test `f4_decrease_stake_bypasses_challenge_check` confirms bypass.

---

### Finding Detail: F-A4 — Genesis Bonus On-Chain Uniqueness Check Ineffective

**Severity:** Low
**Status:** Accepted (oracle mitigates)
**Location:** `validators/reputation.ak` lines 157-172 (`validate_genesis_mint`)

**Description:**

The genesis bonus uniqueness check verifies that no genesis token exists in `tx.inputs`:

```aiken
let already_has_genesis =
    list.any(
      tx.inputs,
      fn(input) {
        list.any(
          assets.flatten(input.output.value),
          fn(entry) {
            let (pid, name, _qty) = entry
            pid == reputation_policy_id && starts_with(name, genesis_prefix)
          },
        )
      },
    )
let is_unique = !already_has_genesis
```

An agent who already has a genesis bonus can mint another by simply not including their existing genesis UTxO in the transaction inputs. The check only examines the current transaction's inputs, not all existing genesis tokens on-chain.

Mitigated by the oracle signature requirement — the oracle tracks which agents have received bonuses off-chain and would refuse to sign duplicate mints.

**Verification:** Not tested — low priority given oracle mitigation.

---

### Finding Detail: F-A5 — Staking Credential Not Validated on Validator Outputs

**Severity:** Low
**Status:** Open
**Location:** Multiple locations — all output address checks only verify `payment_credential`

**Description:**

When validators check that outputs are at the correct address (e.g., continuing StakeUTXO at reputation address), they only verify the payment credential:

```aiken
when output.address.payment_credential is {
  Script(hash) -> hash == refs.reputation_policy_id
  _ -> False
}
```

The staking credential is not checked. A transaction builder could attach their own staking credential to the output address, redirecting staking rewards from UTxOs locked at the validator address. Funds remain accessible (same payment credential), but staking rewards go to the attacker.

**Verification:** Not tested — standard Low-severity eUTxO pattern.

---

### Finding Detail: F-A6 — HistoryBonus Source Validation Defaults to True

**Severity:** Info
**Status:** Accepted (Phase 1.0 design)
**Location:** `validators/reputation.ak` lines 116-135 (`validate_history_bonus_mint`)

**Description:**

Most `HistoryBonusSource` variants bypass source-specific validation:

```aiken
ChallengeWon -> True
_ -> True  // ProposalAdopted, CritiqueIncorporated, EscrowCompleted, etc.
```

The oracle signature (enforced at the mint handler level) is the sole defense against arbitrary bonus minting. This is by design for Phase 1.0 — cross-module validation will be implemented when those modules are deployed.

**Verification:** N/A — design observation.

---

### Finding Detail: F-A7 — DefaultJudgment Fee Creates Datum/Value Accounting Mismatch

**Severity:** Info
**Status:** Open
**Location:** `lib/reputation_staking/challenge_validation.ak` lines 204-254 (`validate_default_judgment`)

**Description:**

DefaultJudgment extracts a fee from the challenge UTxO's AP3X value:
```aiken
let fee = input_datum.stake_amount * params.default_judgment_fee / 10_000
```

But `datum_preserved` ensures `output_datum.stake_amount == input_datum.stake_amount`. After DefaultJudgment, the continuing challenge UTxO's datum claims the original stake amount while actually holding `stake_amount - fee` in AP3X.

During subsequent DistributeOutcome, calculations reference `challenge_datum.stake_amount` (the original amount). The `verify_payment_to_credential` uses `>=` and doesn't verify WHERE the AP3X comes from, so the transaction can succeed if additional AP3X is provided. But it creates a minor inconsistency: the challenger or target receives slightly less than expected from the challenge UTxO, with the shortfall covered by other inputs.

With typical `default_judgment_fee` of 100 basis points (1%), the impact is small.

**Verification:** Not tested — minor accounting observation.

---

## 4. First-Pass Check Results

| Check | Result |
|-------|--------|
| Double Satisfaction (`list.any`) | ✅ Clear — Both spend handlers enforce `count_script_inputs(tx, own_hash) == 1`. Mint handlers run once per policy per transaction. |
| Output-Index Pinning | ✅ N/A — Not used. Continuing outputs identified by unique token identity. |
| Cross-Input Consistency | ✅ Clear — Single-input guard prevents multi-input aggregation. Cross-validator interactions (DistributeOutcome) involve different script addresses. |
| Integer Arithmetic | ✅ Clear — All division operations use positive values with guards. `epoch_length > 0` (reputation.ak:345), `num_capabilities > 0` (challenge_validation.ak:551). Truncation rounds in favor of protocol. |
| Token Identity Validation | ✅ Clear — All policy IDs from compile-time config. No user-provided policy IDs accepted. Agent DIDs validated against registry NFTs. |
| Tautological Datum Validation | ✅ Clear — All continuation checks compare output datum fields against **input** datum fields (old → new pattern). Verified for all 9 datum update paths. |

## 5. Test Results

| Category | Tests | Pass | Fail |
|----------|-------|------|------|
| Behavioral (happy path) | 75 | 75 | 0 |
| Security (exploit / fix verification) | 19 | 19 | 0 |
| Property / scoring / utility | 16 | 16 | 0 |
| **Total** | **110** | **110** | **0** |

Security tests cover: F1 (orphan burn), F3 (owner impersonation), F4 (challenge bypass), F8 (withdrawal verification), datum integrity, F-A1 (fake challenge exploit — 3 tests), F-A2 (slash stake — 4 tests).

## 6. Testnet Results

| Test | Result |
|------|--------|
| Register Agent A | ✅ Accepted (smoke_test_ogmios.py step 1) |
| Register Agent B | ✅ Accepted (step 2) |
| Seed UTXO | ✅ Accepted (step 3) |
| CreateStake | ✅ Accepted (step 4) |
| MintEndorsement | ✅ Accepted (step 5) |
| MintChallenge | ✅ Accepted (step 6) |
| ResolveChallenge (CapabilityVerified) | ✅ Accepted (step 7) |
| DistributeOutcome (CapabilityVerified) | ✅ Accepted (step 8) |
| DistributeOutcome (CapabilityFalsified) | ❓ Not tested |
| SlashEndorsement | ❓ Not tested |

## 7. Overall Verdict

**CONDITIONAL** (pending on-chain falsified path test)

The contract demonstrates strong defensive coding practices: double satisfaction is prevented via single-input guards, datum continuation checks are correctly non-tautological, authorization uses input datum credentials, timing logic has no dead zones, and all division operations are guarded. All prior audit findings (F1-F8) and the two findings from this audit (F-A1, F-A2) have been addressed and verified with unit tests.

**Fixed in this audit:**

1. **F-A1 (Critical → Fixed ✅):** `SlashEndorsement` now reads challenges from `tx.reference_inputs` with address verification, challenge token verification, and correct wrapper datum parsing. Both the fake-challenge exploit and the single-input guard conflict are resolved.

2. **F-A2 (High → Fixed ✅):** New `SlashStake { challenge_ref }` action enables permissionless stake slashing after `CapabilityFalsified`. Works in the same transaction as `DistributeOutcome` since the validators are at different addresses (per-address script input counting).

**Remaining items:**

The CapabilityFalsified → DistributeOutcome → SlashStake → SlashEndorsement flow has been verified with 7 new unit tests (110/110 total) but has NOT been executed on-chain. The existing smoke test covers CapabilityVerified only.

### Conditions for APPROVED:

- [x] F-A1: Fix `validate_endorsement_slash` — reference inputs, address check, token check, wrapper parsing. 3 exploit/fix tests added.
- [x] F-A2: Add `SlashStake` action to reputation validator. 4 exploit/fix tests added.
- [ ] Add on-chain smoke test steps for CapabilityFalsified → DistributeOutcome → SlashStake → SlashEndorsement flow.

---

*Audited by: Claude (Apex v2 Single-Agent Audit)*
*Methodology: Apex v2 Security Audit — 6 first-pass critical checks + 4 full-scan categories*
*Date: 2026-04-13*
