"""
Foundation Oracle — resolves challenges and attests decay.

In Phase 1.0, the oracle is the dev wallet (same signer as fee wallet).
The oracle key is the required-signer for ResolveChallenge and AttestDecay.

Usage:
    from reputation_staking import DockerChainBackend, ReputationStakingClient
    from oracle.oracle import FoundationOracle

    backend = DockerChainBackend()
    client = ReputationStakingClient.from_deploy_state("deploy/deploy_state.json", backend)
    client.setup_scripts("deploy/plutus.json")

    oracle = FoundationOracle(client)
    tx = oracle.resolve_challenge(challenger_did, target_did, "code_review", 0, challenge_datum)
"""

from __future__ import annotations

import logging

from reputation_staking.client import ReputationStakingClient

logger = logging.getLogger(__name__)


class FoundationOracle:
    """Foundation oracle for challenge resolution and decay attestation.

    In Phase 1.0, the oracle = dev wallet. The required-signer-hash
    for oracle operations is the same as the dev wallet vkey hash.
    """

    def __init__(self, client: ReputationStakingClient):
        self.client = client

    def resolve_challenge(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        outcome: int,
        challenge_datum: dict,
    ) -> str:
        """Resolve a challenge with a given outcome.

        Args:
            challenger_did: Challenger's agent DID hex.
            target_did: Target agent's DID hex.
            capability: The challenged capability.
            outcome: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive.
            challenge_datum: The on-chain challenge datum JSON (for building resolved version).

        Returns:
            Transaction hash of the ResolveChallenge tx.
        """
        outcome_names = {0: "CapabilityVerified", 1: "CapabilityFalsified", 2: "Inconclusive"}
        logger.info(
            "Resolving challenge: %s vs %s on %s → %s",
            challenger_did[:16], target_did[:16], capability,
            outcome_names.get(outcome, f"Unknown({outcome})"),
        )

        tx_hash = self.client.resolve_challenge(
            challenger_did=challenger_did,
            target_did=target_did,
            capability=capability,
            outcome_constructor=outcome,
            challenge_datum=challenge_datum,
        )
        logger.info("ResolveChallenge tx: %s", tx_hash)
        return tx_hash

    def distribute_outcome(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        challenge_datum: dict,
    ) -> str:
        """Distribute outcome after a challenge has been resolved.

        Finds the target agent's registry UTXO automatically.

        Args:
            challenger_did: Challenger's agent DID hex.
            target_did: Target agent's DID hex.
            capability: The challenged capability.
            challenge_datum: The resolved challenge datum JSON.

        Returns:
            Transaction hash of the DistributeOutcome tx.
        """
        logger.info(
            "Distributing outcome for challenge: %s vs %s on %s",
            challenger_did[:16], target_did[:16], capability,
        )

        # Find target's registry UTXO
        target_reg = self.client.find_agent_registry_utxo(target_did)
        if not target_reg:
            raise RuntimeError(f"Registry UTXO not found for target {target_did[:16]}...")

        tx_hash = self.client.distribute_outcome(
            challenger_did=challenger_did,
            target_did=target_did,
            capability=capability,
            challenge_datum=challenge_datum,
            agent_a_reg_utxo=target_reg,
        )
        logger.info("DistributeOutcome tx: %s", tx_hash)
        return tx_hash

    def resolve_and_distribute(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        outcome: int,
        challenge_datum: dict,
    ) -> tuple:
        """Resolve a challenge and immediately distribute the outcome.

        Returns:
            Tuple of (resolve_tx_hash, distribute_tx_hash).
        """
        resolve_tx = self.resolve_challenge(
            challenger_did, target_did, capability, outcome, challenge_datum
        )

        # Build the resolved datum for distribute
        from reputation_staking.datums import build_resolved_challenge_datum_json
        resolved_datum = build_resolved_challenge_datum_json(challenge_datum, outcome)

        distribute_tx = self.distribute_outcome(
            challenger_did, target_did, capability, resolved_datum
        )

        return resolve_tx, distribute_tx
