"""
cardano-cli JSON ScriptData format builders for Module 3 datums and redeemers.

These produce the JSON dictionaries that cardano-cli expects for
--tx-out-inline-datum-file, --mint-redeemer-file, --tx-in-redeemer-file, etc.

All encoding follows Aiken's Plutus V3 CBOR conventions:
  - Sum type variants use NESTED Constr
  - OutputReference: TransactionId is raw ByteArray (no Constr wrapper)
  - Timestamps are POSIX milliseconds
"""

from __future__ import annotations

import json
from typing import Any, List

from reputation_staking.utils import slot_to_posix_ms


# ── Credential Helpers ───────────────────────────────────────────────────────


def vk_credential_json(vkey_hash: str) -> dict:
    """VerificationKeyCredential (constructor 0)."""
    return {"constructor": 0, "fields": [{"bytes": vkey_hash}]}


def script_credential_json(script_hash: str) -> dict:
    """ScriptCredential (constructor 1)."""
    return {"constructor": 1, "fields": [{"bytes": script_hash}]}


def output_reference_json(tx_hash: str, tx_ix: int) -> dict:
    """OutputReference — V3: TransactionId is raw ByteArray."""
    return {
        "constructor": 0,
        "fields": [{"bytes": tx_hash}, {"int": tx_ix}],
    }


# ── Datum Builders ───────────────────────────────────────────────────────────


def build_agent_datum_json(
    owner_vkey_hash: str,
    name: str,
    description: str,
    capabilities: List[str],
    framework: str,
    current_slot: int,
) -> dict:
    """Build AgentDatum for Agent Registry registration."""
    return {
        "constructor": 0,
        "fields": [
            vk_credential_json(owner_vkey_hash),
            {"bytes": name.encode().hex()},
            {"bytes": description.encode().hex()},
            {"list": [{"bytes": cap.encode().hex()} for cap in capabilities]},
            {"bytes": framework.encode().hex()},
            {"bytes": "".encode().hex()},  # endpoint (empty)
            {"int": current_slot * 1000},  # registered_at as POSIX ms
        ],
    }


def build_register_redeemer_json(seed_tx_hash: str, seed_tx_ix: int) -> dict:
    """Build Register { seed: OutputReference } redeemer for Agent Registry."""
    return {
        "constructor": 0,  # Register
        "fields": [output_reference_json(seed_tx_hash, seed_tx_ix)],
    }


def build_stake_datum_json(
    agent_did: str,
    owner_vkey_hash: str,
    stake_amount: int,
    capabilities: List[str],
    current_slot: int,
) -> dict:
    """Build StakeDatum. last_updated uses POSIX ms."""
    return {
        "constructor": 0,
        "fields": [
            {"bytes": agent_did},
            vk_credential_json(owner_vkey_hash),
            {"int": stake_amount},
            {"list": [{"bytes": cap.encode().hex()} for cap in capabilities]},
            {"int": slot_to_posix_ms(current_slot)},
            {"int": 0},  # history_points
        ],
    }


def build_endorsement_datum_json(
    endorser_did: str,
    endorser_vkey_hash: str,
    target_did: str,
    stake_amount: int,
    capabilities: List[str],
    current_slot: int,
) -> dict:
    """Build EndorsementDatum wrapped in EndorsementValidatorDatum.Endorsement.

    Aiken uses NESTED Constr encoding: Constr(0, [Constr(0, [6 fields])]).
    """
    return {
        "constructor": 0,  # Endorsement variant
        "fields": [
            {
                "constructor": 0,  # Inner EndorsementDatum record
                "fields": [
                    {"bytes": endorser_did},
                    vk_credential_json(endorser_vkey_hash),
                    {"bytes": target_did},
                    {"int": stake_amount},
                    {"list": [{"bytes": cap.encode().hex()} for cap in capabilities]},
                    {"int": slot_to_posix_ms(current_slot)},
                ],
            },
        ],
    }


def build_challenge_datum_json(
    challenger_did: str,
    challenger_vkey_hash: str,
    target_did: str,
    target_vkey_hash: str,
    capability: str,
    stake_amount: int,
    evidence_hash: str,
    evidence_uri: str,
    current_slot: int,
) -> dict:
    """Build ReputationChallengeDatum wrapped in EndorsementValidatorDatum.Challenge.

    Aiken uses NESTED Constr encoding: Constr(1, [Constr(0, [13 fields])]).
    """
    return {
        "constructor": 1,  # Challenge variant
        "fields": [
            {
                "constructor": 0,  # Inner ReputationChallengeDatum record
                "fields": [
                    {"bytes": challenger_did},
                    vk_credential_json(challenger_vkey_hash),
                    {"bytes": target_did},
                    vk_credential_json(target_vkey_hash),
                    {"bytes": capability.encode().hex()},
                    {"int": stake_amount},
                    {"bytes": evidence_hash},
                    {"bytes": evidence_uri.encode().hex()},
                    {"int": slot_to_posix_ms(current_slot)},
                    {"bytes": ""},  # counter_evidence_hash (empty)
                    {"bytes": ""},  # counter_evidence_uri (empty)
                    {"int": 0},     # response_submitted_at
                    {"constructor": 0, "fields": []},  # Open state
                ],
            },
        ],
    }


