"""Offline tests of the identity-checked Module-6 builders
(scripts/governance_identity.py).

Each builder test builds and signs a transaction against the fake chain and
checks the reference inputs (record, ancestors, params record, CrossRefs,
proposal), the required signers, the validity range, the datum credential,
the times, the redeemers and the mint. The refusal tests check that the
client stops before it builds.
"""

from __future__ import annotations

import asyncio
import dataclasses

import cbor2
import pytest
from pycardano import TransactionInput, VerificationKeyHash

import governance_identity as gi
import vector_identity as vid
from conftest import (
    CRITIC,
    CROSS_REFS_TX,
    ENDORSER,
    IDENTITY_HASH,
    PARENT,
    PROPOSER,
    deploy_state,
    key_pair,
    make_world,
    public_key,
    record,
    record_datum,
    revoked,
    script_hash,
    subagent_of,
    suspended,
    tx_in,
    with_transaction_key,
)

h = bytes.fromhex


# ── Helpers ─────────────────────────────────────────────────────────────────


def tree(value):
    """The Plutus Data tree of a datum or redeemer value."""
    return gi._tree(value)


def spend_redeemers(tx) -> dict:
    """{spent input: redeemer tree}."""
    inputs = list(tx.transaction_body.inputs)
    return {
        inputs[key.index]: tree(value.data)
        for key, value in tx.transaction_witness_set.redeemer.items()
        if key.tag.name == "SPEND"
    }


def mint_redeemers(tx) -> dict:
    """{policy: redeemer tree}."""
    policies = sorted(tx.transaction_body.mint.keys(), key=lambda p: p.to_cbor())
    return {
        bytes(policies[key.index].payload): tree(value.data)
        for key, value in tx.transaction_witness_set.redeemer.items()
        if key.tag.name == "MINT"
    }


def minted(tx) -> dict:
    return {
        (bytes(pid.payload), bytes(name.payload)): quantity
        for pid, assets in (tx.transaction_body.mint or {}).items()
        for name, quantity in assets.items()
    }


def reference_inputs(tx) -> set:
    return set(tx.transaction_body.reference_inputs or [])


def required_signers(tx) -> list[bytes]:
    return [bytes(k) for k in tx.transaction_body.required_signers or []]


def signed_by(tx) -> set[bytes]:
    return {bytes(w.vkey.hash()) for w in tx.transaction_witness_set.vkey_witnesses}


def check_range(tx, window: gi.TxWindow) -> None:
    body = tx.transaction_body
    assert (body.validity_start, body.ttl) == (window.start_slot, window.ttl_slot)
    assert window.hi - window.lo <= vid.MAX_RANGE_MS


def sign(world, prepared, *extra):
    return gi.sign_prepared(prepared, keys=[world.wallet, *extra], change_address=world.wallet_address)


def proposal_terms(**changes) -> gi.ProposalTerms:
    terms = gi.ProposalTerms(
        proposal_hash=vid.blake2b_256(b"new proposal"),
        storage_uri="ipfs://QmNewProposal",
        stake_amount=25_000_000,
        review_window=604_800_000,
    )
    return dataclasses.replace(terms, **changes)


def lock_proposal(world, records, window, activity=None, terms=None, key_hash=None):
    prepared = gi.prepare_proposal_lock(
        world.chain, world.deployment, records=records, refs=world.refs, activity=activity,
        terms=terms or proposal_terms(), credential_key_hash=key_hash or world.key_hash,
        window=window, wallet_address=world.wallet_address,
    )
    return prepared


def submit_proposal(world, records, window, activity=None, terms=None, key_hash=None):
    lock = lock_proposal(world, records, window, activity, terms, key_hash)
    locked = world.locked(world.deployment.proposal_spend, lock.datum, lock.datum.stake_amount + gi.POSITION_DEPOSIT)
    prepared = gi.prepare_submit_proposal(
        world.chain, world.deployment, locked=locked, records=records, refs=world.refs,
        activity=activity, window=window, wallet_address=world.wallet_address,
    )
    return locked, prepared


# ── Deployment state and inputs ─────────────────────────────────────────────


def test_deployment_state_loads_the_identity_deployment():
    deployment = gi.GovernanceDeployment.from_state(deploy_state())
    assert deployment.identity_hash == IDENTITY_HASH
    assert deployment.identity_address == gi.script_address(IDENTITY_HASH)
    assert deployment.identity_address.staking_part is None
    assert deployment.proposal_spend.hash == script_hash(gi.PROPOSAL_SPEND)
    assert deployment.cross_refs_tx == CROSS_REFS_TX
    assert deployment.proposal_spend.reference is None


def test_state_without_identity_hash_is_refused_with_the_deployment_order():
    state = deploy_state()
    del state["identity_hash"]
    state["agent_registry_hash"] = vid.LEGACY_REGISTRY_HASH.hex()
    with pytest.raises(gi.DeploymentStateError) as error:
        gi.GovernanceDeployment.from_state(state, "wallets/deploy_state.json")
    message = str(error.value)
    assert "has no identity_hash" in message and "legacy agent registry" in message
    assert "identity" in message and "Module-1" in message and "Module-3" in message and "Module-6" in message


