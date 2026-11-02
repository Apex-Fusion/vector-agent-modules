//// Identity registry checks of Module 3 at validator level (Part 2 §7 of
//// `docs/IDENTITY_REGISTRY.md` in agents-marketplace, Module-3 row):
////
//// - WithdrawChallenge (an exit): the controller of the challenger's record
////   signs; the record can have any status.
//// - MintHistoryBonus (extends a position): the bonus agent's record has
////   effective status Active and its controller signs.
////
//// Each test builds a full transaction and calls the handler of the
//// validator. Both actions read an identity record, so the validity range
//// is at most one hour long. test_agent_did has controller 1.

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{
  Input, Output, OutputReference, Transaction, ValidityRange,
}
use endorsement
use reputation
use reputation_staking/test_helpers.{
  base_tx, key_address, make_input, make_output, make_output_no_datum,
  make_params_ref_input, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_capabilities, test_config,
  test_controller_hash, test_endorsement_policy_id, test_oracle_credential,
  test_owner_key_hash, test_owner_key_hash_2, test_refs, test_refs_token_policy,
  test_reputation_policy_id, test_tx_hash,
}
use reputation_staking/types.{
  Challenge, ChallengeSpend, ChallengeWon, HistoryBonusDatum, MintHistoryBonus,
  Open, ReputationChallengeDatum, WithdrawChallenge,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_history_bonus_token_name,
}
use shared/identity_fixtures.{
  agent_id, record, record_input, record_input_at_other_script,
  record_input_with_fake_token, record_input_with_stake, retired, revoked,
  subagent, suspended, unaccountable, with_subagent_epoch,
}
use vector_identity/types.{IdentityDatum} as identity_types

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

/// The lower bound of the validity range of the test transactions.
const t0: Int = 1_700_000_000_000

const challenge_stake: Int = 25_000_000

fn range() -> ValidityRange {
  interval.between(t0, t0 + 600_000)
}

fn hour_range() -> ValidityRange {
  interval.between(t0, t0 + 3_600_000)
}

fn long_range() -> ValidityRange {
  interval.between(t0, t0 + 3_600_001)
}

fn open_range() -> ValidityRange {
  interval.after(t0)
}

fn agent_record() -> IdentityDatum {
  record(1, test_capabilities)
}

fn agent_ref(r: IdentityDatum) -> Input {
  record_input(test_agent_did, r)
}

fn parent_id() -> ByteArray {
  agent_id(9)
}

fn subagent_refs(parent: IdentityDatum) -> List<Input> {
  [
    agent_ref(subagent(agent_record(), parent_id(), 0, 1)),
    record_input(parent_id(), parent),
  ]
}

/// The CrossValidatorRefs record (holds a refs_token_policy token).
fn refs_ref_input() -> Input {
  make_input(
    test_tx_hash,
    400,
    make_output(
      key_address(test_owner_key_hash),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_refs_token_policy, "reputation_refs", 1),
      test_refs(),
    ),
  )
}

fn ap3x_to(key: ByteArray, amount: Int) -> Output {
  make_output_no_datum(
    key_address(key),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

// WithdrawChallenge

fn open_challenge() -> ReputationChallengeDatum {
  ReputationChallengeDatum {
    challenger_did: test_agent_did,
    challenger_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    target_credential: VerificationKey(test_owner_key_hash_2),
    challenged_capability: "code_review",
    stake_amount: challenge_stake,
    evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    evidence_uri: "ipfs://evidence",
    created_at: t0,
    counter_evidence_hash: "",
    counter_evidence_uri: "",
    response_submitted_at: 0,
    state: Open,
  }
}

fn challenge_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"7777777777777777777777777777777777777777777777777777777777777777",
    output_index: 0,
  }
}

fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

/// A challenge withdrawal by the challenger with the given identity
/// reference inputs, signers and validity range.
fn withdraw_challenge(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
) -> Bool {
  let challenge_input =
    Input {
      output_reference: challenge_ref(),
      output: make_output(
        script_address(test_endorsement_policy_id),
        assets.from_lovelace(2_000_000)
          |> assets.add(
              test_ap3x_policy_id,
              test_ap3x_asset_name,
              challenge_stake,
            )
          |> assets.add(test_endorsement_policy_id, challenge_token(), 1),
        Challenge(open_challenge()),
      ),
    }
  let tx =
    Transaction {
      ..base_tx(t0),
      validity_range,
      inputs: [challenge_input],
      reference_inputs: [make_params_ref_input(), refs_ref_input(), ..refs],
      outputs: [ap3x_to(test_owner_key_hash, challenge_stake)],
      mint: assets.from_asset(test_endorsement_policy_id, challenge_token(), -1),
      extra_signatories: signers,
    }
  endorsement.endorsement.spend(
    test_config(),
    Some(Challenge(open_challenge())),
    ChallengeSpend(WithdrawChallenge),
    challenge_ref(),
    tx,
  )
}

// MintHistoryBonus

fn bonus_source_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"8888888888888888888888888888888888888888888888888888888888888888",
    output_index: 1,
  }
}

fn bonus_token() -> ByteArray {
  derive_history_bonus_token_name(bonus_source_ref())
}

