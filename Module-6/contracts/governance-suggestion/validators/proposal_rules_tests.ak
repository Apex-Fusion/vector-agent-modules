// Module 6: proposal and activity rules (validator level).
//
// Each test builds a full transaction and calls the spend handler of
// proposal_spend or the mint handler of proposal_mint. A negative test is a
// `fail` test: the handler fails or returns False. Each negative test changes
// one part of a passing transaction of the same builder. The rules:
//   - a transaction has one proposal action (every redeemer);
//   - SpendActivity moves only the activity of the agent of that action, when
//     the action is a submission or a finalisation;
//   - the continuing activity and proposal outputs keep the address and the
//     tokens, keep at least the AP3X, and carry the exact new datum;
//   - the stake returns to the stored proposer credential, with the outputs
//     summed (settlement.owner_paid), also at adoption;
//   - the exact tokens under the proposal policy;
//   - a submission reads the new proposal output, and submitted_at is the
//     lower bound of the validity range;
//   - the emergency gate reads the proposer's own Module 3 stake.

use aiken/collection/list
use aiken/crypto.{blake2b_256}
use aiken/interval
use cardano/address.{Address, Credential, Inline, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction, ValidityRange,
}
use governance_suggestion/tests/test_helpers.{
  activity_input, activity_output, activity_output_for, activity_token_mint,
  activity_utxo_ref, critic_cred, critique_mint_hash, critique_validator_hash,
  cross_refs_ref_input, default_proposal_datum, finalisation_outputs,
  finalisation_tx, make_ref, oracle_key_hash, oracle_ref_input,
  params_ref_input, payment_output, proposal_input, proposal_mint_hash,
  proposal_output, proposal_own_ref, proposal_seed_input, proposal_token_mint,
  proposal_utxo_ref, proposal_validator_hash, proposer_agent, proposer_cred,
  proposer_did, proposer_key_hash, protocol_params_ref_input, spend_redeemer,
  test_config,
}
use governance_suggestion/types.{
  AdoptProposal, AmendProposal, Amended, BurnCritiqueToken, CritiqueDatum,
  Emergency, ExpireProposal, ExpireStaleProposal, ExtendReview,
  GovernanceConfig, ParameterChange, ProposalAction, ProposalDatum,
  ProposerActivityDatum, RejectProposal, SpendActivity, SubmitProposal,
  WithdrawProposal,
}
use governance_suggestion/utils
use proposal
use shared/identity_fixtures.{agent_id, controller_hash, record, record_input}
use shared/token_naming

// ============================================================================
// Fixtures
// ============================================================================

/// The bounds of the 10-minute validity range of the test transactions.
const lo: Int = 2_000_000_000

const hi: Int = 2_000_600_000

fn ten_minutes() -> ValidityRange {
  interval.between(lo, hi)
}

/// The other agent (agent 2) and its proposal seed.
const other_agent: Int = 2

fn other_did() -> ByteArray {
  agent_id(other_agent)
}

fn other_cred() -> Credential {
  VerificationKey(controller_hash(other_agent))
}

fn other_seed() -> OutputReference {
  make_ref(
    #"4444444444444444444444444444444444444444444444444444444444444444",
    0,
  )
}

/// The proposer's identity record (agent 1).
fn proposer_record() -> Input {
  record_input(agent_id(proposer_agent), record(proposer_agent, []))
}

/// The proposal of agent 1 (seed proposal_own_ref), open, expired at `lo`.
fn proposal_datum() -> ProposalDatum {
  default_proposal_datum()
}

fn spend_proposal(
  datum: Data,
  action: ProposalAction,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  proposal.proposal_spend.spend(test_config(), Some(datum), action, own_ref, tx)
}

