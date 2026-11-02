"""The transactions of the client builders against the compiled validators.

Module-3/reputation-staking/plutus.json is applied with a test
ReputationConfig (the values of tests/chain_fixtures.py), and the Module-1
claim validator of Module-1/contracts/plutus.json with a test AuditingConfig
and ProtocolParams; its hash is the audit_claim_validator_hash of the
ReputationConfig. The fake chain uses the applied scripts, hashes and
addresses. aiken tx simulate (shared/python/aiken_simulate.py) evaluates
every redeemer; the time base is the system start of the client
(constants.SYSTEM_START_UNIX_S). A refusal test changes one fact of a built
transaction (a resolved input or the time base) and checks that the expected
redeemer fails.
"""

from __future__ import annotations

from pathlib import Path

import cbor2
import pytest
from pycardano import PaymentSigningKey, UTxO

from aiken_simulate import aiken_available, apply_validator, resolved_inputs, simulate

import tests.chain_fixtures as cf
from reputation_staking.client import challenge_timeout
from reputation_staking.constants import SYSTEM_START_UNIX_S
from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.plutus_data import (
    AuditAuditorWins,
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeInconclusive,
    RepChallengeOutcomeVerified,
    RepChallengeStateEscalated,
    RepChallengeStateResolved,
    ReputationConfig,
)

pytestmark = pytest.mark.skipif(not aiken_available(), reason="aiken is not on PATH")

REPO = Path(__file__).resolve().parents[3]
M3_BLUEPRINT = REPO / "Module-3" / "reputation-staking" / "plutus.json"
M1_BLUEPRINT = REPO / "Module-1" / "contracts" / "plutus.json"
ZERO_TIME_MS = SYSTEM_START_UNIX_S * 1000
WALLET = PaymentSigningKey(bytes.fromhex("11" * 32))  # also the oracle of the test config
AP3X = cf.AP3X
HOUR = 3_600_000
PARAMS = ProtocolParamsInfo()


def constr(index: int, fields) -> cbor2.CBORTag:
    return cbor2.CBORTag(121 + index, list(fields))


def module1_claim():
    """The Module-1 claim validator with a test AuditingConfig (the identity
    registry of the fake chain; a 7-day suspension for AuditorWins) and the
    Module-1 ProtocolParams defaults (params.ak; oracle_active = True)."""
    config = constr(0, [
        bytes.fromhex(cf.IDENTITY_HASH), b"\x31" * 28, b"\x32" * 28, b"\x33" * 28, b"\x34" * 28,
        constr(0, [constr(0, [constr(0, [constr(0, []), 604_800_000])]), constr(1, [])]),
    ])
    params = constr(0, [
        50 * AP3X, 1_800_000, 64_800_000, 5, 25 * AP3X, 1000, 30_000, 5_400_000, 1000,
        21_600_000, 5, 10, 250 * AP3X, constr(1, []), 3_600_000, 1_800_000, 1_800_000,
    ])
    return apply_validator(
        M1_BLUEPRINT, "claim", "claim", cbor2.dumps(config).hex(), cbor2.dumps(params).hex(),
    )


@pytest.fixture(scope="module")
def scripts():
    claim = module1_claim()
    config = ReputationConfig(
        refs_token_policy=bytes.fromhex(cf.REFS_POLICY),
        audit_claim_validator_hash=bytes.fromhex(claim.hash),
        audit_challenge_validator_hash=bytes.fromhex(cf.M1_CHALLENGE_HASH),
        audit_jury_pool_hash=bytes.fromhex("b3" * 28),
        audit_refs_token_policy=bytes.fromhex("b4" * 28),
        params_script_hash=bytes.fromhex(cf.PARAMS_HOLDER),
        treasury_script_hash=bytes.fromhex(cf.TREASURY),
        ap3x_policy_id=b"",
        ap3x_asset_name=b"",
        oracle_credential=cf.key_hash(WALLET),
        params_token_policy=bytes.fromhex(cf.PARAMS_POLICY),
        identity_hash=bytes.fromhex(cf.IDENTITY_HASH),
    ).to_cbor_hex()
    rep = apply_validator(M3_BLUEPRINT, "reputation", "reputation", config)
    end = apply_validator(M3_BLUEPRINT, "endorsement", "endorsement", config)
    return rep, end, claim


@pytest.fixture
def world(scripts, monkeypatch):
    """The fake chain with the applied scripts: the module-level scripts and
    hashes of chain_fixtures come from the applied blueprints."""
    rep, end, claim = scripts
    for name, value in (
        ("REP_SCRIPT", rep.script), ("REP_HASH", rep.hash),
        ("END_SCRIPT", end.script), ("END_HASH", end.hash),
        ("CLAIM_SCRIPT", claim.script), ("CLAIM_HASH", claim.hash),
    ):
        monkeypatch.setattr(cf, name, value)
    w = cf.World(wallet=WALLET)
    w.record("A")
    w.record("B")
    return w


