// Module 6: Self-Improvement Module — Critique & Endorsement Validation
// Spec reference: §5.4–5.9

use aiken/builtin
use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction,
}
use governance_suggestion/settlement
use governance_suggestion/types.{
  Amendment, CritiqueDatum, GovernanceConfig, GovernanceCrossRefs,
  GovernanceEndorsementDatum, GovernanceParams, ProposalDatum,
}
use governance_suggestion/utils
use shared/identity
use vector_identity.{find_record}
use vector_identity/types.{IdentityDatum, Root, Subagent} as identity_types

// ============================================================================
// §5.7 Submit Critique
// ============================================================================

/// Validate a new critique submission.
///
/// The transaction spends `own_ref`, a UTxO at the critique validator with
/// the datum `spent`, and creates the new critique: the output that holds the
/// new critique token utils.critique_token_name(own_ref). Every check reads
/// the datum of that output (the new datum).
///
/// MUST:
///   1. The validity range has finite bounds and is at most one hour long
///      (shared/identity.window), and the critic passes the identity check
///      (utils.agent_record) with the new datum.
///   2. The new datum names the critic and the credential of `spent`, so the
///      locked funds stay with their owner.
///   3. submitted_at is the lower bound of the validity range.
///   4. The proposal that proposal_ref names (named_proposal) is Open or
///      Amended.
///   5. Except for an Amendment critique, the critic and the proposer are
///      different parties (distinct_parties).
///   6. stake_amount >= min_critique_stake, the critique hash is 32 bytes and
///      incorporated = False.
///   7. The new critique is at the address of `own_ref`, holds at least the
///      stake, and holds exactly one token: the new critique token
///      (critique_mint checks the mint).
pub fn validate_submit_critique(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  spent: CritiqueDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  let token = utils.critique_token_name(own_ref)
  expect Some(output) = token_output(refs.critique_mint_hash, token, tx.outputs)
  expect InlineDatum(raw) = output.datum
  expect datum: CritiqueDatum = raw
  when identity.window(tx.validity_range) is {
    Some(w) ->
      when
        utils.agent_record(
          config,
          datum.critic_did,
          datum.critic_credential,
          w,
          tx.reference_inputs,
          tx.extra_signatories,
        )
      is {
        Some(critic_record) ->
          when named_proposal(refs, datum.proposal_ref, tx) is {
            Some(proposal) ->
              and {
                datum.critic_did == spent.critic_did,
                datum.critic_credential == spent.critic_credential,
                datum.submitted_at == w.lo,
                utils.is_proposal_active(proposal.state),
                when datum.critique_type is {
                  // The proposer can document its own improvements
                  Amendment { .. } -> True
                  _ ->
                    distinct_parties(
                      config,
                      datum.critic_did,
                      critic_record,
                      datum.critic_credential,
                      proposal,
                      tx.reference_inputs,
                    )
                },
                datum.stake_amount >= params.min_critique_stake,
                builtin.length_of_bytearray(datum.critique_hash) == 32,
                !datum.incorporated,
                new_position(
                  output,
                  own,
                  refs.critique_mint_hash,
                  token,
                  datum.stake_amount,
                ),
              }
            None -> False
          }
        None -> False
      }
    None -> False
  }
}

// ============================================================================
// §5.8 Withdraw Critique
// ============================================================================

/// Validate critique withdrawal (an exit).
///
/// MUST: the critique is not incorporated, an active proposal is a reference
/// input (has_active_proposal), and the critic credential signs. The rule
/// does not bind the proposal to proposal_ref: a critique whose proposal
/// moved or ended without it can still exit.
pub fn validate_withdraw_critique(
  datum: CritiqueDatum,
  reference_inputs: List<Input>,
  refs: GovernanceCrossRefs,
  signatories: List<ByteArray>,
) -> Bool {
  // 2. Not incorporated
  let not_incorporated = !datum.incorporated

  // 3. An active proposal is a reference input
  let proposal_active = has_active_proposal(refs, reference_inputs)

  // 6. Signed by critic
  let signer_valid = utils.verify_signer(datum.critic_credential, signatories)

  not_incorporated && proposal_active && signer_valid
}

// ============================================================================
// §5.9 Submit Endorsement
// ============================================================================

