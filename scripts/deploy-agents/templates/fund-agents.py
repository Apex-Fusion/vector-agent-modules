#!/usr/bin/env python3
"""Fund the 9 agent wallets from the master wallet.

Sends FUND_AMOUNT lovelace to each agent address in a single multi-output
transaction. Idempotent-ish: if an agent already has ≥ MIN_BALANCE, it is
skipped. Aborts cleanly if the master wallet can't cover the total.

Run from anywhere — paths are resolved relative to $HOME/vector-agents.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pycardano import (
    Address,
    TransactionBuilder,
    TransactionOutput,
    Value,
)

# Reuse the Module-3 SDK helpers for Ogmios connectivity + wallet loading.
sys.path.insert(0, str(Path.home() / "vector-agents/shared/Module-3/python"))
from reputation_staking.ogmios_backend import (  # noqa: E402
    OgmiosHttpContext,
    load_wallet,
    submit_tx,
    wait_for_tx,
)

BASE = Path.home() / "vector-agents"
MASTER = BASE / "master"
AGENTS_DIR = BASE / "agents"

# 1 AP3X = 1_000_000 DFM (base units, same convention as lovelace)
FUND_AMOUNT = 100_000_000       # 100 AP3X per agent on first fund
MIN_BALANCE = 20_000_000        # skip agents already holding ≥ 20 AP3X

AGENTS = [
    "m1-claimer", "m1-auditor", "m1-juror",
    "m3-staker",  "m3-endorser", "m3-challenger",
    "m6-proposer", "m6-critic",   "m6-endorser",
]


def agent_balance(ctx: OgmiosHttpContext, addr: Address) -> int:
    return sum(u.output.amount.coin for u in ctx.utxos(addr))


def main() -> int:
    if not (MASTER / "wallet.skey").exists():
        print("ERROR: master wallet not found. Run bootstrap.sh first.", file=sys.stderr)
        return 1

    ctx = OgmiosHttpContext()
    master_skey, _master_vkey, master_addr = load_wallet(str(MASTER / "wallet.skey"))

    # Figure out who needs funding.
    targets: list[tuple[str, Address]] = []
    for name in AGENTS:
        addr_file = AGENTS_DIR / name / "wallet.addr"
        if not addr_file.exists():
            print(f"  skip {name}: no wallet.addr (did bootstrap.sh --continue run?)")
            continue
        addr = Address.from_primitive(addr_file.read_text().strip())
        bal = agent_balance(ctx, addr)
        if bal >= MIN_BALANCE:
            print(f"  skip {name}: already has {bal/1_000_000:.1f} AP3X")
            continue
        targets.append((name, addr))

    if not targets:
        print("All agents already funded. Nothing to do.")
        return 0

    master_bal = agent_balance(ctx, master_addr)
    needed = FUND_AMOUNT * len(targets) + 2_000_000  # + fee budget
    print(f"Master balance: {master_bal/1_000_000:.1f} AP3X")
    print(f"Need to fund {len(targets)} agents = {needed/1_000_000:.1f} AP3X (incl fee)")
    if master_bal < needed:
        print("ERROR: master wallet underfunded. Top it up and retry.", file=sys.stderr)
        return 1

    # Build a single multi-output tx.
    builder = TransactionBuilder(ctx)
    builder.add_input_address(master_addr)
    for name, addr in targets:
        builder.add_output(TransactionOutput(addr, Value(FUND_AMOUNT)))
        print(f"  → {name}: {FUND_AMOUNT/1_000_000:.0f} AP3X")

    signed = builder.build_and_sign([master_skey], change_address=master_addr)
    tx_hash = submit_tx(signed)
    print(f"\nSubmitted: {tx_hash}")
    print("Waiting for confirmation before returning...")
    wait_for_tx(40)

    # Initialise faucet-ledger so later faucet_request.py calls start from
    # a clean slate per agent — this is the initial funding, not an
    # agent-initiated request, so it does NOT consume lifetime quota.
    ledger_path = MASTER / "faucet-ledger.json"
    if not ledger_path.exists():
        ledger_path.write_text("{}\n")
        print(f"Initialised {ledger_path}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