/// An input at the proposal validator at `ref` that holds `value`, with the
/// inline datum `datum`.
fn proposal_utxo(
  ref: OutputReference,
  value: assets.Value,
  datum: Data,
) -> Input {
  Input {
    output_reference: ref,
    output: Output {
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: None,
      },
      value,
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The open proposal of agent 2 (seed other_seed) at its UTxO.
fn other_proposal_input() -> Input {
  let datum: Data =
    ProposalDatum {
      ..proposal_datum(),
      proposer_did: other_did(),
      proposer_credential: other_cred(),
    }
  proposal_utxo(
    make_ref(
      #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0009",
      0,
    ),
    assets.from_lovelace(25_000_000)
      |> assets.add(proposal_mint_hash, utils.proposal_token_name(other_seed()), 1),
    datum,
  )
}

/// `tx` with a second proposal action: the proposal of agent 2, spent with
/// ExpireProposal.
fn with_second_action(tx: Transaction) -> Transaction {
  let expire: Data = ExpireProposal
  let other = other_proposal_input()
  Transaction {
    ..tx,
    inputs: list.concat(tx.inputs, [other]),
    redeemers: [spend_redeemer(other.output_reference, expire), ..tx.redeemers],
  }
}

/// The activity datum of agent 1 in finalisation_tx (count 1).
fn activity_datum() -> Data {
  let datum: Data =
    ProposerActivityDatum {
      agent_did: proposer_did,
      agent_credential: proposer_cred(),
      active_proposal_count: 1,
      last_proposal_slot: 1_000_000_000,
    }
  datum
}

// ============================================================================
// Transactions of each redeemer (all pass)
// ============================================================================

fn withdraw_with(outputs: List<Output>) -> Transaction {
  let withdraw: Data = WithdrawProposal
  finalisation_tx(
    withdraw,
    proposal_datum(),
    [cross_refs_ref_input(), params_ref_input()],
    outputs,
    [proposer_key_hash],
    ten_minutes(),
  )
}

fn withdraw_tx() -> Transaction {
  withdraw_with(finalisation_outputs(proposal_datum()))
}

fn expire_tx() -> Transaction {
  let expire: Data = ExpireProposal
  finalisation_tx(
    expire,
    proposal_datum(),
    [cross_refs_ref_input(), params_ref_input()],
    finalisation_outputs(proposal_datum()),
    [],
    ten_minutes(),
  )
}

fn stale_datum() -> ProposalDatum {
  ProposalDatum {
    ..proposal_datum(),
    proposal_type: ParameterChange {
      param_name: "MIN_CLAIM_STAKE",
      current_value: 50_000_000,
      proposed_value: 25_000_000,
    },
  }
}

fn expire_stale_tx() -> Transaction {
  let stale: Data = ExpireStaleProposal
  finalisation_tx(
    stale,
    stale_datum(),
    [cross_refs_ref_input(), params_ref_input(), protocol_params_ref_input()],
    finalisation_outputs(stale_datum()),
    [],
    ten_minutes(),
  )
}

fn adoption() -> ProposalAction {
  AdoptProposal {
    reasoning_hash: blake2b_256("adoption reasoning"),
    reward_amount: 100_000_000,
  }
}

fn adopt_with(outputs: List<Output>) -> Transaction {
  let adopt: Data = adoption()
  finalisation_tx(
    adopt,
    proposal_datum(),
    [cross_refs_ref_input(), params_ref_input(), oracle_ref_input()],
    outputs,
    [oracle_key_hash],
    ten_minutes(),
  )
}

fn adopt_tx() -> Transaction {
  adopt_with(finalisation_outputs(proposal_datum()))
}

fn rejection() -> ProposalAction {
  RejectProposal { reasoning_hash: blake2b_256("rejection reasoning") }
}

fn reject_tx() -> Transaction {
  let reject: Data = rejection()
  finalisation_tx(
    reject,
    proposal_datum(),
    [cross_refs_ref_input(), params_ref_input(), oracle_ref_input()],
    finalisation_outputs(proposal_datum()),
    [oracle_key_hash],
    ten_minutes(),
  )
}

fn amendment() -> ProposalAction {
  AmendProposal {
    new_hash: blake2b_256("amended proposal"),
    new_uri: "ipfs://QmAmended",
    incorporated: [],
  }
}

/// The new datum of the amendment.
fn amended_datum() -> ProposalDatum {
  let datum = proposal_datum()
  ProposalDatum {
    ..datum,
    state: Amended { previous_hash: datum.proposal_hash },
    proposal_hash: blake2b_256("amended proposal"),
    storage_uri: "ipfs://QmAmended",
    amendment_count: 1,
  }
}

/// An amendment of the proposal of agent 1 with the continuing outputs
/// `outputs` and the mint `mint`.
fn amend_with(outputs: List<Output>, mint: assets.Value) -> Transaction {
  let amend: Data = amendment()
  Transaction {
    ..transaction.placeholder,
    inputs: [proposal_input(proposal_datum(), proposal_own_ref())],
    reference_inputs: [
      cross_refs_ref_input(),
      params_ref_input(),
      proposer_record(),
    ],
    outputs: outputs,
    mint: mint,
    extra_signatories: [proposer_key_hash],
    validity_range: ten_minutes(),
    redeemers: [spend_redeemer(proposal_utxo_ref(), amend)],
  }
}

fn amend_tx() -> Transaction {
  amend_with([proposal_output(amended_datum())], assets.zero)
}

fn extension() -> ProposalAction {
  ExtendReview { additional_slots: 100_000_000 }
}

/// The new datum of the extension.
fn extended_datum() -> ProposalDatum {
  let datum = proposal_datum()
  ProposalDatum { ..datum, review_window: datum.review_window + 100_000_000 }
}

fn extend_with(outputs: List<Output>, mint: assets.Value) -> Transaction {
  let extend: Data = extension()
  Transaction {
    ..transaction.placeholder,
    inputs: [proposal_input(proposal_datum(), proposal_own_ref())],
    reference_inputs: [
      cross_refs_ref_input(),
      params_ref_input(),
      oracle_ref_input(),
    ],
    outputs: outputs,
    mint: mint,
    extra_signatories: [oracle_key_hash],
    validity_range: ten_minutes(),
    redeemers: [spend_redeemer(proposal_utxo_ref(), extend)],
  }
}

fn extend_tx() -> Transaction {
  extend_with([proposal_output(extended_datum())], assets.zero)
}

/// The new proposal of agent 1, submitted at `lo`.
fn new_proposal() -> ProposalDatum {
  ProposalDatum { ..proposal_datum(), submitted_at: lo }
}

/// The first submission of agent 1: the seed holds `spent`, and the outputs
/// are `outputs`, with the mint `mint`.
fn submit_with(
  spent: ProposalDatum,
  outputs: List<Output>,
  mint: assets.Value,
) -> Transaction {
  let submit: Data = SubmitProposal
  Transaction {
    ..transaction.placeholder,
    inputs: [proposal_seed_input(spent)],
    reference_inputs: [
      cross_refs_ref_input(),
      params_ref_input(),
      proposer_record(),
    ],
    outputs: outputs,
    mint: mint,
    extra_signatories: [proposer_key_hash],
    validity_range: ten_minutes(),
    redeemers: [spend_redeemer(proposal_own_ref(), submit)],
  }
}

fn first_mint() -> assets.Value {
  proposal_token_mint(proposal_own_ref(), 1)
    |> assets.merge(activity_token_mint(proposer_did, 1))
}

/// The first submission of `datum` (seed and new output hold it).
fn submit_datum_tx(datum: ProposalDatum) -> Transaction {
  submit_with(
    datum,
    [proposal_output(datum), activity_output(proposer_did, 1, lo)],
    first_mint(),
  )
}

fn submit_tx() -> Transaction {
  submit_datum_tx(new_proposal())
}

fn submit(tx: Transaction) -> Bool {
  expect [seed, ..] = tx.inputs
  expect InlineDatum(spent) = seed.output.datum
  spend_proposal(spent, SubmitProposal, proposal_own_ref(), tx)
}

/// A second submission of agent 1: its activity UTxO (count 1, last
/// 1_000_000_000) continues with `activity` and the transaction mints `mint`.
fn second_submit_with(activity: Output, mint: assets.Value) -> Transaction {
  let tx = submit_tx()
  let spend_activity: Data = SpendActivity
  Transaction {
    ..tx,
    inputs: [
      proposal_seed_input(new_proposal()),
      activity_input(proposer_did, 1, 1_000_000_000),
    ],
    outputs: [proposal_output(new_proposal()), activity],
    mint: mint,
    redeemers: [
      spend_redeemer(activity_utxo_ref(), spend_activity),
      ..tx.redeemers
    ],
  }
}

fn second_submit_tx() -> Transaction {
  second_submit_with(
    activity_output(proposer_did, 2, lo),
    proposal_token_mint(proposal_own_ref(), 1),
  )
}

fn run_spend_activity(tx: Transaction) -> Bool {
  spend_proposal(activity_datum(), SpendActivity, activity_utxo_ref(), tx)
}

fn proposal_data() -> Data {
  let datum: Data = proposal_datum()
  datum
}

fn stale_data() -> Data {
  let datum: Data = stale_datum()
  datum
}

// ============================================================================
// One proposal action per transaction (all nine redeemers)
// ============================================================================

test submit_with_one_action() {
  submit(submit_tx())
}

test submit_with_second_proposal_action_fails() fail {
  submit(with_second_action(submit_tx()))
}

test withdraw_with_one_action() {
  spend_proposal(proposal_data(), WithdrawProposal, proposal_utxo_ref(), withdraw_tx())
}

test withdraw_with_second_proposal_action_fails() fail {
  spend_proposal(
    proposal_data(),
    WithdrawProposal,
    proposal_utxo_ref(),
    with_second_action(withdraw_tx()),
  )
}

test amend_with_one_action() {
  spend_proposal(proposal_data(), amendment(), proposal_utxo_ref(), amend_tx())
}

test amend_with_second_proposal_action_fails() fail {
  spend_proposal(
    proposal_data(),
    amendment(),
    proposal_utxo_ref(),
    with_second_action(amend_tx()),
  )
}

test adopt_with_one_action() {
  spend_proposal(proposal_data(), adoption(), proposal_utxo_ref(), adopt_tx())
}

test adopt_with_second_proposal_action_fails() fail {
  spend_proposal(
    proposal_data(),
    adoption(),
    proposal_utxo_ref(),
    with_second_action(adopt_tx()),
  )
}

test reject_with_one_action() {
  spend_proposal(proposal_data(), rejection(), proposal_utxo_ref(), reject_tx())
}

test reject_with_second_proposal_action_fails() fail {
  spend_proposal(
    proposal_data(),
    rejection(),
    proposal_utxo_ref(),
    with_second_action(reject_tx()),
  )
}

test expire_with_one_action() {
  spend_proposal(proposal_data(), ExpireProposal, proposal_utxo_ref(), expire_tx())
}

test expire_with_second_proposal_action_fails() fail {
  spend_proposal(
    proposal_data(),
    ExpireProposal,
    proposal_utxo_ref(),
    with_second_action(expire_tx()),
  )
}

test expire_stale_with_one_action() {
  spend_proposal(
    stale_data(),
    ExpireStaleProposal,
    proposal_utxo_ref(),
    expire_stale_tx(),
  )
}

test expire_stale_with_second_proposal_action_fails() fail {
  spend_proposal(
    stale_data(),
    ExpireStaleProposal,
    proposal_utxo_ref(),
    with_second_action(expire_stale_tx()),
  )
}

test extend_review_with_one_action() {
  spend_proposal(proposal_data(), extension(), proposal_utxo_ref(), extend_tx())
}

test extend_review_with_second_proposal_action_fails() fail {
  spend_proposal(
    proposal_data(),
    extension(),
    proposal_utxo_ref(),
    with_second_action(extend_tx()),
  )
}

test spend_activity_with_one_action() {
  run_spend_activity(withdraw_tx())
}

test spend_activity_with_second_proposal_action_fails() fail {
  run_spend_activity(with_second_action(withdraw_tx()))
}

/// Two expired proposals of one proposer and one output with one stake.
test expire_two_proposals_with_one_payment_fails() fail {
  let expire: Data = ExpireProposal
  let second =
    proposal_utxo(
      make_ref(
        #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0008",
        0,
      ),
      assets.from_lovelace(25_000_000)
        |> assets.add(
            proposal_mint_hash,
            utils.proposal_token_name(other_seed()),
            1,
          ),
      proposal_data(),
    )
  let tx = expire_tx()
  spend_proposal(
    proposal_data(),
    ExpireProposal,
    proposal_utxo_ref(),
    Transaction {
      ..tx,
      inputs: list.concat(tx.inputs, [second]),
      mint: assets.merge(tx.mint, proposal_token_mint(other_seed(), -1)),
      redeemers: [spend_redeemer(second.output_reference, expire), ..tx.redeemers],
    },
  )
}

// ============================================================================
// SpendActivity: the activity of the agent of the proposal action
// ============================================================================

test spend_activity_in_submission() {
  run_spend_activity(second_submit_tx())
}

test spend_activity_in_expiry() {
  run_spend_activity(expire_tx())
}

test spend_activity_in_adoption() {
  run_spend_activity(adopt_tx())
}

/// The activity UTxO of agent 2 in the withdrawal of agent 1's proposal.
test spend_activity_of_other_agent_fails() fail {
  let other_ref =
    make_ref(
      #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0002",
      0,
    )
  let other_activity: Data =
    ProposerActivityDatum {
      agent_did: other_did(),
      agent_credential: other_cred(),
      active_proposal_count: 1,
      last_proposal_slot: 1_000_000_000,
    }
  let spend_activity: Data = SpendActivity
  let tx = withdraw_tx()
  spend_proposal(
    other_activity,
    SpendActivity,
    other_ref,
    Transaction {
      ..tx,
      inputs: list.concat(
        tx.inputs,
        [
          proposal_utxo(
            other_ref,
            assets.from_lovelace(2_000_000)
              |> assets.add(
                  proposal_mint_hash,
                  utils.activity_token_name(other_did()),
                  1,
                ),
            other_activity,
          ),
        ],
      ),
      outputs: list.concat(
        tx.outputs,
        [payment_output(critic_cred(), 2_000_000)],
      ),
      redeemers: [spend_redeemer(other_ref, spend_activity), ..tx.redeemers],
    },
  )
}

/// The activity UTxO of agent 1 in its amendment (not a submission or a
/// finalisation).
test spend_activity_in_amendment_fails() fail {
  let spend_activity: Data = SpendActivity
  let tx = amend_tx()
  run_spend_activity(
    Transaction {
      ..tx,
      inputs: list.concat(
        tx.inputs,
        [activity_input(proposer_did, 1, 1_000_000_000)],
      ),
      outputs: list.concat(
        tx.outputs,
        [activity_output(proposer_did, 1, 1_000_000_000)],
      ),
      redeemers: [
        spend_redeemer(activity_utxo_ref(), spend_activity),
        ..tx.redeemers
      ],
    },
  )
}

/// An activity datum of agent 1 on a UTxO with the activity token of agent 2.
test spend_activity_with_token_of_other_agent_fails() fail {
  let fake_ref =
    make_ref(
      #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0003",
      0,
    )
  let spend_activity: Data = SpendActivity
  let tx = withdraw_tx()
  spend_proposal(
    activity_datum(),
    SpendActivity,
    fake_ref,
    Transaction {
      ..tx,
      inputs: list.concat(
        tx.inputs,
        [
          proposal_utxo(
            fake_ref,
            assets.from_lovelace(2_000_000)
              |> assets.add(
                  proposal_mint_hash,
                  utils.activity_token_name(other_did()),
                  1,
                ),
            activity_datum(),
          ),
        ],
      ),
      redeemers: [spend_redeemer(fake_ref, spend_activity), ..tx.redeemers],
    },
  )
}

// ============================================================================
// Continuing activity output (checked by the proposal action)
// ============================================================================

fn withdraw_with_activity(activity: Output) -> Bool {
  spend_proposal(
    proposal_data(),
    WithdrawProposal,
    proposal_utxo_ref(),
    withdraw_with(
      [payment_output(proposer_cred(), proposal_datum().stake_amount), activity],
    ),
  )
}

test withdraw_with_activity_kept() {
  withdraw_with_activity(activity_output(proposer_did, 0, 1_000_000_000))
}

test withdraw_with_activity_value_taken_fails() fail {
  let activity = activity_output(proposer_did, 0, 1_000_000_000)
  withdraw_with_activity(
    Output {
      ..activity,
      value: assets.from_asset(
        proposal_mint_hash,
        utils.activity_token_name(proposer_did),
        1,
      )
        |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 1_000_000),
    },
  )
}

test withdraw_with_activity_moved_fails() fail {
  let activity = activity_output(proposer_did, 0, 1_000_000_000)
  withdraw_with_activity(
    Output {
      ..activity,
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: Some(Inline(proposer_cred())),
      },
    },
  )
}

