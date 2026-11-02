// Module 6: the activity list (validator level, §4.6).
//
// One activity record per agent: the records are a list at the proposal
// validator, sorted by agent_did. The head holds the `activity` record token;
// each node holds the activity token of its agent. The first submission of
// an agent inserts its node after its predecessor in the list
// (activity_tracking.validate_submit_activity), and SpendActivity accepts
// only a node or the head (proposal_validation.validate_spend_activity).
//
// Each test builds a full transaction and calls the spend handler of
// proposal_spend for the lock (SubmitProposal) or for a list entry
// (SpendActivity). A negative test is a `fail` test: the handler fails or
// returns False. Each negative test changes one part of a passing
// transaction. The agents are the test agents of shared/identity_fixtures:
// agent_id(1) < agent_id(2) < agent_id(3) in byte order.

use aiken/collection/list
use aiken/interval
use cardano/address.{Address, Credential, Inline, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction, ValidityRange,
}
use governance_suggestion/tests/test_helpers.{
  activity_token_mint, cross_refs_ref_input, default_proposal_datum,
  finalisation_tx, head_datum, head_input, head_output, make_ref, node_datum,
  node_input, node_output, node_value, params_ref_input, payment_output,
  proposal_address, proposal_mint_hash, proposal_output, proposal_own_ref,
  proposal_seed_input, proposal_token_mint, proposal_utxo_ref, spend_redeemer,
  test_config,
}
use governance_suggestion/types.{
  ProposalDatum, ProposerActivityDatum, SpendActivity, SubmitProposal,
  WithdrawProposal,
}
use governance_suggestion/utils
use proposal
use shared/identity_fixtures.{agent_id, controller_hash, record, record_input}

// ============================================================================
// Fixtures
// ============================================================================

/// The bounds of the 10-minute validity range of the test transactions.
const lo: Int = 2_000_000_000

const hi: Int = 2_000_600_000

fn ten_minutes() -> ValidityRange {
  interval.between(lo, hi)
}

/// The declared credential of agent `n`: its controller key.
fn cred(n: Int) -> Credential {
  VerificationKey(controller_hash(n))
}

/// The time of the last submission of the nodes in the list (before `lo` by
/// more than the cooldown).
const last_submission: Int = 1_000_000_000

/// The UTxO of the node of agent `n`.
fn node_ref(n: Int) -> OutputReference {
  make_ref(
    #"6666666666666666666666666666666666666666666666666666666666666666",
    n,
  )
}

/// The node of agent `n` (count 1) with `next`, at node_ref(n).
fn node(n: Int, next: ByteArray) -> Input {
  node_input(
    node_ref(n),
    node_datum(agent_id(n), cred(n), 1, last_submission, next),
  )
}

/// The new proposal of agent `n` (seed proposal_own_ref), submitted at `lo`.
fn proposal_of(n: Int) -> ProposalDatum {
  ProposalDatum {
    ..default_proposal_datum(),
    proposer_did: agent_id(n),
    proposer_credential: cred(n),
    submitted_at: lo,
  }
}

/// The datum of an entry of the list.
fn entry_datum(entry: Input) -> ProposerActivityDatum {
  expect InlineDatum(raw) = entry.output.datum
  expect datum: ProposerActivityDatum = raw
  datum
}

/// The output `entry` with the datum `datum`.
fn with_datum(entry: Input, datum: ProposerActivityDatum) -> Output {
  Output { ..entry.output, datum: InlineDatum(datum) }
}

/// The new node of agent `n` after an entry whose next_did is `next`.
fn new_node(n: Int, next: ByteArray) -> Output {
  node_output(node_datum(agent_id(n), cred(n), 1, lo, next))
}

/// The mint of a first submission of agent `n`: the activity token and the
/// proposal token.
fn first_mint(n: Int) -> assets.Value {
  proposal_token_mint(proposal_own_ref(), 1)
    |> assets.merge(activity_token_mint(agent_id(n), 1))
}

