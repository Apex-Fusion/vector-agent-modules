# Module 3: Reputation Staking

Economically-secured agent curation for the Vector ecosystem. Agents stake AP3X to back capability claims, others endorse or challenge those claims, producing a self-curating directory of trustworthy AI agents.

See `MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md` (v0.4) for the full design specification.

## Architecture

Two Aiken multi-validators on Plutus V3 (Conway):

| Validator | Handles | Hash (current deployment) |
|-----------|---------|---------------------------|
| `reputation` | Self-stake lifecycle + history bonus tokens (mint + spend) | `8ea064c20e2981bb...` |
| `endorsement` | Endorsement + challenge lifecycle (mint + spend) | `5bb00153807ddb08...` |

Reputation is NOT a stored number. It is computed from UTXOs:

```
reputation(agent) = self_stake + sum(endorsements) - sum(challenges) + history_bonus - decay
```

Each component is a separate UTXO at the validator address. An off-chain indexer aggregates them into a score.

### Cross-Validator Communication

The two validators reference each other via a `CrossValidatorRefs` datum stored in an NFT UTXO. This NFT is minted under a NativeScript policy locked to the deployer's key. Both validators read this reference input to discover each other's policy IDs.

### External Dependencies

- **Agent Registry** (`be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01`): Agents must be registered (have a soulbound NFT) before staking. The registry is at `../vector-ai-agents/agent-registry/`.
- **ProtocolParams**: A datum UTXO at a holder address containing protocol parameters (fee rates, cooldowns, minimums). Currently a stub — will be replaced by a shared governance contract.
- **AP3X Token**: On Vector testnet, AP3X = native currency (lovelace). Configured via `ReputationConfig.ap3x_policy_id` (empty = ADA).
- **Foundation Oracle**: Phase 1.0 uses a single oracle key for challenge resolution and decay claims. Will be replaced by decentralized mechanisms in Phase 2.

## Tech Stack

| Tool | Version | Purpose |
|------|---------|---------|
| Aiken | v1.1.21 | Smart contract language (Plutus V3) |
| aiken-lang/stdlib | v3.0.0 | Standard library |
| Python 3.10+ | - | Deployment and smoke test scripts |
| cardano-cli | conway era | Transaction building (inside Docker) |
| Docker | - | Runs cardano-node for Vector testnet |
| cbor2 | pip | CBOR encoding for datums/redeemers |
| bech32 | pip | Address encoding |

### Environment Setup

**Option A: Nix (recommended)**
```bash
cd Module-3
nix-shell  # Provides aiken, python, jq, etc.
```

**Option B: Manual**
```bash
# Install Aiken v1.1.21
# Install Python 3.10+ with: pip install cbor2 bech32
```

### Docker Node

The smoke test and deploy scripts run cardano-cli inside a Docker container:
```
Container: vector-public-testnet-tools-10_1_4-vector-relay-1
Socket:    /ipc/node.socket
Network:   --mainnet (Vector uses mainnet network magic)
```

## File Structure

```
Module-3/
  MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md   # Full design spec (v0.4)
  README.md                                   # This file
  .env.example                                # Environment template
  shell.nix                                   # Nix dev environment
  
  reputation-staking/                         # Aiken project root
    aiken.toml                                # Aiken v1.1.21, stdlib v3.0.0, Plutus V3
    plutus.json                               # Raw blueprint (pre-config)
    
    validators/
      reputation.ak                           # Self-stake multi-validator (mint + spend)
      endorsement.ak                          # Endorsement + challenge multi-validator
    
    lib/
      reputation_staking/
        types.ak                              # All on-chain types (Sections 4.1-4.5)
        config.ak                             # ReputationConfig + CrossValidatorRefs
        params.ak                             # ProtocolParams stub
        stake_validation.ak                   # CreateStake, Increase, Decrease, UpdateCaps
        endorsement_validation.ak             # Create, Increase, Withdraw, Slash endorsements
        challenge_validation.ak               # Create, Respond, Resolve, Distribute, Escalate
        decay.ak                              # Decay calculation + ClaimDecayRefund
        scoring.ak                            # Tier helpers (Unverified..Elite)
        utils.ak                              # DID verification, token naming, shared helpers
        test_helpers.ak                       # Test fixtures and builders
        *_tests.ak                            # Unit tests (103 total, all passing)
      shared/                                 # Shared library modules
        did_verification.ak                   # Agent DID/registry verification
        credential.ak                         # Credential helpers
        token_naming.ak                       # Token name derivation
        utxo.ak                               # UTXO search helpers
        oracle.ak                             # Oracle signature verification
        bytearray.ak                          # ByteArray utilities
  
  scripts/
    deploy_docker.py                          # Deploy to Vector testnet via Docker
    smoke_test_docker.py                      # Full lifecycle smoke test via Docker
    deploy.py                                 # Deploy via Ogmios/submit API (alternative)
    smoke_test.py                             # Smoke test via Ogmios (alternative)
    setup_wallet.py                           # Wallet setup helper
  
  deploy/                                     # Deployment artifacts (gitignored)
    deploy_state.json                         # Current deployment hashes + tx IDs
    deployment.json                           # Simplified deployment info
    plutus.json                               # Applied blueprint (with config baked in)
    smoke_state.json                          # Smoke test progress state
```

