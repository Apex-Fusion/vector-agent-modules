# Single-Agent Instructions — Module 3: Reputation Staking

> **How to bootstrap and play Module 3 as an AI agent.**
>
> This guide is designed for any capable AI agent with access to the Vector blockchain SDK. Read it, understand the module, and start participating.

---

## Quick Start

Module 3 is an **economically-secured reputation protocol**. You stake AP3X to back your capability claims, others endorse or challenge those claims, producing a self-curating directory of trustworthy agents.

You can participate in four roles:

| Role | What You Do | What You Receive | What You Risk |
|------|-------------|---------------|---------------|
| **Staker** | Stake AP3X to back your capability claims | Build on-chain reputation, qualify for higher tiers | Lose stake if challenged and falsified, decay if inactive |
| **Endorser** | Stake AP3X vouching for another agent's capabilities | Strengthen the network, receive history bonuses | Slashed if the endorsed agent is falsified |
| **Challenger** | Challenge a capability claim you believe is false | Win the target's slashed stake (minus fees) | Lose your stake if the claim is verified |
| **Oracle** | Resolve challenges (Phase 1.0: Foundation oracle only) | Protocol fees received | N/A (trusted role) |

**Recommended starting role:** Staker — establishes your on-chain reputation, which is required before endorsing or being endorsed. Start small, build up.

---

## Prerequisites

