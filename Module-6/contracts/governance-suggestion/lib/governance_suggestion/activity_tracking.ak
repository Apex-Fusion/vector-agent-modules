// Module 6: Self-Improvement Module — Proposer Activity Tracking
// Rate limiting and cooldown enforcement via the activity list (§4.6)

use aiken/builtin
use aiken/collection/dict
use aiken/collection/list
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Transaction}
use governance_suggestion/settlement
use governance_suggestion/types.{
  GovernanceCrossRefs, GovernanceParams, ProposalDatum, ProposerActivityDatum,
}
use governance_suggestion/utils

// ============================================================================
// The activity list
// ============================================================================
//
// The activity records are one list at the proposal validator
// (types.ProposerActivityDatum): the head, then one node per agent in
// ascending order of agent_did (builtin.less_than_bytearray). Each entry
// names the agent_did of the next node in next_did; the last entry has
// next_did = #"".
//
// Invariant: the entries that the head reaches through next_did are all the
// nodes, each agent_did occurs once, and the order is strictly ascending.
//
// - The deployment creates the head with next_did = #"": the list is empty.
//   The head token is a one-shot record token, so there is one head.
// - A node exists only after an insertion. The activity token of an agent is
//   minted only by a first submission (validate_submit_activity), and the
//   new node holds it. No action burns it or moves it out of the node, and
//   no action moves the head token out of the head.
// - An insertion of agent X spends one entry P with P.agent_did < X and
//   (P.next_did == #"" or X < P.next_did). P continues with next_did = X and
//   the new node gets next_did = P.next_did. All other entries stay. The
//   order stays strictly ascending.
// - Every other change of an entry keeps next_did: the next submission of an
//   agent (count + 1) and each finalisation (count - 1).
//
// Proof sketch (one node per agent): suppose X already has a node. Its
// predecessor P0 has P0.next_did = X. Every other entry A with
// A.agent_did < X comes before P0 in the list, so A.next_did <= P0.agent_did
// < X. P0 itself has next_did = X, and X < X is false. Every entry after the
// node of X has agent_did >= X. So no entry P has P.agent_did < X together
// with (P.next_did == #"" or X < P.next_did), and an insertion of X fails.
// A transaction cannot spend two entries for one submission: the submission
// spends exactly two inputs at the proposal validator (the lock and one
// entry).

// ============================================================================
// Rate limiting and insertion
// ============================================================================

/// Rate limit of a proposal submission and the activity list (§4.6).
/// `proposal` is the datum of the new proposal, `proposal_token` its new
/// token, `lo` the lower bound of the validity range and `lock` the input
/// that SubmitProposal spends.
///
/// MUST: the transaction spends exactly two inputs at the proposal
/// validator: `lock` and one entry of the activity list E (its SpendActivity
/// checks that E is a node or the head: proposal_validation
/// .validate_spend_activity).
///
/// When E holds the activity token of the proposer X (its node):
///   1. lo >= last_proposal_slot + proposal_cooldown;
///   2. active_proposal_count < max_active_proposals;
///   3. an output continues E with active_proposal_count + 1 and
///      last_proposal_slot = lo, all other fields unchanged (next_did too)
///      (settlement.continues);
///   4. the transaction mints exactly the proposal token under the proposal
///      policy, and burns nothing under it.
/// Else (first proposal of X), E is the predecessor P of X:
///   5. P.agent_did < X, and P.next_did == #"" or X < P.next_did;
///   6. an output continues P with next_did = X, all other fields unchanged
///      (settlement.continues: the same address and tokens, at least the
///      same AP3X);
///   7. an output at the address of `lock` holds exactly the new activity
///      token of X and AP3X, with the datum
///      {X, proposer_credential, 1, lo, P.next_did};
///   8. the transaction mints exactly the activity token of X and the
///      proposal token under the proposal policy.
pub fn validate_submit_activity(
  params: GovernanceParams,
  refs: GovernanceCrossRefs,
  proposal: ProposalDatum,
  proposal_token: ByteArray,
  lo: Int,
  lock: Input,
  tx: Transaction,
) -> Bool {
  let agent = proposal.proposer_did
  let token = utils.activity_token_name(agent)
  let minted = dict.to_pairs(assets.tokens(tx.mint, refs.proposal_mint_hash))
  // The lock and exactly one entry of the activity list
  expect [input] =
    list.filter(
      tx.inputs,
      fn(i) {
        settlement.is_at_script(i, refs.proposal_validator_hash) && i.output_reference != lock.output_reference
      },
    )
  let entry = activity_datum(input)
  if assets.quantity_of(input.output.value, refs.proposal_mint_hash, token) == 1 {
    // The node of the proposer: the next submission
    let next: Data =
      ProposerActivityDatum {
        ..entry,
        active_proposal_count: entry.active_proposal_count + 1,
        last_proposal_slot: lo,
      }
    and {
      entry.agent_did == agent,
      lo >= entry.last_proposal_slot + params.proposal_cooldown,
      entry.active_proposal_count < params.max_active_proposals,
      settlement.continues(input, next, tx.outputs),
      minted == [Pair(proposal_token, 1)],
    }
  } else {
    // The first submission: insert the node of the proposer after `entry`
    let predecessor: Data = ProposerActivityDatum { ..entry, next_did: agent }
    let node: Data =
      ProposerActivityDatum {
        agent_did: agent,
        agent_credential: proposal.proposer_credential,
        active_proposal_count: 1,
        last_proposal_slot: lo,
        next_did: entry.next_did,
      }
    and {
      builtin.less_than_bytearray(entry.agent_did, agent),
      entry.next_did == #"" || builtin.less_than_bytearray(
        agent,
        entry.next_did,
      ),
      settlement.continues(input, predecessor, tx.outputs),
      // "pact_" sorts before "prop_"
      minted == [Pair(token, 1), Pair(proposal_token, 1)],
      list.any(
        tx.outputs,
        fn(output) {
          and {
            output.address == lock.output.address,
            assets.without_lovelace(output.value) == assets.from_asset(
              refs.proposal_mint_hash,
              token,
              1,
            ),
            output.datum == InlineDatum(node),
          }
        },
      ),
    }
  }
}

/// Activity record at a proposal finalisation (withdraw, adopt, reject,
/// expire, expire stale).
///
/// MUST:
///   1. The transaction spends exactly one input at the proposal validator
///      with the activity token of `agent_did` (the node of the agent).
///   2. Its active_proposal_count is at least 1.
///   3. An output continues it with active_proposal_count - 1, all other
///      fields unchanged (next_did too) (settlement.continues).
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
