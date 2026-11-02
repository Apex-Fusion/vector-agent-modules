//// The bound stake of an endorsement (W4-MODFIX2 item 1), library level.
////
//// An endorsement backs one stake of the target: the stake whose stake_id
//// is the endorsement's target_stake. CreateStake sets stake_id to the
//// output reference of the seed input, and every continuing stake keeps it,
//// so at most one output carries a stake_id with the stake token.
////
//// - MintEndorsementToken reads the endorsed capabilities from the bound
////   stake.
//// - WithdrawEndorsement needs the bound stake as a reference input without
////   challenge locks. A challenge against the stake keeps its lock in the
////   stake from its opening to its closing, so the withdrawal waits for the
////   challenge. Before, the withdrawal looked for open challenges only in the
////   reference inputs that the transaction names.
////
//// SlashEndorsement (validator level) is in
//// validators/endorsement_slash_tests.ak.

use aiken/interval
use cardano/address.{Address, Inline, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use reputation_staking/endorsement_validation.{
  validate_mint_endorsement, validate_withdraw_endorsement,
}
use reputation_staking/stake_validation.{validate_create_stake}
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_identity_ref_input, make_input,
  make_output, make_output_no_datum, script_address, stake_ref_input,
  test_agent_did, test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id,
  test_config, test_endorsement_policy_id, test_other_stake_id,
  test_owner_key_hash, test_params, test_refs, test_reputation_policy_id,
  test_stake_id, test_tx_hash,
}
use reputation_staking/types.{
  Challenge, EndorsementDatum, Open, ReputationChallengeDatum, StakeDatum,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
  derive_stake_token_name,
}

const t0: Int = 1_700_000_000_000

fn range() -> transaction.ValidityRange {
  interval.between(t0, t0 + 600_000)
}

/// The lock of a challenge of agent 1 against agent 2.
fn lock() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

/// An endorsement of agent 2 by agent 1 that backs the stake test_stake_id.
fn endorsement(capabilities: List<ByteArray>, created_at: Int) -> EndorsementDatum {
  EndorsementDatum {
    endorser_did: test_agent_did,
    endorser_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    stake_amount: 5_000_000,
    endorsed_capabilities: capabilities,
    created_at,
    slashed_by: [],
    target_stake: test_stake_id,
  }
}

fn ap3x_to(key: ByteArray, amount: Int) -> Output {
  make_output_no_datum(
    key_address(key),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

/// The stake of agent 2 with `stake_id`, `capabilities` and `locks` at
/// `address`, holding `value`, as an input with output index `index`.
fn stake_at(
  address: Address,
  value: assets.Value,
  stake_id: OutputReference,
  capabilities: List<ByteArray>,
  locks: List<ByteArray>,
  index: Int,
) -> Input {
  make_input(
    test_tx_hash,
    index,
    make_output(
      address,
      value,
      StakeDatum {
        agent_did: test_agent_did_2,
        owner_credential: VerificationKey(test_owner_key_hash),
        stake_amount: 20_000_000,
        staked_capabilities: capabilities,
        last_updated: 0,
        history_points: 0,
        challenge_locks: locks,
        stake_id,
      },
    ),
  )
}

/// The stake value of agent 2: 20 AP3X and its stake token.
fn stake_value() -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000)
    |> assets.add(test_reputation_policy_id, derive_stake_token_name(test_agent_did_2), 1)
}

/// Another stake of agent 2 (another stake_id) without challenge locks.
fn other_stake() -> Input {
  stake_at(
    script_address(test_reputation_policy_id),
    stake_value(),
    test_other_stake_id,
    ["code_review", "testing"],
    [],
    61,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawEndorsement
// ──────────────────────────────────────────────────────────────────────────────

/// WithdrawEndorsement of the endorsement after its cooldown, with the
/// endorser's record and `stakes` (and other outputs) as reference inputs.
fn withdraw_with(stakes: List<Input>) -> Bool {
  let datum = endorsement(["code_review"], t0 - 172_800_000)
  validate_withdraw_endorsement(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      reference_inputs: [make_identity_ref_input(test_agent_did, 1), ..stakes],
      outputs: [ap3x_to(test_owner_key_hash, 5_000_000)],
      mint: assets.from_asset(
        test_endorsement_policy_id,
        derive_endorsement_token_name(test_agent_did, test_agent_did_2),
        -1,
      ),
      extra_signatories: agent_signers(),
    },
    test_config(),
    test_refs(),
    test_params(),
    datum,
    t0,
  )
}

test withdraw_endorsement_with_unlocked_bound_stake() {
  withdraw_with([stake_ref_input(test_agent_did_2, test_stake_id, [])])
}

/// A challenge locks the bound stake. The transaction names no challenge
/// output, so the old rule found no open challenge.
test withdraw_endorsement_with_locked_bound_stake_fails() {
  !withdraw_with([stake_ref_input(test_agent_did_2, test_stake_id, [lock()])])
}

/// A locked bound stake also blocks the withdrawal when the transaction
/// names another stake of the target without locks.
test withdraw_endorsement_with_locked_bound_stake_and_other_stake_fails() {
  !withdraw_with(
    [other_stake(), stake_ref_input(test_agent_did_2, test_stake_id, [lock()])],
  )
}

/// Another stake of the target without locks does not replace the bound
/// stake.
test withdraw_endorsement_with_other_stake_only_fails() fail {
  withdraw_with([other_stake()])
}

test withdraw_endorsement_without_bound_stake_fails() fail {
  withdraw_with([])
}

/// The bound stake must be at the exact reputation address (no stake
/// credential).
test withdraw_endorsement_with_bound_stake_at_other_address_fails() fail {
  withdraw_with(
    [
      stake_at(
        Address {
          payment_credential: Script(test_reputation_policy_id),
          stake_credential: Some(Inline(VerificationKey(test_owner_key_hash))),
        },
        stake_value(),
        test_stake_id,
        ["code_review"],
        [],
        62,
      ),
    ],
  )
}

/// A stake datum without the stake token is not the bound stake.
test withdraw_endorsement_with_bound_stake_without_token_fails() fail {
  withdraw_with(
    [
      stake_at(
        script_address(test_reputation_policy_id),
        assets.from_lovelace(2_000_000)
          |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000),
        test_stake_id,
        ["code_review"],
        [],
        63,
      ),
    ],
  )
}

