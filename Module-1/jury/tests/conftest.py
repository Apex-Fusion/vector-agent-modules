"""Shared test helpers of the jury library.

``FakeChain`` is a ``vector_jury.chain.Chain`` without a network: its UTxOs
live in a dict, its tip is a number, and ``evaluate`` runs ``aiken tx
simulate`` (``shared/python/aiken_simulate.py``) on the validator that the
test applied from ``Module-1/jury/contracts/plutus.json``. So the builders
and ``finish`` run unchanged: pass 1, units x1.05, pass 2, signatures, final
evaluation.

The tests that need the blueprint skip with a reason while the contract has
not written it.
"""

import hashlib
import sys
from pathlib import Path

JURY_DIR = Path(__file__).resolve().parents[1]
MODULE1_DIR = JURY_DIR.parent
SHARED_PYTHON = MODULE1_DIR.parent / "shared" / "python"
for _path in (JURY_DIR, MODULE1_DIR, SHARED_PYTHON):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402
from pycardano import (  # noqa: E402
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawCBOR,
    ScriptHash,
    Transaction,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)

import aiken_simulate as sim  # noqa: E402
import vector_deploy as vd  # noqa: E402
from simulation import vector_identity as vi  # noqa: E402
from vector_jury import builders, rules  # noqa: E402
from vector_jury import types as T  # noqa: E402
from vector_jury.chain import Chain, EvaluationError, ref_key  # noqa: E402
from vector_jury.deployment import (  # noqa: E402
    REFERENCE_ADDRESS,
    Deployment,
    encode_params,
    params_from_json,
    script_address,
)

BLUEPRINT = JURY_DIR / "contracts" / "plutus.json"
#: Vector mainnet: POSIX ms of slot 0.
ZERO_TIME_MS = 1756485600000
AP3X = 1_000_000


def h32(tag: bytes) -> bytes:
    return hashlib.blake2b(tag, digest_size=32).digest()


def h28(tag: bytes) -> bytes:
    return hashlib.blake2b(tag, digest_size=28).digest()


class Key:
    """A fixed test key."""

    def __init__(self, tag: bytes):
        self.skey = PaymentSigningKey(h32(b"jury-test-key:" + tag))
        self.vkey = PaymentVerificationKey.from_signing_key(self.skey)
        self.hash = bytes(self.vkey.hash().payload)
        self.address = Address(self.vkey.hash(), network=Network.MAINNET)


#: Test parameters: the mainnet windows and amounts, test hashes.
TEST_PARAMS_JSON = {
    "identity_hash": h28(b"test identity").hex(),
    "m1_claim": h28(b"test m1 claim").hex(),
    "m1_challenge": h28(b"test m1 challenge").hex(),
    "treasury": str(script_address(h28(b"test treasury"))),
    "bond_min": 250_000_000,
    "slash": 25_000_000,
    "juror_floor": 10_000_000,
    "eligibility_delay": 259_200_000,
    "exit_notice": 259_200_000,
    "commit_window": 7_200_000,
    "reveal_window": 7_200_000,
    "resolve_margin": 7_200_000,
    "claim_period": 2_592_000_000,
    "quorum": 3,
    "majority_num": 2,
    "majority_den": 3,
}

#: Ogmios protocol parameters like Vector mainnet (the cost model values do
#: not matter to aiken tx simulate). The transaction memory limit is aiken's.
RAW_PROTOCOL = {
    "minFeeConstant": {"ada": {"lovelace": 156253}},
    "minFeeCoefficient": 45,
    "maxBlockBodySize": {"bytes": 90112},
    "maxTransactionSize": {"bytes": 16384},
    "maxBlockHeaderSize": {"bytes": 1100},
    "stakeCredentialDeposit": {"ada": {"lovelace": 2_000_000}},
    "stakePoolDeposit": {"ada": {"lovelace": 500_000_000}},
    "stakePoolPledgeInfluence": "3/10",
    "monetaryExpansion": "3/1000",
    "treasuryExpansion": "1/5",
    "version": {"major": 10, "minor": 0},
    "minUtxoDepositConstant": {"ada": {"lovelace": 0}},
    "minStakePoolCost": {"ada": {"lovelace": 170_000_000}},
    "scriptExecutionPrices": {"memory": "577/10000", "cpu": "721/10000000"},
    "maxExecutionUnitsPerTransaction": {"memory": 14_000_000, "cpu": 10_000_000_000},
    "maxExecutionUnitsPerBlock": {"memory": 62_000_000, "cpu": 20_000_000_000},
    "maxValueSize": {"bytes": 5000},
    "collateralPercentage": 150,
    "maxCollateralInputs": 3,
    "minUtxoDepositCoefficient": 4310,
    "plutusCostModels": {"plutus:v3": [0] * 297},
    "minFeeReferenceScripts": {"base": 15.0, "range": 25600, "multiplier": 1.2},
    "maxReferenceScriptsSize": {"bytes": 204800},
}


