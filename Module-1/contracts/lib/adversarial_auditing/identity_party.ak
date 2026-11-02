//// Module-1 as an identity party (Part 2 §3.8, Part 1 §6 "Dispute contract").
////
//// The validator `identity_party` only wires the handlers of this module:
////
//// - mint: verdict tokens. A token is minted only in the resolution of one
////   challenge, and burned only in a verdict application.
//// - spend: a verdict output is spent only in the transaction that burns its
////   token.
//// - withdraw: Module-1 approves only a Consent (it becomes a new party) and a
////   verdict application (a Suspend or a Revoke by its party).
////
//// The challenge validator calls `resolution_verdict_ok` in each resolution.

use adversarial_auditing/identity_checks.{
  Window, at_script_address, config_record_hash, mints_one_token,
  reason_dispute_verdict, window,
}
use adversarial_auditing/types.{
  ApplyVerdict, AuditingConfig, AuditorWins, BurnVerdict, ChallengeAction,
  ChallengeDatum, ClaimDatum, ClaimerWins, Consent, IdentityVerdict,
  Inconclusive, MintVerdict, OracleResolve, PartyApproval, ResolveJury, Resolved,
  Verdict, VerdictMint, VerdictPolicy, VerdictRevoke, VerdictRule,
  VerdictSuspend,
}
use adversarial_auditing/utils.{get_cross_refs}
use aiken/cbor
use aiken/collection/dict
use aiken/collection/list
use aiken/collection/pairs
use aiken/crypto.{blake2b_256}
use cardano/address.{Credential, Script}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Mint, Output, OutputReference, Spend, Transaction,
}
use vector_identity.{days_30_ms, right_revoke, right_suspend}
use vector_identity/redeemers.{
  Adopt, ByParty, IdentityMint, IdentitySpend, Register, Revoke, Suspend, Update,
}
use vector_identity/types.{Hold, IdentityDatum, Party, Signer, Status} as vi

// ## Verdicts

/// The verdict token name: blake2b_256 of the serialised verdict.
pub fn verdict_token_name(verdict: IdentityVerdict) -> ByteArray {
  blake2b_256(cbor.serialise(verdict))
}

/// The verdict policy rule for a challenge outcome. Inconclusive gives none.
pub fn verdict_rule(
  policy: VerdictPolicy,
  outcome: Verdict,
) -> Option<VerdictRule> {
  when outcome is {
    AuditorWins -> policy.auditor_wins
    ClaimerWins -> policy.claimer_wins
    Inconclusive -> None
  }
}

/// A rule is well formed: a suspension lasts more than 0 ms and at most 30
/// days; a revocation has the duration 0.
pub fn rule_ok(rule: VerdictRule) -> Bool {
  when rule.action is {
    VerdictSuspend -> rule.duration > 0 && rule.duration <= days_30_ms
    VerdictRevoke -> rule.duration == 0
  }
}

/// The verdict of the resolution in `tx`. The transaction spends exactly one
/// input of the challenge validator `c`, which holds one challenge token and
/// has the redeemer OracleResolve (outcome from the redeemer) or ResolveJury
/// (outcome from the single continuing challenge output, state Resolved). The
/// verdict policy must give a well-formed rule for the outcome. The claim of
/// `c` is the input at the claim address that holds the claim token named in
/// `c`. The agent is the losing side: the claimer for AuditorWins, the auditor
/// for ClaimerWins. `challenge` is the output reference of `c`. The function
/// fails when one of these conditions is false.
pub fn resolution_verdict(
  config: AuditingConfig,
  challenge_hash: ByteArray,
  tx: Transaction,
) -> IdentityVerdict {
  expect [challenge_input] =
    list.filter(
      tx.inputs,
      fn(input) {
        input.output.address.payment_credential == Script(challenge_hash)
      },
    )
  expect [Pair(_, 1)] =
    dict.to_pairs(assets.tokens(challenge_input.output.value, challenge_hash))
  expect InlineDatum(challenge_data) = challenge_input.output.datum
  expect challenge: ChallengeDatum = challenge_data
  expect Some(redeemer) =
    pairs.get_first(tx.redeemers, Spend(challenge_input.output_reference))
  expect action: ChallengeAction = redeemer
  let outcome =
    when action is {
      OracleResolve { verdict, .. } -> verdict
      ResolveJury -> resolved_outcome(tx.outputs, challenge_hash)
      _ -> fail @"identity_party: the challenge input is not resolved"
    }
  expect Some(rule) = verdict_rule(config.verdict_policy, outcome)
  expect rule_ok(rule)
  let refs = get_cross_refs(config, tx.reference_inputs)
  expect Some(claim_input) =
    list.find(
      tx.inputs,
      fn(input) {
        and {
          input.output.address.payment_credential == Script(
            refs.claim_validator_hash,
          ),
          assets.quantity_of(
            input.output.value,
            refs.claim_validator_hash,
            challenge.claim_token,
          ) == 1,
        }
      },
    )
  expect InlineDatum(claim_data) = claim_input.output.datum
  expect claim: ClaimDatum = claim_data
  let agent =
    when outcome is {
      AuditorWins -> claim.claimer_did
      _ -> challenge.auditor_did
    }
  IdentityVerdict {
    agent,
    action: rule.action,
    duration: rule.duration,
    challenge: challenge_input.output_reference,
  }
}

