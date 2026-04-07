"""
Chain interaction layer — Ogmios RPC, UTxO resolution, TX submission.
Extracted from testnet/deploy_and_run_v10.py for reuse.
"""
import cbor2
import hashlib
import json
import os
import requests
import time

from pycardano import (
    Address, Network, PaymentSigningKey, PaymentVerificationKey,
    PlutusV3Script, TransactionBuilder, TransactionOutput,
    TransactionInput, UTxO, RawCBOR, RawPlutusData, Redeemer,
    MultiAsset, Asset, AssetName, ScriptHash, Value,
    ExecutionUnits, ScriptPubkey, ScriptAll,
)
from pycardano.hash import TransactionId, VerificationKeyHash
from pycardano.backend.base import ProtocolParameters

from simulation.config import OGMIOS_URL, TX_SUBMIT_URL, SYSTEM_START_UNIX, NETWORK


# ═══════════════════════════════════════════════════════════════════════
# PLUTUS DATA HELPER
# ═══════════════════════════════════════════════════════════════════════

class DP(RawPlutusData):
    """Datum/redeemer wrapper for raw CBOR."""
    def to_cbor(self):
        return cbor2.dumps(self.data)


# ═══════════════════════════════════════════════════════════════════════
# OGMIOS RPC
# ═══════════════════════════════════════════════════════════════════════