test withdraw_with_activity_slot_changed_fails() fail {
  withdraw_with_activity(activity_output(proposer_did, 0, lo + 86_400_000_000))
}

test withdraw_with_activity_credential_changed_fails() fail {
  withdraw_with_activity(
    activity_output_for(proposer_did, critic_cred(), 0, 1_000_000_000),
  )
}

test submit_with_activity_moved_fails() fail {
  let activity = activity_output(proposer_did, 2, lo)
  submit(
    second_submit_with(
      Output {
        ..activity,
        address: Address {
          payment_credential: Script(proposal_validator_hash),
          stake_credential: Some(Inline(proposer_cred())),
        },
      },
      proposal_token_mint(proposal_own_ref(), 1),
    ),
  )
}

test second_submit_with_activity() {
  submit(second_submit_tx())
}

// ============================================================================
// Continuing proposal output: AmendProposal and ExtendReview
// ============================================================================

fn amend_to(output: Output) -> Bool {
  spend_proposal(
    proposal_data(),
    amendment(),
    proposal_utxo_ref(),
    amend_with([output, payment_output(proposer_cred(), 24_000_000)], assets.zero),
  )
}

fn extend_to(output: Output) -> Bool {
  spend_proposal(
    proposal_data(),
    extension(),
    proposal_utxo_ref(),
    extend_with([output, payment_output(proposer_cred(), 24_000_000)], assets.zero),
  )
}

