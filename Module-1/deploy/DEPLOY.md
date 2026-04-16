# Deployment — Module 1: Adversarial Auditing

**Network:** Vector Mainnet (Cardano eUTxO L2)  
**Language:** Aiken (Plutus V3)  
**Current Version:** v14-mainnet (2026-04-16)

---

## Contract Hashes

| Validator | Script Hash |
|-----------|-------------|
| challenge | `12700f4aabdd63caab38adfb50455da54a4e4bc0402a4b1d5a90d1fb` |
| claim | `a9d22e8b01d282be8007b8d9e3e8af548aaa56f1c3e433c0eddd8760` |
| jury_pool | `2b01c6b3164237757fc82e64780c63ecfc1d5a733ce919a3e2e75f28` |

> Note: hashes reflect the compiled output with production params applied on-chain (Path B — base AP3X stakes held in `.coin` field). These are the post-param mainnet hashes and differ from both the v12 testnet hashes and the v13 testnet hashes. The on-chain reference scripts were deployed to Vector mainnet on 2026-04-16.

## Mainnet Addresses

| Validator | Address |
|-----------|---------|
| challenge | `addr1wyf8qr6240wk8j4t8zklk5z9tkj55njtcpqz5jcat2gdr7cazrd0t` |
| claim | `addr1wx5ayt5tq8fg905qq7udnclg4a2g42jk78p7gv7qahwcwcqjd9tzq` |
| jury_pool | `addr1wy4sr34nzeprwatleqhxg7qvv0k0c826wv7wjxdrutn472q7yn6fa` |

## Reference Script UTxOs

All three validators are deployed as reference scripts (CIP-33):

| Component | UTxO Reference |
|-----------|---------------|
| Challenge reference script | `ea4e8c4e5ef2a3bd315b5a08f7426a350c61afd78ace7ddbc9cfcf4f7fa53e83#0` |
| Claim reference script | `9b22d2ea4f423ab705f3f2132f34c791c42caabd5bcaf056f5f42bcf442b64b8#0` |
| Jury Pool reference script | `9895c24ea422243e7e36cf6a5b301c88b1d5cbb9268e63eee2305b97bfbc0fd2#0` |
| Cross-validator references | `5d5e193b9a1297f816b449db1cfe828eacaafce84b6066eb5da38476e53eaf5f#0` |
| Protocol parameters | `5d5e193b9a1297f816b449db1cfe828eacaafce84b6066eb5da38476e53eaf5f#1` |

## Production Parameters (baked on-chain)

| Parameter | Value |
|-----------|-------|
| min_claim_stake | 50 AP3X |
| min_juror_bond | 25 AP3X |
| jury_size | 5 |
| min_challenge_window | 30 min |
| max_challenge_window | 24 h |
| resolution_deadline | 72 h |
| commit_window | 30 min |
| reveal_window | 30 min |
| cleanup_buffer | 10 min |
| jury_fee_rate | 10% |
| juror_slash_rate | 10% |
| oracle_active | False (jury mode) |
| min_jury_pool_size | 15 |

> Stakes are held in the `.coin` field (base AP3X, the native chain currency in DFM units). This is Path B — no custom staking token is required.

## Refs NFT Policy

`205d5f77ffebf60b764ba4f1873eff3764f3d1d594e5dac477a928f9` (native script)

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| v1 | 2026-03-23 | Initial implementation — Foundation oracle mode |
| v2 | 2026-03-23 | Parameterized scripts, real AP3X token integration |
| v3 | 2026-03-27 | Added TransitionToVoting action (lifecycle gap fix) |
| v4 | 2026-03-27 | Time unit fix (seconds→ms), CrossRefs auth, token name fix |
| v10 | 2026-03-29 | DistributeRewards via Option A, refs_token collision fix |
| v10.1 | 2026-03-30 | ForfeitClaim Resolved state verification |
| v10.2 | 2026-03-30 | Red team fixes: fake Resolved output, vote authentication |
| v10.3 | 2026-03-31 | Phase 1.1: commit-reveal voting, permissionless ResolveJury |
| v10.6 | 2026-03-31 | All oracle removals, PRNG jury selection, min pool size, cleanup buffer |
| v11 | 2026-04-14 | ResetStaleActiveCase escape hatch; jury_pool rehashed |
| v12 | 2026-04-15 | Redeployed from post-extraction source (`reset.ak` wrapper); same semantics, new hashes. Escape hatch (TimeoutResolve + ResetStaleActiveCase) exercised on-chain. |
| v13 | 2026-04-16 (testnet) | Path B (base AP3X stakes); full normal jury-verdict lifecycle verified on Vector testnet |
| v14 | 2026-04-16 (MAINNET) | Mainnet deploy; Path B base AP3X stakes; deployed to Vector mainnet with same params as v13 testnet validation |

