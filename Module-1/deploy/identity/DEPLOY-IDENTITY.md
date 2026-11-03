# Module deployment for the identity registry release

This runbook gives the module steps of the release (Part 2 §11 step 2 of
`docs/IDENTITY_REGISTRY.md` in agents-marketplace). Do the identity steps
first, with the agents-marketplace deploy tool. Then do the steps below in
this order.

**Status (2026-11-03).** Module-1, the seal, Module-3 and Module-6 wait for
the answer of the module owners on the Module-1 mode and the verdict policy.
Run only dry runs until then. Do not use `--submit`, `build` or `submit`.

## 1. Rules

- A dry run is the default. A dry run builds each transaction, evaluates it
  with Ogmios `evaluateTransaction` and prints the costs. It signs and sends
  nothing.
- The tools read the operator key by path. Give `--vkey` for a dry run and for
  `build`. Give `--skey` only for `sign` and `run --submit`. Do not copy the
  key file. Do not print it.
- The tools take inputs only from the operator address. They never spend the
  config seed and never spend an output at the seal-seed address.
- Each tool writes a state file in `deploy/identity/state/<network>/` of its
  module (not in git). A tool refuses every file that is not a state file of
  its own format, so it cannot write the state of an old deployment.
- A run can resume. A step that the chain already shows is skipped and
  recorded.
- Offline signing: `build` writes unsigned transactions to
  `<state>/tx/NN-<step>.unsigned.json`. Copy the state directory to the
  signing computer. Run `sign --skey <key>` there (no network). Copy the
  directory back and run `submit`. A built transaction is valid for 6 hours.
- Use the same commands with `--network testnet` for Vector testnet. Both
  networks use addresses with network id 1.

Python: a venv with `pycardano 0.19.2` and `cbor2 < 6`. `aiken` v1.1.21 must
be on PATH.

## 2. Inputs

| Input | Source | Read by |
|---|---|---|
| Identity `deployment.json` | agents-marketplace tool | all modules: `identity_hash`, `config_policy`, `config_holder`, `refs.identity`, `reference_address`, `operator_address`, `seal_seed_address`, `config_seed`, `seal`, `network` |
| Operator key | `operator.vkey` / `operator.skey` (by path) | all modules. The key must be the key of `operator_address`. |
| `Module-1/deploy/identity/config.json` | this repository | Module-1: `verdict_policy`, `protocol_params` (16 values) |
| `--mode oracle` or `--mode jury` | the decision of the module owners | Module-1: `oracle_active` |
| Module-1 manifest | `Module-1/deploy/identity/state/<network>/manifest.json` | the identity seal step (`hashes.identity_party`), Module-3, the Module-1 clients |

`config.json` still lists `verdict_policy` as a placeholder. Remove it from
`placeholders` only after the module owners confirm the policy. The plan
refuses placeholders unless `--allow-placeholders` is given; a sending run
always refuses them.

## 3. Command sequence

Set the paths once:

```sh
ID=<agents-marketplace>/contracts/identity-marketplace/deploy/state/mainnet/deployment.json
VKEY=~/.vector-keys/identity-2026-11/operator.vkey
SKEY=~/.vector-keys/identity-2026-11/operator.skey
MODE=<oracle|jury>          # the decision of the module owners
M1=Module-1/deploy/identity/state/mainnet/manifest.json
```

### Step 3 — Module-1 plan (after the identity reference scripts)

```sh
python3 Module-1/scripts/identity_deploy.py plan --mode $MODE --deployment $ID --vkey $VKEY
```

- Output: `Module-1/deploy/identity/state/mainnet/plan/plutus.json` (applied
  blueprint), `plan/plan.json` and the manifest `manifest.json`.