def txin(tag: bytes, index: int = 0) -> TransactionInput:
    return TransactionInput(TransactionId(h32(tag)), index)


def tokens(policy: bytes, name: bytes, quantity: int = 1) -> MultiAsset:
    return MultiAsset({ScriptHash(policy): Asset({AssetName(name): quantity})})


def _purpose_key(purpose: str) -> str:
    """aiken_simulate "Spend[1]" -> Ogmios/pycardano "spend:1"."""
    name, _, rest = purpose.partition("[")
    return f"{name.lower()}:{rest.rstrip(']')}"


class FakeKoios:
    def __init__(self):
        self.metadata = {}
        self.heights = {}
        self.mints = {}

    def tx_metadata(self, tx_hashes):
        return [{"tx_hash": t, "metadata": self.metadata.get(t)} for t in tx_hashes]

    def tx_info(self, tx_hashes):
        return [{"tx_hash": t, "block_height": self.heights[t]} for t in tx_hashes if t in self.heights]

    def asset_history(self, policy, name):
        txs = self.mints.get((policy, name), [])
        return [{"policy_id": policy, "asset_name": name,
                 "minting_txs": [{"tx_hash": t, "quantity": "1", "block_time": i} for i, t in enumerate(txs)]}]


class FakeChain(Chain):
    """A Chain on a dict of UTxOs; ``evaluate`` runs aiken tx simulate."""

    def __init__(self, slot: int):
        super().__init__("mainnet", ogmios=object(), koios=FakeKoios())
        self.pool = {}
        self.slot = slot
        self._protocol = vd.protocol_parameters(RAW_PROTOCOL)
        self.submitted = []
        self.evaluations = 0

    def add(self, *utxos: UTxO) -> None:
        for utxo in utxos:
            self.pool[ref_key(utxo)] = utxo

    def _live(self, *, addresses=(), refs=()):
        wanted = {str(a) for a in addresses}
        keys = {ref_key(r) for r in refs}
        return [u for k, u in self.pool.items() if str(u.output.address) in wanted or k in keys]

    def tip_slot(self) -> int:
        return self.slot

    def protocol_parameters(self):
        return self._protocol

    def check_network(self) -> None:
        return None

    def resolved(self, tx: Transaction):
        return sim.resolved_inputs(tx, list(self.pool.values()) + list(self.virtual.values()))

    def evaluate(self, tx, additional=None):
        data = tx if isinstance(tx, bytes) else bytes.fromhex(tx)
        parsed = Transaction.from_cbor(data)
        self.evaluations += 1
        result = sim.simulate(parsed, self.resolved(parsed), zero_time_ms=ZERO_TIME_MS)
        if not result.passed:
            raise EvaluationError(result.describe() + "\n" + result.report,
                                  failing=[_purpose_key(p) for p in result.failing])
        return {_purpose_key(v.purpose): ExecutionUnits(v.mem, v.cpu) for v in result.verdicts}

    def submit(self, tx) -> str:
        """Record the transaction and show its outputs at once (as if a block
        confirmed it)."""
        self.submitted.append(tx)
        return self.apply(tx)

    def snapshot(self):
        return dict(self.pool), dict(self.virtual), set(self.spent), self.slot

    def restore(self, snap) -> None:
        pool, virtual, spent, slot = snap
        self.pool, self.virtual, self.spent, self.slot = dict(pool), dict(virtual), set(spent), slot


def identity_record_utxo(identity_hash: bytes, agent: bytes, controller: Key, *, not_before: int,
                         subagent_of: bytes = None) -> UTxO:
    """An authentic identity record (Root unless ``subagent_of``) with the
    Accountable party ControllerSet from ``not_before``."""
    party = vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, not_before, None, 3, None)
    lineage = vi.Lineage() if subagent_of is None else vi.Lineage(subagent_of, 0, 1)
    datum = vi.IdentityDatum(
        controller=bytes(controller.vkey.payload), keys=(), parties=(party,), guardian_threshold=0,
        pending_recovery=None, lineage=lineage, subagent_epoch=0, name=b"test juror", description=b"",
        framework=b"Vector-Agent", endpoint=b"", capabilities=(), created_at=not_before, status=vi.Status.active(),
    )
    output = TransactionOutput(script_address(identity_hash), Value(10 * AP3X, tokens(identity_hash, agent)),
                               datum=RawCBOR(vi.identity_datum_cbor(datum)))
    return UTxO(txin(b"identity record:" + agent), output)


