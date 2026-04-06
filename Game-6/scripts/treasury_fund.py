"""
Game 6: Treasury Funding Pipeline

Creates new treasury batch UTxOs at the treasury holder address.
Each batch holds BATCH_SIZE AP3X and can be consumed during proposal adoption.

Usage:
    # Fund 3 new batches of 500 AP3X each (1,500 AP3X total):
    nix-shell shell.nix --run "python scripts/treasury_fund.py --batches 3 --size 500"

    # Fund with defaults (1 batch, 500 AP3X):
    nix-shell shell.nix --run "python scripts/treasury_fund.py"

    # Dry-run (show what would be created):
    nix-shell shell.nix --run "python scripts/treasury_fund.py --dry-run"

Prerequisites:
    - Wallet funded with enough AP3X to cover batch size × count + fees
    - Full deployment completed (deploy.py)
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
DEFAULT_BATCH_SIZE_APEX = 500
TX_WAIT = 10


def script_hash_to_address(script_hash_hex: str) -> str:
    from pycardano import Address, Network
    from pycardano.hash import ScriptHash
    sh = ScriptHash.from_primitive(bytes.fromhex(script_hash_hex))
    return str(Address(payment_part=sh, network=Network.MAINNET))


async def fund_treasury(num_batches: int, batch_size_apex: int, dry_run: bool):
    state = json.load(open(DEPLOY_STATE_FILE))
    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    holders = state.get("holders", {})
    treasury_hash = holders.get("treasury", {}).get("hash", "")
    if not treasury_hash:
        print("[ERROR] Treasury holder hash not found in deploy_state. Run deploy.py first.")
        return

    treasury_addr = script_hash_to_address(treasury_hash)
    batch_size_lovelace = batch_size_apex * 1_000_000
    total_cost = batch_size_lovelace * num_batches

    # Determine next batch ID from deploy_state
    tx_hashes = state.get("tx_hashes", {})
    existing_batch_ids = [
        int(k.replace("treasury_batch_", ""))
        for k in tx_hashes
        if k.startswith("treasury_batch_")
    ]
    next_batch_id = max(existing_batch_ids, default=0) + 1

    print("=" * 50)
    print("Game 6: Treasury Funding Pipeline")
    print("=" * 50)
    print(f"Treasury address: {treasury_addr[:30]}...")
    print(f"Batch size:       {batch_size_apex} AP3X ({batch_size_lovelace} lovelace)")
    print(f"Batches to fund:  {num_batches}")
    print(f"Total cost:       {total_cost / 1_000_000} AP3X + fees")
    print(f"Next batch ID:    {next_batch_id}")

    if dry_run:
        print("\n[DRY RUN] Would create:")
        for i in range(num_batches):
            bid = next_batch_id + i
            print(f"  Batch #{bid}: {batch_size_apex} AP3X at {treasury_addr[:30]}...")
        return

    from vector_agent import VectorAgent
    from vector_agent.governance.datums import build_treasury_batch_datum

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        balance = await agent.get_balance()
        print(f"\nWallet balance: {balance.ada} AP3X")

        if balance.lovelace < total_cost + 5_000_000:
            print(f"[ERROR] Insufficient funds. Need {total_cost / 1_000_000 + 5} AP3X, have {balance.ada}")
            return

        from pycardano import TransactionBuilder, TransactionOutput, Address as PycAddr
        from pycardano.plutus import RawPlutusData

        treasury_pycaddr = PycAddr.from_primitive(treasury_addr)

        for i in range(num_batches):
            bid = next_batch_id + i
            key = f"treasury_batch_{bid}"

            batch_datum = build_treasury_batch_datum(bid, True)

            builder = TransactionBuilder(agent.context)
            builder.add_input_address(agent._wallet.payment_address)
            builder.fee_buffer = 200_000
            builder.add_output(
                TransactionOutput(
                    treasury_pycaddr,
                    batch_size_lovelace,
                    datum=RawPlutusData(batch_datum.data),
                )
            )
            tx = builder.build_and_sign(
                signing_keys=[agent._wallet.payment_signing_key],
                change_address=agent._wallet.payment_address,
            )
            tx_cbor = tx.to_cbor()
            tx_hash = str(tx.id)

            await agent.context.submit_tx_cbor(tx_cbor)
            print(f"  [{key}] Created: {tx_hash}")

            # Update deploy_state
            tx_hashes[key] = tx_hash

            if i < num_batches - 1:
                await asyncio.sleep(TX_WAIT)

        # Save updated deploy_state
        state["tx_hashes"] = tx_hashes
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print(f"\nDeploy state updated with {num_batches} new batch(es).")

        final_balance = await agent.get_balance()
        print(f"Final balance: {final_balance.ada} AP3X")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fund governance treasury with batch UTxOs")
    parser.add_argument("--batches", type=int, default=1, help="Number of batches to create")
    parser.add_argument("--size", type=int, default=DEFAULT_BATCH_SIZE_APEX, help="AP3X per batch")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    args = parser.parse_args()

    asyncio.run(fund_treasury(args.batches, args.size, args.dry_run))
