/// Protocol parameters for Module 1: Adversarial Auditing
///
/// All parameters are governance-adjustable (Module 6 pathway).
/// Stored in a reference UTXO at a governance-controlled address.
/// Validators read params via reference inputs — no redeployment needed.
///
/// ═══ v4 FIX: ALL TIME FIELDS IN POSIX MILLISECONDS ═══
/// Plutus V3 ScriptContext uses POSIXTime in milliseconds.
/// All time-related parameters MUST be in milliseconds to allow direct
/// addition with submitted_at / challenged_at fields from ScriptContext.
/// v3 BUG (CRITICAL-01): challenge_window was in seconds, submitted_at in ms.
/// Adding them directly made a 30-minute window last 1.8 seconds.
/// v4 FIX: store everything in ms. No conversion needed in validators.
///
/// VRF research update (VRF research 2026-03-23):
///   SELECTION_DELAY raised from spec's 10s to 30s minimum.
///   Rationale: 10s is too short; a colluding block producer
///   monitoring pending challenges could grind the slot window.
///   30s (~30_000 ms) meaningfully raises the attack cost.

// ══════════════════════════════════════════════════════════════════════
// PROTOCOL PARAMS TYPE
// ══════════════════════════════════════════════════════════════════════

/// On-chain protocol parameters.
/// CURRENT IMPLEMENTATION: baked in as a compile-time validator parameter.
/// Phase 1.1 migration: move to a governance reference UTXO (see G1-ZR-13).
/// Until then, changing params requires redeployment of all three validators.
/// DO NOT assume reference input reading is implemented — it is not yet.
pub type ProtocolParams {
  /// Minimum AP3X stake required to submit a claim (DFM units, 6 decimals)
  /// Initial: 50_000_000 (50 AP3X)
  min_claim_stake: Int,
  /// Minimum challenge window in POSIX milliseconds (~30 minutes)
  /// Initial: 1_800_000 (30 min × 60 s × 1000 ms)
  min_challenge_window: Int,
  /// Maximum challenge window in POSIX milliseconds (~18 hours)
  /// Initial: 64_800_000 (18 h × 3600 s × 1000 ms)
  max_challenge_window: Int,
  /// Number of jurors per case (must be odd for majority)
  /// Initial: 5
  jury_size: Int,
  /// Minimum AP3X bond for jurors (DFM units, 6 decimals)
  /// Initial: 25_000_000 (25 AP3X)
  min_juror_bond: Int,
  /// Jury fee rate in basis points (1000 = 10% of loser's stake)
  /// Initial: 1000
  jury_fee_rate: Int,
  /// Delay in POSIX milliseconds before jury selection can occur.
  /// Prevents block producer grinding: the entropy slot is fixed after challenge.
  /// Initial: 30_000 (30 seconds × 1000 ms)
  /// Raised from spec's 10s per VRF research 2026-03-23.
  selection_delay: Int,
  /// POSIX milliseconds after challenge for jury to resolve (~90 minutes)
  /// Initial: 5_400_000 (90 min × 60 s × 1000 ms)
  resolution_deadline: Int,
  /// Juror slash rate in basis points for non-participation (1000 = 10%)
  /// Initial: 1000
  juror_slash_rate: Int,
  /// Minimum agent age in POSIX milliseconds (~6 hours, anti-sybil)
  /// Initial: 21_600_000 (6 h × 3600 s × 1000 ms)
  min_agent_age: Int,
  /// Maximum concurrent cases per juror
  /// Initial: 5
  max_concurrent_cases: Int,
  /// Juror count threshold to activate jury mode (deactivate oracle)
  /// Initial: 10
  min_jury_pool_size: Int,
  /// Minimum total juror bonds in pool for activation (DFM units)
  /// Initial: 250_000_000 (250 AP3X)
  min_jury_pool_total: Int,
  /// Whether Foundation oracle mode is active.
  /// True  = Phase 1.0: Foundation resolves challenges
  /// False = Phase 1.1+: jury resolves challenges
  oracle_active: Bool,
  /// Commit window in POSIX milliseconds after challenged_at.
  /// Jurors must submit vote commitments before challenged_at + commit_window.
  /// Initial: 3_600_000 (1 hour)
  commit_window: Int,
  /// Reveal window in POSIX milliseconds after commit_deadline.
  /// Jurors must reveal votes before challenged_at + commit_window + reveal_window.
  /// Initial: 1_800_000 (30 minutes)
  reveal_window: Int,
  /// fix: RT-V5 — Buffer in POSIX milliseconds after resolution_deadline before
  /// CleanupResolved is allowed. Gives jurors time to call DistributeRewards.
  /// Initial: 1_800_000 (30 minutes)
  cleanup_buffer: Int,
}

