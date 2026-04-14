"""
Redeploy ONLY the proposal_spend reference script.

The previous batch deployment accidentally consumed it as a coin input
when deploying subsequent scripts. This deploys it in isolation.

Usage:
    cd Module-6 && .venv/bin/python scripts/redeploy_proposal_spend_ref.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GAME6_ROOT = Path(__file__).parent.parent
DEPLOY_STATE_FILE = GAME6_ROOT / "wallets" / "deploy_state.json"
SKEY_PATH = GAME6_ROOT / "wallets" / "payment.skey"

OGMIOS_URL = os.getenv("VECTOR_OGMIOS_URL", "https://ogmios.vector.testnet.apexfusion.org")
SUBMIT_URL = os.getenv("VECTOR_SUBMIT_URL", "https://submit.vector.testnet.apexfusion.org/api/submit/tx")
EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")


async def deploy_reference_script(agent, script_cbor_hex: str, label: str) -> str:
    """Deploy a validator as a reference script UTxO, return tx hash."""
    from pycardano import TransactionBuilder, TransactionOutput, PlutusV3Script

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2

    min_lovelace = 2_000_000 + script_size * 4400
    min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)

    builder.add_output(
        TransactionOutput(
            agent._wallet.payment_address,
            min_lovelace,
            script=script,
        )
    )

    builder.fee_buffer = max(300_000, script_size * 50)

    tx = builder.build_and_sign(
        signing_keys=[agent._wallet.payment_signing_key],
        change_address=agent._wallet.payment_address,
    )

    tx_cbor = tx.to_cbor()
    if isinstance(tx_cbor, bytes):
        tx_cbor_hex = tx_cbor.hex()
    else:
        tx_cbor_hex = tx_cbor

    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor_hex)
    print(f"  [{label}] Ref script deployed: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    print(f"    Min lovelace: {min_lovelace / 1_000_000} ADA")
    print(f"    Script size: {script_size} bytes")
    return tx_hash


async def main():
    if not DEPLOY_STATE_FILE.exists():
        print("ERROR: deploy_state.json not found")
        sys.exit(1)

    with open(DEPLOY_STATE_FILE) as f:
        state = json.load(f)

    compiled_code = state["validators"]["proposal.proposal_spend.spend"]["compiled_code"]
    expected_hash = state["validators"]["proposal.proposal_spend.spend"]["hash"]

    print(f"=== Redeploying proposal_spend reference script ===")
    print(f"  Script hash: {expected_hash}")
    print(f"  CBOR length: {len(compiled_code)} chars\n")

    from vector_agent import VectorAgent

    async with VectorAgent(
        ogmios_url=OGMIOS_URL,
        submit_url=SUBMIT_URL,
        skey_path=str(SKEY_PATH.absolute()),
    ) as agent:
        balance = await agent.get_balance()
        print(f"  Deployer balance: {balance.ada} AP3X\n")

        tx_hash = await deploy_reference_script(agent, compiled_code, "proposal_spend")

        # Update deploy_state.json
        state["tx_hashes"]["proposal_spend_ref"] = tx_hash
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        print(f"\n  Reference UTxO: {tx_hash}#0")
        print(f"\n  Update GOV_PROPOSAL_SPEND_REF to: {tx_hash}#0")

        # Wait and verify
        print(f"\n  Waiting 30s for confirmation...")
        await asyncio.sleep(30)

        # Verify the UTxO exists
        utxos = await agent.context.async_utxos(str(agent._wallet.payment_address))
        found = any(str(u.input.transaction_id) == tx_hash and u.input.index == 0 for u in utxos)
        if found:
            print(f"  VERIFIED: UTxO {tx_hash}#0 exists on-chain")
        else:
            # Check if it's at index 1 instead (pycardano output ordering)
            found_1 = any(str(u.input.transaction_id) == tx_hash and u.input.index == 1 for u in utxos)
            if found_1:
                print(f"  WARNING: Script is at index 1, not 0! Use {tx_hash}#1")
                state["tx_hashes"]["proposal_spend_ref"] = tx_hash
                with open(DEPLOY_STATE_FILE, "w") as f:
                    json.dump(state, f, indent=2)
            else:
                print(f"  ERROR: UTxO not found! It may have been consumed already.")


if __name__ == "__main__":
    asyncio.run(main())
