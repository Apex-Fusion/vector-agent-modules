"""
Module 6: Recreate Infrastructure UTxOs

For the previous deployment only (wallets/deploy_state.json). The deployment
on the identity registry uses scripts/deploy.py, deploy_params.py and
treasury_fund.py.

Recreates the GovernanceParams, Oracle, and CrossRefs NFT UTxOs
at their holder addresses. These are needed as reference inputs
by the governance validators.

When the deploy state has record_token_policy, the GovernanceParams and
Oracle records hold one-shot record tokens. The tokens cannot be minted
again, so this script does not create these records. It reports a missing
record and tells how to update the existing record instead.

Usage:
    nix-shell shell.nix --run "python scripts/recreate_infra.py"
"""

import asyncio
import cbor2
import hashlib
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from deploy_common import (
    ORACLE_TOKEN_NAME,
    PARAMS_TOKEN_NAME,
    build_cross_refs_data,
    find_record_utxo,
)

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")
TX_WAIT = 20


def print_record_not_created(label: str, policy: str, token_name: bytes, instead: str):
    """Explain why a record with a record token is not created again."""
    print(f"\n--- {label} record: not created ---")
    print(f"  No output at the {label} holder holds the record token "
          f"{policy}.{token_name.hex()} ({token_name.decode()}).")
    print("  The record token policy is one-shot: the token cannot be minted again.")
    print(f"  {instead}")


def compute_native_script_policy(vkey_hash: str) -> tuple[str, bytes]:
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash)])
    script_bytes = b"\x00" + native_script_cbor
    policy_id = hashlib.blake2b(script_bytes, digest_size=28).hexdigest()
    return policy_id, native_script_cbor