/// The outcome of a ResolveJury: the state Resolved of the single output at
/// the challenge validator.
fn resolved_outcome(outputs: List<Output>, challenge_hash: ByteArray) -> Verdict {
  expect [output] =
    list.filter(
      outputs,
      fn(output) { output.address.payment_credential == Script(challenge_hash) },
    )
  expect InlineDatum(data) = output.datum
  expect challenge: ChallengeDatum = data
  expect Resolved { verdict } = challenge.state
  verdict
}

// ## Challenge side

/// The verdict requirement of each challenge resolution (Part 2 §3.8). When
/// the verdict policy gives a verdict for `outcome`, the transaction mints
/// exactly one token, with quantity 1, under `M1`. `M1` comes from the
/// configuration record, so the challenge validator needs no compile-time
/// hash of `identity_party`. `identity_party` checks the token name and the
/// verdict output.
pub fn resolution_verdict_ok(
  config: AuditingConfig,
  outcome: Verdict,
  tx: Transaction,
) -> Bool {
  when verdict_rule(config.verdict_policy, outcome) is {
    None -> True
    Some(_) ->
      mints_one_token(
        tx.mint,
        config_record_hash(
          tx.reference_inputs,
          config.config_policy,
          config.config_holder,
        ),
      )
  }
}

// ## Handlers

/// Mint handler. MintVerdict: exactly one token under the own policy, named
/// `verdict_token_name(v)` for the verdict `v` of the resolution (see
/// `resolution_verdict`). The token goes to the exact enterprise address of
/// the own script with `v` as its inline datum, AP3X and no other asset, and
/// no reference script. BurnVerdict: exactly one token of the own policy is
/// burned, in a transaction with the own withdrawal (`ApplyVerdict` is the
/// only withdrawal approval that accepts a burn).
pub fn mint_handler(
  config: AuditingConfig,
  challenge_hash: ByteArray,
  redeemer: VerdictMint,
  own_policy: ByteArray,
  tx: Transaction,
) -> Bool {
  when redeemer is {
    MintVerdict -> {
      expect [Pair(name, 1)] = dict.to_pairs(assets.tokens(tx.mint, own_policy))
      let verdict = resolution_verdict(config, challenge_hash, tx)
      and {
        name == verdict_token_name(verdict),
        verdict_output_ok(tx.outputs, own_policy, name, verdict),
      }
    }
    BurnVerdict ->
      when dict.to_pairs(assets.tokens(tx.mint, own_policy)) is {
        [Pair(_, -1)] -> pairs.has_key(tx.withdrawals, Script(own_policy))
        _ -> False
      }
  }
}

fn verdict_output_ok(
  outputs: List<Output>,
  own_policy: ByteArray,
  name: ByteArray,
  verdict: IdentityVerdict,
) -> Bool {
  expect Some(output) =
    list.find(
      outputs,
      fn(output) { assets.quantity_of(output.value, own_policy, name) != 0 },
    )
  let verdict_data: Data = verdict
  and {
    at_script_address(output, own_policy),
    assets.without_lovelace(output.value) == assets.from_asset(
      own_policy,
      name,
      1,
    ),
    output.datum == InlineDatum(verdict_data),
    output.reference_script == None,
  }
}

/// Spend handler: the spent input holds exactly one token of the own policy,
/// the transaction burns that token (and no other token of the own policy),
/// and it has the own withdrawal.
pub fn spend_handler(own_ref: OutputReference, tx: Transaction) -> Bool {
  expect Some(own_input) =
    list.find(tx.inputs, fn(input) { input.output_reference == own_ref })
  expect Script(own_hash) = own_input.output.address.payment_credential
  when dict.to_pairs(assets.tokens(own_input.output.value, own_hash)) is {
    [Pair(name, 1)] ->
      when dict.to_pairs(assets.tokens(tx.mint, own_hash)) is {
        [Pair(burned, -1)] ->
          burned == name && pairs.has_key(tx.withdrawals, Script(own_hash))
        _ -> False
      }
    _ -> False
  }
}