/// The output `output` with a stake credential.
fn with_stake_credential(output: Output) -> Output {
  Output {
    ..output,
    address: Address {
      payment_credential: Script(proposal_validator_hash),
      stake_credential: Some(Inline(critic_cred())),
    },
  }
}

/// The output `output` with 1 AP3X instead of the stake.
fn with_value_taken(output: Output) -> Output {
  Output {
    ..output,
    value: assets.from_lovelace(1_000_000)
      |> assets.add(
          proposal_mint_hash,
          utils.proposal_token_name(proposal_own_ref()),
          1,
        ),
  }
}

/// The output `output` without the proposal token.
fn without_token(output: Output) -> Output {
  Output { ..output, value: assets.from_lovelace(25_000_000) }
}

/// The output `output` with one more token of another policy.
fn with_extra_asset(output: Output) -> Output {
  Output { ..output, value: assets.add(output.value, critique_mint_hash, "x", 1) }
}

/// The output `output` with 5 AP3X more.
fn with_more_ap3x(output: Output) -> Output {
  Output { ..output, value: assets.add(output.value, assets.ada_policy_id, assets.ada_asset_name, 5_000_000) }
}

test amend_with_more_ap3x() {
  amend_to(with_more_ap3x(proposal_output(amended_datum())))
}

