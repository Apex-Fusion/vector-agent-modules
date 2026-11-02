//// Identity registry checks of Module 3 (Part 2 §7 of
//// `docs/IDENTITY_REGISTRY.md` in agents-marketplace, Module-3 row).
////
//// Each test builds a full transaction and calls a validation function of
//// the library. The validator-level cases (WithdrawChallenge and
//// MintHistoryBonus) are in validators/identity_check_tests.ak.
////
//// - Actions that open or extend a position (CreateStake, IncreaseStake,
////   UpdateCapabilities, MintEndorsementToken, IncreaseEndorsement,
////   MintChallengeToken) need the acting agent's record with effective
////   status Active, its controller's signature, and each staked capability
////   in the record. The target of an endorsement or a challenge needs
////   effective status Active and the capabilities in its record.
//// - Exits (DecreaseStake, WithdrawEndorsement) need only the controller's
////   signature; the record can have any status.
//// - Every action that reads an identity record has a validity range of at
////   most one hour.
////
//// test_agent_did has controller 1 and test_agent_did_2 has controller 2.

use aiken/interval
use cardano/address.{Script, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, Transaction, ValidityRange}
use reputation_staking/challenge_validation.{
  validate_distribute_outcome, validate_mint_challenge,
}
use reputation_staking/endorsement_validation.{
  validate_increase_endorsement, validate_mint_endorsement,
  validate_withdraw_endorsement,
}
use reputation_staking/stake_validation.{
  validate_create_stake, validate_decrease_stake, validate_increase_stake,
  validate_update_capabilities,
}
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_input, make_output,
  make_output_no_datum, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_capabilities, test_config,
  test_controller_hash, test_endorsement_policy_id, test_owner_key_hash,
  test_owner_key_hash_2, test_params, test_refs, test_reputation_policy_id,
  test_treasury_script_hash, test_tx_hash,
}
use reputation_staking/types.{
  CapabilityVerified, Challenge, EndorsementDatum, Open,
  ReputationChallengeDatum, Resolved, StakeDatum,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
  derive_stake_token_name,
}
use shared/identity_fixtures.{
  accountable_until, agent_id, created_at, record, record_input,
  record_input_at_other_script, record_input_with_fake_token,
  record_input_with_stake, retired, revoked, subagent, suspended, unaccountable,
  with_subagent_epoch,
}
use vector_identity/types.{IdentityDatum} as identity_types

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

/// The lower bound of the validity range of the test transactions.
const t0: Int = 1_700_000_000_000

/// A validity range of ten minutes.
fn range() -> ValidityRange {
  interval.between(t0, t0 + 600_000)
}

/// A validity range of exactly one hour (allowed).
fn hour_range() -> ValidityRange {
  interval.between(t0, t0 + 3_600_000)
}

/// A validity range of one hour and one millisecond (not allowed).
fn long_range() -> ValidityRange {
  interval.between(t0, t0 + 3_600_001)
}

/// A validity range without an upper bound (not allowed).
fn open_range() -> ValidityRange {
  interval.after(t0)
}

fn agent_record() -> IdentityDatum {
  record(1, test_capabilities)
}

fn target_record() -> IdentityDatum {
  record(2, test_capabilities)
}

fn agent_ref(r: IdentityDatum) -> Input {
  record_input(test_agent_did, r)
}

fn target_ref(r: IdentityDatum) -> Input {
  record_input(test_agent_did_2, r)
}

/// Both records, Active.
fn both() -> List<Input> {
  [agent_ref(agent_record()), target_ref(target_record())]
}

/// The acting agent's record is `r`; the target's record is Active.
fn agent_as(r: IdentityDatum) -> List<Input> {
  [agent_ref(r), target_ref(target_record())]
}

/// The target's record is `r`; the acting agent's record is Active.
fn target_as(r: IdentityDatum) -> List<Input> {
  [agent_ref(agent_record()), target_ref(r)]
}

/// The acting agent's record at another input (a wrong address or a fake
/// token); the target's record is Active.
fn agent_input(input: Input) -> List<Input> {
  [input, target_ref(target_record())]
}

/// A hold of the acting agent's controller that is still in force.
fn agent_suspended() -> IdentityDatum {
  suspended(agent_record(), t0 + 1)
}

/// The record with the terminal status of `tombstone`, but with its own
/// capabilities. The identity contract writes a tombstone without
/// capabilities; this record shows that the status alone stops the action.
fn ended(record: IdentityDatum, tombstone: IdentityDatum) -> IdentityDatum {
  IdentityDatum { ..record, status: tombstone.status }
}

