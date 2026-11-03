/// One-shot minting policy for record tokens.
///
/// A record token authenticates a parameter or oracle record: the module
/// validators accept a reference input as a record only when it holds the
/// token (see `shared/record_auth`).
///
/// The policy is parameterized by a seed output reference. Tokens can be
/// minted only in the transaction that spends the seed, and every minted
/// asset name has a quantity of exactly 1. Because the seed can be spent only
/// once, each asset name of the policy exists at most once. A transaction
/// that only burns tokens of the policy is always accepted.

use aiken/collection/dict
use aiken/collection/list
use cardano/address
use cardano/assets.{PolicyId}
use cardano/transaction.{Input, OutputReference, Transaction}

validator record_token(seed: OutputReference) {
  mint(_redeemer: Data, policy_id: PolicyId, tx: Transaction) {
    let quantities = dict.values(assets.tokens(tx.mint, policy_id))
    if list.all(quantities, fn(quantity) { quantity < 0 }) {
      True
    } else {
      list.all(quantities, fn(quantity) { quantity == 1 }) && list.any(
        tx.inputs,
        fn(input) { input.output_reference == seed },
      )
    }
  }

  else(_) {
    fail @"Unsupported script purpose"
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

const test_seed: OutputReference =
  OutputReference {
    transaction_id: #"5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed",
    output_index: 3,
  }

const test_policy: PolicyId =
  #"7070707070707070707070707070707070707070707070707070707070"

fn seed_input() -> Input {
  Input {
    output_reference: test_seed,
    output: transaction.Output {
      address: address.from_verification_key(
        #"0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a",
      ),
      value: assets.from_lovelace(5_000_000),
      datum: transaction.NoDatum,
      reference_script: None,
    },
  }
}

fn other_input() -> Input {
  Input { ..seed_input(), output_reference: OutputReference { ..test_seed, output_index: 4 } }
}

fn mint_tx(inputs: List<Input>, mint: assets.Value) -> Transaction {
  Transaction { ..transaction.placeholder, inputs: inputs, mint: mint }
}

test record_token_mints_params_and_oracle_with_seed() {
  let mint =
    assets.from_asset(test_policy, "params", 1)
      |> assets.add(test_policy, "oracle", 1)
  record_token.mint(test_seed, Void, test_policy, mint_tx([seed_input()], mint))
}

test record_token_rejects_mint_without_seed() {
  let mint = assets.from_asset(test_policy, "params", 1)
  !record_token.mint(test_seed, Void, test_policy, mint_tx([other_input()], mint))
}

test record_token_rejects_quantity_above_one() {
  let mint = assets.from_asset(test_policy, "params", 2)
  !record_token.mint(test_seed, Void, test_policy, mint_tx([seed_input()], mint))
}

test record_token_rejects_mint_and_burn_together() {
  let mint =
    assets.from_asset(test_policy, "params", 1)
      |> assets.add(test_policy, "oracle", -1)
  !record_token.mint(test_seed, Void, test_policy, mint_tx([seed_input()], mint))
}

test record_token_accepts_burn_without_seed() {
  let mint = assets.from_asset(test_policy, "params", -1)
  record_token.mint(test_seed, Void, test_policy, mint_tx([other_input()], mint))
}