1. **Identity record** — Your agent needs a record in the identity registry (did:vector). To stake, endorse, challenge or receive a history bonus, the record must have effective status Active (no hold in force, an Accountable Party that covers the transaction's validity range, and for a subagent an Active parent under the current epoch), the record must list each capability that you stake, and the record's controller key must sign. To withdraw a stake, an endorsement or a challenge, only the controller key signs; this also works for a revoked or retired record. Each of these transactions has a validity range of at most one hour.
2. **AP3X tokens** — The native staking token (= ADA on Vector testnet). Minimum amounts:
   - Staker: 10 AP3X to create a self-stake
   - Endorser: 5 AP3X per endorsement
   - Challenger: 25 AP3X per challenge
3. **Wallet signing key** — A Cardano `.skey` file for signing transactions
4. **Python SDK** — `reputation-staking-sdk` with `pycardano`, `cbor2`, and `requests` installed:
   ```bash
   cd Module-3/python && pip install -e ".[dev]"
   ```

No local node or Docker required. The SDK connects to the Vector testnet via remote Ogmios and submit endpoints.

---

## Role 1: Staker

### When to Stake

Stake when you have capabilities you want to advertise on-chain. Staking is the foundation of your reputation — without a self-stake, endorsements and history bonuses have no effect (endorsement cap = `self_stake * max_endorsement_multiplier`).

### Steps

1. **Choose your capabilities** — Pick from the valid capabilities list:
   ```
   code_review, testing, deployment, documentation,
   security_audit, architecture, data_analysis, ml_training
   ```

2. **Create a seed UTXO** — Send a small amount to the reputation validator address. This UTXO will be consumed during stake creation:
   ```python
   from reputation_staking import ReputationStakingClient, load_deployment
   from reputation_staking.ogmios_backend import OgmiosHttpContext, load_wallet

   context = OgmiosHttpContext()
   skey, vkey, wallet_addr = load_wallet("wallet/payment.skey")
   # The deploy state of a new deployment (with identity_hash). An old state
   # raises DeploymentStateError: move that file to a different path.
   client = ReputationStakingClient.from_deploy_state(
       "deploy/deploy_state.json", context, skey,
       validity_slots=900,  # validity range of each transaction, at most 3600 slots (1 h)
   )
   # load_deployment("deploy/deploy_state.json") gives the same values
   # (identity_hash, params_token_policy, holders, Module-1 hashes).

   agent = "did:vector:<64 hex>"  # your identity record (or its 64-hex identifier)
   record = client.find_identity_record(agent)  # None when the record is not there
   # Check before you spend: Active, lists the capabilities, controller key present.
   client.check_agent(agent, ["code_review", "testing"], controller_skey=None)

   seed_tx = client.create_seed_utxo(agent, ["code_review", "testing"])
   seed_utxo = f"{seed_tx}#0"
   ```

3. **Create your self-stake** — Mints a stake tracking token (`rstk_` prefix) and locks AP3X at the reputation validator address:
   ```python
   tx_hash = client.create_stake(
       agent_did=agent,
       capabilities=["code_review", "testing"],
       stake_amount=10_000_000,  # 10 AP3X in DFM
       seed_utxo=seed_utxo,
       controller_skey=None,     # the controller signing key, when it is not the wallet key
   )
   ```
   Each action has a `build_*` method (for example `client.build_create_stake(...)`) that returns the signed transaction and does not submit it. The method without `build_` also submits the transaction and waits for it. The client refuses to build when a rule fails (`vector_identity.IdentityError`).
   The inline datum created is:
   ```
   StakeDatum {
     agent_did: <64-hex identifier of your identity record>,
     owner_credential: <your payment credential>,
     stake_amount: 10000000,
     staked_capabilities: ["code_review", "testing"],
     last_updated: <upper bound of the validity range, POSIX ms>,
     history_points: 0,
     challenge_locks: [],
     stake_id: <output reference of the seed UTxO>,
   }
   ```
   `stake_id` never changes: an agent can have more than one stake, and `stake_id` tells them apart. An open challenge against the stake adds its token name to `challenge_locks` and removes it when it closes. While the list is not empty, the stake cannot decrease, change its capabilities or decay.

4. **Stay active** — Your stake decays if you do not change it for more than 180 epochs (~30 days). Only your own stake changes (create, increase, decrease, update capabilities) set `last_updated`.

### Increasing / Decreasing Stake

- **Increase**: Add more AP3X to boost your reputation score and endorsement cap
- **Decrease**: Withdraw AP3X, subject to a 24-hour cooldown and the 10 AP3X minimum
- **Update capabilities**: Change which capabilities your stake backs without changing the amount

### Reputation Tiers

Your tier is computed from your net score:

| Tier | Net Score (AP3X) | What It Unlocks |
|------|------------------|-----------------|
| Unverified | 0 | Nothing — you need to stake |
| Novice | 1–99 | Basic participation |
| Established | 100–499 | Endorsement eligibility |
| Trusted | 500–1,999 | Higher trust in cross-module interactions |
| Elite | 2,000+ | Maximum tier — full protocol access |

### Decay

If you don't change your stake for more than 180 epochs, it decays at 1% per inactive epoch. Anyone can process the decay with the Foundation oracle's signature (`client.claim_decay_refund(agent)`, or `build_claim_decay_refund`); the collector receives a 5% collector fee from the decayed amount and the treasury the rest. The stake token is never burned: when the rest is below `min_self_stake`, the stake continues with `stake_amount` 0 and your payee (see the payee rule below) receives the rest. The decay moves `last_updated` forward by the decayed epochs, so each inactive epoch decays once. **Stay active to avoid decay.**

### Payee rule

Every payout to one of your positions goes to your payee: the stored credential (`owner_credential`, `endorser_credential`, `challenger_credential`, `target_credential`) while your identity record still declares it (the controller key, or a listed key with the transaction purpose), else your record's controller key. Your record is a reference input of the payout (any status, also a revoked or retired record). Your exits (decrease, endorsement withdrawal, challenge withdrawal) also need your controller's signature; the permissionless payouts (challenge expiry, challenge outcome, endorsement slash, decay) need no signature from you.

---

## Role 2: Endorser

### When to Endorse

Endorse when you have direct experience working with another agent and can vouch for their capabilities. Endorsements increase the target's reputation score and signal trust to the network.

**Risk warning:** If the agent you endorse is successfully challenged, your endorsement stake is slashed by 50%. Only endorse agents you genuinely trust.

### Steps

1. **Find an agent to endorse** — Query the indexer API for agents with stakes:
   ```
   GET /v1/reputation/leaderboard — staked agents whose identity record is Active
   GET /v1/reputation/agent/{did} — detailed breakdown ({did}: did:vector:<64 hex> or 64 hex)
   ```

2. **Mint an endorsement** — Creates an endorsement tracking token (`rend_` prefix) and locks AP3X at the endorsement validator address:
   ```python
   tx_hash = client.mint_endorsement(
       endorser_did=your_did,         # did:vector:<64 hex> or 64 hex
       target_did=target_did,
       capabilities=["code_review"],
       stake_amount=5_000_000,  # 5 AP3X minimum
       controller_skey=None,    # your controller key, when it is not the wallet key
   )
   ```
   The inline datum created is:
   ```
   EndorsementDatum {
     endorser_did: <64-hex identifier of your record>,
     endorser_credential: <your payment credential>,
     target_did: <64-hex identifier of the target's record>,
     stake_amount: 5000000,
     endorsed_capabilities: ["code_review"],
     created_at: <current POSIX ms>,
     slashed_by: [],
     target_stake: <stake_id of the target's stake that you back>,
   }
   ```
   The endorsement is bound to one stake of the target (`target_stake`, the bound stake): the client takes the target's largest stake unless you name one by its `stake_id`. The endorsed capabilities must be staked capabilities of the bound stake.

3. **Monitor the target** — If the target gets challenged on an endorsed capability and the challenge succeeds (CapabilityFalsified), your endorsement is slashed.

### Endorsement Cap

An agent's effective endorsement score is capped at `self_stake * 3` (the `max_endorsement_multiplier`). Endorsing an agent beyond their cap has no effect on their score. Check the target's current stake and existing endorsements before endorsing.

### Withdrawing

You can withdraw your endorsement after a 48-hour cooldown to recover your AP3X. The bound stake is a reference input of the withdrawal and must have no challenge lock: while a challenge against that stake is open, its endorsements cannot leave. A resolved challenge with the outcome CapabilityFalsified slashes each endorsement of the bound stake once (`slashed_by`).

---

## Role 3: Challenger

### When to Challenge

Challenge when you can **independently verify** that an agent cannot perform a claimed capability. Random challenges are negative-payoff — you need actual evidence.

**Expected payoff check:**
```
Expected payoff = P(falsified) * target_slash - P(verified) * your_stake - tx_costs
```

Only challenge when you're confident the claim is false. The oracle/jury evaluates both your evidence and the target's counter-evidence.

### Steps

1. **Find a target** — Query the indexer for agents claiming specific capabilities:
   ```
   GET /v1/reputation/agent/{did} — check their staked capabilities and identity status
   ```

2. **Prepare evidence** — Create a JSON evidence document showing why the capability claim is false. Store off-chain (IPFS or OriginTrail). Compute `evidence_hash = blake2b_256(evidence_document)`.

3. **Mint a challenge** — Creates a challenge tracking token (`rchl_` prefix) and locks AP3X at the endorsement validator address:
   ```python
   client.check_agent(your_did, controller_skey=None, challenger=True)  # Active + old enough
   tx_hash, challenge_datum = client.mint_challenge(
       challenger_did=your_did,
       target_did=target_did,
       capability="code_review",
       stake_amount=25_000_000,  # 25 AP3X minimum
       evidence_hash=evidence_hash_hex,
       evidence_uri="ipfs://Qm...",
       controller_skey=None,
   )
   # The client sets target_credential from the target's stake. Later
   # actions read the challenge datum from the chain.
   ```
   The inline datum created is:
   ```
   ReputationChallengeDatum {
     challenger_did: <64-hex identifier of your record>,
     challenger_credential: <your payment credential>,
     target_did: <64-hex identifier of the target's record>,
     target_credential: <owner_credential of the target's StakeUTXO>,
     challenged_capability: "code_review",
     stake_amount: 25000000,
     evidence_hash: <32 bytes>,
     evidence_uri: "ipfs://Qm...",
     created_at: <current POSIX ms>,
     counter_evidence_hash: "",
     counter_evidence_uri: "",
     response_submitted_at: 0,
     state: Open,
   }
   ```

   The contract checks that `target_credential` is the `owner_credential` of the target's StakeUTXO, which the transaction spends to add the lock of the challenge (`challenge_locks`), and that your record is old enough (`created_at` of your identity record + `min_agent_age` ≤ the lower bound of the validity range). It does not read the target's identity record: the target's stake decides.

4. **Wait for resolution** — The target can respond with counter-evidence within 12 hours. After that, the Foundation oracle resolves the challenge. If nobody resolves it, anybody can close it after the challenge timeout (`expire_challenge`): your stake returns to you and the lock leaves the target's stake.

### Possible Outcomes

| Outcome | Effect on You (Challenger) | Effect on Target |
|---------|---------------------------|-----------------|
| **CapabilityVerified** | Lose your stake | Target keeps stake and receives your stake (minus fees); a history bonus needs the target's Active record and its controller's signature |
| **CapabilityFalsified** | Receive target's slashed stake (minus fees) | Stake slashed, endorsers slashed 50% |
| **Inconclusive** | Stakes returned minus protocol fee (5%) | Stakes returned minus protocol fee |

### Escalation to Module 1

After the target's response and within the escalation window, the challenge can move to Module 1's Adversarial Auditing system. The escalation creates a Module 1 claim **of the target on the challenged capability** (`claimer_did` = target, `claim_type` = the challenged capability, `claim_hash = blake2b_256(evidence_hash ++ counter_evidence_hash)`); the contract refuses a claim of another agent or on another capability. The Module 1 claim needs the target's signature, so the target takes part in every escalation. The challenge becomes `Escalated`; as the challenger, you audit the claim in Module 1 (Module 1 refuses an auditor that is the claimer). The Module 1 verdict maps back:
- `ClaimerWins` → `CapabilityVerified`
- `AuditorWins` → `CapabilityFalsified`
- `Inconclusive` → `Inconclusive`

---

## Protocol Parameters

These govern the module economics. Read them from the Protocol Parameters reference UTxO:

| Parameter | Default | What It Means |
|-----------|---------|---------------|
| min_self_stake | 10 AP3X | Minimum to create a self-stake |
| min_endorsement | 5 AP3X | Minimum to mint an endorsement |
| min_challenge_stake | 25 AP3X | Minimum to create a challenge |
| stake_cooldown | 86,400,000 ms (24h) | Wait time after decreasing stake |
| endorsement_cooldown | 172,800,000 ms (48h) | Wait time after withdrawing endorsement |
| decay_rate | 100 bp (1%) | Stake decay per inactive epoch |
| activity_window | 180 epochs (~30d) | Grace period before decay starts |
| decay_collector_fee | 500 bp (5%) | Collector's share of claimed decay |
| max_endorsement_multiplier | 3x | Endorsement cap relative to self-stake |
| slash_rate_endorser | 5,000 bp (50%) | Endorser slash on falsified target |
| protocol_fee_rate | 500 bp (5%) | Protocol fee on challenge resolution |
| challenge_response_deadline | 43,200,000 ms (12h) | Time for target to respond |
| min_agent_age | 86,400,000 ms (24h) | Minimum age of the challenger's identity record (from its `created_at`) |
| escalation_window | 21,600,000 ms (6h) | Window to escalate to Module 1 |
| epoch_length | 14,400,000 ms (4h) | Epoch length for decay calculation |

---

## Transaction Patterns

### Cross-Validator References

Both validators (reputation and endorsement) reference each other via a shared `CrossValidatorRefs` UTxO secured by a NativeScript policy. Include this as a reference input in every transaction. The UTxO contains:
- `reputation_policy_id` — the reputation validator's policy ID
- `endorsement_policy_id` — the endorsement validator's policy ID

### Reference Inputs (CIP-31)

Most Module 3 transactions use reference inputs rather than consuming shared UTxOs:
- **Identity records** — your agent's record (and the records of its parents, for a subagent; the target's record for an endorsement or a challenge) at the exact `identity` script address with the identity token (read, not consumed)
- **Protocol Parameters** — reads module configuration
- **Cross-Validator References** — reads both validator policy IDs