/// Validate a new endorsement submission.
///
/// The transaction spends `own_ref`, a UTxO at the endorsement validator with
/// the datum `spent`, and creates the new endorsement: the output that holds
/// the endorsement token utils.endorsement_token_name(endorser_did,
/// proposal_ref) of `spent`. Every check reads the datum of that output (the
/// new datum).
///
/// MUST:
///   1. The validity range has finite bounds and is at most one hour long,
///      and the endorser passes the identity check (utils.agent_record) with
///      the new datum.
///   2. The new datum names the endorser, the credential and the proposal of
///      `spent`, so the locked funds stay with their owner and the token name
///      matches the new datum.
///   3. created_at is the lower bound of the validity range.
///   4. The proposal that proposal_ref names (named_proposal) is Open or
///      Amended.
///   5. The endorser and the proposer are different parties
///      (distinct_parties).
///   6. stake_amount >= min_governance_endorsement.
///   7. The new endorsement is at the address of `own_ref`, holds at least
///      the stake, and holds exactly one token: the endorsement token
///      (critique_mint checks the mint).
pub fn validate_submit_endorsement(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  spent: GovernanceEndorsementDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  let token = utils.endorsement_token_name(spent.endorser_did, spent.proposal_ref)
  expect Some(output) = token_output(refs.critique_mint_hash, token, tx.outputs)
  expect InlineDatum(raw) = output.datum
  expect datum: GovernanceEndorsementDatum = raw
  when identity.window(tx.validity_range) is {
    Some(w) ->
      when
        utils.agent_record(
          config,
          datum.endorser_did,
          datum.endorser_credential,
          w,
          tx.reference_inputs,
          tx.extra_signatories,
        )
      is {
        Some(endorser_record) ->
          when named_proposal(refs, datum.proposal_ref, tx) is {
            Some(proposal) ->
              and {
                datum.endorser_did == spent.endorser_did,
                datum.endorser_credential == spent.endorser_credential,
                datum.proposal_ref == spent.proposal_ref,
                datum.created_at == w.lo,
                utils.is_proposal_active(proposal.state),
                distinct_parties(
                  config,
                  datum.endorser_did,
                  endorser_record,
                  datum.endorser_credential,
                  proposal,
                  tx.reference_inputs,
                ),
                datum.stake_amount >= params.min_governance_endorsement,
                new_position(
                  output,
                  own,
                  refs.critique_mint_hash,
                  token,
                  datum.stake_amount,
                ),
              }
            None -> False
          }
        None -> False
      }
    None -> False
  }
}

// ============================================================================
// Withdraw Endorsement
// ============================================================================

/// Validate endorsement withdrawal. Anytime — no lock.
pub fn validate_withdraw_endorsement(
  datum: GovernanceEndorsementDatum,
  signatories: List<ByteArray>,
) -> Bool {
  utils.verify_signer(datum.endorser_credential, signatories)
}

// ============================================================================
// Critique mint policy (critique and endorsement tokens)
// ============================================================================

/// Validate a mint or burn under the critique policy `policy_id`.
///
/// MUST:
///   1. The transaction spends an input at the critique validator or at the
///      endorsement validator, so a spend validator runs.
///   2. The tokens that the transaction mints under the policy (positive
///      quantities) are exactly the tokens of the critiques and endorsements
///      that it creates, each with quantity 1:
///        - utils.critique_token_name(r) for each input r at the critique
///          validator spent with MintCritiqueToken;
///        - utils.endorsement_token_name(endorser_did, proposal_ref) of the
///          datum of each input at the endorsement validator spent with
///          MintEndorsementToken.
///      Two creations with the same token name fail.
/// A burn needs no rule here: only the spend of the UTxO that holds a token
/// releases it, and the spend validators check the burn
/// (settlement.burns_held_tokens).
pub fn validate_critique_mint(
  refs: GovernanceCrossRefs,
  policy_id: PolicyId,
  tx: Transaction,
) -> Bool {
  let (created, spends) =
    list.foldr(
      tx.inputs,
      ([], False),
      fn(input, acc) {
        let (names, found) = acc
        if settlement.is_at_script(input, refs.critique_validator_hash) {
          if settlement.constr_index(settlement.spend_redeemer(tx, input)) == settlement.mint_index {
            ([utils.critique_token_name(input.output_reference), ..names], True)
          } else {
            (names, True)
          }
        } else if settlement.is_at_script(
          input,
          refs.endorsement_validator_hash,
        ) {
          if settlement.constr_index(settlement.spend_redeemer(tx, input)) == settlement.mint_index {
            expect endorsement: GovernanceEndorsementDatum =
              settlement.input_datum(tx, input)
            (
              [
                utils.endorsement_token_name(
                  endorsement.endorser_did,
                  endorsement.proposal_ref,
                ),
                ..names
              ],
              True,
            )
          } else {
            (names, True)
          }
        } else {
          (names, found)
        }
      },
    )
  let minted =
    list.count(
      dict.to_pairs(assets.tokens(tx.mint, policy_id)),
      fn(pair) { pair.2nd > 0 },
    )
  and {
    spends,
    minted == list.length(created),
    list.all(
      created,
      fn(name) { assets.quantity_of(tx.mint, policy_id, name) == 1 },
    ),
  }
}

