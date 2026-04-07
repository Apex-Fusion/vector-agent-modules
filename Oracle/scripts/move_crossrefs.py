"""
Mint a fresh CrossRefs NFT at the oracle holder script address.

The CrossRefs NFT must NOT be at the same address as GovernanceParams
(they share a ScriptCredential and read_governance_params would find
the wrong datum). The oracle holder address is a different script hash.

Usage:
    nix-shell shell.nix --run "python scripts/move_crossrefs.py"
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import cbor2
from dotenv import load_dotenv

load_dotenv()

ORACLE_ROOT = Path(__file__).parent.parent
WALLET_FILE = ORACLE_ROOT / "wallets" / "governance_wallet.json"
SKEY_PATH = ORACLE_ROOT / "wallets" / "payment.skey"
DEPLOY_STATE_FILE = ORACLE_ROOT / "wallets" / "deploy_state.json"

OGMIOS_URL = os.getenv("VECTOR_OGMIOS_URL", "https://ogmios.vector.testnet.apexfusion.org")
SUBMIT_URL = os.getenv("VECTOR_SUBMIT_URL", "https://submit.vector.testnet.apexfusion.org/api/submit/tx")
EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")

sys.path.insert(0, str(ORACLE_ROOT))

from deploy import compute_native_script_policy


async def mint_crossrefs_at_oracle():
    with open(WALLET_FILE) as f:
        wallet = json.load(f)
    with open(DEPLOY_STATE_FILE) as f:
        state = json.load(f)

    holders = state.get("holders", {})
    oracle_addr = holders.get("oracle", {}).get("address", "")
    refs_policy = state.get("refs_token_policy", "")

    print(f"Target: {oracle_addr} (oracle holder)")

    # Build CrossRefs datum from current deploy state
    proposal_hash = state.get("proposal_validator_hash", "")
    critique_hash = state.get("critique_validator_hash", "")
    proposal_mint_hash = state["validators"]["proposal.proposal_mint.mint"]["hash"]
    critique_mint_hash = state["validators"]["critique.critique_mint.mint"]["hash"]

    print(f"CrossRefs datum:")
    print(f"  proposal_spend:  {proposal_hash}")
    print(f"  critique_spend:  {critique_hash}")
    print(f"  proposal_mint:   {proposal_mint_hash}")
    print(f"  critique_mint:   {critique_mint_hash}")

    cross_refs_data = cbor2.CBORTag(121, [
        bytes.fromhex(proposal_hash),
        bytes.fromhex(critique_hash),
        bytes.fromhex(proposal_mint_hash),
        bytes.fromhex(critique_mint_hash),
    ])

    _, native_script_cbor = compute_native_script_policy(wallet["vkey_hash"])

    from vector_agent import VectorAgent
    from pycardano import (
        TransactionBuilder, TransactionOutput, Address, Value,
        NativeScript, MultiAsset, Asset, AssetName,
    )
    from pycardano.plutus import RawPlutusData

    async with VectorAgent(
        ogmios_url=OGMIOS_URL,
        submit_url=SUBMIT_URL,
        skey_path=str(SKEY_PATH.absolute()),
    ) as agent:
        balance = await agent.get_balance()
        print(f"\nBalance: {balance.ada} AP3X")

        native_script = NativeScript.from_primitive(cbor2.loads(native_script_cbor))
        policy_id = native_script.hash()
        asset_name = AssetName(b"gov_refs_v2")

        target = Address.from_primitive(oracle_addr)

        builder = TransactionBuilder(agent.context)
        builder.add_input_address(agent._wallet.payment_address)
        builder.fee_buffer = 200_000

        # Mint new refs NFT
        builder.native_scripts = [native_script]
        builder.mint = MultiAsset({policy_id: Asset({asset_name: 1})})

        # Output: NFT + inline datum at oracle holder address
        ma = MultiAsset({policy_id: Asset({asset_name: 1})})
        builder.add_output(
            TransactionOutput(
                target,
                Value(3_000_000, ma),
                datum=RawPlutusData(cross_refs_data),
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
        print(f"\nCrossRefs NFT minted at oracle holder: {tx_hash}")
        print(f"Explorer: {EXPLORER_URL}/tx/{tx_hash}")

        # Update deploy state
        state["tx_hashes"]["cross_refs_nft"] = tx_hash
        state["cross_refs_address"] = oracle_addr
        # Update refs_token_policy if it changed (same policy, new asset name)
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print("deploy_state.json updated")


if __name__ == "__main__":
    asyncio.run(mint_crossrefs_at_oracle())
