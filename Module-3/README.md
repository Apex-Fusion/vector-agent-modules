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

Two Aiken multi-validators on Plutus V3 (Conway), deployed to both testnet and mainnet:

| Validator | Handles | Testnet Hash | Mainnet Hash |
|-----------|---------|-------------|-------------|
| `reputation` | Self-stake lifecycle + history bonus tokens (mint + spend) | `7e0d53b6797cd770...` | `5168e1871cfdb1e5...` |
| `endorsement` | Endorsement + challenge lifecycle (mint + spend) | `715726f3670743b1...` | `77196bed7fb84576...` |

110 unit tests, 12/12 smoke test steps passing on both Vector testnet and mainnet (includes CapabilityVerified and CapabilityFalsified paths).

## Python SDK

| Package | Purpose |
|---------|---------|
| `reputation_staking` | SDK: client, Ogmios backend, PlutusData types, scoring |
| `indexer` | Off-chain indexer: UTXO scanning, score computation, REST API |
| `oracle` | Foundation oracle: challenge resolution |

```bash
cd Module-3/python
pip install -e ".[dev,indexer]"
```

### Quick Start (Remote / Ogmios)

```python
from reputation_staking import ReputationStakingClient
from reputation_staking.ogmios_backend import OgmiosHttpContext, load_wallet

context = OgmiosHttpContext()
skey, vkey, wallet_addr = load_wallet("wallet/payment.skey")
client = ReputationStakingClient.from_deploy_state(
    "deploy/deploy_state.json", context, skey,
)
tx = client.create_stake("agent_did_hex", ["code_review"], 10_000_000)
```

No local node or Docker required. Uses Ogmios HTTP JSON-RPC for chain queries and the HTTP submit endpoint for transaction submission, matching Module 1 and Module 6. Works with both testnet and mainnet endpoints.

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

**Mainnet:**
```
reputation_validator:  5168e1871cfdb1e55c18ee173acbcdce092044a48bc2e23f3ba35093
endorsement_validator: 77196bed7fb8457610800cc7241cf4496e00d7901de9079fb0323ebf
refs_token_policy:     09dce01a3c2f2fddeda34a547bb4a5ef9f156feae6c4f45d6d74af84
```

**Testnet:**
```
reputation_validator:  7e0d53b6797cd7707eb923b0ab044d4e03ef54cf115a6c14fadfb38e
endorsement_validator: 715726f3670743b145b92d859cc5025128a99de88cd5ac42120258b4
refs_token_policy:     b07ad1a1244a388d54463fce3c68aa8d4ddc5a3297159d20590d574f
```

**Shared (both networks):**
```
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
    reputation_staking/                       # SDK package
      client.py                               # ReputationStakingClient (PyCardano TransactionBuilder)
      ogmios_backend.py                       # OgmiosHttpContext, submit, evaluate, wallet utils
      plutus_data.py                          # PlutusData classes matching on-chain types
      scoring.py                              # Reputation score computation + decay + tiers
      constants.py                            # Network params, Ogmios URLs, tier thresholds
      models.py                               # Off-chain dataclasses + enums
      token_names.py                          # Token name derivations (rstk_, rend_, rchl_, etc.)
      datums.py                               # cardano-cli JSON datum builders (legacy)
      backend.py                              # ChainBackend Protocol (legacy)
      docker_backend.py                       # Docker/cardano-cli backend (legacy)
      utils.py                                # Address helpers, slot<->POSIX conversions
    indexer/                                  # Off-chain indexer + REST API
    oracle/                                   # Foundation oracle service
    tests/                                    # Unit tests

  scripts/
    deploy_docker.py                          # Deploy to Vector testnet via Docker
    deploy_ogmios.py                          # Deploy to Vector testnet via Ogmios (remote)
    deploy_mainnet_ogmios.py                  # Deploy to Vector mainnet via Ogmios
    smoke_test_ogmios.py                      # Full lifecycle smoke test — testnet (12 steps)
    smoke_test_mainnet_ogmios.py              # Full lifecycle smoke test — mainnet (12 steps)
    smoke_test_docker.py                      # Legacy smoke test — Docker/cardano-cli
    setup_wallet_docker.py                    # Docker-based wallet setup

  deploy/                                     # Deployment artifacts
    deploy_state.json                         # Testnet deployment hashes + tx IDs
    plutus.json                               # Testnet applied blueprint (with config)
    mainnet/
      deploy_state.json                       # Mainnet deployment hashes + tx IDs
      plutus.json                             # Mainnet applied blueprint (with config)
```

## Building and Testing

```bash
# Build contracts
cd reputation-staking && aiken build

# Run 110 unit tests
aiken check

# Run smoke test on Vector testnet (remote — no Docker required)
cd Module-3 && PYTHONPATH=python:$PYTHONPATH python3 scripts/smoke_test_ogmios.py

# Run smoke test on Vector mainnet
cd Module-3 && PYTHONPATH=python:$PYTHONPATH python3 scripts/smoke_test_mainnet_ogmios.py

# Run smoke test via Docker (legacy — requires local node)
cd Module-3 && python3 scripts/smoke_test_docker.py

# Run SDK unit tests
cd python && python -m pytest tests/
```

## External Dependencies

- **Agent Registry** (`be1a0a...`): Agents must have a soulbound NFT before staking
- **ProtocolParams**: Datum UTxO at a holder address (22 fields, Module 3-specific)
- **AP3X Token**: Native currency on Vector (= ADA/lovelace)
- **Foundation Oracle**: Phase 1.0 uses dev wallet key for challenge resolution
- **Module 1** (Adversarial Auditing): Challenge escalation path via `EscalateToAudit` / `ResolveEscalation`

## Architecture

Module 3 uses the same remote chain interaction pattern as Module 1 and Module 6:

- **PyCardano TransactionBuilder** for transaction construction (no cardano-cli)
- **Ogmios HTTP JSON-RPC** for chain queries (protocol params, UTxOs, tip)
- **HTTP submit endpoint** for transaction submission
- **CIP-33 reference scripts** for large multi-validator transactions
- **PlutusData classes** for type-safe datum/redeemer construction

The legacy Docker/cardano-cli backend (`DockerChainBackend`) is preserved for local-node testing but is not used by the main client.

## Vector Network Details

**Mainnet:**
```
Ogmios:       https://ogmios.vector.mainnet.apexfusion.org (HTTP JSON-RPC)
Submit:       https://submit.vector.mainnet.apexfusion.org/api/submit/tx
Network:      mainnet (Vector uses mainnet network magic)
System start: 2025-08-29T16:40:00Z, 1s slots
```

**Testnet:**
```
Ogmios:       https://ogmios.vector.testnet.apexfusion.org (HTTP JSON-RPC)
Submit:       https://submit.vector.testnet.apexfusion.org/api/submit/tx
Network:      mainnet (Vector testnet also uses mainnet network magic)
System start: 2025-07-09T10:38:04Z, 1s slots
```
