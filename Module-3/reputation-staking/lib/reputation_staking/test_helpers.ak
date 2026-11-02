/// Shared test helpers for constructing mock data.

use aiken/collection/dict
use aiken/interval
use cardano/address.{Address, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, NoDatum, Output, OutputReference, Transaction,
}
use reputation_staking/config.{CrossValidatorRefs, ReputationConfig}
use reputation_staking/params.{ProtocolParams}
use shared/identity_fixtures
use shared/record_auth
use vector_identity/types as identity_types

// ──────────────────────────────────────────────────────────────────────────────
// Test constants
// ──────────────────────────────────────────────────────────────────────────────

pub const test_reputation_policy_id: ByteArray =
  #"aa00000000000000000000000000000000000000000000000000000000000001"

pub const test_endorsement_policy_id: ByteArray =
  #"bb00000000000000000000000000000000000000000000000000000000000002"

pub const test_refs_token_policy: ByteArray = #"cc"

// The identity registry hash of the tests is identity_fixtures.identity_hash.

pub const test_params_script_hash: ByteArray =
  #"ee00000000000000000000000000000000000000000000000000000000000005"

pub const test_treasury_script_hash: ByteArray =
  #"ff00000000000000000000000000000000000000000000000000000000000006"

pub const test_ap3x_policy_id: ByteArray =
  #"1100000000000000000000000000000000000000000000000000000000000007"

pub const test_ap3x_asset_name: ByteArray = "AP3X"

pub const test_oracle_credential: ByteArray =
  #"2200000000000000000000000000000000000000000000000000000000000008"

pub const test_params_token_policy: ByteArray =
  #"4400000000000000000000000000000000000000000000000000000044"

// Module 1 deployed script hashes (from vector-agent-modules repo)
pub const test_audit_claim_hash: ByteArray =
  #"6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda"

pub const test_audit_challenge_hash: ByteArray =
  #"781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc"

pub const test_audit_jury_pool_hash: ByteArray =
  #"b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962"

pub const test_audit_refs_token_policy: ByteArray =
  #"3300000000000000000000000000000000000000000000000000000000000010"

pub const test_agent_did: ByteArray = "agent_did_001"

pub const test_agent_did_2: ByteArray = "agent_did_002"

/// The owner key hash of test_agent_did: the hash of the listed transaction
/// key `identity_fixtures.ed_key(1)` of its test record (a declared
/// credential, not the controller key).
pub const test_owner_key_hash: ByteArray =
  #"e1f283c8b88cc8a7c3c2686da4a56240a12dfd92717298155572dc83"

/// The owner key hash of test_agent_did_2: the hash of the listed
/// transaction key `identity_fixtures.ed_key(2)` of its test record.
pub const test_owner_key_hash_2: ByteArray =
  #"80230b45a9b6111656d73e1219966373ea23254337a9eb492a3daa57"

pub const test_tx_hash: ByteArray =
  #"0000000000000000000000000000000000000000000000000000000000000000"

// ──────────────────────────────────────────────────────────────────────────────
// Config and params builders
// ──────────────────────────────────────────────────────────────────────────────

pub fn test_config() -> ReputationConfig {
  ReputationConfig {
    refs_token_policy: test_refs_token_policy,
    audit_claim_validator_hash: test_audit_claim_hash,
    audit_challenge_validator_hash: test_audit_challenge_hash,
    audit_jury_pool_hash: test_audit_jury_pool_hash,
    audit_refs_token_policy: test_audit_refs_token_policy,
    params_script_hash: test_params_script_hash,
    treasury_script_hash: test_treasury_script_hash,
    ap3x_policy_id: test_ap3x_policy_id,
    ap3x_asset_name: test_ap3x_asset_name,
    oracle_credential: test_oracle_credential,
    params_token_policy: test_params_token_policy,
    identity_hash: identity_fixtures.identity_hash,
  }
}

pub fn test_refs() -> CrossValidatorRefs {
  CrossValidatorRefs {
    reputation_policy_id: test_reputation_policy_id,
    endorsement_policy_id: test_endorsement_policy_id,
  }
}

/// Protocol parameters with the deployment values (all times in POSIX ms).
pub fn test_params() -> ProtocolParams {
  ProtocolParams {
    min_self_stake: 10_000_000,
    min_endorsement: 5_000_000,
    min_challenge_stake: 25_000_000,
    stake_cooldown: 86_400_000,
    endorsement_cooldown: 172_800_000,
    decay_rate: 100,
    activity_window: 180,
    decay_collector_fee: 500,
    history_multiplier: 1_000,
    max_endorsement_multiplier: 3,
    slash_rate_endorser: 5_000,
    protocol_fee_rate: 500,
    challenge_response_deadline: 43_200_000,
    min_agent_age: 86_400_000,
    escalation_window: 21_600_000,
    default_judgment_fee: 100,
    genesis_agent_cap: 100,
    genesis_bonus_amount: 100_000_000,
    genesis_minting_window: test_genesis_minting_end,
    genesis_protection_period: 5_184_000_000,
    valid_capabilities: ["code_review", "testing", "deployment"],
    epoch_length: 14_400_000,
  }
}

