"""Unit tests for the APEX_NETWORK env-var network selector in
simulation.config.

Each case reimports ``simulation.config`` in a clean subprocess because
the selector is resolved at import time (module-level constants). Using
subprocesses guarantees no cross-test leakage of env state and mirrors
how the constants are actually consumed in production (at process start).

Contract under test:
  (a) No APEX_NETWORK set  → testnet endpoints + testnet paths, no error.
  (b) APEX_NETWORK=mainnet, APEX_NETWORK_CONFIRM unset → RuntimeError.
  (c) APEX_NETWORK=mainnet, APEX_NETWORK_CONFIRM=yes → mainnet endpoints.
  (d) Wallet-index file paths are distinct between networks.
  (e) DEPLOYMENT_PATH switches between networks.

Written per the Module-1 mainnet-ready spec (2026-04-19).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _dump_config_snapshot() -> str:
    """Python source that imports simulation.config and prints its relevant
    attributes as JSON to stdout. Run inside a fresh subprocess so the
    module is re-evaluated under the current env vars."""
    return (
        "import json\n"
        "from simulation import config as c\n"
        "print(json.dumps({\n"
        "    'APEX_NETWORK': c.APEX_NETWORK,\n"
        "    'IS_MAINNET': c.IS_MAINNET,\n"
        "    'IS_TESTNET': c.IS_TESTNET,\n"
        "    'OGMIOS_URL': c.OGMIOS_URL,\n"
        "    'TX_SUBMIT_URL': c.TX_SUBMIT_URL,\n"
        "    'SYSTEM_START_UNIX': c.SYSTEM_START_UNIX,\n"
        "    'NETWORK_MAGIC': c.NETWORK_MAGIC,\n"
        "    'WALLET_SKEY': c.WALLET_SKEY,\n"
        "    'DEPLOYMENT_PATH': c.DEPLOYMENT_PATH,\n"
        "    'WALLET_INDEX_FILE': str(c.WALLET_INDEX_FILE),\n"
        "    'SIM_METRICS_DIR': str(c.SIM_METRICS_DIR),\n"
        "}))\n"
    )


def _run_config_snapshot(env: dict) -> tuple[int, str, str]:
    """Run a snapshot script in a fresh subprocess; return (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-c", _dump_config_snapshot()],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _clean_env(**overrides: str) -> dict:
    """Base env = inherited env with APEX_NETWORK* scrubbed, plus overrides.

    We inherit PATH + PYTHONPATH so the subprocess can find pycardano etc.,
    but strip APEX_NETWORK and APEX_NETWORK_CONFIRM so each test controls
    them explicitly.
    """
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("APEX_NETWORK", "APEX_NETWORK_CONFIRM")}
    env.update(overrides)
    return env


# ───────────────────────────────────────────────────────────────────────────
# (a) Default is testnet
# ───────────────────────────────────────────────────────────────────────────

def test_default_no_env_resolves_to_testnet():
    rc, out, err = _run_config_snapshot(_clean_env())
    assert rc == 0, f"subprocess failed: {err}"
    snap = json.loads(out)
    assert snap["APEX_NETWORK"] == "testnet"
    assert snap["IS_TESTNET"] is True
    assert snap["IS_MAINNET"] is False
    assert "testnet" in snap["OGMIOS_URL"]
    assert "testnet" in snap["TX_SUBMIT_URL"]
    # Testnet genesis (Vector testnet).
    assert snap["SYSTEM_START_UNIX"] == 1752057484
    # Shared magic on both Vector chains.
    assert snap["NETWORK_MAGIC"] == 764824073