// ══════════════════════════════════════════════════════════════════════
// DEFAULT VALUES (used in tests; on-chain reads from reference UTXO)
// ══════════════════════════════════════════════════════════════════════

pub const default_min_claim_stake: Int = 50_000_000

/// 30 minutes in POSIX milliseconds (30 × 60 × 1000)
pub const default_min_challenge_window: Int = 1_800_000

/// 18 hours in POSIX milliseconds (18 × 3600 × 1000)
pub const default_max_challenge_window: Int = 64_800_000

pub const default_jury_size: Int = 5

pub const default_min_juror_bond: Int = 25_000_000

pub const default_jury_fee_rate: Int = 1000

/// 30 seconds in POSIX milliseconds.
/// Per VRF research (2026-03-23): 10s is too short;
/// 30s meaningfully raises the cost of block-producer grinding.
pub const default_selection_delay: Int = 30_000

/// 90 minutes in POSIX milliseconds (90 × 60 × 1000)
pub const default_resolution_deadline: Int = 5_400_000

pub const default_juror_slash_rate: Int = 1000

/// 6 hours in POSIX milliseconds (6 × 3600 × 1000)
pub const default_min_agent_age: Int = 21_600_000

pub const default_max_concurrent_cases: Int = 5

pub const default_min_jury_pool_size: Int = 10

pub const default_min_jury_pool_total: Int = 250_000_000

/// 1 hour in POSIX milliseconds (60 × 60 × 1000)
pub const default_commit_window: Int = 3_600_000

/// 30 minutes in POSIX milliseconds (30 × 60 × 1000)
pub const default_reveal_window: Int = 1_800_000

// ══════════════════════════════════════════════════════════════════════
// PARAMETER HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Compute jury fee from loser's stake using basis points.
/// jury_fee_rate: 1000 = 10% → loser_stake * 1000 / 10000 = 10%
pub fn compute_jury_fee(loser_stake: Int, jury_fee_rate: Int) -> Int {
  loser_stake * jury_fee_rate / 10000
}

/// Compute juror slash amount from bond.
/// juror_slash_rate: 1000 = 10% → bond * 1000 / 10000 = 10%
pub fn compute_slash_amount(bond: Int, juror_slash_rate: Int) -> Int {
  bond * juror_slash_rate / 10000
}

/// Check if challenge window is within governance bounds.
pub fn challenge_window_valid(
  window: Int,
  min_window: Int,
  max_window: Int,
) -> Bool {
  window >= min_window && window <= max_window
}

/// Compute each juror's share of the jury fee.
pub fn juror_fee_share(total_jury_fee: Int, juror_count: Int) -> Int {
  if juror_count == 0 {
    0
  } else {
    total_jury_fee / juror_count
  }
}

/// Compute majority vote threshold: strictly more than half of jury_size.
/// With jury_size=5: supermajority = 3 (ceil(5/2) + 1 would be 4, but spec says 3 of 5)
/// Per spec §4.6: "If >= SUPERMAJORITY (3 of 5) agree" — i.e., (jury_size / 2) + 1
pub fn supermajority_threshold(jury_size: Int) -> Int {
  jury_size / 2 + 1
}
