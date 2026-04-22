"""Integration test for the preflight harness UTxO overlay.

Scope: verifies ``UTxOOverlay`` + ``SimulatingOgmiosContext`` correctly
track consumed/added UTxOs across a toy simulated transaction. The
full lifecycle preflight (``_preflight_lifecycle_eval.py``) requires
Ogmios + a funded master wallet and is exercised out-of-band.

Catherine note: the harness ships with this minimal test as a smoke
check; Caroline should expand coverage (slot advancement edge cases,
additionalUtxo JSON shape under multi-asset outputs, PreflightEvalError
path on a synthetic eval error, etc.).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from pycardano import (
    Address,
    Network,
    TransactionInput,
    TransactionOutput,
    UTxO,
)
from pycardano.hash import TransactionId


# ───────────────────────────────────────────────────────────────────────
# Import _preflight_lifecycle_eval.py — it lives at the repo root, not
# under the simulation package, so load it by path.
# ───────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PREFLIGHT_PATH = _REPO_ROOT / "_preflight_lifecycle_eval.py"


@pytest.fixture(scope="module")
def preflight_module():
    spec = importlib.util.spec_from_file_location(
        "_preflight_lifecycle_eval", _PREFLIGHT_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_preflight_lifecycle_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────────────────────────────────────────────────────
# Fixture: a toy UTxO at a fixed address.
# ───────────────────────────────────────────────────────────────────────

def _make_utxo(txid_hex: str, idx: int, addr: Address, lovelace: int) -> UTxO:
    tid = TransactionId(bytes.fromhex(txid_hex))
    ti = TransactionInput(tid, idx)
    to = TransactionOutput(addr, lovelace)
    return UTxO(ti, to)


@pytest.fixture
def fake_addr() -> Address:
    # Synthetic 28-byte vkh → bech32 address at testnet.
    from pycardano.hash import VerificationKeyHash
    vkh = VerificationKeyHash(bytes(28))
    return Address(vkh, network=Network.TESTNET)


# ───────────────────────────────────────────────────────────────────────
# UTxOOverlay: mutation + lookup.
# ───────────────────────────────────────────────────────────────────────

class TestUTxOOverlay:
    def test_initial_state(self, preflight_module):
        overlay = preflight_module.UTxOOverlay()
        assert overlay._consumed_refs == set()
        assert overlay._added_utxos == []
        assert overlay._slot_offset == 0

    def test_filter_chain_utxos_drops_consumed(
        self, preflight_module, fake_addr,
    ):
        overlay = preflight_module.UTxOOverlay()
        u1 = _make_utxo("aa" * 32, 0, fake_addr, 5_000_000)
        u2 = _make_utxo("bb" * 32, 1, fake_addr, 3_000_000)

        # Mark u1 as consumed.
        overlay._consumed_refs.add("aa" * 32 + "#0")
        kept = overlay.filter_chain_utxos([u1, u2], str(fake_addr))
        assert len(kept) == 1
        assert bytes(kept[0].input.transaction_id).hex() == "bb" * 32

    def test_apply_tx_records_consumed_and_added(
        self, preflight_module, fake_addr,
    ):
        """Simulate a TX that spends one input and produces two outputs.
        After apply_tx, consumed_refs has the input, added_utxos has both
        outputs keyed by (tx_hash, 0) and (tx_hash, 1).
        """
        from pycardano import Transaction, TransactionBody, VerificationKeyWitness
        from pycardano.witness import TransactionWitnessSet

        # Build a minimal TX: 1 input (a non-overlay real UTxO), 2 outputs.
        src_txid = "cc" * 32
        ti = TransactionInput(TransactionId(bytes.fromhex(src_txid)), 0)
        out1 = TransactionOutput(fake_addr, 2_000_000)
        out2 = TransactionOutput(fake_addr, 3_000_000)
        body = TransactionBody(inputs=[ti], outputs=[out1, out2], fee=200_000)
        witness_set = TransactionWitnessSet()
        tx = Transaction(body, witness_set)
        tx_hash_hex = bytes(tx.id).hex()

        overlay = preflight_module.UTxOOverlay()
        summary = overlay.apply_tx(tx, tx_hash_hex)

        assert summary == {"consumed": 1, "added": 2}
        assert (src_txid + "#0") in overlay._consumed_refs
        assert len(overlay._added_utxos) == 2
        # Outputs are keyed by new tx_hash.
        assert bytes(overlay._added_utxos[0].input.transaction_id).hex() \
            == tx_hash_hex
        assert overlay._added_utxos[0].input.index == 0
        assert overlay._added_utxos[1].input.index == 1
        # Slot advances by +15/tx to model Vector testnet block-inclusion
        # latency (~10–30s per submit). With +15 the simulated slot
        # progression mirrors live wall-clock, so preflight exposes the
        # 180-s commit_window narrowness that the earlier +1 model missed.
        assert overlay._slot_offset == 15

    def test_added_at_address_filters_by_address(
        self, preflight_module, fake_addr,
    ):
        overlay = preflight_module.UTxOOverlay()
        other = Address.from_primitive(str(fake_addr))  # same addr, different obj
        # Manually inject an added UTxO.
        u = _make_utxo("dd" * 32, 0, fake_addr, 1_000_000)
        overlay._added_utxos.append(u)
        found = overlay.added_at_address(str(fake_addr))
        assert len(found) == 1
        assert found[0] is u

        # An address that doesn't match returns nothing.
        from pycardano.hash import VerificationKeyHash as _Vkh
        other_vkh = _Vkh(bytes([1]) + bytes(27))
        other_addr = Address(other_vkh, network=Network.TESTNET)
        found_other = overlay.added_at_address(str(other_addr))
        assert found_other == []

    def test_lookup_ref_consumed_vs_added(
        self, preflight_module, fake_addr,
    ):
        overlay = preflight_module.UTxOOverlay()
        u = _make_utxo("ee" * 32, 5, fake_addr, 7_000_000)
        overlay._added_utxos.append(u)

        # Lookup returns the UTxO object.
        hit = overlay.lookup_ref("ee" * 32, 5)
        assert hit is u

        # Mark as consumed; lookup returns sentinel.
        overlay._consumed_refs.add("ee" * 32 + "#5")
        hit2 = overlay.lookup_ref("ee" * 32, 5)
        assert hit2 == "CONSUMED"

        # Unknown ref returns None.
        miss = overlay.lookup_ref("ff" * 32, 0)
        assert miss is None

    def test_all_simulated_utxos_skips_consumed(
        self, preflight_module, fake_addr,
    ):
        overlay = preflight_module.UTxOOverlay()
        u1 = _make_utxo("11" * 32, 0, fake_addr, 1_000_000)
        u2 = _make_utxo("22" * 32, 0, fake_addr, 2_000_000)
        overlay._added_utxos.extend([u1, u2])

        live = overlay.all_simulated_utxos()
        assert len(live) == 2

        overlay._consumed_refs.add("11" * 32 + "#0")
        live2 = overlay.all_simulated_utxos()
        assert len(live2) == 1
        assert live2[0] is u2


# ───────────────────────────────────────────────────────────────────────
# _utxo_to_ogmios_additional: UTxO → Ogmios JSON shape.
# ───────────────────────────────────────────────────────────────────────

class TestOgmiosAdditionalUtxoShape:
    def test_pure_ada_utxo_shape(self, preflight_module, fake_addr):
        u = _make_utxo("33" * 32, 7, fake_addr, 4_500_000)
        item = preflight_module._utxo_to_ogmios_additional(u)
        assert item["transaction"] == {"id": "33" * 32}
        assert item["index"] == 7
        assert item["address"] == str(fake_addr)
        assert item["value"] == {"ada": {"lovelace": 4_500_000}}
        assert "datum" not in item
        assert "script" not in item

    def test_multi_asset_utxo_shape(self, preflight_module, fake_addr):
        from pycardano import (
            Asset,
            AssetName,
            MultiAsset,
            ScriptHash,
            Value,
        )
        policy = ScriptHash(bytes(28))
        asset_name = AssetName(b"jur_" + bytes(28))
        ma = MultiAsset()
        a = Asset()
        a[asset_name] = 1
        ma[policy] = a
        v = Value(25_000_000, ma)
        ti = TransactionInput(TransactionId(bytes.fromhex("44" * 32)), 0)
        to = TransactionOutput(fake_addr, v)
        u = UTxO(ti, to)

        item = preflight_module._utxo_to_ogmios_additional(u)
        assert item["value"]["ada"] == {"lovelace": 25_000_000}
        policy_hex = bytes(policy).hex()
        assert policy_hex in item["value"]
        assert bytes(asset_name).hex() in item["value"][policy_hex]
        assert item["value"][policy_hex][bytes(asset_name).hex()] == 1


# ───────────────────────────────────────────────────────────────────────
# SimulatingOgmiosContext: uses overlay + delegates protocol_param.
# ───────────────────────────────────────────────────────────────────────

class TestSimulatingOgmiosContext:
    def test_raises_without_shared_overlay(self, preflight_module):
        # Both shared slots must be installed by the harness; without them,
        # SimulatingOgmiosContext refuses to instantiate.
        preflight_module.SimulatingOgmiosContext._shared_overlay = None
        preflight_module.SimulatingOgmiosContext._shared_real_ctx_class = None
        with pytest.raises(RuntimeError, match="_shared_real_ctx_class not set"):
            preflight_module.SimulatingOgmiosContext()

    def test_utxos_merges_real_and_overlay(
        self, preflight_module, fake_addr, monkeypatch,
    ):
        """Overlay-added UTxOs appear in ``utxos(addr)`` output and
        consumed real UTxOs are filtered out.
        """
        overlay = preflight_module.UTxOOverlay()
        # Stage: one real UTxO (will be consumed) + one overlay UTxO (added).
        real_u_live = _make_utxo("55" * 32, 0, fake_addr, 9_000_000)
        real_u_consumed = _make_utxo("66" * 32, 0, fake_addr, 8_000_000)
        overlay._consumed_refs.add("66" * 32 + "#0")
        added_u = _make_utxo("77" * 32, 2, fake_addr, 5_500_000)
        overlay._added_utxos.append(added_u)

        # Mock the real OgmiosContext so we don't hit the network.
        class _FakeReal:
            _pp = "pp-sentinel"
            last_block_slot = 10_000_000

            def utxos(self, _addr):
                return [real_u_live, real_u_consumed]

            @property
            def protocol_param(self):
                return self._pp

            def genesis_param(self):
                return None

        import simulation.chain as _chain
        monkeypatch.setattr(_chain, "OgmiosContext", _FakeReal)

        preflight_module.SimulatingOgmiosContext._shared_overlay = overlay
        preflight_module.SimulatingOgmiosContext._shared_real_ctx_class = _FakeReal
        try:
            ctx = preflight_module.SimulatingOgmiosContext()
            out = ctx.utxos(str(fake_addr))
            refs = {bytes(u.input.transaction_id).hex() + f"#{u.input.index}"
                    for u in out}
            assert ("55" * 32 + "#0") in refs  # real kept
            assert ("66" * 32 + "#0") not in refs  # real consumed
            assert ("77" * 32 + "#2") in refs  # overlay added
        finally:
            preflight_module.SimulatingOgmiosContext._shared_overlay = None
            preflight_module.SimulatingOgmiosContext._shared_real_ctx_class = None

    def test_last_block_slot_advances_with_overlay_offset(
        self, preflight_module, monkeypatch,
    ):
        overlay = preflight_module.UTxOOverlay()
        overlay._slot_offset = 175  # 7 steps simulated

        class _FakeReal:
            last_block_slot = 9_000_000

            def utxos(self, _a):
                return []

            @property
            def protocol_param(self):
                return None

            def genesis_param(self):
                return None

        import simulation.chain as _chain
        monkeypatch.setattr(_chain, "OgmiosContext", _FakeReal)
        preflight_module.SimulatingOgmiosContext._shared_overlay = overlay
        preflight_module.SimulatingOgmiosContext._shared_real_ctx_class = _FakeReal
        try:
            ctx = preflight_module.SimulatingOgmiosContext()
            assert ctx.last_block_slot == 9_000_000 + 175
        finally:
            preflight_module.SimulatingOgmiosContext._shared_overlay = None
            preflight_module.SimulatingOgmiosContext._shared_real_ctx_class = None
