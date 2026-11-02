"""Tests for token name derivation — validated against known on-chain values."""

from reputation_staking import vector_identity as vi
from reputation_staking.token_names import (
    derive_challenge_token_name,
    derive_endorsement_token_name,
    derive_genesis_bonus_token_name,
    derive_history_bonus_token_name,
    derive_stake_token_name,
)
from tests.conftest import (
    AGENT_A_DID,
    AGENT_B_DID,
    KNOWN_CHALLENGE_TOKEN,
    KNOWN_ENDORSEMENT_TOKEN,
    KNOWN_STAKE_TOKEN,
)


class TestStakeTokenName:
    def test_known_agent_a(self):
        """Stake token for Agent A must match the on-chain smoke test value."""
        assert derive_stake_token_name(AGENT_A_DID) == KNOWN_STAKE_TOKEN

    def test_prefix(self):
        result = bytes.fromhex(derive_stake_token_name(AGENT_A_DID))
        assert result[:5] == b"rstk_"

    def test_length(self):
        result = bytes.fromhex(derive_stake_token_name(AGENT_A_DID))
        assert len(result) == 32  # 5 prefix + 27 hash

    def test_different_agents_produce_different_tokens(self):
        assert derive_stake_token_name(AGENT_A_DID) != derive_stake_token_name(
            AGENT_B_DID
        )


class TestEndorsementTokenName:
    def test_known_b_endorses_a(self):
        """Endorsement token for B->A must match the on-chain smoke test value."""
        assert (
            derive_endorsement_token_name(AGENT_B_DID, AGENT_A_DID)
            == KNOWN_ENDORSEMENT_TOKEN
        )

    def test_prefix(self):
        result = bytes.fromhex(
            derive_endorsement_token_name(AGENT_B_DID, AGENT_A_DID)
        )
        assert result[:5] == b"rend_"

    def test_direction_matters(self):
        """B->A and A->B must produce different tokens."""
        assert derive_endorsement_token_name(
            AGENT_B_DID, AGENT_A_DID
        ) != derive_endorsement_token_name(AGENT_A_DID, AGENT_B_DID)


class TestChallengeTokenName:
    def test_known_b_challenges_a(self):
        """Challenge token for B challenges A on code_review must match on-chain."""
        assert (
            derive_challenge_token_name(AGENT_B_DID, AGENT_A_DID, "code_review")
            == KNOWN_CHALLENGE_TOKEN
        )

    def test_prefix(self):
        result = bytes.fromhex(
            derive_challenge_token_name(AGENT_B_DID, AGENT_A_DID, "code_review")
        )
        assert result[:5] == b"rchl_"

    def test_different_capability_different_token(self):
        t1 = derive_challenge_token_name(AGENT_B_DID, AGENT_A_DID, "code_review")
        t2 = derive_challenge_token_name(AGENT_B_DID, AGENT_A_DID, "testing")
        assert t1 != t2


class TestHistoryBonusTokenName:
    def test_prefix(self):
        result = bytes.fromhex(
            derive_history_bonus_token_name("ab" * 32, 0)
        )
        assert result[:7] == b"hbonus_"

    def test_length(self):
        result = bytes.fromhex(
            derive_history_bonus_token_name("ab" * 32, 0)
        )
        assert len(result) == 31  # 7 prefix + 24 hash

    def test_different_outputs_different_tokens(self):
        t1 = derive_history_bonus_token_name("ab" * 32, 0)
        t2 = derive_history_bonus_token_name("ab" * 32, 1)
        assert t1 != t2


class TestGenesisBonusTokenName:
    def test_prefix(self):
        result = bytes.fromhex(derive_genesis_bonus_token_name(AGENT_A_DID))
        assert result[:8] == b"genesis_"

    def test_length(self):
        result = bytes.fromhex(derive_genesis_bonus_token_name(AGENT_A_DID))
        assert len(result) == 32  # 8 prefix + 24 hash


class TestIdentityIdentifier:
    """Agent identifiers are identity record identifiers (did:vector)."""

    def test_stake_token_of_identity_identifier(self):
        # Part 1 §3.1 vector: the identifier of the seed (00…00, 0)
        agent_id = vi.identifier_from_seed(bytes(32), 0)
        assert agent_id.hex() == (
            "a2e5e227858e84f1a8f9b0c1246e6cbc9336d707d43ba43d0e1cb7c51c45f4c9"
        )
        token = bytes.fromhex(derive_stake_token_name(agent_id.hex()))
        assert token.startswith(b"rstk_") and len(token) == 32

    def test_did_and_hex_name_the_same_agent(self):
        agent_id = vi.identifier_from_seed(bytes.fromhex("ab" * 32), 24)
        did = vi.did_from_id(agent_id)
        assert vi.normalize_agent_id(did).hex() == agent_id.hex()
        assert derive_stake_token_name(vi.normalize_agent_id(did).hex()) == (
            derive_stake_token_name(agent_id.hex())
        )
