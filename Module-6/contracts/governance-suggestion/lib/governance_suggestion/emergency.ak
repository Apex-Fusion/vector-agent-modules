// Module 6: Self-Improvement Module — Emergency Proposal Validation
// Spec reference: §5.1b
//
// Emergency proposals require:
//   - Higher stake (MIN_PROPOSAL_STAKE × EMERGENCY_STAKE_MULTIPLIER)
//   - Restricted to ParameterChange or ProtocolUpgrade types only
//   - Fixed review window (EMERGENCY_REVIEW_WINDOW)
//   - Proposer reputation >= Established tier (100+ AP3X self-stake of the
//     proposer in Module 3)

use aiken/builtin
use aiken/collection/list
use cardano/address.{Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{InlineDatum, Input}
use governance_suggestion/types.{
  GovernanceConfig, GovernanceParams, ParameterChange, ProposalDatum,
  ProtocolUpgrade,
}
use governance_suggestion/utils
use shared/token_naming

/// Minimum self-stake amount for Established tier (100 AP3X in lovelace).
const established_tier_min_stake: Int = 100_000_000

/// Zeroed hash constant (28 bytes) — when Module 3 is not deployed,
/// reputation_validator_hash is set to all zeros. Script hashes are 28 bytes.
const zeroed_hash: ByteArray =
  #"00000000000000000000000000000000000000000000000000000000"

/// Prefix of the Module 3 stake token: "rstk_" ++ blake2b_256(agent_did)[0..27]
/// (Module 3 utils.derive_stake_token_name).
const stake_token_prefix: ByteArray = "rstk_"

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

/// Verify that the proposer has at least Established tier reputation: its
/// own Module 3 stake position is a reference input.
///
/// MUST (when Module 3 is deployed): a reference input
///   1. is at the Module 3 reputation validator (payment credential
///      Script(reputation_validator_hash)),
///   2. holds exactly one stake token of the proposer: the asset
///      "rstk_" ++ blake2b_256(proposer_did)[0..27] of the policy
///      reputation_validator_hash, quantity 1,
///   3. has an inline datum whose first field (StakeDatum.agent_did) is
///      `proposer_did`,
///   4. holds at least 100 AP3X.
/// The stake of another agent never counts.
///
/// If Module 3 is not deployed (reputation_validator_hash == zeroed), this is
/// skipped (emergency proposals allowed without reputation gate during
/// bootstrap).
pub fn verify_proposer_reputation(
  config: GovernanceConfig,
  proposer_did: ByteArray,
  reference_inputs: List<Input>,
) -> Bool {
  // Skip if Module 3 not deployed
  if config.reputation_validator_hash == zeroed_hash {
    True
  } else {
    let stake_token =
      token_naming.derive_token_name(stake_token_prefix, proposer_did, 27)
    list.any(
      reference_inputs,
      fn(input) {
        when input.output.address.payment_credential is {
          ScriptCredential(hash) ->
            and {
              hash == config.reputation_validator_hash,
              assets.quantity_of(
                input.output.value,
                config.reputation_validator_hash,
                stake_token,
              ) == 1,
              when input.output.datum is {
                InlineDatum(raw) ->
                  builtin.un_b_data(
                    builtin.head_list(builtin.snd_pair(builtin.un_constr_data(raw))),
                  ) == proposer_did
                _ -> False
              },
              utils.lovelace_of(input.output.value) >= established_tier_min_stake,
            }
          _ -> False
        }
      },
    )
  }
}