def build_resolved_challenge_datum_json(
    original_datum: dict, outcome_constructor: int
) -> dict:
    """Update challenge datum to Resolved state.

    Args:
        original_datum: The original challenge datum JSON.
        outcome_constructor: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive
    """
    new_datum = json.loads(json.dumps(original_datum))
    # Inner ReputationChallengeDatum is at fields[0], state is inner field[12]
    new_datum["fields"][0]["fields"][12] = {
        "constructor": 2,  # Resolved
        "fields": [
            {"constructor": outcome_constructor, "fields": []}
        ],
    }
    return new_datum


def build_history_bonus_datum_json(
    agent_did: str,
    source_constructor: int,
    bonus_points: int,
    source_tx_hash: str,
    source_tx_ix: int,
    current_slot: int,
) -> dict:
    """Build HistoryBonusDatum.

    Args:
        source_constructor: 0=ChallengeWon, 1=AuditClaimWon, etc.
    """
    return {
        "constructor": 0,
        "fields": [
            {"bytes": agent_did},
            {"constructor": source_constructor, "fields": []},
            {"int": bonus_points},
            output_reference_json(source_tx_hash, source_tx_ix),
            {"int": slot_to_posix_ms(current_slot)},
        ],
    }


# ── Redeemer Constants ───────────────────────────────────────────────────────

def redeemer_json(constructor: int, fields: Any = None) -> dict:
    """Build a simple redeemer."""
    return {"constructor": constructor, "fields": fields or []}


# Reputation mint redeemers
MINT_STAKE_TOKEN = redeemer_json(0)
BURN_STAKE_TOKEN = redeemer_json(1)
MINT_HISTORY_BONUS = redeemer_json(2)
BURN_GENESIS_BONUS = redeemer_json(3)

# Reputation spend redeemers
CREATE_STAKE = redeemer_json(0)
INCREASE_STAKE = redeemer_json(1)


def decrease_stake_redeemer(amount: int) -> dict:
    return redeemer_json(2, [{"int": amount}])


def update_capabilities_redeemer(capabilities: List[str]) -> dict:
    return redeemer_json(3, [{"list": [{"bytes": c.encode().hex()} for c in capabilities]}])


CLAIM_DECAY_REFUND = redeemer_json(4)

# Endorsement mint redeemers
MINT_ENDORSEMENT_TOKEN = redeemer_json(0)
BURN_ENDORSEMENT_TOKEN = redeemer_json(1)
MINT_CHALLENGE_TOKEN = redeemer_json(2)
BURN_CHALLENGE_TOKEN = redeemer_json(3)

# Endorsement spend redeemers (wrapped in EndorsementSpend / ChallengeSpend)

# EndorsementSpend(IncreaseEndorsement)
INCREASE_ENDORSEMENT = {"constructor": 0, "fields": [{"constructor": 0, "fields": []}]}

# EndorsementSpend(WithdrawEndorsement)
WITHDRAW_ENDORSEMENT = {"constructor": 0, "fields": [{"constructor": 1, "fields": []}]}


def slash_endorsement_redeemer(challenge_tx_hash: str, challenge_tx_ix: int) -> dict:
    """EndorsementSpend(SlashEndorsement { challenge_ref })"""
    return {
        "constructor": 0,
        "fields": [
            {
                "constructor": 2,
                "fields": [output_reference_json(challenge_tx_hash, challenge_tx_ix)],
            }
        ],
    }


# ChallengeSpend redeemers

# ChallengeSpend(WithdrawChallenge)
WITHDRAW_CHALLENGE = {"constructor": 1, "fields": [{"constructor": 0, "fields": []}]}


def respond_to_challenge_redeemer(counter_evidence_hash: str, counter_evidence_uri: str) -> dict:
    """ChallengeSpend(RespondToChallenge { hash, uri })"""
    return {
        "constructor": 1,
        "fields": [
            {
                "constructor": 1,
                "fields": [
                    {"bytes": counter_evidence_hash},
                    {"bytes": counter_evidence_uri.encode().hex()},
                ],
            }
        ],
    }


# ChallengeSpend(EscalateToAudit)
ESCALATE_TO_AUDIT = {"constructor": 1, "fields": [{"constructor": 2, "fields": []}]}

# ChallengeSpend(ResolveEscalation)
RESOLVE_ESCALATION = {"constructor": 1, "fields": [{"constructor": 3, "fields": []}]}


def resolve_challenge_redeemer(outcome_constructor: int) -> dict:
    """ChallengeSpend(ResolveChallenge { outcome }).

    Args:
        outcome_constructor: 0=CapabilityVerified, 1=CapabilityFalsified, 2=Inconclusive
    """
    return {
        "constructor": 1,  # ChallengeSpend
        "fields": [
            {
                "constructor": 4,  # ResolveChallenge
                "fields": [
                    {"constructor": outcome_constructor, "fields": []}
                ],
            }
        ],
    }


# ChallengeSpend(DefaultJudgment)
DEFAULT_JUDGMENT = {"constructor": 1, "fields": [{"constructor": 5, "fields": []}]}

# ChallengeSpend(DistributeOutcome)
DISTRIBUTE_OUTCOME = {
    "constructor": 1,  # ChallengeSpend
    "fields": [
        {"constructor": 6, "fields": []}  # DistributeOutcome
    ],
}
