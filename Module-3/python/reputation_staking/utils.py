"""
Utility functions for the Reputation Staking SDK.

Address encoding, slot/POSIX conversion and agent identifier output.
"""

from typing import Any, Dict

import bech32
from pycardano import Address, Network, ScriptHash, VerificationKeyHash

from reputation_staking import vector_identity as vi
from reputation_staking.constants import SLOT_LENGTH_S, SYSTEM_START_UNIX_S


# ── Slot / POSIX Time Conversion ────────────────────────────────────────────


def slot_to_posix_ms(slot: int) -> int:
    """Convert a Vector slot number to POSIX time in milliseconds.

    The validity range of a transaction is in slots; a validator sees it in
    POSIX milliseconds, as this function computes it.
    """
    return (SYSTEM_START_UNIX_S + slot * SLOT_LENGTH_S) * 1000


def posix_ms_to_slot(posix_ms: int) -> int:
    """Convert POSIX time in milliseconds to a Vector slot number."""
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


def credential_address(credential: Any) -> Address:
    """The enterprise address (mainnet network tag, no stake part) of a
    payment credential from a datum: VerificationKeyCredential or
    ScriptCredential (reputation_staking.plutus_data). A validator that pays
    to a credential compares only the payment credential of the output."""
    if hasattr(credential, "vkey_hash"):
        return Address(
            payment_part=VerificationKeyHash(bytes(credential.vkey_hash)),
            network=Network.MAINNET,
        )
    if hasattr(credential, "script_hash"):
        return Address(
            payment_part=ScriptHash(bytes(credential.script_hash)),
            network=Network.MAINNET,
        )
    raise TypeError(f"not a payment credential: {type(credential).__name__}")


# ── Agent Identifiers in API Output ─────────────────────────────────────────

# Output fields that hold a 64-hex agent identifier (the asset name of the
# agent's identity record token).
AGENT_ID_FIELDS = ("agent_did", "endorser_did", "target_did", "challenger_did")


def did_or_none(agent_id_hex: Any) -> Any:
    """The canonical DID (`did:vector:<64 hex>`) of a 64-hex identifier, or
    None when the value is not an identifier."""
    try:
        return vi.did_from_id(agent_id_hex)
    except (TypeError, ValueError):
        return None


def add_did_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Add the canonical DID next to each agent identifier of an output row.

    For each field of AGENT_ID_FIELDS, the row gets `<field>_uri` with the
    canonical DID. `related_dids` (comma-separated identifiers of a sybil
    flag) gets `related_did_uris` (a list). The function changes the row and
    returns it.
    """
    for field in AGENT_ID_FIELDS:
        if field in row:
            row[f"{field}_uri"] = did_or_none(row[field])
    if "related_dids" in row:
        related = [part for part in str(row["related_dids"] or "").split(",") if part]
        row["related_did_uris"] = [did_or_none(part) for part in related]
    return row
