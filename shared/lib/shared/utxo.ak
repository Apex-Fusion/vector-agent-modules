/// Shared UTXO search and counting utilities.
///
/// Common patterns for finding inputs/outputs by token or script address.
/// Used by Games 3, 6, and future games.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{Input, Output}

/// Find a UTXO in the inputs that holds the specified token.
pub fn find_input_with_token(
  inputs: List<Input>,
  policy_id: PolicyId,
  asset_name: ByteArray,
) -> Input {
  expect Some(input) =
    list.find(
      inputs,
      fn(input) {
        assets.quantity_of(input.output.value, policy_id, asset_name) >= 1
      },
    )
  input
}

/// Count outputs at a specific script address.
pub fn count_outputs_at_script(
  outputs: List<Output>,
  script_hash: ByteArray,
) -> Int {
  list.count(
    outputs,
    fn(output) {
      when output.address.payment_credential is {
        Script(hash) -> hash == script_hash
        _ -> False
      }
    },
  )
}

/// Check that at least one input comes from the given script address.
pub fn has_script_input(
  inputs: List<Input>,
  script_hash: ByteArray,
) -> Bool {
  list.any(
    inputs,
    fn(input) {
      when input.output.address.payment_credential is {
        Script(hash) -> hash == script_hash
        _ -> False
      }
    },
  )
}

/// Count how many inputs come from the given script address.
pub fn count_script_inputs(
  inputs: List<Input>,
  script_hash: ByteArray,
) -> Int {
  list.count(
    inputs,
    fn(input) {
      when input.output.address.payment_credential is {
        Script(hash) -> hash == script_hash
        _ -> False
      }
    },
  )
}
