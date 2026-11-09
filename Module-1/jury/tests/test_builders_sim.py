"""Integration: every jury action is built offline by its builder, finished
by ``finish`` (pass 1, units x1.05, pass 2, signatures, final evaluation)
and evaluated with ``aiken tx simulate`` on the validator applied from
``contracts/plutus.json`` (``conftest.FakeChain``). The chain view moves with
each transaction, so the lifecycle runs end to end: register, open, commit,
reveal, finalize, slash, claim, close, request-exit, exit and top-up.

Negative cases build a transaction that the validator must refuse and check
which redeemer fails: a reveal with a wrong salt (the case spend), a commit
after ``commit_end`` (the case spend) and a slash before ``reveal_end`` (the
juror spend).
"""

import json

import pytest

from conftest import AP3X, World, challenge_utxo, claim_utxo, h32
from vector_jury import builders, documents, rules, state
from vector_jury import types as T
from vector_jury.builders import BuildError
from vector_jury.chain import EvaluationError, ref_key

pytestmark = pytest.mark.aiken


def prompt_doc(name: str) -> bytes:
    return json.dumps({"type": "vector-jury-prompt/1", "juror": name, "max_output_tokens": 4000}).encode()


def record_doc(name: str, vote: str) -> bytes:
    return json.dumps({"type": "vector-jury-record/1", "juror": name, "vote": vote, "reason": "test é ✓"}).encode()


def juror_of(world: World, token: bytes) -> state.JurorOutput:
    found = state.find_juror(world.chain, world.deployment, token)
    assert found is not None, token.hex()
    return found


def case_of(world: World, token: bytes) -> state.CaseOutput:
    found = state.find_case(world.chain, world.deployment, token)
    assert found is not None, token.hex()
    return found


def register(world: World, name: str) -> bytes:
    world.add_identity(name)
    key = world.juror_key(name)
    plan = builders.build_register(world.chain, world.deployment, world.payer.address, agent=world.agent(name),
                                   owner=key.hash, prompt=prompt_doc(name))
    result = world.run(plan, key)
    assert result.signed and result.size <= 16_384
    return result.info["juror_token"]


def module1_pair(world: World, tag: bytes):
    claim_token = rules.token_name(b"clm_", (h32(tag + b" claim seed"), 0))
    ch = challenge_utxo(world.params, tag=tag, auditor=world.auditor, claim_token=claim_token,
                        challenged_at=world.now_ms() - 600_000)
    cl = claim_utxo(world.params, tag=tag, claimer=world.claimer, token=claim_token)
    world.chain.add(ch, cl)
    return ch, cl


def open_case(world: World, tag: bytes, fee: int) -> bytes:
    ch, _cl = module1_pair(world, tag)
    plan = builders.build_open(world.chain, world.deployment, world.payer.address, challenge=ref_key(ch), fee=fee)
    return world.run(plan).info["case_token"]


def salt_of(name: str) -> bytes:
    return h32(b"salt " + name.encode())


def commit(world: World, juror: bytes, case: bytes, name: str, vote: str, **kw):
    plan = builders.build_commit(world.chain, world.deployment, world.payer.address, juror=juror_of(world, juror),
                                 case=case_of(world, case), vote=vote, salt=kw.pop("salt", salt_of(name)),
                                 record_hash=documents.doc_hash(record_doc(name, vote)), **kw)
    return plan


def reveal(world: World, juror: bytes, case: bytes, name: str, vote: str, **kw):
    return builders.build_reveal(world.chain, world.deployment, world.payer.address, juror=juror_of(world, juror),
                                 case=case_of(world, case), vote=vote, salt=kw.pop("salt", salt_of(name)),
                                 record=record_doc(name, vote), **kw)


