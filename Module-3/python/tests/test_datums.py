"""Tests for JSON datum builders — validated against known smoke test datums."""

import json

from reputation_staking.datums import (
    BURN_CHALLENGE_TOKEN,
    BURN_GENESIS_BONUS,
    CREATE_STAKE,
    DISTRIBUTE_OUTCOME,
    EXPIRE_CHALLENGE,
    LOCK_FOR_CHALLENGE,
    MINT_CHALLENGE_TOKEN,
    MINT_ENDORSEMENT_TOKEN,
    MINT_HISTORY_BONUS,
    MINT_STAKE_TOKEN,
    build_challenge_datum_json,
    build_endorsement_datum_json,
    build_history_bonus_datum_json,
    build_resolved_challenge_datum_json,
    build_stake_datum_json,
    release_challenge_redeemer,
    resolve_challenge_redeemer,
    script_credential_json,
    slash_stake_redeemer,
    vk_credential_json,
)
from tests.conftest import AGENT_A_DID, AGENT_B_DID


VKEY_HASH = "2ef77ec4340057363c3824919a61db70ee9683ee9b7d15283aa91931"
# A POSIX ms time: the lower bound of a validity range
LO_MS = 1_775_799_692_000
# The seed output reference of a stake (its stake_id)
SEED_TX = "5e" * 32


def _challenge(target_credential=None, created_at_ms=LO_MS):
    return build_challenge_datum_json(
        AGENT_B_DID, VKEY_HASH, AGENT_A_DID,
        target_credential or vk_credential_json(VKEY_HASH),
        "code_review", 25_000_000, "ab" * 32, "ipfs://test", created_at_ms,
    )


class TestVkCredentialJson:
    def test_structure(self):
        result = vk_credential_json(VKEY_HASH)
        assert result == {
            "constructor": 0,
            "fields": [{"bytes": VKEY_HASH}],
        }


class TestStakeDatumJson:
    def _stake(self):
        return build_stake_datum_json(
            AGENT_A_DID, VKEY_HASH, 10_000_000, ["code_review"], LO_MS, SEED_TX, 3,
        )

    def test_field_count(self):
        """8 fields; challenge_locks is empty at creation and stake_id (last)
        is the seed output reference."""
        datum = self._stake()
        assert len(datum["fields"]) == 8
        assert datum["fields"][6] == {"list": []}
        assert datum["fields"][7] == {
            "constructor": 0, "fields": [{"bytes": SEED_TX}, {"int": 3}],
        }

    def test_constructor_zero(self):
        assert self._stake()["constructor"] == 0

    def test_posix_ms_timestamp(self):
        """last_updated is the POSIX ms value as given (the upper bound)."""
        assert self._stake()["fields"][4]["int"] == LO_MS


class TestEndorsementDatumJson:
    def test_nested_constr(self):
        """Endorsement uses nested Constr: Constr(0, [Constr(0, [8 fields])])."""
        datum = build_endorsement_datum_json(
            AGENT_B_DID, VKEY_HASH, AGENT_A_DID, 5_000_000, ["code_review"], LO_MS, SEED_TX, 3,
        )
        assert datum["constructor"] == 0  # Endorsement variant
        inner = datum["fields"][0]
        assert inner["constructor"] == 0  # Inner EndorsementDatum
        assert len(inner["fields"]) == 8
        assert inner["fields"][5] == {"int": LO_MS}
        assert inner["fields"][6] == {"list": []}  # slashed_by
        # target_stake: the stake_id of the bound stake
        assert inner["fields"][7] == {
            "constructor": 0, "fields": [{"bytes": SEED_TX}, {"int": 3}],
        }


class TestReputationMintRedeemers:
    def test_no_stake_burn(self):
        """StakeMintAction has no BurnStakeToken: MintStakeToken 0,
        MintHistoryBonus 1, BurnGenesisBonus 2."""
        assert MINT_STAKE_TOKEN == {"constructor": 0, "fields": []}
        assert MINT_HISTORY_BONUS == {"constructor": 1, "fields": []}
        assert BURN_GENESIS_BONUS == {"constructor": 2, "fields": []}


