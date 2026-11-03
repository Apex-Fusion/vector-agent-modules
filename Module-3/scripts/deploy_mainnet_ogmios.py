#!/usr/bin/env python3
"""
Module 3: Reputation Staking — deploy via Ogmios (mainnet and testnet)

  python3 scripts/deploy_mainnet_ogmios.py [run|build|sign|submit]
      --deployment <identity deployment.json>
      --module1-manifest <Module-1 manifest.json>
      (--vkey <operator.vkey> | --skey <operator.skey>)
      [--submit] [--dry-run-funds AP3X] [--network mainnet|testnet]
      [--state-dir DIR] [--report FILE] [--allow-placeholders]

"run" is a dry run by default: it builds every transaction that the chain
does not show yet, evaluates it with Ogmios evaluateTransaction and prints
the fee and min-UTxO totals; nothing is signed or sent. "run --submit" signs
with --skey and sends. "build" writes unsigned transactions, "sign" signs
them offline with --skey, "submit" sends the signed ones
(shared/python/vector_deploy.py).

Deployment order (deploy_common.py): config_holder, config_seal, identity,
Module-1 with identity_party, the seal, then Module-3 (this script), then
Module-6. The operator key (the key of operator_address in the identity
deployment) deploys Module-3: it owns the holders, it is the oracle
credential of ReputationConfig and the key of the refs token policy.

Inputs (no hand edits): identity_hash from the identity deployment.json, the
Module-1 claim, challenge and jury pool hashes and refs token policy from the
Module-1 manifest (deploy_common.deployment_inputs_from_files).

Steps (one transaction each; a step whose result the chain shows is skipped):
  records               spends the record token seed, mints the `params`
                        record token and creates the ProtocolParams record
                        (output 0) at the params holder (tag 31)
  publish-reputation,
  publish-endorsement   the reference scripts at the reference address of
                        the identity deployment (output 0, unspendable)
  cross-refs            mints the `reputation_refs` token of
                        ScriptPubkey(operator) and creates the
                        CrossValidatorRefs output (output 0) at the params
                        holder

State: deploy/identity/state/<network>/deploy_state.json (or --state-dir).
It keeps the keys that reputation_staking.deployment.load_deployment reads,
and the seed, the record token policy and genesis_minting_window of the first
run. The state files of the old deployments (for example
deploy/mainnet/deploy_state.json) are never written.
"""

import argparse
import json
import sys
from pathlib import Path

from pycardano import (
    Address,
    PlutusV3Script,
    Redeemer,
    ScriptHash,
    TransactionInput,
)
from pycardano.serialization import RawCBOR

