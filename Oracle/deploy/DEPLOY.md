# Deployment — Oracle: Governance Suggestion Engine

**Network:** Vector Testnet (Cardano eUTxO L2, magic: 764824073)  
**Language:** Aiken (Plutus V3)  
**Date:** 2026-04-03 (initial), 2026-04-06 (GovernanceParams updated)

---

## Contract Hashes

### Governance Validators

| Validator | Script Hash |
|-----------|-------------|
| proposal_spend | `a74fc555e9b045695be1a26bdc9131681efa6b61738413ab9b2c7ea4` |
| proposal_mint | `e8f38052352a3d20c5fe025e2a02d615826a154b26f2239286b8d565` |
| critique_spend | `ced52074861af95e2082004d6061b0fc4bb30fded61f9605bfc20e55` |
| critique_mint | `2e252a89894d379ce5c0023a57de4627056e4a96da72bd8fedba04bd` |
| endorsement_spend | `5fc449848d85f30287e5bc0bd2b3e95d872ef97be27f1480c12f1a9d` |

### Infrastructure Holders (always-succeeds, parameterized by tag)

| Holder | Script Hash | Address |
|--------|-------------|---------|
| params (tag=1) | `f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867` | `addr1w8uc78dvuxkgq4s4enqr276wevmr5saegl7fnudxvxzssecjx7n6t` |
| oracle (tag=2) | `0611cf74214ef8fdd88a9e521cd9dae2a5269b4d1f0beb8d0133763e` | `addr1wyrprnm5y9803lwc3209y8xemt322f5mf50sh6udqyehv0s9kk34m` |
| treasury (tag=3) | `ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6` | `addr1wx434t2jc3m5uhdf7tq05xjdqu3q5z7a2lhrmn5mapsd43srh7ll8` |

## Testnet Addresses

| Validator | Address |
|-----------|---------|
| proposal_spend | `addr1wxn5l324axcy262mux3xhhy3x95pa7ntv9ecgyatnvk8afq0g8924` |
| critique_spend | `addr1w8mg4qa5y34u7hzsgzqp2cxrvlpuhvclh4np0ksh0ucsw24cqprgx` |
| endorsement_spend | `addr1w9073fyfr2cpusg0090ztjk0etd8rwh0904yl9yqvzhxd5ag6lq57` |

## Reference Script UTxOs (CIP-33)

| Component | UTxO Reference |
|-----------|---------------|
| Proposal mint ref | `e82c188244cba737312119cc93efcb88544b3f7a12e94adad5f1360043afc3bd#0` |
| Proposal spend ref | `4c7de3a2ccc8b46a5929523f410d457d2ba322b1a0a0f46764d441a6185f05df#0` |
| Critique mint ref | `b7d4861dc45946dbd9d4ec230066ed6416740ec7996ce5834a8222e75364773a#0` |
| Critique spend ref | `c269404c217d5b467b9299323eb99e075c8ef4800032fe5bd14467109c247d62#0` |
| Endorsement spend ref | `2cb511c36a2714f989805aae9abe1686b2b7ca5741c3084ef3abc1ae3a4e0ea7#0` |
| Holder params ref | `9743c6b6939f94e27271259388d18aa39be75174800b8f1307dc7752b78ecfab#0` |
| Holder oracle ref | `cf2792f1bc9458108e0a2a5770b3800abca6dfe820473cbd5e61116200323024#0` |
| Holder treasury ref | `c58cb241b91132b7f2345a748039cd3a6670d31139feaa734716561028d37881#0` |

## Infrastructure UTxOs

| Component | UTxO Reference |
|-----------|---------------|
| GovernanceParams datum | `bbe1aedc7b1978daf6065819c4f8a4b84d058fb705da2b6696f2ca0286adaff0#0` |
| GovernanceOracle datum | `a5b71cea177c2b9589877c0de8ead7daa3bd4d0b5b951a1ded7845961fcf2213#0` |
| CrossRefs NFT | `b0ae0684bad3db716d1dabe4d16f1aa1f2af0a31079eb2efc9e1efa59b6f2dfb#0` |
| Treasury batch 1 | `0f5d7624dc283ef8ef60949de11a8c7532a712e4ae0091030981281c0be3b1fb#0` |
| Treasury batch 2 | `35971a04b487404059022747b54629d7118bdcaa8680589ec8f7f8bf7abc9e7c#0` |
| Treasury batch 3 | `3c49c9bbf3f92eff0c794f7ca9ab925aaab67a3a9b062d2ad96327f32791afcb#0` |

