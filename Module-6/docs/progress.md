# Module 6: Governance Suggestion Engine — Progress Tracker

Spec reference: `Module-6/docs/GAME-6-GOVERNANCE-SUGGESTION-ENGINE-IMPL-SPEC.md` (v0.4)

---

## Phase 1.0 — Minimum Viable Governance

### Smart Contracts (Aiken)

- [x] All on-chain types — `ProposalDatum`, `CritiqueDatum`, `GovernanceEndorsementDatum`, `ProposalPriority`, `ProposalState`, `ProposalAction`, `CritiqueAction`, `GovernanceEndorsementAction`, `GovernanceOracleDatum`, `OracleGovernanceAction`, `GovernanceParams`, `GovernanceConfig`, `ProposerActivityDatum`, `TreasuryBatchDatum` (`lib/governance_suggestion/types.ak`)
- [x] Proposal validator — mint + spend multi-validator (`validators/proposal.ak`)
- [x] Critique & endorsement validator — mint + spend multi-validator (`validators/critique.ak`)
- [x] Proposal validation logic — all 8 actions: SubmitProposal, WithdrawProposal, AmendProposal, AdoptProposal, RejectProposal, ExpireProposal, ExpireStaleProposal, ExtendReview (`lib/governance_suggestion/proposal_validation.ak`)
- [x] Critique validation logic — SubmitCritique, WithdrawCritique, IncorporateCritique, SubmitEndorsement, WithdrawEndorsement (`lib/governance_suggestion/critique_validation.ak`)
- [x] GovernanceParams reading + default testnet values (`lib/governance_suggestion/params.ak`)
- [x] Governable parameter name validation — Module 1, 3, 6 params (`lib/governance_suggestion/params.ak`)
- [x] ProposerActivity rate limiting + cooldown enforcement (`lib/governance_suggestion/activity_tracking.ak`)
- [x] Reward distribution — 70% proposer / 20% critics / 10% protocol, no-critic case (`lib/governance_suggestion/reward_distribution.ak`)
- [x] Treasury batch UTXO management (`lib/governance_suggestion/treasury_batch.ak`)
- [x] Utility functions — DID verify, oracle verify, signer verify, token names (`lib/governance_suggestion/utils.ak`)
- [x] Shared utility library extracted (`shared/lib/shared/` — DID verification, credential, UTXO helpers, token naming, oracle, bytearray)
- [x] Module 6 wired to shared library via symlink (`lib/shared/` -> `shared/lib/shared/`)

### Aiken Tests (168 passing)

- [x] Proposal tests — 49 tests (`tests/proposal_tests.ak`)
- [x] Critique tests — 19 tests (`tests/critique_tests.ak`)
- [x] Reward tests — 23 tests (`tests/reward_tests.ak`)
- [x] Integration tests — 30 tests (`tests/integration_tests.ak`)
- [x] Property-based tests — 47 tests (`tests/property_tests.ak`) — stake conservation, state machine integrity, activity monotonicity, critique idempotency, oracle exclusivity, reward bounds, token lifecycle, boundary values, governable params
- [ ] Validator-level tests with mock transactions (unit-level; on-chain lifecycle covered by smoke tests 1-9)

### On-Chain Libraries

- [x] Emergency validation library — extracted from `proposal_validation.ak` into dedicated `emergency.ak` (§5.1b: type gate, stake multiplier, reputation gate, review window)

### On-Chain Contract Fixes

- [x] Token name length: 5-byte prefix + 27-byte hash = 32 bytes
- [x] `GovernanceCrossRefs` expanded with `proposal_mint_hash` and `critique_mint_hash`
- [x] All `quantity_of` calls use `refs.proposal_mint_hash` for token operations
- [x] Activity tracking output parsing guarded by token check before `expect ProposerActivityDatum`
- [x] Mint guards use `refs.proposal_validator_hash` from CrossRefs for spend input check
- [x] Bug C: Token name derivation uses `own_ref` instead of `list.head(inputs)` (`proposal_validation.ak`)
- [x] Bug D: Activity tracking output checks use `proposal_mint_hash` (lines 76, 168 of `activity_tracking.ak`)
- [x] Debug fail-traces added to `validate_submit_proposal` for on-chain diagnostics (`if !check { fail @"FAIL:check_name" }`)
- [x] Debug fail-traces added to `validate_submit_rate_limit` None branch for detailed activity tracking diagnostics
- [x] Bug O: All temporal GovernanceParams values converted to POSIX ms (matching on-chain `validity_range` and `submitted_at`)
- [x] All 168 Aiken tests pass

### Python SDK (`agent-sdk-py/src/vector_agent/governance/`)

