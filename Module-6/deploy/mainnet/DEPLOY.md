# Deployment — Module 6: Self-Improvement Module

**Network:** Vector Mainnet
**Language:** Aiken (Plutus V3)
**Date:** 2026-04-15 (v8, against agent-registry v2 `be1a0a29…`)

---

## Contract Hashes

### Governance Validators

| Validator | Script Hash |
|-----------|-------------|
| proposal_spend | `98b610c59597e9046dbede8d38d6f9c2c6635167ddcdcb874d39d589` |
| proposal_mint | `fdcefb68c765c4e4c1483baa01b6e9624c870d9d56380f7c2dfb65cc` |
| critique_spend | `51d852464933e2b7c83fbed6f2818feec5ebd6e542b4b10404ea30ea` |
| critique_mint | `b4562214183267db848af597672061a42e149e14f0e989db4d8b6296` |
| endorsement_spend | `d710216bbb422993aea316db9fcbfe6c2451341b71d629e8bb93e0ee` |

### Infrastructure Holders (always-succeeds, parameterized by tag)

| Holder | Script Hash | Address |
|--------|-------------|---------|
| params (tag=1) | `f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867` | `addr1w8uc78dvuxkgq4s4enqr276wevmr5saegl7fnudxvxzssecjx7n6t` |
| oracle (tag=2) | `0611cf74214ef8fdd88a9e521cd9dae2a5269b4d1f0beb8d0133763e` | `addr1wyrprnm5y9803lwc3209y8xemt322f5mf50sh6udqyehv0s9kk34m` |
| treasury (tag=3) | `ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6` | `addr1wx434t2jc3m5uhdf7tq05xjdqu3q5z7a2lhrmn5mapsd43srh7ll8` |

## Validator Addresses

| Validator | Address |
|-----------|---------|
| proposal_spend | `addr1wxvtvyx9jkt7jprdhm0g6wxkl8pvvc63vlwumju8f5uatzgnjjjcr` |
| critique_spend | `addr1w9gas5jxfye79d7g87lddu5p3lhvt67ku4ptfvgyqn4rp6stn80z3` |
| endorsement_spend | `addr1w8t3qgtthdpznyaw5vtdh87tlekzg5f5rdcav20ghwf7pms2mfltj` |

## Reference Script UTxOs

All reference scripts deployed to unspendable script-hash-derived addresses.

| Script | UTxO |
|--------|------|
| Holder params ref | `cd0af17af3df2fcf5c09bfcd04f4c0d9079f637879ff2dfbab435ae5d62062f3#0` |
| Holder oracle ref | `066c74de2691fd1b8799377a32cc98729fe91be6ac989f5c4c1ab000b3acd68a#0` |
| Holder treasury ref | `e09ba2619087792717e2e592bef3209331a5f62de95b8832f50ef04e5e36c474#0` |
| Proposal mint ref | `69fd3e0107964b6e7b396963c2106ffb3e9f777a4e0a1ade070bfdbcfa33d0e6#0` |
| Proposal spend ref | `e05eceb3ca1e5ccdee93c56ac9d6ed8b480f343bfc0032cfcfe39f3fd89e3269#0` |
| Critique mint ref | `482a723ff19ef119e30a335df208c04216e779c576e3921ca5208391a8b992c3#0` |
| Critique spend ref | `04958e69d714c83e8d3928ae6b731c21e7b732b0e309664d3a0e605dd656cf0b#0` |
| Endorsement spend ref | `0f2f3604fc85cb73d943eda8c0636eabdf8577ea8011acb6a46e452747430f12#0` |

## Infrastructure UTxOs

| UTxO | TX Hash |
|------|---------|
| GovernanceParams datum | `fc5c4ecde448123d604d034ded1ada696b3f3a082e6cd83899ee1e93eb8af6bf#0` |
| GovernanceOracle datum | `efebb237b84e27e1089a3ca58cf4f413e43ac04b7c882c30468a553c11a3e793#0` |
| GovernanceCrossRefs NFT | `72e29ff88f8fceca4182edba6d6b225a8377389ac7ba8148b68f0b03c36fe937#0` |
| Treasury batch 1 (30 AP3X) | `93ad5c224116d2dfc8fe47fe06b9be40dc5dcb40da89c92cafec3fd3e4ad73bb#0` |
| Treasury batch 2 (30 AP3X) | `4d73e88f1fa85d0f99b4e96ca0ed0390df9ecb963a276dd71897c6d44fe6c795#0` |
| Treasury batch 3 (30 AP3X) | `1aa5b4dbdfea93875d65f70468caec288571b95d25dd80a551225db92f471e15#0` |

## External Dependencies

| Dependency | Hash | Version |
|------------|------|---------|
| Agent Registry | `be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01` | v2-audited |

## Token Policies

| Token | Policy ID |
|-------|-----------|
| CrossRefs NFT | `c1a4a814834dfe46ef8c6aec4b298260cd5e8e2aff3bf6a1a2d5140d` |
| Proposal tokens | `fdcefb68c765c4e4c1483baa01b6e9624c870d9d56380f7c2dfb65cc` |
| Critique tokens | `b4562214183267db848af597672061a42e149e14f0e989db4d8b6296` |

## Endpoints

| Service | URL |
|---------|-----|
| Ogmios | `https://ogmios.vector.mainnet.apexfusion.org` |
| Submit | `https://submit.vector.mainnet.apexfusion.org/api/submit/tx` |
| Koios | `https://v2.koios.vector.mainnet.apexfusion.org/` |
| Explorer | `https://vector.apexscan.org` |

## Deployer

| Field | Value |
|-------|-------|
| Wallet address | `addr1q9ptpk527dm3ph77e2q2p2hnmypw7d7y9nru03cn9emmh7mczry6wn4sxkgkpkg3cx07hlyjrls6y7fq4h36rkzkyrnslachm0` |
| Total cost | ~225 AP3X (14 transactions) |
| Remaining balance | ~1779 AP3X |

## Version History

| Version | Date | Notes |
|---------|------|-------|
| v8 | 2026-04-15 | Initial mainnet deployment. 14 TXs: 3 holder refs, 5 validator refs, params/oracle datums, 3 treasury batches, CrossRefs NFT. All reference scripts at unspendable addresses. Agent registry v2. |
