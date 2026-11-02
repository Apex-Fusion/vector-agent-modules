"""Offline fixtures for the Module-6 client tests.

The tests build transactions with pycardano against a fake chain context. No
test reads the network or a key file: every key is generated.
"""

from __future__ import annotations

import dataclasses
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from pycardano import (
    Address,
    ChainContext,
    ExecutionUnits,
    GenesisParameters,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PlutusV3Script,
    ProtocolParameters,
    Transaction,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    plutus_script_hash,
)
from pycardano.serialization import RawCBOR

MODULE_ROOT = Path(__file__).resolve().parent.parent
SHARED_PYTHON = MODULE_ROOT.parent / "shared" / "python"
for path in (MODULE_ROOT / "scripts", MODULE_ROOT / "dashboard", SHARED_PYTHON):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import governance_identity as gi  # noqa: E402
import vector_identity as vid  # noqa: E402

h = bytes.fromhex

IDENTITY_HASH = h("1d" * 28)
RECORD_TOKEN_POLICY = h("7a" * 28)
REFS_TOKEN_POLICY = h("7b" * 28)
PARAMS_HOLDER = h("4a" * 28)
ORACLE_HOLDER = h("4b" * 28)
TREASURY_HOLDER = h("4c" * 28)
CROSS_REFS_TX = h("c5" * 32)

PROPOSER = h("a1" * 32)
CRITIC = h("a2" * 32)
ENDORSER = h("a3" * 32)
PARENT = h("a9" * 32)

#: Vector testnet start (2025-07-09T10:38:04Z) and a tip slot.
SYSTEM_START_MS = 1_752_055_084_000
TIP_SLOT = 40_000_000

#: Fake script bytes for the five applied validators.
SCRIPTS = {
    gi.PROPOSAL_SPEND: PlutusV3Script(h("4e4d010000332222200101")),
    gi.PROPOSAL_MINT: PlutusV3Script(h("4e4d010000332222200102")),
    gi.CRITIQUE_SPEND: PlutusV3Script(h("4e4d010000332222200103")),
    gi.CRITIQUE_MINT: PlutusV3Script(h("4e4d010000332222200104")),
    gi.ENDORSEMENT_SPEND: PlutusV3Script(h("4e4d010000332222200105")),
}


def script_hash(title: str) -> bytes:
    return bytes(plutus_script_hash(SCRIPTS[title]).payload)


def params_value() -> gi.GovernanceParams:
    """The default parameters of params.ak (times in POSIX ms)."""
    return gi.GovernanceParams(
        min_proposal_stake=25_000_000,
        min_critique_stake=5_000_000,
        min_governance_endorsement=5_000_000,
        min_review_window=259_200_000,
        max_review_window=2_419_200_000,
        max_amendments=5,
        max_active_proposals=3,
        proposal_cooldown=86_400_000,
        proposer_reward_share=7_000,
        critic_reward_share=2_000,
        protocol_fee_rate=1_000,
        min_adoption_reward=50_000_000,
        max_adoption_reward=500_000_000,
        max_incorporated_critiques=5,
        max_treasury_request=10_000_000_000,
        emergency_stake_multiplier=5_000,
        emergency_review_window=43_200_000,
        param_execution_delay=604_800_000,
        min_prediction_pool=100_000_000,
        credibility_pool_low_threshold=500_000_000,
        credibility_pool_critical_threshold=200_000_000,
    )


def deploy_state(**changes) -> dict:
    """A deploy state as scripts/deploy.py writes it."""
    state = {
        "network": "vector-testnet",
        "identity_hash": IDENTITY_HASH.hex(),
        "record_token_policy": RECORD_TOKEN_POLICY.hex(),
        "refs_token_policy": REFS_TOKEN_POLICY.hex(),
        "holders": {
            name: {"hash": value.hex(), "address": str(gi.script_address(value))}
            for name, value in (("params", PARAMS_HOLDER), ("oracle", ORACLE_HOLDER), ("treasury", TREASURY_HOLDER))
        },
        "validators": {
            title: {"hash": script_hash(title).hex(), "compiled_code": bytes(script).hex()}
            for title, script in SCRIPTS.items()
        },
        "proposal_validator_hash": script_hash(gi.PROPOSAL_SPEND).hex(),
        "critique_validator_hash": script_hash(gi.CRITIQUE_SPEND).hex(),
        "cross_refs_address": str(gi.script_address(ORACLE_HOLDER)),
        "tx_hashes": {"params_utxo": "aa" * 32, "oracle_utxo": "aa" * 32, "cross_refs_nft": CROSS_REFS_TX.hex()},
    }
    state.update(changes)
    return state


