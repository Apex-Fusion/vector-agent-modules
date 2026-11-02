"""
ReputationStakingClient — high-level API for Module 3 operations.

Uses PyCardano TransactionBuilder with Ogmios for remote chain interaction,
matching the patterns used by Module 1 and Module 6.

Usage (Ogmios/remote):
    from reputation_staking import ReputationStakingClient
    from reputation_staking.ogmios_backend import OgmiosHttpContext, load_wallet

    context = OgmiosHttpContext()
    skey, vkey, wallet_addr = load_wallet("wallet/payment.skey")
    client = ReputationStakingClient.from_deploy_state(
        "deploy/deploy_state.json", context, skey,
    )
    seed_tx = client.create_seed_utxo(agent_did, ["code_review"])
    tx = client.create_stake(agent_did, ["code_review"], 10_000_000,
                             seed_utxo=f"{seed_tx}#0")

Agents and identity records (Part 2 §7 of docs/IDENTITY_REGISTRY.md in
agents-marketplace, Module-3 row):

- An agent identifier is the 32-byte identifier of the agent's identity
  record. The methods accept a canonical DID (`did:vector:<64 hex>`) or
  64 hex characters.
- An action that opens or extends a position (CreateStake, an endorsement, a
  challenge, a history bonus) reads the acting agent's record. The
  transaction has the record and the ancestor records
  (`vector_identity.record_chain`) as reference inputs and a validity range
  of at most one hour. created_at is the lower bound `lo` of that range;
  last_updated of a stake is the upper bound `hi` of the range of the last
  owner action. The record must have effective status Active over [lo, hi]
  and list each staked or endorsed capability. The target of an endorsement
  also needs an Active record that lists the capabilities.
- The controller of the record signs: its key hash is a required signer. When
  the controller key is not the wallet key, pass `controller_skey`. The
  wallet key is the payout credential that an opening stores
  (owner_credential, endorser_credential, challenger_credential) and it
  signs. It must be a declared credential of the record: the controller key
  or a listed key with the transaction purpose.
- Challenges lock the target's stake. A challenge reads no target record:
  the target's stake decides (it lists the capability, and its
  owner_credential becomes target_credential). The opening spends the stake
  with LockForChallenge, which adds the challenge token name to
  challenge_locks. DistributeOutcome spends it again: ReleaseChallenge
  removes the lock (CapabilityVerified, Inconclusive), and SlashStake removes
  the lock and slashes the stake (CapabilityFalsified).
- The client checks these rules before it builds a transaction. It raises
  vector_identity.IdentityError (or a subclass) when a rule fails.

Each action has a `build_*` method that returns the signed transaction and a
method that also submits it.
"""

from __future__ import annotations

import dataclasses
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    Redeemer,
    ScriptHash,
    Transaction,
    TransactionBuilder,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
)
from pycardano.serialization import IndefiniteList
from pycardano.utils import min_lovelace_post_alonzo

from reputation_staking import vector_identity as vi
from reputation_staking.constants import (
    DEFAULT_VALIDITY_SLOTS,
    HISTORY_BONUS_OUTPUT_LOVELACE,
    OGMIOS_URL,
    PARAMS_TOKEN_NAME,
    SLOT_LENGTH_S,
    TX_SUBMIT_URL,
)
from reputation_staking.deployment import Deployment, load_deployment
from reputation_staking.models import ProtocolParamsInfo
from reputation_staking.ogmios_backend import (
    NETWORK,
    get_collateral_utxo,
    get_wallet_utxos,
    resolve_utxo,
    submit_tx,
    wait_for_tx,
)
from reputation_staking.plutus_data import (
    BurnChallengeTokenRedeemer,
    BurnEndorsementTokenRedeemer,
    ChallengeSpendRedeemer,
    CreateStakeRedeemer,
    DistributeOutcomeRedeemer,
    EndorsementDatum,
    EndorsementSpendRedeemer,
    EndorsementValidatorDatumChallenge,
    EndorsementValidatorDatumEndorsement,
    HistoryBonusDatum,
    HistoryBonusSourceChallengeWon,
    LockForChallengeRedeemer,
    MintChallengeTokenRedeemer,
    MintEndorsementTokenRedeemer,
    MintHistoryBonusRedeemer,
    MintStakeTokenRedeemer,
    OutputReference,
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeInconclusive,
    RepChallengeOutcomeVerified,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ReputationChallengeDatum,
    ReleaseChallengeRedeemer,
    ResolveChallengeRedeemer,
    SlashEndorsementRedeemer,
    SlashStakeRedeemer,
    StakeDatum,
    VerificationKeyCredential,
    decode_datum,
)
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from reputation_staking.utils import credential_address, slot_to_posix_ms

logger = logging.getLogger(__name__)

# Fee buffer for TransactionBuilder — PyCardano underestimates fees for
# script-heavy transactions, matching Module 1's workaround.
FEE_BUFFER = 500_000

OUTCOME_CLASSES = {
    0: RepChallengeOutcomeVerified,
    1: RepChallengeOutcomeFalsified,
    2: RepChallengeOutcomeInconclusive,
}

AgentRef = Union[str, bytes]


@dataclass(frozen=True)
class TxWindow:
    """The validity range of a transaction: the slots (validity_start and
    ttl) and the POSIX ms values that a validator sees (lo and hi)."""

    start_slot: int
    end_slot: int
    lo: int
    hi: int


def _ref(utxo: UTxO) -> str:
    return f"{bytes(utxo.input.transaction_id).hex()}#{utxo.input.index}"


def _key_hash(skey: PaymentSigningKey) -> bytes:
    return bytes(PaymentVerificationKey.from_signing_key(skey).hash())


def _capability_bytes(capabilities: Iterable[Union[str, bytes]]) -> List[bytes]:
    return [c.encode("utf-8") if isinstance(c, str) else bytes(c) for c in capabilities]


def _token_quantity(utxo: UTxO, policy: ScriptHash, name: AssetName) -> int:
    amount = utxo.output.amount
    multi_asset = getattr(amount, "multi_asset", None)
    if not multi_asset or policy not in multi_asset:
        return 0
    return int(multi_asset[policy].get(name, 0))


def _out_ref(utxo: UTxO) -> OutputReference:
    return OutputReference(
        transaction_id=bytes(utxo.input.transaction_id), output_index=utxo.input.index,
    )


def _without_lock(stake: StakeDatum, name: bytes) -> IndefiniteList:
    """challenge_locks of `stake` without the first `name` (the contract's
    `without_lock`). Raises ValueError when the stake has no such lock."""
    locks = [bytes(n) for n in stake.challenge_locks]
    if name not in locks:
        raise ValueError(
            f"the stake of {vi.did_from_id(bytes(stake.agent_did))} has no lock of "
            f"the challenge token {name.hex()}"
        )
    locks.remove(name)
    return IndefiniteList(locks)