def ogmios_rpc(method, params=None):
    """Call Ogmios JSON-RPC endpoint."""
    body = {"jsonrpc": "2.0", "method": method, "id": None}
    if params:
        body["params"] = params
    r = requests.post(OGMIOS_URL, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Ogmios {method}: {data['error']}")
    return data.get("result", data)


# ═══════════════════════════════════════════════════════════════════════
# CONTEXT (Protocol Params + Slot + UTxO queries)
# ═══════════════════════════════════════════════════════════════════════

class OgmiosContext:
    """Lightweight Ogmios-backed chain context for PyCardano."""

    def __init__(self):
        self._pp = None

    def _frac(self, v):
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/")
            return int(a) / int(b)
        return float(v)

    @property
    def protocol_param(self):
        if self._pp is None:
            raw = ogmios_rpc("queryLedgerState/protocolParameters")
            self._pp = ProtocolParameters(
                min_fee_constant=raw.get("minFeeConstant", {}).get("ada", {}).get("lovelace", 155381),
                min_fee_coefficient=raw.get("minFeeCoefficient", 44),
                max_block_size=raw.get("maxBlockBodySize", {}).get("bytes", 90112),
                max_tx_size=raw.get("maxTransactionSize", {}).get("bytes", 16384),
                max_block_header_size=raw.get("maxBlockHeaderSize", {}).get("bytes", 1100),
                key_deposit=raw.get("stakeCredentialDeposit", {}).get("ada", {}).get("lovelace", 2000000),
                pool_deposit=raw.get("stakePoolDeposit", {}).get("ada", {}).get("lovelace", 500000000),
                pool_influence=self._frac(raw.get("stakePoolPledgeInfluence", "3/10")),
                monetary_expansion=self._frac(raw.get("monetaryExpansion", "3/1000")),
                treasury_expansion=self._frac(raw.get("treasuryExpansion", "2/10")),
                decentralization_param=0,
                extra_entropy="",
                protocol_major_version=raw.get("version", {}).get("major", 10),
                protocol_minor_version=raw.get("version", {}).get("minor", 0),
                min_utxo=raw.get("minUtxoDepositCoefficient", 4310),
                min_pool_cost=raw.get("minStakePoolCost", {}).get("ada", {}).get("lovelace", 170000000),
                price_mem=self._frac(raw.get("scriptExecutionPrices", {}).get("memory", "577/10000")),
                price_step=self._frac(raw.get("scriptExecutionPrices", {}).get("cpu", "721/10000000")),
                max_tx_ex_mem=raw.get("maxExecutionUnitsPerTransaction", {}).get("memory", 14000000),
                max_tx_ex_steps=raw.get("maxExecutionUnitsPerTransaction", {}).get("cpu", 10000000000),
                max_block_ex_mem=raw.get("maxExecutionUnitsPerBlock", {}).get("memory", 62000000),
                max_block_ex_steps=raw.get("maxExecutionUnitsPerBlock", {}).get("cpu", 40000000000),
                max_val_size=raw.get("maxValueSize", {}).get("bytes", 5000),
                collateral_percent=raw.get("collateralPercentage", 150),
                max_collateral_inputs=raw.get("maxCollateralInputs", 3),
                coins_per_utxo_byte=raw.get("minUtxoDepositCoefficient", 4310),
                cost_models={},
            )
        return self._pp

    @property
    def last_block_slot(self):
        return ogmios_rpc("queryLedgerState/tip").get("slot", 0)

    def genesis_param(self):
        return None

    def utxos(self, address):
        """Query UTxOs at an address via Ogmios."""
        result = ogmios_rpc("queryLedgerState/utxo", {"addresses": [address]})
        utxos = []
        for item in result:
            txid = item["transaction"]["id"]
            idx = item["index"]
            ti = TransactionInput(TransactionId(bytes.fromhex(txid)), idx)

            value = item.get("value", {})
            lovelace = value.get("ada", {}).get("lovelace", 0)

            ma = MultiAsset()
            for policy_hex, assets_map in value.items():
                if policy_hex == "ada":
                    continue
                policy_sh = ScriptHash(bytes.fromhex(policy_hex))
                a = Asset()
                for asset_name_hex, qty in assets_map.items():
                    a[AssetName(bytes.fromhex(asset_name_hex))] = qty
                ma[policy_sh] = a

            pv = Value(lovelace, ma) if ma else lovelace

            datum = None
            if "datum" in item:
                datum_hex = item["datum"]
                datum = RawCBOR(bytes.fromhex(datum_hex))

            script_ref = None
            if "script" in item:
                scr = item["script"]
                if "plutus:v3" in scr:
                    script_ref = PlutusV3Script(bytes.fromhex(scr["plutus:v3"]))

            addr = Address.from_primitive(item.get("address", address))
            to = TransactionOutput(addr, pv, datum=datum, script=script_ref)
            utxos.append(UTxO(ti, to))
        return utxos


def resolve_utxo(txid_str, idx):
    """Resolve a specific UTxO by TX hash + index."""
    result = ogmios_rpc("queryLedgerState/utxo",
                        {"outputReferences": [{"transaction": {"id": txid_str}, "index": idx}]})
    if not result:
        raise RuntimeError(f"UTxO {txid_str}#{idx} not found")
    item = result[0]

    ti = TransactionInput(TransactionId(bytes.fromhex(txid_str)), idx)
    value = item.get("value", {})
    lovelace = value.get("ada", {}).get("lovelace", 0)

    ma = MultiAsset()
    for policy_hex, assets_map in value.items():
        if policy_hex == "ada":
            continue
        policy_sh = ScriptHash(bytes.fromhex(policy_hex))
        a = Asset()
        for asset_name_hex, qty in assets_map.items():
            a[AssetName(bytes.fromhex(asset_name_hex))] = qty
        ma[policy_sh] = a

    pv = Value(lovelace, ma) if ma else lovelace

    datum = None
    if "datum" in item:
        datum = RawCBOR(bytes.fromhex(item["datum"]))

    script_ref = None
    if "script" in item:
        scr = item["script"]
        if "plutus:v3" in scr:
            script_ref = PlutusV3Script(bytes.fromhex(scr["plutus:v3"]))

    addr = Address.from_primitive(item.get("address", ""))
    to = TransactionOutput(addr, pv, datum=datum, script=script_ref)
    return UTxO(ti, to)


def resolve_ref_utxo(txid, idx):
    """Resolve a reference script UTxO."""
    utxo = resolve_utxo(txid, idx)
    if utxo.output.script is None:
        raise RuntimeError(f"No script at {txid}#{idx}")
    return utxo


# ═══════════════════════════════════════════════════════════════════════
# TX SUBMISSION
# ═══════════════════════════════════════════════════════════════════════

def submit_tx(tx_bytes):
    """Submit a signed TX to the testnet."""
    r = requests.post(TX_SUBMIT_URL,
                      data=tx_bytes,
                      headers={"Content-Type": "application/cbor"},
                      timeout=60)
    if r.status_code != 202:
        raise RuntimeError(f"Submit failed ({r.status_code}): {r.text[:500]}")
    return r.json()


def tx_to_bytes(tx):
    """Convert a signed TX to bytes for submission."""
    tx_cbor = tx.to_cbor()
    return tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)


def wait_confirm(secs=25):
    """Wait for TX confirmation."""
    time.sleep(secs)


# ═══════════════════════════════════════════════════════════════════════
# WALLET HELPERS
# ═══════════════════════════════════════════════════════════════════════