def protocol_parameters() -> ProtocolParameters:
    """Protocol parameters for offline builds only (fake PlutusV3 cost model)."""
    return ProtocolParameters(
        min_fee_constant=155_381,
        min_fee_coefficient=44,
        max_block_size=90_112,
        max_tx_size=16_384,
        max_block_header_size=1_100,
        key_deposit=2_000_000,
        pool_deposit=500_000_000,
        pool_influence=Fraction(3, 10),
        monetary_expansion=Fraction(3, 1000),
        treasury_expansion=Fraction(1, 5),
        decentralization_param=Fraction(0),
        extra_entropy="",
        protocol_major_version=10,
        protocol_minor_version=0,
        min_utxo=0,
        min_pool_cost=170_000_000,
        price_mem=Fraction(577, 10_000),
        price_step=Fraction(721, 10_000_000),
        max_tx_ex_mem=14_000_000,
        max_tx_ex_steps=10_000_000_000,
        max_block_ex_mem=62_000_000,
        max_block_ex_steps=20_000_000_000,
        max_val_size=5_000,
        collateral_percent=150,
        max_collateral_inputs=3,
        coins_per_utxo_word=0,
        coins_per_utxo_byte=4_310,
        cost_models={"PlutusV3": {str(i): 0 for i in range(297)}},
        maximum_reference_scripts_size={"bytes": 204_800},
        min_fee_reference_scripts={"base": 15.0, "range": 25_600, "multiplier": 1.2},
    )


class FakeChain(ChainContext):
    """A chain context in memory: UTxOs by address, fixed tip, fixed
    execution units. ``async_submit_tx_cbor`` applies a transaction to the
    UTxO set, so the flows can run offline."""

    def __init__(self, tip_slot: int = TIP_SLOT):
        self.tip_slot = tip_slot
        self.store: dict[str, list[UTxO]] = {}
        self.evaluated: list[Transaction] = []
        self.submitted: list[Transaction] = []

    def add(self, *utxos: UTxO) -> None:
        for utxo in utxos:
            self.store.setdefault(str(utxo.output.address), []).append(utxo)

    @property
    def protocol_param(self) -> ProtocolParameters:
        return protocol_parameters()

    @property
    def genesis_param(self) -> GenesisParameters:
        return GenesisParameters(
            active_slots_coefficient=Fraction(1, 20),
            update_quorum=5,
            max_lovelace_supply=45_000_000_000_000_000,
            network_magic=764_824_073,
            epoch_length=86_400,
            system_start=SYSTEM_START_MS // 1000,
            slots_per_kes_period=129_600,
            slot_length=1,
            max_kes_evolutions=62,
            security_param=2_160,
        )

    @property
    def network(self) -> Network:
        return Network.MAINNET

    @property
    def epoch(self) -> int:
        return 400

    @property
    def last_block_slot(self) -> int:
        return self.tip_slot

    def utxos(self, address) -> list[UTxO]:
        return list(self.store.get(str(address), []))

    def evaluate_tx(self, tx: Transaction) -> dict:
        self.evaluated.append(tx)
        return {
            f"{key.tag.name.lower()}:{key.index}": ExecutionUnits(1_000_000, 400_000_000)
            for key in tx.transaction_witness_set.redeemer.keys()
        }

    async def async_utxos(self, address) -> list[UTxO]:
        return self.utxos(address)

    async def async_last_block_slot(self) -> int:
        return self.tip_slot

    async def async_genesis_param(self) -> GenesisParameters:
        return self.genesis_param

    async def async_submit_tx_cbor(self, cbor) -> str:
        tx = Transaction.from_cbor(cbor)
        self.submitted.append(tx)
        spent = set(tx.transaction_body.inputs)
        for address, utxos in self.store.items():
            self.store[address] = [u for u in utxos if u.input not in spent]
        for index, output in enumerate(tx.transaction_body.outputs):
            self.add(UTxO(TransactionInput(tx.id, index), output))
        return str(tx.id)


