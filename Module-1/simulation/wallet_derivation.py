"""
Global incremental wallet-index allocator + deterministic per-index derivation.

Sim Phase 2 iter-3a (Catherine).

Purpose
-------
Multiple sim scenarios run concurrently against the same Vector testnet master
wallet. To avoid UTxO collisions and double-spend bugs, each agent role in
each scenario must own a distinct sub-wallet. We assign each one a globally
unique integer ``index`` from a single growing counter, persisted on disk
under a process-wide file lock, and derive that sub-wallet's signing key
deterministically from ``(master_skey, index)`` so the same index ALWAYS
produces the same wallet.

Public API
----------
- ``allocate_indices(n, *, scenario, role, index_path=None) -> list[int]``
    Reserve ``n`` consecutive indices from the global counter. Atomic, file-
    lock-protected. Appends one allocation record per index to the index
    file's ``allocations`` array. Returns the allocated indices in ascending
    order.

- ``derive(index, *, master_skey_path) -> dict``
    Derive ``{index, skey, vkey, address}`` deterministically from the master
    skey + the integer index. Re-running with the same inputs is byte-identical
    in skey/vkey/address.

Index file shape
----------------
    {
        "next_index": <int>,
        "allocations": [
            {"index": <int>, "scenario": <str>, "role": <str>, "ts": <iso8601>},
            ...
        ]
    }

Derivation scheme
-----------------
We do NOT implement BIP32. We seed a fresh ``PaymentSigningKey`` from
``blake2b_256(master_skey_bytes || index_be4)``. This is deterministic and
reproducible from the master skey + index alone; it has no relationship to
external HD-wallet tools (Daedalus, Eternl, etc.). For Module-1 sim use the
sub-wallets are throwaway (funded fresh each scenario, drained at the end),
so external recoverability is not a requirement.

Address network
---------------
Uses ``simulation.config.NETWORK`` (Vector treats Cardano "mainnet" addressing
as its testnet — magic 764824073). All addresses returned by ``derive`` are
single-key addresses (no stake key) at that network.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pycardano import (
    Address,
    PaymentSigningKey,
    PaymentVerificationKey,
)

from simulation.config import NETWORK, WALLET_INDEX_FILE


# Default index path: network-scoped via simulation.config.WALLET_INDEX_FILE
# (resolves to ``.wallet_index_testnet.json`` or
# ``.wallet_index_mainnet.json`` depending on the APEX_NETWORK env var).
# Distinct files per chain guarantee that a testnet sim run can NEVER reuse
# a mainnet-bonded sub-wallet index (or vice versa) — which is the one
# thing that could leak real mainnet stake into a test run. Override via
# the ``index_path`` kwarg.
_DEFAULT_INDEX_PATH = WALLET_INDEX_FILE


def _utc_iso_now() -> str:
    """ISO-8601 UTC timestamp, millisecond precision, trailing 'Z'."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _load_or_init(path: Path) -> dict:
    """Load the index file; create with default contents if absent.

    Caller MUST already hold the file lock (this is called from inside the
    flock-guarded region in ``allocate_indices``).

    Recovery contract:
      - Missing file → return default ``{"next_index": 0, "allocations": []}``
        (first-ever allocate; no prior state to preserve).
      - Present but JSON-parse fails OR shape is wrong (``next_index`` not
        a non-negative int, or ``allocations`` not a list) → atomically
        rename the bad file to ``<path>.corrupt-<unix_ts>`` and raise
        ``RuntimeError``. We do NOT silently zero the counter when a file
        existed on disk — even a truncated write may correspond to indices
        already bound to funded sub-wallets, and re-issuing them would
        leak real ADA. Operator must inspect the quarantined file and
        restore from a known-good backup (or accept the loss by deleting
        it).
    """
    if not path.exists():
        return {"next_index": 0, "allocations": []}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # Read failure (e.g. transient IO error). Match the legacy resilience
        # contract — start fresh rather than crashing every future run.
        return {"next_index": 0, "allocations": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Truncated / unparseable JSON: unify with wrong-shape handling —
        # quarantine the bad file and raise. Silent recovery to a fresh
        # counter would risk re-issuing indices already bound to funded
        # sub-wallets. Operator must inspect and restore from backup
        # (or accept the loss by deleting the quarantined file).
        corrupt = _quarantine_corrupt(path)
        raise RuntimeError(
            f"malformed index file at {path}; renamed to {corrupt} for "
            f"inspection (JSON parse failed: {exc})"
        )

    # JSON parsed cleanly — now validate shape. Wrong shape means the file
    # was written deliberately by something (or a prior good run that an
    # operator edited), so funded sub-wallets MAY exist at the recorded
    # indices. Quarantine + raise rather than silently re-allocating.
    if not isinstance(data, dict):
        corrupt = _quarantine_corrupt(path)
        raise RuntimeError(
            f"malformed index file at {path}; renamed to {corrupt} for "
            f"inspection (top-level value is {type(data).__name__}, "
            f"expected object)"
        )

    next_index = data.get("next_index", 0)
    # bool is a subclass of int in Python; reject it to match
    # "next_index non-int" intent.
    if not isinstance(next_index, int) or isinstance(next_index, bool) \
            or next_index < 0:
        corrupt = _quarantine_corrupt(path)
        raise RuntimeError(
            f"malformed index file at {path}; renamed to {corrupt} for "
            f"inspection (next_index has wrong type/value: "
            f"{type(next_index).__name__}={next_index!r})"
        )

    allocations = data.get("allocations", [])
    if not isinstance(allocations, list):
        corrupt = _quarantine_corrupt(path)
        raise RuntimeError(
            f"malformed index file at {path}; renamed to {corrupt} for "
            f"inspection (allocations has wrong type: "
            f"{type(allocations).__name__})"
        )

    # Normalise: ensure both keys present so callers can mutate safely.
    data["next_index"] = next_index
    data["allocations"] = allocations
    return data


def _quarantine_corrupt(path: Path) -> Path:
    """Atomically rename a malformed index file aside for operator inspection.

    Uses ``os.replace`` so the rename is atomic on POSIX — no half-state
    where the index file is gone but the backup not yet written. The
    suffix encodes the unix timestamp so multiple corruptions don't
    overwrite each other when an operator retries on a flapping disk.
    """
    corrupt = path.with_name(path.name + f".corrupt-{int(time.time())}")
    try:
        os.replace(path, corrupt)
    except FileNotFoundError:
        # Already gone (race with a concurrent operator). Surface the
        # would-be path anyway so the error message points somewhere useful.
        return corrupt
    return corrupt


def _atomic_write(path: Path, data: dict) -> None:
    """Atomically write ``data`` (JSON) to ``path``.

    Mirror of the ``scenario.checkpoint`` pattern: write to ``<path>.tmp``,
    fsync via close, then ``os.replace`` (atomic on POSIX). On replace failure
    we unlink the tmp to avoid residue, then re-raise.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # fsync may fail on certain fs (e.g. some tmpfs); we still got
            # a successful write so let os.replace try.
            pass
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def allocate_indices(
    n: int,
    *,
    scenario: str,
    role: str,
    index_path: Path | None = None,
) -> list[int]:
    """Reserve ``n`` consecutive global wallet indices.

    Uses ``fcntl.flock(LOCK_EX)`` on the index file's directory-anchored lock
    so concurrent processes serialise on read-modify-write.

    Args:
        n: number of indices to reserve. Must be >= 1.
        scenario: scenario name (recorded in the allocation log for audit).
        role: role hint (e.g. "agents", "claimant"); recorded for audit.
        index_path: override the default index file location.

    Returns:
        The ``n`` allocated indices, in ascending order.

    Raises:
        ValueError: if ``n < 1``.
    """
    if n < 1:
        raise ValueError(f"allocate_indices: n must be >= 1, got {n}")

    path = Path(index_path) if index_path is not None else _DEFAULT_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # Open (create) a sidecar lock file. We lock the lock file rather than
    # the index file itself so that os.replace doesn't invalidate the lock.
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            data = _load_or_init(path)
            start = int(data.get("next_index", 0))
            allocated = list(range(start, start + n))

            ts = _utc_iso_now()
            for idx in allocated:
                data["allocations"].append({
                    "index": idx,
                    "scenario": scenario,
                    "role": role,
                    "ts": ts,
                })
            data["next_index"] = start + n

            _atomic_write(path, data)
            return allocated
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _master_skey_to_bytes(master_skey: Any) -> bytes:
    """Coerce a master skey (path / PaymentSigningKey / raw bytes) to its
    raw 32-byte ed25519 seed."""
    if isinstance(master_skey, (bytes, bytearray)):
        if len(master_skey) == 32:
            return bytes(master_skey)
        # Defensive: use as-is for non-32-byte seeds (test stubs)
        return bytes(master_skey)
    if isinstance(master_skey, (str, Path)):
        skey_obj = PaymentSigningKey.load(str(master_skey))
        return _master_skey_to_bytes(skey_obj)
    payload = getattr(master_skey, "payload", None)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    cbor = master_skey.to_cbor()
    raw = cbor if isinstance(cbor, (bytes, bytearray)) else bytes.fromhex(cbor)
    if len(raw) >= 34 and raw[0] == 0x58 and raw[1] == 0x20:
        return bytes(raw[2:34])
    raise RuntimeError(
        f"could not extract 32-byte seed from master skey of type {type(master_skey).__name__}"
    )


def derive(
    index: int,
    *,
    master_skey_path: Path | None = None,
    master_skey: Any = None,
) -> dict[str, Any]:
    """Deterministically derive a sub-wallet from the master skey + index.

    Args:
        index: integer wallet index (typically obtained from
            ``allocate_indices``).
        master_skey_path: path to a master skey JSON file (pycardano format).
        master_skey: alternatively, an already-loaded ``PaymentSigningKey``
            object or raw 32-byte seed. Exactly ONE of ``master_skey_path``
            or ``master_skey`` must be provided.

    Returns:
        ``{"index": int, "skey": PaymentSigningKey,
           "vkey": PaymentVerificationKey, "address": Address}``

        Re-calling with the same (master_skey, index) yields byte-identical
        skey / vkey / address objects.
    """
    if index < 0:
        raise ValueError(f"derive: index must be >= 0, got {index}")
    if (master_skey_path is None) == (master_skey is None):
        raise ValueError(
            "derive: exactly one of master_skey_path or master_skey must be set"
        )

    src = master_skey_path if master_skey_path is not None else master_skey
    master_bytes = _master_skey_to_bytes(src)
    seed = hashlib.blake2b(
        master_bytes + int(index).to_bytes(4, "big", signed=False),
        digest_size=32,
    ).digest()

    # Construct a PaymentSigningKey directly from the 32-byte seed. pycardano
    # stores it as the .payload attribute; it accepts raw bytes via
    # from_primitive.
    skey = PaymentSigningKey.from_primitive(seed)
    vkey = PaymentVerificationKey.from_signing_key(skey)
    address = Address(payment_part=vkey.hash(), network=NETWORK)

    return {
        "index": int(index),
        "skey": skey,
        "vkey": vkey,
        "address": address,
    }