@pytest.mark.parametrize("value", ["00" * 28, vid.LEGACY_REGISTRY_HASH.hex(), "abcd", "zz" * 28])
def test_state_with_a_bad_identity_hash_is_refused(value):
    with pytest.raises(gi.DeploymentStateError):
        gi.GovernanceDeployment.from_state(deploy_state(identity_hash=value))


def test_state_with_wrong_compiled_code_is_refused():
    state = deploy_state()
    state["validators"][gi.PROPOSAL_SPEND]["hash"] = "00" * 28
    with pytest.raises(gi.DeploymentStateError, match="does not have the hash"):
        gi.GovernanceDeployment.from_state(state)


def test_reference_scripts_come_from_the_deploy_state():
    state = deploy_state()
    state["tx_hashes"].update({key: f"{i:02x}" * 32 for i, key in enumerate(gi.REFERENCE_SCRIPT_KEYS.values(), 1)})
    deployment = gi.GovernanceDeployment.from_state(state)
    reference = deployment.proposal_spend.witness
    assert reference.input == TransactionInput.from_primitive([h("01" * 32), 0])
    assert reference.output.script == deployment.proposal_spend.script


def test_agent_identifier_from_argument_or_environment():
    did = vid.did_from_id(PROPOSER)
    assert gi.agent_id_from(None, {gi.AGENT_DID_ENV: did}) == PROPOSER
    assert gi.agent_id_from(PROPOSER.hex().upper(), {}) == PROPOSER
    assert gi.agent_id_from(vid.did_from_id(CRITIC), {gi.AGENT_DID_ENV: did}) == CRITIC
    with pytest.raises(gi.GovernanceClientError, match=gi.AGENT_DID_ENV):
        gi.agent_id_from(None, {})
    with pytest.raises(gi.GovernanceClientError):
        gi.agent_id_from("did:vector:agent:1234", {})


def test_window_is_at_most_one_hour(world):
    window = world.window()
    assert window.hi - window.lo == gi.DEFAULT_RANGE_MS
    assert window.lo == world.clock.posix_ms(world.chain.tip_slot - 60)
    assert gi.window_from_tip(world.clock, 1000, vid.MAX_RANGE_MS).hi - window.clock.posix_ms(940) == vid.MAX_RANGE_MS
    with pytest.raises(vid.IdentityError, match="hi - lo <= 3600000"):
        world.window(vid.MAX_RANGE_MS + 1000)
    with pytest.raises(vid.IdentityError):
        gi.TxWindow(world.clock, 100, 100)


# ── Lookups ─────────────────────────────────────────────────────────────────


def test_params_record_needs_the_record_token(world):
    found = gi.find_params(world.chain.utxos(world.deployment.params_address), world.deployment)
    assert found.utxo == world.params and found.params.max_active_proposals == 3
    without_token = dataclasses.replace(world.params.output, amount=world.params.output.amount.coin)
    with pytest.raises(gi.MissingReferenceError, match="no params record"):
        gi.find_params([dataclasses.replace(world.params, output=without_token)], world.deployment)


def test_cross_refs_must_name_the_deployment(world):
    utxos = world.chain.utxos(world.deployment.cross_refs_address)
    assert gi.find_cross_refs(utxos, world.deployment).utxo == world.cross_refs
    other = dataclasses.replace(world.deployment.cross_refs, critique_mint_hash=h("00" * 28))
    stale = dataclasses.replace(world.cross_refs, output=dataclasses.replace(
        world.cross_refs.output, datum=gi.inline_datum(other.to_data())))
    with pytest.raises(gi.MissingReferenceError, match="no CrossRefs output"):
        gi.find_cross_refs([stale, world.oracle], world.deployment)


def test_cross_refs_have_five_fields_with_the_endorsement_hash_last(world):
    """GovernanceCrossRefs: proposal_validator_hash, critique_validator_hash,
    proposal_mint_hash, critique_mint_hash, endorsement_validator_hash."""
    refs = world.deployment.cross_refs
    tree = refs.to_data()
    assert tree.tag == 121 and len(tree.value) == 5
    assert tree.value[4] == world.deployment.endorsement_spend.hash
    assert gi.CrossRefs.from_data(gi.inline_datum(tree)) == refs
    # The 4-field datum of an older deployment does not decode and never
    # counts as the CrossRefs output of this deployment.
    old = gi.inline_datum(gi.vid.constr(0, tree.value[:4]))
    with pytest.raises(gi.GovernanceDatumError):
        gi.CrossRefs.from_data(old)
    stale = dataclasses.replace(world.cross_refs, output=dataclasses.replace(
        world.cross_refs.output, datum=old))
    with pytest.raises(gi.MissingReferenceError, match="endorsement_spend"):
        gi.find_cross_refs([stale], world.deployment)