fn parent_id() -> ByteArray {
  agent_id(9)
}

fn parent_record() -> IdentityDatum {
  record(9, [])
}

/// The acting agent is a subagent (depth 1, parent epoch 0) of the parent
/// record `parent`.
fn subagent_refs(parent: IdentityDatum) -> List<Input> {
  [
    agent_ref(subagent(agent_record(), parent_id(), 0, 1)),
    record_input(parent_id(), parent),
    target_ref(target_record()),
  ]
}

/// The acting agent is a subagent, but its parent's record is missing.
fn subagent_without_parent() -> List<Input> {
  [
    agent_ref(subagent(agent_record(), parent_id(), 0, 1)),
    target_ref(target_record()),
  ]
}

/// The owner key and the controller of test_agent_did sign.
fn signers() -> List<ByteArray> {
  agent_signers()
}

/// Only the owner key signs (no controller signature).
fn owner_only() -> List<ByteArray> {
  [test_owner_key_hash]
}

/// The owner key and the controller of another agent sign.
fn other_controller() -> List<ByteArray> {
  [test_owner_key_hash, test_controller_hash(2)]
}

/// Only the controller of test_agent_did signs.
fn controller_only() -> List<ByteArray> {
  [test_controller_hash(1)]
}

fn tx_with(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  outputs: List<Output>,
) -> Transaction {
  Transaction {
    ..base_tx(t0),
    validity_range,
    reference_inputs: refs,
    outputs,
    extra_signatories: signers,
  }
}

fn ap3x_to(key: ByteArray, amount: Int) -> Output {
  make_output_no_datum(
    key_address(key),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

// Stakes

fn stake_datum(
  amount: Int,
  capabilities: List<ByteArray>,
  last_updated: Int,
) -> StakeDatum {
  StakeDatum {
    agent_did: test_agent_did,
    owner_credential: VerificationKey(test_owner_key_hash),
    stake_amount: amount,
    staked_capabilities: capabilities,
    last_updated,
    history_points: 0,
  }
}

fn stake_output(datum: StakeDatum) -> Output {
  make_output(
    script_address(test_reputation_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(
          test_ap3x_policy_id,
          test_ap3x_asset_name,
          datum.stake_amount,
        )
      |> assets.add(
          test_reputation_policy_id,
          derive_stake_token_name(datum.agent_did),
          1,
        ),
    datum,
  )
}

/// The StakeUTXO of test_agent_did_2 (owner key 2, the test capabilities).
fn target_stake_ref() -> Input {
  let datum =
    StakeDatum {
      agent_did: test_agent_did_2,
      owner_credential: VerificationKey(test_owner_key_hash_2),
      stake_amount: 20_000_000,
      staked_capabilities: test_capabilities,
      last_updated: 500,
      history_points: 0,
    }
  make_input(
    test_tx_hash,
    50,
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did_2),
            1,
          ),
      datum,
    ),
  )
}

fn create_stake(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  capabilities: List<ByteArray>,
) -> Bool {
  let datum = stake_datum(10_000_000, capabilities, t0)
  let output = stake_output(datum)
  validate_create_stake(
    tx_with(refs, signers, validity_range, [output]),
    test_config(),
    test_refs(),
    test_params(),
    datum,
    output,
    t0,
  )
}

fn increase_stake(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  capabilities: List<ByteArray>,
) -> Bool {
  let input = stake_datum(10_000_000, capabilities, 1_000)
  let datum = stake_datum(20_000_000, capabilities, t0)
  let output = stake_output(datum)
  validate_increase_stake(
    tx_with(refs, signers, validity_range, [output]),
    test_config(),
    test_refs(),
    input,
    datum,
    output,
    t0,
  )
}

fn update_capabilities(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  new_capabilities: List<ByteArray>,
) -> Bool {
  // The stake cooldown (24 h) ended at t0.
  let input = stake_datum(10_000_000, ["code_review"], t0 - 86_400_000)
  let datum =
    StakeDatum {
      ..input,
      staked_capabilities: new_capabilities,
      last_updated: t0,
    }
  let output = stake_output(datum)
  validate_update_capabilities(
    tx_with(refs, signers, validity_range, [output]),
    test_config(),
    test_refs(),
    test_params(),
    input,
    datum,
    output,
    new_capabilities,
    t0,
  )
}

