"""Offline tests of the deployment scripts.

- scripts/test_expire.py takes the node of the proposer from the activity
  list (the expiry spends that node), not the first activity node at the
  proposal validator.
- scripts/redeploy_proposal.py refuses a deployment with the head of the
  activity list: by the deploy state before step 1, and by the outputs at the
  proposal_spend address before the first transaction.
- The scripts that read a deploy state (smoke_test.py, test_expire.py,
  test_expire_e2e.py, test_validated_submit.py, update_params.py,
  treasury_replenish.py and agents/analytics_template.py) read the state of
  scripts/deploy.py on --network by default, and another state only from
  --state. The state of scripts/deploy.py has no endpoints: the network
  presets give them.
- The scripts of the previous deployment (redeploy_proposal.py,
  move_crossrefs.py, recreate_infra.py, redeploy_proposal_spend_ref.py,
  redeploy_ref_scripts.py) say so; the first two import their helpers from
  old_deploy_helpers.py, and deploy.py has no copies of them.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

import deploy
import deploy_common as dc
import governance_identity as gi
import move_crossrefs
import old_deploy_helpers
import recreate_infra
import redeploy_proposal
import redeploy_proposal_spend_ref
import redeploy_ref_scripts
import smoke_test
import test_expire
import test_expire_e2e
import test_validated_submit
import treasury_replenish
import update_params
import vector_deploy as vd
from conftest import CRITIC, ENDORSER, MODULE_ROOT, PROPOSER, deploy_state, make_world, tx_in


def _agent_template():
    """agents/analytics_template.py as a module (agents/ is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("analytics_template",
                                                  MODULE_ROOT / "agents" / "analytics_template.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analytics_template = _agent_template()

# ── scripts/test_expire.py ───────────────────────────────────────────────────


def proposal_utxos(world):
    return world.chain.utxos(world.deployment.proposal_spend.address)


@pytest.fixture
def listed(world):
    """The list head -> PROPOSER -> CRITIC, and a proposal of CRITIC."""
    world.head(next_did=PROPOSER)
    world.activity_utxo(PROPOSER, 1, 0, byte=0x80, next_did=CRITIC)
    world.activity_utxo(CRITIC, 1, 0, byte=0x81)
    proposal = world.proposal_utxo(world.proposal_datum(CRITIC))
    return world, proposal


def test_expiry_takes_the_node_of_the_proposer(listed):
    world, proposal = listed
    found, datum, node = test_expire.expiry_inputs(proposal_utxos(world), world.deployment, proposal.input)
    assert found.input == proposal.input
    assert datum.proposer_did == CRITIC
    assert node.datum.agent_did == CRITIC
    assert node.utxo.input == tx_in(0x81)


def test_latest_takes_the_active_proposal_with_the_latest_submitted_at(listed):
    """A newer proposal of PROPOSER is the latest active one; a withdrawn
    proposal with an even later submitted_at does not count."""
    world, older = listed
    base = world.proposal_datum().submitted_at
    newer = world.proposal_utxo(world.proposal_datum(PROPOSER, submitted_at=base + 1), byte=0x72)
    world.proposal_utxo(
        world.proposal_datum(PROPOSER, submitted_at=base + 2, state=gi.ProposalState.WITHDRAWN), byte=0x73,
    )
    found, datum, node = test_expire.expiry_inputs(proposal_utxos(world), world.deployment, None)
    assert found.input == newer.input != older.input
    assert node.datum.agent_did == PROPOSER


def test_refuses_a_proposer_without_a_node(listed):
    world, _ = listed
    proposal = world.proposal_utxo(world.proposal_datum(ENDORSER), byte=0x74)
    with pytest.raises(gi.GovernanceClientError, match="no node in the activity list"):
        test_expire.expiry_inputs(proposal_utxos(world), world.deployment, proposal.input)


def test_refuses_a_node_without_active_proposals(world):
    world.head(next_did=CRITIC)
    world.activity_utxo(CRITIC, 0, 0, byte=0x81)
    proposal = world.proposal_utxo(world.proposal_datum(CRITIC))
    with pytest.raises(gi.GovernanceClientError, match="active_proposal_count 0"):
        test_expire.expiry_inputs(proposal_utxos(world), world.deployment, proposal.input)


def test_refuses_a_finalised_proposal(listed):
    world, _ = listed
    proposal = world.proposal_utxo(world.proposal_datum(CRITIC, state=gi.ProposalState.WITHDRAWN), byte=0x75)
    with pytest.raises(gi.GovernanceClientError, match="WITHDRAWN"):
        test_expire.expiry_inputs(proposal_utxos(world), world.deployment, proposal.input)


def test_refuses_an_output_without_a_proposal_token(listed):
    world, _ = listed
    with pytest.raises(gi.MissingReferenceError, match="not a submitted proposal"):
        test_expire.expiry_inputs(proposal_utxos(world), world.deployment, tx_in(0x81))


# ── scripts/redeploy_proposal.py ─────────────────────────────────────────────


@pytest.fixture
def redeploy(tmp_path, monkeypatch):
    """Run redeploy() with a deploy state file and a fake VectorAgent whose
    chain is `chain`. Any step that changes the chain fails the test."""

    def run(state: dict, chain=None) -> list:
        state_file = tmp_path / "deploy_state.json"
        state_file.write_text(json.dumps(state))
        wallet_file = tmp_path / "governance_wallet.json"
        wallet_file.write_text(json.dumps({"address": "addr1test", "vkey_hash": "0c" * 28}))
        monkeypatch.setattr(redeploy_proposal, "DEPLOY_STATE_FILE", state_file)
        monkeypatch.setattr(redeploy_proposal, "WALLET_FILE", wallet_file)
        steps = []

        def applied(config_cbor):
            steps.append("apply")
            return {"validators": [
                {"title": title, "hash": "ff" * 28, "compiledCode": "00"} for title in state["validators"]
            ]}

        async def forbidden(*args, **kwargs):
            raise AssertionError("the redeploy submitted a transaction")

        class FakeAgent:
            def __init__(self, **kwargs):
                self.context = chain

            async def __aenter__(self):
                steps.append("connect")
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_balance(self):
                return types.SimpleNamespace(ada=100)

        monkeypatch.setattr(redeploy_proposal, "apply_governance_config", applied)
        monkeypatch.setattr(redeploy_proposal, "deploy_reference_script", forbidden)
        monkeypatch.setattr(redeploy_proposal, "mint_refs_nft", forbidden)
        monkeypatch.setitem(sys.modules, "vector_agent", types.SimpleNamespace(VectorAgent=FakeAgent))
        asyncio.run(redeploy_proposal.redeploy())
        return steps

    return run


def test_redeploy_refuses_a_state_with_the_activity_head(redeploy, capsys):
    state = deploy_state()
    state["tx_hashes"]["activity_head"] = "aa" * 32
    assert redeploy(state) == []
    assert "head of the activity list" in capsys.readouterr().out


def test_redeploy_refuses_an_activity_head_on_chain(redeploy, capsys):
    """The deploy state does not name the head, but the chain has it at the
    proposal_spend address: the script stops before the first transaction."""
    world = make_world()  # its chain has the head (deploy.py step 5a)
    assert redeploy(deploy_state(), world.chain) == ["apply", "connect"]
    assert "head of the activity list" in capsys.readouterr().out


def test_redeploy_without_a_head_reaches_the_first_transaction(redeploy):
    world = make_world()
    address = str(world.deployment.proposal_spend.address)
    world.chain.store[address] = []  # no head
    with pytest.raises(AssertionError, match="submitted a transaction"):
        redeploy(deploy_state(), world.chain)


# ── The deploy state of the scripts (--state, --network) ─────────────────────


PARSERS = {
    "smoke_test.py": smoke_test.parse_args,
    "test_expire.py": lambda argv: test_expire.parse_args(["--latest", *argv]),
    "test_expire_e2e.py": test_expire_e2e.parse_args,
    "update_params.py": lambda argv: update_params.parse_args(argv)[0],
    "treasury_replenish.py": treasury_replenish.parse_args,
    "test_validated_submit.py": test_validated_submit.parse_args,
    "agents/analytics_template.py": analytics_template.parse_args,
}


@pytest.mark.parametrize("script", sorted(PARSERS))
def test_the_scripts_read_the_state_of_deploy_py_by_default(script):
    parse = PARSERS[script]
    args = parse([])
    assert args.network == "mainnet"
    assert dc.state_path(args) == dc.MODULE_DIR / "deploy" / "identity" / "state" / "mainnet" / "deploy_state.json"
    assert dc.state_path(parse(["--network", "testnet"])) == dc.STATE_ROOT / "testnet" / "deploy_state.json"
    # The state of the previous deployment only on --state.
    assert dc.state_path(parse(["--state", "wallets/deploy_state.json"])) == Path("wallets/deploy_state.json")


def test_the_other_options_of_the_scripts_stay():
    args, overrides = update_params.parse_args(["--network", "testnet", "--proposal_cooldown=1000",
                                                "--max_amendments=7", "ignored"])
    assert args.network == "testnet" and overrides == {"proposal_cooldown": 1000, "max_amendments": 7}
    args = test_expire.parse_args(["ab" * 32, "2", "--state", "s.json"])
    assert (args.tx_hash, args.output_index, args.latest, args.state) == ("ab" * 32, 2, False, Path("s.json"))
    args = smoke_test.parse_args(["--agent", "a1" * 32, "--finalise", "adopt", "--network", "testnet"])
    assert (args.agent, args.finalise, args.network) == ("a1" * 32, "adopt", "testnet")
    args = treasury_replenish.parse_args(["--force", "--target", "10"])
    assert (args.force, args.target, args.state) == (True, 10, None)
    args = test_validated_submit.parse_args(["--agent", "a2" * 32, "--state", "wallets/deploy_state.json"])
    assert (args.agent, args.state, args.network) == ("a2" * 32, Path("wallets/deploy_state.json"), "mainnet")
    args = analytics_template.parse_args(["--monitor-only", "--network", "testnet"])
    assert (args.monitor_only, args.network, args.state) == (True, "testnet", None)


RUNNERS = {
    "smoke_test.py": lambda path: smoke_test.smoke_test(smoke_test.parse_args(["--state", str(path)])),
    "test_expire_e2e.py": lambda path: test_expire_e2e.run_test(None, path, "mainnet"),
    "test_validated_submit.py": lambda path: test_validated_submit.main(None, path, "mainnet"),
    "update_params.py": lambda path: update_params.main(["--state", str(path)]),
    "treasury_replenish.py": lambda path: treasury_replenish.check_and_replenish(True, False, 5, path, "mainnet"),
    "agents/analytics_template.py": lambda path: analytics_template.main(["--state", str(path)]),
}


@pytest.mark.parametrize("script", sorted(RUNNERS))
def test_the_scripts_read_the_given_state_first(script, tmp_path):
    """The scripts read the deploy state before they connect: a missing state
    stops them and names scripts/deploy.py."""
    with pytest.raises(FileNotFoundError, match="scripts/deploy.py"):
        asyncio.run(RUNNERS[script](tmp_path / "missing" / "deploy_state.json"))


def test_a_state_of_deploy_py_on_another_network_is_refused(tmp_path):
    path = tmp_path / "deploy_state.json"
    path.write_text(json.dumps(dict(deploy_state(), format=dc.STATE_FORMAT, module="module6", network="testnet")))
    with pytest.raises(dc.DeployInputError, match="give --network testnet"):
        dc.read_state(path, "mainnet")
    assert dc.read_state(path, "testnet")["network"] == "testnet"
    with pytest.raises(FileNotFoundError, match="scripts/deploy.py"):
        dc.read_state(tmp_path / "missing.json", "mainnet")


def test_the_state_of_deploy_py_takes_the_endpoints_of_the_network(monkeypatch):
    """The state of scripts/deploy.py has no endpoints: the environment and
    the network presets give them. The state of the previous deployment keeps
    its own endpoints."""
    for name in ("VECTOR_OGMIOS_URL", "VECTOR_SUBMIT_URL", "VECTOR_EXPLORER_URL"):
        monkeypatch.delenv(name, raising=False)
    new = {"format": dc.STATE_FORMAT, "module": "module6", "network": "testnet"}
    assert dc.state_endpoints(new, "testnet") == {
        "ogmios_url": vd.NETWORKS["testnet"].ogmios_url,
        "submit_url": vd.NETWORKS["testnet"].submit_url,
        "explorer_url": "https://vector.testnet.apexscan.org",
    }
    assert dc.state_endpoints(new, "mainnet")["explorer_url"] == "https://vector.apexscan.org"
    old = {"ogmios_url": "https://ogmios.old", "submit_url": "https://submit.old", "explorer_url": "https://explorer.old"}
    assert dc.state_endpoints(old, "mainnet") == old
    monkeypatch.setenv("VECTOR_OGMIOS_URL", "https://ogmios.example")
    assert dc.state_endpoints(new, "testnet")["ogmios_url"] == "https://ogmios.example"


# ── The scripts of the previous deployment ───────────────────────────────────


def test_the_scripts_of_the_previous_deployment_use_old_deploy_helpers():
    for module in (redeploy_proposal, move_crossrefs, recreate_infra, redeploy_proposal_spend_ref,
                   redeploy_ref_scripts):
        assert "For the previous deployment only (wallets/deploy_state.json)" in module.__doc__
        assert module.DEPLOY_STATE_FILE.parts[-2:] == ("wallets", "deploy_state.json")
    for name in ("apply_governance_config", "script_hash_to_address", "deploy_reference_script",
                 "compute_native_script_policy", "mint_refs_nft"):
        assert getattr(redeploy_proposal, name) is getattr(old_deploy_helpers, name)
    assert move_crossrefs.compute_native_script_policy is old_deploy_helpers.compute_native_script_policy
    # One copy of each helper: deploy.py uses the shared ones of deploy_common
    # and has none of the wallet helpers.
    assert old_deploy_helpers.apply_governance_config is dc.apply_governance_config is deploy.apply_governance_config
    assert old_deploy_helpers.script_hash_to_address is dc.script_hash_to_address is deploy.script_hash_to_address
    for name in ("deploy_reference_script", "mint_refs_nft", "compute_native_script_policy", "sign_tx"):
        assert not hasattr(deploy, name)
    operator = bytes.fromhex("0c" * 28)
    policy, native = old_deploy_helpers.compute_native_script_policy(operator.hex())
    assert policy == vd.native_policy_id(vd.operator_native_script(operator)).hex()
    assert dc.script_hash_to_address("4a" * 28) == str(vd.script_address(bytes.fromhex("4a" * 28)))
