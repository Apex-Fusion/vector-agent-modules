// Module 6: identity registry checks (validator level).
//
// Part 2 §7 of docs/IDENTITY_REGISTRY.md in agents-marketplace, Module-6 row
// and Time rule. A proposal submission, an amendment, a critique submission
// and an endorsement submission need:
//   - a validity range with finite bounds of at most one hour;
//   - the record of the agent in the datum at the exact identity address with
//     the identity token, with effective status Active over the range;
//   - a declared credential: the controller or a listed key with the
//     transaction purpose, and that key signs.
// The exits (WithdrawProposal, WithdrawCritique, WithdrawEndorsement) read no
// identity record.
//
// Each test builds a full transaction and calls the spend handler of the
// proposal, critique or endorsement validator. Each case changes one part of
// the passing case `active(n)`. SubmitProposal fails with the trace
// FAIL:identity_valid, so its negative tests are `fail` tests. The map of
// checks to tests is tests/identity-checks.md.

use aiken/crypto.{blake2b_256}
use aiken/interval
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction, ValidityRange,
}
use critique
use governance_suggestion/params
use governance_suggestion/proposal_validation
use governance_suggestion/tests/test_helpers.{
  activity_input, activity_output, activity_output_for, activity_token_mint,
  activity_utxo_ref, critic_agent, critic_cred, critic_key_hash,
  critique_mint_hash, critique_output, critique_own_ref, critique_validator_hash,
  cross_refs_ref_input, default_proposal_datum, endorsement_validator_hash,
  head_input, head_output, head_utxo_ref, make_ref, params_ref_input,
  payment_output, proposal_input, proposal_output, proposal_own_ref,
  proposal_ref_input, proposal_token_mint, proposal_validator_hash,
  proposer_agent, proposer_cred, proposer_did, proposer_key_hash,
  spend_redeemer, test_config, test_cross_refs,
}
use governance_suggestion/types.{
  AmendProposal, Amended, CritiqueDatum, GovernanceEndorsementDatum,
  MintCritiqueToken, MintEndorsementToken, ProposalAction, ProposalDatum,
  SpendActivity, SubmitProposal, Supportive, WithdrawCritique,
  WithdrawEndorsement, WithdrawProposal,
}
use governance_suggestion/utils
use proposal
use shared/identity_fixtures.{
  accountable_until, agent_id, controller_hash, ed_key, ed_key_hash, p256_key,
  p256_key_hash, record, record_input, record_input_at_other_script,
  record_input_with_fake_token, record_input_with_stake, retired, revoked,
  subagent, suspended, unaccountable, with_key, with_subagent_epoch,
  with_transaction_key,
}
use vector_identity.{
  purpose_assertion, purpose_authentication, purpose_transaction,
}
use vector_identity/types.{Ed25519, IdentityDatum, P256} as identity_types

// ============================================================================
// Fixtures
// ============================================================================

/// The bounds of the 10-minute validity range of the test transactions.
const lo: Int = 2_000_000_000

const hi: Int = 2_000_600_000

/// Test agents of shared/identity_fixtures besides the proposer (1) and the
/// critic (2): the endorser, another agent and the parent of a subagent.
const endorser_agent: Int = 3

const other_agent: Int = 7

const parent_agent: Int = 9

/// The proposer's record (agent 1). Critique and endorsement submissions
/// read it for the rule that the critic or endorser and the proposer are
/// different parties.
fn proposer_record() -> Input {
  record_input(agent_id(proposer_agent), record(proposer_agent, []))
}

/// Lovelace of a critique or endorsement UTxO (5 AP3X stake + 2 AP3X).
const position_lovelace: Int = 7_000_000

fn endorsement_ref() -> OutputReference {
  make_ref(
    #"3333333333333333333333333333333333333333333333333333333333333333",
    0,
  )
}

