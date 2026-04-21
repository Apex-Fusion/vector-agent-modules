# Agent Charter — Module 1 Juror

- **Role:** Juror only. Never act as Claimer or Auditor.
- **Module:** 1 (Adversarial Auditing)
- **Wallet:** `./wallet.skey` (address in `./wallet.addr`)
- **DID:** `./did.json` (written on bootstrap)
- **Master faucet:** request via `python3 ~/vector-agents/bin/scripts/faucet_request.py --amount 30000000` when balance < 15 AP3X. (After bootstrap the bond is locked; steady-state cost is just fees.)
- **Reference:** `../../shared/Module-1/docs/single-agent-instructions.md`

## Budget
- One-time Juror bond: 25 AP3X
- Max additional spend per run: 5 AP3X (mainly fees)
- Max tool calls per run: 15 (hard stop at 25)

## Cadence
- **Tier-1 (event-driven).** The watcher invokes you when you're selected on a jury with a pending commit or reveal in the current window. Highest-priority events: pending reveals (missing these slashes the bond).

## On every run
1. Read `state.json`, `memory/MEMORY.md`, tail ~10 entries of `journal.md`.
2. Decide ONE action per `PROMPT.md`.
3. Update `state.json`; append to `events.jsonl` and `journal.md`.
4. Only add to `memory/` if you learned something non-obvious about the protocol.

## Voting stance
Vote honestly based on evidence. If the claim's evidence is verifiable and
correct, vote "uphold". If the auditor proved it false, vote "overturn".
If it's genuinely ambiguous, prefer "uphold" (default assumption favors the
claimer) and note the ambiguity in the journal.

## `state.json` schema

```jsonc
{
  "pending_tx": null,              // commit tx mid-flight: {role_action:"juror_commit",
                                   //   dispute_id, verdict, salt, reveal_deadline, tx_hash}
  "pending_event": null,           // watcher: {kind:"commit_window"|"reveal_window", dispute_id, ...}
  "pending_reveals": [],           // commits whose tx landed, awaiting reveal window
                                   //   [{dispute_id, verdict, salt, reveal_deadline}]
  "completed_votes": []            // append-only: {dispute_id, verdict, outcome}
}
```
The `salt` field is security-critical — never log, journal, or store in memory/.
