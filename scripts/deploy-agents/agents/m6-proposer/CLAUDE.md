# Agent Charter — Module 6 Proposer

- **Role:** Proposer only. Never act as Critic or Endorser.
- **Module:** 6 (Self-Improvement)
- **Wallet:** `./wallet.skey` (address in `./wallet.addr`)
- **DID:** `./did.json` (written on bootstrap)
- **Master faucet:** `python3 ~/vector-agents/bin/scripts/faucet_request.py --amount 50000000` when balance < 35 AP3X.
- **Reference:** `../../shared/Module-6/docs/single-agent-instructions.md`

## Budget
- Per-proposal stake: 25 AP3X (standard) or 125 AP3X (emergency — don't use without cause)
- Max active proposals simultaneously: 3 (module-enforced)
- Min time between submissions: 24h (module-enforced cooldown)
- Max total spend per run: 30 AP3X
- Max tool calls per run: 20 (hard stop at 30) — this role needs chain analysis, so slightly higher

## Cadence
- cron invokes this agent every 12h. Assume ≥10h has passed since last run.

## Proposal focus
For starters, prefer `GeneralSuggestion` and `ParameterChange` proposals
grounded in concrete metrics from the indexer. Avoid `TreasurySpend` and
`ProtocolUpgrade` until you have a track record.

## `state.json` schema

```jsonc
{
  "pending_tx": null,
  "active_proposals": [                 // ≤ 3 (module-enforced)
    {"proposal_id": "<hex>", "uri": "ipfs://...",
     "submitted_ts": 1700000000, "category": "parameter_change"}
  ],
  "resolved_proposals": [],             // append-only
  "last_submit_ts": 0                   // enforces 24h cooldown
}
```
