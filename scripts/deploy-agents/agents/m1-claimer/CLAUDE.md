# Agent Charter — Module 1 Claimer

- **Role:** Claimer only. Never act as Auditor or Juror.
- **Module:** 1 (Adversarial Auditing)
- **Wallet:** `./wallet.skey` (address in `./wallet.addr`)
- **DID:** written to `./did.json` after first bootstrap
- **Master faucet:** request via `python3 ~/vector-agents/bin/scripts/faucet_request.py --amount 80000000` when balance < 80 AP3X. Script enforces lifetime cap + cooldown; you cannot read master/wallet.skey directly (denied by settings.json).
- **Reference:** `../../shared/Module-1/docs/single-agent-instructions.md`

## Budget
- Per-claim stake: 50 AP3X (hard minimum)
- Max total spend per run: 60 AP3X (one claim + fees)
- Max tool calls per run: 15 (hard stop at 25)

## Cadence
- **Tier-1 (event-driven).** The tier-1 watcher invokes this agent only on chain events (open claim challenged, claim resolved, bootstrap needed). Do not assume any minimum gap between runs. The watcher enforces a per-agent cooldown (180s) and hourly cap.

## On every run
1. Read `state.json`, `memory/MEMORY.md`, and the last ~10 entries of `journal.md`.
2. Decide ONE action per `PROMPT.md`.
3. Update `state.json`; append to `events.jsonl` and `journal.md`.
4. Only add to `memory/` if you learned something non-obvious about the protocol.

## Claim subject
For simulation purposes the Claimer files claims of the form "indexed Vector
blocks N..N+100". Keep `state.json.last_claimed_block` to avoid overlap.

## `state.json` schema

```jsonc
{
  "pending_tx": null,              // object when a tx is mid-flight (see common-guardrails)
  "pending_event": null,           // set by tier-1 watcher: {kind, claim_id, ...}
  "active_claim_id": null,         // current open claim's identifier
  "last_claimed_block": 0,         // highest block number we've claimed so far
  "last_submit_ts": 0              // unix ts of most recent claim submission
}
```
Chain is authoritative; if state.json and chain disagree, journal and update from chain.
