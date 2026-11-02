/// Authenticated parameter and oracle records.
///
/// A record is an output at a holder script address that holds exactly one
/// token of a one-shot record token policy
/// (`shared/holder-scripts/validators/record_token.ak`). The policy mints each
/// asset name once, so at most one output can hold a given record token. The
/// holder accepts a spend only with the owner key, so only the owner can
/// change a record.
///
/// Validators accept a reference input as a record only when it is at the
/// holder address and holds the record token. Any other output at the holder
/// address is ignored.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{AssetName, PolicyId}
use cardano/transaction.{InlineDatum, Input}

/// Asset name of the record token that authenticates a parameter record.
pub const params_token_name: AssetName = "params"

/// Asset name of the record token that authenticates an oracle record.
pub const oracle_token_name: AssetName = "oracle"

/// True when the input is at the script address `holder_hash` and holds
/// exactly one token (`token_policy`, `token_name`).
pub fn is_record(
  input: Input,
  holder_hash: ByteArray,
  token_policy: PolicyId,
  token_name: AssetName,
) -> Bool {
  when input.output.address.payment_credential is {
    Script(hash) ->
      hash == holder_hash && assets.quantity_of(
        input.output.value,
        token_policy,
        token_name,
      ) == 1
    _ -> False
  }
}

/// The inline datum of the record (`holder_hash`, `token_policy`, `token_name`)
/// in the reference inputs. None when no reference input is that record, or
/// when the record has no inline datum.
pub fn find_record(
  reference_inputs: List<Input>,
  holder_hash: ByteArray,
  token_policy: PolicyId,
  token_name: AssetName,
) -> Option<Data> {
  when
    list.find(
      reference_inputs,
      fn(input) { is_record(input, holder_hash, token_policy, token_name) },
    )
  is {
    Some(input) ->
      when input.output.datum is {
        InlineDatum(raw) -> Some(raw)
        _ -> None
      }
    None -> None
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

const test_holder: ByteArray =
  #"4040404040404040404040404040404040404040404040404040404040"

const test_policy: PolicyId =
  #"7070707070707070707070707070707070707070707070707070707070"

fn test_input(holder: ByteArray, value: assets.Value, datum: Data) -> Input {
  Input {
    output_reference: transaction.OutputReference {
      transaction_id: #"0000000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    },
    output: transaction.Output {
      address: address.from_script(holder),
      value,
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

fn token_value(name: AssetName, quantity: Int) -> assets.Value {
  assets.from_lovelace(2_000_000) |> assets.add(test_policy, name, quantity)
}

fn as_int(found: Option<Data>) -> Option<Int> {
  when found is {
    Some(raw) -> {
      expect n: Int = raw
      Some(n)
    }
    None -> None
  }
}

test find_record_with_token() {
  let record = test_input(test_holder, token_value(params_token_name, 1), 42)
  as_int(find_record([record], test_holder, test_policy, params_token_name)) == Some(
    42,
  )
}

/// The old lookup took the first reference input at the holder address.
/// A holder output without the record token is no longer a record.
test find_record_ignores_output_without_token() {
  let forged = test_input(test_holder, assets.from_lovelace(2_000_000), 1)
  let record = test_input(test_holder, token_value(params_token_name, 1), 2)
  find_record([forged], test_holder, test_policy, params_token_name) == None && as_int(
    find_record([forged, record], test_holder, test_policy, params_token_name),
  ) == Some(2)
}

test find_record_ignores_other_token_name() {
  let oracle = test_input(test_holder, token_value(oracle_token_name, 1), 3)
  find_record([oracle], test_holder, test_policy, params_token_name) == None
}

test find_record_ignores_other_address() {
  let other =
    test_input(
      #"5050505050505050505050505050505050505050505050505050505050",
      token_value(params_token_name, 1),
      4,
    )
  find_record([other], test_holder, test_policy, params_token_name) == None
}
