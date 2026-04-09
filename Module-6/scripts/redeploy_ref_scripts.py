"""
Redeploy consumed spend-validator reference scripts (CIP-33).

The proposal_spend, critique_spend, and endorsement_spend reference script UTxOs
were accidentally consumed. This script re-deploys them and updates deploy_state.json.

Usage:
    cd Module-6 && .venv/bin/python scripts/redeploy_ref_scripts.py
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

# The 3 spend reference scripts that need redeployment
SCRIPTS_TO_REDEPLOY = [
    ("proposal.proposal_spend.spend", "proposal_spend"),
    ("critique.critique_spend.spend", "critique_spend"),
    ("critique.endorsement_spend.spend", "endorsement_spend"),
]


async def deploy_reference_script(agent, script_cbor_hex: str, label: str) -> str:
    """Deploy a validator as a reference script UTxO, return tx hash."""
    from pycardano import TransactionBuilder, TransactionOutput, PlutusV3Script

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2

    # Min UTXO for reference scripts: ~4400 lovelace per byte + 2 AP3X base
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
    return tx_hash


async def main():
    # Load deploy state
    if not DEPLOY_STATE_FILE.exists():
        print("ERROR: deploy_state.json not found")
        sys.exit(1)

    with open(DEPLOY_STATE_FILE) as f:
        state = json.load(f)

    validators = state.get("validators", {})

    print("=== Redeploying consumed spend reference scripts ===\n")

    from vector_agent import VectorAgent

    async with VectorAgent(
        ogmios_url=OGMIOS_URL,
        submit_url=SUBMIT_URL,
        skey_path=str(SKEY_PATH.absolute()),
    ) as agent:
        balance = await agent.get_balance()
        print(f"  Deployer balance: {balance.ada} AP3X\n")

        tx_hashes = state.get("tx_hashes", {})
        TX_WAIT = 20

        for title, label in SCRIPTS_TO_REDEPLOY:
            if title not in validators:
                print(f"  ERROR: {title} not found in deploy_state.json validators")
                continue

            compiled_code = validators[title]["compiled_code"]
            expected_hash = validators[title]["hash"]
            print(f"  Deploying {label} (hash: {expected_hash[:16]}...)")

            tx_hash = await deploy_reference_script(agent, compiled_code, label)
            tx_hashes[f"{label}_ref"] = tx_hash
            print(f"    Waiting {TX_WAIT}s for confirmation...\n")
            await asyncio.sleep(TX_WAIT)

        # Update deploy_state.json with new tx hashes
        state["tx_hashes"] = tx_hashes
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        print("\n=== Updated deploy_state.json ===")
        print("\nNew reference script UTxO refs (for deployment.json + MCP env vars):")
        for _, label in SCRIPTS_TO_REDEPLOY:
            key = f"{label}_ref"
            tx = tx_hashes.get(key, "NOT DEPLOYED")
            print(f"  {key}: {tx}#0")

        # Print final balance
        balance = await agent.get_balance()
        print(f"\n  Deployer balance after: {balance.ada} AP3X")


if __name__ == "__main__":
    asyncio.run(main())
