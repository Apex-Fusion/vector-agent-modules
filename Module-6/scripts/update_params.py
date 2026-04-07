"""
Module 6: Update GovernanceParams UTXO

Spends the existing GovernanceParams UTXO (at always-succeeds holder)
and creates a new one with updated values.

Usage:
    nix-shell shell.nix --run "python scripts/update_params.py"
"""

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


async def main():
    print("=" * 60)
    print("Module 6: Update GovernanceParams UTXO")
    print("=" * 60)

    state = json.load(open(DEPLOY_STATE_FILE))
    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    holders = state.get("holders", {})
    params_addr = holders["params"]["address"]
    params_compiled = holders["params"]["compiled_code"]

    from vector_agent import VectorAgent
    from vector_agent.governance.datums import build_governance_params
    from pycardano import (
        Address, TransactionBuilder, TransactionOutput,
        PlutusV3Script, Redeemer, RedeemerTag,
    )
    from pycardano.plutus import RawPlutusData

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        addr = Address.from_primitive(params_addr)
        utxos = await agent.context.async_utxos(addr)

        # Find existing params UTXO
        old_utxo = None
        for u in utxos:
            if u.output.datum is not None:
                old_utxo = u
                break

        if not old_utxo:
            print("[ERROR] No existing GovernanceParams UTXO found")
            return

        old_data = old_utxo.output.datum.data.value
        print(f"\n  Old GovernanceParams:")
        print(f"    min_review_window:       {old_data[3]}")
        print(f"    max_review_window:       {old_data[4]}")
        print(f"    proposal_cooldown:       {old_data[7]}")
        print(f"    emergency_review_window: {old_data[16]}")

        # Build new params — accept CLI overrides (e.g. --proposal_cooldown=1000)
        overrides = {}
        for arg in sys.argv[1:]:
            if arg.startswith("--") and "=" in arg:
                key, val = arg[2:].split("=", 1)
                overrides[key] = int(val)
        new_params = build_governance_params(**overrides)
        new_data = new_params.data.value

        print(f"\n  New GovernanceParams:")
        print(f"    min_review_window:       {new_data[3]}")
        print(f"    max_review_window:       {new_data[4]}")
        print(f"    proposal_cooldown:       {new_data[7]}")
        print(f"    emergency_review_window: {new_data[16]}")

        # Build tx: spend old params UTXO + create new one
        holder_script = PlutusV3Script(bytes.fromhex(params_compiled))

        builder = TransactionBuilder(agent.context)
        builder.add_input_address(agent._wallet.payment_address)
        builder.fee_buffer = 300_000

        # Add the script input (spend old params UTXO)
        builder.add_script_input(
            old_utxo,
            script=holder_script,
            redeemer=Redeemer(0),  # Holder accepts any redeemer
        )

        # Output: new params datum at same address
        builder.add_output(
            TransactionOutput(
                addr,
                3_000_000,
                datum=RawPlutusData(new_params.data),
            )
        )

        # Set collateral
        builder.collateral_change_address = agent._wallet.payment_address

        tx = builder.build_and_sign(
            signing_keys=[agent._wallet.payment_signing_key],
            change_address=agent._wallet.payment_address,
        )

        tx_cbor = tx.to_cbor()
        if isinstance(tx_cbor, bytes):
            tx_cbor = tx_cbor.hex()

        tx_hash = str(tx.id)
        await agent.context.async_submit_tx_cbor(tx_cbor)
        print(f"\n[OK] GovernanceParams updated: {tx_hash}")
        print(f"     Explorer: {state['explorer_url']}/tx/{tx_hash}")

        # Update deploy_state
        state["tx_hashes"]["params_utxo"] = tx_hash
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print(f"[OK] deploy_state.json updated")


if __name__ == "__main__":
    asyncio.run(main())