fn decrease_stake(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
) -> Bool {
  let input = stake_datum(20_000_000, ["code_review"], t0 - 86_400_000)
  let datum = StakeDatum { ..input, stake_amount: 15_000_000, last_updated: t0 }
  let output = stake_output(datum)
  validate_decrease_stake(
    tx_with(
      refs,
      signers,
      validity_range,
      [output, ap3x_to(test_owner_key_hash, 5_000_000)],
    ),
    test_config(),
    test_refs(),
    test_params(),
    input,
    datum,
    output,
    5_000_000,
    t0,
  )
}

// Endorsements

fn endorsement_datum(
  capabilities: List<ByteArray>,
  amount: Int,
  created: Int,
) -> EndorsementDatum {
  EndorsementDatum {
    endorser_did: test_agent_did,
    endorser_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    stake_amount: amount,
    endorsed_capabilities: capabilities,
    created_at: created,
  }
}

fn endorsement_token() -> ByteArray {
  derive_endorsement_token_name(test_agent_did, test_agent_did_2)
}

fn endorsement_output(datum: EndorsementDatum) -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(
          test_ap3x_policy_id,
          test_ap3x_asset_name,
          datum.stake_amount,
        )
      |> assets.add(test_endorsement_policy_id, endorsement_token(), 1),
    datum,
  )
}

fn mint_endorsement(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  capabilities: List<ByteArray>,
) -> Bool {
  let datum = endorsement_datum(capabilities, 5_000_000, t0)
  let output = endorsement_output(datum)
  validate_mint_endorsement(
    tx_with([target_stake_ref(), ..refs], signers, validity_range, [output]),
    test_config(),
    test_refs(),
    test_params(),
    datum,
    output,
  )
}

fn increase_endorsement(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  capabilities: List<ByteArray>,
) -> Bool {
  let input = endorsement_datum(capabilities, 5_000_000, 1_000)
  let datum = EndorsementDatum { ..input, stake_amount: 10_000_000 }
  let output = endorsement_output(datum)
  validate_increase_endorsement(
    tx_with(refs, signers, validity_range, [output]),
    test_config(),
    test_refs(),
    input,
    datum,
    output,
  )
}

fn withdraw_endorsement(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
) -> Bool {
  // The endorsement cooldown (48 h) ended at t0.
  let input = endorsement_datum(["code_review"], 5_000_000, t0 - 172_800_000)
  let tx =
    Transaction {
      ..tx_with(
        refs,
        signers,
        validity_range,
        [ap3x_to(test_owner_key_hash, 5_000_000)],
      ),
      mint: assets.from_asset(
        test_endorsement_policy_id,
        endorsement_token(),
        -1,
      ),
    }
  validate_withdraw_endorsement(
    tx,
    test_config(),
    test_refs(),
    test_params(),
    input,
    t0,
  )
}

// Challenges

fn challenge_datum(
  capability: ByteArray,
  target_credential: address.Credential,
) -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did,
    challenger_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    target_credential,
    challenged_capability: capability,
    stake_amount: 25_000_000,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at: t0,
    counter_evidence_hash: "",
    counter_evidence_uri: "",
    response_submitted_at: 0,
    state: Open,
  }
}

fn challenge_token(capability: ByteArray) -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, capability)
}

fn mint_challenge_with(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
  capability: ByteArray,
  target_credential: address.Credential,
) -> Bool {
  let datum = challenge_datum(capability, target_credential)
  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
        |> assets.add(
            test_endorsement_policy_id,
            challenge_token(capability),
            1,
          ),
      Challenge(datum),
    )
  validate_mint_challenge(
    tx_with([target_stake_ref(), ..refs], signers, validity_range, [output]),
    test_config(),
    test_refs(),
    test_params(),
    datum,
    output,
  )
}

