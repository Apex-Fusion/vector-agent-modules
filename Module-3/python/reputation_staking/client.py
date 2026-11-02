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
  challenge_locks. Every closing path spends it again: ReleaseChallenge
  removes the lock (WithdrawChallenge, ExpireChallenge, and DistributeOutcome
  for CapabilityVerified or Inconclusive), and SlashStake removes the lock
  and slashes the stake (CapabilityFalsified).
- Stakes: `stake_id` of a StakeDatum is the output reference of the seed
  input that CreateStake spent. Every continuing stake keeps it, and no
  action burns a stake token: a stake that decays below min_self_stake
  continues with stake_amount 0. An agent can have more than one stake. An
  opening that reads a stake of the target (an endorsement, a challenge)
  uses the target's stake with the largest stake_amount, unless the caller
  names a stake by its stake_id. A closing path spends the stake that holds
  the lock of its challenge.
- Bound stake: an endorsement stores the stake_id of the stake that it read
  at creation (target_stake). Every withdrawal and slash of the endorsement
  has that stake as a reference input. A withdrawal needs the bound stake
  without challenge locks; a slash needs the bound stake with the lock of
  the challenge.
- Payee rule (`payee`): every payout to a position goes to the payee of the
  position's agent: the stored credential while the agent's record declares
  it (the controller key, or a listed key with the transaction purpose),
  else the controller key of the record. The record is a reference input of
  the payout transaction (any status, also a tombstone). The exits
  (DecreaseStake, WithdrawEndorsement, WithdrawChallenge) also need the
  signature of the record's controller (`controller_skey` when it is not the
  wallet key). The permissionless payouts (ExpireChallenge,
  DistributeOutcome, SlashEndorsement) need no signature.
- Escalation: EscalateToAudit creates a Module-1 claim of the target (the
  claimer is the target: Module-1 ClaimerWins maps to CapabilityVerified)
  with claim_hash = blake2b_256(evidence_hash ++ counter_evidence_hash) and
  claim_type "reputation_challenge"; the challenge continues as
  Escalated { audit_claim_token }. ResolveEscalation reads the resolved
  Module-1 challenge of that claim and maps its verdict.
- The client checks these rules before it builds a transaction. It raises
  vector_identity.IdentityError (or a subclass) when an identity rule fails
  and ValueError (or RuntimeError for a missing UTxO) for another rule.

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
    plutus_script_hash,
)
from pycardano.serialization import IndefiniteList
from pycardano.utils import min_lovelace_post_alonzo

from reputation_staking import vector_identity as vi
from reputation_staking.constants import (
    CHALLENGE_PREFIX,
    CHALLENGE_SLICE_LEN,
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
    AuditAuditorWins,
    AuditChallengeDatum,
    AuditClaimDatum,
    AuditClaimerWins,
    AuditInconclusive,
    AuditOpen,
    AuditResolved,
    AuditSubmitClaimRedeemer,
    BurnChallengeTokenRedeemer,
    BurnEndorsementTokenRedeemer,
    ChallengeSpendRedeemer,
    CreateStakeRedeemer,
    Credential,
    DecreaseStakeRedeemer,
    DistributeOutcomeRedeemer,
    EndorsementDatum,
    EndorsementSpendRedeemer,
    EndorsementValidatorDatumChallenge,
    EndorsementValidatorDatumEndorsement,
    EscalateToAuditRedeemer,
    ExpireChallengeRedeemer,
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
    RepChallengeStateEscalated,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ReputationChallengeDatum,
    ReleaseChallengeRedeemer,
    ResolveChallengeRedeemer,
    ResolveEscalationRedeemer,
    SlashEndorsementRedeemer,
    SlashStakeRedeemer,
    StakeDatum,
    VerificationKeyCredential,
    WithdrawChallengeRedeemer,
    WithdrawEndorsementRedeemer,
    decode_datum,
)
from reputation_staking.token_names import (
    derive_audit_claim_token_name,
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
    derive_token_name,
)
from reputation_staking.utils import (
    credential_address,
    script_hash_to_address,
    slot_to_posix_ms,
)

logger = logging.getLogger(__name__)

# Fee buffer for TransactionBuilder — PyCardano underestimates fees for
# script-heavy transactions, matching Module 1's workaround.
FEE_BUFFER = 500_000

OUTCOME_CLASSES = {
    0: RepChallengeOutcomeVerified,
    1: RepChallengeOutcomeFalsified,
    2: RepChallengeOutcomeInconclusive,
}

# ResolveEscalation maps the Module-1 verdict to the outcome
# (challenge_validation.map_audit_verdict): the Module-1 claimer is the
# target of the challenge.
AUDIT_OUTCOMES = {
    AuditClaimerWins: RepChallengeOutcomeVerified,
    AuditAuditorWins: RepChallengeOutcomeFalsified,
    AuditInconclusive: RepChallengeOutcomeInconclusive,
}

# claim_type of the Module-1 claim of an escalation.
ESCALATION_CLAIM_TYPE = b"reputation_challenge"

# stake_id in the datum of a seed UTxO. CreateStake decodes the seed datum as
# a StakeDatum but does not read its stake_id: it writes the output reference
# of the seed in the new stake.
SEED_STAKE_ID = OutputReference(transaction_id=bytes(32), output_index=0)

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


def _ref_text(ref: OutputReference) -> str:
    """An OutputReference as "tx_hash#index"."""
    return f"{bytes(ref.transaction_id).hex()}#{ref.output_index}"


