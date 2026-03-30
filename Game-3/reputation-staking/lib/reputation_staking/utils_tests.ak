/// Tests for utility functions.

use aiken/builtin
use reputation_staking/types.{
  challenge_token_prefix, endorsement_token_prefix, stake_token_prefix,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
  derive_stake_token_name, is_non_empty, is_subset, starts_with,
}

// ──────────────────────────────────────────────────────────────────────────────
// starts_with tests
// ──────────────────────────────────────────────────────────────────────────────

test starts_with_basic() {
  starts_with("hello_world", "hello")
}

test starts_with_exact() {
  starts_with("abc", "abc")
}

test starts_with_empty_prefix() {
  starts_with("anything", "")
}

test starts_with_mismatch() {
  !starts_with("hello", "world")
}

test starts_with_longer_prefix() {
  !starts_with("hi", "hello")
}

test starts_with_empty_data() {
  !starts_with("", "x")
}

// ──────────────────────────────────────────────────────────────────────────────
// is_non_empty tests
// ──────────────────────────────────────────────────────────────────────────────

test is_non_empty_true() {
  is_non_empty(["a", "b"])
}

test is_non_empty_single() {
  is_non_empty(["a"])
}

test is_non_empty_false() {
  let empty: List<ByteArray> = []
  !is_non_empty(empty)
}

// ──────────────────────────────────────────────────────────────────────────────
// is_subset tests
// ──────────────────────────────────────────────────────────────────────────────

test is_subset_true() {
  is_subset(["a", "b"], ["a", "b", "c"])
}

test is_subset_equal() {
  is_subset(["a", "b"], ["a", "b"])
}

test is_subset_empty() {
  is_subset([], ["a", "b"])
}

test is_subset_false() {
  !is_subset(["a", "d"], ["a", "b", "c"])
}

// ──────────────────────────────────────────────────────────────────────────────
// Token name derivation tests
// ──────────────────────────────────────────────────────────────────────────────

test derive_stake_token_name_has_prefix() {
  let name = derive_stake_token_name("agent_did_123")
  starts_with(name, stake_token_prefix)
}

test derive_stake_token_name_correct_length() {
  let name = derive_stake_token_name("agent_did_123")
  // "rstk_" is 5 bytes + 28 hash bytes = 33 bytes total
  builtin.length_of_bytearray(name) == 5 + 28
}

test derive_stake_token_name_deterministic() {
  let name1 = derive_stake_token_name("agent_did_123")
  let name2 = derive_stake_token_name("agent_did_123")
  name1 == name2
}

test derive_stake_token_name_different_for_different_dids() {
  let name1 = derive_stake_token_name("agent_did_1")
  let name2 = derive_stake_token_name("agent_did_2")
  name1 != name2
}

test derive_endorsement_token_name_has_prefix() {
  let name = derive_endorsement_token_name("endorser_did", "target_did")
  starts_with(name, endorsement_token_prefix)
}

test derive_endorsement_token_name_correct_length() {
  let name = derive_endorsement_token_name("endorser_did", "target_did")
  // "rend_" is 5 bytes + 28 hash bytes = 33 bytes total
  builtin.length_of_bytearray(name) == 5 + 28
}

test derive_endorsement_token_name_order_matters() {
  let name1 = derive_endorsement_token_name("agent_a", "agent_b")
  let name2 = derive_endorsement_token_name("agent_b", "agent_a")
  name1 != name2
}

test derive_challenge_token_name_has_prefix() {
  let name =
    derive_challenge_token_name("challenger", "target", "capability")
  starts_with(name, challenge_token_prefix)
}

test derive_challenge_token_name_correct_length() {
  let name =
    derive_challenge_token_name("challenger", "target", "capability")
  // "rchl_" is 5 bytes + 24 hash bytes = 29 bytes total
  builtin.length_of_bytearray(name) == 5 + 24
}

test derive_challenge_token_name_different_capability() {
  let name1 = derive_challenge_token_name("challenger", "target", "cap_a")
  let name2 = derive_challenge_token_name("challenger", "target", "cap_b")
  name1 != name2
}