def tx_in(byte: int, index: int = 0) -> TransactionInput:
    return TransactionInput(TransactionId(bytes([byte]) * 32), index)


def key_pair() -> gi.SigningKeyPair:
    return gi.SigningKeyPair.from_signing_key(PaymentSigningKey.generate())


def public_key(pair: gi.SigningKeyPair) -> bytes:
    return bytes(pair.verification_key.payload)


ACCOUNTABLE = vid.Party(vid.Role.ACCOUNTABLE, vid.Signer.controller_set(), None, 0, None, 3, None)


def record_datum(controller: bytes, **changes) -> vid.IdentityDatum:
    """An Active root record with an Accountable Party."""
    base = vid.IdentityDatum(
        controller=controller,
        keys=(),
        parties=(ACCOUNTABLE,),
        guardian_threshold=0,
        pending_recovery=None,
        lineage=vid.Lineage(),
        subagent_epoch=0,
        name=b"agent",
        description=b"",
        framework=b"",
        endpoint=b"https://agent.example",
        capabilities=(b"governance",),
        created_at=SYSTEM_START_MS,
        status=vid.Status.active(),
    )
    return dataclasses.replace(base, **changes)


def record_utxo(agent_id: bytes, datum: vid.IdentityDatum, byte: int) -> UTxO:
    """An authentic record output at the identity address."""
    value = Value(5_000_000, MultiAsset.from_primitive({IDENTITY_HASH: {agent_id: 1}}))
    output = TransactionOutput(gi.identity_address(IDENTITY_HASH), value, datum=RawCBOR(vid.identity_datum_cbor(datum)))
    return UTxO(tx_in(byte), output)


def record(agent_id: bytes, datum: vid.IdentityDatum, byte: int) -> vid.IdentityRecord:
    utxo = record_utxo(agent_id, datum, byte)
    return vid.IdentityRecord(agent_id, datum, utxo)


def suspended(datum: vid.IdentityDatum) -> vid.IdentityDatum:
    hold = vid.Hold(vid.Signer.controller_set(), 6, SYSTEM_START_MS, vid.MAX_TIME_MS)
    return dataclasses.replace(datum, status=vid.Status.suspended([hold]))


def revoked(datum: vid.IdentityDatum) -> vid.IdentityDatum:
    return dataclasses.replace(datum, status=vid.Status.revoked(vid.Signer.controller_set(), 1, SYSTEM_START_MS))


def with_transaction_key(datum: vid.IdentityDatum, key: bytes) -> vid.IdentityDatum:
    return dataclasses.replace(datum, keys=(vid.Key(vid.KeyType.ED25519, key, vid.PURPOSE_TRANSACTION),))


def subagent_of(datum: vid.IdentityDatum, parent: bytes) -> vid.IdentityDatum:
    return dataclasses.replace(datum, lineage=vid.Lineage(parent, 0, 1))