/// Withdraw handler: Consent or a verdict application, for the own script
/// credential.
pub fn withdraw_handler(
  identity_hash: ByteArray,
  redeemer: PartyApproval,
  account: Credential,
  tx: Transaction,
) -> Bool {
  expect Script(own_hash) = account
  when redeemer is {
    Consent -> consent_ok(identity_hash, own_hash, tx)
    ApplyVerdict -> apply_verdict_ok(identity_hash, own_hash, tx)
  }
}

// ## Consent

/// Consent (Part 2 §3.8): an identity Register or Adopt (a mint of the
/// identity policy and no identity input), or an Update (one identity input
/// with the redeemer Update, no identity mint), in which:
///
/// - every Module-1 party of the old record is in the new record and
///   unchanged;
/// - one or two Module-1 parties are new (no Module-1 party with their role
///   in the old record), with different roles, and each new one has role
///   Accountable or Responsible, rights equal to the role's highest rights,
///   `not_before <= lo`, no `not_after` and no `removal_after`;
/// - the new record has no other Module-1 party;
/// - an Update keeps the status;
/// - no token of the own policy is minted or burned.
pub fn consent_ok(
  identity_hash: ByteArray,
  own_hash: ByteArray,
  tx: Transaction,
) -> Bool {
  let Window { lo, hi } = window(tx.validity_range)
  let module1 = vi.Script { hash: own_hash }
  let (before, after) = consent_parties(identity_hash, tx)
  let old = list.filter(before, fn(party) { party.signer == module1 })
  let mine = list.filter(after, fn(party) { party.signer == module1 })
  let new =
    list.filter(
      mine,
      fn(party) { !list.any(old, fn(kept) { kept.role == party.role }) },
    )
  let new_roles_ok =
    when new is {
      [_] -> True
      [first, second] -> first.role != second.role
      _ -> False
    }
  // The window rule and the mint rule come first, so they run on every path.
  and {
    lo <= hi,
    dict.is_empty(assets.tokens(tx.mint, own_hash)),
    list.all(old, fn(party) { list.has(mine, party) }),
    new_roles_ok,
    list.length(mine) == list.length(old) + list.length(new),
    list.all(new, fn(party) { consent_terms_ok(party, lo) }),
  }
}

/// The parties of the old record (none for Register and Adopt) and of the new
/// record of a Consent transaction. Fails unless the transaction is an
/// identity Register, Adopt or Update with one record output.
fn consent_parties(
  identity_hash: ByteArray,
  tx: Transaction,
) -> (List<Party>, List<Party>) {
  let record_inputs =
    list.filter(
      tx.inputs,
      fn(input) {
        input.output.address.payment_credential == Script(identity_hash)
      },
    )
  expect [output] =
    list.filter(
      tx.outputs,
      fn(output) { output.address.payment_credential == Script(identity_hash) },
    )
  expect InlineDatum(after_data) = output.datum
  expect after: IdentityDatum = after_data
  when record_inputs is {
    [] -> {
      expect Some(redeemer) = pairs.get_first(tx.redeemers, Mint(identity_hash))
      expect mint_action: IdentityMint = redeemer
      let creation =
        when mint_action is {
          Register { .. } -> True
          Adopt { .. } -> True
        }
      expect [Pair(id, 1)] =
        dict.to_pairs(assets.tokens(tx.mint, identity_hash))
      expect creation
      expect assets.quantity_of(output.value, identity_hash, id) == 1
      ([], after.parties)
    }
    [input] -> {
      expect Some(redeemer) =
        pairs.get_first(tx.redeemers, Spend(input.output_reference))
      expect spend_action: IdentitySpend = redeemer
      expect Update = spend_action
      expect dict.is_empty(assets.tokens(tx.mint, identity_hash))
      expect [Pair(id, 1)] =
        dict.to_pairs(assets.tokens(input.output.value, identity_hash))
      expect assets.quantity_of(output.value, identity_hash, id) == 1
      expect InlineDatum(before_data) = input.output.datum
      expect before: IdentityDatum = before_data
      expect after.status == before.status
      (before.parties, after.parties)
    }
    _ -> fail @"identity_party: more than one identity input"
  }
}

