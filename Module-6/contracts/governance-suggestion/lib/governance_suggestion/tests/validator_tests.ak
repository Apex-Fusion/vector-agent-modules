// Oracle: Self-Improvement Module — Validator-Level Mock Transaction Tests
//
// These tests call the validation functions from proposal_validation.ak and
// critique_validation.ak with fully constructed transactions (inputs,
// outputs, reference inputs, mint values, signatories, redeemers), as the
// validator entry points pass them.
//
// Each test constructs the minimum viable transaction for a specific action
// and verifies both positive (valid tx) and negative (invalid tx) cases.

use aiken/crypto.{blake2b_256}
use cardano/address.{Address, Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output, Transaction}
use governance_suggestion/critique_validation
use governance_suggestion/params
use governance_suggestion/proposal_validation
use governance_suggestion/tests/test_helpers.{
  activity_output, activity_token_mint, critic_agent, critic_cred, critic_did,
  critic_key_hash, critique_output, critique_own_ref, critique_seed_input,
  cross_refs_ref_input, default_proposal_datum, finalisation_outputs,
  finalisation_tx, identity_ref_input, oracle_key_hash, oracle_ref_inputs,
  params_ref_input, proposal_input, proposal_output, proposal_own_ref,
  proposal_ref_input, proposal_seed_input, proposal_utxo_ref, proposer_agent,
  proposer_cred, proposer_did, proposer_key_hash, protocol_params_ref_input,
  standard_ref_inputs, test_config, test_cross_refs, validity_range_from,
}
use governance_suggestion/types.{
  AdoptProposal, Amended, CritiqueDatum, ExpireProposal, ExpireStaleProposal,
  GovernanceEndorsementDatum, ParameterChange, ProposalDatum, RejectProposal,
  Supportive, WithdrawProposal,
}
use governance_suggestion/utils

// ============================================================================
// Builders
// ============================================================================

/// The lower bound of the submission transactions.
const submit_lo: Int = 2_000_000_000

/// The default proposal, submitted at `submit_lo`.
fn submitted() -> ProposalDatum {
  ProposalDatum { ..default_proposal_datum(), submitted_at: submit_lo }
}

/// A first proposal submission of `datum` (the new datum; the seed holds the
/// same datum), with the given reference inputs and signers.
fn submit_with(
  datum: ProposalDatum,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [proposal_seed_input(datum)],
      reference_inputs: reference_inputs,
      outputs: [
        proposal_output(datum),
        activity_output(proposer_did, 1, submit_lo),
      ],
      mint: test_helpers.proposal_token_mint(proposal_own_ref(), 1)
        |> assets.merge(activity_token_mint(proposer_did, 1)),
      extra_signatories: signatories,
      validity_range: validity_range_from(submit_lo),
    }
  proposal_validation.validate_submit_proposal(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    proposal_own_ref(),
    tx,
  )
}

fn submit(datum: ProposalDatum) -> Bool {
  submit_with(datum, standard_ref_inputs(proposer_agent), [proposer_key_hash])
}

// ============================================================================
// §5.1 SubmitProposal Tests
// ============================================================================

test submit_proposal_valid() {
  submit(submitted())
}

test submit_proposal_missing_did() fail {
  // No identity record reference input — the identity check fails
  submit_with(
    submitted(),
    [cross_refs_ref_input(), params_ref_input()],
    [proposer_key_hash],
  )
}

test submit_proposal_insufficient_stake() fail {
  // Stake below min (25 AP3X = 25_000_000)
  submit(ProposalDatum { ..submitted(), stake_amount: 1_000_000 })
}

test submit_proposal_bad_hash_length() fail {
  // Hash is 16 bytes instead of 32
  submit(
    ProposalDatum {
      ..submitted(),
      proposal_hash: #"aabbccddaabbccddaabbccddaabbccdd",
    },
  )
}

test submit_proposal_empty_uri() fail {
  submit(ProposalDatum { ..submitted(), storage_uri: "" })
}

test submit_proposal_wrong_signer() fail {
  // Wrong signer — critic instead of proposer
  submit_with(
    submitted(),
    standard_ref_inputs(proposer_agent),
    [critic_key_hash],
  )
}

test submit_proposal_window_too_short() fail {
  // Review window below min (259_200_000 = ~3 days)
  submit(ProposalDatum { ..submitted(), review_window: 1_000 })
}

test submit_proposal_window_too_long() fail {
  // Review window above max (2_419_200_000 = ~28 days)
  submit(ProposalDatum { ..submitted(), review_window: 9_999_999_999 })
}

test submit_proposal_non_open_state() fail {
  submit(
    ProposalDatum {
      ..submitted(),
      state: Amended { previous_hash: blake2b_256("old") },
    },
  )
}