def _parse_ref(text: str) -> OutputReference:
    """An OutputReference from "tx_hash#index". Raises ValueError."""
    try:
        tx_hash, index = text.split("#")
        tx_id = bytes.fromhex(tx_hash)
        output_index = int(index)
    except (AttributeError, ValueError):
        raise ValueError(f"{text!r} is not an output reference (tx_hash#index)") from None
    if len(tx_id) != 32 or output_index < 0:
        raise ValueError(f"{text!r} is not an output reference (tx_hash#index)")
    return OutputReference(transaction_id=tx_id, output_index=output_index)


def _policy_tokens(utxo: UTxO, policy: ScriptHash) -> dict:
    """{asset name bytes: quantity} of one policy in the value of `utxo`."""
    multi_asset = getattr(utxo.output.amount, "multi_asset", None)
    if not multi_asset or policy not in multi_asset:
        return {}
    return {bytes(name.payload): int(q) for name, q in multi_asset[policy].items() if int(q) != 0}


def escalation_deadline(params: ProtocolParamsInfo, created_at: int) -> int:
    """The last POSIX ms upper bound of an EscalateToAudit transaction:
    created_at + challenge_response_deadline + escalation_window."""
    return created_at + params.challenge_response_deadline + params.escalation_window


def challenge_timeout(params: ProtocolParamsInfo, created_at: int) -> int:
    """The POSIX ms time after which an unresolved challenge can expire
    (challenge_validation.challenge_timeout): the escalation deadline plus
    one activity window (activity_window × epoch_length). ExpireChallenge
    needs a lower bound after it."""
    return escalation_deadline(params, created_at) + params.activity_window * params.epoch_length


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


