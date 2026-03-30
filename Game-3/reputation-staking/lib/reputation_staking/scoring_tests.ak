/// Tests for scoring module.

use reputation_staking/scoring.{get_tier, meets_tier}
use reputation_staking/types.{Elite, Established, Novice, Trusted, Unverified}

test get_tier_unverified() {
  get_tier(0) == Unverified
}

test get_tier_negative() {
  get_tier(-5) == Unverified
}

test get_tier_novice_low() {
  get_tier(1) == Novice
}

test get_tier_novice_high() {
  get_tier(99) == Novice
}

test get_tier_established_low() {
  get_tier(100) == Established
}

test get_tier_established_high() {
  get_tier(499) == Established
}

test get_tier_trusted_low() {
  get_tier(500) == Trusted
}

test get_tier_trusted_high() {
  get_tier(1999) == Trusted
}

test get_tier_elite() {
  get_tier(2000) == Elite
}

test get_tier_elite_high() {
  get_tier(100_000) == Elite
}

test meets_tier_same() {
  meets_tier(Novice, Novice)
}

test meets_tier_higher() {
  meets_tier(Elite, Novice)
}

test meets_tier_lower() {
  !meets_tier(Novice, Elite)
}

test meets_tier_unverified_passes_unverified() {
  meets_tier(Unverified, Unverified)
}

test meets_tier_unverified_fails_novice() {
  !meets_tier(Unverified, Novice)
}