def test_oracle_record_needs_the_record_token(world):
    assert gi.find_oracle(world.chain.utxos(world.deployment.oracle_address), world.deployment) == world.oracle
    with pytest.raises(gi.MissingReferenceError, match="no oracle record"):
        gi.find_oracle([world.cross_refs], world.deployment)


def test_identity_records_include_the_ancestors(world):
    parent = world.agent(PARENT, byte=0x61)
    child = world.agent(PROPOSER, subagent_of(record_datum(public_key(world.wallet)), PARENT))
    chain = gi.identity_records(world.chain.utxos(world.deployment.identity_address), world.deployment, PROPOSER)
    assert [r.agent_id for r in chain] == [PROPOSER, PARENT]
    assert [r.utxo for r in chain] == [child.utxo, parent.utxo]
    with pytest.raises(vid.MissingAncestorError):
        gi.identity_records([child.utxo], world.deployment, PROPOSER)
    with pytest.raises(vid.RecordNotFoundError):
        gi.identity_records([], world.deployment, PROPOSER)


def test_activity_record_of_the_agent(world):
    address = world.deployment.proposal_spend.address
    assert gi.find_activity(world.chain.utxos(address), world.deployment, PROPOSER) is None
    activity = world.activity_utxo(PROPOSER, 1, 0)
    world.activity_utxo(CRITIC, 1, 0, byte=0x81)
    assert gi.find_activity(world.chain.utxos(address), world.deployment, PROPOSER) == activity
    world.activity_utxo(PROPOSER, 2, 0, byte=0x82)
    with pytest.raises(gi.GovernanceClientError, match="several activity records"):
        gi.find_activity(world.chain.utxos(address), world.deployment, PROPOSER)


def test_token_names_follow_the_contract():
    ref = tx_in(0x11, 3)
    serialised = vid.serialise_data(vid.output_reference_data(bytes(ref.transaction_id), 3))
    assert gi.proposal_token_name(ref) == b"prop_" + vid.blake2b_256(serialised)[:27]
    assert gi.critique_token_name(ref) == b"crit_" + vid.blake2b_256(serialised)[:27]
    assert gi.activity_token_name(PROPOSER) == b"pact_" + vid.blake2b_256(PROPOSER)[:27]
    assert len(gi.proposal_token_name(ref)) == 32


def test_datums_round_trip(world):
    proposal = world.proposal_datum(
        proposal_type=gi.parameter_change("MIN_PROPOSAL_STAKE", 25, 30),
        storage_uri=b"ipfs://" + b"b" * 80,
        incorporated_critiques=(tx_in(5, 1),),
        state=gi.ProposalState.AMENDED,
        state_hash=vid.blake2b_256(b"old"),
    )
    encoded = vid.serialise_data(proposal.to_data())
    assert gi.ProposalDatum.from_data(encoded) == proposal
    critique = gi.CritiqueDatum(CRITIC, gi.Credential.key(world.key_hash), tx_in(5), vid.blake2b_256(b"c"),
                                b"ipfs://c", gi.amendment_critique(vid.blake2b_256(b"s")), 5_000_000, 7, True)
    assert gi.CritiqueDatum.from_data(vid.serialise_data(critique.to_data())) == critique
    endorsement = gi.EndorsementDatum(ENDORSER, gi.Credential.key(world.key_hash), tx_in(5), 5_000_000, 9)
    assert gi.EndorsementDatum.from_data(vid.serialise_data(endorsement.to_data())) == endorsement
    with pytest.raises(gi.GovernanceDatumError):
        gi.ProposalDatum.from_data(vid.serialise_data(critique.to_data()))


# ── SubmitProposal ──────────────────────────────────────────────────────────


def test_proposal_lock_writes_the_planned_lower_bound(world):
    records = [world.agent(PROPOSER)]
    window = world.window()
    prepared = lock_proposal(world, records, window)
    tx = sign(world, prepared)
    out = tx.transaction_body.outputs[0]
    assert out.address == world.deployment.proposal_spend.address
    assert out.amount.coin == 25_000_000 + gi.POSITION_DEPOSIT
    datum = gi.ProposalDatum.from_data(out.datum)
    assert datum.proposer_did == PROPOSER
    assert datum.proposer_credential == gi.Credential.key(world.key_hash)
    assert datum.submitted_at == window.lo
    assert (datum.state, datum.amendment_count, datum.incorporated_critiques) == (gi.ProposalState.OPEN, 0, ())
    assert tx.transaction_body.ttl == window.ttl_slot
    assert not tx.transaction_body.reference_inputs and not tx.transaction_body.required_signers


