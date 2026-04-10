"""
Constants for the Module 3 Reputation Staking SDK.

Network parameters, tier thresholds, token prefixes, and well-known hashes.
"""

# ── Vector Testnet Network Parameters ────────────────────────────────────────

SYSTEM_START_UNIX_S = 1752057484  # 2025-07-09T10:38:04Z
SLOT_LENGTH_S = 1  # 1 second per slot

# ── AP3X Currency ────────────────────────────────────────────────────────────

AP3X_DECIMALS = 6
DFM_PER_AP3X = 1_000_000  # 1 AP3X = 1,000,000 DFM (like lovelace)

# ── Reputation Tier Thresholds ───────────────────────────────────────────────
# On-chain scoring.ak uses whole AP3X units for comparison.
# These are in AP3X (whole units), matching the on-chain logic exactly.

TIER_NOVICE_AP3X = 1         # 1-99 AP3X
TIER_ESTABLISHED_AP3X = 100  # 100-499 AP3X
TIER_TRUSTED_AP3X = 500      # 500-1,999 AP3X
TIER_ELITE_AP3X = 2_000      # 2,000+ AP3X

# ── Token Name Prefixes ─────────────────────────────────────────────────────

STAKE_PREFIX = b"rstk_"
ENDORSEMENT_PREFIX = b"rend_"
CHALLENGE_PREFIX = b"rchl_"
HISTORY_BONUS_PREFIX = b"hbonus_"
GENESIS_BONUS_PREFIX = b"genesis_"

# ── Token Name Slice Lengths ────────────────────────────────────────────────
# prefix + hash_slice = max 32 bytes (Cardano token name limit)

STAKE_SLICE_LEN = 27         # 5 + 27 = 32
ENDORSEMENT_SLICE_LEN = 27   # 5 + 27 = 32
CHALLENGE_SLICE_LEN = 24     # 5 + 24 = 29
HISTORY_BONUS_SLICE_LEN = 24 # 7 + 24 = 31 (hbonus_ is 7 bytes)
GENESIS_BONUS_SLICE_LEN = 24 # 8 + 24 = 32 (genesis_ is 8 bytes)

# ── Docker Defaults ──────────────────────────────────────────────────────────

DOCKER_CONTAINER = "vector-public-testnet-tools-10_1_4-vector-relay-1"
DOCKER_SOCKET_PATH = "ipc/node.socket"
NETWORK_FLAG = "--mainnet"  # Vector testnet uses mainnet network magic
DEV_WALLET_DIR = "/tmp/m3dev"

# ── Well-Known Script Hashes ────────────────────────────────────────────────

REGISTRY_POLICY_ID = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
TREASURY_SCRIPT_HASH = "ab1aad52c4774e5da9f2c0fa1a4d07220a0bdd57ee3dce9be860dac6"
PARAMS_HOLDER_HASH = "f98f1dace1ac805615ccc0357b4ecb363a43b947fc99f1a661850867"

# ── Ogmios / Remote Chain ───────────────────────────────────────────────────

OGMIOS_URL = "https://ogmios.vector.testnet.apexfusion.org"
OGMIOS_HOST = "ogmios.vector.testnet.apexfusion.org"
OGMIOS_PORT = 443
OGMIOS_SECURE = True
TX_SUBMIT_URL = "https://submit.vector.testnet.apexfusion.org/api/submit/tx"

# ── Transaction Timing ───────────────────────────────────────────────────────

TX_WAIT_SECONDS = 25  # Default wait between transactions for confirmation