// ============================================================================
// Finalisations: WithdrawProposal, ExpireProposal, AdoptProposal,
// RejectProposal, ExpireStaleProposal
// ============================================================================

/// The finalisation of the default proposal with `action`, the reference
/// inputs `refs_in`, the outputs of finalisation_outputs, the signers
/// `signatories` and a range from `lo`.
fn finalise(
  action: Data,
  datum: ProposalDatum,
  refs_in: List<Input>,
  signatories: List<ByteArray>,
  lo: Int,
) -> Transaction {
  finalisation_tx(
    action,
    datum,
    refs_in,
    finalisation_outputs(datum),
    signatories,
    validity_range_from(lo),
  )
}

// §5.2 WithdrawProposal

fn withdraw(signatories: List<ByteArray>) -> Bool {
  let datum = default_proposal_datum()
  let action: Data = WithdrawProposal
  proposal_validation.validate_withdraw_proposal(
    test_cross_refs(),
    datum,
    proposal_utxo_ref(),
    finalise(action, datum, [], signatories, submit_lo),
  )
}

test withdraw_proposal_valid() {
  withdraw([proposer_key_hash])
}

test withdraw_proposal_wrong_signer() fail {
  withdraw([critic_key_hash])
}

// §5.6 ExpireProposal

fn expire(current_slot: Int) -> Bool {
  let datum = default_proposal_datum()
  let action: Data = ExpireProposal
  proposal_validation.validate_expire_proposal(
    test_cross_refs(),
    datum,
    current_slot,
    proposal_utxo_ref(),
    finalise(action, datum, [], [], current_slot),
  )
}

test expire_proposal_valid() {
  // Current slot past submitted_at + review_window
  let datum = default_proposal_datum()
  expire(datum.submitted_at + datum.review_window + 1)
}

test expire_proposal_too_early() fail {
  // Current slot BEFORE window expiry
  let datum = default_proposal_datum()
  expire(datum.submitted_at + datum.review_window - 1_000)
}

// §5.4 AdoptProposal

fn adopt(reward_amount: Int, signatories: List<ByteArray>) -> Bool {
  let datum = default_proposal_datum()
  let reasoning_hash = blake2b_256("adoption reasoning")
  let action: Data = AdoptProposal { reasoning_hash, reward_amount }
  proposal_validation.validate_adopt_proposal(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    reasoning_hash,
    reward_amount,
    proposal_utxo_ref(),
    finalise(
      action,
      datum,
      oracle_ref_inputs(proposer_agent),
      signatories,
      submit_lo,
    ),
  )
}

test adopt_proposal_valid() {
  // 100 AP3X, oracle signs
  adopt(100_000_000, [oracle_key_hash])
}

test adopt_proposal_no_oracle_sig() fail {
  // No oracle signature — regular proposer signed instead
  adopt(100_000_000, [proposer_key_hash])
}

test adopt_proposal_reward_too_low() fail {
  // Below min_adoption_reward (50_000_000)
  adopt(1_000_000, [oracle_key_hash])
}

test adopt_proposal_reward_too_high() fail {
  // Above max_adoption_reward (500_000_000)
  adopt(999_000_000, [oracle_key_hash])
}

// §5.5 RejectProposal

fn reject(signatories: List<ByteArray>) -> Bool {
  let datum = default_proposal_datum()
  let reasoning_hash = blake2b_256("rejection reasoning")
  let action: Data = RejectProposal { reasoning_hash }
  proposal_validation.validate_reject_proposal(
    test_config(),
    test_cross_refs(),
    datum,
    reasoning_hash,
    proposal_utxo_ref(),
    finalise(
      action,
      datum,
      oracle_ref_inputs(proposer_agent),
      signatories,
      submit_lo,
    ),
  )
}

test reject_proposal_valid() {
  reject([oracle_key_hash])
}

test reject_proposal_no_oracle_sig() fail {
  // No oracle sig
  reject([proposer_key_hash])
}

// §5.6b ExpireStaleProposal

fn expire_stale(datum: ProposalDatum) -> Bool {
  let action: Data = ExpireStaleProposal
  proposal_validation.validate_expire_stale_proposal(
    test_config(),
    test_cross_refs(),
    datum,
    proposal_utxo_ref(),
    // Reference inputs must include ProtocolParams at protocol_params_hash
    finalise(
      action,
      datum,
      [cross_refs_ref_input(), protocol_params_ref_input()],
      [],
      submit_lo,
    ),
  )
}

test expire_stale_proposal_valid() {
  expire_stale(
    ProposalDatum {
      ..default_proposal_datum(),
      proposal_type: ParameterChange {
        param_name: "MIN_CLAIM_STAKE",
        current_value: 50_000_000,
        proposed_value: 25_000_000,
      },
    },
  )
}

