"""
ReputationStakingClient — high-level API for Module 3 operations.

Uses PyCardano TransactionBuilder with Ogmios for remote chain interaction,
matching the patterns used by Module 1 and Module 6.

Usage (Ogmios/remote):
    from reputation_staking.ogmios_backend import create_context, load_wallet
    from reputation_staking.client import ReputationStakingClient

    context = create_context()
    skey, vkey, wallet_addr = load_wallet("wallet/payment.skey")
    client = ReputationStakingClient.from_deploy_state(
        "deploy/deploy_state.json", context, skey,
    )
    tx = client.create_stake("agent_did_hex", ["code_review"], 10_000_000)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Set

from pycardano import (
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    Redeemer,
    ScriptHash,
    TransactionBuilder,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.hash import VerificationKeyHash
from pycardano.serialization import IndefiniteList

from reputation_staking.constants import (
    OGMIOS_URL,
    REGISTRY_POLICY_ID,
    TX_SUBMIT_URL,
    TX_WAIT_SECONDS,
    TREASURY_SCRIPT_HASH,
)
from reputation_staking.ogmios_backend import (
    NETWORK,
    find_utxo_with_token,
    get_collateral_utxo,
    get_current_slot,
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
    MintChallengeTokenRedeemer,
    MintEndorsementTokenRedeemer,
    MintHistoryBonusRedeemer,
    MintStakeTokenRedeemer,
    OutputReference,
    RepChallengeOutcomeVerified,
    RepChallengeOutcomeFalsified,
    RepChallengeOutcomeInconclusive,
    RepChallengeStateOpen,
    RepChallengeStateResolved,
    ReputationChallengeDatum,
    ResolveChallengeRedeemer,
    SlashEndorsementRedeemer,
    SlashStakeRedeemer,
    StakeDatum,
    VerificationKeyCredential,
)
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from reputation_staking.utils import (
    script_hash_to_address,
    slot_to_posix_ms,
    vkey_hash_to_address,
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


class ReputationStakingClient:
    """High-level client for Module 3 Reputation Staking operations.

    Uses PyCardano TransactionBuilder for transaction construction and
    Ogmios for chain queries. Transactions are submitted via HTTP POST.
    """

    def __init__(
        self,
        context,
        skey: PaymentSigningKey,
        deploy_state: dict,
        ogmios_url: str = OGMIOS_URL,
        submit_url: str = TX_SUBMIT_URL,
    ):
        self.context = context
        self.skey = skey
        self.vkey = PaymentVerificationKey.from_signing_key(skey)
        self.wallet_addr = Address(
            payment_part=self.vkey.hash(), network=NETWORK
        )
        self.ogmios_url = ogmios_url
        self.submit_url = submit_url
        self.deploy = deploy_state

        # Validator hashes and addresses
        self.reputation_hash = deploy_state["reputation_validator_hash"]
        self.endorsement_hash = deploy_state["endorsement_validator_hash"]
        self.reputation_addr = Address.from_primitive(
            deploy_state["reputation_address"]
        )
        self.endorsement_addr = Address.from_primitive(
            deploy_state["endorsement_address"]
        )

        # ScriptHash objects for minting
        self.rep_policy = ScriptHash(bytes.fromhex(self.reputation_hash))
        self.end_policy = ScriptHash(bytes.fromhex(self.endorsement_hash))

        self.registry_addr = script_hash_to_address(REGISTRY_POLICY_ID)
        self.treasury_addr = Address.from_primitive(
            script_hash_to_address(TREASURY_SCRIPT_HASH)
        )

        # Reference script UTxO refs (resolved lazily)
        tx_hashes = deploy_state.get("tx_hashes", {})
        self._rep_ref_hash = tx_hashes.get("reputation_spend_ref")
        self._end_ref_hash = tx_hashes.get("endorsement_spend_ref")
        self._params_hash = tx_hashes.get("params_utxo")
        self._cross_refs_hash = tx_hashes.get("cross_refs_nft")

        # Resolved UTxO cache
        self._rep_ref_utxo: Optional[UTxO] = None
        self._end_ref_utxo: Optional[UTxO] = None
        self._params_utxo: Optional[UTxO] = None
        self._cross_refs_utxo: Optional[UTxO] = None

        # Protected UTxOs (reference scripts — don't spend these)
        self._protected_refs: Set[str] = set()
        for h in [self._rep_ref_hash, self._end_ref_hash, self._cross_refs_hash]:
            if h:
                self._protected_refs.add(f"{h}#0")

    @classmethod
    def from_deploy_state(
        cls,
        deploy_state_path: str,
        context,
        skey: PaymentSigningKey,
        ogmios_url: str = OGMIOS_URL,
        submit_url: str = TX_SUBMIT_URL,
    ) -> "ReputationStakingClient":
        """Create client from a deploy_state.json file."""
        with open(deploy_state_path) as f:
            deploy_state = json.load(f)
        return cls(context, skey, deploy_state, ogmios_url, submit_url)

    # ── Reference UTxO Resolution (lazy) ───────────────────────────────────

    def _resolve_ref(self, attr: str, tx_hash: Optional[str]) -> UTxO:
        """Resolve and cache a reference UTxO."""
        cached = getattr(self, attr, None)
        if cached:
            return cached
        if not tx_hash:
            raise RuntimeError(f"No tx hash for {attr} in deploy_state")
        utxo = resolve_utxo(tx_hash, 0, self.ogmios_url)
        setattr(self, attr, utxo)
        return utxo

    @property
    def rep_ref_utxo(self) -> UTxO:
        """Reputation validator reference script UTxO."""
        return self._resolve_ref("_rep_ref_utxo", self._rep_ref_hash)

    @property
    def end_ref_utxo(self) -> UTxO:
        """Endorsement validator reference script UTxO."""
        return self._resolve_ref("_end_ref_utxo", self._end_ref_hash)

    @property
    def params_utxo(self) -> UTxO:
        """Protocol parameters UTxO (reference input)."""
        return self._resolve_ref("_params_utxo", self._params_hash)

    @property
    def cross_refs_utxo(self) -> UTxO:
        """Cross-validator references NFT UTxO (reference input)."""
        return self._resolve_ref("_cross_refs_utxo", self._cross_refs_hash)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _vkey_hash_bytes(self) -> bytes:
        return bytes(self.vkey.hash())

    def _vkey_hash_hex(self) -> str:
        return bytes(self.vkey.hash()).hex()

    def _wallet_utxos(self) -> list:
        return get_wallet_utxos(self.context, self.wallet_addr, self._protected_refs)

    def _collateral(self) -> UTxO:
        return get_collateral_utxo(
            self.context, self.wallet_addr, self._protected_refs
        )

    def _current_slot(self) -> int:
        return get_current_slot(self.ogmios_url)

    def _new_builder(self) -> TransactionBuilder:
        """Create a TransactionBuilder with standard settings."""
        b = TransactionBuilder(self.context)
        b.fee_buffer = FEE_BUFFER
        return b

    def _add_wallet_inputs(self, builder: TransactionBuilder):
        """Add all spendable wallet UTxOs as inputs."""
        for u in self._wallet_utxos():
            builder.add_input(u)

    def _add_common_refs(self, builder: TransactionBuilder):
        """Add params + cross-refs as reference inputs."""
        builder.reference_inputs.add(self.params_utxo)
        builder.reference_inputs.add(self.cross_refs_utxo)

    def _sign_submit_wait(self, builder: TransactionBuilder, wait: bool = True) -> str:
        """Build, sign, submit, and optionally wait."""
        tx = builder.build_and_sign([self.skey], change_address=self.wallet_addr)
        tx_hash = submit_tx(tx, self.submit_url)
        if wait:
            wait_for_tx()
        return tx_hash

    # ── UTxO Finders ─────────────────────────────────────────────────────

    def find_agent_registry_utxo(self, agent_did: str) -> Optional[UTxO]:
        """Find the registry UTxO for a given agent DID."""
        return find_utxo_with_token(
            self.registry_addr, REGISTRY_POLICY_ID, agent_did,
            self.ogmios_url,
        )

    def find_stake_utxo(self, agent_did: str) -> Optional[UTxO]:
        """Find the stake UTxO for an agent."""
        token_name = derive_stake_token_name(agent_did)
        return find_utxo_with_token(
            str(self.reputation_addr), self.reputation_hash, token_name,
            self.ogmios_url,
        )

    def find_endorsement_utxo(
        self, endorser_did: str, target_did: str
    ) -> Optional[UTxO]:
        """Find an endorsement UTxO."""
        token_name = derive_endorsement_token_name(endorser_did, target_did)
        return find_utxo_with_token(
            str(self.endorsement_addr), self.endorsement_hash, token_name,
            self.ogmios_url,
        )

    def find_challenge_utxo(
        self, challenger_did: str, target_did: str, capability: str
    ) -> Optional[UTxO]:
        """Find a challenge UTxO."""
        token_name = derive_challenge_token_name(
            challenger_did, target_did, capability
        )
        return find_utxo_with_token(
            str(self.endorsement_addr), self.endorsement_hash, token_name,
            self.ogmios_url,
        )

    # ── Stake Operations ─────────────────────────────────────────────────

    def create_seed_utxo(
        self,
        agent_did: str,
        capabilities: list,
        min_lovelace: int = 2_000_000,
        wait: bool = True,
    ) -> str:
        """Send a seed UTxO to the reputation address (required before CreateStake).

        Returns:
            Transaction hash. The seed UTxO will be at {tx_hash}#0.
        """
        current_slot = self._current_slot()

        datum = StakeDatum(
            agent_did=bytes.fromhex(agent_did),
            owner_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
            stake_amount=min_lovelace,
            staked_capabilities=IndefiniteList(
                [cap.encode() for cap in capabilities]
            ),
            last_updated=slot_to_posix_ms(current_slot),
            history_points=0,
        )

        builder = self._new_builder()
        self._add_wallet_inputs(builder)
        builder.add_output(
            TransactionOutput(self.reputation_addr, min_lovelace, datum=datum)
        )

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("Seed UTxO tx: %s", tx_hash)
        return tx_hash

    def create_stake(
        self,
        agent_did: str,
        capabilities: list,
        stake_amount: int,
        seed_utxo: Optional[str] = None,
        wait: bool = True,
    ) -> str:
        """Create an initial self-stake for an agent.

        Args:
            agent_did: Agent's DID (NFT asset name hex).
            capabilities: List of capability strings.
            stake_amount: Amount in DFM (lovelace).
            seed_utxo: "txhash#idx" at reputation address to consume.
            wait: Whether to wait for confirmation.

        Returns:
            Transaction hash.
        """
        if not seed_utxo:
            raise ValueError("seed_utxo is required for CreateStake")

        current_slot = self._current_slot()
        caps_bytes = IndefiniteList([cap.encode() for cap in capabilities])

        # Token name
        stake_token_name = derive_stake_token_name(agent_did)
        token_an = AssetName(bytes.fromhex(stake_token_name))

        # Datum
        datum = StakeDatum(
            agent_did=bytes.fromhex(agent_did),
            owner_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
            stake_amount=stake_amount,
            staked_capabilities=caps_bytes,
            last_updated=slot_to_posix_ms(current_slot),
            history_points=0,
        )

        # Reference inputs
        agent_reg_utxo = self.find_agent_registry_utxo(agent_did)
        if not agent_reg_utxo:
            raise RuntimeError(f"Agent registry UTxO not found for {agent_did[:16]}...")

        # Resolve the seed UTxO
        parts = seed_utxo.split("#")
        seed = resolve_utxo(parts[0], int(parts[1]), self.ogmios_url)

        # Mint
        mint_ma = MultiAsset({self.rep_policy: Asset({token_an: 1})})
        out_ma = MultiAsset({self.rep_policy: Asset({token_an: 1})})

        # Build transaction
        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        # Spend seed UTxO with CreateStake redeemer
        builder.add_script_input(
            seed,
            script=self.rep_ref_utxo,
            redeemer=Redeemer(CreateStakeRedeemer()),
        )

        # Mint stake token
        builder.mint = mint_ma
        builder.add_minting_script(
            self.rep_ref_utxo,
            redeemer=Redeemer(MintStakeTokenRedeemer()),
        )

        # Output: stake UTxO with token + datum
        builder.add_output(
            TransactionOutput(
                self.reputation_addr,
                Value(stake_amount, out_ma),
                datum=datum,
            )
        )

        # Reference inputs
        builder.reference_inputs.add(agent_reg_utxo)
        self._add_common_refs(builder)

        # Signing + validity
        builder.required_signers = [self.vkey.hash()]
        builder.validity_start = current_slot
        builder.ttl = current_slot + 600

        # Collateral
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("CreateStake tx: %s", tx_hash)
        return tx_hash

    # ── Endorsement Operations ───────────────────────────────────────────

    def mint_endorsement(
        self,
        endorser_did: str,
        target_did: str,
        capabilities: list,
        stake_amount: int,
        wait: bool = True,
    ) -> str:
        """Mint an endorsement from one agent to another.

        Returns:
            Transaction hash.
        """
        current_slot = self._current_slot()

        token_name = derive_endorsement_token_name(endorser_did, target_did)
        token_an = AssetName(bytes.fromhex(token_name))

        # Reference inputs
        endorser_reg = self.find_agent_registry_utxo(endorser_did)
        target_reg = self.find_agent_registry_utxo(target_did)
        stake_utxo = self.find_stake_utxo(target_did)
        if not endorser_reg or not target_reg:
            raise RuntimeError("Agent registry UTxOs not found")
        if not stake_utxo:
            raise RuntimeError("Target agent stake UTxO not found")

        # Datum (wrapped: EndorsementValidatorDatum.Endorsement)
        inner = EndorsementDatum(
            endorser_did=bytes.fromhex(endorser_did),
            endorser_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
            target_did=bytes.fromhex(target_did),
            stake_amount=stake_amount,
            endorsed_capabilities=IndefiniteList(
                [cap.encode() for cap in capabilities]
            ),
            created_at=slot_to_posix_ms(current_slot),
        )
        datum = EndorsementValidatorDatumEndorsement(datum=inner)

        # Mint
        mint_ma = MultiAsset({self.end_policy: Asset({token_an: 1})})
        out_ma = MultiAsset({self.end_policy: Asset({token_an: 1})})

        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        builder.mint = mint_ma
        builder.add_minting_script(
            self.end_ref_utxo,
            redeemer=Redeemer(MintEndorsementTokenRedeemer()),
        )

        builder.add_output(
            TransactionOutput(
                self.endorsement_addr,
                Value(stake_amount, out_ma),
                datum=datum,
            )
        )

        builder.reference_inputs.add(endorser_reg)
        builder.reference_inputs.add(target_reg)
        builder.reference_inputs.add(stake_utxo)
        self._add_common_refs(builder)

        builder.required_signers = [self.vkey.hash()]
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("MintEndorsement tx: %s", tx_hash)
        return tx_hash

    # ── Challenge Operations ─────────────────────────────────────────────

    def mint_challenge(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        stake_amount: int,
        evidence_hash: str,
        evidence_uri: str,
        wait: bool = True,
    ) -> tuple:
        """Mint a challenge against a target agent's capability.

        Returns:
            Tuple of (tx_hash, challenge_datum). The datum is needed
            for resolve_challenge() and distribute_outcome().
        """
        current_slot = self._current_slot()

        token_name = derive_challenge_token_name(
            challenger_did, target_did, capability
        )
        token_an = AssetName(bytes.fromhex(token_name))

        # Reference inputs
        challenger_reg = self.find_agent_registry_utxo(challenger_did)
        target_reg = self.find_agent_registry_utxo(target_did)
        stake_utxo = self.find_stake_utxo(target_did)
        if not challenger_reg or not target_reg:
            raise RuntimeError("Agent registry UTxOs not found")
        if not stake_utxo:
            raise RuntimeError("Target agent stake UTxO not found")

        # Inner datum
        inner = ReputationChallengeDatum(
            challenger_did=bytes.fromhex(challenger_did),
            challenger_credential=VerificationKeyCredential(
                self._vkey_hash_bytes()
            ),
            target_did=bytes.fromhex(target_did),
            target_credential=VerificationKeyCredential(
                self._vkey_hash_bytes()
            ),
            challenged_capability=capability.encode(),
            stake_amount=stake_amount,
            evidence_hash=bytes.fromhex(evidence_hash),
            evidence_uri=evidence_uri.encode(),
            created_at=slot_to_posix_ms(current_slot),
            counter_evidence_hash=b"",
            counter_evidence_uri=b"",
            response_submitted_at=0,
            state=RepChallengeStateOpen(),
        )
        datum = EndorsementValidatorDatumChallenge(datum=inner)

        # Mint
        mint_ma = MultiAsset({self.end_policy: Asset({token_an: 1})})
        out_ma = MultiAsset({self.end_policy: Asset({token_an: 1})})

        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        builder.mint = mint_ma
        builder.add_minting_script(
            self.end_ref_utxo,
            redeemer=Redeemer(MintChallengeTokenRedeemer()),
        )

        builder.add_output(
            TransactionOutput(
                self.endorsement_addr,
                Value(stake_amount, out_ma),
                datum=datum,
            )
        )

        builder.reference_inputs.add(challenger_reg)
        builder.reference_inputs.add(target_reg)
        builder.reference_inputs.add(stake_utxo)
        self._add_common_refs(builder)

        builder.required_signers = [self.vkey.hash()]
        builder.validity_start = current_slot
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("MintChallenge tx: %s", tx_hash)
        return tx_hash, datum

    def resolve_challenge(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        outcome_constructor: int,
        challenge_datum,
        wait: bool = True,
    ) -> str:
        """Resolve a challenge (Foundation oracle in Phase 1.0).

        Args:
            outcome_constructor: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive
            challenge_datum: The original challenge datum (PlutusData or JSON dict).

        Returns:
            Transaction hash.
        """
        current_slot = self._current_slot()

        token_name = derive_challenge_token_name(
            challenger_did, target_did, capability
        )
        token_an = AssetName(bytes.fromhex(token_name))

        # Find the on-chain challenge UTxO
        challenge_utxo = self.find_challenge_utxo(
            challenger_did, target_did, capability
        )
        if not challenge_utxo:
            raise RuntimeError("Challenge UTxO not found on-chain")

        # Build resolved datum
        outcome_cls = OUTCOME_CLASSES.get(outcome_constructor)
        if not outcome_cls:
            raise ValueError(f"Invalid outcome constructor: {outcome_constructor}")

        # Extract inner datum and update state
        if isinstance(challenge_datum, EndorsementValidatorDatumChallenge):
            inner = challenge_datum.datum
        elif isinstance(challenge_datum, dict):
            # Legacy JSON format — rebuild as PlutusData
            inner = self._challenge_datum_from_json(challenge_datum)
        else:
            inner = challenge_datum

        resolved_inner = ReputationChallengeDatum(
            challenger_did=inner.challenger_did,
            challenger_credential=inner.challenger_credential,
            target_did=inner.target_did,
            target_credential=inner.target_credential,
            challenged_capability=inner.challenged_capability,
            stake_amount=inner.stake_amount,
            evidence_hash=inner.evidence_hash,
            evidence_uri=inner.evidence_uri,
            created_at=inner.created_at,
            counter_evidence_hash=inner.counter_evidence_hash,
            counter_evidence_uri=inner.counter_evidence_uri,
            response_submitted_at=inner.response_submitted_at,
            state=RepChallengeStateResolved(outcome=outcome_cls()),
        )
        resolved_datum = EndorsementValidatorDatumChallenge(datum=resolved_inner)

        challenge_amount = inner.stake_amount
        mint_value_str = f"1 {self.endorsement_hash}.{token_name}"

        # Redeemer: ChallengeSpend(ResolveChallenge { outcome })
        spend_redeemer = ChallengeSpendRedeemer(
            action=ResolveChallengeRedeemer(outcome=outcome_cls())
        )

        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        # Spend challenge UTxO
        builder.add_script_input(
            challenge_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(spend_redeemer),
        )

        # Output: updated challenge UTxO with Resolved state
        out_ma = MultiAsset({self.end_policy: Asset({token_an: 1})})
        builder.add_output(
            TransactionOutput(
                self.endorsement_addr,
                Value(challenge_amount, out_ma),
                datum=resolved_datum,
            )
        )

        self._add_common_refs(builder)
        builder.required_signers = [self.vkey.hash()]
        builder.validity_start = current_slot
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("ResolveChallenge tx: %s", tx_hash)
        return tx_hash

    def distribute_outcome(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        challenge_datum,
        agent_a_reg_utxo=None,
        wait: bool = True,
    ) -> str:
        """Distribute outcome after challenge resolution.

        Burns challenge token, mints history bonus, pays target + treasury.

        Args:
            challenge_datum: The resolved challenge datum (PlutusData or JSON dict).
            agent_a_reg_utxo: Registry UTxO for target agent. Auto-detected if None.

        Returns:
            Transaction hash.
        """
        current_slot = self._current_slot()

        token_name = derive_challenge_token_name(
            challenger_did, target_did, capability
        )
        challenge_token_an = AssetName(bytes.fromhex(token_name))

        # Find the resolved challenge UTxO
        resolved_utxo = self.find_challenge_utxo(
            challenger_did, target_did, capability
        )
        if not resolved_utxo:
            raise RuntimeError("Resolved challenge UTxO not found")

        # Auto-detect registry UTxO if not provided
        if agent_a_reg_utxo is None:
            agent_a_reg_utxo = self.find_agent_registry_utxo(target_did)
        elif isinstance(agent_a_reg_utxo, str):
            parts = agent_a_reg_utxo.split("#")
            agent_a_reg_utxo = resolve_utxo(parts[0], int(parts[1]), self.ogmios_url)
        if not agent_a_reg_utxo:
            raise RuntimeError(f"Registry UTxO not found for target {target_did[:16]}...")

        # Get inner datum
        if isinstance(challenge_datum, EndorsementValidatorDatumChallenge):
            inner = challenge_datum.datum
        elif isinstance(challenge_datum, dict):
            inner = self._challenge_datum_from_json(challenge_datum)
        else:
            inner = challenge_datum

        challenge_amount = inner.stake_amount
        protocol_fee = challenge_amount * 500 // 10000  # 5%
        # Ensure treasury output meets Cardano min UTxO (~1 ADA)
        treasury_output = max(protocol_fee, 1_000_000)
        target_payout = challenge_amount - protocol_fee

        # Target address
        target_addr = self.wallet_addr  # In Phase 1.0, dev wallet is both parties

        # History bonus token
        resolved_tx_hash = bytes(resolved_utxo.input.transaction_id).hex()
        resolved_tx_idx = resolved_utxo.input.index
        hbonus_token_name = derive_history_bonus_token_name(
            resolved_tx_hash, resolved_tx_idx
        )
        hbonus_token_an = AssetName(bytes.fromhex(hbonus_token_name))

        hbonus_datum = HistoryBonusDatum(
            agent_did=bytes.fromhex(target_did),
            source=HistoryBonusSourceChallengeWon(),
            bonus_points=0,
            source_ref=OutputReference(
                transaction_id=bytes.fromhex(resolved_tx_hash),
                output_index=resolved_tx_idx,
            ),
            created_at=slot_to_posix_ms(current_slot),
        )

        # Mint: burn challenge token (-1) + mint history bonus (+1)
        mint_ma = MultiAsset()
        mint_ma[self.end_policy] = Asset({challenge_token_an: -1})
        mint_ma[self.rep_policy] = Asset({hbonus_token_an: 1})

        # Spend redeemer: ChallengeSpend(DistributeOutcome)
        spend_redeemer = ChallengeSpendRedeemer(
            action=DistributeOutcomeRedeemer()
        )

        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        # Spend resolved challenge UTxO
        builder.add_script_input(
            resolved_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(spend_redeemer),
        )

        # Mint/burn
        builder.mint = mint_ma
        builder.add_minting_script(
            self.end_ref_utxo,
            redeemer=Redeemer(BurnChallengeTokenRedeemer()),
        )
        builder.add_minting_script(
            self.rep_ref_utxo,
            redeemer=Redeemer(MintHistoryBonusRedeemer()),
        )

        # Outputs
        builder.add_output(TransactionOutput(target_addr, target_payout))
        builder.add_output(TransactionOutput(self.treasury_addr, treasury_output))

        hbonus_out_ma = MultiAsset({self.rep_policy: Asset({hbonus_token_an: 1})})
        builder.add_output(
            TransactionOutput(
                self.reputation_addr,
                Value(2_000_000, hbonus_out_ma),
                datum=hbonus_datum,
            )
        )

        # Reference inputs
        builder.reference_inputs.add(agent_a_reg_utxo)
        self._add_common_refs(builder)

        builder.required_signers = [self.vkey.hash()]
        builder.validity_start = current_slot
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("DistributeOutcome tx: %s", tx_hash)
        return tx_hash

    # ── Slash Operations (CapabilityFalsified path) ────────────────────

    def slash_endorsement(
        self,
        endorser_did: str,
        target_did: str,
        challenger_did: str,
        capability: str,
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
        current_slot = self._current_slot()

        # Find endorsement UTxO
        endorsement_utxo = self.find_endorsement_utxo(endorser_did, target_did)
        if not endorsement_utxo:
            raise RuntimeError("Endorsement UTxO not found on-chain")

        endorsement_token_name = derive_endorsement_token_name(endorser_did, target_did)
        endorsement_token_an = AssetName(bytes.fromhex(endorsement_token_name))

        # Resolve the challenge UTxO for reference input
        parts = resolved_challenge_utxo_ref.split("#")
        challenge_ref_utxo = resolve_utxo(parts[0], int(parts[1]), self.ogmios_url)
        if not challenge_ref_utxo:
            raise RuntimeError("Resolved challenge UTxO not found on-chain")

        challenge_oref = OutputReference(
            transaction_id=bytes.fromhex(parts[0]),
            output_index=int(parts[1]),
        )

        # Calculate slash: 50% of endorsement stake
        # Parse endorsement datum to get stake_amount
        endorsement_value = endorsement_utxo.output.amount
        endorsement_lovelace = endorsement_value.coin if hasattr(endorsement_value, 'coin') else endorsement_value
        slash_rate = 5000  # 50% in basis points
        slash_amount = endorsement_lovelace * slash_rate // 10000
        remaining = endorsement_lovelace - slash_amount
        min_endorsement = 5_000_000  # 5 AP3X

        # Spend redeemer: EndorsementSpend(SlashEndorsement { challenge_ref })
        spend_redeemer = EndorsementSpendRedeemer(
            action=SlashEndorsementRedeemer(challenge_ref=challenge_oref)
        )

        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        # Spend endorsement UTxO
        builder.add_script_input(
            endorsement_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(spend_redeemer),
        )

        if remaining >= min_endorsement:
            # Continuing endorsement with reduced stake
            out_ma = MultiAsset({self.end_policy: Asset({endorsement_token_an: 1})})
            # Rebuild endorsement datum with reduced stake
            inner_datum = endorsement_utxo.output.datum
            if isinstance(inner_datum, EndorsementValidatorDatumEndorsement):
                e_datum = inner_datum.datum
            else:
                # Parse from raw CBOR
                from pycardano.serialization import RawPlutusData
                e_datum_raw = endorsement_utxo.output.datum
                e_datum = e_datum_raw  # Will need adjustment based on actual format

            reduced_datum = EndorsementValidatorDatumEndorsement(
                datum=EndorsementDatum(
                    endorser_did=bytes.fromhex(endorser_did),
                    endorser_credential=VerificationKeyCredential(self._vkey_hash_bytes()),
                    target_did=bytes.fromhex(target_did),
                    stake_amount=remaining,
                    endorsed_capabilities=[capability.encode()],
                    created_at=slot_to_posix_ms(current_slot),
                )
            )
            builder.add_output(
                TransactionOutput(
                    self.endorsement_addr,
                    Value(remaining, out_ma),
                    datum=reduced_datum,
                )
            )
        else:
            # Burn endorsement token — remaining below minimum
            burn_ma = MultiAsset({self.end_policy: Asset({endorsement_token_an: -1})})
            builder.mint = burn_ma
            builder.add_minting_script(
                self.end_ref_utxo,
                redeemer=Redeemer(BurnEndorsementTokenRedeemer()),
            )

        # Reference inputs: resolved challenge + common refs
        builder.reference_inputs.add(challenge_ref_utxo)
        self._add_common_refs(builder)

        builder.required_signers = [self.vkey.hash()]
        builder.validity_start = current_slot
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("SlashEndorsement tx: %s", tx_hash)
        return tx_hash

    def distribute_falsified_outcome(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        challenge_datum,
        wait: bool = True,
    ) -> str:
        """Distribute outcome for CapabilityFalsified + slash target's stake.

        Combined transaction that:
        1. Consumes resolved challenge UTxO (DistributeOutcome on endorsement validator)
        2. Consumes target's stake UTxO (SlashStake on reputation validator)
        3. Burns challenge token
        4. Mints history bonus token for challenger
        5. Outputs: challenger reward, treasury fee, reduced stake, history bonus

        Returns:
            Transaction hash.
        """
        current_slot = self._current_slot()

        challenge_token_name = derive_challenge_token_name(
            challenger_did, target_did, capability
        )
        challenge_token_an = AssetName(bytes.fromhex(challenge_token_name))

        # Find resolved challenge UTxO
        resolved_utxo = self.find_challenge_utxo(
            challenger_did, target_did, capability
        )
        if not resolved_utxo:
            raise RuntimeError("Resolved challenge UTxO not found")

        # Find target's stake UTxO
        stake_utxo = self.find_stake_utxo(target_did)
        if not stake_utxo:
            raise RuntimeError("Target stake UTxO not found")

        # Find agent registry UTxO for target
        agent_reg_utxo = self.find_agent_registry_utxo(target_did)
        if not agent_reg_utxo:
            raise RuntimeError(f"Registry UTxO not found for target {target_did[:16]}...")

        # Get inner challenge datum
        if isinstance(challenge_datum, EndorsementValidatorDatumChallenge):
            inner = challenge_datum.datum
        elif isinstance(challenge_datum, dict):
            inner = self._challenge_datum_from_json(challenge_datum)
        else:
            inner = challenge_datum

        # Parse stake datum to get num_capabilities and stake_amount
        # We need these for slash calculation
        stake_value = stake_utxo.output.amount
        stake_lovelace = stake_value.coin if hasattr(stake_value, 'coin') else stake_value
        # For the on-chain datum, we read from the UTxO
        # The stake has capabilities ["code_review", "testing"] = 2 capabilities
        # slash = stake_amount / num_capabilities
        # We'll pass the actual values from the datum

        challenge_amount = inner.stake_amount

        # Stake slash calculation: stake / num_capabilities
        # We need to extract the StakeDatum from the UTxO
        stake_datum_raw = stake_utxo.output.datum
        if isinstance(stake_datum_raw, StakeDatum):
            s_datum = stake_datum_raw
        else:
            # Ogmios returns RawCBOR — decode via cbor2 then parse
            from pycardano.serialization import RawCBOR
            if isinstance(stake_datum_raw, RawCBOR):
                import cbor2
                decoded = cbor2.loads(stake_datum_raw.cbor)
                s_datum = StakeDatum.from_primitive(decoded)
            else:
                s_datum = StakeDatum.from_primitive(stake_datum_raw)

        num_capabilities = len(s_datum.staked_capabilities)
        if num_capabilities == 0:
            raise RuntimeError("Target has no staked capabilities")
        target_slash = s_datum.stake_amount // num_capabilities
        remaining_stake = s_datum.stake_amount - target_slash

        # Protocol fee on slashed amount
        protocol_fee = target_slash * 500 // 10000  # 5%
        # Ensure treasury output meets Cardano min UTxO (~1 ADA)
        treasury_output = max(protocol_fee, 1_000_000)
        challenger_reward = target_slash - protocol_fee + challenge_amount

        # History bonus token for challenger (who won the challenge)
        resolved_tx_hash = bytes(resolved_utxo.input.transaction_id).hex()
        resolved_tx_idx = resolved_utxo.input.index
        hbonus_token_name = derive_history_bonus_token_name(
            resolved_tx_hash, resolved_tx_idx
        )
        hbonus_token_an = AssetName(bytes.fromhex(hbonus_token_name))

        # History bonus datum — challenger won
        hbonus_datum = HistoryBonusDatum(
            agent_did=bytes.fromhex(challenger_did),
            source=HistoryBonusSourceChallengeWon(),
            bonus_points=0,
            source_ref=OutputReference(
                transaction_id=bytes.fromhex(resolved_tx_hash),
                output_index=resolved_tx_idx,
            ),
            created_at=slot_to_posix_ms(current_slot),
        )

        # Challenge OutputReference for SlashStake redeemer
        challenge_oref = OutputReference(
            transaction_id=bytes.fromhex(resolved_tx_hash),
            output_index=resolved_tx_idx,
        )

        # Stake token for continuing output
        stake_token_name = derive_stake_token_name(target_did)
        stake_token_an = AssetName(bytes.fromhex(stake_token_name))

        # Mint: burn challenge token (-1) + mint history bonus (+1)
        mint_ma = MultiAsset()
        mint_ma[self.end_policy] = Asset({challenge_token_an: -1})
        mint_ma[self.rep_policy] = Asset({hbonus_token_an: 1})

        # Redeemers
        challenge_spend_redeemer = ChallengeSpendRedeemer(
            action=DistributeOutcomeRedeemer()
        )
        slash_stake_redeemer = SlashStakeRedeemer(
            challenge_ref=challenge_oref
        )

        builder = self._new_builder()
        self._add_wallet_inputs(builder)

        # Spend 1: resolved challenge UTxO (endorsement validator)
        builder.add_script_input(
            resolved_utxo,
            script=self.end_ref_utxo,
            redeemer=Redeemer(challenge_spend_redeemer),
        )

        # Spend 2: target's stake UTxO (reputation validator)
        builder.add_script_input(
            stake_utxo,
            script=self.rep_ref_utxo,
            redeemer=Redeemer(slash_stake_redeemer),
        )

        # Mint/burn
        builder.mint = mint_ma
        builder.add_minting_script(
            self.end_ref_utxo,
            redeemer=Redeemer(BurnChallengeTokenRedeemer()),
        )
        builder.add_minting_script(
            self.rep_ref_utxo,
            redeemer=Redeemer(MintHistoryBonusRedeemer()),
        )

        # Output 1: challenger receives reward + original stake back
        challenger_addr = self.wallet_addr  # Phase 1.0: dev wallet is both parties
        builder.add_output(TransactionOutput(challenger_addr, challenger_reward))

        # Output 2: protocol treasury fee (use treasury_output >= min UTxO)
        builder.add_output(TransactionOutput(self.treasury_addr, treasury_output))

        # Output 3: continuing stake UTxO with reduced stake
        stake_out_ma = MultiAsset({self.rep_policy: Asset({stake_token_an: 1})})
        reduced_stake_datum = StakeDatum(
            agent_did=s_datum.agent_did,
            owner_credential=s_datum.owner_credential,
            stake_amount=remaining_stake,
            staked_capabilities=s_datum.staked_capabilities,
            last_updated=slot_to_posix_ms(current_slot),
            history_points=s_datum.history_points,
        )
        builder.add_output(
            TransactionOutput(
                self.reputation_addr,
                Value(remaining_stake, stake_out_ma),
                datum=reduced_stake_datum,
            )
        )

        # Output 4: history bonus UTxO
        hbonus_out_ma = MultiAsset({self.rep_policy: Asset({hbonus_token_an: 1})})
        builder.add_output(
            TransactionOutput(
                self.reputation_addr,
                Value(2_000_000, hbonus_out_ma),
                datum=hbonus_datum,
            )
        )

        # Reference inputs
        # Target agent registry (for falsified payouts validation)
        builder.reference_inputs.add(agent_reg_utxo)
        # Challenger agent registry (for history bonus verify_agent_exists)
        challenger_reg_utxo = self.find_agent_registry_utxo(challenger_did)
        if challenger_reg_utxo and challenger_reg_utxo != agent_reg_utxo:
            builder.reference_inputs.add(challenger_reg_utxo)
        self._add_common_refs(builder)

        builder.required_signers = [self.vkey.hash()]
        builder.validity_start = current_slot
        builder.collaterals = [self._collateral()]

        tx_hash = self._sign_submit_wait(builder, wait)
        logger.info("DistributeFalsifiedOutcome tx: %s", tx_hash)
        return tx_hash

    # ── Internal Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _challenge_datum_from_json(datum_json: dict) -> ReputationChallengeDatum:
        """Convert a cardano-cli JSON challenge datum to PlutusData.

        Handles the nested Constr format from the old datums.py builders.
        """
        # Outer: Constr(1, [inner]) — Challenge variant
        inner_fields = datum_json["fields"][0]["fields"]
        return ReputationChallengeDatum(
            challenger_did=bytes.fromhex(inner_fields[0]["bytes"]),
            challenger_credential=VerificationKeyCredential(
                bytes.fromhex(inner_fields[1]["fields"][0]["bytes"])
            ),
            target_did=bytes.fromhex(inner_fields[2]["bytes"]),
            target_credential=VerificationKeyCredential(
                bytes.fromhex(inner_fields[3]["fields"][0]["bytes"])
            ),
            challenged_capability=bytes.fromhex(inner_fields[4]["bytes"]),
            stake_amount=inner_fields[5]["int"],
            evidence_hash=bytes.fromhex(inner_fields[6]["bytes"]),
            evidence_uri=bytes.fromhex(inner_fields[7]["bytes"]),
            created_at=inner_fields[8]["int"],
            counter_evidence_hash=bytes.fromhex(inner_fields[9]["bytes"]),
            counter_evidence_uri=bytes.fromhex(inner_fields[10]["bytes"]),
            response_submitted_at=inner_fields[11]["int"],
            state=RepChallengeStateOpen(),
        )