fn mint_challenge(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
) -> Bool {
  mint_challenge_with(
    refs,
    signers,
    validity_range,
    "code_review",
    VerificationKey(test_owner_key_hash_2),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// CreateStake
// ──────────────────────────────────────────────────────────────────────────────

test create_stake_active_agent() {
  create_stake(both(), signers(), range(), ["code_review", "testing"])
}

test create_stake_range_of_one_hour() {
  create_stake(both(), signers(), hour_range(), ["code_review"])
}

test create_stake_suspended_agent_fails() {
  !create_stake(
    agent_as(agent_suspended()),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_unaccountable_agent_fails() {
  !create_stake(
    agent_as(unaccountable(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

/// The Accountable Party's period ends inside the validity range.
test create_stake_accountable_party_ending_in_range_fails() {
  !create_stake(
    agent_as(accountable_until(agent_record(), t0 + 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_revoked_agent_fails() {
  !create_stake(
    agent_as(ended(agent_record(), revoked(agent_record()))),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_retired_agent_fails() {
  !create_stake(
    agent_as(ended(agent_record(), retired(agent_record()))),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_without_controller_signature_fails() {
  !create_stake(both(), owner_only(), range(), ["code_review"])
}

test create_stake_signed_by_other_controller_fails() {
  !create_stake(both(), other_controller(), range(), ["code_review"])
}

/// The old owner_credential signature is still needed.
test create_stake_without_owner_signature_fails() {
  !create_stake(both(), controller_only(), range(), ["code_review"])
}

test create_stake_capability_not_in_record_fails() {
  !create_stake(both(), signers(), range(), ["code_review", "translation"])
}

test create_stake_record_with_stake_credential_fails() {
  !create_stake(
    agent_input(record_input_with_stake(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_record_at_other_script_fails() {
  !create_stake(
    agent_input(record_input_at_other_script(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_record_with_fake_token_fails() {
  !create_stake(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

/// Only the record of another agent (test_agent_did_2) is present.
test create_stake_record_of_other_agent_fails() {
  !create_stake(
    [target_ref(target_record())],
    other_controller(),
    range(),
    ["code_review"],
  )
}

test create_stake_range_over_one_hour_fails() {
  !create_stake(both(), signers(), long_range(), ["code_review"])
}

test create_stake_open_range_fails() {
  !create_stake(both(), signers(), open_range(), ["code_review"])
}

test create_stake_subagent_active_parent() {
  create_stake(
    subagent_refs(parent_record()),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_subagent_suspended_parent_fails() {
  !create_stake(
    subagent_refs(suspended(parent_record(), t0 + 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_subagent_after_parent_epoch_increase_fails() {
  !create_stake(
    subagent_refs(with_subagent_epoch(parent_record(), 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_subagent_revoked_parent_fails() {
  !create_stake(
    subagent_refs(revoked(parent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test create_stake_subagent_without_parent_record_fails() fail {
  create_stake(subagent_without_parent(), signers(), range(), ["code_review"])
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseStake
// ──────────────────────────────────────────────────────────────────────────────

test increase_stake_active_agent() {
  increase_stake(both(), signers(), range(), ["code_review", "testing"])
}

test increase_stake_range_of_one_hour() {
  increase_stake(both(), signers(), hour_range(), ["code_review"])
}

test increase_stake_suspended_agent_fails() {
  !increase_stake(
    agent_as(agent_suspended()),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_stake_unaccountable_agent_fails() {
  !increase_stake(
    agent_as(unaccountable(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_stake_revoked_agent_fails() {
  !increase_stake(agent_as(revoked(agent_record())), signers(), range(), [])
}

test increase_stake_retired_agent_fails() {
  !increase_stake(agent_as(retired(agent_record())), signers(), range(), [])
}

test increase_stake_without_controller_signature_fails() {
  !increase_stake(both(), owner_only(), range(), ["code_review"])
}

test increase_stake_signed_by_other_controller_fails() {
  !increase_stake(both(), other_controller(), range(), ["code_review"])
}

/// A staked capability that the record no longer lists.
test increase_stake_capability_not_in_record_fails() {
  !increase_stake(both(), signers(), range(), ["code_review", "translation"])
}

test increase_stake_record_with_stake_credential_fails() {
  !increase_stake(
    agent_input(record_input_with_stake(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_stake_record_with_fake_token_fails() {
  !increase_stake(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_stake_without_record_fails() {
  !increase_stake([], signers(), range(), ["code_review"])
}

test increase_stake_range_over_one_hour_fails() {
  !increase_stake(both(), signers(), long_range(), ["code_review"])
}

test increase_stake_open_range_fails() {
  !increase_stake(both(), signers(), open_range(), ["code_review"])
}

test increase_stake_subagent_active_parent() {
  increase_stake(
    subagent_refs(parent_record()),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_stake_subagent_suspended_parent_fails() {
  !increase_stake(
    subagent_refs(suspended(parent_record(), t0 + 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_stake_subagent_without_parent_record_fails() fail {
  increase_stake(subagent_without_parent(), signers(), range(), ["code_review"])
}

// ──────────────────────────────────────────────────────────────────────────────
// UpdateCapabilities
// ──────────────────────────────────────────────────────────────────────────────

test update_capabilities_active_agent() {
  update_capabilities(both(), signers(), range(), ["testing", "deployment"])
}

test update_capabilities_range_of_one_hour() {
  update_capabilities(both(), signers(), hour_range(), ["testing"])
}

test update_capabilities_suspended_agent_fails() {
  !update_capabilities(
    agent_as(agent_suspended()),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_unaccountable_agent_fails() {
  !update_capabilities(
    agent_as(unaccountable(agent_record())),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_revoked_agent_fails() {
  !update_capabilities(
    agent_as(ended(agent_record(), revoked(agent_record()))),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_retired_agent_fails() {
  !update_capabilities(
    agent_as(ended(agent_record(), retired(agent_record()))),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_without_controller_signature_fails() {
  !update_capabilities(both(), owner_only(), range(), ["testing"])
}

test update_capabilities_signed_by_other_controller_fails() {
  !update_capabilities(both(), other_controller(), range(), ["testing"])
}

test update_capabilities_capability_not_in_record_fails() {
  !update_capabilities(both(), signers(), range(), ["testing", "translation"])
}

test update_capabilities_record_at_other_script_fails() {
  !update_capabilities(
    agent_input(record_input_at_other_script(test_agent_did, agent_record())),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_record_with_fake_token_fails() {
  !update_capabilities(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_range_over_one_hour_fails() {
  !update_capabilities(both(), signers(), long_range(), ["testing"])
}

test update_capabilities_open_range_fails() {
  !update_capabilities(both(), signers(), open_range(), ["testing"])
}

test update_capabilities_subagent_after_parent_epoch_increase_fails() {
  !update_capabilities(
    subagent_refs(with_subagent_epoch(parent_record(), 1)),
    signers(),
    range(),
    ["testing"],
  )
}

test update_capabilities_subagent_active_parent() {
  update_capabilities(
    subagent_refs(parent_record()),
    signers(),
    range(),
    ["testing"],
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// DecreaseStake (an exit: any status, the controller signs)
// ──────────────────────────────────────────────────────────────────────────────

test decrease_stake_active_agent() {
  decrease_stake(both(), controller_only(), range())
}

test decrease_stake_range_of_one_hour() {
  decrease_stake(both(), controller_only(), hour_range())
}

test decrease_stake_suspended_agent() {
  decrease_stake(agent_as(agent_suspended()), controller_only(), range())
}

test decrease_stake_unaccountable_agent() {
  decrease_stake(
    agent_as(unaccountable(agent_record())),
    controller_only(),
    range(),
  )
}

test decrease_stake_revoked_agent() {
  decrease_stake(agent_as(revoked(agent_record())), controller_only(), range())
}

test decrease_stake_retired_agent() {
  decrease_stake(agent_as(retired(agent_record())), controller_only(), range())
}

/// An exit reads no ancestor: a subagent exits without its parent's record,
/// also after the parent ended its subagents.
test decrease_stake_subagent_without_parent_record() {
  decrease_stake(subagent_without_parent(), controller_only(), range())
}

/// The owner_credential signature alone is not enough.
test decrease_stake_without_controller_signature_fails() {
  !decrease_stake(both(), owner_only(), range())
}

test decrease_stake_signed_by_other_controller_fails() {
  !decrease_stake(both(), [test_controller_hash(2)], range())
}

test decrease_stake_without_record_fails() {
  !decrease_stake([target_ref(target_record())], controller_only(), range())
}

test decrease_stake_record_with_stake_credential_fails() {
  !decrease_stake(
    agent_input(record_input_with_stake(test_agent_did, agent_record())),
    controller_only(),
    range(),
  )
}

test decrease_stake_record_with_fake_token_fails() {
  !decrease_stake(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    controller_only(),
    range(),
  )
}

test decrease_stake_range_over_one_hour_fails() {
  !decrease_stake(both(), controller_only(), long_range())
}

test decrease_stake_open_range_fails() {
  !decrease_stake(both(), controller_only(), open_range())
}

// ──────────────────────────────────────────────────────────────────────────────
// MintEndorsementToken (endorser acts; the target is Active and lists the
// endorsed capabilities)
// ──────────────────────────────────────────────────────────────────────────────

test mint_endorsement_active_agents() {
  mint_endorsement(both(), signers(), range(), ["code_review", "testing"])
}

test mint_endorsement_range_of_one_hour() {
  mint_endorsement(both(), signers(), hour_range(), ["code_review"])
}

test mint_endorsement_suspended_endorser_fails() {
  !mint_endorsement(
    agent_as(agent_suspended()),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_unaccountable_endorser_fails() {
  !mint_endorsement(
    agent_as(unaccountable(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_revoked_endorser_fails() {
  !mint_endorsement(
    agent_as(revoked(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_retired_endorser_fails() {
  !mint_endorsement(
    agent_as(retired(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_suspended_target_fails() {
  !mint_endorsement(
    target_as(suspended(target_record(), t0 + 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_unaccountable_target_fails() {
  !mint_endorsement(
    target_as(unaccountable(target_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_revoked_target_fails() {
  !mint_endorsement(
    target_as(ended(target_record(), revoked(target_record()))),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_retired_target_fails() {
  !mint_endorsement(
    target_as(ended(target_record(), retired(target_record()))),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_without_target_record_fails() {
  !mint_endorsement(
    [agent_ref(agent_record())],
    signers(),
    range(),
    ["code_review"],
  )
}

/// The target's stake backs "testing", but its record no longer lists it.
test mint_endorsement_capability_not_in_target_record_fails() {
  !mint_endorsement(
    target_as(record(2, ["code_review"])),
    signers(),
    range(),
    ["code_review", "testing"],
  )
}

test mint_endorsement_without_controller_signature_fails() {
  !mint_endorsement(both(), owner_only(), range(), ["code_review"])
}

/// The target's controller signs instead of the endorser's controller.
test mint_endorsement_signed_by_target_controller_fails() {
  !mint_endorsement(both(), other_controller(), range(), ["code_review"])
}

test mint_endorsement_without_endorser_credential_signature_fails() {
  !mint_endorsement(both(), controller_only(), range(), ["code_review"])
}

test mint_endorsement_record_with_stake_credential_fails() {
  !mint_endorsement(
    agent_input(record_input_with_stake(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_record_with_fake_token_fails() {
  !mint_endorsement(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_target_record_at_other_script_fails() {
  !mint_endorsement(
    [
      agent_ref(agent_record()),
      record_input_at_other_script(test_agent_did_2, target_record()),
    ],
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_range_over_one_hour_fails() {
  !mint_endorsement(both(), signers(), long_range(), ["code_review"])
}

test mint_endorsement_open_range_fails() {
  !mint_endorsement(both(), signers(), open_range(), ["code_review"])
}

test mint_endorsement_subagent_active_parent() {
  mint_endorsement(
    subagent_refs(parent_record()),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_subagent_suspended_parent_fails() {
  !mint_endorsement(
    subagent_refs(suspended(parent_record(), t0 + 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test mint_endorsement_subagent_without_parent_record_fails() fail {
  mint_endorsement(
    subagent_without_parent(),
    signers(),
    range(),
    ["code_review"],
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseEndorsement
// ──────────────────────────────────────────────────────────────────────────────

test increase_endorsement_active_agents() {
  increase_endorsement(both(), signers(), range(), ["code_review"])
}

test increase_endorsement_range_of_one_hour() {
  increase_endorsement(both(), signers(), hour_range(), ["code_review"])
}

test increase_endorsement_suspended_endorser_fails() {
  !increase_endorsement(
    agent_as(agent_suspended()),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_unaccountable_endorser_fails() {
  !increase_endorsement(
    agent_as(unaccountable(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_revoked_endorser_fails() {
  !increase_endorsement(
    agent_as(revoked(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_retired_endorser_fails() {
  !increase_endorsement(
    agent_as(retired(agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_suspended_target_fails() {
  !increase_endorsement(
    target_as(suspended(target_record(), t0 + 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_revoked_target_fails() {
  !increase_endorsement(
    target_as(ended(target_record(), revoked(target_record()))),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_capability_not_in_target_record_fails() {
  !increase_endorsement(
    target_as(record(2, ["testing"])),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_without_controller_signature_fails() {
  !increase_endorsement(both(), owner_only(), range(), ["code_review"])
}

test increase_endorsement_signed_by_target_controller_fails() {
  !increase_endorsement(both(), other_controller(), range(), ["code_review"])
}

test increase_endorsement_record_with_fake_token_fails() {
  !increase_endorsement(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_record_at_other_script_fails() {
  !increase_endorsement(
    agent_input(record_input_at_other_script(test_agent_did, agent_record())),
    signers(),
    range(),
    ["code_review"],
  )
}

test increase_endorsement_range_over_one_hour_fails() {
  !increase_endorsement(both(), signers(), long_range(), ["code_review"])
}

test increase_endorsement_open_range_fails() {
  !increase_endorsement(both(), signers(), open_range(), ["code_review"])
}

test increase_endorsement_subagent_after_parent_epoch_increase_fails() {
  !increase_endorsement(
    subagent_refs(with_subagent_epoch(parent_record(), 1)),
    signers(),
    range(),
    ["code_review"],
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawEndorsement (an exit: any status, the endorser's controller signs)
// ──────────────────────────────────────────────────────────────────────────────

test withdraw_endorsement_active_agent() {
  withdraw_endorsement(both(), controller_only(), range())
}

test withdraw_endorsement_range_of_one_hour() {
  withdraw_endorsement(both(), controller_only(), hour_range())
}

test withdraw_endorsement_suspended_agent() {
  withdraw_endorsement(agent_as(agent_suspended()), controller_only(), range())
}

test withdraw_endorsement_unaccountable_agent() {
  withdraw_endorsement(
    agent_as(unaccountable(agent_record())),
    controller_only(),
    range(),
  )
}

test withdraw_endorsement_revoked_agent() {
  withdraw_endorsement(
    agent_as(revoked(agent_record())),
    controller_only(),
    range(),
  )
}

test withdraw_endorsement_retired_agent() {
  withdraw_endorsement(
    agent_as(retired(agent_record())),
    controller_only(),
    range(),
  )
}

/// The target does not matter for an exit: its record can be missing.
test withdraw_endorsement_without_target_record() {
  withdraw_endorsement([agent_ref(agent_record())], controller_only(), range())
}

test withdraw_endorsement_subagent_without_parent_record() {
  withdraw_endorsement(subagent_without_parent(), controller_only(), range())
}

test withdraw_endorsement_without_controller_signature_fails() {
  !withdraw_endorsement(both(), owner_only(), range())
}

test withdraw_endorsement_signed_by_target_controller_fails() {
  !withdraw_endorsement(both(), [test_controller_hash(2)], range())
}

test withdraw_endorsement_without_record_fails() {
  !withdraw_endorsement(
    [target_ref(target_record())],
    controller_only(),
    range(),
  )
}

test withdraw_endorsement_record_with_stake_credential_fails() {
  !withdraw_endorsement(
    agent_input(record_input_with_stake(test_agent_did, agent_record())),
    controller_only(),
    range(),
  )
}

test withdraw_endorsement_record_with_fake_token_fails() {
  !withdraw_endorsement(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    controller_only(),
    range(),
  )
}

test withdraw_endorsement_range_over_one_hour_fails() {
  !withdraw_endorsement(both(), controller_only(), long_range())
}

test withdraw_endorsement_open_range_fails() {
  !withdraw_endorsement(both(), controller_only(), open_range())
}

// ──────────────────────────────────────────────────────────────────────────────
// MintChallengeToken (challenger acts and is old enough; the target is Active
// and lists the challenged capability)
// ──────────────────────────────────────────────────────────────────────────────

test mint_challenge_active_agents() {
  mint_challenge(both(), signers(), range())
}

test mint_challenge_range_of_one_hour() {
  mint_challenge(both(), signers(), hour_range())
}

/// min_agent_age (24 h) after the challenger's created_at is exactly t0.
test mint_challenge_challenger_exactly_old_enough() {
  mint_challenge(
    agent_as(created_at(agent_record(), t0 - 86_400_000)),
    signers(),
    range(),
  )
}

/// The age comes from the record's created_at and uses the lower bound.
test mint_challenge_challenger_too_young_fails() {
  !mint_challenge(
    agent_as(created_at(agent_record(), t0 - 86_400_000 + 1)),
    signers(),
    range(),
  )
}

test mint_challenge_suspended_challenger_fails() {
  !mint_challenge(agent_as(agent_suspended()), signers(), range())
}

test mint_challenge_unaccountable_challenger_fails() {
  !mint_challenge(agent_as(unaccountable(agent_record())), signers(), range())
}

test mint_challenge_revoked_challenger_fails() {
  !mint_challenge(agent_as(revoked(agent_record())), signers(), range())
}

test mint_challenge_retired_challenger_fails() {
  !mint_challenge(agent_as(retired(agent_record())), signers(), range())
}

test mint_challenge_suspended_target_fails() {
  !mint_challenge(
    target_as(suspended(target_record(), t0 + 1)),
    signers(),
    range(),
  )
}

test mint_challenge_unaccountable_target_fails() {
  !mint_challenge(target_as(unaccountable(target_record())), signers(), range())
}

test mint_challenge_revoked_target_fails() {
  !mint_challenge(
    target_as(ended(target_record(), revoked(target_record()))),
    signers(),
    range(),
  )
}

test mint_challenge_retired_target_fails() {
  !mint_challenge(
    target_as(ended(target_record(), retired(target_record()))),
    signers(),
    range(),
  )
}

/// The target's stake backs "code_review", but its record does not list it.
test mint_challenge_capability_not_in_target_record_fails() {
  !mint_challenge(target_as(record(2, ["testing"])), signers(), range())
}

test mint_challenge_without_target_record_fails() {
  !mint_challenge([agent_ref(agent_record())], signers(), range())
}

test mint_challenge_without_controller_signature_fails() {
  !mint_challenge(both(), owner_only(), range())
}

test mint_challenge_signed_by_target_controller_fails() {
  !mint_challenge(both(), other_controller(), range())
}

test mint_challenge_without_challenger_credential_signature_fails() {
  !mint_challenge(both(), controller_only(), range())
}

/// The challenge must name the target stake's owner credential as the target
/// credential: the target's payouts and its response use it.
test mint_challenge_with_other_target_credential_fails() {
  !mint_challenge_with(
    both(),
    signers(),
    range(),
    "code_review",
    VerificationKey(test_owner_key_hash),
  )
}

test mint_challenge_record_with_stake_credential_fails() {
  !mint_challenge(
    agent_input(record_input_with_stake(test_agent_did, agent_record())),
    signers(),
    range(),
  )
}

test mint_challenge_record_at_other_script_fails() {
  !mint_challenge(
    agent_input(record_input_at_other_script(test_agent_did, agent_record())),
    signers(),
    range(),
  )
}

test mint_challenge_record_with_fake_token_fails() {
  !mint_challenge(
    agent_input(record_input_with_fake_token(test_agent_did, agent_record())),
    signers(),
    range(),
  )
}

test mint_challenge_target_record_with_fake_token_fails() {
  !mint_challenge(
    [
      agent_ref(agent_record()),
      record_input_with_fake_token(test_agent_did_2, target_record()),
    ],
    signers(),
    range(),
  )
}

test mint_challenge_range_over_one_hour_fails() {
  !mint_challenge(both(), signers(), long_range())
}

test mint_challenge_open_range_fails() {
  !mint_challenge(both(), signers(), open_range())
}

test mint_challenge_subagent_active_parent() {
  mint_challenge(subagent_refs(parent_record()), signers(), range())
}

test mint_challenge_subagent_unaccountable_parent_fails() {
  !mint_challenge(
    subagent_refs(unaccountable(parent_record())),
    signers(),
    range(),
  )
}

test mint_challenge_subagent_after_parent_epoch_increase_fails() {
  !mint_challenge(
    subagent_refs(with_subagent_epoch(parent_record(), 1)),
    signers(),
    range(),
  )
}

test mint_challenge_subagent_without_parent_record_fails() fail {
  mint_challenge(subagent_without_parent(), signers(), range())
}

// ──────────────────────────────────────────────────────────────────────────────
// DistributeOutcome (CapabilityVerified): no history bonus needed
// ──────────────────────────────────────────────────────────────────────────────

/// The verified outcome pays the target and the treasury without a history
/// bonus mint, so a target that is not Active (or does not sign) cannot hold
/// the challenge stake.
test distribute_verified_without_history_bonus() {
  let datum =
    ReputationChallengeDatum {
      ..challenge_datum("code_review", VerificationKey(test_owner_key_hash_2)),
      state: Resolved { outcome: CapabilityVerified },
    }
  // protocol_fee_rate = 500 bp: fee = 1_250_000, target reward = 23_750_000
  let tx =
    Transaction {
      ..base_tx(t0),
      outputs: [
        ap3x_to(test_owner_key_hash_2, 23_750_000),
        make_output_no_datum(
          script_address(test_treasury_script_hash),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 1_250_000),
        ),
      ],
      mint: assets.from_asset(
        test_endorsement_policy_id,
        challenge_token("code_review"),
        -1,
      ),
    }
  validate_distribute_outcome(
    tx,
    test_config(),
    test_refs(),
    test_params(),
    datum,
  )
}

test distribute_verified_underpaying_target_fails() {
  let datum =
    ReputationChallengeDatum {
      ..challenge_datum("code_review", VerificationKey(test_owner_key_hash_2)),
      state: Resolved { outcome: CapabilityVerified },
    }
  let tx =
    Transaction {
      ..base_tx(t0),
      outputs: [
        ap3x_to(test_owner_key_hash_2, 23_749_999),
        make_output_no_datum(
          script_address(test_treasury_script_hash),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 1_250_000),
        ),
      ],
      mint: assets.from_asset(
        test_endorsement_policy_id,
        challenge_token("code_review"),
        -1,
      ),
    }
  !validate_distribute_outcome(
    tx,
    test_config(),
    test_refs(),
    test_params(),
    datum,
  )
}

/// A script credential with the hash of the stake owner's key is not the
/// stake owner's credential.
test target_credential_script_is_not_the_stake_owner() {
  !mint_challenge_with(
    both(),
    signers(),
    range(),
    "code_review",
    Script(test_owner_key_hash_2),
  )
}