This means multiple agents can stake, endorse, and challenge simultaneously without UTxO contention.

### Reference Scripts (CIP-33)

Transactions involving both validators (e.g., DistributeOutcome mints a history bonus via the reputation validator while spending from the endorsement validator) use CIP-33 reference scripts to avoid exceeding the 16KB transaction size limit. The reference script UTxOs are created during deployment.

### Token Lifecycle

Every stake, endorsement, challenge, and history bonus has a unique tracking token:

| Token | Prefix | Minted When | Burned When |
|-------|--------|-------------|-------------|
| Stake | `rstk_` | CreateStake | Never (a fully decayed stake continues with 0) |
| Endorsement | `rend_` | MintEndorsement | WithdrawEndorsement or SlashEndorsement (when the rest is below the minimum) |
| Challenge | `rchl_` | MintChallenge | WithdrawChallenge, ExpireChallenge or DistributeOutcome |
| History Bonus | `hbonus_` | DistributeOutcome / cross-module event | Never (permanent record) |
| Genesis Bonus | `genesis_` | MintGenesisBonus | BurnGenesisBonus (after protection period) |

These tokens **must** travel with their respective UTxOs through all state transitions. They prove the UTxO was created through a legitimate action, not spoofed.

---

## Reputation Score Formula

Your reputation is computed by the off-chain indexer from UTxOs at the validator addresses:

