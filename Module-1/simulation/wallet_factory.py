"""
Wallet Factory — Generate, fund, and register simulation agent wallets.

Creates N independent wallets, funds each with ADA + AP3X from the deploy wallet,
and registers each as an agent in the identity registry (one identity record
per wallet; the wallet key is the record's controller).
"""
import hashlib
import json
import os
import time
from pathlib import Path

from pycardano import (
    Address, PaymentSigningKey, PaymentVerificationKey,
    TransactionBuilder, TransactionOutput, TransactionInput,
    UTxO, RawPlutusData,
    MultiAsset, Asset, AssetName, ScriptHash, Value,
)
from pycardano.hash import TransactionId

from simulation.config import (
    NETWORK,
    AP3X_POLICY_ID, AP3X_ASSET_NAME, WALLET_SKEY, DEPLOYMENT_PATH,
    SIM_DIR,
)
from simulation.chain import (
    OgmiosContext, submit_tx, tx_to_bytes, wait_confirm,
    ensure_collateral, get_wallet_utxos_no_collateral,
    resolve_utxo,
)
from simulation.tx_builder import DeploymentState, build_register_identity
from simulation.vector_identity import did_from_id


# ═══════════════════════════════════════════════════════════════════════
# WALLET GENERATION
# ═══════════════════════════════════════════════════════════════════════

def generate_wallets(n: int, output_dir: Path = None) -> list:
    """Generate N independent wallets. Returns list of {skey, vkey, address, skey_path}."""
    if output_dir is None:
        output_dir = SIM_DIR / "wallets"
    output_dir.mkdir(parents=True, exist_ok=True)

    wallets = []
    for i in range(n):
        skey = PaymentSigningKey.generate()
        vkey = PaymentVerificationKey.from_signing_key(skey)
        addr = Address(vkey.hash(), network=NETWORK)

        skey_path = output_dir / f"agent_{i:03d}.skey"
        # Save signing key
        skey_cbor = skey.to_cbor()
        skey_json = {
            "type": "PaymentSigningKeyShelley_ed25519",
            "description": f"Sim Agent {i}",
            "cborHex": skey_cbor if isinstance(skey_cbor, str) else skey_cbor.hex(),
        }
        skey_path.write_text(json.dumps(skey_json, indent=2))

        wallets.append({
            "id": i,
            "skey": skey,
            "vkey": vkey,
            "address": addr,
            "address_str": str(addr),
            "skey_path": str(skey_path),
            "vkh": str(vkey.hash()),
        })

    # Save wallet index
    index = [{
        "id": w["id"],
        "address": w["address_str"],
        "vkh": w["vkh"],
        "skey_path": w["skey_path"],
    } for w in wallets]
    (output_dir / "wallet_index.json").write_text(json.dumps(index, indent=2))

    print(f"  Generated {n} wallets in {output_dir}")
    return wallets


def load_wallets(wallet_dir: Path = None) -> list:
    """Load previously generated wallets from disk."""
    if wallet_dir is None:
        wallet_dir = SIM_DIR / "wallets"

    index_path = wallet_dir / "wallet_index.json"
    if not index_path.exists():
        raise RuntimeError(f"No wallet index at {index_path}")

    index = json.loads(index_path.read_text())
    wallets = []
    for entry in index:
        skey_data = json.loads(Path(entry["skey_path"]).read_text())
        hex_str = skey_data["cborHex"]
        skey = PaymentSigningKey.from_primitive(bytes.fromhex(hex_str))
        vkey = PaymentVerificationKey.from_signing_key(skey)
        addr = Address(vkey.hash(), network=NETWORK)

        wallets.append({
            "id": entry["id"],
            "skey": skey,
            "vkey": vkey,
            "address": addr,
            "address_str": str(addr),
            "skey_path": entry["skey_path"],
            "vkh": entry["vkh"],
        })

    print(f"  Loaded {len(wallets)} wallets from {wallet_dir}")
    return wallets


# ═══════════════════════════════════════════════════════════════════════
# FUNDING
# ═══════════════════════════════════════════════════════════════════════

def fund_wallets(wallets: list, ada_per_wallet: int = 50_000_000,
                 ap3x_per_wallet: int = 1_000_000_000,
                 batch_size: int = 10) -> dict:
    """Fund wallets with ADA + AP3X from the deploy wallet.
    
    Sends in batches to avoid TX size limits.
    Returns funding summary.
    """
    context = OgmiosContext()

    # Load deploy wallet
    deploy_skey = PaymentSigningKey.load(WALLET_SKEY)
    deploy_vkey = PaymentVerificationKey.from_signing_key(deploy_skey)
    deploy_addr = Address(deploy_vkey.hash(), network=NETWORK)

    ap3x_policy = ScriptHash(bytes.fromhex(AP3X_POLICY_ID))
    ap3x_name = AssetName(bytes.fromhex(AP3X_ASSET_NAME))

    funded = []
    total_batches = (len(wallets) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + batch_size, len(wallets))
        batch = wallets[batch_start:batch_end]

        print(f"\n  Funding batch {batch_idx + 1}/{total_batches} ({len(batch)} wallets)...")

        ensure_collateral(context, deploy_skey, deploy_vkey, deploy_addr)
        wallet_utxos = get_wallet_utxos_no_collateral(context, deploy_addr)

        builder = TransactionBuilder(context)
        builder.fee_buffer = 500_000
        for u in wallet_utxos:
            builder.add_input(u)

        for w in batch:
            # ADA + AP3X per wallet
            out_ma = MultiAsset()
            a = Asset()
            a[ap3x_name] = ap3x_per_wallet
            out_ma[ap3x_policy] = a

            builder.add_output(TransactionOutput(
                w["address"], Value(ada_per_wallet, out_ma)))

        current_slot = context.last_block_slot
        builder.validity_start = current_slot - 60
        builder.ttl = current_slot + 3600

        tx = builder.build_and_sign([deploy_skey], change_address=deploy_addr)
        tx_hash = submit_tx(tx_to_bytes(tx))
        print(f"    ✅ Funded {len(batch)} wallets: TX {tx_hash}")

        for i, w in enumerate(batch):
            funded.append({
                "id": w["id"],
                "address": w["address_str"],
                "tx_hash": tx_hash,
                "output_index": i,
                "ada": ada_per_wallet,
                "ap3x": ap3x_per_wallet,
            })

        wait_confirm(secs=30)

    print(f"\n  Total funded: {len(funded)} wallets")
    return {"funded": funded, "total_ada": ada_per_wallet * len(funded),
            "total_ap3x": ap3x_per_wallet * len(funded)}