def test_submit_proposal_first_proposal(world):
    rec = world.agent(PROPOSER)
    window = world.window()
    locked, prepared = submit_proposal(world, [rec], window)
    tx = sign(world, prepared)
    body = tx.transaction_body
    deployment = world.deployment

    assert reference_inputs(tx) == {rec.utxo.input, world.params.input, world.cross_refs.input}
    assert required_signers(tx) == [world.key_hash]
    assert world.key_hash in signed_by(tx)
    check_range(tx, window)

    token = gi.proposal_token_name(locked.input)
    activity_token = gi.activity_token_name(PROPOSER)
    assert minted(tx) == {(deployment.proposal_mint.hash, token): 1, (deployment.proposal_mint.hash, activity_token): 1}
    assert spend_redeemers(tx) == {locked.input: gi.SUBMIT_PROPOSAL}
    assert mint_redeemers(tx) == {deployment.proposal_mint.hash: gi.SUBMIT_PROPOSAL}

    proposal_out, activity_out = body.outputs[0], body.outputs[1]
    assert proposal_out.address == deployment.proposal_spend.address
    assert gi.policy_tokens(proposal_out, deployment.proposal_mint.hash) == {token: 1}
    assert proposal_out.amount.coin == locked.output.amount.coin
    datum = gi.ProposalDatum.from_data(proposal_out.datum)
    assert datum == gi.ProposalDatum.from_data(locked.output.datum)
    assert datum.submitted_at == window.lo
    assert datum.proposer_credential == gi.Credential.key(world.key_hash)

    activity = gi.ActivityDatum.from_data(activity_out.datum)
    assert activity == gi.ActivityDatum(PROPOSER, gi.Credential.key(world.key_hash), 1, window.lo)
    assert gi.policy_tokens(activity_out, deployment.proposal_mint.hash) == {activity_token: 1}
    assert world.chain.evaluated, "the builder asks the chain context for execution units"


def test_submit_proposal_spends_the_activity_record(world):
    rec = world.agent(PROPOSER)
    window = world.window()
    activity = world.activity_utxo(PROPOSER, 1, window.lo - 90_000_000)
    locked, prepared = submit_proposal(world, [rec], window, activity=activity)
    tx = sign(world, prepared)

    assert spend_redeemers(tx) == {locked.input: gi.SUBMIT_PROPOSAL, activity.utxo.input: gi.SPEND_ACTIVITY}
    assert tree(gi._redeemer(gi.SPEND_ACTIVITY).data).tag == 1281
    assert minted(tx) == {(world.deployment.proposal_mint.hash, gi.proposal_token_name(locked.input)): 1}
    out = tx.transaction_body.outputs[1]
    assert gi.ActivityDatum.from_data(out.datum) == dataclasses.replace(
        activity.datum, active_proposal_count=2, last_proposal_slot=window.lo)
    assert out.amount == activity.utxo.output.amount and out.address == activity.utxo.output.address


def test_submit_proposal_of_a_subagent_adds_the_parent_record(world):
    parent = world.agent(PARENT, byte=0x61)
    child = world.agent(PROPOSER, subagent_of(record_datum(public_key(world.wallet)), PARENT))
    window = world.window()
    _, prepared = submit_proposal(world, [child, parent], window)
    tx = sign(world, prepared)
    assert {child.utxo.input, parent.utxo.input} <= reference_inputs(tx)


def test_submit_proposal_with_a_listed_transaction_key(world):
    controller = key_pair()
    datum = with_transaction_key(record_datum(public_key(controller)), public_key(world.wallet))
    rec = world.agent(PROPOSER, datum)
    _, prepared = submit_proposal(world, [rec], world.window())
    tx = sign(world, prepared)
    assert required_signers(tx) == [world.key_hash]
    assert controller.key_hash not in signed_by(tx)


def test_submit_proposal_with_a_credential_key_besides_the_wallet(world):
    agent_key = key_pair()
    rec = world.agent(PROPOSER, record_datum(public_key(agent_key)))
    _, prepared = submit_proposal(world, [rec], world.window(), key_hash=agent_key.key_hash)
    with pytest.raises(gi.GovernanceClientError, match="needs the signatures"):
        sign(world, prepared)
    tx = sign(world, prepared, agent_key)
    assert required_signers(tx) == [agent_key.key_hash]
    assert {agent_key.key_hash, world.key_hash} <= signed_by(tx)
    assert gi.ProposalDatum.from_data(tx.transaction_body.outputs[0].datum).proposer_credential.hash == agent_key.key_hash


@pytest.mark.parametrize("change", [suspended, revoked])
def test_submit_proposal_refuses_an_agent_that_is_not_active(world, change):
    rec = world.agent(PROPOSER, change(record_datum(public_key(world.wallet))))
    with pytest.raises(vid.IdentityStatusError):
        lock_proposal(world, [rec], world.window())
    locked = world.locked(world.deployment.proposal_spend, world.proposal_datum(), 27_000_000)
    with pytest.raises(vid.IdentityStatusError):
        gi.prepare_submit_proposal(world.chain, world.deployment, locked=locked, records=[rec], refs=world.refs,
                                   activity=None, window=world.window(), wallet_address=world.wallet_address)


