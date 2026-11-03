"""
Module 6: replace the GovernanceParams record of the deployment

In the deployment on the identity registry, only the records step of
scripts/deploy.py creates the GovernanceParams record: it holds the `params`
token of the one-shot record token policy. This tool replaces the record.
One transaction spends the record at the params holder (the holder script of
the deploy state, a unit redeemer, the operator key as required signer and
collateral) and creates it again at the same address, with the same `params`
token, the same lovelace (or the min-UTxO when that is higher) and the new
values. The validators read GovernanceParams only from this record.

Usage:
    python3 scripts/deploy_params.py [run|build|sign|submit] (--vkey F | --skey F)
            [--params FILE] [--submit] [--dry-run-funds AP3X]
            [--state-dir DIR] [--network mainnet|testnet] [--report F]

    --params FILE   a JSON object with the 21 GovernanceParams fields
                    (default: the values of deploy.py,
                    deploy_common.DEFAULT_GOVERNANCE_PARAMS). Times are POSIX
                    milliseconds, amounts are lovelace.

"run" is a dry run (build the transaction, evaluate it with Ogmios, send
nothing), or a send with --submit (sign with --skey). "build" writes the
unsigned transaction, "sign" signs it offline with --skey, "submit" sends the
signed one with the values of the build. The tool reads the deploy state of
scripts/deploy.py (default deploy/identity/state/<network>/deploy_state.json)
and refuses a state without a params record. When the record holds the
values already, the tool does nothing. A send records the step
params-update-<n> and writes tx_hashes["params_utxo"], governance_params and
client_settings (GOV_PARAMS_UTXO, VECTOR_MODULE6_PARAMS_UTXO) into the state.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

import requests

import deploy
from deploy_common import (
    DEFAULT_GOVERNANCE_PARAMS,
    GOVERNANCE_PARAMS_FIELDS,
    PARAMS_TOKEN_NAME,
    PARAMS_UPDATE_PREFIX,
    DeployInputError,
    check_governance_params,
    current_params_entry,
    governance_params_data,
    governance_params_from_data,
    plutus_data_cbor,
)

import vector_deploy as vd  # noqa: E402  (shared/python, on sys.path through deploy_common)
from pycardano import Address, PlutusV3Script, UTxO, VerificationKeyHash  # noqa: E402
from pycardano.serialization import RawCBOR  # noqa: E402


def read_params(path: Optional[Path]) -> dict:
    """The 21 GovernanceParams values of a JSON file, or the deploy values."""
    if path is None:
        return dict(DEFAULT_GOVERNANCE_PARAMS)
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise DeployInputError(f"cannot read the GovernanceParams file {path}: {error}") from None
    if not isinstance(data, dict):
        raise DeployInputError(f"{path} must hold a JSON object with the 21 GovernanceParams fields")
    return check_governance_params(data)


def is_params_update(name: str) -> bool:
    return name.startswith(PARAMS_UPDATE_PREFIX) and name[len(PARAMS_UPDATE_PREFIX):].isdigit()


def next_step_name(state: vd.DeployState) -> str:
    """params-update-<n>: n is one more than the last recorded replacement."""
    numbers = [int(name[len(PARAMS_UPDATE_PREFIX):]) for name in state.steps if is_params_update(name)]
    return f"{PARAMS_UPDATE_PREFIX}{max(numbers, default=0) + 1}"


def params_holder(state: vd.DeployState) -> tuple[Address, PlutusV3Script, bytes, bytes]:
    """(address, holder script, record token policy, operator key hash) of
    the params record of a deploy state."""
    data = state.data
    holder = data["holders"]["params"]
    script = PlutusV3Script(bytes.fromhex(holder["compiled_code"]))
    if vd.script_hash_of(script).hex() != holder["hash"]:
        raise vd.DeployError(f"{state.path}: the params holder code does not have the hash {holder['hash']}")
    return (Address.from_primitive(holder["address"]), script, bytes.fromhex(data["record_token_policy"]),
            bytes.fromhex(data["holder_owner_vkey_hash"]))


def params_records(utxos: Iterable[UTxO], policy: bytes) -> list[UTxO]:
    """The outputs that hold the `params` record token (one at most)."""
    return sorted((u for u in utxos if vd.has_token(u, policy, PARAMS_TOKEN_NAME)), key=lambda u: vd.ref_key(u.input))


def params_update_step(state: vd.DeployState, name: str, values: Mapping[str, int]) -> vd.Step:
    """Spend the params record and create it again with `values` (output 0)."""
    address, script, policy, operator = params_holder(state)
    values = check_governance_params(values)
    tree = governance_params_data(values)
    datum = plutus_data_cbor(tree)

    def build(run: vd.Run):
        records = params_records(run.chain.view(addresses=[str(address)]), policy)
        if not records:
            raise vd.DeployError(f"{name}: no output at the params holder {address} holds the params record token. "
                                 "Run scripts/deploy.py: its records step creates the record.")
        record = records[0]
        builder = run.builder()
        builder.add_script_input(record, script=script, redeemer=deploy.unit_redeemer())
        builder.add_output(vd.exact_output(run.chain, address, assets=vd.tokens(policy, {PARAMS_TOKEN_NAME: 1}),
                                           datum=RawCBOR(datum), at_least=vd.coin_of(record)))
        # The holder accepts the spend only with the signature of its owner.
        builder.required_signers = [VerificationKeyHash(operator)]
        return builder

    def find(run: vd.Run) -> Optional[dict]:
        for utxo in params_records(run.live(addresses=[str(address)]), policy):
            if deploy.datum_tree(utxo) == tree:
                return {"tx": bytes(utxo.input.transaction_id).hex(), "params": vd.ref_key(utxo.input),
                        "governance_params": dict(values)}
        return None

    return vd.Step(
        name=name,
        title="spend the GovernanceParams record and create it again with the new values",
        build=build, find=find,
        record=lambda tx: {"params": f"{tx}#0", "governance_params": dict(values)},
        collateral=True,
    )


def describe_change(current: Optional[Mapping[str, int]], values: Mapping[str, int],
                    log: Callable[[str], None]) -> None:
    if current is None:
        log("  The params record does not hold a GovernanceParams datum. The new record holds:")
        for field in GOVERNANCE_PARAMS_FIELDS:
            log(f"    {field:36} {values[field]:,}")
        return
    changed = [field for field in GOVERNANCE_PARAMS_FIELDS if current[field] != values[field]]
    for field in changed:
        log(f"    {field:36} {current[field]:,} -> {values[field]:,}")


def command_params(args, *, chain: Optional[vd.DeployChain] = None, post: Callable = requests.post,
                   sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> None:
    state_dir: Path = args.state_dir or deploy.default_state_dir(args.network)
    state = vd.DeployState.load(state_dir / deploy.STATE_FILE, module=deploy.MODULE, network=args.network)
    deploy.require_state(state)
    operator = vd.operator_key_hash_from_args(args)
    deploy.check_owner(state, operator)
    if args.action == "sign":
        vd.sign_pending(state, vd.load_signing_key(args.skey), operator, log)
        return
    if current_params_entry(state.steps) is None:
        raise vd.DeployError(f"{state.path} has no params record yet. Run scripts/deploy.py first: its records "
                             "step creates the record.")
    sending = args.submit or args.action in ("build", "submit")
    if args.action == "submit":
        names = [name for name in state.pending if is_params_update(name)]
        steps = []
        for name in names:
            values = state.pending[name].get("governance_params")
            if not values:
                raise vd.DeployError(f"{name}: the built transaction has no GovernanceParams values; build again")
            if args.params is not None and read_params(args.params) != values:
                raise vd.DeployError(f"{name}: --params differs from the values of the build")
            steps.append(params_update_step(state, name, values))
        values = None
    else:
        values = read_params(args.params)
        steps = [params_update_step(state, next_step_name(state), values)]
    if args.action == "build":
        deploy.check_pending(state, is_params_update)

    chain = chain if chain is not None else deploy.make_chain(args)
    chain.check_network()
    protected, protected_addresses = deploy.protected_inputs(state)
    run = vd.Run(chain, operator, protected=protected, protected_addresses=protected_addresses,
                 dry_run_funds=deploy.lovelace(args.dry_run_funds), log=log)
    address, _, policy, _ = params_holder(state)
    run.labels[str(address)] = "the Module-6 params holder"

    mode = "dry run" if not sending else args.action if args.action != "run" else "send"
    log(f"Module-6 GovernanceParams record on {chain.preset.name} ({mode})")
    if values is not None:
        records = params_records(run.live(addresses=[str(address)]), policy)
        if not records:
            raise vd.DeployError(f"no output at the params holder {address} holds the params record token "
                                 f"{policy.hex()}.{PARAMS_TOKEN_NAME.decode()}")
        tree = deploy.datum_tree(records[0])
        current = governance_params_from_data(tree)
        log(f"  params record {vd.ref_key(records[0].input)} at {address}")
        if tree == governance_params_data(values):
            log("  The record holds these values already: nothing to do.")
            return
        describe_change(current, values, log)
    log("")
    built = vd.execute(
        args, run, steps, state,
        tx_dir=state_dir / "tx",
        header={"module": deploy.MODULE, "tool": "deploy_params", "network": chain.preset.name, "mode": mode,
                "time": vd.utc_now(), "governance_params": values},
        after=lambda: deploy.update_state(state),
        post=post, sleep=sleep,
    )
    if args.action == "build":
        for b in built:
            state.pending[b.step.name]["governance_params"] = dict(values)
        state.save()
    if sending:
        deploy.update_state(state)
        log(f"\nState: {state.path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    vd.add_run_arguments(parser)
    parser.add_argument("--params", type=Path, help="JSON object with the 21 GovernanceParams fields "
                                                     "(default: the values of deploy.py)")
    parser.add_argument("--state-dir", type=Path, help="default: deploy/identity/state/<network>")
    return parser


def main(argv: Optional[Iterable[str]] = None, *, chain: Optional[vd.DeployChain] = None,
         post: Callable = requests.post, sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    try:
        command_params(args, chain=chain, post=post, sleep=sleep)
    except (vd.DeployError, DeployInputError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
