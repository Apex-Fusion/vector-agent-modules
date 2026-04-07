"""
Wallet Factory — Generate, fund, and register simulation agent wallets.

Creates N independent wallets, funds each with ADA + AP3X from the deploy wallet,
and registers each as an agent in the Agent Registry.
"""
import cbor2
import hashlib
import json
import os
import time
from pathlib import Path

from pycardano import (
    Address, PaymentSigningKey, PaymentVerificationKey,
    TransactionBuilder, TransactionOutput, TransactionInput,
    UTxO, RawCBOR, RawPlutusData, Redeemer, PlutusV3Script,
    MultiAsset, Asset, AssetName, ScriptHash, Value,
    ExecutionUnits,
)
from pycardano.hash import TransactionId

from simulation.config import (
    NETWORK, REGISTRY_POLICY, REGISTRY_ADDR,
    AP3X_POLICY_ID, AP3X_ASSET_NAME, WALLET_SKEY,
    TESTNET_DIR, SIM_DIR, SYSTEM_START_UNIX,
)
from simulation.chain import (
    OgmiosContext, submit_tx, tx_to_bytes, wait_confirm,
    ensure_collateral, get_wallet_utxos_no_collateral,
    evaluate_and_rebuild, agent_did_name, resolve_utxo,
)


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
# DID REGISTRATION
# ═══════════════════════════════════════════════════════════════════════

def register_agents(wallets: list, context: OgmiosContext = None) -> list:
    """Register each wallet as an agent in the Agent Registry.
    
    Returns list of {id, did_hex, tx_hash, reg_utxo_ref}.
    """
    if context is None:
        context = OgmiosContext()

    # Load deploy wallet (signs all registrations — oracle/admin key)
    deploy_skey = PaymentSigningKey.load(WALLET_SKEY)
    deploy_vkey = PaymentVerificationKey.from_signing_key(deploy_skey)
    deploy_addr = Address(deploy_vkey.hash(), network=NETWORK)

    # Load registry mint script
    registry_blueprint = Path(TESTNET_DIR / "agent-registry-plutus.json")
    if not registry_blueprint.exists():
        # Fallback: use from contracts
        from simulation.config import CONTRACTS_DIR
        alt = CONTRACTS_DIR.parent / "agent-registry" / "plutus.json"
        if alt.exists():
            registry_blueprint = alt

    registry_script = None
    if registry_blueprint.exists():
        with open(registry_blueprint) as f:
            bp = json.load(f)
        for v in bp.get("validators", []):
            if "mint" in v.get("title", ""):
                registry_script = PlutusV3Script(bytes.fromhex(v["compiledCode"]))
                break

    registry_policy = ScriptHash(bytes.fromhex(REGISTRY_POLICY))
    registry_addr = Address.from_primitive(REGISTRY_ADDR)

    results = []

    for w in wallets:
        print(f"\n  Registering agent {w['id'] + 1}/{len(wallets)}...")

        ensure_collateral(context, deploy_skey, deploy_vkey, deploy_addr)
        current_slot = context.last_block_slot
        wallet_utxos = get_wallet_utxos_no_collateral(context, deploy_addr)

        # Sort UTxOs deterministically for seed
        sorted_utxos = sorted(wallet_utxos,
            key=lambda u: (bytes(u.input.transaction_id).hex(), u.input.index))
        seed_utxo = sorted_utxos[0]
        seed_tx_hash = bytes(seed_utxo.input.transaction_id)
        seed_tx_idx = seed_utxo.input.index

        did_bytes = agent_did_name(seed_tx_hash, seed_tx_idx)
        did_hex = did_bytes.hex()

        seed_ref_cbor = cbor2.CBORTag(121, [seed_tx_hash, seed_tx_idx])
        register_redeemer_cbor = RawCBOR(cbor2.dumps(cbor2.CBORTag(121, [seed_ref_cbor])))
        register_redeemer = Redeemer(register_redeemer_cbor,
                                     ExecutionUnits(mem=500_000, steps=200_000_000))

        agent_nft_an = AssetName(did_bytes)
        mint_ma = MultiAsset()
        mint_a = Asset()
        mint_a[agent_nft_an] = 1
        mint_ma[registry_policy] = mint_a

        # Agent datum: credential = wallet's VKH
        agent_datum = cbor2.CBORTag(121, [
            cbor2.CBORTag(121, [bytes(w["vkey"].hash())]),
            f"SimAgent{w['id']}".encode(),
            b"Simulation agent",
            [],
            b"Apex-Sim",
            b"",
            (SYSTEM_START_UNIX + current_slot) * 1000,
        ])

        nft_ma = MultiAsset()
        na = Asset()
        na[agent_nft_an] = 1
        nft_ma[registry_policy] = na

        if registry_script is not None:
            builder = TransactionBuilder(context)
            builder.fee_buffer = 500_000
            for u in wallet_utxos:
                builder.add_input(u)
            builder.mint = mint_ma
            builder.add_minting_script(registry_script, register_redeemer)
            builder.add_output(TransactionOutput(
                registry_addr, Value(15_000_000, nft_ma),
                datum=RawCBOR(cbor2.dumps(agent_datum))))
            builder.required_signers = [deploy_vkey.hash()]
            builder.validity_start = current_slot - 60
            builder.ttl = current_slot + 3600

            _, budgets = evaluate_and_rebuild(builder, deploy_skey, deploy_vkey,
                                              deploy_addr, context)
            for key, b in budgets.items():
                if "mint" in key:
                    register_redeemer = Redeemer(register_redeemer_cbor,
                        ExecutionUnits(mem=b["mem"], steps=b["cpu"]))

            builder2 = TransactionBuilder(context)
            builder2.fee_buffer = 500_000
            for u in wallet_utxos:
                builder2.add_input(u)
            builder2.mint = mint_ma
            builder2.add_minting_script(registry_script, register_redeemer)
            builder2.add_output(TransactionOutput(
                registry_addr, Value(15_000_000, nft_ma),
                datum=RawCBOR(cbor2.dumps(agent_datum))))
            builder2.required_signers = [deploy_vkey.hash()]
            builder2.validity_start = current_slot - 60
            builder2.ttl = current_slot + 3600

            tx = builder2.build_and_sign([deploy_skey], change_address=deploy_addr)
            tx_hash = submit_tx(tx_to_bytes(tx))
        else:
            raise RuntimeError("Agent registry mint script not found")

        print(f"    DID: {did_hex[:20]}... TX: {tx_hash}")

        results.append({
            "id": w["id"],
            "did_hex": did_hex,
            "did_bytes": did_bytes,
            "wallet_address": w["address_str"],
            "tx_hash": tx_hash,
        })

        wait_confirm(secs=25)

    # Save results
    results_path = SIM_DIR / "agent_registrations.json"
    save_data = [{
        "id": r["id"],
        "did_hex": r["did_hex"],
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
                              ap3x_per_wallet: int = 1_000_000_000):
    """Full setup: generate wallets, fund them, register DIDs.
    
    n_jurors of the agents will also be registered as jurors (in Phase B).
    """
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
    print("\n--- Step 3: Register agents in Agent Registry ---")
    registrations = register_agents(wallets)

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
