// Module 6: Self-Improvement Module — Proposer Activity Tracking
// Rate limiting and cooldown enforcement via ProposerActivity UTXO

use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Address}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Transaction}
use governance_suggestion/settlement
use governance_suggestion/types.{
  GovernanceCrossRefs, GovernanceParams, ProposalDatum, ProposerActivityDatum,
}
use governance_suggestion/utils

// ============================================================================
// Rate Limiting
// ============================================================================

/// Rate limit of a proposal submission and the activity record of the
/// proposer (§4.6). `proposal` is the datum of the new proposal,
/// `proposal_token` its new token, `lo` the lower bound of the validity range
/// and `address` the address of the new proposal.
///
/// When the transaction spends the activity UTxO of the proposer (exactly one
/// input at the proposal validator with the activity token
/// utils.activity_token_name(proposer_did)), MUST:
///   1. lo >= last_proposal_slot + proposal_cooldown;
///   2. active_proposal_count < max_active_proposals;
///   3. an output continues the activity UTxO with active_proposal_count + 1
///      and last_proposal_slot = lo, all other fields unchanged
///      (settlement.continues);
///   4. the transaction mints exactly the proposal token under the proposal
///      policy, and burns nothing under it.
/// When it spends none (first proposal), MUST:
///   5. an output at `address` holds exactly the new activity token and AP3X,
///      with the datum {proposer_did, proposer_credential, 1, lo};
///   6. the transaction mints exactly the activity token and the proposal
///      token under the proposal policy.
/// Two activity inputs of the proposer fail.
pub fn validate_submit_activity(
  params: GovernanceParams,
  refs: GovernanceCrossRefs,
  proposal: ProposalDatum,
  proposal_token: ByteArray,
  lo: Int,
  address: Address,
  tx: Transaction,
) -> Bool {
  let token = utils.activity_token_name(proposal.proposer_did)
  let minted = dict.to_pairs(assets.tokens(tx.mint, refs.proposal_mint_hash))
  when activity_inputs(refs, token, tx.inputs) is {
    [input] -> {
      let activity = activity_datum(input)
      let next: Data =
        ProposerActivityDatum {
          ..activity,
          active_proposal_count: activity.active_proposal_count + 1,
          last_proposal_slot: lo,
        }
      and {
        lo >= activity.last_proposal_slot + params.proposal_cooldown,
        activity.active_proposal_count < params.max_active_proposals,
        settlement.continues(input, next, tx.outputs),
        minted == [Pair(proposal_token, 1)],
      }
    }
    [] -> {
      let first: Data =
        ProposerActivityDatum {
          agent_did: proposal.proposer_did,
          agent_credential: proposal.proposer_credential,
          active_proposal_count: 1,
          last_proposal_slot: lo,
        }
      and {
        // "pact_" sorts before "prop_"
        minted == [Pair(token, 1), Pair(proposal_token, 1)],
        list.any(
          tx.outputs,
          fn(output) {
            and {
              output.address == address,
              assets.without_lovelace(output.value) == assets.from_asset(
                refs.proposal_mint_hash,
                token,
                1,
              ),
              output.datum == InlineDatum(first),
            }
          },
        ),
      }
    }
    _ -> False
  }
}

/// Activity record at a proposal finalisation (withdraw, adopt, reject,
/// expire, expire stale).
///
/// MUST:
///   1. The transaction spends exactly one input at the proposal validator
///      with the activity token of `agent_did`.
///   2. Its active_proposal_count is at least 1.
///   3. An output continues it with active_proposal_count - 1, all other
///      fields unchanged (settlement.continues).
pub fn validate_finalize_activity(
  refs: GovernanceCrossRefs,
  agent_did: ByteArray,
  tx: Transaction,
) -> Bool {
  expect [input] =
    activity_inputs(refs, utils.activity_token_name(agent_did), tx.inputs)
  let activity = activity_datum(input)
  let next: Data =
    ProposerActivityDatum {
      ..activity,
      active_proposal_count: activity.active_proposal_count - 1,
    }
  activity.active_proposal_count >= 1 && settlement.continues(
    input,
    next,
    tx.outputs,
  )
}

// ============================================================================
// Helpers
// ============================================================================

/// The inputs at the proposal validator that hold the activity token `token`.
fn activity_inputs(
  refs: GovernanceCrossRefs,
  token: ByteArray,
  inputs: List<Input>,
) -> List<Input> {
  list.filter(
    inputs,
    fn(input) {
      settlement.is_at_script(input, refs.proposal_validator_hash) && assets.quantity_of(
        input.output.value,
        refs.proposal_mint_hash,
        token,
      ) == 1
    },
  )
}

/// The inline ProposerActivityDatum of an activity input.
fn activity_datum(input: Input) -> ProposerActivityDatum {
  expect InlineDatum(raw) = input.output.datum
  expect activity: ProposerActivityDatum = raw
  activity
}
