// Game 6: Governance Suggestion Engine — Proposer Activity Tracking
// Rate limiting and cooldown enforcement via ProposerActivity UTXO

use aiken/builtin
use aiken/collection/list
use aiken/crypto.{blake2b_256}
use cardano/address.{Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output}

use governance_suggestion/types.{
  GovernanceConfig, GovernanceCrossRefs, GovernanceParams, ProposerActivityDatum,
}

// ============================================================================
// Rate Limiting
// ============================================================================

/// Validate rate limiting for proposal submission.
/// Consumes the ProposerActivity UTXO to verify count and cooldown.
/// Returns True if the agent is allowed to submit a new proposal.
pub fn validate_submit_rate_limit(
  params: GovernanceParams,
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  agent_did: ByteArray,
  current_slot: Int,
  inputs: List<Input>,
  outputs: List<Output>,
) -> Bool {
  let activity_token = activity_token_name_for(config, agent_did)

  // Try to find existing ProposerActivity UTXO in inputs
  let maybe_activity =
    list.find(
      inputs,
      fn(input) {
        let is_at_proposal_validator =
          when input.output.address.payment_credential is {
            ScriptCredential(hash) -> hash == refs.proposal_validator_hash
            _ -> False
          }
        let has_activity_token =
          assets.quantity_of(
            input.output.value,
            refs.proposal_mint_hash,
            activity_token,
          ) == 1
        is_at_proposal_validator && has_activity_token
      },
    )

  when maybe_activity is {
    Some(activity_input) -> {
      // Existing activity — verify cooldown and count
      expect InlineDatum(raw) = activity_input.output.datum
      expect activity: ProposerActivityDatum = raw

      // Cooldown: enough time since last proposal
      let cooldown_met =
        current_slot >= activity.last_proposal_slot + params.proposal_cooldown
      // Count: not at maximum
      let under_limit =
        activity.active_proposal_count < params.max_active_proposals

      // Verify continuing output has incremented count and updated slot
      // Guard: check for activity token before parsing datum to avoid
      // hard crash from unIData on ProposalDatum outputs.
      let valid_output =
        list.any(
          outputs,
          fn(output) {
            let has_token =
              assets.quantity_of(
                output.value,
                refs.proposal_mint_hash,
                activity_token,
              ) == 1
            if has_token {
              when output.datum is {
                InlineDatum(out_raw) -> {
                  expect out_activity: ProposerActivityDatum = out_raw
                  out_activity.agent_did == agent_did && out_activity.active_proposal_count == activity.active_proposal_count + 1 && out_activity.last_proposal_slot == current_slot
                }
                _ -> False
              }
            } else {
              False
            }
          },
        )

      cooldown_met && under_limit && valid_output
    }
    None -> {
      // First proposal ever — InitActivity: create new tracking UTXO
      // Count outputs with the activity token to distinguish "not found" from "found but wrong"
      let token_count =
        list.foldl(
          outputs,
          0,
          fn(output, acc) {
            if
              assets.quantity_of(
                output.value,
                refs.proposal_mint_hash,
                activity_token,
              ) == 1
            {
              acc + 1
            } else {
              acc
            }
          },
        )
      if token_count == 0 {
        fail @"RATE:activity_token_not_in_any_output"
      } else {
        // Token found, now check datum
        let found =
          list.any(
            outputs,
            fn(output) {
              let has_activity_token =
                assets.quantity_of(
                  output.value,
                  refs.proposal_mint_hash,
                  activity_token,
                ) == 1
              if has_activity_token {
                when output.datum is {
                  InlineDatum(out_raw) -> {
                    expect out_activity: ProposerActivityDatum = out_raw
                    let did_ok = out_activity.agent_did == agent_did
                    let count_ok = out_activity.active_proposal_count == 1
                    let slot_ok = out_activity.last_proposal_slot == current_slot
                    if !did_ok {
                      fail @"RATE:did_mismatch"
                    } else if !count_ok {
                      fail @"RATE:count_not_1"
                    } else if !slot_ok {
                      fail @"RATE:slot_mismatch"
                    } else {
                      True
                    }
                  }
                  _ -> fail @"RATE:no_inline_datum"
                }
              } else {
                False
              }
            },
          )
        if found {
          True
        } else {
          fail @"RATE:datum_check_failed_but_token_found"
        }
      }
    }
  }
}

/// Validate activity decrement on proposal finalization (adopt, reject, expire, withdraw).
/// The ProposerActivity UTXO must be consumed and re-created with count -= 1.
pub fn validate_finalize_activity(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  agent_did: ByteArray,
  inputs: List<Input>,
  outputs: List<Output>,
) -> Bool {
  let activity_token = activity_token_name_for(config, agent_did)

  // Find ProposerActivity UTXO in inputs
  expect Some(activity_input) =
    list.find(
      inputs,
      fn(input) {
        let is_at_proposal_validator =
          when input.output.address.payment_credential is {
            ScriptCredential(hash) -> hash == refs.proposal_validator_hash
            _ -> False
          }
        let has_activity_token =
          assets.quantity_of(
            input.output.value,
            refs.proposal_mint_hash,
            activity_token,
          ) == 1
        is_at_proposal_validator && has_activity_token
      },
    )

  expect InlineDatum(raw) = activity_input.output.datum
  expect activity: ProposerActivityDatum = raw

  // Verify continuing output has decremented count
  // Guard: check for activity token before parsing datum.
  list.any(
    outputs,
    fn(output) {
      let has_token =
        assets.quantity_of(
          output.value,
          refs.proposal_mint_hash,
          activity_token,
        ) == 1
      if has_token {
        when output.datum is {
          InlineDatum(out_raw) -> {
            expect out_activity: ProposerActivityDatum = out_raw
            out_activity.agent_did == agent_did && out_activity.active_proposal_count == activity.active_proposal_count - 1 && out_activity.active_proposal_count >= 0
          }
          _ -> False
        }
      } else {
        False
      }
    },
  )
}

// ============================================================================
// Helper
// ============================================================================

/// Compute the activity token name for a given agent DID
fn activity_token_name_for(
  _config: GovernanceConfig,
  agent_did: ByteArray,
) -> ByteArray {
  let hash = blake2b_256(agent_did)
  builtin.append_bytearray("pact_", builtin.slice_bytearray(0, 27, hash))
}
