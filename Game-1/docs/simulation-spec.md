# Game 1: Adversarial Auditing — Testnet Simulation Specification

**Author:** Jelisaveta  
**Date:** 2026-03-25  
**Version:** 1.0  
**Status:** Draft  

---

## 1. Purpose

Simulate a multi-agent economy playing Game 1 (Adversarial Auditing) on the Vector testnet. The simulation proves that the on-chain dispute mechanism works at scale, measures economic dynamics, and identifies emergent behaviors that unit tests cannot capture.

This is NOT a load test. It is an **economic simulation** — a population of agents with diverse strategies interacting through smart contracts, producing measurable outcomes that validate (or invalidate) the game-theoretic design.

---

## 2. Goals

1. **Lifecycle coverage:** Exercise all 13 validator actions on-chain in realistic sequences
2. **Economic dynamics:** Measure AP3X flow, accumulation patterns, and equilibrium
3. **Fraud detection:** Quantify how effectively the challenge mechanism catches dishonest claims
4. **Throughput:** Measure claims/hour and resolution time under realistic load
5. **Edge cases:** Discover timing-dependent, ordering-dependent, or concurrency-dependent bugs
6. **Parameter tuning:** Evaluate whether default protocol parameters (stake amounts, windows, fees) produce healthy dynamics

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    SimController                         │
│  (Python orchestrator — single process, async TX loop)   │
├──────────┬──────────┬──────────┬────────────────────────┤
│ AgentPool│ClaimGen  │Challenge │ OracleBot              │
│          │          │Engine    │                        │
├──────────┴──────────┴──────────┴────────────────────────┤
│                  MetricsCollector                         │
├─────────────────────────────────────────────────────────┤
│                  Vector Testnet                           │
│  (Ogmios + Submit API)                                   │
└─────────────────────────────────────────────────────────┘
```

### 3.1 Components

**SimController** — Main orchestration loop. Runs discrete time steps (one step per slot or per N slots). Each step:
1. Advance clock
2. Each agent decides its action (or no action) based on current state
3. Collect all actions, build TXs, submit to testnet
4. Wait for confirmation
5. Update world state
6. Record metrics

**AgentPool** — N simulated agents, each with:
- Wallet (address + signing key)
- DID (registered in Agent Registry)
- AP3X balance
- Strategy parameters (drawn from statistical distributions)
- Action history

**ClaimGenerator** — Decides when agents submit claims:
- Submission rate per agent: Poisson(λ) per epoch
- Claim validity: Bernoulli(p_honest) — honest claim or fraudulent
- Claim size (stake): drawn from LogNormal(μ, σ) bounded by [min_claim_stake, balance]

**ChallengeEngine** — Monitors open claims and decides challenges:
- Detection probability: each agent has p_detect drawn from Beta(α, β)
- Challenge decision: if agent detects fraud AND has sufficient stake AND is within window
- Auditor selection: random eligible agent (has stake, hasn't challenged this claim already)

**OracleBot** — Phase 1.0 Foundation oracle:
- Resolves challenges after a configurable delay
- Accuracy: Bernoulli(p_oracle_correct) — defaults to 0.95
- When wrong: creates unjust outcomes (measures system resilience)
- Resolution verdicts: ClaimerWins, AuditorWins, Inconclusive (weighted by evidence quality)

**MetricsCollector** — Records all events and computes statistics (see Section 6)

---

## 4. Agent Model

### 4.1 Agent Types (drawn from mixture distribution)

| Type | Fraction | p_honest | p_detect | Strategy |
|------|----------|----------|----------|----------|
| **Honest Worker** | 60% | 0.95 | 0.3 | Submits mostly valid claims, occasionally challenges obvious fraud |
| **Careful Auditor** | 20% | 0.90 | 0.8 | Submits valid claims, actively monitors and challenges |
| **Opportunist** | 15% | 0.50 | 0.5 | Mixes valid and fraudulent claims, challenges when profitable |
| **Adversary** | 5% | 0.10 | 0.2 | Mostly fraudulent claims, rarely challenges |

### 4.2 Agent Parameters

Each agent is initialized with:

```python
@dataclass
class SimAgent:
    id: int
    wallet: Wallet                     # pycardano signing key + address
    did: bytes                         # Agent Registry DID (32 bytes)
    agent_type: AgentType              # from mixture distribution
    p_honest: float                    # probability of submitting honest claim
    p_detect: float                    # probability of detecting a fraudulent claim
    ap3x_balance: int                  # current AP3X holdings
    claims_submitted: int = 0
    claims_won: int = 0
    claims_lost: int = 0
    challenges_filed: int = 0
    challenges_won: int = 0
    challenges_lost: int = 0
    total_earned: int = 0
    total_lost: int = 0
