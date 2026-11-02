#!/usr/bin/env python3
"""Tests of scripts/identity_deploy.py that need no aiken build.

Run: python3 scripts/test_identity_deploy.py (from Module-1/). The tests use
the Python standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
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

HASHES = {
    ("identity", "identity_hash"): "1d" * 28,
    ("identity", "config_policy"): "cf" * 28,
    ("identity", "config_holder"): "c4" * 28,
    ("module1", "oracle_vkh"): "0a" * 28,
    ("module1", "refs_token_policy"): "99" * 28,
}


def complete_config() -> dict:
    """The default configuration with test hashes and no placeholders."""
    config = json.loads(deploy.DEFAULT_CONFIG.read_text())
    config["placeholders"] = []
    for (section, name), value in HASHES.items():
        config[section][name] = value
    return config


class BuildCalled(Exception):
    """The plan went on to the aiken build."""


class PlanGuardTest(unittest.TestCase):
    def run_plan(self, config: dict, allow: bool = False) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config))
            args = argparse.Namespace(config=path, out=Path(tmp) / "out", allow_placeholders=allow)
            original = deploy.aiken

            def no_build(*_args, **_kwargs):
                raise BuildCalled()

            deploy.aiken = no_build
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    status = deploy.command_plan(args)
            finally:
                deploy.aiken = original
            return status, stderr.getvalue()

    def test_complete_config_goes_to_the_build(self) -> None:
        with self.assertRaises(BuildCalled):
            self.run_plan(complete_config())

    def test_plan_refuses_each_all_zero_hash(self) -> None:
        for section, name in deploy.NONZERO_FIELDS:
            with self.subTest(field=f"{section}.{name}"):
                config = complete_config()
                config[section][name] = "00" * 28
                status, message = self.run_plan(config)
                self.assertEqual(status, 2)
                self.assertIn(f"{section}.{name}", message)

    def test_plan_refuses_the_default_zero_values_without_placeholders_list(self) -> None:
        config = json.loads(deploy.DEFAULT_CONFIG.read_text())
        config["placeholders"] = []
        status, message = self.run_plan(config)
        self.assertEqual(status, 2)
        for section, name in deploy.NONZERO_FIELDS:
            self.assertIn(f"{section}.{name}", message)

    def test_dry_run_accepts_zero_values(self) -> None:
        config = complete_config()
        config["module1"]["refs_token_policy"] = "00" * 28
        with self.assertRaises(BuildCalled):
            self.run_plan(config, allow=True)

    def test_unsafe_values_lists_placeholders_and_zero_fields_once(self) -> None:
        config = complete_config()
        config["placeholders"] = ["identity.identity_hash", "verdict_policy"]
        config["identity"]["identity_hash"] = "00" * 28
        config["module1"]["oracle_vkh"] = "00" * 28
        self.assertEqual(
            deploy.unsafe_values(copy.deepcopy(config)),
            ["identity.identity_hash", "verdict_policy", "module1.oracle_vkh"],
        )


class PlanParameterOrderTest(unittest.TestCase):
    """identity_party gets (AuditingConfig, challenge hash, claim hash)."""

    def test_identity_party_parameters(self) -> None:
        hashes = {
            "claim": "c1" * 28,
            "challenge": "c2" * 28,
            "jury_pool": "c3" * 28,
            "identity_party": "e1" * 28,
        }
        applied: list[tuple[str, bytes]] = []

        def fake_aiken(*args: str) -> str:
            if args and args[0] == "build":
                out = deploy.CONTRACTS_DIR / args[args.index("-o") + 1]
                out.write_text("{}")
            return ""

        def fake_apply(blueprint: Path, out: Path, validator: str, parameter: bytes) -> None:
            applied.append((validator, parameter))
            out.write_text(Path(blueprint).read_text())

        def fake_hash(_blueprint: Path, validator: str) -> str:
            return hashes[validator]

        originals = (deploy.aiken, deploy.apply, deploy.blueprint_hash)
        deploy.aiken, deploy.apply, deploy.blueprint_hash = fake_aiken, fake_apply, fake_hash
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                config = complete_config()
                path.write_text(json.dumps(config))
                args = argparse.Namespace(config=path, out=Path(tmp) / "out", allow_placeholders=False)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(deploy.command_plan(args), 0)
                plan = json.loads((Path(tmp) / "out" / "plan.json").read_text())
        finally:
            deploy.aiken, deploy.apply, deploy.blueprint_hash = originals
        party = [parameter for validator, parameter in applied if validator == "identity_party"]
        self.assertEqual(
            party,
            [
                deploy.encode(deploy.auditing_config(config)),
                deploy.encode(bytes.fromhex(hashes["challenge"])),
                deploy.encode(bytes.fromhex(hashes["claim"])),
            ],
        )
        self.assertEqual(
            plan["parameters"]["identity_party_claim_hash_cbor"],
            deploy.encode(bytes.fromhex(hashes["claim"])).hex(),
        )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
