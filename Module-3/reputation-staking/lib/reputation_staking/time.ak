/// Time rules for Module 3: Reputation Staking.
///
/// All times are POSIX milliseconds, the unit of the transaction validity
/// range.
///
/// - Elapsed-time checks ("the deadline has passed", "the cooldown is over")
///   use the lower bound of the validity range.
/// - Before-deadline checks ("the action happens before the deadline") use
///   the upper bound, so a transaction cannot pass them with an early lower
///   bound after the deadline.
/// - A time that the contract stores (created_at, response_submitted_at)
///   equals the lower bound of a validity range of at most
///   `max_validity_range` milliseconds, so it is at most that much earlier
///   than the time of the transaction.

use aiken/interval.{Finite}
use cardano/transaction.{Transaction}

/// Maximum length of the validity range of a transaction that stores a time:
/// one hour.
pub const max_validity_range: Int = 3_600_000

/// The lower bound of the validity range, when it is finite.
pub fn lower_bound(tx: Transaction) -> Option<Int> {
  when tx.validity_range.lower_bound.bound_type is {
    Finite(time) -> Some(time)
    _ -> None
  }
}

/// The upper bound of the validity range, when it is finite.
pub fn upper_bound(tx: Transaction) -> Option<Int> {
  when tx.validity_range.upper_bound.bound_type is {
    Finite(time) -> Some(time)
    _ -> None
  }
}

/// The lower bound of the validity range when both bounds are finite and the
/// range is at most `max_validity_range` long. Otherwise None.
pub fn bounded_lower_bound(tx: Transaction) -> Option<Int> {
  when (lower_bound(tx), upper_bound(tx)) is {
    (Some(lo), Some(hi)) ->
      if hi - lo <= max_validity_range {
        Some(lo)
      } else {
        None
      }
    _ -> None
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

test bounded_lower_bound_accepts_one_hour() {
  let tx =
    Transaction {
      ..transaction.placeholder,
      validity_range: interval.between(1_000, 1_000 + max_validity_range),
    }
  bounded_lower_bound(tx) == Some(1_000)
}

test bounded_lower_bound_rejects_longer_range() {
  let tx =
    Transaction {
      ..transaction.placeholder,
      validity_range: interval.between(1_000, 1_001 + max_validity_range),
    }
  bounded_lower_bound(tx) == None
}

test bounded_lower_bound_rejects_open_range() {
  let tx =
    Transaction {
      ..transaction.placeholder,
      validity_range: interval.after(1_000),
    }
  bounded_lower_bound(tx) == None && lower_bound(tx) == Some(1_000) && upper_bound(
    tx,
  ) == None
}
