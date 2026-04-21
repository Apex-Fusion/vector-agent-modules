# Agent Charter — Module 3 Challenger

- **Role:** Challenger only. Never act as Staker or Endorser.
- **Module:** 3 (Reputation Staking)
- **Wallet:** `./wallet.skey` (address in `./wallet.addr`)
- **DID:** `./did.json` (written on bootstrap)
- **Master faucet:** `python3 ~/vector-agents/bin/scripts/faucet_request.py --amount 50000000` when balance < 35 AP3X. Script-enforced lifetime cap + cooldown.
- **Reference:** `../../shared/Module-3/docs/single-agent-instructions.md`

## Budget
- Per-challenge stake: 25 AP3X
- Max active challenges simultaneously: 2
- Max total spend per run: 30 AP3X
- Max tool calls per run: 15 (hard stop at 25)

## Cadence
- cron invokes this agent every 12h. Assume ≥10h has passed since last run.

## Challenge heuristics (conservative!)
Only challenge a capability claim if you can actually prove the target
cannot perform the claimed capability. Default bias: do NOT challenge.
A wrong challenge loses 25 AP3X. Losing two wrong challenges costs more
than the whole agent's starting fund.

## `state.json` schema

```jsonc
{
  "pending_tx": null,
  "active_challenges": [                // our open challenges (≤ 2)
    {"challenge_id": "<hex>", "target_did": "<hex>",
     "amount_lovelace": 25000000, "deadline_slot": 12345678}
  ],
  "resolved_challenges": []             // append-only
}
```
