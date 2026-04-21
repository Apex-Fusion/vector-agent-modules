#!/usr/bin/env python3
"""Approved chain-query helper for agents.

Agents are allowlisted to run exactly:
    python3 ~/vector-agents/bin/scripts/chain.py <subcommand> [args...]

All dynamic behavior is dispatched by fixed subcommands. No eval, no -c.

Subcommands:
    balance                       AP3X balance of wallet.skey in cwd.
    utxos                         UTxOs as JSON.
    tip                           Current slot + epoch + epoch_length_slots.
    tx-status <tx-hash-hex>       "landed" or "unknown".
    slots-since <slot>            Slots + epochs elapsed since <slot>.
    juror-prep <dispute_id> <verdict> <reveal_deadline_slot>
                                  Generates a fresh salt, computes the
                                  commit hash, writes salt directly into
                                  state.json.pending_tx (NEVER emits salt
                                  to stdout). Returns the commit hash only.
                                  verdict must be "uphold" or "overturn".

Output: JSON to stdout. Errors to stderr + exit 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path.home() / "vector-agents"
sys.path.insert(0, str(BASE / "shared/Module-3/python"))
from reputation_staking.ogmios_backend import (  # noqa: E402
    OgmiosHttpContext,
    load_wallet,
    get_current_slot,
    ogmios_rpc,
    resolve_utxo,
)

# Fallback when protocol params can't be queried. This is the Cardano
# mainnet default (432000 slots = 5 days). Apex-Fusion is a Cardano fork
# running --mainnet. If chain.py falls back, the 'fallback_used' flag in
# the tip output makes it obvious — m3-staker's decay check should not
# silently trust epoch math when fallback_used=true.
FALLBACK_EPOCH_LENGTH_SLOTS = 432_000

_VERDICT_BYTE = {"uphold": b"\x00", "overturn": b"\x01"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_ID = re.compile(r"^[0-9a-f]{1,128}$")


def _epoch_length() -> tuple[int, bool]:
    """Returns (slots_per_epoch, used_fallback)."""
    try:
        summaries = ogmios_rpc("queryLedgerState/eraSummaries") or []
        for era in reversed(summaries):
            if not isinstance(era, dict):
                continue
            params = era.get("parameters") or {}
            el = params.get("epochLength")
            if isinstance(el, dict) and isinstance(el.get("slots"), int):
                return el["slots"], False
            if isinstance(el, int):
                return el, False
    except Exception:
        pass
    return FALLBACK_EPOCH_LENGTH_SLOTS, True


def _write_state_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def cmd_balance(_argv: list[str]) -> int:
    skey_path = Path.cwd() / "wallet.skey"
    if not skey_path.exists():
        print(json.dumps({"error": "no wallet.skey in cwd"}))
        return 1
    ctx = OgmiosHttpContext()
    _skey, _vkey, addr = load_wallet(str(skey_path))
    utxos = ctx.utxos(addr)
    total = sum(u.output.amount.coin for u in utxos)
    print(json.dumps({
        "address": str(addr),
        "lovelace": total,
        "ap3x": total / 1_000_000,
        "utxo_count": len(utxos),
    }))
    return 0


def cmd_utxos(_argv: list[str]) -> int:
    skey_path = Path.cwd() / "wallet.skey"
    if not skey_path.exists():
        print(json.dumps({"error": "no wallet.skey in cwd"}))
        return 1
    ctx = OgmiosHttpContext()
    _skey, _vkey, addr = load_wallet(str(skey_path))
    utxos = ctx.utxos(addr)
    result = []
    for u in utxos:
        txid = bytes(u.input.transaction_id).hex()
        result.append({
            "ref": f"{txid}#{u.input.index}",
            "lovelace": u.output.amount.coin,
        })
    print(json.dumps({"utxos": result, "address": str(addr)}))
    return 0


def cmd_tip(_argv: list[str]) -> int:
    slot = get_current_slot()
    epoch_length, fallback_used = _epoch_length()
    print(json.dumps({
        "slot": slot,
        "epoch": slot // epoch_length,
        "epoch_length_slots": epoch_length,
        "fallback_used": fallback_used,
        "wallclock": int(time.time()),
    }))
    return 0


def cmd_slots_since(argv: list[str]) -> int:
    if not argv:
        print(json.dumps({"error": "usage: slots-since <slot-number>"}))
        return 1
    try:
        ref = int(argv[0])
    except ValueError:
        print(json.dumps({"error": "slot must be an integer"}))
        return 1
    slot = get_current_slot()
    epoch_length, fallback_used = _epoch_length()
    print(json.dumps({
        "current_slot": slot,
        "reference_slot": ref,
        "slots_since": slot - ref,
        "epochs_since": (slot - ref) // epoch_length,
        "epoch_length_slots": epoch_length,
        "fallback_used": fallback_used,
    }))
    return 0


def cmd_tx_status(argv: list[str]) -> int:
    """'landed' iff output 0 of the tx is resolvable on chain. 'unknown'
    means pending, never-submitted, or already-fully-spent; callers must
    wait and re-check rather than rebroadcasting.
    """
    if not argv:
        print(json.dumps({"error": "usage: tx-status <tx-hash-hex>"}))
        return 1
    txid = argv[0].strip().lower()
    if not _HEX64.match(txid):
        print(json.dumps({"error": "tx-hash must be 64 hex chars"}))
        return 1
    try:
        resolve_utxo(txid, 0)
        print(json.dumps({"tx_hash": txid, "status": "landed"}))
        return 0
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg:
            print(json.dumps({"tx_hash": txid, "status": "unknown"}))
            return 0
        print(json.dumps({"tx_hash": txid, "status": "error", "error": str(e)}))
        return 1


def cmd_juror_prep(argv: list[str]) -> int:
    """Prepare a juror commit: generate salt, compute hash, write salt to
    state.json. Salt never appears on stdout. Returns only the commit hash.

    Usage: juror-prep <dispute_id_hex> <uphold|overturn> <reveal_deadline_slot>

    Effect:
      - Reads ./state.json (must exist as a JSON object).
      - Generates 32 random bytes (os.urandom).
      - Computes commit_hash = blake2b_256(verdict_byte ++ salt).
      - Sets ./state.json.pending_tx = {
            "role_action": "juror_commit",
            "dispute_id": <id>,
            "verdict": <uphold|overturn>,
            "salt_hex": <salt_hex>,               # stored locally, NEVER printed
            "reveal_deadline_slot": <int>,
            "commit_hash": <hex>,
            "tx_hash": null,
            "prepared_ts": <unix_ts>
        }
      - Prints {"ok": true, "commit_hash": <hex>, "reveal_deadline_slot": <int>}.

    The juror then builds+signs+broadcasts the commit tx using commit_hash
    from this output, and uses `chain.py` (a separate call) to fill in
    tx_hash after broadcast.
    """
    if len(argv) != 3:
        print(json.dumps({"error": "usage: juror-prep <dispute_id> <uphold|overturn> <reveal_deadline>"}))
        return 1
    dispute_id = argv[0].strip().lower()
    verdict    = argv[1].strip().lower()
    try:
        reveal_deadline = int(argv[2])
    except ValueError:
        print(json.dumps({"error": "reveal_deadline must be an integer slot"}))
        return 1
    if not _HEX_ID.match(dispute_id):
        print(json.dumps({"error": "dispute_id must be hex"}))
        return 1
    if verdict not in _VERDICT_BYTE:
        print(json.dumps({"error": "verdict must be 'uphold' or 'overturn'"}))
        return 1

    state_path = Path.cwd() / "state.json"
    if not state_path.exists():
        print(json.dumps({"error": "state.json not found in cwd"}))
        return 1
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            raise ValueError("state.json is not a JSON object")
    except Exception as e:
        print(json.dumps({"error": f"state.json unreadable: {e}"}))
        return 1

    if isinstance(state.get("pending_tx"), dict):
        print(json.dumps({"error": "pending_tx already exists; reconcile first"}))
        return 1

    salt = os.urandom(32)
    commit_hash = hashlib.blake2b(_VERDICT_BYTE[verdict] + salt, digest_size=32).hexdigest()

    state["pending_tx"] = {
        "role_action":         "juror_commit",
        "dispute_id":          dispute_id,
        "verdict":             verdict,
        "salt_hex":            salt.hex(),       # stored, never printed
        "reveal_deadline_slot": reveal_deadline,
        "commit_hash":         commit_hash,
        "tx_hash":             None,
        "prepared_ts":         int(time.time()),
    }
    _write_state_atomic(state_path, state)

    # Return ONLY the hash + deadline. Never return the salt.
    print(json.dumps({
        "ok": True,
        "commit_hash": commit_hash,
        "reveal_deadline_slot": reveal_deadline,
    }))
    return 0


COMMANDS = {
    "balance":     cmd_balance,
    "utxos":       cmd_utxos,
    "tip":         cmd_tip,
    "slots-since": cmd_slots_since,
    "tx-status":   cmd_tx_status,
    "juror-prep":  cmd_juror_prep,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"usage: chain.py <{'|'.join(COMMANDS)}> [args...]", file=sys.stderr)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
