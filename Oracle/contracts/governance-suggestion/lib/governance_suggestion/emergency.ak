// Oracle: Governance Suggestion Engine — Emergency Proposal Validation
// Spec reference: §5.1b
//
// Emergency proposals require:
//   - Higher stake (MIN_PROPOSAL_STAKE × EMERGENCY_STAKE_MULTIPLIER)
//   - Restricted to ParameterChange or ProtocolUpgrade types only
//   - Fixed review window (EMERGENCY_REVIEW_WINDOW)
//   - Proposer reputation >= Established tier (100+ AP3X self-stake in Module 3)

use aiken/builtin
use aiken/collection/list
use cardano/address.{Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{Input}

use governance_suggestion/types.{
  GovernanceConfig, GovernanceParams, ParameterChange, ProposalDatum,
  ProtocolUpgrade,
}
use governance_suggestion/utils

/// Minimum self-stake amount for Established tier (100 AP3X in lovelace).
const established_tier_min_stake: Int = 100_000_000

/// Zeroed hash constant (28 bytes) — when Module 3 is not deployed,
/// reputation_validator_hash is set to all zeros. Script hashes are 28 bytes.
const zeroed_hash: ByteArray =
  #"00000000000000000000000000000000000000000000000000000000"

/// Validate that an emergency proposal meets all §5.1b requirements:
///   1. Proposal type is ParameterChange or ProtocolUpgrade
///   2. Stake >= MIN_PROPOSAL_STAKE × EMERGENCY_STAKE_MULTIPLIER
///   3. Review window == EMERGENCY_REVIEW_WINDOW
///   4. Proposer reputation >= Established tier (via Module 3 reference input)
pub fn validate_emergency_proposal(
  config: GovernanceConfig,
  params: GovernanceParams,
  datum: ProposalDatum,
  reference_inputs: List<Input>,
) -> Bool {
  let emergency_stake =
    params.min_proposal_stake * params.emergency_stake_multiplier / 1_000

  // Emergency proposals must be ParameterChange or ProtocolUpgrade
  let type_allowed =
    when datum.proposal_type is {
      ParameterChange { .. } -> True
      ProtocolUpgrade { .. } -> True
      _ -> False
    }

  // Review window must equal the emergency review window exactly
  let window_valid = datum.review_window == params.emergency_review_window

  // Proposer reputation >= Established tier
  let reputation_gate =
    verify_proposer_reputation(config, datum.proposer_did, reference_inputs)

  if !type_allowed {
    fail @"FAIL:emergency_type"
  } else if datum.stake_amount < emergency_stake {
    fail @"FAIL:emergency_stake"
  } else if !window_valid {
    fail @"FAIL:emergency_window"
  } else if !reputation_gate {
    fail @"FAIL:emergency_reputation"
  } else {
    True
  }
}

/// Verify that a proposer has at least Established tier reputation
/// by checking their Module 3 StakeUTXO self-stake amount via reference input.
///
/// If Module 3 is not deployed (reputation_validator_hash == zeroed), this is skipped
/// (emergency proposals allowed without reputation gate during bootstrap).
pub fn verify_proposer_reputation(
  config: GovernanceConfig,
  proposer_did: ByteArray,
  reference_inputs: List<Input>,
) -> Bool {
  // Skip if Module 3 not deployed
  if config.reputation_validator_hash == zeroed_hash {
    True
  } else {
    // Find proposer's StakeUTXO at reputation validator address
    // StakeUTXO is identified by stake token "rstk_" prefix under reputation policy
    list.any(
      reference_inputs,
      fn(input) {
        let is_at_reputation =
          when input.output.address.payment_credential is {
            ScriptCredential(hash) -> hash == config.reputation_validator_hash
            _ -> False
          }
        if is_at_reputation {
          // Check for stake token with "rstk_" prefix (Module 3 convention)
          let has_stake_token =
            list.any(
              assets.flatten(input.output.value),
              fn(entry) {
                let (pid, name, _qty) = entry
                pid == config.reputation_validator_hash && builtin.slice_bytearray(
                  0,
                  5,
                  name,
                ) == "rstk_"
              },
            )
          if has_stake_token {
            // The StakeUTXO's lovelace value represents the self-stake amount.
            // Established tier requires >= 100 AP3X (100_000_000 lovelace).
            utils.lovelace_of(input.output.value) >= established_tier_min_stake
          } else {
            False
          }
        } else {
          False
        }
      },
    )
  }
}