test amend_to_other_stake_credential_fails() fail {
  amend_to(with_stake_credential(proposal_output(amended_datum())))
}

test amend_with_value_taken_fails() fail {
  amend_to(with_value_taken(proposal_output(amended_datum())))
}

test amend_without_proposal_token_fails() fail {
  amend_to(without_token(proposal_output(amended_datum())))
}

test amend_with_extra_asset_fails() fail {
  amend_to(with_extra_asset(proposal_output(amended_datum())))
}

test amend_changing_review_window_fails() fail {
  amend_to(
    proposal_output(
      ProposalDatum { ..amended_datum(), review_window: 2_000_000_000 },
    ),
  )
}

test extend_review_with_more_ap3x() {
  extend_to(with_more_ap3x(proposal_output(extended_datum())))
}

test extend_review_to_other_stake_credential_fails() fail {
  extend_to(with_stake_credential(proposal_output(extended_datum())))
}

test extend_review_with_value_taken_fails() fail {
  extend_to(with_value_taken(proposal_output(extended_datum())))
}

test extend_review_without_proposal_token_fails() fail {
  extend_to(without_token(proposal_output(extended_datum())))
}

test extend_review_changing_credential_fails() fail {
  extend_to(
    proposal_output(
      ProposalDatum { ..extended_datum(), proposer_credential: critic_cred() },
    ),
  )
}

