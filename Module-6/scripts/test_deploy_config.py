#!/usr/bin/env python3
"""
Module 6: offline checks of the deploy inputs, the holder tags, the
GovernanceConfig layout, the datum builders and the deploy state values

No chain connection, no keys and no aiken CLI. Needs cbor2 (deploy_common,
vector_identity and Module-1/scripts/identity_deploy.py need no more).

Checks:
  - HOLDER_TAGS are the Module-6 tags of shared/python/holder_scripts.py:
    params 61, oracle 62, treasury 63. They differ from the tags 1, 2, 3 of
    the previous deployment and from the Module-1 (11) and Module-3 (31, 33)
    tags.
  - load_identity_deployment reads the identity deployment.json with the
    rules of Module-1: it refuses another network, a zero or legacy
    identity_hash, a reference address that can spend, and the same operator
    and seal-seed address. check_operator_address refuses another key.
  - build_governance_config_cbor gives tag 121 and a definite array of 11
    byte strings (head 0x8b), in the order of the Aiken type
    GovernanceConfig, with identity_hash last and reputation_validator_hash
    and jury_pool_hash zero. It refuses a missing, malformed, zero or legacy
    identity_hash and names the deployment order.
  - governance_params_data gives the 21 GovernanceParams fields in the order
    of the Aiken type, with the values of the v8 deployment and the three
    Phase 1.2 values. check_governance_params refuses a missing, unknown or
    bad field.
  - oracle_datum_data gives GovernanceOracleDatum (3 fields): the
    VerificationKey credential of the operator, the treasury holder hash and
    oracle_active True. treasury_batch_data gives TreasuryBatchDatum (2
    fields).
  - build_cross_refs_data gives constructor 0 with 5 fields in the order of
    the Aiken type GovernanceCrossRefs, with endorsement_validator_hash last.
    It refuses a missing, short or zero hash.
  - build_activity_head_data gives the head of the empty activity list:
    ProposerActivityDatum (constructor 0, 5 fields) with agent_did = b"",
    the deployer credential, count 0, last_proposal_slot 0 and
    next_did = b"". The record token name of the head is `activity`.
  - plutus_data_cbor writes the Plutus encoding (indefinite arrays).
  - state_tx_hashes and client_settings give the tx_hashes keys of the
    earlier deploy states and the client values; a value of a step stays
    None until the state records the step.
  - redeploy_state_error refuses a redeploy (scripts/redeploy_proposal.py)
    of a deployment with the head of the activity list: in the deploy state
    or in the outputs at the proposal_spend address.

Usage:
    python3 scripts/test_deploy_config.py
"""

import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cbor2

from deploy_common import (
    ACTIVITY_TOKEN_NAME,
    DEFAULT_GOVERNANCE_PARAMS,
    GOVERNANCE_PARAMS_FIELDS,
    HOLDER_TAGS,
    LEGACY_REGISTRY_HASH,
    ORACLE_TOKEN_NAME,
    PARAMS_TOKEN_NAME,
    REFS_TOKEN_NAME,
    VALIDATOR_TITLES,
    DeployInputError,
    _module1_identity_deploy,
    build_activity_head_data,
    build_cross_refs_data,
    build_governance_config_cbor,
    check_governance_params,
    check_operator_address,
    client_settings,
    governance_params_data,
    governance_params_from_data,
    load_identity_deployment,
    oracle_datum_data,
    plutus_data_cbor,
    redeploy_state_error,
    state_tx_hashes,
    treasury_batch_data,
)
from holder_scripts import HOLDER_TAGS as HOLDER_TAGS_BY_MODULE

TYPES_AK = (Path(__file__).resolve().parent.parent / "contracts" / "governance-suggestion" / "lib"
            / "governance_suggestion" / "types.ak")

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


def value_error(fn, *args) -> bool:
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def dummy(byte: int) -> str:
    """A 28-byte dummy hash as hex: `byte` repeated."""
    return (bytes([byte]) * 28).hex()


def aiken_fields(type_name: str) -> list:
    """The field names of a record type of types.ak, in order."""
    source = TYPES_AK.read_text()
    match = re.search(r"pub type " + type_name + r" \{(.*?)\n\}", source, re.S)
    return re.findall(r"^\s+(\w+):", match.group(1), re.M) if match else []


ZERO = "00" * 28
IDENTITY = dummy(0x1D)
OWNER = dummy(0x0C)
M1 = _module1_identity_deploy()


def address(key_hash: bytes) -> str:
    """The enterprise key address (network id 1) of a key hash."""
    return M1.bech32("addr", b"\x61" + key_hash)


