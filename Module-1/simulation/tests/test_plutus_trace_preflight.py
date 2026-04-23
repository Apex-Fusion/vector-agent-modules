"""Tests for _install_plutus_trace_preflight() in _verify_lifecycle_live.py.

Scope:
  1. When ogmios_rpc returns eval errors, the wrapper dumps the expected
     file under /tmp/sim-submit-errors/plutus_trace_<ts>.txt and raises
     RuntimeError with the correct message prefix.
  2. When ogmios_rpc itself raises (transient Ogmios glitch), the wrapper
     falls through to the real submit_tx without re-raising.

Both tests use unittest.mock — no live network access required.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — load _verify_lifecycle_live as a module without executing main()
# ---------------------------------------------------------------------------

_LIVE_PY = Path(__file__).parents[2] / "_verify_lifecycle_live.py"


def _load_live_module() -> ModuleType:
    """Import _verify_lifecycle_live without triggering its if __name__ guard."""
    spec = importlib.util.spec_from_file_location("_verify_lifecycle_live", _LIVE_PY)
    assert spec is not None and spec.loader is not None, (
        f"Cannot locate {_LIVE_PY}"
    )
    mod = importlib.util.module_from_spec(spec)
    # Stub sys.argv so argparse inside the module doesn't choke in test context.
    old_argv = sys.argv
    sys.argv = ["_verify_lifecycle_live.py"]
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        sys.argv = old_argv
    return mod


# ---------------------------------------------------------------------------
# Test 1 — error-result path: dump file written, RuntimeError raised
# ---------------------------------------------------------------------------

class TestPlutusTracePreflight_ErrorResult:
    """Given ogmios_rpc returns a list containing an 'error' item,
    the preflight wrapper must:
      - write a dump file to PLUTUS_TRACE_DIR / plutus_trace_<ts>.txt
      - raise RuntimeError whose message starts with 'plutus_trace preflight failed:'
      - NOT call real_submit_chain
    """

    def test_dump_written_and_error_raised(self, tmp_path, monkeypatch):
        # Arrange
        live = _load_live_module()

        fake_eval_errors = [{"error": {"code": 3110, "message": "ExceededMaxExecutionUnits"}}]

        # Patch PLUTUS_TRACE_DIR to tmp_path so we don't write to /tmp in tests.
        monkeypatch.setattr(live, "PLUTUS_TRACE_DIR", tmp_path)

        fake_rpc = MagicMock(return_value=fake_eval_errors)
        fake_real_submit = MagicMock(return_value=None)
        fake_tx_bytes = b"\xde\xad\xbe\xef"

        import simulation.chain as _chain
        import simulation.tx_builder as _txb

        original_chain_submit = _chain.submit_tx
        original_txb_submit = _txb.submit_tx
        original_chain_rpc = _chain.ogmios_rpc

        try:
            # Patch chain internals before calling _install_plutus_trace_preflight
            _chain.ogmios_rpc = fake_rpc
            _chain.submit_tx = fake_real_submit
            _txb.submit_tx = fake_real_submit

            # Act
            live._install_plutus_trace_preflight()

            with pytest.raises(RuntimeError) as exc_info:
                _chain.submit_tx(fake_tx_bytes)

        finally:
            _chain.submit_tx = original_chain_submit
            _txb.submit_tx = original_txb_submit
            _chain.ogmios_rpc = original_chain_rpc

        # Assert — RuntimeError message
        msg = str(exc_info.value)
        assert msg.startswith("plutus_trace preflight failed:"), (
            f"Expected error prefix not found in: {msg!r}"
        )
        assert "dump=" in msg
        assert "preview=" in msg

        # Assert — dump file exists under tmp_path
        dump_files = list(tmp_path.glob("plutus_trace_*.txt"))
        assert len(dump_files) == 1, (
            f"Expected 1 dump file, found: {dump_files}"
        )

        # Assert — dump file contains expected keys
        payload = json.loads(dump_files[0].read_text())
        assert "tx_cbor_hex" in payload
        assert "eval_result" in payload
        assert "eval_errors" in payload
        assert payload["eval_errors"] == fake_eval_errors

        # Assert — real submit was NOT called (error path stops here)
        fake_real_submit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2 — transient-raise path: ogmios_rpc throws, fall through to submit
# ---------------------------------------------------------------------------

class TestPlutusTracePreflight_TransientRpcRaise:
    """Given ogmios_rpc itself raises (e.g. ConnectionRefusedError),
    the preflight wrapper must:
      - NOT re-raise
      - fall through and call real_submit_chain with the original tx_bytes
    """

    def test_fallthrough_on_rpc_exception(self, monkeypatch):
        # Arrange
        live = _load_live_module()

        fake_rpc = MagicMock(side_effect=ConnectionRefusedError("Ogmios unreachable"))
        fake_real_submit = MagicMock(return_value={"txHash": "aabbcc"})
        fake_tx_bytes = b"\xca\xfe\xba\xbe"

        import simulation.chain as _chain
        import simulation.tx_builder as _txb

        original_chain_submit = _chain.submit_tx
        original_txb_submit = _txb.submit_tx
        original_chain_rpc = _chain.ogmios_rpc

        try:
            _chain.ogmios_rpc = fake_rpc
            _chain.submit_tx = fake_real_submit
            _txb.submit_tx = fake_real_submit

            # Act
            live._install_plutus_trace_preflight()

            # Should NOT raise — fall through expected
            result = _chain.submit_tx(fake_tx_bytes)

        finally:
            _chain.submit_tx = original_chain_submit
            _txb.submit_tx = original_txb_submit
            _chain.ogmios_rpc = original_chain_rpc

        # Assert — real submit was called with the original bytes
        fake_real_submit.assert_called_once_with(fake_tx_bytes)

        # Assert — return value from real submit is propagated
        assert result == {"txHash": "aabbcc"}
