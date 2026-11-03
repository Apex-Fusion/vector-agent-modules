"""
Token name derivation for Module 3: Reputation Staking.

All token names follow the pattern: prefix + blake2b_256(data)[:slice_len].
These must match the on-chain Aiken logic in:
  reputation_staking/utils.ak (derive_stake_token_name, etc.)
  Module-1/contracts/lib/adversarial_auditing/utils.ak (claim_token_name, for
  the Module-1 claim of an escalation)
"""

import hashlib

import cbor2

from reputation_staking.constants import (
    AUDIT_CLAIM_PREFIX,
    AUDIT_CLAIM_SLICE_LEN,
    CHALLENGE_PREFIX,
    CHALLENGE_SLICE_LEN,
    ENDORSEMENT_PREFIX,
    ENDORSEMENT_SLICE_LEN,
    GENESIS_BONUS_PREFIX,
    GENESIS_BONUS_SLICE_LEN,
    HISTORY_BONUS_PREFIX,
    HISTORY_BONUS_SLICE_LEN,
    STAKE_PREFIX,
    STAKE_SLICE_LEN,
)


def derive_token_name(prefix: bytes, data: bytes, slice_len: int = 27) -> bytes:
    """Derive a token name: prefix + blake2b_256(data)[:slice_len]."""
    h = hashlib.blake2b(data, digest_size=32).digest()
    return prefix + h[:slice_len]


def derive_stake_token_name(agent_did_hex: str) -> str:
    """Derive stake token name hex: rstk_ + blake2b(agent_did)[0:27]."""
    return derive_token_name(
        STAKE_PREFIX, bytes.fromhex(agent_did_hex), STAKE_SLICE_LEN
    ).hex()


def derive_endorsement_token_name(endorser_did_hex: str, target_did_hex: str) -> str:
    """Derive endorsement token name hex: rend_ + blake2b(endorser||target)[0:27]."""
    pair = bytes.fromhex(endorser_did_hex) + bytes.fromhex(target_did_hex)
    return derive_token_name(ENDORSEMENT_PREFIX, pair, ENDORSEMENT_SLICE_LEN).hex()


def derive_challenge_token_name(
    challenger_did_hex: str, target_did_hex: str, capability: str
) -> str:
    """Derive challenge token name hex: rchl_ + blake2b(challenger||target||cap)[0:24]."""
    data = (
        bytes.fromhex(challenger_did_hex)
        + bytes.fromhex(target_did_hex)
        + capability.encode()
    )
    return derive_token_name(CHALLENGE_PREFIX, data, CHALLENGE_SLICE_LEN).hex()


def _output_reference_cbor(tx_hash: str, tx_ix: int) -> bytes:
    """builtin.serialise_data of an OutputReference (Plutus V3):
    Constr(0, [ByteArray tx_hash, Int tx_ix]) in the Conway indefinite-length
    CBOR. TransactionId is transparent (raw ByteArray), not wrapped in a
    Constr. D8 79 = tag 121 (constructor 0), 9F = indefinite array, FF =
    break."""
    return (
        b"\xd8\x79\x9f"
        + cbor2.dumps(bytes.fromhex(tx_hash))
        + cbor2.dumps(tx_ix)
        + b"\xff"
    )


def derive_history_bonus_token_name(source_tx_hash: str, source_tx_ix: int) -> str:
    """Derive history bonus token name from source OutputReference:
    hbonus_ + blake2b(serialise(source_ref))[0:24]."""
    return derive_token_name(
        HISTORY_BONUS_PREFIX,
        _output_reference_cbor(source_tx_hash, source_tx_ix),
        HISTORY_BONUS_SLICE_LEN,
    ).hex()


def derive_audit_claim_token_name(seed_tx_hash: str, seed_tx_ix: int) -> str:
    """Derive the name of a Module-1 claim token from its seed (the first
    input of the transaction that mints it): clm_ +
    blake2b(serialise(seed))[0:28], as Module-1 claim_token_name does."""
    return derive_token_name(
        AUDIT_CLAIM_PREFIX,
        _output_reference_cbor(seed_tx_hash, seed_tx_ix),
        AUDIT_CLAIM_SLICE_LEN,
    ).hex()


def derive_genesis_bonus_token_name(agent_did_hex: str) -> str:
    """Derive genesis bonus token name hex: genesis_ + blake2b(agent_did)[0:24]."""
    return derive_token_name(
        GENESIS_BONUS_PREFIX, bytes.fromhex(agent_did_hex), GENESIS_BONUS_SLICE_LEN
    ).hex()
