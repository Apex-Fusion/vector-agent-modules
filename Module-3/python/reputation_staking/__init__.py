"""
Module 3: Reputation Staking — Python SDK.

Economically-secured agent curation for the Vector ecosystem.

Quick start (Ogmios/remote — matches Module 1 and Module 6):
    from reputation_staking import ReputationStakingClient
    from reputation_staking.ogmios_backend import create_context, load_wallet

    context = create_context()
    skey, vkey, wallet_addr = load_wallet("wallet/payment.skey")
    client = ReputationStakingClient.from_deploy_state(
        "deploy/deploy_state.json", context, skey,
    )
    tx = client.create_stake(agent_did, ["code_review"], 10_000_000)

Legacy (Docker/cardano-cli — requires local node):
    from reputation_staking.docker_backend import DockerChainBackend
    # DockerChainBackend is still available but not used by the main client
"""

__version__ = "0.3.0"

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    HistoryBonusSource,
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
    get_tier,
)
from reputation_staking.token_names import (
    derive_agent_nft_name_conway,
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_genesis_bonus_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from reputation_staking.client import ReputationStakingClient
from reputation_staking.utils import (
    posix_ms_to_slot,
    script_hash_to_address,
    slot_to_posix_ms,
    vkey_hash_to_address,
)

__all__ = [
    # Client
    "ReputationStakingClient",
    # Models
    "StakeInfo",
    "EndorsementInfo",
    "ChallengeInfo",
    "HistoryBonusInfo",
    "ReputationScore",
    "ProtocolParamsInfo",
    # Enums
    "ReputationTier",
    "RepChallengeState",
    "RepChallengeOutcome",
    "HistoryBonusSource",
    # Scoring
    "compute_reputation_score",
    "compute_decay",
    "get_tier",
    # Token names
    "derive_stake_token_name",
    "derive_endorsement_token_name",
    "derive_challenge_token_name",
    "derive_history_bonus_token_name",
    "derive_genesis_bonus_token_name",
    "derive_agent_nft_name_conway",
    # Utils
    "slot_to_posix_ms",
    "posix_ms_to_slot",
    "script_hash_to_address",
    "vkey_hash_to_address",
    "DFM_PER_AP3X",
]