def test_submit_proposal_refuses_an_undeclared_key(world):
    rec = world.agent(PROPOSER, record_datum(public_key(key_pair())))
    with pytest.raises(vid.IdentityError, match="neither the controller"):
        lock_proposal(world, [rec], world.window())
    locked = world.locked(world.deployment.proposal_spend, world.proposal_datum(), 27_000_000)
    with pytest.raises(vid.IdentityError, match="neither the controller"):
        gi.prepare_submit_proposal(world.chain, world.deployment, locked=locked, records=[rec], refs=world.refs,
                                   activity=None, window=world.window(), wallet_address=world.wallet_address)


def test_submit_proposal_refuses_a_missing_ancestor(world):
    child = world.agent(PROPOSER, subagent_of(record_datum(public_key(world.wallet)), PARENT))
    with pytest.raises(vid.MissingAncestorError):
        lock_proposal(world, [child], world.window())


def test_submit_proposal_refuses_a_record_from_another_registry(world):
    rec = world.agent(PROPOSER)
    other = record(PROPOSER, rec.datum, 0x62)
    fake_output = dataclasses.replace(other.utxo.output, address=gi.script_address(h("2e" * 28)))
    fake = vid.IdentityRecord(PROPOSER, rec.datum, dataclasses.replace(other.utxo, output=fake_output))
    with pytest.raises(vid.IdentityError, match="not an authentic record"):
        lock_proposal(world, [fake], world.window())


def test_submit_proposal_refuses_the_rate_limit(world):
    rec = world.agent(PROPOSER)
    window = world.window()
    recent = world.activity_utxo(PROPOSER, 1, window.lo - 1000)
    with pytest.raises(gi.GovernanceClientError, match="cooldown"):
        lock_proposal(world, [rec], window, activity=recent)
    full = gi.ActivityRecord(recent.utxo, dataclasses.replace(recent.datum, active_proposal_count=3, last_proposal_slot=0))
    with pytest.raises(gi.GovernanceClientError, match="active proposals"):
        lock_proposal(world, [rec], window, activity=full)


@pytest.mark.parametrize("terms, message", [
    (proposal_terms(stake_amount=1_000_000), "stake is below"),
    (proposal_terms(review_window=1_000), "review window"),
    (proposal_terms(proposal_hash=b"short"), "32 bytes"),
    (proposal_terms(storage_uri=""), "storage_uri is empty"),
    (proposal_terms(proposal_type=gi.parameter_change("NOT_A_PARAM", 1, 2)), "governable"),
    (proposal_terms(priority=gi.Priority.EMERGENCY), "emergency"),
])
def test_submit_proposal_refuses_terms_that_the_contract_rejects(world, terms, message):
    rec = world.agent(PROPOSER)
    with pytest.raises(gi.GovernanceClientError, match=message):
        lock_proposal(world, [rec], world.window(), terms=terms)


def test_emergency_proposal_with_valid_terms(world):
    rec = world.agent(PROPOSER)
    terms = proposal_terms(
        priority=gi.Priority.EMERGENCY, stake_amount=125_000_000, review_window=43_200_000,
        proposal_type=gi.protocol_upgrade(vid.blake2b_256(b"upgrade")),
    )
    prepared = lock_proposal(world, [rec], world.window(), terms=terms)
    assert prepared.datum.priority == gi.Priority.EMERGENCY


def test_submit_proposal_refuses_a_lock_with_tokens(world):
    rec = world.agent(PROPOSER)
    proposal = world.proposal_utxo(world.proposal_datum())
    with pytest.raises(gi.GovernanceClientError, match="not a proposal lock"):
        gi.prepare_submit_proposal(world.chain, world.deployment, locked=proposal, records=[rec], refs=world.refs,
                                   activity=None, window=world.window(), wallet_address=world.wallet_address)


def test_submit_proposal_refuses_references_of_another_deployment(world):
    rec = world.agent(PROPOSER)
    refs = world.refs
    stale = gi.GovernanceReferences(refs.params, gi.CrossRefsRecord(
        refs.cross_refs.utxo, dataclasses.replace(refs.cross_refs.refs, proposal_mint_hash=h("00" * 28))))
    with pytest.raises(gi.MissingReferenceError, match="CrossRefs"):
        gi.prepare_proposal_lock(world.chain, world.deployment, records=[rec], refs=stale, activity=None,
                                 terms=proposal_terms(), credential_key_hash=world.key_hash,
                                 window=world.window(), wallet_address=world.wallet_address)


def test_reference_scripts_replace_the_inline_scripts():
    state = deploy_state()
    state["tx_hashes"].update({key: f"{i:02x}" * 32 for i, key in enumerate(gi.REFERENCE_SCRIPT_KEYS.values(), 1)})
    world = make_world(state)
    rec = world.agent(PROPOSER)
    _, prepared = submit_proposal(world, [rec], world.window())
    tx = sign(world, prepared)
    expected = {world.deployment.proposal_spend.reference.input, world.deployment.proposal_mint.reference.input}
    assert expected <= reference_inputs(tx)
    assert not tx.transaction_witness_set.plutus_v3_script


# ── AmendProposal ───────────────────────────────────────────────────────────