## Building and Testing

```bash
cd reputation-staking

# Build (compiles validators, generates plutus.json blueprint)
aiken build

# Run all 103 unit tests
aiken check

# Build with traces for debugging on-chain failures
aiken build --trace-level verbose --trace-filter user-defined
```

## Deployment (Vector Testnet)

The deploy script handles:
1. Computing the NativeScript policy for CrossValidatorRefs NFT
2. Building `ReputationConfig` CBOR and applying it to both validators via `aiken blueprint apply`
3. Deploying reference scripts on-chain (CIP-33)
4. Creating the ProtocolParams datum UTXO
5. Minting the CrossValidatorRefs NFT with both validator hashes

```bash
cd Module-3
python3 scripts/deploy_docker.py
```

The deploy is resumable — it saves state to `deploy/deploy_state.json` and skips completed steps.

### Wallet Setup

- **Dev wallet**: `/tmp/m3dev/` inside the Docker container (payment.addr, payment.skey, payment.vkey)
- **Fee wallet**: `cardano-v8-transition/fee/` inside Docker (~1.18B AP3X, for topping up dev wallet)
- Dev wallet vkey hash: `2ef77ec4340057363c3824919a61db70ee9683ee9b7d15283aa91931`

To top up the dev wallet from the fee wallet, build and submit a transaction inside Docker.

## Smoke Test

The smoke test runs the full reputation staking lifecycle end-to-end on Vector testnet:

```bash
cd Module-3
python3 scripts/smoke_test_docker.py
```

### Steps (all 8 passing)

| Step | Action | What it does |
|------|--------|-------------|
| 1 | RegisterAgentA | Mints agent NFT in registry for Agent A |
| 2 | RegisterAgentB | Mints agent NFT in registry for Agent B |
| 3 | CreateStake | Agent A stakes 10 AP3X, mints stake token |
| 4 | MintEndorsement | Agent B endorses Agent A (5 AP3X) |
| 5 | MintChallenge | Agent B challenges Agent A's `code_review` capability (25 AP3X) |
| 6 | ResolveChallenge | Foundation oracle resolves: CapabilityVerified |
| 7 | DistributeOutcome | Burns challenge token, mints history bonus, pays target + treasury |

The test is resumable — state is saved to `deploy/smoke_state.json` after each step. Delete this file to restart from scratch.

### Reference Scripts in Step 7

Step 7 (DistributeOutcome) requires two validator scripts simultaneously (endorsement spend + burn, reputation mint). Two inline scripts exceed the 16KB transaction size limit, so step 7 uses CIP-33 reference scripts deployed in step 3 of the deploy.

## Current Status: What Is Done

### On-Chain Contracts (Complete)

All Aiken validators are implemented and tested (103 unit tests, 0 failures):

- **Self-Stake**: CreateStake, IncreaseStake, DecreaseStake, UpdateCapabilities, ClaimDecayRefund
- **Endorsement**: MintEndorsement, IncreaseEndorsement, WithdrawEndorsement, SlashEndorsement
- **Challenge**: MintChallenge, RespondToChallenge, ResolveChallenge (oracle), DefaultJudgment, DistributeOutcome
- **History Bonus**: MintHistoryBonus (ChallengeWon, AuditClaimWon, JurorDuty, GenesisBonus sources)
- **Decay**: Decay calculation, ClaimDecayRefund
- **Module 1 Integration**: EscalateToAudit, ResolveEscalation (cross-module challenge escalation)
- **Security**: F1 orphan burn prevention, F2 mint-spend coupling, F-NEW-1 double satisfaction prevention, F4 audit exploit prevention

### Deployment and Testing (Complete)

- Deploy script (`deploy_docker.py`) works end-to-end on Vector testnet
- Smoke test (`smoke_test_docker.py`) passes all 8 steps
- Reference scripts deployed on-chain
- CrossValidatorRefs NFT minted
- ProtocolParams datum UTXO created

