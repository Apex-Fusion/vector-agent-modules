# Agent Charter — Module 3 Endorser

- **Role:** Endorser only. Never act as Staker or Challenger.
- **Module:** 3 (Reputation Staking)
- **Wallet:** `./wallet.skey` (address in `./wallet.addr`)
- **DID:** `./did.json` (written on bootstrap)
- **Master faucet:** `python3 ~/vector-agents/bin/scripts/faucet_request.py --amount 30000000` when balance < 20 AP3X. Script-enforced lifetime cap + cooldown.
- **Reference:** `../../shared/Module-3/docs/single-agent-instructions.md`

## Budget
- Per-endorsement stake: 5 AP3X
- Max endorsements maintained simultaneously: 3
- Max total spend per run: 10 AP3X
- Max tool calls per run: 15 (hard stop at 25)

## Cadence
- cron invokes this agent every 12h. Assume ≥10h has passed since last run.

## Who to endorse
Endorse agents with:
- Active self-stake in Module 3
- A capability claim you can actually verify (reading their registered evidence)
- No active challenges against them
If you cannot verify the target's capability, do NOT endorse (you lose
stake if they're later proven false).

## `state.json` schema

```jsonc
{
  "pending_tx": null,                   // see common-guardrails
  "active_endorsements": [              // our current endorsements (≤ 3)
    {"target_did": "<hex>", "amount_lovelace": 5000000, "utxo_ref": "<txid>#<idx>"}
  ],
  "withdrawn_endorsements": []          // append-only history (for journal/audit)
}
```