def critique_of(world, proposal, critic=CRITIC, **changes):
    datum = gi.CritiqueDatum(
        critic_did=critic,
        critic_credential=gi.Credential.key(world.key_hash),
        proposal_ref=proposal.input,
        critique_hash=vid.blake2b_256(b"critique " + critic),
        storage_uri=b"ipfs://QmCritique",
        critique_type=gi.supportive(),
        stake_amount=5_000_000,
        submitted_at=gi.SlotClock(0, 1000).posix_ms(1),
    )
    return dataclasses.replace(datum, **changes)


def amend(world, proposal, records, window, incorporate=(), new_hash=None):
    return gi.prepare_amend_proposal(
        world.chain, world.deployment, proposal=proposal, records=records, refs=world.refs,
        new_hash=new_hash or vid.blake2b_256(b"amended"), new_uri="ipfs://QmAmended",
        window=window, wallet_address=world.wallet_address, incorporate=incorporate,
    )


def test_amend_proposal_with_incorporated_critiques(world):
    rec = world.agent(PROPOSER)
    datum = world.proposal_datum()
    proposal = world.proposal_utxo(datum)
    first = world.critique_utxo(critique_of(world, proposal))
    second = world.critique_utxo(critique_of(world, proposal, critic=ENDORSER), byte=0xA1)
    window = world.window()
    tx = sign(world, amend(world, proposal, [rec], window, [first, second]))
    body = tx.transaction_body

    assert reference_inputs(tx) == {rec.utxo.input, world.params.input, world.cross_refs.input}
    assert required_signers(tx) == [world.key_hash]
    check_range(tx, window)
    assert not body.mint
    new_hash = vid.blake2b_256(b"amended")
    assert spend_redeemers(tx) == {
        proposal.input: gi.amend_proposal_redeemer(new_hash, b"ipfs://QmAmended", [first.input, second.input]),
        first.input: gi.INCORPORATE_CRITIQUE,
        second.input: gi.INCORPORATE_CRITIQUE,
    }
    out = body.outputs[0]
    assert (out.address, out.amount) == (proposal.output.address, proposal.output.amount)
    assert gi.ProposalDatum.from_data(out.datum) == dataclasses.replace(
        datum, proposal_hash=new_hash, storage_uri=b"ipfs://QmAmended", amendment_count=1,
        incorporated_critiques=(first.input, second.input), state=gi.ProposalState.AMENDED,
        state_hash=datum.proposal_hash,
    )
    for utxo, continuing in ((first, body.outputs[1]), (second, body.outputs[2])):
        assert (continuing.address, continuing.amount) == (utxo.output.address, utxo.output.amount)
        assert gi.CritiqueDatum.from_data(continuing.datum) == dataclasses.replace(
            gi.CritiqueDatum.from_data(utxo.output.datum), incorporated=True)


def test_amend_proposal_refusals(world):
    rec = world.agent(PROPOSER)
    datum = world.proposal_datum()
    proposal = world.proposal_utxo(datum)
    window = world.window()
    with pytest.raises(gi.GovernanceClientError, match="differ from the current hash"):
        amend(world, proposal, [rec], window, new_hash=datum.proposal_hash)
    other = world.proposal_utxo(world.proposal_datum(), byte=0x72)
    with pytest.raises(gi.GovernanceClientError, match="another proposal"):
        amend(world, proposal, [rec], window, [world.critique_utxo(critique_of(world, other))])
    done = world.critique_utxo(critique_of(world, proposal, incorporated=True), byte=0xA2)
    with pytest.raises(gi.GovernanceClientError, match="already incorporated"):
        amend(world, proposal, [rec], window, [done])
    full = world.proposal_utxo(world.proposal_datum(amendment_count=5), byte=0x73)
    with pytest.raises(gi.GovernanceClientError, match="amendments"):
        amend(world, full, [rec], window)
    lock = world.locked(world.deployment.proposal_spend, datum, 27_000_000)
    with pytest.raises(gi.GovernanceClientError, match="no proposal token"):
        amend(world, lock, [rec], window)


def test_amend_proposal_refuses_an_agent_that_is_not_active(world):
    rec = world.agent(PROPOSER, suspended(record_datum(public_key(world.wallet))))
    proposal = world.proposal_utxo(world.proposal_datum())
    with pytest.raises(vid.IdentityStatusError):
        amend(world, proposal, [rec], world.window())


# ── Critiques ───────────────────────────────────────────────────────────────


def critique_terms(**changes) -> gi.CritiqueTerms:
    terms = gi.CritiqueTerms(vid.blake2b_256(b"critique"), "ipfs://QmCritique", 5_000_000)
    return dataclasses.replace(terms, **changes)


def lock_critique(world, records, proposal, window, terms=None):
    return gi.prepare_critique_lock(
        world.chain, world.deployment, records=records, refs=world.refs, proposal=proposal,
        terms=terms or critique_terms(), credential_key_hash=world.key_hash, window=window,
        wallet_address=world.wallet_address,
    )


