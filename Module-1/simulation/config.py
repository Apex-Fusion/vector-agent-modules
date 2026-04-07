"""
Simulation Configuration — Module 1 Adversarial Auditing
"""
from dataclasses import dataclass, field
import os
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════
# NETWORK
# ═══════════════════════════════════════════════════════════════════════

OGMIOS_URL = "https://ogmios.vector.testnet.apexfusion.org"
TX_SUBMIT_URL = "https://submit.vector.testnet.apexfusion.org/api/submit/tx"
SYSTEM_START_UNIX = 1752057484

# Vector uses mainnet addressing
from pycardano import Network
NETWORK = Network.MAINNET

# ═══════════════════════════════════════════════════════════════════════
# ON-CHAIN REFERENCES
# ═══════════════════════════════════════════════════════════════════════

REGISTRY_POLICY = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
REGISTRY_ADDR = "addr1wxlp5z3fztdpsp6ha57dvx6khw82kqvgcxwu8s8rjykjcqghprf42"
AP3X_POLICY_ID = "cb20555235cc1630cba8f36027b145bea0d928131431e20854e57609"
AP3X_ASSET_NAME = "417065784167656e747354657374"  # "ApexAgentsTest"

# ═══════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════

WORKSPACE = Path(os.environ.get("APEX_WORKSPACE", "."))
TESTNET_DIR = WORKSPACE / "testnet"
SIM_DIR = WORKSPACE / "simulation"
RESULTS_DIR = WORKSPACE / "simulation-results"
CONTRACTS_DIR = WORKSPACE / "contracts"
BLUEPRINT_PATH = CONTRACTS_DIR / "plutus.json"

AIKEN_BIN = os.environ.get("AIKEN_BIN", "aiken")
WALLET_SKEY = str(TESTNET_DIR / "wallet.skey")

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
