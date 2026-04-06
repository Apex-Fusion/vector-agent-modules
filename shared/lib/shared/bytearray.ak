/// Shared ByteArray and list helper utilities.

use aiken/builtin
use aiken/collection/list

/// Check if a ByteArray starts with a given prefix.
pub fn starts_with(data: ByteArray, prefix: ByteArray) -> Bool {
  let prefix_len = builtin.length_of_bytearray(prefix)
  let data_len = builtin.length_of_bytearray(data)
  if data_len < prefix_len {
    False
  } else {
    builtin.slice_bytearray(0, prefix_len, data) == prefix
  }
}

/// Check if a list is non-empty.
pub fn is_non_empty(items: List<a>) -> Bool {
  when items is {
    [] -> False
    _ -> True
  }
}

/// Check that all items in `subset` also appear in `superset`.
pub fn is_subset(subset: List<ByteArray>, superset: List<ByteArray>) -> Bool {
  list.all(subset, fn(item) { list.has(superset, item) })
}