test expire_stale_proposal_not_param_change() fail {
  // GeneralSuggestion — not a ParameterChange, should fail
  expire_stale(default_proposal_datum())
}

// ============================================================================
// §5.6c ExtendReview Tests
// ============================================================================

fn extend(additional_slots: Int, signatories: List<ByteArray>) -> Bool {
  let datum = default_proposal_datum()
  // Continuing output with updated review_window
  let updated_datum =
    ProposalDatum {
      ..datum,
      review_window: datum.review_window + additional_slots,
    }
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [proposal_input(datum, proposal_own_ref())],
      reference_inputs: oracle_ref_inputs(proposer_agent),
      outputs: [proposal_output(updated_datum)],
      extra_signatories: signatories,
      validity_range: validity_range_from(submit_lo),
    }
  proposal_validation.validate_extend_review(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    additional_slots,
    proposal_utxo_ref(),
    tx,
  )
}

test extend_review_valid() {
  extend(100_000_000, [oracle_key_hash])
}

test extend_review_exceeds_max() fail {
  // Push total past max_review_window (2_419_200_000)
  extend(9_999_999_999, [oracle_key_hash])
}

test extend_review_no_oracle_sig() fail {
  // No oracle signature
  extend(100_000_000, [proposer_key_hash])
}

// ============================================================================
// §5.3 AmendProposal Tests
// ============================================================================

fn amend(datum: ProposalDatum, new_hash: ByteArray, new_uri: ByteArray) -> Bool {
  // No incorporated critiques for simplicity
  let amended_datum =
    ProposalDatum {
      ..datum,
      state: Amended { previous_hash: datum.proposal_hash },
      proposal_hash: new_hash,
      storage_uri: new_uri,
      amendment_count: datum.amendment_count + 1,
    }
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [proposal_input(datum, proposal_own_ref())],
      reference_inputs: standard_ref_inputs(proposer_agent),
      outputs: [proposal_output(amended_datum)],
      extra_signatories: [proposer_key_hash],
      validity_range: validity_range_from(1_500_000_000),
    }
  proposal_validation.validate_amend_proposal(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    new_hash,
    new_uri,
    [],
    proposal_utxo_ref(),
    tx,
  )
}

test amend_proposal_valid() {
  amend(
    default_proposal_datum(),
    blake2b_256("amended proposal content v2"),
    "ipfs://QmAmendedProposal",
  )
}

test amend_proposal_max_amendments() fail {
  // Already at max amendments (5)
  amend(
    ProposalDatum { ..default_proposal_datum(), amendment_count: 5 },
    blake2b_256("amended proposal content v7"),
    "ipfs://QmAmended7",
  )
}

// ============================================================================
// §5.7 SubmitCritique Tests
// ============================================================================

/// The lower bound of the critique and endorsement submissions.
const critique_lo: Int = 1_500_000_000

fn critique_by(did: ByteArray, critique_type: types.CritiqueType) -> CritiqueDatum {
  CritiqueDatum {
    critic_did: did,
    critic_credential: if did == proposer_did {
      proposer_cred()
    } else {
      critic_cred()
    },
    proposal_ref: proposal_own_ref(),
    critique_hash: blake2b_256("critique content"),
    storage_uri: "ipfs://QmCritique",
    critique_type,
    stake_amount: 5_000_000,
    submitted_at: critique_lo,
    incorporated: False,
  }
}

/// A critique submission of `datum` (the seed holds the same datum), signed
/// by `signer`, with the identity records `records`, the proposer's record
/// and the open default proposal as reference inputs.
fn submit_critique(
  datum: CritiqueDatum,
  records: List<Input>,
  signer: ByteArray,
) -> Bool {
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [critique_seed_input(critique_own_ref(), datum)],
      reference_inputs: [
        cross_refs_ref_input(),
        params_ref_input(),
        proposal_ref_input(default_proposal_datum()),
        ..records
      ],
      outputs: [critique_output(critique_own_ref(), datum)],
      extra_signatories: [signer],
      validity_range: validity_range_from(critique_lo),
    }
  critique_validation.validate_submit_critique(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    critique_own_ref(),
    tx,
  )
}

/// The records of the critic and of the proposer.
fn both_records() -> List<Input> {
  [identity_ref_input(critic_agent), identity_ref_input(proposer_agent)]
}

test submit_critique_valid() {
  submit_critique(
    critique_by(critic_did, Supportive),
    both_records(),
    critic_key_hash,
  )
}