/// The terms of a new Module-1 party.
fn consent_terms_ok(party: Party, lo: Int) -> Bool {
  let highest =
    when party.role is {
      vi.Accountable -> right_suspend + right_revoke
      vi.Responsible -> right_suspend
      _ -> -1
    }
  and {
    party.rights == highest,
    party.not_before <= lo,
    party.not_after == None,
    party.removal_after == None,
  }
}

// ## Verdict application

/// Verdict application (Part 2 §3.8):
///
/// - the transaction burns exactly one token of the own policy, held by an
///   input at the own script, whose inline datum is a verdict `v` with
///   `verdict_token_name(v)` = the token name;
/// - its single identity input holds the token `(identity_hash, v.agent)`;
/// - suspend: the identity redeemer is `Suspend { ByParty { index } }` for a
///   Module-1 party, and the new record holds the Module-1 hold
///   `{ reason: 64, since: lo, until }`, where `until` is the later of
///   `lo + v.duration` and the `until` of the Module-1 hold in force in the
///   old record;
/// - revoke: the identity redeemer is `Revoke { ByParty { index } }` for a
///   Module-1 party, and the new status is
///   `Revoked { by: Module-1, reason: 64, at: lo }` without `invalid_from` and
///   successor.
pub fn apply_verdict_ok(
  identity_hash: ByteArray,
  own_hash: ByteArray,
  tx: Transaction,
) -> Bool {
  let Window { lo, hi } = window(tx.validity_range)
  let module1 = vi.Script { hash: own_hash }
  expect [Pair(name, -1)] = dict.to_pairs(assets.tokens(tx.mint, own_hash))
  expect Some(verdict_input) =
    list.find(
      tx.inputs,
      fn(input) { assets.quantity_of(input.output.value, own_hash, name) == 1 },
    )
  expect InlineDatum(verdict_data) = verdict_input.output.datum
  expect verdict: IdentityVerdict = verdict_data
  expect [record_input] =
    list.filter(
      tx.inputs,
      fn(input) {
        input.output.address.payment_credential == Script(identity_hash)
      },
    )
  expect [output] =
    list.filter(
      tx.outputs,
      fn(output) { output.address.payment_credential == Script(identity_hash) },
    )
  expect InlineDatum(before_data) = record_input.output.datum
  expect before: IdentityDatum = before_data
  expect InlineDatum(after_data) = output.datum
  expect after: IdentityDatum = after_data
  expect Some(redeemer) =
    pairs.get_first(tx.redeemers, Spend(record_input.output_reference))
  expect action: IdentitySpend = redeemer
  // The window rule and the checks for both actions come first, so they run
  // on every path.
  and {
    lo <= hi,
    verdict_input.output.address.payment_credential == Script(own_hash),
    verdict_token_name(verdict) == name,
    assets.quantity_of(record_input.output.value, identity_hash, verdict.agent) == 1,
    when verdict.action is {
      VerdictSuspend ->
        when action is {
          Suspend{actor: ByParty { index }} -> and {
              module1_party(before, index, module1),
              verdict_hold_ok(
                before.status,
                after.status,
                module1,
                lo,
                lo + verdict.duration,
              ),
            }
          _ -> False
        }
      VerdictRevoke ->
        when action is {
          Revoke{actor: ByParty { index }} -> and {
              module1_party(before, index, module1),
              after.status == vi.Revoked {
                by: module1,
                reason: reason_dispute_verdict,
                at: lo,
                invalid_from: None,
                successor: None,
              },
            }
          _ -> False
        }
    },
  }
}

/// The party at `index` of the record is a Module-1 party.
fn module1_party(record: IdentityDatum, index: Int, module1: Signer) -> Bool {
  when list.at(record.parties, index) is {
    Some(party) -> party.signer == module1
    None -> False
  }
}

/// The new status holds the Module-1 hold `{ reason: 64, since: lo, until }`
/// with `until` = the later of `verdict_until` and the `until` of the
/// Module-1 hold in force (`until > lo`) in the old status.
fn verdict_hold_ok(
  before: Status,
  after: Status,
  module1: Signer,
  lo: Int,
  verdict_until: Int,
) -> Bool {
  let current =
    when before is {
      vi.Suspended { holds } ->
        when
          list.find(holds, fn(hold) { hold.by == module1 && hold.until > lo })
        is {
          Some(hold) -> hold.until
          None -> verdict_until
        }
      _ -> verdict_until
    }
  let until =
    if current > verdict_until {
      current
    } else {
      verdict_until
    }
  when after is {
    vi.Suspended { holds } ->
      list.has(
        holds,
        Hold { by: module1, reason: reason_dispute_verdict, since: lo, until },
      )
    _ -> False
  }
}