# ═══════════════════════════════════════════════════════════════════════
# IDENTITY REGISTRATION
# ═══════════════════════════════════════════════════════════════════════

def register_agents(wallets: list, deployment: DeploymentState,
                    context: OgmiosContext = None) -> list:
    """Register each wallet as an agent in the identity registry
    (``tx_builder.build_register_identity``, Part 2 §3.4).

    The deploy wallet pays the fees and the deposit and gives the seed UTxO.
    The wallet key is the record's controller and signs, so it is the
    declared credential of the agent's claims. The record has the default
    Accountable party (the controller). ``deployment`` holds the identity
    values (``DeploymentState.identity``).

    Returns list of {id, did_hex, did, wallet_address, tx_hash}; did_hex is
    the 32-byte identifier in hex.
    """
    if context is None:
        context = OgmiosContext()

    # Load deploy wallet (pays the fees and the deposits of all records)
    deploy_skey = PaymentSigningKey.load(WALLET_SKEY)
    deploy_vkey = PaymentVerificationKey.from_signing_key(deploy_skey)
    deploy_addr = Address(deploy_vkey.hash(), network=NETWORK)

    results = []

    for w in wallets:
        print(f"\n  Registering agent {w['id'] + 1}/{len(wallets)}...")

        tx, did_hex = build_register_identity(
            context, deployment,
            deploy_skey, deploy_vkey, deploy_addr,
            w["skey"],
            name=f"SimAgent{w['id']}".encode(),
            description=b"Simulation agent",
            framework=b"Vector-Agent",
        )
        tx_hash = submit_tx(tx_to_bytes(tx))
        print(f"    DID: {did_from_id(did_hex)} TX: {tx_hash}")

        results.append({
            "id": w["id"],
            "did_hex": did_hex,
            "did": did_from_id(did_hex),
            "wallet_address": w["address_str"],
            "tx_hash": tx_hash,
        })

        wait_confirm(secs=25)

    # Save results
    results_path = SIM_DIR / "agent_registrations.json"
    save_data = [{
        "id": r["id"],
        "did_hex": r["did_hex"],
        "did": r["did"],
        "wallet_address": r["wallet_address"],
        "tx_hash": r["tx_hash"],
    } for r in results]
    results_path.write_text(json.dumps(save_data, indent=2))
    print(f"\n  {len(results)} agents registered. Saved to {results_path}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# MAIN — Setup all wallets
# ═══════════════════════════════════════════════════════════════════════

def setup_simulation_wallets(n_agents: int = 50, n_jurors: int = 20,
                              ada_per_wallet: int = 50_000_000,
                              ap3x_per_wallet: int = 1_000_000_000,
                              deployment_path: str = DEPLOYMENT_PATH):
    """Full setup: generate wallets, fund them, register identity records.

    n_jurors of the agents will also be registered as jurors (in Phase B).
    ``deployment_path`` is the deployment manifest with the identity values
    (see simulation/config.py).
    """
    deployment = DeploymentState(json.loads(Path(deployment_path).read_text()))
    # Fail before any transaction when an identity value is missing.
    deployment.identity

    print("=" * 70)
    print(f"  SIMULATION WALLET SETUP — {n_agents} agents")
    print("=" * 70)

    # Step 1: Generate wallets
    print("\n--- Step 1: Generate wallets ---")
    wallets = generate_wallets(n_agents)

    # Step 2: Fund wallets
    print("\n--- Step 2: Fund wallets with ADA + AP3X ---")
    funding = fund_wallets(wallets, ada_per_wallet=ada_per_wallet,
                           ap3x_per_wallet=ap3x_per_wallet)

    # Step 3: Register agents
    print("\n--- Step 3: Register agents in the identity registry ---")
    registrations = register_agents(wallets, deployment)

    # Save complete setup
    setup = {
        "n_agents": n_agents,
        "n_jurors": n_jurors,
        "funding": funding,
        "registrations": [{
            "id": r["id"],
            "did_hex": r["did_hex"],
            "wallet_address": r["wallet_address"],
        } for r in registrations],
    }
    setup_path = SIM_DIR / "setup_complete.json"
    setup_path.write_text(json.dumps(setup, indent=2))

    print("\n" + "=" * 70)
    print(f"  SETUP COMPLETE: {n_agents} agents ready")
    print(f"  Total ADA distributed: {funding['total_ada'] / 1_000_000:.0f} ADA")
    print(f"  Total AP3X distributed: {funding['total_ap3x'] / 1_000_000:.0f} AP3X")
    print(f"  Setup saved: {setup_path}")
    print("=" * 70)

    return setup


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    setup_simulation_wallets(n_agents=n)