test submit_critique_self_critique_supportive() fail {
  // Critic DID == Proposer DID with Supportive type — should fail
  submit_critique(
    critique_by(proposer_did, Supportive),
    [identity_ref_input(proposer_agent)],
    proposer_key_hash,
  )
}

test submit_critique_self_amendment_allowed() {
  // Self-critique with Amendment type — should be allowed
  submit_critique(
    critique_by(
      proposer_did,
      types.Amendment { suggested_change_hash: blake2b_256("improvement") },
    ),
    [identity_ref_input(proposer_agent)],
    proposer_key_hash,
  )
}

test submit_critique_missing_did() fail {
  // No identity record of the critic
  submit_critique(
    critique_by(critic_did, Supportive),
    [identity_ref_input(proposer_agent)],
    critic_key_hash,
  )
}

test submit_critique_insufficient_stake() fail {
  // Below min_critique_stake (5_000_000)
  submit_critique(
    CritiqueDatum { ..critique_by(critic_did, Supportive), stake_amount: 1_000 },
    both_records(),
    critic_key_hash,
  )
}

// ============================================================================
// §5.8 WithdrawCritique Tests
// ============================================================================

fn withdraw_critique(incorporated: Bool, signer: ByteArray) -> Bool {
  let critique_datum =
    CritiqueDatum { ..critique_by(critic_did, Supportive), incorporated }
  // Need proposal as reference input to check it's still active
  critique_validation.validate_withdraw_critique(
    critique_datum,
    [proposal_ref_input(default_proposal_datum())],
    test_cross_refs(),
    [signer],
  )
}

test withdraw_critique_valid() {
  withdraw_critique(False, critic_key_hash)
}

test withdraw_critique_incorporated() fail {
  // Already incorporated — can't withdraw
  withdraw_critique(True, critic_key_hash)
}

test withdraw_critique_wrong_signer() fail {
  // Wrong signer
  withdraw_critique(False, proposer_key_hash)
}

// ============================================================================
// §5.9 SubmitEndorsement Tests
// ============================================================================

fn endorsement_by(did: ByteArray) -> GovernanceEndorsementDatum {
  GovernanceEndorsementDatum {
    endorser_did: did,
    endorser_credential: if did == proposer_did {
      proposer_cred()
    } else {
      critic_cred()
    },
    proposal_ref: proposal_own_ref(),
    stake_amount: 5_000_000,
    created_at: critique_lo,
  }
}

/// An endorsement submission of `datum`, signed by `signer`, with the
/// identity records `records` and the open default proposal.
fn submit_endorsement(
  datum: GovernanceEndorsementDatum,
  records: List<Input>,
  signer: ByteArray,
) -> Bool {
  let seed =
    test_helpers.make_ref(
      #"3333333333333333333333333333333333333333333333333333333333333333",
      0,
    )
  let address =
    Address {
      payment_credential: Script(test_helpers.endorsement_validator_hash),
      stake_credential: None,
    }
  let tx =
    Transaction {
      ..transaction.placeholder,
      inputs: [
        Input {
          output_reference: seed,
          output: Output {
            address,
            value: assets.from_lovelace(7_000_000),
            datum: InlineDatum(datum),
            reference_script: None,
          },
        },
      ],
      reference_inputs: [
        cross_refs_ref_input(),
        params_ref_input(),
        proposal_ref_input(default_proposal_datum()),
        ..records
      ],
      outputs: [
        Output {
          address,
          value: assets.from_lovelace(7_000_000)
            |> assets.add(
                test_helpers.critique_mint_hash,
                utils.endorsement_token_name(
                  datum.endorser_did,
                  datum.proposal_ref,
                ),
                1,
              ),
          datum: InlineDatum(datum),
          reference_script: None,
        },
      ],
      extra_signatories: [signer],
      validity_range: validity_range_from(critique_lo),
    }
  critique_validation.validate_submit_endorsement(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    seed,
    tx,
  )
}

test submit_endorsement_valid() {
  submit_endorsement(endorsement_by(critic_did), both_records(), critic_key_hash)
}

test submit_endorsement_self_endorse() fail {
  // Endorser DID == Proposer DID — not allowed
  submit_endorsement(
    endorsement_by(proposer_did),
    [identity_ref_input(proposer_agent)],
    proposer_key_hash,
  )
}

// ============================================================================
// WithdrawEndorsement Tests
// ============================================================================

test withdraw_endorsement_valid() {
  critique_validation.validate_withdraw_endorsement(
    endorsement_by(critic_did),
    [critic_key_hash],
  )
}

test withdraw_endorsement_wrong_signer() fail {
  // Wrong signer
  critique_validation.validate_withdraw_endorsement(
    endorsement_by(critic_did),
    [proposer_key_hash],
  )
}