def ensure_collateral(context, skey, vkey, wallet_addr):
    """Ensure wallet has a pure ADA UTxO for collateral."""
    utxos = context.utxos(str(wallet_addr))
    has_pure_ada = any(
        not (hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset)
        for u in utxos
    )
    if has_pure_ada:
        return
    print("    [collateral] Splitting wallet for pure ADA collateral...")
    builder = TransactionBuilder(context)
    builder.fee_buffer = 300_000
    for u in utxos:
        builder.add_input(u)
    builder.add_output(TransactionOutput(wallet_addr, 5_000_000))
    builder.add_output(TransactionOutput(wallet_addr, 5_000_000))
    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx_to_bytes(tx))
    print(f"    [collateral] Split TX: {tx_hash}")
    wait_confirm(secs=25)


def get_wallet_utxos_no_collateral(context, wallet_addr):
    """Get wallet UTxOs excluding the smallest pure-ADA UTxO (reserved for collateral)."""
    utxos = context.utxos(str(wallet_addr))
    pure_ada = []
    with_tokens = []
    for u in utxos:
        if hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset:
            with_tokens.append(u)
        else:
            pure_ada.append(u)
    if not pure_ada:
        return utxos
    # Reserve smallest pure-ADA UTxO for collateral
    pure_ada.sort(key=lambda u: u.output.amount if isinstance(u.output.amount, int)
                  else u.output.amount.coin)
    return pure_ada[1:] + with_tokens


def evaluate_and_rebuild(builder, skey, vkey, wallet_addr, context):
    """Evaluate TX to get execution budgets, return (tx_bytes, budgets)."""
    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_bytes = tx_to_bytes(tx)
    tx_hex = tx_bytes.hex()

    eval_result = ogmios_rpc("evaluateTransaction", {"transaction": {"cbor": tx_hex}})

    budgets = {}
    if isinstance(eval_result, list):
        for item in eval_result:
            v = item.get("validator", {})
            key = f"{v.get('purpose', '?')}:{v.get('index', '?')}"
            budget = item.get("budget", {})
            budgets[key] = {"mem": budget.get("memory", 500000), "cpu": budget.get("cpu", 200000000)}
    elif isinstance(eval_result, dict):
        for key, val in eval_result.items():
            if isinstance(val, dict) and "memory" in val:
                budgets[key] = {"mem": val["memory"], "cpu": val["cpu"]}

    return tx_bytes, budgets


# ═══════════════════════════════════════════════════════════════════════
# TOKEN NAME DERIVATION (matches Aiken utils.ak)
# ═══════════════════════════════════════════════════════════════════════

def derive_token_name(prefix: bytes, seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    """Derive token name: prefix(4) + blake2b_256(cbor(OutputReference))[0:28]."""
    seed_ref_cbor = cbor2.dumps(cbor2.CBORTag(121, [seed_tx_hash, seed_tx_idx]))
    h = hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()
    return prefix + h[:28]


def claim_token_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    return derive_token_name(b"clm_", seed_tx_hash, seed_tx_idx)


def challenge_token_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    return derive_token_name(b"chl_", seed_tx_hash, seed_tx_idx)


def juror_token_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    return derive_token_name(b"jur_", seed_tx_hash, seed_tx_idx)


def agent_did_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    """Agent DID = blake2b_256(cbor(OutputReference))[0:28] — no prefix."""
    seed_ref_cbor = cbor2.dumps(cbor2.CBORTag(121, [seed_tx_hash, seed_tx_idx]))
    h = hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()
    return h[:28]


# ═══════════════════════════════════════════════════════════════════════
# PRNG (matches Aiken select_jurors_prng)
# ═══════════════════════════════════════════════════════════════════════

def select_jurors_prng(seed: bytes, eligible: list, n: int) -> list:
    """Deterministic jury selection matching Aiken's select_jurors_prng.

    Uses blake2b_256 hash chain with Fisher-Yates style removal.
    seed: challenge token name (bytes)
    eligible: sorted list of DID bytes
    n: jury_size
    """
    remaining = list(eligible)
    selected = []
    for i in range(n):
        if not remaining:
            break
        i_cbor = cbor2.dumps(i)
        index_hash = hashlib.blake2b(seed + i_cbor, digest_size=32).digest()
        raw_index = int.from_bytes(index_hash[:8], "big")
        index = raw_index % len(remaining)
        selected.append(remaining.pop(index))
    return selected


# ═══════════════════════════════════════════════════════════════════════
# SLOT / TIME CONVERSION
# ═══════════════════════════════════════════════════════════════════════

def slot_to_posix_ms(slot: int) -> int:
    """Convert slot number to POSIX milliseconds."""
    return (SYSTEM_START_UNIX + slot) * 1000


def posix_ms_to_slot(posix_ms: int) -> int:
    """Convert POSIX milliseconds to slot number."""
    return (posix_ms // 1000) - SYSTEM_START_UNIX
