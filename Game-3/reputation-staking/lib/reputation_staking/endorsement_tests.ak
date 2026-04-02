/// Tests for endorsement validation logic.

use cardano/address.{VerificationKey}
use cardano/assets
use reputation_staking/endorsement_validation.{
  validate_increase_endorsement, validate_mint_endorsement,
  validate_withdraw_endorsement,
}
use reputation_staking/test_helpers.{
  base_tx, make_input, make_output,
  make_registry_ref_input, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_config,
  test_endorsement_policy_id, test_owner_key_hash, test_owner_key_hash_2,
  test_params, test_refs, test_reputation_policy_id, test_tx_hash,
}
use reputation_staking/types.{EndorsementDatum, StakeDatum}
use reputation_staking/utils.{
  derive_endorsement_token_name, derive_stake_token_name,
}
use cardano/transaction.{Input, Transaction}

// ──────────────────────────────────────────────────────────────────────────────
// Helper: build a target's StakeUTXO reference input
// ──────────────────────────────────────────────────────────────────────────────

fn make_target_stake_ref(target_did: ByteArray, target_key: ByteArray) -> Input {
  let stake_datum =
    StakeDatum {
      agent_did: target_did,
      owner_credential: VerificationKey(target_key),
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review", "testing", "deployment"],
      last_updated: 500,
      history_points: 0,
    }
  let stake_token = derive_stake_token_name(target_did)
  let value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000)
      |> assets.add(test_reputation_policy_id, stake_token, 1)
  let output =
    make_output(
      script_address(test_reputation_policy_id),
      value,
      stake_datum,
    )
  make_input(test_tx_hash, 50, output)
}

// ──────────────────────────────────────────────────────────────────────────────
// MintEndorsementToken tests
// ──────────────────────────────────────────────────────────────────────────────

test mint_endorsement_happy_path() {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let current_slot = 2000

  let endorser_cred = VerificationKey(test_owner_key_hash)
  let endorsement_amount = 5_000_000

  let output_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did_2,
      stake_amount: endorsement_amount,
      endorsed_capabilities: ["code_review", "testing"],
      created_at: current_slot,
    }

  let token_name =
    derive_endorsement_token_name(test_agent_did, test_agent_did_2)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, endorsement_amount)
      |> assets.add(test_endorsement_policy_id, token_name, 1)

  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      output_value,
      output_datum,
    )

  // Reference inputs: both agents in registry + target's stake
  let endorser_reg = make_registry_ref_input(test_agent_did, test_owner_key_hash)
  let target_reg = make_registry_ref_input(test_agent_did_2, test_owner_key_hash_2)
  let target_stake = make_target_stake_ref(test_agent_did_2, test_owner_key_hash_2)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [endorser_reg, target_reg, target_stake],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_mint_endorsement(tx, cfg, refs, params, output_datum, output)
}

test mint_endorsement_self_endorse_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let current_slot = 2000

  let endorser_cred = VerificationKey(test_owner_key_hash)

  // Self-endorsement: endorser_did == target_did
  let output_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did,
      stake_amount: 5_000_000,
      endorsed_capabilities: ["code_review"],
      created_at: current_slot,
    }

  let token_name =
    derive_endorsement_token_name(test_agent_did, test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 5_000_000)
      |> assets.add(test_endorsement_policy_id, token_name, 1)

  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      output_value,
      output_datum,
    )

  let agent_reg = make_registry_ref_input(test_agent_did, test_owner_key_hash)
  let agent_stake = make_target_stake_ref(test_agent_did, test_owner_key_hash)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [agent_reg, agent_stake],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_mint_endorsement(tx, cfg, refs, params, output_datum, output)
}

test mint_endorsement_below_minimum_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let current_slot = 2000

  let endorser_cred = VerificationKey(test_owner_key_hash)
  // Below minimum endorsement (5_000_000)
  let endorsement_amount = 1_000_000

  let output_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did_2,
      stake_amount: endorsement_amount,
      endorsed_capabilities: ["code_review"],
      created_at: current_slot,
    }

  let token_name =
    derive_endorsement_token_name(test_agent_did, test_agent_did_2)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, endorsement_amount)
      |> assets.add(test_endorsement_policy_id, token_name, 1)

  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      output_value,
      output_datum,
    )

  let endorser_reg = make_registry_ref_input(test_agent_did, test_owner_key_hash)
  let target_reg = make_registry_ref_input(test_agent_did_2, test_owner_key_hash_2)
  let target_stake = make_target_stake_ref(test_agent_did_2, test_owner_key_hash_2)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [endorser_reg, target_reg, target_stake],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_mint_endorsement(tx, cfg, refs, params, output_datum, output)
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseEndorsement tests
// ──────────────────────────────────────────────────────────────────────────────

test increase_endorsement_happy_path() {
  let cfg = test_config()
  let refs = test_refs()

  let endorser_cred = VerificationKey(test_owner_key_hash)

  let input_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did_2,
      stake_amount: 5_000_000,
      endorsed_capabilities: ["code_review"],
      created_at: 1000,
    }

  let output_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did_2,
      stake_amount: 10_000_000,
      endorsed_capabilities: ["code_review"],
      created_at: 1000,
    }

  let token_name =
    derive_endorsement_token_name(test_agent_did, test_agent_did_2)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
      |> assets.add(test_endorsement_policy_id, token_name, 1)

  let output =
    make_output(
      script_address(test_endorsement_policy_id),
      output_value,
      output_datum,
    )

  let tx =
    base_tx(5000)
      |> fn(t) {
          Transaction {
            ..t,
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_increase_endorsement(tx, cfg, refs, input_datum, output_datum, output)
}

// ──────────────────────────────────────────────────────────────────────────────
// WithdrawEndorsement tests
// ──────────────────────────────────────────────────────────────────────────────

test withdraw_endorsement_happy_path() {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let created_at = 1000
  // After cooldown (43_200 slots)
  let current_slot = created_at + 43_200 + 1

  let endorser_cred = VerificationKey(test_owner_key_hash)

  let input_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did_2,
      stake_amount: 5_000_000,
      endorsed_capabilities: ["code_review"],
      created_at,
    }

  let token_name =
    derive_endorsement_token_name(test_agent_did, test_agent_did_2)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [],
            mint: assets.from_lovelace(0)
              |> assets.add(test_endorsement_policy_id, token_name, -1),
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_withdraw_endorsement(tx, cfg, refs, params, input_datum, current_slot)
}

test withdraw_endorsement_too_early_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let created_at = 1000
  // Before cooldown
  let current_slot = created_at + 100

  let endorser_cred = VerificationKey(test_owner_key_hash)

  let input_datum =
    EndorsementDatum {
      endorser_did: test_agent_did,
      endorser_credential: endorser_cred,
      target_did: test_agent_did_2,
      stake_amount: 5_000_000,
      endorsed_capabilities: ["code_review"],
      created_at,
    }

  let token_name =
    derive_endorsement_token_name(test_agent_did, test_agent_did_2)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            mint: assets.from_lovelace(0)
              |> assets.add(test_endorsement_policy_id, token_name, -1),
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_withdraw_endorsement(tx, cfg, refs, params, input_datum, current_slot)
}