// ============================================================================
// Stake return: AdoptProposal and summed outputs
// ============================================================================

test adopt_returns_stake() {
  spend_proposal(proposal_data(), adoption(), proposal_utxo_ref(), adopt_tx())
}

test adopt_without_stake_return_fails() fail {
  spend_proposal(
    proposal_data(),
    adoption(),
    proposal_utxo_ref(),
    adopt_with([activity_output(proposer_did, 0, 1_000_000_000)]),
  )
}

/// A critique of the proposer's own credential, settled in the expiry.
fn expire_with_own_critique(paid: Int) -> Bool {
  let critique_ref =
    make_ref(
      #"2222222222222222222222222222222222222222222222222222222222222222",
      5,
    )
  let critique: Data =
    CritiqueDatum {
      critic_did: proposer_did,
      critic_credential: proposer_cred(),
      proposal_ref: proposal_own_ref(),
      critique_hash: blake2b_256("own critique"),
      storage_uri: "ipfs://QmOwnCritique",
      critique_type: types.Amendment {
        suggested_change_hash: blake2b_256("change"),
      },
      stake_amount: 5_000_000,
      submitted_at: 1_500_000_000,
      incorporated: False,
    }
  let burn: Data = BurnCritiqueToken
  let critique_token = utils.critique_token_name(critique_ref)
  let tx = expire_tx()
  spend_proposal(
    proposal_data(),
    ExpireProposal,
    proposal_utxo_ref(),
    Transaction {
      ..tx,
      inputs: list.concat(
        tx.inputs,
        [
          Input {
            output_reference: critique_ref,
            output: Output {
              address: Address {
                payment_credential: Script(critique_validator_hash),
                stake_credential: None,
              },
              value: assets.from_lovelace(7_000_000)
                |> assets.add(critique_mint_hash, critique_token, 1),
              datum: InlineDatum(critique),
              reference_script: None,
            },
          },
        ],
      ),
      outputs: [
        payment_output(proposer_cred(), paid),
        activity_output(proposer_did, 0, 1_000_000_000),
      ],
      mint: assets.merge(
        tx.mint,
        assets.from_asset(critique_mint_hash, critique_token, -1),
      ),
      redeemers: [spend_redeemer(critique_ref, burn), ..tx.redeemers],
    },
  )
}