def run(world, tx, name: str, replace=(), shift_ms: int = 0):
    """Simulate `tx` with the UTxOs of the fake chain (`replace`: UTxOs that
    take the place of the chain UTxOs with the same output reference)."""
    pool = dict(world.context.refs)
    for utxo in replace:
        pool[cf.ref(utxo)] = utxo
    sim = simulate(
        tx, resolved_inputs(tx, list(pool.values())), zero_time_ms=ZERO_TIME_MS + shift_ms,
    )
    print(f"SIM {name}: {'PASS' if sim.passed else 'FAIL'} {sim.describe()}")
    return sim


def spend_of(tx, utxo) -> str:
    """The purpose of the redeemer that spends `utxo` (inputs in ledger order)."""
    inputs = sorted(tx.transaction_body.inputs, key=lambda i: (bytes(i.transaction_id), i.index))
    keys = [(bytes(i.transaction_id), i.index) for i in inputs]
    return f"Spend[{keys.index((bytes(utxo.input.transaction_id), utxo.input.index))}]"


def same_ref(original: UTxO, changed: UTxO) -> UTxO:
    """`changed` at the output reference of `original`."""
    return UTxO(original.input, changed.output)


def passes(sim):
    assert sim.passed, sim.report[-3000:]


# ── Openings ─────────────────────────────────────────────────────────────────


def test_create_stake_writes_the_seed_as_stake_id(world):
    seed = world.seed("A")
    tx = world.client().build_create_stake(
        world.did("A"), ["code_review", "testing"], 10 * AP3X, cf.ref(seed),
        controller_skey=world.ctrl["A"],
    )
    passes(run(world, tx, "CreateStake"))


def test_mint_endorsement_binds_the_stake(world):
    world.stake_a()
    tx = world.client().build_mint_endorsement(
        world.did("B"), world.did("A"), ["code_review"], 20 * AP3X, controller_skey=world.ctrl["B"],
    )
    passes(run(world, tx, "MintEndorsement"))


def test_mint_challenge_locks_the_stake(world):
    world.stake_a()
    tx, _ = world.client().build_mint_challenge(
        world.did("B"), world.did("A"), "code_review", 25 * AP3X, "ab" * 32, "ipfs://e",
        controller_skey=world.ctrl["B"],
    )
    passes(run(world, tx, "MintChallenge"))


# ── Exits ────────────────────────────────────────────────────────────────────


def test_decrease_stake(world):
    world.stake_a()
    tx = world.client().build_decrease_stake(world.did("A"), 40 * AP3X, controller_skey=world.ctrl["A"])
    passes(run(world, tx, "DecreaseStake"))


def add_endorsement(world, **stake_kwargs):
    stake = world.stake_a(**stake_kwargs)
    endorsement = world.context.add(cf.endorsement_utxo(
        world.ids["B"], world.ids["A"], cf.key_hash(world.wallet),
        created_at=cf.LO - 3 * 86_400_000, target_stake=cf.stake_datum_of(stake).stake_id,
    ))
    return stake, endorsement


def test_withdraw_endorsement(world):
    add_endorsement(world)
    tx = world.client().build_withdraw_endorsement(
        world.did("B"), world.did("A"), controller_skey=world.ctrl["B"],
    )
    passes(run(world, tx, "WithdrawEndorsement"))


def test_withdraw_endorsement_fails_with_a_locked_bound_stake(world):
    """The same transaction with a lock in the bound stake (the reference
    input): the endorsement spend fails, the burn passes."""
    stake, endorsement = add_endorsement(world)
    tx = world.client().build_withdraw_endorsement(
        world.did("B"), world.did("A"), controller_skey=world.ctrl["B"],
    )
    locked = same_ref(stake, cf.stake_utxo(
        world.ids["A"], world.owner_a, challenge_locks=(world.lock_b(),),
        stake_id=cf.stake_datum_of(stake).stake_id,
    ))
    sim = run(world, tx, "WithdrawEndorsement/locked", replace=[locked])
    assert not sim.passed
    assert sim.failing == [spend_of(tx, endorsement)]


def add_challenge(world, created_at, response=False, state=None):
    stake = world.stake_a(challenge_locks=(world.lock_b(),))
    extra = dict(counter_evidence_hash=b"\x0f" * 32, response_submitted_at=created_at + HOUR)
    challenge = world.context.add(cf.challenge_utxo(
        world.ids["B"], world.ids["A"], cf.key_hash(world.wallet), world.owner_a,
        created_at=created_at, state=state, **(extra if response else {}),
    ))
    return stake, challenge


def test_withdraw_challenge(world):
    add_challenge(world, cf.LO - HOUR)
    tx = world.client().build_withdraw_challenge(
        world.did("B"), world.did("A"), "code_review", controller_skey=world.ctrl["B"],
    )
    passes(run(world, tx, "WithdrawChallenge"))


