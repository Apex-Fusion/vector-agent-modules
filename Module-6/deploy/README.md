# Module-6 Deployment Artifacts

Layout:

```
deploy/
├── plutus.json    ← Compiled Plutus V3 blueprint. Network-agnostic.
├── testnet/       ← Vector testnet deployment (current)
│   ├── DEPLOY.md            ← Human-readable: hashes, addresses, params, lifecycle
│   ├── deployment.json      ← Machine-readable deployment state
│   └── lifecycle-results.json
└── mainnet/       ← Vector mainnet deployment (v8, 2026-04-15 — live)
```

## Why per-network folders

The Module-6 validators are **parameterized** by `GovernanceConfig` (which embeds the agent-registry script hash, among other refs). The same `plutus.json` blueprint compiles to *different* script hashes and addresses on each network depending on the parameters baked in at deploy time. Each network therefore produces its own set of:

- 5 validator script hashes (proposal/critique mint+spend, endorsement spend)
- 3 script addresses
- 5 reference-script UTxOs (CIP-33)
- 3 holder-script UTxOs
- 1 GovernanceParams datum UTxO
- 1 GovernanceOracle datum UTxO
- 1 CrossRefs NFT UTxO
- 3 Treasury batch UTxOs

…all of which live under the network-specific folder.

`plutus.json` is the compiled bytecode of the unparameterized validators — same bytes regardless of network — so it stays at the top of `deploy/`.

## When you redeploy

`scripts/deploy.py` writes its outputs to `wallets/deploy_state.json` keyed by network. After a successful deploy, refresh the human-readable artifacts under the matching `deploy/<network>/` folder.