test expire_with_own_critique_paid_in_full() {
  expire_with_own_critique(32_000_000)
}

/// One output with the proposal stake does not also pay the critique.
test expire_with_own_critique_and_one_stake_fails() fail {
  expire_with_own_critique(25_000_000)
}

// ============================================================================
// Exact tokens under the proposal policy
// ============================================================================

fn junk_token_mint(quantity: Int) -> assets.Value {
  proposal_token_mint(other_seed(), quantity)
}

fn junk_token_output() -> Output {
  Output {
    ..payment_output(proposer_cred(), 2_000_000),
    value: assets.from_lovelace(2_000_000)
      |> assets.add(proposal_mint_hash, utils.proposal_token_name(other_seed()), 1),
  }
}

test submit_minting_extra_proposal_token_fails() fail {
  submit(
    submit_with(
      new_proposal(),
      [
        proposal_output(new_proposal()),
        activity_output(proposer_did, 1, lo),
        junk_token_output(),
      ],
      assets.merge(first_mint(), junk_token_mint(1)),
    ),
  )
}

test submit_with_activity_minting_activity_token_fails() fail {
  submit(
    second_submit_with(
      activity_output(proposer_did, 2, lo),
      first_mint(),
    ),
  )
}

test withdraw_minting_extra_token_fails() fail {
  let tx = withdraw_tx()
  spend_proposal(
    proposal_data(),
    WithdrawProposal,
    proposal_utxo_ref(),
    Transaction {
      ..tx,
      outputs: list.concat(tx.outputs, [junk_token_output()]),
      mint: assets.merge(tx.mint, junk_token_mint(1)),
    },
  )
}

test amend_minting_token_fails() fail {
  spend_proposal(
    proposal_data(),
    amendment(),
    proposal_utxo_ref(),
    amend_with(
      [proposal_output(amended_datum()), junk_token_output()],
      junk_token_mint(1),
    ),
  )
}

test extend_review_minting_token_fails() fail {
  spend_proposal(
    proposal_data(),
    extension(),
    proposal_utxo_ref(),
    extend_with(
      [proposal_output(extended_datum()), junk_token_output()],
      junk_token_mint(1),
    ),
  )
}

test proposal_mint_with_one_action() {
  proposal.proposal_mint.mint(
    test_config(),
    SubmitProposal,
    proposal_mint_hash,
    submit_tx(),
  )
}

/// Only an activity UTxO at the proposal validator: no proposal action.
test proposal_mint_without_proposal_action_fails() fail {
  let spend_activity: Data = SpendActivity
  proposal.proposal_mint.mint(
    test_config(),
    SubmitProposal,
    proposal_mint_hash,
    Transaction {
      ..transaction.placeholder,
      inputs: [activity_input(proposer_did, 1, 1_000_000_000)],
      reference_inputs: [cross_refs_ref_input()],
      outputs: [junk_token_output()],
      mint: junk_token_mint(1),
      redeemers: [spend_redeemer(activity_utxo_ref(), spend_activity)],
    },
  )
}

// ============================================================================
// Submission reads the new output; submitted_at is the lower bound
// ============================================================================

/// A first submission whose seed holds the valid new proposal and whose new
/// output holds `datum`.
fn submit_new_output(datum: ProposalDatum) -> Bool {
  submit(
    submit_with(
      new_proposal(),
      [proposal_output(datum), activity_output(proposer_did, 1, lo)],
      first_mint(),
    ),
  )
}

test submit_new_output_of_seed_datum() {
  submit_new_output(new_proposal())
}

test submit_new_output_with_other_credential_fails() fail {
  submit_new_output(
    ProposalDatum { ..new_proposal(), proposer_credential: critic_cred() },
  )
}

test submit_new_output_with_amended_state_fails() fail {
  submit_new_output(
    ProposalDatum {
      ..new_proposal(),
      state: Amended { previous_hash: blake2b_256("old") },
    },
  )
}

/// The new output holds the stake of 25 AP3X, but its datum declares a stake
/// below the minimum.
test submit_new_output_with_low_stake_fails() fail {
  let datum = ProposalDatum { ..new_proposal(), stake_amount: 1_000_000 }
  submit(
    submit_with(
      new_proposal(),
      [
        Output {
          ..proposal_output(datum),
          value: proposal_output(new_proposal()).value,
        },
        activity_output(proposer_did, 1, lo),
      ],
      first_mint(),
    ),
  )
}

