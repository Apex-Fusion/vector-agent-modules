#!/usr/bin/env python3
"""Approved faucet-request helper.

Usage (from an agent's working directory):
    python3 ~/vector-agents/bin/scripts/faucet_request.py [--amount LOVELACE]

Policy (hard-coded, non-negotiable from the agent's side):
  - Valid agent cwd:            ~/vector-agents/agents/m[1-9]-[a-z]+
  - Lifetime cap per agent:     300 AP3X
  - Global cap across agents:   700 AP3X (master is ~1000 AP3X; keep headroom)
  - Per-request max:            100 AP3X
  - Cooldown between requests:  11h
  - Agent must hold < 85 AP3X to request (above largest single-run stake)

Ledger writes are atomic (tmp + os.replace) and use "reserve → commit"
semantics: we write a tentative entry BEFORE submit_tx, then finalize
after. A crash between the two leaves the quota consumed (conservative;
better than allowing a retry to double-draw).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

BASE   = Path.home() / "vector-agents"
MASTER = BASE / "master"
LEDGER = MASTER / "faucet-ledger.json"
LEDGER_LOCK = MASTER / "faucet-ledger.lock"
AGENTS_ROOT = (BASE / "agents").resolve()
RESERVATIONS_MAX_AGE_S = 90 * 24 * 3600   # keep last 90d of reservation records

sys.path.insert(0, str(BASE / "shared/Module-3/python"))
from reputation_staking.ogmios_backend import (  # noqa: E402
    OgmiosHttpContext,
    load_wallet,
    submit_tx,
)
from pycardano import (  # noqa: E402
    Address,
    TransactionBuilder,
    TransactionOutput,
    Value,
)

LIFETIME_CAP_LOVELACE    = 300_000_000     # 300 AP3X per agent
GLOBAL_CAP_LOVELACE      = 700_000_000     # 700 AP3X across all agents
PER_REQUEST_MAX_LOVELACE = 100_000_000     # 100 AP3X per request
COOLDOWN_S               = 11 * 3600       # 11h between requests
REQUESTING_MAX_BALANCE   = 85_000_000      # agent must hold < 85 AP3X
AGENT_NAME_RE            = re.compile(r"^m[1-9]-[a-z]+$")


def load_ledger() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _prune_reservations(ledger: dict) -> None:
    """Drop reservation records older than RESERVATIONS_MAX_AGE_S.

    Totals (`entry["total"]`, `entry["requests"]`) are preserved; only the
    historical detail list is trimmed. Keeps faucet-ledger.json bounded.
    """
    cutoff = time.time() - RESERVATIONS_MAX_AGE_S
    for entry in ledger.values():
        rs = entry.get("reservations")
        if isinstance(rs, list):
            entry["reservations"] = [r for r in rs if r.get("ts", 0) >= cutoff]


def save_ledger_atomic(ledger: dict) -> None:
    _prune_reservations(ledger)
    tmp = LEDGER.with_suffix(LEDGER.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    os.replace(tmp, LEDGER)


def global_total(ledger: dict) -> int:
    return sum(int(e.get("total", 0)) for e in ledger.values())


def infer_agent_name() -> str:
    """The agent's cwd IS its name (e.g. ~/vector-agents/agents/m3-staker).

    Hard-validates the cwd is under $AGENTS_ROOT and the basename matches
    the agent regex. Any deviation (cd elsewhere, symlinked dir, etc.)
    aborts to prevent quota spoofing.
    """
    cwd = Path.cwd().resolve()
    try:
        rel = cwd.relative_to(AGENTS_ROOT)
    except ValueError:
        raise SystemExit("faucet_request.py must be run from an agent's cwd")
    if len(rel.parts) != 1:
        raise SystemExit("cwd must be the agent's top-level dir, not a subdir")
    name = rel.parts[0]
    if not AGENT_NAME_RE.match(name):
        raise SystemExit(f"invalid agent name: {name}")
    return name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=int, default=50_000_000,
                    help="Lovelace (1 AP3X = 1_000_000). Default 50 AP3X.")
    args = ap.parse_args()

    if args.amount <= 0 or args.amount > PER_REQUEST_MAX_LOVELACE:
        print(json.dumps({"error": f"amount must be in (0, {PER_REQUEST_MAX_LOVELACE}]"}))
        return 1

    # Serialize all faucet requests via a ledger flock. Prevents TOCTOU
    # between the load_ledger → global-cap-check → save_ledger_atomic
    # window if two agents ever run concurrently.
    MASTER.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_LOCK, "w") as _lock:
        try:
            fcntl.flock(_lock.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            print(json.dumps({"error": f"could not acquire ledger lock: {e}"}))
            return 1
        return _main_locked(args)


def _main_locked(args: argparse.Namespace) -> int:
    agent = infer_agent_name()
    addr_file = Path.cwd() / "wallet.addr"
    if not addr_file.exists():
        print(json.dumps({"error": "no wallet.addr in cwd"}))
        return 1
    agent_addr = Address.from_primitive(addr_file.read_text().strip())

    ctx = OgmiosHttpContext()

    # Balance check — don't fund agents that aren't actually low.
    bal = sum(u.output.amount.coin for u in ctx.utxos(agent_addr))
    if bal >= REQUESTING_MAX_BALANCE:
        print(json.dumps({
            "error": "balance too high to request funds",
            "balance_lovelace": bal,
            "threshold_lovelace": REQUESTING_MAX_BALANCE,
        }))
        return 1

    # Lifetime + global + cooldown checks (atomic read).
    ledger = load_ledger()
    entry = ledger.get(agent, {"total": 0, "last_ts": 0, "requests": 0})
    if entry["total"] + args.amount > LIFETIME_CAP_LOVELACE:
        print(json.dumps({
            "error": "lifetime cap would be exceeded",
            "agent_total_lovelace": entry["total"],
            "cap_lovelace": LIFETIME_CAP_LOVELACE,
        }))
        return 1
    if global_total(ledger) + args.amount > GLOBAL_CAP_LOVELACE:
        print(json.dumps({
            "error": "global cap would be exceeded",
            "global_total_lovelace": global_total(ledger),
            "cap_lovelace": GLOBAL_CAP_LOVELACE,
        }))
        return 1
    now = time.time()
    if now - entry["last_ts"] < COOLDOWN_S:
        print(json.dumps({
            "error": "cooldown in effect",
            "seconds_remaining": int(COOLDOWN_S - (now - entry["last_ts"])),
        }))
        return 1

    # Reserve quota atomically BEFORE tx submit. Conservative: a crash
    # between reserve and submit leaves quota consumed (no duplicate
    # draws on retry). Admin can manually adjust the ledger to release
    # quota if the reservation is truly lost.
    entry["total"]    = entry.get("total", 0) + args.amount
    entry["last_ts"]  = now
    entry["requests"] = entry.get("requests", 0) + 1
    entry.setdefault("reservations", []).append({
        "ts": now, "amount": args.amount, "status": "reserved",
    })
    ledger[agent] = entry
    save_ledger_atomic(ledger)

    # Now the tx itself.
    master_skey, _, master_addr = load_wallet(str(MASTER / "wallet.skey"))
    builder = TransactionBuilder(ctx)
    builder.add_input_address(master_addr)
    builder.add_output(TransactionOutput(agent_addr, Value(args.amount)))
    signed = builder.build_and_sign([master_skey], change_address=master_addr)
    tx_hash = submit_tx(signed)

    # Finalize the reservation.
    entry["reservations"][-1]["status"] = "committed"
    entry["reservations"][-1]["tx_hash"] = tx_hash
    entry["last_tx"] = tx_hash
    ledger[agent] = entry
    save_ledger_atomic(ledger)

    print(json.dumps({
        "ok": True,
        "tx_hash": tx_hash,
        "amount_lovelace": args.amount,
        "amount_ap3x": args.amount / 1_000_000,
        "agent_lifetime_total_lovelace": entry["total"],
        "global_total_lovelace": global_total(ledger),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