class ReputationStakingClient:
    """High-level client for Module 3 Reputation Staking operations.

    Uses PyCardano TransactionBuilder for transaction construction and
    the chain context (OgmiosHttpContext) for chain queries. Transactions
    are submitted via HTTP POST.
    """

    def __init__(
        self,
        context,
        skey: PaymentSigningKey,
        deploy_state: Union[dict, Deployment],
        ogmios_url: str = OGMIOS_URL,
        submit_url: str = TX_SUBMIT_URL,
        validity_slots: int = DEFAULT_VALIDITY_SLOTS,
    ):
        self.deployment = (
            deploy_state if isinstance(deploy_state, Deployment)
            else load_deployment(deploy_state)
        )
        if validity_slots <= 0 or validity_slots * SLOT_LENGTH_S * 1000 > vi.MAX_RANGE_MS:
            raise ValueError(
                f"validity_slots must be between 1 and "
                f"{vi.MAX_RANGE_MS // (SLOT_LENGTH_S * 1000)}: a transaction that reads "
                f"an identity record has a validity range of at most one hour"
            )
        self.context = context
        self.skey = skey
        self.vkey = PaymentVerificationKey.from_signing_key(skey)
        self.wallet_addr = Address(payment_part=self.vkey.hash(), network=NETWORK)
        self.ogmios_url = ogmios_url
        self.submit_url = submit_url
        self.validity_slots = validity_slots

        d = self.deployment
        self.reputation_hash = d.reputation_validator_hash
        self.endorsement_hash = d.endorsement_validator_hash
        self.reputation_addr = Address.from_primitive(d.reputation_address)
        self.endorsement_addr = Address.from_primitive(d.endorsement_address)
        self.treasury_addr = Address.from_primitive(d.treasury_holder_address)
        self.params_holder_addr = Address.from_primitive(d.params_holder_address)
        self.identity_addr = Address.from_primitive(d.identity_address)
        self.identity_hash = bytes.fromhex(d.identity_hash)

        # ScriptHash objects for minting
        self.rep_policy = ScriptHash(bytes.fromhex(self.reputation_hash))
        self.end_policy = ScriptHash(bytes.fromhex(self.endorsement_hash))
        self.params_token_policy = ScriptHash(bytes.fromhex(d.params_token_policy))

        # Reference script and CrossValidatorRefs UTxOs (resolved lazily).
        # They are at the deploy wallet address: never spend them.
        self._reference_cache: dict = {}
        self._protected_refs = {
            d.reputation_spend_ref, d.endorsement_spend_ref, d.cross_refs_nft,
        }

    @classmethod
    def from_deploy_state(
        cls,
        deploy_state_path: str,
        context,
        skey: PaymentSigningKey,
        ogmios_url: str = OGMIOS_URL,
        submit_url: str = TX_SUBMIT_URL,
        validity_slots: int = DEFAULT_VALIDITY_SLOTS,
    ) -> "ReputationStakingClient":
        """Create a client from a deploy_state.json file. Raises
        DeploymentStateError for an old or incomplete deploy state."""
        return cls(
            context, skey, load_deployment(deploy_state_path),
            ogmios_url, submit_url, validity_slots,
        )

    # ── Chain reads ─────────────────────────────────────────────────────

    def _resolve_utxo(self, ref: str) -> UTxO:
        """The UTxO at "<tx hash>#<index>"."""
        tx_hash, index = ref.split("#")
        return resolve_utxo(tx_hash, int(index), self.ogmios_url)

    def _utxos_at(self, address) -> List[UTxO]:
        return list(self.context.utxos(str(address)))

    def _tip_slot(self) -> int:
        return self.context.last_block_slot

    def _reference(self, ref: str) -> UTxO:
        if ref not in self._reference_cache:
            self._reference_cache[ref] = self._resolve_utxo(ref)
        return self._reference_cache[ref]

    @property
    def rep_ref_utxo(self) -> UTxO:
        """Reputation validator reference script UTxO."""
        return self._reference(self.deployment.reputation_spend_ref)

    @property
    def end_ref_utxo(self) -> UTxO:
        """Endorsement validator reference script UTxO."""
        return self._reference(self.deployment.endorsement_spend_ref)

    @property
    def cross_refs_utxo(self) -> UTxO:
        """Cross-validator references NFT UTxO (reference input)."""
        return self._reference(self.deployment.cross_refs_nft)

    def params_record(self) -> Tuple[UTxO, ProtocolParamsInfo]:
        """The ProtocolParams record and its values. The record is the
        output at the params holder that holds exactly one `params` token of
        params_token_policy (shared/lib/shared/record_auth.ak)."""
        name = AssetName(PARAMS_TOKEN_NAME)
        for utxo in self._utxos_at(self.params_holder_addr):
            if (
                _token_quantity(utxo, self.params_token_policy, name) == 1
                and utxo.output.datum is not None
            ):
                return utxo, ProtocolParamsInfo.from_datum(utxo.output.datum)
        raise RuntimeError(
            f"no ProtocolParams record at {self.params_holder_addr} with the "
            f"params token of policy {self.deployment.params_token_policy}"
        )

    @property
    def params_utxo(self) -> UTxO:
        """The ProtocolParams record UTxO (reference input)."""
        return self.params_record()[0]

    def protocol_params(self) -> ProtocolParamsInfo:
        """The values of the ProtocolParams record."""
        return self.params_record()[1]

    # ── Identity records ────────────────────────────────────────────────

    def identity_utxos(self) -> List[UTxO]:
        """All UTxOs at the identity address."""
        return self._utxos_at(self.identity_addr)

    def find_identity_record(
        self, agent_did: AgentRef, utxos: Optional[List[UTxO]] = None
    ) -> Optional[vi.IdentityRecord]:
        """The authentic record of an agent (vector_identity.find_record),
        or None."""
        if utxos is None:
            utxos = self.identity_utxos()
        return vi.find_record(utxos, self.identity_hash, agent_did)

    def record_chain(
        self, agent_did: AgentRef, utxos: Optional[List[UTxO]] = None
    ) -> List[vi.IdentityRecord]:
        """The agent's record and the ancestor records that `effective`
        reads (vector_identity.record_chain). Raises RecordNotFoundError or
        MissingAncestorError."""
        if utxos is None:
            utxos = self.identity_utxos()
        return vi.record_chain(
            agent_did, lambda agent_id: vi.find_record(utxos, self.identity_hash, agent_id)
        )

    def tx_window(self) -> TxWindow:
        """The validity range of the next transaction: from the tip slot for
        validity_slots slots. lo and hi are POSIX ms."""
        start = self._tip_slot()
        end = start + self.validity_slots
        lo, hi = slot_to_posix_ms(start), slot_to_posix_ms(end)
        vi.check_window(lo, hi)
        return TxWindow(start, end, lo, hi)

    def _active_chain(
        self,
        agent_id: bytes,
        window: TxWindow,
        capabilities: Sequence[bytes],
        utxos: Optional[List[UTxO]] = None,
    ) -> List[vi.IdentityRecord]:
        chain = self.record_chain(agent_id, utxos)
        vi.require_active(chain, window.lo, window.hi, capabilities)
        return chain

    def _controller_key(
        self, record: vi.IdentityRecord, controller_skey: Optional[PaymentSigningKey]
    ) -> PaymentSigningKey:
        """The signing key of the record's controller: `controller_skey`
        when given, else the wallet key. Raises IdentityError when the key
        is not the controller key."""
        needed = vi.controller_key_hash(record.datum)
        if controller_skey is not None:
            if _key_hash(controller_skey) != needed:
                raise vi.IdentityError(
                    f"the given controller key (hash {_key_hash(controller_skey).hex()}) "
                    f"is not the controller of {record.did} (key hash {needed.hex()})"
                )
            return controller_skey
        if _key_hash(self.skey) == needed:
            return self.skey
        raise vi.IdentityError(
            f"the controller of {record.did} has key hash {needed.hex()}, not the "
            f"wallet key hash {_key_hash(self.skey).hex()}. Pass the controller "
            f"signing key (controller_skey)."
        )

    @staticmethod
    def _require_age(record: vi.IdentityRecord, params: ProtocolParamsInfo, window: TxWindow):
        """A challenger's record is at least min_agent_age old at lo."""
        ready_at = record.datum.created_at + params.min_agent_age
        if ready_at > window.lo:
            raise vi.IdentityError(
                f"{record.did} was created at {record.datum.created_at} (POSIX ms). "
                f"A challenger's record must be at least min_agent_age "
                f"({params.min_agent_age} ms) old at the lower bound of the "
                f"validity range ({window.lo}). It can challenge from {ready_at}."
            )

    def check_agent(
        self,
        agent_did: AgentRef,
        capabilities: Iterable[Union[str, bytes]] = (),
        controller_skey: Optional[PaymentSigningKey] = None,
        *,
        signs: bool = True,
        challenger: bool = False,
    ) -> vi.IdentityRecord:
        """Check an agent as the builders do, before a transaction: the record
        and its ancestors are at the identity address, the record has
        effective status Active over the window of the next transaction and
        lists each capability. With `signs`, a signing key of the controller
        is available (`controller_skey` or the wallet key). With
        `challenger`, the record is at least min_agent_age old. Returns the
        record. Raises IdentityError (or a subclass) when a check fails."""
        window = self.tx_window()
        chain = self._active_chain(
            vi.normalize_agent_id(agent_did), window, _capability_bytes(capabilities)
        )
        if signs:
            self._controller_key(chain[0], controller_skey)
        if challenger:
            self._require_age(chain[0], self.protocol_params(), window)
        return chain[0]

    def _require_oracle(self, action: str):
        if _key_hash(self.skey).hex() != self.deployment.oracle_vkey_hash:
            raise ValueError(
                f"{action} needs the signature of the oracle key "
                f"{self.deployment.oracle_vkey_hash} (oracle_credential). The wallet "
                f"key hash is {_key_hash(self.skey).hex()}."
            )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _vkey_hash_bytes(self) -> bytes:
        return bytes(self.vkey.hash())

    def _wallet_utxos(self) -> list:
        return get_wallet_utxos(self.context, self.wallet_addr, self._protected_refs)

    def _collateral(self) -> UTxO:
        return get_collateral_utxo(self.context, self.wallet_addr, self._protected_refs)

    def _new_builder(self, window: Optional[TxWindow]) -> TransactionBuilder:
        """A TransactionBuilder with all spendable wallet UTxOs as inputs
        and, when given, the validity range of `window`."""
        builder = TransactionBuilder(self.context)
        builder.fee_buffer = FEE_BUFFER
        for u in self._wallet_utxos():
            builder.add_input(u)
        if window is not None:
            builder.validity_start = window.start_slot
            builder.ttl = window.end_slot
        return builder

    @staticmethod
    def _add_reference_inputs(builder: TransactionBuilder, utxos: Iterable[UTxO]):
        """Add reference inputs; each output reference once."""
        present = {_ref(u) for u in builder.reference_inputs if isinstance(u, UTxO)}
        for utxo in utxos:
            if _ref(utxo) not in present:
                builder.reference_inputs.add(utxo)
                present.add(_ref(utxo))

    def _output(
        self,
        address: Address,
        coin: int,
        multi_asset: Optional[MultiAsset] = None,
        datum=None,
    ) -> TransactionOutput:
        """An output with at least `coin` lovelace and at least the minimum
        lovelace of the output (the wallet pays the difference)."""
        probe = TransactionOutput(
            address,
            Value(1_000_000, deepcopy(multi_asset)) if multi_asset else Value(1_000_000),
            datum=datum,
        )
        minimum = min_lovelace_post_alonzo(probe, self.context)
        amount = max(coin, minimum)
        value = Value(amount, multi_asset) if multi_asset else Value(amount)
        return TransactionOutput(address, value, datum=datum)

    def _continuing_stake(
        self, stake_utxo: UTxO, datum: StakeDatum, coin: Optional[int] = None,
    ) -> TransactionOutput:
        """The continuing StakeUTXO of a spent stake: the same address and
        tokens, `coin` lovelace (the lovelace of the spent stake when None),
        raised to the minimum of the output, and `datum`."""
        amount = stake_utxo.output.amount
        return self._output(
            stake_utxo.output.address,
            int(amount.coin) if coin is None else coin,
            deepcopy(amount.multi_asset),
            datum,
        )

    def _finish(
        self,
        builder: TransactionBuilder,
        signing_keys: Sequence[PaymentSigningKey],
        required: Sequence[bytes],
    ) -> Transaction:
        """Set the required signers and the collateral, then build and sign
        with the wallet key and `signing_keys`."""
        signers = list(dict.fromkeys(bytes(h) for h in required))
        builder.required_signers = [VerificationKeyHash(h) for h in signers]
        builder.collaterals = [self._collateral()]
        keys = {}
        for key in (self.skey, *signing_keys):
            keys.setdefault(_key_hash(key), key)
        return builder.build_and_sign(list(keys.values()), change_address=self.wallet_addr)

    def _submit(self, tx: Transaction, wait: bool) -> str:
        tx_hash = submit_tx(tx, self.submit_url)
        if wait:
            wait_for_tx()
        return tx_hash

    def _find_with_token(self, address: Address, policy: ScriptHash, token_hex: str) -> Optional[UTxO]:
        name = AssetName(bytes.fromhex(token_hex))
        for utxo in self._utxos_at(address):
            if _token_quantity(utxo, policy, name) >= 1:
                return utxo
        return None

    # ── UTxO Finders ─────────────────────────────────────────────────────

    def find_stake_utxo(self, agent_did: AgentRef) -> Optional[UTxO]:
        """Find the stake UTxO for an agent."""
        agent_id = vi.normalize_agent_id(agent_did)
        return self._find_with_token(
            self.reputation_addr, self.rep_policy, derive_stake_token_name(agent_id.hex())
        )

    def find_endorsement_utxo(
        self, endorser_did: AgentRef, target_did: AgentRef
    ) -> Optional[UTxO]:
        """Find an endorsement UTxO."""
        token_name = derive_endorsement_token_name(
            vi.normalize_agent_id(endorser_did).hex(), vi.normalize_agent_id(target_did).hex()
        )
        return self._find_with_token(self.endorsement_addr, self.end_policy, token_name)

    def find_challenge_utxo(
        self, challenger_did: AgentRef, target_did: AgentRef, capability: str
    ) -> Optional[UTxO]:
        """Find a challenge UTxO."""
        token_name = derive_challenge_token_name(
            vi.normalize_agent_id(challenger_did).hex(),
            vi.normalize_agent_id(target_did).hex(),
            capability,
        )
        return self._find_with_token(self.endorsement_addr, self.end_policy, token_name)

    def get_challenge(
        self, challenger_did: AgentRef, target_did: AgentRef, capability: str
    ) -> Tuple[UTxO, ReputationChallengeDatum]:
        """The challenge UTxO and its datum. Raises RuntimeError when the
        challenge is not on chain."""
        utxo = self.find_challenge_utxo(challenger_did, target_did, capability)
        if utxo is None:
            raise RuntimeError("Challenge UTxO not found on-chain")
        wrapped = decode_datum(EndorsementValidatorDatumChallenge, utxo.output.datum)
        return utxo, wrapped.datum

    def _target_stake(self, target_id: bytes) -> Tuple[UTxO, StakeDatum]:
        utxo = self.find_stake_utxo(target_id)
        if utxo is None:
            raise RuntimeError(f"{vi.did_from_id(target_id)} has no stake UTxO")
        return utxo, decode_datum(StakeDatum, utxo.output.datum)

    def _require_declared_wallet(self, record: vi.IdentityRecord) -> None:
        """The wallet key is the payout credential that an opening stores.
        The contract accepts only a declared credential of the acting
        agent's record: the controller key or a listed key with the
        transaction purpose. Raises IdentityError otherwise."""
        vi.require_declared_credential(record, self._vkey_hash_bytes())

    # ── Stake Operations ─────────────────────────────────────────────────

    def build_create_seed_utxo(
        self, agent_did: AgentRef, capabilities: list, min_lovelace: int = 2_000_000
    ) -> Transaction:
        """A seed UTxO at the reputation address for a later CreateStake.
        CreateStake reads agent_did from its datum."""
        datum = StakeDatum(
            agent_did=vi.normalize_agent_id(agent_did),
            owner_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
            stake_amount=min_lovelace,
            staked_capabilities=IndefiniteList(_capability_bytes(capabilities)),
            last_updated=slot_to_posix_ms(self._tip_slot()),
            history_points=0,
            challenge_locks=IndefiniteList([]),
        )
        builder = self._new_builder(None)
        builder.add_output(TransactionOutput(self.reputation_addr, min_lovelace, datum=datum))
        return builder.build_and_sign([self.skey], change_address=self.wallet_addr)

    def create_seed_utxo(
        self,
        agent_did: AgentRef,
        capabilities: list,
        min_lovelace: int = 2_000_000,
        wait: bool = True,
    ) -> str:
        """Send a seed UTxO to the reputation address (required before CreateStake).

        Returns:
            Transaction hash. The seed UTxO will be at {tx_hash}#0.
        """
        tx_hash = self._submit(
            self.build_create_seed_utxo(agent_did, capabilities, min_lovelace), wait
        )
        logger.info("Seed UTxO tx: %s", tx_hash)
        return tx_hash

    def build_create_stake(
        self,
        agent_did: AgentRef,
        capabilities: list,
        stake_amount: int,
        seed_utxo: Optional[str] = None,
        controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Transaction:
        """CreateStake: spend the seed UTxO, mint the stake token and create
        the stake UTxO. The agent's record is Active over the validity range
        and lists each capability; the controller and the wallet key
        (owner_credential, a declared credential of the record) sign;
        last_updated is the upper bound; challenge_locks is empty. The seed
        holds no reputation token."""
        if not seed_utxo:
            raise ValueError("seed_utxo is required for CreateStake")
        agent_id = vi.normalize_agent_id(agent_did)
        caps = _capability_bytes(capabilities)
        if not caps:
            raise ValueError("CreateStake needs at least one capability")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        if stake_amount < params.min_self_stake:
            raise ValueError(
                f"stake_amount {stake_amount} is below min_self_stake {params.min_self_stake}"
            )
        chain = self._active_chain(agent_id, window, caps)
        controller = self._controller_key(chain[0], controller_skey)
        self._require_declared_wallet(chain[0])

        seed = self._resolve_utxo(seed_utxo)
        if seed.output.address.payment_part != self.rep_policy:
            raise ValueError(f"seed UTxO {seed_utxo} is not at the reputation address")
        seed_assets = getattr(seed.output.amount, "multi_asset", None) or {}
        if any(quantity for quantity in seed_assets.get(self.rep_policy, {}).values()):
            raise ValueError(
                f"seed UTxO {seed_utxo} holds a reputation token; CreateStake "
                f"needs a seed without one"
            )
        seed_datum = decode_datum(StakeDatum, seed.output.datum)
        if bytes(seed_datum.agent_did) != agent_id:
            raise ValueError(
                f"seed UTxO {seed_utxo} names agent {bytes(seed_datum.agent_did).hex()}, "
                f"not {agent_id.hex()}"
            )

        token = AssetName(bytes.fromhex(derive_stake_token_name(agent_id.hex())))
        datum = StakeDatum(
            agent_did=agent_id,
            owner_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
            stake_amount=stake_amount,
            staked_capabilities=IndefiniteList(caps),
            last_updated=window.hi,
            history_points=0,
            challenge_locks=IndefiniteList([]),
        )

        builder = self._new_builder(window)
        builder.add_script_input(
            seed, script=self.rep_ref_utxo, redeemer=Redeemer(CreateStakeRedeemer()),
        )
        builder.mint = MultiAsset({self.rep_policy: Asset({token: 1})})
        builder.add_minting_script(self.rep_ref_utxo, redeemer=Redeemer(MintStakeTokenRedeemer()))
        builder.add_output(
            self._output(
                self.reputation_addr, stake_amount,
                MultiAsset({self.rep_policy: Asset({token: 1})}), datum,
            )
        )
        self._add_reference_inputs(
            builder, [r.utxo for r in chain] + [params_utxo, self.cross_refs_utxo]
        )
        return self._finish(
            builder, [controller], [self._vkey_hash_bytes(), vi.controller_key_hash(chain[0].datum)]
        )

    def create_stake(
        self,
        agent_did: AgentRef,
        capabilities: list,
        stake_amount: int,
        seed_utxo: Optional[str] = None,
        controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> str:
        """Create an initial self-stake for an agent.

        Args:
            agent_did: The agent (canonical DID or 64 hex characters).
            capabilities: List of capability strings; the record lists each.
            stake_amount: Amount in DFM (lovelace).
            seed_utxo: "txhash#idx" at reputation address to consume.
            controller_skey: Signing key of the record's controller when it
                is not the wallet key.
            wait: Whether to wait for confirmation.

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_create_stake(agent_did, capabilities, stake_amount, seed_utxo, controller_skey),
            wait,
        )
        logger.info("CreateStake tx: %s", tx_hash)
        return tx_hash

    # ── Endorsement Operations ───────────────────────────────────────────

    def build_mint_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        capabilities: list,
        stake_amount: int,
        controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Transaction:
        """MintEndorsementToken: the endorser's record is Active and its
        controller and the wallet key (endorser_credential, a declared
        credential of the record) sign; the target's record is Active and
        lists each endorsed capability; the target's stake backs each
        endorsed capability; created_at is the lower bound; slashed_by is
        empty."""
        endorser_id = vi.normalize_agent_id(endorser_did)
        target_id = vi.normalize_agent_id(target_did)
        if endorser_id == target_id:
            raise ValueError("an agent cannot endorse itself")
        caps = _capability_bytes(capabilities)
        if not caps:
            raise ValueError("an endorsement needs at least one capability")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        if stake_amount < params.min_endorsement:
            raise ValueError(
                f"stake_amount {stake_amount} is below min_endorsement {params.min_endorsement}"
            )
        utxos = self.identity_utxos()
        endorser_chain = self._active_chain(endorser_id, window, (), utxos)
        target_chain = self._active_chain(target_id, window, caps, utxos)
        controller = self._controller_key(endorser_chain[0], controller_skey)
        self._require_declared_wallet(endorser_chain[0])

        stake_utxo, stake = self._target_stake(target_id)
        staked = {bytes(c) for c in stake.staked_capabilities}
        unbacked = [c for c in caps if c not in staked]
        if unbacked:
            raise ValueError(
                f"the stake of {vi.did_from_id(target_id)} does not back "
                + ", ".join(c.decode("utf-8", "replace") for c in unbacked)
            )

        token = AssetName(
            bytes.fromhex(derive_endorsement_token_name(endorser_id.hex(), target_id.hex()))
        )
        datum = EndorsementValidatorDatumEndorsement(
            datum=EndorsementDatum(
                endorser_did=endorser_id,
                endorser_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
                target_did=target_id,
                stake_amount=stake_amount,
                endorsed_capabilities=IndefiniteList(caps),
                created_at=window.lo,
                slashed_by=IndefiniteList([]),
            )
        )

        builder = self._new_builder(window)
        builder.mint = MultiAsset({self.end_policy: Asset({token: 1})})
        builder.add_minting_script(
            self.end_ref_utxo, redeemer=Redeemer(MintEndorsementTokenRedeemer()),
        )
        builder.add_output(
            self._output(
                self.endorsement_addr, stake_amount,
                MultiAsset({self.end_policy: Asset({token: 1})}), datum,
            )
        )
        self._add_reference_inputs(
            builder,
            [r.utxo for r in endorser_chain] + [r.utxo for r in target_chain]
            + [stake_utxo, params_utxo, self.cross_refs_utxo],
        )
        return self._finish(
            builder, [controller],
            [self._vkey_hash_bytes(), vi.controller_key_hash(endorser_chain[0].datum)],
        )

    def mint_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        capabilities: list,
        stake_amount: int,
        controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> str:
        """Mint an endorsement from one agent to another.

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_mint_endorsement(
                endorser_did, target_did, capabilities, stake_amount, controller_skey,
            ),
            wait,
        )
        logger.info("MintEndorsement tx: %s", tx_hash)
        return tx_hash

    # ── Challenge Operations ─────────────────────────────────────────────

    def build_mint_challenge(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        stake_amount: int,
        evidence_hash: str,
        evidence_uri: str,
        controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Tuple[Transaction, EndorsementValidatorDatumChallenge]:
        """MintChallengeToken: the challenger's record is Active, at least
        min_agent_age old at the lower bound, and its controller and the
        wallet key (challenger_credential, a declared credential of the
        record) sign; created_at is the lower bound.

        The target's record is not read: the target's stake decides. It
        lists the capability, and target_credential is its owner_credential.
        The transaction spends the stake with LockForChallenge; the
        continuing stake keeps its address, value and datum, with the
        challenge token name added first to challenge_locks."""
        challenger_id = vi.normalize_agent_id(challenger_did)
        target_id = vi.normalize_agent_id(target_did)
        if challenger_id == target_id:
            raise ValueError("an agent cannot challenge itself")
        evidence = bytes.fromhex(evidence_hash)
        if len(evidence) != 32:
            raise ValueError("evidence_hash must be 32 bytes (64 hex characters)")
        capability_bytes = capability.encode("utf-8")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        if stake_amount < params.min_challenge_stake:
            raise ValueError(
                f"stake_amount {stake_amount} is below min_challenge_stake "
                f"{params.min_challenge_stake}"
            )
        challenger_chain = self._active_chain(challenger_id, window, ())
        self._require_age(challenger_chain[0], params, window)
        controller = self._controller_key(challenger_chain[0], controller_skey)
        self._require_declared_wallet(challenger_chain[0])

        stake_utxo, stake = self._target_stake(target_id)
        if capability_bytes not in {bytes(c) for c in stake.staked_capabilities}:
            raise ValueError(
                f"the stake of {vi.did_from_id(target_id)} does not back {capability}"
            )
        token_bytes = bytes.fromhex(
            derive_challenge_token_name(challenger_id.hex(), target_id.hex(), capability)
        )
        if token_bytes in {bytes(n) for n in stake.challenge_locks}:
            raise ValueError(
                f"an open challenge of {vi.did_from_id(challenger_id)} against "
                f"{capability} already locks the stake of {vi.did_from_id(target_id)}"
            )
        token = AssetName(token_bytes)
        datum = EndorsementValidatorDatumChallenge(
            datum=ReputationChallengeDatum(
                challenger_did=challenger_id,
                challenger_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
                target_did=target_id,
                target_credential=stake.owner_credential,
                challenged_capability=capability_bytes,
                stake_amount=stake_amount,
                evidence_hash=evidence,
                evidence_uri=evidence_uri.encode("utf-8"),
                created_at=window.lo,
                counter_evidence_hash=b"",
                counter_evidence_uri=b"",
                response_submitted_at=0,
                state=RepChallengeStateOpen(),
            )
        )
        locked = dataclasses.replace(
            stake,
            challenge_locks=IndefiniteList([token_bytes] + [bytes(n) for n in stake.challenge_locks]),
        )

        builder = self._new_builder(window)
        builder.add_script_input(
            stake_utxo, script=self.rep_ref_utxo, redeemer=Redeemer(LockForChallengeRedeemer()),
        )
        builder.mint = MultiAsset({self.end_policy: Asset({token: 1})})
        builder.add_minting_script(
            self.end_ref_utxo, redeemer=Redeemer(MintChallengeTokenRedeemer()),
        )
        builder.add_output(
            self._output(
                self.endorsement_addr, stake_amount,
                MultiAsset({self.end_policy: Asset({token: 1})}), datum,
            )
        )
        builder.add_output(self._continuing_stake(stake_utxo, locked))
        self._add_reference_inputs(
            builder,
            [r.utxo for r in challenger_chain] + [params_utxo, self.cross_refs_utxo],
        )
        tx = self._finish(
            builder, [controller],
            [self._vkey_hash_bytes(), vi.controller_key_hash(challenger_chain[0].datum)],
        )
        return tx, datum

    def mint_challenge(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        stake_amount: int,
        evidence_hash: str,
        evidence_uri: str,
        controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> tuple:
        """Mint a challenge against a target agent's capability.

        Returns:
            Tuple of (tx_hash, challenge_datum).
        """
        tx, datum = self.build_mint_challenge(
            challenger_did, target_did, capability, stake_amount,
            evidence_hash, evidence_uri, controller_skey,
        )
        tx_hash = self._submit(tx, wait)
        logger.info("MintChallenge tx: %s", tx_hash)
        return tx_hash, datum

    def build_resolve_challenge(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        outcome_constructor: int,
    ) -> Transaction:
        """ResolveChallenge (Foundation oracle): the oracle key signs; the
        continuing challenge output keeps the address, the value and every
        field except `state` = Resolved { outcome }. The challenge datum
        comes from the challenge UTxO."""
        outcome_cls = OUTCOME_CLASSES.get(outcome_constructor)
        if outcome_cls is None:
            raise ValueError(f"Invalid outcome constructor: {outcome_constructor}")
        self._require_oracle("ResolveChallenge")
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if not isinstance(inner.state, RepChallengeStateOpen):
            raise ValueError("ResolveChallenge needs a challenge in state Open")
        resolved = EndorsementValidatorDatumChallenge(
            datum=dataclasses.replace(inner, state=RepChallengeStateResolved(outcome=outcome_cls()))
        )
        window = self.tx_window()
        params_utxo, _ = self.params_record()

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(
                ChallengeSpendRedeemer(action=ResolveChallengeRedeemer(outcome=outcome_cls()))
            ),
        )
        amount = challenge_utxo.output.amount
        builder.add_output(
            TransactionOutput(
                challenge_utxo.output.address,
                Value(amount.coin, deepcopy(amount.multi_asset)),
                datum=resolved,
            )
        )
        self._add_reference_inputs(builder, [params_utxo, self.cross_refs_utxo])
        return self._finish(builder, [], [self._vkey_hash_bytes()])

    def resolve_challenge(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        outcome_constructor: int,
        wait: bool = True,
    ) -> str:
        """Resolve a challenge (Foundation oracle in Phase 1.0).

        Args:
            outcome_constructor: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_resolve_challenge(challenger_did, target_did, capability, outcome_constructor),
            wait,
        )
        logger.info("ResolveChallenge tx: %s", tx_hash)
        return tx_hash

    @staticmethod
    def resolved_outcome(inner: ReputationChallengeDatum):
        """The outcome of a Resolved challenge. Raises ValueError for
        another state."""
        if not isinstance(inner.state, RepChallengeStateResolved):
            raise ValueError("the challenge is not Resolved")
        return inner.state.outcome

    def _history_bonus(
        self,
        agent_id: bytes,
        source: UTxO,
        bonus_points: int,
        window: TxWindow,
        controller_skey: Optional[PaymentSigningKey],
    ) -> Tuple[AssetName, TransactionOutput, List[vi.IdentityRecord], PaymentSigningKey]:
        """The ChallengeWon history bonus (MintHistoryBonus): the oracle
        signs, the bonus agent's record is Active over the validity range
        and its controller signs. Returns the token, the bonus output, the
        record chain and the controller key."""
        self._require_oracle("MintHistoryBonus")
        chain = self._active_chain(agent_id, window, ())
        controller = self._controller_key(chain[0], controller_skey)
        tx_hex = bytes(source.input.transaction_id).hex()
        token = AssetName(bytes.fromhex(derive_history_bonus_token_name(tx_hex, source.input.index)))
        datum = HistoryBonusDatum(
            agent_did=agent_id,
            source=HistoryBonusSourceChallengeWon(),
            bonus_points=bonus_points,
            source_ref=OutputReference(
                transaction_id=bytes.fromhex(tx_hex), output_index=source.input.index,
            ),
            created_at=window.lo,
        )
        output = self._output(
            self.reputation_addr, HISTORY_BONUS_OUTPUT_LOVELACE,
            MultiAsset({self.rep_policy: Asset({token: 1})}), datum,
        )
        return token, output, chain, controller

    def build_distribute_outcome(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        mint_bonus: bool = False,
        bonus_controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Transaction:
        """DistributeOutcome for CapabilityVerified or Inconclusive.

        Burns the challenge token and spends the target's stake with
        ReleaseChallenge: the continuing stake keeps its address, value and
        datum, without the lock of this challenge. fee = stake ×
        protocol_fee_rate. CapabilityVerified pays the challenge stake less
        the fee to target_credential and the fee to the treasury.
        Inconclusive pays the stake less half of the fee to
        challenger_credential and that half to the treasury; the target's
        stake keeps all its AP3X. The wallet pays the transaction fee and the
        rest of the payouts.

        A history bonus is not a condition of DistributeOutcome (W2-M36
        decision 4). With `mint_bonus` (CapabilityVerified only), the
        transaction also mints the target's ChallengeWon bonus: the oracle
        (the wallet key) signs, the target's record is Active and its
        controller signs (`bonus_controller_skey` when it is not the wallet
        key).
        """
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        outcome = self.resolved_outcome(inner)
        if isinstance(outcome, RepChallengeOutcomeFalsified):
            raise ValueError(
                "the outcome is CapabilityFalsified: use distribute_falsified_outcome"
            )
        verified = isinstance(outcome, RepChallengeOutcomeVerified)
        if mint_bonus and not verified:
            raise ValueError("an Inconclusive outcome has no winner, so no history bonus")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        challenge_stake = inner.stake_amount
        fee = challenge_stake * params.protocol_fee_rate // 10_000
        token_bytes = bytes.fromhex(
            derive_challenge_token_name(
                bytes(inner.challenger_did).hex(), bytes(inner.target_did).hex(),
                bytes(inner.challenged_capability).decode("utf-8"),
            )
        )
        challenge_token = AssetName(token_bytes)
        stake_utxo, stake = self._target_stake(bytes(inner.target_did))
        released = dataclasses.replace(stake, challenge_locks=_without_lock(stake, token_bytes))

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=DistributeOutcomeRedeemer())),
        )
        builder.add_script_input(
            stake_utxo,
            script=self.rep_ref_utxo,
            redeemer=Redeemer(ReleaseChallengeRedeemer(challenge_ref=_out_ref(challenge_utxo))),
        )
        builder.add_output(self._continuing_stake(stake_utxo, released))
        mint = MultiAsset({self.end_policy: Asset({challenge_token: -1})})
        if verified:
            treasury_fee = fee
            builder.add_output(
                self._output(credential_address(inner.target_credential), challenge_stake - fee)
            )
        else:
            treasury_fee = fee // 2
            builder.add_output(
                self._output(
                    credential_address(inner.challenger_credential), challenge_stake - treasury_fee,
                )
            )
        if treasury_fee > 0:
            builder.add_output(self._output(self.treasury_addr, treasury_fee))

        signing_keys: List[PaymentSigningKey] = []
        required = [self._vkey_hash_bytes()]
        references = [params_utxo, self.cross_refs_utxo]
        if mint_bonus:
            token, output, chain, controller = self._history_bonus(
                bytes(inner.target_did), challenge_utxo, challenge_stake, window,
                bonus_controller_skey,
            )
            mint[self.rep_policy] = Asset({token: 1})
            builder.add_output(output)
            references = [r.utxo for r in chain] + references
            signing_keys.append(controller)
            required.append(vi.controller_key_hash(chain[0].datum))

        builder.mint = mint
        builder.add_minting_script(
            self.end_ref_utxo, redeemer=Redeemer(BurnChallengeTokenRedeemer()),
        )
        if mint_bonus:
            builder.add_minting_script(
                self.rep_ref_utxo, redeemer=Redeemer(MintHistoryBonusRedeemer()),
            )
        self._add_reference_inputs(builder, references)
        return self._finish(builder, signing_keys, required)

    def distribute_outcome(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        mint_bonus: bool = False,
        bonus_controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> str:
        """Distribute the outcome of a CapabilityVerified or Inconclusive
        challenge (see build_distribute_outcome).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_distribute_outcome(
                challenger_did, target_did, capability, mint_bonus, bonus_controller_skey,
            ),
            wait,
        )
        logger.info("DistributeOutcome tx: %s", tx_hash)
        return tx_hash

    # ── Slash Operations (CapabilityFalsified path) ────────────────────

    def build_slash_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        resolved_challenge_utxo_ref: str,
    ) -> Transaction:
        """SlashEndorsement after CapabilityFalsified.

        The resolved challenge is a reference input. slash = stake ×
        slash_rate_endorser, fee = slash × protocol_fee_rate. The challenger
        gets slash − fee and the treasury gets the fee. When the remaining
        stake is at least min_endorsement, the endorsement continues at the
        same address with the same datum, stake_amount = remaining and the
        challenge token name added first to slashed_by. Else the endorsement
        token is burned and the endorser gets the remaining stake. A
        challenge slashes an endorsement at most once (slashed_by).
        """
        endorser_id = vi.normalize_agent_id(endorser_did)
        target_id = vi.normalize_agent_id(target_did)
        endorsement_utxo = self.find_endorsement_utxo(endorser_id, target_id)
        if endorsement_utxo is None:
            raise RuntimeError("Endorsement UTxO not found on-chain")
        endorsement = decode_datum(
            EndorsementValidatorDatumEndorsement, endorsement_utxo.output.datum
        ).datum

        challenge_utxo = self._resolve_utxo(resolved_challenge_utxo_ref)
        if challenge_utxo.output.address.payment_part != self.end_policy:
            raise ValueError("the resolved challenge is not at the endorsement address")
        challenge = decode_datum(
            EndorsementValidatorDatumChallenge, challenge_utxo.output.datum
        ).datum
        if not isinstance(self.resolved_outcome(challenge), RepChallengeOutcomeFalsified):
            raise ValueError("SlashEndorsement needs a CapabilityFalsified challenge")
        if bytes(challenge.target_did) != target_id:
            raise ValueError("the challenge has another target than the endorsement")
        endorsed = {bytes(c) for c in endorsement.endorsed_capabilities}
        if bytes(challenge.challenged_capability) not in endorsed:
            raise ValueError("the endorsement does not endorse the challenged capability")
        challenge_token = bytes.fromhex(
            derive_challenge_token_name(
                bytes(challenge.challenger_did).hex(), target_id.hex(),
                bytes(challenge.challenged_capability).decode("utf-8"),
            )
        )
        if challenge_token in {bytes(n) for n in endorsement.slashed_by}:
            raise ValueError("this challenge already slashed the endorsement")

        window = self.tx_window()
        params_utxo, params = self.params_record()
        slash = endorsement.stake_amount * params.slash_rate_endorser // 10_000
        remaining = endorsement.stake_amount - slash
        fee = slash * params.protocol_fee_rate // 10_000
        continues = remaining >= params.min_endorsement
        token = AssetName(
            bytes.fromhex(derive_endorsement_token_name(endorser_id.hex(), target_id.hex()))
        )

        builder = self._new_builder(window)
        builder.add_script_input(
            endorsement_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(
                EndorsementSpendRedeemer(
                    action=SlashEndorsementRedeemer(
                        challenge_ref=OutputReference(
                            transaction_id=bytes(challenge_utxo.input.transaction_id),
                            output_index=challenge_utxo.input.index,
                        )
                    )
                )
            ),
        )
        if continues:
            builder.add_output(
                self._output(
                    endorsement_utxo.output.address, remaining,
                    MultiAsset({self.end_policy: Asset({token: 1})}),
                    EndorsementValidatorDatumEndorsement(
                        datum=dataclasses.replace(
                            endorsement,
                            stake_amount=remaining,
                            slashed_by=IndefiniteList(
                                [challenge_token] + [bytes(n) for n in endorsement.slashed_by]
                            ),
                        )
                    ),
                )
            )
            endorser_due = 0
        else:
            builder.mint = MultiAsset({self.end_policy: Asset({token: -1})})
            builder.add_minting_script(
                self.end_ref_utxo, redeemer=Redeemer(BurnEndorsementTokenRedeemer()),
            )
            endorser_due = remaining

        challenger_due = slash - fee
        if challenge.challenger_credential == endorsement.endorser_credential:
            due = challenger_due + endorser_due
            if due > 0:
                builder.add_output(
                    self._output(credential_address(challenge.challenger_credential), due)
                )
        else:
            if challenger_due > 0:
                builder.add_output(
                    self._output(credential_address(challenge.challenger_credential), challenger_due)
                )
            if endorser_due > 0:
                builder.add_output(
                    self._output(credential_address(endorsement.endorser_credential), endorser_due)
                )
        if fee > 0:
            builder.add_output(self._output(self.treasury_addr, fee))

        self._add_reference_inputs(builder, [challenge_utxo, params_utxo, self.cross_refs_utxo])
        return self._finish(builder, [], [self._vkey_hash_bytes()])

    def slash_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        resolved_challenge_utxo_ref: str,
        wait: bool = True,
    ) -> str:
        """Slash an endorsement after CapabilityFalsified resolution.

        The resolved challenge UTxO is included as a reference input (not consumed).
        Must be called BEFORE distribute_falsified_outcome, which consumes the challenge.

        Args:
            resolved_challenge_utxo_ref: "txhash#idx" of the resolved challenge UTxO.

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_slash_endorsement(endorser_did, target_did, resolved_challenge_utxo_ref),
            wait,
        )
        logger.info("SlashEndorsement tx: %s", tx_hash)
        return tx_hash

    def build_distribute_falsified_outcome(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        mint_bonus: bool = False,
        bonus_controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Transaction:
        """DistributeOutcome for CapabilityFalsified with SlashStake.

        Spends the resolved challenge (DistributeOutcome) and the target's
        locked stake (SlashStake), burns the challenge token, pays the
        challenger held + target_slash − fee (held = the AP3X that the
        challenge UTxO holds) and the treasury the fee, and continues the
        stake with stake_amount − target_slash (target_slash = stake_amount /
        number of staked capabilities), without the lock of this challenge
        and with the same last_updated (a slash is not an owner action).
        With `mint_bonus`, the transaction also mints the challenger's
        ChallengeWon bonus (see build_distribute_outcome).
        """
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if not isinstance(self.resolved_outcome(inner), RepChallengeOutcomeFalsified):
            raise ValueError("the outcome is not CapabilityFalsified: use distribute_outcome")
        target_id = bytes(inner.target_did)
        stake_utxo, stake = self._target_stake(target_id)
        num_capabilities = len(stake.staked_capabilities)
        if num_capabilities == 0:
            raise RuntimeError("Target has no staked capabilities")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        target_slash = stake.stake_amount // num_capabilities
        remaining = stake.stake_amount - target_slash
        fee = target_slash * params.protocol_fee_rate // 10_000
        held = int(challenge_utxo.output.amount.coin)
        challenger_reward = held + target_slash - fee

        token_bytes = bytes.fromhex(
            derive_challenge_token_name(
                bytes(inner.challenger_did).hex(), target_id.hex(),
                bytes(inner.challenged_capability).decode("utf-8"),
            )
        )
        challenge_token = AssetName(token_bytes)
        slashed = dataclasses.replace(
            stake, stake_amount=remaining, challenge_locks=_without_lock(stake, token_bytes),
        )

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=DistributeOutcomeRedeemer())),
        )
        builder.add_script_input(
            stake_utxo,
            script=self.rep_ref_utxo,
            redeemer=Redeemer(SlashStakeRedeemer(challenge_ref=_out_ref(challenge_utxo))),
        )
        builder.add_output(
            self._output(credential_address(inner.challenger_credential), challenger_reward)
        )
        if fee > 0:
            builder.add_output(self._output(self.treasury_addr, fee))
        builder.add_output(self._continuing_stake(stake_utxo, slashed, remaining))

        mint = MultiAsset({self.end_policy: Asset({challenge_token: -1})})
        signing_keys: List[PaymentSigningKey] = []
        required = [self._vkey_hash_bytes()]
        references = [params_utxo, self.cross_refs_utxo]
        if mint_bonus:
            token, output, chain, controller = self._history_bonus(
                bytes(inner.challenger_did), challenge_utxo, inner.stake_amount, window,
                bonus_controller_skey,
            )
            mint[self.rep_policy] = Asset({token: 1})
            builder.add_output(output)
            references = [r.utxo for r in chain] + references
            signing_keys.append(controller)
            required.append(vi.controller_key_hash(chain[0].datum))

        builder.mint = mint
        builder.add_minting_script(
            self.end_ref_utxo, redeemer=Redeemer(BurnChallengeTokenRedeemer()),
        )
        if mint_bonus:
            builder.add_minting_script(
                self.rep_ref_utxo, redeemer=Redeemer(MintHistoryBonusRedeemer()),
            )
        self._add_reference_inputs(builder, references)
        return self._finish(builder, signing_keys, required)

    def distribute_falsified_outcome(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        mint_bonus: bool = False,
        bonus_controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> str:
        """Distribute outcome for CapabilityFalsified + slash target's stake
        (see build_distribute_falsified_outcome).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_distribute_falsified_outcome(
                challenger_did, target_did, capability, mint_bonus, bonus_controller_skey,
            ),
            wait,
        )
        logger.info("DistributeFalsifiedOutcome tx: %s", tx_hash)
        return tx_hash