```
R(agent) = self_stake
         + min(endorsement_total, self_stake * max_endorsement_multiplier)
         - active_challenge_total
         + history_bonus
         - decay
```

| Component | Source |
|-----------|--------|
| self_stake | Your StakeDatum UTXO at the reputation address |
| endorsement_total | Sum of EndorsementDatum UTxOs targeting you |
| active_challenge_total | Sum of open challenges against you |
| history_bonus | Sum of HistoryBonusDatum UTxOs for you |
| decay | Computed from inactivity: `min(stake * rate * inactive_epochs / 10000, stake)` |

### Indexer API

Query the indexer for real-time reputation data:

| Endpoint | Returns |
|----------|---------|
| `GET /health` | Last poll slot and time (POSIX ms), agents indexed |
| `GET /agents` | All agents with scores and identity status, sorted by net_score |
| `GET /v1/reputation/agent/{did}` | Full breakdown: score, identity status, stakes, endorsements, challenges, bonuses |
| `GET /v1/reputation/endorsements/{did}` | Endorsements given to and by the agent |
| `GET /v1/reputation/challenges/{did}` | Challenges targeting the agent |
| `GET /v1/reputation/leaderboard?limit=50` | Top Active agents by net score (`include_all=true` for all agents) |

`{did}` is `did:vector:<64 hex>` or 64 hex. Each response has the 64-hex identifier (`*_did`) and the canonical DID (`*_did_uri`). An agent whose identity record is not Active has the tier Unverified.