def test_submit_critique_mints_through_critique_mint(world):
    rec = world.agent(CRITIC)
    proposal = world.proposal_utxo(world.proposal_datum())
    window = world.window()
    lock = lock_critique(world, [rec], proposal, window)
    lock_tx = sign(world, lock)
    assert gi.CritiqueDatum.from_data(lock_tx.transaction_body.outputs[0].datum).submitted_at == window.lo
    locked = world.locked(world.deployment.critique_spend, lock.datum, 7_000_000)

    tx = sign(world, gi.prepare_submit_critique(
        world.chain, world.deployment, locked=locked, records=[rec], refs=world.refs, proposal=proposal,
        window=window, wallet_address=world.wallet_address,
    ))
    deployment = world.deployment
    assert reference_inputs(tx) == {rec.utxo.input, world.params.input, world.cross_refs.input, proposal.input}
    assert required_signers(tx) == [world.key_hash]
    check_range(tx, window)
    token = gi.critique_token_name(locked.input)
    assert minted(tx) == {(deployment.critique_mint.hash, token): 1}
    assert spend_redeemers(tx) == {locked.input: gi.MINT_CRITIQUE_TOKEN}
    assert mint_redeemers(tx) == {deployment.critique_mint.hash: gi.MINT_CRITIQUE_TOKEN}
    out = tx.transaction_body.outputs[0]
    assert out.address == deployment.critique_spend.address
    assert gi.policy_tokens(out, deployment.critique_mint.hash) == {token: 1}
    datum = gi.CritiqueDatum.from_data(out.datum)
    assert datum == lock.datum and datum.submitted_at == window.lo
    assert datum.critic_credential == gi.Credential.key(world.key_hash)


def test_critique_refusals(world):
    proposer = world.agent(PROPOSER)
    critic = world.agent(CRITIC, byte=0x63)
    proposal = world.proposal_utxo(world.proposal_datum())
    window = world.window()
    with pytest.raises(gi.GovernanceClientError, match="Amendment critique"):
        lock_critique(world, [proposer], proposal, window)
    own = lock_critique(world, [proposer], proposal, window, critique_terms(
        critique_type=gi.amendment_critique(vid.blake2b_256(b"change"))))
    assert own.datum.is_amendment
    with pytest.raises(gi.GovernanceClientError, match="stake is below"):
        lock_critique(world, [critic], proposal, window, critique_terms(stake_amount=1))
    lock = world.locked(world.deployment.proposal_spend, world.proposal_datum(), 27_000_000)
    with pytest.raises(gi.GovernanceClientError, match="no proposal token"):
        lock_critique(world, [critic], lock, window)
    closed = world.proposal_utxo(world.proposal_datum(state=gi.ProposalState.WITHDRAWN), byte=0x74)
    with pytest.raises(gi.GovernanceClientError, match="WITHDRAWN"):
        lock_critique(world, [critic], closed, window)
    retired = world.agent(ENDORSER, revoked(record_datum(public_key(world.wallet))), byte=0x64)
    with pytest.raises(vid.IdentityStatusError):
        lock_critique(world, [retired], proposal, window)


def test_submit_critique_needs_the_named_proposal(world):
    rec = world.agent(CRITIC)
    proposal = world.proposal_utxo(world.proposal_datum())
    other = world.proposal_utxo(world.proposal_datum(), byte=0x75)
    locked = world.locked(world.deployment.critique_spend, critique_of(world, proposal), 7_000_000)
    with pytest.raises(gi.GovernanceClientError, match="is not"):
        gi.prepare_submit_critique(world.chain, world.deployment, locked=locked, records=[rec], refs=world.refs,
                                   proposal=other, window=world.window(), wallet_address=world.wallet_address)


# ── Endorsements ────────────────────────────────────────────────────────────


def test_submit_endorsement_through_the_validator(world):
    rec = world.agent(ENDORSER)
    proposal = world.proposal_utxo(world.proposal_datum())
    window = world.window()
    lock = gi.prepare_endorsement_lock(
        world.chain, world.deployment, records=[rec], refs=world.refs, proposal=proposal, stake_amount=5_000_000,
        credential_key_hash=world.key_hash, window=window, wallet_address=world.wallet_address,
    )
    assert lock.datum.created_at == window.lo
    locked = world.locked(world.deployment.endorsement_spend, lock.datum, 7_000_000)
    tx = sign(world, gi.prepare_submit_endorsement(
        world.chain, world.deployment, locked=locked, records=[rec], refs=world.refs, proposal=proposal,
        window=window, wallet_address=world.wallet_address,
    ))
    assert reference_inputs(tx) == {rec.utxo.input, world.params.input, world.cross_refs.input, proposal.input}
    assert required_signers(tx) == [world.key_hash]
    check_range(tx, window)
    assert not tx.transaction_body.mint
    assert spend_redeemers(tx) == {locked.input: gi.MINT_ENDORSEMENT_TOKEN}
    out = tx.transaction_body.outputs[0]
    assert (out.address, out.amount) == (world.deployment.endorsement_spend.address, locked.output.amount)
    datum = gi.EndorsementDatum.from_data(out.datum)
    assert datum == lock.datum
    assert datum.endorser_credential == gi.Credential.key(world.key_hash)