/// An input at the script `hash` with `lovelace` and the inline datum `datum`.
fn script_input(
  ref: OutputReference,
  hash: ByteArray,
  lovelace: Int,
  datum: Data,
) -> Input {
  Input {
    output_reference: ref,
    output: Output {
      address: Address {
        payment_credential: Script(hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(lovelace),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

fn ten_minutes() -> ValidityRange {
  interval.between(lo, hi)
}

// ============================================================================
// Cases
// ============================================================================

/// The identity part of a transaction: the agent identifier and the
/// credential in the datum, the identity record reference inputs, the signers
/// and the validity range.
type Case {
  did: ByteArray,
  credential: Credential,
  records: List<Input>,
  signers: List<ByteArray>,
  range: ValidityRange,
}

/// Agent `n` with its Active root record. The controller key is the declared
/// credential and signs. The range is 10 minutes.
fn active(n: Int) -> Case {
  Case {
    did: agent_id(n),
    credential: VerificationKey(controller_hash(n)),
    records: [record_input(agent_id(n), record(n, []))],
    signers: [controller_hash(n)],
    range: ten_minutes(),
  }
}

/// Agent `n` with the authentic record input of `r`.
fn with_record(n: Int, r: IdentityDatum) -> Case {
  Case { ..active(n), records: [record_input(agent_id(n), r)] }
}

/// Agent `n` with `records` as the identity reference inputs.
fn with_records(n: Int, records: List<Input>) -> Case {
  Case { ..active(n), records }
}

/// Agent `n` with the record `r` and the credential `key_hash`, signed by
/// that key.
fn with_key_credential(n: Int, r: IdentityDatum, key_hash: ByteArray) -> Case {
  Case {
    ..with_record(n, r),
    credential: VerificationKey(key_hash),
    signers: [key_hash],
  }
}

fn with_range(n: Int, range: ValidityRange) -> Case {
  Case { ..active(n), range }
}

/// Agent `n` as a subagent (depth 1) of agent 9 created under parent epoch
/// 0, with `parent` as the record of agent 9.
fn subagent_with_parent(n: Int, parent: IdentityDatum) -> Case {
  with_records(
    n,
    [
      record_input(
        agent_id(n),
        subagent(record(n, []), agent_id(parent_agent), 0, 1),
      ),
      record_input(agent_id(parent_agent), parent),
    ],
  )
}

// Statuses

fn suspended_agent(n: Int) -> Case {
  with_record(n, suspended(record(n, []), hi))
}

fn unaccountable_agent(n: Int) -> Case {
  with_record(n, unaccountable(record(n, [])))
}

/// The Accountable Party covers `lo` but ends before `hi`.
fn accountability_ends_before_upper_bound(n: Int) -> Case {
  with_record(n, accountable_until(record(n, []), hi - 1))
}

fn revoked_agent(n: Int) -> Case {
  with_record(n, revoked(record(n, [])))
}

fn retired_agent(n: Int) -> Case {
  with_record(n, retired(record(n, [])))
}

// Declared credentials

/// The listed Ed25519 key `ed_key(n)` with the transaction purpose.
fn listed_transaction_key(n: Int) -> Case {
  with_key_credential(
    n,
    record(n, []) |> with_transaction_key(n),
    ed_key_hash(n),
  )
}

/// A key that is neither the controller nor a listed key.
fn unlisted_key(n: Int) -> Case {
  with_key_credential(n, record(n, []), ed_key_hash(n))
}

/// A listed Ed25519 key with the authentication and assertion purposes only.
fn key_without_transaction_purpose(n: Int) -> Case {
  with_key_credential(
    n,
    record(n, [])
      |> with_key(
          Ed25519,
          ed_key(n),
          purpose_authentication + purpose_assertion,
        ),
    ed_key_hash(n),
  )
}

/// A listed P-256 key, even with the transaction bit in its purposes.
fn p256_key_credential(n: Int) -> Case {
  with_key_credential(
    n,
    record(n, [])
      |> with_key(
          P256,
          p256_key(n),
          purpose_authentication + purpose_transaction,
        ),
    p256_key_hash(n),
  )
}

/// A script credential with the controller's hash.
fn script_credential(n: Int) -> Case {
  Case { ..active(n), credential: Script(controller_hash(n)) }
}

/// The controller is the declared credential, but another key signs.
fn unsigned_credential(n: Int) -> Case {
  Case { ..active(n), signers: [controller_hash(other_agent)] }
}

/// The controller of another agent declares and signs. Both records are
/// reference inputs.
fn other_agent_controller(n: Int) -> Case {
  Case {
    ..active(n),
    records: [
      record_input(agent_id(n), record(n, [])),
      record_input(agent_id(other_agent), record(other_agent, [])),
    ],
    credential: VerificationKey(controller_hash(other_agent)),
    signers: [controller_hash(other_agent)],
  }
}

// Record lookup

fn record_with_stake_credential(n: Int) -> Case {
  with_records(n, [record_input_with_stake(agent_id(n), record(n, []))])
}

fn record_at_other_script(n: Int) -> Case {
  with_records(n, [record_input_at_other_script(agent_id(n), record(n, []))])
}

fn record_with_fake_token(n: Int) -> Case {
  with_records(n, [record_input_with_fake_token(agent_id(n), record(n, []))])
}

/// The authentic record input of another identifier, with the record content
/// (and controller) of agent `n`.
fn record_of_other_agent(n: Int) -> Case {
  with_records(n, [record_input(agent_id(other_agent), record(n, []))])
}

fn without_record(n: Int) -> Case {
  with_records(n, [])
}

// Validity ranges

fn range_over_one_hour(n: Int) -> Case {
  with_range(n, interval.between(lo, lo + 3_600_001))
}

fn range_without_upper_bound(n: Int) -> Case {
  with_range(n, interval.after(lo))
}

fn range_without_lower_bound(n: Int) -> Case {
  with_range(n, interval.before(hi))
}

fn range_of_one_hour(n: Int) -> Case {
  with_range(n, interval.between(lo, lo + 3_600_000))
}

// Subagents

fn subagent_of_active_parent(n: Int) -> Case {
  subagent_with_parent(n, record(parent_agent, []))
}

fn subagent_of_suspended_parent(n: Int) -> Case {
  subagent_with_parent(n, suspended(record(parent_agent, []), hi))
}

/// The parent increased its subagent epoch after the subagent was created.
fn subagent_of_bumped_parent_epoch(n: Int) -> Case {
  subagent_with_parent(n, with_subagent_epoch(record(parent_agent, []), 1))
}

fn subagent_without_parent(n: Int) -> Case {
  with_records(
    n,
    [
      record_input(
        agent_id(n),
        subagent(record(n, []), agent_id(parent_agent), 0, 1),
      ),
    ],
  )
}

// ============================================================================
// Actions that open or extend a position
// ============================================================================

/// The default proposal with the agent and the credential of `c`, submitted
/// at the lower bound `lo`.
fn proposal_of(c: Case) -> ProposalDatum {
  ProposalDatum {
    ..default_proposal_datum(),
    proposer_did: c.did,
    proposer_credential: c.credential,
    submitted_at: lo,
  }
}

/// A SubmitProposal transaction for `c`: it spends the locked proposal UTxO
/// (proposal_own_ref) and the head of the empty activity list, mints the
/// proposal token and the first activity token, and creates the proposal,
/// the node of the agent and the head with next_did = the agent.
fn submit_proposal_tx(c: Case) -> Transaction {
  let datum = proposal_of(c)
  let submit: Data = SubmitProposal
  let spend_activity: Data = SpendActivity
  Transaction {
    ..transaction.placeholder,
    inputs: [
      script_input(
        proposal_own_ref(),
        proposal_validator_hash,
        datum.stake_amount,
        datum,
      ),
      head_input(#""),
    ],
    reference_inputs: [cross_refs_ref_input(), params_ref_input(), ..c.records],
    outputs: [
      proposal_output(datum),
      activity_output_for(c.did, c.credential, 1, lo),
      head_output(c.did),
    ],
    mint: proposal_token_mint(proposal_own_ref(), 1)
      |> assets.merge(activity_token_mint(c.did, 1)),
    extra_signatories: c.signers,
    validity_range: c.range,
    redeemers: [
      spend_redeemer(proposal_own_ref(), submit),
      spend_redeemer(head_utxo_ref(), spend_activity),
    ],
  }
}

fn submit_proposal(c: Case) -> Bool {
  let raw: Data = proposal_of(c)
  proposal.proposal_spend.spend(
    test_config(),
    Some(raw),
    SubmitProposal,
    proposal_own_ref(),
    submit_proposal_tx(c),
  )
}

/// The AmendProposal action of the amendment transactions.
fn amendment() -> ProposalAction {
  AmendProposal {
    new_hash: blake2b_256("amended proposal"),
    new_uri: "ipfs://QmAmended",
    incorporated: [],
  }
}

/// An AmendProposal transaction for `c`: the proposer amends the open
/// proposal without incorporated critiques.
fn amend_proposal_tx(c: Case) -> Transaction {
  let datum = proposal_of(c)
  let amended =
    ProposalDatum {
      ..datum,
      state: Amended { previous_hash: datum.proposal_hash },
      proposal_hash: blake2b_256("amended proposal"),
      storage_uri: "ipfs://QmAmended",
      amendment_count: 1,
    }
  Transaction {
    ..transaction.placeholder,
    inputs: [proposal_input(datum, proposal_own_ref())],
    reference_inputs: [cross_refs_ref_input(), params_ref_input(), ..c.records],
    outputs: [proposal_output(amended)],
    extra_signatories: c.signers,
    validity_range: c.range,
  }
}

fn amend_proposal(c: Case) -> Bool {
  let raw: Data = proposal_of(c)
  let tx = amend_proposal_tx(c)
  expect [input] = tx.inputs
  proposal.proposal_spend.spend(
    test_config(),
    Some(raw),
    amendment(),
    input.output_reference,
    tx,
  )
}

/// A Supportive critique by `c` of the default proposal.
fn critique_of(c: Case) -> CritiqueDatum {
  CritiqueDatum {
    critic_did: c.did,
    critic_credential: c.credential,
    proposal_ref: proposal_own_ref(),
    critique_hash: blake2b_256("critique"),
    storage_uri: "ipfs://QmCritique",
    critique_type: Supportive,
    stake_amount: 5_000_000,
    submitted_at: lo,
    incorporated: False,
  }
}

/// A MintCritiqueToken transaction for `c`: it spends the locked critique
/// UTxO and creates the critique with its token; the open default proposal
/// and the proposer's record are reference inputs.
fn submit_critique(c: Case) -> Bool {
  let raw: Data = critique_of(c)
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [
        script_input(
          critique_own_ref(),
          critique_validator_hash,
          position_lovelace,
          raw,
        ),
      ],
      reference_inputs: [
        cross_refs_ref_input(),
        params_ref_input(),
        proposal_ref_input(default_proposal_datum()),
        proposer_record(),
        ..c.records
      ],
      outputs: [critique_output(critique_own_ref(), critique_of(c))],
      extra_signatories: c.signers,
      validity_range: c.range,
    }
  critique.critique_spend.spend(
    test_config(),
    Some(raw),
    MintCritiqueToken,
    critique_own_ref(),
    tx,
  )
}

/// An endorsement by `c` of the default proposal.
fn endorsement_of(c: Case) -> GovernanceEndorsementDatum {
  GovernanceEndorsementDatum {
    endorser_did: c.did,
    endorser_credential: c.credential,
    proposal_ref: proposal_own_ref(),
    stake_amount: 5_000_000,
    created_at: lo,
  }
}

/// A MintEndorsementToken transaction for `c`: it spends the locked
/// endorsement UTxO and creates the endorsement with its token; the open
/// default proposal and the proposer's record are reference inputs.
fn submit_endorsement(c: Case) -> Bool {
  let raw: Data = endorsement_of(c)
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [
        script_input(
          endorsement_ref(),
          endorsement_validator_hash,
          position_lovelace,
          raw,
        ),
      ],
      reference_inputs: [
        cross_refs_ref_input(),
        params_ref_input(),
        proposal_ref_input(default_proposal_datum()),
        proposer_record(),
        ..c.records
      ],
      outputs: [
        Output {
          address: Address {
            payment_credential: Script(endorsement_validator_hash),
            stake_credential: None,
          },
          value: assets.from_lovelace(position_lovelace)
            |> assets.add(
                critique_mint_hash,
                utils.endorsement_token_name(c.did, proposal_own_ref()),
                1,
              ),
          datum: InlineDatum(raw),
          reference_script: None,
        },
      ],
      extra_signatories: c.signers,
      validity_range: c.range,
    }
  critique.endorsement_spend.spend(
    test_config(),
    Some(raw),
    MintEndorsementToken,
    endorsement_ref(),
    tx,
  )
}

// ============================================================================
// Exits
// ============================================================================

/// The proposer (agent 1) withdraws the default proposal: the proposal token
/// is burned, the activity count decreases and the stake returns.
fn withdraw_proposal(records: List<Input>, range: ValidityRange) -> Bool {
  let datum = default_proposal_datum()
  let raw: Data = datum
  let withdraw: Data = WithdrawProposal
  let spend_activity: Data = SpendActivity
  let input = proposal_input(datum, proposal_own_ref())
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [input, activity_input(proposer_did, 1, 1_000_000_000)],
      reference_inputs: [cross_refs_ref_input(), params_ref_input(), ..records],
      outputs: [
        payment_output(proposer_cred(), datum.stake_amount),
        activity_output(proposer_did, 0, 1_000_000_000),
      ],
      mint: proposal_token_mint(proposal_own_ref(), -1),
      extra_signatories: [proposer_key_hash],
      validity_range: range,
      redeemers: [
        spend_redeemer(input.output_reference, withdraw),
        spend_redeemer(activity_utxo_ref(), spend_activity),
      ],
    }
  proposal.proposal_spend.spend(
    test_config(),
    Some(raw),
    WithdrawProposal,
    input.output_reference,
    tx,
  )
}

/// The critic (agent 2) withdraws its critique of the open default proposal.
fn withdraw_critique(records: List<Input>, range: ValidityRange) -> Bool {
  let raw: Data = critique_of(active(critic_agent))
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [
        script_input(
          critique_own_ref(),
          critique_validator_hash,
          position_lovelace,
          raw,
        ),
      ],
      reference_inputs: [
        cross_refs_ref_input(),
        params_ref_input(),
        proposal_ref_input(default_proposal_datum()),
        ..records
      ],
      outputs: [payment_output(critic_cred(), position_lovelace)],
      extra_signatories: [critic_key_hash],
      validity_range: range,
    }
  critique.critique_spend.spend(
    test_config(),
    Some(raw),
    WithdrawCritique,
    critique_own_ref(),
    tx,
  )
}

/// The endorser (agent 3) withdraws its endorsement.
fn withdraw_endorsement(records: List<Input>, range: ValidityRange) -> Bool {
  let raw: Data = endorsement_of(active(endorser_agent))
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [
        script_input(
          endorsement_ref(),
          endorsement_validator_hash,
          position_lovelace,
          raw,
        ),
      ],
      reference_inputs: [cross_refs_ref_input(), params_ref_input(), ..records],
      outputs: [
        payment_output(
          VerificationKey(controller_hash(endorser_agent)),
          position_lovelace,
        ),
      ],
      extra_signatories: [controller_hash(endorser_agent)],
      validity_range: range,
    }
  critique.endorsement_spend.spend(
    test_config(),
    Some(raw),
    WithdrawEndorsement,
    endorsement_ref(),
    tx,
  )
}

/// The record of agent `n` as the only identity reference input.
fn only_record(n: Int, r: IdentityDatum) -> List<Input> {
  [record_input(agent_id(n), r)]
}

/// A range of two hours: exits read no identity record, so the one-hour
/// rule does not apply to them.
fn two_hours() -> ValidityRange {
  interval.between(lo, lo + 7_200_000)
}

// ============================================================================
// SubmitProposal
// ============================================================================

test submit_proposal_active_agent() {
  submit_proposal(active(proposer_agent))
}

test submit_proposal_listed_transaction_key() {
  submit_proposal(listed_transaction_key(proposer_agent))
}

test submit_proposal_range_of_one_hour() {
  submit_proposal(range_of_one_hour(proposer_agent))
}

test submit_proposal_subagent_of_active_parent() {
  submit_proposal(subagent_of_active_parent(proposer_agent))
}

test submit_proposal_suspended_agent_fails() fail {
  submit_proposal(suspended_agent(proposer_agent))
}

test submit_proposal_unaccountable_agent_fails() fail {
  submit_proposal(unaccountable_agent(proposer_agent))
}

test submit_proposal_accountability_ends_before_upper_bound_fails() fail {
  submit_proposal(accountability_ends_before_upper_bound(proposer_agent))
}

test submit_proposal_revoked_agent_fails() fail {
  submit_proposal(revoked_agent(proposer_agent))
}

test submit_proposal_retired_agent_fails() fail {
  submit_proposal(retired_agent(proposer_agent))
}

test submit_proposal_unlisted_key_fails() fail {
  submit_proposal(unlisted_key(proposer_agent))
}

test submit_proposal_key_without_transaction_purpose_fails() fail {
  submit_proposal(key_without_transaction_purpose(proposer_agent))
}

test submit_proposal_p256_key_fails() fail {
  submit_proposal(p256_key_credential(proposer_agent))
}

test submit_proposal_script_credential_fails() fail {
  submit_proposal(script_credential(proposer_agent))
}

test submit_proposal_unsigned_credential_fails() fail {
  submit_proposal(unsigned_credential(proposer_agent))
}

test submit_proposal_other_agent_controller_fails() fail {
  submit_proposal(other_agent_controller(proposer_agent))
}

test submit_proposal_record_with_stake_credential_fails() fail {
  submit_proposal(record_with_stake_credential(proposer_agent))
}

test submit_proposal_record_at_other_script_fails() fail {
  submit_proposal(record_at_other_script(proposer_agent))
}

test submit_proposal_record_with_fake_token_fails() fail {
  submit_proposal(record_with_fake_token(proposer_agent))
}

test submit_proposal_record_of_other_agent_fails() fail {
  submit_proposal(record_of_other_agent(proposer_agent))
}

test submit_proposal_without_record_fails() fail {
  submit_proposal(without_record(proposer_agent))
}

test submit_proposal_range_over_one_hour_fails() fail {
  submit_proposal(range_over_one_hour(proposer_agent))
}

test submit_proposal_range_without_upper_bound_fails() fail {
  submit_proposal(range_without_upper_bound(proposer_agent))
}

/// The validator fails first at `expect Some(current_slot)`.
test submit_proposal_range_without_lower_bound_fails() fail {
  submit_proposal(range_without_lower_bound(proposer_agent))
}

/// The identity check also rejects the range without a lower bound
/// (FAIL:identity_valid).
test submit_proposal_check_rejects_range_without_lower_bound() fail {
  let c = range_without_lower_bound(proposer_agent)
  let tx = submit_proposal_tx(c)
  proposal_validation.validate_submit_proposal(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    proposal_of(c),
    proposal_own_ref(),
    tx,
  )
}

test submit_proposal_subagent_of_suspended_parent_fails() fail {
  submit_proposal(subagent_of_suspended_parent(proposer_agent))
}

test submit_proposal_subagent_of_bumped_parent_epoch_fails() fail {
  submit_proposal(subagent_of_bumped_parent_epoch(proposer_agent))
}

/// Fails in vector_identity.effective: the parent record is missing.
test submit_proposal_subagent_without_parent_fails() fail {
  submit_proposal(subagent_without_parent(proposer_agent))
}

// ============================================================================
// AmendProposal
// ============================================================================

test amend_proposal_active_agent() {
  amend_proposal(active(proposer_agent))
}

test amend_proposal_listed_transaction_key() {
  amend_proposal(listed_transaction_key(proposer_agent))
}

test amend_proposal_range_of_one_hour() {
  amend_proposal(range_of_one_hour(proposer_agent))
}

test amend_proposal_subagent_of_active_parent() {
  amend_proposal(subagent_of_active_parent(proposer_agent))
}

test amend_proposal_suspended_agent_fails() {
  !amend_proposal(suspended_agent(proposer_agent))
}

test amend_proposal_unaccountable_agent_fails() {
  !amend_proposal(unaccountable_agent(proposer_agent))
}

test amend_proposal_accountability_ends_before_upper_bound_fails() {
  !amend_proposal(accountability_ends_before_upper_bound(proposer_agent))
}

test amend_proposal_revoked_agent_fails() {
  !amend_proposal(revoked_agent(proposer_agent))
}

test amend_proposal_retired_agent_fails() {
  !amend_proposal(retired_agent(proposer_agent))
}

test amend_proposal_unlisted_key_fails() {
  !amend_proposal(unlisted_key(proposer_agent))
}

test amend_proposal_key_without_transaction_purpose_fails() {
  !amend_proposal(key_without_transaction_purpose(proposer_agent))
}

test amend_proposal_p256_key_fails() {
  !amend_proposal(p256_key_credential(proposer_agent))
}

test amend_proposal_script_credential_fails() {
  !amend_proposal(script_credential(proposer_agent))
}

test amend_proposal_unsigned_credential_fails() {
  !amend_proposal(unsigned_credential(proposer_agent))
}

test amend_proposal_other_agent_controller_fails() {
  !amend_proposal(other_agent_controller(proposer_agent))
}

test amend_proposal_record_with_stake_credential_fails() {
  !amend_proposal(record_with_stake_credential(proposer_agent))
}

test amend_proposal_record_at_other_script_fails() {
  !amend_proposal(record_at_other_script(proposer_agent))
}

test amend_proposal_record_with_fake_token_fails() {
  !amend_proposal(record_with_fake_token(proposer_agent))
}

test amend_proposal_record_of_other_agent_fails() {
  !amend_proposal(record_of_other_agent(proposer_agent))
}

test amend_proposal_without_record_fails() {
  !amend_proposal(without_record(proposer_agent))
}

test amend_proposal_range_over_one_hour_fails() {
  !amend_proposal(range_over_one_hour(proposer_agent))
}

test amend_proposal_range_without_upper_bound_fails() {
  !amend_proposal(range_without_upper_bound(proposer_agent))
}

/// The validator fails first at `expect Some(current_slot)`.
test amend_proposal_range_without_lower_bound_fails() fail {
  amend_proposal(range_without_lower_bound(proposer_agent))
}

/// The identity check also rejects the range without a lower bound.
test amend_proposal_check_rejects_range_without_lower_bound() {
  let c = range_without_lower_bound(proposer_agent)
  let tx = amend_proposal_tx(c)
  expect AmendProposal { new_hash, new_uri, incorporated } = amendment()
  expect [input] = tx.inputs
  !proposal_validation.validate_amend_proposal(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    proposal_of(c),
    new_hash,
    new_uri,
    incorporated,
    input.output_reference,
    tx,
  )
}

test amend_proposal_subagent_of_suspended_parent_fails() {
  !amend_proposal(subagent_of_suspended_parent(proposer_agent))
}

test amend_proposal_subagent_of_bumped_parent_epoch_fails() {
  !amend_proposal(subagent_of_bumped_parent_epoch(proposer_agent))
}

/// Fails in vector_identity.effective: the parent record is missing.
test amend_proposal_subagent_without_parent_fails() fail {
  amend_proposal(subagent_without_parent(proposer_agent))
}

// ============================================================================
// MintCritiqueToken (critique submission)
// ============================================================================

test submit_critique_active_agent() {
  submit_critique(active(critic_agent))
}

test submit_critique_listed_transaction_key() {
  submit_critique(listed_transaction_key(critic_agent))
}

test submit_critique_range_of_one_hour() {
  submit_critique(range_of_one_hour(critic_agent))
}

test submit_critique_subagent_of_active_parent() {
  submit_critique(subagent_of_active_parent(critic_agent))
}

test submit_critique_suspended_agent_fails() {
  !submit_critique(suspended_agent(critic_agent))
}

test submit_critique_unaccountable_agent_fails() {
  !submit_critique(unaccountable_agent(critic_agent))
}

test submit_critique_accountability_ends_before_upper_bound_fails() {
  !submit_critique(accountability_ends_before_upper_bound(critic_agent))
}

test submit_critique_revoked_agent_fails() {
  !submit_critique(revoked_agent(critic_agent))
}

test submit_critique_retired_agent_fails() {
  !submit_critique(retired_agent(critic_agent))
}

test submit_critique_unlisted_key_fails() {
  !submit_critique(unlisted_key(critic_agent))
}

test submit_critique_key_without_transaction_purpose_fails() {
  !submit_critique(key_without_transaction_purpose(critic_agent))
}

test submit_critique_p256_key_fails() {
  !submit_critique(p256_key_credential(critic_agent))
}

test submit_critique_script_credential_fails() {
  !submit_critique(script_credential(critic_agent))
}

test submit_critique_unsigned_credential_fails() {
  !submit_critique(unsigned_credential(critic_agent))
}

test submit_critique_other_agent_controller_fails() {
  !submit_critique(other_agent_controller(critic_agent))
}

test submit_critique_record_with_stake_credential_fails() {
  !submit_critique(record_with_stake_credential(critic_agent))
}

test submit_critique_record_at_other_script_fails() {
  !submit_critique(record_at_other_script(critic_agent))
}

test submit_critique_record_with_fake_token_fails() {
  !submit_critique(record_with_fake_token(critic_agent))
}

test submit_critique_record_of_other_agent_fails() {
  !submit_critique(record_of_other_agent(critic_agent))
}

test submit_critique_without_record_fails() {
  !submit_critique(without_record(critic_agent))
}

test submit_critique_range_over_one_hour_fails() {
  !submit_critique(range_over_one_hour(critic_agent))
}

test submit_critique_range_without_upper_bound_fails() {
  !submit_critique(range_without_upper_bound(critic_agent))
}

test submit_critique_range_without_lower_bound_fails() {
  !submit_critique(range_without_lower_bound(critic_agent))
}

test submit_critique_subagent_of_suspended_parent_fails() {
  !submit_critique(subagent_of_suspended_parent(critic_agent))
}

test submit_critique_subagent_of_bumped_parent_epoch_fails() {
  !submit_critique(subagent_of_bumped_parent_epoch(critic_agent))
}

/// Fails in vector_identity.effective: the parent record is missing.
test submit_critique_subagent_without_parent_fails() fail {
  submit_critique(subagent_without_parent(critic_agent))
}

// ============================================================================
// MintEndorsementToken (endorsement submission)
// ============================================================================

test submit_endorsement_active_agent() {
  submit_endorsement(active(endorser_agent))
}

test submit_endorsement_listed_transaction_key() {
  submit_endorsement(listed_transaction_key(endorser_agent))
}

test submit_endorsement_range_of_one_hour() {
  submit_endorsement(range_of_one_hour(endorser_agent))
}

test submit_endorsement_subagent_of_active_parent() {
  submit_endorsement(subagent_of_active_parent(endorser_agent))
}

test submit_endorsement_suspended_agent_fails() {
  !submit_endorsement(suspended_agent(endorser_agent))
}

test submit_endorsement_unaccountable_agent_fails() {
  !submit_endorsement(unaccountable_agent(endorser_agent))
}

test submit_endorsement_accountability_ends_before_upper_bound_fails() {
  !submit_endorsement(accountability_ends_before_upper_bound(endorser_agent))
}

test submit_endorsement_revoked_agent_fails() {
  !submit_endorsement(revoked_agent(endorser_agent))
}

test submit_endorsement_retired_agent_fails() {
  !submit_endorsement(retired_agent(endorser_agent))
}

test submit_endorsement_unlisted_key_fails() {
  !submit_endorsement(unlisted_key(endorser_agent))
}

test submit_endorsement_key_without_transaction_purpose_fails() {
  !submit_endorsement(key_without_transaction_purpose(endorser_agent))
}

test submit_endorsement_p256_key_fails() {
  !submit_endorsement(p256_key_credential(endorser_agent))
}

test submit_endorsement_script_credential_fails() {
  !submit_endorsement(script_credential(endorser_agent))
}

test submit_endorsement_unsigned_credential_fails() {
  !submit_endorsement(unsigned_credential(endorser_agent))
}

test submit_endorsement_other_agent_controller_fails() {
  !submit_endorsement(other_agent_controller(endorser_agent))
}

test submit_endorsement_record_with_stake_credential_fails() {
  !submit_endorsement(record_with_stake_credential(endorser_agent))
}

test submit_endorsement_record_at_other_script_fails() {
  !submit_endorsement(record_at_other_script(endorser_agent))
}

test submit_endorsement_record_with_fake_token_fails() {
  !submit_endorsement(record_with_fake_token(endorser_agent))
}

test submit_endorsement_record_of_other_agent_fails() {
  !submit_endorsement(record_of_other_agent(endorser_agent))
}

test submit_endorsement_without_record_fails() {
  !submit_endorsement(without_record(endorser_agent))
}

test submit_endorsement_range_over_one_hour_fails() {
  !submit_endorsement(range_over_one_hour(endorser_agent))
}

test submit_endorsement_range_without_upper_bound_fails() {
  !submit_endorsement(range_without_upper_bound(endorser_agent))
}

test submit_endorsement_range_without_lower_bound_fails() {
  !submit_endorsement(range_without_lower_bound(endorser_agent))
}

test submit_endorsement_subagent_of_suspended_parent_fails() {
  !submit_endorsement(subagent_of_suspended_parent(endorser_agent))
}

test submit_endorsement_subagent_of_bumped_parent_epoch_fails() {
  !submit_endorsement(subagent_of_bumped_parent_epoch(endorser_agent))
}

/// Fails in vector_identity.effective: the parent record is missing.
test submit_endorsement_subagent_without_parent_fails() fail {
  submit_endorsement(subagent_without_parent(endorser_agent))
}

// ============================================================================
// Exits: no identity check
// ============================================================================

test withdraw_proposal_suspended_agent() {
  withdraw_proposal(
    only_record(proposer_agent, suspended(record(proposer_agent, []), hi)),
    ten_minutes(),
  )
}

test withdraw_proposal_revoked_agent() {
  withdraw_proposal(
    only_record(proposer_agent, revoked(record(proposer_agent, []))),
    ten_minutes(),
  )
}

test withdraw_proposal_retired_agent() {
  withdraw_proposal(
    only_record(proposer_agent, retired(record(proposer_agent, []))),
    ten_minutes(),
  )
}

test withdraw_proposal_without_record() {
  withdraw_proposal([], ten_minutes())
}

test withdraw_proposal_range_over_one_hour() {
  withdraw_proposal([], two_hours())
}

test withdraw_critique_suspended_agent() {
  withdraw_critique(
    only_record(critic_agent, suspended(record(critic_agent, []), hi)),
    ten_minutes(),
  )
}

test withdraw_critique_revoked_agent() {
  withdraw_critique(
    only_record(critic_agent, revoked(record(critic_agent, []))),
    ten_minutes(),
  )
}

test withdraw_critique_retired_agent() {
  withdraw_critique(
    only_record(critic_agent, retired(record(critic_agent, []))),
    ten_minutes(),
  )
}

test withdraw_critique_without_record() {
  withdraw_critique([], ten_minutes())
}

test withdraw_critique_range_over_one_hour() {
  withdraw_critique([], two_hours())
}

test withdraw_endorsement_suspended_agent() {
  withdraw_endorsement(
    only_record(endorser_agent, suspended(record(endorser_agent, []), hi)),
    ten_minutes(),
  )
}

test withdraw_endorsement_revoked_agent() {
  withdraw_endorsement(
    only_record(endorser_agent, revoked(record(endorser_agent, []))),
    ten_minutes(),
  )
}

test withdraw_endorsement_retired_agent() {
  withdraw_endorsement(
    only_record(endorser_agent, retired(record(endorser_agent, []))),
    ten_minutes(),
  )
}

test withdraw_endorsement_without_record() {
  withdraw_endorsement([], ten_minutes())
}

test withdraw_endorsement_range_over_one_hour() {
  withdraw_endorsement([], two_hours())
}
