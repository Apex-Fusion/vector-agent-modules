//// Mint rules: SPEC §6.1 `Register`, §6.2 `Open`, §6.3 `Burn`.
////
//// `policy_id` is `J`, the script hash of the jury.

use aiken/collection/list
use aiken/primitive/bytearray
use cardano/address.{Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, OutputReference, Transaction}
use jury/datum.{case_fields, juror_fields, no_ballots}
use jury/identity.{identity_ok}
use jury/module1.{ChallengeDatum, Challenged, ClaimDatum, PendingOracle}
use jury/outcome.{empty_tally}
use jury/redeemers.{Burn, MintAction, Open, Register}
use jury/time.{window}
use jury/tokens.{case_prefix, juror_prefix, token_name}
use jury/tx.{
  mints_one, only_burns, only_token, spends, token_output, valid_refund_address,
}
use jury/types.{JuryParams} as types

/// The mint handler of the jury.
pub fn validate(
  params: JuryParams,
  redeemer: MintAction,
  policy_id: ByteArray,
  tx: Transaction,
) -> Bool {
  when redeemer is {
    Register { seed } -> register(params, seed, policy_id, tx)
    Open { seed } -> open(params, seed, policy_id, tx)
    // §6.3.
    Burn -> only_burns(tx.mint, policy_id)
  }
}

/// §6.1 `Register { seed }`.
fn register(
  params: JuryParams,
  seed: OutputReference,
  policy_id: ByteArray,
  tx: Transaction,
) -> Bool {
  let name = token_name(juror_prefix, seed)
  // Rule 3: the output form (token_output) and the datum `Juror`
  // (juror_fields decodes all fields).
  let (lovelace, data) = token_output(tx.outputs, policy_id, name)
  let j = juror_fields(data)
  // Rule 6, step 1 of §5.8.
  let w = window(tx.validity_range)
  and {
    // 1.
    spends(tx.inputs, seed),
    // 2.
    mints_one(tx.mint, policy_id, name),
    // 4.
    bytearray.length(j.agent) == 32,
    bytearray.length(j.owner) == 28,
    bytearray.length(j.prompt_hash) == 32,
    j.registered_at == w.hi,
    j.exit_after == None,
    j.open == [],
    // 5.
    lovelace >= params.bond_min,
    // 6.
    identity_ok(params.identity_hash, j.agent, j.owner, w, tx),
  }
}

/// §6.2 `Open { seed }`.
fn open(
  params: JuryParams,
  seed: OutputReference,
  policy_id: ByteArray,
  tx: Transaction,
) -> Bool {
  let name = token_name(case_prefix, seed)
  // Rule 3: the output form (token_output) and the datum `Case`
  // (case_fields decodes all fields except the ballots; rule 8 checks that
  // the ballots are the empty list).
  let (lovelace, data) = token_output(tx.outputs, policy_id, name)
  let c = case_fields(data)
  // Rule 7: finite bounds and `hi - lo <= 3,600,000`.
  let w = window(tx.validity_range)
  // Rules 4 and 5.
  let (ch_out, ch) =
    challenge(tx.reference_inputs, params.m1_challenge, c.challenge)
  let (cl_out, cl) = claim(tx.reference_inputs, params.m1_claim, ch.claim_token)
  and {
    // 1.
    spends(tx.inputs, seed),
    // 2.
    mints_one(tx.mint, policy_id, name),
    // 4.
    when ch.state is {
      PendingOracle -> True
      _ -> False
    },
    // 5.
    when cl.state is {
      Challenged -> True
      _ -> False
    },
    // 6.
    c.claim == ch.claim_token,
    c.challenge_out == ch_out,
    c.claim_out == cl_out,
    c.claimer == cl.claimer_did,
    c.auditor == ch.auditor_did,
    c.claim_hash == cl.claim_hash,
    c.evidence_hash == ch.evidence_hash,
    bytearray.length(c.claimer) == 32,
    bytearray.length(c.auditor) == 32,
    bytearray.length(c.claim_hash) == 32,
    bytearray.length(c.evidence_hash) == 32,
    // 7. The deadline is capped at 365 days after the opening.
    c.deadline == earlier(
      ch.challenged_at + ch.resolution_deadline,
      w.hi + deadline_cap,
    ),
    c.opened_at == w.hi,
    c.commit_end == w.hi + params.commit_window,
    c.reveal_end == c.commit_end + params.reveal_window,
    c.reveal_end + params.resolve_margin <= c.deadline,
    // 8.
    no_ballots(c),
    c.tally == empty_tally,
    c.state == types.Open,
    c.fee >= 0,
    lovelace >= c.fee,
    // 9.
    valid_refund_address(c.refund_to, policy_id),
    // A Module-1 TimeoutResolve payout checks only the credential and the
    // amount: the refund must not go to a party credential.
    c.refund_to.payment_credential != cl.claimer_credential,
    c.refund_to.payment_credential != ch.auditor_credential,
  }
}

/// Rule 4 of §6.2: the reference and the datum of the reference input with
/// payment credential `Script(m1_challenge)` whose tokens of the policy
/// `m1_challenge` are exactly one token, named `name`. The datum must be
/// inline and decode as `ChallengeDatum`. Fails when there is no such
/// reference input.
fn challenge(
  refs: List<Input>,
  m1_challenge: ByteArray,
  name: ByteArray,
) -> (OutputReference, ChallengeDatum) {
  let credential = Script(m1_challenge)
  expect Some(input) =
    list.find(
      refs,
      fn(input) {
        and {
          input.output.address.payment_credential == credential,
          only_token(input.output.value, m1_challenge, name),
        }
      },
    )
  expect InlineDatum(data) = input.output.datum
  expect ch: ChallengeDatum = data
  (input.output_reference, ch)
}

/// Rule 5 of §6.2: the reference and the datum of the reference input with
/// payment credential `Script(m1_claim)` that holds one token of the policy
/// `m1_claim` named `name`. The datum must be inline and decode as
/// `ClaimDatum`. Fails when there is no such reference input.
fn claim(
  refs: List<Input>,
  m1_claim: ByteArray,
  name: ByteArray,
) -> (OutputReference, ClaimDatum) {
  let credential = Script(m1_claim)
  expect Some(input) =
    list.find(
      refs,
      fn(input) {
        and {
          input.output.address.payment_credential == credential,
          assets.quantity_of(input.output.value, m1_claim, name) == 1,
        }
      },
    )
  expect InlineDatum(data) = input.output.datum
  expect cl: ClaimDatum = data
  (input.output_reference, cl)
}

/// The longest time from the opening to the case deadline (§6.2 rule 7):
/// 365 days in milliseconds.
pub const deadline_cap: Int = 31_536_000_000

fn earlier(a: Int, b: Int) -> Int {
  if a < b {
    a
  } else {
    b
  }
}
