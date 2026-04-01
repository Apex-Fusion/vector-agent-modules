# Deployment — Game 1: Adversarial Auditing

**Network:** Vector Testnet (Cardano eUTxO L2, magic: 764824073)  
**Language:** Aiken (Plutus V3)  
**Final Version:** v10.6

---

## Contract Hashes

| Validator | Script Hash |
|-----------|-------------|
| challenge | `781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc` |
| claim | `6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda` |
| jury_pool | `b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962` |

## Testnet Addresses

| Validator | Address |
|-----------|---------|
| challenge | `addr1w9upssmgrpvme2athy9zyzkcgczvkvj2aar40349c34fdlqvc4dzd` |
| claim | `addr1w95gf47gdgrkrk52v8n20g6xr9a299yla7qrpgltsj2ymks92jxwq` |
| jury_pool | `addr1wxc44uy39pzhuzdj83u3rx4qery96fwflktx2mexz87ujcsxgtf0q` |

## Reference Script UTxOs

All three validators are deployed as reference scripts (CIP-33):

| Component | UTxO Reference |
|-----------|---------------|
| Challenge reference script | `20f4d1f62dd2247b8091485d84f949c019bc95ee415caa0953bcdbbd33c07301#0` |
| Claim reference script | `540fc16f66ce4f4186e33fc298f22a6e6787ebf4562b0c34a02260e7263d392e#0` |
| Jury Pool reference script | `92eb3826f2a95b606534c77d55ed493ea5401b041b1fbc06c45ff2007580d5d1#0` |
| Cross-validator references | `42856795e208ae815ef033e2c526af05267b8d59a21e1339b9cd766c4b458412#0` |
| Protocol parameters | `42856795e208ae815ef033e2c526af05267b8d59a21e1339b9cd766c4b458412#1` |

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
| v10.6 | 2026-03-31 | **Final** — all oracle removals, PRNG jury selection, min pool size, cleanup buffer |

## Lifecycle Validation

All 13 lifecycle steps confirmed passing on Vector testnet (v10.6):

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

The compiled Plutus V3 blueprint is available at [`plutus.json`](plutus.json). This contains all three validators pre-compiled and ready for deployment.