- [x] `GovernanceClient` class — all governance actions (`client.py`)
- [x] CBOR type encodings — all redeemer/datum types (`types.py`)
- [x] Datum builders — all datum types (`datums.py`)
- [x] Aiken blueprint reader (`blueprint.py`)
- [x] `GovernanceIndexer` class — on-demand UTxO queries for proposals/critiques/endorsements/treasury (`indexer.py`)

### SDK New Methods (local, not yet published to PyPI)

- [x] `validated_adopt_proposal()` — full on-chain adoption: burn token, decrement activity, oracle-signed (`client.py`)
- [x] `validated_expire_proposal()` — full on-chain expiry: burn token, decrement activity, permissionless (`client.py`)
- [x] `compute_critique_quality()` — 5-heuristic quality scoring (data_backed, specificity, novelty, track_record, timeliness) (`indexer.py`)
- [x] `compute_proposal_quality_signal()` — Foundation review ordering (reputation, endorsement, controversy, track record) (`indexer.py`)
- [x] `get_proposals_ranked()` — proposals sorted by quality signal for Foundation review (`indexer.py`)

### SDK Bug Fixes (local, not yet published to PyPI)

- [x] Bug A: `agent.py` — Added `validity_start` + `ttl` to spend branch TransactionBuilder (validators require `Finite` lower bound)
- [x] Bug E: `agent.py` — Added `validity_start: int | None` parameter to `interact_contract()` so callers can pass an explicit slot
- [x] Bug E: `client.py` — `validated_submit_proposal` re-queries tip after lock confirmation and passes aligned `validity_start`
- [x] Bug F: `client.py` — On-chain `current_slot` is POSIX time (ms), not slot number. SDK now queries genesis config (`slotLength`, `startTime`) and converts slot to POSIX ms for `submitted_at` and `last_proposal_slot`
- [x] Bug G: `client.py` — `review_window` stays in slots/seconds (matches GovernanceParams values), not converted to ms
- [x] `agent.py` — Reference UTxO resolution searches explicit address, script address, and wallet address
- [x] All prior SDK fixes (multi-asset parsing, evaluate_tx_cbor, fee_buffer, Bool encoding, mint support, etc.)

### Python Scripts (`Module-6/scripts/`)

- [x] `deploy.py` — Full deployment with `aiken blueprint apply`, deploys CrossRefs NFT directly at oracle holder address (not wallet)
- [x] `deploy.py` — Saves `cross_refs_address` in deploy_state, increased `fee_buffer` for reference scripts
- [x] `redeploy_proposal.py` — Targeted redeployment of changed validators + CrossRefs
- [x] `move_crossrefs.py` — Mint CrossRefs NFT at oracle holder (legacy, no longer needed since deploy.py does it directly)
- [x] `smoke_test.py` — CrossRefs hash verification, exact tx_hash matching from deploy_state, multi-pass discovery
- [x] `test_validated_submit.py` — Standalone test 6 (validated_submit_proposal) in isolation
- [x] `debug_traced_v2.py` — Lock at traced address and evaluate with full traces
- [x] `treasury_fund.py` — Create new treasury batch UTxOs (configurable count, size, dry-run mode)
- [x] `treasury_replenish.py` — Monitor batch count and auto-replenish when below threshold (status/force modes)
- [x] `test_expire.py` — Standalone expire test for time-dependent validation (run after review window elapses)
- [x] `test_expire_e2e.py` — End-to-end expire test: submit proposal, wait for review window, expire on-chain (includes temporal unit diagnostics)
- [x] `recreate_infra.py` — Recreate missing GovernanceParams, Oracle, CrossRefs NFT infrastructure UTxOs
- [x] `update_params.py` — Update GovernanceParams UTXO at always-succeeds holder (spend old, create new)

### Infrastructure

- [x] Nix dev environment — Aiken 1.1.21, Python 3.11, pycardano (`shell.nix`)
- [x] Testnet endpoint config — Ogmios, Submit, Koios, Explorer (`.env`)
- [x] Wallet + signing key (`wallets/`)
- [x] Deploy state JSON (`wallets/deploy_state.json`)

### Testnet Deployment

**Latest clean deployment (2026-04-03):** Built with `aiken build --trace-level verbose --trace-filter user-defined` for on-chain diagnostics. Full fresh deploy from wiped deploy_state.

| Validator | Applied Hash |
|-----------|-------------|
| proposal_mint | `722d6fd508384cdc91cb2436bff1d14b969c01c6bb2372e2186613ab` |
| proposal_spend | `f4fc49ff1ae95349ce9c6d13f6cd1d92c604600816a017be59160b37` |
| critique_mint | (current from deploy_state) |
| critique_spend | (current from deploy_state) |
| endorsement_spend | (current from deploy_state) |

CrossRefs NFT minted directly at oracle holder address. All 14 deployment TXs confirmed.

