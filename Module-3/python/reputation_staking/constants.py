"""
Constants for the Module 3 Reputation Staking SDK.

Network parameters, tier thresholds, token prefixes and transaction limits.

The SDK has no script hash of a deployment. The hashes and addresses of a
deployment (validators, params and treasury holders, record token policy,
identity registry, Module-1) come from its deploy state file
(reputation_staking.deployment).
"""

# ── Vector Testnet Network Parameters ────────────────────────────────────────

SYSTEM_START_UNIX_S = 1752057484  # 2025-07-09T10:38:04Z
SLOT_LENGTH_S = 1  # 1 second per slot

# Vector mainnet system start. A mainnet tool sets
# reputation_staking.utils.SYSTEM_START_UNIX_S to this value before it
# converts slots to POSIX times.
MAINNET_SYSTEM_START_UNIX_S = 1756485600  # 2025-08-29T16:40:00Z

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

# Asset name of the record token that the ProtocolParams record holds
# (shared/lib/shared/record_auth.ak)
PARAMS_TOKEN_NAME = b"params"

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

# ── Ogmios / Remote Chain ───────────────────────────────────────────────────

OGMIOS_URL = "https://ogmios.vector.testnet.apexfusion.org"
OGMIOS_HOST = "ogmios.vector.testnet.apexfusion.org"
OGMIOS_PORT = 443
OGMIOS_SECURE = True
TX_SUBMIT_URL = "https://submit.vector.testnet.apexfusion.org/api/submit/tx"

MAINNET_OGMIOS_URL = "https://ogmios.vector.mainnet.apexfusion.org"
MAINNET_SUBMIT_URL = "https://submit.vector.mainnet.apexfusion.org/api/submit/tx"

# ── Transaction Timing ───────────────────────────────────────────────────────

TX_WAIT_SECONDS = 25  # Default wait between transactions for confirmation

# Validity range of the client transactions, in slots (1 slot = 1 second on
# Vector). A transaction that reads an identity record must have
# hi - lo <= 3_600_000 ms (vector_identity.MAX_RANGE_MS), so at most 3600
# slots. The default keeps a margin: 15 minutes.
DEFAULT_VALIDITY_SLOTS = 900

# ── Transaction Outputs ──────────────────────────────────────────────────────

# The smallest payout output that the client makes (treasury fees and other
# payouts below this value get this value; the wallet pays the difference).
MIN_PAYOUT_LOVELACE = 1_000_000

# Lovelace in a new history bonus output
HISTORY_BONUS_OUTPUT_LOVELACE = 2_000_000
