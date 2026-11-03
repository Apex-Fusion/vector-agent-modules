"""
Module 3: Reputation Staking — Python SDK.

Economically-secured agent curation for the Vector ecosystem.

Quick start (Ogmios/remote — matches Module 1 and Module 6):
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

An agent is an identity record (did:vector). `agent_did` is its canonical
DID or its 64-hex identifier. The record must be Active and list the
capabilities, and its controller key signs (see client.py).

Legacy (Docker/cardano-cli — requires local node):
    from reputation_staking.docker_backend import DockerChainBackend
    # DockerChainBackend is still available but not used by the main client
"""

__version__ = "0.4.0"

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.deployment import (
    Deployment,
    DeploymentStateError,
    load_deployment,
)
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    HistoryBonusSource,
    IdentityStatus,
    ProtocolParamsInfo,
    RepChallengeOutcome,
    RepChallengeState,
    ReputationScore,
    ReputationTier,
    StakeInfo,
)
from reputation_staking.scoring import (
    compute_decay,
    compute_reputation_score,
    epoch_of,
    get_tier,
    inactive_epochs,
)
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_genesis_bonus_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from reputation_staking.client import ReputationStakingClient, TxWindow
from reputation_staking.utils import (
    add_did_fields,
    posix_ms_to_slot,
    script_hash_to_address,
    slot_to_posix_ms,
    vkey_hash_to_address,
)

__all__ = [
    # Client
    "ReputationStakingClient",
    "TxWindow",
    # Deployment
    "Deployment",
    "DeploymentStateError",
    "load_deployment",
    # Models
    "StakeInfo",
    "EndorsementInfo",
    "ChallengeInfo",
    "HistoryBonusInfo",
    "ReputationScore",
    "ProtocolParamsInfo",
    # Enums
    "IdentityStatus",
    "ReputationTier",
    "RepChallengeState",
    "RepChallengeOutcome",
    "HistoryBonusSource",
    # Scoring
    "compute_reputation_score",
    "compute_decay",
    "epoch_of",
    "inactive_epochs",
    "get_tier",
    # Token names
    "derive_stake_token_name",
    "derive_endorsement_token_name",
    "derive_challenge_token_name",
    "derive_history_bonus_token_name",
    "derive_genesis_bonus_token_name",
    # Utils
    "add_did_fields",
    "slot_to_posix_ms",
    "posix_ms_to_slot",
    "script_hash_to_address",
    "vkey_hash_to_address",
    "DFM_PER_AP3X",
]
