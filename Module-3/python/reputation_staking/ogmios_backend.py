"""
Ogmios + PyCardano chain backend for Module 3.

Remote-capable implementation matching the patterns used by Module 1 and Module 6.
Uses a custom HTTP-based Ogmios context (the Vector testnet Ogmios endpoint is
HTTP-only, not WebSocket), with PyCardano TransactionBuilder for tx construction
and the Vector testnet HTTP submit endpoint for submission.

Usage:
    from reputation_staking.ogmios_backend import (
        OgmiosHttpContext, submit_tx, resolve_utxo, load_wallet,
    )

    context = OgmiosHttpContext()
    skey, vkey, addr = load_wallet("wallet/payment.skey")
    # Use with TransactionBuilder(context) ...
    submit_tx(signed_tx)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import cbor2
import requests
from pycardano import (
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawCBOR,
    Redeemer,
    ScriptHash,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.backend.base import GenesisParameters, ProtocolParameters
from pycardano.hash import TransactionId, VerificationKeyHash

from reputation_staking.constants import (
    OGMIOS_URL,
    SYSTEM_START_UNIX_S,
    TX_SUBMIT_URL,
    TX_WAIT_SECONDS,
)

logger = logging.getLogger(__name__)

NETWORK = Network.MAINNET  # Vector testnet uses mainnet network magic


# ── Ogmios JSON-RPC ───────────────────────────────────────────────────────


def ogmios_rpc(
    method: str, params: Optional[dict] = None, ogmios_url: str = OGMIOS_URL
) -> dict:
    """Send an Ogmios JSON-RPC request via HTTP POST."""
    payload: dict = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    resp = requests.post(ogmios_url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Ogmios RPC error ({method}): {data['error']}")
    return data.get("result", {})


def get_current_slot(ogmios_url: str = OGMIOS_URL) -> int:
    """Get the current tip slot from Ogmios."""
    result = ogmios_rpc("queryNetwork/tip", ogmios_url=ogmios_url)
    return result["slot"]


# ── Custom HTTP Chain Context ──────────────────────────────────────────────


class OgmiosHttpContext:
    """PyCardano-compatible chain context using Ogmios HTTP JSON-RPC.

    The Vector testnet Ogmios endpoint only supports HTTP (not WebSocket),
    so we can't use PyCardano's built-in OgmiosChainContext. This class
    implements the subset of the ChainContext interface that TransactionBuilder
    needs: protocol_param, utxos(), network, and genesis_param.

    Follows Module 1's pattern (simulation/chain.py).
    """

    def __init__(self, ogmios_url: str = OGMIOS_URL):
        self._ogmios_url = ogmios_url
        self._protocol_param: Optional[ProtocolParameters] = None
        self._genesis_param: Optional[GenesisParameters] = None

    @property
    def network(self) -> Network:
        return NETWORK

    @property
    def protocol_param(self) -> ProtocolParameters:
        if self._protocol_param is None:
            self._protocol_param = self._query_protocol_params()
        return self._protocol_param

    @property
    def genesis_param(self) -> GenesisParameters:
        if self._genesis_param is None:
            self._genesis_param = GenesisParameters(
                active_slots_coefficient=0.05,
                update_quorum=5,
                max_lovelace_supply=45_000_000_000_000_000,
                network_magic=764824073,
                epoch_length=432000,
                system_start=SYSTEM_START_UNIX_S,
                slots_per_kes_period=129600,
                slot_length=1,
                max_kes_evolutions=62,
                security_param=2160,
            )
        return self._genesis_param

    @property
    def last_block_slot(self) -> int:
        return get_current_slot(self._ogmios_url)

    def evaluate_tx_cbor(self, cbor) -> Dict[str, "ExecutionUnits"]:
        """Evaluate execution units for a transaction via Ogmios.

        Required by PyCardano's TransactionBuilder for automatic script
        execution budget estimation.
        """
        if isinstance(cbor, bytes):
            cbor_hex = cbor.hex()
        else:
            cbor_hex = str(cbor)

        raw_budgets = evaluate_tx(cbor_hex, self._ogmios_url)

        # Convert to PyCardano ExecutionUnits keyed by "purpose:index"
        result = {}
        for key, budget in raw_budgets.items():
            result[key] = ExecutionUnits(
                mem=budget["mem"], steps=budget["cpu"]
            )
        return result

    def evaluate_tx(self, tx) -> Dict[str, "ExecutionUnits"]:
        """Evaluate execution units for a Transaction object."""
        raw = tx.to_cbor()
        return self.evaluate_tx_cbor(raw)

    def utxos(self, address: str) -> List[UTxO]:
        """Query all UTxOs at an address."""
        addr_str = str(address)
        result = ogmios_rpc(
            "queryLedgerState/utxo",
            params={"addresses": [addr_str]},
            ogmios_url=self._ogmios_url,
        )
        return [_parse_ogmios_utxo(item) for item in result]

    def _query_protocol_params(self) -> ProtocolParameters:
        """Fetch and parse protocol parameters from Ogmios."""
        result = ogmios_rpc(
            "queryLedgerState/protocolParameters",
            ogmios_url=self._ogmios_url,
        )
        # Map Ogmios v6 field names to PyCardano ProtocolParameters
        return ProtocolParameters(
            min_fee_constant=result.get("minFeeConstant", {}).get("ada", {}).get("lovelace", 155381),
            min_fee_coefficient=result.get("minFeeCoefficient", 44),
            max_block_size=result.get("maxBlockBodySize", {}).get("bytes", 90112),
            max_tx_size=result.get("maxTransactionSize", {}).get("bytes", 16384),
            max_block_header_size=result.get("maxBlockHeaderSize", {}).get("bytes", 1100),
            key_deposit=result.get("stakeCredentialDeposit", {}).get("ada", {}).get("lovelace", 2_000_000),
            pool_deposit=result.get("stakePoolDeposit", {}).get("ada", {}).get("lovelace", 500_000_000),
            pool_influence=self._parse_fraction(result.get("stakePoolPledgeInfluence", "3/10")),
            monetary_expansion=self._parse_fraction(result.get("monetaryExpansion", "3/1000")),
            treasury_expansion=self._parse_fraction(result.get("treasuryExpansion", "2/10")),
            decentralization_param=0,
            extra_entropy="",
            protocol_major_version=result.get("version", {}).get("major", 10),
            protocol_minor_version=result.get("version", {}).get("minor", 0),
            min_utxo=result.get("minUtxoDepositConstant", {}).get("ada", {}).get("lovelace", 1_000_000),
            min_pool_cost=result.get("minStakePoolCost", {}).get("ada", {}).get("lovelace", 170_000_000),
            price_mem=self._parse_fraction(result.get("scriptExecutionPrices", {}).get("memory", "577/10000")),
            price_step=self._parse_fraction(result.get("scriptExecutionPrices", {}).get("cpu", "721/10000000")),
            max_tx_ex_mem=result.get("maxExecutionUnitsPerTransaction", {}).get("memory", 14_000_000),
            max_tx_ex_steps=result.get("maxExecutionUnitsPerTransaction", {}).get("cpu", 10_000_000_000),
            max_block_ex_mem=result.get("maxExecutionUnitsPerBlock", {}).get("memory", 62_000_000),
            max_block_ex_steps=result.get("maxExecutionUnitsPerBlock", {}).get("cpu", 20_000_000_000),
            max_val_size=result.get("maxValueSize", {}).get("bytes", 5000),
            collateral_percent=result.get("collateralPercentage", 150),
            max_collateral_inputs=result.get("maxCollateralInputs", 3),
            coins_per_utxo_word=0,  # Deprecated, but required by PyCardano
            coins_per_utxo_byte=result.get("minUtxoDepositCoefficient", 4310),
            cost_models=self._parse_cost_models(result),
            maximum_reference_scripts_size=None,
            min_fee_reference_scripts=None,
        )

    @staticmethod
    def _parse_cost_models(result: dict) -> dict:
        """Parse Ogmios plutusCostModels into PyCardano format.

        PyCardano's TransactionBuilder looks up cost models via string keys
        like "PlutusV1", "PlutusV2", "PlutusV3" (see script_data_hash property).
        Each value is a dict mapping string indices to integer cost values.
        """
        ogmios_cms = result.get("plutusCostModels", {})
        cost_models = {}

        lang_map = {
            "plutus:v1": "PlutusV1",
            "plutus:v2": "PlutusV2",
            "plutus:v3": "PlutusV3",
        }
        for ogmios_key, pycardano_key in lang_map.items():
            values = ogmios_cms.get(ogmios_key)
            if values and isinstance(values, list):
                cost_models[pycardano_key] = {
                    str(i): v for i, v in enumerate(values)
                }

        return cost_models

    @staticmethod
    def _parse_fraction(val) -> float:
        """Parse an Ogmios fraction string like '577/10000' to float."""
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str) and "/" in val:
            num, den = val.split("/")
            return int(num) / int(den)
        return 0.0


# ── UTxO Parsing ───────────────────────────────────────────────────────────


def _parse_ogmios_value(val: dict) -> Value:
    """Parse an Ogmios value object into a PyCardano Value."""
    lovelace = val.get("ada", {}).get("lovelace", 0)
    multi = MultiAsset()
    for policy_hex, assets in val.items():
        if policy_hex == "ada":
            continue
        policy = ScriptHash(bytes.fromhex(policy_hex))
        asset_map = Asset()
        for asset_name_hex, quantity in assets.items():
            asset_map[AssetName(bytes.fromhex(asset_name_hex))] = quantity
        multi[policy] = asset_map
    if len(multi) > 0:
        return Value(lovelace, multi)
    return Value(lovelace)


def _parse_ogmios_utxo(item: dict) -> UTxO:
    """Parse a single Ogmios UTxO result into a PyCardano UTxO."""
    tx_id = TransactionId(bytes.fromhex(item["transaction"]["id"]))
    tx_input = TransactionInput(tx_id, item["index"])

    address = Address.from_primitive(item["address"])
    value = _parse_ogmios_value(item["value"])

    # Inline datum — Ogmios returns CBOR hex
    datum = None
    if "datum" in item and item["datum"]:
        datum_hex = item["datum"]
        if isinstance(datum_hex, str):
            datum = RawCBOR(bytes.fromhex(datum_hex))

    # Reference script — Ogmios v6 format: {"language": "plutus:v3", "cbor": "hex"}
    script = None
    if "script" in item and item["script"]:
        script_info = item["script"]
        if isinstance(script_info, dict):
            if script_info.get("language") == "plutus:v3" and "cbor" in script_info:
                script = PlutusV3Script(bytes.fromhex(script_info["cbor"]))
            elif "plutus:v3" in script_info:
                # Alternative format: {"plutus:v3": "hex"}
                script = PlutusV3Script(bytes.fromhex(script_info["plutus:v3"]))

    output = TransactionOutput(address, value, datum=datum, script=script)
    return UTxO(tx_input, output)


# ── UTxO Resolution ────────────────────────────────────────────────────────


def resolve_utxo(
    tx_hash_hex: str, tx_idx: int, ogmios_url: str = OGMIOS_URL
) -> UTxO:
    """Resolve a specific UTxO by transaction hash and index via Ogmios."""
    result = ogmios_rpc(
        "queryLedgerState/utxo",
        params={
            "outputReferences": [
                {"transaction": {"id": tx_hash_hex}, "index": tx_idx}
            ]
        },
        ogmios_url=ogmios_url,
    )
    if not result:
        raise RuntimeError(f"UTxO {tx_hash_hex}#{tx_idx} not found on chain")
    return _parse_ogmios_utxo(result[0])


def resolve_utxos_at_address(
    address: str, ogmios_url: str = OGMIOS_URL
) -> List[UTxO]:
    """Query all UTxOs at an address via Ogmios HTTP."""
    result = ogmios_rpc(
        "queryLedgerState/utxo",
        params={"addresses": [address]},
        ogmios_url=ogmios_url,
    )
    return [_parse_ogmios_utxo(item) for item in result]


def find_utxo_with_token(
    address: str,
    policy_id_hex: str,
    token_name_hex: str,
    ogmios_url: str = OGMIOS_URL,
) -> Optional[UTxO]:
    """Find a UTxO at address containing a specific native token."""
    policy = ScriptHash(bytes.fromhex(policy_id_hex))
    token_an = AssetName(bytes.fromhex(token_name_hex))
    utxos = resolve_utxos_at_address(address, ogmios_url)
    for u in utxos:
        if u.output.amount.multi_asset:
            assets = u.output.amount.multi_asset.get(policy)
            if assets and token_an in assets:
                return u
    return None


# ── Transaction Submission ─────────────────────────────────────────────────


def tx_to_bytes(tx) -> bytes:
    """Convert a signed PyCardano transaction to raw CBOR bytes."""
    raw = tx.to_cbor()
    if isinstance(raw, str):
        return bytes.fromhex(raw)
    return raw


def submit_tx(tx, submit_url: str = TX_SUBMIT_URL) -> str:
    """Submit a signed transaction via HTTP POST.

    Args:
        tx: A signed PyCardano Transaction object.
        submit_url: The HTTP submit endpoint URL.

    Returns:
        Transaction hash hex string.
    """
    tx_bytes = tx_to_bytes(tx)
    resp = requests.post(
        submit_url,
        data=tx_bytes,
        headers={"Content-Type": "application/cbor"},
        timeout=30,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"Transaction submission failed ({resp.status_code}): {resp.text}"
        )
    tx_hash = str(tx.id)
    logger.info("Submitted tx: %s", tx_hash)
    return tx_hash


# ── Execution Budget Evaluation ────────────────────────────────────────────


def evaluate_tx(tx_cbor_hex: str, ogmios_url: str = OGMIOS_URL) -> Dict[str, dict]:
    """Evaluate execution budgets via Ogmios.

    Returns:
        Dict mapping "purpose:index" (e.g. "mint:0", "spend:2") to
        {"mem": int, "cpu": int}.
    """
    result = ogmios_rpc(
        "evaluateTransaction",
        params={"transaction": {"cbor": tx_cbor_hex}},
        ogmios_url=ogmios_url,
    )
    budgets = {}
    if isinstance(result, list):
        for entry in result:
            validator = entry.get("validator", {})
            purpose = validator.get("purpose", "unknown")
            index = validator.get("index", 0)
            budget = entry.get("budget", {})
            key = f"{purpose}:{index}"
            budgets[key] = {
                "mem": budget.get("memory", 0),
                "cpu": budget.get("cpu", 0),
            }
    elif isinstance(result, dict):
        for key, entry in result.items():
            budget = entry.get("budget", entry)
            budgets[key] = {
                "mem": budget.get("memory", 0),
                "cpu": budget.get("cpu", 0),
            }
    return budgets


# ── Wallet Utilities ───────────────────────────────────────────────────────


def load_wallet(
    skey_path: str,
) -> Tuple[PaymentSigningKey, PaymentVerificationKey, Address]:
    """Load a wallet from a cardano-cli format .skey file.

    Returns:
        (signing_key, verification_key, address)
    """
    skey = PaymentSigningKey.load(skey_path)
    vkey = PaymentVerificationKey.from_signing_key(skey)
    addr = Address(payment_part=vkey.hash(), network=NETWORK)
    return skey, vkey, addr


def get_wallet_utxos(
    context: OgmiosHttpContext,
    wallet_addr: Address,
    exclude: Optional[Set[str]] = None,
) -> List[UTxO]:
    """Get spendable wallet UTxOs, optionally excluding specific ones."""
    all_utxos = context.utxos(wallet_addr)
    if not exclude:
        return list(all_utxos)
    return [
        u
        for u in all_utxos
        if f"{bytes(u.input.transaction_id).hex()}#{u.input.index}" not in exclude
    ]


def get_collateral_utxo(
    context: OgmiosHttpContext,
    wallet_addr: Address,
    exclude: Optional[Set[str]] = None,
) -> UTxO:
    """Find a suitable collateral UTxO (>= 5 ADA).

    Prefers pure-ADA UTxOs but falls back to UTxOs with native tokens
    (allowed in Conway era).
    """
    utxos = context.utxos(wallet_addr)
    pure_ada = []
    with_tokens = []
    for u in utxos:
        ref = f"{bytes(u.input.transaction_id).hex()}#{u.input.index}"
        if exclude and ref in exclude:
            continue
        lovelace = u.output.amount.coin
        if lovelace < 5_000_000:
            continue
        if not u.output.amount.multi_asset or len(u.output.amount.multi_asset) == 0:
            pure_ada.append(u)
        else:
            with_tokens.append(u)

    # Prefer pure-ADA, fall back to any UTxO with enough ADA
    candidates = pure_ada if pure_ada else with_tokens
    if not candidates:
        raise RuntimeError(
            "No suitable collateral UTxO found (need UTxO >= 5 ADA)"
        )
    candidates.sort(key=lambda u: u.output.amount.coin)
    return candidates[0]


def wait_for_tx(seconds: int = TX_WAIT_SECONDS):
    """Wait for transaction confirmation."""
    logger.info("Waiting %ds for confirmation...", seconds)
    time.sleep(seconds)