/// A submission of agent `n` that spends the lock and `entries` (each with
/// SpendActivity), with the outputs `outputs` and the mint `mint`.
fn submission(
  n: Int,
  entries: List<Input>,
  outputs: List<Output>,
  mint: assets.Value,
) -> Transaction {
  let submit: Data = SubmitProposal
  let spend_activity: Data = SpendActivity
  Transaction {
    ..transaction.placeholder,
    inputs: [proposal_seed_input(proposal_of(n)), ..entries],
    reference_inputs: [
      cross_refs_ref_input(),
      params_ref_input(),
      record_input(agent_id(n), record(n, [])),
    ],
    outputs: outputs,
    mint: mint,
    extra_signatories: [controller_hash(n)],
    validity_range: ten_minutes(),
    redeemers: [
      spend_redeemer(proposal_own_ref(), submit),
      ..list.map(
        entries,
        fn(entry) { spend_redeemer(entry.output_reference, spend_activity) },
      )
    ],
  }
}

/// The outputs of the insertion of agent `n` after `entry`: the proposal, the
/// new node and `entry` with next_did = agent n.
fn insertion_outputs(n: Int, entry: Input) -> List<Output> {
  let e = entry_datum(entry)
  [
    proposal_output(proposal_of(n)),
    new_node(n, e.next_did),
    with_datum(entry, ProposerActivityDatum { ..e, next_did: agent_id(n) }),
  ]
}

/// The first submission of agent `n` with the predecessor `entry`.
fn insertion(n: Int, entry: Input) -> Transaction {
  submission(n, [entry], insertion_outputs(n, entry), first_mint(n))
}

/// The insertion of agent `n` after `entry`, where the predecessor continues
/// with `continued` instead.
fn insertion_with_predecessor(
  n: Int,
  entry: Input,
  continued: Output,
) -> Transaction {
  let e = entry_datum(entry)
  submission(
    n,
    [entry],
    [proposal_output(proposal_of(n)), new_node(n, e.next_did), continued],
    first_mint(n),
  )
}

/// The SubmitProposal handler for the lock of `tx`.
fn submit(tx: Transaction) -> Bool {
  expect [lock, ..] = tx.inputs
  expect InlineDatum(spent) = lock.output.datum
  proposal.proposal_spend.spend(
    test_config(),
    Some(spent),
    SubmitProposal,
    proposal_own_ref(),
    tx,
  )
}

/// The SpendActivity handler for the list entry `entry` in `tx`.
fn spend_entry(entry: Input, tx: Transaction) -> Bool {
  expect InlineDatum(raw) = entry.output.datum
  proposal.proposal_spend.spend(
    test_config(),
    Some(raw),
    SpendActivity,
    entry.output_reference,
    tx,
  )
}

/// Every proposal_spend handler of an insertion after `entry`, and the mint.
fn accepted(entry: Input, tx: Transaction) -> Bool {
  and {
    submit(tx),
    spend_entry(entry, tx),
    proposal.proposal_mint.mint(
      test_config(),
      SubmitProposal,
      proposal_mint_hash,
      tx,
    ),
  }
}

// ============================================================================
// Insertion of the node of a first proposal
// ============================================================================

