"""
Golden-bytes RED tests for `simulation.chain.derive_token_name` and its
three wrappers (`claim_token_name`, `challenge_token_name`, `juror_token_name`).

Contract under test
───────────────────
The Aiken validator (`contracts/lib/adversarial_auditing/utils.ak:34-54`)
computes each NFT's asset name as:

    prefix(4) ++ blake2b_256(cbor.serialise(seed_output_ref))[0..28]

Aiken's `cbor.serialise` of an `OutputReference` produces an
**indefinite-length** Plutus Data list under the Constr-0 tag (121):

    d8 79  9f  58 20 <32-byte-hash>  <idx-cbor>  ff
    │      │  │                      │           │
    tag    │  bytes(32) header       uint        list-terminator
           indefinite-list-start

The v13 mainnet helper
(`/home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py:316-330`)
is the proven-correct reference; it assembles this exact byte sequence by hand
and produced the token names accepted by Aiken's on-chain validator on mainnet.

The current `simulation.chain.derive_token_name` uses
`cbor2.dumps(CBORTag(121, [hash, idx]))`, which emits the DEFINITE-length form
(`d8 79 82 …`) — different bytes, different hash, different token name.
Claim mint therefore fails on chain with `correct_mint = False` because the
sim-derived token name disagrees with the validator's own derivation.

These tests pin the byte-exact expected output to the Aiken-canonical form
so Catherine's one-function fix can flip them RED→GREEN cleanly.

Do NOT use `simulation.chain` on the expected side — these tests recompute
the expected bytes independently from the Aiken reference form.
"""

import hashlib

import pytest

from simulation import chain


# ─────────────────────────────────────────────────────────────────────────
# Helpers — assemble the Aiken-canonical indefinite-length form by hand
# ─────────────────────────────────────────────────────────────────────────

def _cbor_uint(n: int) -> bytes:
    """Minimal CBOR unsigned-int encoding (matches what Aiken's serialiser emits).

    Covers the branches we need to exercise:
      0..23     → 1 byte  (value in minor)
      24..255   → 2 bytes (0x18, uint8)
      256..65535→ 3 bytes (0x19, uint16 BE)
    """
    if n < 0:
        raise ValueError("seed_tx_idx must be non-negative")
    if n <= 23:
        return bytes([n])
    if n <= 0xFF:
        return bytes([0x18, n])
    if n <= 0xFFFF:
        return bytes([0x19]) + n.to_bytes(2, "big")
    raise ValueError(f"idx {n} out of range for these tests")


