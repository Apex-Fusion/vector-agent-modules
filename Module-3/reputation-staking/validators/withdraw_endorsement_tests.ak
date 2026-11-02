/// WithdrawEndorsement through the endorsement spend handler (W4-MODFIX2
/// item 1). The withdrawal needs the bound stake of the endorsement (the
/// target stake whose stake_id is target_stake) as a reference input without
/// challenge locks. The library cases are in
/// lib/reputation_staking/bound_stake_tests.ak.

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, OutputReference, Transaction}
use endorsement
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_identity_ref_input, make_input,
  make_output, make_output_no_datum, make_params_ref_input, script_address,
  stake_ref_input, test_agent_did, test_agent_did_2, test_ap3x_asset_name,
  test_ap3x_policy_id, test_config, test_endorsement_policy_id,
  test_owner_key_hash, test_refs, test_refs_token_policy, test_stake_id,
  test_tx_hash,
}
use reputation_staking/types.{
  Endorsement, EndorsementDatum, EndorsementSpend, WithdrawEndorsement,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
}

const t0: Int = 1_700_000_000_000

fn endorsement_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"8888888888888888888888888888888888888888888888888888888888888888",
    output_index: 1,
  }
}

/// An endorsement of agent 2 by agent 1 whose cooldown ended at t0.
fn datum() -> EndorsementDatum {
  EndorsementDatum {
    endorser_did: test_agent_did,
    endorser_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    stake_amount: 5_000_000,
    endorsed_capabilities: ["code_review"],
    created_at: t0 - 172_800_000,
    slashed_by: [],
    target_stake: test_stake_id,
  }
}

fn token() -> ByteArray {
  derive_endorsement_token_name(test_agent_did, test_agent_did_2)
}

fn endorsement_input() -> Input {
  Input {
    output_reference: endorsement_ref(),
    output: make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 5_000_000)
        |> assets.add(test_endorsement_policy_id, token(), 1),
      Endorsement(datum()),
    ),
  }
}

fn refs_ref_input() -> Input {
  make_input(
    test_tx_hash,
    400,
    make_output(
      key_address(test_owner_key_hash),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_refs_token_policy, "reputation_refs", 1),
      test_refs(),
    ),
  )
}

/// The withdrawal with the bound stake `stakes` among the reference inputs.
fn withdraw(stakes: List<Input>) -> Bool {
  let tx =
    Transaction {
      ..base_tx(t0),
      validity_range: interval.between(t0, t0 + 600_000),
      inputs: [endorsement_input()],
      reference_inputs: [
        make_params_ref_input(),
        refs_ref_input(),
        make_identity_ref_input(test_agent_did, 1),
        ..stakes
      ],
      outputs: [
        make_output_no_datum(
          key_address(test_owner_key_hash),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 5_000_000),
        ),
      ],
      mint: assets.from_asset(test_endorsement_policy_id, token(), -1),
      extra_signatories: agent_signers(),
    }
  endorsement.endorsement.spend(
    test_config(),
    Some(Endorsement(datum())),
    EndorsementSpend(WithdrawEndorsement),
    endorsement_ref(),
    tx,
  )
}

test withdraw_endorsement_spend_with_unlocked_bound_stake() {
  withdraw([stake_ref_input(test_agent_did_2, test_stake_id, [])])
}

test withdraw_endorsement_spend_with_locked_bound_stake_fails() {
  !withdraw(
    [
      stake_ref_input(
        test_agent_did_2,
        test_stake_id,
        [
          derive_challenge_token_name(
            test_agent_did,
            test_agent_did_2,
            "code_review",
          ),
        ],
      ),
    ],
  )
}

test withdraw_endorsement_spend_without_bound_stake_fails() fail {
  withdraw([])
}
