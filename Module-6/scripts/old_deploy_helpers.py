"""
Module 6: helpers of the scripts of the previous deployment

redeploy_proposal.py and move_crossrefs.py operate the previous Module-6
deployment (wallets/deploy_state.json) with the wallet of the Python SDK.
They import their helpers from this module:

  - apply_governance_config, script_hash_to_address: the shared helpers of
    deploy_common.py (scripts/deploy.py uses them too).
  - compute_native_script_policy: the policy ID and the CBOR of
    ScriptPubkey(key hash).
  - deploy_reference_script, mint_refs_nft: transactions with an SDK agent
    (sign_tx signs them with the wallet of the agent).

The deployment on the identity registry (scripts/deploy.py) does not use
deploy_reference_script, mint_refs_nft, sign_tx or
compute_native_script_policy.
"""

import hashlib
import os

import cbor2

from deploy_common import (  # noqa: F401  (apply_governance_config, script_hash_to_address: for the old scripts)
    REFS_TOKEN_NAME,
    apply_governance_config,
    script_hash_to_address,
)

EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")


def compute_native_script_policy(vkey_hash: str) -> tuple[str, bytes]:
    """The policy ID and the CBOR of ScriptPubkey(vkey_hash)."""
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash)])
    policy_id = hashlib.blake2b(b"\x00" + native_script_cbor, digest_size=28).hexdigest()
    return policy_id, native_script_cbor


def sign_tx(builder, agent):
    """Build and sign a transaction with the wallet of an SDK agent (normal or
    extended signing key). pycardano's build_and_sign gives an empty
    verification key for an extended key, so the function adds the witness
    itself."""
    from pycardano import Transaction, VerificationKeyWitness
    from pycardano.key import PaymentExtendedSigningKey

    sk = agent._wallet.payment_signing_key
    change_addr = agent._wallet.payment_address

    if not isinstance(sk, PaymentExtendedSigningKey):
        return builder.build_and_sign(signing_keys=[sk], change_address=change_addr)

    tx_body = builder.build(change_address=change_addr)
    vk = agent._wallet.payment_verification_key
    signature = sk.sign(tx_body.hash())
    witness_set = builder.build_witness_set()
    witness_set.vkey_witnesses = [VerificationKeyWitness(vk, signature)]
    return Transaction(tx_body, witness_set)


async def deploy_reference_script(agent, script_cbor_hex: str, label: str) -> str:
    """Send a validator as reference script to the address of its own hash
    (the layout of the previous deployment)."""
    from pycardano import Address, Network, PlutusV3Script, TransactionBuilder, TransactionOutput
    from pycardano.plutus import script_hash as compute_script_hash

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2
    unspendable_addr = Address(payment_part=compute_script_hash(script), network=Network.MAINNET)
    min_lovelace = 2_000_000 + script_size * 4400
    min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.add_output(TransactionOutput(unspendable_addr, min_lovelace, script=script))
    builder.fee_buffer = max(300_000, script_size * 50)

    tx = sign_tx(builder, agent)
    tx_cbor = tx.to_cbor()
    tx_cbor_hex = tx_cbor.hex() if isinstance(tx_cbor, bytes) else tx_cbor
    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor_hex)
    print(f"  [{label}] Ref script deployed: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def mint_refs_nft(agent, native_script_cbor: bytes, refs_datum_data, label: str,
                        target_address: str = None) -> str:
    """Mint the "governance_refs" token and create the CrossRefs output with
    an inline datum at `target_address` (else at the wallet address)."""
    from pycardano import (
        Address, Asset, AssetName, MultiAsset, NativeScript, TransactionBuilder, TransactionOutput, Value,
    )
    from pycardano.plutus import RawPlutusData

    native_script = NativeScript.from_primitive(cbor2.loads(native_script_cbor))
    policy_id = native_script.hash()
    asset_name = AssetName(REFS_TOKEN_NAME)

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000
    builder.native_scripts = [native_script]
    builder.mint = MultiAsset({policy_id: Asset({asset_name: 1})})
    output_addr = Address.from_primitive(target_address) if target_address else agent._wallet.payment_address
    builder.add_output(TransactionOutput(
        output_addr,
        Value(3_000_000, MultiAsset({policy_id: Asset({asset_name: 1})})),
        datum=RawPlutusData(refs_datum_data),
    ))

    tx = sign_tx(builder, agent)
    tx_cbor = tx.to_cbor()
    tx_cbor_hex = tx_cbor.hex() if isinstance(tx_cbor, bytes) else tx_cbor
    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor_hex)
    print(f"  [{label}] Refs NFT minted: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash
