// Module 6: Self-Improvement Module — Settlement Rules
// Rules that bind a critique or endorsement spend to a proposal action in the
// same transaction, and that return stakes to their owners.

use aiken/collection/dict
use aiken/collection/list
use aiken/collection/pairs
use cardano/address.{Credential, Script as ScriptCredential}
use cardano/assets.{PolicyId}
use cardano/transaction.{
  DatumHash, InlineDatum, Input, NoDatum, Output, OutputReference, Spend,
  Transaction,
}

use governance_suggestion/types.{
  AdoptProposal, AmendProposal, BurnCritiqueToken, BurnEndorsementToken,
  CritiqueAction, CritiqueDatum, ExpireProposal, ExpireStaleProposal,
  GovernanceCrossRefs, GovernanceEndorsementAction, GovernanceEndorsementDatum,
  ProposalAction, ProposalDatum, RejectProposal, RewardCritique,
  WithdrawProposal,
}

// ============================================================================
// Inputs and redeemers
// ============================================================================

/// The input that `own_ref` spends. Fails when the transaction does not spend it.
pub fn own_input(tx: Transaction, own_ref: OutputReference) -> Input {
  expect Some(input) =
    list.find(tx.inputs, fn(input) { input.output_reference == own_ref })
  input
}

/// True when the input is at the script `hash`.
fn is_at_script(input: Input, hash: ByteArray) -> Bool {
  when input.output.address.payment_credential is {
    ScriptCredential(h) -> h == hash
    _ -> False
  }
}

/// The spend redeemer of an input. Fails when the input has none.
fn spend_redeemer(tx: Transaction, input: Input) -> Data {
  expect Some(redeemer) =
    pairs.get_first(tx.redeemers, Spend(input.output_reference))
  redeemer
}

/// The datum of an input, inline or by hash. Fails when the input has none.
fn input_datum(tx: Transaction, input: Input) -> Data {
  when input.output.datum is {
    InlineDatum(raw) -> raw
    DatumHash(hash) -> {
      expect Some(raw) = dict.get(tx.datums, hash)
      raw
    }
    NoDatum -> fail @"input without datum"
  }
}

/// The ProposalAction redeemers of all inputs at the proposal validator.
fn proposal_actions(
  refs: GovernanceCrossRefs,
  tx: Transaction,
) -> List<ProposalAction> {
  list.foldr(
    tx.inputs,
    [],
    fn(input, acc) {
      if is_at_script(input, refs.proposal_validator_hash) {
        expect action: ProposalAction = spend_redeemer(tx, input)
        [action, ..acc]
      } else {
        acc
      }
    },
  )
}

// ============================================================================
// Proposal actions in the same transaction
// ============================================================================

/// True for the proposal actions that finalise a proposal: withdrawal,
/// adoption, rejection, expiration and stale expiration. Each of them burns
/// the proposal token.
pub fn is_finalisation(action: ProposalAction) -> Bool {
  when action is {
    WithdrawProposal -> True
    AdoptProposal { .. } -> True
    RejectProposal { .. } -> True
    ExpireProposal -> True
    ExpireStaleProposal -> True
    _ -> False
  }
}

/// True when the transaction finalises a proposal.
pub fn has_proposal_finalisation(
  refs: GovernanceCrossRefs,
  tx: Transaction,
) -> Bool {
  list.any(proposal_actions(refs, tx), is_finalisation)
}

/// True when the transaction adopts a proposal.
pub fn has_proposal_adoption(refs: GovernanceCrossRefs, tx: Transaction) -> Bool {
  list.any(
    proposal_actions(refs, tx),
    fn(action) {
      when action is {
        AdoptProposal { .. } -> True
        _ -> False
      }
    },
  )
}

/// True when the transaction amends a proposal and the amendment lists
/// `critique_ref` in its incorporated critiques.
pub fn amendment_incorporates(
  refs: GovernanceCrossRefs,
  tx: Transaction,
  critique_ref: OutputReference,
) -> Bool {
  list.any(
    proposal_actions(refs, tx),
    fn(action) {
      when action is {
        AmendProposal { incorporated, .. } -> list.has(incorporated, critique_ref)
        _ -> False
      }
    },
  )
}

// ============================================================================
// Token burn
// ============================================================================

/// True when every token of `policy` that `input` holds is burned: for each
/// such asset name, the transaction burns all units that its inputs hold.
pub fn burns_held_tokens(
  policy: PolicyId,
  input: Input,
  tx: Transaction,
) -> Bool {
  dict.foldl(
    assets.tokens(input.output.value, policy),
    True,
    fn(name, _quantity, acc) {
      let held =
        list.foldl(
          tx.inputs,
          0,
          fn(i, total) {
            total + assets.quantity_of(i.output.value, policy, name)
          },
        )
      acc && assets.quantity_of(tx.mint, policy, name) == -held
    },
  )
}

// ============================================================================
// Stake return
// ============================================================================

/// True when the outputs to `owner` hold at least the lovelace that the
/// transaction returns to `owner`.
///
/// The transaction returns to `owner` the lovelace of each input that it
/// spends with a settling action and that names `owner`:
///   - critique inputs spent with RewardCritique or BurnCritiqueToken,
///   - endorsement inputs at `endorsement_hash` spent with
///     BurnEndorsementToken (only when `endorsement_hash` is given),
///   - proposal inputs spent with a finalising action.
/// The outputs to `owner` are summed, so one output cannot count for two
/// inputs.
pub fn owner_paid(
  refs: GovernanceCrossRefs,
  endorsement_hash: Option<ByteArray>,
  owner: Credential,
  tx: Transaction,
) -> Bool {
  let paid =
    list.foldl(
      tx.outputs,
      0,
      fn(output: Output, total) {
        if output.address.payment_credential == owner {
          total + assets.lovelace_of(output.value)
        } else {
          total
        }
      },
    )
  paid >= owed_to(refs, endorsement_hash, owner, tx)
}

/// The lovelace that the transaction returns to `owner` (see owner_paid).
fn owed_to(
  refs: GovernanceCrossRefs,
  endorsement_hash: Option<ByteArray>,
  owner: Credential,
  tx: Transaction,
) -> Int {
  list.foldl(
    tx.inputs,
    0,
    fn(input, total) {
      let returned =
        if is_at_script(input, refs.critique_validator_hash) {
          expect action: CritiqueAction = spend_redeemer(tx, input)
          when action is {
            RewardCritique { .. } | BurnCritiqueToken -> {
              expect critique: CritiqueDatum = input_datum(tx, input)
              critique.critic_credential == owner
            }
            _ -> False
          }
        } else if is_at_script(input, refs.proposal_validator_hash) {
          expect action: ProposalAction = spend_redeemer(tx, input)
          if is_finalisation(action) {
            expect proposal: ProposalDatum = input_datum(tx, input)
            proposal.proposer_credential == owner
          } else {
            False
          }
        } else {
          when endorsement_hash is {
            Some(hash) ->
              if is_at_script(input, hash) {
                expect action: GovernanceEndorsementAction =
                  spend_redeemer(tx, input)
                when action is {
                  BurnEndorsementToken -> {
                    expect endorsement: GovernanceEndorsementDatum =
                      input_datum(tx, input)
                    endorsement.endorser_credential == owner
                  }
                  _ -> False
                }
              } else {
                False
              }
            None -> False
          }
        }
      if returned {
        total + assets.lovelace_of(input.output.value)
      } else {
        total
      }
    },
  )
}
