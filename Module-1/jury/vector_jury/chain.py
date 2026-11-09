"""Chain access of the jury tools: Ogmios for UTxOs, the tip, the protocol
parameters, evaluation and submission; Koios for block heights, transaction
metadata and asset history.

``Chain`` reuses the Ogmios client and the UTxO conversions of
``shared/python/vector_deploy.py``. It takes its network from its argument,
not from the environment, so one process can follow the deployments of one
network without the import-time switch of ``simulation/config.py``.

Times: Vector has slots of one second. ``TimeBase`` converts slots to POSIX
milliseconds (mainnet: slot 0 is 1756485600000 ms, the system start
2025-08-29T16:40:00Z).

Chained dry runs: ``apply`` adds the outputs of a built (not submitted)
transaction to a virtual view and hides its inputs. ``utxos_at`` and
``utxos_by_refs`` show the view, and ``evaluate`` sends the virtual outputs
that a transaction reads as Ogmios ``additionalUtxo``.

Submission: ``submit`` sends a signed transaction with Ogmios
``submitTransaction`` and returns the transaction id of the node's answer.
"""

import dataclasses
import json
import math
import time
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

import requests
import cbor2
from pycardano import (Address, Asset, AssetName, ChainContext, ExecutionUnits, MultiAsset, Network, ProtocolParameters,
                       RawCBOR, ScriptHash, Transaction, TransactionId, TransactionInput, TransactionOutput, UTxO, Value)

from . import _paths  # noqa: F401  (shared/python on sys.path)
import vector_deploy as vd

#: Vector mainnet and testnet both use network id 1 in addresses.
ADDRESS_NETWORK = Network.MAINNET

NetworkPreset = vd.NetworkPreset
NETWORKS = vd.NETWORKS


class ChainError(RuntimeError):
    """A chain query or a submission failed."""


class EvaluationError(ChainError):
    """Ogmios (or the test evaluator) rejected a script of the transaction."""

    def __init__(self, message: str, *, failing: Sequence[str] = ()):
        super().__init__(message)
        #: The failing redeemers, for example ["spend:1"], when known.
        self.failing = list(failing)


class SubmitError(ChainError):
    """The node refused the transaction."""


def network_preset(name: str, **overrides) -> NetworkPreset:
    """The endpoints of a network (``vector_deploy.network_preset``: flags,
    then VECTOR_OGMIOS_URL / VECTOR_SUBMIT_URL / VECTOR_KOIOS_URL, then the
    preset)."""
    try:
        return vd.network_preset(name, **overrides)
    except vd.DeployError as error:
        raise ChainError(str(error)) from None