# ── Permissionless closing and payouts ───────────────────────────────────────

EXPIRED_AT = cf.LO - challenge_timeout(PARAMS, 0) - 1


def test_expire_challenge(world):
    add_challenge(world, EXPIRED_AT, response=True)
    tx = world.client().build_expire_challenge(world.did("B"), world.did("A"), "code_review")
    passes(run(world, tx, "ExpireChallenge"))


def test_expire_challenge_fails_before_the_timeout(world):
    """The same transaction 1 ms earlier: lo is the timeout, not after it."""
    _, challenge = add_challenge(world, EXPIRED_AT, response=True)
    tx = world.client().build_expire_challenge(world.did("B"), world.did("A"), "code_review")
    sim = run(world, tx, "ExpireChallenge/early", shift_ms=-1)
    assert not sim.passed
    assert sim.failing == [spend_of(tx, challenge)]


@pytest.mark.parametrize("outcome", [RepChallengeOutcomeVerified(), RepChallengeOutcomeInconclusive()])
def test_distribute_outcome(world, outcome):
    add_challenge(world, cf.LO - 86_400_000, state=RepChallengeStateResolved(outcome=outcome))
    tx = world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")
    passes(run(world, tx, f"DistributeOutcome/{type(outcome).__name__}"))


def test_distribute_falsified_outcome(world):
    add_challenge(
        world, cf.LO - 86_400_000,
        state=RepChallengeStateResolved(outcome=RepChallengeOutcomeFalsified()),
    )
    tx = world.client().build_distribute_falsified_outcome(world.did("B"), world.did("A"), "code_review")
    passes(run(world, tx, "DistributeOutcome/Falsified"))


def test_payout_to_the_controller_after_a_key_removal(world):
    """A's record no longer lists owner_a: the client pays A's controller."""
    add_challenge(
        world, cf.LO - 86_400_000,
        state=RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified()),
    )
    world.record("A", keys=())
    tx = world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")
    passes(run(world, tx, "DistributeOutcome/controller"))


def test_payout_fails_to_a_credential_that_the_record_no_longer_declares(world):
    """The transaction pays target_credential (owner_a); in the chain state
    of the simulation A's record no longer lists owner_a, so the payee is
    A's controller key and the challenge spend fails."""
    _, challenge = add_challenge(
        world, cf.LO - 86_400_000,
        state=RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified()),
    )
    tx = world.client().build_distribute_outcome(world.did("B"), world.did("A"), "code_review")
    record = world.records["A"]
    without_key = same_ref(record, cf.record_utxo(world.ids["A"], cf.identity_record(
        cf.public_key(world.ctrl["A"]), keys=(),
    )))
    sim = run(world, tx, "DistributeOutcome/undeclared", replace=[without_key])
    assert not sim.passed
    assert sim.failing == [spend_of(tx, challenge)]


@pytest.mark.parametrize("amount, name", [(20 * AP3X, "continues"), (8 * AP3X, "closes")])
def test_slash_endorsement(world, amount, name):
    stake = world.stake_a(challenge_locks=(world.lock_b(),))
    challenge = world.context.add(cf.challenge_utxo(
        world.ids["B"], world.ids["A"], cf.key_hash(world.wallet), world.owner_a,
        state=RepChallengeStateResolved(outcome=RepChallengeOutcomeFalsified()),
    ))
    world.context.add(cf.endorsement_utxo(
        world.ids["B"], world.ids["A"], cf.key_hash(world.wallet), amount=amount,
        target_stake=cf.stake_datum_of(stake).stake_id,
    ))
    tx = world.client().build_slash_endorsement(world.did("B"), world.did("A"), cf.ref(challenge))
    passes(run(world, tx, f"SlashEndorsement/{name}"))


# ── Escalation ───────────────────────────────────────────────────────────────


def test_escalate_to_audit_with_the_module1_claim(world):
    """The whole transaction passes: the Module-3 challenge spend and the
    Module-1 claim mint (SubmitClaim of the applied claim validator)."""
    add_challenge(world, cf.LO - 13 * HOUR, response=True)
    tx, _ = world.client().build_escalate_to_audit(
        world.did("B"), world.did("A"), "code_review", 50 * AP3X, 1_800_000, cf.CLAIM_REF,
        storage_uri="ipfs://claim", claimer_skey=world.owner_a_key,
    )
    passes(run(world, tx, "EscalateToAudit"))


def test_resolve_escalation(world):
    token = b"clm_" + b"\x07" * 28
    add_challenge(
        world, cf.LO - 2 * 86_400_000, response=True,
        state=RepChallengeStateEscalated(audit_claim_token=token),
    )
    world.context.add(cf.audit_challenge_utxo(
        token, world.ids["B"], cf.audit_resolved(AuditAuditorWins()),
    ))
    tx = world.client().build_resolve_escalation(world.did("B"), world.did("A"), "code_review")
    passes(run(world, tx, "ResolveEscalation"))