def test_endorsement_refusals(world):
    proposer = world.agent(PROPOSER)
    endorser = world.agent(ENDORSER, byte=0x63)
    proposal = world.proposal_utxo(world.proposal_datum())
    window = world.window()

    def lock(records, stake=5_000_000):
        return gi.prepare_endorsement_lock(
            world.chain, world.deployment, records=records, refs=world.refs, proposal=proposal,
            stake_amount=stake, credential_key_hash=world.key_hash, window=window,
            wallet_address=world.wallet_address,
        )

    with pytest.raises(gi.GovernanceClientError, match="own proposal"):
        lock([proposer])
    with pytest.raises(gi.GovernanceClientError, match="stake is below"):
        lock([endorser], stake=1)
    stranger = world.agent(CRITIC, record_datum(public_key(key_pair())), byte=0x64)
    with pytest.raises(vid.IdentityError, match="neither the controller"):
        lock([stranger])


# ── Flows (async, against the fake chain) ───────────────────────────────────


def test_flows_lock_then_create(world):
    world.agent(PROPOSER)
    world.agent(ENDORSER, byte=0x63)

    async def run():
        submitted = await gi.submit_proposal(
            world.chain, world.deployment, agent_id=vid.did_from_id(PROPOSER), terms=proposal_terms(),
            wallet=world.wallet, wallet_address=world.wallet_address, delay=0,
        )
        critique = await gi.submit_critique(
            world.chain, world.deployment, agent_id=PROPOSER, proposal_ref=submitted.position,
            terms=critique_terms(critique_type=gi.amendment_critique(vid.blake2b_256(b"x"))),
            wallet=world.wallet, wallet_address=world.wallet_address, delay=0,
        )
        amended = await gi.amend_proposal(
            world.chain, world.deployment, proposal_ref=submitted.position, new_hash=vid.blake2b_256(b"v2"),
            new_uri="ipfs://QmV2", wallet=world.wallet, wallet_address=world.wallet_address,
            incorporate=[critique.position],
        )
        endorsed = await gi.submit_endorsement(
            world.chain, world.deployment, agent_id=ENDORSER, proposal_ref=amended.position,
            stake_amount=5_000_000, wallet=world.wallet, wallet_address=world.wallet_address, delay=0,
        )
        return submitted, critique, amended, endorsed

    submitted, critique, amended, endorsed = asyncio.run(run())
    assert [str(tx.id) for tx in world.chain.submitted] == [
        submitted.lock_tx, submitted.tx, critique.lock_tx, critique.tx, amended.tx, endorsed.lock_tx, endorsed.tx]
    assert submitted.activity == TransactionInput(submitted.position.transaction_id, 1)
    assert submitted.minted == (gi.proposal_token_name(TransactionInput(world.chain.submitted[0].id, 0)),
                                gi.activity_token_name(PROPOSER))
    activity = gi.find_activity(world.chain.utxos(world.deployment.proposal_spend.address), world.deployment, PROPOSER)
    assert activity.utxo.input == submitted.activity and activity.datum.active_proposal_count == 1
    proposal = gi.find_utxo(world.chain.utxos(world.deployment.proposal_spend.address), amended.position)
    proposal_datum = gi.ProposalDatum.from_data(proposal.output.datum)
    assert proposal_datum.state == gi.ProposalState.AMENDED
    assert proposal_datum.incorporated_critiques == (critique.position,)
    endorsement = gi.find_utxo(world.chain.utxos(world.deployment.endorsement_spend.address), endorsed.position)
    assert gi.EndorsementDatum.from_data(endorsement.output.datum).proposal_ref == amended.position
    for index in (1, 3, 4, 6):
        assert required_signers(world.chain.submitted[index]) == [world.key_hash]
    assert not world.chain.submitted[0].transaction_body.required_signers


def test_sdk_reference_inputs_format(world):
    assert gi.sdk_reference_inputs(world.params) == [{
        "tx_hash": str(world.params.input.transaction_id),
        "output_index": world.params.input.index,
        "address": str(world.deployment.params_address),
    }]


def test_signing_needs_the_wallet_key(world):
    rec = world.agent(PROPOSER)
    prepared = lock_proposal(world, [rec], world.window())
    with pytest.raises(gi.GovernanceClientError, match=world.key_hash.hex()):
        gi.sign_prepared(prepared, keys=[key_pair()], change_address=world.wallet_address)


def test_signed_transaction_round_trips(world):
    rec = world.agent(PROPOSER)
    _, prepared = submit_proposal(world, [rec], world.window())
    tx = sign(world, prepared)
    decoded = type(tx).from_cbor(tx.to_cbor())
    assert decoded.id == tx.id
    assert required_signers(decoded) == [world.key_hash]
    assert gi.ProposalDatum.from_data(decoded.transaction_body.outputs[0].datum) == prepared.datum
