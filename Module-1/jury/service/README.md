# Module-1 jury service

One process runs the relay of the Foundation (`SPEC.md` §12) and the reference jurors (`SPEC.md` §13).

- The relay opens a case for each genuine Module-1 challenge, signs `OracleResolve` with the result of the canonical case, applies `AuditorWins` verdicts, and cleans up: Finalize, slash, claim, close, `TimeoutResolve` and verdict reclaims.
- Each configured juror buys one model call per canonical case through the marketplace gateway, commits its vote, and reveals it with its record.

The code is in `Module-1/jury/vector_jury/`: `service.py` (the loop and the transaction queue), `relay.py`, `juror.py`, `gateway.py` and `input_template.py`.

## Files

| File | Use |
|---|---|
| `Dockerfile` | The image. Build it from the repository root. |
| `Dockerfile.dockerignore` | The files that the build reads. |
| `docker-compose.yml` | The service `module1-jury`. |
| `config.example.json` | The config for the container (paths in the image). |
| `config.dev.example.json` | The config for a run from the repository (paths relative to the file, dry run on). |
| `gateway-keys.env.example` | The names of the gateway API key variables, one for each juror. |

## How it works

- Each loop (`poll_seconds`, default 60) reads the chain once: the genuine `PendingOracle` challenges (§11.1) with the canonical case of each dispute (§11.2), the juror and case outputs of the followed deployments, and the verdict outputs with `deposit_to` = the operator address.
- The relay decides and submits its transactions. Then each juror gets its jobs: commits run in worker threads, because a model call takes minutes. Reveals run in the loop.
- The relay and the jurors use one key: the operator key is the fee payer, the oracle key, the owner of the Foundation jurors and the refund address. All transactions go through one queue. One transaction is sent at a time, and the queue waits until the chain shows its output (at most 3 minutes) before the next one. Commits, reveals, resolutions, `TimeoutResolve` and Open go first.
- The service reads the chain again on every loop, so a restart is safe. The transaction log of the relay and the state of each juror are in the state directory.

### Relay decisions (§12.2)

`D` is the challenge deadline. `now` is the time of the chain tip.

