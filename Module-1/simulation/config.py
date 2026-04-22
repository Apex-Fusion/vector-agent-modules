"""
Simulation Configuration — Module 1 Adversarial Auditing

NETWORK SELECTOR
----------------
Set ``APEX_NETWORK`` to ``testnet`` (default) or ``mainnet`` to pick which
Vector chain the simulation targets. ALL network-scoped artifacts (Ogmios/
submit endpoints, master orchestrator skey, deployment manifest path,
wallet-index file, metrics dir) derive from this single switch so that a
testnet run NEVER touches mainnet state and vice versa.

Mainnet requires an explicit ``APEX_NETWORK_CONFIRM=yes`` safety-rail env
var: importing this module with ``APEX_NETWORK=mainnet`` unset-confirm
raises ``RuntimeError``. This prevents accidental mainnet invocation from
a misconfigured shell.

Hardcoded paths (``wallet.skey`` / ``game1-sim-deployment.json``) have
been purged from every callsite — use the constants defined below
(``WALLET_SKEY``, ``DEPLOYMENT_PATH``, ``WALLET_INDEX_FILE``,
``SIM_METRICS_DIR``) instead.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path

from pycardano import Network


# ═══════════════════════════════════════════════════════════════════════
# NETWORK SELECTOR
# ═══════════════════════════════════════════════════════════════════════

_VALID_NETWORKS = ("testnet", "mainnet")

# Resolve at import time; callers that want to switch networks must set
# the env var BEFORE importing this module (or any module that transitively
# imports it).
APEX_NETWORK = os.environ.get("APEX_NETWORK", "testnet").strip().lower()

if APEX_NETWORK not in _VALID_NETWORKS:
    raise RuntimeError(
        f"APEX_NETWORK={APEX_NETWORK!r} is not valid. "
        f"Must be one of {_VALID_NETWORKS!r}."
    )

if APEX_NETWORK == "mainnet":
    _confirm = os.environ.get("APEX_NETWORK_CONFIRM", "").strip().lower()
    if _confirm != "yes":
        raise RuntimeError(
            "Refusing to initialize with APEX_NETWORK=mainnet without "
            "explicit confirmation. Set APEX_NETWORK_CONFIRM=yes to "
            "acknowledge that this run will touch the Vector MAINNET "
            "chain. This safety rail prevents accidental mainnet "
            "invocation from a misconfigured shell."
        )

IS_MAINNET = (APEX_NETWORK == "mainnet")
IS_TESTNET = (APEX_NETWORK == "testnet")


# ═══════════════════════════════════════════════════════════════════════
# NETWORK ENDPOINTS + GENESIS CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

# Vector uses Cardano "mainnet" addressing (0x61 prefix) on BOTH chains —
# this is a Vector-specific quirk and is independent of APEX_NETWORK. The
# Network enum here refers to address encoding, not which chain we talk to.
NETWORK = Network.MAINNET

# Both Vector chains share the same protocol-layer network magic
# (764824073). Kept as a constant for clarity at call-sites that need to
# construct genesis params.
NETWORK_MAGIC = 764824073

if IS_MAINNET:
    OGMIOS_URL = "https://ogmios.vector.mainnet.apexfusion.org"
    TX_SUBMIT_URL = "https://submit.vector.mainnet.apexfusion.org/api/submit/tx"
    # Vector mainnet genesis: 2025-08-29 16:40:00 UTC
    SYSTEM_START_UNIX = 1756485600
else:
    OGMIOS_URL = "https://ogmios.vector.testnet.apexfusion.org"
    TX_SUBMIT_URL = "https://submit.vector.testnet.apexfusion.org/api/submit/tx"
    # Vector testnet genesis (unchanged from v14)
    SYSTEM_START_UNIX = 1752057484


# ═══════════════════════════════════════════════════════════════════════
# ON-CHAIN REFERENCES (identical on both Vector chains)
# ═══════════════════════════════════════════════════════════════════════

REGISTRY_POLICY = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
REGISTRY_ADDR = "addr1wxlp5z3fztdpsp6ha57dvx6khw82kqvgcxwu8s8rjykjcqghprf42"
AP3X_POLICY_ID = "cb20555235cc1630cba8f36027b145bea0d928131431e20854e57609"
# Legacy — Path A only; Path B (v13+) uses base AP3X held in .coin field, no asset name
# needed. Constant retained here for backward compatibility with simulation code
# (wallet_factory.py, tx_builder.py, world_state.py) that has not yet been migrated
# to Path B. Do not use in new code — stake outputs should reference value.coin directly.
AP3X_ASSET_NAME = "417065784167656e747354657374"  # "ApexAgentsTest" — Path A legacy


# ═══════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════

WORKSPACE = Path(os.environ.get("APEX_WORKSPACE", "."))
TESTNET_DIR = WORKSPACE / "testnet"  # legacy name; Chuck's convention keeps
                                     # mainnet deploy artifacts in this dir
                                     # alongside testnet ones.
SIM_DIR = WORKSPACE / "simulation"
RESULTS_DIR = WORKSPACE / "simulation-results"
CONTRACTS_DIR = WORKSPACE / "contracts"
BLUEPRINT_PATH = CONTRACTS_DIR / "plutus.json"

# The wallet index is sim-code state (not workspace data) — it tracks the
# monotonic derivation counter across every scenario ever run. It MUST live
# next to the sim code regardless of APEX_WORKSPACE, otherwise scripts
# launched with a different workspace re-allocate index 0 on top of
# already-funded on-chain UTxOs. Pinning to __file__.parent guarantees a
# single source of truth.
_SIM_PACKAGE_DIR = Path(__file__).resolve().parent

AIKEN_BIN = os.environ.get("AIKEN_BIN", "aiken")

# Network-scoped artifacts. These are the ONLY source of truth — all
# callsites must import these constants rather than reconstructing paths
# from strings. This is how we guarantee no testnet/mainnet bleed.
if IS_MAINNET:
    WALLET_SKEY = str(TESTNET_DIR / "mainnet_orchestrator.skey")
    DEPLOYMENT_PATH = str(TESTNET_DIR / "module1-v15-sim-mainnet-deployment.json")
    WALLET_INDEX_FILE = _SIM_PACKAGE_DIR / ".wallet_index_mainnet.json"
    SIM_METRICS_DIR = Path("/tmp/apex-sim-metrics-mainnet")
else:
    WALLET_SKEY = str(TESTNET_DIR / "wallet.skey")
    DEPLOYMENT_PATH = str(TESTNET_DIR / "game1-sim-deployment.json")
    WALLET_INDEX_FILE = _SIM_PACKAGE_DIR / ".wallet_index_testnet.json"
    SIM_METRICS_DIR = Path("/tmp/apex-sim-metrics-testnet")


# ═══════════════════════════════════════════════════════════════════════
# PROTOCOL PARAMS (for simulation — shortened windows)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class GameParams:
    """On-chain ProtocolParams for Module 1."""
    min_claim_stake: int = 50_000_000       # 50 AP3X
    min_challenge_window: int = 1_800_000   # 30 min
    max_challenge_window: int = 64_800_000  # 18 hours
    jury_size: int = 5
    min_juror_bond: int = 25_000_000        # 25 AP3X
    jury_fee_rate: int = 1000               # 10% (basis points)
    selection_delay: int = 30_000           # 30 seconds
    resolution_deadline: int = 900_000      # 15 min (test), prod=5_400_000
    juror_slash_rate: int = 1000            # 10%
    min_agent_age: int = 21_600_000         # 6 hours
    max_concurrent_cases: int = 5
    min_jury_pool_size: int = 10
    min_jury_pool_total: int = 250_000_000  # 250 AP3X
    oracle_active: bool = False             # Phase 1.1 = jury mode
    commit_window: int = 300_000            # 5 min (test), prod=3_600_000
    reveal_window: int = 300_000            # 5 min (test), prod=1_800_000
    cleanup_buffer: int = 120_000           # 2 min


# ═══════════════════════════════════════════════════════════════════════
# SIMULATION PARAMS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SimConfig:
    """Simulation-level configuration."""
    n_agents: int = 50
    n_jurors: int = 20                      # Jurors from the agent pool
    n_epochs: int = 100
    epoch_interval_seconds: int = 60
    claim_rate_per_agent: float = 0.1       # Poisson λ per epoch
    initial_ap3x_per_agent: int = 1_000_000_000  # 1000 AP3X
    oracle_accuracy: float = 0.95
    oracle_delay_epochs: int = 3
    random_seed: int = 42
    challenge_window_ms: int = 1_800_000    # 30 min
    game_params: GameParams = field(default_factory=GameParams)