Tests 1-5 pass. Test 6 in progress — see Active Debugging below.

### Off-Chain

- [x] Indexer queries — `GovernanceIndexer` class in `agent-sdk-py/src/vector_agent/governance/indexer.py`
- [x] MCP server tools — 5 tools in `mcp-server/src/vector/governance.ts`
- [x] Foundation review dashboard — `Module-6/dashboard/` (FastAPI + vanilla HTML/JS, quality-ranked queue, adopt/reject/extend/expire, treasury, stats)

---

## Phase 1.1 — Full Critique & Amendment System

### On-Chain (done)

- [x] Amendment logic — incorporate critiques, update proposal hash/URI/state
- [x] Reward distribution — proposer + critic shares on adoption
- [x] Stale proposal detection — ExpireStaleProposal action
- [x] cross-module bonus UTXOs
- [x] Emergency proposal reputation gate
- [x] Dedicated `emergency.ak` validation library — extracted from `proposal_validation.ak`

### Off-Chain

- [x] MCP server tools — 5 governance tools in vector-mcp TypeScript server
- [x] Chain analytics agent template — `Module-6/agents/analytics_template.py` (treasury health, governance metrics, peer proposal review)
- [x] Critique quality scoring — 5-heuristic scoring in `GovernanceIndexer` + `quality_signal` for Foundation review ordering
- [x] Treasury funding pipeline — `scripts/treasury_fund.py`
- [x] Treasury batch replenishment automation — `scripts/treasury_replenish.py`
- [x] Foundation review dashboard — `Module-6/dashboard/` (proposal queue, detail view, oracle actions, treasury, stats)

---

## Foundation Review Dashboard (2026-04-07)

### Features

- [x] FastAPI backend with REST API (`Module-6/dashboard/server.py`)
- [x] Quality-ranked proposal queue — proposals sorted by quality_signal (reputation, endorsements, controversy, track record)
- [x] Emergency proposals highlighted with countdown timer
- [x] Proposal detail modal — critiques, endorsements, proposer track record, reward calculator
- [x] One-click oracle actions — adopt (with reward amount), reject, extend review, expire
- [x] Treasury view — batch count, total balance, runway estimate
- [x] Governance stats — adoption rate, proposal counts, chain health
- [x] Auto-poll every 30 seconds
- [x] Dual signing mode — `direct` (skey on server, testnet) / `external` (unsigned tx export, production)
- [x] File logging (`dashboard.log`) for debugging action failures
- [x] Orphan filtering — tokenless lock-only proposals excluded from all views
- [x] Activity UTxO selection — picks activity with `count >= 1` for expire/adopt

### Infrastructure (2026-04-06)

- [x] Recreated GovernanceParams, Oracle, CrossRefs NFT UTxOs (`scripts/recreate_infra.py`) — all 3 holder addresses had 0 UTxOs
- [x] Redeployed GovernanceParams UTXO with corrected POSIX ms values (`scripts/update_params.py`)
- [x] Full smoke test re-run (8/8 pass) with temporarily lowered cooldown, then restored to 24h

---

## Phase 1.2 — Prediction Market & Accountability (deferred)

Types defined, logic not implemented.

---

## Bug History (18 bugs found and fixed)

