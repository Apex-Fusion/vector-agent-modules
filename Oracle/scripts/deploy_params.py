"""
Deploy GovernanceParams UTXO to Vector testnet.

Creates a UTXO at the wallet address with an inline GovernanceParams datum.
This UTXO is required as a reference input by all governance validators.

Usage:
    nix-shell shell.nix --run "python scripts/deploy_params.py"
"""

import asyncio
import json
import os
from pathlib import Path

import cbor2
from dotenv import load_dotenv

load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")


async def deploy_params():
    print("=" * 60)
    print("Oracle: Deploy GovernanceParams UTXO")
    print("=" * 60)

    from vector_agent import VectorAgent
    from vector_agent.governance.datums import build_governance_params

    state = json.load(open(DEPLOY_STATE_FILE))
    wallet = json.load(open(WALLET_FILE))

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))
    skey_path = str(Path("wallets/payment.skey").absolute())
    explorer = state.get("explorer_url", "")

    print(f"\nWallet: {wallet['address']}")
    print(f"Params hash: {state['governance_config']['governance_params_hash']}")

    # Build GovernanceParams datum with default testnet values
    params_datum = build_governance_params()
    params_cbor = cbor2.dumps(params_datum.data)
    print(f"Params datum: {len(params_cbor)} bytes CBOR")

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        balance = await agent.get_balance()
        print(f"Balance: {balance.ada} AP3X")

        if balance.lovelace < 5_000_000:
            print("ERROR: Need at least 5 AP3X to deploy params UTXO")
            return

        # Send a UTXO to our own address with the GovernanceParams inline datum.
        # The validators check reference inputs for a UTXO at governance_params_hash
        # address. Since governance_params_hash == wallet vkey_hash, this UTXO at
        # our own address satisfies the check.
        from pycardano import TransactionBuilder, TransactionOutput, Address
        from pycardano.plutus import RawPlutusData

        builder = TransactionBuilder(agent.context)
        builder.add_input_address(agent._wallet.payment_address)

        # Create output with inline datum at wallet address
        # Min UTXO (~2 ADA) to keep the UTXO alive
        builder.add_output(
            TransactionOutput(
                agent._wallet.payment_address,
                3_000_000,  # 3 AP3X to be safe with min UTXO
                datum=RawPlutusData(cbor2.loads(params_cbor)),
            )
        )

        tx = builder.build_and_sign(
            signing_keys=[agent._wallet.payment_signing_key],
            change_address=agent._wallet.payment_address,
        )

        tx_cbor = tx.to_cbor()
        if isinstance(tx_cbor, bytes):
            tx_cbor = tx_cbor.hex()

        tx_hash = str(tx.id)
        await agent.context.async_submit_tx_cbor(tx_cbor)

        print(f"\nGovernanceParams UTXO deployed!")
        print(f"  TX: {tx_hash}")
        print(f"  Explorer: {explorer}/tx/{tx_hash}")
        print(f"\nThis UTXO must be included as a reference input in all spend transactions.")
        print(f"The smoke test should now be able to attempt withdraw/adopt actions.")


if __name__ == "__main__":
    asyncio.run(deploy_params())
