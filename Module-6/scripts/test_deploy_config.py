#!/usr/bin/env python3
"""
Module 6: offline checks of the identity_hash input, the GovernanceConfig
layout, the GovernanceCrossRefs layout and the head of the activity list

No chain connection, no keys and no aiken CLI. Needs cbor2.

Checks:
  - identity_hash_from_env reads VECTOR_IDENTITY_HASH. It refuses missing,
    malformed, zero and legacy registry values, and names the deployment
    order.
  - build_governance_config_cbor gives constructor 0 with 11 fields in the
    order of the Aiken type GovernanceConfig, with identity_hash last and
    reputation_validator_hash and jury_pool_hash zero. It refuses a config
    without a valid identity_hash.
  - resume_state_error refuses a state of the legacy registry layout, a
    state with another identity_hash and a state whose records have no head
    of the activity list.
  - build_cross_refs_data gives constructor 0 with 5 fields in the order of
    the Aiken type GovernanceCrossRefs, with endorsement_validator_hash last.
    It refuses a missing, short or zero hash.
  - build_activity_head_data gives the head of the empty activity list:
    ProposerActivityDatum (constructor 0, 5 fields) with agent_did = b"",
    the deployer credential, count 0, last_proposal_slot 0 and
    next_did = b"". The record token name of the head is `activity`.
  - redeploy_state_error refuses a redeploy (scripts/redeploy_proposal.py)
    of a deployment with the head of the activity list: in the deploy state
    or in the outputs at the proposal_spend address.

Usage:
    python3 scripts/test_deploy_config.py
"""

import sys
from types import SimpleNamespace

import cbor2

