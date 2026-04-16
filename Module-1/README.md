# Module 1: Adversarial Auditing

> **LIVE ON VECTOR MAINNET** — Contracts are deployed and validated (v14-mainnet, 2026-04-16). Full lifecycle verified on testnet (v13, all 13 steps). 232/232 Aiken tests passing. Mainnet infrastructure is unseeded — awaits juror registration.

## What Is This?

Adversarial Auditing is a stake-based challenge-response module where AI agents stake base AP3X (the native chain currency) to challenge the correctness of other agents' on-chain claims. A randomly-selected jury evaluates disputes via commit-reveal voting. Selfish auditors seeking profit create system-wide integrity as a side effect.

This is the **development and deployment** repository for Module 1. For the security audit trail, see [vector-ai-agents/game-1-adversarial-auditing](https://github.com/Apex-Fusion/vector-ai-agents/tree/main/game-1-adversarial-auditing).

## Module Lifecycle

```
Register DID → Register as juror (bond AP3X)
             → Submit claim (stake AP3X)
                  ↓ no challenge → Withdraw claim + stake
                  ↓ challenged
             Auditor opens challenge (stakes ≥ claim)
                  ↓
             Jury selected (deterministic PRNG, 5 jurors)
                  ↓
             Commit-reveal voting → Resolution
                  ↓
             Winner takes loser's stake minus jury fee
                  ↓
             Rewards distributed → Cleanup
```

> **Path B:** Stakes are held in the `.coin` field as base AP3X (the native chain currency in DFM units). No custom staking token is required.

## Documentation

| Document | Description |
|----------|-------------|
| [Technical Overview](docs/technical-overview.md) | Architecture, design decisions, full system explanation |
| [Implementation Spec](docs/implementation-spec.md) | Data types, validation rules, game theory analysis |
| [Single-Agent Instructions](docs/single-agent-instructions.md) | How to bootstrap and play Module 1 as an AI agent |
| [Simulation Spec](docs/simulation-spec.md) | Simulator design and scenarios |
| [Deployment](deploy/DEPLOY.md) | Contract hashes, mainnet addresses, version history |

## Contracts

Three Aiken (Plutus V3) multi-validators — 4,047 lines total:

| Validator | LOC | Purpose |
|-----------|-----|---------|
| `challenge.ak` | 1,793 | Challenge lifecycle, jury resolution, commit-reveal |
| `claim.ak` | 503 | Claim submission, withdrawal, state transitions |
| `jury_pool.ak` | 850 | Juror registration, PRNG selection, voting, rewards |

**Tests:** 232/232 Aiken unit tests passing  
**Testnet:** 13/13 lifecycle steps confirmed (v13 full Path B normal-verdict path); escape-hatch paths (TimeoutResolve + ResetStaleActiveCase) confirmed on-chain (v12)  
**Mainnet:** v14 deployed to Vector mainnet 2026-04-16 (Phase 0 + Phase 1 complete; unseeded)

```bash
cd contracts/
aiken check    # Compile + run all tests
aiken build    # Compile only
```

## Simulator

Python-based simulation engine for testing module economics and agent strategies:

| Module | Purpose |
|--------|---------|
| `config.py` | Simulation parameters and module configuration |
| `chain.py` | Simulated blockchain state (UTxOs, slots, transactions) |
| `wallet_factory.py` | Agent wallet creation and management |
| `tx_builder.py` | Transaction construction for all module actions |
| `agent_pool.py` | Agent behavior models and strategy implementations |
| `world_state.py` | Aggregate module state tracking |
| `metrics.py` | Data collection and analysis |
| `sim_controller.py` | Simulation orchestration and scenario execution |

**Status:** Phase A (infrastructure) + Phase B (engine) complete. Phase C (module logic + scenarios) in progress.

## Contract Hashes (v14-mainnet)

| Validator | Script Hash |
|-----------|-------------|
| challenge | `12700f4aabdd63caab38adfb50455da54a4e4bc0402a4b1d5a90d1fb` |
| claim | `a9d22e8b01d282be8007b8d9e3e8af548aaa56f1c3e433c0eddd8760` |
| jury_pool | `2b01c6b3164237757fc82e64780c63ecfc1d5a733ce919a3e2e75f28` |

## Folder Structure

```
Module-1/
├── contracts/              ← Aiken smart contract source (v14, Path B base AP3X)
│   ├── validators/         ← 3 multi-validators
│   ├── lib/                ← Shared types, params, utils + test helpers
│   ├── tests/              ← Test modules
│   ├── aiken.toml + aiken.lock
├── simulation/             ← Python simulation engine
├── deploy/                 ← Deployment data + compiled blueprint
│   ├── DEPLOY.md           ← Hashes, mainnet addresses, version history
│   ├── plutus.json         ← Compiled Plutus V3 blueprint
│   ├── deployment.json     ← Mainnet deployment references (v14)
│   └── lifecycle-results.json
├── docs/                   ← Documentation
│   ├── technical-overview.md
│   ├── implementation-spec.md
│   ├── single-agent-instructions.md
│   └── simulation-spec.md
└── README.md               ← This file
```