MODULE3_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE3_ROOT.parent
for path in (REPO_ROOT / "shared" / "python", MODULE3_ROOT / "python"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import vector_deploy as vd  # noqa: E402
from holder_scripts import aiken_apply  # noqa: E402

from deploy_common import (  # noqa: E402
    PARAMS_HOLDER_TAG,
    PARAMS_TOKEN_NAME,
    REFS_TOKEN_NAME,
    TREASURY_HOLDER_TAG,
    UNIT_REDEEMER_CBOR,
    DeployInputError,
    apply_holder,
    apply_record_token_policy,
    build_cross_refs_cbor,
    build_protocol_params_cbor,
    build_reputation_config_cbor,
    deployment_inputs_from_files,
    deployment_inputs_state,
    genesis_minting_end_ms,
    resume_state_error,
    select_seed_utxo,
    utxo_ref,
)

CONTRACT_DIR = MODULE3_ROOT / "reputation-staking"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
STATE_ROOT = MODULE3_ROOT / "deploy" / "identity" / "state"
IDENTITY_DEPLOY = REPO_ROOT / "Module-1" / "scripts" / "identity_deploy.py"

#: AP3X in the ProtocolParams record (the operator can spend it to update the
#: record) and in the CrossValidatorRefs output.
RECORD_LOVELACE = 3_000_000

#: (blueprint title, step name, tx_hashes key) of the reference scripts.
REFERENCE_SCRIPTS = (
    ("reputation.reputation.spend", "publish-reputation", "reputation_spend_ref"),
    ("endorsement.endorsement.spend", "publish-endorsement", "endorsement_spend_ref"),
)

#: The step of each tx_hashes key that the clients read.
STEP_KEYS = {
    "records": "params_utxo",
    "publish-reputation": "reputation_spend_ref",
    "publish-endorsement": "endorsement_spend_ref",
    "cross-refs": "cross_refs_nft",
}


def identity_tools():
    """Module-1/scripts/identity_deploy.py: the reader and the checks of the
    identity deployment file."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("identity_deploy", IDENTITY_DEPLOY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_reputation_config(config_cbor_hex: str) -> dict:
    """Apply ReputationConfig to both Module-3 validators. Returns
    {title: {"hash", "compiled_code"}} without the `else` handlers."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="m3-apply-") as tmp:
        first = Path(tmp) / "reputation.json"
        aiken_apply(BLUEPRINT_PATH, "reputation", "reputation", config_cbor_hex, first)
        applied = aiken_apply(first, "endorsement", "endorsement", config_cbor_hex)
    return {
        v["title"]: {"hash": v["hash"], "compiled_code": v["compiledCode"]}
        for v in applied["validators"]
        if ".else" not in v["title"]
    }


def load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise vd.DeployError(f"cannot read the {what} {path}: {error}") from None


def module3_steps(run, values: dict, scripts: dict, seed: TransactionInput, record_policy: dict) -> list:
    """The on-chain steps of Module-3."""
    reference = Address.from_primitive(values["reference_address"])
    params_holder = vd.script_address(bytes.fromhex(values["params_holder_hash"]))
    params_cbor = build_protocol_params_cbor(values["genesis_minting_window"])
    refs_cbor = build_cross_refs_cbor(values["reputation_validator_hash"], values["endorsement_validator_hash"])
    record_token = bytes.fromhex(record_policy["policy_id"])
    native = vd.operator_native_script(run.operator_key_hash)
    refs_policy = vd.native_policy_id(native)

    def build_records(run):
        seed_utxo = run.chain.view(refs=[seed])
        if not seed_utxo:
            raise vd.DeployError(
                f"records: the record token seed {vd.ref_key(seed)} is spent and the chain does not "
                "show the ProtocolParams record. Wait if the record transaction is pending; else move "
                "the state to another path and start a new deployment."
            )
        builder = run.builder()
        builder.add_input(seed_utxo[0])
        builder.mint = vd.tokens(record_token, {PARAMS_TOKEN_NAME: 1})
        builder.add_minting_script(
            PlutusV3Script(bytes.fromhex(record_policy["compiled_code"])),
            redeemer=Redeemer(RawCBOR(UNIT_REDEEMER_CBOR)),
        )
        builder.add_output(vd.exact_output(
            run.chain, params_holder, assets=vd.tokens(record_token, {PARAMS_TOKEN_NAME: 1}),
            datum=RawCBOR(params_cbor), at_least=RECORD_LOVELACE,
        ))
        return builder

    def find_records(run):
        for utxo in run.live(addresses=[str(params_holder)]):
            if vd.has_token(utxo, record_token, PARAMS_TOKEN_NAME):
                return {"tx": bytes(utxo.input.transaction_id).hex(), "ref": vd.ref_key(utxo.input)}
        return None

    steps = [vd.Step(
        name="records",
        title="mint the `params` record token; create the ProtocolParams record at the params holder",
        build=build_records,
        find=find_records,
        record=lambda tx: {"ref": f"{tx}#0"},
        collateral=True,
    )]

    for title, name, _key in REFERENCE_SCRIPTS:
        code = bytes.fromhex(scripts[title]["compiled_code"])
        script_hash = bytes.fromhex(scripts[title]["hash"])

        def build(run, code=code):
            builder = run.builder()
            builder.add_output(vd.exact_output(run.chain, reference, script=PlutusV3Script(code)))
            return builder

        steps.append(vd.Step(
            name=name,
            title=f"publish the {title.split('.')[0]} reference script ({len(code):,} bytes)",
            build=build,
            find=lambda run, h=script_hash: vd.find_reference_script(run, reference, h, index0=True),
            record=lambda tx: {"ref": f"{tx}#0"},
        ))

    def build_cross_refs(run):
        builder = run.builder()
        builder.mint = vd.tokens(refs_policy, {REFS_TOKEN_NAME: 1})
        builder.native_scripts = [native]
        builder.add_output(vd.exact_output(
            run.chain, params_holder, assets=vd.tokens(refs_policy, {REFS_TOKEN_NAME: 1}),
            datum=RawCBOR(refs_cbor), at_least=RECORD_LOVELACE,
        ))
        return builder

    def find_cross_refs(run):
        for utxo in run.live(addresses=[str(params_holder)]):
            if vd.has_token(utxo, refs_policy, REFS_TOKEN_NAME) and vd.datum_equals(utxo, refs_cbor) \
                    and utxo.input.index == 0:
                return {"tx": bytes(utxo.input.transaction_id).hex(), "ref": vd.ref_key(utxo.input)}
        return None

    steps.append(vd.Step(
        name="cross-refs",
        title="mint the `reputation_refs` token; create the CrossValidatorRefs output at the params holder",
        build=build_cross_refs,
        find=find_cross_refs,
        record=lambda tx: {"ref": f"{tx}#0"},
    ))
    return steps


def update_state(state: vd.DeployState) -> None:
    """Write the tx_hashes keys that the clients read from the recorded steps."""
    tx_hashes = state.data.setdefault("tx_hashes", {})
    for step, key in STEP_KEYS.items():
        ref = state.steps.get(step, {}).get("ref")
        if ref:
            tx_hashes[key] = ref.split("#")[0]
    state.save()


def plan(args, run: vd.Run, state: vd.DeployState, deployment: dict, inputs: dict) -> tuple:
    """The values of the deployment: holders, seed, record token policy,
    ReputationConfig, applied validators. A first send saves them in the
    state before the first transaction."""
    operator = run.operator_key_hash.hex()
    params_holder = apply_holder(PARAMS_HOLDER_TAG, operator)
    treasury_holder = apply_holder(TREASURY_HOLDER_TAG, operator)

    seed_ref = state.data.get("record_token_seed")
    if not seed_ref:
        try:
            seed_ref = utxo_ref(select_seed_utxo(run.wallet_utxos()))
        except RuntimeError:
            raise vd.DeployError(
                f"the operator address {run.operator_address} has no lovelace-only output for the "
                "record token seed. Fund it, or use --dry-run-funds for a dry run."
            ) from None
    seed = vd.parse_ref(seed_ref)
    record_policy = apply_record_token_policy(bytes(seed.transaction_id).hex(), seed.index)
    saved_policy = state.data.get("params_token_policy")
    if saved_policy and saved_policy != record_policy["policy_id"]:
        raise vd.DeployError(f"the seed gives policy {record_policy['policy_id']}, but the state has "
                             f"{saved_policy}: the record token script changed after the first run")
    genesis_minting_window = state.data.get("genesis_minting_window") or genesis_minting_end_ms()
    refs_policy = vd.native_policy_id(vd.operator_native_script(run.operator_key_hash)).hex()
    config_cbor = build_reputation_config_cbor(
        refs_token_policy=refs_policy,
        audit_claim_validator_hash=inputs["audit_claim_validator_hash"],
        audit_challenge_validator_hash=inputs["audit_challenge_validator_hash"],
        audit_jury_pool_hash=inputs["audit_jury_pool_hash"],
        audit_refs_token_policy=inputs["audit_refs_token_policy"],
        params_script_hash=params_holder["hash"],
        treasury_script_hash=treasury_holder["hash"],
        ap3x_policy_id="",
        ap3x_asset_name="",
        oracle_credential=operator,
        params_token_policy=record_policy["policy_id"],
        identity_hash=inputs["identity_hash"],
    )
    scripts = apply_reputation_config(config_cbor)
    reputation_hash = scripts["reputation.reputation.spend"]["hash"]
    endorsement_hash = scripts["endorsement.endorsement.spend"]["hash"]
    values = {
        "network": args.network,
        "wallet_address": str(run.operator_address),
        "wallet_vkey_hash": operator,
        "holder_owner_vkey_hash": operator,
        "refs_token_policy": refs_policy,
        "refs_token_name": REFS_TOKEN_NAME.hex(),
        "reputation_validator_hash": reputation_hash,
        "endorsement_validator_hash": endorsement_hash,
        "reputation_address": str(vd.script_address(bytes.fromhex(reputation_hash))),
        "endorsement_address": str(vd.script_address(bytes.fromhex(endorsement_hash))),
        "params_holder_tag": PARAMS_HOLDER_TAG,
        "params_holder_hash": params_holder["hash"],
        "params_holder_address": str(vd.script_address(bytes.fromhex(params_holder["hash"]))),
        "treasury_holder_tag": TREASURY_HOLDER_TAG,
        "treasury_holder_hash": treasury_holder["hash"],
        "treasury_holder_address": str(vd.script_address(bytes.fromhex(treasury_holder["hash"]))),
        "cross_refs_address": str(vd.script_address(bytes.fromhex(params_holder["hash"]))),
        "record_token_seed": seed_ref,
        "params_token_policy": record_policy["policy_id"],
        "genesis_minting_window": genesis_minting_window,
        "reputation_config_cbor": config_cbor,
        "reference_address": deployment["reference_address"],
        "ap3x_policy_id": "",
        "ap3x_asset_name": "",
        "validators": {title: {"hash": v["hash"]} for title, v in scripts.items()},
        **deployment_inputs_state(inputs),
    }
    return values, scripts, seed, record_policy


def deploy(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    vd.add_run_arguments(parser)
    parser.add_argument("--deployment", type=Path, help="the identity deployment.json")
    parser.add_argument("--module1-manifest", type=Path, help="the Module-1 manifest.json")
    parser.add_argument("--state-dir", type=Path, help="default: deploy/identity/state/<network>")
    parser.add_argument("--allow-placeholders", action="store_true",
                        help="dry run with a Module-1 plan that still has placeholders")
    args = parser.parse_args(argv)
    state_dir = args.state_dir or STATE_ROOT / args.network
    try:
        state = vd.DeployState.load(state_dir / "deploy_state.json", module="module3", network=args.network)
        operator = vd.operator_key_hash_from_args(args)
        if args.action == "sign":
            vd.sign_pending(state, vd.load_signing_key(args.skey), operator, print)
            return 0
        if args.deployment is None or args.module1_manifest is None:
            raise vd.DeployError("give --deployment (identity) and --module1-manifest (Module-1)")
        identity = identity_tools()
        try:
            deployment = identity.load_identity_deployment(args.deployment, args.network)
            identity.check_operator_address(deployment, operator)
        except identity.PlanError as error:
            raise vd.DeployError(str(error)) from None
        manifest = load_json(args.module1_manifest, "Module-1 manifest")
        inputs = deployment_inputs_from_files(deployment, manifest)
        sending = args.submit or args.action in ("build", "submit")
        if manifest.get("placeholders") or manifest.get("not_for_deployment"):
            note = "the Module-1 plan has placeholders (" + ", ".join(manifest.get("placeholders") or []) + ")"
            if sending or not args.allow_placeholders:
                raise vd.DeployError(note + ("; sending needs the final Module-1 plan." if sending
                                             else "; use --allow-placeholders for a dry run."))
            print(f"NOTE: {note}: the hashes of this dry run are not final.")
        error = resume_state_error(state.data, operator.hex(), inputs)
        if error:
            raise vd.DeployError(f"{state.path}: {error}")

        preset = vd.network_from_args(args)
        chain = vd.DeployChain(vd.Ogmios(preset.ogmios_url), preset)
        chain.check_network()
        run = vd.Run(
            chain, operator,
            protected={deployment["config_seed"]},
            protected_addresses={deployment["seal_seed_address"]},
            dry_run_funds=int(args.dry_run_funds * vd.LOVELACE_PER_AP3X),
            labels={deployment["reference_address"]: "the reference address (unspendable)"},
        )
        values, scripts, seed, record_policy = plan(args, run, state, deployment, inputs)
        run.labels[values["params_holder_address"]] = "the Module-3 params holder"
        if sending:
            # Keep the seed, the record token policy and the inputs before the
            # first transaction: a resumed run must use the same values.
            state.data.update(values)
            state.save()
        print(f"Module-3 deploy on {preset.name} ({'send' if sending else 'dry run'})")
        print(f"  reputation  {values['reputation_validator_hash']}")
        print(f"  endorsement {values['endorsement_validator_hash']}")
        print(f"  params holder {values['params_holder_hash']} (tag {PARAMS_HOLDER_TAG}), "
              f"treasury holder {values['treasury_holder_hash']} (tag {TREASURY_HOLDER_TAG})")
        print(f"  record token seed {values['record_token_seed']}, policy {values['params_token_policy']}")
        steps = module3_steps(run, values, scripts, seed, record_policy)
        vd.execute(
            args, run, steps, state,
            tx_dir=state_dir / "tx",
            header={"module": "module3", "network": preset.name, "time": vd.utc_now(),
                    "validators": values["validators"], "params_holder_hash": values["params_holder_hash"],
                    "treasury_holder_hash": values["treasury_holder_hash"],
                    "params_token_policy": values["params_token_policy"]},
            after=lambda: update_state(state),
        )
        if sending:
            update_state(state)
    except (vd.DeployError, DeployInputError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(deploy())
