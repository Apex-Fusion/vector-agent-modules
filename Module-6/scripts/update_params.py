"""
Module 6: Update GovernanceParams UTXO

Spends the existing GovernanceParams record at the params holder and creates
a new one with updated values. The holder accepts the spend only with the
owner signature, so the wallet key hash is a required signer.

When the deploy state has record_token_policy, the record is the output that
holds the `params` record token, and the token moves to the new record.
Without record_token_policy (deployment before record tokens), the record is
the first output with a datum at the params holder address.

Usage:
    nix-shell shell.nix --run "python scripts/update_params.py [--network N] [--state PATH] [--<field>=<int> ...]"

    --state: the deploy state (default:
    deploy/identity/state/<network>/deploy_state.json of scripts/deploy.py;
    --network, default mainnet). The state of the previous deployment is used
    only with --state wallets/deploy_state.json. --<field>=<int> changes one
    value of the SDK defaults (build_governance_params), for example
    --proposal_cooldown=1000. For the deployment on the identity registry,
    scripts/deploy_params.py replaces the record with the values of a file.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from deploy_common import (
    PARAMS_TOKEN_NAME,
    add_state_arguments,
    find_record_utxo,
    read_state,
    state_endpoints,
    state_path,
)

WALLET_FILE = Path("wallets/governance_wallet.json")


def parse_args(argv=None):
    """(--network and --state, the GovernanceParams values to change). Each
    other argument --<field>=<int> changes one value; other arguments do
    nothing."""
    parser = argparse.ArgumentParser(description="Module 6: update the GovernanceParams record")
    add_state_arguments(parser)
    args, rest = parser.parse_known_args(argv)
    overrides = {}
    for arg in rest:
        if arg.startswith("--") and "=" in arg:
            key, val = arg[2:].split("=", 1)
            overrides[key] = int(val)
    return args, overrides


async def main(argv=None):
    args, overrides = parse_args(argv)
    print("=" * 60)
    print("Module 6: Update GovernanceParams UTXO")
    print("=" * 60)

    path = state_path(args)
    state = read_state(path, args.network)
    endpoints = state_endpoints(state, args.network)
    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    holders = state.get("holders", {})
    params_addr = holders["params"]["address"]
    params_compiled = holders["params"]["compiled_code"]

    from vector_agent import VectorAgent
    from vector_agent.governance.datums import build_governance_params
    from pycardano import (
        Address, Asset, AssetName, MultiAsset, ScriptHash, TransactionBuilder,
        TransactionOutput, PlutusV3Script, Redeemer, RedeemerTag, Value,
    )
    from pycardano.plutus import RawPlutusData

    record_token_policy = state.get("record_token_policy")

    ogmios_url = endpoints["ogmios_url"]
    submit_url = endpoints["submit_url"]

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        owner_vkh = agent._wallet.payment_verification_key.hash()
        holder_owner = state.get("holder_owner_vkey_hash")
        if holder_owner and holder_owner != str(owner_vkh):
            print(f"[ERROR] The holder owner is {holder_owner}, but the wallet key hash is {owner_vkh}")
            return

        addr = Address.from_primitive(params_addr)
        utxos = await agent.context.async_utxos(addr)

        # Find existing params UTXO
        old_utxo = None
        if record_token_policy:
            # The record is the output that holds the params record token
            old_utxo = find_record_utxo(utxos, record_token_policy, PARAMS_TOKEN_NAME)
        else:
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

        # Build new params with the CLI overrides (e.g. --proposal_cooldown=1000)
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

        # Output 0: new params datum at same address. The params record token
        # moves from the old record to the new one.
        new_value = 3_000_000
        if record_token_policy:
            new_value = Value(3_000_000, MultiAsset({
                ScriptHash(bytes.fromhex(record_token_policy)): Asset({AssetName(PARAMS_TOKEN_NAME): 1}),
            }))
        builder.add_output(
            TransactionOutput(
                addr,
                new_value,
                datum=RawPlutusData(new_params.data),
            )
        )

        # The holder needs the owner signature
        builder.required_signers = [owner_vkh]

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
        print(f"     Explorer: {endpoints['explorer_url']}/tx/{tx_hash}")

        # Update deploy_state
        state["tx_hashes"]["params_utxo"] = tx_hash
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
        print(f"[OK] {path} updated")


if __name__ == "__main__":
    asyncio.run(main())