@dataclasses.dataclass
class World:
    """A deployment, a wallet and the outputs that the builders read."""

    chain: FakeChain
    deployment: gi.GovernanceDeployment
    wallet: gi.SigningKeyPair
    wallet_address: Address
    clock: gi.SlotClock
    params: UTxO
    cross_refs: UTxO
    oracle: UTxO

    @property
    def refs(self) -> gi.GovernanceReferences:
        return gi.GovernanceReferences(
            gi.ParamsRecord(self.params, gi.GovernanceParams.from_data(self.params.output.datum)),
            gi.CrossRefsRecord(self.cross_refs, gi.CrossRefs.from_data(self.cross_refs.output.datum)),
        )

    @property
    def key_hash(self) -> bytes:
        return self.wallet.key_hash

    def window(self, length_ms: int = gi.DEFAULT_RANGE_MS) -> gi.TxWindow:
        return gi.window_from_tip(self.clock, self.chain.tip_slot, length_ms)

    def agent(self, agent_id: bytes, datum: vid.IdentityDatum | None = None, byte: int = 0x60) -> vid.IdentityRecord:
        """The record of an agent whose controller is the wallet key; also on
        the fake chain."""
        datum = record_datum(public_key(self.wallet)) if datum is None else datum
        rec = record(agent_id, datum, byte)
        self.chain.add(rec.utxo)
        return rec

    def proposal_datum(self, proposer: bytes = PROPOSER, **changes) -> gi.ProposalDatum:
        datum = gi.ProposalDatum(
            proposer_did=proposer,
            proposer_credential=gi.Credential.key(self.key_hash),
            proposal_hash=vid.blake2b_256(b"proposal v1"),
            proposal_type=gi.general_suggestion(),
            storage_uri=b"ipfs://QmProposal",
            stake_amount=25_000_000,
            submitted_at=SYSTEM_START_MS,
            review_window=604_800_000,
        )
        return dataclasses.replace(datum, **changes)

    def proposal_utxo(self, datum: gi.ProposalDatum, byte: int = 0x70, token: bool = True,
                      seed: TransactionInput | None = None) -> UTxO:
        """A submitted proposal: at the proposal validator with the token of
        its seed (``PROPOSAL_SEED`` by default)."""
        name = gi.proposal_token_name(PROPOSAL_SEED if seed is None else seed)
        assets = {self.deployment.proposal_mint.hash: {name: 1}} if token else {}
        value = Value(datum.stake_amount + gi.POSITION_DEPOSIT, MultiAsset.from_primitive(assets))
        utxo = UTxO(tx_in(byte), TransactionOutput(
            self.deployment.proposal_spend.address, value, datum=gi.inline_datum(datum.to_data())
        ))
        self.chain.add(utxo)
        return utxo

    def activity_utxo(self, agent_id: bytes, count: int, last: int, byte: int = 0x80,
                      next_did: bytes = b"", key_hash: bytes | None = None) -> gi.ActivityRecord:
        """The node of ``agent_id`` in the activity list."""
        credential = gi.Credential.key(self.key_hash if key_hash is None else key_hash)
        datum = gi.ActivityDatum(agent_id, credential, count, last, next_did)
        assets = {self.deployment.proposal_mint.hash: {gi.activity_token_name(agent_id): 1}}
        utxo = UTxO(tx_in(byte), TransactionOutput(
            self.deployment.proposal_spend.address,
            Value(gi.ACTIVITY_LOVELACE, MultiAsset.from_primitive(assets)),
            datum=gi.inline_datum(datum.to_data()),
        ))
        self.chain.add(utxo)
        return gi.ActivityRecord(utxo, datum)

    def head(self, next_did: bytes = b"") -> gi.ActivityRecord:
        """Replace the head of the activity list (as deploy.py creates it)
        with a head whose next_did is ``next_did``."""
        address = str(self.deployment.proposal_spend.address)
        self.chain.store[address] = [
            u for u in self.chain.store.get(address, []) if u.input != HEAD_REF
        ]
        datum = gi.ActivityDatum(b"", gi.Credential.key(self.key_hash), 0, 0, next_did)
        utxo = UTxO(HEAD_REF, TransactionOutput(
            self.deployment.proposal_spend.address,
            Value(3_000_000, MultiAsset.from_primitive({RECORD_TOKEN_POLICY: {gi.ACTIVITY_TOKEN_NAME: 1}})),
            datum=gi.inline_datum(datum.to_data()),
        ))
        self.chain.add(utxo)
        return gi.ActivityRecord(utxo, datum)

    def activity_list(self) -> gi.ActivityList:
        return gi.find_activity_list(self.chain.utxos(self.deployment.proposal_spend.address), self.deployment)

    def locked(self, script: gi.DeployedScript, datum, lovelace: int, byte: int = 0x90) -> UTxO:
        """A lock output as the lock builders make it."""
        utxo = UTxO(tx_in(byte), TransactionOutput(script.address, Value(lovelace), datum=gi.inline_datum(datum.to_data())))
        self.chain.add(utxo)
        return utxo

    def critique_utxo(self, datum: gi.CritiqueDatum, byte: int = 0xA0) -> UTxO:
        assets = {self.deployment.critique_mint.hash: {gi.critique_token_name(tx_in(byte, 1)): 1}}
        utxo = UTxO(tx_in(byte), TransactionOutput(
            self.deployment.critique_spend.address,
            Value(datum.stake_amount + gi.POSITION_DEPOSIT, MultiAsset.from_primitive(assets)),
            datum=gi.inline_datum(datum.to_data()),
        ))
        self.chain.add(utxo)
        return utxo