```

### 4.3 Decision Model

Each time step, each agent:

1. **Submit claim?** — Poisson draw. If firing:
   - Stake amount: `max(min_stake, min(LogNormal(μ=17, σ=0.5), balance * 0.3))`
   - Honest: Bernoulli(p_honest)
   - If honest: generate valid claim hash from real data
   - If fraudulent: generate random claim hash (no backing evidence)

2. **Challenge?** — For each open claim from OTHER agents:
   - Is claim fraudulent? Ground truth known to simulation (not to agents)
   - Detection: Bernoulli(p_detect) — agent's ability to detect fraud
   - If detected AND balance >= claim stake AND within challenge window:
     - Challenge with stake >= claim stake

3. **Withdraw?** — For each owned claim where challenge window expired:
   - If state == Open and window expired: WithdrawClaim

### 4.4 Population Initialization

```python
def create_population(n: int, seed: int) -> List[SimAgent]:
    rng = np.random.default_rng(seed)
    
    # Agent type mixture
    types = rng.choice(
        [AgentType.HONEST, AgentType.AUDITOR, AgentType.OPPORTUNIST, AgentType.ADVERSARY],
        size=n,
        p=[0.60, 0.20, 0.15, 0.05]
    )
    
    agents = []
    for i, agent_type in enumerate(types):
        p_honest = {
            AgentType.HONEST: rng.beta(19, 1),        # mean 0.95, tight
            AgentType.AUDITOR: rng.beta(9, 1),         # mean 0.90
            AgentType.OPPORTUNIST: rng.beta(5, 5),     # mean 0.50, wide
            AgentType.ADVERSARY: rng.beta(1, 9),       # mean 0.10
        }[agent_type]
        
        p_detect = {
            AgentType.HONEST: rng.beta(3, 7),          # mean 0.30
            AgentType.AUDITOR: rng.beta(8, 2),         # mean 0.80
            AgentType.OPPORTUNIST: rng.beta(5, 5),     # mean 0.50
            AgentType.ADVERSARY: rng.beta(2, 8),       # mean 0.20
        }[agent_type]
        
        agents.append(SimAgent(
            id=i,
            agent_type=agent_type,
            p_honest=np.clip(p_honest, 0.01, 0.99),
            p_detect=np.clip(p_detect, 0.01, 0.99),
            ap3x_balance=1_000_000_000,  # 1000 AP3X each (6 decimals)
        ))
    return agents
```

---

## 5. Simulation Parameters

### 5.1 Default Configuration

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `n_agents` | 50 | Enough for statistical significance, manageable on testnet |
| `n_epochs` | 100 | Each epoch = one decision round |
| `epoch_interval_seconds` | 60 | One decision round per minute |
| `claim_rate_per_agent` | 0.1 | ~5 claims per agent over 100 epochs |
| `initial_ap3x_per_agent` | 1,000,000,000 | 1000 AP3X (6 decimals) |
| `oracle_accuracy` | 0.95 | Oracle correct 95% of the time |
| `oracle_delay_epochs` | 3 | Oracle resolves 3 epochs after challenge |
| `random_seed` | 42 | Reproducibility |
| `challenge_window_ms` | 1_800_000 | 30 minutes (matches ProtocolParams) |
| `min_claim_stake` | 50_000_000 | 50 AP3X (matches ProtocolParams) |

### 5.2 Scenario Variations

Run multiple scenarios to explore parameter space:

| Scenario | Change | Tests |
|----------|--------|-------|
| **Baseline** | Default params | Normal operation |
| **High Fraud** | Adversary fraction = 30% | System under attack |
| **Perfect Oracle** | oracle_accuracy = 1.0 | Upper bound on fraud detection |
| **Slow Oracle** | oracle_delay = 10 epochs | Effect of delayed justice |
| **Low Stakes** | min_claim_stake = 10M | Economic incentive sensitivity |
| **Large Population** | n_agents = 200 | Scalability |
| **Stress** | claim_rate = 0.5, n_agents = 100 | Throughput limits |

---

## 6. Metrics

### 6.1 Per-Epoch Metrics

```python
@dataclass
class EpochMetrics:
    epoch: int
    claims_submitted: int
    claims_honest: int
    claims_fraudulent: int
    challenges_filed: int
    challenges_correct: int       # challenged a fraud
    challenges_incorrect: int     # challenged an honest claim
    oracle_resolutions: int
    oracle_correct: int
    oracle_incorrect: int
    withdrawals: int
    timeouts: int
    total_ap3x_staked: int
    total_ap3x_redistributed: int
    txs_submitted: int
    txs_confirmed: int
    txs_failed: int
    avg_tx_fee_ada: float
    epoch_duration_seconds: float