// ============================================================================
// Incorporate Critique (called during AmendProposal)
// ============================================================================

/// Validate that a critique is being incorporated into a proposal amendment.
///
/// MUST:
///   1. The critique is not yet incorporated.
///   2. The transaction amends a proposal, and the amendment lists this
///      critique (own_ref) in its incorporated critiques.
///   3. The amended proposal is the proposal that the critique names: its
///      input holds the proposal token utils.proposal_token_name(proposal_ref).
///   4. No other input has the same address, value and datum as this critique.
///   5. Exactly one output continues the critique: same address, same value,
///      and the same datum except incorporated = True.
pub fn validate_incorporate_critique(
  refs: GovernanceCrossRefs,
  datum: CritiqueDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  let incorporated_datum: Data = CritiqueDatum { ..datum, incorporated: True }
  when settlement.amendment_of(refs, tx, own_ref) is {
    Some(amended) ->
      and {
        // 1. Must not already be incorporated
        !datum.incorporated,
        // 3. The amended proposal is the proposal that the critique names
        assets.quantity_of(
          amended.output.value,
          refs.proposal_mint_hash,
          utils.proposal_token_name(datum.proposal_ref),
        ) == 1,
        // 4. One input with this address, value and datum
        list.count(
          tx.inputs,
          fn(input) {
            input.output.address == own.output.address && input.output.value == own.output.value && input.output.datum == own.output.datum
          },
        ) == 1,
        // 5. One continuing output: same address and value, incorporated = True
        list.count(
          tx.outputs,
          fn(output) {
            output.address == own.output.address && output.value == own.output.value && output.datum == InlineDatum(
              incorporated_datum,
            )
          },
        ) == 1,
      }
    // 2. A proposal amendment in this transaction lists this critique
    None -> False
  }
}

// ============================================================================
// Reward Critique (called during AdoptProposal)
// ============================================================================

/// Validate the settlement of an incorporated critique at proposal adoption.
///
/// MUST:
///   1. The critique is incorporated.
///   2. The transaction adopts the proposal that the critique names
///      (settlement.adopts).
///   3. The critique tokens that the critique holds are burned.
///   4. The outputs to the critic hold at least the lovelace that the
///      transaction returns to the critic (settlement.owner_paid).
pub fn validate_reward_critique(
  refs: GovernanceCrossRefs,
  datum: CritiqueDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  and {
    datum.incorporated,
    settlement.adopts(refs, datum.proposal_ref, tx),
    settlement.burns_held_tokens(refs.critique_mint_hash, own, tx),
    settlement.owner_paid(refs, datum.critic_credential, tx),
  }
}

// ============================================================================
// Burn Critique Token (called during proposal finalisation)
// ============================================================================

/// Validate the settlement of a critique at proposal finalisation.
///
/// MUST:
///   1. The transaction finalises (withdraw, adopt, reject, expire or expire
///      stale) the proposal that the critique names, or the critic
///      credential signs and the transaction finalises any proposal
///      (settlement.settles).
///   2. The critique tokens that the critique holds are burned.
///   3. The outputs to the critic hold at least the lovelace that the
///      transaction returns to the critic (settlement.owner_paid).
pub fn validate_burn_critique(
  refs: GovernanceCrossRefs,
  datum: CritiqueDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  and {
    settlement.settles(refs, datum.proposal_ref, datum.critic_credential, tx),
    settlement.burns_held_tokens(refs.critique_mint_hash, own, tx),
    settlement.owner_paid(refs, datum.critic_credential, tx),
  }
}

// ============================================================================
// Burn Endorsement Token (called during proposal finalisation)
// ============================================================================