def _challenge_token_name(inner: ReputationChallengeDatum) -> bytes:
    """The challenge token name of a challenge (also the name of its lock in
    the target's stake): rchl_ + blake2b_256(challenger_did ++ target_did ++
    challenged_capability)[0:24]."""
    return derive_token_name(
        CHALLENGE_PREFIX,
        bytes(inner.challenger_did) + bytes(inner.target_did) + bytes(inner.challenged_capability),
        CHALLENGE_SLICE_LEN,
    )


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

    def stakes_of(self, agent_did: AgentRef) -> List[Tuple[UTxO, StakeDatum]]:
        """The stakes of an agent: the UTxOs at the reputation address that
        hold the agent's stake token with quantity 1, with their datums. An
        agent can have more than one stake; stake_id tells them apart. A
        fully decayed stake (stake_amount 0) is a stake."""
        agent_id = vi.normalize_agent_id(agent_did)
        token = AssetName(bytes.fromhex(derive_stake_token_name(agent_id.hex())))
        return [
            (utxo, decode_datum(StakeDatum, utxo.output.datum))
            for utxo in self._utxos_at(self.reputation_addr)
            if _token_quantity(utxo, self.rep_policy, token) == 1 and utxo.output.datum is not None
        ]

    def find_stake_utxo(self, agent_did: AgentRef) -> Optional[UTxO]:
        """The agent's stake with the largest stake_amount (the stake that
        an opening reads unless the caller names one), or None."""
        stakes = self.stakes_of(agent_did)
        if not stakes:
            return None
        return max(stakes, key=lambda s: (s[1].stake_amount, _ref_text(s[1].stake_id)))[0]

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

    def _target_stake(
        self, target_id: bytes, stake_id: Optional[str] = None,
    ) -> Tuple[UTxO, StakeDatum]:
        """The target's stake that an opening reads: the stake with
        `stake_id` ("tx_hash#index") when given, else the stake with the
        largest stake_amount. Raises RuntimeError when there is none."""
        if stake_id is not None:
            return self._bound_stake(target_id, _parse_ref(stake_id))
        utxo = self.find_stake_utxo(target_id)
        if utxo is None:
            raise RuntimeError(f"{vi.did_from_id(target_id)} has no stake UTxO")
        return utxo, decode_datum(StakeDatum, utxo.output.datum)

    def _bound_stake(self, target_id: bytes, stake_id: OutputReference) -> Tuple[UTxO, StakeDatum]:
        """The stake of the target with `stake_id` (`utils.bound_stake`): the
        bound stake of an endorsement. Raises RuntimeError when it is not on
        chain."""
        for utxo, stake in self.stakes_of(target_id):
            if stake.stake_id == stake_id:
                return utxo, stake
        raise RuntimeError(
            f"{vi.did_from_id(target_id)} has no stake with stake_id {_ref_text(stake_id)}"
        )

    def _locked_stake(self, target_id: bytes, lock: bytes) -> Tuple[UTxO, StakeDatum]:
        """The stake of the target that the challenge with the token name
        `lock` locks (its challenge_locks hold the name). Raises ValueError
        when no stake of the target holds the lock."""
        for utxo, stake in self.stakes_of(target_id):
            if lock in {bytes(n) for n in stake.challenge_locks}:
                return utxo, stake
        raise ValueError(
            f"no stake of {vi.did_from_id(target_id)} holds the lock of the challenge "
            f"token {lock.hex()}"
        )

    def _require_declared_wallet(self, record: vi.IdentityRecord) -> None:
        """The wallet key is the payout credential that an opening stores.
        The contract accepts only a declared credential of the acting
        agent's record: the controller key or a listed key with the
        transaction purpose. Raises IdentityError otherwise."""
        vi.require_declared_credential(record, self._vkey_hash_bytes())

    # ── Payee rule ───────────────────────────────────────────────────────

    def _record(self, agent_id: bytes, utxos: Optional[List[UTxO]] = None) -> vi.IdentityRecord:
        """The record of an agent with any status (also a tombstone). Raises
        RecordNotFoundError."""
        record = self.find_identity_record(agent_id, utxos)
        if record is None:
            raise vi.RecordNotFoundError(agent_id)
        return record

    @staticmethod
    def _record_payee(record: vi.IdentityRecord, stored: Credential) -> Credential:
        """`utils.record_payee`: `stored` while the record declares it, else
        the controller key of the record."""
        if isinstance(stored, VerificationKeyCredential) and vi.declared_credential(
            record.datum, bytes(stored.vkey_hash)
        ):
            return stored
        return VerificationKeyCredential(vi.controller_key_hash(record.datum))

    def payee(self, agent_did: AgentRef, stored: Credential) -> Credential:
        """The payee of a payout to a position of `agent_did` whose stored
        credential is `stored` (the payee rule, `utils.payee`): `stored` when
        the agent's record still declares it (the controller key, or a listed
        key with the transaction purpose), else
        VerificationKeyCredential(blake2b_224(record.controller)). The record
        can have any status. A script credential is never declared. Raises
        RecordNotFoundError when the agent has no record."""
        return self._record_payee(self._record(vi.normalize_agent_id(agent_did)), stored)

    def _payout(
        self, agent_id: bytes, stored: Credential, utxos: Optional[List[UTxO]] = None,
    ) -> Tuple[Credential, vi.IdentityRecord]:
        """The payee of a payout to a position of `agent_id` and the record
        that the payout transaction has as a reference input."""
        record = self._record(agent_id, utxos)
        return self._record_payee(record, stored), record

    def _exit_record(
        self, agent_id: bytes, stored: Credential, controller_skey: Optional[PaymentSigningKey],
    ) -> Tuple[Credential, vi.IdentityRecord, PaymentSigningKey]:
        """An exit (`utils.exit_paid`): the record of the agent (any status),
        the signing key of its controller, and the payee of the payout."""
        payee, record = self._payout(agent_id, stored)
        return payee, record, self._controller_key(record, controller_skey)

    # ── Stake Operations ─────────────────────────────────────────────────

    def build_create_seed_utxo(
        self, agent_did: AgentRef, capabilities: list, min_lovelace: int = 2_000_000
    ) -> Transaction:
        """A seed UTxO at the reputation address for a later CreateStake.
        CreateStake reads agent_did from its datum. The datum is a StakeDatum
        (8 fields); its stake_id is SEED_STAKE_ID, which CreateStake does not
        read (the new stake gets the output reference of the seed)."""
        datum = StakeDatum(
            agent_did=vi.normalize_agent_id(agent_did),
            owner_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
            stake_amount=min_lovelace,
            staked_capabilities=IndefiniteList(_capability_bytes(capabilities)),
            last_updated=slot_to_posix_ms(self._tip_slot()),
            history_points=0,
            challenge_locks=IndefiniteList([]),
            stake_id=SEED_STAKE_ID,
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
        last_updated is the upper bound; challenge_locks is empty; stake_id is
        the output reference of the seed. The seed holds no reputation
        token."""
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
            stake_id=_out_ref(seed),
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

    def build_decrease_stake(
        self,
        agent_did: AgentRef,
        amount: int,
        controller_skey: Optional[PaymentSigningKey] = None,
        *,
        stake_id: Optional[str] = None,
    ) -> Transaction:
        """DecreaseStake (an exit): take `amount` AP3X out of a stake of the
        agent (the stake with `stake_id`, "tx_hash#index", else the largest
        stake).

        The record of the agent is a reference input (any status, also a
        tombstone) and its controller signs (`controller_skey` when it is not
        the wallet key). The payee of owner_credential (`payee`) receives
        `amount`. The continuing stake keeps its address, tokens and datum,
        with stake_amount − amount (at least min_self_stake) and last_updated
        = the upper bound of the validity range. The stake has no challenge
        lock, and its stake_cooldown has passed at the lower bound."""
        agent_id = vi.normalize_agent_id(agent_did)
        if amount <= 0:
            raise ValueError("DecreaseStake needs a positive amount")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        stake_utxo, stake = self._target_stake(agent_id, stake_id)
        remaining = stake.stake_amount - amount
        if remaining < params.min_self_stake:
            raise ValueError(
                f"the stake keeps {remaining}, below min_self_stake {params.min_self_stake}"
            )
        if list(stake.challenge_locks):
            raise ValueError(
                f"a challenge locks the stake of {vi.did_from_id(agent_id)}: it cannot "
                f"decrease until the challenge closes"
            )
        ready_at = stake.last_updated + params.stake_cooldown
        if ready_at > window.lo:
            raise ValueError(
                f"the stake changed at {stake.last_updated} (POSIX ms); the stake_cooldown "
                f"ends at {ready_at}, after the lower bound {window.lo}"
            )
        payee, record, controller = self._exit_record(
            agent_id, stake.owner_credential, controller_skey,
        )
        decreased = dataclasses.replace(stake, stake_amount=remaining, last_updated=window.hi)

        builder = self._new_builder(window)
        builder.add_script_input(
            stake_utxo, script=self.rep_ref_utxo, redeemer=Redeemer(DecreaseStakeRedeemer(amount)),
        )
        builder.add_output(
            self._continuing_stake(stake_utxo, decreased, int(stake_utxo.output.amount.coin) - amount)
        )
        builder.add_output(self._output(credential_address(payee), amount))
        self._add_reference_inputs(builder, [record.utxo, params_utxo, self.cross_refs_utxo])
        return self._finish(builder, [controller], [vi.controller_key_hash(record.datum)])

    def decrease_stake(
        self,
        agent_did: AgentRef,
        amount: int,
        controller_skey: Optional[PaymentSigningKey] = None,
        *,
        stake_id: Optional[str] = None,
        wait: bool = True,
    ) -> str:
        """Take AP3X out of a stake (see build_decrease_stake).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_decrease_stake(agent_did, amount, controller_skey, stake_id=stake_id),
            wait,
        )
        logger.info("DecreaseStake tx: %s", tx_hash)
        return tx_hash

    # ── Endorsement Operations ───────────────────────────────────────────

    def build_mint_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        capabilities: list,
        stake_amount: int,
        controller_skey: Optional[PaymentSigningKey] = None,
        *,
        target_stake: Optional[str] = None,
    ) -> Transaction:
        """MintEndorsementToken: the endorser's record is Active and its
        controller and the wallet key (endorser_credential, a declared
        credential of the record) sign; the target's record is Active and
        lists each endorsed capability; the stake of the target that the
        endorsement reads (the stake with stake_id `target_stake`,
        "tx_hash#index", else the target's largest stake) is a reference
        input and backs each endorsed capability; the endorsement stores its
        stake_id in target_stake (the bound stake); created_at is the lower
        bound; slashed_by is empty."""
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

        stake_utxo, stake = self._target_stake(target_id, target_stake)
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
                target_stake=stake.stake_id,
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
        *,
        target_stake: Optional[str] = None,
    ) -> str:
        """Mint an endorsement from one agent to another (see
        build_mint_endorsement).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_mint_endorsement(
                endorser_did, target_did, capabilities, stake_amount, controller_skey,
                target_stake=target_stake,
            ),
            wait,
        )
        logger.info("MintEndorsement tx: %s", tx_hash)
        return tx_hash

    def build_withdraw_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Transaction:
        """WithdrawEndorsement (an exit): spend the endorsement, burn its
        token and pay its stake_amount to the payee of endorser_credential
        (`payee`).

        The record of the endorser is a reference input (any status, also a
        tombstone) and its controller signs (`controller_skey` when it is not
        the wallet key). The bound stake (the stake of the target whose
        stake_id is target_stake) is a reference input, and no challenge
        locks it: the withdrawal waits until every challenge against that
        stake closes. endorsement_cooldown has passed since created_at at the
        lower bound of the validity range."""
        endorser_id = vi.normalize_agent_id(endorser_did)
        target_id = vi.normalize_agent_id(target_did)
        endorsement_utxo = self.find_endorsement_utxo(endorser_id, target_id)
        if endorsement_utxo is None:
            raise RuntimeError("Endorsement UTxO not found on-chain")
        endorsement = decode_datum(
            EndorsementValidatorDatumEndorsement, endorsement_utxo.output.datum
        ).datum
        window = self.tx_window()
        params_utxo, params = self.params_record()
        ready_at = endorsement.created_at + params.endorsement_cooldown
        if ready_at > window.lo:
            raise ValueError(
                f"the endorsement was created at {endorsement.created_at} (POSIX ms); the "
                f"endorsement_cooldown ends at {ready_at}, after the lower bound {window.lo}"
            )
        stake_utxo, stake = self._bound_stake(target_id, endorsement.target_stake)
        if list(stake.challenge_locks):
            raise ValueError(
                f"a challenge locks the bound stake {_ref_text(stake.stake_id)} of the "
                f"endorsement: the withdrawal waits until every challenge against that "
                f"stake closes"
            )
        payee, record, controller = self._exit_record(
            endorser_id, endorsement.endorser_credential, controller_skey,
        )
        token = AssetName(
            bytes.fromhex(derive_endorsement_token_name(endorser_id.hex(), target_id.hex()))
        )

        builder = self._new_builder(window)
        builder.add_script_input(
            endorsement_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(EndorsementSpendRedeemer(action=WithdrawEndorsementRedeemer())),
        )
        builder.mint = MultiAsset({self.end_policy: Asset({token: -1})})
        builder.add_minting_script(
            self.end_ref_utxo, redeemer=Redeemer(BurnEndorsementTokenRedeemer()),
        )
        builder.add_output(self._output(credential_address(payee), endorsement.stake_amount))
        self._add_reference_inputs(
            builder, [record.utxo, stake_utxo, params_utxo, self.cross_refs_utxo],
        )
        return self._finish(builder, [controller], [vi.controller_key_hash(record.datum)])

    def withdraw_endorsement(
        self,
        endorser_did: AgentRef,
        target_did: AgentRef,
        controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> str:
        """Withdraw an endorsement (see build_withdraw_endorsement).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_withdraw_endorsement(endorser_did, target_did, controller_skey), wait,
        )
        logger.info("WithdrawEndorsement tx: %s", tx_hash)
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

        The target's record is not read: the target's stake decides (the
        target's stake with the largest stake_amount). It lists the
        capability, and target_credential is its owner_credential. The
        transaction spends the stake with LockForChallenge; the continuing
        stake keeps its address, value and datum (also stake_id), with the
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

    def _release_lock(
        self, builder: TransactionBuilder, challenge_utxo: UTxO, inner: ReputationChallengeDatum,
    ) -> AssetName:
        """Spend the target's stake that the challenge locks with
        ReleaseChallenge: the continuing stake keeps its address, value and
        datum, without the lock of the challenge. Returns the challenge token
        (the caller burns it). Raises ValueError when no stake of the target
        holds the lock."""
        lock = _challenge_token_name(inner)
        stake_utxo, stake = self._locked_stake(bytes(inner.target_did), lock)
        builder.add_script_input(
            stake_utxo,
            script=self.rep_ref_utxo,
            redeemer=Redeemer(ReleaseChallengeRedeemer(challenge_ref=_out_ref(challenge_utxo))),
        )
        released = dataclasses.replace(stake, challenge_locks=_without_lock(stake, lock))
        builder.add_output(self._continuing_stake(stake_utxo, released))
        return AssetName(lock)

    def _burn_challenge_token(self, builder: TransactionBuilder, token: AssetName) -> None:
        builder.mint = MultiAsset({self.end_policy: Asset({token: -1})})
        builder.add_minting_script(
            self.end_ref_utxo, redeemer=Redeemer(BurnChallengeTokenRedeemer()),
        )

    def build_withdraw_challenge(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        controller_skey: Optional[PaymentSigningKey] = None,
    ) -> Transaction:
        """WithdrawChallenge (an exit): the challenger closes an Open
        challenge without a response. The upper bound of the validity range
        is before created_at + challenge_response_deadline.

        The challenge token is burned and the payee of challenger_credential
        (`payee`) receives stake_amount. The target's stake that the
        challenge locks continues without the lock (ReleaseChallenge). The
        record of the challenger is a reference input (any status, also a
        tombstone) and its controller signs (`controller_skey` when it is not
        the wallet key)."""
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if not isinstance(inner.state, RepChallengeStateOpen):
            raise ValueError("WithdrawChallenge needs a challenge in state Open")
        if inner.response_submitted_at != 0:
            raise ValueError(
                "the target responded: WithdrawChallenge needs a challenge without a response"
            )
        window = self.tx_window()
        params_utxo, params = self.params_record()
        deadline = inner.created_at + params.challenge_response_deadline
        if window.hi >= deadline:
            raise ValueError(
                f"the response deadline is {deadline} (POSIX ms): WithdrawChallenge needs an "
                f"upper bound before it, not {window.hi}"
            )
        payee, record, controller = self._exit_record(
            bytes(inner.challenger_did), inner.challenger_credential, controller_skey,
        )

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=WithdrawChallengeRedeemer())),
        )
        self._burn_challenge_token(builder, self._release_lock(builder, challenge_utxo, inner))
        builder.add_output(self._output(credential_address(payee), inner.stake_amount))
        self._add_reference_inputs(builder, [record.utxo, params_utxo, self.cross_refs_utxo])
        return self._finish(builder, [controller], [vi.controller_key_hash(record.datum)])

    def withdraw_challenge(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        controller_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> str:
        """Withdraw a challenge (see build_withdraw_challenge).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_withdraw_challenge(challenger_did, target_did, capability, controller_skey),
            wait,
        )
        logger.info("WithdrawChallenge tx: %s", tx_hash)
        return tx_hash

    def build_expire_challenge(
        self, challenger_did: AgentRef, target_did: AgentRef, capability: str,
    ) -> Transaction:
        """ExpireChallenge (permissionless): close a challenge that nobody
        resolved before the challenge timeout (`challenge_timeout`). The
        challenge is Open with a response, or Escalated; DefaultJudgment
        closes an Open challenge without a response. The lower bound of the
        validity range is after the timeout.

        The challenge token is burned and the payee of challenger_credential
        (`payee`) receives stake_amount (no fee). The challenger's record is
        a reference input (any status). The target's stake that the
        challenge locks continues without the lock (ReleaseChallenge) and
        keeps all its AP3X. Only the wallet signs."""
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if isinstance(inner.state, RepChallengeStateResolved):
            raise ValueError(
                "the challenge is Resolved: use distribute_outcome or "
                "distribute_falsified_outcome"
            )
        if isinstance(inner.state, RepChallengeStateOpen) and inner.response_submitted_at <= 0:
            raise ValueError(
                "an Open challenge without a response closes with DefaultJudgment, not "
                "with ExpireChallenge"
            )
        window = self.tx_window()
        params_utxo, params = self.params_record()
        timeout = challenge_timeout(params, inner.created_at)
        if window.lo <= timeout:
            raise ValueError(
                f"the challenge times out at {timeout} (POSIX ms): ExpireChallenge needs a "
                f"lower bound after it, not {window.lo}"
            )
        payee, record = self._payout(bytes(inner.challenger_did), inner.challenger_credential)

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=ExpireChallengeRedeemer())),
        )
        self._burn_challenge_token(builder, self._release_lock(builder, challenge_utxo, inner))
        builder.add_output(self._output(credential_address(payee), inner.stake_amount))
        self._add_reference_inputs(builder, [record.utxo, params_utxo, self.cross_refs_utxo])
        return self._finish(builder, [], [self._vkey_hash_bytes()])

    def expire_challenge(
        self, challenger_did: AgentRef, target_did: AgentRef, capability: str, wait: bool = True,
    ) -> str:
        """Expire a challenge after the challenge timeout (see
        build_expire_challenge).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_expire_challenge(challenger_did, target_did, capability), wait,
        )
        logger.info("ExpireChallenge tx: %s", tx_hash)
        return tx_hash

    def build_escalate_to_audit(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        claim_stake: int,
        challenge_window_ms: int,
        claim_script_ref: str,
        *,
        storage_uri: str = "",
        claimer_skey: Optional[PaymentSigningKey] = None,
    ) -> Tuple[Transaction, bytes]:
        """EscalateToAudit: move an Open challenge with a response to
        Module-1. The upper bound of the validity range is at most
        `escalation_deadline` (created_at + challenge_response_deadline +
        escalation_window).

        The transaction mints the one Module-1 claim token (policy
        module1_claim_validator_hash, ClaimAction.SubmitClaim with the
        reference script at `claim_script_ref`, the `claim_ref` of the
        Module-1 deployment) and creates the Module-1 claim output at the
        exact Module-1 claim address. The claim holds exactly `claim_stake`
        lovelace (the wallet pays it) and the claim token. Its ClaimDatum
        names the target as claimer (Module-1 ClaimerWins maps to
        CapabilityVerified), with claim_hash = blake2b_256(evidence_hash ++
        counter_evidence_hash), claim_type "reputation_challenge",
        submitted_at = the lower bound and `challenge_window_ms` (within the
        bounds of the Module-1 ProtocolParams). The claim token name comes
        from the first input of the transaction (Module-1
        claim_token_name). The challenge continues at its address with its
        value and Escalated { audit_claim_token }.

        Signatures: the claimer key (`claimer_skey`, else the wallet key)
        is a declared credential of the target's record, which is Active
        over the validity range (the Module-1 claim rules); the transaction
        also needs the signature of challenger_credential or
        target_credential (the wallet key or the claimer key).

        Returns:
            (transaction, the asset name of the claim token)
        """
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if not isinstance(inner.state, RepChallengeStateOpen):
            raise ValueError("EscalateToAudit needs a challenge in state Open")
        if inner.response_submitted_at <= 0:
            raise ValueError(
                "EscalateToAudit needs the response of the target (both parties have evidence)"
            )
        if claim_stake <= 0 or challenge_window_ms <= 0:
            raise ValueError("claim_stake and challenge_window_ms must be positive")
        window = self.tx_window()
        params_utxo, params = self.params_record()
        deadline = escalation_deadline(params, inner.created_at)
        if window.hi > deadline:
            raise ValueError(
                f"the escalation window ends at {deadline} (POSIX ms): EscalateToAudit needs "
                f"an upper bound at or before it, not {window.hi}"
            )
        target_id = bytes(inner.target_did)
        claimer = claimer_skey or self.skey
        claimer_hash = _key_hash(claimer)
        chain = self._active_chain(target_id, window, ())
        vi.require_declared_credential(chain[0], claimer_hash)
        signers = {self._vkey_hash_bytes(), claimer_hash}
        if not any(
            isinstance(c, VerificationKeyCredential) and bytes(c.vkey_hash) in signers
            for c in (inner.challenger_credential, inner.target_credential)
        ):
            raise ValueError(
                "EscalateToAudit needs the signature of challenger_credential or "
                "target_credential: neither is the wallet key or the claimer key"
            )
        claim_hash_hex = self.deployment.module1_claim_validator_hash
        claim_ref_utxo = self._reference(claim_script_ref)
        script = claim_ref_utxo.output.script
        if script is None or plutus_script_hash(script).payload.hex() != claim_hash_hex:
            raise ValueError(
                f"the reference script at {claim_script_ref} is not the Module-1 claim "
                f"validator {claim_hash_hex} of the deployment"
            )
        self._protected_refs.add(claim_script_ref)
        claim_policy = ScriptHash(bytes.fromhex(claim_hash_hex))

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=EscalateToAuditRedeemer())),
        )
        seed = min(builder.inputs, key=lambda u: (bytes(u.input.transaction_id), u.input.index))
        claim_token = bytes.fromhex(
            derive_audit_claim_token_name(bytes(seed.input.transaction_id).hex(), seed.input.index)
        )
        token = MultiAsset({claim_policy: Asset({AssetName(claim_token): 1})})
        claim_output = TransactionOutput(
            Address.from_primitive(script_hash_to_address(claim_hash_hex)),
            Value(claim_stake, deepcopy(token)),
            datum=AuditClaimDatum(
                claimer_did=target_id,
                claimer_credential=VerificationKeyCredential(claimer_hash),
                claim_hash=vi.blake2b_256(
                    bytes(inner.evidence_hash) + bytes(inner.counter_evidence_hash)
                ),
                claim_type=ESCALATION_CLAIM_TYPE,
                storage_uri=storage_uri.encode("utf-8"),
                stake_amount=claim_stake,
                submitted_at=window.lo,
                challenge_window=challenge_window_ms,
                state=AuditOpen(),
            ),
        )
        minimum = min_lovelace_post_alonzo(claim_output, self.context)
        if claim_stake < minimum:
            raise ValueError(
                f"claim_stake {claim_stake} is below the minimum lovelace {minimum} of the "
                f"claim output (the Module-1 claim holds exactly its stake)"
            )
        amount = challenge_utxo.output.amount
        builder.add_output(
            TransactionOutput(
                challenge_utxo.output.address,
                Value(amount.coin, deepcopy(amount.multi_asset)),
                datum=EndorsementValidatorDatumChallenge(
                    datum=dataclasses.replace(
                        inner, state=RepChallengeStateEscalated(audit_claim_token=claim_token),
                    )
                ),
            )
        )
        builder.add_output(claim_output)
        builder.mint = token
        builder.add_minting_script(claim_ref_utxo, redeemer=Redeemer(AuditSubmitClaimRedeemer()))
        self._add_reference_inputs(
            builder, [r.utxo for r in chain] + [params_utxo, self.cross_refs_utxo],
        )
        tx = self._finish(builder, [claimer], [self._vkey_hash_bytes(), claimer_hash])
        return tx, claim_token

    def escalate_to_audit(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        claim_stake: int,
        challenge_window_ms: int,
        claim_script_ref: str,
        *,
        storage_uri: str = "",
        claimer_skey: Optional[PaymentSigningKey] = None,
        wait: bool = True,
    ) -> Tuple[str, str]:
        """Escalate a challenge to Module-1 (see build_escalate_to_audit).

        Returns:
            (transaction hash, claim token name hex). The claim is at
            {tx_hash}#1.
        """
        tx, claim_token = self.build_escalate_to_audit(
            challenger_did, target_did, capability, claim_stake, challenge_window_ms,
            claim_script_ref, storage_uri=storage_uri, claimer_skey=claimer_skey,
        )
        tx_hash = self._submit(tx, wait)
        logger.info("EscalateToAudit tx: %s (claim token %s)", tx_hash, claim_token.hex())
        return tx_hash, claim_token.hex()

    def _audit_challenge_of(self, utxo: UTxO, claim_token: bytes) -> Optional[AuditChallengeDatum]:
        """The datum of a Module-1 challenge of the claim with the token
        `claim_token`: the UTxO is at the exact Module-1 challenge address,
        holds exactly one token of the Module-1 challenge policy (quantity 1)
        and its datum names the claim token. Else None."""
        hash_hex = self.deployment.module1_challenge_validator_hash
        if str(utxo.output.address) != str(Address.from_primitive(script_hash_to_address(hash_hex))):
            return None
        tokens = _policy_tokens(utxo, ScriptHash(bytes.fromhex(hash_hex)))
        if list(tokens.values()) != [1] or utxo.output.datum is None:
            return None
        try:
            datum = decode_datum(AuditChallengeDatum, utxo.output.datum)
        except Exception:  # another datum shape: not a Module-1 challenge
            return None
        return datum if bytes(datum.claim_token) == claim_token else None

    def find_audit_challenge(self, claim_token: bytes) -> Optional[Tuple[UTxO, AuditChallengeDatum]]:
        """The Module-1 challenge of the claim with the token `claim_token`
        (see `_audit_challenge_of`), or None."""
        address = Address.from_primitive(
            script_hash_to_address(self.deployment.module1_challenge_validator_hash)
        )
        for utxo in self._utxos_at(address):
            datum = self._audit_challenge_of(utxo, claim_token)
            if datum is not None:
                return utxo, datum
        return None

    def build_resolve_escalation(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        audit_challenge_ref: Optional[str] = None,
    ) -> Transaction:
        """ResolveEscalation (permissionless): an Escalated
        { audit_claim_token } challenge takes the verdict of the Module-1
        challenge of that claim.

        The Module-1 challenge (`audit_challenge_ref`, else
        `find_audit_challenge`) is a reference input at the exact Module-1
        challenge address with exactly one Module-1 challenge token,
        claim_token == audit_claim_token and the state AuditResolved; its
        auditor is the challenger or the target. ClaimerWins gives
        CapabilityVerified, AuditorWins CapabilityFalsified and
        AuditInconclusive Inconclusive. The challenge continues at its
        address with its value and fields and the state Resolved { outcome }.
        Only the wallet signs."""
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if not isinstance(inner.state, RepChallengeStateEscalated):
            raise ValueError("ResolveEscalation needs a challenge in state Escalated")
        claim_token = bytes(inner.state.audit_claim_token)
        if audit_challenge_ref is None:
            found = self.find_audit_challenge(claim_token)
            if found is None:
                raise RuntimeError(
                    f"no Module-1 challenge names the claim token {claim_token.hex()}"
                )
            audit_utxo, audit = found
        else:
            audit_utxo = self._resolve_utxo(audit_challenge_ref)
            audit = self._audit_challenge_of(audit_utxo, claim_token)
            if audit is None:
                raise ValueError(
                    f"{audit_challenge_ref} is not a Module-1 challenge of the claim token "
                    f"{claim_token.hex()}: it must be at the Module-1 challenge address, "
                    f"hold one Module-1 challenge token and name the claim token"
                )
        if not isinstance(audit.state, AuditResolved):
            raise ValueError("the Module-1 challenge is not resolved (AuditResolved)")
        if bytes(audit.auditor_did) not in (bytes(inner.challenger_did), bytes(inner.target_did)):
            raise ValueError(
                "the auditor of the Module-1 challenge is neither the challenger nor the target"
            )
        outcome = AUDIT_OUTCOMES[type(audit.state.verdict)]()
        window = self.tx_window()
        params_utxo, _ = self.params_record()

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=ResolveEscalationRedeemer())),
        )
        amount = challenge_utxo.output.amount
        builder.add_output(
            TransactionOutput(
                challenge_utxo.output.address,
                Value(amount.coin, deepcopy(amount.multi_asset)),
                datum=EndorsementValidatorDatumChallenge(
                    datum=dataclasses.replace(inner, state=RepChallengeStateResolved(outcome=outcome))
                ),
            )
        )
        self._add_reference_inputs(builder, [audit_utxo, params_utxo, self.cross_refs_utxo])
        return self._finish(builder, [], [self._vkey_hash_bytes()])

    def resolve_escalation(
        self,
        challenger_did: AgentRef,
        target_did: AgentRef,
        capability: str,
        audit_challenge_ref: Optional[str] = None,
        wait: bool = True,
    ) -> str:
        """Resolve an escalated challenge from its Module-1 verdict (see
        build_resolve_escalation).

        Returns:
            Transaction hash.
        """
        tx_hash = self._submit(
            self.build_resolve_escalation(challenger_did, target_did, capability, audit_challenge_ref),
            wait,
        )
        logger.info("ResolveEscalation tx: %s", tx_hash)
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

        Burns the challenge token and spends the target's stake that the
        challenge locks with ReleaseChallenge: the continuing stake keeps its
        address, value and datum, without the lock of this challenge. fee =
        stake × protocol_fee_rate. CapabilityVerified pays the challenge
        stake less the fee to the payee of target_credential and the fee to
        the treasury. Inconclusive pays the stake less half of the fee to the
        payee of challenger_credential and that half to the treasury; the
        target's stake keeps all its AP3X. The record of the payee's agent
        is a reference input (the payee rule, `payee`; any status). The
        wallet pays the transaction fee and the rest of the payouts.

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
        if verified:
            treasury_fee = fee
            payee, record = self._payout(bytes(inner.target_did), inner.target_credential)
        else:
            treasury_fee = fee // 2
            payee, record = self._payout(bytes(inner.challenger_did), inner.challenger_credential)

        builder = self._new_builder(window)
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(ChallengeSpendRedeemer(action=DistributeOutcomeRedeemer())),
        )
        challenge_token = self._release_lock(builder, challenge_utxo, inner)
        mint = MultiAsset({self.end_policy: Asset({challenge_token: -1})})
        builder.add_output(self._output(credential_address(payee), challenge_stake - treasury_fee))
        if treasury_fee > 0:
            builder.add_output(self._output(self.treasury_addr, treasury_fee))

        signing_keys: List[PaymentSigningKey] = []
        required = [self._vkey_hash_bytes()]
        references = [record.utxo, params_utxo, self.cross_refs_utxo]
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

        The resolved challenge and the bound stake of the endorsement (the
        stake of the target whose stake_id is target_stake) are reference
        inputs; the challenge locks the bound stake (its challenge_locks
        hold the challenge token name). slash = stake × slash_rate_endorser,
        fee = slash × protocol_fee_rate. The payee of challenger_credential
        gets slash − fee and the treasury gets the fee. When the remaining
        stake is at least min_endorsement, the endorsement continues at the
        same address with the same datum, stake_amount = remaining and the
        challenge token name added first to slashed_by. Else the endorsement
        token is burned and the payee of endorser_credential gets the
        remaining stake; when both payees are equal, one output holds both
        amounts. The record of each payee's agent is a reference input (the
        payee rule, `payee`). A challenge slashes an endorsement at most once
        (slashed_by). Only the wallet signs.
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
        challenge_token = _challenge_token_name(challenge)
        if challenge_token in {bytes(n) for n in endorsement.slashed_by}:
            raise ValueError("this challenge already slashed the endorsement")

        stake_utxo, stake = self._bound_stake(target_id, endorsement.target_stake)
        if challenge_token not in {bytes(n) for n in stake.challenge_locks}:
            raise ValueError(
                f"the challenge does not lock the bound stake {_ref_text(stake.stake_id)} of "
                f"the endorsement: a challenge slashes only the endorsements of the stake "
                f"that it locks"
            )
        window = self.tx_window()
        params_utxo, params = self.params_record()
        slash = endorsement.stake_amount * params.slash_rate_endorser // 10_000
        remaining = endorsement.stake_amount - slash
        fee = slash * params.protocol_fee_rate // 10_000
        continues = remaining >= params.min_endorsement
        token = AssetName(
            bytes.fromhex(derive_endorsement_token_name(endorser_id.hex(), target_id.hex()))
        )
        challenger_payee, challenger_record = self._payout(
            bytes(challenge.challenger_did), challenge.challenger_credential,
        )
        references = [challenge_utxo, stake_utxo, challenger_record.utxo]

        builder = self._new_builder(window)
        builder.add_script_input(
            endorsement_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(
                EndorsementSpendRedeemer(
                    action=SlashEndorsementRedeemer(challenge_ref=_out_ref(challenge_utxo))
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
        payouts: List[Tuple[Credential, int]] = []
        if challenger_due > 0:
            payouts.append((challenger_payee, challenger_due))
        if endorser_due > 0:
            endorser_payee, endorser_record = self._payout(
                endorser_id, endorsement.endorser_credential,
            )
            references.append(endorser_record.utxo)
            if payouts and payouts[0][0] == endorser_payee:
                payouts[0] = (endorser_payee, payouts[0][1] + endorser_due)
            else:
                payouts.append((endorser_payee, endorser_due))
        for payee, due in payouts:
            builder.add_output(self._output(credential_address(payee), due))
        if fee > 0:
            builder.add_output(self._output(self.treasury_addr, fee))

        self._add_reference_inputs(builder, references + [params_utxo, self.cross_refs_utxo])
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
        stake that the challenge locks (SlashStake), burns the challenge
        token, pays the payee of challenger_credential (`payee`; the
        challenger's record is a reference input) held + target_slash − fee
        (held = the AP3X that the challenge UTxO holds) and the treasury the
        fee, and continues the stake with stake_amount − target_slash
        (target_slash = stake_amount / number of staked capabilities),
        without the lock of this challenge and with the same last_updated (a
        slash is not an owner action). With `mint_bonus`, the transaction
        also mints the challenger's ChallengeWon bonus (see
        build_distribute_outcome).
        """
        challenge_utxo, inner = self.get_challenge(challenger_did, target_did, capability)
        if not isinstance(self.resolved_outcome(inner), RepChallengeOutcomeFalsified):
            raise ValueError("the outcome is not CapabilityFalsified: use distribute_outcome")
        target_id = bytes(inner.target_did)
        token_bytes = _challenge_token_name(inner)
        stake_utxo, stake = self._locked_stake(target_id, token_bytes)
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
        payee, record = self._payout(bytes(inner.challenger_did), inner.challenger_credential)

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
        builder.add_output(self._output(credential_address(payee), challenger_reward))
        if fee > 0:
            builder.add_output(self._output(self.treasury_addr, fee))
        builder.add_output(self._continuing_stake(stake_utxo, slashed, remaining))

        mint = MultiAsset({self.end_policy: Asset({challenge_token: -1})})
        signing_keys: List[PaymentSigningKey] = []
        required = [self._vkey_hash_bytes()]
        references = [record.utxo, params_utxo, self.cross_refs_utxo]
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