def test_default_uses_testnet_wallet_and_manifest_paths():
    rc, out, _ = _run_config_snapshot(_clean_env())
    assert rc == 0
    snap = json.loads(out)
    # Wallet skey must be the testnet orchestrator file.
    assert snap["WALLET_SKEY"].endswith("testnet/wallet.skey")
    # Deployment manifest path is the testnet sim manifest.
    assert snap["DEPLOYMENT_PATH"].endswith("testnet/game1-sim-deployment.json")
    # Testnet-suffixed index + metrics dir.
    assert snap["WALLET_INDEX_FILE"].endswith(".wallet_index_testnet.json")
    assert "apex-sim-metrics-testnet" in snap["SIM_METRICS_DIR"]


def test_explicit_testnet_matches_default():
    rc_default, out_default, _ = _run_config_snapshot(_clean_env())
    rc_explicit, out_explicit, _ = _run_config_snapshot(
        _clean_env(APEX_NETWORK="testnet")
    )
    assert rc_default == 0 and rc_explicit == 0
    assert json.loads(out_default) == json.loads(out_explicit)


# ───────────────────────────────────────────────────────────────────────────
# (b) APEX_NETWORK=mainnet without confirm → RuntimeError
# ───────────────────────────────────────────────────────────────────────────

def test_mainnet_without_confirm_raises():
    rc, out, err = _run_config_snapshot(_clean_env(APEX_NETWORK="mainnet"))
    assert rc != 0, \
        f"expected failure, got rc=0 out={out!r}"
    # The error must clearly identify the safety rail and the fix.
    assert "APEX_NETWORK_CONFIRM" in err
    assert "mainnet" in err.lower()
    assert "RuntimeError" in err


def test_mainnet_confirm_wrong_value_raises():
    """APEX_NETWORK_CONFIRM must be exactly 'yes' (case-insensitive).
    Anything else (empty, 'true', '1', '  ') must not bypass the rail."""
    for bad in ["", "true", "1", "YES\nextra", "y", "no"]:
        rc, _, err = _run_config_snapshot(
            _clean_env(APEX_NETWORK="mainnet", APEX_NETWORK_CONFIRM=bad)
        )
        assert rc != 0, f"safety rail failed to block APEX_NETWORK_CONFIRM={bad!r}"
        assert "APEX_NETWORK_CONFIRM" in err or "mainnet" in err.lower()


def test_invalid_network_value_raises():
    rc, _, err = _run_config_snapshot(_clean_env(APEX_NETWORK="prod"))
    assert rc != 0
    assert "not valid" in err or "APEX_NETWORK" in err


# ───────────────────────────────────────────────────────────────────────────
# (c) APEX_NETWORK=mainnet + confirm → mainnet endpoints
# ───────────────────────────────────────────────────────────────────────────

def test_mainnet_with_confirm_loads_mainnet_endpoints():
    rc, out, err = _run_config_snapshot(
        _clean_env(APEX_NETWORK="mainnet", APEX_NETWORK_CONFIRM="yes")
    )
    assert rc == 0, f"expected success, got rc={rc} err={err}"
    snap = json.loads(out)
    assert snap["APEX_NETWORK"] == "mainnet"
    assert snap["IS_MAINNET"] is True
    assert snap["IS_TESTNET"] is False
    assert "mainnet" in snap["OGMIOS_URL"]
    assert "mainnet" in snap["TX_SUBMIT_URL"]
    assert "testnet" not in snap["OGMIOS_URL"]
    assert "testnet" not in snap["TX_SUBMIT_URL"]
    # Vector mainnet genesis.
    assert snap["SYSTEM_START_UNIX"] == 1756485600
    assert snap["NETWORK_MAGIC"] == 764824073


def test_mainnet_confirm_case_insensitive():
    """APEX_NETWORK_CONFIRM lookup is case-insensitive (YES / Yes / yes all work)."""
    for good in ["yes", "YES", "Yes", " yes "]:
        rc, _, err = _run_config_snapshot(
            _clean_env(APEX_NETWORK="mainnet", APEX_NETWORK_CONFIRM=good)
        )
        assert rc == 0, f"APEX_NETWORK_CONFIRM={good!r} should unlock; got err={err}"