- Check: the printed `M1 (seal)` hash and the seal datum `581c<M1>`.
- The manifest has the identity values of `deployment.json`,
  `module1.oracle_vkh` (the operator key hash), `module1.refs_token_policy`
  (ScriptPubkey(operator)), `verdict_policy`, `protocol_params` (with
  `oracle_active` of the mode), `parameters`, `hashes` and `addresses`.
- The plan refuses to change the hashes of a manifest that records on-chain
  steps.

### Step 4 — the seal (agents-marketplace tool)

The seal step reads `hashes.identity_party` from `$M1`. It compares
`identity.*` with `deployment.json`.

### Step 5 — check the seal

```sh
python3 Module-1/scripts/identity_deploy.py check-seal --deployment $ID
```

It reads `seal.config_record` from the chain and compares its datum with the
identity_party hash of the plan. Exit status 1 means a mismatch: stop.

### Step 7 — Module-1 on-chain steps

```sh
python3 Module-1/scripts/identity_deploy.py deploy --mode $MODE --deployment $ID --vkey $VKEY
# then either: deploy --mode $MODE --deployment $ID --skey $SKEY --submit
# or offline:  deploy build --vkey $VKEY ... ; deploy sign --skey $SKEY ... ; deploy submit --vkey $VKEY ...
```

| Step | Transaction | Result in the manifest |
|---|---|---|
| `publish-claim`, `publish-challenge`, `publish-jury_pool`, `publish-identity_party` | reference script as output 0 at `reference_address` (unspendable) | `claim_ref`, `challenge_ref`, `jury_pool_ref`, `identity_party_ref` |
| `register-identity_party` | stake registration certificate 0 (no deposit field) of Script(M1); no script witness | `identity_party_registration` |
| `records` | mints 1 `auditing_refs` token of ScriptPubkey(operator); output 0 CrossValidatorRefs `[claim, challenge, jury_pool]` with the token, output 1 ProtocolParams, both at the Module-1 records holder `holder(11, operator)` | `cross_refs_utxo`, `params_utxo` |

- A sending run needs a plan without placeholders and the seal of M1 on chain.
- Registration: Conway accepts certificate 0 without a witness. Certificate 7
  (with deposit) needs the witness of Script(M1), and identity_party fails
  every publish purpose. A Vector testnet precedent of certificate 0 for a
  script credential: transaction `a033f4ec…7807`.
- Ogmios `rewardAccountSummaries` lists only delegated accounts. Script(M1)
  can never be delegated, so the tool checks the registration with Koios
  `account_info`. The Module-1 clients read the balance in the same way.
- The deposit (5 AP3X on mainnet, 500 AP3X on testnet) stays locked for
  ever: Script(M1) cannot be deregistered.

### Step 8 — Module-3

```sh
python3 Module-3/scripts/deploy_mainnet_ogmios.py --deployment $ID --module1-manifest $M1 --vkey $VKEY
# send: --skey $SKEY --submit, or build / sign / submit
```

| Step | Transaction | `tx_hashes` key |
|---|---|---|
| `records` | spends the record token seed; mints `params`; ProtocolParams record (output 0) at the params holder `holder(31, operator)` | `params_utxo` |
| `publish-reputation`, `publish-endorsement` | reference script as output 0 at `reference_address` | `reputation_spend_ref`, `endorsement_spend_ref` |
| `cross-refs` | mints 1 `reputation_refs` token of ScriptPubkey(operator); CrossValidatorRefs (output 0) at the params holder | `cross_refs_nft` |

- Inputs: `identity_hash` from `$ID`; `hashes.claim`, `hashes.challenge`,
  `hashes.jury_pool`, `module1.refs_token_policy` from `$M1`.
- State: `Module-3/deploy/identity/state/mainnet/deploy_state.json`. The
  Module-3 SDK reads it with `reputation_staking.deployment.load_deployment`.
- ReputationConfig has 12 fields with `identity_hash` last. The treasury
  holder is `holder(33, operator)`.
- A send (`run --submit`, `build`, `submit`) waits until the seal on chain
  names M1 of `$M1` (the check of Module-1 `seal_status`). A dry run works
  before the seal and prints the seal status.