# ── Time ─────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class TimeBase:
    """Slot <-> POSIX milliseconds of a network."""

    zero_time_ms: int
    zero_slot: int = 0
    slot_length_ms: int = 1000

    def slot_to_ms(self, slot: int) -> int:
        return self.zero_time_ms + (int(slot) - self.zero_slot) * self.slot_length_ms

    def ms_to_slot(self, posix_ms: int, *, ceil: bool = False) -> int:
        """The slot of a POSIX time: the slot that starts at or before it, or
        with ``ceil`` the first slot that starts at or after it."""
        offset = int(posix_ms) - self.zero_time_ms
        if ceil:
            return self.zero_slot + -((-offset) // self.slot_length_ms)
        return self.zero_slot + offset // self.slot_length_ms


#: The time bases of the Vector networks (system start, 1-second slots).
TIME_BASES = {
    "mainnet": TimeBase(1756485600000),
    "testnet": TimeBase(1752057484000),
}


# ── Koios ────────────────────────────────────────────────────────────────────


class Koios:
    """Read-only Koios queries. Koios shares its rate limit with production
    services, so the callers batch their queries and cache the answers."""

    BATCH = 50

    def __init__(self, url: str, *, timeout: int = 30, post: Callable = requests.post, get: Callable = requests.get):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._post = post
        self._get = get

    def _answer(self, response, what: str) -> list:
        try:
            data = response.json()
        except ValueError:
            raise ChainError(f"Koios {what}: HTTP {response.status_code}: {response.text[:300]}") from None
        if not isinstance(data, list):
            raise ChainError(f"Koios {what}: {json.dumps(data)[:300]}")
        return data

    def post(self, path: str, body: Mapping) -> list:
        try:
            response = self._post(f"{self.url}/{path}", json=dict(body), timeout=self.timeout)
        except requests.RequestException as error:
            raise ChainError(f"Koios {path}: {error}") from None
        return self._answer(response, path)

    def get(self, path: str, params: Mapping) -> list:
        try:
            response = self._get(f"{self.url}/{path}", params=dict(params), timeout=self.timeout)
        except requests.RequestException as error:
            raise ChainError(f"Koios {path}: {error}") from None
        return self._answer(response, path)

    def _batched(self, path: str, tx_hashes: Sequence[str], extra: Mapping = ()) -> list:
        out: list = []
        hashes = list(dict.fromkeys(tx_hashes))
        for i in range(0, len(hashes), self.BATCH):
            out.extend(self.post(path, {"_tx_hashes": hashes[i:i + self.BATCH], **dict(extra)}))
        return out

    def tx_info(self, tx_hashes: Sequence[str], *, full: bool = False) -> list:
        """``tx_info``. Without ``full``: no inputs, metadata, assets,
        withdrawals, certificates and scripts (block height, slot and time
        stay). With ``full``: inputs, assets (mints) and scripts (redeemers),
        and the outputs with their inline datums."""
        # Vector Koios returns inline_datum.bytes only with both _scripts and
        # _bytecode set.
        flags = {"_inputs": full, "_metadata": False, "_assets": full, "_withdrawals": False,
                 "_certs": False, "_scripts": full, "_bytecode": full}
        return self._batched("tx_info", tx_hashes, flags)

    def tx_cbor(self, tx_hashes: Sequence[str]) -> list:
        """``tx_cbor``: ``[{"tx_hash", "block_height", "cbor"}]`` (the CBOR can
        be null on an instance that does not keep it)."""
        return self._batched("tx_cbor", tx_hashes)

    def policy_asset_list(self, policy: str) -> list:
        """``policy_asset_list``: every asset ever minted under a policy (a
        burned asset has total supply 0)."""
        return self.get("policy_asset_list", {"_asset_policy": policy})

    def credential_utxos(self, credentials: Sequence[str]) -> list:
        """``credential_utxos`` (extended: assets and inline datums) of
        payment credentials, any stake credential."""
        return self.post("credential_utxos", {"_payment_credentials": list(credentials), "_extended": True})

    def tx_metadata(self, tx_hashes: Sequence[str]) -> list:
        """``tx_metadata``: ``[{"tx_hash", "metadata"}]`` (JSON metadata, byte
        strings as ``0x…`` text)."""
        return self._batched("tx_metadata", tx_hashes)

    def asset_history(self, policy: str, name: str) -> list:
        """``asset_history``: the minting transactions of an asset."""
        return self.get("asset_history", {"_asset_policy": policy, "_asset_name": name})


# ── pycardano context ────────────────────────────────────────────────────────


class _Context(ChainContext):
    """The pycardano chain context of a ``Chain`` (TransactionBuilder reads
    the protocol parameters, the network and the tip)."""

    def __init__(self, chain: "Chain"):
        self._chain = chain

    @property
    def protocol_param(self) -> ProtocolParameters:
        return self._chain.protocol_parameters()

    @property
    def network(self) -> Network:
        return ADDRESS_NETWORK

    @property
    def last_block_slot(self) -> int:
        return self._chain.tip_slot()

    @property
    def genesis_param(self):
        return None

    def _utxos(self, address: str) -> List[UTxO]:
        return self._chain.utxos_at(address)

    def evaluate_tx_cbor(self, cbor) -> Dict[str, ExecutionUnits]:
        return self._chain.evaluate(cbor if isinstance(cbor, bytes) else bytes.fromhex(cbor))


# ── Chain ────────────────────────────────────────────────────────────────────


def ref_key(ref: Union[TransactionInput, UTxO]) -> str:
    """``"<tx id>#<index>"`` of an input or a UTxO."""
    ref = ref.input if isinstance(ref, UTxO) else ref
    return f"{bytes(ref.transaction_id).hex()}#{ref.index}"


def parse_ref(text: Union[str, TransactionInput]) -> TransactionInput:
    """A TransactionInput from ``"<64 hex>#<index>"``."""
    if isinstance(text, TransactionInput):
        return text
    try:
        return vd.parse_ref(text)
    except vd.DeployError as error:
        raise ValueError(str(error)) from None


def _cbor(tx) -> bytes:
    if isinstance(tx, Transaction):
        return vd.tx_bytes(tx)
    return tx if isinstance(tx, bytes) else bytes.fromhex(tx)


class Chain:
    """Ogmios and Koios access to one Vector network, with a virtual view for
    chained dry runs."""

    def __init__(
        self,
        network: str = "mainnet",
        *,
        ogmios_url: Optional[str] = None,
        koios_url: Optional[str] = None,
        ogmios: Optional[vd.Ogmios] = None,
        koios: Optional[Koios] = None,
        time_base: Optional[TimeBase] = None,
    ):
        if network not in TIME_BASES and time_base is None:
            raise ChainError(f"unknown network {network!r}; use one of {', '.join(TIME_BASES)}")
        self.network = network
        self.preset = network_preset(network, ogmios_url=ogmios_url, koios_url=koios_url) \
            if network in NETWORKS else None
        self.time = time_base or TIME_BASES[network]
        self.ogmios = ogmios if ogmios is not None else (vd.Ogmios(self.preset.ogmios_url) if self.preset else None)
        self.koios = koios if koios is not None else (Koios(self.preset.koios_url) if self.preset else None)
        self.context = _Context(self)
        self.virtual: Dict[str, UTxO] = {}
        self.spent: set = set()
        self._protocol: Optional[ProtocolParameters] = None
        self._network_checked = False

    # ── protocol, tip and time ───────────────────────────────────────────

    def protocol_parameters(self) -> ProtocolParameters:
        """The protocol parameters with the Plutus cost models (cached)."""
        if self._protocol is None:
            try:
                self._protocol = vd.protocol_parameters(self._rpc("queryLedgerState/protocolParameters"))
            except vd.DeployError as error:
                raise ChainError(str(error)) from None
        return self._protocol

    def tip_slot(self) -> int:
        return int(self._rpc("queryLedgerState/tip")["slot"])

    def block_height(self) -> int:
        """The block height of the tip (Ogmios ``queryNetwork/blockHeight``)."""
        return int(self._rpc("queryNetwork/blockHeight"))

    def now_ms(self) -> int:
        """The POSIX time of the tip slot."""
        return self.slot_to_ms(self.tip_slot())

    def slot_to_ms(self, slot: int) -> int:
        return self.time.slot_to_ms(slot)

    def ms_to_slot(self, posix_ms: int, *, ceil: bool = False) -> int:
        return self.time.ms_to_slot(posix_ms, ceil=ceil)

    def check_network(self) -> None:
        """Refuse an Ogmios endpoint of another network (its start time)."""
        if self._network_checked or self.preset is None:
            return
        start = self._rpc("queryNetwork/startTime")
        if start != self.preset.start_time:
            raise ChainError(f"{self.ogmios.url} serves the network that started at {start}, "
                             f"not {self.network} ({self.preset.start_time})")
        self._network_checked = True

    # ── UTxOs ────────────────────────────────────────────────────────────

    def _live(self, *, addresses: Sequence[str] = (), refs: Sequence[TransactionInput] = ()) -> List[UTxO]:
        try:
            return self.ogmios.utxos(addresses=addresses, refs=refs)
        except vd.DeployError as error:
            raise ChainError(str(error)) from None

    def utxos_at(self, address: Union[str, Address]) -> List[UTxO]:
        """The UTxOs at an address (the virtual view included; a virtual
        output that the chain shows too counts once)."""
        address = str(address)
        found = [u for u in self._live(addresses=[address])
                 if ref_key(u) not in self.spent and ref_key(u) not in self.virtual]
        found += [u for k, u in self.virtual.items() if k not in self.spent and str(u.output.address) == address]
        return found

    def utxos_by_refs(self, refs: Iterable[Union[str, TransactionInput]]) -> List[UTxO]:
        """The unspent outputs among ``refs`` (the virtual view included), in
        the order of ``refs``."""
        wanted = [parse_ref(r) for r in refs]
        live_refs = [r for r in wanted if ref_key(r) not in self.virtual]
        live = {ref_key(u): u for u in (self._live(refs=live_refs) if live_refs else [])}
        out = []
        for ref in wanted:
            key = ref_key(ref)
            if key in self.spent:
                continue
            utxo = self.virtual.get(key) or live.get(key)
            if utxo is not None:
                out.append(utxo)
        return out

    def utxo(self, ref: Union[str, TransactionInput]) -> Optional[UTxO]:
        found = self.utxos_by_refs([ref])
        return found[0] if found else None

    def require_utxo(self, ref: Union[str, TransactionInput], what: str = "output") -> UTxO:
        utxo = self.utxo(ref)
        if utxo is None:
            raise ChainError(f"the {what} {ref_key(parse_ref(ref))} is not on chain (spent or not confirmed)")
        return utxo

    def wait_for(self, ref: Union[str, TransactionInput], *, timeout: float = 300, poll: float = 5,
                 sleep: Callable[[float], None] = time.sleep) -> UTxO:
        """Wait until the chain shows the output ``ref``."""
        deadline = time.monotonic() + timeout
        while True:
            utxo = self.utxo(ref)
            if utxo is not None:
                return utxo
            if time.monotonic() > deadline:
                raise ChainError(f"the chain does not show {ref_key(parse_ref(ref))} after {timeout} s")
            sleep(poll)

    # ── virtual view ─────────────────────────────────────────────────────

    def apply(self, tx) -> str:
        """Add the outputs of a built transaction to the view and hide its
        inputs. Returns its id."""
        data = _cbor(tx)
        body = Transaction.from_cbor(data).transaction_body
        tx_id = vd.transaction_id(data)
        for ref in body.inputs:
            self.spent.add(ref_key(ref))
        for index, output in enumerate(body.outputs):
            ref = TransactionInput(TransactionId(bytes.fromhex(tx_id)), index)
            self.virtual[ref_key(ref)] = UTxO(ref, output)
        return tx_id

    def additional_for(self, tx) -> List[UTxO]:
        """The virtual outputs that a transaction spends or reads."""
        body = Transaction.from_cbor(_cbor(tx)).transaction_body
        refs = list(body.inputs) + list(body.reference_inputs or []) + list(body.collateral or [])
        return [self.virtual[ref_key(r)] for r in refs if ref_key(r) in self.virtual]

    # ── evaluation and submission ────────────────────────────────────────

    def evaluate(self, tx, additional: Optional[Sequence[UTxO]] = None) -> Dict[str, ExecutionUnits]:
        """Ogmios ``evaluateTransaction``: the execution units of each redeemer,
        keyed ``"spend:<i>"`` / ``"mint:<i>"``. The virtual outputs that the
        transaction reads go as ``additionalUtxo`` (and ``additional``)."""
        data = _cbor(tx)
        extra = list(self.additional_for(data)) + list(additional or [])
        try:
            return self.ogmios.evaluate(data, extra)
        except vd.DeployError as error:
            raise EvaluationError(str(error)) from None

    def submit(self, tx) -> str:
        """Send a signed transaction with Ogmios ``submitTransaction``. Returns
        the transaction id that the node reports."""
        self.check_network()
        data = _cbor(tx)
        try:
            result = self.ogmios.rpc("submitTransaction", {"transaction": {"cbor": data.hex()}})
        except vd.DeployError as error:
            raise SubmitError(str(error)) from None
        try:
            return result["transaction"]["id"]
        except (TypeError, KeyError):
            raise SubmitError(f"unexpected submitTransaction answer: {json.dumps(result)[:300]}") from None

    # ── Koios ────────────────────────────────────────────────────────────

    def tx_block_heights(self, tx_ids: Sequence[str]) -> Dict[str, Optional[int]]:
        """The block height of each transaction (None when Koios does not show
        it yet)."""
        heights: Dict[str, Optional[int]] = {t: None for t in tx_ids}
        if not tx_ids:
            return heights
        for entry in self.koios.tx_info(list(tx_ids)):
            if entry.get("tx_hash") in heights and entry.get("block_height") is not None:
                heights[entry["tx_hash"]] = int(entry["block_height"])
        return heights

    def tx_metadata(self, tx_ids: Sequence[str]) -> Dict[str, Optional[dict]]:
        """The JSON metadata of each transaction (Koios), or None."""
        found: Dict[str, Optional[dict]] = {t: None for t in tx_ids}
        if not tx_ids:
            return found
        for entry in self.koios.tx_metadata(list(tx_ids)):
            if entry.get("tx_hash") in found:
                found[entry["tx_hash"]] = entry.get("metadata")
        return found

    def utxos_by_payment_credential(self, credential: bytes) -> List[UTxO]:
        """The unspent outputs whose payment credential is ``Script(credential)``
        with any stake credential (Koios ``credential_utxos``; SPEC §12.2
        step 1). When Koios fails, the enterprise address on Ogmios. The
        virtual view applies."""
        credential = bytes(credential)
        try:
            live = [self.koios_utxo(r) for r in self.koios.credential_utxos([credential.hex()])
                    if not r.get("is_spent")]
        except Exception:  # noqa: BLE001 - Koios down or a test double without the endpoint
            live = self._live(addresses=[str(Address(ScriptHash(credential), network=ADDRESS_NETWORK))])
        found = [u for u in live if ref_key(u) not in self.spent and ref_key(u) not in self.virtual]
        found += [u for k, u in self.virtual.items() if k not in self.spent
                  and isinstance(u.output.address.payment_part, ScriptHash)
                  and bytes(u.output.address.payment_part.payload) == credential]
        return found

    @staticmethod
    def koios_utxo(row: Mapping) -> UTxO:
        """A pycardano UTxO from a Koios extended UTxO row (inline datum bytes,
        else its JSON form)."""
        from .types import data_from_json

        assets = MultiAsset()
        for a in row.get("asset_list") or []:
            assets += MultiAsset({ScriptHash(bytes.fromhex(a["policy_id"])):
                                  Asset({AssetName(bytes.fromhex(a["asset_name"])): int(a["quantity"])})})
        inline = row.get("inline_datum") or {}
        datum = None
        if inline.get("bytes"):
            datum = RawCBOR(bytes.fromhex(inline["bytes"]))
        elif inline.get("value"):
            datum = RawCBOR(cbor2.dumps(data_from_json(inline["value"])))
        ref = TransactionInput(TransactionId(bytes.fromhex(row["tx_hash"])), int(row["tx_index"]))
        return UTxO(ref, TransactionOutput(Address.from_primitive(row["address"]), Value(int(row["value"]), assets),
                                           datum=datum))

    def asset_mint_txs(self, policy: Union[bytes, str], name: Union[bytes, str]) -> List[str]:
        """The transactions that minted an asset (Koios ``asset_history``),
        oldest first when Koios gives the block time."""
        policy_hex = policy.hex() if isinstance(policy, bytes) else policy
        name_hex = name.hex() if isinstance(name, bytes) else name
        txs = []
        for entry in self.koios.asset_history(policy_hex, name_hex):
            for mint in entry.get("minting_txs") or []:
                if int(mint.get("quantity", 0)) > 0:
                    txs.append((mint.get("block_time") or 0, mint["tx_hash"]))
        return [tx for _, tx in sorted(txs)]

    # ── helpers ──────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: Optional[Mapping] = None):
        try:
            return self.ogmios.rpc(method, params)
        except vd.DeployError as error:
            raise ChainError(str(error)) from None


def scale_units(units: ExecutionUnits, factor: float) -> ExecutionUnits:
    """Execution units times ``factor``, rounded up."""
    return ExecutionUnits(mem=math.ceil(units.mem * factor), steps=math.ceil(units.steps * factor))