from deploy_common import (
    ACTIVITY_TOKEN_NAME,
    IDENTITY_HASH_ENV,
    LEGACY_REGISTRY_HASH,
    ORACLE_TOKEN_NAME,
    PARAMS_TOKEN_NAME,
    DeployInputError,
    build_activity_head_data,
    build_cross_refs_data,
    build_governance_config_cbor,
    identity_hash_from_env,
    redeploy_state_error,
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


ZERO = "00" * 28
IDENTITY = dummy(0x1D)
OWNER = dummy(0x0C)


def proposal_output(policy: str, name: bytes):
    """An output at the proposal_spend address with one token (policy, name),
    in the shape of a pycardano UTxO."""
    amount = SimpleNamespace(coin=2_000_000, multi_asset={bytes.fromhex(policy): {name: 1}})
    return SimpleNamespace(
        input=SimpleNamespace(transaction_id=b"\xcc" * 32, index=2),
        output=SimpleNamespace(amount=amount),
    )


def build(identity_hash: str) -> str:
    return build_governance_config_cbor(
        refs_token_policy=dummy(0x01),
        oracle_holder_hash=dummy(0x05),
        params_holder_hash=dummy(0x06),
        treasury_holder_hash=dummy(0x07),
        record_token_policy=dummy(0x0A),
        identity_hash=identity_hash,
    )


def main() -> int:
    print("Module-6 identity_hash input and GovernanceConfig")

    identity_hash = identity_hash_from_env({IDENTITY_HASH_ENV: IDENTITY.upper()})
    check("identity_hash is kept as lower-case hex", identity_hash == IDENTITY)

    config = cbor2.loads(bytes.fromhex(build(identity_hash)))
    check("constructor 0", config.tag == 121)
    check("11 fields", len(config.value) == 11)
    check("field order, identity_hash last", [field.hex() for field in config.value] == [
        dummy(0x01),  # refs_token_policy
        ZERO,         # prediction_validator_hash
        ZERO,         # reputation_validator_hash
        ZERO,         # jury_pool_hash
        dummy(0x05),  # governance_oracle_hash
        dummy(0x06),  # governance_params_hash
        dummy(0x07),  # governance_treasury_hash
        ZERO,         # credibility_pool_hash
        ZERO,         # protocol_params_hash
        dummy(0x0A),  # record_token_policy
        IDENTITY,     # identity_hash
    ])

    error = input_error(identity_hash_from_env, {})
    check("unset: refused with the variable and the deployment order",
          IDENTITY_HASH_ENV in error and "Deployment order" in error)
    for label, value in [
        ("empty", ""),
        ("not hex", "zz" * 28),
        ("27 bytes", "ab" * 27),
        ("all zero", ZERO),
        ("legacy registry hash", LEGACY_REGISTRY_HASH),
    ]:
        env_error = input_error(identity_hash_from_env, {IDENTITY_HASH_ENV: value})
        build_error = input_error(build, value)
        check(f"{label}: refused by the input and by the config builder",
              IDENTITY_HASH_ENV in env_error and "Deployment order" in env_error
              and "Deployment order" in build_error)

    saved = {
        "holder_owner_vkey_hash": OWNER,
        "record_token_policy": dummy(0x0A),
        "record_token_seed": "aa" * 32 + "#0",
        "tx_hashes": {"params_utxo": "bb" * 32, "oracle_utxo": "bb" * 32, "activity_head": "bb" * 32},
        "identity_hash": IDENTITY,
    }
    check("resume with the same identity_hash: continues",
          resume_state_error(saved, OWNER, IDENTITY) is None)
    legacy = {key: value for key, value in saved.items() if key != "identity_hash"}
    legacy["agent_registry_hash"] = LEGACY_REGISTRY_HASH
    check("resume of a legacy registry state: refused",
          "legacy agent registry" in (resume_state_error(legacy, OWNER, IDENTITY) or ""))
    check("resume with another identity_hash: refused",
          "identity_hash" in (resume_state_error(saved, OWNER, dummy(0x2E)) or ""))
    without_head = dict(saved, tx_hashes={"params_utxo": "bb" * 32, "oracle_utxo": "bb" * 32})
    check("resume of records without the activity head: refused",
          "activity list" in (resume_state_error(without_head, OWNER, IDENTITY) or ""))
    check("resume before the record step: continues",
          resume_state_error(dict(saved, tx_hashes={}), OWNER, IDENTITY) is None)

    print("\nModule-6 GovernanceCrossRefs datum")
    refs = build_cross_refs_data(
        dummy(0x11), dummy(0x12), dummy(0x13), dummy(0x14), dummy(0x15),
    )
    check("cross refs: constructor 0", refs.tag == 121)
    check("cross refs: 5 fields", len(refs.value) == 5)
    check("cross refs: field order, endorsement_validator_hash last",
          [field.hex() for field in refs.value] == [
              dummy(0x11),  # proposal_validator_hash
              dummy(0x12),  # critique_validator_hash
              dummy(0x13),  # proposal_mint_hash
              dummy(0x14),  # critique_mint_hash
              dummy(0x15),  # endorsement_validator_hash
          ])
    check("cross refs: CBOR round trip",
          cbor2.loads(cbor2.dumps(refs)).value == refs.value)
    for label, value in [
        ("missing", ""),
        ("27 bytes", "ab" * 27),
        ("all zero", ZERO),
    ]:
        try:
            build_cross_refs_data(
                dummy(0x11), dummy(0x12), dummy(0x13), dummy(0x14), value,
            )
            refused = False
        except ValueError:
            refused = True
        check(f"cross refs: endorsement_validator_hash {label}: refused", refused)

    print("\nModule-6 head of the activity list")
    head = build_activity_head_data(OWNER)
    check("activity head: constructor 0", head.tag == 121)
    check("activity head: 5 fields (ProposerActivityDatum)", len(head.value) == 5)
    agent_did, credential, count, last, next_did = head.value
    check("activity head: agent_did is empty", agent_did == b"")
    check("activity head: VerificationKey credential of the deployer",
          credential.tag == 121 and [c.hex() for c in credential.value] == [OWNER])
    check("activity head: count 0 and last_proposal_slot 0", (count, last) == (0, 0))
    check("activity head: next_did is empty (empty list)", next_did == b"")
    check("activity head: CBOR round trip",
          cbor2.loads(cbor2.dumps(head)) == head)
    check("activity head: record token name `activity`, distinct from the other records",
          ACTIVITY_TOKEN_NAME == b"activity"
          and len({ACTIVITY_TOKEN_NAME, PARAMS_TOKEN_NAME, ORACLE_TOKEN_NAME}) == 3)
    try:
        build_activity_head_data("ab" * 27)
        refused = False
    except ValueError:
        refused = True
    check("activity head: a 27-byte deployer key hash: refused", refused)

    print("\nModule-6 redeploy of a deployment with an activity list")
    head_output = proposal_output(dummy(0x0A), ACTIVITY_TOKEN_NAME)
    node_output = proposal_output(dummy(0x0B), b"pact_" + b"\x01" * 27)
    before_records = dict(saved, tx_hashes={})
    check("redeploy with the head in the deploy state: refused",
          "activity list" in (redeploy_state_error(saved) or ""))
    check("redeploy with the head on chain: refused",
          "activity list" in (redeploy_state_error(before_records, [node_output, head_output]) or ""))
    check("redeploy without a head: continues",
          redeploy_state_error(before_records, [node_output]) is None)
    check("redeploy with an `activity` token of another policy: continues",
          redeploy_state_error(before_records, [proposal_output(dummy(0x2A), ACTIVITY_TOKEN_NAME)]) is None)

    print("\nAll checks passed." if not FAILED else f"\n{len(FAILED)} checks failed.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
