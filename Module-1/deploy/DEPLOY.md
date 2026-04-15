# Deployment — Module 1: Adversarial Auditing

**Network:** Vector Testnet (Cardano eUTxO L2, magic: 764824073)  
**Language:** Aiken (Plutus V3)  
**Current Version:** v12

---

## Contract Hashes

| Validator | Script Hash |
|-----------|-------------|
| challenge | `e93ec8e10ae9180564f6acb98130a37425974c83204b7309bd5d572e` |
| claim | `6f02f3191bf806386ba1141192ac80838cd27deb0db68214de8d32e5` |
| jury_pool | `37e93880f270e784e675dda8cbfb315607b99431b9a8548323a2b0ec` |

> Note: hashes reflect the compiled output including trace instrumentation (`--trace-level verbose`). A production build without traces will produce different jury_pool hash. The on-chain reference scripts were deployed with tracing enabled.

## Testnet Addresses

| Validator | Address |
|-----------|---------|
| challenge | `addr1w85naj8ppt53spty76ktnqfs5d6zt96vsvsykucfh4w4wtsq0ct3g` |
| claim | `addr1w9hs9ucer0uqvwrt5y2pry4vszpce5naavxmdqs5m6xn9eg9q29cs` |
| jury_pool | `addr1wym7jwyq7fcw0p8xwhw63jlmx9tq0wv5xxu6s4yryw3tpmqrephmy` |

## Reference Script UTxOs

All three validators are deployed as reference scripts (CIP-33):

| Component | UTxO Reference |
|-----------|---------------|
| Challenge reference script | `73404929fb14633751123d85c3dc67d82269a8aebb2f49d38af68d5c19e59af1#0` |
| Claim reference script | `8eafb8891572f95ce84c77b5d44a660a9a48ddbf8f372e056f0a402defbb523b#0` |
| Jury Pool reference script | `962f5609f3ac90855dd79e5328d2b5d60bd97410ac24aa868a57464ac811a339#0` |
| Cross-validator references | `73a8f17d1e5cb8a3a5fb0b00ed585e5203da0a5d130dc36b55bddb0f96ad9d10#0` |
| Protocol parameters | `73a8f17d1e5cb8a3a5fb0b00ed585e5203da0a5d130dc36b55bddb0f96ad9d10#1` |

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

## On-chain Lifecycle Verification (v12)

The following redeemer paths were exercised on-chain against the v12 reference scripts:

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

### Coverage Note

Normal jury-verdict resolution path (RevealVote → ResolveJury → DistributeRewards → CleanupResolved) is validated exhaustively in the Aiken test suite (226/226 tests green, including 4 Transaction-level e2e tests for ResetStaleActiveCase). On-chain v12 exercise focused on the escape-hatch path (TimeoutResolve + ResetStaleActiveCase), which is the new logic introduced in this version. Unchanged paths were exercised on-chain in prior v11 deploy.

## Lifecycle Validation

All 13 lifecycle steps confirmed passing on Vector testnet (v10.6 code base, v11 deploy):

| Step | Status |
|------|--------|
| Phase 0 — Apply parameters | ✅ SUCCESS |
| Phase 1 — Deploy reference scripts | ✅ SUCCESS |
| Step 1 — RegisterAgent ×15 | ✅ SUCCESS |
| Step 2 — RegisterJuror ×5 | ✅ SUCCESS |
| Step 3 — SubmitClaim | ✅ SUCCESS |
| Step 4 — OpenChallenge | ✅ SUCCESS |
| Step 5a — TransitionToVoting | ✅ SUCCESS |
| Step 5b — SelectJury | ✅ SUCCESS |
| Step 6a — CommitVotes ×5 | ✅ SUCCESS |
| Step 6b — RevealVotes ×5 | ✅ SUCCESS |
| Step 7 — ResolveJury | ✅ SUCCESS |
| Step 8 — DistributeRewards | ✅ SUCCESS |
| Step 9 — CleanupResolved | ✅ SUCCESS |

Full deployment data: [`deployment.json`](deployment.json)  
Full lifecycle results: [`lifecycle-results.json`](lifecycle-results.json)

## Conway CBOR Note

Vector testnet uses Conway-era CBOR encoding. Aiken's `plutus.json` output uses definite-length encoding, but the node may expect indefinite-length encoding for certain structures. If deployment fails with `DecoderErrorDeserialiseFailure`, check CBOR encoding — see the [Conway CBOR advisory](https://github.com/Apex-Fusion/vector-ai-agents/blob/main/smart-contract-audit/docs/testing-on-vector.md) in the smart-contract-audit docs.

## Compiled Blueprint

The compiled Plutus V3 blueprint is available at [`plutus.json`](plutus.json). This contains all three validators pre-compiled and ready for deployment. The blueprint was regenerated during v12 deploy and reflects the current on-chain reference scripts.