```

### 6.2 Aggregate Metrics (computed post-simulation)

| Metric | Formula | Target |
|--------|---------|--------|
| **Fraud Detection Rate** | challenges_correct / total_fraudulent_claims | > 0.7 |
| **False Accusation Rate** | challenges_incorrect / total_challenges | < 0.1 |
| **Honest Agent ROI** | (earned - lost) / initial_balance for honest agents | > 0 |
| **Adversary ROI** | (earned - lost) / initial_balance for adversaries | < 0 |
| **Gini Coefficient** | AP3X distribution inequality over time | Monitor |
| **Time to Resolution** | mean epochs from challenge to resolution | < oracle_delay + 2 |
| **Throughput** | confirmed TXs / wall clock time | Measure |
| **System Solvency** | sum(all_ap3x) == initial_total (conservation law) | Must be exact |

### 6.3 Statistical Analysis

- **Confidence intervals:** Bootstrap resampling (10,000 samples) on all aggregate metrics
- **Distribution fitting:** Fit agent AP3X balances to known distributions (normal, log-normal, Pareto) at each epoch
- **Markov transition matrix:** Empirical claim state transition probabilities
- **Convergence test:** Kolmogorov-Smirnov test on AP3X distribution between epoch 50 and 100 — if p > 0.05, system has reached equilibrium
- **Monte Carlo variance:** Run 10 simulations with different seeds, compute inter-run variance

---

## 7. Implementation Plan

### 7.1 Phase A: Infrastructure (1-2 sessions)

1. **Wallet factory:** Script to generate N wallets, register N DIDs in Agent Registry, fund each with AP3X
2. **TX builder library:** Reusable functions for all 13 validator actions (extracted from smoke tests)
3. **World state tracker:** Reads on-chain UTxOs to maintain current state of all claims, challenges, jurors

### 7.2 Phase B: Simulation Engine (2-3 sessions)

1. **Agent decision loop:** Per-agent decision function based on strategy parameters
2. **SimController:** Epoch loop with TX batching, confirmation, state update
3. **OracleBot:** Automated dispute resolution with configurable accuracy
4. **Error handling:** TX failures, timeouts, UTxO contention (retry with backoff)

### 7.3 Phase C: Metrics & Analysis (1-2 sessions)

1. **MetricsCollector:** Real-time event logging to JSON/CSV
2. **Analysis notebook:** Python script with matplotlib/seaborn for visualization
3. **Scenario runner:** CLI to run predefined scenarios with parameter overrides

### 7.4 Phase D: Execution & Report (1 session)

1. Run baseline scenario
2. Run 2-3 variant scenarios
3. Compile results into simulation report with charts
4. Identify parameter recommendations

---

## 8. Technical Considerations

### 8.1 Testnet Constraints

- **Faucet:** Need to fund N wallets with ADA. May need multiple faucet requests or one large funding TX.
- **AP3X distribution:** Mint additional AP3X tokens or distribute from existing 1M supply
- **Rate limiting:** Ogmios/submit API may throttle. Build in backoff.
- **UTxO contention:** Multiple agents can't spend the same UTxO. Batch carefully.
- **Slot timing:** 1 slot = 1 second on Vector testnet. TX confirmation ~20-30 seconds.

### 8.2 Concurrency Model

The simulation is NOT concurrent from the testnet's perspective — we submit TXs sequentially within each epoch. This avoids UTxO contention. Each epoch:

1. Collect all agent decisions
2. Sort by priority (challenges > claims > withdrawals)
3. Build TXs sequentially, each consuming the latest UTxO set
4. Submit batch
5. Wait for all confirmations
6. Update state

For the stress test scenario, we can experiment with parallel TX submission.

### 8.3 Reproducibility

- Fixed random seed for all stochastic components
- Deterministic agent ordering per epoch (sort by ID)
- All TX hashes logged for on-chain verification
- Full simulation state checkpointed every 10 epochs

### 8.4 Wallet Management

**Option A (simple):** Generate all N wallets from a single master seed using HD derivation. Fund from the deploy wallet.

**Option B (realistic):** Generate independent wallets. Fund via a setup TX that splits ADA and AP3X to N outputs.

Recommend Option B — it's how real agents would work.

---

## 9. Expected Outcomes

### 9.1 If Game 1 is well-designed:
- Honest agents accumulate AP3X over time (positive ROI)
- Adversaries lose AP3X over time (negative ROI)
- Fraud detection rate correlates with auditor population fraction
- System reaches economic equilibrium within ~50 epochs
- AP3X conservation law holds exactly (sum = constant)

### 9.2 If Game 1 has design flaws:
- Adversaries can profit (attack strategy ROI > 0)
- Honest agents avoid participation (rational exit)
- Gini coefficient diverges (wealth concentration)
- System doesn't reach equilibrium (oscillation or collapse)

### 9.3 What we learn either way:
- Optimal min_claim_stake for the population mix
- Whether 30-minute challenge window is sufficient
- Oracle error impact on system health
- Minimum auditor population needed for security

---

## 10. File Structure

```
workspace-apex/
├── simulation/
│   ├── sim_controller.py        # Main orchestration loop
│   ├── agent_pool.py            # Agent creation, decision logic
│   ├── tx_builder.py            # Reusable TX construction for all 13 actions
│   ├── oracle_bot.py            # Automated oracle resolution
│   ├── world_state.py           # On-chain state tracker
│   ├── metrics.py               # Event logging and analysis
│   ├── scenarios.py             # Predefined scenario configurations
│   ├── setup_wallets.py         # Wallet generation and funding
│   ├── analysis.py              # Post-simulation statistical analysis
│   ├── config.py                # Simulation parameters
│   └── README.md                # How to run
├── simulation-results/
│   ├── baseline/                # Results per scenario
│   ├── high-fraud/
│   └── ...
└── specs/
    └── game1-simulation-spec.md # This document
