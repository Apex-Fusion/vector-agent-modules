"""Offline tests of two deployment scripts.

- scripts/test_expire.py takes the node of the proposer from the activity
  list (the expiry spends that node), not the first activity node at the
  proposal validator.
- scripts/redeploy_proposal.py refuses a deployment with the head of the
  activity list: by the deploy state before step 1, and by the outputs at the
  proposal_spend address before the first transaction.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import governance_identity as gi
import redeploy_proposal
import test_expire
from conftest import CRITIC, ENDORSER, PROPOSER, deploy_state, make_world, tx_in

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
