"""Module-6 deployment on the identity registry (Part 2 §7 of
docs/IDENTITY_REGISTRY.md in agents-marketplace).

Usage:

  python3 scripts/deploy.py [run|build|sign|submit] --deployment FILE
          (--vkey F | --skey F) [--module1-manifest FILE] [--submit]
          [--dry-run-funds AP3X] [--state-dir DIR] [--network mainnet|testnet]
          [--report F]

Inputs:
  --deployment   The identity deployment.json of the agents-marketplace tool
                 (format vector-identity-deployment/1). The tool reads
                 identity_hash, reference_address, operator_address,
                 seal_seed_address, config_seed and the seal. It checks the
                 file with the rules of Module-1 (scripts/identity_deploy.py).
  --vkey/--skey  The operator key: the key of operator_address. One operator
                 key deploys every module. It owns the holders, it is the
                 oracle credential, and its ScriptPubkey policy mints the
                 CrossRefs token "governance_refs".
  --module1-manifest
                 The Module-1 manifest.json (identity_deploy.py plan). A send
                 (run --submit, build, submit) needs it: the tool sends only
                 when the seal on chain names identity_party (M1) of the
                 manifest (Module-1 seal_status), and the manifest must name
                 the identity_hash and the network of the identity
                 deployment. A dry run works before the seal and without the
                 manifest; it prints the seal status.

The plan (on each run, without the network):
  1. The params, oracle and treasury holders: holder(61|62|63, operator)
     (shared/python/holder_scripts.py).
  2. refs_token_policy: ScriptPubkey(operator).
  3. The record token seed: the largest lovelace-only operator UTxO. The tool
     chooses it once and saves it before the first on-chain step. A resumed
     run uses the saved seed. In a dry run with --dry-run-funds, the virtual
     funds output is the seed. record_token_policy is record_token(seed).
  4. GovernanceConfig (11 fields, identity_hash last), applied with
     `aiken blueprint apply` to proposal_mint, proposal_spend, critique_mint,
     critique_spend and endorsement_spend in a temporary directory.

The steps (one transaction each; the tool skips a step when the chain shows
its result):
  records                 Spends the seed. Mints the params, oracle and
                          activity record tokens (record_token policy, unit
                          redeemer, collateral). Output 0: the
                          GovernanceParams record at the params holder.
                          Output 1: the oracle record at the oracle holder.
                          Output 2: the head of the activity list at the
                          proposal_spend address. Each output holds 3 AP3X,
                          or its min-UTxO when that is higher.
  publish-holder-params, publish-holder-oracle, publish-holder-treasury,
  publish-proposal_mint, publish-proposal_spend, publish-critique_mint,
  publish-critique_spend, publish-endorsement_spend
                          One reference script each, as output 0 at the
                          reference address of the identity deployment (the
                          all-zero key hash, unspendable), with the exact
                          min-UTxO.
  treasury-1, treasury-2, treasury-3
                          30 AP3X each at the treasury holder with
                          TreasuryBatchDatum(id, True) (output 0).
  cross-refs              Mints one "governance_refs" token of
                          ScriptPubkey(operator). Output 0 at the oracle
                          holder: the token and the 5-field
                          GovernanceCrossRefs.

Actions: "run" is a dry run (build each transaction, evaluate it with Ogmios
evaluateTransaction, send nothing), or a send with --submit (sign with
--skey). "build" writes unsigned transactions, "sign" signs them offline with
--skey, "submit" sends the signed ones. Sends wait for the seal of M1. The
tool never spends config_seed or an output at seal_seed_address of the
identity deployment.

State: DIR/deploy_state.json (default deploy/identity/state/<network>/;
format vector-module-deploy/1, module "module6"). The tool refuses every
other file, so it never writes the state of the previous deployment
(wallets/deploy_state.json). A dry run writes no state. The state has the
keys that scripts/governance_identity.GovernanceDeployment.from_state and
the Python SDK (GovernanceDeployment.from_deploy_state) read, the tx_hashes
of the steps, and client_settings: the environment values of the mcp-server
(GOV_*) and of the Python SDK (VECTOR_MODULE6_*). The tool fills each value
when it records the step that gives it.

The tools deploy_params.py (replace the params record) and treasury_fund.py
(more treasury batches) use the same state and the same runner
(shared/python/vector_deploy.py).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import cbor2
import requests

# deploy_common puts shared/python on sys.path (vector_deploy, holder_scripts).
from deploy_common import (
    ACTIVITY_TOKEN_NAME,
    BLUEPRINT_PATH,
    CRITIQUE_MINT,
    CRITIQUE_SPEND,
    DEFAULT_GOVERNANCE_PARAMS,
    ENDORSEMENT_SPEND,
    HOLDER_TAGS,
    ORACLE_TOKEN_NAME,
    PARAMS_TOKEN_NAME,
    PROPOSAL_MINT,
    PROPOSAL_SPEND,
    RECORDS_STEP,
    REFS_TOKEN_NAME,
    STATE_FILE,
    STATE_ROOT,
    UNIT_REDEEMER,
    VALIDATOR_TITLES,
    DeployInputError,
    apply_governance_config,
    apply_holder,
    apply_record_token_policy,
    build_activity_head_data,
    build_cross_refs_data,
    build_governance_config_cbor,
    check_operator_address,
    client_settings,
    current_params_entry,
    governance_params_data,
    load_identity_deployment,
    load_module1_manifest,
    module1_identity_deploy,
    oracle_datum_data,
    plutus_data_cbor,
    script_hash_to_address,
    state_tx_hashes,
    treasury_batch_data,
)

import vector_deploy as vd  # noqa: E402  (shared/python)
from pycardano import Address, PlutusV3Script, Redeemer, UTxO  # noqa: E402
from pycardano.serialization import RawCBOR  # noqa: E402

# ── Paths and constants ──────────────────────────────────────────────────────

MODULE = "module6"
# BLUEPRINT_PATH, STATE_ROOT and STATE_FILE come from deploy_common: the
# state of a deployment is STATE_ROOT/<network>/STATE_FILE,
# and STATE_ROOT/<network>/tx/ holds the transaction files of the offline
# flow. The state is not in git.

#: Lovelace of each record of the records step (or its min-UTxO when higher).
RECORD_LOVELACE = 3_000_000
#: The treasury batches of a deployment and their lovelace.
TREASURY_BATCHES = 3
TREASURY_BATCH_LOVELACE = 30_000_000

#: The steps of a deployment, in order.
STEP_NAMES = (
    RECORDS_STEP,
    "publish-holder-params",
    "publish-holder-oracle",
    "publish-holder-treasury",
    *(f"publish-{name}" for name in VALIDATOR_TITLES.values()),
    *(f"treasury-{batch_id}" for batch_id in range(1, TREASURY_BATCHES + 1)),
    "cross-refs",
)

#: The plan values that a resumed run must give again. A difference means
#: other inputs or other contracts.
PLAN_KEYS = (
    "identity_hash",
    "reference_address",
    "operator_address",
    "holder_owner_vkey_hash",
    "refs_token_policy",
    "record_token_seed",
    "record_token_policy",
    "holders",
    "governance_config_cbor",
    "validators",
)


def default_state_dir(network: str) -> Path:
    return STATE_ROOT / network


def lovelace(ap3x: float) -> int:
    return int(round(ap3x * vd.LOVELACE_PER_AP3X))


# ── Plan ─────────────────────────────────────────────────────────────────────


def applied_validators(blueprint: Mapping[str, Any]) -> dict:
    """{title: {"hash", "compiled_code"}} of the five validators of an applied
    blueprint. Each validator must have no open parameter, and its hash must
    be the hash of its compiled code."""
    found = {}
    for title in VALIDATOR_TITLES:
        entry = next((v for v in blueprint["validators"] if v["title"] == title), None)
        if entry is None or entry.get("parameters"):
            raise vd.DeployError(f"the applied blueprint has no {title}, or {title} has open parameters")
        code = entry["compiledCode"]
        if vd.script_hash_of(PlutusV3Script(bytes.fromhex(code))).hex() != entry["hash"]:
            raise vd.DeployError(f"the compiled code of {title} does not have the hash {entry['hash']}")
        found[title] = {"hash": entry["hash"], "compiled_code": code}
    return found


def module6_plan(identity: Mapping[str, Any], operator_key_hash: bytes, seed: str, *,
                 blueprint_path: Path = BLUEPRINT_PATH) -> dict:
    """The values of the deployment, with the keys of the deploy state: the
    holders, the policies, GovernanceConfig and the applied validators. Needs
    the aiken CLI; no network."""
    operator = bytes(operator_key_hash).hex()
    try:
        holders = {}
        for tag, role in HOLDER_TAGS:
            info = apply_holder(tag, operator)
            holders[role] = {"tag": tag, "hash": info["hash"], "address": script_hash_to_address(info["hash"]),
                             "compiled_code": info["compiled_code"]}
        seed_ref = vd.parse_ref(seed)
        record_token = apply_record_token_policy(bytes(seed_ref.transaction_id).hex(), seed_ref.index)
        refs_policy = vd.native_policy_id(vd.operator_native_script(operator_key_hash)).hex()
        config_cbor = build_governance_config_cbor(
            refs_token_policy=refs_policy,
            oracle_holder_hash=holders["oracle"]["hash"],
            params_holder_hash=holders["params"]["hash"],
            treasury_holder_hash=holders["treasury"]["hash"],
            record_token_policy=record_token["policy_id"],
            identity_hash=identity["identity_hash"],
        )
        validators = applied_validators(apply_governance_config(config_cbor, blueprint_path))
    except (RuntimeError, ValueError) as error:
        if isinstance(error, (vd.DeployError, DeployInputError)):
            raise
        raise vd.DeployError(f"the plan cannot be made: {error}") from error
    record_script = PlutusV3Script(bytes.fromhex(record_token["compiled_code"]))
    if vd.script_hash_of(record_script).hex() != record_token["policy_id"]:
        raise vd.DeployError("the record token policy does not have the hash of its compiled code")
    for role, info in holders.items():
        if vd.script_hash_of(PlutusV3Script(bytes.fromhex(info["compiled_code"]))).hex() != info["hash"]:
            raise vd.DeployError(f"the {role} holder does not have the hash of its compiled code")
    proposal_spend = validators[PROPOSAL_SPEND]["hash"]
    return {
        "identity_hash": identity["identity_hash"],
        "identity_deployment": {key: identity[key] for key in ("file", "sha256", "config_seed", "seal_seed_address")},
        "reference_address": identity["reference_address"],
        "operator_address": identity["operator_address"],
        "holder_owner_vkey_hash": operator,
        "refs_token_policy": refs_policy,
        "refs_token_name": REFS_TOKEN_NAME.decode(),
        "record_token_seed": seed,
        "record_token_policy": record_token["policy_id"],
        "record_token_compiled_code": record_token["compiled_code"],
        "holders": holders,
        "governance_config_cbor": config_cbor,
        "validators": validators,
        "proposal_validator_hash": proposal_spend,
        "critique_validator_hash": validators[CRITIQUE_SPEND]["hash"],
        "cross_refs_address": holders["oracle"]["address"],
        "activity_head": {
            "address": script_hash_to_address(proposal_spend),
            "record_token_policy": record_token["policy_id"],
            "token_name": ACTIVITY_TOKEN_NAME.decode(),
            "tx": None,
        },
        "governance_params": dict(DEFAULT_GOVERNANCE_PARAMS),
    }


def plan_labels(plan: Mapping[str, Any]) -> dict:
    holders = plan["holders"]
    return {
        plan["reference_address"]: "the reference address (unspendable)",
        holders["params"]["address"]: f"the Module-6 params holder (tag {holders['params']['tag']})",
        holders["oracle"]["address"]: f"the Module-6 oracle holder (tag {holders['oracle']['tag']})",
        holders["treasury"]["address"]: f"the Module-6 treasury holder (tag {holders['treasury']['tag']})",
        plan["activity_head"]["address"]: "the proposal_spend validator",
    }


def describe_plan(plan: Mapping[str, Any], *, seed_virtual: bool, log: Callable[[str], None]) -> None:
    def size(code: str) -> int:
        return len(code) // 2

    seed_note = " (the virtual dry-run funds output)" if seed_virtual else ""
    log(f"  identity_hash          {plan['identity_hash']}")
    log(f"  operator               {plan['holder_owner_vkey_hash']} ({plan['operator_address']})")
    log(f"  record token seed      {plan['record_token_seed']}{seed_note}")
    log(f"  record_token_policy    {plan['record_token_policy']}")
    log(f"  refs_token_policy      {plan['refs_token_policy']} (ScriptPubkey(operator), "
        f"token \"{plan['refs_token_name']}\")")
    log(f"  GovernanceConfig       {len(plan['governance_config_cbor']) // 2} bytes CBOR")
    for role, info in plan["holders"].items():
        log(f"  holder {role:9} tag {info['tag']}  {info['hash']}  {size(info['compiled_code']):>6,} bytes  "
            f"{info['address']}")
    for title, info in plan["validators"].items():
        log(f"  {title:33} {info['hash']}  {size(info['compiled_code']):>6,} bytes")


def plan_report(plan: Mapping[str, Any], *, network: str, mode: str, seed_virtual: bool) -> dict:
    """The header of the --report file."""
    return {
        "module": MODULE,
        "network": network,
        "mode": mode,
        "time": vd.utc_now(),
        "identity_hash": plan["identity_hash"],
        "operator": plan["holder_owner_vkey_hash"],
        "record_token_seed": plan["record_token_seed"],
        "seed_is_virtual": seed_virtual,
        "record_token_policy": plan["record_token_policy"],
        "refs_token_policy": plan["refs_token_policy"],
        "governance_config_cbor": plan["governance_config_cbor"],
        "holders": {role: {"tag": info["tag"], "hash": info["hash"], "address": info["address"],
                           "size": len(info["compiled_code"]) // 2}
                    for role, info in plan["holders"].items()},
        "validators": {title: {"hash": info["hash"], "size": len(info["compiled_code"]) // 2}
                       for title, info in plan["validators"].items()},
    }


# ── Seed and state ───────────────────────────────────────────────────────────


def choose_seed(run: vd.Run, state: vd.DeployState) -> tuple[str, bool]:
    """The record token seed and whether it is virtual. A saved seed comes
    first. In a dry run with --dry-run-funds, the virtual funds output is the
    seed. Else the seed is the largest lovelace-only operator UTxO that is
    not protected."""
    saved = state.data.get("record_token_seed")
    if saved:
        return saved, False
    if run.dry_run_funds:
        for key, utxo in run.chain.virtual.items():
            if utxo.output.address == run.operator_address and vd.lovelace_only(utxo) and key not in run.chain.spent:
                return key, True
    wallet = run.wallet_utxos()
    if not wallet:
        raise vd.DeployError(
            f"the operator address {run.operator_address} has no lovelace-only output for the record token "
            "seed. Fund the address, or use --dry-run-funds for a dry run."
        )
    return vd.ref_key(wallet[0].input), False


def check_owner(state: vd.DeployState, operator_key_hash: bytes) -> None:
    owner = state.data.get("holder_owner_vkey_hash")
    if owner and owner != bytes(operator_key_hash).hex():
        raise vd.DeployError(f"{state.path} belongs to the operator key {owner}, not to "
                             f"{bytes(operator_key_hash).hex()}")


def check_plan(state: vd.DeployState, plan: Mapping[str, Any]) -> None:
    """The values of a saved state must be the values of this run."""
    for key in PLAN_KEYS:
        saved = state.data.get(key)
        if saved is not None and saved != plan[key]:
            raise vd.DeployError(
                f"{state.path}: {key} of the state differs from this run. The identity deployment, the operator "
                "key or the contracts changed after the first run. Use the inputs of that run, or give a new "
                "--state-dir to start a new deployment."
            )


def write_plan(state: vd.DeployState, plan: Mapping[str, Any]) -> None:
    """Put the plan values into the state. The GovernanceParams values of
    the state stay when deploy_params.py replaced the record."""
    for key, value in plan.items():
        if key == "governance_params":
            state.data.setdefault(key, dict(value))
        else:
            state.data[key] = json.loads(json.dumps(value))


def update_state(state: vd.DeployState) -> None:
    """Write tx_hashes, the transaction of the activity head, the
    GovernanceParams values and client_settings from the recorded steps, and
    save the state. The tx_hashes that another tool wrote into the state
    (treasury_replenish.py, update_params.py) stay, unless a recorded step
    gives the same key."""
    data = state.data
    steps = data.get("steps") or {}
    data["tx_hashes"] = {**(data.get("tx_hashes") or {}), **state_tx_hashes(steps)}
    if isinstance(data.get("activity_head"), dict):
        data["activity_head"]["tx"] = data["tx_hashes"].get("activity_head")
    values = (current_params_entry(steps) or {}).get("governance_params")
    if values:
        data["governance_params"] = dict(values)
    data["client_settings"] = client_settings(data)
    state.save()


def require_state(state: vd.DeployState) -> None:
    """The state must have the plan of scripts/deploy.py."""
    missing = [key for key in ("identity_hash", "holders", "record_token_policy", "identity_deployment")
               if not state.data.get(key)]
    if missing:
        raise vd.DeployError(f"{state.path} has no Module-6 deployment ({', '.join(missing)} missing). "
                             "Run scripts/deploy.py first (build, or run --submit).")


def protected_inputs(state: vd.DeployState) -> tuple[set, set]:
    """(protected outputs, protected addresses) of the identity deployment
    of a state: config_seed and seal_seed_address."""
    identity = state.data.get("identity_deployment") or {}
    if not identity.get("config_seed") or not identity.get("seal_seed_address"):
        raise vd.DeployError(f"{state.path} has no config_seed or seal_seed_address of the identity deployment")
    return {identity["config_seed"]}, {identity["seal_seed_address"]}


def check_pending(state: vd.DeployState, owned: Callable[[str], bool]) -> None:
    """A build replaces the pending transactions of the state. Refuse it when
    another tool built some of them."""
    other = [name for name in state.pending if not owned(name)]
    if other:
        raise vd.DeployError(
            f"{state.path} has built transactions of another tool ({', '.join(other)}). A new build replaces "
            "them. Send them first (submit action), or remove them from \"pending\"."
        )


def make_chain(args) -> vd.DeployChain:
    preset = vd.network_from_args(args)
    return vd.DeployChain(vd.Ogmios(preset.ogmios_url), preset)


# ── Steps ────────────────────────────────────────────────────────────────────


def datum_tree(utxo: UTxO) -> Any:
    """The inline datum of a UTxO as a Plutus Data tree, or None."""
    raw = vd.datum_cbor(utxo)
    if raw is None:
        return None
    try:
        return cbor2.loads(raw)
    except (ValueError, cbor2.CBORDecodeError):
        return None


def _tx(utxo: UTxO) -> str:
    return bytes(utxo.input.transaction_id).hex()


def _token_utxos(run: vd.Run, address: Address, policy: bytes, name: bytes) -> list[UTxO]:
    found = [u for u in run.live(addresses=[str(address)]) if vd.has_token(u, policy, name)]
    return sorted(found, key=lambda u: vd.ref_key(u.input))


def unit_redeemer() -> Redeemer:
    return Redeemer(RawCBOR(plutus_data_cbor(UNIT_REDEEMER)))


def publish_step(name: str, title: str, reference: Address, compiled_code: str) -> vd.Step:
    """A reference script as output 0 at the reference address, with the
    exact min-UTxO."""
    script = PlutusV3Script(bytes.fromhex(compiled_code))
    script_hash = vd.script_hash_of(script)

    def build(run: vd.Run):
        builder = run.builder()
        builder.add_output(vd.exact_output(run.chain, reference, script=script))
        return builder

    return vd.Step(
        name=name, title=title, build=build,
        find=lambda run: vd.find_reference_script(run, reference, script_hash, index0=True),
        record=lambda tx: {"ref": f"{tx}#0"},
    )


def records_step(state: vd.DeployState, plan: Mapping[str, Any]) -> vd.Step:
    """The record step: it spends the seed and mints the three record tokens
    of the one-shot policy."""
    holders = plan["holders"]
    operator = plan["holder_owner_vkey_hash"]
    policy = bytes.fromhex(plan["record_token_policy"])
    script = PlutusV3Script(bytes.fromhex(plan["record_token_compiled_code"]))
    records = (
        (Address.from_primitive(holders["params"]["address"]), PARAMS_TOKEN_NAME,
         plutus_data_cbor(governance_params_data(plan["governance_params"]))),
        (Address.from_primitive(holders["oracle"]["address"]), ORACLE_TOKEN_NAME,
         plutus_data_cbor(oracle_datum_data(operator, holders["treasury"]["hash"]))),
        (Address.from_primitive(plan["activity_head"]["address"]), ACTIVITY_TOKEN_NAME,
         plutus_data_cbor(build_activity_head_data(operator))),
    )
    seed = plan["record_token_seed"]

    def seed_utxo(run: vd.Run) -> UTxO:
        found = run.chain.view(refs=[vd.parse_ref(seed)])
        if not found:
            raise vd.DeployError(
                f"records: the record token seed {seed} is spent, and the chain shows no records of its policy. "
                "If the records transaction waits for the chain, run again later. Else give a new --state-dir "
                "to start a new deployment."
            )
        utxo = found[0]
        if utxo.output.address != run.operator_address or not vd.lovelace_only(utxo):
            raise vd.DeployError(f"records: the seed {seed} is not a lovelace-only output of the operator address")
        return utxo

    def build(run: vd.Run):
        builder = run.builder()
        builder.add_input(seed_utxo(run))
        builder.mint = vd.tokens(policy, {name: 1 for _, name, _ in records})
        builder.add_minting_script(script, unit_redeemer())
        for address, name, datum in records:
            builder.add_output(vd.exact_output(run.chain, address, assets=vd.tokens(policy, {name: 1}),
                                               datum=RawCBOR(datum), at_least=RECORD_LOVELACE))
        return builder

    def find(run: vd.Run) -> Optional[dict]:
        # The policy mints only in the transaction that spends the seed, so a
        # token of the policy shows that the step is done. The params record
        # can move (deploy_params.py), and the head moves with each first
        # submission of an agent: the values are the current outputs.
        found = {}
        for (address, name, _), key in zip(records, ("params", "oracle", "activity_head")):
            utxos = _token_utxos(run, address, policy, name)
            if utxos:
                found[key] = vd.ref_key(utxos[0].input)
        if "params" not in found:
            return None
        known = (state.steps.get(RECORDS_STEP) or {}).get("tx")
        txs = {ref.partition("#")[0] for ref in found.values()}
        found["tx"] = known or (txs.pop() if len(txs) == 1 else found["params"].partition("#")[0])
        return found

    return vd.Step(
        name=RECORDS_STEP,
        title="spend the seed; mint the params, oracle and activity record tokens; create the GovernanceParams "
              "record, the oracle record and the head of the activity list",
        build=build, find=find,
        record=lambda tx: {"params": f"{tx}#0", "oracle": f"{tx}#1", "activity_head": f"{tx}#2"},
        collateral=True,
    )


def treasury_step(state: vd.DeployState, treasury_address: str, batch_id: int, batch_lovelace: int) -> vd.Step:
    """A treasury batch: output 0 at the treasury holder with
    TreasuryBatchDatum(batch_id, True). deploy.py makes batches 1 to 3,
    treasury_fund.py makes the next ones."""
    address = Address.from_primitive(treasury_address)
    tree = treasury_batch_data(batch_id, True)
    datum = plutus_data_cbor(tree)
    name = f"treasury-{batch_id}"

    def build(run: vd.Run):
        builder = run.builder()
        builder.add_output(vd.exact_output(run.chain, address, datum=RawCBOR(datum), at_least=batch_lovelace))
        return builder

    def find(run: vd.Run) -> Optional[dict]:
        known = state.steps.get(name) or {}
        batches = [u for u in run.live(addresses=[str(address)])
                   if datum_tree(u) == tree and vd.coin_of(u) >= batch_lovelace]
        if batches:
            best = min(batches, key=lambda u: (_tx(u) != known.get("tx"), u.input.index, vd.ref_key(u.input)))
            return {"tx": _tx(best), "batch": vd.ref_key(best.input), "lovelace": vd.coin_of(best)}
        if known.get("tx"):
            # An adoption spends a batch. The state keeps the transaction that
            # made it, after the chain showed it.
            return {key: value for key, value in known.items() if key not in ("time", "found_on_chain")}
        return None

    return vd.Step(
        name=name,
        title=f"treasury batch {batch_id}: {batch_lovelace / vd.LOVELACE_PER_AP3X:,.6f} AP3X at the treasury holder",
        build=build, find=find,
        record=lambda tx: {"batch": f"{tx}#0", "lovelace": batch_lovelace},
    )


def cross_refs_step(state: vd.DeployState, plan: Mapping[str, Any]) -> vd.Step:
    """The CrossRefs output: the "governance_refs" token of
    ScriptPubkey(operator) and the 5-field GovernanceCrossRefs, as output 0
    at the oracle holder."""
    native = vd.operator_native_script(bytes.fromhex(plan["holder_owner_vkey_hash"]))
    policy = vd.native_policy_id(native)
    if policy.hex() != plan["refs_token_policy"]:
        raise vd.DeployError("refs_token_policy is not ScriptPubkey(operator)")
    validators = plan["validators"]
    tree = build_cross_refs_data(
        validators[PROPOSAL_SPEND]["hash"], validators[CRITIQUE_SPEND]["hash"],
        validators[PROPOSAL_MINT]["hash"], validators[CRITIQUE_MINT]["hash"],
        validators[ENDORSEMENT_SPEND]["hash"],
    )
    datum = plutus_data_cbor(tree)
    address = Address.from_primitive(plan["cross_refs_address"])

    def build(run: vd.Run):
        builder = run.builder()
        builder.native_scripts = [native]
        builder.mint = vd.tokens(policy, {REFS_TOKEN_NAME: 1})
        builder.add_output(vd.exact_output(run.chain, address, assets=vd.tokens(policy, {REFS_TOKEN_NAME: 1}),
                                           datum=RawCBOR(datum)))
        return builder

    def find(run: vd.Run) -> Optional[dict]:
        known = (state.steps.get("cross-refs") or {}).get("tx")
        matches = [u for u in _token_utxos(run, address, policy, REFS_TOKEN_NAME) if datum_tree(u) == tree]
        if not matches:
            return None
        best = min(matches, key=lambda u: (_tx(u) != known, u.input.index, vd.ref_key(u.input)))
        return {"tx": _tx(best), "cross_refs": vd.ref_key(best.input)}

    return vd.Step(
        name="cross-refs",
        title="mint the \"governance_refs\" token; create the GovernanceCrossRefs output at the oracle holder",
        build=build, find=find,
        record=lambda tx: {"cross_refs": f"{tx}#0"},
    )


def module6_steps(plan: Mapping[str, Any], state: vd.DeployState) -> list:
    """The on-chain steps of a Module-6 deployment, in order."""
    reference = Address.from_primitive(plan["reference_address"])
    steps = [records_step(state, plan)]
    for role in ("params", "oracle", "treasury"):
        info = plan["holders"][role]
        steps.append(publish_step(
            f"publish-holder-{role}",
            f"publish the {role} holder script (tag {info['tag']}, {len(info['compiled_code']) // 2:,} bytes)",
            reference, info["compiled_code"],
        ))
    for title, name in VALIDATOR_TITLES.items():
        code = plan["validators"][title]["compiled_code"]
        steps.append(publish_step(f"publish-{name}",
                                  f"publish the {name} reference script ({len(code) // 2:,} bytes)",
                                  reference, code))
    for batch_id in range(1, TREASURY_BATCHES + 1):
        steps.append(treasury_step(state, plan["holders"]["treasury"]["address"], batch_id,
                                   TREASURY_BATCH_LOVELACE))
    steps.append(cross_refs_step(state, plan))
    if tuple(step.name for step in steps) != STEP_NAMES:
        raise vd.DeployError("internal error: the steps are not STEP_NAMES")
    return steps


# ── Command ──────────────────────────────────────────────────────────────────


def seal_check(chain: vd.DeployChain, identity: Mapping[str, Any],
               manifest: Optional[Mapping[str, Any]]) -> tuple[bool, str]:
    """(ok, note): Module-1 seal_status for M1 (hashes.identity_party) of the
    Module-1 manifest. ok is True only when the configuration record on chain
    names M1. Without a manifest the tool does not check the seal."""
    if manifest is None:
        return False, "not checked (no --module1-manifest; a send needs it)"
    return module1_identity_deploy().seal_status(vd, chain, dict(identity), manifest["hashes"]["identity_party"])


def command_deploy(args, *, chain: Optional[vd.DeployChain] = None, post: Callable = requests.post,
                   sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> None:
    state_dir: Path = args.state_dir or default_state_dir(args.network)
    state = vd.DeployState.load(state_dir / STATE_FILE, module=MODULE, network=args.network)
    operator = vd.operator_key_hash_from_args(args)
    check_owner(state, operator)
    if args.action == "sign":
        vd.sign_pending(state, vd.load_signing_key(args.skey), operator, log)
        return
    identity = load_identity_deployment(args.deployment, args.network)
    check_operator_address(identity, operator)
    sending = args.submit or args.action in ("build", "submit")
    manifest = load_module1_manifest(args.module1_manifest, identity) if args.module1_manifest else None
    if sending and manifest is None:
        raise vd.DeployError("a send (run --submit, build, submit) needs --module1-manifest, the Module-1 "
                             "manifest.json: Module-6 is deployed only after the seal names M1")
    if args.action == "submit" and not state.data.get("record_token_seed"):
        raise vd.DeployError(f"{state.path} has no built transactions; run the build action first")
    if args.action == "build":
        check_pending(state, lambda name: name in STEP_NAMES)

    chain = chain if chain is not None else make_chain(args)
    chain.check_network()
    seal_ok, seal_note = seal_check(chain, identity, manifest)
    log(f"Seal check: {seal_note}")
    if sending and not seal_ok:
        raise vd.DeployError("Module-6 is deployed only after the seal of M1 (check-seal must pass)")
    run = vd.Run(
        chain, operator,
        protected={identity["config_seed"]},
        protected_addresses={identity["seal_seed_address"]},
        dry_run_funds=lovelace(args.dry_run_funds),
        log=log,
    )
    if str(run.operator_address) != identity["operator_address"]:
        raise vd.DeployError("the operator key does not match operator_address of the identity deployment")
    seed, seed_virtual = choose_seed(run, state)
    plan = module6_plan(identity, operator, seed)
    check_plan(state, plan)
    run.labels.update(plan_labels(plan))

    mode = "dry run" if not sending else args.action if args.action != "run" else "send"
    log(f"Module-6 deploy on {chain.preset.name} ({mode})")
    describe_plan(plan, seed_virtual=seed_virtual, log=log)
    if sending:
        write_plan(state, plan)
        update_state(state)  # the seed is in the state before the first on-chain step
    log("")
    vd.execute(
        args, run, module6_steps(plan, state), state,
        tx_dir=state_dir / "tx",
        header=plan_report(plan, network=chain.preset.name, mode=mode, seed_virtual=seed_virtual)
        | {"seal_check": {"ok": seal_ok, "note": seal_note}},
        after=lambda: update_state(state),
        post=post, sleep=sleep,
    )
    if sending:
        update_state(state)
        log(f"\nState: {state.path} (client_settings: the values for the clients)")
    else:
        log(f"State: {state.path} (a dry run writes no state)")


def build_parser() -> argparse.ArgumentParser:
    # No abbreviations: "--sub" must not mean --submit.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    vd.add_run_arguments(parser)
    parser.add_argument("--deployment", type=Path,
                        help="the identity deployment.json (format vector-identity-deployment/1)")
    parser.add_argument("--module1-manifest", type=Path,
                        help="the Module-1 manifest.json; a send needs it (the seal must name its M1)")
    parser.add_argument("--state-dir", type=Path, help="default: deploy/identity/state/<network>")
    return parser


def main(argv: Optional[Iterable[str]] = None, *, chain: Optional[vd.DeployChain] = None,
         post: Callable = requests.post, sleep: Callable[[float], None] = time.sleep) -> int:
    parser = build_parser()
    args = parser.parse_args(None if argv is None else list(argv))
    if args.deployment is None and args.action != "sign":
        parser.error("--deployment is required: the identity deployment.json of the agents-marketplace tool")
    try:
        command_deploy(args, chain=chain, post=post, sleep=sleep)
    except (vd.DeployError, DeployInputError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
