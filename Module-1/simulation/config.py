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
from typing import Any, Mapping

from pycardano import Network

from simulation.identity_party import AuditingConfig, VerdictPolicy


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
# IDENTITY DEPLOYMENT VALUES (Part 2 §3.8, §7)
# ═══════════════════════════════════════════════════════════════════════
#
# The deployment manifest (DEPLOYMENT_PATH) holds the identity values next
# to the Module-1 references. There are no defaults: a missing value stops
# the simulation with IdentityConfigError. The values come from the
# deployments, in this order: identity registry, Module-1, Module-3,
# Module-6. The keys use the format of deploy/identity/config.json and of
# the plan.json that scripts/identity_deploy.py writes:
#
#   "identity": {
#     "identity_hash": "<56 hex>",       identity script hash = policy ID
#     "identity_ref":  "<txid>#<index>", reference script of `identity`
#     "config_policy": "<56 hex>",       policy ID of `config_seal`
#     "config_holder": "<56 hex>"        script hash of `config_holder`
#   },
#   "hashes": { ..., "identity_party": "<56 hex>" },   M1 (plan.json)
#   "identity_party_ref": "<txid>#<index>",  reference script of identity_party
#   "module1": { "oracle_vkh": "<56 hex>", "refs_token_policy": "<56 hex>" },
#   "verdict_policy": {
#     "auditor_wins": {"action": "suspend" | "revoke", "duration_ms": int} | null,
#     "claimer_wins": {"action": "suspend" | "revoke", "duration_ms": int} | null
#   }

#: The order of the deployments that give the identity values.
IDENTITY_DEPLOYMENT_ORDER = "identity registry, Module-1, Module-3, Module-6"

#: The manifest keys of the identity values, as dotted JSON paths.
IDENTITY_MANIFEST_KEYS = (
    "identity.identity_hash",
    "identity.identity_ref",
    "identity.config_policy",
    "identity.config_holder",
    "hashes.identity_party",
    "identity_party_ref",
    "module1.oracle_vkh",
    "module1.refs_token_policy",
    "verdict_policy",
)


class IdentityConfigError(ValueError):
    """The deployment manifest has no identity value, or a value is not
    valid."""


@dataclass(frozen=True)
class IdentityDeployment:
    """The identity values of a Module-1 deployment."""

    identity_hash: bytes          # identity script hash (= policy ID)
    identity_ref: str             # "<txid>#<index>" of the identity reference script
    config_policy: bytes          # config_seal policy ID
    config_holder: bytes          # config_holder script hash (address CA)
    identity_party_hash: bytes    # M1: hash of identity_party
    identity_party_ref: str       # "<txid>#<index>" of the identity_party reference script
    oracle_vkh: bytes
    refs_token_policy: bytes
    verdict_policy: VerdictPolicy

    @property
    def auditing_config(self) -> AuditingConfig:
        """The AuditingConfig parameter of the Module-1 validators."""
        return AuditingConfig(
            identity_hash=self.identity_hash,
            oracle_vkh=self.oracle_vkh,
            refs_token_policy=self.refs_token_policy,
            config_policy=self.config_policy,
            config_holder=self.config_holder,
            verdict_policy=self.verdict_policy,
        )


def _missing(path: str) -> IdentityConfigError:
    return IdentityConfigError(
        f"the deployment manifest has no `{path}`. The identity values come "
        f"from the deployments, in this order: {IDENTITY_DEPLOYMENT_ORDER}. "
        f"Add `{path}` to the manifest (see simulation/config.py, identity "
        f"deployment values) before you run the simulation."
    )


def _manifest_value(manifest: Mapping[str, Any], path: str) -> Any:
    node: Any = manifest
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise _missing(path)
        node = node[part]
    if node is None or node == "":
        raise _missing(path)
    return node


def _hash28(manifest: Mapping[str, Any], path: str) -> bytes:
    value = _manifest_value(manifest, path)
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError):
        raw = b""
    if len(raw) != 28:
        raise IdentityConfigError(f"`{path}` must be 28 bytes (56 hex characters)")
    if raw == bytes(28):
        raise IdentityConfigError(
            f"`{path}` is the all-zero placeholder of deploy/identity/config.json; "
            f"use the value of the deployment"
        )
    return raw


def _out_ref(manifest: Mapping[str, Any], path: str) -> str:
    value = _manifest_value(manifest, path)
    txid, sep, index = str(value).partition("#")
    try:
        valid = sep == "#" and len(bytes.fromhex(txid)) == 32 and int(index) >= 0
    except ValueError:
        valid = False
    if not valid:
        raise IdentityConfigError(f"`{path}` must be \"<64 hex txid>#<index>\"")
    return f"{txid.lower()}#{int(index)}"


def load_identity_deployment(manifest: Mapping[str, Any]) -> IdentityDeployment:
    """Read the identity values of a deployment manifest, in the order of
    IDENTITY_MANIFEST_KEYS. Raises IdentityConfigError when a value is
    missing or not valid; the message names the deployment order."""
    identity_hash = _hash28(manifest, "identity.identity_hash")
    identity_ref = _out_ref(manifest, "identity.identity_ref")
    config_policy = _hash28(manifest, "identity.config_policy")
    config_holder = _hash28(manifest, "identity.config_holder")
    identity_party_hash = _hash28(manifest, "hashes.identity_party")
    identity_party_ref = _out_ref(manifest, "identity_party_ref")
    oracle_vkh = _hash28(manifest, "module1.oracle_vkh")
    refs_token_policy = _hash28(manifest, "module1.refs_token_policy")
    try:
        verdict_policy = VerdictPolicy.from_json(
            _manifest_value(manifest, "verdict_policy")
        )
    except IdentityConfigError:
        raise
    except ValueError as exc:
        raise IdentityConfigError(str(exc)) from exc
    return IdentityDeployment(
        identity_hash=identity_hash,
        identity_ref=identity_ref,
        config_policy=config_policy,
        config_holder=config_holder,
        identity_party_hash=identity_party_hash,
        identity_party_ref=identity_party_ref,
        oracle_vkh=oracle_vkh,
        refs_token_policy=refs_token_policy,
        verdict_policy=verdict_policy,
    )


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