## On-chain Lifecycle Verification (v12 — Escape-Hatch Path)

The following redeemer paths were exercised on-chain against the v12 reference scripts. This focused on the escape-hatch path introduced in v12:

| Path | TX Hash |
|------|---------|
| OpenChallenge | `bab0a988c54725ca66dc4136a6cb0c75946cec5d38a53f519dfdb038862324c2` |
| TransitionToVoting | `02968a1ac1dcfd748d8865556b25087d81877d2a70a0b496ea539786b29568fe` |
| SelectJury | `1d4cb7ce97f8647286374a5ccd2cd32956f3890732fa6654f097c8f3c114688c` |
| CommitVote 1/5 | `b728aecba5f4b7b47874e91be1478631c1180f2ef18ea3780cc45de0270b25ec` |
| CommitVote 2/5 | `400b3f0999af9c9d40cfe2adf89029af83464bb5f72a611017895ba4155e579c` |
| CommitVote 3/5 | `afb20ef6a7feb7be16c85b712a315f19365cd3b78e93ee5f7ab1cf38ec8bc7ef` |
| CommitVote 4/5 | `741dbd206f04a867a9437606283eda30cc15c498b8918eef8c337bdec0d39007` |
| TimeoutResolve | `110fd711693b87ed41715054f6be631b2b00fcf4253e01779f467ae51b88d894` |
| ResetStaleActiveCase (juror 4d908ec1) | `421dd8a939f6f42400040d7b461b6c7e46e4b5ab5339dcbea1c34cc7dd468976` |
| ResetStaleActiveCase (juror 5ace2021) | `06292ad58fe0663837bd5946079359ef4cf3593e067dd349f3cc566d1309c302` |
| ResetStaleActiveCase (juror a58b6fbd) | `2ba46e0c898710c48442f03a9b6dff8edbf903ddbb769284ae3bf395d11630ff` |
| ResetStaleActiveCase (juror 5b76a067) | `63e9c74c1e6bac3e83a6cfa2ef7f1427625e4541c0fb87df923df81f7aeccffa` |
| ResetStaleActiveCase (juror 61a0d910) | `aac89ffcc1ea308d29e6d543e49fc61af91da853dfae033e9173b17423a848af` |

TimeoutResolve burned challenge_token `63686c5fbbe79245fcab5c8188558a5b3b6dfd42123ffe7615f93e29d4e8f6e2` and claim_token `636c6d5fce9fb78aae703a3f856fa95600533a9c7c00e72b479ee163a6b97054`; refunded 50M AP3X to claimer and 50M AP3X to auditor. All 5 jurors verified on-chain: `active_case=None`. Total ADA cost for Phase 3: ~3.6 ADA.

### v13 Testnet Full-Lifecycle Verification (Path B — Normal Jury-Verdict Path)

The complete 13-step normal jury-verdict lifecycle was exercised on-chain against the v13 testnet reference scripts using Path B (base AP3X stakes). All steps passed:

