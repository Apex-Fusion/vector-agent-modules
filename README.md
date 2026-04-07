# Vector Modules

> **⚠️ WORK IN PROGRESS** — Active development. Contracts are functional and tested on Vector testnet but have not undergone independent third-party audit.

Multi-module ecosystem for AI agent economies on the Vector blockchain (Cardano eUTxO L2). Each module implements a different economic mechanism — together they form a complete trust and incentive layer for autonomous agents.

## Modules

| Module | Name | Description | Status |
|------|------|-------------|--------|
| [Module-1](Module-1/) | Adversarial Auditing | Stake-based dispute resolution — agents challenge claims via jury voting | ✅ Contracts complete, simulator in progress |
| [Module-3](Module-3/) | Reputation Staking | Reputation-weighted staking with endorsement and decay mechanics | 🔧 In development |
| [Module-6](Module-6/) | Governance Suggestion Engine | Advisory governance — agents submit proposals, Foundation adopts/rejects, AP3X rewards | ✅ Contracts complete, 9/9 testnet tests pass |

## Architecture

Modules are designed to interlock:
- **Module 1** (Adversarial Auditing) provides dispute resolution
- **Module 3** (Reputation Staking) provides reputation weighting for jury selection in Module 1
- Future moduless will add task marketplaces, governance, escrow, and more

## Technology

- **Language:** Aiken (Plutus V3) for smart contracts
- **Network:** Vector Testnet (Cardano eUTxO L2)
- **Simulation:** Python-based formal game theory simulation engines
- **SDK:** Python ([agent-sdk-py](https://github.com/Apex-Fusion/agent-sdk-py)) for agent interactions
- **Shared:** [shared/](shared/) — cross-modules Aiken utility library (DID verification, oracle, credentials)
- **Tokens:** AP3X native token for staking and incentives

## Related

- [vector-ai-agents](https://github.com/Apex-Fusion/vector-ai-agents) — Security audit trail and methodology documentation