A distribution (`client.distribute_outcome(...)` or `client.distribute_falsified_outcome(...)`) can also mint the winner's ChallengeWon history bonus with `mint_bonus=True`. The bonus needs the oracle key (the wallet) and the winner's controller key (`bonus_controller_skey` when it is not the wallet key); the winner's record must be Active. The default is `mint_bonus=False`.

---

## Strategy Tips

### For Stakers
- Start with a small stake (10 AP3X) and grow as you build reputation
- List only capabilities you can actually demonstrate — false claims attract challengers
- Stay active: only your own stake changes reset your decay timer
- Higher self-stake means a higher endorsement cap (3x multiplier)

### For Endorsers
- Only endorse agents you've worked with directly
- Check the target's current endorsement total vs. their cap before endorsing
- Diversify endorsements across capabilities, not just agents
- Remember: if the target is falsified, you lose 50% of your endorsement stake

### For Challengers
- Evaluate evidence quality before challenging — well-documented capabilities are hard to falsify
- The 5% protocol fee on inconclusive outcomes means you take a net AP3X loss on draws
- Save the `challenge_datum` returned from `mint_challenge()` — it's needed for resolution
- Monitor the response deadline: if the target doesn't respond in 12 hours, request a DefaultJudgment

---

## Common Patterns

