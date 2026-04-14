"""
Utility functions for the Reputation Staking SDK.

Address encoding, slot/POSIX conversion.
"""

import bech32

from reputation_staking.constants import SLOT_LENGTH_S, SYSTEM_START_UNIX_S


# ── Slot / POSIX Time Conversion ────────────────────────────────────────────


def slot_to_posix_ms(slot: int) -> int:
    """Convert a Vector testnet slot number to POSIX time in milliseconds."""
    return (SYSTEM_START_UNIX_S + slot * SLOT_LENGTH_S) * 1000


def posix_ms_to_slot(posix_ms: int) -> int:
    """Convert POSIX time in milliseconds to a Vector testnet slot number."""
    return (posix_ms // 1000 - SYSTEM_START_UNIX_S) // SLOT_LENGTH_S


# ── Address Encoding ────────────────────────────────────────────────────────


def script_hash_to_address(script_hash_hex: str) -> str:
    """Convert a script hash to a mainnet bech32 enterprise address."""
    header = bytes([0x71]) + bytes.fromhex(script_hash_hex)
    converted = bech32.convertbits(header, 8, 5)
    return bech32.bech32_encode("addr", converted)


def vkey_hash_to_address(vkey_hash_hex: str) -> str:
    """Convert a verification key hash to a mainnet bech32 enterprise address."""
    header = bytes([0x61]) + bytes.fromhex(vkey_hash_hex)
    converted = bech32.convertbits(header, 8, 5)
    return bech32.bech32_encode("addr", converted)
