#!/usr/bin/env python3
"""Transfer AP3X from the master faucet wallet to a target address.

Used by autonomous agents when their own wallet balance is insufficient for
a required action. Invoked via Bash:

    python3 ~/vector-agents/bin/pull_from_master.py \\
        --to <bech32_address> --amount <AP3X_float>

Prints a JSON object on stdout: {"ok": true, "tx_hash": "...", "to": "...",
"amount_ap3x": 50.0, "master_balance_after_ap3x": 699.4}

On error, prints {"ok": false, "error": "..."} and exits non-zero.

Safety:
- Hardcoded master wallet path (~/vector-agents/master/wallet.skey).
- Refuses transfers > MAX_PULL_AP3X (100) per invocation.
- Refuses if master balance would drop below RESERVE_AP3X (20).
- Prints the recipient address + amount before broadcasting for the log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MAX_PULL_AP3X = 100
RESERVE_AP3X = 20

HOME = Path(os.environ.get("HOME", "/home/user"))
MASTER_SKEY = HOME / "vector-agents" / "master" / "wallet.skey"
SDK_PATH = HOME / "code" / "vector-agent-modules" / "Module-3" / "python"


def fail(msg: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stdout)
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="Bech32 recipient address")
    parser.add_argument(
        "--amount",
        type=float,
        required=True,
        help=f"Amount in AP3X (max {MAX_PULL_AP3X}, 1 AP3X = 1_000_000 lovelace)",
    )
    args = parser.parse_args()

    if args.amount <= 0 or args.amount > MAX_PULL_AP3X:
        fail(f"amount must be in (0, {MAX_PULL_AP3X}]; got {args.amount}")

    if not MASTER_SKEY.exists():
        fail(f"master wallet not found at {MASTER_SKEY}")

    sys.path.insert(0, str(SDK_PATH))
    try:
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
    except ImportError as e:
        fail(f"SDK import failed: {e}")

    ctx = OgmiosHttpContext()
    try:
        master_skey, _master_vkey, master_addr = load_wallet(str(MASTER_SKEY))
    except Exception as e:
        fail(f"load_wallet failed: {e}")

    master_balance = sum(u.output.amount.coin for u in ctx.utxos(master_addr))
    amount_lovelace = int(args.amount * 1_000_000)
    reserve_lovelace = RESERVE_AP3X * 1_000_000

    if master_balance - amount_lovelace < reserve_lovelace:
        fail(
            f"master would drop below {RESERVE_AP3X} AP3X reserve "
            f"(balance={master_balance / 1_000_000:.2f}, requested={args.amount})"
        )

    try:
        to_addr = Address.from_primitive(args.to)
    except Exception as e:
        fail(f"invalid --to address: {e}")

    tb = TransactionBuilder(ctx)
    for u in ctx.utxos(master_addr):
        tb.add_input(u)
    tb.add_output(TransactionOutput(to_addr, Value(amount_lovelace)))
    try:
        tx = tb.build_and_sign([master_skey], change_address=master_addr)
    except Exception as e:
        fail(f"build_and_sign failed: {e}")

    try:
        tx_hash = submit_tx(tx)
    except Exception as e:
        fail(f"submit_tx failed: {e}")

    master_balance_after = master_balance - amount_lovelace
    print(
        json.dumps(
            {
                "ok": True,
                "tx_hash": tx_hash,
                "to": args.to,
                "amount_ap3x": args.amount,
                "master_balance_after_ap3x": master_balance_after / 1_000_000,
            }
        )
    )


if __name__ == "__main__":
    main()
