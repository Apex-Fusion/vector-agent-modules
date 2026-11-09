//// Time bounds of a transaction (SPEC §1 `lo`, `hi`; §6 time rules).
////
//// `lo` and `hi` are the values of the finite bounds of the validity range.
//// The inclusive flags are ignored. A function that reads a bound fails when
//// that bound is infinite.

use aiken/interval.{Finite, Interval, IntervalBound}
use cardano/transaction.{ValidityRange}
use vector_identity.{max_range_ms}

/// The finite bounds of a validity range.
pub type Window {
  lo: Int,
  hi: Int,
}

/// `lo`. Fails when the lower bound is infinite.
pub fn lower(range: ValidityRange) -> Int {
  expect Finite(lo) = range.lower_bound.bound_type
  lo
}

/// `hi`. Fails when the upper bound is infinite.
pub fn upper(range: ValidityRange) -> Int {
  expect Finite(hi) = range.upper_bound.bound_type
  hi
}

/// Both bounds. Fails when a bound is infinite, when `hi < lo` or when
/// `hi - lo > 3,600,000` (one hour, `vector_identity.max_range_ms`). This is
/// the window of §5.8 rule 1, §6.2 rule 7 and §6.8 `RequestExit`.
pub fn window(range: ValidityRange) -> Window {
  let lo = lower(range)
  let hi = upper(range)
  expect lo <= hi
  expect hi - lo <= max_range_ms
  Window { lo, hi }
}

// ## Tests

fn range(lower_bound: IntervalBound, upper_bound: IntervalBound) -> Interval {
  Interval { lower_bound, upper_bound }
}

test window_of_finite_range() {
  window(interval.between(1_000, 3_601_000)) == Window {
    lo: 1_000,
    hi: 3_601_000,
  }
}

test window_ignores_inclusive_flags() {
  let r =
    range(
      IntervalBound { bound_type: Finite(5), is_inclusive: False },
      IntervalBound { bound_type: Finite(9), is_inclusive: False },
    )
  window(r) == Window { lo: 5, hi: 9 }
}

test window_fails_over_one_hour() fail {
  window(interval.between(1_000, 3_601_001)).lo == 1_000
}

test window_fails_when_hi_before_lo() fail {
  window(interval.between(2_000, 1_000)).lo == 2_000
}

test window_fails_without_upper_bound() fail {
  window(interval.after(1_000)).lo == 1_000
}

test window_fails_without_lower_bound() fail {
  window(interval.before(1_000)).hi == 1_000
}

test lower_reads_only_lower_bound() {
  lower(interval.after(7)) == 7
}

test upper_reads_only_upper_bound() {
  upper(interval.before(7)) == 7
}

test lower_fails_when_infinite() fail {
  lower(interval.before(7)) == 7
}

test upper_fails_when_infinite() fail {
  upper(interval.after(7)) == 7
}
