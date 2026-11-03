/// Protocol parameters for Module 3: Reputation Staking.
///
/// ProtocolParams is read from the params record: the reference input at the
/// params holder that holds the `params` record token. This type contains
/// only the Module 3 fields needed by the reputation validators.
///
/// Time units: every duration and every point in time is in POSIX
/// milliseconds, the unit of the transaction validity range. Epoch counts are
/// in epochs of `epoch_length` milliseconds.
///
/// See MODULE-3-REPUTATION-STAKING-IMPL-SPEC.md Sections 10 and 11.2b.

use cardano/assets.{PolicyId}
use cardano/transaction.{Input}
use shared/record_auth

/// ProtocolParams — Module 3 fields only.
pub type ProtocolParams {
  /// Minimum AP3X for self-stake (initially 10 AP3X = 10_000_000 DFM)
  min_self_stake: Int,
  /// Minimum AP3X for endorsement (initially 5 AP3X)
  min_endorsement: Int,
  /// Minimum AP3X to file a challenge (initially 25 AP3X)
  min_challenge_stake: Int,
  /// Milliseconds between stake modifications (86_400_000 = 24 h)
  stake_cooldown: Int,
  /// Milliseconds before an endorsement can be withdrawn (172_800_000 = 48 h)
  endorsement_cooldown: Int,
  /// Decay rate in basis points per epoch (100 = 1%)
  decay_rate: Int,
  /// Epochs of inactivity before decay starts (180 epochs = 30 days)
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
  /// Milliseconds for the target to respond to a challenge (43_200_000 = 12 h)
  challenge_response_deadline: Int,
  /// Minimum agent age in milliseconds before it can challenge (86_400_000 = 24 h)
  min_agent_age: Int,
  /// Milliseconds after the response deadline to escalate to Module 1
  /// (21_600_000 = 6 h)
  escalation_window: Int,
  /// Default judgment fee in basis points (100 = 1%)
  default_judgment_fee: Int,
  /// Maximum genesis bonus recipients
  genesis_agent_cap: Int,
  /// History points per genesis agent (100 AP3X equivalent)
  genesis_bonus_amount: Int,
  /// POSIX time in milliseconds at which genesis bonus minting closes
  /// (deployment time + 2_419_200_000 = 28 days)
  genesis_minting_window: Int,
  /// Milliseconds before an expired genesis UTXO can be burned
  /// (5_184_000_000 = 60 days)
  genesis_protection_period: Int,
  /// Valid capability tags for the current taxonomy
  valid_capabilities: List<ByteArray>,
  /// Milliseconds per epoch, for decay (14_400_000 = 4 h)
  epoch_length: Int,
}

/// Find and parse the ProtocolParams record in the reference inputs.
///
/// The record is the reference input at `params_script_hash` that holds the
/// record token (`params_token_policy`, `params`). Fails when there is no
/// such reference input.
pub fn find_protocol_params(
  reference_inputs: List<Input>,
  params_script_hash: ByteArray,
  params_token_policy: PolicyId,
) -> ProtocolParams {
  expect Some(raw) =
    record_auth.find_record(
      reference_inputs,
      params_script_hash,
      params_token_policy,
      record_auth.params_token_name,
    )
  expect datum: ProtocolParams = raw
  datum
}
