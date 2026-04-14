"""Tests for token name derivation — validated against known on-chain values."""

from reputation_staking.token_names import (
    derive_agent_nft_name_conway,
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


class TestAgentNftName:
    def test_known_agent_a_did(self):
        """The NFT name derivation for Agent A's seed must produce Agent A's DID.

        Agent A was registered using a seed UTXO, and the resulting NFT
        asset name (= agent DID) is stored in smoke_state.json.

        However, the seed UTXO used for registration was the best wallet
        UTXO at the time — we can't reconstruct it from the smoke state
        alone. So we just test that the function is deterministic and
        produces a 64-char hex string.
        """
        result = derive_agent_nft_name_conway("aa" * 32, 0)
        assert len(result) == 64  # 32 bytes = 64 hex chars
        # Deterministic
        assert result == derive_agent_nft_name_conway("aa" * 32, 0)

    def test_different_seed_different_nft(self):
        n1 = derive_agent_nft_name_conway("aa" * 32, 0)
        n2 = derive_agent_nft_name_conway("aa" * 32, 1)
        assert n1 != n2