/// The proposal token goes to the proposer's address; a plain output at the
/// proposal validator holds the stake.
test submit_token_outside_proposal_fails() fail {
  let datum = new_proposal()
  submit(
    submit_with(
      datum,
      [
        Output {
          ..proposal_output(datum),
          address: Address {
            payment_credential: proposer_cred(),
            stake_credential: None,
          },
        },
        Output {
          ..proposal_output(datum),
          value: assets.from_lovelace(datum.stake_amount),
        },
        activity_output(proposer_did, 1, lo),
      ],
      first_mint(),
    ),
  )
}

/// The seed holds a proposal of agent 2; agent 1 submits its own proposal
/// with the funds of that seed.
test submit_with_seed_of_other_owner_fails() fail {
  submit(
    submit_with(
      ProposalDatum {
        ..new_proposal(),
        proposer_did: other_did(),
        proposer_credential: other_cred(),
      },
      [proposal_output(new_proposal()), activity_output(proposer_did, 1, lo)],
      first_mint(),
    ),
  )
}

test submit_submitted_before_lower_bound_fails() fail {
  submit(submit_datum_tx(ProposalDatum { ..new_proposal(), submitted_at: lo - 1 }))
}

test submit_submitted_after_lower_bound_fails() fail {
  submit(submit_datum_tx(ProposalDatum { ..new_proposal(), submitted_at: lo + 1 }))
}

// ============================================================================
// Emergency gate: the proposer's own Module 3 stake
// ============================================================================

/// Script hash of the Module 3 reputation validator in these tests.
const reputation_hash: ByteArray =
  #"6600000000000000000000000000000000000000000000000000006600"

fn emergency_config() -> GovernanceConfig {
  GovernanceConfig { ..test_config(), reputation_validator_hash: reputation_hash }
}

/// The datum layout of a Module 3 stake position (StakeDatum); the gate reads
/// its first field.
type StakePosition {
  agent_did: ByteArray,
  owner_credential: Credential,
  stake_amount: Int,
  staked_capabilities: List<ByteArray>,
  last_updated: Int,
  history_points: Int,
}

/// A Module 3 stake position at the reputation validator with the stake
/// token of `token_did`, the datum of `datum_did` and `lovelace`.
fn stake_input(
  token_did: ByteArray,
  datum_did: ByteArray,
  lovelace: Int,
) -> Input {
  let datum: Data =
    StakePosition {
      agent_did: datum_did,
      owner_credential: proposer_cred(),
      stake_amount: lovelace,
      staked_capabilities: ["governance"],
      last_updated: 1_000_000_000,
      history_points: 0,
    }
  Input {
    output_reference: make_ref(
      #"7777777777777777777777777777777777777777777777777777777777777777",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(reputation_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(lovelace)
        |> assets.add(
            reputation_hash,
            token_naming.derive_token_name("rstk_", token_did, 27),
            1,
          ),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// An emergency ParameterChange proposal of agent 1 (125 AP3X, 12 h review).
fn emergency_proposal() -> ProposalDatum {
  ProposalDatum {
    ..new_proposal(),
    proposal_type: ParameterChange {
      param_name: "MIN_CLAIM_STAKE",
      current_value: 100,
      proposed_value: 50,
    },
    stake_amount: 125_000_000,
    review_window: 43_200_000,
    priority: Emergency,
  }
}

fn submit_emergency(stake: Input) -> Bool {
  let datum = emergency_proposal()
  let tx = submit_datum_tx(datum)
  let raw: Data = datum
  proposal.proposal_spend.spend(
    emergency_config(),
    Some(raw),
    SubmitProposal,
    proposal_own_ref(),
    Transaction { ..tx, reference_inputs: [stake, ..tx.reference_inputs] },
  )
}

test emergency_submit_with_own_stake() {
  submit_emergency(stake_input(proposer_did, proposer_did, 150_000_000))
}

test emergency_submit_with_stake_of_other_agent_fails() fail {
  submit_emergency(stake_input(other_did(), other_did(), 150_000_000))
}

test emergency_submit_with_own_token_and_other_datum_fails() fail {
  submit_emergency(stake_input(proposer_did, other_did(), 150_000_000))
}

test emergency_submit_with_small_own_stake_fails() fail {
  submit_emergency(stake_input(proposer_did, proposer_did, 99_000_000))
}
