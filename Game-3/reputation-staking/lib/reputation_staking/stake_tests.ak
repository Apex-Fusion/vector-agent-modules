/// Tests for stake validation logic.

use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Transaction}
use reputation_staking/stake_validation.{
  validate_create_stake, validate_decrease_stake, validate_increase_stake,
  validate_update_capabilities,
}
use reputation_staking/test_helpers.{
  base_tx, key_address, make_output, make_output_no_datum, make_params_ref_input,
  make_registry_ref_input,
  script_address, test_agent_did, test_ap3x_asset_name, test_ap3x_policy_id,
  test_config, test_owner_key_hash, test_params, test_refs,
  test_reputation_policy_id,
}
use reputation_staking/types.{StakeDatum}
use reputation_staking/utils.{derive_stake_token_name}

// ──────────────────────────────────────────────────────────────────────────────
// CreateStake tests
// ──────────────────────────────────────────────────────────────────────────────

test create_stake_happy_path() {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let current_slot = 1000

  let owner_cred = VerificationKey(test_owner_key_hash)
  let stake_amount = 10_000_000

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount,
      staked_capabilities: ["code_review", "testing"],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, stake_amount)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let registry_ref = make_registry_ref_input(test_agent_did, test_owner_key_hash)
  let params_ref = make_params_ref_input()

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [registry_ref, params_ref],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_create_stake(tx, cfg, refs, params, output_datum, output, current_slot)
}

test create_stake_below_minimum_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let current_slot = 1000

  let owner_cred = VerificationKey(test_owner_key_hash)
  // Below minimum (10_000_000)
  let stake_amount = 5_000_000

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount,
      staked_capabilities: ["code_review"],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, stake_amount)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let registry_ref = make_registry_ref_input(test_agent_did, test_owner_key_hash)
  let params_ref = make_params_ref_input()

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [registry_ref, params_ref],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  // Should return False (below minimum)
  validate_create_stake(tx, cfg, refs, params, output_datum, output, current_slot)
}

test create_stake_empty_capabilities_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let current_slot = 1000

  let owner_cred = VerificationKey(test_owner_key_hash)
  let stake_amount = 10_000_000

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount,
      staked_capabilities: [],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, stake_amount)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let registry_ref = make_registry_ref_input(test_agent_did, test_owner_key_hash)
  let params_ref = make_params_ref_input()

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [registry_ref, params_ref],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_create_stake(tx, cfg, refs, params, output_datum, output, current_slot)
}

// ──────────────────────────────────────────────────────────────────────────────
// IncreaseStake tests
// ──────────────────────────────────────────────────────────────────────────────

test increase_stake_happy_path() {
  let cfg = test_config()
  let refs = test_refs()
  let current_slot = 5000

  let owner_cred = VerificationKey(test_owner_key_hash)

  let input_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 10_000_000,
      staked_capabilities: ["code_review", "testing"],
      last_updated: 1000,
      history_points: 50,
    }

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review", "testing"],
      last_updated: current_slot,
      history_points: 50,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_increase_stake(tx, cfg, refs, input_datum, output_datum, output, current_slot)
}

test increase_stake_decrease_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let current_slot = 5000

  let owner_cred = VerificationKey(test_owner_key_hash)

  let input_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review"],
      last_updated: 1000,
      history_points: 0,
    }

  // Trying to decrease via IncreaseStake — should fail
  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 10_000_000,
      staked_capabilities: ["code_review"],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_increase_stake(tx, cfg, refs, input_datum, output_datum, output, current_slot)
}

// ──────────────────────────────────────────────────────────────────────────────
// DecreaseStake tests
// ──────────────────────────────────────────────────────────────────────────────

test decrease_stake_happy_path() {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  // Cooldown is 21_600 — set last_updated old enough
  let last_updated = 1000
  let current_slot = last_updated + 21_600 + 1

  let owner_cred = VerificationKey(test_owner_key_hash)
  let decrease_amount = 5_000_000

  let input_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review"],
      last_updated,
      history_points: 0,
    }

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 15_000_000,
      staked_capabilities: ["code_review"],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 15_000_000)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  // F8 fix: Owner must receive the withdrawn AP3X
  let owner_withdrawal_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, decrease_amount)
  let owner_withdrawal_output =
    make_output_no_datum(key_address(test_owner_key_hash), owner_withdrawal_value)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [],
            outputs: [output, owner_withdrawal_output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_decrease_stake(
    tx, cfg, refs, params, input_datum, output_datum, output, decrease_amount, current_slot,
  )
}

test decrease_stake_cooldown_not_met_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  // Cooldown NOT met
  let last_updated = 1000
  let current_slot = last_updated + 100

  let owner_cred = VerificationKey(test_owner_key_hash)

  let input_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 20_000_000,
      staked_capabilities: ["code_review"],
      last_updated,
      history_points: 0,
    }

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 15_000_000,
      staked_capabilities: ["code_review"],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 15_000_000)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_decrease_stake(
    tx, cfg, refs, params, input_datum, output_datum, output, 5_000_000, current_slot,
  )
}

test decrease_stake_below_minimum_fails() fail {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let last_updated = 1000
  let current_slot = last_updated + 21_600 + 1

  let owner_cred = VerificationKey(test_owner_key_hash)
  // Decrease so much that remaining < min_self_stake (10_000_000)
  let decrease_amount = 6_000_000

  let input_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 15_000_000,
      staked_capabilities: ["code_review"],
      last_updated,
      history_points: 0,
    }

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 9_000_000,
      staked_capabilities: ["code_review"],
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 9_000_000)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_decrease_stake(
    tx, cfg, refs, params, input_datum, output_datum, output, decrease_amount, current_slot,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// UpdateCapabilities tests
// ──────────────────────────────────────────────────────────────────────────────

test update_capabilities_happy_path() {
  let cfg = test_config()
  let refs = test_refs()
  let params = test_params()
  let last_updated = 1000
  let current_slot = last_updated + 21_600 + 1

  let owner_cred = VerificationKey(test_owner_key_hash)
  let new_caps = ["testing", "deployment"]

  let input_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 10_000_000,
      staked_capabilities: ["code_review", "testing"],
      last_updated,
      history_points: 0,
    }

  let output_datum =
    StakeDatum {
      agent_did: test_agent_did,
      owner_credential: owner_cred,
      stake_amount: 10_000_000,
      staked_capabilities: new_caps,
      last_updated: current_slot,
      history_points: 0,
    }

  let stake_token_name = derive_stake_token_name(test_agent_did)
  let output_value =
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
      |> assets.add(test_reputation_policy_id, stake_token_name, 1)

  let output =
    make_output(
      script_address(test_reputation_policy_id),
      output_value,
      output_datum,
    )

  let registry_ref = make_registry_ref_input(test_agent_did, test_owner_key_hash)

  let tx =
    base_tx(current_slot)
      |> fn(t) {
          Transaction {
            ..t,
            reference_inputs: [registry_ref],
            outputs: [output],
            extra_signatories: [test_owner_key_hash],
          }
        }

  validate_update_capabilities(
    tx, cfg, refs, params, input_datum, output_datum, output, new_caps, current_slot,
  )
}
