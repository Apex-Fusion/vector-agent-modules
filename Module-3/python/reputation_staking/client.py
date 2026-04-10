"""
ReputationStakingClient — high-level API for Module 3 operations.

Wraps chain backend + datum builders into clean action methods.

Usage:
    from reputation_staking import DockerChainBackend, ReputationStakingClient

    backend = DockerChainBackend()
    client = ReputationStakingClient.from_deploy_state("deploy/deploy_state.json", backend)

    # One-time: write Plutus scripts to Docker container
    client.setup_scripts("deploy/plutus.json")

    tx = client.create_stake("agent_did_hex", ["code_review"], 10_000_000)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Set

import cbor2

from reputation_staking.backend import ChainBackend
from reputation_staking.constants import (
    REGISTRY_POLICY_ID,
    TX_WAIT_SECONDS,
    TREASURY_SCRIPT_HASH,
)
from reputation_staking.datums import (
    BURN_CHALLENGE_TOKEN,
    CREATE_STAKE,
    DISTRIBUTE_OUTCOME,
    INCREASE_STAKE,
    MINT_CHALLENGE_TOKEN,
    MINT_ENDORSEMENT_TOKEN,
    MINT_HISTORY_BONUS,
    MINT_STAKE_TOKEN,
    build_challenge_datum_json,
    build_endorsement_datum_json,
    build_history_bonus_datum_json,
    build_resolved_challenge_datum_json,
    build_stake_datum_json,
    resolve_challenge_redeemer,
)
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from reputation_staking.utils import script_hash_to_address, vkey_hash_to_address

logger = logging.getLogger(__name__)

# Working directory inside Docker for temporary files
WORK_DIR = "/tmp/m3sdk"


class ReputationStakingClient:
    """High-level client for Module 3 Reputation Staking operations."""

    def __init__(self, backend: ChainBackend, deploy_state: dict):
        self.backend = backend
        self.deploy = deploy_state

        # Extract frequently-used values
        self.reputation_hash = deploy_state["reputation_validator_hash"]
        self.endorsement_hash = deploy_state["endorsement_validator_hash"]
        self.reputation_addr = deploy_state["reputation_address"]
        self.endorsement_addr = deploy_state["endorsement_address"]
        self.refs_policy = deploy_state["refs_token_policy"]
        self.params_holder_addr = deploy_state["params_holder_address"]
        self.registry_addr = script_hash_to_address(REGISTRY_POLICY_ID)

        # Reference script UTXOs (deployed on-chain)
        tx_hashes = deploy_state.get("tx_hashes", {})
        self.reputation_ref_utxo = (
            f"{tx_hashes['reputation_spend_ref']}#0"
            if "reputation_spend_ref" in tx_hashes
            else None
        )
        self.endorsement_ref_utxo = (
            f"{tx_hashes['endorsement_spend_ref']}#0"
            if "endorsement_spend_ref" in tx_hashes
            else None
        )
        self.params_utxo = (
            f"{tx_hashes['params_utxo']}#0"
            if "params_utxo" in tx_hashes
            else None
        )
        self.cross_refs_utxo = (
            f"{tx_hashes['cross_refs_nft']}#0"
            if "cross_refs_nft" in tx_hashes
            else None
        )

        # Non-spendable UTXOs (reference scripts + cross-refs NFT)
        self._protected_utxos: Set[str] = set()
        for utxo in [
            self.reputation_ref_utxo,
            self.endorsement_ref_utxo,
            self.cross_refs_utxo,
        ]:
            if utxo:
                self._protected_utxos.add(utxo)

        # Script file paths (set by setup_scripts)
        self._reputation_script: Optional[str] = None
        self._endorsement_spend_script: Optional[str] = None
        self._endorsement_mint_script: Optional[str] = None

    @classmethod
    def from_deploy_state(
        cls, deploy_state_path: str, backend: ChainBackend
    ) -> "ReputationStakingClient":
        """Create client from a deploy_state.json file."""
        with open(deploy_state_path) as f:
            deploy_state = json.load(f)
        return cls(backend, deploy_state)

    # ── Script Setup ────────────────────────────────────────────────────

    def setup_scripts(self, blueprint_path: str) -> None:
        """Write Plutus script envelopes from blueprint to Docker container.

        Reads the applied blueprint (plutus.json), creates cardano-cli
        compatible PlutusScriptV3 envelopes, and writes them to WORK_DIR.

        Args:
            blueprint_path: Path to the applied plutus.json blueprint.
        """
        self._ensure_workdir()
        with open(blueprint_path) as f:
            blueprint = json.load(f)

        for v in blueprint["validators"]:
            title = v["title"]
            if ".else" in title:
                continue
            safe_name = title.replace(".", "_")
            envelope = {
                "type": "PlutusScriptV3",
                "description": title,
                "cborHex": cbor2.dumps(bytes.fromhex(v["compiledCode"])).hex(),
            }
            remote_path = f"{WORK_DIR}/{safe_name}.plutus"
            self.backend.write_json(remote_path, envelope)

            # Track the script paths we care about
            if "reputation" in title and "spend" in title:
                self._reputation_script = remote_path
            elif "endorsement" in title and "spend" in title:
                self._endorsement_spend_script = remote_path
            elif "endorsement" in title and "mint" in title:
                self._endorsement_mint_script = remote_path

        logger.info(
            "Scripts written to %s (reputation=%s, endorse_spend=%s, endorse_mint=%s)",
            WORK_DIR, self._reputation_script, self._endorsement_spend_script,
            self._endorsement_mint_script,
        )

    def _require_script(self, name: str) -> str:
        """Get a script path, raising if setup_scripts() hasn't been called."""
        path = getattr(self, f"_{name}", None)
        if not path:
            raise RuntimeError(
                f"Script '{name}' not available. Call setup_scripts() first."
            )
        return path

    # ── Helpers ──────────────────────────────────────────────────────────

    def _wallet_addr(self) -> str:
        return self.backend.get_wallet_address()

    def _vkey_hash(self) -> str:
        return self.backend.get_wallet_vkey_hash()

    def _best_utxo(self) -> tuple:
        return self.backend.get_best_utxo(self._wallet_addr(), self._protected_utxos)

    def _collateral_arg(self) -> str:
        c = self.backend.get_collateral(self._wallet_addr(), self._protected_utxos)
        return f"--tx-in-collateral {c} " if c else ""

    def _ensure_workdir(self):
        self.backend.docker_exec(f"mkdir -p {WORK_DIR}")

    def _write_datum(self, name: str, data) -> str:
        """Write a datum JSON file and return the remote path."""
        path = f"{WORK_DIR}/{name}"
        self.backend.write_json(path, data)
        return path

    def _wait_for_confirmation(self, seconds: int = TX_WAIT_SECONDS):
        logger.info("Waiting %ds for confirmation...", seconds)
        time.sleep(seconds)

    # ── UTXO Finders ─────────────────────────────────────────────────────

    def find_agent_registry_utxo(self, agent_did: str) -> Optional[str]:
        """Find the registry UTXO for a given agent DID."""
        return self.backend.find_utxo_with_token(
            self.registry_addr, REGISTRY_POLICY_ID, agent_did
        )

    def find_stake_utxo(self, agent_did: str) -> Optional[str]:
        """Find the stake UTXO for an agent."""
        token_name = derive_stake_token_name(agent_did)
        return self.backend.find_utxo_with_token(
            self.reputation_addr, self.reputation_hash, token_name
        )

    def find_endorsement_utxo(
        self, endorser_did: str, target_did: str
    ) -> Optional[str]:
        """Find an endorsement UTXO."""
        token_name = derive_endorsement_token_name(endorser_did, target_did)
        return self.backend.find_utxo_with_token(
            self.endorsement_addr, self.endorsement_hash, token_name
        )

    def find_challenge_utxo(
        self, challenger_did: str, target_did: str, capability: str
    ) -> Optional[str]:
        """Find a challenge UTXO."""
        token_name = derive_challenge_token_name(challenger_did, target_did, capability)
        return self.backend.find_utxo_with_token(
            self.endorsement_addr, self.endorsement_hash, token_name
        )

    # ── Stake Operations ─────────────────────────────────────────────────

    def create_seed_utxo(
        self,
        agent_did: str,
        capabilities: list,
        min_lovelace: int = 2_000_000,
        wait: bool = True,
    ) -> str:
        """Send a seed UTXO to the reputation address (required before CreateStake).

        The seed has a dummy StakeDatum and will be consumed by create_stake().

        Returns:
            Transaction hash. The seed UTXO will be at {tx_hash}#0.
        """
        self._ensure_workdir()
        current_slot = self.backend.get_current_slot()
        wallet_addr = self._wallet_addr()
        vkey_hash = self._vkey_hash()

        seed_datum = build_stake_datum_json(
            agent_did, vkey_hash, min_lovelace, capabilities, current_slot
        )
        datum_path = self._write_datum("seed_datum.json", seed_datum)

        txin, _ = self._best_utxo()

        self.backend.cardano_cli(
            f"conway transaction build {self.backend.network_flag} "
            f"--tx-in {txin} "
            f"--tx-out '{self.reputation_addr}+{min_lovelace}' "
            f"--tx-out-inline-datum-file {datum_path} "
            f"{self._collateral_arg()}"
            f"--change-address {wallet_addr} "
            f"--out-file {WORK_DIR}/tx_seed.raw"
        )

        tx_hash = self.backend.sign_and_submit(
            "seed_utxo", f"{WORK_DIR}/tx_seed.raw", f"{WORK_DIR}/tx_seed.signed"
        )
        logger.info("Seed UTXO tx: %s", tx_hash)
        if wait:
            self._wait_for_confirmation()
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

        Requires a seed UTXO at the reputation address (created beforehand).

        Args:
            agent_did: Agent's DID (NFT asset name hex).
            capabilities: List of capability strings.
            stake_amount: Amount in DFM (lovelace).
            seed_utxo: UTXO at reputation address to consume. Auto-detected if None.
            wait: Whether to wait for confirmation.

        Returns:
            Transaction hash.
        """
        self._ensure_workdir()
        current_slot = self.backend.get_current_slot()
        wallet_addr = self._wallet_addr()
        vkey_hash = self._vkey_hash()

        stake_token_name = derive_stake_token_name(agent_did)
        stake_mint_value = f"1 {self.reputation_hash}.{stake_token_name}"

        # Find required reference inputs
        agent_reg_utxo = self.find_agent_registry_utxo(agent_did)
        if not agent_reg_utxo:
            raise RuntimeError(f"Agent registry UTXO not found for DID {agent_did[:16]}...")

        if not seed_utxo:
            raise ValueError("seed_utxo is required for CreateStake")

        # Build datum + redeemers
        stake_datum_path = self._write_datum(
            "stake_datum.json",
            build_stake_datum_json(agent_did, vkey_hash, stake_amount, capabilities, current_slot),
        )
        create_stake_path = self._write_datum("create_stake_redeemer.json", CREATE_STAKE)
        mint_stake_path = self._write_datum("mint_stake_redeemer.json", MINT_STAKE_TOKEN)

        txin, _ = self._best_utxo()

        rep_script = self._require_script("reputation_script")
        self.backend.cardano_cli(
            f"conway transaction build {self.backend.network_flag} "
            f"--tx-in {txin} "
            f"--tx-in {seed_utxo} "
            f"--tx-in-script-file {rep_script} "
            f"--tx-in-inline-datum-present "
            f"--tx-in-redeemer-file {create_stake_path} "
            f"--read-only-tx-in-reference {agent_reg_utxo} "
            f"--read-only-tx-in-reference {self.params_utxo} "
            f"--read-only-tx-in-reference {self.cross_refs_utxo} "
            f"--tx-out '{self.reputation_addr}+{stake_amount}+{stake_mint_value}' "
            f"--tx-out-inline-datum-file {stake_datum_path} "
            f"--mint '{stake_mint_value}' "
            f"--mint-script-file {rep_script} "
            f"--mint-redeemer-file {mint_stake_path} "
            f"--required-signer-hash {vkey_hash} "
            f"--invalid-before {current_slot} "
            f"--invalid-hereafter {current_slot + 600} "
            f"{self._collateral_arg()}"
            f"--change-address {wallet_addr} "
            f"--out-file {WORK_DIR}/tx_stake.raw"
        )

        tx_hash = self.backend.sign_and_submit(
            "create_stake", f"{WORK_DIR}/tx_stake.raw", f"{WORK_DIR}/tx_stake.signed"
        )
        logger.info("CreateStake tx: %s", tx_hash)
        if wait:
            self._wait_for_confirmation()
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

        Args:
            endorser_did: Endorser's DID hex.
            target_did: Target agent's DID hex.
            capabilities: Capabilities being endorsed.
            stake_amount: Endorsement amount in DFM.
            wait: Whether to wait for confirmation.

        Returns:
            Transaction hash.
        """
        self._ensure_workdir()
        current_slot = self.backend.get_current_slot()
        wallet_addr = self._wallet_addr()
        vkey_hash = self._vkey_hash()

        token_name = derive_endorsement_token_name(endorser_did, target_did)
        mint_value = f"1 {self.endorsement_hash}.{token_name}"

        # Reference inputs
        endorser_reg = self.find_agent_registry_utxo(endorser_did)
        target_reg = self.find_agent_registry_utxo(target_did)
        stake_utxo = self.find_stake_utxo(target_did)

        if not endorser_reg or not target_reg:
            raise RuntimeError("Agent registry UTXOs not found")
        if not stake_utxo:
            raise RuntimeError("Target agent stake UTXO not found")

        datum_path = self._write_datum(
            "endorsement_datum.json",
            build_endorsement_datum_json(
                endorser_did, vkey_hash, target_did, stake_amount, capabilities, current_slot
            ),
        )
        mint_redeemer_path = self._write_datum("mint_endorsement_redeemer.json", MINT_ENDORSEMENT_TOKEN)

        txin, _ = self._best_utxo()

        self.backend.cardano_cli(
            f"conway transaction build {self.backend.network_flag} "
            f"--tx-in {txin} "
            f"--read-only-tx-in-reference {endorser_reg} "
            f"--read-only-tx-in-reference {target_reg} "
            f"--read-only-tx-in-reference {stake_utxo} "
            f"--read-only-tx-in-reference {self.params_utxo} "
            f"--read-only-tx-in-reference {self.cross_refs_utxo} "
            f"--tx-out '{self.endorsement_addr}+{stake_amount}+{mint_value}' "
            f"--tx-out-inline-datum-file {datum_path} "
            f"--mint '{mint_value}' "
            f"--mint-script-file {self._require_script('endorsement_mint_script')} "
            f"--mint-redeemer-file {mint_redeemer_path} "
            f"--required-signer-hash {vkey_hash} "
            f"{self._collateral_arg()}"
            f"--change-address {wallet_addr} "
            f"--out-file {WORK_DIR}/tx_endorse.raw"
        )

        tx_hash = self.backend.sign_and_submit(
            "mint_endorsement", f"{WORK_DIR}/tx_endorse.raw", f"{WORK_DIR}/tx_endorse.signed"
        )
        logger.info("MintEndorsement tx: %s", tx_hash)
        if wait:
            self._wait_for_confirmation()
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
            Tuple of (tx_hash, challenge_datum_json). The datum is needed
            for resolve_challenge() and distribute_outcome().
        """
        self._ensure_workdir()
        current_slot = self.backend.get_current_slot()
        wallet_addr = self._wallet_addr()
        vkey_hash = self._vkey_hash()

        token_name = derive_challenge_token_name(challenger_did, target_did, capability)
        mint_value = f"1 {self.endorsement_hash}.{token_name}"

        # Reference inputs
        challenger_reg = self.find_agent_registry_utxo(challenger_did)
        target_reg = self.find_agent_registry_utxo(target_did)
        stake_utxo = self.find_stake_utxo(target_did)

        if not challenger_reg or not target_reg:
            raise RuntimeError("Agent registry UTXOs not found")
        if not stake_utxo:
            raise RuntimeError("Target agent stake UTXO not found")

        challenge_datum = build_challenge_datum_json(
            challenger_did, vkey_hash, target_did, vkey_hash,
            capability, stake_amount, evidence_hash, evidence_uri, current_slot
        )
        datum_path = self._write_datum("challenge_datum.json", challenge_datum)
        mint_redeemer_path = self._write_datum("mint_challenge_redeemer.json", MINT_CHALLENGE_TOKEN)

        txin, _ = self._best_utxo()

        self.backend.cardano_cli(
            f"conway transaction build {self.backend.network_flag} "
            f"--tx-in {txin} "
            f"--read-only-tx-in-reference {challenger_reg} "
            f"--read-only-tx-in-reference {target_reg} "
            f"--read-only-tx-in-reference {stake_utxo} "
            f"--read-only-tx-in-reference {self.params_utxo} "
            f"--read-only-tx-in-reference {self.cross_refs_utxo} "
            f"--tx-out '{self.endorsement_addr}+{stake_amount}+{mint_value}' "
            f"--tx-out-inline-datum-file {datum_path} "
            f"--mint '{mint_value}' "
            f"--mint-script-file {self._require_script('endorsement_mint_script')} "
            f"--mint-redeemer-file {mint_redeemer_path} "
            f"--required-signer-hash {vkey_hash} "
            f"--invalid-before {current_slot} "
            f"{self._collateral_arg()}"
            f"--change-address {wallet_addr} "
            f"--out-file {WORK_DIR}/tx_challenge.raw"
        )

        tx_hash = self.backend.sign_and_submit(
            "mint_challenge", f"{WORK_DIR}/tx_challenge.raw", f"{WORK_DIR}/tx_challenge.signed"
        )
        logger.info("MintChallenge tx: %s", tx_hash)
        if wait:
            self._wait_for_confirmation()
        return tx_hash, challenge_datum

    def resolve_challenge(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        outcome_constructor: int,
        challenge_datum: dict,
        wait: bool = True,
    ) -> str:
        """Resolve a challenge (Foundation oracle in Phase 1.0).

        Args:
            outcome_constructor: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive
            challenge_datum: The original challenge datum JSON (for building resolved version).

        Returns:
            Transaction hash.
        """
        self._ensure_workdir()
        current_slot = self.backend.get_current_slot()
        wallet_addr = self._wallet_addr()
        vkey_hash = self._vkey_hash()

        token_name = derive_challenge_token_name(challenger_did, target_did, capability)
        challenge_utxo = self.find_challenge_utxo(challenger_did, target_did, capability)
        if not challenge_utxo:
            raise RuntimeError("Challenge UTXO not found on-chain")

        resolved_datum = build_resolved_challenge_datum_json(challenge_datum, outcome_constructor)
        datum_path = self._write_datum("resolved_datum.json", resolved_datum)
        redeemer_path = self._write_datum(
            "resolve_redeemer.json", resolve_challenge_redeemer(outcome_constructor)
        )

        challenge_amount = challenge_datum["fields"][0]["fields"][5]["int"]
        mint_value_str = f"1 {self.endorsement_hash}.{token_name}"

        txin, _ = self._best_utxo()

        self.backend.cardano_cli(
            f"conway transaction build {self.backend.network_flag} "
            f"--tx-in {txin} "
            f"--tx-in {challenge_utxo} "
            f"--tx-in-script-file {self._require_script('endorsement_spend_script')} "
            f"--tx-in-inline-datum-present "
            f"--tx-in-redeemer-file {redeemer_path} "
            f"--read-only-tx-in-reference {self.params_utxo} "
            f"--read-only-tx-in-reference {self.cross_refs_utxo} "
            f"--tx-out '{self.endorsement_addr}+{challenge_amount}+{mint_value_str}' "
            f"--tx-out-inline-datum-file {datum_path} "
            f"--required-signer-hash {vkey_hash} "
            f"--invalid-before {current_slot} "
            f"{self._collateral_arg()}"
            f"--change-address {wallet_addr} "
            f"--out-file {WORK_DIR}/tx_resolve.raw"
        )

        tx_hash = self.backend.sign_and_submit(
            "resolve_challenge", f"{WORK_DIR}/tx_resolve.raw", f"{WORK_DIR}/tx_resolve.signed"
        )
        logger.info("ResolveChallenge tx: %s", tx_hash)
        if wait:
            self._wait_for_confirmation()
        return tx_hash

    def distribute_outcome(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        challenge_datum: dict,
        agent_a_reg_utxo: str,
        wait: bool = True,
    ) -> str:
        """Distribute outcome after challenge resolution.

        For CapabilityVerified: burns challenge token, mints history bonus,
        pays target + treasury.

        Args:
            challenge_datum: The resolved challenge datum JSON.
            agent_a_reg_utxo: Registry UTXO for the target agent (reference input).

        Returns:
            Transaction hash.
        """
        self._ensure_workdir()
        current_slot = self.backend.get_current_slot()
        wallet_addr = self._wallet_addr()
        vkey_hash = self._vkey_hash()

        token_name = derive_challenge_token_name(challenger_did, target_did, capability)

        # Find the resolved challenge UTXO
        resolved_utxo = self.find_challenge_utxo(challenger_did, target_did, capability)
        if not resolved_utxo:
            raise RuntimeError("Resolved challenge UTXO not found")

        distribute_path = self._write_datum("distribute_redeemer.json", DISTRIBUTE_OUTCOME)

        txin, _ = self._best_utxo()

        # Extract challenge amount from datum
        challenge_amount = challenge_datum["fields"][0]["fields"][5]["int"]
        protocol_fee = challenge_amount * 500 // 10000  # 5%
        target_payout = challenge_amount - protocol_fee

        target_addr = vkey_hash_to_address(vkey_hash)
        treasury_addr = script_hash_to_address(TREASURY_SCRIPT_HASH)

        # Burn challenge token
        burn_value = f"-1 {self.endorsement_hash}.{token_name}"

        # Mint history bonus token
        resolved_tx_hash = resolved_utxo.split("#")[0]
        resolved_tx_ix = int(resolved_utxo.split("#")[1])
        hbonus_token_name = derive_history_bonus_token_name(resolved_tx_hash, resolved_tx_ix)
        hbonus_mint_value = f"1 {self.reputation_hash}.{hbonus_token_name}"

        hbonus_datum = build_history_bonus_datum_json(
            target_did, 0, 0, resolved_tx_hash, resolved_tx_ix, current_slot
        )
        hbonus_datum_path = self._write_datum("hbonus_datum.json", hbonus_datum)
        mint_hbonus_path = self._write_datum("mint_hbonus_redeemer.json", MINT_HISTORY_BONUS)
        burn_challenge_path = self._write_datum("burn_challenge_redeemer.json", BURN_CHALLENGE_TOKEN)

        # Build using reference scripts (avoid >16KB tx size limit)
        combined_mint = f"{burn_value} + {hbonus_mint_value}"

        if self.endorsement_ref_utxo and self.reputation_ref_utxo:
            spend_args = (
                f"--spending-tx-in-reference {self.endorsement_ref_utxo} "
                f"--spending-plutus-script-v3 "
                f"--spending-reference-tx-in-inline-datum-present "
                f"--spending-reference-tx-in-redeemer-file {distribute_path} "
            )
            mint_args = (
                f"--mint '{combined_mint}' "
                f"--mint-tx-in-reference {self.endorsement_ref_utxo} "
                f"--mint-plutus-script-v3 "
                f"--mint-reference-tx-in-redeemer-file {burn_challenge_path} "
                f"--policy-id {self.endorsement_hash} "
                f"--mint-tx-in-reference {self.reputation_ref_utxo} "
                f"--mint-plutus-script-v3 "
                f"--mint-reference-tx-in-redeemer-file {mint_hbonus_path} "
                f"--policy-id {self.reputation_hash} "
            )
        else:
            spend_args = (
                f"--tx-in-script-file {self._require_script('endorsement_spend_script')} "
                f"--tx-in-inline-datum-present "
                f"--tx-in-redeemer-file {distribute_path} "
            )
            mint_args = (
                f"--mint '{combined_mint}' "
                f"--mint-script-file {self._require_script('endorsement_mint_script')} "
                f"--mint-redeemer-file {burn_challenge_path} "
                f"--mint-script-file {self._require_script('reputation_script')} "
                f"--mint-redeemer-file {mint_hbonus_path} "
            )

        self.backend.cardano_cli(
            f"conway transaction build {self.backend.network_flag} "
            f"--tx-in {txin} "
            f"--tx-in {resolved_utxo} "
            f"{spend_args}"
            f"--read-only-tx-in-reference {self.params_utxo} "
            f"--read-only-tx-in-reference {self.cross_refs_utxo} "
            f"--read-only-tx-in-reference {agent_a_reg_utxo} "
            f"--tx-out '{target_addr}+{target_payout}' "
            f"--tx-out '{treasury_addr}+{protocol_fee}' "
            f"--tx-out '{self.reputation_addr}+2000000+{hbonus_mint_value}' "
            f"--tx-out-inline-datum-file {hbonus_datum_path} "
            f"{mint_args}"
            f"--invalid-before {current_slot} "
            f"--required-signer-hash {vkey_hash} "
            f"{self._collateral_arg()}"
            f"--change-address {wallet_addr} "
            f"--out-file {WORK_DIR}/tx_distribute.raw"
        )

        tx_hash = self.backend.sign_and_submit(
            "distribute_outcome",
            f"{WORK_DIR}/tx_distribute.raw",
            f"{WORK_DIR}/tx_distribute.signed",
        )
        logger.info("DistributeOutcome tx: %s", tx_hash)
        if wait:
            self._wait_for_confirmation()
        return tx_hash