def identity_deployment(**changes) -> dict:
    data = {
        "format": "vector-identity-deployment/1",
        "network": "mainnet",
        "identity_hash": IDENTITY,
        "config_policy": dummy(0xCF),
        "config_holder": dummy(0xC4),
        "reference_address": address(bytes(28)),
        "operator_address": address(bytes.fromhex(OWNER)),
        "seal_seed_address": address(b"\x5e" * 28),
        "config_seed": "AB" * 32 + "#0",
        "refs": {"identity": "cd" * 32 + "#0"},
        "seal": None,
    }
    data.update(changes)
    return data


def load(data: dict, network: str = "mainnet"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "deployment.json"
        path.write_text(json.dumps(data))
        return load_identity_deployment(path, network)


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
    print("Module-6 holder tags")
    check("tags 61, 62, 63 from holder_scripts.HOLDER_TAGS['module6']",
          HOLDER_TAGS == [(61, "params"), (62, "oracle"), (63, "treasury")]
          and {tag for tag, _ in HOLDER_TAGS} == set(HOLDER_TAGS_BY_MODULE["module6"].values()))
    other = {1, 2, 3} | set(HOLDER_TAGS_BY_MODULE["module1"].values()) | set(HOLDER_TAGS_BY_MODULE["module3"].values())
    check("no tag of the previous deployment (1, 2, 3), Module-1 (11) or Module-3 (31, 33)",
          not other & {tag for tag, _ in HOLDER_TAGS})

    print("\nModule-6 identity deployment input")
    identity = load(identity_deployment())
    check("the values of the identity deployment",
          identity["identity_hash"] == IDENTITY
          and identity["reference_address"] == address(bytes(28))
          and identity["operator_address"] == address(bytes.fromhex(OWNER))
          and identity["seal_seed_address"] == address(b"\x5e" * 28)
          and identity["network"] == "mainnet" and identity["sealed"] is False)
    check("config_seed in lower case", identity["config_seed"] == "ab" * 32 + "#0")
    check("refs.identity is not needed", load(identity_deployment(refs={}))["identity_hash"] == IDENTITY)
    for label, data, network, expected in [
        ("another network", identity_deployment(), "testnet", "network"),
        ("another format", identity_deployment(format="other/1"), "mainnet", "identity deployment file"),
        ("a zero identity_hash", identity_deployment(identity_hash=ZERO), "mainnet", "all zero"),
        ("the legacy identity_hash", identity_deployment(identity_hash=LEGACY_REGISTRY_HASH), "mainnet",
         "legacy agent registry"),
        ("a reference address that can spend", identity_deployment(reference_address=address(b"\x01" * 28)),
         "mainnet", "reference_address"),
        ("the same operator and seal-seed address",
         identity_deployment(seal_seed_address=address(bytes.fromhex(OWNER))), "mainnet", "must differ"),
        ("a bad config_seed", identity_deployment(config_seed="ab#0"), "mainnet", "config_seed"),
    ]:
        check(f"identity deployment with {label}: refused", expected in input_error(load, data, network))
    check("the operator key of operator_address: accepted",
          input_error(check_operator_address, identity, bytes.fromhex(OWNER)) == "")
    check("another operator key: refused",
          "is not the key of the operator address" in input_error(check_operator_address, identity,
                                                                  bytes.fromhex(dummy(0x0D))))

    print("\nModule-6 GovernanceConfig")
    raw = bytes.fromhex(build(IDENTITY))
    check("tag 121 and a definite array of 11 items (d8 79 8b)", raw[:3] == b"\xd8\x79\x8b")
    config = cbor2.loads(raw)
    check("constructor 0", config.tag == 121)
    check("11 fields, as the Aiken type", len(config.value) == 11 == len(aiken_fields("GovernanceConfig")))
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
    check("the Aiken field names end with record_token_policy, identity_hash",
          aiken_fields("GovernanceConfig")[-2:] == ["record_token_policy", "identity_hash"])
    for label, value in [
        ("empty", ""),
        ("not hex", "zz" * 28),
        ("27 bytes", "ab" * 27),
        ("all zero", ZERO),
        ("legacy registry hash", LEGACY_REGISTRY_HASH),
    ]:
        check(f"identity_hash {label}: refused with the deployment order",
              "Deployment order" in input_error(build, value))

    print("\nModule-6 GovernanceParams")
    params = governance_params_data()
    check("21 fields in the order of the Aiken type",
          list(GOVERNANCE_PARAMS_FIELDS) == aiken_fields("GovernanceParams") and len(params.value) == 21)
    check("constructor 0 with the default values",
          params.tag == 121 and params.value == list(DEFAULT_GOVERNANCE_PARAMS.values()))
    v8 = {
        "min_proposal_stake": 25_000_000, "min_critique_stake": 5_000_000,
        "min_governance_endorsement": 10_000_000, "min_review_window": 259_200_000,
        "max_review_window": 2_419_200_000, "max_amendments": 5, "max_active_proposals": 3,
        "proposal_cooldown": 86_400_000, "proposer_reward_share": 7_000, "critic_reward_share": 2_000,
        "protocol_fee_rate": 1_000, "min_adoption_reward": 50_000_000, "max_adoption_reward": 500_000_000,
        "max_incorporated_critiques": 10, "max_treasury_request": 10_000_000_000,
        "emergency_stake_multiplier": 5_000, "emergency_review_window": 43_200_000,
        "param_execution_delay": 604_800_000,
    }
    check("the values of the v8 deployment", all(DEFAULT_GOVERNANCE_PARAMS[k] == v for k, v in v8.items()))
    check("the Phase 1.2 values",
          (DEFAULT_GOVERNANCE_PARAMS["min_prediction_pool"], DEFAULT_GOVERNANCE_PARAMS["credibility_pool_low_threshold"],
           DEFAULT_GOVERNANCE_PARAMS["credibility_pool_critical_threshold"]) == (100_000_000, 500_000_000, 100_000_000))
    check("decode gives the values again",
          governance_params_from_data(cbor2.loads(plutus_data_cbor(params))) == DEFAULT_GOVERNANCE_PARAMS)
    missing = {k: v for k, v in DEFAULT_GOVERNANCE_PARAMS.items() if k != "max_amendments"}
    check("a missing field: refused", "missing max_amendments" in input_error(check_governance_params, missing))
    check("an unknown field: refused",
          "unknown extra" in input_error(check_governance_params, dict(DEFAULT_GOVERNANCE_PARAMS, extra=1)))
    check("a negative or bool value: refused",
          "max_amendments" in input_error(check_governance_params, dict(DEFAULT_GOVERNANCE_PARAMS, max_amendments=-1))
          and "max_amendments" in input_error(check_governance_params,
                                              dict(DEFAULT_GOVERNANCE_PARAMS, max_amendments=True)))

    print("\nModule-6 oracle and treasury batch datums")
    oracle = oracle_datum_data(OWNER, dummy(0x07))
    check("oracle: constructor 0 with 3 fields, as the Aiken type",
          oracle.tag == 121 and len(oracle.value) == 3 == len(aiken_fields("GovernanceOracleDatum")))
    credential, treasury, active = oracle.value
    check("oracle: VerificationKey credential of the operator",
          credential.tag == 121 and [c.hex() for c in credential.value] == [OWNER])
    check("oracle: the treasury holder hash", treasury.hex() == dummy(0x07))
    check("oracle: oracle_active is True (constructor 1)", active == cbor2.CBORTag(122, []))
    check("oracle: a 27-byte key hash: refused", value_error(oracle_datum_data, "ab" * 27, dummy(0x07)))
    batch = treasury_batch_data(2, True)
    check("treasury batch: constructor 0 with 2 fields, as the Aiken type",
          batch.tag == 121 and len(batch.value) == 2 == len(aiken_fields("TreasuryBatchDatum")))
    check("treasury batch: batch_id and active True", batch.value == [2, cbor2.CBORTag(122, [])])
    check("treasury batch: inactive is constructor 0", treasury_batch_data(2, False).value[1] == cbor2.CBORTag(121, []))
    check("treasury batch: id 0 refused", value_error(treasury_batch_data, 0))
    check("Plutus encoding: indefinite arrays (9f ... ff)",
          plutus_data_cbor(batch) == bytes.fromhex("d8799f02d87a80ff")
          and cbor2.loads(plutus_data_cbor(oracle)) == oracle)

    print("\nModule-6 GovernanceCrossRefs datum")
    refs = build_cross_refs_data(
        dummy(0x11), dummy(0x12), dummy(0x13), dummy(0x14), dummy(0x15),
    )
    check("cross refs: constructor 0", refs.tag == 121)
    check("cross refs: 5 fields, as the Aiken type", len(refs.value) == 5 == len(aiken_fields("GovernanceCrossRefs")))
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
    check("cross refs: the token name", REFS_TOKEN_NAME == b"governance_refs")
    for label, value in [
        ("missing", ""),
        ("27 bytes", "ab" * 27),
        ("all zero", ZERO),
    ]:
        check(f"cross refs: endorsement_validator_hash {label}: refused",
              value_error(build_cross_refs_data, dummy(0x11), dummy(0x12), dummy(0x13), dummy(0x14), value))

    print("\nModule-6 head of the activity list")
    head = build_activity_head_data(OWNER)
    check("activity head: constructor 0", head.tag == 121)
    check("activity head: 5 fields (ProposerActivityDatum)",
          len(head.value) == 5 == len(aiken_fields("ProposerActivityDatum")))
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
    check("activity head: a 27-byte deployer key hash: refused", value_error(build_activity_head_data, "ab" * 27))

    print("\nModule-6 deploy state values")
    tx = {name: bytes([0xE0 + i]).hex() * 32 for i, name in enumerate(
        ["records", "holder", "proposal_spend", "proposal_mint", "cross-refs", "treasury", "update"])}
    steps = {
        "records": {"tx": tx["records"], "time": "2026-11-03T10:00:00Z", "params": tx["records"] + "#0",
                    "oracle": tx["records"] + "#1", "activity_head": tx["records"] + "#2"},
        "publish-holder-params": {"tx": tx["holder"], "time": "2026-11-03T10:00:01Z", "ref": tx["holder"] + "#0"},
        "publish-proposal_spend": {"tx": tx["proposal_spend"], "time": "2026-11-03T10:00:02Z",
                                   "ref": tx["proposal_spend"] + "#0"},
        "treasury-4": {"tx": tx["treasury"], "time": "2026-11-03T10:00:03Z", "batch": tx["treasury"] + "#0"},
    }
    hashes = state_tx_hashes(steps)
    check("tx_hashes with the key names of the earlier states", hashes == {
        "params_utxo": tx["records"], "oracle_utxo": tx["records"], "activity_head": tx["records"],
        "holder_params_ref": tx["holder"], "proposal_spend_ref": tx["proposal_spend"],
        "treasury_batch_4": tx["treasury"],
    })
    state = {
        "identity_hash": IDENTITY, "record_token_policy": dummy(0x0A), "refs_token_policy": dummy(0x01),
        "holders": {role: {"hash": dummy(0x05 + i), "address": f"addr_{role}"}
                    for i, role in enumerate(("oracle", "params", "treasury"))},
        "validators": {title: {"hash": dummy(0x20 + i)} for i, title in enumerate(VALIDATOR_TITLES)},
        "cross_refs_address": "addr_oracle", "steps": steps,
    }
    settings = client_settings(state)
    mcp, sdk = settings["mcp-server"], settings["agent-sdk-py"]
    check("mcp-server: the listed GOV_* values", {
        "GOV_PROPOSAL_SPEND_HASH", "GOV_PROPOSAL_MINT_HASH", "GOV_CRITIQUE_SPEND_HASH", "GOV_CRITIQUE_MINT_HASH",
        "GOV_ENDORSEMENT_SPEND_HASH", "GOV_TREASURY_ADDRESS", "GOV_PROPOSAL_SPEND_REF", "GOV_PROPOSAL_MINT_REF",
        "GOV_PARAMS_UTXO", "GOV_ORACLE_UTXO", "GOV_CROSSREFS_UTXO"} <= set(mcp))
    check("mcp-server: values of the plan and of the recorded steps",
          mcp["GOV_PROPOSAL_SPEND_HASH"] == state["validators"]["proposal.proposal_spend.spend"]["hash"]
          and mcp["GOV_TREASURY_ADDRESS"] == "addr_treasury"
          and mcp["GOV_PROPOSAL_SPEND_REF"] == tx["proposal_spend"] + "#0"
          and mcp["GOV_PARAMS_UTXO"] == tx["records"] + "#0" and mcp["GOV_ORACLE_UTXO"] == tx["records"] + "#1")
    check("mcp-server: a step that is not recorded gives None",
          mcp["GOV_PROPOSAL_MINT_REF"] is None and mcp["GOV_CROSSREFS_UTXO"] is None)
    check("agent-sdk-py: VECTOR_MODULE6_* values",
          sdk["VECTOR_MODULE6_IDENTITY_HASH"] == IDENTITY
          and sdk["VECTOR_MODULE6_PARAMS_HOLDER_HASH"] == dummy(0x06)
          and sdk["VECTOR_MODULE6_PROPOSAL_SPEND_REF_UTXO"] == tx["proposal_spend"] + "#0"
          and all(name.startswith("VECTOR_MODULE6_") for name in sdk))
    steps["params-update-1"] = {"tx": tx["update"], "time": "2026-11-03T11:00:00Z", "params": tx["update"] + "#0",
                                "governance_params": dict(DEFAULT_GOVERNANCE_PARAMS)}
    check("a newer params-update gives the params record",
          state_tx_hashes(steps)["params_utxo"] == tx["update"]
          and client_settings(state)["mcp-server"]["GOV_PARAMS_UTXO"] == tx["update"] + "#0")

    print("\nModule-6 redeploy of a deployment with an activity list")
    saved = {
        "holder_owner_vkey_hash": OWNER,
        "record_token_policy": dummy(0x0A),
        "record_token_seed": "aa" * 32 + "#0",
        "tx_hashes": {"params_utxo": "bb" * 32, "oracle_utxo": "bb" * 32, "activity_head": "bb" * 32},
        "identity_hash": IDENTITY,
    }
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
