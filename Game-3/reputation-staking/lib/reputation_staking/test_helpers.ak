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
use reputation_staking/utils.{AgentDatum}

// ──────────────────────────────────────────────────────────────────────────────
// Test constants
// ──────────────────────────────────────────────────────────────────────────────

pub const test_reputation_policy_id: ByteArray =
  #"aa00000000000000000000000000000000000000000000000000000000000001"

pub const test_endorsement_policy_id: ByteArray =
  #"bb00000000000000000000000000000000000000000000000000000000000002"

pub const test_refs_token_policy: ByteArray = #"cc"

pub const test_registry_policy_id: ByteArray =
  #"cc00000000000000000000000000000000000000000000000000000000000003"

pub const test_registry_script_hash: ByteArray =
  #"dd00000000000000000000000000000000000000000000000000000000000004"

pub const test_params_script_hash: ByteArray =
  #"ee00000000000000000000000000000000000000000000000000000000000005"

pub const test_treasury_script_hash: ByteArray =
  #"ff00000000000000000000000000000000000000000000000000000000000006"

pub const test_ap3x_policy_id: ByteArray =
  #"1100000000000000000000000000000000000000000000000000000000000007"

pub const test_ap3x_asset_name: ByteArray = "AP3X"

pub const test_oracle_credential: ByteArray =
  #"2200000000000000000000000000000000000000000000000000000000000008"

pub const test_agent_did: ByteArray = "agent_did_001"

pub const test_agent_did_2: ByteArray = "agent_did_002"

pub const test_owner_key_hash: ByteArray =
  #"aabb000000000000000000000000000000000000000000000000000000000009"

pub const test_owner_key_hash_2: ByteArray =
  #"aabb00000000000000000000000000000000000000000000000000000000000a"

pub const test_tx_hash: ByteArray =
  #"0000000000000000000000000000000000000000000000000000000000000000"

// ──────────────────────────────────────────────────────────────────────────────
// Config and params builders
// ──────────────────────────────────────────────────────────────────────────────

pub fn test_config() -> ReputationConfig {
  ReputationConfig {
    refs_token_policy: test_refs_token_policy,
    registry_policy_id: test_registry_policy_id,
    registry_script_hash: test_registry_script_hash,
    audit_claim_validator_hash: #"0000000000000000000000000000000000000000000000000000000000000000",
    params_script_hash: test_params_script_hash,
    treasury_script_hash: test_treasury_script_hash,
    ap3x_policy_id: test_ap3x_policy_id,
    ap3x_asset_name: test_ap3x_asset_name,
    oracle_credential: test_oracle_credential,
  }
}

pub fn test_refs() -> CrossValidatorRefs {
  CrossValidatorRefs {
    reputation_policy_id: test_reputation_policy_id,
    endorsement_policy_id: test_endorsement_policy_id,
  }
}

pub fn test_params() -> ProtocolParams {
  ProtocolParams {
    min_self_stake: 10_000_000,
    min_endorsement: 5_000_000,
    min_challenge_stake: 25_000_000,
    stake_cooldown: 21_600,
    endorsement_cooldown: 43_200,
    decay_rate: 100,
    activity_window: 180,
    decay_collector_fee: 500,
    history_multiplier: 1_000,
    max_endorsement_multiplier: 3,
    slash_rate_endorser: 5_000,
    protocol_fee_rate: 500,
    challenge_response_deadline: 10_800,
    min_agent_age: 21_600,
    escalation_window: 5_400,
    default_judgment_fee: 100,
    genesis_agent_cap: 100,
    genesis_bonus_amount: 100_000_000,
    genesis_minting_window: 604_800,
    genesis_protection_period: 129_600,
    valid_capabilities: ["code_review", "testing", "deployment"],
    epoch_length: 900,
  }
}

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
// Agent registry reference input builder
// ──────────────────────────────────────────────────────────────────────────────

pub fn make_registry_ref_input(
  agent_did: ByteArray,
  owner_key_hash: ByteArray,
) -> Input {
  let agent_datum =
    AgentDatum {
      owner: VerificationKey(owner_key_hash),
      name: "Test Agent",
      description: "A test agent",
      capabilities: ["code_review", "testing", "deployment"],
      framework: "aiken",
      endpoint: "https://example.com",
      registered_at: 0,
    }
  let value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_registry_policy_id, agent_did, 1)
  let output =
    make_output(script_address(test_registry_script_hash), value, agent_datum)
  make_input(test_tx_hash, 100, output)
}

// ──────────────────────────────────────────────────────────────────────────────
// Protocol params reference input builder
// ──────────────────────────────────────────────────────────────────────────────

pub fn make_params_ref_input() -> Input {
  let params = test_params()
  let value = assets.from_lovelace(2_000_000)
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