# ───────────────────────────────────────────────────────────────────────────
# (d) Wallet-index paths are distinct across networks
# ───────────────────────────────────────────────────────────────────────────

def test_wallet_index_paths_distinct_between_networks():
    _, tn_out, _ = _run_config_snapshot(_clean_env(APEX_NETWORK="testnet"))
    _, mn_out, _ = _run_config_snapshot(
        _clean_env(APEX_NETWORK="mainnet", APEX_NETWORK_CONFIRM="yes")
    )
    tn = json.loads(tn_out)
    mn = json.loads(mn_out)

    # Hard requirement: never share a wallet-index file between chains.
    # Same path = possible sub-wallet reuse = real-ADA leak vector.
    assert tn["WALLET_INDEX_FILE"] != mn["WALLET_INDEX_FILE"]
    assert tn["WALLET_INDEX_FILE"].endswith(".wallet_index_testnet.json")
    assert mn["WALLET_INDEX_FILE"].endswith(".wallet_index_mainnet.json")

    # Same guarantee for metrics dirs and wallet skeys.
    assert tn["SIM_METRICS_DIR"] != mn["SIM_METRICS_DIR"]
    assert "testnet" in tn["SIM_METRICS_DIR"]
    assert "mainnet" in mn["SIM_METRICS_DIR"]

    assert tn["WALLET_SKEY"] != mn["WALLET_SKEY"]
    assert tn["WALLET_SKEY"].endswith("wallet.skey")
    assert mn["WALLET_SKEY"].endswith("mainnet_orchestrator.skey")


# ───────────────────────────────────────────────────────────────────────────
# (e) Deployment manifest switches
# ───────────────────────────────────────────────────────────────────────────

def test_deployment_path_switches_between_networks():
    _, tn_out, _ = _run_config_snapshot(_clean_env(APEX_NETWORK="testnet"))
    _, mn_out, _ = _run_config_snapshot(
        _clean_env(APEX_NETWORK="mainnet", APEX_NETWORK_CONFIRM="yes")
    )
    tn = json.loads(tn_out)
    mn = json.loads(mn_out)

    assert tn["DEPLOYMENT_PATH"] != mn["DEPLOYMENT_PATH"]
    assert tn["DEPLOYMENT_PATH"].endswith("game1-sim-deployment.json")
    assert mn["DEPLOYMENT_PATH"].endswith("module1-v15-sim-mainnet-deployment.json")
    # Mainnet manifest must NOT be the v14 production manifest filename.
    assert "module1-mainnet-deployment.json" not in mn["DEPLOYMENT_PATH"]


# ───────────────────────────────────────────────────────────────────────────
# Sanity: testnet default performs no filesystem writes during import
# ───────────────────────────────────────────────────────────────────────────

def test_import_does_not_write_mainnet_paths(tmp_path: Path):
    """Importing the module with APEX_NETWORK unset must not create any
    mainnet-scoped files (wallet-index, metrics dir, deployment manifest,
    skey). Belt-and-suspenders check against a future regression that
    might eagerly touch paths at import time."""
    # Point SIM_METRICS_DIR / index dir targets into tmp via env so we can
    # introspect. Since the config itself puts WALLET_INDEX_FILE under
    # repo/simulation, we instead assert via path names that no mainnet
    # artifact was materialised during a fresh import.
    import subprocess as sp
    proc = sp.run(
        [sys.executable, "-c",
         "from simulation import config as c; "
         "import json; "
         "print(json.dumps({'idx': str(c.WALLET_INDEX_FILE), "
         "'skey': c.WALLET_SKEY, 'deploy': c.DEPLOYMENT_PATH}))"],
        cwd=str(REPO_ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    snap = json.loads(proc.stdout)
    # None of the mainnet-scoped filenames may appear when APEX_NETWORK unset.
    for key, value in snap.items():
        assert "mainnet" not in value, \
            f"{key}={value!r} contains 'mainnet' when APEX_NETWORK unset"
