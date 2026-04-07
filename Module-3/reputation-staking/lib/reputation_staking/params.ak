/// Protocol parameters stub for Module 3: Reputation Staking.
///
/// In production, ProtocolParams is a shared type defined in the system-level
/// params contract and read via reference input. This stub contains only the
/// Module 3 fields needed by the reputation validators.
///
/// See GAME-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 10 and 11.2b.

use aiken/collection/list
use cardano/address.{Script}
use cardano/transaction.{InlineDatum, Input, Output}

/// Stub ProtocolParams — Module 3 fields only.
/// Replace with the real shared ProtocolParams type when available.
pub type ProtocolParams {
  /// Minimum AP3X for self-stake (initially 10 AP3X = 10_000_000 DFM)
  min_self_stake: Int,
  /// Minimum AP3X for endorsement (initially 5 AP3X)
  min_endorsement: Int,
  /// Minimum AP3X to file a challenge (initially 25 AP3X)
  min_challenge_stake: Int,
  /// Slots between stake modifications (21,600 slots ≈ 24h)
  stake_cooldown: Int,
  /// Slots before endorsement can be withdrawn (43,200 slots ≈ 48h)
  endorsement_cooldown: Int,
  /// Decay rate in basis points per epoch (100 = 1%)
  decay_rate: Int,
  /// Epochs of inactivity before decay kicks in (180 epochs ≈ 30 days)
  activity_window: Int,
  /// Decay collector fee in basis points (500 = 5%)
  decay_collector_fee: Int,
  /// History multiplier in basis points (1000 = 10%)
  history_multiplier: Int,
  /// Max endorsement multiplier (3 = endorsements capped at 3× self-stake)
  max_endorsement_multiplier: Int,
  /// Endorser slash rate in basis points (5000 = 50%)
  slash_rate_endorser: Int,
  /// Protocol fee rate in basis points (500 = 5%)
  protocol_fee_rate: Int,
  /// Slots for target to respond to challenge (10,800 ≈ 12h)
  challenge_response_deadline: Int,
  /// Minimum agent age in slots before they can challenge (21,600 ≈ 24h)
  min_agent_age: Int,
  /// Slots after response deadline to escalate to Module 1 (5,400 ≈ 6h)
  escalation_window: Int,
  /// Default judgment fee in basis points (100 = 1%)
  default_judgment_fee: Int,
  /// Maximum genesis bonus recipients
  genesis_agent_cap: Int,
  /// History points per genesis agent (100 AP3X equivalent)
  genesis_bonus_amount: Int,
  /// Slots after launch for minting genesis bonuses (604,800 ≈ 28 days)
  genesis_minting_window: Int,
  /// Slots before expired genesis UTXOs can be burned (129,600 ≈ 60 days)
  genesis_protection_period: Int,
  /// Valid capability tags for the current taxonomy
  valid_capabilities: List<ByteArray>,
  /// Slots per epoch (for decay calculation: 900 slots ≈ 4h per epoch)
  epoch_length: Int,
}

/// Find and parse the ProtocolParams UTXO from reference inputs.
pub fn find_protocol_params(
  reference_inputs: List<Input>,
  params_script_hash: ByteArray,
) -> ProtocolParams {
  expect Some(params_input) =
    list.find(
      reference_inputs,
      fn(input) {
        when input.output.address.payment_credential is {
          Script(hash) -> hash == params_script_hash
          _ -> False
        }
      },
    )
  expect_protocol_params(params_input.output)
}

/// Extract ProtocolParams from an output's inline datum.
fn expect_protocol_params(output: Output) -> ProtocolParams {
  expect InlineDatum(raw) = output.datum
  expect datum: ProtocolParams = raw
  datum
}
