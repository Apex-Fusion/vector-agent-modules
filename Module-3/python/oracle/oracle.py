"""
Foundation Oracle — resolves challenges and distributes their outcomes.

In Phase 1.0, the oracle key is the deploy wallet key: the deploy scripts set
ReputationConfig.oracle_credential to it (deploy state `wallet_vkey_hash`).
The oracle signs ResolveChallenge and each history bonus mint.

The oracle builds every transaction with ReputationStakingClient, so the
client's identity and time rules apply: a history bonus needs the Active
identity record of the bonus agent and the signature of its controller, in a
transaction with a validity range of at most one hour. Resolutions read no
identity record.

Usage:
    from reputation_staking import ReputationStakingClient
    from reputation_staking.ogmios_backend import OgmiosHttpContext, load_wallet
    from oracle.oracle import FoundationOracle

    context = OgmiosHttpContext()
    skey, _, _ = load_wallet("/path/to/oracle_payment.skey")
    client = ReputationStakingClient.from_deploy_state(
        "deploy/deploy_state.json", context, skey,
    )
    oracle = FoundationOracle(client)
    oracle.resolve_challenge(challenger_did, target_did, "code_review", 0)
"""

from __future__ import annotations

import logging
from typing import Optional

from pycardano import PaymentSigningKey, PaymentVerificationKey

from reputation_staking.client import ReputationStakingClient
from reputation_staking.plutus_data import RepChallengeOutcomeFalsified

logger = logging.getLogger(__name__)

OUTCOME_NAMES = {0: "CapabilityVerified", 1: "CapabilityFalsified", 2: "Inconclusive"}


class FoundationOracle:
    """Foundation oracle for challenge resolution.

    The client's wallet key must be the oracle key of the deployment.
    """

    def __init__(self, client: ReputationStakingClient):
        wallet = bytes(PaymentVerificationKey.from_signing_key(client.skey).hash()).hex()
        if wallet != client.deployment.oracle_vkey_hash:
            raise ValueError(
                f"the client's wallet key hash {wallet} is not the oracle key "
                f"{client.deployment.oracle_vkey_hash} of the deployment"
            )
        self.client = client

    def resolve_challenge(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        outcome: int,
    ) -> str:
        """Resolve a challenge with a given outcome.

        Args:
            challenger_did: Challenger's DID or 64-hex identifier.
            target_did: Target agent's DID or 64-hex identifier.
            capability: The challenged capability.
            outcome: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive.

        Returns:
            Transaction hash of the ResolveChallenge tx.
        """
        logger.info(
            "Resolving challenge: %s vs %s on %s → %s",
            challenger_did, target_did, capability,
            OUTCOME_NAMES.get(outcome, f"Unknown({outcome})"),
        )
        tx_hash = self.client.resolve_challenge(
            challenger_did=challenger_did,
            target_did=target_did,
            capability=capability,
            outcome_constructor=outcome,
        )
        logger.info("ResolveChallenge tx: %s", tx_hash)
        return tx_hash

    def distribute_outcome(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        mint_bonus: bool = False,
        bonus_controller_skey: Optional[PaymentSigningKey] = None,
    ) -> str:
        """Distribute the outcome of a resolved challenge.

        CapabilityFalsified also slashes the target's stake
        (distribute_falsified_outcome); the other outcomes use
        distribute_outcome. With `mint_bonus`, the winner (the target for
        CapabilityVerified, the challenger for CapabilityFalsified) gets a
        ChallengeWon history bonus. Its record must be Active, and its
        controller signs (`bonus_controller_skey` when that key is not the
        oracle key).

        Returns:
            Transaction hash of the DistributeOutcome tx.
        """
        logger.info(
            "Distributing outcome for challenge: %s vs %s on %s",
            challenger_did, target_did, capability,
        )
        _, inner = self.client.get_challenge(challenger_did, target_did, capability)
        outcome = self.client.resolved_outcome(inner)
        if isinstance(outcome, RepChallengeOutcomeFalsified):
            tx_hash = self.client.distribute_falsified_outcome(
                challenger_did, target_did, capability,
                mint_bonus=mint_bonus, bonus_controller_skey=bonus_controller_skey,
            )
        else:
            tx_hash = self.client.distribute_outcome(
                challenger_did, target_did, capability,
                mint_bonus=mint_bonus, bonus_controller_skey=bonus_controller_skey,
            )
        logger.info("DistributeOutcome tx: %s", tx_hash)
        return tx_hash

    def resolve_and_distribute(
        self,
        challenger_did: str,
        target_did: str,
        capability: str,
        outcome: int,
        mint_bonus: bool = False,
        bonus_controller_skey: Optional[PaymentSigningKey] = None,
    ) -> tuple:
        """Resolve a challenge, wait for the confirmation, then distribute the
        outcome. Slash the endorsements of a CapabilityFalsified challenge
        before the distribution (the distribution spends the challenge).

        Returns:
            Tuple of (resolve_tx_hash, distribute_tx_hash).
        """
        resolve_tx = self.resolve_challenge(challenger_did, target_did, capability, outcome)
        distribute_tx = self.distribute_outcome(
            challenger_did, target_did, capability,
            mint_bonus=mint_bonus, bonus_controller_skey=bonus_controller_skey,
        )
        return resolve_tx, distribute_tx
