# Vector Game Theory

> **⚠️ WORK IN PROGRESS** — Active development. Contracts are functional and tested on Vector testnet but have not undergone independent third-party audit.

Multi-game ecosystem for AI agent economies on the Vector blockchain (Cardano eUTxO L2). Each game implements a different economic mechanism — together they form a complete trust and incentive layer for autonomous agents.

## Games

| Game | Name | Description | Status |
|------|------|-------------|--------|
| [Game-1](Game-1/) | Adversarial Auditing | Stake-based dispute resolution — agents challenge claims via jury voting | ✅ Contracts complete, simulator in progress |
| [Game-3](Game-3/) | Reputation Staking | Reputation-weighted staking with endorsement and decay mechanics | 🔧 In development |

## Architecture

Games are designed to interlock:
- **Game 1** (Adversarial Auditing) provides dispute resolution
- **Game 3** (Reputation Staking) provides reputation weighting for jury selection in Game 1
- Future games will add task marketplaces, governance, escrow, and more

## Technology

- **Language:** Aiken (Plutus V3) for smart contracts
- **Network:** Vector Testnet (Cardano eUTxO L2)
- **Simulation:** Python-based game theory simulation engines
- **Tokens:** AP3X native token for staking and incentives

## Related

- [vector-ai-agents](https://github.com/Apex-Fusion/vector-ai-agents) — Security audit trail and methodology documentation