/// Validate the settlement of a governance endorsement at proposal
/// finalisation.
///
/// MUST:
///   1. The transaction finalises the proposal that the endorsement names, or
///      the endorser credential signs and the transaction finalises any
///      proposal (settlement.settles).
///   2. The endorsement tokens (critique mint policy) that the endorsement
///      holds are burned.
///   3. The outputs to the endorser hold at least the lovelace that the
///      transaction returns to the endorser (settlement.owner_paid).
pub fn validate_burn_endorsement(
  refs: GovernanceCrossRefs,
  datum: GovernanceEndorsementDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  and {
    settlement.settles(
      refs,
      datum.proposal_ref,
      datum.endorser_credential,
      tx,
    ),
    settlement.burns_held_tokens(refs.critique_mint_hash, own, tx),
    settlement.owner_paid(refs, datum.endorser_credential, tx),
  }
}

// ============================================================================
// Internal Helpers
// ============================================================================

/// The output that holds the token (policy, name), when there is one. A new
/// token with quantity 1 is in at most one output.
fn token_output(
  policy: PolicyId,
  name: ByteArray,
  outputs: List<Output>,
) -> Option<Output> {
  list.find(
    outputs,
    fn(output) { assets.quantity_of(output.value, policy, name) == 1 },
  )
}

/// True when the new position `output` is at the address of the spent input
/// `own`, holds at least `stake` AP3X, and holds exactly one token: (policy,
/// name) with quantity 1.
fn new_position(
  output: Output,
  own: Input,
  policy: PolicyId,
  name: ByteArray,
  stake: Int,
) -> Bool {
  and {
    output.address == own.output.address,
    utils.lovelace_of(output.value) >= stake,
    assets.without_lovelace(output.value) == assets.from_asset(policy, name, 1),
  }
}

/// The datum of the proposal that `proposal_ref` names: the input or
/// reference input at the proposal validator that holds the proposal token
/// utils.proposal_token_name(proposal_ref). None when the transaction has no
/// such input.
fn named_proposal(
  refs: GovernanceCrossRefs,
  proposal_ref: OutputReference,
  tx: Transaction,
) -> Option<ProposalDatum> {
  let token = utils.proposal_token_name(proposal_ref)
  let names =
    fn(input: Input) {
      settlement.is_at_script(input, refs.proposal_validator_hash) && assets.quantity_of(
        input.output.value,
        refs.proposal_mint_hash,
        token,
      ) == 1
    }
  let found =
    when list.find(tx.reference_inputs, names) is {
      None -> list.find(tx.inputs, names)
      some -> some
    }
  when found is {
    Some(input) -> {
      expect InlineDatum(raw) = input.output.datum
      expect proposal: ProposalDatum = raw
      Some(proposal)
    }
    None -> None
  }
}

/// True when the agent (`agent_did`, its Active record `agent_record` and its
/// declared credential `agent_credential`) and the proposer of `proposal`
/// are different parties. The proposer's record must be a reference input
/// (vector_identity.find_record, any status). The rule fails when:
///   1. the identifiers are equal;
///   2. the two records have the same controller key;
///   3. the agent's credential equals the proposer credential;
///   4. one record is a subagent of the other (its lineage parent is the
///      other identifier);
///   5. both records are subagents of the same parent.
/// Only the direct parent counts; deeper ancestry is not compared.
fn distinct_parties(
  config: GovernanceConfig,
  agent_did: ByteArray,
  agent_record: IdentityDatum,
  agent_credential: Credential,
  proposal: ProposalDatum,
  reference_inputs: List<Input>,
) -> Bool {
  when
    find_record(reference_inputs, config.identity_hash, proposal.proposer_did)
  is {
    Some(proposer_record) -> {
      let agent_parent = parent_of(agent_record)
      let proposer_parent = parent_of(proposer_record)
      and {
        agent_did != proposal.proposer_did,
        agent_record.controller != proposer_record.controller,
        agent_credential != proposal.proposer_credential,
        agent_parent != Some(proposal.proposer_did),
        proposer_parent != Some(agent_did),
        agent_parent == None || agent_parent != proposer_parent,
      }
    }
    None -> False
  }
}

/// The parent identifier of a subagent record, None for a root record.
fn parent_of(record: IdentityDatum) -> Option<ByteArray> {
  when record.lineage is {
    Subagent { parent, .. } -> Some(parent)
    Root -> None
  }
}

/// True when a reference input at the proposal validator has an inline
/// ProposalDatum in state Open or Amended.
fn has_active_proposal(
  refs: GovernanceCrossRefs,
  reference_inputs: List<Input>,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      if settlement.is_at_script(input, refs.proposal_validator_hash) {
        when input.output.datum is {
          InlineDatum(raw) -> {
            expect datum: ProposalDatum = raw
            utils.is_proposal_active(datum.state)
          }
          _ -> False
        }
      } else {
        False
      }
    },
  )
}
