"""Shared test fixtures for the Reputation Staking SDK."""

import pytest

from reputation_staking.models import ProtocolParamsInfo


@pytest.fixture
def default_params() -> ProtocolParamsInfo:
    """Default protocol parameters matching the Vector testnet deployment."""
    return ProtocolParamsInfo()


# Known values from deploy/smoke_state.json (2026-04-10 smoke test run)
AGENT_A_DID = "036dc41f85740d155904b4dff933c592ed3874bdea3b888724ed936ec6a08358"
AGENT_B_DID = "72f261c505ca9be49d7ba4bd38184bb97923fe3247862642e1ddc25fccd784b3"
KNOWN_STAKE_TOKEN = "7273746b5fe309190e90acd5882e420ed2ea9f82dfe178223d873b8401a331f2"
KNOWN_ENDORSEMENT_TOKEN = "72656e645f78982af37a6684874de30d910d6ca58d2a54ed02deebf5b155927e"
KNOWN_CHALLENGE_TOKEN = "7263686c5f436f7420acd6aad4a58c3cf536da1bbd976060b8f2f7fad3"

# Agent A seed UTXO for NFT derivation
AGENT_A_SEED_TX = "61f0cd404a8327be76d4f7345a5045ce758d7cfc3483ebb3612f57da5e1e5a62"
AGENT_A_SEED_IX = 0