def challenge_utxo(params: T.JuryParams, *, tag: bytes, auditor: bytes, claim_token: bytes, challenged_at: int,
                   resolution_deadline: int = 259_200_000, state: int = 0, evidence_uri: bytes = None,
                   auditor_key: Key = None) -> UTxO:
    datum = T.ChallengeDatum(
        claim_ref=(h32(tag + b" claim ref"), 0), auditor_did=auditor,
        auditor_credential=T.M1Credential(False, (auditor_key or Key(b"auditor")).hash),
        stake_amount=60 * AP3X, evidence_hash=h32(tag + b" evidence"),
        evidence_uri=evidence_uri if evidence_uri is not None else b"vector-tx:" + b"A" * 43,
        challenged_at=challenged_at, resolution_deadline=resolution_deadline, eligible_jurors=(),
        state=state, claim_token=claim_token,
    )
    token = rules.token_name(b"chl_", (h32(tag + b" challenge seed"), 0))
    output = TransactionOutput(script_address(params.m1_challenge), Value(60 * AP3X, tokens(params.m1_challenge, token)),
                               datum=RawCBOR(datum.cbor()))
    return UTxO(txin(tag + b" challenge"), output)


def claim_utxo(params: T.JuryParams, *, tag: bytes, claimer: bytes, token: bytes, state: int = 1,
               storage_uri: bytes = None) -> UTxO:
    datum = T.ClaimDatum(
        claimer_did=claimer, claimer_credential=T.M1Credential(False, Key(b"claimer").hash),
        claim_hash=h32(tag + b" claim doc"), claim_type=b"service-quality",
        storage_uri=storage_uri if storage_uri is not None else b"https://example.org/" + b"c" * 80,
        stake_amount=50 * AP3X, submitted_at=ZERO_TIME_MS, challenge_window=3_600_000, state=state,
    )
    output = TransactionOutput(script_address(params.m1_claim), Value(50 * AP3X, tokens(params.m1_claim, token)),
                               datum=RawCBOR(datum.cbor()))
    return UTxO(txin(tag + b" claim"), output)


@pytest.fixture(scope="session")
def applied():
    """The jury validator applied with TEST_PARAMS_JSON (aiken blueprint apply)."""
    if not sim.aiken_available():
        pytest.skip("aiken is not on PATH")
    if not BLUEPRINT.exists():
        pytest.skip(f"{BLUEPRINT} does not exist yet (the contract is not built)")
    params = params_from_json(TEST_PARAMS_JSON)
    return sim.apply_validator(BLUEPRINT, "jury", "jury", encode_params(params).hex())


def make_deployment(applied, params_json=None) -> Deployment:
    params = params_from_json(params_json or TEST_PARAMS_JSON)
    script_hash = bytes.fromhex(applied.hash)
    return Deployment(
        network="mainnet", params=params, params_cbor=encode_params(params), script_hash=script_hash,
        address=script_address(script_hash), reference_script=txin(b"jury reference script"), plutus=None,
        txs={}, time="2026-11-09T00:00:00Z", test=True,
    )


#: The tip at the start of a test world (2026-11-09).
T0 = 38_000_000


class World:
    """A fake chain with the applied jury validator, a funded fee payer, test
    jurors with identity records and the two parties of a challenge."""

    def __init__(self, applied):
        self.deployment = make_deployment(applied)
        self.params = self.deployment.params
        self.chain = FakeChain(T0)
        self.payer = Key(b"payer")
        self.claimer = h32(b"claimer did")
        self.auditor = h32(b"auditor did")
        self.chain.add(UTxO(self.deployment.reference_script, TransactionOutput(
            Address.from_primitive(REFERENCE_ADDRESS), Value(60 * AP3X), script=PlutusV3Script(bytes.fromhex(applied.compiled_code)))))
        for i in range(12):
            self.chain.add(UTxO(txin(b"payer funds", i), TransactionOutput(self.payer.address, Value(2_000 * AP3X))))
        self.chain.add(UTxO(txin(b"payer collateral"), TransactionOutput(self.payer.address, Value(5 * AP3X))))
        self.not_before = self.chain.slot_to_ms(T0 - 86_400)
        self.owners = {}

    def juror_key(self, name: str) -> Key:
        return Key(b"juror owner " + name.encode())

    def agent(self, name: str) -> bytes:
        return h32(b"juror did " + name.encode())

    def add_identity(self, name: str, **kw) -> UTxO:
        utxo = identity_record_utxo(self.params.identity_hash, self.agent(name), self.juror_key(name),
                                    not_before=self.not_before, **kw)
        self.chain.add(utxo)
        return utxo

    def run(self, plan, *keys):
        """finish the plan with the payer key and ``keys``, then apply it."""
        result = builders.finish(plan, [self.payer.skey, *[k.skey for k in keys]])
        self.chain.apply(result.cbor)
        return result

    def advance(self, seconds: int) -> None:
        self.chain.slot += seconds

    def now_ms(self) -> int:
        return self.chain.slot_to_ms(self.chain.slot)


@pytest.fixture
def world(applied):
    return World(applied)


@pytest.fixture(autouse=True)
def offline_blockfrost(monkeypatch):
    """The tests never call Blockfrost: the CBOR fallback finds no document."""
    from vector_jury import documents

    def no_document(tx_id, timeout):
        raise documents.DocumentError(f"transaction {tx_id} has no label {documents.LABEL} document (offline test)")

    monkeypatch.setattr(documents, "_blockfrost_document", no_document)