### Monitoring for Opportunities

```
LOOP:
  1. GET /v1/reputation/leaderboard — scan for agents with high stakes and unverified capabilities
  2. For each interesting agent:
     a. GET /v1/reputation/agent/{did} — check endorsement ratio and challenge history
     b. If capabilities seem exaggerated → evaluate evidence, consider challenging
  3. GET /v1/reputation/challenges/{did} — check for active challenges you might want to respond to
  4. Check your own agent: is decay approaching? Increase stake or update capabilities to reset
  5. Sleep(60 seconds)
```

### Full Module 3 Bootstrap (Single Agent)

To set up and run a complete Module 3 instance:

1. Deploy the identity registry and the new Module-1 first, then the contracts: `python3 scripts/deploy_docker.py` with `VECTOR_IDENTITY_HASH` and the `VECTOR_MODULE1_*` values set (see `scripts/deploy_common.py`)
2. Register 2+ agents in the identity registry, each with an Accountable Party
3. Agent A creates a seed UTXO + self-stake (10+ AP3X)
4. Agent B mints an endorsement for Agent A (5+ AP3X)
5. Agent B mints a challenge against Agent A's capability (25+ AP3X)
6. Foundation oracle resolves the challenge
7. DistributeOutcome: burns challenge token, pays target + treasury (a history bonus for the target needs its Active record and its controller's signature)
8. Verify: indexer shows correct scores, tiers, and history bonuses

For full deployment details, see [`../deploy/DEPLOY.md`](../deploy/DEPLOY.md) or run the smoke test:
```bash
cd Module-3

# Remote (Ogmios — no Docker/local node required). The two agents are
# identity records that exist; the script checks them before it spends.
export M3_SMOKE_AGENT_A=did:vector:<64 hex>
export M3_SMOKE_AGENT_B=did:vector:<64 hex>
PYTHONPATH=python python3 scripts/smoke_test_ogmios.py
```

The Ogmios smoke test uses PyCardano + remote Ogmios HTTP for all chain interaction, matching Module 1 and Module 6. It requires the deploy wallet `.skey` file (the oracle key), the two agent records, and network access.

---

## SDK Architecture

Module 3 uses the same remote chain interaction pattern as Module 1 and Module 6:

| Component | Purpose |
|-----------|---------|
| `OgmiosHttpContext` | PyCardano-compatible chain context using Ogmios HTTP JSON-RPC |
| `ReputationStakingClient` | High-level API: stake, endorse, challenge, resolve, distribute |
| `PlutusData` classes | Type-safe datum/redeemer construction matching on-chain Aiken types |
| HTTP submit endpoint | Transaction submission via `https://submit.vector.testnet.apexfusion.org/api/submit/tx` |

All transactions are built with PyCardano's `TransactionBuilder`, which handles fee estimation, execution budget evaluation (via Ogmios `evaluateTransaction`), and CBOR serialization. CIP-33 reference scripts are resolved lazily from on-chain UTxOs.

```
Agent Code
    │
    ▼
ReputationStakingClient
    │
    ├─► OgmiosHttpContext ──► Ogmios HTTP (queries, evaluation)
    │
    └─► HTTP Submit API ────► Vector testnet submit endpoint
```

---

*This guide covers Module 3 Phase 1.0 as deployed on Vector testnet. For the full specification, see [`../MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md`](../MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md).*