async def create_datum_utxo(agent, address, datum_data, lovelace: int, label: str) -> str:
    from pycardano import TransactionBuilder, TransactionOutput, Address
    from pycardano.plutus import RawPlutusData

    if isinstance(address, str):
        address = Address.from_primitive(address)

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000

    builder.add_output(
        TransactionOutput(
            address,
            lovelace,
            datum=RawPlutusData(datum_data),
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
    print(f"  [{label}] Datum UTXO created: {tx_hash}")
    return tx_hash


async def mint_refs_nft(agent, native_script_cbor: bytes, refs_datum_data, target_address: str) -> str:
    from pycardano import (
        TransactionBuilder, TransactionOutput, Asset, AssetName,
        MultiAsset, NativeScript, Value, Address,
    )
    from pycardano.plutus import RawPlutusData

    native_script = NativeScript.from_primitive(cbor2.loads(native_script_cbor))
    policy_id = native_script.hash()
    asset_name = AssetName(b"governance_refs")

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000

    builder.native_scripts = [native_script]
    builder.mint = MultiAsset({policy_id: Asset({asset_name: 1})})

    output_addr = Address.from_primitive(target_address)
    builder.add_output(
        TransactionOutput(
            output_addr,
            Value(3_000_000, MultiAsset({policy_id: Asset({asset_name: 1})})),
            datum=RawPlutusData(refs_datum_data),
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
    print(f"  [cross-refs] NFT minted: {tx_hash}")
    return tx_hash


async def main():
    print("=" * 60)
    print("Module 6: Recreate Infrastructure UTxOs")
    print("=" * 60)

    state = json.load(open(DEPLOY_STATE_FILE))
    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    holders = state.get("holders", {})
    validators = state.get("validators", {})

    from vector_agent import VectorAgent
    from pycardano import Address as PycAddr

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        tx_hashes = state.get("tx_hashes", {})

        # Check what's missing
        params_addr = holders["params"]["address"]
        oracle_addr = holders["oracle"]["address"]

        params_utxos = await agent.context.async_utxos(PycAddr.from_primitive(params_addr))
        oracle_utxos = await agent.context.async_utxos(PycAddr.from_primitive(oracle_addr))

        record_token_policy = state.get("record_token_policy")
        if record_token_policy:
            # A record is the output that holds its record token
            has_params = find_record_utxo(params_utxos, record_token_policy, PARAMS_TOKEN_NAME) is not None
            has_oracle = find_record_utxo(oracle_utxos, record_token_policy, ORACLE_TOKEN_NAME) is not None
        else:
            has_params = any(u.output.datum is not None for u in params_utxos)
            has_oracle = any(u.output.datum is not None for u in oracle_utxos)

        refs_policy = state.get("refs_token_policy", "")
        has_crossrefs = False
        for u in oracle_utxos:
            if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                for pid in u.output.amount.multi_asset:
                    if pid.payload.hex() == refs_policy:
                        has_crossrefs = True
                        break

        print(f"\n  GovernanceParams: {'OK' if has_params else 'MISSING'}")
        print(f"  Oracle:           {'OK' if has_oracle else 'MISSING'}")
        print(f"  CrossRefs NFT:    {'OK' if has_crossrefs else 'MISSING'}")

        if has_params and has_oracle and has_crossrefs:
            print("\n[OK] All infrastructure UTxOs present. Nothing to do.")
            return

        not_created = []

        # 1. Recreate GovernanceParams
        if not has_params and record_token_policy:
            print_record_not_created(
                "params", record_token_policy, PARAMS_TOKEN_NAME,
                "Update the existing record instead: python scripts/update_params.py "
                "spends the output that holds the `params` token and moves the token "
                "to the new record.",
            )
            not_created.append("GovernanceParams")
        elif not has_params:
            print("\n--- Recreating GovernanceParams UTXO ---")
            from vector_agent.governance.datums import build_governance_params
            params_datum = build_governance_params()
            tx = await create_datum_utxo(agent, params_addr, params_datum.data, 3_000_000, "params")
            tx_hashes["params_utxo"] = tx
            print(f"    Waiting {TX_WAIT}s for confirmation...")
            await asyncio.sleep(TX_WAIT)

        # 2. Recreate Oracle
        if not has_oracle and record_token_policy:
            print_record_not_created(
                "oracle", record_token_policy, ORACLE_TOKEN_NAME,
                "Update the existing record instead: spend the output that holds the "
                "`oracle` token with the owner key, and create the new oracle record "
                "with the same token.",
            )
            not_created.append("Oracle")
        elif not has_oracle:
            print("\n--- Recreating Oracle UTXO ---")
            from vector_agent.governance.datums import build_oracle_datum
            oracle_datum = build_oracle_datum(
                oracle_vkey_hash=bytes.fromhex(wallet["vkey_hash"]),
                treasury_script_hash=bytes.fromhex(holders["treasury"]["hash"]),
            )
            tx = await create_datum_utxo(agent, oracle_addr, oracle_datum.data, 3_000_000, "oracle")
            tx_hashes["oracle_utxo"] = tx
            print(f"    Waiting {TX_WAIT}s for confirmation...")
            await asyncio.sleep(TX_WAIT)

        # 3. Re-mint CrossRefs NFT
        if not has_crossrefs:
            print("\n--- Re-minting CrossRefs NFT ---")
            _, native_script_cbor = compute_native_script_policy(wallet["vkey_hash"])

            # Build CrossRefs datum with validator hashes (5 fields, the
            # endorsement validator last)
            proposal_hash = validators.get("proposal.proposal_spend.spend", {}).get("hash", "")
            critique_hash = validators.get("critique.critique_spend.spend", {}).get("hash", "")
            proposal_mint_hash = validators.get("proposal.proposal_mint.mint", {}).get("hash", "")
            critique_mint_hash = validators.get("critique.critique_mint.mint", {}).get("hash", "")
            endorsement_hash = validators.get("critique.endorsement_spend.spend", {}).get("hash", "")

            cross_refs_data = build_cross_refs_data(
                proposal_hash,
                critique_hash,
                proposal_mint_hash,
                critique_mint_hash,
                endorsement_hash,
            )

            tx = await mint_refs_nft(agent, native_script_cbor, cross_refs_data, oracle_addr)
            tx_hashes["cross_refs_nft"] = tx

        # Save updated deploy state
        state["tx_hashes"] = tx_hashes
        with open(DEPLOY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print(f"\n[OK] deploy_state.json updated")

        print(f"\n{'='*60}")
        if not_created:
            print(f"Not created (record tokens): {', '.join(not_created)}")
        else:
            print("Infrastructure recreated successfully!")
        print(f"{'='*60}")
        print(f"\nNext: python scripts/test_expire_e2e.py")


if __name__ == "__main__":
    asyncio.run(main())
