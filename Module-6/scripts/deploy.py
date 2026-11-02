"""
Module 6: Full Deployment Script (testnet or mainnet)

Deployment order (deploy_common.py): config_holder, config_seal, identity,
Module-1 with identity_party, the seal, then Module-3, then Module-6 (this
script). New Foundation operator keys deploy and operate the modules. The
wallet key (wallets/ or VECTOR_MNEMONIC_FILE) must be one of these keys. Old
module keys are not reused.

Handles the complete deployment flow:
  0. Read identity_hash from VECTOR_IDENTITY_HASH (no default). Stop when it
     is missing or not allowed.
  1. Compute the holder scripts (params/oracle/treasury), owned by the wallet key
  2. Compute the NativeScript refs_token_policy
  3. Choose a seed UTxO and derive the one-shot record token policy
  4. Apply GovernanceConfig (11 fields, identity_hash last) to all validators
     via `aiken blueprint apply`
  5. Deploy to the chain:
     a. One transaction spends the seed, mints the `params` and `oracle` record
        tokens, and creates the GovernanceParams record (output 0) and the
        oracle record (output 1)
     b. Deploy holder reference scripts (params/oracle/treasury)
     c. Deploy Module-6 validator reference scripts (CIP-33)
     d. Create Treasury batch UTxOs
     e. Mint refs NFT and deploy GovernanceCrossRefs datum
  6. Save full deployment state

Resume: the seed, the record token policy, identity_hash and the tx hashes
are kept in wallets/deploy_state.json. A new run reuses them and skips
completed steps. A run with another identity_hash stops.

Usage:
    Set VECTOR_IDENTITY_HASH (environment or .env) to the script hash of the
    identity contract, then:
    nix-shell shell.nix --run "python scripts/deploy.py"

    For mainnet, set env vars:
        VECTOR_OGMIOS_URL=https://ogmios.vector.mainnet.apexfusion.org
        VECTOR_SUBMIT_URL=https://submit.vector.mainnet.apexfusion.org/api/submit/tx
        VECTOR_EXPLORER_URL=https://vector.apexscan.org

Prerequisites:
    1. Wallet funded (run setup_wallet.py or import_wallet.py first)
    2. Aiken contracts compiled (aiken build in contracts/governance-suggestion)
    3. Holder scripts compiled (aiken build in shared/holder-scripts)
    4. The identity registry deployed (deployment order above)
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

from deploy_common import (
    HOLDER_BLUEPRINT_PATH,
    HOLDER_TAGS,
    IDENTITY_HASH_ENV,
    ORACLE_TOKEN_NAME,
    PARAMS_TOKEN_NAME,
    UNIT_REDEEMER,
    DeployInputError,
    apply_holder,
    apply_record_token_policy,
    build_governance_config_cbor,
    find_record_utxo,
    identity_hash_from_env,
    parse_utxo_ref,
    resume_state_error,
    select_collateral_utxo,
    select_seed_utxo,
    utxo_ref,
)

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────

GAME6_ROOT = Path(__file__).parent.parent
CONTRACT_DIR = GAME6_ROOT / "contracts" / "governance-suggestion"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
WALLET_FILE = GAME6_ROOT / "wallets" / "governance_wallet.json"
SKEY_PATH = GAME6_ROOT / "wallets" / "payment.skey"
MNEMONIC_FILE = os.getenv("VECTOR_MNEMONIC_FILE")  # Optional: path to BIP39 mnemonic file
DEPLOY_STATE_FILE = GAME6_ROOT / "wallets" / "deploy_state.json"

# ── Network config ───────────────────────────────────────────────────────────

OGMIOS_URL = os.getenv("VECTOR_OGMIOS_URL", "https://ogmios.vector.testnet.apexfusion.org")
SUBMIT_URL = os.getenv("VECTOR_SUBMIT_URL", "https://submit.vector.testnet.apexfusion.org/api/submit/tx")
EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")
NETWORK = "vector-mainnet" if "mainnet" in OGMIOS_URL else "vector-testnet"


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


# ── Apply parameters ────────────────────────────────────────────────────────

def apply_governance_config(config_cbor_hex: str) -> dict:
    """Apply GovernanceConfig to all Module-6 validators, return applied blueprint."""
    # Start from the raw blueprint
    current = str(BLUEPRINT_PATH)
    validators_to_apply = [
        ("proposal", "proposal_mint"),
        ("proposal", "proposal_spend"),
        ("critique", "critique_mint"),
        ("critique", "critique_spend"),
        ("critique", "endorsement_spend"),
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
            # Write intermediate to temp file
            out_path = str(CONTRACT_DIR / f"plutus-applied-{i}.json")
            cmd.extend(["-o", out_path])
            subprocess.run(cmd, check=True)
            current = out_path
        else:
            # Last one — capture stdout
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            applied_bp = json.loads(result.stdout)
            current = None

    # Clean up intermediate files
    for i in range(len(validators_to_apply) - 1):
        f = CONTRACT_DIR / f"plutus-applied-{i}.json"
        if f.exists():
            f.unlink()

    return applied_bp


# ── Bech32 address computation ───────────────────────────────────────────────

def script_hash_to_address(script_hash_hex: str) -> str:
    """Convert a script hash to a Bech32 address (Vector uses mainnet flag)."""
    from pycardano import Address, Network, ScriptHash
    script_hash = ScriptHash(bytes.fromhex(script_hash_hex))
    addr = Address(payment_part=script_hash, network=Network.MAINNET)
    return str(addr)


# ── Transaction signing ────────────────────────────────────────────────────

def sign_tx(builder, agent):
    """Build and sign a tx. Works with both standard and extended (HD) signing keys.

    pycardano's build_and_sign has a bug with PaymentExtendedSigningKey where
    to_verification_key() returns an empty payload. We work around it by
    building the body first, then the witness set from the builder (scripts,
    redeemers, datums), and then adding the verification key witness manually.
    """
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


# ── Deployment transactions ─────────────────────────────────────────────────

async def deploy_reference_script(agent, script_cbor_hex: str, label: str) -> str:
    """Deploy a validator as a reference script at an unspendable address."""
    from pycardano import TransactionBuilder, TransactionOutput, PlutusV3Script, Address, Network
    from pycardano.plutus import script_hash as compute_script_hash

    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_size = len(script_cbor_hex) // 2

    # Send to unspendable script-hash-derived address (prevents accidental consumption)
    sh = compute_script_hash(script)
    unspendable_addr = Address(payment_part=sh, network=Network.MAINNET)

    # Min UTXO for reference scripts: ~4400 lovelace per byte + 2 AP3X base
    # (Conway era charges heavily for inline scripts)
    min_lovelace = 2_000_000 + script_size * 4400
    # Round up to nearest AP3X + 2 AP3X safety margin
    min_lovelace = ((min_lovelace + 999_999) // 1_000_000 + 2) * 1_000_000

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)

    builder.add_output(
        TransactionOutput(
            unspendable_addr,
            min_lovelace,
            script=script,
        )
    )

    # pycardano underestimates fees for transactions with reference scripts
    # because it doesn't account for the script bytes in the fee calculation.
    # Add a fee buffer proportional to script size.
    builder.fee_buffer = max(300_000, script_size * 50)

    tx = sign_tx(builder, agent)

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


async def create_datum_utxo(agent, address, datum_data, lovelace: int, label: str) -> str:
    """Create a UTXO at a script address with an inline datum."""
    from pycardano import TransactionBuilder, TransactionOutput, Address
    from pycardano.plutus import RawPlutusData

    if isinstance(address, str):
        address = Address.from_primitive(address)

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000  # pycardano fee underestimate workaround

    builder.add_output(
        TransactionOutput(
            address,
            lovelace,
            datum=RawPlutusData(datum_data),
        )
    )

    tx = sign_tx(builder, agent)

    tx_cbor = tx.to_cbor()
    if isinstance(tx_cbor, bytes):
        tx_cbor = tx_cbor.hex()

    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor)
    print(f"  [{label}] Datum UTXO created: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def mint_record_tokens(agent, seed_utxo, collateral_utxo, record_token: dict,
                             records: list, label: str) -> str:
    """Mint the record tokens and create the records in one transaction.

    The transaction spends the seed UTxO: the one-shot record token policy
    accepts a mint only in that transaction. `records` is a list of
    (address, datum_data, token_name). Output i of the transaction is record i:
    3 AP3X, one record token and the inline datum. The policy is a Plutus V3
    script, so the transaction carries the script, a unit redeemer and
    collateral.
    """
    from pycardano import (
        Address, Asset, AssetName, MultiAsset, PlutusV3Script, Redeemer,
        ScriptHash, TransactionBuilder, TransactionOutput, Value,
    )
    from pycardano.plutus import RawPlutusData

    policy_id = ScriptHash(bytes.fromhex(record_token["policy_id"]))
    policy_script = PlutusV3Script(bytes.fromhex(record_token["compiled_code"]))

    builder = TransactionBuilder(agent.context)
    builder.add_input(seed_utxo)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 300_000  # pycardano fee underestimate workaround

    builder.mint = MultiAsset({
        policy_id: Asset({AssetName(token_name): 1 for _, _, token_name in records})
    })
    builder.add_minting_script(policy_script, Redeemer(RawPlutusData(UNIT_REDEEMER)))

    for address, datum_data, token_name in records:
        builder.add_output(
            TransactionOutput(
                Address.from_primitive(address),
                Value(3_000_000, MultiAsset({policy_id: Asset({AssetName(token_name): 1})})),
                datum=RawPlutusData(datum_data),
            )
        )

    builder.collaterals = [collateral_utxo]

    tx = sign_tx(builder, agent)

    tx_cbor = tx.to_cbor()
    if isinstance(tx_cbor, bytes):
        tx_cbor = tx_cbor.hex()

    tx_hash = str(tx.id)
    await agent.context.async_submit_tx_cbor(tx_cbor)
    print(f"  [{label}] Record tokens minted: {tx_hash}")
    print(f"    Explorer: {EXPLORER_URL}/tx/{tx_hash}")
    return tx_hash


async def mint_refs_nft(agent, native_script_cbor: bytes, refs_datum_data, label: str, target_address: str = None) -> str:
    """Mint a refs NFT and create the CrossRefs UTXO with inline datum.

    If target_address is provided, the NFT is sent there (e.g. oracle holder).
    Otherwise it goes to the wallet address.
    """
    from pycardano import (
        TransactionBuilder, TransactionOutput, Asset, AssetName,
        MultiAsset, NativeScript, ScriptPubkey, Value, Address,
    )
    from pycardano.plutus import RawPlutusData

    # Build the NativeScript from raw CBOR
    native_script = NativeScript.from_primitive(cbor2.loads(native_script_cbor))

    policy_id = native_script.hash()
    asset_name = AssetName(b"governance_refs")

    builder = TransactionBuilder(agent.context)
    builder.add_input_address(agent._wallet.payment_address)
    builder.fee_buffer = 200_000  # pycardano fee underestimate workaround

    # Mint the refs NFT
    builder.native_scripts = [native_script]
    builder.mint = MultiAsset({policy_id: Asset({asset_name: 1})})

    # Output: NFT + inline CrossRefs datum at target address
    # (defaults to wallet, but oracle holder is preferred to avoid coin selection)
    output_addr = Address.from_primitive(target_address) if target_address else agent._wallet.payment_address
    builder.add_output(
        TransactionOutput(
            output_addr,
            Value(3_000_000, MultiAsset({policy_id: Asset({asset_name: 1})})),
            datum=RawPlutusData(refs_datum_data),
        )
    )

    tx = sign_tx(builder, agent)

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
    print("Module 6: Self-Improvement Module — Full Deployment")
    print("=" * 60)

    # identity_hash comes from the identity deployment (deploy_common.py)
    try:
        identity_hash = identity_hash_from_env()
    except DeployInputError as e:
        print(f"ERROR: {e}")
        return
    print(f"\n{IDENTITY_HASH_ENV}: {identity_hash}")

    # ── Step 0: Load wallet, blueprints and deploy state ─────────────────

    mnemonic = None
    if MNEMONIC_FILE:
        mnemonic = Path(MNEMONIC_FILE).read_text().strip()
        # Derive vkey_hash from mnemonic using SDK's HD wallet
        from vector_agent.wallet.hd import HDWallet as VectorHDWallet
        hd = VectorHDWallet(mnemonic)
        wallet = {
            "address": str(hd.payment_address),
            "vkey_hash": str(hd.payment_verification_key.hash()),
        }
    else:
        wallet = load_wallet()
    print(f"\nWallet: {wallet['address']}")
    print(f"VKey Hash: {wallet['vkey_hash']}")

    if not BLUEPRINT_PATH.exists():
        print("ERROR: Module-6 blueprint not found. Run 'aiken build' in contracts/governance-suggestion/")
        return
    if not HOLDER_BLUEPRINT_PATH.exists():
        print("ERROR: Holder blueprint not found. Run 'aiken build' in shared/holder-scripts/")
        return

    blueprint = load_blueprint(BLUEPRINT_PATH)
    print(f"Blueprint: {blueprint['preamble']['title']} v{blueprint['preamble']['version']}")

    # Resume support: load the state of prior runs
    existing_state = {}
    if DEPLOY_STATE_FILE.exists():
        with open(DEPLOY_STATE_FILE) as f:
            existing_state = json.load(f)
    tx_hashes = existing_state.get("tx_hashes", {})
    state_error = resume_state_error(existing_state, wallet["vkey_hash"], identity_hash)
    if state_error:
        print(f"ERROR: {DEPLOY_STATE_FILE}: {state_error}")
        return
    saved_policy = existing_state.get("record_token_policy")
    saved_seed = existing_state.get("record_token_seed")

    # ── Step 1: Compute holder hashes ─────────────────────────────────────

    print("\n--- Step 1: Holder Script Hashes (owner = wallet key) ---")
    holders = {}
    for tag, name in HOLDER_TAGS:
        info = apply_holder(tag, wallet["vkey_hash"], HOLDER_BLUEPRINT_PATH)
        holders[name] = info
        addr = script_hash_to_address(info["hash"])
        print(f"  {name}: hash={info['hash']}")
        print(f"         addr={addr}")

    # ── Step 2: Compute NativeScript policy ──────────────────────────────

    print("\n--- Step 2: NativeScript (refs_token_policy) ---")
    refs_policy_id, native_script_cbor = compute_native_script_policy(wallet["vkey_hash"])
    print(f"  Policy ID: {refs_policy_id}")

    # ── Step 3: Record token policy (seed UTxO) ──────────────────────────

    print("\n--- Step 3: Record Token Policy ---")

    from vector_agent import VectorAgent

    agent_kwargs = dict(ogmios_url=OGMIOS_URL, submit_url=SUBMIT_URL)
    if mnemonic:
        agent_kwargs["mnemonic"] = mnemonic
    else:
        agent_kwargs["skey_path"] = str(SKEY_PATH.absolute())

    if saved_seed:
        # Never choose a new seed once a policy was saved
        seed_ref = saved_seed
        print(f"  Seed (from deploy state): {seed_ref}")
    else:
        async with VectorAgent(**agent_kwargs) as agent:
            wallet_utxos = await agent.context.async_utxos(str(agent._wallet.payment_address))
        if not wallet_utxos:
            print("  ERROR: Wallet has no UTxO. Fund wallet first.")
            return
        seed_ref = utxo_ref(select_seed_utxo(wallet_utxos))
        print(f"  Seed (new): {seed_ref}")

    record_token = apply_record_token_policy(*parse_utxo_ref(seed_ref))
    record_token["seed"] = seed_ref
    if saved_policy and saved_policy != record_token["policy_id"]:
        print(f"  ERROR: The seed gives policy {record_token['policy_id']}, "
              f"but the deploy state has {saved_policy}.")
        print("         The record token script changed after the first run.")
        return
    print(f"  Policy ID: {record_token['policy_id']}")

    # ── Step 4: Build GovernanceConfig and apply to validators ────────────

    print("\n--- Step 4: Apply GovernanceConfig ---")
    config_cbor = build_governance_config_cbor(
        refs_token_policy=refs_policy_id,
        oracle_holder_hash=holders["oracle"]["hash"],
        params_holder_hash=holders["params"]["hash"],
        treasury_holder_hash=holders["treasury"]["hash"],
        record_token_policy=record_token["policy_id"],
        identity_hash=identity_hash,
    )
    print(f"  Config CBOR: {len(config_cbor) // 2} bytes")

    applied_bp = apply_governance_config(config_cbor)

    applied_validators = {}
    print("\n  Applied validator hashes:")
    for v in applied_bp["validators"]:
        title = v["title"]
        applied_validators[title] = {
            "hash": v["hash"],
            "compiled_code": v["compiledCode"],
        }
        print(f"    {title}: {v['hash']}")

    # Extract the key hashes (proposal mint/spend share hash, critique mint/spend share hash)
    proposal_hash = applied_validators["proposal.proposal_spend.spend"]["hash"]
    critique_hash = applied_validators["critique.critique_spend.spend"]["hash"]
    print(f"\n  Proposal validator hash: {proposal_hash}")
    print(f"  Critique validator hash: {critique_hash}")

    # Keep the seed, the record token policy and identity_hash before the
    # first on-chain step
    save_deploy_state(wallet, holders, refs_policy_id, record_token, applied_validators,
                      proposal_hash, critique_hash, tx_hashes, identity_hash)

    # ── Step 5: Deploy to the chain ──────────────────────────────────────

    print(f"\n--- Step 5: Deploy to {NETWORK} ---")

    async with VectorAgent(**agent_kwargs) as agent:
        balance = await agent.get_balance()
        print(f"  Balance: {balance.ada} AP3X ({balance.lovelace} lovelace)")

        min_required = 150_000_000  # ~150 AP3X for all deployments
        if balance.lovelace < min_required:
            print(f"  ERROR: Need at least {min_required / 1_000_000} AP3X. Fund wallet first.")
            return

        TX_WAIT = 20  # seconds between transactions for UTxO confirmation

        params_addr = script_hash_to_address(holders["params"]["hash"])
        oracle_addr = script_hash_to_address(holders["oracle"]["hash"])

        # 5a: First on-chain step. One transaction spends the seed, mints the
        # `params` and `oracle` record tokens, and creates the GovernanceParams
        # record (output 0) and the oracle record (output 1).
        if "params_utxo" in tx_hashes and "oracle_utxo" in tx_hashes:
            print(f"\n  5a. GovernanceParams and Oracle records already created (skipping)")
        else:
            print("\n  5a. Minting record tokens + creating GovernanceParams and Oracle records...")
            wallet_utxos = await agent.context.async_utxos(str(agent._wallet.payment_address))
            seed_utxo = next(
                (u for u in wallet_utxos if utxo_ref(u) == record_token["seed"]), None
            )
            if seed_utxo is None:
                # The seed is spent: a prior run can have created the records
                params_record = find_record_utxo(
                    await agent.context.async_utxos(params_addr),
                    record_token["policy_id"], PARAMS_TOKEN_NAME,
                )
                oracle_record = find_record_utxo(
                    await agent.context.async_utxos(oracle_addr),
                    record_token["policy_id"], ORACLE_TOKEN_NAME,
                )
                if params_record is None or oracle_record is None:
                    print(f"  ERROR: Seed UTxO {record_token['seed']} is spent and the records are not on chain.")
                    print("         If the record transaction is pending, wait and run again.")
                    print(f"         Otherwise move {DEPLOY_STATE_FILE} to a different path")
                    print("         and start a new deployment.")
                    return
                tx_hashes["params_utxo"] = str(params_record.input.transaction_id)
                tx_hashes["oracle_utxo"] = str(oracle_record.input.transaction_id)
                print(f"  Records found: params {utxo_ref(params_record)}, "
                      f"oracle {utxo_ref(oracle_record)}")
            else:
                from vector_agent.governance.datums import build_governance_params, build_oracle_datum
                params_datum = build_governance_params()
                oracle_datum = build_oracle_datum(
                    oracle_vkey_hash=bytes.fromhex(wallet["vkey_hash"]),
                    treasury_script_hash=bytes.fromhex(holders["treasury"]["hash"]),
                )
                # The oracle credential must be a verification key credential
                # (constructor 0). The validators reject a script credential.
                if oracle_datum.data.value[0].tag != 121:
                    print("  ERROR: The oracle datum credential is not a verification key credential.")
                    return
                collateral = select_collateral_utxo(wallet_utxos, avoid_ref=record_token["seed"])
                tx = await mint_record_tokens(
                    agent, seed_utxo, collateral, record_token,
                    [
                        (params_addr, params_datum.data, PARAMS_TOKEN_NAME),  # output 0
                        (oracle_addr, oracle_datum.data, ORACLE_TOKEN_NAME),  # output 1
                    ],
                    "records",
                )
                tx_hashes["params_utxo"] = tx
                tx_hashes["oracle_utxo"] = tx
                await asyncio.sleep(TX_WAIT)
            save_deploy_state(wallet, holders, refs_policy_id, record_token, applied_validators,
                              proposal_hash, critique_hash, tx_hashes, identity_hash)

        # 5b: Deploy holder reference scripts
        print("\n  5b. Deploying holder reference scripts...")
        for name in ["params", "oracle", "treasury"]:
            key = f"holder_{name}_ref"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
                continue
            tx = await deploy_reference_script(agent, holders[name]["compiled_code"], f"holder-{name}")
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 5c: Deploy Module-6 validator reference scripts
        print("\n  5c. Deploying Module-6 validator reference scripts...")
        ref_script_keys = [
            ("proposal.proposal_mint.mint", "proposal_mint"),
            ("proposal.proposal_spend.spend", "proposal_spend"),
            ("critique.critique_mint.mint", "critique_mint"),
            ("critique.critique_spend.spend", "critique_spend"),
            ("critique.endorsement_spend.spend", "endorsement_spend"),
        ]
        for title, label in ref_script_keys:
            key = f"{label}_ref"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed: {tx_hashes[key][:16]}... (skipping)")
                continue
            tx = await deploy_reference_script(agent, applied_validators[title]["compiled_code"], label)
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 5d: Create Treasury batch UTxOs at treasury holder address
        print("\n  5d. Creating Treasury batch UTxOs...")
        from vector_agent.governance.datums import build_treasury_batch_datum
        treasury_addr = script_hash_to_address(holders["treasury"]["hash"])
        for batch_id in range(1, 4):  # 3 batches
            key = f"treasury_batch_{batch_id}"
            if key in tx_hashes:
                print(f"  [{key}] Already deployed (skipping)")
                continue
            batch_datum = build_treasury_batch_datum(batch_id, True)
            batch_lovelace = 30_000_000  # 30 AP3X per batch
            tx = await create_datum_utxo(
                agent, treasury_addr, batch_datum.data, batch_lovelace,
                f"treasury-batch-{batch_id}"
            )
            tx_hashes[key] = tx
            await asyncio.sleep(TX_WAIT)

        # 5e: Mint refs NFT and deploy GovernanceCrossRefs datum
        if "cross_refs_nft" not in tx_hashes:
            print("\n  5e. Minting refs NFT + deploying GovernanceCrossRefs...")
            proposal_mint_hash = applied_validators.get(
                "proposal.proposal_mint.mint", {}
            ).get("hash", "")
            critique_mint_hash = applied_validators.get(
                "critique.critique_mint.mint", {}
            ).get("hash", "")
            cross_refs_data = cbor2.CBORTag(121, [
                bytes.fromhex(proposal_hash),
                bytes.fromhex(critique_hash),
                bytes.fromhex(proposal_mint_hash),
                bytes.fromhex(critique_mint_hash),
            ])
            # Mint at oracle holder address (not wallet) to prevent
            # coin selection from consuming the CrossRefs UTxO.
            oracle_holder_addr = script_hash_to_address(holders["oracle"]["hash"])
            tx = await mint_refs_nft(
                agent, native_script_cbor, cross_refs_data, "cross-refs",
                target_address=oracle_holder_addr,
            )
            tx_hashes["cross_refs_nft"] = tx
        else:
            print(f"\n  5e. CrossRefs NFT already minted (skipping)")

        print(f"\n  Final balance: {(await agent.get_balance()).ada} AP3X")

    # ── Step 6: Save deployment state ────────────────────────────────────

    save_deploy_state(wallet, holders, refs_policy_id, record_token, applied_validators,
                      proposal_hash, critique_hash, tx_hashes, identity_hash)

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print("=" * 60)
    print(f"\nNext: python scripts/smoke_test.py")


def save_deploy_state(wallet, holders, refs_policy_id, record_token, applied_validators,
                      proposal_hash, critique_hash, tx_hashes, identity_hash):
    """Write wallets/deploy_state.json.

    record_token: {"policy_id", "seed", ...} of the one-shot record token
    policy. The holders are owned by the wallet key (holder_owner_vkey_hash).
    identity_hash: the last GovernanceConfig field, from the identity
    deployment.
    """
    state = {
        "network": NETWORK,
        "wallet_address": wallet["address"],
        "wallet_vkey_hash": wallet["vkey_hash"],
        "holder_owner_vkey_hash": wallet["vkey_hash"],
        "refs_token_policy": refs_policy_id,
        "record_token_policy": record_token["policy_id"],
        "record_token_seed": record_token["seed"],
        "holders": {
            name: {"hash": info["hash"],
                    "address": script_hash_to_address(info["hash"]),
                    "compiled_code": info["compiled_code"]}
            for name, info in holders.items()
        },
        "validators": {
            title: {"hash": info["hash"], "compiled_code": info["compiled_code"]}
            for title, info in applied_validators.items()
        },
        "proposal_validator_hash": proposal_hash,
        "critique_validator_hash": critique_hash,
        "identity_hash": identity_hash,
        "cross_refs_address": script_hash_to_address(holders["oracle"]["hash"]),
        "tx_hashes": tx_hashes,
        "explorer_url": EXPLORER_URL,
        "ogmios_url": OGMIOS_URL,
        "submit_url": SUBMIT_URL,
    }
    DEPLOY_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nDeployment state saved to {DEPLOY_STATE_FILE}")


if __name__ == "__main__":
    asyncio.run(deploy())
