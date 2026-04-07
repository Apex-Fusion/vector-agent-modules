/// Reputation scoring helpers for Module 3: Reputation Staking.
///
/// Provides on-chain tier computation for tier-gating in other contracts.
/// The full reputation score is computed off-chain by the indexer, but
/// tier boundaries are useful on-chain for access control.
///
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Section 5.4.

use reputation_staking/types.{
  Elite, Established, Novice, ReputationTier, Trusted, Unverified,
}

/// Compute the reputation tier from a raw score.
///
/// | Tier        | Score Range     |
/// |-------------|-----------------|
/// | Unverified  | 0               |
/// | Novice      | 1–99 AP3X       |
/// | Established | 100–499 AP3X    |
/// | Trusted     | 500–1,999 AP3X  |
/// | Elite       | 2,000+ AP3X     |
pub fn get_tier(score: Int) -> ReputationTier {
  if score >= 2_000 {
    Elite
  } else if score >= 500 {
    Trusted
  } else if score >= 100 {
    Established
  } else if score >= 1 {
    Novice
  } else {
    Unverified
  }
}

/// Check if a tier meets a minimum required tier.
/// Tier ordering: Unverified < Novice < Established < Trusted < Elite
pub fn meets_tier(actual: ReputationTier, required: ReputationTier) -> Bool {
  tier_to_int(actual) >= tier_to_int(required)
}

fn tier_to_int(tier: ReputationTier) -> Int {
  when tier is {
    Unverified -> 0
    Novice -> 1
    Established -> 2
    Trusted -> 3
    Elite -> 4
  }
}