```

---

## Appendix A: AP3X Token Economics in Simulation

Starting supply: `n_agents * initial_ap3x_per_agent`

AP3X flows:
- **SubmitClaim:** agent → claim UTxO (locked)
- **OpenChallenge:** agent → challenge UTxO (locked)
- **WithdrawClaim (unchallenged):** claim UTxO → agent (full return)
- **OracleResolve (ClaimerWins):** both stakes → claimer (winner takes all)
- **OracleResolve (AuditorWins):** both stakes → auditor (via ForfeitClaim)
- **OracleResolve (Inconclusive):** both stakes returned minus jury fee
- **TimeoutResolve:** both stakes returned

Conservation law: sum of all AP3X (in wallets + locked in UTxOs) = constant at all times.

## Appendix B: Statistical Distributions Reference

| Parameter | Distribution | Params | Mean | Variance |
|-----------|-------------|--------|------|----------|
| Agent type | Categorical | [0.60, 0.20, 0.15, 0.05] | — | — |
| p_honest (Honest) | Beta | α=19, β=1 | 0.95 | 0.002 |
| p_honest (Auditor) | Beta | α=9, β=1 | 0.90 | 0.008 |
| p_honest (Opportunist) | Beta | α=5, β=5 | 0.50 | 0.023 |
| p_honest (Adversary) | Beta | α=1, β=9 | 0.10 | 0.008 |
| p_detect (Honest) | Beta | α=3, β=7 | 0.30 | 0.019 |
| p_detect (Auditor) | Beta | α=8, β=2 | 0.80 | 0.015 |
| p_detect (Opportunist) | Beta | α=5, β=5 | 0.50 | 0.023 |
| p_detect (Adversary) | Beta | α=2, β=8 | 0.20 | 0.015 |
| Claim rate | Poisson | λ=0.1/epoch | 0.1 | 0.1 |
| Stake amount | LogNormal | μ=17.7, σ=0.5 | ~50M | — |

---

*This specification will be refined as lifecycle tests reveal additional constraints and as the simulation implementation progresses.*