def _aiken_canonical_output_ref_cbor(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    """Assemble the exact bytes Aiken's `cbor.serialise(OutputReference)` emits.

        d8 79        — tag(121), Constr-0
        9f           — indefinite-length list start
        58 20 <32B>  — bytes header + 32-byte tx hash
        <idx-cbor>   — uint encoding of the output index
        ff           — indefinite-length list break
    """
    assert len(seed_tx_hash) == 32, "seed_tx_hash must be exactly 32 bytes"
    return (
        b"\xd8\x79"            # CBOR tag 121 (Constr-0)
        + b"\x9f"              # indefinite-length array start
        + b"\x58\x20"          # bytes(32) header
        + seed_tx_hash
        + _cbor_uint(seed_tx_idx)
        + b"\xff"              # break (end of indefinite array)
    )


def _expected_token_name(prefix: bytes, seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    out_ref_cbor = _aiken_canonical_output_ref_cbor(seed_tx_hash, seed_tx_idx)
    digest = hashlib.blake2b(out_ref_cbor, digest_size=32).digest()
    return prefix + digest[:28]


# Fixed, non-trivial seeds covering both idx-cbor encoding branches.
# 0x7be4… was the value used in the bug-proof script in the ticket.
SEED_HASH_A = bytes.fromhex("7be4" + "00" * 30)   # idx in minor-inline range
SEED_HASH_B = bytes.fromhex(
    "d3adbeefcafef00d" + "1122334455667788" + "99aabbccddeeff00" + "0f1e2d3c4b5a6978"
)                                                  # idx in uint8 range


# ─────────────────────────────────────────────────────────────────────────
# Golden-bytes tests — one per prefix
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "seed_tx_hash,seed_tx_idx",
    [
        (SEED_HASH_A, 0),    # minor-inline idx
        (SEED_HASH_B, 15),   # minor-inline idx, upper boundary (<=23)
        (SEED_HASH_B, 42),   # uint8 idx (0x18 branch)
    ],
    ids=["idx0", "idx15", "idx42"],
)
def test_derive_token_name_matches_aiken_indefinite_length_encoding_clm(
    seed_tx_hash, seed_tx_idx
):
    """`claim_token_name` must match Aiken's indefinite-length CBOR form."""
    expected = _expected_token_name(b"clm_", seed_tx_hash, seed_tx_idx)
    actual = chain.claim_token_name(seed_tx_hash, seed_tx_idx)
    assert actual == expected, (
        f"claim_token_name({seed_tx_hash.hex()}, {seed_tx_idx}) "
        f"= {actual.hex()} but Aiken-canonical expected {expected.hex()}. "
        "derive_token_name is emitting the DEFINITE-length CBOR list "
        "(d87982…) instead of the INDEFINITE-length form (d8799f…ff) "
        "that Aiken's cbor.serialise produces."
    )


@pytest.mark.parametrize(
    "seed_tx_hash,seed_tx_idx",
    [
        (SEED_HASH_A, 0),
        (SEED_HASH_B, 15),
        (SEED_HASH_B, 42),
    ],
    ids=["idx0", "idx15", "idx42"],
)
def test_derive_token_name_matches_aiken_indefinite_length_encoding_chl(
    seed_tx_hash, seed_tx_idx
):
    """`challenge_token_name` must match Aiken's indefinite-length CBOR form."""
    expected = _expected_token_name(b"chl_", seed_tx_hash, seed_tx_idx)
    actual = chain.challenge_token_name(seed_tx_hash, seed_tx_idx)
    assert actual == expected, (
        f"challenge_token_name({seed_tx_hash.hex()}, {seed_tx_idx}) "
        f"= {actual.hex()} but Aiken-canonical expected {expected.hex()}. "
        "derive_token_name must emit the INDEFINITE-length form (d8799f…ff)."
    )


@pytest.mark.parametrize(
    "seed_tx_hash,seed_tx_idx",
    [
        (SEED_HASH_A, 0),
        (SEED_HASH_B, 15),
        (SEED_HASH_B, 42),
    ],
    ids=["idx0", "idx15", "idx42"],
)
def test_derive_token_name_matches_aiken_indefinite_length_encoding_jur(
    seed_tx_hash, seed_tx_idx
):
    """`juror_token_name` must match Aiken's indefinite-length CBOR form."""
    expected = _expected_token_name(b"jur_", seed_tx_hash, seed_tx_idx)
    actual = chain.juror_token_name(seed_tx_hash, seed_tx_idx)
    assert actual == expected, (
        f"juror_token_name({seed_tx_hash.hex()}, {seed_tx_idx}) "
        f"= {actual.hex()} but Aiken-canonical expected {expected.hex()}. "
        "derive_token_name must emit the INDEFINITE-length form (d8799f…ff)."
    )


# ─────────────────────────────────────────────────────────────────────────
# Cross-consistency — lock in the three 4-byte prefixes
# ─────────────────────────────────────────────────────────────────────────

def test_token_name_prefixes_match_aiken_hardcoded_constants():
    """Prefixes must match `contracts/lib/adversarial_auditing/utils.ak`.

    Aiken file hardcodes:
        claim     → #"636c6d5f"  = b"clm_"
        challenge → #"63686c5f"  = b"chl_"
        juror     → #"6a75725f"  = b"jur_"

    The only way to observe the prefix from Python is via the first 4 bytes
    of a derived name (the blake2b digest is deterministic for a fixed seed).
    This test asserts all three wrappers start with the correct 4-byte prefix,
    so that a regression in `claim_token_name` accidentally using "chl_" (or
    vice-versa) would fail loudly.
    """
    seed_tx_hash = SEED_HASH_A
    seed_tx_idx = 0

    assert chain.claim_token_name(seed_tx_hash, seed_tx_idx)[:4] == b"clm_", (
        "claim_token_name must start with b'clm_' (hex 636c6d5f) per "
        "contracts/lib/adversarial_auditing/utils.ak:37"
    )
    assert chain.challenge_token_name(seed_tx_hash, seed_tx_idx)[:4] == b"chl_", (
        "challenge_token_name must start with b'chl_' (hex 63686c5f) per "
        "contracts/lib/adversarial_auditing/utils.ak:45"
    )
    assert chain.juror_token_name(seed_tx_hash, seed_tx_idx)[:4] == b"jur_", (
        "juror_token_name must start with b'jur_' (hex 6a75725f) per "
        "contracts/lib/adversarial_auditing/utils.ak:53"
    )
    # Digest suffix is 28 bytes → total asset name is 32 bytes (AssetName max).
    assert len(chain.claim_token_name(seed_tx_hash, seed_tx_idx)) == 32
    assert len(chain.challenge_token_name(seed_tx_hash, seed_tx_idx)) == 32
    assert len(chain.juror_token_name(seed_tx_hash, seed_tx_idx)) == 32