def make_world(state: dict | None = None) -> World:
    chain = FakeChain()
    deployment = gi.GovernanceDeployment.from_state(deploy_state() if state is None else state)
    wallet = key_pair()
    wallet_address = Address(wallet.verification_key.hash(), network=Network.MAINNET)
    chain.add(*(UTxO(tx_in(0x10 + i), TransactionOutput(wallet_address, 200_000_000)) for i in range(3)))
    params = UTxO(tx_in(0x20), TransactionOutput(
        deployment.params_address,
        Value(3_000_000, MultiAsset.from_primitive({RECORD_TOKEN_POLICY: {b"params": 1}})),
        datum=gi.inline_datum(params_value().to_data()),
    ))
    oracle = UTxO(tx_in(0x21, 1), TransactionOutput(
        deployment.oracle_address,
        Value(3_000_000, MultiAsset.from_primitive({RECORD_TOKEN_POLICY: {b"oracle": 1}})),
        datum=gi.inline_datum(gi._constr(0, [gi._constr(0, [wallet.key_hash]), TREASURY_HOLDER, gi._constr(1)])),
    ))
    cross_refs = UTxO(TransactionInput(TransactionId(CROSS_REFS_TX), 0), TransactionOutput(
        deployment.cross_refs_address,
        Value(2_000_000, MultiAsset.from_primitive({REFS_TOKEN_POLICY: {b"crossrefs": 1}})),
        datum=gi.inline_datum(deployment.cross_refs.to_data()),
    ))
    chain.add(params, oracle, cross_refs)
    for script in (deployment.proposal_spend, deployment.proposal_mint, deployment.critique_spend,
                   deployment.critique_mint, deployment.endorsement_spend):
        if script.reference is not None:
            chain.add(script.reference)
    clock = gi.SlotClock.from_genesis(chain.genesis_param)
    world = World(chain, deployment, wallet, wallet_address, clock, params, cross_refs, oracle)
    world.head()  # the head of the empty activity list (deploy.py step 5a)
    return world


#: The seed of the proposals of World.proposal_utxo.
PROPOSAL_SEED = tx_in(0x71)

#: The UTxO of the head of the activity list.
HEAD_REF = tx_in(0x8F)

BLUEPRINT = MODULE_ROOT / "contracts" / "governance-suggestion" / "plutus.json"


def applied_state() -> dict:
    """A deploy state with the validators of the committed blueprint, applied
    with the GovernanceConfig of this module's test deployment (11 fields, as
    scripts/deploy_common.py builds it), and reference-script transactions."""
    from aiken_simulate import apply_validator
    from deploy_common import build_governance_config_cbor

    config = build_governance_config_cbor(
        refs_token_policy=REFS_TOKEN_POLICY.hex(), oracle_holder_hash=ORACLE_HOLDER.hex(),
        params_holder_hash=PARAMS_HOLDER.hex(), treasury_holder_hash=TREASURY_HOLDER.hex(),
        record_token_policy=RECORD_TOKEN_POLICY.hex(), identity_hash=IDENTITY_HASH.hex(),
    )
    validators = {}
    for title in SCRIPTS:
        module, validator, _ = title.split(".")
        applied = apply_validator(BLUEPRINT, module, validator, config)
        validators[title] = {"hash": applied.hash, "compiled_code": applied.compiled_code}
    tx_hashes = {"params_utxo": "aa" * 32, "oracle_utxo": "aa" * 32, "activity_head": "aa" * 32,
                 "cross_refs_nft": CROSS_REFS_TX.hex()}
    tx_hashes.update({key: f"{i:02x}" * 32 for i, key in enumerate(gi.REFERENCE_SCRIPT_KEYS.values(), 0xE1)})
    return deploy_state(
        validators=validators,
        proposal_validator_hash=validators[gi.PROPOSAL_SPEND]["hash"],
        critique_validator_hash=validators[gi.CRITIQUE_SPEND]["hash"],
        tx_hashes=tx_hashes,
    )


@pytest.fixture
def world() -> World:
    return make_world()