| Bug | Issue | Fix | Status |
|-----|-------|-----|--------|
| A | SDK never set `builder.validity_start` — validator requires `Finite` lower bound | `agent.py`: query tip, set `validity_start = tip - 60` | Fixed |
| B | CrossRefs NFT at wallet consumed by coin selection, losing datum | `deploy.py`: mint CrossRefs directly at oracle holder address | Fixed |
| C | Token name used `list.head(inputs)` (lexicographic first) — wallet input could sort before script input | `proposal_validation.ak`: use `own_ref` instead | Fixed |
| D | `activity_tracking.ak` output checks used `proposal_validator_hash` instead of `proposal_mint_hash` | Lines 76, 168: changed to `proposal_mint_hash` | Fixed |
| E | Slot mismatch: client queried tip once at lock time, agent recomputed at spend time | Added `validity_start` param to `interact_contract()`, client passes aligned slot | Fixed |
| F | On-chain `current_slot` is POSIX time (ms), SDK used slot numbers for `last_proposal_slot` | SDK queries genesis config, converts `slot * slot_length_ms + system_start_ms` | Fixed |
| G | `review_window` was converted to ms but GovernanceParams values are in slots | Keep review_window in original units (slots/seconds) | Fixed |
| H | **CBOR encoding mismatch**: Aiken `serialise_data()` uses indefinite-length arrays (`0x9f...0xff`), Python `cbor2` uses definite-length (`0x82`) — different blake2b_256 → wrong token name | Created `plutus_cbor.py` with `plutus_serialise_data()` matching Aiken's encoding; updated 3 token name functions in `client.py` | Fixed |
| I | **Activity UTXO datum crash**: `proposal_spend` had no case for activity UTXOs — `WithdrawProposal` tried `expect ProposalDatum` on a 4-field `ProposerActivityDatum`, crashing on `tailList` of empty list | Added `SpendActivity` variant to `ProposalAction` enum + handler in `proposal_spend` that guards on another proposal input being spent | Fixed |
| J | **Plutus constructor tag encoding**: Constructors 7+ use CBOR tags 1280+ (not 128+). `ExtendReview` (tag 128) and `SpendActivity` (tag 129) were invalid | Fixed to use `CBORTag(1280, [...])` and `CBORTag(1281, [])` in `types.py` | Fixed |
| K | **Activity count off-by-one**: `validate_finalize_activity` checked `out_count - 1 >= 0` instead of `out_count >= 0`, rejecting valid decrement from 1→0 | Changed to `out_activity.active_proposal_count >= 0` in `activity_tracking.ak` | Fixed |
| L | **Zeroed hash length mismatch**: `zeroed_hash` constant was 32 bytes but script hashes are 28 bytes — `reputation_validator_hash == zeroed_hash` always False, cross-module bonus never skipped | Changed to 28-byte zeroed constant in `proposal_validation.ak` and `emergency.ak` | Fixed |
| M | **Oracle datum crash on CrossRefs**: `verify_oracle_signature` crashed parsing CrossRefs NFT datum as `GovernanceOracleDatum` when both sit at oracle holder address | Added `is_oracle_datum` field-count guard in `shared/oracle.ak` | Fixed |
| N | **Redeploy CrossRefs target**: `redeploy_proposal.py` minted CrossRefs NFT at wallet instead of oracle holder — smoke test found stale CrossRefs | Added `target_address=oracle_holder_addr` to redeploy script | Fixed |
| O | **Temporal units mismatch**: Bug F converted `submitted_at` to POSIX ms, but Bug G kept `review_window` in raw slots/seconds. On-chain expire check `current_slot > submitted_at + review_window` added ~604,800 to ~1.77 trillion ms — effective review window was ~10 minutes instead of ~7 days. All temporal GovernanceParams fields affected: `min_review_window`, `max_review_window`, `proposal_cooldown`, `emergency_review_window`, `param_execution_delay` | Converted all temporal GovernanceParams values and SDK defaults to POSIX ms. Updated `datums.py`, `client.py`, `params.ak`, and all test fixtures. Redeployed GovernanceParams UTXO with corrected values. | Fixed |
| P | **Dashboard proposer_did hex→bytes**: Indexer returned `proposer_did` as hex string; SDK's `_activity_token_name()` encoded hex chars as UTF-8, producing wrong token name → `InputUTxODepletedException` during coin selection | Added `_ensure_did_bytes()` in `server.py` to convert hex strings from indexer back to raw bytes before passing to `GovernanceClient` | Fixed |
| Q | **Orphaned tokenless proposals**: Proposals created via simple `submit_proposal()` (smoke test Phase 1) had no `prop_*` token. Dashboard showed them; clicking expire failed with "No proposal token found". These UTxOs are permanently locked (no validator recovery path). | Added `has_proposal_token` field to `GovernanceIndexer`; dashboard filters out tokenless UTxOs from all views | Fixed |
| R | **Activity UTxO count=0**: `_find_activity_utxo()` picked the first `pact_` UTxO it found, which could have `count=0` (already decremented from prior expire). Expire tried to decrement to -1, failing validator's `active_proposal_count >= 0` check | Skip activity UTxOs with `count < 1` in `_find_activity_utxo()` | Fixed |

### Smoke Test Results (2026-04-06)

Tests 1-8: **PASS** (8/8) — full re-run with temporarily lowered cooldown (1s), then restored to 24h
Test 9: **PASS** — `test_expire_e2e.py` full lifecycle: submit → wait → expire (tx `f4acccac91edd7dc...`)

All 9/9 tests pass.

### Key Debug Technique

```bash
aiken build --trace-level verbose --trace-filter user-defined
```

This includes only explicit `fail @"..."` and `trace @"..."` statements (9KB vs 13KB with `--trace-level verbose` alone). Keeps validators small enough to embed in 16KB transactions.

---

## Monitoring & Alerting (spec 19b.3 — not started)

- [ ] Treasury balance alert (< 2,500 AP3X)
- [ ] Oracle disabled alert
- [ ] High-quality proposal expiry warning
- [ ] Emergency proposal notification
- [ ] Low engagement warning (no proposals in 7 days)
- [ ] Low adoption rate warning (< 10% over 30 days)
- [ ] Daily governance health digest