| Situation | Action |
|---|---|
| No case under §11.2 rules 1-2, and no followed deployment covers `challenged_at` | `OracleResolve(Inconclusive)`, basis `no-jury` |
| No case, `D - now >= 6 h 10 min` | Open a case in the deployment of `challenged_at` (no fee, 10 AP3X, refund to the relay), from `challenged_at + 30 min`, or at once when `D - now <= 6 h 40 min` |
| No case, `D - now < 6 h 10 min` | `OracleResolve(Inconclusive)`, basis `no-time` |
| Canonical case, `now >= reveal_end`, still `Open` | Finalize it |
| The Finalize transaction is 10 blocks deep | `OracleResolve(outcome)` with the resolution document (§10.5), `hi <= D`; for `AuditorWins`, then `ApplyVerdict` |
| Finalize failed twice | Resolve from the tally of the live case output (10 blocks deep), `finalized_in: null` |
| `now > D`, and no own `OracleResolve` can still land | `TimeoutResolve` |
| An open entry with `reveal_end <= now - 1 min` | Slash |
| A `Settling` case | Claim the shares of the Foundation jurors, then Close when it is allowed |
| An own case that is not canonical, after `reveal_end` | Finalize it |
| A verdict with `deposit_to` = the relay | Apply it (when the claimer's record lists Module-1 as a party); reclaim it after it lapses |

Rules that apply to every decision:

- The relay signs at most one `OracleResolve` for each challenge. A submission that the node refused does not count.
- The relay never signs an `OracleResolve` when `D` is less than 10 minutes away, and the upper bound of each `OracleResolve` is at or before `D`.
- Before each Module-1 transaction the relay checks that the claim and challenge outputs hold their `stake_amount`. It refuses on a mismatch.
- "No case" also counts the live cases that the indexer does not show yet, and an own Open of the last 30 minutes.
- The relay writes each transaction to its log (`relay/txlog.jsonl`, fsync) before it sends the transaction.

### Juror flow (§13)

1. For each canonical case in its commit window (in random order): check the eligibility, read the two documents and check their hashes, and build the model input (`vector-jury-input/1`, below).
2. Compute the record budget: 16,384 bytes minus the size of a reveal with an empty record, minus 113 bytes for each of 30 more ballots and 47 bytes for each open entry of the juror. The budget is at most 15,000.
3. Buy one model call through the gateway, pinned to the first supplier of the juror. On `429`, `409` or a `503` that comes before funding, wait as `Retry-After` says, then try the next supplier. Stop at the cut-off (3 minutes before `commit_end`). A request that may have funded an escrow counts as a paid attempt. The juror makes at most 2 paid attempts for one case. The count and the verified call are saved before anything else, so a restart never pays twice.
4. Check the signed receipt: the 9 fields, `agent_did`, `blake2b_224(public key) == supplier_pkh`, `supplier_pkh` and `escrow_ref`, the Ed25519 signature over the canonical JSON, and the two hashes (`prompt_hash` from the request, `response_hash` from the answer). The public key comes from the DID document of the supplier, else from its `/capability`.
5. Read the vote: the LAST JSON object in the output text with a valid `vote`. Every `{` starts a candidate; the candidate that ends last wins, so an outer object wins over the objects in it. Else the vote is `Inconclusive`.
6. Write the record (§10.4). It holds the request as the gateway hashed it (the normalized input items and the committed `max_output_tokens`). When the full response does not fit the budget, `response` is `{"cut": true, "output_text": …}`.
7. Make a salt (`secrets`), save the salt, vote and record (fsync, mode 600), then submit the commit.
8. In the reveal window, reveal with the record in the metadata. When the reveal is too large, first publish the record in a `juror-record` transaction, then reveal with a `juror-record-ref` document that holds the URI of that transaction.

A case with less than 5 minutes left before `commit_end` gets no model call.

## Model input `vector-jury-input/1`

The input is one JSON object (`json.dumps`, `ensure_ascii=False`, separators `(",", ":")`), with keys in this order:

```json
{
  "template": "vector-jury-input/1",
  "jury": "<J, hex>",
  "case": "<case token name, hex>",
  "challenge": "<challenge token name, hex>",
  "claim": "<claim token name, hex>",
  "claimer": "did:vector:<agent>",
  "auditor": "did:vector:<agent>",
  "deadline": "<challenge deadline, ISO 8601 UTC with milliseconds>",
  "documents": {
    "claim":    {"uri": "…", "hash": "<expected hash>", "status": "ok|missing|mismatch", "cut": false, "text": "…"},
    "evidence": {"uri": "…", "hash": "<expected hash>", "status": "ok|missing|mismatch", "cut": false, "text": "…"}
  }
}
```

- `hash` is the hash that the case holds. `status` is the result of the hash check. `text` is the document that was read (also on `mismatch`), or `""` when it is `missing`.
- Bytes are decoded as UTF-8; an invalid byte becomes U+FFFD. U+0000 is removed. Other control characters (except tab, line feed and carriage return) and DEL become U+FFFD. The text `\u0000` becomes `\u 0000`.
- `text` is the longest prefix with at most 3,500 bytes of UTF-8 whose size in the record (the text escaped as a JSON string twice) is at most 4,300 bytes. `cut` is true when the text is shorter than the document.
- `uri` is the longest prefix with at most 256 bytes of UTF-8 and a record size of at most 300 bytes.
- The two record-size caps keep every record at 15,000 bytes or less with any document, so the juror never skips a commit because of the record size. Plain text documents reach the 3,500-byte limit first. Pretty-printed JSON documents are cut at about 3,100 bytes.
- The prompts of the Foundation jurors name `documents.claim.text` and `documents.evidence.text` as untrusted data.

A change of these rules or of the layout needs a new template name.

## Configuration

`config.json` (format `vector-jury-service/1`; relative paths are relative to the config file):

| Key | Meaning |
|---|---|
| `network` | `mainnet` or `testnet` |
| `ogmios_url`, `koios_url` | Endpoint overrides (null: the network preset) |
| `module1_manifest` | The Module-1 deployment manifest |
| `jury_manifests` | The followed jury deployments, in order. Their windows `cases_from`/`cases_until` are in the manifests (§12.1). The relay needs at least one. |
| `operator_key` | The operator signing key file (cardano-cli JSON) |
| `operator_vkh` | Its key hash; the service checks the key, the Module-1 oracle key and the juror list owner against it |
| `state_dir` | The state directory |
| `poll_seconds` | The loop interval (at least 5) |
| `relay` | Run the relay (true/false) |
| `dry_run` | Build and evaluate only (see below) |
| `gateway_url` | The marketplace gateway |
| `jurors_file` | The juror list (`vector-jury-foundation-jurors/2`), for example `jurors/foundation/jurors.json` |
| `jurors` | The names of the jurors to run (null: all, `[]`: none) |

Each juror reads its gateway API key from the environment variable that `api_key_env` of the juror list names.

State directory:

| Path | Content |
|---|---|
| `commits/<network>/<J>/<case>/<juror token>.json` | Salt, vote and record of each commit attempt (mode 600) |
| `jurors/<juror>/<J>/<case>.json` | Paid attempts, the verified model call, the reveal and the overflow publication (mode 600) |
| `relay/txlog.jsonl` | The relay transaction log |

Back up the state directory. A juror that loses a salt cannot reveal, and its commitment is slashed.

## Operation

Set up the files next to `docker-compose.yml` (they are not in git):

```sh
cd Module-1/jury/service
cp config.example.json config.json
cp gateway-keys.env.example gateway-keys.env && chmod 600 gateway-keys.env   # fill in the keys
mkdir -p keys && cp /path/to/operator.skey keys/operator.skey
sudo chown 10001:10001 keys/operator.skey && sudo chmod 400 keys/operator.skey  # the container user
```

The deployment manifest (`Module-1/jury/deploy/mainnet/manifest.json`) must exist in the image: build the image after the deploy tool wrote it.

| Task | Command |
|---|---|
| Start | `docker compose up -d --build` |
| Stop | `docker compose stop` (the grace period lets a transaction confirm) |
| Logs | `docker compose logs -f module1-jury` (JSON lines; for example `… \| jq 'select(.event \| startswith("relay"))'`) |
| One loop, dry run | `docker compose run --rm module1-jury --config /config/config.json --once --dry-run` |
| Dry run from the repository | `PYTHONPATH=Module-1:shared/python:Module-1/jury python -m vector_jury.service --config Module-1/jury/service/config.dev.example.json --once --dry-run` |

A dry run (`--dry-run` or `"dry_run": true`) reads the chain, prints the decisions, and builds and evaluates the jury transactions. It submits nothing, makes no paid model call and writes no state. Without a readable key it reports the Module-1 transactions as decisions only. With a key, the Module-1 builders also need `APEX_NETWORK_CONFIRM=yes`.

A run that submits on mainnet needs `APEX_NETWORK_CONFIRM=yes` (the compose file sets it).

Log events: `service.*` (start, world, stop), `relay.loop`, `relay.note`, `relay.tx`, `relay.action_failed`, `juror.loop`, `juror.note`, `juror.gateway`, `juror.commit`, `juror.reveal`, `juror.contention`, `tx.unconfirmed`. Keys and API keys never appear in the logs.

### Add a juror

1. Register the juror DID with its prompt document (`vector-jury register`), with the operator key as owner. It can vote 3 days later.
2. Add it to `jurors/foundation/jurors.json` (format `vector-jury-foundation-jurors/2`): name, agent, model, ordered suppliers, prompt file and hash, `max_output_tokens` and `api_key_env`. The prompt file must hash to `prompt_hash`, and the registered juror output must hold the same hash.
3. Create a gateway API key for the juror, fund it, and add it to `gateway-keys.env` under its `api_key_env`.
4. Add its name to `jurors` in `config.json` (or keep `null` for all), rebuild and restart.

### Change the prompt of a juror

The registered juror output holds the hash of its prompt document, so a new prompt needs a new registration. One DID can have two juror outputs, and it gets one ballot per case (SPEC §6.9).

1. Register the juror DID again with the new prompt document. The new output can vote 3 days later.
2. Put the new prompt file and its hash in `jurors/foundation/jurors.json`, rebuild and restart. The juror then votes only with the output that holds the configured hash.
3. Send `RequestExit` for the old output. It cannot vote after this request. Take the bond back with `Exit` after the exit notice.
4. When the new outputs of the Foundation jurors can vote only after `cases_from`, move `cases_from` in the manifest to that time before it comes, and update the indexer (`JURY_DEPLOYMENTS`) and the service. A challenge before `cases_from` gets the basis `no-jury`, not a case without jurors.

### Follow a new deployment

1. Deploy the new validator (`deploy/jury_deploy.py`) and register the Foundation jurors in it.
2. When the jurors are eligible, publish the windows in the manifests: the new manifest gets `cases_from`, the old one gets `cases_until` (the same time, in the future).
3. Add the new manifest to `jury_manifests` (keep the old ones: the relay still finalizes, slashes, claims and closes there), rebuild and restart.
4. Remove an old manifest only after its last case is closed and the jurors left it.

## Spec choices

- Genuine challenges, canonical cases and "no case" use the library rules (`module1.genuine_challenges`, `state.canonical_for`, `state.dispute_cases`, `state.case_counts`).
- The relay resolves no-time and no-jury disputes only when the deadline is at least 10 minutes away; later, `TimeoutResolve` after the deadline returns the stakes.
- After a node took one `OracleResolve` of a challenge, the relay never signs another. When it expires without effect, `TimeoutResolve` follows after the deadline.
- A `503` from the gateway counts as "busy" only with a code that the gateway sends before it funds an escrow (`supplier_capacity_timeout`, `suppliers_unavailable`, `supplier_preflight_failed`, `supplier_identity_changed`, `wallet_transaction_pending`, `wallet_operation_timeout`). Other `5xx` answers and timeouts count as paid attempts.
- The record of a juror fits the reveal when its metadata (CBOR) adds at most the budget minus 64 bytes to the reveal.