def failing_redeemers(plan, error: EvaluationError) -> set:
    """The names of the plan's redeemers that the evaluation reports as
    failing (aiken_simulate evaluates each redeemer first)."""
    protocol = plan.chain.protocol_parameters()
    count = len(plan.redeemers)
    placeholder = builders.ExecutionUnits(protocol.max_tx_ex_mem // count, protocol.max_tx_ex_steps // count)
    _builder, redeemers, _tx, _body = builders._assemble(plan, {n: placeholder for n in plan.redeemers}, 0)
    names = {builders._key(r): n for n, r in redeemers.items()}
    return {names[k] for k in error.failing}


def expect_refusal(world: World, plan, *keys) -> set:
    with pytest.raises(EvaluationError) as raised:
        world.run(plan, *keys)
    return failing_redeemers(plan, raised.value)


def test_lifecycle(world):
    params = world.params
    names = ("ann", "bob", "cat", "dan", "eve")
    jurors = {n: register(world, n) for n in names}
    ann = juror_of(world, jurors["ann"])
    assert ann.datum == T.Juror(world.agent("ann"), world.juror_key("ann").hash, documents.doc_hash(prompt_doc("ann")),
                                ann.datum.registered_at, T.Nothing(), [])
    assert ann.lovelace == params.bond_min

    # Eligible three days after the registration.
    world.advance(params.eligibility_delay // 1000 + 1_300)
    fee = 3 * AP3X
    case_x = open_case(world, b"case x", fee)
    x = case_of(world, case_x)
    assert x.datum.fee == fee and x.datum.ballots == [] and x.datum.state == T.Open()
    assert x.lovelace - fee >= builders.min_utxo(world.chain, x.utxo.output)
    assert x.datum.commit_end == x.datum.opened_at + params.commit_window

    votes = {"ann": "AuditorWins", "bob": "AuditorWins", "cat": "ClaimerWins", "dan": "AuditorWins"}
    for name, vote in votes.items():
        world.run(commit(world, jurors[name], case_x, name, vote), world.juror_key(name))
    x = case_of(world, case_x)
    assert [bytes(b.juror) for b in x.datum.ballots] == [jurors[n] for n in reversed(list(votes))]
    assert x.lovelace - fee >= builders.min_utxo(world.chain, x.utxo.output)
    assert [bytes(e.case) for e in juror_of(world, jurors["dan"]).datum.open] == [case_x]

    # Negative: a commit after commit_end (eve is eligible, the window is over).
    world.advance((x.datum.commit_end - world.now_ms()) // 1000 + 120)
    late = commit(world, jurors["eve"], case_x, "eve", "ClaimerWins", check=False,
                  validity=(world.chain.slot - 60, world.chain.slot + 600))
    assert expect_refusal(world, late, world.juror_key("eve")) == {"case"}
    with pytest.raises(BuildError, match="commit window"):
        commit(world, jurors["eve"], case_x, "eve", "ClaimerWins")

    # Negative: a reveal with a wrong salt.
    wrong = reveal(world, jurors["ann"], case_x, "ann", "AuditorWins", salt=h32(b"wrong"), check=False)
    assert expect_refusal(world, wrong, world.juror_key("ann")) == {"case"}
    with pytest.raises(BuildError, match="do not match the commitment"):
        reveal(world, jurors["ann"], case_x, "ann", "AuditorWins", salt=h32(b"wrong"))

    for name in ("ann", "bob", "cat"):
        result = world.run(reveal(world, jurors[name], case_x, name, votes[name]), world.juror_key(name))
        assert documents.parse_metadata(result.cbor).data == record_doc(name, votes[name])
    x = case_of(world, case_x)
    assert x.datum.tally == T.Tally(1, 2, 0)
    assert rules.outcome(x.datum.tally, params) == T.AuditorWins()

    # Negative: a slash before reveal_end.
    early = builders.build_slash(world.chain, world.deployment, world.payer.address, juror=juror_of(world, jurors["dan"]),
                                 case=case_x, check=False, validity=(world.chain.slot - 60, world.chain.slot + 600))
    assert expect_refusal(world, early) == {"juror"}
    with pytest.raises(BuildError):
        builders.build_slash(world.chain, world.deployment, world.payer.address,
                             juror=juror_of(world, jurors["dan"]), case=case_x)

    # Finalize: fee 3 AP3X, 3 reveals -> Settling, share 1 AP3X.
    world.advance((x.datum.reveal_end - world.now_ms()) // 1000 + 120)
    result = world.run(builders.build_finalize(world.chain, world.deployment, world.payer.address, case=x))
    assert result.info["result"] == "AuditorWins" and result.info["share"] == AP3X and not result.info["burns"]
    x = case_of(world, case_x)
    assert x.datum.state == T.Settling(AP3X)

    # Slash dan (committed, never revealed).
    dan = juror_of(world, jurors["dan"])
    result = world.run(builders.build_slash(world.chain, world.deployment, world.payer.address, juror=dan, case=case_x))
    after = juror_of(world, jurors["dan"])
    assert after.lovelace == dan.lovelace - params.slash and after.datum.open == []
    treasury = world.chain.utxos_at(world.deployment.treasury_address)
    assert [u.output.amount.coin for u in treasury] == [params.slash]

    # Claim the shares, then close.
    for name in ("ann", "bob", "cat"):
        before = juror_of(world, jurors[name]).lovelace
        world.run(builders.build_claim(world.chain, world.deployment, world.payer.address,
                                       juror=juror_of(world, jurors[name]), case=case_of(world, case_x)),
                  world.juror_key(name))
        assert juror_of(world, jurors[name]).lovelace == before + AP3X
    x = case_of(world, case_x)
    assert [type(b.status).__name__ for b in x.datum.ballots] == ["Committed", "Paid", "Paid", "Paid"]
    refund_to = T.address_of(x.datum.refund_to)
    result = world.run(builders.build_close(world.chain, world.deployment, world.payer.address, case=x))
    assert state.find_case(world.chain, world.deployment, case_x) is None
    refund = [u for u in world.chain.utxos_at(refund_to) if u.output.datum is not None]
    assert [T.same_data(u.output.datum, T.OutputReference.of(x.utxo.input)) for u in refund] == [True]
    assert refund[0].output.amount.coin == x.lovelace

    # A case without a fee and without a ballot: Finalize burns it.
    case_z = open_case(world, b"case z", 0)
    z = case_of(world, case_z)
    world.advance((z.datum.reveal_end - world.now_ms()) // 1000 + 120)
    result = world.run(builders.build_finalize(world.chain, world.deployment, world.payer.address, case=z))
    assert result.info["burns"] and state.find_case(world.chain, world.deployment, case_z) is None

    # Exit: request, wait for the notice, exit (ann has no open entry).
    result = world.run(builders.build_request_exit(world.chain, world.deployment, world.payer.address,
                                                   juror=juror_of(world, jurors["ann"])), world.juror_key("ann"))
    exit_after = rules.exit_allowed_at(juror_of(world, jurors["ann"]).datum)
    assert exit_after == result.info["exit_after"]
    with pytest.raises(BuildError, match="exit"):
        builders.build_exit(world.chain, world.deployment, world.payer.address, juror=juror_of(world, jurors["ann"]))
    world.advance((exit_after - world.now_ms()) // 1000 + 120)
    world.run(builders.build_exit(world.chain, world.deployment, world.payer.address,
                                  juror=juror_of(world, jurors["ann"])), world.juror_key("ann"))
    assert state.find_juror(world.chain, world.deployment, jurors["ann"]) is None

    # Top-up.
    bob = juror_of(world, jurors["bob"])
    world.run(builders.build_top_up(world.chain, world.deployment, world.payer.address, juror=bob, amount=7 * AP3X),
              world.juror_key("bob"))
    assert juror_of(world, jurors["bob"]).lovelace == bob.lovelace + 7 * AP3X
    assert world.chain.evaluations > 0


def test_register_refuses_a_subagent_and_a_foreign_owner(world):
    world.add_identity("sub", subagent_of=h32(b"parent did"))
    with pytest.raises(BuildError, match="subagent"):
        builders.build_register(world.chain, world.deployment, world.payer.address, agent=world.agent("sub"),
                                owner=world.juror_key("sub").hash, prompt=b"{}")
    world.add_identity("own")
    with pytest.raises(BuildError, match="neither the controller"):
        builders.build_register(world.chain, world.deployment, world.payer.address, agent=world.agent("own"),
                                owner=world.payer.hash, prompt=b"{}")


def test_register_without_the_owner_signature_fails_on_chain(world):
    """The builder lists the owner as a required signer; a transaction that
    names another key fails the mint (the identity check needs the owner in
    extra_signatories)."""
    world.add_identity("sig")
    key = world.juror_key("sig")
    plan = builders.build_register(world.chain, world.deployment, world.payer.address, agent=world.agent("sig"),
                                   owner=key.hash, prompt=b"{}")
    make = plan.make

    def without_owner(units):
        b, named = make(units)
        b.required_signers = []
        return b, named

    plan.make = without_owner
    plan.signers = (world.payer.hash,)
    assert expect_refusal(world, plan) == {"mint"}


def test_dry_run_without_keys_is_unsigned(world):
    world.add_identity("dry")
    plan = builders.build_register(world.chain, world.deployment, world.payer.address, agent=world.agent("dry"),
                                   owner=world.juror_key("dry").hash, prompt=b"{}")
    result = builders.finish(plan)
    assert not result.signed and not result.submitted
    assert set(result.missing_signers) == {world.payer.hash, world.juror_key("dry").hash}
    with pytest.raises(BuildError, match="signing keys"):
        builders.finish(plan, submit=True)