/// The list is empty: the node of agent 2 goes after the head.
test insert_into_empty_list() {
  let entry = head_input(#"")
  accepted(entry, insertion(2, entry))
}

/// The list is head -> 3: the node of agent 2 goes between the head and 3.
test insert_after_head_before_first_node() {
  let entry = head_input(agent_id(3))
  accepted(entry, insertion(2, entry))
}

/// The list is head -> 1 -> 3: the node of agent 2 goes between 1 and 3.
test insert_between_two_nodes() {
  let entry = node(1, agent_id(3))
  accepted(entry, insertion(2, entry))
}

/// The list is head -> 1: the node of agent 2 goes after the last node.
test insert_after_last_node() {
  let entry = node(1, #"")
  accepted(entry, insertion(2, entry))
}

/// The predecessor keeps more AP3X than before: allowed.
test insert_with_predecessor_more_ap3x() {
  let entry = node(1, #"")
  let continued =
    with_datum(entry, ProposerActivityDatum { ..entry_datum(entry), next_did: agent_id(2) })
  submit(
    insertion_with_predecessor(
      2,
      entry,
      Output {
        ..continued,
        value: assets.add(
          continued.value,
          assets.ada_policy_id,
          assets.ada_asset_name,
          1_000_000,
        ),
      },
    ),
  )
}

// ============================================================================
// One node per agent: a second insertion fails
// ============================================================================

/// The list is head -> 2 -> 3. Agent 2 has a node; the head names it, so the
/// head is not a predecessor of agent 2.
test second_insertion_after_head_fails() fail {
  let entry = head_input(agent_id(2))
  submit(insertion(2, entry))
}

/// The list is head -> 1 -> 3. Agent 3 has a node; node 1 names it.
test second_insertion_after_previous_node_fails() fail {
  let entry = node(1, agent_id(3))
  submit(insertion(3, entry))
}

/// The list is head -> 1 -> 2 -> 3. Agent 3 has a node; node 1 comes before
/// it, and its next_did (2) is below agent 3.
test second_insertion_after_earlier_node_fails() fail {
  let entry = node(1, agent_id(2))
  submit(insertion(3, entry))
}

/// The list is head -> 1 -> 2. Agent 1 has a node; the last node (2) comes
/// after it.
test second_insertion_after_last_node_fails() fail {
  let entry = node(2, #"")
  submit(insertion(1, entry))
}

// ============================================================================
// The predecessor: order and next_did
// ============================================================================

/// The node of agent 3 is not before agent 2.
test insert_after_greater_node_fails() fail {
  let entry = node(3, #"")
  submit(insertion(2, entry))
}

/// The list is head -> 1: agent 2 goes after node 1, not after the head.
test insert_past_next_did_fails() fail {
  let entry = head_input(agent_id(1))
  submit(insertion(2, entry))
}

// ============================================================================
// The predecessor continues with only next_did changed
// ============================================================================

fn changed_predecessor(change: fn(ProposerActivityDatum) -> ProposerActivityDatum) -> Bool {
  let entry = node(1, agent_id(3))
  let e = entry_datum(entry)
  submit(
    insertion_with_predecessor(
      2,
      entry,
      with_datum(entry, change(ProposerActivityDatum { ..e, next_did: agent_id(2) })),
    ),
  )
}

test insert_with_predecessor_count_changed_fails() fail {
  changed_predecessor(
    fn(d) { ProposerActivityDatum { ..d, active_proposal_count: 2 } },
  )
}

test insert_with_predecessor_slot_changed_fails() fail {
  changed_predecessor(fn(d) { ProposerActivityDatum { ..d, last_proposal_slot: lo } })
}

test insert_with_predecessor_credential_changed_fails() fail {
  changed_predecessor(fn(d) { ProposerActivityDatum { ..d, agent_credential: cred(2) } })
}

test insert_with_predecessor_agent_changed_fails() fail {
  changed_predecessor(fn(d) { ProposerActivityDatum { ..d, agent_did: agent_id(7) } })
}

/// The predecessor keeps its old next_did.
test insert_with_predecessor_next_did_kept_fails() fail {
  changed_predecessor(fn(d) { ProposerActivityDatum { ..d, next_did: agent_id(3) } })
}

test insert_with_predecessor_moved_fails() fail {
  let entry = node(1, agent_id(3))
  let continued =
    with_datum(entry, ProposerActivityDatum { ..entry_datum(entry), next_did: agent_id(2) })
  submit(
    insertion_with_predecessor(
      2,
      entry,
      Output {
        ..continued,
        address: Address { ..proposal_address(), stake_credential: Some(Inline(cred(2))) },
      },
    ),
  )
}

test insert_with_predecessor_value_taken_fails() fail {
  let entry = node(1, agent_id(3))
  let continued =
    with_datum(entry, ProposerActivityDatum { ..entry_datum(entry), next_did: agent_id(2) })
  submit(
    insertion_with_predecessor(
      2,
      entry,
      Output {
        ..continued,
        value: assets.from_asset(
          proposal_mint_hash,
          utils.activity_token_name(agent_id(1)),
          1,
        )
          |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 1_000_000),
      },
    ),
  )
}

/// The head continues without its record token.
test insert_with_head_token_taken_fails() fail {
  let entry = head_input(#"")
  submit(
    insertion_with_predecessor(
      2,
      entry,
      Output {
        ..head_output(agent_id(2)),
        value: assets.from_lovelace(3_000_000),
      },
    ),
  )
}

// ============================================================================
// The new node and the mint
// ============================================================================

fn insertion_with_node(n: Int, entry: Input, node_out: Output) -> Transaction {
  let e = entry_datum(entry)
  submission(
    n,
    [entry],
    [
      proposal_output(proposal_of(n)),
      node_out,
      with_datum(entry, ProposerActivityDatum { ..e, next_did: agent_id(n) }),
    ],
    first_mint(n),
  )
}

/// The new node must take the next_did of the predecessor (3), not #"".
test insert_with_new_node_next_did_wrong_fails() fail {
  submit(insertion_with_node(2, node(1, agent_id(3)), new_node(2, #"")))
}

test insert_with_new_node_count_wrong_fails() fail {
  submit(
    insertion_with_node(
      2,
      node(1, agent_id(3)),
      node_output(node_datum(agent_id(2), cred(2), 2, lo, agent_id(3))),
    ),
  )
}

test insert_with_new_node_credential_wrong_fails() fail {
  submit(
    insertion_with_node(
      2,
      node(1, agent_id(3)),
      node_output(node_datum(agent_id(2), cred(1), 1, lo, agent_id(3))),
    ),
  )
}

test insert_with_new_node_at_other_address_fails() fail {
  let out = new_node(2, agent_id(3))
  submit(
    insertion_with_node(
      2,
      node(1, agent_id(3)),
      Output {
        ..out,
        address: Address { ..proposal_address(), stake_credential: Some(Inline(cred(2))) },
      },
    ),
  )
}

/// The first submission mints the proposal token only.
test insert_without_activity_token_mint_fails() fail {
  let entry = node(1, #"")
  submit(
    submission(
      2,
      [entry],
      insertion_outputs(2, entry),
      proposal_token_mint(proposal_own_ref(), 1),
    ),
  )
}

// ============================================================================
// Exactly two inputs at the proposal validator
// ============================================================================

/// The insertion also spends the node of agent 3 with SpendActivity.
test insert_with_third_proposal_input_fails() fail {
  let entry = node(1, agent_id(3))
  let third = node(3, #"")
  submit(
    submission(
      2,
      [entry, third],
      list.concat(insertion_outputs(2, entry), [third.output]),
      first_mint(2),
    ),
  )
}

/// The next submission of agent 1 also spends the head.
test next_submission_with_head_spent_fails() fail {
  let own = node(1, #"")
  let head = head_input(agent_id(1))
  submit(
    submission(
      1,
      [own, head],
      [
        proposal_output(proposal_of(1)),
        with_datum(
          own,
          node_datum(agent_id(1), cred(1), 2, lo, #""),
        ),
        head.output,
      ],
      proposal_token_mint(proposal_own_ref(), 1),
    ),
  )
}

// ============================================================================
// The next submission and the finalisations keep next_did
// ============================================================================

/// The list is head -> 1 -> 3; agent 1 submits again.
fn next_submission(continued: ProposerActivityDatum) -> Transaction {
  let own = node(1, agent_id(3))
  submission(
    1,
    [own],
    [proposal_output(proposal_of(1)), with_datum(own, continued)],
    proposal_token_mint(proposal_own_ref(), 1),
  )
}

test next_submission_keeps_next_did() {
  let own = node(1, agent_id(3))
  let tx =
    next_submission(node_datum(agent_id(1), cred(1), 2, lo, agent_id(3)))
  submit(tx) && spend_entry(own, tx)
}

test next_submission_changing_next_did_fails() fail {
  submit(next_submission(node_datum(agent_id(1), cred(1), 2, lo, #"")))
}

/// The withdrawal of the proposal of agent 1 with its node (next_did 3), and
/// the node continued as `continued`.
fn withdrawal(continued: ProposerActivityDatum, extra: List<Input>) -> Transaction {
  let withdraw: Data = WithdrawProposal
  let spend_activity: Data = SpendActivity
  let own = node(1, agent_id(3))
  let datum = ProposalDatum { ..default_proposal_datum(), proposer_did: agent_id(1), proposer_credential: cred(1) }
  let tx =
    finalisation_tx(
      withdraw,
      datum,
      [cross_refs_ref_input(), params_ref_input()],
      [payment_output(cred(1), datum.stake_amount), with_datum(own, continued)],
      [controller_hash(1)],
      ten_minutes(),
    )
  // finalisation_tx spends a node of agent 1 at activity_utxo_ref; use the
  // node with next_did 3 instead.
  expect [proposal_in, _] = tx.inputs
  Transaction {
    ..tx,
    inputs: list.concat([proposal_in, own], extra),
    outputs: list.concat(
      tx.outputs,
      list.map(extra, fn(input) { input.output }),
    ),
    redeemers: list.concat(
      [
        spend_redeemer(proposal_utxo_ref(), withdraw),
        spend_redeemer(own.output_reference, spend_activity),
      ],
      list.map(
        extra,
        fn(input) { spend_redeemer(input.output_reference, spend_activity) },
      ),
    ),
  }
}

fn withdraw_proposal(tx: Transaction) -> Bool {
  expect [proposal_in, ..] = tx.inputs
  expect InlineDatum(raw) = proposal_in.output.datum
  proposal.proposal_spend.spend(
    test_config(),
    Some(raw),
    WithdrawProposal,
    proposal_utxo_ref(),
    tx,
  )
}

test withdrawal_keeps_next_did() {
  let tx =
    withdrawal(
      node_datum(agent_id(1), cred(1), 0, last_submission, agent_id(3)),
      [],
    )
  withdraw_proposal(tx) && spend_entry(node(1, agent_id(3)), tx)
}

test withdrawal_changing_next_did_fails() fail {
  withdraw_proposal(
    withdrawal(node_datum(agent_id(1), cred(1), 0, last_submission, #""), []),
  )
}

// ============================================================================
// SpendActivity: a node or the head; never the head in a finalisation
// ============================================================================

/// The head moves in the withdrawal of a proposal of agent 1 (it continues
/// unchanged).
test head_in_finalisation_fails() fail {
  let head = head_input(agent_id(1))
  let tx =
    withdrawal(
      node_datum(agent_id(1), cred(1), 0, last_submission, agent_id(3)),
      [head],
    )
  spend_entry(head, tx)
}

/// An output at the proposal validator with the head datum but without the
/// `activity` record token is not the head.
test insert_after_fake_head_fails() fail {
  let fake =
    Input {
      output_reference: make_ref(
        #"6767676767676767676767676767676767676767676767676767676767676767",
        0,
      ),
      output: Output {
        address: proposal_address(),
        value: assets.from_lovelace(3_000_000),
        datum: InlineDatum(head_datum(#"")),
        reference_script: None,
      },
    }
  spend_entry(fake, insertion(2, fake))
}

/// The head also holds a token of the proposal policy.
test head_with_proposal_token_fails() fail {
  let head = head_input(#"")
  let entry =
    Input {
      ..head,
      output: Output {
        ..head.output,
        value: assets.merge(head.output.value, node_value(agent_id(7))),
      },
    }
  spend_entry(entry, insertion(2, entry))
}

/// A node datum on a UTxO with the activity token of another agent.
test node_with_token_of_other_agent_fails() fail {
  let entry =
    Input {
      output_reference: node_ref(1),
      output: Output {
        ..node_output(node_datum(agent_id(1), cred(1), 1, last_submission, #"")),
        value: node_value(agent_id(7)),
      },
    }
  spend_entry(entry, insertion(2, entry))
}
