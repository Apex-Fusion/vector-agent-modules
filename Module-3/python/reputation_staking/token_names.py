"""
Token name derivation for Module 3: Reputation Staking.

All token names follow the pattern: prefix + blake2b_256(data)[:slice_len].
These must match the on-chain Aiken logic in:
  reputation_staking/utils.ak (derive_stake_token_name, etc.)
"""

import hashlib

import cbor2

from reputation_staking.constants import (
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


def derive_history_bonus_token_name(source_tx_hash: str, source_tx_ix: int) -> str:
    """Derive history bonus token name from source OutputReference.

    Uses builtin.serialise_data(oref) — Conway indefinite-length CBOR.
    V3: OutputReference = Constr(0, [ByteArray tx_hash, Int tx_ix])
    TransactionId is transparent (raw ByteArray), NOT wrapped in Constr.
    """
    tx_hash_bytes = bytes.fromhex(source_tx_hash)
    # Constr(0, [tx_hash, tx_ix]) — Conway indefinite-length CBOR
    # D8 79 = tag 121 (constructor 0), 9F = indef array, FF = break
    oref_cbor = (
        b"\xd8\x79\x9f"
        + cbor2.dumps(tx_hash_bytes)
        + cbor2.dumps(source_tx_ix)
        + b"\xff"
    )
    return derive_token_name(
        HISTORY_BONUS_PREFIX, oref_cbor, HISTORY_BONUS_SLICE_LEN
    ).hex()


def derive_genesis_bonus_token_name(agent_did_hex: str) -> str:
    """Derive genesis bonus token name hex: genesis_ + blake2b(agent_did)[0:24]."""
    return derive_token_name(
        GENESIS_BONUS_PREFIX, bytes.fromhex(agent_did_hex), GENESIS_BONUS_SLICE_LEN
    ).hex()


def derive_agent_nft_name_conway(seed_tx_hash: str, seed_tx_ix: int) -> str:
    """Derive agent NFT asset name from seed OutputReference.

    Conway uses indefinite-length CBOR arrays (9F...FF encoding).
    OutputReference = Constr(0, [ByteString tx_hash, Int tx_ix])
    Conway CBOR: D8 79 (tag 121 = constructor 0) + 9F (indef) + fields + FF (break)
    """
    tx_hash_bytes = bytes.fromhex(seed_tx_hash)
    constr_cbor = b"\xd8\x79"  # tag 121 = constructor 0
    constr_cbor += b"\x9f"  # indefinite array start
    constr_cbor += cbor2.dumps(tx_hash_bytes)
    constr_cbor += cbor2.dumps(seed_tx_ix)
    constr_cbor += b"\xff"  # break

    return hashlib.blake2b(constr_cbor, digest_size=32).hexdigest()