/// A history bonus mint (source ChallengeWon) for test_agent_did, signed by
/// the oracle and `signers`.
fn mint_bonus(
  refs: List<Input>,
  signers: List<ByteArray>,
  validity_range: ValidityRange,
) -> Bool {
  let bonus =
    HistoryBonusDatum {
      agent_did: test_agent_did,
      source: ChallengeWon,
      bonus_points: 3_000_000,
      source_ref: bonus_source_ref(),
      created_at: t0,
    }
  // The bonus is minted in a transaction that spends an output of the
  // endorsement validator (DistributeOutcome).
  let endorsement_input =
    make_input(
      test_tx_hash,
      7,
      make_output(
        script_address(test_endorsement_policy_id),
        assets.from_lovelace(2_000_000),
        Challenge(open_challenge()),
      ),
    )
  let tx =
    Transaction {
      ..base_tx(t0),
      validity_range,
      inputs: [endorsement_input],
      reference_inputs: [make_params_ref_input(), refs_ref_input(), ..refs],
      outputs: [
        make_output(
          script_address(test_reputation_policy_id),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_reputation_policy_id, bonus_token(), 1),
          bonus,
        ),
      ],
      mint: assets.from_asset(test_reputation_policy_id, bonus_token(), 1),
      extra_signatories: [test_oracle_credential, ..signers],
    }
  reputation.reputation.mint(
    test_config(),
    MintHistoryBonus,
    test_reputation_policy_id,
    tx,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawChallenge (an exit)
// ──────────────────────────────────────────────────────────────────────────────

test withdraw_challenge_active_agent() {
  withdraw_challenge(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_range_of_one_hour() {
  withdraw_challenge(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    hour_range(),
  )
}

test withdraw_challenge_suspended_agent() {
  withdraw_challenge(
    [agent_ref(suspended(agent_record(), t0 + 1))],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_unaccountable_agent() {
  withdraw_challenge(
    [agent_ref(unaccountable(agent_record()))],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_revoked_agent() {
  withdraw_challenge(
    [agent_ref(revoked(agent_record()))],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_retired_agent() {
  withdraw_challenge(
    [agent_ref(retired(agent_record()))],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_subagent_without_parent_record() {
  withdraw_challenge(
    [agent_ref(subagent(agent_record(), parent_id(), 0, 1))],
    [test_controller_hash(1)],
    range(),
  )
}

/// The challenger_credential signature alone is not enough.
test withdraw_challenge_without_controller_signature_fails() {
  !withdraw_challenge(
    [agent_ref(agent_record())],
    [test_owner_key_hash],
    range(),
  )
}

test withdraw_challenge_signed_by_target_controller_fails() {
  !withdraw_challenge(
    [agent_ref(agent_record()), record_input(test_agent_did_2, record(2, []))],
    [test_controller_hash(2)],
    range(),
  )
}

test withdraw_challenge_without_record_fails() {
  !withdraw_challenge([], [test_controller_hash(1)], range())
}

test withdraw_challenge_record_with_stake_credential_fails() {
  !withdraw_challenge(
    [record_input_with_stake(test_agent_did, agent_record())],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_record_at_other_script_fails() {
  !withdraw_challenge(
    [record_input_at_other_script(test_agent_did, agent_record())],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_record_with_fake_token_fails() {
  !withdraw_challenge(
    [record_input_with_fake_token(test_agent_did, agent_record())],
    [test_controller_hash(1)],
    range(),
  )
}

test withdraw_challenge_range_over_one_hour_fails() {
  !withdraw_challenge(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    long_range(),
  )
}

test withdraw_challenge_open_range_fails() {
  !withdraw_challenge(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    open_range(),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// MintHistoryBonus
// ──────────────────────────────────────────────────────────────────────────────

test mint_bonus_active_agent() {
  mint_bonus([agent_ref(agent_record())], [test_controller_hash(1)], range())
}

test mint_bonus_range_of_one_hour() {
  mint_bonus(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    hour_range(),
  )
}

test mint_bonus_suspended_agent_fails() {
  !mint_bonus(
    [agent_ref(suspended(agent_record(), t0 + 1))],
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_unaccountable_agent_fails() {
  !mint_bonus(
    [agent_ref(unaccountable(agent_record()))],
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_revoked_agent_fails() {
  !mint_bonus(
    [agent_ref(revoked(agent_record()))],
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_retired_agent_fails() {
  !mint_bonus(
    [agent_ref(retired(agent_record()))],
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_without_controller_signature_fails() {
  !mint_bonus([agent_ref(agent_record())], [], range())
}

test mint_bonus_signed_by_other_controller_fails() {
  !mint_bonus([agent_ref(agent_record())], [test_controller_hash(2)], range())
}

test mint_bonus_without_record_fails() {
  !mint_bonus([], [test_controller_hash(1)], range())
}

test mint_bonus_record_with_stake_credential_fails() {
  !mint_bonus(
    [record_input_with_stake(test_agent_did, agent_record())],
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_record_with_fake_token_fails() {
  !mint_bonus(
    [record_input_with_fake_token(test_agent_did, agent_record())],
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_range_over_one_hour_fails() {
  !mint_bonus(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    long_range(),
  )
}

test mint_bonus_open_range_fails() {
  !mint_bonus(
    [agent_ref(agent_record())],
    [test_controller_hash(1)],
    open_range(),
  )
}

test mint_bonus_subagent_active_parent() {
  mint_bonus(subagent_refs(record(9, [])), [test_controller_hash(1)], range())
}

test mint_bonus_subagent_after_parent_epoch_increase_fails() {
  !mint_bonus(
    subagent_refs(with_subagent_epoch(record(9, []), 1)),
    [test_controller_hash(1)],
    range(),
  )
}

test mint_bonus_subagent_without_parent_record_fails() fail {
  mint_bonus(
    [agent_ref(subagent(agent_record(), parent_id(), 0, 1))],
    [test_controller_hash(1)],
    range(),
  )
}
