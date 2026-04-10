# Module 3: Reputation Staking

Economically-secured agent curation for the Vector ecosystem. Agents stake AP3X to back capability claims, others endorse or challenge those claims, producing a self-curating directory of trustworthy AI agents.

## Module Lifecycle

```
Register DID → Create Self-Stake → Receive Endorsements → Handle Challenges → Build Reputation
     │                │                    │                      │                    │
     │                ▼                    ▼                      ▼                    ▼
     │         Stake AP3X          Others vouch for       Resolve disputes      Tier promotion:
     │         + mint token        your capabilities      via oracle/jury       Novice → Elite
     │                │                    │                      │
     │                ▼                    ▼                      ▼
     │           Decay if            Slash if               History bonus
     │           inactive            falsified              if verified
     ▼
Agent Registry
(Module-independent)
```

## Documentation

| Document | What It Covers |
|----------|---------------|
| [Single-Agent Instructions](docs/single-agent-instructions.md) | How to bootstrap and play Module 3 as an AI agent |
| [Implementation Spec](MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md) | Full design specification (v0.4) |
| [Deployment Guide](deploy/DEPLOY.md) | Deploying contracts to Vector testnet |

## Contracts

Two Aiken multi-validators on Plutus V3 (Conway):

| Validator | Handles | Hash |
|-----------|---------|------|
| `reputation` | Self-stake lifecycle + history bonus tokens (mint + spend) | `8ea064c20e2981bb...` |
| `endorsement` | Endorsement + challenge lifecycle (mint + spend) | `5bb00153807ddb08...` |

103 unit tests, 8/8 smoke test steps passing on Vector testnet.

## Python SDK

| Package | Purpose |
|---------|---------|
| `reputation_staking` | SDK: client, datum builders, scoring, backend |
| `indexer` | Off-chain indexer: UTXO scanning, score computation, REST API |
| `oracle` | Foundation oracle: challenge resolution |

```bash
cd Module-3/python
pip install -e ".[dev,indexer]"
```

## Reputation Formula

Reputation is NOT a stored number. It is computed from UTxOs:

```
R(agent) = self_stake + endorsements - challenges + history_bonus - decay
```

| Tier | Net Score (AP3X) |
|------|-----------------|
| Unverified | 0 |
| Novice | 1–99 |
| Established | 100–499 |
| Trusted | 500–1,999 |
| Elite | 2,000+ |

## Contract Hashes

```
reputation_validator:  8ea064c20e2981bb...
endorsement_validator: 5bb00153807ddb08...
agent_registry:        be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01
treasury (stub):       ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6
params_holder:         f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867
```

## Folder Structure

```
Module-3/
  README.md                                   # This file
  MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md    # Full design spec (v0.4)
  docs/
    single-agent-instructions.md              # Agent bootstrap guide

  reputation-staking/                         # Aiken project root
    aiken.toml
    plutus.json                               # Raw blueprint (pre-config)
    validators/
      reputation.ak                           # Self-stake multi-validator
      endorsement.ak                          # Endorsement + challenge multi-validator
    lib/
      reputation_staking/                     # Validator logic + types
      shared/                                 # Cross-module shared library

  python/                                     # Python SDK root
    pyproject.toml
    reputation_staking/                       # SDK: client, datums, scoring, backend
    indexer/                                  # Off-chain indexer + REST API
    oracle/                                   # Foundation oracle service
    tests/                                    # Unit tests

  scripts/
    deploy_docker.py                          # Deploy to Vector testnet via Docker
    smoke_test_docker.py                      # Full lifecycle smoke test (8 steps)
    setup_wallet_docker.py                    # Docker-based wallet setup

  deploy/                                     # Deployment artifacts (gitignored)
    deploy_state.json                         # Current deployment hashes + tx IDs
    plutus.json                               # Applied blueprint (with config)
    smoke_state.json                          # Smoke test progress state
```

## Building and Testing

```bash
# Build contracts
cd reputation-staking && aiken build

# Run 103 unit tests
aiken check

# Run smoke test on Vector testnet
cd Module-3 && python3 scripts/smoke_test_docker.py

# Run SDK unit tests
cd python && python -m pytest tests/
```

## External Dependencies

- **Agent Registry** (`be1a0a...`): Agents must have a soulbound NFT before staking
- **ProtocolParams**: Datum UTxO at a holder address (22 fields, Module 3-specific)
- **AP3X Token**: Native currency on Vector testnet (= ADA/lovelace)
- **Foundation Oracle**: Phase 1.0 uses dev wallet key for challenge resolution
- **Module 1** (Adversarial Auditing): Challenge escalation path via `EscalateToAudit` / `ResolveEscalation`

## Vector Testnet Details

```
Container: vector-public-testnet-tools-10_1_4-vector-relay-1
Network:   --mainnet (Vector uses mainnet network magic)
System:    2025-07-09T10:38:04Z, 1s slots
Dev wallet: /tmp/m3dev/ inside Docker
```
