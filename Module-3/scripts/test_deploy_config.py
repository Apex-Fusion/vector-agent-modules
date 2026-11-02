#!/usr/bin/env python3
"""
Module 3: offline checks of the deploy inputs and the ReputationConfig layout

No chain connection, no keys and no aiken CLI. Needs cbor2.

Checks:
  - deployment_inputs_from_env reads the five inputs. It refuses missing,
    malformed, zero and retired values, and names the deployment order.
  - build_reputation_config_cbor gives constructor 0 with 12 fields in the
    order of reputation-staking/lib/reputation_staking/config.ak, with
    identity_hash last. It refuses a config without identity_hash or a
    Module-1 value.
  - resume_state_error refuses a state of the legacy registry layout and a
    state with other input values.

Usage:
    python3 scripts/test_deploy_config.py
"""

import sys

import cbor2

from deploy_common import (
    DEPLOYMENT_INPUTS,
    RETIRED_VALUES,
    DeployInputError,
    build_reputation_config_cbor,
    deployment_inputs_from_env,
    deployment_inputs_state,
    resume_state_error,
)

FAILED = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        FAILED.append(name)


def input_error(fn, *args, **kwargs) -> str:
    """The DeployInputError message of fn(*args, **kwargs), or "" when it
    raises none."""
    try:
        fn(*args, **kwargs)
    except DeployInputError as e:
        return str(e)
    return ""


def dummy(byte: int) -> str:
    """A 28-byte dummy hash as hex: `byte` repeated."""
    return (bytes([byte]) * 28).hex()


ENV = {
    "VECTOR_MODULE1_CLAIM_HASH": dummy(0x11),
    "VECTOR_MODULE1_CHALLENGE_HASH": dummy(0x12),
    "VECTOR_MODULE1_JURY_POOL_HASH": dummy(0x13),
    "VECTOR_MODULE1_REFS_TOKEN_POLICY": dummy(0x14),
    "VECTOR_IDENTITY_HASH": dummy(0x1D),
}
OWNER = dummy(0x0C)


def build(inputs: dict) -> str:
    return build_reputation_config_cbor(
        refs_token_policy=dummy(0x01),
        params_script_hash=dummy(0x06),
        treasury_script_hash=dummy(0x07),
        ap3x_policy_id="",
        ap3x_asset_name="",
        oracle_credential=OWNER,
        params_token_policy=dummy(0x0B),
        **inputs,
    )


def main() -> int:
    print("Module-3 deploy inputs and ReputationConfig")

    inputs = deployment_inputs_from_env(ENV)
    check("the five inputs are read", list(inputs) == list(DEPLOYMENT_INPUTS))
    upper = {name: value.upper() for name, value in ENV.items()}
    check("upper-case hex is kept as lower case", deployment_inputs_from_env(upper) == inputs)

    config = cbor2.loads(bytes.fromhex(build(inputs)))
    check("constructor 0", config.tag == 121)
    check("12 fields", len(config.value) == 12)
    check("field order, identity_hash last", [field.hex() for field in config.value] == [
        dummy(0x01),  # refs_token_policy
        dummy(0x11),  # audit_claim_validator_hash
        dummy(0x12),  # audit_challenge_validator_hash
        dummy(0x13),  # audit_jury_pool_hash
        dummy(0x14),  # audit_refs_token_policy
        dummy(0x06),  # params_script_hash
        dummy(0x07),  # treasury_script_hash
        "",           # ap3x_policy_id
        "",           # ap3x_asset_name
        OWNER,        # oracle_credential
        dummy(0x0B),  # params_token_policy
        dummy(0x1D),  # identity_hash
    ])

    error = input_error(deployment_inputs_from_env, {})
    check("no inputs: the error names each variable",
          all(env_name in error for env_name, _, _ in DEPLOYMENT_INPUTS.values()))
    check("no inputs: the error names the deployment order", "Deployment order" in error)

    refused = ["", "zz" * 28, "ab" * 27, "00" * 28, *RETIRED_VALUES]
    for field, (env_name, _, _) in DEPLOYMENT_INPUTS.items():
        errors = [
            input_error(deployment_inputs_from_env, dict(ENV, **{env_name: value}))
            for value in refused
        ]
        check(f"{env_name}: missing, malformed, zero and retired values are refused",
              all(env_name in e and "Deployment order" in e for e in errors))
        error = input_error(build, dict(inputs, **{field: ""}))
        check(f"config without {field}: refused with the deployment order",
              field in error and "Deployment order" in error)

    old_claim = "a9d22e8b01d282be8007b8d9e3e8af548aaa56f1c3e433c0eddd8760"
    error = input_error(build, dict(inputs, audit_claim_validator_hash=old_claim))
    check("config with the old mainnet Module-1 claim hash: refused",
          "old mainnet Module-1 claim validator hash" in error)

    entries = deployment_inputs_state(inputs)
    check("deploy state entries", entries == {
        "module1_claim_validator_hash": dummy(0x11),
        "module1_challenge_validator_hash": dummy(0x12),
        "module1_jury_pool_hash": dummy(0x13),
        "module1_refs_token_policy": dummy(0x14),
        "identity_hash": dummy(0x1D),
    })

    saved = {
        "holder_owner_vkey_hash": OWNER,
        "params_token_policy": dummy(0x0B),
        "record_token_seed": "aa" * 32 + "#0",
        "tx_hashes": {"params_utxo": "bb" * 32},
        **entries,
    }
    check("resume with the same inputs: continues", resume_state_error(saved, OWNER, inputs) is None)
    legacy = {key: value for key, value in saved.items() if key not in entries}
    legacy.update({
        "agent_registry_hash": "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01",
        "module1_refs_token_policy": "205d5f77ffebf60b764ba4f1873eff3764f3d1d594e5dac477a928f9",
    })
    check("resume of a legacy registry state: refused",
          "legacy agent registry" in (resume_state_error(legacy, OWNER, inputs) or ""))
    for key in entries:
        other = dict(saved, **{key: dummy(0x2E)})
        check(f"resume with another {key}: refused",
              key in (resume_state_error(other, OWNER, inputs) or ""))

    print("\nAll checks passed." if not FAILED else f"\n{len(FAILED)} checks failed.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
