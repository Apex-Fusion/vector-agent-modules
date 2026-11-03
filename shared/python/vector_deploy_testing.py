"""Offline doubles of Ogmios and Koios for the tests of the deploy tools.

``FakeOgmios`` holds UTxOs in memory and serves the Ogmios calls of
``vector_deploy``: protocol parameters (the Vector mainnet result of
``fixtures/ogmios-protocol-parameters-mainnet.json``), the tip, the start
time, UTxO queries and ``evaluateTransaction``. The evaluation runs every
redeemer with ``aiken tx simulate`` (``aiken_simulate``) on the stored and the
additional UTxOs, so a builder gets real execution units from the applied
validators, and a failing script fails the build as it would on Ogmios.
``confirm`` adds the outputs of a sent transaction, as the chain would.

Nothing goes to a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from pycardano import ExecutionUnits, Transaction, TransactionId, TransactionInput, UTxO

import vector_deploy as vd
from aiken_simulate import simulate

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAINNET_PROTOCOL_PARAMETERS = FIXTURES / "ogmios-protocol-parameters-mainnet.json"

#: Vector mainnet: system start 2025-08-29T16:40:00Z, 1 s slots from slot 0.
MAINNET_ZERO_TIME_MS = 1_756_485_600_000

_LABELS = {"Spend": "spend", "Mint": "mint", "Publish": "certificate", "Withdraw": "withdrawal",
           "Vote": "voting", "Propose": "proposing"}


class FakeOgmios:
    def __init__(self, *, tip: int = 37_000_000, network: str = "mainnet",
                 protocol: Optional[dict] = None, zero_time_ms: int = MAINNET_ZERO_TIME_MS):
        self.url = "fake-ogmios"
        self.tip = tip
        self.network = network
        self.protocol = protocol or json.loads(MAINNET_PROTOCOL_PARAMETERS.read_text())
        self.zero_time_ms = zero_time_ms
        self.store: dict[str, UTxO] = {}
        self.evaluated: list[tuple[bytes, list[str]]] = []

    def add(self, *utxos: UTxO) -> None:
        for utxo in utxos:
            self.store[vd.ref_key(utxo.input)] = utxo

    def remove(self, *refs: TransactionInput) -> None:
        for ref in refs:
            self.store.pop(vd.ref_key(ref), None)

    def confirm(self, tx_cbor: bytes) -> None:
        """Apply a transaction: remove its inputs, add its outputs."""
        tx = Transaction.from_cbor(tx_cbor)
        tx_id = vd.transaction_id(tx_cbor)
        self.remove(*tx.transaction_body.inputs)
        for index, output in enumerate(tx.transaction_body.outputs):
            self.add(UTxO(TransactionInput(TransactionId(bytes.fromhex(tx_id)), index), output))

    # The Ogmios calls of vector_deploy

    def protocol_parameters(self) -> dict:
        return self.protocol

    def start_time(self) -> str:
        return vd.NETWORKS[self.network].start_time

    def tip_slot(self) -> int:
        return self.tip

    def utxos(self, *, addresses: Sequence[str] = (), refs: Sequence[TransactionInput] = ()) -> list[UTxO]:
        wanted = {str(a) for a in addresses}
        keys = {vd.ref_key(r) for r in refs}
        return [u for k, u in self.store.items() if str(u.output.address) in wanted or k in keys]

    def evaluate(self, tx_cbor: bytes, additional: Sequence[UTxO] = ()) -> dict:
        tx = Transaction.from_cbor(tx_cbor)
        self.evaluated.append((tx_cbor, [vd.ref_key(u.input) for u in additional]))
        if not tx.transaction_witness_set.redeemer:
            return {}
        pool = list(self.store.values()) + list(additional)
        body = tx.transaction_body
        refs = list(body.inputs) + list(body.reference_inputs or []) + list(body.collateral or [])
        by_key = {vd.ref_key(u.input): u for u in pool}
        missing = [vd.ref_key(r) for r in refs if vd.ref_key(r) not in by_key]
        if missing:
            raise vd.DeployError(f"evaluateTransaction: unknown inputs {missing}")
        resolved = list({vd.ref_key(r): by_key[vd.ref_key(r)] for r in refs}.values())
        result = simulate(tx, resolved, zero_time_ms=self.zero_time_ms)
        if not result.passed:
            raise vd.DeployError(f"evaluateTransaction: {result.describe()}")
        units = {}
        for verdict in result.verdicts:
            purpose, index = verdict.purpose.rstrip("]").split("[")
            units[f"{_LABELS[purpose]}:{index}"] = ExecutionUnits(verdict.mem, verdict.cpu)
        return units


class FakeKoios:
    def __init__(self, registered: Sequence[str] = (), balances: Optional[dict] = None):
        self.registered_addresses = set(registered)
        self.balances = balances or {}
        self.url = "fake-koios"

    def account_info(self, stake_addresses: Sequence[str]) -> list[dict]:
        return [
            {"stake_address": a,
             "status": "registered" if a in self.registered_addresses else "not registered",
             "rewards_available": str(self.balances.get(a, 0))}
            for a in stake_addresses
        ]

    def registered(self, stake_address: str) -> bool:
        return stake_address in self.registered_addresses


class FakeSubmit:
    """The submit endpoint: records each transaction and confirms it on the
    fake chain."""

    def __init__(self, ogmios: FakeOgmios):
        self.ogmios = ogmios
        self.sent: list[bytes] = []

    def __call__(self, url, data=None, headers=None, timeout=None, **_kw):
        assert headers == {"Content-Type": "application/cbor"}
        self.sent.append(bytes(data))
        self.ogmios.confirm(bytes(data))

        class Response:
            status_code = 202
            text = ""

        return Response()


def fake_chain(network: str = "mainnet", **kw) -> tuple[vd.DeployChain, FakeOgmios, FakeKoios]:
    ogmios = FakeOgmios(network=network, **kw)
    koios = FakeKoios()
    chain = vd.DeployChain(ogmios, vd.NETWORKS[network], koios=koios)
    return chain, ogmios, koios