## Token Policies

| Token | Policy ID |
|-------|-----------|
| CrossRefs NFT | `96ab6199348249e31bbf6335d50f494eaabe9f142043a8bdcb3c9dba` |
| Proposal tokens | `e8f38052352a3d20c5fe025e2a02d615826a154b26f2239286b8d565` (= proposal_mint hash) |
| Critique tokens | `2e252a89894d379ce5c0023a57de4627056e4a96da72bd8fedba04bd` (= critique_mint hash) |

## External Dependencies

| Component | Hash / Identifier |
|-----------|-------------------|
| Agent Registry | `be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01` |

## GovernanceParams (on-chain values)

All temporal values are in POSIX milliseconds (matching on-chain `validity_range` lower bound).

| Parameter | Value | Human |
|-----------|-------|-------|
| min_proposal_stake | 25,000,000 | 25 AP3X |
| min_critique_stake | 5,000,000 | 5 AP3X |
| min_governance_endorsement | 10,000,000 | 10 AP3X |
| min_review_window | 259,200,000 | ~3 days |
| max_review_window | 2,419,200,000 | ~28 days |
| max_amendments | 5 | |
| max_active_proposals | 3 | per agent |
| proposal_cooldown | 86,400,000 | ~24 hours |
| proposer_reward_share | 7,000 | 70% (basis points) |
| critic_reward_share | 2,000 | 20% (basis points) |
| protocol_fee_rate | 1,000 | 10% (basis points) |
| min_adoption_reward | 50,000,000 | 50 AP3X |
| max_adoption_reward | 500,000,000 | 500 AP3X |
| max_incorporated_critiques | 10 | |
| max_treasury_request | 10,000,000,000 | 10,000 AP3X |
| emergency_stake_multiplier | 5,000 | 5x (basis points) |
| emergency_review_window | 43,200,000 | ~12 hours |

## Lifecycle Validation

All 9 smoke test steps confirmed passing on Vector testnet (2026-04-06):

| Step | Status |
|------|--------|
| 1. Query wallet balance | PASS |
| 2. Submit proposal (lock) | PASS |
| 3. Submit critique (lock) | PASS |
| 4. Endorse proposal (lock) | PASS |
| 5. Withdraw endorsement (spend) | PASS |
| 6. Validated submit proposal (mint+spend) | PASS |
| 7. Validated withdraw proposal (burn+spend) | PASS |
| 8. Validated adopt proposal (oracle+burn+spend) | PASS |
| 9. Expire proposal (permissionless, time-gated) | PASS |

Full deployment data: [`deployment.json`](deployment.json)  
Full lifecycle results: [`lifecycle-results.json`](lifecycle-results.json)

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| v1 | 2026-04-01 | Initial types + proposal validation |
| v2 | 2026-04-02 | Critique & endorsement validators, CrossRefs pattern |
| v3 | 2026-04-03 | Full deployment — 14 TXs, CrossRefs at oracle holder, treasury batches |
| v4 | 2026-04-03 | Bug fixes C-G: token names, activity tracking, slot/POSIX conversion |
| v5 | 2026-04-04 | Bug fixes H-N: CBOR encoding, activity UTxO crash, Plutus tags, oracle datum guard |
| v6 | 2026-04-06 | Bug O: temporal units fix (slots -> POSIX ms). All 9/9 tests pass. |

## Bug Summary

15 bugs found and fixed during testnet deployment. See [`../docs/progress.md`](../docs/progress.md) for full details.

Key categories:
- **CBOR encoding** (H, J): Aiken vs Python serialization differences
- **Token operations** (C, D): wrong input refs, wrong policy hashes
- **Time handling** (E, F, G, O): slot vs POSIX ms conversions
- **Datum parsing** (I, M): missing pattern matches for multi-type UTxOs
- **Deployment** (B, N): CrossRefs NFT placement

## Conway CBOR Note

Vector testnet uses Conway-era CBOR encoding. Aiken's `plutus.json` output uses definite-length encoding, but the node may expect indefinite-length encoding for certain structures. If deployment fails with `DecoderErrorDeserialiseFailure`, check CBOR encoding.

## Compiled Blueprint

The compiled Plutus V3 blueprint is available at [`plutus.json`](plutus.json). This contains all validators pre-compiled and ready for deployment.