### Step 9 — Module-6

```sh
python3 Module-6/scripts/deploy.py --deployment $ID --module1-manifest $M1 --vkey $VKEY
python3 Module-6/scripts/treasury_fund.py --vkey $VKEY --batches N --size AP3X   # more batches later
python3 Module-6/scripts/deploy_params.py --vkey $VKEY --params <values.json>       # replace the params record later
```

- Holders: params `holder(61, operator)`, oracle `holder(62, operator)`,
  treasury `holder(63, operator)`. GovernanceConfig has 11 fields as a
  definite-length CBOR array, with `identity_hash` last.
- A send of `deploy.py` needs `--module1-manifest` and waits until the seal
  names M1, as for Module-3. A dry run works before the seal.
- State: `Module-6/deploy/identity/state/mainnet/deploy_state.json`. It has a
  `client_settings` object with the values of the external clients.
- `smoke_test.py`, `test_expire.py`, `test_expire_e2e.py`, `update_params.py`
  and `treasury_replenish.py` read this state by default (`--state`,
  `--network`). They read the old `wallets/deploy_state.json` only with an
  explicit `--state`. `recreate_infra.py`, `redeploy_proposal.py` and
  `move_crossrefs.py` are for the old deployment only.

## 4. Client settings

| Client | Where it reads the deployment | Set |
|---|---|---|
| Module-1 simulation and tools | the manifest `$M1` (`DEPLOYMENT_PATH`) | the file |
| Module-3 SDK, indexer, dashboard | the Module-3 state (`load_deployment`) | the file |
| Module-6 scripts and dashboard | the Module-6 state (`GovernanceDeployment.from_state`, `MODULE6_DEPLOYMENT_FILE`) | the file |
| mcp-server | the 15 `GOV_*` environment values of its identity branch (the release branch); the `GOV_*_UTXO` values of `main` are not used | `client_settings["mcp-server"]` of the Module-6 state. The built-in defaults are the old deployment: set every value. |
| agent-sdk-py | `GovernanceDeployment.from_deploy_state` (the Module-6 state) or `VECTOR_MODULE6_*` | `client_settings["agent-sdk-py"]` of the Module-6 state |

No client computes a holder address from a tag: every client reads the holder
hashes or addresses from a state file or from its configuration.

## 5. Costs (dry runs on mainnet, 2026-11-03)

Measured with the mainnet protocol parameters from Ogmios (fee 45 lovelace
per byte + 156,253; 4,310 lovelace per UTxO byte; stake deposit 5 AP3X).
"Outputs" are the AP3X in outputs that do not go back to the operator. The
reference scripts stay locked for ever.

| Module | Fees | Deposit | Outputs | Total |
|---|---:|---:|---:|---:|
| Module-1 (either mode) | 2.753133 | 5.000000 | 172.085370 | 179.838503 |
| Module-3 | 1.951292 | 0 | 125.481820 | 127.433112 |
| Module-6 (3 treasury batches of 30 AP3X) | 3.800696 | 0 | 256.870990 | 260.671686 |
| **All modules** | **8.505121** | **5.000000** | **554.438180** | **567.943301** |

Module-1 transactions (both modes have the same sizes):

| Step | Bytes | Fee | Output |
|---|---:|---:|---:|
| publish-claim | 7,403 | 0.489523 | 31.734530 |
| publish-challenge | 12,593 | 0.723073 | 54.103430 |
| publish-jury_pool | 10,558 | 0.631498 | 45.332580 |
| publish-identity_party | 8,910 | 0.557338 | 38.229700 |
| register-identity_party | 236 | 0.167008 | deposit 5 |
| records | 629 | 0.184693 | 1.499880 + 1.185250 |

Fund the operator address with at least the total of the modules plus a
margin of about 10 AP3X for the change and the collateral.
