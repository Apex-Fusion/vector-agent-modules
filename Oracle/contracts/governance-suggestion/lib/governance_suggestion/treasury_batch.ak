// Oracle: Governance Suggestion Engine — Treasury Batch Management
// Spec reference: §9.4, §11.7

use aiken/collection/list
use cardano/address.{Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output}

use governance_suggestion/types.{GovernanceConfig, TreasuryBatchDatum}

// ============================================================================
// Treasury Batch Validation
// ============================================================================

/// Validate that a treasury batch has sufficient funds for the reward.
/// Used during AdoptProposal to verify the reward source.
pub fn validate_treasury_batch(
  config: GovernanceConfig,
  batch_input: Input,
  reward_amount: Int,
  outputs: List<Output>,
) -> Bool {
  // Verify batch is at treasury address
  let is_at_treasury =
    when batch_input.output.address.payment_credential is {
      ScriptCredential(hash) -> hash == config.governance_treasury_hash
      _ -> False
    }

  // Parse batch datum
  expect InlineDatum(raw) = batch_input.output.datum
  expect batch: TreasuryBatchDatum = raw

  // Verify batch is active
  let is_active = batch.active

  // Verify batch has enough AP3X (native lovelace)
  let batch_lovelace = assets.lovelace_of(batch_input.output.value)
  let has_funds = batch_lovelace >= reward_amount

  // If batch has remainder after reward, verify change UTXO goes back to treasury
  let remainder = batch_lovelace - reward_amount
  let remainder_valid =
    if remainder > 0 {
      list.any(
        outputs,
        fn(output) {
          when output.address.payment_credential is {
            ScriptCredential(hash) -> hash == config.governance_treasury_hash
            _ -> False
          }
        },
      )
    } else {
      True
    }

  is_at_treasury && is_active && has_funds && remainder_valid
}

/// Find a treasury batch UTXO in the transaction inputs.
pub fn find_treasury_batch(
  config: GovernanceConfig,
  inputs: List<Input>,
) -> Option<Input> {
  list.find(
    inputs,
    fn(input) {
      when input.output.address.payment_credential is {
        ScriptCredential(hash) -> hash == config.governance_treasury_hash
        _ -> False
      }
    },
  )
}
