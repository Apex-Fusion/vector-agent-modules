"""Tests for reputation_staking.deployment: the deploy state check."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cbor2
import pytest

from reputation_staking.deployment import (
    DEPLOYMENT_ORDER,
    DeploymentStateError,
    load_deployment,
)
from reputation_staking.utils import script_hash_to_address
from tests.chain_fixtures import (
    CLAIM_HASH,
    CROSS_REFS,
    END_REF,
    IDENTITY_HASH,
    PARAMS_HOLDER,
    REP_REF,
    TREASURY,
    deploy_state,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import deploy_common  # noqa: E402

ORACLE = bytes.fromhex("0f" * 28)
LEGACY = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
ORDER = "identity registry, Module-1, Module-3, Module-6"


def test_new_state_loads():
    d = load_deployment(deploy_state(ORACLE))
    assert d.identity_hash == IDENTITY_HASH
    assert d.identity_address == script_hash_to_address(IDENTITY_HASH)
    assert d.identity_address.startswith("addr1w")
    assert d.params_holder_address == script_hash_to_address(PARAMS_HOLDER)
    assert d.treasury_holder_address == script_hash_to_address(TREASURY)
    assert d.oracle_vkey_hash == ORACLE.hex()
    assert (d.reputation_spend_ref, d.endorsement_spend_ref, d.cross_refs_nft) == (
        REP_REF, END_REF, CROSS_REFS,
    )
    assert d.module1_claim_validator_hash == CLAIM_HASH


def test_state_file_loads(tmp_path):
    path = tmp_path / "deploy_state.json"
    path.write_text(json.dumps(deploy_state(ORACLE)))
    assert load_deployment(path).identity_hash == IDENTITY_HASH


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(DeploymentStateError, match="does not exist") as exc:
        load_deployment(tmp_path / "deploy_state.json")
    assert ORDER in str(exc.value)


def test_old_state_with_legacy_registry_is_refused():
    old = deploy_state(ORACLE)
    del old["identity_hash"]
    old["agent_registry_hash"] = LEGACY
    with pytest.raises(DeploymentStateError) as exc:
        load_deployment(old, source="deploy/deploy_state.json")
    message = str(exc.value)
    assert message.startswith("deploy/deploy_state.json:")
    assert "agent_registry_hash" in message
    assert "Move the file to a different path" in message
    assert ORDER in message and DEPLOYMENT_ORDER in message


def test_state_without_identity_hash_is_refused():
    state = deploy_state(ORACLE)
    del state["identity_hash"]
    with pytest.raises(DeploymentStateError, match="no identity_hash"):
        load_deployment(state)


def test_state_without_record_tokens_is_refused():
    state = deploy_state(ORACLE)
    del state["params_token_policy"]
    with pytest.raises(DeploymentStateError, match="record tokens"):
        load_deployment(state)


def test_legacy_registry_hash_as_identity_hash_is_refused():
    with pytest.raises(DeploymentStateError, match="legacy agent registry"):
        load_deployment(deploy_state(ORACLE, identity_hash=LEGACY))


@pytest.mark.parametrize("key, value", [
    ("module1_jury_pool_hash", "ab" * 27),
    ("treasury_holder_hash", "zz" * 28),
    ("wallet_vkey_hash", None),
])
def test_malformed_or_missing_hash_is_refused(key, value):
    state = deploy_state(ORACLE)
    if value is None:
        del state[key]
    else:
        state[key] = value
    with pytest.raises(DeploymentStateError, match=key):
        load_deployment(state)


def test_address_that_does_not_match_its_hash_is_refused():
    state = deploy_state(ORACLE, treasury_holder_address=script_hash_to_address("cd" * 28))
    with pytest.raises(DeploymentStateError, match="treasury_holder_address"):
        load_deployment(state)


def test_incomplete_deploy_run_is_refused():
    state = deploy_state(ORACLE)
    del state["tx_hashes"]["cross_refs_nft"]
    with pytest.raises(DeploymentStateError, match="cross_refs_nft"):
        load_deployment(state)


def test_reputation_config_matches_deploy_scripts():
    """The config that the SDK derives from a deploy state is the config of
    deploy_common for the same values (12 fields, identity_hash last)."""
    d = load_deployment(deploy_state(ORACLE))
    expected = deploy_common.build_reputation_config_cbor(
        refs_token_policy=d.refs_token_policy,
        audit_claim_validator_hash=d.module1_claim_validator_hash,
        audit_challenge_validator_hash=d.module1_challenge_validator_hash,
        audit_jury_pool_hash=d.module1_jury_pool_hash,
        audit_refs_token_policy=d.module1_refs_token_policy,
        params_script_hash=d.params_holder_hash,
        treasury_script_hash=d.treasury_holder_hash,
        ap3x_policy_id="",
        ap3x_asset_name="",
        oracle_credential=ORACLE.hex(),
        params_token_policy=d.params_token_policy,
        identity_hash=d.identity_hash,
    )
    assert cbor2.loads(d.reputation_config().to_cbor()) == cbor2.loads(bytes.fromhex(expected))
