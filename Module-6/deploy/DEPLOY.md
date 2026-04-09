# Deployment — Module 6: Governance Suggestion Engine

**Network:** Vector Testnet (Cardano eUTxO L2, magic: 764824073)  
**Language:** Aiken (Plutus V3)  
**Date:** 2026-04-03 (initial), 2026-04-09 (v7 redeployment with correct agent registry)

---

## Contract Hashes

### Governance Validators

| Validator | Script Hash |
|-----------|-------------|
| proposal_spend | `40fe1895df7bfd4a732cecd3c6f56b942fd36690c0cff9358dc8a0f8` |
| proposal_mint | `10dff07bb98b5c88b488522c0b7d8bf9ad335907cb20a479ba3b3166` |
| critique_spend | `9e9aaf7ea0e03695fbe1bf60429e2a715cbc40da82b17f8a52dedeb1` |
| critique_mint | `1f5614b709a30e35034666dbe13599786d39b3db24471b88c468c74c` |
| endorsement_spend | `1fac8b35509d379c304fcafdf12b8ed0845af5543dd5a6490fb75b7b` |

### Infrastructure Holders (always-succeeds, parameterized by tag)

| Holder | Script Hash | Address |
|--------|-------------|---------|
| params (tag=1) | `f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867` | `addr1w8uc78dvuxkgq4s4enqr276wevmr5saegl7fnudxvxzssecjx7n6t` |
| oracle (tag=2) | `0611cf74214ef8fdd88a9e521cd9dae2a5269b4d1f0beb8d0133763e` | `addr1wyrprnm5y9803lwc3209y8xemt322f5mf50sh6udqyehv0s9kk34m` |
| treasury (tag=3) | `ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6` | `addr1wx434t2jc3m5uhdf7tq05xjdqu3q5z7a2lhrmn5mapsd43srh7ll8` |

## Testnet Addresses

| Validator | Address |
|-----------|---------|
| proposal_spend | `addr1w9q0uxy4maal6jnn9nkd83h4dw2zl5mxjrqvl7f43hy2p7q0cdmzy` |
| critique_spend | `addr1wx0f4tm75rsrd90muxlkqs579fc4e0zqm2ptzlu22t0davghd6l2a` |
| endorsement_spend | `addr1wy06eze42zwn08psfl90muft3mgggkh42s7atfjfp7m4k7c8j8zpc` |

## Reference Script UTxOs (CIP-33)

| Component | UTxO Reference |
|-----------|---------------|
| Proposal mint ref | `3cb52ec82479c398d96b06aa82f2a85d5ecfc128f5010cfb70cd9b276d75cb33#0` |
| Proposal spend ref | `f0d528777d3910ec15b0d538b60015ce07e62126a5f90205eb9032cdf25190f9#0` |
| Critique mint ref | `1b4c0d8b5bb053d4c3d6f88be2c970a65eb9a2a1be25aff4dc82e82bc832ab50#0` |
| Critique spend ref | `903b43892d4aa11b4c8099b64a0bd4570a195bae8aeb5aa1c5f1e30d6f038c55#0` |
| Endorsement spend ref | `46b15fa6a2d978e406c5f67a23ec84128af91addc87e8d437b8ab301ccd333d7#0` |
| Holder params ref | `b3d3b7fd29da9c1c032b5c51806a8782a251a35622c3eb13b33b198f6e23af18#0` |
| Holder oracle ref | `b98e42ad638e18f4a247076d45051920089b38675260a4d329b7f79bf6d8730f#0` |
| Holder treasury ref | `aa4b4b5558dc4307474ef48d1b5986dee486f23ca86c05b39159b5c3f393957c#0` |

## Infrastructure UTxOs

| Component | UTxO Reference |
|-----------|---------------|
| GovernanceParams datum | `47d17de567810f44a7608935bc9c2be7bccaee0336f7a312786fb8bbcb1b4de9#0` |
| GovernanceOracle datum | `3e0685b959805ad41f94504c929518a04b35f475bdd6f29b9f983e55f467e590#0` |
| CrossRefs NFT | `71815087d85ed2f2554eb222cbdfb96e8fc96049c7d9f79a42a86fc8cb12b69e#0` |
| Treasury batch 1 | `a49b808431e0c23e2f0a7c49aa958e9d7e5c1be5c0811d4ded81ccc3f02155c3#0` |
| Treasury batch 2 | `6ff42562596122b120e771d10b4714711ed32c99036a36e5632635cf7eaac437#0` |
| Treasury batch 3 | `6fcad23cbb66e79c64ad9e4a88b45990b2c30ed0190c09083bff1348bf56d271#0` |

## Token Policies

| Token | Policy ID |
|-------|-----------|
| CrossRefs NFT | `96ab6199348249e31bbf6335d50f494eaabe9f142043a8bdcb3c9dba` |
| Proposal tokens | `10dff07bb98b5c88b488522c0b7d8bf9ad335907cb20a479ba3b3166` (= proposal_mint hash) |
| Critique tokens | `1f5614b709a30e35034666dbe13599786d39b3db24471b88c468c74c` (= critique_mint hash) |

## External Dependencies

| Component | Hash / Identifier |
|-----------|-------------------|
| Agent Registry | `5dd5118943d5aa7329696181252a6565a27dbf2c6de92b02a6aae361` |

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
| v7 | 2026-04-09 | Redeployment with correct agent registry hash (5dd51189...). Full contract redeploy + MCP server fixes. |

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
