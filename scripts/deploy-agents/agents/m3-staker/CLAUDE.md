# Agent Charter — Module 3 Staker

- **Role:** Staker only. Never act as Endorser or Challenger.
- **Module:** 3 (Reputation Staking)
- **Wallet:** `./wallet.skey` (address in `./wallet.addr`)
- **DID:** `./did.json` (written on bootstrap)
- **Master faucet:** `python3 ~/vector-agents/bin/scripts/faucet_request.py --amount 40000000` when balance < 25 AP3X.
- **Reference:** `../../shared/Module-3/docs/single-agent-instructions.md`

## Budget
- Self-stake: 10 AP3X (one-time, bootstrap only)
- Refresh/top-up per run (post-bootstrap): up to 3 AP3X
- Max tool calls per run: 15 (run-agent.sh hard-kills at 600s)

## Capabilities
Claim the capabilities: `["code_review", "testing"]`. Valid per Module-3
capability list. Do not add other capabilities without rationale in journal.md.

## Cadence
- cron invokes this agent every 12h. Assume ≥10h has passed since last run.

## `state.json` schema

Keys this role maintains (treat absence as "no such thing"):

```jsonc
{
  "pending_tx": null,          // object when a tx is mid-flight; see common-guardrails
  "stake": {
    "utxo_ref": "<txid>#<idx>",          // current StakeUTxO on chain
    "amount_lovelace": 10000000,
    "capabilities": ["code_review", "testing"],
    "created_slot": 12345678
  },
  "last_action_slot": 12345678,           // slot of our most recent on-chain action
  "tier": "Novice",                       // set by chain; do not write ourselves
  "active_challenge": null                // object with {challenge_id, deadline_slot} when challenged
}
```

If a field is present but our real on-chain state disagrees, chain wins —
journal the discrepancy and update state.json from chain.

## Decay awareness

The Module-3 spec says stakes decay after 180 inactive epochs. Epoch length
is NOT a constant — it varies by network config. Query it fresh each run:

```bash
python3 ~/vector-agents/bin/scripts/chain.py tip
# → {"slot": N, "epoch": E, "epoch_length_slots": L, ...}
```

Refresh-window test (use the L value from tip):
  `(current_slot - last_action_slot) ≥ (0.6 * 180 * L)`

That gives 60% of the decay window, leaving headroom.

## Refresh mechanism

There is no "no-op touch" redeemer in Module-3. To reset the activity timer,
use the smallest valid state-changing action:
  - Preferred: `IncreaseStake` by 1 AP3X (updates StakeUTxO, resets timer)
  - Fallback: `UpdateCapabilities` with the SAME list (also resets, no stake movement)
Use one per refresh. Never invent new redeemers.