/// POSIX ms at which genesis minting closes in the test parameters
/// (2025-01-01T00:00:00Z).
pub const test_genesis_minting_end: Int = 1_735_689_600_000

// ──────────────────────────────────────────────────────────────────────────────
// Address builders
// ──────────────────────────────────────────────────────────────────────────────

pub fn script_address(hash: ByteArray) -> Address {
  Address { payment_credential: Script(hash), stake_credential: None }
}

pub fn key_address(hash: ByteArray) -> Address {
  Address {
    payment_credential: VerificationKey(hash),
    stake_credential: None,
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Output builders
// ──────────────────────────────────────────────────────────────────────────────

pub fn make_output(address: Address, value: assets.Value, datum: Data) -> Output {
  Output {
    address,
    value,
    datum: InlineDatum(datum),
    reference_script: None,
  }
}

pub fn make_output_no_datum(address: Address, value: assets.Value) -> Output {
  Output { address, value, datum: NoDatum, reference_script: None }
}

// ──────────────────────────────────────────────────────────────────────────────
// Input builders
// ──────────────────────────────────────────────────────────────────────────────

pub fn make_input(
  tx_hash: ByteArray,
  index: Int,
  output: Output,
) -> Input {
  Input {
    output_reference: OutputReference {
      transaction_id: tx_hash,
      output_index: index,
    },
    output,
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Identity registry reference input builders
// ──────────────────────────────────────────────────────────────────────────────

/// The capabilities of the test identity records.
pub const test_capabilities: List<ByteArray> =
  ["code_review", "testing", "deployment"]

/// The key hash of controller key `n` of the identity fixtures. The record of
/// test_agent_did uses controller 1, the record of test_agent_did_2 uses
/// controller 2.
pub fn test_controller_hash(n: Int) -> ByteArray {
  identity_fixtures.controller_hash(n)
}

/// The authentic identity record of `agent_did` (Active, controller key `n`,
/// the test capabilities, created_at 0) as a reference input. It lists
/// `identity_fixtures.ed_key(n)` with the transaction purpose, so the owner
/// key of the agent (test_owner_key_hash for n = 1, test_owner_key_hash_2
/// for n = 2) is a declared credential.
pub fn make_identity_ref_input(agent_did: ByteArray, n: Int) -> Input {
  identity_fixtures.record_input(agent_did, test_record(n))
}

/// The Active test record with controller key `n`, the test capabilities and
/// the listed transaction key `identity_fixtures.ed_key(n)`.
pub fn test_record(n: Int) -> identity_types.IdentityDatum {
  identity_fixtures.with_transaction_key(
    identity_fixtures.record(n, test_capabilities),
    n,
  )
}

test owner_key_hashes_are_listed_transaction_keys() {
  and {
    test_owner_key_hash == identity_fixtures.ed_key_hash(1),
    test_owner_key_hash_2 == identity_fixtures.ed_key_hash(2),
  }
}

/// The signatures of the owner key and of the record controller of
/// test_agent_did.
pub fn agent_signers() -> List<ByteArray> {
  [test_owner_key_hash, test_controller_hash(1)]
}

/// The signatures of the owner key and of the record controller of
/// test_agent_did_2.
pub fn agent_2_signers() -> List<ByteArray> {
  [test_owner_key_hash_2, test_controller_hash(2)]
}

// ──────────────────────────────────────────────────────────────────────────────
// Protocol params reference input builder
// ──────────────────────────────────────────────────────────────────────────────

/// The ProtocolParams record: at the params holder, holding the `params`
/// record token.
pub fn make_params_ref_input() -> Input {
  let params = test_params()
  let value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_params_token_policy, record_auth.params_token_name, 1)
  let output =
    make_output(script_address(test_params_script_hash), value, params)
  make_input(test_tx_hash, 200, output)
}

// ──────────────────────────────────────────────────────────────────────────────
// Base transaction builder
// ──────────────────────────────────────────────────────────────────────────────

pub fn base_tx(current_slot: Int) -> Transaction {
  Transaction {
    inputs: [],
    reference_inputs: [],
    outputs: [],
    fee: 200_000,
    mint: assets.zero,
    certificates: [],
    withdrawals: [],
    validity_range: interval.between(current_slot, current_slot + 100),
    extra_signatories: [],
    redeemers: [],
    datums: dict.empty,
    id: test_tx_hash,
    votes: [],
    proposal_procedures: [],
    current_treasury_amount: None,
    treasury_donation: None,
  }
}
