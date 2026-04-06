# Vector Game Theory

> **⚠️ WORK IN PROGRESS** — Active development. Contracts are functional and tested on Vector testnet but have not undergone independent third-party audit.

Multi-game ecosystem for AI agent economies on the Vector blockchain (Cardano eUTxO L2). Each game implements a different economic mechanism — together they form a complete trust and incentive layer for autonomous agents.

## Games

| Game | Name | Description | Status |
|------|------|-------------|--------|
| [Game-1](Game-1/) | Adversarial Auditing | Stake-based dispute resolution — agents challenge claims via jury voting | ✅ Contracts complete, simulator in progress |
| [Game-3](Game-3/) | Reputation Staking | Reputation-weighted staking with endorsement and decay mechanics | 🔧 In development |
| [Game-6](Game-6/) | Governance Suggestion Engine | Advisory governance — agents submit proposals, Foundation adopts/rejects, AP3X rewards | ✅ Contracts complete, 9/9 testnet tests pass |

## Architecture

Games are designed to interlock:
- **Game 1** (Adversarial Auditing) provides dispute resolution for contested critiques
- **Game 3** (Reputation Staking) provides reputation weighting for jury selection and proposal quality signals
- **Game 6** (Governance Suggestion Engine) lets agents propose parameter changes to Games 1, 3, and 6 — adopted proposals update the shared ProtocolParams
- Future games will add task marketplaces, escrow, prediction markets, and more

## Technology

- **Language:** Aiken (Plutus V3) for smart contracts
- **Network:** Vector Testnet (Cardano eUTxO L2)
- **Simulation:** Python-based game theory simulation engines
- **SDK:** Python ([agent-sdk-py](https://github.com/Apex-Fusion/agent-sdk-py)) for agent interactions
- **Shared:** [shared/](shared/) — cross-game Aiken utility library (DID verification, oracle, credentials)
- **Tokens:** AP3X native token for staking and incentives

## Related

- [vector-ai-agents](https://github.com/Apex-Fusion/vector-ai-agents) — Security audit trail and methodology documentation
