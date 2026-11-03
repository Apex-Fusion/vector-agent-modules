#!/usr/bin/env python3
"""Tests of scripts/identity_deploy.py that need no aiken build and no chain.

Run: python3 scripts/test_identity_deploy.py (from Module-1/). The tests use
the Python standard library only. The tests of the deploy transactions are in
simulation/tests/test_deploy_transactions.py (pycardano, aiken simulate).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "identity_deploy.py"


def load_script():
    spec = importlib.util.spec_from_file_location("identity_deploy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


deploy = load_script()

#: The operator key of the tests: a 32-byte verification key and its hash.
OPERATOR_VKEY = bytes(range(32))
OPERATOR = hashlib.blake2b(OPERATOR_VKEY, digest_size=28).digest()
REFERENCE_ADDRESS = deploy.bech32("addr", b"\x61" + bytes(28))


def enterprise(key_hash: bytes) -> str:
    return deploy.bech32("addr", b"\x61" + key_hash)


def identity_deployment(**changes) -> dict:
    """An identity deployment.json as the agents-marketplace tool writes it."""
    data = {
        "format": deploy.IDENTITY_DEPLOYMENT_FORMAT,
        "network": "mainnet",
        "identity_hash": "1d" * 28,
        "config_policy": "cf" * 28,
        "config_holder": "c4" * 28,
        "reference_address": REFERENCE_ADDRESS,
        "operator_address": enterprise(OPERATOR),
        "seal_seed_address": enterprise(b"\x5e" * 28),
        "config_seed": "ab" * 32 + "#0",
        "refs": {"identity": "cd" * 32 + "#0"},
        "seal": None,
    }
    data.update(changes)
    return data


def complete_config() -> dict:
    """The default configuration without placeholders."""
    config = json.loads(deploy.DEFAULT_CONFIG.read_text())
    config["placeholders"] = []
    return config


class BuildCalled(Exception):
    """The plan went on to the aiken build."""


class Files:
    """The input files of a plan in a temporary directory."""

    def __init__(self, tmp: Path, config: dict, deployment: dict | None):
        self.config = tmp / "config.json"
        self.config.write_text(json.dumps(config))
        self.deployment = None
        if deployment is not None:
            self.deployment = tmp / "deployment.json"
            self.deployment.write_text(json.dumps(deployment))
        self.vkey = tmp / "operator.vkey"
        self.vkey.write_text(json.dumps({
            "type": "PaymentVerificationKeyShelley_ed25519",
            "description": "",
            "cborHex": "5820" + OPERATOR_VKEY.hex(),
        }))
        self.state = tmp / "state"

    def args(self, *, mode="oracle", allow=False, vkey=True, deployment=True) -> argparse.Namespace:
        return argparse.Namespace(
            config=self.config,
            deployment=self.deployment if deployment else None,
            vkey=self.vkey if vkey else None,
            mode=mode,
            network="mainnet",
            state_dir=self.state,
            allow_placeholders=allow,
        )


@contextlib.contextmanager
def patched(**names):
    originals = {name: getattr(deploy, name) for name in names}
    for name, value in names.items():
        setattr(deploy, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(deploy, name, value)


def no_build(*_args, **_kwargs):
    raise BuildCalled()


def run_plan(args: argparse.Namespace) -> tuple[int, str]:
    stderr = io.StringIO()
    with patched(aiken=no_build), contextlib.redirect_stderr(stderr):
        status = deploy.command_plan(args)
    return status, stderr.getvalue()


class PlanGuardTest(unittest.TestCase):
    def test_complete_inputs_go_to_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(), identity_deployment())
            with self.assertRaises(BuildCalled):
                run_plan(files.args())

    def test_plan_refuses_the_config_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(deploy.DEFAULT_CONFIG.read_text())
            files = Files(Path(tmp), config, identity_deployment())
            status, message = run_plan(files.args())
            self.assertEqual(status, 2)
            self.assertIn("verdict_policy", message)
            with self.assertRaises(BuildCalled):
                run_plan(files.args(allow=True))

    def test_plan_without_deployment_or_key_lists_the_zero_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(), None)
            status, message = run_plan(files.args(vkey=False, deployment=False))
            self.assertEqual(status, 2)
            for section, name in deploy.NONZERO_FIELDS:
                self.assertIn(f"{section}.{name}", message)
            self.assertIn("identity.identity_ref", message)
            with self.assertRaises(BuildCalled):
                run_plan(files.args(vkey=False, deployment=False, allow=True))

    def test_plan_refuses_each_all_zero_identity_hash(self) -> None:
        for key in ("identity_hash", "config_policy", "config_holder"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                files = Files(Path(tmp), complete_config(), identity_deployment(**{key: "00" * 28}))
                status, message = run_plan(files.args())
                self.assertEqual(status, 2)
                self.assertIn(key, message)

    def test_plan_refuses_an_identity_deployment_without_identity_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(), identity_deployment(refs={}))
            status, message = run_plan(files.args())
            self.assertEqual(status, 2)
            self.assertIn("refs.identity", message)
            with self.assertRaises(BuildCalled):
                run_plan(files.args(allow=True))

    def test_plan_refuses_a_wrong_identity_deployment(self) -> None:
        cases = {
            "format": identity_deployment(format="other/1"),
            "network": identity_deployment(network="testnet"),
            "reference_address": identity_deployment(reference_address=enterprise(b"\x01" * 28)),
            "seal-seed address": identity_deployment(seal_seed_address=enterprise(OPERATOR)),
            "config_seed": identity_deployment(config_seed="ab" * 32),
        }
        for label, data in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                files = Files(Path(tmp), complete_config(), data)
                status, _ = run_plan(files.args(allow=True))
                self.assertEqual(status, 2)

    def test_plan_refuses_a_key_that_is_not_the_operator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(),
                          identity_deployment(operator_address=enterprise(b"\x0b" * 28)))
            status, message = run_plan(files.args(allow=True))
            self.assertEqual(status, 2)
            self.assertIn("operator", message)

    def test_config_must_not_set_oracle_active(self) -> None:
        config = complete_config()
        config["protocol_params"]["oracle_active"] = True
        with self.assertRaises(deploy.PlanError):
            deploy.mode_params(config, "oracle")

    def test_unsafe_values_lists_placeholders_and_zero_fields_once(self) -> None:
        values = deploy.plan_values(complete_config(), identity_deployment(), OPERATOR, "jury")
        values["placeholders"] = ["identity.identity_hash", "verdict_policy"]
        values["identity"]["identity_hash"] = "00" * 28
        values["module1"]["oracle_vkh"] = "00" * 28
        self.assertEqual(
            deploy.unsafe_values(copy.deepcopy(values)),
            ["identity.identity_hash", "verdict_policy", "module1.oracle_vkh"],
        )


class ValuesTest(unittest.TestCase):
    def test_identity_values_come_from_the_deployment_and_the_key(self) -> None:
        values = deploy.plan_values(complete_config(), identity_deployment(), OPERATOR, "oracle")
        self.assertEqual(values["identity"], {
            "identity_hash": "1d" * 28, "identity_ref": "cd" * 32 + "#0",
            "config_policy": "cf" * 28, "config_holder": "c4" * 28,
        })
        self.assertEqual(values["module1"]["oracle_vkh"], OPERATOR.hex())
        # ScriptPubkey(operator): blake2b-224 of 0x00 and the CBOR [0, h'key hash'].
        native = b"\x00" + b"\x82\x00\x58\x1c" + OPERATOR
        self.assertEqual(values["module1"]["refs_token_policy"],
                         hashlib.blake2b(native, digest_size=28).hexdigest())
        self.assertEqual(values["module1"]["refs_token_name"], b"auditing_refs".hex())

    def test_the_mode_sets_oracle_active(self) -> None:
        config = complete_config()
        oracle = deploy.encode(deploy.protocol_params(deploy.mode_params(config, "oracle")))
        jury = deploy.encode(deploy.protocol_params(deploy.mode_params(config, "jury")))
        # Field 14 (oracle_active): Constr 1 (True) or Constr 0 (False).
        self.assertIn(b"\xd8\x7a\x80", oracle)
        self.assertIn(b"\xd8\x79\x80", jury)
        self.assertEqual(len(oracle), len(jury))
        self.assertEqual(list(deploy.mode_params(config, "jury")), deploy.PROTOCOL_FIELDS)

    def test_both_modes_keep_the_verdict_policy_of_the_config(self) -> None:
        for mode in deploy.MODES:
            values = deploy.plan_values(complete_config(), identity_deployment(), OPERATOR, mode)
            self.assertEqual(values["verdict_policy"], {
                "auditor_wins": {"action": "suspend", "duration_ms": 604_800_000},
                "claimer_wins": None,
            })

    def test_script_addresses_use_network_id_1(self) -> None:
        addresses = deploy.script_addresses("ab" * 28)
        self.assertTrue(addresses["enterprise"].startswith("addr1w"))
        self.assertTrue(addresses["reward"].startswith("stake17"))
        self.assertEqual(deploy.address_payment_part(addresses["enterprise"]), (0x71, b"\xab" * 28))


class FakeBuild:
    """Fake aiken calls: ``hashes`` gives the hash of each validator."""

    def __init__(self, hashes: dict):
        self.hashes = hashes
        self.applied: list = []

    def aiken(self, *args: str) -> str:
        if args and args[0] == "build":
            out = deploy.CONTRACTS_DIR / args[args.index("-o") + 1]
            out.write_text(json.dumps({"validators": []}))
        return ""

    def apply(self, blueprint: Path, out: Path, validator: str, parameter: bytes) -> None:
        self.applied.append((validator, parameter))
        out.write_text(Path(blueprint).read_text())

    def blueprint_hash(self, _blueprint: Path, validator: str) -> str:
        return self.hashes[validator]

    def records_holder(self, operator_hex: str) -> dict:
        return {"tag": 11, "hash": "11" * 28, "address": deploy.script_addresses("11" * 28)["enterprise"]}


HASHES = {
    "claim": "c1" * 28,
    "challenge": "c2" * 28,
    "jury_pool": "c3" * 28,
    "identity_party": "e1" * 28,
}


def fake_plan(files: Files, fake: FakeBuild, **kw) -> int:
    with patched(aiken=fake.aiken, apply=fake.apply, blueprint_hash=fake.blueprint_hash,
                 records_holder=fake.records_holder), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return deploy.command_plan(files.args(**kw))


class PlanOutputTest(unittest.TestCase):
    """identity_party gets (AuditingConfig, challenge hash, claim hash); the
    plan writes the manifest with the identity values."""

    def test_identity_party_parameters_and_manifest(self) -> None:
        fake = FakeBuild(HASHES)
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(), identity_deployment())
            self.assertEqual(fake_plan(files, fake), 0)
            plan = json.loads((files.state / "plan" / "plan.json").read_text())
            manifest = json.loads((files.state / "manifest.json").read_text())
        values = deploy.plan_values(complete_config(), identity_deployment(), OPERATOR, "oracle")
        party = [parameter for validator, parameter in fake.applied if validator == "identity_party"]
        self.assertEqual(party, [
            deploy.encode(deploy.auditing_config(values)),
            deploy.encode(bytes.fromhex(HASHES["challenge"])),
            deploy.encode(bytes.fromhex(HASHES["claim"])),
        ])
        self.assertEqual(plan["parameters"]["identity_party_claim_hash_cbor"],
                         deploy.encode(bytes.fromhex(HASHES["claim"])).hex())
        self.assertEqual(plan["mode"], "oracle")
        # The keys that the seal step and the Module-1 clients read.
        self.assertEqual(manifest["hashes"]["identity_party"], HASHES["identity_party"])
        self.assertEqual(manifest["identity"], values["identity"])
        self.assertEqual(manifest["module1"]["oracle_vkh"], OPERATOR.hex())
        self.assertEqual(manifest["module1"]["refs_token_policy"], values["module1"]["refs_token_policy"])
        self.assertEqual(manifest["verdict_policy"], values["verdict_policy"])
        self.assertIs(manifest["protocol_params"]["oracle_active"], True)
        self.assertEqual(manifest["reference_address"], REFERENCE_ADDRESS)
        self.assertEqual(manifest["format"], "vector-module-deploy/1")
        self.assertFalse(manifest["not_for_deployment"])
        self.assertEqual(manifest["steps"], {})

    def test_a_plan_with_on_chain_steps_keeps_its_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(), identity_deployment())
            self.assertEqual(fake_plan(files, FakeBuild(HASHES)), 0)
            manifest_path = files.state / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["steps"] = {"publish-claim": {"tx": "aa" * 32, "ref": "aa" * 32 + "#0"}}
            manifest_path.write_text(json.dumps(manifest))
            blueprint = (files.state / "plan" / "plutus.json").read_text()
            # Another mode gives other hashes: refused, the plan stays.
            other = dict(HASHES, identity_party="e2" * 28)
            self.assertEqual(fake_plan(files, FakeBuild(other), mode="jury"), 2)
            self.assertEqual(json.loads(manifest_path.read_text())["hashes"], HASHES)
            self.assertEqual((files.state / "plan" / "plutus.json").read_text(), blueprint)
            # The same hashes: the plan runs again and keeps the steps.
            self.assertEqual(fake_plan(files, FakeBuild(HASHES)), 0)
            self.assertIn("publish-claim", json.loads(manifest_path.read_text())["steps"])

    def test_check_manifest_refuses_another_mode_or_identity(self) -> None:
        fake = FakeBuild(HASHES)
        with tempfile.TemporaryDirectory() as tmp:
            files = Files(Path(tmp), complete_config(), identity_deployment())
            self.assertEqual(fake_plan(files, fake), 0)
            manifest = json.loads((files.state / "manifest.json").read_text())
        deployment = identity_deployment()
        deploy.check_manifest(manifest, deployment, OPERATOR, "oracle")
        with self.assertRaises(deploy.PlanError):
            deploy.check_manifest(manifest, deployment, OPERATOR, "jury")
        with self.assertRaises(deploy.PlanError):
            deploy.check_manifest(manifest, identity_deployment(identity_hash="1e" * 28), OPERATOR, "oracle")
        with self.assertRaises(deploy.PlanError):
            deploy.check_manifest(manifest, identity_deployment(refs={"identity": "ce" * 32 + "#1"}),
                                  OPERATOR, "oracle")
        with self.assertRaises(deploy.PlanError):
            deploy.check_manifest(manifest, deployment, b"\x0b" * 28, "oracle")


class SealTest(unittest.TestCase):
    def test_seal_datum_is_the_byte_string_of_m1(self) -> None:
        m1 = "e1" * 28
        self.assertTrue(deploy.seal_matches(deploy.encode(bytes.fromhex(m1)).hex(), m1))
        self.assertFalse(deploy.seal_matches(deploy.encode(b"\xe2" * 28).hex(), m1))
        with self.assertRaises(ValueError):
            deploy.seal_matches(deploy.encode(b"\xe1" * 27).hex(), m1)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
