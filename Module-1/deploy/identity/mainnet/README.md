# Module-1 on Vector mainnet for the identity registry (2026-11-03)

This folder publishes the Module-1 deployment of the identity registry release (`DEPLOY-IDENTITY.md` in the folder above). The tool `scripts/identity_deploy.py` wrote these files in its state folder (`deploy/identity/state/mainnet/`, not in git). The copies here are the same, except that the two local file paths are replaced by public paths. They hold only public chain data.

| File | Content |
|---|---|
| `manifest.json` | The Module-1 manifest: mode, verdict policy, protocol values, applied script hashes, addresses, reference outputs and transaction IDs. Module-1 clients read it (`DEPLOYMENT_PATH`). |
| `plan.json` | The plan of `identity_deploy.py plan`: the parameters in their application order. |
| `plutus.json` | The applied blueprint (traces silent). |

## Values

| Value | |
|---|---|
| Mode | Oracle (`oracle_active` true). The oracle is the Foundation operator key `470ded7c9d487c2c3832af84aea70987b2c9a01480192ebbf035d028`. |
| Verdict policy | Auditor wins: the claimer is suspended for 7 days. Claimer wins: no verdict. No revocations. |
| Protocol values | `config.json` in the folder above (the v14 values) |
| `identity_party` (M1) | `f6b094a89b58d5e196a53d6e5ebedcbb837db4a1b16a1477cf98305d` |
| `claim` | `53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523` |
| `challenge` | `9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08` |
| `jury_pool` | `1734760872e81fdc312cc380e2ce2bf597eff01ccb6d98ab7e8e0aee` |
| Records holder | `addr1wyrurgew6mp3x32xvq3nnq3ruep09qhs7vemy82m5qqczfsuu5n2k` |
| Identity deployment | agents-marketplace `contracts/identity-marketplace/deploy/mainnet/deployment.json` at `4c95c3f` (policy `d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762`) |

## The seal

The configuration record `dea7fd5baef73c0216e1098fe9db7a86561666ff4f6f3a44738df8f666cd379b#0` of the identity registry names M1. Nobody can spend it, so the identity registry trusts this Module-1 for ever. `identity_deploy.py check-seal --deployment <deployment.json>` compares the record with the plan.

## Transactions

| Step | Transaction | Result |
|---|---|---|
| `publish-claim` | `a5ad79d3ded0804241649242f03ab5aa22792361d625391ba5d42a58fec08335` | `claim_ref` `…#0` |
| `publish-challenge` | `fe2f04f569107f75faf6b93bb93d7c3aa616185612fd6a64c3dba712d0f687e5` | `challenge_ref` `…#0` |
| `publish-jury_pool` | `4f092c18ebb08427f590c94ddd5673f5f4c5de6ef4c6fd2e9fb339e091041f71` | `jury_pool_ref` `…#0` |
| `publish-identity_party` | `319d39dbe94fce88ab5ec5526ba3127c6d97c1f9c4a44abe02e6a2949c54770b` | `identity_party_ref` `…#0` |
| `register-identity_party` | `4e09cb641ad3ca9e3ff20402cfa83cbb59a4626fb24fe45cee61a82d5e0b665b` | stake registration of Script(M1), deposit 5 AP3X |
| `records` | `72e417cad239afc47626574246ee3a15dd2cddd8bc648f0a21fa290299dc8941` | CrossValidatorRefs `#0`, ProtocolParams `#1` |

The reference outputs are at the unspendable reference address of the identity deployment. Total cost: 179.838503 AP3X (fees 2.753133, deposit 5, outputs 172.085370).
