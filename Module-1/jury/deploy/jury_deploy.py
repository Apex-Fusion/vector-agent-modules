#!/usr/bin/env python3
"""Deploy the Module-1 jury validator (SPEC §3, §15).

1. ``aiken build -t silent`` in ``Module-1/jury/contracts`` (skip with
   ``--no-build``); it writes the unapplied blueprint ``contracts/plutus.json``.
2. Read the JuryParams: ``--params FILE`` (the ``params`` object of a
   manifest), else the mainnet values of SPEC §3. Check the value rules of §3.
3. ``aiken blueprint apply`` with the JuryParams CBOR. The script hash is
   blake2b-224 of 0x03 and the applied script; the jury address is
   ``Script(J)`` without a stake credential.
4. With a fee payer (``--skey``, or ``--vkey`` for a dry run): build the
   reference-script transaction, one output at the zero-key address
   ``addr1vyqq…kdl5mw`` with the applied script, funded by the fee payer. The
   default is a dry run: the transaction is built and checked, nothing is
   signed or sent, no file is written. ``--submit`` signs it, sends it with
   Ogmios, waits until the chain shows it, then writes the applied
   blueprint and the manifest (``vector-module1-jury-deployment/1``). When
   the chain already shows the reference script, ``--submit`` sends nothing
   and writes the files.

Test mode (``--test``): other values (``--params FILE``, else short windows
and a small bond), and another manifest path (default
``deploy/<network>-test/manifest.json``). The tool never writes test values
to ``deploy/<network>/manifest.json``.

A submit on mainnet needs ``APEX_NETWORK_CONFIRM=yes``. Key files are read by
path; the tool prints only addresses and key hashes.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from vector_jury import _paths  # noqa: E402,F401  (shared/python on sys.path)
from vector_jury.deployment import (  # noqa: E402
    BLUEPRINT,
    MAINNET_PARAMS,
    MANIFEST_FORMAT,
    REFERENCE_ADDRESS,
    VALIDATOR_MODULE,
    VALIDATOR_NAME,
    DeploymentError,
    blueprint_validator,
    default_manifest_path,
    encode_params,
    load_manifest,
    params_from_json,
    params_problems,
    params_to_json,
    script_address,
    script_hash_of,
    utc_now,
    write_manifest,
)

import vector_deploy as vd  # noqa: E402

#: Test values: short windows, a small bond, quorum 1.
TEST_PARAMS: Dict[str, Any] = dict(
    MAINNET_PARAMS,
    bond_min=20_000_000,
    slash=2_000_000,
    juror_floor=5_000_000,
    eligibility_delay=600_000,
    exit_notice=600_000,
    commit_window=900_000,
    reveal_window=900_000,
    resolve_margin=600_000,
    claim_period=3_600_000,
    quorum=1,
)


class ToolError(RuntimeError):
    pass


def aiken(*args: str, cwd: Path) -> str:
    try:
        result = subprocess.run(["aiken", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise ToolError("aiken is not on PATH") from None
    if result.returncode != 0:
        raise ToolError(f"aiken {' '.join(args[:2])} failed:\n{result.stderr.strip()}")
    return result.stdout


def load_params(args) -> Dict[str, Any]:
    if args.params:
        try:
            data = json.loads(Path(args.params).read_text())
        except (OSError, ValueError) as error:
            raise ToolError(f"cannot read {args.params}: {error}") from None
        return data.get("params", data) if isinstance(data, dict) else data
    return dict(TEST_PARAMS if args.test else MAINNET_PARAMS)


def plan(args, workdir: Path) -> Dict[str, Any]:
    """Build, apply and hash. Returns the plan values."""
    contracts = Path(args.contracts)
    if not args.no_build:
        aiken("build", "-t", "silent", cwd=contracts)
    blueprint = Path(args.blueprint) if args.blueprint else contracts / "plutus.json"
    if not blueprint.exists():
        raise ToolError(f"{blueprint} does not exist; build the contracts first (aiken build)")
    raw_params = load_params(args)
    params = params_from_json(raw_params)
    problems = params_problems(params)
    if problems:
        raise ToolError("the parameters break the rules of SPEC §3: " + "; ".join(problems))
    if args.network == "mainnet" and not args.test and params_to_json(params) != params_to_json(params_from_json(MAINNET_PARAMS)):
        raise ToolError("mainnet without --test uses exactly the values of SPEC §3; use --test for other values")
    unapplied = blueprint_validator(blueprint)
    if len(unapplied.get("parameters") or []) != 1:
        raise ToolError(f"{blueprint}: {VALIDATOR_MODULE}.{VALIDATOR_NAME} must have one parameter (JuryParams)")
    params_cbor = encode_params(params)
    applied_path = workdir / "plutus.json"
    aiken("blueprint", "apply", "-i", str(blueprint.resolve()), "-m", VALIDATOR_MODULE, "-v", VALIDATOR_NAME,
          "-o", str(applied_path), params_cbor.hex(), cwd=workdir)
    validator = blueprint_validator(applied_path)
    if validator.get("parameters"):
        raise ToolError("the applied validator still has open parameters")
    code = bytes.fromhex(validator["compiledCode"])
    script_hash = script_hash_of(code)
    if validator.get("hash") and validator["hash"] != script_hash.hex():
        raise ToolError(f"the blueprint hash {validator['hash']} is not blake2b_224(0x03 || script) {script_hash.hex()}")
    preamble = json.loads(applied_path.read_text()).get("preamble", {})
    compiler = preamble.get("compiler", {})
    return {
        "params": params,
        "params_json": params_to_json(params),
        "params_cbor": params_cbor,
        "code": code,
        "script_hash": script_hash,
        "address": script_address(script_hash),
        "applied": applied_path,
        "blueprint": blueprint,
        "compiler": f"{compiler.get('name', 'aiken')} {compiler.get('version', '')}".strip(),
    }


def manifest_path(args) -> Path:
    if args.manifest:
        path = Path(args.manifest)
    elif args.test:
        path = HERE / f"{args.network}-test" / "manifest.json"
    else:
        path = default_manifest_path(args.network)
    if args.test and path.resolve() == default_manifest_path(args.network).resolve():
        raise ToolError(f"test values never go to {path}; give another --manifest")
    return path


def operator_key_hash(args) -> Optional[bytes]:
    if args.skey:
        return bytes(vd.load_verification_key(args.skey).hash().payload)
    if args.vkey:
        return bytes(vd.load_verification_key(args.vkey).hash().payload)
    return None


def reference_transaction(args, values: Dict[str, Any], operator: bytes):
    """(built transactions, found on chain, chain) of the reference-script step."""
    from pycardano import Address, PlutusV3Script

    preset = vd.network_preset(args.network, ogmios_url=args.ogmios_url)
    chain = vd.DeployChain(vd.Ogmios(preset.ogmios_url), preset)
    chain.check_network()
    run = vd.Run(chain, operator, dry_run_funds=int(args.dry_run_funds * vd.LOVELACE_PER_AP3X),
                 labels={args.reference_address: "the reference address"})
    reference = Address.from_primitive(args.reference_address)
    script = PlutusV3Script(values["code"])

    def build(run):
        builder = run.builder()
        builder.add_output(vd.exact_output(run.chain, reference, script=script))
        return builder

    step = vd.Step(
        name="publish-jury",
        title=f"publish the jury reference script ({len(values['code']):,} bytes)",
        build=build,
        find=lambda run: vd.find_reference_script(run, reference, values["script_hash"], index0=True),
        record=lambda tx: {"ref": f"{tx}#0"},
    )
    built, found = vd.plan_steps(run, [step], None)
    vd.describe_run(built, found, print)
    return built, found, chain, run


def write_files(path: Path, values: Dict[str, Any], tx_id: str, ref: str, args) -> None:
    if path.exists():
        try:
            existing = load_manifest(path)
        except DeploymentError:
            raise ToolError(f"{path} exists and is not a {MANIFEST_FORMAT} manifest; give another --manifest") from None
        if existing.script_hash != values["script_hash"]:
            raise ToolError(f"{path} belongs to the deployment {existing.script_hash.hex()}; give another --manifest")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(values["applied"], path.parent / "plutus.json")
    manifest = {
        "format": MANIFEST_FORMAT,
        "network": args.network,
        "params": values["params_json"],
        "params_cbor": values["params_cbor"].hex(),
        "script_hash": values["script_hash"].hex(),
        "address": str(values["address"]),
        "reference_script": ref,
        "reference_address": args.reference_address,
        "script_size": len(values["code"]),
        "plutus": "plutus.json",
        "compiler": values["compiler"],
        "txs": {"reference_script": tx_id},
        "time": utc_now(),
        # SPEC §12.1: a deployment gets cases_from only when the Foundation jurors
        # are eligible in it; without cases_from it is current for no challenge.
        "cases_from": args.cases_from,
        "cases_until": args.cases_until,
    }
    if args.test:
        manifest["test"] = True
    write_manifest(path, manifest)
    print(f"wrote {path} and {path.parent / 'plutus.json'}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", choices=sorted(vd.NETWORKS), default="mainnet")
    parser.add_argument("--ogmios-url", help=f"default: ${vd.ENV_OGMIOS_URL}, else the preset of --network")
    parser.add_argument("--params", type=Path, help="JuryParams JSON (a params object, or a manifest)")
    parser.add_argument("--test", action="store_true", help="test mode: other values, another manifest path")
    parser.add_argument("--manifest", type=Path, help="manifest path (default deploy/<network>[-test]/manifest.json)")
    parser.add_argument("--contracts", type=Path, default=_paths.CONTRACTS_DIR, help="the Aiken project")
    parser.add_argument("--blueprint", type=Path, help=f"unapplied blueprint (default {BLUEPRINT})")
    parser.add_argument("--no-build", action="store_true", help="do not run aiken build")
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--skey", type=Path, help="fee payer signing key file (needed for --submit)")
    key.add_argument("--vkey", type=Path, help="fee payer verification key file (dry run)")
    parser.add_argument("--submit", action="store_true", help="sign, send and write the manifest (default: dry run)")
    parser.add_argument("--reference-address", default=REFERENCE_ADDRESS,
                        help="address of the reference script output (default: the unspendable zero-key "
                             "address; another address only with --test, for example the operator address so "
                             "that a smoke test can spend it back)")
    parser.add_argument("--cases-from", type=int, help="POSIX ms: first challenged_at that this deployment takes "
                                                       "(default: none; set it in the manifest when the Foundation "
                                                       "jurors are eligible, SPEC §12.1)")
    parser.add_argument("--cases-until", type=int, help="POSIX ms: end of the window (default: none, open end)")
    parser.add_argument("--dry-run-funds", type=float, default=0.0, metavar="AP3X",
                        help="dry run only: a virtual fee payer UTxO with this many AP3X")
    args = parser.parse_args(argv)
    try:
        if args.submit and args.network == "mainnet" and \
                os.environ.get("APEX_NETWORK_CONFIRM", "").strip().lower() != "yes":
            raise ToolError("--submit on mainnet needs APEX_NETWORK_CONFIRM=yes in the environment")
        if args.submit and not args.skey:
            raise ToolError("--submit needs the fee payer signing key (--skey)")
        if args.submit and args.dry_run_funds:
            raise ToolError("--dry-run-funds is only for a dry run")
        if args.reference_address != REFERENCE_ADDRESS and not args.test:
            raise ToolError("--reference-address is allowed only with --test (mainnet uses the zero-key address)")
        try:
            from pycardano import Address as _Address

            _Address.from_primitive(args.reference_address)
        except Exception as error:  # noqa: BLE001 - pycardano raises several types
            raise ToolError(f"--reference-address is not an address: {error}") from None
        path = manifest_path(args)
        with tempfile.TemporaryDirectory(prefix="jury-deploy-") as tmp:
            values = plan(args, Path(tmp))
            print(f"network:      {args.network}{' (test values)' if args.test else ''}")
            print(f"params:       {json.dumps(values['params_json'])}")
            print(f"params_cbor:  {values['params_cbor'].hex()}")
            print(f"script hash:  {values['script_hash'].hex()}")
            print(f"address:      {values['address']}")
            print(f"script size:  {len(values['code']):,} bytes")
            print(f"compiler:     {values['compiler']}")
            print(f"manifest:     {path}")
            operator = operator_key_hash(args)
            if operator is None:
                print("\nNo fee payer key (--skey or --vkey): the reference-script transaction is not built.")
                return 0
            print(f"fee payer:    {vd.key_address(operator)} (key hash {operator.hex()})\n")
            built, found, chain, run = reference_transaction(args, values, operator)
            if not args.submit:
                print("\nDry run: nothing was signed or sent, and no file was written.")
                return 0
            if found:
                _step, entry = found[0]
                tx_id, ref = entry["tx"], entry["ref"]
                print(f"the chain already shows the reference script at {ref}; nothing to send")
            else:
                signed = vd.sign_transactions(built, vd.load_signing_key(args.skey), operator)[0]
                answer = chain.ogmios.rpc("submitTransaction", {"transaction": {"cbor": signed.hex()}})
                tx_id = answer["transaction"]["id"]
                print(f"submitted {tx_id}; waiting until the chain shows it")
                vd.wait_for_transaction(chain, tx_id)
                ref = f"{tx_id}#0"
            write_files(path, values, tx_id, ref, args)
    except (ToolError, DeploymentError, vd.DeployError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
