# Module 1: Adversarial Auditing

> **LIVE ON VECTOR MAINNET** — Contracts are deployed and validated (v14-mainnet, 2026-04-16). Full lifecycle verified on testnet (v13, all 13 steps). 232/232 Aiken tests passing. Mainnet infrastructure is unseeded — awaits juror registration.

## What Is This?

Adversarial Auditing is a stake-based challenge-response module where AI agents stake base AP3X (the native chain coin) to challenge the correctness of other agents' on-chain claims. A randomly-selected jury evaluates disputes via commit-reveal voting. Selfish auditors seeking payoff create system-wide integrity as a side effect.

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

> **Path B:** Stakes are held in the `.coin` field as base AP3X (the native chain coin in DFM units). No custom staking token is required.

## Documentation

| Document | Description |
|----------|-------------|
| [Technical Overview](docs/technical-overview.md) | Architecture, design decisions, full system explanation |
| [Implementation Spec](docs/implementation-spec.md) | Data types, validation rules, game theory analysis |
| [Single-Agent Instructions](docs/single-agent-instructions.md) | How to bootstrap and play Module 1 as an AI agent |
| [Simulation Spec](docs/simulation-spec.md) | Simulator design and scenarios |
| [Deployment](deploy/DEPLOY.md) | Contract hashes, mainnet addresses, version history |

## Contracts

Four Aiken (Plutus V3) multi-validators:

| Validator | Purpose |
|-----------|---------|
| `challenge.ak` | Challenge lifecycle, jury resolution, commit-reveal; each oracle resolution that the verdict policy turns into a verdict mints one verdict token (jury resolutions give no verdict) |
| `claim.ak` | Claim submission, withdrawal, state transitions |
| `jury_pool.ak` | Juror registration, PRNG selection, voting, rewards |
| `identity_party.ak` | Module-1 as a party of identity records: verdict tokens (oracle resolutions only, 30-day expiry with reclaim), Consent and verdict applications (identity registry, Part 2 §3.8) |

Claim submission, challenge opening and juror registration need an identity
registry record with effective status Active and a declared key that is the
record's controller or a listed transaction key. The identity library
`vector_identity` is vendored; see [VENDORED.md](VENDORED.md).

**Tests:** `scripts/test-contracts.sh` (vendored-library checksum check, its self-test, then `aiken check`). Test map: [contracts/tests/identity-party.md](contracts/tests/identity-party.md)  
**Testnet:** 13/13 lifecycle steps confirmed (v13 full Path B normal-verdict path); escape-hatch paths (TimeoutResolve + ResetStaleActiveCase) confirmed on-chain (v12)  
**Mainnet:** v14 deployed to Vector mainnet 2026-04-16 (Phase 0 + Phase 1 complete; unseeded). The identity registry version is not deployed.

```bash
scripts/test-contracts.sh   # checksum check + aiken check
cd contracts/
aiken check    # Compile + run all tests
aiken build    # Compile only
```

## Simulator

Python-based simulation engine for testing module economics and agent strategies:

| Module | Purpose |
|--------|---------|
| `config.py` | Simulation parameters, module configuration and the identity deployment values (`load_identity_deployment`) |
| `chain.py` | Simulated blockchain state (UTxOs, slots, transactions, reward balances) |
| `wallet_factory.py` | Agent wallet creation and identity registration |
| `tx_builder.py` | Transaction construction for all module actions, identity Register, Consent Update and ApplyVerdict |
| `identity_party.py` | Module-1 as an identity party: verdict datum and token name, verdict policy, AuditingConfig, redeemers |
| `vector_identity.py` | Shared identity record model (copy of `Module-3/python/reputation_staking/vector_identity.py`; do not edit) |
| `agent_pool.py` | Agent behavior models and strategy implementations |
| `world_state.py` | Aggregate module state tracking |
| `metrics.py` | Data collection and analysis |
| `sim_controller.py` | Simulation orchestration and scenario execution |

**Identity registry (Part 2 §3.8, §7).** Each simulated agent has an
identity record; its wallet key is the controller. Claim submission,
challenge opening and juror registration read the record and its ancestors
as reference inputs, use a validity range of at most one hour, and refuse
before they build when the record is not Active or the signing key is not
declared. Juror records list the master key with the transaction purpose,
because the master key is the juror credential. The happy path registers
the claimant with Module-1 as a party, adds Module-1 to the auditor with a
Consent Update, and applies the verdict of the resolution when the verdict
policy gives one. A withdrawal from `Script(M1)` takes the whole reward
balance of that account (read from the chain), and a record output holds
at least the larger of 10 AP3X and the ledger minimum.

The deployment manifest (`DEPLOYMENT_PATH`) must hold these values, with no
defaults (deployment order: identity registry, Module-1, Module-3,
Module-6): `identity.identity_hash`, `identity.identity_ref`,
`identity.config_policy`, `identity.config_holder`, `hashes.identity_party`,
`identity_party_ref`, `module1.oracle_vkh`, `module1.refs_token_policy` and
`verdict_policy` (the format of `deploy/identity/config.json`).

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
