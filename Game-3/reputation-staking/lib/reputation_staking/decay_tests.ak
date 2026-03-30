/// Tests for decay calculation module.

use reputation_staking/decay.{calculate_collector_fee, calculate_decay}

// ──────────────────────────────────────────────────────────────────────────────
// calculate_decay tests
// ──────────────────────────────────────────────────────────────────────────────

test decay_basic() {
  // 1000 AP3X stake, 100 basis points (1%) rate, 1 epoch inactive
  // decay = 1000 * 100 * 1 / 10_000 = 10
  calculate_decay(1000, 1, 100) == 10
}

test decay_multiple_epochs() {
  // 1000 stake, 100 bps, 5 epochs
  // decay = 1000 * 100 * 5 / 10_000 = 50
  calculate_decay(1000, 5, 100) == 50
}

test decay_capped_at_stake() {
  // 100 stake, 100 bps, 200 epochs → raw = 2000, capped at 100
  calculate_decay(100, 200, 100) == 100
}

test decay_zero_epochs() {
  calculate_decay(1000, 0, 100) == 0
}

test decay_zero_rate() {
  calculate_decay(1000, 5, 0) == 0
}

test decay_large_stake() {
  // 10_000_000 DFM (10 AP3X), 100 bps, 3 epochs
  // decay = 10_000_000 * 100 * 3 / 10_000 = 300_000
  calculate_decay(10_000_000, 3, 100) == 300_000
}

// ──────────────────────────────────────────────────────────────────────────────
// calculate_collector_fee tests
// ──────────────────────────────────────────────────────────────────────────────

test collector_fee_basic() {
  // 100 decay, 500 bps (5%)
  // fee = 100 * 500 / 10_000 = 5
  calculate_collector_fee(100, 500) == 5
}

test collector_fee_zero_decay() {
  calculate_collector_fee(0, 500) == 0
}

test collector_fee_zero_rate() {
  calculate_collector_fee(100, 0) == 0
}

test collector_fee_large() {
  // 300_000 decay, 500 bps
  // fee = 300_000 * 500 / 10_000 = 15_000
  calculate_collector_fee(300_000, 500) == 15_000
}