/// The old rule scanned the challenge outputs in the reference inputs. The
/// new rule reads the bound stake: an open challenge output against the
/// target does not block the withdrawal when it does not lock the bound
/// stake (it challenged another stake of the target).
test withdraw_endorsement_ignores_challenge_of_other_stake() {
  let challenge =
    make_input(
      test_tx_hash,
      70,
      make_output(
        script_address(test_endorsement_policy_id),
        assets.from_lovelace(2_000_000)
          |> assets.add(test_endorsement_policy_id, lock(), 1),
        Challenge(
          ReputationChallengeDatum {
            challenger_did: test_agent_did,
            challenger_credential: VerificationKey(test_owner_key_hash),
            target_did: test_agent_did_2,
            target_credential: VerificationKey(test_owner_key_hash),
            challenged_capability: "code_review",
            stake_amount: 25_000_000,
            evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            evidence_uri: "ipfs://evidence",
            created_at: t0 - 1_000,
            counter_evidence_hash: "",
            counter_evidence_uri: "",
            response_submitted_at: 0,
            state: Open,
          },
        ),
      ),
    )
  withdraw_with([challenge, stake_ref_input(test_agent_did_2, test_stake_id, [])])
}

// ──────────────────────────────────────────────────────────────────────────────
// MintEndorsementToken reads the bound stake
// ──────────────────────────────────────────────────────────────────────────────

/// MintEndorsementToken of an endorsement of `capabilities` with `stakes`
/// as reference inputs (besides both identity records).
fn endorse_with(capabilities: List<ByteArray>, stakes: List<Input>) -> Bool {
  let datum = endorsement(capabilities, t0)
  let token = derive_endorsement_token_name(test_agent_did, test_agent_did_2)
  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 5_000_000)
        |> assets.add(test_endorsement_policy_id, token, 1),
      datum,
    )
  validate_mint_endorsement(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      reference_inputs: [
        make_identity_ref_input(test_agent_did, 1),
        make_identity_ref_input(test_agent_did_2, 2),
        ..stakes
      ],
      outputs: [output],
      mint: assets.from_asset(test_endorsement_policy_id, token, 1),
      extra_signatories: agent_signers(),
    },
    test_config(),
    test_refs(),
    test_params(),
    datum,
    output,
  )
}

/// The bound stake of agent 2 that stakes only "testing".
fn bound_testing_stake() -> Input {
  stake_at(
    script_address(test_reputation_policy_id),
    stake_value(),
    test_stake_id,
    ["testing"],
    [],
    64,
  )
}

test mint_endorsement_reads_bound_stake() {
  endorse_with(["testing"], [other_stake(), bound_testing_stake()])
}

/// The capability is staked by another stake of the target, not by the
/// bound stake.
test mint_endorsement_with_capability_of_other_stake_fails() {
  !endorse_with(["code_review"], [other_stake(), bound_testing_stake()])
}

test mint_endorsement_without_bound_stake_fails() fail {
  endorse_with(["code_review"], [other_stake()])
}

// ──────────────────────────────────────────────────────────────────────────────
// CreateStake sets stake_id
// ──────────────────────────────────────────────────────────────────────────────

/// CreateStake of agent 1 from the seed at (test_tx_hash, 9) with the new
/// stake_id `stake_id`.
fn create_with_stake_id(stake_id: OutputReference) -> Bool {
  let datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: VerificationKey(test_owner_key_hash),
      stake_amount: 10_000_000,
      staked_capabilities: ["code_review"],
      last_updated: t0 + 600_000,
      history_points: 0,
      challenge_locks: [],
      stake_id,
    }
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
        |> assets.add(
            test_reputation_policy_id,
            derive_stake_token_name(test_agent_did),
            1,
          ),
      datum,
    )
  let seed =
    make_input(
      test_tx_hash,
      9,
      make_output(
        script_address(test_reputation_policy_id),
        assets.from_lovelace(2_000_000),
        datum,
      ),
    )
  validate_create_stake(
    Transaction {
      ..base_tx(t0),
      validity_range: range(),
      inputs: [seed],
      reference_inputs: [make_identity_ref_input(test_agent_did, 1)],
      outputs: [output],
      mint: assets.from_asset(
        test_reputation_policy_id,
        derive_stake_token_name(test_agent_did),
        1,
      ),
      extra_signatories: agent_signers(),
    },
    test_config(),
    test_refs(),
    test_params(),
    seed,
    datum,
    output,
  )
}

test create_stake_sets_stake_id_to_seed() {
  create_with_stake_id(
    OutputReference { transaction_id: test_tx_hash, output_index: 9 },
  )
}

/// A new stake cannot take the stake_id of another stake.
test create_stake_with_stake_id_of_other_stake_fails() {
  !create_with_stake_id(test_stake_id)
}
