"""
Game 6: Treasury Batch Replenishment

Monitors treasury batch UTxO count and replenishes when below threshold.
Replenishment trigger: available batches < MIN_TREASURY_BATCHES (5),
i.e. < 2,500 AP3X in batch UTxOs.

Usage:
    # Check treasury status and replenish if needed:
    nix-shell shell.nix --run "python scripts/treasury_replenish.py"

    # Check status only (no replenishment):
    nix-shell shell.nix --run "python scripts/treasury_replenish.py --status"

    # Force replenishment even if above threshold:
    nix-shell shell.nix --run "python scripts/treasury_replenish.py --force --target 10"
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")

# Spec §7.1, §9.4
MIN_TREASURY_BATCHES = 5
BATCH_SIZE_APEX = 500
BATCH_SIZE_LOVELACE = BATCH_SIZE_APEX * 1_000_000
TX_WAIT = 10


def script_hash_to_address(script_hash_hex: str) -> str:
    from pycardano import Address, Network
    from pycardano.hash import ScriptHash
    sh = ScriptHash.from_primitive(bytes.fromhex(script_hash_hex))
    return str(Address(payment_part=sh, network=Network.MAINNET))


async def check_and_replenish(status_only: bool, force: bool, target_batches: int):
    state = json.load(open(DEPLOY_STATE_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    holders = state.get("holders", {})
    treasury_hash = holders.get("treasury", {}).get("hash", "")
    if not treasury_hash:
        print("[ERROR] Treasury holder hash not found. Run deploy.py first.")
        return

    treasury_addr = script_hash_to_address(treasury_hash)

    from vector_agent import VectorAgent
    from pycardano import Address as PycAddr
    from pycardano.plutus import RawPlutusData

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        # Query treasury UTxOs
        treasury_utxos = await agent.context.async_utxos(
            PycAddr.from_primitive(treasury_addr)
        )

        # Count batch UTxOs (those with TreasuryBatchDatum)
        batch_utxos = []
        total_lovelace = 0
        for u in treasury_utxos:
            amount = u.output.amount
            lv = amount.coin if hasattr(amount, "coin") else (amount if isinstance(amount, int) else 0)
            total_lovelace += lv

            if u.output.datum is not None:
                try:
                    d = u.output.datum.data if isinstance(u.output.datum, RawPlutusData) else u.output.datum
                    if hasattr(d, "tag") and hasattr(d, "value") and len(d.value) >= 2:
                        batch_id = d.value[0]
                        active_tag = d.value[1]
                        is_active = hasattr(active_tag, "tag") and active_tag.tag == 122
                        batch_utxos.append({
                            "batch_id": batch_id,
                            "active": is_active,
                            "lovelace": lv,
                            "tx_hash": str(u.input.transaction_id),
                            "output_index": u.input.index,
                        })
                except Exception:
                    pass

        active_batches = [b for b in batch_utxos if b["active"]]
        active_lovelace = sum(b["lovelace"] for b in active_batches)

        print("=" * 50)
        print("Game 6: Treasury Status")
        print("=" * 50)
        print(f"Treasury address:   {treasury_addr[:30]}...")
        print(f"Total UTxOs:        {len(treasury_utxos)}")
        print(f"Total lovelace:     {total_lovelace:,} ({total_lovelace / 1_000_000:.1f} AP3X)")
        print(f"Batch UTxOs:        {len(batch_utxos)} ({len(active_batches)} active)")
        print(f"Active batch value: {active_lovelace:,} ({active_lovelace / 1_000_000:.1f} AP3X)")
        print(f"Threshold:          {MIN_TREASURY_BATCHES} batches ({MIN_TREASURY_BATCHES * BATCH_SIZE_APEX} AP3X)")

        if active_batches:
            print("\nActive batches:")
            for b in sorted(active_batches, key=lambda x: x["batch_id"]):
                print(f"  Batch #{b['batch_id']}: {b['lovelace'] / 1_000_000:.1f} AP3X ({b['tx_hash'][:16]}...)")

        needs_replenishment = len(active_batches) < MIN_TREASURY_BATCHES
        if needs_replenishment:
            print(f"\n[ALERT] Treasury below threshold! {len(active_batches)} < {MIN_TREASURY_BATCHES}")
        else:
            print(f"\n[OK] Treasury healthy ({len(active_batches)} >= {MIN_TREASURY_BATCHES})")

        if status_only:
            return

        # Determine how many batches to create
        if force:
            batches_needed = max(0, target_batches - len(active_batches))
        elif needs_replenishment:
            batches_needed = MIN_TREASURY_BATCHES - len(active_batches)
        else:
            print("No replenishment needed.")
            return

        if batches_needed <= 0:
            print("No new batches needed.")
            return

        cost = batches_needed * BATCH_SIZE_LOVELACE
        balance = await agent.get_balance()
        print(f"\nReplenishment plan:")
        print(f"  Batches to create: {batches_needed}")
        print(f"  Cost:              {cost / 1_000_000} AP3X + fees")
        print(f"  Wallet balance:    {balance.ada} AP3X")

        if balance.lovelace < cost + 5_000_000:
            print(f"[ERROR] Insufficient funds. Need {cost / 1_000_000 + 5} AP3X.")
            return

        # Create new batches
        from pycardano import TransactionBuilder, TransactionOutput
        from vector_agent.governance.datums import build_treasury_batch_datum

        treasury_pycaddr = PycAddr.from_primitive(treasury_addr)
        existing_ids = [b["batch_id"] for b in batch_utxos]
        next_id = max(existing_ids, default=0) + 1

        tx_hashes = state.get("tx_hashes", {})

        for i in range(batches_needed):
            bid = next_id + i
            batch_datum = build_treasury_batch_datum(bid, True)

            builder = TransactionBuilder(agent.context)
            builder.add_input_address(agent._wallet.payment_address)
            builder.fee_buffer = 200_000
            builder.add_output(
                TransactionOutput(
                    treasury_pycaddr,
                    BATCH_SIZE_LOVELACE,
                    datum=RawPlutusData(batch_datum.data),
                )
            )
            tx = builder.build_and_sign(
                signing_keys=[agent._wallet.payment_signing_key],
                change_address=agent._wallet.payment_address,
            )
            tx_hash = str(tx.id)
            await agent.context.submit_tx_cbor(tx.to_cbor())
            print(f"  [treasury_batch_{bid}] Created: {tx_hash}")

            tx_hashes[f"treasury_batch_{bid}"] = tx_hash

            if i < batches_needed - 1:
                await asyncio.sleep(TX_WAIT)

        # Save updated state
        state["tx_hashes"] = tx_hashes
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        final_balance = await agent.get_balance()
        print(f"\nReplenished {batches_needed} batch(es). Final balance: {final_balance.ada} AP3X")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor and replenish governance treasury batches")
    parser.add_argument("--status", action="store_true", help="Check status only, no replenishment")
    parser.add_argument("--force", action="store_true", help="Force replenishment even if above threshold")
    parser.add_argument("--target", type=int, default=MIN_TREASURY_BATCHES,
                        help=f"Target number of active batches (default {MIN_TREASURY_BATCHES})")
    args = parser.parse_args()

    asyncio.run(check_and_replenish(args.status, args.force, args.target))
