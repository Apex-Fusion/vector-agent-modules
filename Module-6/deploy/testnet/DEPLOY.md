# Deployment — Module 6: Self-Improvement Module

**Network:** Vector Testnet (Cardano eUTxO L2, magic: 764824073)  
**Language:** Aiken (Plutus V3)  
**Date:** 2026-04-03 (initial), 2026-04-09 (v7 redeployment with correct agent registry), 2026-04-15 (v8 redeployment against agent-registry v2 `be1a0a29…`)

---

## Contract Hashes

### Governance Validators

| Validator | Script Hash |
|-----------|-------------|
| proposal_spend | `f815f51a76002d6a973e83fecf60f45473e040acee85c631fcce134d` |
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
| proposal_spend | `addr1w8uptag6wcqz665h86planmq73288czq4nhgt333ln8pxngfzg955` |
| critique_spend | `addr1w88d2gr5scd0jh3qsgqy6crpkr7yhvc0mmtpl9s9hlpqu4gra6s0g` |
| endorsement_spend | `addr1w90ugjvy3kzlxq58uk7qh54na9wcwthe003879yqcyh348gvfdvx4` |

## Reference Script UTxOs (CIP-33)

| Component | UTxO Reference |
|-----------|---------------|
| Proposal mint ref | `f87a1c7289a52c493193195771c0fdcfcedb7d1d9d0e681c5e4927a902ee02d6#0` |
| Proposal spend ref | `f3d41f34035676e3c9bd76ac6e55bc96e3559d065dc7e9855a183415b60929ba#0` |
| Critique mint ref | `163c797f3d51ae431617d5ad9cc0bc6b461f7e89810bf57fea6f8539e819dfe3#0` |
| Critique spend ref | `7e55f8f07dfedbcfa59408c9128253e4217593b8a63484a5f3c139824103eb7e#0` |
| Endorsement spend ref | `a1ca4b35e0c675f12785e69741fc2425068add806a13028dd8a065bdf67b00e5#0` |
| Holder params ref | `b3d3b7fd29da9c1c032b5c51806a8782a251a35622c3eb13b33b198f6e23af18#0` |
| Holder oracle ref | `b98e42ad638e18f4a247076d45051920089b38675260a4d329b7f79bf6d8730f#0` |
| Holder treasury ref | `aa4b4b5558dc4307474ef48d1b5986dee486f23ca86c05b39159b5c3f393957c#0` |

## Infrastructure UTxOs

| Component | UTxO Reference |
|-----------|---------------|
| GovernanceParams datum | `2c082e833649175b4a543a5a0cf61f9b736acdfa0d315d1184645185e9a52796#0` |
| GovernanceOracle datum | `7a23dfdf9468dd35cee3cad03008f2538c86834d4e5140e0ffaf2ff93e7c04a7#0` |
| CrossRefs NFT | `96a4acff8be0fb96b3839ee6c9c1fa75809b94f4967218eaf813ac56b939c4b2#0` |
| Treasury batch 1 | `6193e3a74b595ee9e5d8a4382e67de83a2ded2cc9f04fd58f16ca47e13769cea#0` |
| Treasury batch 2 | `9f18bb40d269507f048649e92ded1cda8facc3aadfc24d7486653c8e85590dcd#0` |
| Treasury batch 3 | `10b0884a628eb4ed7b05cdffa07e301c05fa2aead5dd887391bb08660fb15671#0` |

## Token Policies

| Token | Policy ID |
|-------|-----------|
| CrossRefs NFT | `96ab6199348249e31bbf6335d50f494eaabe9f142043a8bdcb3c9dba` |
| Proposal tokens | `e8f38052352a3d20c5fe025e2a02d615826a154b26f2239286b8d565` (= proposal_mint hash) |
| Critique tokens | `2e252a89894d379ce5c0023a57de4627056e4a96da72bd8fedba04bd` (= critique_mint hash) |

## External Dependencies

| Component | Hash / Identifier |
|-----------|-------------------|
| Agent Registry (v2, audited) | `be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01` |
| Agent Registry script address | `addr1wxlp5z3fztdpsp6ha57dvx6khw82kqvgcxwu8s8rjykjcqghprf42` |

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
| v8 | 2026-04-15 | Migrated to **agent-registry v2** (`be1a0a29…`, audited / Conway-CBOR-fixed). All parameterized validators recompiled and redeployed. Reference scripts redeployed to unspendable script-hash-derived addresses (all 5 governance validators). Dashboard leaderboard updated to show all roles (proposers, critics, endorsers). mcp-server `governance.ts` env defaults updated in `Apex-Fusion/mcp-server@e4b2697`. Smoke test 8/8 passing on testnet. |

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
