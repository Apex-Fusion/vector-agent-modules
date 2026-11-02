/// Endorsement slash tests (validator level).
///
/// After a challenge resolves with CapabilityFalsified, SlashEndorsement
/// moves the endorser slash (less the protocol fee) to the challenger and the
/// fee to the treasury. The endorsement continues with the remaining stake,
/// or closes and returns the remaining stake to the endorser.

use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Transaction}
use endorsement
use reputation_staking/test_helpers.{
  base_tx, key_address, make_input, make_output, make_output_no_datum,
  make_params_ref_input, script_address, test_agent_did, test_agent_did_2,
  test_ap3x_asset_name, test_ap3x_policy_id, test_config,
  test_endorsement_policy_id, test_owner_key_hash, test_owner_key_hash_2,
  test_refs, test_refs_token_policy, test_treasury_script_hash, test_tx_hash,
}
use reputation_staking/types.{
  CapabilityFalsified, Challenge, Endorsement, EndorsementDatum,
  EndorsementSpend, ReputationChallengeDatum, Resolved, SlashEndorsement,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
}

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

const endorser_did: ByteArray = "agent_did_003"

const endorser_key: ByteArray =
  #"aabb00000000000000000000000000000000000000000000000000000000000c"

const other_key: ByteArray =
  #"aabb00000000000000000000000000000000000000000000000000000000000d"

fn endorsement_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"8888888888888888888888888888888888888888888888888888888888888888",
    output_index: 0,
  }
}

fn challenge_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"9999999999999999999999999999999999999999999999999999999999999999",
    output_index: 0,
  }
}

fn endorsement_datum(stake: Int) -> EndorsementDatum {
  EndorsementDatum {
    endorser_did,
    endorser_credential: VerificationKey(endorser_key),
    target_did: test_agent_did_2,
    stake_amount: stake,
    endorsed_capabilities: ["code_review"],
    created_at: 1_000,
  }
}

fn endorsement_token() -> ByteArray {
  derive_endorsement_token_name(endorser_did, test_agent_did_2)
}

fn endorsement_value(ap3x: Int) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, ap3x)
    |> assets.add(test_endorsement_policy_id, endorsement_token(), 1)
}

fn endorsement_input(stake: Int) -> Input {
  Input {
    output_reference: endorsement_ref(),
    output: make_output(
      script_address(test_endorsement_policy_id),
      endorsement_value(stake),
      Endorsement(endorsement_datum(stake)),
    ),
  }
}

/// A challenge against the target that resolved with CapabilityFalsified.
fn falsified_challenge_ref_input() -> Input {
  let challenge =
    ReputationChallengeDatum {
      challenger_did: test_agent_did,
      challenger_credential: VerificationKey(test_owner_key_hash),
      target_did: test_agent_did_2,
      target_credential: VerificationKey(test_owner_key_hash_2),
      challenged_capability: "code_review",
      stake_amount: 25_000_000,
      evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      evidence_uri: "ipfs://evidence",
      created_at: 1_000,
      counter_evidence_hash: "",
      counter_evidence_uri: "",
      response_submitted_at: 0,
      state: Resolved { outcome: CapabilityFalsified },
    }
  Input {
    output_reference: challenge_ref(),
    output: make_output(
      script_address(test_endorsement_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
        |> assets.add(
            test_endorsement_policy_id,
            derive_challenge_token_name(
              test_agent_did,
              test_agent_did_2,
              "code_review",
            ),
            1,
          ),
      Challenge(challenge),
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

fn ap3x_output(address: address.Address, amount: Int) -> Output {
  make_output_no_datum(
    address,
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, amount),
  )
}

fn slash_tx(stake: Int, outputs: List<Output>, mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(2_000),
    inputs: [endorsement_input(stake)],
    reference_inputs: [
      make_params_ref_input(),
      refs_ref_input(),
      falsified_challenge_ref_input(),
    ],
    outputs: outputs,
    mint: mint,
  }
}

fn slash(stake: Int, tx: Transaction) -> Bool {
  endorsement.endorsement.spend(
    test_config(),
    Some(Endorsement(endorsement_datum(stake))),
    EndorsementSpend(SlashEndorsement { challenge_ref: challenge_ref() }),
    endorsement_ref(),
    tx,
  )
}

fn continuing_endorsement(datum: EndorsementDatum) -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    endorsement_value(datum.stake_amount),
    Endorsement(datum),
  )
}

