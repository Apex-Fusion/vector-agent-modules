"""Offline chain fixtures for the client and indexer tests.

A fake chain context (protocol parameters with a test cost model, fixed
execution units, an in-memory ledger), a deploy state of a new deployment,
identity record outputs and Module-3 outputs. No network access; keys are
generated for each test run.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, List, Optional

import cbor2
from pycardano import (
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    ScriptHash,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
    plutus_script_hash,
)
from pycardano.backend.base import GenesisParameters, ProtocolParameters
from pycardano.serialization import IndefiniteList, RawCBOR

from reputation_staking import vector_identity as vi
from reputation_staking.client import SEED_STAKE_ID, ReputationStakingClient
from reputation_staking.constants import SLOT_LENGTH_S
from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.plutus_data import (
    AuditChallengeDatum,
    AuditResolved,
    EndorsementDatum,
    EndorsementValidatorDatumChallenge,
    EndorsementValidatorDatumEndorsement,
    NoStakeCredential,
    OutputReference,
    PlutusAddress,
    ProtocolParams,
    RepChallengeStateOpen,
    ReputationChallengeDatum,
    StakeDatum,
    VerificationKeyCredential,
)
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_stake_token_name,
)
from reputation_staking.utils import script_hash_to_address, slot_to_posix_ms

# ── Chain time ───────────────────────────────────────────────────────────────

TIP_SLOT = 30_000_000
LO = slot_to_posix_ms(TIP_SLOT)
VALIDITY_SLOTS = 900
HI = slot_to_posix_ms(TIP_SLOT + VALIDITY_SLOTS)

AP3X = 1_000_000

# ── Scripts and hashes of the deployment ─────────────────────────────────────

REP_SCRIPT = PlutusV3Script(b"\x01reputation test script")
END_SCRIPT = PlutusV3Script(b"\x02endorsement test script")
REP_HASH = plutus_script_hash(REP_SCRIPT).payload.hex()
END_HASH = plutus_script_hash(END_SCRIPT).payload.hex()
IDENTITY_HASH = "1d" * 28
PARAMS_HOLDER = "a1" * 28
PARAMS_POLICY = "a2" * 28
TREASURY = "a3" * 28
REFS_POLICY = "a4" * 28

REP_REF = "11" * 32 + "#0"
END_REF = "12" * 32 + "#0"
CROSS_REFS = "13" * 32 + "#0"

# The Module-1 deployment that Module-3 names: the claim validator (its
# reference script UTxO is the `claim_ref` of Module-1) and the challenge
# validator (only its address and policy are read).
CLAIM_SCRIPT = PlutusV3Script(b"\x03module-1 claim test script")
CLAIM_HASH = plutus_script_hash(CLAIM_SCRIPT).payload.hex()
CLAIM_REF = "15" * 32 + "#0"
M1_CHALLENGE_HASH = "b2" * 28
M1_DEPLOYER = bytes.fromhex("e1" * 28)


def address(hash_hex: str) -> Address:
    return Address.from_primitive(script_hash_to_address(hash_hex))


def key_address(key_hash: bytes) -> Address:
    return Address(payment_part=VerificationKeyHash(key_hash), network=Network.MAINNET)


def key_hash(skey: PaymentSigningKey) -> bytes:
    return bytes(PaymentVerificationKey.from_signing_key(skey).hash())


def public_key(skey: PaymentSigningKey) -> bytes:
    return bytes(PaymentVerificationKey.from_signing_key(skey).payload)


def deploy_state(oracle_key_hash: bytes, **overrides) -> dict:
    """A deploy state with the keys that the new deploy scripts write."""
    state = {
        "network": "vector-testnet",
        "holder_owner_vkey_hash": oracle_key_hash.hex(),
        "wallet_vkey_hash": oracle_key_hash.hex(),
        "wallet_address": str(key_address(oracle_key_hash)),
        "reputation_validator_hash": REP_HASH,
        "endorsement_validator_hash": END_HASH,
        "reputation_address": script_hash_to_address(REP_HASH),
        "endorsement_address": script_hash_to_address(END_HASH),
        "refs_token_policy": REFS_POLICY,
        "params_holder_hash": PARAMS_HOLDER,
        "params_holder_address": script_hash_to_address(PARAMS_HOLDER),
        "treasury_holder_hash": TREASURY,
        "treasury_holder_address": script_hash_to_address(TREASURY),
        "record_token_seed": "ee" * 32 + "#1",
        "params_token_policy": PARAMS_POLICY,
        "genesis_minting_window": LO + 2_419_200_000,
        "module1_claim_validator_hash": CLAIM_HASH,
        "module1_challenge_validator_hash": M1_CHALLENGE_HASH,
        "module1_jury_pool_hash": "b3" * 28,
        "module1_refs_token_policy": "b4" * 28,
        "identity_hash": IDENTITY_HASH,
        "ap3x_policy_id": "",
        "ap3x_asset_name": "",
        "validators": {},
        "tx_hashes": {
            "params_utxo": "14" * 32,
            "reputation_spend_ref": REP_REF.split("#")[0],
            "endorsement_spend_ref": END_REF.split("#")[0],
            "cross_refs_nft": CROSS_REFS.split("#")[0],
        },
    }
    state.update(overrides)
    return state


# ── Fake chain context ───────────────────────────────────────────────────────


class AnyUnits(dict):
    """Execution units for every redeemer (no script runs offline)."""

    def __contains__(self, key):
        return True

    def __missing__(self, key):
        return ExecutionUnits(1_000_000, 300_000_000)


class FakeContext:
    """The parts of a pycardano chain context that TransactionBuilder uses.
    The cost model is a test value; a real client takes the protocol
    parameters from the chain (OgmiosHttpContext)."""

    def __init__(self, tip: int = TIP_SLOT):
        self.tip = tip
        self.ledger: Dict[str, List[UTxO]] = {}
        self.refs: Dict[str, UTxO] = {}

    def add(self, utxo: UTxO) -> UTxO:
        self.ledger.setdefault(str(utxo.output.address), []).append(utxo)
        self.refs[ref(utxo)] = utxo
        return utxo

    def remove(self, utxo: UTxO):
        self.ledger[str(utxo.output.address)].remove(utxo)
        del self.refs[ref(utxo)]

    @property
    def network(self):
        return Network.MAINNET

    @property
    def protocol_param(self):
        return ProtocolParameters(
            min_fee_constant=155381, min_fee_coefficient=44, max_block_size=90112,
            max_tx_size=16384, max_block_header_size=1100, key_deposit=2_000_000,
            pool_deposit=500_000_000, pool_influence=0.3, monetary_expansion=0.003,
            treasury_expansion=0.2, decentralization_param=0, extra_entropy="",
            protocol_major_version=10, protocol_minor_version=0, min_utxo=1_000_000,
            min_pool_cost=170_000_000, price_mem=0.0577, price_step=0.0000721,
            max_tx_ex_mem=14_000_000, max_tx_ex_steps=10_000_000_000,
            max_block_ex_mem=62_000_000, max_block_ex_steps=20_000_000_000,
            max_val_size=5000, collateral_percent=150, max_collateral_inputs=3,
            coins_per_utxo_word=0, coins_per_utxo_byte=4310,
            cost_models={"PlutusV3": {str(i): 1 for i in range(297)}},
            maximum_reference_scripts_size=None, min_fee_reference_scripts=None,
        )

    @property
    def genesis_param(self):
        return GenesisParameters(
            active_slots_coefficient=0.05, update_quorum=5,
            max_lovelace_supply=45_000_000_000_000_000, network_magic=764824073,
            epoch_length=432000, system_start=1752057484, slots_per_kes_period=129600,
            slot_length=1, max_kes_evolutions=62, security_param=2160,
        )

    @property
    def last_block_slot(self):
        return self.tip

    def utxos(self, address) -> List[UTxO]:
        return list(self.ledger.get(str(address), []))

    def evaluate_tx(self, tx):
        return AnyUnits()


class OfflineClient(ReputationStakingClient):
    """The client with UTxO resolution from the fake ledger."""

    def _resolve_utxo(self, utxo_ref: str) -> UTxO:
        if utxo_ref not in self.context.refs:
            raise RuntimeError(f"UTxO {utxo_ref} not found on chain")
        return self.context.refs[utxo_ref]


# ── UTxOs ────────────────────────────────────────────────────────────────────

_counter = [0]


def _next_input(tx_hex: Optional[str] = None, index: int = 0) -> TransactionInput:
    if tx_hex is None:
        _counter[0] += 1
        tx_hex = f"{_counter[0]:064x}"
    return TransactionInput.from_primitive([bytes.fromhex(tx_hex), index])


def ref(utxo: UTxO) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def utxo_at(address_: Address, value: Value, datum=None, script=None,
            tx_hex: Optional[str] = None, index: int = 0) -> UTxO:
    return UTxO(_next_input(tx_hex, index), TransactionOutput(address_, value, datum=datum, script=script))


def tokens(policy_hex: str, name: bytes, quantity: int = 1) -> MultiAsset:
    return MultiAsset({ScriptHash(bytes.fromhex(policy_hex)): Asset({AssetName(name): quantity})})


def identity_record(
    controller: bytes,
    capabilities: Iterable[str] = ("code_review", "testing"),
    created_at: int = LO - 10 * vi.DAY_MS,
    **changes,
) -> vi.IdentityDatum:
    """An Active root record with an Accountable Party (the controller set)
    that covers every window."""
    datum = vi.IdentityDatum(
        controller=controller,
        keys=(),
        parties=(
            vi.Party(vi.Role.ACCOUNTABLE, vi.Signer.controller_set(), None, 0, None, 3, None),
        ),
        guardian_threshold=0,
        pending_recovery=None,
        lineage=vi.Lineage(),
        subagent_epoch=0,
        name=b"agent",
        description=b"",
        framework=b"",
        endpoint=b"",
        capabilities=tuple(c.encode() for c in capabilities),
        created_at=created_at,
        status=vi.Status.active(),
    )
    return dataclasses.replace(datum, **changes)


def record_utxo(agent_id: bytes, datum: vi.IdentityDatum) -> UTxO:
    """An authentic record output at the identity address."""
    return utxo_at(
        address(IDENTITY_HASH),
        Value(10 * AP3X, tokens(IDENTITY_HASH, agent_id)),
        datum=RawCBOR(vi.identity_datum_cbor(datum)),
    )


def params_record(params: Optional[ProtocolParamsInfo] = None) -> UTxO:
    p = params or ProtocolParamsInfo(genesis_minting_window=LO + 2_419_200_000)
    datum = ProtocolParams(
        **{
            f.name: (
                IndefiniteList([c.encode() for c in p.valid_capabilities])
                if f.name == "valid_capabilities" else getattr(p, f.name)
            )
            for f in dataclasses.fields(ProtocolParamsInfo)
        }
    )
    return utxo_at(
        address(PARAMS_HOLDER), Value(3 * AP3X, tokens(PARAMS_POLICY, b"params")),
        datum=RawCBOR(datum.to_cbor()),
    )


def cross_refs_utxo(wallet: Address) -> UTxO:
    datum = cbor2.dumps(cbor2.CBORTag(121, [bytes.fromhex(REP_HASH), bytes.fromhex(END_HASH)]))
    return utxo_at(
        wallet, Value(3 * AP3X, tokens(REFS_POLICY, b"reputation_refs")),
        datum=RawCBOR(datum), tx_hex=CROSS_REFS.split("#")[0],
    )


def new_stake_id() -> OutputReference:
    """A fresh stake_id (the output reference of a seed that nobody spent
    before)."""
    tx_input = _next_input()
    return OutputReference(transaction_id=bytes(tx_input.transaction_id), output_index=0)


def stake_datum_of(utxo: UTxO) -> StakeDatum:
    return StakeDatum.from_cbor(bytes(utxo.output.datum.cbor))


def stake_utxo(agent_id: bytes, owner_key_hash: bytes, amount: int = 100 * AP3X,
               capabilities=("code_review", "testing"), last_updated: int = LO - vi.DAYS_7_MS,
               challenge_locks=(), stake_id: Optional[OutputReference] = None) -> UTxO:
    datum = StakeDatum(
        agent_did=agent_id,
        owner_credential=VerificationKeyCredential(owner_key_hash),
        stake_amount=amount,
        staked_capabilities=IndefiniteList([c.encode() for c in capabilities]),
        last_updated=last_updated,
        history_points=0,
        challenge_locks=IndefiniteList(list(challenge_locks)),
        stake_id=stake_id if stake_id is not None else new_stake_id(),
    )
    token = bytes.fromhex(derive_stake_token_name(agent_id.hex()))
    # A stake output holds at least the minimum lovelace of the ledger.
    coin = max(amount, 2 * AP3X)
    return utxo_at(address(REP_HASH), Value(coin, tokens(REP_HASH, token)), datum=RawCBOR(datum.to_cbor()))


def endorsement_utxo(endorser_id: bytes, target_id: bytes, endorser_key_hash: bytes,
                     amount: int = 20 * AP3X, capabilities=("code_review",),
                     created_at: int = LO - vi.DAY_MS, slashed_by=(),
                     target_stake: Optional[OutputReference] = None) -> UTxO:
    """An endorsement of `target_id`, bound to the stake whose stake_id is
    `target_stake` (a stake that nobody holds when None)."""
    datum = EndorsementValidatorDatumEndorsement(
        datum=EndorsementDatum(
            endorser_did=endorser_id,
            endorser_credential=VerificationKeyCredential(endorser_key_hash),
            target_did=target_id,
            stake_amount=amount,
            endorsed_capabilities=IndefiniteList([c.encode() for c in capabilities]),
            created_at=created_at,
            slashed_by=IndefiniteList(list(slashed_by)),
            target_stake=target_stake if target_stake is not None else new_stake_id(),
        )
    )
    token = bytes.fromhex(derive_endorsement_token_name(endorser_id.hex(), target_id.hex()))
    return utxo_at(address(END_HASH), Value(amount, tokens(END_HASH, token)), datum=RawCBOR(datum.to_cbor()))


def challenge_token(challenger_id: bytes, target_id: bytes, capability: str = "code_review") -> bytes:
    """The challenge token name (also the name of its stake lock)."""
    return bytes.fromhex(derive_challenge_token_name(challenger_id.hex(), target_id.hex(), capability))


def challenge_utxo(challenger_id: bytes, target_id: bytes, challenger_key_hash: bytes,
                   target_key_hash: bytes, state=None, amount: int = 25 * AP3X,
                   capability: str = "code_review", created_at: int = LO - vi.DAY_MS,
                   counter_evidence_hash: bytes = b"", response_submitted_at: int = 0) -> UTxO:
    datum = EndorsementValidatorDatumChallenge(
        datum=ReputationChallengeDatum(
            challenger_did=challenger_id,
            challenger_credential=VerificationKeyCredential(challenger_key_hash),
            target_did=target_id,
            target_credential=VerificationKeyCredential(target_key_hash),
            challenged_capability=capability.encode(),
            stake_amount=amount,
            evidence_hash=b"\x0e" * 32,
            evidence_uri=b"ipfs://evidence",
            created_at=created_at,
            counter_evidence_hash=counter_evidence_hash,
            counter_evidence_uri=b"ipfs://counter-evidence" if response_submitted_at else b"",
            response_submitted_at=response_submitted_at,
            state=state or RepChallengeStateOpen(),
        )
    )
    token = bytes.fromhex(
        derive_challenge_token_name(challenger_id.hex(), target_id.hex(), capability)
    )
    return utxo_at(address(END_HASH), Value(amount, tokens(END_HASH, token)), datum=RawCBOR(datum.to_cbor()))


def audit_resolved(verdict) -> AuditResolved:
    """The Module-1 state AuditResolved { verdict, refund_to }."""
    return AuditResolved(
        verdict=verdict,
        refund_to=PlutusAddress(VerificationKeyCredential(b"\x24" * 28), NoStakeCredential()),
    )


def audit_challenge_utxo(claim_token: bytes, auditor_id: bytes, state,
                         token_name: bytes = b"chl_" + b"\x01" * 28,
                         address_hash: Optional[str] = None) -> UTxO:
    """A Module-1 challenge of the claim with the token `claim_token` at the
    Module-1 challenge address, with one Module-1 challenge token."""
    policy = M1_CHALLENGE_HASH
    datum = AuditChallengeDatum(
        claim_ref=OutputReference(transaction_id=b"\x21" * 32, output_index=0),
        auditor_did=auditor_id,
        auditor_credential=VerificationKeyCredential(b"\x22" * 28),
        stake_amount=25 * AP3X,
        evidence_hash=b"\x23" * 32,
        evidence_uri=b"ipfs://audit-evidence",
        challenged_at=LO - vi.DAY_MS,
        resolution_deadline=5_400_000,
        eligible_jurors=IndefiniteList([]),
        state=state,
        claim_token=claim_token,
    )
    return utxo_at(
        address(address_hash or policy), Value(25 * AP3X, tokens(policy, token_name)),
        datum=RawCBOR(datum.to_cbor()),
    )


def deployment_chain(wallet_skey: PaymentSigningKey, oracle_key_hash: Optional[bytes] = None):
    """A fake chain with the deployment UTxOs, the reference script of the
    Module-1 claim validator (at the Module-1 deployer address) and two
    wallet UTxOs. Returns (context, deploy state)."""
    context = FakeContext()
    wallet = key_address(key_hash(wallet_skey))
    context.add(utxo_at(wallet, Value(20 * AP3X), script=REP_SCRIPT, tx_hex=REP_REF.split("#")[0]))
    context.add(utxo_at(wallet, Value(20 * AP3X), script=END_SCRIPT, tx_hex=END_REF.split("#")[0]))
    context.add(cross_refs_utxo(wallet))
    context.add(params_record())
    context.add(utxo_at(
        key_address(M1_DEPLOYER), Value(30 * AP3X), script=CLAIM_SCRIPT,
        tx_hex=CLAIM_REF.split("#")[0],
    ))
    context.add(utxo_at(wallet, Value(5_000 * AP3X)))
    context.add(utxo_at(wallet, Value(10 * AP3X)))
    return context, deploy_state(oracle_key_hash or key_hash(wallet_skey))


# ── Transaction readers ──────────────────────────────────────────────────────


def refs_of(tx) -> set:
    return {
        f"{bytes(i.transaction_id).hex()}#{i.index}"
        for i in tx.transaction_body.reference_inputs or []
    }


def inputs_of(tx) -> set:
    return {f"{bytes(i.transaction_id).hex()}#{i.index}" for i in tx.transaction_body.inputs}


def signers_of(tx) -> set:
    return {bytes(s) for s in tx.transaction_body.required_signers or []}


def witness_keys(tx) -> set:
    return {
        bytes(w.vkey.hash()) for w in tx.transaction_witness_set.vkey_witnesses or []
    }


def redeemers_of(tx) -> list:
    """[(tag, redeemer data)] of the transaction."""
    r = tx.transaction_witness_set.redeemer
    if hasattr(r, "items"):
        return [(k.tag.name.lower(), v.data) for k, v in r.items()]
    return [(x.tag.name.lower(), x.data) for x in r]


def mint_of(tx) -> dict:
    """{(policy hex, asset name bytes): quantity}"""
    return {
        (bytes(policy.payload).hex(), bytes(name.payload)): quantity
        for policy, assets in (tx.transaction_body.mint or {}).items()
        for name, quantity in assets.items()
    }


def outputs_to(tx, address_) -> list:
    return [o for o in tx.transaction_body.outputs if str(o.address) == str(address_)]


def assert_window(tx):
    body = tx.transaction_body
    assert body.validity_start == TIP_SLOT
    assert body.ttl == TIP_SLOT + VALIDITY_SLOTS
    assert (body.ttl - body.validity_start) * SLOT_LENGTH_S * 1000 <= vi.MAX_RANGE_MS


# ── The chain of a test ──────────────────────────────────────────────────────


class World:
    """A deployment, a wallet (also the oracle key) and three agents:
    A (staker, target), B (endorser, challenger) and P (parent of a
    subagent). Each agent has its own controller key."""

    def __init__(self, wallet: Optional[PaymentSigningKey] = None):
        self.wallet = wallet or PaymentSigningKey.generate()
        self.context, self.state = deployment_chain(self.wallet)
        self.ctrl = {name: PaymentSigningKey.generate() for name in ("A", "B", "P")}
        self.ids = {
            "A": vi.identifier_from_seed(bytes.fromhex("0a" * 32), 0),
            "B": vi.identifier_from_seed(bytes.fromhex("0b" * 32), 0),
            "P": vi.identifier_from_seed(bytes.fromhex("0c" * 32), 0),
        }
        self.records = {}
        # owner_credential of A's stakes: a key that A's record lists.
        self.owner_a_key = PaymentSigningKey.generate()
        self.owner_a = key_hash(self.owner_a_key)

    def did(self, name: str) -> str:
        return vi.did_from_id(self.ids[name])

    def record(self, name: str, **changes):
        """Put the record of an agent at the identity address. The record
        lists the wallet key with the transaction purpose (unless `keys` is
        given), so the wallet key is a declared credential of the record.
        A's record also lists the key of owner_a (the owner_credential of
        A's stakes)."""
        if name in self.records:
            self.context.remove(self.records[name])
        keys = [vi.Key(vi.KeyType.ED25519, public_key(self.wallet), vi.PURPOSE_TRANSACTION)]
        if name == "A":
            keys.append(vi.Key(vi.KeyType.ED25519, public_key(self.owner_a_key), vi.PURPOSE_TRANSACTION))
        changes.setdefault("keys", tuple(keys))
        datum = identity_record(public_key(self.ctrl[name]), **changes)
        self.records[name] = self.context.add(record_utxo(self.ids[name], datum))
        return self.records[name]

    def lock_b(self, capability: str = "code_review") -> bytes:
        """The lock (challenge token name) of B's challenge against A."""
        return challenge_token(self.ids["B"], self.ids["A"], capability)

    def client(self, wallet=None, **kwargs) -> OfflineClient:
        return OfflineClient(self.context, wallet or self.wallet, self.state, **kwargs)

    def deployment_refs(self) -> set:
        """The ProtocolParams record and CrossValidatorRefs references."""
        [params] = self.context.utxos(address(PARAMS_HOLDER))
        return {ref(params), CROSS_REFS}

    def stake_a(self, **kwargs):
        return self.context.add(stake_utxo(self.ids["A"], self.owner_a, **kwargs))

    def seed(self, name: str):
        datum = StakeDatum(
            agent_did=self.ids[name],
            owner_credential=VerificationKeyCredential(key_hash(self.wallet)),
            stake_amount=2 * AP3X,
            staked_capabilities=IndefiniteList([b"code_review"]),
            last_updated=0,
            history_points=0,
            challenge_locks=IndefiniteList([]),
            stake_id=SEED_STAKE_ID,
        )
        return self.context.add(
            utxo_at(address(REP_HASH), Value(2 * AP3X), datum=RawCBOR(datum.to_cbor()))
        )