| Step | TX Hash |
|------|---------|
| Phase 0 — Apply parameters | `1c3e70a12646ed9bebd03ac1ee482a458dd4357aecfd5c0318c0ec1d68f34700` (params/cross_refs) |
| Phase 1 — Deploy reference scripts | challenge: `1018b770b78aa369bad55292d6709fd5800db29875fa19faecd38c4f2e2e7554`, claim: `b20f03ca7430e5f63ebc683acc1d7d610c79e2e9f27477b97764d42ede7a7e9c`, jury_pool: `26fa176ab7716ffa63b7eb61edaf1bf0fa37952b7264c8ef822668d08b610b41` |
| Step 1 — RegisterAgent ×15 | 15 DIDs registered (see `game1-v13-smoke-results.json`) |
| Step 2 — RegisterJuror ×15 | 15 jurors bonded |
| Step 3 — SubmitClaim | `0653bb44b030bdf1d2b76fab1e1f18d24e39f9ea3d3312948cf43696c9627a48` |
| Step 4 — OpenChallenge | `537c1105778003242b1bff65b97bc654f8060743bc3be3a0e6052eb735efca09` |
| Step 5a — TransitionToVoting | `d54726b03efefb11ac800f41bf209e94c3c9eb5aa49f9e29295ce544f4203c93` |
| Step 5b — SelectJury | `7f5a07118309e1828d40f9a62160c79523dc3ebc3bfff23652502aaab6766e21` |
| Step 6a — CommitVotes ×5 | 3× ClaimerWins, 2× AuditorWins (see smoke results) |
| Step 6b — RevealVotes ×5 | All 5 revealed |
| Step 7 — ResolveJury | `bc39cf9c3e13f9eccc82a61236c08bcf4e80e3b75c7aa998edaf1c04bf8b2c15` (ClaimerWins, 95M AP3X to claimer, 5M AP3X jury fee) |
| Step 8 — DistributeRewards | 1M AP3X per majority juror |
| Step 9 — CleanupResolved | `098918ad29136780df4e15138ac47ff0de4c9def23aea96b74d4c48c3c37552a` |

All 13 steps green. v13 testnet run validated Path B contract semantics end-to-end before mainnet deploy.

### Mainnet Deploy Scope (v14)

Mainnet deploy executed **Phase 0 + Phase 1 only** (parameter application and reference script deployment). No smoke lifecycle was run on real AP3X. Mainnet infrastructure is live but unseeded — awaits user-driven juror registration (minimum 15 jurors required before disputes can be opened).

### Coverage Note

Path B contract semantics are validated by two independent lines of evidence:
1. **Aiken unit tests** — 232/232 passing, including comprehensive path_b_tests coverage
2. **Full-lifecycle on-chain run on v13 testnet** — all 13 steps green on Vector testnet

Mainnet infrastructure is live. Unseeded — no active jurors or claims yet. The normal jury-verdict path was exhaustively validated before mainnet deploy; the escape-hatch path (TimeoutResolve + ResetStaleActiveCase) was validated on-chain during v12.

## Lifecycle Validation

All 13 lifecycle steps confirmed passing on Vector testnet (v13 testnet, Path B):

| Step | Status |
|------|--------|
| Phase 0 — Apply parameters | SUCCESS |
| Phase 1 — Deploy reference scripts | SUCCESS |
| Step 1 — RegisterAgent ×15 | SUCCESS |
| Step 2 — RegisterJuror ×15 | SUCCESS |
| Step 3 — SubmitClaim | SUCCESS |
| Step 4 — OpenChallenge | SUCCESS |
| Step 5a — TransitionToVoting | SUCCESS |
| Step 5b — SelectJury | SUCCESS |
| Step 6a — CommitVotes ×5 | SUCCESS |
| Step 6b — RevealVotes ×5 | SUCCESS |
| Step 7 — ResolveJury | SUCCESS |
| Step 8 — DistributeRewards | SUCCESS |
| Step 9 — CleanupResolved | SUCCESS |

Full deployment data: [`deployment.json`](deployment.json)  
Full v13 lifecycle results: available in the testnet workspace (not included in this repository).

## Conway CBOR Note

Vector uses Conway-era CBOR encoding. Aiken's `plutus.json` output uses definite-length encoding, but the node may expect indefinite-length encoding for certain structures. If deployment fails with `DecoderErrorDeserialiseFailure`, check CBOR encoding — see the [Conway CBOR advisory](https://github.com/Apex-Fusion/vector-ai-agents/blob/main/smart-contract-audit/docs/testing-on-vector.md) in the smart-contract-audit docs.

## Compiled Blueprint

The compiled Plutus V3 blueprint is available at [`plutus.json`](plutus.json). This contains all three validators pre-compiled and ready for deployment. The blueprint reflects the v14 mainnet contract build.