// stake 10 AP3X: slash 5, remaining 5 (= min_endorsement), fee 0.25
const stake_continue: Int = 10_000_000

// stake 6 AP3X: slash 3, remaining 3 (< min_endorsement), fee 0.15
const stake_close: Int = 6_000_000

// ──────────────────────────────────────────────────────────────────────────────
// Continuing endorsement
// ──────────────────────────────────────────────────────────────────────────────

test slash_endorsement_pays_challenger_and_treasury() {
  let tx =
    slash_tx(
      stake_continue,
      [
        continuing_endorsement(endorsement_datum(5_000_000)),
        ap3x_output(key_address(test_owner_key_hash), 4_750_000),
        ap3x_output(script_address(test_treasury_script_hash), 250_000),
      ],
      assets.zero,
    )
  slash(stake_continue, tx)
}

/// The old rule did not say where the slashed AP3X goes.
test slash_endorsement_without_payment_to_challenger_fails() {
  let tx =
    slash_tx(
      stake_continue,
      [
        continuing_endorsement(endorsement_datum(5_000_000)),
        ap3x_output(key_address(other_key), 5_000_000),
      ],
      assets.zero,
    )
  !slash(stake_continue, tx)
}

/// The old rule checked only the stake amount and the DIDs of the continuing
/// datum. The other fields stay the same.
test slash_endorsement_with_changed_endorser_credential_fails() {
  let changed =
    EndorsementDatum {
      ..endorsement_datum(5_000_000),
      endorser_credential: VerificationKey(other_key),
    }
  let tx =
    slash_tx(
      stake_continue,
      [
        continuing_endorsement(changed),
        ap3x_output(key_address(test_owner_key_hash), 4_750_000),
        ap3x_output(script_address(test_treasury_script_hash), 250_000),
      ],
      assets.zero,
    )
  !slash(stake_continue, tx)
}

/// The old rule did not check the address of the continuing endorsement.
test slash_endorsement_with_output_at_other_address_fails() {
  let moved =
    Output {
      ..continuing_endorsement(endorsement_datum(5_000_000)),
      address: key_address(other_key),
    }
  let tx =
    slash_tx(
      stake_continue,
      [
        moved,
        ap3x_output(key_address(test_owner_key_hash), 4_750_000),
        ap3x_output(script_address(test_treasury_script_hash), 250_000),
      ],
      assets.zero,
    )
  !slash(stake_continue, tx)
}

// ──────────────────────────────────────────────────────────────────────────────
// Closing endorsement (remaining below min_endorsement)
// ──────────────────────────────────────────────────────────────────────────────

fn endorsement_burn() -> assets.Value {
  assets.from_asset(test_endorsement_policy_id, endorsement_token(), -1)
}

test slash_endorsement_closes_and_returns_remaining() {
  let tx =
    slash_tx(
      stake_close,
      [
        ap3x_output(key_address(endorser_key), 3_000_000),
        ap3x_output(key_address(test_owner_key_hash), 2_850_000),
        ap3x_output(script_address(test_treasury_script_hash), 150_000),
      ],
      endorsement_burn(),
    )
  slash(stake_close, tx)
}

/// The old rule only required the token burn.
test slash_endorsement_closing_without_payment_to_endorser_fails() {
  let tx =
    slash_tx(
      stake_close,
      [
        ap3x_output(key_address(other_key), 3_000_000),
        ap3x_output(key_address(test_owner_key_hash), 2_850_000),
        ap3x_output(script_address(test_treasury_script_hash), 150_000),
      ],
      endorsement_burn(),
    )
  !slash(stake_close, tx)
}
