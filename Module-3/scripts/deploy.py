"""
Module 3: Reputation Staking — Full Testnet Deployment Script

Handles the complete deployment flow:
  1. Compute NativeScript refs_token_policy from wallet vkey_hash
  2. Build ReputationConfig CBOR and apply to both validators via `aiken blueprint apply`
  3. Deploy holder reference scripts (params/treasury)
  4. Deploy Module-3 validator reference scripts (CIP-33)
  5. Create ProtocolParams datum UTXO at params holder address
  6. Mint CrossValidatorRefs NFT and create datum UTXO
  7. Save full deployment state

Usage:
    nix-shell shell.nix --run "python scripts/deploy.py"

Prerequisites:
    1. Wallet funded (run setup_wallet.py first)
    2. Aiken contracts compiled (aiken build in reputation-staking/)
    3. Holder scripts compiled (aiken build in shared/holder-scripts/)
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import cbor2
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────

MODULE3_ROOT = Path(__file__).parent.parent
REPO_ROOT = MODULE3_ROOT.parent
CONTRACT_DIR = MODULE3_ROOT / "reputation-staking"
HOLDER_DIR = REPO_ROOT / "shared" / "holder-scripts"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
HOLDER_BLUEPRINT_PATH = HOLDER_DIR / "plutus.json"
WALLET_DIR = MODULE3_ROOT / "wallets"
WALLET_FILE = WALLET_DIR / "reputation_wallet.json"
SKEY_PATH = WALLET_DIR / "payment.skey"
DEPLOY_STATE_FILE = WALLET_DIR / "deploy_state.json"

# ── Network config ───────────────────────────────────────────────────────────

OGMIOS_URL = os.getenv("VECTOR_OGMIOS_URL", "https://ogmios.vector.testnet.apexfusion.org")
SUBMIT_URL = os.getenv("VECTOR_SUBMIT_URL", "https://submit.vector.testnet.apexfusion.org/api/submit/tx")
EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")

# ── Constants ────────────────────────────────────────────────────────────────

AGENT_REGISTRY_HASH = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
ZEROED_28 = "00" * 28

# AP3X token (testnet) — update if token changes
AP3X_POLICY_ID = os.getenv("AP3X_POLICY_ID", ZEROED_28)
AP3X_ASSET_NAME = os.getenv("AP3X_ASSET_NAME", "")

# Module 1 contract hashes (from Module-1/deploy/deployment.json)
MODULE1_CLAIM_HASH = "6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda"
MODULE1_CHALLENGE_HASH = "781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc"
MODULE1_JURY_POOL_HASH = "b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962"
MODULE1_REFS_TOKEN_POLICY = os.getenv("MODULE1_REFS_TOKEN_POLICY", ZEROED_28)


def load_wallet() -> dict:
    if not WALLET_FILE.exists():
        raise FileNotFoundError("Wallet not found. Run: python scripts/setup_wallet.py")
    with open(WALLET_FILE) as f:
        return json.load(f)


def load_blueprint(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def get_validator(blueprint: dict, title_prefix: str) -> dict:
    for v in blueprint["validators"]:
        if v["title"].startswith(title_prefix):
            return {"title": v["title"], "compiled_code": v["compiledCode"], "hash": v["hash"]}
    raise ValueError(f"Validator not found: {title_prefix}")


# ── NativeScript policy ─────────────────────────────────────────────────────

def compute_native_script_policy(vkey_hash: str) -> tuple[str, bytes]:
    """Compute NativeScript policy ID for ScriptPubkey(vkey_hash)."""
    native_script_cbor = cbor2.dumps([0, bytes.fromhex(vkey_hash)])
    script_bytes = b"\x00" + native_script_cbor
    policy_id = hashlib.blake2b(script_bytes, digest_size=28).hexdigest()
    return policy_id, native_script_cbor


# ── ReputationConfig CBOR ────────────────────────────────────────────────────

def build_reputation_config_cbor(
    refs_token_policy: str,
    params_holder_hash: str,
    treasury_holder_hash: str,
    oracle_vkey_hash: str,
) -> str:
    """Build ReputationConfig as Plutus Data CBOR (hex).

    Constructor 0 with 12 fields matching the Aiken type definition.
    """
    fields = [
        bytes.fromhex(refs_token_policy),           # refs_token_policy
        bytes.fromhex(AGENT_REGISTRY_HASH),         # registry_policy_id
        bytes.fromhex(AGENT_REGISTRY_HASH),         # registry_script_hash
        bytes.fromhex(MODULE1_CLAIM_HASH),          # audit_claim_validator_hash
        bytes.fromhex(MODULE1_CHALLENGE_HASH),      # audit_challenge_validator_hash
        bytes.fromhex(MODULE1_JURY_POOL_HASH),      # audit_jury_pool_hash
        bytes.fromhex(MODULE1_REFS_TOKEN_POLICY),   # audit_refs_token_policy
        bytes.fromhex(params_holder_hash),          # params_script_hash
        bytes.fromhex(treasury_holder_hash),        # treasury_script_hash
        bytes.fromhex(AP3X_POLICY_ID),              # ap3x_policy_id
        bytes.fromhex(AP3X_ASSET_NAME) if AP3X_ASSET_NAME else b"",  # ap3x_asset_name
        bytes.fromhex(oracle_vkey_hash),            # oracle_credential
    ]
    config_data = cbor2.CBORTag(121, fields)
    return cbor2.dumps(config_data).hex()


# ── Apply parameters ────────────────────────────────────────────────────────

def apply_holder_tag(holder_blueprint: Path, tag: int) -> dict:
    """Apply an integer tag to the holder validator, return hash + compiled code."""
    tag_cbor = cbor2.dumps(tag).hex()
    result = subprocess.run(
        ["aiken", "blueprint", "apply", "-i", str(holder_blueprint),
         "-m", "holder", "-v", "holder", tag_cbor],
        capture_output=True, text=True, check=True,
    )
    applied = json.loads(result.stdout)
    v = applied["validators"][0]
    return {"hash": v["hash"], "compiled_code": v["compiledCode"]}


def apply_reputation_config(config_cbor_hex: str) -> dict:
    """Apply ReputationConfig to both Module-3 validators, return applied blueprint."""
    current = str(BLUEPRINT_PATH)
    validators_to_apply = [
        ("reputation", "reputation_mint"),
        ("reputation", "reputation_spend"),
        ("endorsement", "endorsement_mint"),
        ("endorsement", "endorsement_spend"),
    ]

    applied_bp = None
    for i, (module, validator) in enumerate(validators_to_apply):
        is_last = i == len(validators_to_apply) - 1
        cmd = [
            "aiken", "blueprint", "apply",
            "-i", current,
            "-m", module, "-v", validator,
            config_cbor_hex,
        ]
        if not is_last:
            out_path = str(CONTRACT_DIR / f"plutus-applied-{i}.json")
            cmd.extend(["-o", out_path])
            subprocess.run(cmd, check=True)
            current = out_path
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            applied_bp = json.loads(result.stdout)

    # Clean up intermediate files
    for i in range(len(validators_to_apply) - 1):
        f = CONTRACT_DIR / f"plutus-applied-{i}.json"
        if f.exists():
            f.unlink()

    return applied_bp


# ── ProtocolParams datum ─────────────────────────────────────────────────────

def build_protocol_params_datum() -> bytes:
    """Build ProtocolParams as Plutus Data CBOR.

    Constructor 0 with fields matching the Aiken ProtocolParams type.
    Values from MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Section 10.
    """
    valid_capabilities = [
        b"code_review", b"testing", b"deployment", b"documentation",
        b"security_audit", b"architecture", b"data_analysis", b"ml_training",
    ]

    fields = [
        10_000_000,     # min_self_stake (10 AP3X)
        5_000_000,      # min_endorsement (5 AP3X)
        25_000_000,     # min_challenge_stake (25 AP3X)
        21_600,         # stake_cooldown (24h in slots)
        43_200,         # endorsement_cooldown (48h in slots)
        100,            # decay_rate (1% per epoch)
        180,            # activity_window (180 epochs ~ 30 days)
        500,            # decay_collector_fee (5%)
        1000,           # history_multiplier (10%)
        3,              # max_endorsement_multiplier (3x)
        5000,           # slash_rate_endorser (50%)
        500,            # protocol_fee_rate (5%)
        10_800,         # challenge_response_deadline (12h in slots)
        21_600,         # min_agent_age (24h in slots)
        5_400,          # escalation_window (6h in slots)
        100,            # default_judgment_fee (1%)
        50,             # genesis_agent_cap
        100_000_000,    # genesis_bonus_amount (100 AP3X)
        604_800,        # genesis_minting_window (28 days in slots)
        129_600,        # genesis_protection_period (60 days in slots)
        valid_capabilities,  # valid_capabilities
        900,            # epoch_length (4h in slots)
    ]
    params_data = cbor2.CBORTag(121, fields)
    return cbor2.dumps(params_data)


# ── CrossValidatorRefs datum ─────────────────────────────────────────────────

def build_cross_refs_datum(reputation_hash: str, endorsement_hash: str) -> bytes:
    """Build CrossValidatorRefs as Plutus Data CBOR.

    Constructor 0 with 2 fields: reputation_policy_id, endorsement_policy_id.
    """
    refs_data = cbor2.CBORTag(121, [
        bytes.fromhex(reputation_hash),
        bytes.fromhex(endorsement_hash),
    ])
    return cbor2.dumps(refs_data)


# ── Bech32 address computation ───────────────────────────────────────────────

def script_hash_to_testnet_address(script_hash_hex: str) -> str:
    """Convert a script hash to a Bech32 address (Vector uses mainnet flag)."""
    from pycardano import Address, Network, ScriptHash
    script_hash = ScriptHash(bytes.fromhex(script_hash_hex))
    addr = Address(payment_part=script_hash, network=Network.MAINNET)
    return str(addr)


# ── Deployment transactions ─────────────────────────────────────────────────

async def deploy_reference_script(agent, script_cbor_hex: str, label: str) -> str:
    """Deploy a validator as a reference script (CIP-33), return tx hash."""
    from pycardano import TransactionBuilder, TransactionOutput, PlutusV3Script

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2

    min_lovelace = 2_000_000 + script_size * 4400
    min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = max(300_000, script_size * 50)

    builder.add_output(
        TransactionOutput(
            agent._wallet.payment_address,
            min_lovelace,
            script=script,
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
    print(f"  [{label}] Ref script deployed: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def create_datum_utxo(agent, address, datum_cbor: bytes, lovelace: int, label: str) -> str:
    """Create a UTXO at a script address with an inline datum."""
    from pycardano import TransactionBuilder, TransactionOutput, Address
    from pycardano.plutus import RawPlutusData

    if isinstance(address, str):
        address = Address.from_primitive(address)

    datum_data = cbor2.loads(datum_cbor)

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
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def mint_refs_nft(agent, native_script_cbor: bytes, refs_datum_cbor: bytes, label: str, target_address: str) -> str:
    """Mint the CrossValidatorRefs NFT and create datum UTXO."""
    from pycardano import (
        TransactionBuilder, TransactionOutput, Asset, AssetName,
        MultiAsset, NativeScript, Value, Address,
    )
    from pycardano.plutus import RawPlutusData

    native_script = NativeScript.from_primitive(cbor2.loads(native_script_cbor))
    policy_id = native_script.hash()
    asset_name = AssetName(b"reputation_refs")

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000

    builder.native_scripts = [native_script]
    builder.mint = MultiAsset({policy_id: Asset({asset_name: 1})})

    output_addr = Address.from_primitive(target_address)
    refs_data = cbor2.loads(refs_datum_cbor)

    builder.add_output(
        TransactionOutput(
            output_addr,
            Value(3_000_000, MultiAsset({policy_id: Asset({asset_name: 1})})),
            datum=RawPlutusData(refs_data),
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
    print(f"  [{label}] Refs NFT minted: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


# ── Main deployment flow ─────────────────────────────────────────────────────

async def deploy():
    print("=" * 60)
    print("Module 3: Reputation Staking — Full Deployment")
    print("=" * 60)

    # ── Step 0: Load wallet and blueprints ────────────────────────────────

    wallet = load_wallet()
    print(f"\nWallet: {wallet['address']}")
    print(f"VKey Hash: {wallet['vkey_hash']}")

    if not BLUEPRINT_PATH.exists():
        print("ERROR: Blueprint not found. Run 'aiken build' in reputation-staking/")
        return
    if not HOLDER_BLUEPRINT_PATH.exists():
        print("ERROR: Holder blueprint not found. Run 'aiken build' in shared/holder-scripts/")
        return

    blueprint = load_blueprint(BLUEPRINT_PATH)
    print(f"Blueprint: {blueprint['preamble']['title']} v{blueprint['preamble']['version']}")

    # ── Step 1: Compute holder hashes ─────────────────────────────────────

    print("\n--- Step 1: Holder Script Hashes ---")
    holders = {}
    for tag, name in [(1, "params"), (3, "treasury")]:
        info = apply_holder_tag(HOLDER_BLUEPRINT_PATH, tag)
        holders[name] = info
        addr = script_hash_to_testnet_address(info["hash"])
        print(f"  {name}: hash={info['hash']}")
        print(f"         addr={addr}")

    # ── Step 2: Compute NativeScript policy ──────────────────────────────

    print("\n--- Step 2: NativeScript (refs_token_policy) ---")
    refs_policy_id, native_script_cbor = compute_native_script_policy(wallet["vkey_hash"])
    print(f"  Policy ID: {refs_policy_id}")

    # ── Step 3: Build ReputationConfig and apply to validators ────────────

    print("\n--- Step 3: Apply ReputationConfig ---")
    config_cbor = build_reputation_config_cbor(
        refs_token_policy=refs_policy_id,
        params_holder_hash=holders["params"]["hash"],
        treasury_holder_hash=holders["treasury"]["hash"],
        oracle_vkey_hash=wallet["vkey_hash"],
    )
    print(f"  Config CBOR: {len(config_cbor) // 2} bytes")

    applied_bp = apply_reputation_config(config_cbor)

    applied_validators = {}
    print("\n  Applied validator hashes:")
    for v in applied_bp["validators"]:
        title = v["title"]
        applied_validators[title] = {
            "hash": v["hash"],
            "compiled_code": v["compiledCode"],
        }
        if ".else" not in title:
            print(f"    {title}: {v['hash']}")

    reputation_hash = applied_validators["reputation.reputation.spend"]["hash"]
    endorsement_hash = applied_validators["endorsement.endorsement.spend"]["hash"]
    print(f"\n  Reputation validator hash: {reputation_hash}")
    print(f"  Endorsement validator hash: {endorsement_hash}")

    reputation_addr = script_hash_to_testnet_address(reputation_hash)
    endorsement_addr = script_hash_to_testnet_address(endorsement_hash)
    print(f"  Reputation address: {reputation_addr}")
    print(f"  Endorsement address: {endorsement_addr}")

    # ── Step 4: Deploy to testnet ────────────────────────────────────────

    print("\n--- Step 4: Deploy to Testnet ---")

    from vector_agent import VectorAgent

    async with VectorAgent(
        ogmios_url=OGMIOS_URL,
        submit_url=SUBMIT_URL,
        skey_path=str(SKEY_PATH.absolute()),
    ) as agent:
        balance = await agent.get_balance()
        print(f"  Balance: {balance.ada} AP3X ({balance.lovelace} lovelace)")

        min_required = 100_000_000  # ~100 AP3X for all deployments
        if balance.lovelace < min_required:
            print(f"  ERROR: Need at least {min_required / 1_000_000} AP3X. Fund wallet first.")
            save_deploy_state(wallet, holders, refs_policy_id, applied_validators,
                              reputation_hash, endorsement_hash, {})
            return

        # Resume support: load existing tx_hashes from prior runs
        existing_state = {}
        if DEPLOY_STATE_FILE.exists():
            with open(DEPLOY_STATE_FILE) as f:
                existing_state = json.load(f)
        tx_hashes = existing_state.get("tx_hashes", {})
        TX_WAIT = 20  # seconds between transactions

        # 4a: Deploy holder reference scripts
        print("\n  4a. Deploying holder reference scripts...")
        for name in ["params", "treasury"]:
            key = f"holder_{name}_ref"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
                continue
            tx = await deploy_reference_script(agent, holders[name]["compiled_code"], f"holder-{name}")
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 4b: Deploy Module-3 validator reference scripts
        print("\n  4b. Deploying Module-3 validator reference scripts...")
        ref_script_keys = [
            ("reputation.reputation.mint", "reputation_mint"),
            ("reputation.reputation.spend", "reputation_spend"),
            ("endorsement.endorsement.mint", "endorsement_mint"),
            ("endorsement.endorsement.spend", "endorsement_spend"),
        ]
        for title, label in ref_script_keys:
            key = f"{label}_ref"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
                continue
            tx = await deploy_reference_script(agent, applied_validators[title]["compiled_code"], label)
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 4c: Create ProtocolParams UTXO at params holder address
        if "params_utxo" not in tx_hashes:
            print("\n  4c. Creating ProtocolParams UTXO...")
            params_datum_cbor = build_protocol_params_datum()
            params_addr = script_hash_to_testnet_address(holders["params"]["hash"])
            tx = await create_datum_utxo(
                agent, params_addr, params_datum_cbor, 3_000_000, "params"
            )
            tx_hashes["params_utxo"] = tx
            await asyncio.sleep(TX_WAIT)
        else:
            print(f"\n  4c. ProtocolParams already deployed (skipping)")

        # 4d: Mint CrossValidatorRefs NFT and create datum UTXO
        if "cross_refs_nft" not in tx_hashes:
            print("\n  4d. Minting CrossValidatorRefs NFT...")
            refs_datum_cbor = build_cross_refs_datum(reputation_hash, endorsement_hash)
            # Store at params holder address (prevent coin selection from consuming it)
            params_holder_addr = script_hash_to_testnet_address(holders["params"]["hash"])
            tx = await mint_refs_nft(
                agent, native_script_cbor, refs_datum_cbor, "cross-refs",
                target_address=params_holder_addr,
            )
            tx_hashes["cross_refs_nft"] = tx
        else:
            print(f"\n  4d. CrossRefs NFT already minted (skipping)")

        print(f"\n  Final balance: {(await agent.get_balance()).ada} AP3X")

    # ── Step 5: Save deployment state ────────────────────────────────────

    save_deploy_state(wallet, holders, refs_policy_id, applied_validators,
                      reputation_hash, endorsement_hash, tx_hashes)

    # ── Step 6: Save applied blueprint to deploy/ ────────────────────────

    deploy_dir = MODULE3_ROOT / "deploy"
    deploy_dir.mkdir(exist_ok=True)
    with open(deploy_dir / "plutus.json", "w") as f:
        json.dump(applied_bp, f, indent=2)
    print(f"\nApplied blueprint saved to deploy/plutus.json")

    # Save deployment.json
    deployment = {
        "version": "v1",
        "hashes": {
            "reputation": reputation_hash,
            "endorsement": endorsement_hash,
        },
        "addresses": {
            "reputation": reputation_addr,
            "endorsement": endorsement_addr,
        },
        "cross_refs_utxo": f"{tx_hashes.get('cross_refs_nft', 'NOT_DEPLOYED')}#0",
        "params_utxo": f"{tx_hashes.get('params_utxo', 'NOT_DEPLOYED')}#0",
    }
    for title, label in ref_script_keys:
        key = f"{label}_ref"
        if key in tx_hashes:
            deployment[key] = f"{tx_hashes[key]}#0"
    with open(deploy_dir / "deployment.json", "w") as f:
        json.dump(deployment, f, indent=2)
    print(f"Deployment info saved to deploy/deployment.json")

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print("=" * 60)
    print(f"\nNext: python scripts/smoke_test.py")


def save_deploy_state(wallet, holders, refs_policy_id, applied_validators,
                      reputation_hash, endorsement_hash, tx_hashes):
    state = {
        "network": "vector-testnet",
        "wallet_address": wallet["address"],
        "wallet_vkey_hash": wallet["vkey_hash"],
        "refs_token_policy": refs_policy_id,
        "holders": {
            name: {
                "hash": info["hash"],
                "address": script_hash_to_testnet_address(info["hash"]),
                "compiled_code": info["compiled_code"],
            }
            for name, info in holders.items()
        },
        "validators": {
            title: {"hash": info["hash"], "compiled_code": info["compiled_code"]}
            for title, info in applied_validators.items()
        },
        "reputation_validator_hash": reputation_hash,
        "endorsement_validator_hash": endorsement_hash,
        "agent_registry_hash": AGENT_REGISTRY_HASH,
        "ap3x_policy_id": AP3X_POLICY_ID,
        "ap3x_asset_name": AP3X_ASSET_NAME,
        "tx_hashes": tx_hashes,
        "explorer_url": EXPLORER_URL,
        "ogmios_url": OGMIOS_URL,
        "submit_url": SUBMIT_URL,
    }
    WALLET_DIR.mkdir(exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nDeployment state saved to {DEPLOY_STATE_FILE}")


if __name__ == "__main__":
    asyncio.run(deploy())