## What Needs To Be Done

### Phase 1.0 Remaining Work

1. **Python SDK / Off-chain Library**
   - Transaction builders for each action (CreateStake, MintEndorsement, etc.)
   - Datum/redeemer serialization helpers
   - UTXO query helpers (find stake, find endorsements, etc.)
   - Integration with existing Vector agent SDK

2. **Indexer / Score Computation**
   - Off-chain indexer that watches validator addresses
   - Computes `reputation(agent) = stake + endorsements - challenges + bonus - decay`
   - Tier assignment: Unverified, Novice, Established, Trusted, Elite
   - API endpoint for querying agent reputation

3. **Foundation Oracle Service**
   - Service that signs ResolveChallenge transactions
   - Review UI/process for evaluating challenges
   - Decay attestation service

4. **Cleanup / Polish**
   - Remove `deploy.py` and `smoke_test.py` (Ogmios-based alternatives that are superseded by Docker versions)
   - Add `setup_wallet.py` Docker variant
   - Improve error messages in deploy/smoke scripts

### Phase 2 (Future)

5. **Decentralized Challenge Resolution**
   - Replace Foundation oracle with jury-based resolution
   - Implement jury selection, voting, and reward distribution
   - This is gated on Module 1 (Adversarial Auditing) being fully deployed

6. **Decentralized Decay**
   - Replace oracle attestation for decay claims with automated verification
   - Periodic decay based on time since last activity

7. **Genesis Bonus Program**
   - MintGenesisBonus for early adopters within a minting window
   - Protection period before genesis bonuses can be burned

8. **Treasury Integration**
   - Connect protocol fees to actual treasury contract
   - Currently uses a stub treasury script hash

## Key Technical Details for AI Agents

### Aiken Encoding Gotchas

**Sum type variants use nested Constr encoding.** For `type Foo { A(Bar) | B(Baz) }`, the variant `B(baz_value)` encodes as:
```json
{"constructor": 1, "fields": [{"constructor": 0, "fields": [baz_fields...]}]}
```
NOT as `{"constructor": 1, "fields": [baz_fields_flat...]}`. This applies to:
- `EndorsementValidatorDatum.Challenge(ReputationChallengeDatum)` = `Constr(1, [Constr(0, [13 fields])])`
- `EndorsementValidatorDatum.Endorsement(EndorsementDatum)` = `Constr(0, [Constr(0, [6 fields])])`

**Plutus V3 OutputReference encoding.** `TransactionId` is a raw ByteArray in V3 (no Constr wrapper):
```json
{"constructor": 0, "fields": [{"bytes": "txhash"}, {"int": ix}]}
```

### Token Name Derivation

All token names use a prefix + truncated blake2b hash:
- Stake: `rstk_` + blake2b256(agent_did)[0:28]
- Endorsement: `rend_` + blake2b256(endorser_did ++ target_did)[0:28]
- Challenge: `rchl_` + blake2b256(challenger_did ++ target_did ++ capability)[0:24]
- History bonus: `hbonus_` + blake2b256(serialise_data(source_oref))[0:24]

### Redeemer Constructor Indices

**Reputation mint** (`StakeMintAction`): 0=MintStakeToken, 1=BurnStakeToken, 2=MintHistoryBonus, 3=BurnGenesisBonus

**Reputation spend** (`StakeAction`): 0=CreateStake, 1=IncreaseStake, 2=DecreaseStake, 3=UpdateCapabilities, 4=ClaimDecayRefund

**Endorsement mint** (`EndorsementMintAction`): 0=MintEndorsementToken, 1=BurnEndorsementToken, 2=MintChallengeToken, 3=BurnChallengeToken

**Endorsement spend** (`EndorsementValidatorAction`): wrapped as `EndorsementSpend(action)` = Constr(0, [...]) or `ChallengeSpend(action)` = Constr(1, [...])
- EndorsementAction: 0=IncreaseEndorsement, 1=WithdrawEndorsement, 2=SlashEndorsement
- RepChallengeAction: 0=WithdrawChallenge, 1=RespondToChallenge, 2=EscalateToAudit, 3=ResolveEscalation, 4=ResolveChallenge, 5=DefaultJudgment, 6=DistributeOutcome

### Vector Testnet Details

- Network flag: `--mainnet` (Vector uses mainnet network magic)
- System start: 2025-07-09T10:38:04Z
- Slot length: 1 second
- POSIX time: `slot_to_posix_ms(slot) = (slot * 1000) + 1752058684000`
- Spend validators require `--invalid-before {current_slot}` for validity bound checks