class TestChallengeDatumJson:
    def test_nested_constr(self):
        """Challenge uses nested Constr: Constr(1, [Constr(0, [13 fields])])."""
        datum = _challenge()
        assert datum["constructor"] == 1  # Challenge variant
        inner = datum["fields"][0]
        assert inner["constructor"] == 0  # Inner ReputationChallengeDatum
        assert len(inner["fields"]) == 13

    def test_open_state(self):
        state = _challenge()["fields"][0]["fields"][12]
        assert state == {"constructor": 0, "fields": []}  # Open

    def test_target_credential_is_the_given_credential(self):
        """target_credential is the owner_credential of the target's stake,
        also a script credential."""
        owner = script_credential_json("cd" * 28)
        inner = _challenge(target_credential=owner)["fields"][0]
        assert inner["fields"][3] == owner
        assert inner["fields"][1] == vk_credential_json(VKEY_HASH)

    def test_matches_known_smoke_datum(self):
        """Validate against the challenge datum saved in smoke_state.json
        (created_at 1775799692000 ms)."""
        datum = build_challenge_datum_json(
            AGENT_B_DID, VKEY_HASH, AGENT_A_DID, vk_credential_json(VKEY_HASH),
            "code_review", 25_000_000,
            "93190a5f98bfa2e0c47ec5cac29c34e891ba9360b42848a9820863e59b81a3c2",
            "ipfs://smoke-test-evidence",
            1_775_799_692_000,
        )
        # Verify structure: Constr(1, [Constr(0, [13 fields])])
        assert datum["constructor"] == 1
        inner = datum["fields"][0]
        assert inner["constructor"] == 0
        assert inner["fields"][0]["bytes"] == AGENT_B_DID
        assert inner["fields"][2]["bytes"] == AGENT_A_DID
        assert inner["fields"][5]["int"] == 25_000_000
        assert inner["fields"][8]["int"] == 1_775_799_692_000


class TestResolvedChallengeDatumJson:
    def test_resolved_state(self):
        resolved = build_resolved_challenge_datum_json(_challenge(), 0)  # CapabilityVerified
        state = resolved["fields"][0]["fields"][12]
        assert state == {
            "constructor": 2,  # Resolved
            "fields": [{"constructor": 0, "fields": []}],  # CapabilityVerified
        }

    def test_original_not_mutated(self):
        original = _challenge()
        original_copy = json.loads(json.dumps(original))
        build_resolved_challenge_datum_json(original, 0)
        assert original == original_copy  # Not mutated


class TestHistoryBonusDatumJson:
    def test_structure(self):
        datum = build_history_bonus_datum_json(
            AGENT_A_DID, 0, 0, "ab" * 32, 0, LO_MS
        )
        assert datum["constructor"] == 0
        assert len(datum["fields"]) == 5
        assert datum["fields"][1] == {"constructor": 0, "fields": []}  # ChallengeWon
        assert datum["fields"][4] == {"int": LO_MS}


class TestRedeemerConstants:
    def test_mint_stake_token(self):
        assert MINT_STAKE_TOKEN == {"constructor": 0, "fields": []}

    def test_create_stake(self):
        assert CREATE_STAKE == {"constructor": 0, "fields": []}

    def test_mint_endorsement_token(self):
        assert MINT_ENDORSEMENT_TOKEN == {"constructor": 0, "fields": []}

    def test_mint_challenge_token(self):
        assert MINT_CHALLENGE_TOKEN == {"constructor": 2, "fields": []}

    def test_burn_challenge_token(self):
        assert BURN_CHALLENGE_TOKEN == {"constructor": 3, "fields": []}

    def test_distribute_outcome(self):
        assert DISTRIBUTE_OUTCOME == {
            "constructor": 1,
            "fields": [{"constructor": 6, "fields": []}],
        }

    def test_expire_challenge(self):
        """ExpireChallenge is the last RepChallengeAction constructor (7)."""
        assert EXPIRE_CHALLENGE == {
            "constructor": 1,
            "fields": [{"constructor": 7, "fields": []}],
        }

    def test_stake_lock_redeemers(self):
        """SlashStake (5), LockForChallenge (6), ReleaseChallenge (7)."""
        ref = {"constructor": 0, "fields": [{"bytes": "ab" * 32}, {"int": 1}]}
        assert slash_stake_redeemer("ab" * 32, 1) == {"constructor": 5, "fields": [ref]}
        assert LOCK_FOR_CHALLENGE == {"constructor": 6, "fields": []}
        assert release_challenge_redeemer("ab" * 32, 1) == {"constructor": 7, "fields": [ref]}


class TestResolveChallengeRedeemer:
    def test_capability_verified(self):
        r = resolve_challenge_redeemer(0)
        assert r == {
            "constructor": 1,  # ChallengeSpend
            "fields": [
                {
                    "constructor": 4,  # ResolveChallenge
                    "fields": [{"constructor": 0, "fields": []}],
                }
            ],
        }

    def test_capability_falsified(self):
        r = resolve_challenge_redeemer(1)
        assert r["fields"][0]["fields"][0]["constructor"] == 1

    def test_inconclusive(self):
        r = resolve_challenge_redeemer(2)
        assert r["fields"][0]["fields"][0]["constructor"] == 2
