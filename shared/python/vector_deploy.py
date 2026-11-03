"""Deployment runner of the module deploy tools.

The deploy tools of Module-1 (``scripts/identity_deploy.py``), Module-3
(``scripts/deploy_mainnet_ogmios.py``) and Module-6 (``scripts/deploy.py``,
``deploy_params.py`` and ``treasury_fund.py``) describe their transactions
as steps. This module builds and runs the steps:

- Dry run (the default): build each transaction that the chain does not show
  yet, evaluate it with Ogmios ``evaluateTransaction``, check its fee, its
  size and the min-UTxO of each output, and print the costs. Nothing is
  signed or sent. A later step spends the outputs of an earlier step as
  virtual UTxOs; they go to Ogmios as ``additionalUtxo``. ``dry_run_funds``
  adds one virtual wallet UTxO when the operator address has no funds yet.
- Submit: build and evaluate all remaining transactions in the same way,
  print the fee and min-UTxO totals, sign each transaction with the operator
  key, send them in order and wait until the chain shows each one before the
  next. Each sent transaction is recorded in the state file.
- Offline signing: ``build_unsigned`` writes the remaining transactions
  unsigned (cardano-cli TextEnvelope files, chained on the outputs of the
  earlier ones), ``sign_files`` adds the operator witness without a network,
  and ``submit_signed`` sends the signed files in order.

A run can resume. A step whose result the chain already shows is skipped and
recorded. Protocol parameters, including the Plutus cost models, come from
Ogmios. The runner takes inputs only from the operator address: lovelace-only
outputs without datum or script that the caller does not protect.

Vector mainnet and Vector testnet both use the address network id 1
(``addr1...``). The runner checks the network of an Ogmios endpoint with its
start time.

Key files are read by path. The runner never prints or writes key material.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import cbor2
import requests
from pycardano import (
    Address,
    Asset,
    AssetName,
    ChainContext,
    ExecutionUnits,
    MultiAsset,
    NativeScript,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV1Script,
    PlutusV2Script,
    PlutusV3Script,
    ProtocolParameters,
    ScriptHash,
    ScriptPubkey,
    Transaction,
    TransactionBuilder,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
)
from pycardano.serialization import RawCBOR
from pycardano.utils import fee as ledger_fee

from aiken_simulate import _array_head, _head, _items, _map_head, _skip

LOVELACE_PER_AP3X = 1_000_000

#: Vector mainnet and testnet both use network id 1 in addresses.
ADDRESS_NETWORK = Network.MAINNET

#: Format of the state files that this runner writes.
STATE_FORMAT = "vector-module-deploy/1"

#: Validity of a built transaction (slots of 1 s). It stays below the Ogmios
#: forecast horizon (the safe zone of 25,920 slots), so that a transaction
#: with a script can be evaluated.
DEFAULT_TTL_SLOTS = 6 * 3600

#: Time to wait until the chain shows a sent transaction.
CONFIRM_TIMEOUT_S = 300
CONFIRM_POLL_S = 5

#: The smallest lovelace-only wallet output that serves as collateral.
MIN_COLLATERAL = 5_000_000

#: AP3X that the input selection adds above the outputs and the deposits, to
#: pay the fee and keep a change output.
SELECTION_MARGIN = 5_000_000


class DeployError(RuntimeError):
    """A deploy run cannot continue. The message says why and what to do."""


# ── Networks ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class NetworkPreset:
    name: str
    ogmios_url: str
    submit_url: str
    koios_url: str
    #: ``queryNetwork/startTime`` of the network.
    start_time: str


NETWORKS = {
    "mainnet": NetworkPreset(
        "mainnet",
        "https://ogmios.vector.mainnet.apexfusion.org",
        "https://submit.vector.mainnet.apexfusion.org/api/submit/tx",
        "https://v2.koios.vector.mainnet.apexfusion.org/api/v1",
        "2025-08-29T16:40:00Z",
    ),
    "testnet": NetworkPreset(
        "testnet",
        "https://ogmios.vector.testnet.apexfusion.org",
        "https://submit.vector.testnet.apexfusion.org/api/submit/tx",
        "https://v2.koios.vector.testnet.apexfusion.org/api/v1",
        "2025-07-09T10:38:04Z",
    ),
}

ENV_OGMIOS_URL = "VECTOR_OGMIOS_URL"
ENV_SUBMIT_URL = "VECTOR_SUBMIT_URL"
ENV_KOIOS_URL = "VECTOR_KOIOS_URL"


def network_preset(
    name: str,
    *,
    ogmios_url: Optional[str] = None,
    submit_url: Optional[str] = None,
    koios_url: Optional[str] = None,
    environ: Mapping[str, str] = os.environ,
) -> NetworkPreset:
    """The endpoints of a network. A flag value overrides the environment
    (VECTOR_OGMIOS_URL, VECTOR_SUBMIT_URL, VECTOR_KOIOS_URL), and the
    environment overrides the preset."""
    if name not in NETWORKS:
        raise DeployError(f"unknown network {name!r}; use one of {', '.join(NETWORKS)}")
    base = NETWORKS[name]
    return dataclasses.replace(
        base,
        ogmios_url=ogmios_url or environ.get(ENV_OGMIOS_URL) or base.ogmios_url,
        submit_url=submit_url or environ.get(ENV_SUBMIT_URL) or base.submit_url,
        koios_url=koios_url or environ.get(ENV_KOIOS_URL) or base.koios_url,
    )


def add_network_arguments(parser) -> None:
    """The network options of every deploy tool."""
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--ogmios-url", help=f"default: ${ENV_OGMIOS_URL}, else the preset of --network")
    parser.add_argument("--submit-url", help=f"default: ${ENV_SUBMIT_URL}, else the preset of --network")


def network_from_args(args) -> NetworkPreset:
    return network_preset(args.network, ogmios_url=args.ogmios_url, submit_url=args.submit_url)


# ── Ogmios ───────────────────────────────────────────────────────────────────


def _lovelace(value: Mapping[str, Any]) -> int:
    return int(value["ada"]["lovelace"])


def _fraction(value: Any) -> Fraction:
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/")
        return Fraction(int(numerator), int(denominator))
    return Fraction(value)


_PURPOSES = {
    "spend": "spend",
    "mint": "mint",
    "publish": "certificate",
    "withdraw": "withdrawal",
    "vote": "voting",
    "propose": "proposing",
}


class Ogmios:
    """Ogmios v6 JSON-RPC over HTTP (the Vector endpoints serve HTTP only)."""

    def __init__(self, url: str, *, timeout: int = 60, post: Callable = requests.post):
        self.url = url
        self.timeout = timeout
        self._post = post

    def rpc(self, method: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": None}
        if params is not None:
            body["params"] = params
        try:
            response = self._post(self.url, json=body, timeout=self.timeout)
        except requests.RequestException as error:
            raise DeployError(f"Ogmios {method}: {error}") from error
        try:
            data = response.json()
        except ValueError:
            raise DeployError(f"Ogmios {method}: HTTP {response.status_code}: {response.text[:500]}") from None
        if "error" in data:
            raise DeployError(f"Ogmios {method}: {json.dumps(data['error'])[:4000]}")
        return data.get("result")

    def protocol_parameters(self) -> dict:
        return self.rpc("queryLedgerState/protocolParameters")

    def start_time(self) -> str:
        return self.rpc("queryNetwork/startTime")

    def tip_slot(self) -> int:
        return int(self.rpc("queryLedgerState/tip")["slot"])

    def utxos(self, *, addresses: Sequence[str] = (), refs: Sequence[TransactionInput] = ()) -> list[UTxO]:
        params: dict[str, Any] = {}
        if addresses:
            params["addresses"] = [str(a) for a in addresses]
        if refs:
            params["outputReferences"] = [
                {"transaction": {"id": bytes(r.transaction_id).hex()}, "index": r.index} for r in refs
            ]
        if not params:
            return []
        return [utxo_from_ogmios(item) for item in self.rpc("queryLedgerState/utxo", params)]

    def evaluate(self, tx_cbor: bytes, additional: Sequence[UTxO] = ()) -> dict[str, ExecutionUnits]:
        """The execution units of each redeemer, keyed as pycardano keys them
        ("spend:0", "mint:0", "certificate:0", "withdrawal:0")."""
        params: dict[str, Any] = {"transaction": {"cbor": tx_cbor.hex()}}
        if additional:
            params["additionalUtxo"] = [utxo_to_ogmios(u) for u in additional]
        result = self.rpc("evaluateTransaction", params) or []
        units = {}
        for entry in result:
            purpose = _PURPOSES.get(entry["validator"]["purpose"], entry["validator"]["purpose"])
            units[f"{purpose}:{entry['validator']['index']}"] = ExecutionUnits(
                mem=int(entry["budget"]["memory"]), steps=int(entry["budget"]["cpu"]),
            )
        return units


class Koios:
    """Koios reads (read-only). Ogmios rewardAccountSummaries lists only
    delegated accounts, so a registered credential that can never be
    delegated (a script that fails every publish purpose) shows only here."""

    def __init__(self, url: str, *, timeout: int = 60, post: Callable = requests.post):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._post = post

    def account_info(self, stake_addresses: Sequence[str]) -> list[dict]:
        try:
            response = self._post(f"{self.url}/account_info", json={"_stake_addresses": list(stake_addresses)},
                                  timeout=self.timeout)
            data = response.json()
        except (requests.RequestException, ValueError) as error:
            raise DeployError(f"Koios account_info: {error}") from error
        if not isinstance(data, list):
            raise DeployError(f"Koios account_info: {json.dumps(data)[:500]}")
        return data

    def registered(self, stake_address: str) -> bool:
        return any(a.get("stake_address") == stake_address and a.get("status") == "registered"
                   for a in self.account_info([stake_address]))


def stake_address(credential: bytes, *, script: bool) -> str:
    """The reward address of a stake credential (network id 1)."""
    part = ScriptHash(bytes(credential)) if script else VerificationKeyHash(bytes(credential))
    return str(Address(staking_part=part, network=ADDRESS_NETWORK))


def submit_tx(submit_url: str, tx_cbor: bytes, *, post: Callable = requests.post) -> str:
    """Send a signed transaction to the cardano-submit-api endpoint. Returns
    the transaction id."""
    try:
        response = post(submit_url, data=tx_cbor, headers={"Content-Type": "application/cbor"}, timeout=60)
    except requests.RequestException as error:
        raise DeployError(f"submit: {error}") from error
    if response.status_code not in (200, 202):
        raise DeployError(f"submit: HTTP {response.status_code}: {response.text[:4000]}")
    return transaction_id(tx_cbor)


def protocol_parameters(raw: Mapping[str, Any]) -> ProtocolParameters:
    """pycardano protocol parameters from the Ogmios v6 result. There are no
    defaults: a missing value is an error."""
    try:
        cost_models = {}
        for language, name in (("plutus:v1", "PlutusV1"), ("plutus:v2", "PlutusV2"), ("plutus:v3", "PlutusV3")):
            values = raw.get("plutusCostModels", {}).get(language)
            if values:
                cost_models[name] = {index: int(v) for index, v in enumerate(values)}
        if "PlutusV3" not in cost_models:
            raise DeployError("the Ogmios protocol parameters have no Plutus V3 cost model")
        reference = raw["minFeeReferenceScripts"]
        return ProtocolParameters(
            min_fee_constant=_lovelace(raw["minFeeConstant"]),
            min_fee_coefficient=int(raw["minFeeCoefficient"]),
            max_block_size=int(raw["maxBlockBodySize"]["bytes"]),
            max_tx_size=int(raw["maxTransactionSize"]["bytes"]),
            max_block_header_size=int(raw["maxBlockHeaderSize"]["bytes"]),
            key_deposit=_lovelace(raw["stakeCredentialDeposit"]),
            pool_deposit=_lovelace(raw["stakePoolDeposit"]),
            pool_influence=_fraction(raw["stakePoolPledgeInfluence"]),
            monetary_expansion=_fraction(raw["monetaryExpansion"]),
            treasury_expansion=_fraction(raw["treasuryExpansion"]),
            decentralization_param=Fraction(0),
            extra_entropy="",
            protocol_major_version=int(raw["version"]["major"]),
            protocol_minor_version=int(raw["version"]["minor"]),
            min_utxo=_lovelace(raw["minUtxoDepositConstant"]),
            min_pool_cost=_lovelace(raw["minStakePoolCost"]),
            price_mem=_fraction(raw["scriptExecutionPrices"]["memory"]),
            price_step=_fraction(raw["scriptExecutionPrices"]["cpu"]),
            max_tx_ex_mem=int(raw["maxExecutionUnitsPerTransaction"]["memory"]),
            max_tx_ex_steps=int(raw["maxExecutionUnitsPerTransaction"]["cpu"]),
            max_block_ex_mem=int(raw["maxExecutionUnitsPerBlock"]["memory"]),
            max_block_ex_steps=int(raw["maxExecutionUnitsPerBlock"]["cpu"]),
            max_val_size=int(raw["maxValueSize"]["bytes"]),
            collateral_percent=int(raw["collateralPercentage"]),
            max_collateral_inputs=int(raw["maxCollateralInputs"]),
            coins_per_utxo_word=0,
            coins_per_utxo_byte=int(raw["minUtxoDepositCoefficient"]),
            cost_models=cost_models,
            maximum_reference_scripts_size={"bytes": int(raw["maxReferenceScriptsSize"]["bytes"])},
            min_fee_reference_scripts={
                "base": float(reference["base"]),
                "range": int(reference["range"]),
                "multiplier": float(reference["multiplier"]),
            },
        )
    except KeyError as missing:
        raise DeployError(f"the Ogmios protocol parameters have no {missing}") from None


def _value_from_ogmios(value: Mapping[str, Any]) -> Value:
    assets = MultiAsset()
    for policy, names in value.items():
        if policy == "ada":
            continue
        assets[ScriptHash(bytes.fromhex(policy))] = Asset(
            {AssetName(bytes.fromhex(name)): int(quantity) for name, quantity in names.items()}
        )
    return Value(_lovelace(value), assets)


def _script_from_ogmios(script: Mapping[str, Any]):
    language = script.get("language")
    code = bytes.fromhex(script["cbor"])
    if language == "plutus:v3":
        return PlutusV3Script(code)
    if language == "plutus:v2":
        return PlutusV2Script(code)
    if language == "plutus:v1":
        return PlutusV1Script(code)
    if language == "native":
        return NativeScript.from_cbor(code)
    raise DeployError(f"unknown script language {language!r} in an Ogmios UTxO")


def utxo_from_ogmios(item: Mapping[str, Any]) -> UTxO:
    ref = TransactionInput(TransactionId(bytes.fromhex(item["transaction"]["id"])), int(item["index"]))
    datum = RawCBOR(bytes.fromhex(item["datum"])) if item.get("datum") else None
    datum_hash = bytes.fromhex(item["datumHash"]) if item.get("datumHash") else None
    script = _script_from_ogmios(item["script"]) if item.get("script") else None
    output = TransactionOutput(
        Address.from_primitive(item["address"]),
        _value_from_ogmios(item["value"]),
        datum_hash=datum_hash,
        datum=datum,
        script=script,
    )
    return UTxO(ref, output)


def _datum_cbor(datum: Any) -> bytes:
    if isinstance(datum, RawCBOR):
        return bytes(datum.cbor)
    data = datum.to_cbor() if hasattr(datum, "to_cbor") else cbor2.dumps(datum)
    return data if isinstance(data, bytes) else bytes.fromhex(data)


def utxo_to_ogmios(utxo: UTxO) -> dict:
    """An Ogmios v6 UTxO object (for ``additionalUtxo``)."""
    output = utxo.output
    amount = output.amount if isinstance(output.amount, Value) else Value(output.amount)
    value: dict[str, Any] = {"ada": {"lovelace": amount.coin}}
    for policy, names in (amount.multi_asset or {}).items():
        value[bytes(policy.payload).hex()] = {
            bytes(name.payload).hex(): int(quantity) for name, quantity in names.items()
        }
    item: dict[str, Any] = {
        "transaction": {"id": bytes(utxo.input.transaction_id).hex()},
        "index": utxo.input.index,
        "address": str(output.address),
        "value": value,
    }
    if output.datum is not None:
        item["datum"] = _datum_cbor(output.datum).hex()
    elif output.datum_hash is not None:
        item["datumHash"] = bytes(output.datum_hash.payload).hex()
    if output.script is not None:
        script = output.script
        if isinstance(script, NativeScript):
            item["script"] = {"language": "native", "cbor": script.to_cbor_hex()}
        else:
            item["script"] = {"language": f"plutus:v{script.version}", "cbor": bytes(script).hex()}
    return item


def ref_key(ref: TransactionInput) -> str:
    return f"{bytes(ref.transaction_id).hex()}#{ref.index}"


def parse_ref(text: str) -> TransactionInput:
    tx_id, sep, index = str(text).partition("#")
    try:
        raw = bytes.fromhex(tx_id)
        ok = sep == "#" and len(raw) == 32 and index.isdigit()
    except ValueError:
        ok = False
    if not ok:
        raise DeployError(f"{text!r} is not an output reference \"<64 hex>#<index>\"")
    return TransactionInput(TransactionId(raw), int(index))


# ── Chain context ────────────────────────────────────────────────────────────


class DeployChain(ChainContext):
    """A pycardano chain context on Ogmios with a virtual view.

    The view holds the outputs of transactions that a run built but did not
    send (``apply``) and hides the outputs that they spend. Evaluation sends
    the virtual outputs that a transaction reads as ``additionalUtxo``.
    """

    def __init__(self, ogmios: Ogmios, network: NetworkPreset, koios: Optional[Koios] = None):
        self.ogmios = ogmios
        self.preset = network
        self.koios = koios if koios is not None else Koios(network.koios_url)
        self._protocol: Optional[ProtocolParameters] = None
        self._raw_protocol: Optional[dict] = None
        self._tip: Optional[int] = None
        self.virtual: dict[str, UTxO] = {}
        self.spent: set[str] = set()

    # pycardano ChainContext interface

    @property
    def protocol_param(self) -> ProtocolParameters:
        if self._protocol is None:
            self._raw_protocol = self.ogmios.protocol_parameters()
            self._protocol = protocol_parameters(self._raw_protocol)
        return self._protocol

    @property
    def network(self) -> Network:
        return ADDRESS_NETWORK

    @property
    def last_block_slot(self) -> int:
        if self._tip is None:
            self._tip = self.ogmios.tip_slot()
        return self._tip

    def utxos(self, address) -> list[UTxO]:
        return self.view(addresses=[str(address)])

    def evaluate_tx_cbor(self, cbor) -> dict[str, ExecutionUnits]:
        data = cbor if isinstance(cbor, bytes) else bytes.fromhex(cbor)
        return self.ogmios.evaluate(data, self.additional_for(data))

    # Checks and views

    def check_network(self) -> None:
        """Refuse an Ogmios endpoint of another network."""
        start = self.ogmios.start_time()
        if start != self.preset.start_time:
            raise DeployError(
                f"{self.ogmios.url} serves the network that started at {start}, but "
                f"--network {self.preset.name} started at {self.preset.start_time}"
            )

    def refresh(self) -> None:
        self._tip = None

    def view(self, *, addresses: Sequence[str] = (), refs: Sequence[TransactionInput] = ()) -> list[UTxO]:
        """The live UTxOs at the addresses (or with the references) without the
        spent ones, and the virtual ones."""
        live = self.ogmios.utxos(addresses=addresses, refs=[r for r in refs if ref_key(r) not in self.virtual])
        found = [u for u in live if ref_key(u.input) not in self.spent]
        wanted = {str(a) for a in addresses}
        for key, utxo in self.virtual.items():
            if key in self.spent:
                continue
            if str(utxo.output.address) in wanted or any(ref_key(r) == key for r in refs):
                found.append(utxo)
        return found

    def apply(self, tx_cbor: bytes) -> None:
        """Add the outputs of a built transaction to the view and hide its
        inputs."""
        tx = Transaction.from_cbor(tx_cbor)
        tx_id = transaction_id(tx_cbor)
        for ref in tx.transaction_body.inputs:
            self.spent.add(ref_key(ref))
        for index, output in enumerate(tx.transaction_body.outputs):
            ref = TransactionInput(TransactionId(bytes.fromhex(tx_id)), index)
            self.virtual[ref_key(ref)] = UTxO(ref, output)

    def additional_for(self, tx_cbor: bytes) -> list[UTxO]:
        """The virtual UTxOs that a transaction spends or reads."""
        body = Transaction.from_cbor(tx_cbor).transaction_body
        refs = list(body.inputs) + list(body.reference_inputs or []) + list(body.collateral or [])
        return [self.virtual[ref_key(r)] for r in refs if ref_key(r) in self.virtual]

    def is_virtual(self, ref: TransactionInput) -> bool:
        return ref_key(ref) in self.virtual


# ── Keys and addresses ───────────────────────────────────────────────────────


def _read_key_file(path: Path) -> Any:
    try:
        text = Path(path).read_text().strip()
    except OSError as error:
        raise DeployError(f"cannot read the key file {path}: {error.strerror}") from None
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            raise DeployError(f"{path} is not a cardano-cli key file (JSON)") from None
    return text


def load_signing_key(path) -> PaymentSigningKey:
    """An Ed25519 payment signing key from a cardano-cli TextEnvelope file
    (PaymentSigningKeyShelley_ed25519) or a file with 64 hex characters."""
    content = _read_key_file(path)
    if isinstance(content, dict):
        if content.get("type") != "PaymentSigningKeyShelley_ed25519":
            raise DeployError(f"{path}: expected a PaymentSigningKeyShelley_ed25519 key, got {content.get('type')!r}")
        raw = bytes.fromhex(content.get("cborHex", ""))
        if raw[:2] != b"\x58\x20" or len(raw) != 34:
            raise DeployError(f"{path}: the key is not 32 bytes")
        return PaymentSigningKey(raw[2:])
    try:
        raw = bytes.fromhex(content)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise DeployError(f"{path}: expected a TextEnvelope key file or 64 hex characters")
    return PaymentSigningKey(raw)


def load_verification_key(path) -> PaymentVerificationKey:
    """A payment verification key from a vkey TextEnvelope file, or the key of
    a signing key file."""
    content = _read_key_file(path)
    if isinstance(content, dict) and content.get("type") == "PaymentVerificationKeyShelley_ed25519":
        raw = bytes.fromhex(content.get("cborHex", ""))
        if raw[:2] != b"\x58\x20" or len(raw) != 34:
            raise DeployError(f"{path}: the key is not 32 bytes")
        return PaymentVerificationKey(raw[2:])
    return PaymentVerificationKey.from_signing_key(load_signing_key(path))


def key_address(key_hash: bytes) -> Address:
    """The enterprise address of a payment key hash."""
    return Address(payment_part=VerificationKeyHash(bytes(key_hash)), network=ADDRESS_NETWORK)


def script_address(script_hash: bytes) -> Address:
    """The enterprise address of a script."""
    return Address(payment_part=ScriptHash(bytes(script_hash)), network=ADDRESS_NETWORK)


def operator_native_script(key_hash: bytes) -> NativeScript:
    """ScriptPubkey(key hash): the native-script policy that only the key
    can mint or burn with."""
    return ScriptPubkey(VerificationKeyHash(bytes(key_hash)))


def native_policy_id(script: NativeScript) -> bytes:
    return bytes(script.hash().payload)


# ── Transaction bytes ────────────────────────────────────────────────────────


def tx_bytes(tx: Transaction) -> bytes:
    data = tx.to_cbor()
    return data if isinstance(data, bytes) else bytes.fromhex(data)


def _tx_items(tx_cbor: bytes) -> list[bytes]:
    major, count, pos = _head(tx_cbor, 0)
    if major != 4:
        raise DeployError("the transaction is not a CBOR array")
    items, end = _items(tx_cbor, pos, count)
    if end != len(tx_cbor) or len(items) != 4:
        raise DeployError("the transaction is not [body, witnesses, valid, auxiliary data]")
    return items


def transaction_id(tx_cbor: bytes) -> str:
    """The id of a transaction: blake2b-256 of the body bytes as they are."""
    return hashlib.blake2b(_tx_items(tx_cbor)[0], digest_size=32).hexdigest()


def _body_entries(tx_cbor: bytes) -> dict[int, bytes]:
    body = _tx_items(tx_cbor)[0]
    major, count, pos = _head(body, 0)
    if major != 5:
        raise DeployError("the transaction body is not a CBOR map")
    flat, _ = _items(body, pos, None if count is None else 2 * count)
    return {cbor2.loads(flat[i]): flat[i + 1] for i in range(0, len(flat), 2)}


def raw_outputs(tx_cbor: bytes) -> list[bytes]:
    """The outputs of a transaction as they are serialised in its body."""
    outputs = _body_entries(tx_cbor)[1]
    major, count, pos = _head(outputs, 0)
    items, _ = _items(outputs, pos, count)
    return items


def add_vkey_witness(tx_cbor: bytes, signing_key: PaymentSigningKey) -> bytes:
    """The transaction with the Ed25519 witness of ``signing_key`` added to
    its witness set. The body bytes do not change, so the id stays."""
    items = _tx_items(tx_cbor)
    body_hash = hashlib.blake2b(items[0], digest_size=32).digest()
    vkey = PaymentVerificationKey.from_signing_key(signing_key)
    witness = cbor2.dumps([bytes(vkey.payload), signing_key.sign(body_hash)])
    major, count, pos = _head(items[1], 0)
    if major != 5:
        raise DeployError("the witness set is not a CBOR map")
    flat, _ = _items(items[1], pos, None if count is None else 2 * count)
    entries = [(cbor2.loads(flat[i]), flat[i + 1]) for i in range(0, len(flat), 2)]
    for index, (key, value) in enumerate(entries):
        if key == 0:
            present = cbor2.loads(value)
            present = list(present.value if isinstance(present, cbor2.CBORTag) else present)
            if any(bytes(w[0]) == bytes(vkey.payload) for w in present):
                return tx_cbor
            entries[index] = (0, cbor2.dumps(present + [cbor2.loads(witness)]))
            break
    else:
        entries.insert(0, (0, _array_head(1) + witness))
    entries.sort(key=lambda kv: kv[0])
    witness_set = _map_head(len(entries)) + b"".join(cbor2.dumps(k) + v for k, v in entries)
    return _array_head(4) + items[0] + witness_set + items[2] + items[3]


def vkey_witness_hashes(tx_cbor: bytes) -> set[bytes]:
    """The key hashes of the Ed25519 witnesses of a transaction."""
    witnesses = Transaction.from_cbor(tx_cbor).transaction_witness_set.vkey_witnesses or []
    return {bytes(w.vkey.hash().payload) for w in witnesses}


def write_text_envelope(path: Path, tx_cbor: bytes, *, signed: bool, description: str) -> None:
    envelope = {
        "type": "Witnessed Tx ConwayEra" if signed else "Unwitnessed Tx ConwayEra",
        "description": description,
        "cborHex": tx_cbor.hex(),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(envelope, indent=2) + "\n")


def read_text_envelope(path: Path) -> bytes:
    try:
        envelope = json.loads(Path(path).read_text())
        return bytes.fromhex(envelope["cborHex"])
    except (OSError, ValueError, KeyError) as error:
        raise DeployError(f"{path} is not a transaction TextEnvelope file: {error}") from None


# ── Outputs ──────────────────────────────────────────────────────────────────


def min_utxo(chain: ChainContext, output: TransactionOutput) -> int:
    """The ledger minimum of an output with the coin it holds:
    (160 + serialised size) x coinsPerUTxOByte."""
    size = len(TransactionOutput(output.address, output.amount, output.datum_hash, output.datum,
                                 output.script, True).to_cbor())
    return (160 + size) * chain.protocol_param.coins_per_utxo_byte


def exact_output(
    chain: ChainContext,
    address: Address,
    *,
    assets: Optional[MultiAsset] = None,
    datum: Any = None,
    script: Any = None,
    at_least: int = 0,
) -> TransactionOutput:
    """An output that holds the larger of ``at_least`` and its ledger min-UTxO."""
    coin = max(at_least, 1_000_000)
    while True:
        output = TransactionOutput(address, Value(coin, assets or MultiAsset()), datum=datum, script=script)
        need = max(at_least, min_utxo(chain, output))
        if need <= coin:
            return output if need == coin else TransactionOutput(
                address, Value(need, assets or MultiAsset()), datum=datum, script=script,
            )
        coin = need


def tokens(policy: bytes, names: Mapping[bytes, int]) -> MultiAsset:
    return MultiAsset({ScriptHash(bytes(policy)): Asset({AssetName(n): q for n, q in names.items()})})


def has_token(utxo: UTxO, policy: bytes, name: bytes) -> bool:
    amount = utxo.output.amount
    if isinstance(amount, int) or not amount.multi_asset:
        return False
    for pid, assets in amount.multi_asset.items():
        if bytes(pid.payload) == bytes(policy):
            for asset, quantity in assets.items():
                if bytes(asset.payload) == bytes(name) and quantity == 1:
                    return True
    return False


def datum_equals(utxo: UTxO, expected_cbor: bytes) -> bool:
    """True when the inline datum of a UTxO is the Plutus Data of
    ``expected_cbor`` (a definite or an indefinite list encoding of the same
    data counts as equal)."""
    found = datum_cbor(utxo)
    if found is None:
        return False
    return found == bytes(expected_cbor) or cbor2.loads(found) == cbor2.loads(bytes(expected_cbor))


def datum_cbor(utxo: UTxO) -> Optional[bytes]:
    """The inline datum of a UTxO as CBOR, or None."""
    if utxo.output.datum is None:
        return None
    return _datum_cbor(utxo.output.datum)


def script_hash_of(script) -> bytes:
    from pycardano import script_hash

    return bytes(script_hash(script).payload)


def lovelace_only(utxo: UTxO) -> bool:
    amount = utxo.output.amount
    plain = isinstance(amount, int) or not amount.multi_asset or not any(
        q for names in amount.multi_asset.values() for q in names.values()
    )
    return plain and utxo.output.datum is None and utxo.output.datum_hash is None and utxo.output.script is None


def coin_of(utxo: UTxO) -> int:
    amount = utxo.output.amount
    return amount if isinstance(amount, int) else amount.coin


# ── Steps ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Step:
    """One transaction of a deployment.

    ``build`` returns a TransactionBuilder with the outputs, mints,
    certificates, scripts and extra inputs of the step (no wallet inputs).
    ``find`` returns the values to record when the chain already shows the
    result of the step (with "tx" when the transaction is known), or None.
    ``record`` returns the values that the tool keeps for the transaction of
    the step, from its id (for example ``{"ref": "<id>#0"}``).
    ``collateral``: the transaction runs a Plutus script.
    """

    name: str
    title: str
    build: Callable[["Run"], TransactionBuilder]
    find: Optional[Callable[["Run"], Optional[dict]]] = None
    record: Optional[Callable[[str], dict]] = None
    collateral: bool = False


def find_reference_script(run: "Run", address: Address, script_hash: bytes, *, index0: bool = False) -> Optional[dict]:
    """The output at ``address`` that holds the script with ``script_hash``
    as reference script: {"tx", "ref"}. With ``index0``, only output 0 of a
    transaction counts (clients that read "<tx>#0")."""
    for utxo in run.live(addresses=[str(address)]):
        script = utxo.output.script
        if script is None or script_hash_of(script) != bytes(script_hash):
            continue
        if index0 and utxo.input.index != 0:
            continue
        return {"tx": bytes(utxo.input.transaction_id).hex(), "ref": ref_key(utxo.input)}
    return None


@dataclasses.dataclass
class OutputCost:
    index: int
    address: str
    coin: int
    min_utxo: int
    label: str


@dataclasses.dataclass
class Built:
    """A built transaction of a step, before signing."""

    step: Step
    cbor: bytes
    tx_id: str
    fee: int
    min_fee: int
    signed_size: int
    deposit: int
    outputs: list[OutputCost]
    change: int
    spent: int
    budgets: dict[str, ExecutionUnits]
    virtual_inputs: list[str]

    @property
    def locked(self) -> int:
        """AP3X in the outputs that do not return to the operator."""
        return sum(o.coin for o in self.outputs)

    @property
    def cost(self) -> int:
        """AP3X that the transaction takes from the operator."""
        return self.fee + self.deposit + self.locked

    def summary(self) -> dict:
        return {
            "step": self.step.name,
            "tx": self.tx_id,
            "signed_size": self.signed_size,
            "fee": self.fee,
            "min_fee": self.min_fee,
            "deposit": self.deposit,
            "outputs": [dataclasses.asdict(o) for o in self.outputs],
            "locked": self.locked,
            "min_utxo": sum(o.min_utxo for o in self.outputs),
            "cost": self.cost,
            "budgets": {k: {"mem": v.mem, "cpu": v.steps} for k, v in self.budgets.items()},
            "virtual_inputs": self.virtual_inputs,
        }


class Run:
    """The context of one deploy run: the chain, the operator and the inputs
    that the run must not spend."""

    def __init__(
        self,
        chain: DeployChain,
        operator_key_hash: bytes,
        *,
        protected: Iterable[str] = (),
        protected_addresses: Iterable[str] = (),
        dry_run_funds: int = 0,
        ttl_slots: int = DEFAULT_TTL_SLOTS,
        log: Callable[[str], None] = print,
        labels: Optional[Mapping[str, str]] = None,
    ):
        self.chain = chain
        self.operator_key_hash = bytes(operator_key_hash)
        self.operator_address = key_address(self.operator_key_hash)
        self.protected = set(protected)
        self.protected_addresses = {str(a) for a in protected_addresses}
        if str(self.operator_address) in self.protected_addresses:
            raise DeployError(f"the operator address {self.operator_address} is a protected address")
        self.ttl_slots = ttl_slots
        self.log = log
        self.labels = dict(labels or {})
        self.dry_run_funds = dry_run_funds
        if dry_run_funds:
            ref = TransactionInput(TransactionId(hashlib.blake2b(b"vector-deploy dry-run funds", digest_size=32).digest()), 0)
            chain.virtual[ref_key(ref)] = UTxO(ref, TransactionOutput(self.operator_address, Value(dry_run_funds)))

    @property
    def protocol(self) -> ProtocolParameters:
        return self.chain.protocol_param

    def builder(self) -> TransactionBuilder:
        return TransactionBuilder(self.chain)

    def live(self, *, addresses: Sequence[str] = (), refs: Sequence[TransactionInput] = ()) -> list[UTxO]:
        """The UTxOs on chain (no virtual outputs of this run)."""
        return self.chain.ogmios.utxos(addresses=addresses, refs=refs)

    def wallet_utxos(self) -> list[UTxO]:
        """The spendable operator UTxOs: lovelace only, no datum or script,
        not protected. Largest first."""
        utxos = [
            u for u in self.chain.view(addresses=[str(self.operator_address)])
            if lovelace_only(u) and ref_key(u.input) not in self.protected
        ]
        return sorted(utxos, key=lambda u: (-coin_of(u), ref_key(u.input)))

    def label(self, address: Address) -> str:
        return self.labels.get(str(address), str(address))

    def complete(self, step: Step) -> Built:
        """Add wallet inputs, collateral, the validity range and the change,
        build, evaluate and check the transaction of a step."""
        buffer = 0
        for _attempt in range(3):
            builder = step.build(self)
            for utxo in builder.inputs:
                if ref_key(utxo.input) in self.protected or str(utxo.output.address) in self.protected_addresses:
                    raise DeployError(f"{step.name}: the step spends the protected output {ref_key(utxo.input)}")
            wallet = [u for u in self.wallet_utxos() if u not in builder.inputs]
            need = sum(o.amount.coin for o in builder.outputs) + SELECTION_MARGIN
            need += self.protocol.key_deposit * sum(
                1 for c in (builder.certificates or []) if type(c).__name__ == "StakeRegistration"
            )
            have = sum(coin_of(u) for u in builder.inputs)
            for utxo in wallet:
                if have >= need:
                    break
                builder.add_input(utxo)
                have += coin_of(utxo)
            if have < need:
                raise DeployError(
                    f"{step.name}: the operator address {self.operator_address} has "
                    f"{have / LOVELACE_PER_AP3X:.6f} AP3X in spendable outputs; the step needs about "
                    f"{need / LOVELACE_PER_AP3X:.6f} AP3X. Fund the address, or use --dry-run-funds "
                    "for a dry run."
                )
            if step.collateral:
                candidates = [u for u in self.wallet_utxos() if coin_of(u) >= MIN_COLLATERAL]
                if not candidates:
                    raise DeployError(f"{step.name}: no lovelace-only operator output of at least 5 AP3X for the collateral")
                builder.collaterals = [min(candidates, key=lambda u: (coin_of(u), ref_key(u.input)))]
            builder.ttl = self.chain.last_block_slot + self.ttl_slots
            if buffer:
                builder.fee_buffer = buffer
            try:
                body = builder.build(change_address=self.operator_address)
            except DeployError:
                raise
            except Exception as error:  # pycardano balance or evaluation errors
                raise DeployError(f"{step.name}: the transaction cannot be built: {error}") from error
            witnesses = builder.build_witness_set()
            unsigned = tx_bytes(Transaction(body, witnesses))
            built = self._check(step, unsigned)
            if built.fee >= built.min_fee:
                return built
            buffer += built.min_fee - built.fee + 1_000
        raise DeployError(f"{step.name}: the fee stays below the minimum fee")

    def _check(self, step: Step, unsigned: bytes) -> Built:
        """Evaluate the transaction, measure its signed size and its outputs."""
        tx = Transaction.from_cbor(unsigned)
        body = tx.transaction_body
        budgets = self.chain.ogmios.evaluate(unsigned, self.chain.additional_for(unsigned))
        redeemers = tx.transaction_witness_set.redeemer
        expected = len(redeemers) if redeemers else 0
        if len(budgets) != expected:
            raise DeployError(f"{step.name}: Ogmios evaluated {len(budgets)} of {expected} redeemers")
        signers = {self.operator_key_hash}
        fake = unsigned
        for signer in sorted(signers):
            fake = add_vkey_witness(fake, PaymentSigningKey(hashlib.blake2b(signer, digest_size=32).digest()))
        signed_size = len(fake)
        if signed_size > self.protocol.max_tx_size:
            raise DeployError(f"{step.name}: the signed transaction has {signed_size} bytes, "
                              f"more than the limit of {self.protocol.max_tx_size}")
        steps = sum(u.steps for u in budgets.values())
        mem = sum(u.mem for u in budgets.values())
        ref_scripts = 0
        resolved = {ref_key(u.input): u for u in self.chain.view(refs=list(body.inputs) + list(body.reference_inputs or []))}
        for ref in list(body.inputs) + list(body.reference_inputs or []):
            found = resolved.get(ref_key(ref))
            if found is not None and found.output.script is not None:
                ref_scripts += len(bytes(found.output.script)) if not isinstance(found.output.script, NativeScript) \
                    else len(found.output.script.to_cbor())
        min_fee = ledger_fee(self.chain, signed_size, steps, mem, ref_scripts)
        outputs = []
        change = 0
        for index, raw in enumerate(raw_outputs(unsigned)):
            output = body.outputs[index]
            need = (160 + len(raw)) * self.protocol.coins_per_utxo_byte
            if output.amount.coin < need:
                raise DeployError(f"{step.name}: output {index} holds {output.amount.coin} lovelace, "
                                  f"less than its min-UTxO {need}")
            if output.address == self.operator_address and output.datum is None and output.script is None:
                change += output.amount.coin
            else:
                outputs.append(OutputCost(index, str(output.address), output.amount.coin, need, self.label(output.address)))
        deposit = self.protocol.key_deposit * sum(
            1 for c in (body.certificates or []) if type(c).__name__ == "StakeRegistration"
        )
        spent_refs = list(body.inputs)
        live = {ref_key(u.input): u for u in self.chain.view(refs=spent_refs)}
        spent = sum(coin_of(live[ref_key(r)]) for r in spent_refs if ref_key(r) in live)
        return Built(
            step=step,
            cbor=unsigned,
            tx_id=transaction_id(unsigned),
            fee=body.fee,
            min_fee=min_fee,
            signed_size=signed_size,
            deposit=deposit,
            outputs=outputs,
            change=change,
            spent=spent,
            budgets=budgets,
            virtual_inputs=[ref_key(u.input) for u in self.chain.additional_for(unsigned)],
        )


# ── State ────────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DeployState:
    """The state file of a deployment: the values of the tool and the record
    of the steps. The runner writes only files of its own format; it refuses
    every other existing file, so a run never overwrites the state of an old
    deployment."""

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.data = data

    @classmethod
    def load(cls, path, *, module: str, network: str) -> "DeployState":
        path = Path(path)
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except ValueError:
                raise DeployError(f"{path} is not JSON") from None
            if data.get("format") != STATE_FORMAT:
                raise DeployError(
                    f"{path} is not a state file of this deploy tool (no \"format\": \"{STATE_FORMAT}\"). "
                    "It can be the state of an old deployment; this tool never writes it. "
                    "Use --state-dir with a new directory."
                )
            if data.get("module") != module or data.get("network") != network:
                raise DeployError(f"{path} belongs to {data.get('module')} on {data.get('network')}, "
                                  f"not to {module} on {network}")
            return cls(path, data)
        return cls(path, {"format": STATE_FORMAT, "module": module, "network": network,
                          "steps": {}, "pending": {}})

    @property
    def steps(self) -> dict:
        return self.data.setdefault("steps", {})

    @property
    def pending(self) -> dict:
        return self.data.setdefault("pending", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(tmp, self.path)

    def record(self, name: str, tx_id: str, values: Optional[Mapping[str, Any]] = None, *, found: bool = False) -> None:
        entry = {"tx": tx_id, "time": utc_now()}
        if found:
            entry["found_on_chain"] = True
        entry.update(values or {})
        self.steps[name] = entry
        self.pending.pop(name, None)


# ── Runs ─────────────────────────────────────────────────────────────────────


def _ap3x(lovelace: int) -> str:
    return f"{lovelace / LOVELACE_PER_AP3X:,.6f}"


def cost_table(built: Sequence[Built]) -> str:
    """The fee and min-UTxO table of built transactions, with totals."""
    lines = [
        f"{'step':28} {'tx id':16} {'bytes':>6} {'fee':>11} {'deposit':>11} {'outputs':>13} {'min-UTxO':>13} {'cost':>13}",
    ]
    for b in built:
        lines.append(
            f"{b.step.name:28} {b.tx_id[:16]} {b.signed_size:>6} {_ap3x(b.fee):>11} {_ap3x(b.deposit):>11} "
            f"{_ap3x(b.locked):>13} {_ap3x(sum(o.min_utxo for o in b.outputs)):>13} {_ap3x(b.cost):>13}"
        )
    fee = sum(b.fee for b in built)
    deposit = sum(b.deposit for b in built)
    locked = sum(b.locked for b in built)
    minimum = sum(o.min_utxo for b in built for o in b.outputs)
    lines.append(
        f"{'total (AP3X)':28} {'':16} {'':>6} {_ap3x(fee):>11} {_ap3x(deposit):>11} "
        f"{_ap3x(locked):>13} {_ap3x(minimum):>13} {_ap3x(fee + deposit + locked):>13}"
    )
    return "\n".join(lines)


def describe(b: Built) -> str:
    parts = [f"  {b.step.name}: {b.step.title}", f"    tx {b.tx_id}, {b.signed_size} bytes signed, "
             f"fee {_ap3x(b.fee)} (minimum {_ap3x(b.min_fee)})"]
    for o in b.outputs:
        parts.append(f"    output {o.index}: {_ap3x(o.coin)} AP3X to {o.label} (min-UTxO {_ap3x(o.min_utxo)})")
    if b.deposit:
        parts.append(f"    deposit {_ap3x(b.deposit)} AP3X")
    for key, units in sorted(b.budgets.items()):
        parts.append(f"    Ogmios evaluation {key}: mem {units.mem:,}, cpu {units.steps:,}")
    if not b.budgets:
        parts.append("    Ogmios evaluation: no script runs")
    if b.virtual_inputs:
        parts.append(f"    reads {len(b.virtual_inputs)} output(s) of earlier steps that are not on chain (dry run)")
    return "\n".join(parts)


def plan_steps(run: Run, steps: Sequence[Step], state: Optional[DeployState]) -> tuple[list[Built], list[tuple[Step, dict]]]:
    """Build the transactions of the steps that the chain does not show, each
    on the outputs of the earlier ones. Returns the built transactions and
    the steps that the chain shows (with the values to record)."""
    built: list[Built] = []
    found: list[tuple[Step, dict]] = []
    for step in steps:
        done = state.steps.get(step.name) if state is not None else None
        on_chain = step.find(run) if step.find is not None else None
        if on_chain:
            found.append((step, on_chain))
            continue
        if done:
            raise DeployError(
                f"{step.name}: the state records transaction {done.get('tx')}, but the chain does not "
                "show its result. Wait until the chain shows it and run again. If the "
                "transaction was lost, remove the step from the state file to build it again."
            )
        b = run.complete(step)
        run.chain.apply(b.cbor)
        built.append(b)
    return built, found


def record_found(state: DeployState, found: Sequence[tuple[Step, dict]], log: Callable[[str], None]) -> None:
    """Record the steps whose result the chain shows."""
    for step, values in found:
        values = dict(values)
        tx_id = values.pop("tx", None)
        known = state.steps.get(step.name, {})
        if known.get("tx") == tx_id and all(known.get(k) == v for k, v in values.items()):
            continue
        state.record(step.name, tx_id, values, found=True)
        log(f"  {step.name}: the chain shows the result" + (f" (transaction {tx_id})" if tx_id else "") + "; recorded")
    state.save()


def wait_for_transaction(chain: DeployChain, tx_id: str, *, timeout: int = CONFIRM_TIMEOUT_S,
                         poll: int = CONFIRM_POLL_S, sleep: Callable[[float], None] = time.sleep) -> None:
    """Wait until the chain shows output 0 of the transaction."""
    ref = TransactionInput(TransactionId(bytes.fromhex(tx_id)), 0)
    deadline = time.monotonic() + timeout
    while True:
        if chain.ogmios.utxos(refs=[ref]):
            return
        if time.monotonic() > deadline:
            raise DeployError(f"the chain does not show transaction {tx_id} after {timeout} s; "
                              "run the tool again to resume")
        sleep(poll)


def _send(run: Run, state: DeployState, step: Step, tx_id: str, tx: bytes, submit_url: str, *,
          post: Callable, sleep: Callable[[float], None], after: Optional[Callable[[], None]]) -> None:
    if transaction_id(tx) != tx_id:
        raise DeployError(f"{step.name}: the signed transaction is not the built one")
    if run.operator_key_hash not in vkey_witness_hashes(tx):
        raise DeployError(f"{step.name}: the transaction has no witness of the operator key")
    body = Transaction.from_cbor(tx).transaction_body
    live = {ref_key(u.input) for u in run.chain.ogmios.utxos(refs=list(body.inputs))}
    missing = [ref_key(r) for r in body.inputs if ref_key(r) not in live]
    if missing:
        raise DeployError(f"{step.name}: the inputs {', '.join(missing)} are not on chain (spent or not "
                          "confirmed). Build the transactions again.")
    run.log(f"  {step.name}: sending {tx_id}")
    submit_tx(submit_url, tx, post=post)
    state.pending[step.name] = dict(state.pending.get(step.name, {}), tx=tx_id, sent=utc_now())
    state.save()
    wait_for_transaction(run.chain, tx_id, sleep=sleep)
    state.record(step.name, tx_id, step.record(tx_id) if step.record else {})
    state.save()
    run.log(f"  {step.name}: on chain")
    if after is not None:
        after()


def submit_built(run: Run, state: DeployState, built: Sequence[Built], signed: Sequence[bytes],
                 submit_url: str, *, post: Callable = requests.post,
                 sleep: Callable[[float], None] = time.sleep, after: Optional[Callable[[], None]] = None) -> None:
    """Send signed transactions in order. Each one must be on chain before the
    next one goes."""
    for b, tx in zip(built, signed):
        _send(run, state, b.step, b.tx_id, tx, submit_url, post=post, sleep=sleep, after=after)


def sign_transactions(built: Sequence[Built], signing_key: PaymentSigningKey, operator_key_hash: bytes) -> list[bytes]:
    vkey = PaymentVerificationKey.from_signing_key(signing_key)
    if bytes(vkey.hash().payload) != bytes(operator_key_hash):
        raise DeployError("the signing key is not the operator key of this deployment")
    return [add_vkey_witness(b.cbor, signing_key) for b in built]


def build_unsigned(state: DeployState, built: Sequence[Built], tx_dir: Path, log: Callable[[str], None]) -> None:
    """Write the built transactions as unsigned TextEnvelope files and record
    them as pending. A new build replaces the pending transactions of an
    earlier one."""
    state.data["pending"] = {}
    for number, b in enumerate(built, start=1):
        path = Path(tx_dir) / f"{number:02d}-{b.step.name}.unsigned.json"
        write_text_envelope(path, b.cbor, signed=False, description=b.step.name)
        state.pending[b.step.name] = {"tx": b.tx_id, "file": str(path), "built": utc_now()}
        log(f"  {b.step.name}: unsigned transaction {b.tx_id} -> {path}")
    state.save()


def sign_pending(state: DeployState, signing_key: PaymentSigningKey, operator_key_hash: bytes,
                 log: Callable[[str], None]) -> None:
    """Offline: sign the unsigned files of the pending transactions."""
    if not state.pending:
        raise DeployError(f"{state.path} has no built transactions; run the build command first")
    paths = [Path(entry["file"]) for entry in state.pending.values()]
    written = sign_files(paths, signing_key, operator_key_hash)
    for (name, entry), path in zip(list(state.pending.items()), written):
        if transaction_id(read_text_envelope(path)) != entry["tx"]:
            raise DeployError(f"{name}: {path} is not the built transaction {entry['tx']}")
        entry["signed"] = str(path)
        log(f"  {name}: signed -> {path}")
    state.save()


def submit_pending(run: Run, state: DeployState, steps: Sequence[Step], submit_url: str, *,
                   post: Callable = requests.post, sleep: Callable[[float], None] = time.sleep,
                   after: Optional[Callable[[], None]] = None) -> None:
    """Send the signed pending transactions in the order of the steps."""
    names = [s for s in steps if s.name in state.pending]
    if not names:
        raise DeployError(f"{state.path} has no built transactions to send")
    for step in names:
        entry = state.pending[step.name]
        if not entry.get("signed"):
            raise DeployError(f"{step.name}: the transaction is not signed; run the sign command first")
        on_chain = step.find(run) if step.find is not None else None
        if on_chain:
            record_found(state, [(step, on_chain)], run.log)
            continue
        tx = read_text_envelope(Path(entry["signed"]))
        _send(run, state, step, entry["tx"], tx, submit_url, post=post, sleep=sleep, after=after)


def sign_files(paths: Sequence[Path], signing_key: PaymentSigningKey, operator_key_hash: bytes) -> list[Path]:
    """Offline: sign unsigned TextEnvelope files. Writes ``*.signed.json``
    next to each file and returns their paths."""
    vkey = PaymentVerificationKey.from_signing_key(signing_key)
    if bytes(vkey.hash().payload) != bytes(operator_key_hash):
        raise DeployError("the signing key is not the operator key of this deployment")
    written = []
    for path in paths:
        path = Path(path)
        unsigned = read_text_envelope(path)
        signed = add_vkey_witness(unsigned, signing_key)
        out = path.with_name(path.name.replace(".unsigned.json", ".signed.json"))
        if out == path:
            out = path.with_suffix(".signed.json")
        write_text_envelope(out, signed, signed=True, description=json.loads(path.read_text()).get("description", ""))
        written.append(out)
    return written


def describe_run(built: Sequence[Built], found: Sequence[tuple[Step, dict]], log: Callable[[str], None]) -> None:
    for step, values in found:
        tx_id = values.get("tx")
        log(f"  {step.name}: done on chain" + (f" (transaction {tx_id})" if tx_id else ""))
    for b in built:
        log(describe(b))
    if built:
        log("")
        log(cost_table(built))
    else:
        log("  nothing to do: the chain shows every step")


def write_report(path: Optional[Path], header: Mapping[str, Any], built: Sequence[Built],
                 found: Sequence[tuple[Step, dict]]) -> None:
    if path is None:
        return
    report = dict(header)
    report["found_on_chain"] = {step.name: values for step, values in found}
    report["transactions"] = [b.summary() for b in built]
    report["totals"] = {
        "fee": sum(b.fee for b in built),
        "deposit": sum(b.deposit for b in built),
        "locked": sum(b.locked for b in built),
        "min_utxo": sum(o.min_utxo for b in built for o in b.outputs),
        "cost": sum(b.cost for b in built),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2) + "\n")


# ── Command line ─────────────────────────────────────────────────────────────

#: The actions of a deploy tool. "run" is a dry run, or a send with --submit.
ACTIONS = ("run", "build", "sign", "submit")


def add_run_arguments(parser, *, actions: bool = True) -> None:
    """The run options of every deploy tool."""
    if actions:
        parser.add_argument(
            "action", nargs="?", choices=ACTIONS, default="run",
            help="run: dry run (default), or send with --submit; build: write unsigned "
                 "transactions; sign: sign them offline with --skey; submit: send the signed ones",
        )
    add_network_arguments(parser)
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--skey", type=Path, help="operator signing key file (run --submit, sign)")
    key.add_argument("--vkey", type=Path, help="operator verification key file (dry run, build, submit)")
    parser.add_argument("--submit", action="store_true", help="sign with --skey and send (default: dry run)")
    parser.add_argument("--dry-run-funds", type=float, default=0.0, metavar="AP3X",
                        help="dry run only: add a virtual operator UTxO with this many AP3X")
    parser.add_argument("--report", type=Path, help="write the built transactions and costs as JSON")


def operator_key_hash_from_args(args) -> bytes:
    """The operator key hash from --skey or --vkey. Checks the options of the
    action."""
    action = getattr(args, "action", "run")
    if args.skey is None and args.vkey is None:
        raise DeployError("give the operator key: --vkey (dry run, build, submit) or --skey (run --submit, sign)")
    if (args.submit or action == "sign") and args.skey is None:
        raise DeployError("signing needs the operator signing key: give --skey")
    if args.submit and action != "run":
        raise DeployError("--submit belongs to the run action; the submit action sends signed files")
    if args.dry_run_funds and (args.submit or action != "run"):
        raise DeployError("--dry-run-funds adds virtual funds; it is only for a dry run")
    path = args.skey if args.skey is not None else args.vkey
    return bytes(load_verification_key(path).hash().payload)


def execute(
    args,
    run: Run,
    steps: Sequence[Step],
    state: DeployState,
    *,
    tx_dir: Path,
    header: Mapping[str, Any],
    after: Optional[Callable[[], None]] = None,
    post: Callable = requests.post,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Built]:
    """Run the action of the command line on the steps: dry run, send
    (--submit), build or submit. The sign action needs no network and runs
    through ``sign_pending``."""
    action = getattr(args, "action", "run")
    if action == "submit":
        submit_pending(run, state, steps, run.chain.preset.submit_url, post=post, sleep=sleep, after=after)
        return []
    built, found = plan_steps(run, steps, state)
    describe_run(built, found, run.log)
    write_report(args.report, header, built, found)
    if action == "run" and not args.submit:
        run.log("\nDry run: nothing was signed or sent.")
        return built
    record_found(state, found, run.log)
    if after is not None:
        after()
    if action == "build":
        build_unsigned(state, built, tx_dir, run.log)
        run.log("\nSign the files offline (sign action with --skey), then send them (submit action).")
        return built
    if not built:
        return built
    signed = sign_transactions(built, load_signing_key(args.skey), run.operator_key_hash)
    run.log(f"\nSending {len(built)} transaction(s); total cost "
            f"{_ap3x(sum(b.cost for b in built))} AP3X (fees, deposits and outputs above).")
    submit_built(run, state, built, signed, run.chain.preset.submit_url, post=post, sleep=sleep, after=after)
    return built


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 vector_deploy.py sign --skey KEY --operator-key-hash HEX FILE...``:
    offline signing of unsigned transaction files (TextEnvelope)."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sign = commands.add_parser("sign", help="sign unsigned transaction files offline")
    sign.add_argument("--skey", type=Path, required=True)
    sign.add_argument("--operator-key-hash", required=True, help="the expected operator key hash (56 hex)")
    sign.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        written = sign_files(args.files, load_signing_key(args.skey), bytes.fromhex(args.operator_key_hash))
    except DeployError as error:
        print(f"ERROR: {error}")
        return 1
    for path in written:
        print(f"signed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
