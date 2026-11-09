//// Transaction helpers of the jury rules (SPEC §4, §6).

use aiken/collection/dict
use aiken/collection/list
use aiken/collection/pairs
use aiken/primitive/bytearray
use cardano/address.{Address, Inline, Script, VerificationKey}
use cardano/assets.{Value}
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Spend, Transaction,
}
use jury/redeemers.{SpendAction}

/// The own input of a spend handler (§6).
pub type Own {
  /// `J`: the script hash, the policy id and the payment credential of the
  /// jury address.
  hash: ByteArray,
  /// The own token name.
  name: ByteArray,
  /// The lovelace of the own input.
  lovelace: Int,
  /// The inline datum of the own input.
  datum: Data,
  /// The reference of the own input: the tag of its tagged outputs.
  reference: OutputReference,
}

/// A counterpart (§6): the input that holds a named jury token, and its spend
/// redeemer.
pub type Counterpart {
  input: Input,
  action: SpendAction,
}

/// The jury address: `Script(hash)` without a stake credential (§4).
pub fn jury_address(hash: ByteArray) -> Address {
  Address { payment_credential: Script(hash), stake_credential: None }
}

/// True when the tokens of `policy` in `value` are exactly one token: `name`
/// with quantity 1. Other policies are not checked.
pub fn only_token(value: Value, policy: ByteArray, name: ByteArray) -> Bool {
  when dict.to_pairs(assets.tokens(value, policy)) is {
    [Pair(token, 1)] -> token == name
    _ -> False
  }
}

/// The lovelace of `value` when `value` holds only lovelace and the token
/// (`policy`, `name`) with quantity 1. Else `None`.
pub fn token_lovelace(
  value: Value,
  policy: ByteArray,
  name: ByteArray,
) -> Option<Int> {
  when dict.to_pairs(assets.to_dict(value)) is {
    [Pair(ada_policy, ada), Pair(token_policy, tokens)] ->
      when (dict.to_pairs(ada), dict.to_pairs(tokens)) is {
        ([Pair(_, lovelace)], [Pair(token, 1)]) ->
          if and {
            ada_policy == assets.ada_policy_id,
            token_policy == policy,
            token == name,
          } {
            Some(lovelace)
          } else {
            None
          }
        _ -> None
      }
    _ -> None
  }
}

/// The output that holds the jury token `name` (the continuing output of §6,
/// and the new output of §6.1 and §6.2). The output must be at the jury
/// address, hold only lovelace and that token (quantity 1), have an inline
/// datum and no reference script. Returns its lovelace and its datum. Fails
/// when no output holds the token in this form.
///
/// A jury token has quantity 1 and a unique name, so at most one output holds
/// it. An output in another form is skipped, so the token in such an output
/// makes the search fail.
pub fn token_output(
  outputs: List<Output>,
  hash: ByteArray,
  name: ByteArray,
) -> (Int, Data) {
  find_token_output(outputs, jury_address(hash), hash, name)
}

fn find_token_output(
  outputs: List<Output>,
  address: Address,
  hash: ByteArray,
  name: ByteArray,
) -> (Int, Data) {
  when outputs is {
    [] -> fail @"no jury output holds the token"
    [output, ..rest] ->
      if output.address == address {
        when token_lovelace(output.value, hash, name) is {
          Some(lovelace) -> {
            expect InlineDatum(datum) = output.datum
            expect None = output.reference_script
            (lovelace, datum)
          }
          None -> find_token_output(rest, address, hash, name)
        }
      } else {
        find_token_output(rest, address, hash, name)
      }
  }
}

/// The counterpart that holds the jury token `name`, with its redeemer from
/// `tx.redeemers` (`Spend(out_ref)`), decoded as `SpendAction`. Fails when no
/// input holds the token or when its redeemer is missing or not a
/// `SpendAction`.
pub fn counterpart(
  tx: Transaction,
  hash: ByteArray,
  name: ByteArray,
) -> Counterpart {
  expect Some(input) =
    list.find(
      tx.inputs,
      fn(input) { assets.quantity_of(input.output.value, hash, name) == 1 },
    )
  expect Some(redeemer) =
    pairs.get_first(tx.redeemers, Spend(input.output_reference))
  expect action: SpendAction = redeemer
  Counterpart { input, action }
}

/// The inline datum of `input`. Fails when the datum is not inline.
pub fn inline_datum(input: Input) -> Data {
  expect InlineDatum(data) = input.output.datum
  data
}

/// The transaction spends `out_ref`.
pub fn spends(inputs: List<Input>, out_ref: OutputReference) -> Bool {
  list.any(inputs, fn(input) { input.output_reference == out_ref })
}

/// The transaction has exactly one input with payment credential
/// `Script(hash)`.
pub fn single_jury_input(inputs: List<Input>, hash: ByteArray) -> Bool {
  let credential = Script(hash)
  list.count(
    inputs,
    fn(input) { input.output.address.payment_credential == credential },
  ) == 1
}

/// The mint field mints exactly one token of `policy`: `name` with
/// quantity 1.
pub fn mints_one(mint: Value, policy: ByteArray, name: ByteArray) -> Bool {
  only_token(mint, policy, name)
}

/// The mint field burns the token (`policy`, `name`): quantity -1.
pub fn burns(mint: Value, policy: ByteArray, name: ByteArray) -> Bool {
  assets.quantity_of(mint, policy, name) == -1
}

/// Every quantity of `policy` in the mint field is negative.
pub fn only_burns(mint: Value, policy: ByteArray) -> Bool {
  list.all(
    dict.to_pairs(assets.tokens(mint, policy)),
    fn(pair) { pair.2nd < 0 },
  )
}

/// A tagged output (§6): some output is at exactly `address`, has the inline
/// datum `tag` (the reference of the own input, as Plutus Data) and holds at
/// least `lovelace`.
pub fn pays_tagged(
  outputs: List<Output>,
  address: Address,
  tag: OutputReference,
  lovelace: Int,
) -> Bool {
  let datum = InlineDatum(tag)
  list.any(
    outputs,
    fn(output) {
      and {
        output.address == address,
        output.datum == datum,
        assets.lovelace_of(output.value) >= lovelace,
      }
    },
  )
}

/// Rule 9 of §6.2: a valid ledger address that is not at `Script(jury)`. The
/// payment credential is `VerificationKey(h)` or `Script(h)` with a 28-byte
/// `h`; the stake credential is `None` or `Inline` with a 28-byte hash.
pub fn valid_refund_address(address: Address, jury: ByteArray) -> Bool {
  and {
    when address.payment_credential is {
      VerificationKey(h) -> bytearray.length(h) == 28
      Script(h) -> bytearray.length(h) == 28 && h != jury
    },
    when address.stake_credential is {
      None -> True
      Some(Inline(VerificationKey(h))) -> bytearray.length(h) == 28
      Some(Inline(Script(h))) -> bytearray.length(h) == 28
      Some(_) -> False
    },
  }
}

/// `key` signs the transaction.
pub fn signed_by(tx: Transaction, key: ByteArray) -> Bool {
  list.has(tx.extra_signatories, key)
}
