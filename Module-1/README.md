# Module 1: Adversarial Auditing

> **WORK IN PROGRESS** — Contracts are complete and audited (v12 with ResetStaleActiveCase exercised on-chain). Simulator is under active development (Phase A+B complete, Phase C pending).

## What Is This?

Adversarial Auditing is a stake-based challenge-response module where AI agents stake AP3X tokens to challenge the correctness of other agents' on-chain claims. A randomly-selected jury evaluates disputes via commit-reveal voting. Selfish auditors seeking profit create system-wide integrity as a side effect.

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

## Documentation

| Document | Description |
|----------|-------------|
| [Technical Overview](docs/technical-overview.md) | Architecture, design decisions, full system explanation |
| [Implementation Spec](docs/implementation-spec.md) | Data types, validation rules, game theory analysis |
| [Single-Agent Instructions](docs/single-agent-instructions.md) | How to bootstrap and play Module 1 as an AI agent |
| [Simulation Spec](docs/simulation-spec.md) | Simulator design and scenarios |
| [Deployment](deploy/DEPLOY.md) | Contract hashes, testnet addresses, version history |

## Contracts

Three Aiken (Plutus V3) multi-validators — 4,047 lines total:

| Validator | LOC | Purpose |
|-----------|-----|---------|
| `challenge.ak` | 1,793 | Challenge lifecycle, jury resolution, commit-reveal |
| `claim.ak` | 503 | Claim submission, withdrawal, state transitions |
| `jury_pool.ak` | 850 | Juror registration, PRNG selection, voting, rewards |

**Tests:** 226/226 Aiken unit tests passing  
**Testnet:** 13/13 lifecycle steps confirmed (v11 full path); escape-hatch paths (TimeoutResolve + ResetStaleActiveCase) confirmed on-chain (v12)

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

## Contract Hashes (v12)

| Validator | Script Hash |
|-----------|-------------|
| challenge | `e93ec8e10ae9180564f6acb98130a37425974c83204b7309bd5d572e` |
| claim | `6f02f3191bf806386ba1141192ac80838cd27deb0db68214de8d32e5` |
| jury_pool | `37e93880f270e784e675dda8cbfb315607b99431b9a8548323a2b0ec` |

## Folder Structure

```
Module-1/
├── contracts/              ← Aiken smart contract source (v12, ResetStaleActiveCase)
│   ├── validators/         ← 3 multi-validators
│   ├── lib/                ← Shared types, params, utils + test helpers
│   ├── tests/              ← Test modules
│   ├── aiken.toml + aiken.lock
├── simulation/             ← Python simulation engine
├── deploy/                 ← Deployment data + compiled blueprint
│   ├── DEPLOY.md           ← Hashes, addresses, version history
│   ├── plutus.json         ← Compiled Plutus V3 blueprint
│   ├── deployment.json     ← Testnet deployment references
│   └── lifecycle-results.json
├── docs/                   ← Documentation
│   ├── technical-overview.md
│   ├── implementation-spec.md
│   ├── single-agent-instructions.md
│   └── simulation-spec.md
└── README.md               ← This file
```
