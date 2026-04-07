"""
Module 6: Recover locked funds from ALL deployments.

Scans every deploy_state*.json in wallets/ to find all unique holder and
validator addresses, then sweeps recoverable funds back to the wallet.

Recoverable:
  - Holder script UTxOs (params/oracle/treasury — always-succeeds validators)

Not recoverable:
  - Proposal UTxOs — require burning proposal tokens that were never minted
  - Endorsement/critique UTxOs — validator logic rejects (missing ref inputs
    from swept holders, or contract logic mismatch)

Usage:
    nix-shell shell.nix --run "python scripts/recover_funds.py"
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

GAME6_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(GAME6_ROOT))
load_dotenv(GAME6_ROOT / ".env")

WALLETS_DIR = GAME6_ROOT / "wallets"
SKEY_PATH = WALLETS_DIR / "payment.skey"


def collect_all_addresses():
    """Scan all deploy_state*.json files and collect unique addresses."""
    from pycardano.plutus import PlutusV3Script, script_hash as compute_script_hash
    from pycardano import Address
    from pycardano.network import Network

    holders = {}   # addr -> {name, compiled_code, source}
    validators = {}  # addr -> {title, compiled_code, source}

    for f in sorted(WALLETS_DIR.glob("deploy_state*.json*")):
        state = json.load(open(f))
        fname = f.name

        for name, info in state.get("holders", {}).items():
            addr = info.get("address", "")
            if addr and addr not in holders:
                holders[addr] = {
                    "name": name,
                    "compiled_code": info["compiled_code"],
                    "source": fname,
                }

        for title, info in state.get("validators", {}).items():
            code = info.get("compiled_code", "")
            if code:
                script = PlutusV3Script(bytes.fromhex(code))
                sh = compute_script_hash(script)
                addr = str(Address(payment_part=sh, network=Network.MAINNET))
                if addr not in validators:
                    validators[addr] = {
                        "title": title,
                        "compiled_code": code,
                        "source": fname,
                    }

    return holders, validators


async def sweep_holder(agent, label: str, script_cbor_hex: str, script_addr_str: str):
    """Sweep all UTXOs from an always-succeeds holder script address."""
    from pycardano import (
        TransactionBuilder, PlutusV3Script,
        Redeemer, ExecutionUnits, PlutusData,
    )

    utxos = await agent.context.async_utxos(script_addr_str)
    if not utxos:
        print(f"  [{label}] Empty — skip")
        return 0

    total_lovelace = sum(
        (u.output.amount if isinstance(u.output.amount, int) else u.output.amount.coin)
        for u in utxos
    )
    print(f"  [{label}] {len(utxos)} UTXOs, {total_lovelace / 1_000_000:.1f} ADA")

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    recovered = 0

    batch_size = 8
    for batch_start in range(0, len(utxos), batch_size):
        batch = utxos[batch_start:batch_start + batch_size]

        builder = TransactionBuilder(agent.context)
        builder.add_input_address(agent._wallet.payment_address)

        for u in batch:
            redeemer = Redeemer(PlutusData(), ExecutionUnits(mem=500_000, steps=200_000_000))
            builder.add_script_input(u, script=script, redeemer=redeemer)

        wallet_utxos = await agent.context.async_utxos(str(agent._wallet.payment_address))
        for wu in wallet_utxos:
            val = wu.output.amount
            lv = val if isinstance(val, int) else val.coin
            if lv >= 5_000_000:
                builder.collaterals = [wu]
                break

        builder.fee_buffer = 300_000

        try:
            tx = builder.build_and_sign(
                signing_keys=[agent._wallet.payment_signing_key],
                change_address=agent._wallet.payment_address,
            )
            tx_cbor = tx.to_cbor()
            tx_hex = tx_cbor.hex() if isinstance(tx_cbor, bytes) else tx_cbor
            tx_hash = await agent._context._submit.submit(tx_hex)

            batch_lv = sum(
                (u.output.amount if isinstance(u.output.amount, int) else u.output.amount.coin)
                for u in batch
            )
            recovered += batch_lv
            print(f"    TX: {tx_hash} ({batch_lv / 1_000_000:.1f} ADA)")

            if batch_start + batch_size < len(utxos):
                await asyncio.sleep(25)
        except Exception as e:
            err_str = str(e)
            # Truncate long error messages
            if len(err_str) > 120:
                err_str = err_str[:120] + "..."
            print(f"    ERROR: {err_str}")
            # Try one-at-a-time
            for u in batch:
                try:
                    b2 = TransactionBuilder(agent.context)
                    b2.add_input_address(agent._wallet.payment_address)
                    b2.add_script_input(u, script=script,
                                        redeemer=Redeemer(PlutusData(), ExecutionUnits(mem=500_000, steps=200_000_000)))
                    wus = await agent.context.async_utxos(str(agent._wallet.payment_address))
                    for wu in wus:
                        val = wu.output.amount
                        if (isinstance(val, int) and val >= 5_000_000) or (hasattr(val, 'coin') and val.coin >= 5_000_000):
                            b2.collaterals = [wu]
                            break
                    b2.fee_buffer = 300_000
                    t2 = b2.build_and_sign(
                        signing_keys=[agent._wallet.payment_signing_key],
                        change_address=agent._wallet.payment_address,
                    )
                    tc2 = t2.to_cbor()
                    th2 = tc2.hex() if isinstance(tc2, bytes) else tc2
                    h = await agent._context._submit.submit(th2)
                    ulv = u.output.amount if isinstance(u.output.amount, int) else u.output.amount.coin
                    recovered += ulv
                    print(f"    TX (single): {h} ({ulv / 1_000_000:.1f} ADA)")
                    await asyncio.sleep(25)
                except Exception as e2:
                    e2s = str(e2)
                    if len(e2s) > 100:
                        e2s = e2s[:100] + "..."
                    print(f"    SKIP: {e2s}")

    return recovered


async def recover():
    from vector_agent import VectorAgent

    print("=" * 60)
    print("Module 6: Fund Recovery (All Deployments)")
    print("=" * 60)

    holders, validators = collect_all_addresses()
    print(f"\nScanned {len(list(WALLETS_DIR.glob('deploy_state*')))} deploy state files")
    print(f"Found {len(holders)} unique holder addresses, {len(validators)} unique validator addresses")

    ogmios_url = os.getenv("VECTOR_OGMIOS_URL")
    submit_url = os.getenv("VECTOR_SUBMIT_URL")
    skey_path = str(SKEY_PATH.absolute())

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
        spend_limit_per_tx=10_000_000_000,
        spend_limit_daily=10_000_000_000,
    ) as agent:
        balance = await agent.get_balance()
        print(f"\nWallet: {agent.address}")
        print(f"Starting balance: {balance.ada} ADA")

        total_recovered = 0
        total_locked = 0

        # ============================================================
        # Phase 1: Sweep holders (always-succeeds)
        # ============================================================
        print(f"\n{'─' * 60}")
        print("Phase 1: Holder Scripts (always-succeeds)")
        print(f"{'─' * 60}")

        for addr, info in sorted(holders.items(), key=lambda x: x[1]["name"]):
            recovered = await sweep_holder(
                agent,
                label=f"{info['name']} ({info['source']})",
                script_cbor_hex=info["compiled_code"],
                script_addr_str=addr,
            )
            total_recovered += recovered
            if recovered > 0:
                await asyncio.sleep(25)

        # ============================================================
        # Phase 2: Check validators
        # ============================================================
        print(f"\n{'─' * 60}")
        print("Phase 2: Validator Addresses")
        print(f"{'─' * 60}")

        for addr, info in sorted(validators.items(), key=lambda x: x[1]["title"]):
            utxos = await agent.context.async_utxos(addr)
            if not utxos:
                continue

            total_lv = sum(
                (u.output.amount if isinstance(u.output.amount, int) else u.output.amount.coin)
                for u in utxos
            )
            total_locked += total_lv

            title = info["title"]
            # Proposals are unrecoverable (need token burn)
            if "proposal" in title:
                print(f"  [LOCKED] {title:45s} {total_lv / 1_000_000:>8.1f} ADA  ({len(utxos)} UTXOs, {info['source']})")
            elif "endorsement" in title or "critique" in title:
                print(f"  [LOCKED] {title:45s} {total_lv / 1_000_000:>8.1f} ADA  ({len(utxos)} UTXOs, {info['source']})")
            else:
                print(f"  [?????] {title:45s} {total_lv / 1_000_000:>8.1f} ADA  ({len(utxos)} UTXOs)")

        # ============================================================
        # Final report
        # ============================================================
        await asyncio.sleep(10)
        final = await agent.get_balance()
        net = (final.lovelace - balance.lovelace) / 1_000_000

        print(f"\n{'=' * 60}")
        print("Recovery Complete")
        print(f"{'=' * 60}")
        print(f"Starting balance:  {balance.ada:>10} ADA")
        print(f"Final balance:     {final.ada:>10} ADA")
        print(f"Net recovered:     {net:>10.1f} ADA (after fees)")
        print(f"Still locked:      {total_locked / 1_000_000:>10.1f} ADA (validators)")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(recover())
