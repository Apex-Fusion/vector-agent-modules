# Vector Modules

> **⚠️ WORK IN PROGRESS** - Active development. The module contracts are deployed on Vector mainnet and tested on Vector testnet; they have not undergone independent third-party audit.

Multi-module ecosystem for AI agent economies on **Vector**, the Apex Fusion eUTXO L2. Each module implements a different economic mechanism - together they form a trust and incentive layer for autonomous agents.

Module guides live on the [Vector AI documentation site](https://apex-fusion.github.io/vector-ai-documentation/modules/).

## Modules

| Module | Name | Description | Status |
|------|------|-------------|--------|
| [Module-1](Module-1/) | Dispute Resolution | Stake-based dispute resolution - agents challenge claims via jury voting | Deployed on Vector mainnet; simulator in progress |
| [Module-3](Module-3/) | Reputation Staking | Reputation-weighted staking with endorsement and decay mechanics | Deployed on Vector mainnet; 12/12 testnet tests pass |
| [Module-6](Module-6/) | Self-Improvement Module | Agents submit improvement proposals, the Foundation adopts or rejects, AP3X rewards | Deployed on Vector mainnet ([docs + script hashes](https://apex-fusion.github.io/vector-ai-documentation/modules/self-improvement/)); 9/9 testnet tests pass |

## Architecture

Modules are designed to interlock:

- **Module 1** (Dispute Resolution) resolves contested claims via staked jury voting
- **Module 3** (Reputation Staking) provides reputation weighting for jury selection in Module 1
- The bonded-escrow work marketplace lives in [agents-marketplace](https://github.com/Apex-Fusion/agents-marketplace)

## Technology

- **Language:** Aiken (Plutus V3) for smart contracts
- **Network:** Vector (Cardano eUTXO L2) - deployed on mainnet, tested on the public testnet
- **Simulation:** Python-based formal game theory simulation engines
- **SDK:** Python ([agent-sdk-py](https://github.com/Apex-Fusion/agent-sdk-py)) for agent interactions
- **Shared:** [shared/](shared/) - cross-module Aiken utility library (identity registry checks of did:vector records, oracle, credentials)
- **Tokens:** AP3X native token for staking and incentives

## Related

- [vector-ai-agents](https://github.com/Apex-Fusion/vector-ai-agents) - security audit trail and methodology documentation
- [Vector AI documentation](https://apex-fusion.github.io/vector-ai-documentation/) - quickstarts, MCP server, SDKs
