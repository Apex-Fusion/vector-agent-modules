// Module 6: Critique and endorsement spend tests (validator level).
//
// Each test builds a full transaction and calls the spend handler of the
// critique or endorsement validator, the critique mint policy, or the
// proposal spend validator for an amendment. Negative tests name the rule
// that the transaction does not meet.

use aiken/collection/list
use aiken/crypto.{blake2b_256}
use aiken/interval
use cardano/address.{Address, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, ScriptPurpose, Spend,
  Transaction, ValidityRange,
}
use critique
use governance_suggestion/tests/test_helpers.{
  critic_cred, critic_did, critic_key_hash, critique_mint_hash,
  critique_output, critique_seed_input, critique_validator_hash,
  cross_refs_ref_input, default_proposal_datum, endorsement_validator_hash,
  make_ref, params_ref_input,
  payment_output, proposal_input, proposal_mint_hash, proposal_output,
  proposal_own_ref, proposal_validator_hash, proposer_cred, proposer_key_hash,
  test_config,
}
use governance_suggestion/types.{
  AdoptProposal, AmendProposal, Amended, BurnCritiqueToken,
  BurnEndorsementToken, CritiqueDatum, ExpireProposal,
  GovernanceEndorsementDatum, IncorporateCritique, MintCritiqueToken,
  MintEndorsementToken, ProposalDatum, RewardCritique, Supportive,
  WithdrawCritique,
}
use governance_suggestion/utils
use proposal
use shared/identity_fixtures.{
  agent_id, controller_hash, controller_key, ed_key_hash, record, record_input,
  subagent, with_key, with_transaction_key,
}
use vector_identity.{purpose_authentication, purpose_transaction}
use vector_identity/types.{Ed25519, IdentityDatum} as identity_types

// ============================================================================
// Fixtures
// ============================================================================

/// Lovelace locked in each test critique (5 AP3X stake + 2 AP3X deposit).
const critique_lovelace: Int = 7_000_000

fn critique_ref(n: Int) -> OutputReference {
  make_ref(
    #"2222222222222222222222222222222222222222222222222222222222222222",
    n,
  )
}

fn endorsement_ref() -> OutputReference {
  make_ref(
    #"3333333333333333333333333333333333333333333333333333333333333333",
    0,
  )
}

/// The proposal input of test_helpers.proposal_input.
fn proposal_utxo_ref() -> OutputReference {
  make_ref(
    #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0002",
    0,
  )
}

fn critique_datum(incorporated: Bool) -> CritiqueDatum {
  CritiqueDatum {
    critic_did,
    critic_credential: critic_cred(),
    proposal_ref: proposal_own_ref(),
    critique_hash: blake2b_256("critique"),
    storage_uri: "ipfs://QmCritique",
    critique_type: Supportive,
    stake_amount: 5_000_000,
    submitted_at: 1_500_000_000,
    incorporated,
  }
}

fn critique_token(n: Int) -> ByteArray {
  utils.critique_token_name(critique_ref(n))
}

fn critique_address() -> Address {
  Address {
    payment_credential: Script(critique_validator_hash),
    stake_credential: None,
  }
}

fn critique_value(n: Int) -> assets.Value {
  assets.from_lovelace(critique_lovelace)
    |> assets.add(critique_mint_hash, critique_token(n), 1)
}

fn critique_input(n: Int, datum: CritiqueDatum) -> Input {
  Input {
    output_reference: critique_ref(n),
    output: Output {
      address: critique_address(),
      value: critique_value(n),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

fn endorsement_datum() -> GovernanceEndorsementDatum {
  GovernanceEndorsementDatum {
    endorser_did: "did_endorser_test_003",
    endorser_credential: critic_cred(),
    proposal_ref: proposal_own_ref(),
    stake_amount: 5_000_000,
    created_at: 1_500_000_000,
  }
}

fn endorsement_token() -> ByteArray {
  utils.endorsement_token_name("did_endorser_test_003", proposal_own_ref())
}

fn endorsement_input() -> Input {
  Input {
    output_reference: endorsement_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(endorsement_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(critique_lovelace)
        |> assets.add(critique_mint_hash, endorsement_token(), 1),
      datum: InlineDatum(endorsement_datum()),
      reference_script: None,
    },
  }
}

fn redeemer(ref: OutputReference, action: Data) -> Pair<ScriptPurpose, Data> {
  Pair(Spend(ref), action)
}

fn proposal_burn() -> assets.Value {
  assets.from_asset(
    proposal_mint_hash,
    utils.proposal_token_name(proposal_own_ref()),
    -1,
  )
}

fn critique_burn(n: Int) -> assets.Value {
  assets.from_asset(critique_mint_hash, critique_token(n), -1)
}

fn base_tx() -> Transaction {
  Transaction {
    ..transaction.placeholder,
    reference_inputs: [cross_refs_ref_input(), params_ref_input()],
  }
}

/// A transaction that expires the test proposal and settles critique 0.
fn expiry_with_critique(outputs: List<Output>, mint: assets.Value) -> Transaction {
  let expire: Data = ExpireProposal
  let burn: Data = BurnCritiqueToken
  Transaction {
    ..base_tx(),
    inputs: [
      proposal_input(default_proposal_datum(), proposal_own_ref()),
      critique_input(0, critique_datum(False)),
    ],
    outputs: outputs,
    mint: mint,
    redeemers: [redeemer(proposal_utxo_ref(), expire), redeemer(critique_ref(0), burn)],
  }
}

fn spend_critique(
  datum: CritiqueDatum,
  action: types.CritiqueAction,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let raw: Data = datum
  critique.critique_spend.spend(test_config(), Some(raw), action, own_ref, tx)
}

fn spend_endorsement(tx: Transaction) -> Bool {
  let raw: Data = endorsement_datum()
  critique.endorsement_spend.spend(
    test_config(),
    Some(raw),
    BurnEndorsementToken,
    endorsement_ref(),
    tx,
  )
}

// ============================================================================
// BurnCritiqueToken
// ============================================================================

test burn_critique_settles_in_proposal_expiry() {
  let tx =
    expiry_with_critique(
      [payment_output(critic_cred(), critique_lovelace)],
      assets.merge(proposal_burn(), critique_burn(0)),
    )
  spend_critique(critique_datum(False), BurnCritiqueToken, critique_ref(0), tx)
}

/// Rule: the transaction finalises a proposal.
test burn_critique_without_proposal_finalisation_fails() {
  let burn: Data = BurnCritiqueToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [critique_input(0, critique_datum(False))],
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: critique_burn(0),
      redeemers: [redeemer(critique_ref(0), burn)],
    }
  !spend_critique(critique_datum(False), BurnCritiqueToken, critique_ref(0), tx)
}

/// Rule: the critic receives the lovelace of the critique.
test burn_critique_without_payment_to_critic_fails() {
  let tx =
    expiry_with_critique(
      [
        payment_output(critic_cred(), 1_000_000),
        payment_output(proposer_cred(), critique_lovelace),
      ],
      assets.merge(proposal_burn(), critique_burn(0)),
    )
  !spend_critique(critique_datum(False), BurnCritiqueToken, critique_ref(0), tx)
}

/// Rule: the critique token is burned.
test burn_critique_without_token_burn_fails() {
  let tx =
    expiry_with_critique(
      [payment_output(critic_cred(), critique_lovelace)],
      proposal_burn(),
    )
  !spend_critique(critique_datum(False), BurnCritiqueToken, critique_ref(0), tx)
}

/// Rule: one output to the critic counts once. Two critiques of the same
/// critic need the lovelace of both.
test burn_two_critiques_with_one_payment_fails() {
  let expire: Data = ExpireProposal
  let burn: Data = BurnCritiqueToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        critique_input(0, critique_datum(False)),
        critique_input(1, critique_datum(False)),
      ],
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: proposal_burn()
        |> assets.merge(critique_burn(0))
        |> assets.merge(critique_burn(1)),
      redeemers: [
        redeemer(proposal_utxo_ref(), expire),
        redeemer(critique_ref(0), burn),
        redeemer(critique_ref(1), burn),
      ],
    }
  !spend_critique(critique_datum(False), BurnCritiqueToken, critique_ref(0), tx)
}

test burn_two_critiques_with_full_payment() {
  let expire: Data = ExpireProposal
  let burn: Data = BurnCritiqueToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        critique_input(0, critique_datum(False)),
        critique_input(1, critique_datum(False)),
      ],
      outputs: [payment_output(critic_cred(), 2 * critique_lovelace)],
      mint: proposal_burn()
        |> assets.merge(critique_burn(0))
        |> assets.merge(critique_burn(1)),
      redeemers: [
        redeemer(proposal_utxo_ref(), expire),
        redeemer(critique_ref(0), burn),
        redeemer(critique_ref(1), burn),
      ],
    }
  spend_critique(critique_datum(False), BurnCritiqueToken, critique_ref(1), tx)
}

/// Rule: when the critic is also the proposer of the finalised proposal, the
/// outputs to that credential cover both the proposal and the critique.
test burn_critique_of_proposer_counts_proposal_stake() {
  let own_critique =
    CritiqueDatum { ..critique_datum(False), critic_credential: proposer_cred() }
  let expire: Data = ExpireProposal
  let burn: Data = BurnCritiqueToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        critique_input(0, own_critique),
      ],
      outputs: [payment_output(proposer_cred(), 25_000_000)],
      mint: assets.merge(proposal_burn(), critique_burn(0)),
      redeemers: [redeemer(proposal_utxo_ref(), expire), redeemer(critique_ref(0), burn)],
    }
  !spend_critique(own_critique, BurnCritiqueToken, critique_ref(0), tx)
}

// ============================================================================
// IncorporateCritique
// ============================================================================

fn amend_action(incorporated: List<OutputReference>) -> Data {
  let action: Data =
    AmendProposal {
      new_hash: blake2b_256("amended proposal"),
      new_uri: "ipfs://QmAmended",
      incorporated,
    }
  action
}

fn incorporated_output(value: assets.Value, address: Address) -> Output {
  Output {
    address,
    value,
    datum: InlineDatum(critique_datum(True)),
    reference_script: None,
  }
}

fn amendment_tx(
  listed: List<OutputReference>,
  inputs: List<Input>,
  outputs: List<Output>,
) -> Transaction {
  let incorporate: Data = IncorporateCritique
  Transaction {
    ..base_tx(),
    inputs: [proposal_input(default_proposal_datum(), proposal_own_ref()), ..inputs],
    outputs: outputs,
    redeemers: [
      redeemer(proposal_utxo_ref(), amend_action(listed)),
      redeemer(critique_ref(0), incorporate),
      redeemer(critique_ref(1), incorporate),
    ],
  }
}

test incorporate_critique_in_amendment() {
  let tx =
    amendment_tx(
      [critique_ref(0)],
      [critique_input(0, critique_datum(False))],
      [incorporated_output(critique_value(0), critique_address())],
    )
  spend_critique(critique_datum(False), IncorporateCritique, critique_ref(0), tx)
}

/// Rule: the transaction amends a proposal.
test incorporate_critique_without_amendment_fails() {
  let incorporate: Data = IncorporateCritique
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [critique_input(0, critique_datum(False))],
      outputs: [incorporated_output(critique_value(0), critique_address())],
      redeemers: [redeemer(critique_ref(0), incorporate)],
    }
  !spend_critique(critique_datum(False), IncorporateCritique, critique_ref(0), tx)
}

/// Rule: the amendment lists this critique.
test incorporate_critique_not_listed_fails() {
  let tx =
    amendment_tx(
      [critique_ref(7)],
      [critique_input(0, critique_datum(False))],
      [incorporated_output(critique_value(0), critique_address())],
    )
  !spend_critique(critique_datum(False), IncorporateCritique, critique_ref(0), tx)
}

/// Rule: the continuing output is at the critique address.
test incorporate_critique_output_at_other_address_fails() {
  let tx =
    amendment_tx(
      [critique_ref(0)],
      [critique_input(0, critique_datum(False))],
      [
        incorporated_output(
          critique_value(0),
          Address { payment_credential: proposer_cred(), stake_credential: None },
        ),
      ],
    )
  !spend_critique(critique_datum(False), IncorporateCritique, critique_ref(0), tx)
}

/// Rule: the continuing output keeps the value of the critique.
test incorporate_critique_with_reduced_value_fails() {
  let reduced =
    assets.from_lovelace(2_000_000)
      |> assets.add(critique_mint_hash, critique_token(0), 1)
  let tx =
    amendment_tx(
      [critique_ref(0)],
      [critique_input(0, critique_datum(False))],
      [
        incorporated_output(reduced, critique_address()),
        payment_output(proposer_cred(), 5_000_000),
      ],
    )
  !spend_critique(critique_datum(False), IncorporateCritique, critique_ref(0), tx)
}

/// Rule: one input per continuing output. Two inputs with the same address,
/// value and datum cannot share one continuing output.
test incorporate_two_equal_critiques_with_one_output_fails() {
  let plain_value = assets.from_lovelace(critique_lovelace)
  let plain_input =
    fn(n) {
      Input {
        output_reference: critique_ref(n),
        output: Output {
          address: critique_address(),
          value: plain_value,
          datum: InlineDatum(critique_datum(False)),
          reference_script: None,
        },
      }
    }
  let tx =
    amendment_tx(
      [critique_ref(0), critique_ref(1)],
      [plain_input(0), plain_input(1)],
      [incorporated_output(plain_value, critique_address())],
    )
  !spend_critique(critique_datum(False), IncorporateCritique, critique_ref(0), tx)
}

/// Rule: the critique is not yet incorporated.
test incorporate_already_incorporated_critique_fails() {
  let tx =
    amendment_tx(
      [critique_ref(0)],
      [critique_input(0, critique_datum(True))],
      [incorporated_output(critique_value(0), critique_address())],
    )
  !spend_critique(critique_datum(True), IncorporateCritique, critique_ref(0), tx)
}

// ============================================================================
// RewardCritique
// ============================================================================

fn adoption_tx(action: Data, outputs: List<Output>) -> Transaction {
  let reward: Data = RewardCritique { reward_share: 1_000_000 }
  Transaction {
    ..base_tx(),
    inputs: [
      proposal_input(default_proposal_datum(), proposal_own_ref()),
      critique_input(0, critique_datum(True)),
    ],
    outputs: outputs,
    mint: assets.merge(proposal_burn(), critique_burn(0)),
    redeemers: [redeemer(proposal_utxo_ref(), action), redeemer(critique_ref(0), reward)],
  }
}

fn adopt_action() -> Data {
  let action: Data =
    AdoptProposal {
      reasoning_hash: blake2b_256("adoption reasoning"),
      reward_amount: 100_000_000,
    }
  action
}

test reward_critique_in_adoption() {
  let tx =
    adoption_tx(
      adopt_action(),
      [payment_output(critic_cred(), critique_lovelace + 1_000_000)],
    )
  spend_critique(
    critique_datum(True),
    RewardCritique { reward_share: 1_000_000 },
    critique_ref(0),
    tx,
  )
}

/// Rule: the transaction adopts a proposal.
test reward_critique_without_adoption_fails() {
  let expire: Data = ExpireProposal
  let tx =
    adoption_tx(expire, [payment_output(critic_cred(), critique_lovelace)])
  !spend_critique(
    critique_datum(True),
    RewardCritique { reward_share: 1_000_000 },
    critique_ref(0),
    tx,
  )
}

/// Rule: the critic receives the lovelace of the critique.
test reward_critique_without_payment_to_critic_fails() {
  let tx =
    adoption_tx(adopt_action(), [payment_output(proposer_cred(), critique_lovelace)])
  !spend_critique(
    critique_datum(True),
    RewardCritique { reward_share: 1_000_000 },
    critique_ref(0),
    tx,
  )
}

// ============================================================================
// BurnEndorsementToken
// ============================================================================

fn endorsement_burn() -> assets.Value {
  assets.from_asset(critique_mint_hash, endorsement_token(), -1)
}

test burn_endorsement_settles_in_proposal_expiry() {
  let expire: Data = ExpireProposal
  let burn: Data = BurnEndorsementToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        endorsement_input(),
      ],
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: assets.merge(proposal_burn(), endorsement_burn()),
      redeemers: [redeemer(proposal_utxo_ref(), expire), redeemer(endorsement_ref(), burn)],
    }
  spend_endorsement(tx)
}

/// Rule: the transaction finalises a proposal.
test burn_endorsement_without_proposal_finalisation_fails() {
  let burn: Data = BurnEndorsementToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [endorsement_input()],
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: endorsement_burn(),
      redeemers: [redeemer(endorsement_ref(), burn)],
    }
  !spend_endorsement(tx)
}

/// Rule: one output to the owner counts once. A critique and an endorsement
/// of the same credential need the lovelace of both.
test burn_endorsement_and_critique_with_one_payment_fails() {
  let expire: Data = ExpireProposal
  let burn_endorsement: Data = BurnEndorsementToken
  let burn_critique: Data = BurnCritiqueToken
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        critique_input(0, critique_datum(False)),
        endorsement_input(),
      ],
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: proposal_burn()
        |> assets.merge(critique_burn(0))
        |> assets.merge(endorsement_burn()),
      redeemers: [
        redeemer(proposal_utxo_ref(), expire),
        redeemer(critique_ref(0), burn_critique),
        redeemer(endorsement_ref(), burn_endorsement),
      ],
    }
  !spend_endorsement(tx)
}

// ============================================================================
// Settlement through the proposal that the position names
// ============================================================================

/// A live proposal Y: not the proposal that the transaction finalises.
fn live_proposal_ref() -> OutputReference {
  make_ref(
    #"9999999999999999999999999999999999999999999999999999999999999999",
    3,
  )
}

/// The expiry of the test proposal X that settles critique 0 with `datum`,
/// signed by `signers`.
fn unrelated_expiry(datum: CritiqueDatum, signers: List<ByteArray>) -> Transaction {
  let expire: Data = ExpireProposal
  let burn: Data = BurnCritiqueToken
  Transaction {
    ..base_tx(),
    inputs: [
      proposal_input(default_proposal_datum(), proposal_own_ref()),
      critique_input(0, datum),
    ],
    outputs: [payment_output(critic_cred(), critique_lovelace)],
    mint: assets.merge(proposal_burn(), critique_burn(0)),
    redeemers: [redeemer(proposal_utxo_ref(), expire), redeemer(critique_ref(0), burn)],
    extra_signatories: signers,
  }
}

fn critique_of_live_proposal() -> CritiqueDatum {
  CritiqueDatum { ..critique_datum(True), proposal_ref: live_proposal_ref() }
}

/// Rule: the transaction finalises the proposal that the critique names. An
/// incorporated critique of a live proposal does not settle in the expiry of
/// another proposal when nobody signs.
test burn_critique_of_live_proposal_in_unrelated_expiry_fails() {
  !spend_critique(
    critique_of_live_proposal(),
    BurnCritiqueToken,
    critique_ref(0),
    unrelated_expiry(critique_of_live_proposal(), []),
  )
}

/// The way out: when the critic signs, any finalisation settles the critique.
test burn_critique_of_unrelated_proposal_signed_by_critic() {
  spend_critique(
    critique_of_live_proposal(),
    BurnCritiqueToken,
    critique_ref(0),
    unrelated_expiry(critique_of_live_proposal(), [critic_key_hash]),
  )
}

fn endorsement_of_live_proposal() -> GovernanceEndorsementDatum {
  GovernanceEndorsementDatum {
    ..endorsement_datum(),
    proposal_ref: live_proposal_ref(),
  }
}

/// The expiry of the test proposal X that settles an endorsement of the live
/// proposal Y, signed by `signers`.
fn unrelated_endorsement_expiry(signers: List<ByteArray>) -> Transaction {
  let expire: Data = ExpireProposal
  let burn: Data = BurnEndorsementToken
  let input =
    Input {
      ..endorsement_input(),
      output: Output {
        ..endorsement_input().output,
        datum: InlineDatum(endorsement_of_live_proposal()),
      },
    }
  Transaction {
    ..base_tx(),
    inputs: [proposal_input(default_proposal_datum(), proposal_own_ref()), input],
    outputs: [payment_output(critic_cred(), critique_lovelace)],
    mint: assets.merge(proposal_burn(), endorsement_burn()),
    redeemers: [redeemer(proposal_utxo_ref(), expire), redeemer(endorsement_ref(), burn)],
    extra_signatories: signers,
  }
}

fn spend_live_endorsement(tx: Transaction) -> Bool {
  let raw: Data = endorsement_of_live_proposal()
  critique.endorsement_spend.spend(
    test_config(),
    Some(raw),
    BurnEndorsementToken,
    endorsement_ref(),
    tx,
  )
}

/// Rule: the transaction finalises the proposal that the endorsement names.
test burn_endorsement_of_live_proposal_in_unrelated_expiry_fails() {
  !spend_live_endorsement(unrelated_endorsement_expiry([]))
}

/// The way out: when the endorser signs, any finalisation settles the
/// endorsement.
test burn_endorsement_of_unrelated_proposal_signed_by_endorser() {
  spend_live_endorsement(unrelated_endorsement_expiry([critic_key_hash]))
}

/// Rule: RewardCritique needs the adoption of the proposal that the critique
/// names.
test reward_critique_in_adoption_of_other_proposal_fails() {
  let reward: Data = RewardCritique { reward_share: 1_000_000 }
  let tx =
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        critique_input(0, critique_of_live_proposal()),
      ],
      outputs: [payment_output(critic_cred(), critique_lovelace + 1_000_000)],
      mint: assets.merge(proposal_burn(), critique_burn(0)),
      redeemers: [
        redeemer(proposal_utxo_ref(), adopt_action()),
        redeemer(critique_ref(0), reward),
      ],
    }
  !spend_critique(
    critique_of_live_proposal(),
    RewardCritique { reward_share: 1_000_000 },
    critique_ref(0),
    tx,
  )
}

// ============================================================================
// Incorporation: the critique names the amended proposal
// ============================================================================

/// Rule: the amended proposal is the proposal that the critique names.
test incorporate_critique_of_other_proposal_fails() {
  let datum = CritiqueDatum { ..critique_datum(False), proposal_ref: live_proposal_ref() }
  let tx =
    amendment_tx(
      [critique_ref(0)],
      [critique_input(0, datum)],
      [
        Output {
          address: critique_address(),
          value: critique_value(0),
          datum: InlineDatum(CritiqueDatum { ..datum, incorporated: True }),
          reference_script: None,
        },
      ],
    )
  !spend_critique(datum, IncorporateCritique, critique_ref(0), tx)
}

/// The bounds of the 10-minute validity range of the creation and amendment
/// transactions below.
const lo: Int = 2_000_000_000

const hi: Int = 2_000_600_000

fn ten_minutes() -> ValidityRange {
  interval.between(lo, hi)
}

/// The amendment of the test proposal (agent 1) that lists `listed`, spends
/// `critique_inputs` with IncorporateCritique, and creates `critique_outputs`
/// after the continuing proposal.
fn amendment_listing(
  listed: List<OutputReference>,
  critique_inputs: List<Input>,
  critique_outputs: List<Output>,
) -> Bool {
  let datum = default_proposal_datum()
  let incorporate: Data = IncorporateCritique
  let action =
    AmendProposal {
      new_hash: blake2b_256("amended proposal"),
      new_uri: "ipfs://QmAmended",
      incorporated: listed,
    }
  let amend: Data = action
  let amended =
    ProposalDatum {
      ..datum,
      state: Amended { previous_hash: datum.proposal_hash },
      proposal_hash: blake2b_256("amended proposal"),
      storage_uri: "ipfs://QmAmended",
      amendment_count: 1,
      incorporated_critiques: listed,
    }
  let raw: Data = datum
  proposal.proposal_spend.spend(
    test_config(),
    Some(raw),
    action,
    proposal_utxo_ref(),
    Transaction {
      ..base_tx(),
      inputs: [proposal_input(datum, proposal_own_ref()), ..critique_inputs],
      reference_inputs: [
        cross_refs_ref_input(),
        params_ref_input(),
        record_input(agent_id(1), record(1, [])),
      ],
      outputs: [proposal_output(amended), ..critique_outputs],
      extra_signatories: [proposer_key_hash],
      validity_range: ten_minutes(),
      redeemers: [
        redeemer(proposal_utxo_ref(), amend),
        ..list.map(
          critique_inputs,
          fn(input) { redeemer(input.output_reference, incorporate) },
        )
      ],
    },
  )
}

test amend_incorporating_named_critique() {
  amendment_listing(
    [critique_ref(0)],
    [critique_input(0, critique_datum(False))],
    [incorporated_output(critique_value(0), critique_address())],
  )
}

/// Rule: the transaction spends each listed critique with
/// IncorporateCritique.
test amend_listing_unspent_critique_fails() fail {
  amendment_listing(
    [critique_ref(3)],
    [],
    [incorporated_output(critique_value(0), critique_address())],
  )
}

/// Rule: the listed critiques are distinct.
test amend_listing_critique_twice_fails() fail {
  amendment_listing(
    [critique_ref(0), critique_ref(0)],
    [critique_input(0, critique_datum(False))],
    [incorporated_output(critique_value(0), critique_address())],
  )
}

// ============================================================================
// Critique creation: the new output, the named proposal, the time and the
// parties
// ============================================================================

/// The seed of the critique submissions.
fn critique_seed() -> OutputReference {
  critique_ref(9)
}

/// The seed of another proposal.
fn other_proposal_seed() -> OutputReference {
  make_ref(
    #"4444444444444444444444444444444444444444444444444444444444444444",
    0,
  )
}

/// A Supportive critique by agent `n` (controller credential) of the proposal
/// that `proposal_ref` names, submitted at `lo`.
fn critique_by(n: Int, proposal_ref: OutputReference) -> CritiqueDatum {
  CritiqueDatum {
    critic_did: agent_id(n),
    critic_credential: VerificationKey(controller_hash(n)),
    proposal_ref,
    critique_hash: blake2b_256("critique"),
    storage_uri: "ipfs://QmCritique",
    critique_type: Supportive,
    stake_amount: 5_000_000,
    submitted_at: lo,
    incorporated: False,
  }
}

/// An open proposal of agent `proposer` that `seed` names (it holds the
/// proposal token of `seed`), as a reference input with output index `index`.
fn named_proposal_input(seed: OutputReference, proposer: Int, index: Int) -> Input {
  let datum: Data =
    ProposalDatum {
      ..default_proposal_datum(),
      proposer_did: agent_id(proposer),
      proposer_credential: VerificationKey(controller_hash(proposer)),
    }
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0006",
      index,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(25_000_000)
        |> assets.add(proposal_mint_hash, utils.proposal_token_name(seed), 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The test proposal of agent 1, named by proposal_own_ref.
fn test_proposal() -> Input {
  named_proposal_input(proposal_own_ref(), 1, 0)
}

/// The authentic record input of agent `n` with the record `r`.
fn record_of(n: Int, r: IdentityDatum) -> Input {
  record_input(agent_id(n), r)
}

/// A critique submission: the seed holds `spent`; the transaction creates
/// `outputs`, mints the critique token of the seed, reads `proposals` and
/// `records`, and `signers` sign.
fn critique_submission(
  spent: CritiqueDatum,
  outputs: List<Output>,
  proposals: List<Input>,
  records: List<Input>,
  signers: List<ByteArray>,
) -> Transaction {
  let mint: Data = MintCritiqueToken
  Transaction {
    ..transaction.placeholder,
    inputs: [critique_seed_input(critique_seed(), spent)],
    reference_inputs: list.concat(
      [cross_refs_ref_input(), params_ref_input(), ..proposals],
      records,
    ),
    outputs: outputs,
    mint: assets.from_asset(
      critique_mint_hash,
      utils.critique_token_name(critique_seed()),
      1,
    ),
    extra_signatories: signers,
    validity_range: ten_minutes(),
    redeemers: [redeemer(critique_seed(), mint)],
  }
}

fn submit_critique(spent: CritiqueDatum, tx: Transaction) -> Bool {
  spend_critique(spent, MintCritiqueToken, critique_seed(), tx)
}

/// The records of the critic (agent 2) and of the proposer (agent 1).
fn usual_records() -> List<Input> {
  [record_of(2, record(2, [])), record_of(1, record(1, []))]
}

/// Critique `datum` of agent 2 with its new output, the test proposal and
/// both records; the critic signs.
fn usual_critique_tx(datum: CritiqueDatum) -> Transaction {
  critique_submission(
    datum,
    [critique_output(critique_seed(), datum)],
    [test_proposal()],
    usual_records(),
    [controller_hash(2)],
  )
}

test submit_critique_of_named_proposal() {
  let datum = critique_by(2, proposal_own_ref())
  submit_critique(datum, usual_critique_tx(datum))
}

/// Rule: the proposal that proposal_ref names is a reference input. Another
/// open proposal does not count.
test submit_critique_names_missing_proposal_fails() fail {
  let datum = critique_by(2, other_proposal_seed())
  submit_critique(datum, usual_critique_tx(datum))
}

/// Rule: the parties rule reads the proposal that proposal_ref names, here a
/// proposal of the critic itself, not the first proposal reference input.
test submit_critique_self_check_reads_named_proposal_fails() fail {
  let datum = critique_by(2, other_proposal_seed())
  submit_critique(
    datum,
    critique_submission(
      datum,
      [critique_output(critique_seed(), datum)],
      [test_proposal(), named_proposal_input(other_proposal_seed(), 2, 1)],
      usual_records(),
      [controller_hash(2)],
    ),
  )
}

/// Rule: the checks read the new critique; its credential is the credential
/// of the spent UTxO.
test submit_critique_new_output_with_other_credential_fails() fail {
  let spent = critique_by(2, proposal_own_ref())
  let new =
    CritiqueDatum { ..spent, critic_credential: VerificationKey(controller_hash(5)) }
  submit_critique(
    spent,
    critique_submission(
      spent,
      [critique_output(critique_seed(), new)],
      [test_proposal()],
      usual_records(),
      [controller_hash(2)],
    ),
  )
}

/// Rule: the transaction creates the critique with its token.
test submit_critique_without_new_output_fails() fail {
  let spent = critique_by(2, proposal_own_ref())
  submit_critique(
    spent,
    Transaction {
      ..usual_critique_tx(spent),
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: assets.zero,
    },
  )
}

/// Rule: the new critique is at the address of the spent UTxO.
test submit_critique_new_output_at_wallet_fails() fail {
  let spent = critique_by(2, proposal_own_ref())
  let output = critique_output(critique_seed(), spent)
  submit_critique(
    spent,
    Transaction {
      ..usual_critique_tx(spent),
      outputs: [
        Output {
          ..output,
          address: Address { payment_credential: critic_cred(), stake_credential: None },
        },
      ],
    },
  )
}

/// Rule: the new critique holds at least the stake.
test submit_critique_new_output_below_stake_fails() fail {
  let spent = critique_by(2, proposal_own_ref())
  let output = critique_output(critique_seed(), spent)
  submit_critique(
    spent,
    Transaction {
      ..usual_critique_tx(spent),
      outputs: [
        Output {
          ..output,
          value: assets.from_lovelace(1_000_000)
            |> assets.add(
                critique_mint_hash,
                utils.critique_token_name(critique_seed()),
                1,
              ),
        },
      ],
    },
  )
}

/// Rule: the new critique names the critic of the spent UTxO, so the funds
/// of another agent's UTxO stay with it.
test submit_critique_with_seed_of_other_owner_fails() fail {
  let spent = critique_by(5, proposal_own_ref())
  let new = critique_by(2, proposal_own_ref())
  submit_critique(
    spent,
    critique_submission(
      spent,
      [critique_output(critique_seed(), new)],
      [test_proposal()],
      usual_records(),
      [controller_hash(2)],
    ),
  )
}

/// Rule: submitted_at is the lower bound of the validity range.
test submit_critique_submitted_before_lower_bound_fails() fail {
  let datum = CritiqueDatum { ..critique_by(2, proposal_own_ref()), submitted_at: lo - 1 }
  submit_critique(datum, usual_critique_tx(datum))
}

/// A Supportive critique of the test proposal (agent 1) by agent 2 with the
/// record `critic`, the credential `key_hash` (which signs), the proposer's
/// record `proposer` (when Some) and the records `extra`.
fn critique_parties(
  critic: IdentityDatum,
  key_hash: ByteArray,
  proposer: Option<IdentityDatum>,
  extra: List<Input>,
) -> Bool {
  let datum =
    CritiqueDatum {
      ..critique_by(2, proposal_own_ref()),
      critic_credential: VerificationKey(key_hash),
    }
  let proposer_records =
    when proposer is {
      Some(r) -> [record_of(1, r)]
      None -> []
    }
  submit_critique(
    datum,
    critique_submission(
      datum,
      [critique_output(critique_seed(), datum)],
      [test_proposal()],
      [record_of(2, critic), ..list.concat(proposer_records, extra)],
      [key_hash],
    ),
  )
}

/// The record of agent `n` as a subagent of agent `parent`.
fn child_of(n: Int, parent: Int) -> IdentityDatum {
  subagent(record(n, []), agent_id(parent), 0, 1)
}

/// The record of agent 2 with the controller key of agent 1 and the listed
/// transaction key ed_key(2).
fn same_controller_record() -> IdentityDatum {
  IdentityDatum { ..record(2, []), controller: controller_key(1) }
    |> with_transaction_key(2)
}

/// The record of agent 2 that lists the controller key of agent 1 as a
/// transaction key.
fn shared_key_record() -> IdentityDatum {
  record(2, [])
    |> with_key(
        Ed25519,
        controller_key(1),
        purpose_authentication + purpose_transaction,
      )
}

test submit_critique_by_subagent_of_other_parent() {
  critique_parties(
    child_of(2, 9),
    controller_hash(2),
    Some(record(1, [])),
    [record_of(9, record(9, []))],
  )
}

/// Rule: the two records have different controller keys.
test submit_critique_by_same_controller_fails() fail {
  critique_parties(same_controller_record(), ed_key_hash(2), Some(record(1, [])), [])
}

/// Rule: the critic credential is not the proposer credential.
test submit_critique_with_proposer_credential_fails() fail {
  critique_parties(shared_key_record(), controller_hash(1), Some(record(1, [])), [])
}

/// Rule: the critic is not a subagent of the proposer.
test submit_critique_by_subagent_of_proposer_fails() fail {
  critique_parties(child_of(2, 1), controller_hash(2), Some(record(1, [])), [])
}

/// Rule: the proposer is not a subagent of the critic.
test submit_critique_of_subagent_proposer_fails() fail {
  critique_parties(record(2, []), controller_hash(2), Some(child_of(1, 2)), [])
}

/// Rule: the critic and the proposer are not subagents of the same parent.
test submit_critique_by_sibling_subagent_fails() fail {
  critique_parties(
    child_of(2, 9),
    controller_hash(2),
    Some(child_of(1, 9)),
    [record_of(9, record(9, []))],
  )
}

/// Rule: the proposer's record is a reference input.
test submit_critique_without_proposer_record_fails() fail {
  critique_parties(record(2, []), controller_hash(2), None, [])
}

// ============================================================================
// Endorsement creation: the new output, the named proposal, the time and the
// parties
// ============================================================================

/// An endorsement by agent `n` (controller credential) of the proposal that
/// `proposal_ref` names, created at `lo`.
fn endorsement_by(n: Int, proposal_ref: OutputReference) -> GovernanceEndorsementDatum {
  GovernanceEndorsementDatum {
    endorser_did: agent_id(n),
    endorser_credential: VerificationKey(controller_hash(n)),
    proposal_ref,
    stake_amount: 5_000_000,
    created_at: lo,
  }
}

fn endorsement_address() -> Address {
  Address {
    payment_credential: Script(endorsement_validator_hash),
    stake_credential: None,
  }
}

/// The new endorsement with `datum`: 7 AP3X and its endorsement token.
fn endorsement_output(datum: GovernanceEndorsementDatum) -> Output {
  Output {
    address: endorsement_address(),
    value: assets.from_lovelace(critique_lovelace)
      |> assets.add(
          critique_mint_hash,
          utils.endorsement_token_name(datum.endorser_did, datum.proposal_ref),
          1,
        ),
    datum: InlineDatum(datum),
    reference_script: None,
  }
}

/// An endorsement submission: the seed (endorsement_ref) holds `spent`; the
/// transaction creates `outputs`, mints the endorsement token of `spent`,
/// reads `proposals` and `records`, and `signers` sign.
fn endorsement_submission(
  spent: GovernanceEndorsementDatum,
  outputs: List<Output>,
  proposals: List<Input>,
  records: List<Input>,
  signers: List<ByteArray>,
) -> Transaction {
  let mint: Data = MintEndorsementToken
  Transaction {
    ..transaction.placeholder,
    inputs: [
      Input {
        output_reference: endorsement_ref(),
        output: Output {
          address: endorsement_address(),
          value: assets.from_lovelace(critique_lovelace),
          datum: InlineDatum(spent),
          reference_script: None,
        },
      },
    ],
    reference_inputs: list.concat(
      [cross_refs_ref_input(), params_ref_input(), ..proposals],
      records,
    ),
    outputs: outputs,
    mint: assets.from_asset(
      critique_mint_hash,
      utils.endorsement_token_name(spent.endorser_did, spent.proposal_ref),
      1,
    ),
    extra_signatories: signers,
    validity_range: ten_minutes(),
    redeemers: [redeemer(endorsement_ref(), mint)],
  }
}

fn submit_endorsement(
  spent: GovernanceEndorsementDatum,
  tx: Transaction,
) -> Bool {
  let raw: Data = spent
  critique.endorsement_spend.spend(
    test_config(),
    Some(raw),
    MintEndorsementToken,
    endorsement_ref(),
    tx,
  )
}

/// Endorsement `datum` of agent 2 with its new output, the test proposal and
/// both records; the endorser signs.
fn usual_endorsement_tx(datum: GovernanceEndorsementDatum) -> Transaction {
  endorsement_submission(
    datum,
    [endorsement_output(datum)],
    [test_proposal()],
    usual_records(),
    [controller_hash(2)],
  )
}

test submit_endorsement_of_named_proposal() {
  let datum = endorsement_by(2, proposal_own_ref())
  submit_endorsement(datum, usual_endorsement_tx(datum))
}

/// Rule: the proposal that proposal_ref names is a reference input.
test submit_endorsement_names_missing_proposal_fails() fail {
  let datum = endorsement_by(2, other_proposal_seed())
  submit_endorsement(datum, usual_endorsement_tx(datum))
}

/// Rule: the parties rule reads the proposal that proposal_ref names.
test submit_endorsement_self_check_reads_named_proposal_fails() fail {
  let datum = endorsement_by(2, other_proposal_seed())
  submit_endorsement(
    datum,
    endorsement_submission(
      datum,
      [endorsement_output(datum)],
      [test_proposal(), named_proposal_input(other_proposal_seed(), 2, 1)],
      usual_records(),
      [controller_hash(2)],
    ),
  )
}

/// Rule: the checks read the new endorsement; its credential is the
/// credential of the spent UTxO.
test submit_endorsement_new_output_with_other_credential_fails() fail {
  let spent = endorsement_by(2, proposal_own_ref())
  let new =
    GovernanceEndorsementDatum {
      ..spent,
      endorser_credential: VerificationKey(controller_hash(5)),
    }
  submit_endorsement(
    spent,
    endorsement_submission(
      spent,
      [endorsement_output(new)],
      [test_proposal()],
      usual_records(),
      [controller_hash(2)],
    ),
  )
}

/// Rule: the transaction creates the endorsement with its token.
test submit_endorsement_without_new_output_fails() fail {
  let spent = endorsement_by(2, proposal_own_ref())
  submit_endorsement(
    spent,
    Transaction {
      ..usual_endorsement_tx(spent),
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: assets.zero,
    },
  )
}

/// Rule: created_at is the lower bound of the validity range.
test submit_endorsement_created_after_lower_bound_fails() fail {
  let datum =
    GovernanceEndorsementDatum {
      ..endorsement_by(2, proposal_own_ref()),
      created_at: lo + 60_000,
    }
  submit_endorsement(datum, usual_endorsement_tx(datum))
}

/// An endorsement of the test proposal (agent 1) by agent 2 with the record
/// `endorser`, the credential `key_hash` (which signs), the proposer's record
/// `proposer` (when Some) and the records `extra`.
fn endorsement_parties(
  endorser: IdentityDatum,
  key_hash: ByteArray,
  proposer: Option<IdentityDatum>,
  extra: List<Input>,
) -> Bool {
  let datum =
    GovernanceEndorsementDatum {
      ..endorsement_by(2, proposal_own_ref()),
      endorser_credential: VerificationKey(key_hash),
    }
  let proposer_records =
    when proposer is {
      Some(r) -> [record_of(1, r)]
      None -> []
    }
  submit_endorsement(
    datum,
    endorsement_submission(
      datum,
      [endorsement_output(datum)],
      [test_proposal()],
      [record_of(2, endorser), ..list.concat(proposer_records, extra)],
      [key_hash],
    ),
  )
}

test submit_endorsement_by_subagent_of_other_parent() {
  endorsement_parties(
    child_of(2, 9),
    controller_hash(2),
    Some(record(1, [])),
    [record_of(9, record(9, []))],
  )
}

test submit_endorsement_by_same_controller_fails() fail {
  endorsement_parties(same_controller_record(), ed_key_hash(2), Some(record(1, [])), [])
}

test submit_endorsement_with_proposer_credential_fails() fail {
  endorsement_parties(shared_key_record(), controller_hash(1), Some(record(1, [])), [])
}

test submit_endorsement_by_subagent_of_proposer_fails() fail {
  endorsement_parties(child_of(2, 1), controller_hash(2), Some(record(1, [])), [])
}

test submit_endorsement_of_subagent_proposer_fails() fail {
  endorsement_parties(record(2, []), controller_hash(2), Some(child_of(1, 2)), [])
}

test submit_endorsement_by_sibling_subagent_fails() fail {
  endorsement_parties(
    child_of(2, 9),
    controller_hash(2),
    Some(child_of(1, 9)),
    [record_of(9, record(9, []))],
  )
}

test submit_endorsement_without_proposer_record_fails() fail {
  endorsement_parties(record(2, []), controller_hash(2), None, [])
}

// ============================================================================
// Critique mint policy: exact token names
// ============================================================================

fn critique_mint(tx: Transaction) -> Bool {
  let unused: Data = MintCritiqueToken
  critique.critique_mint.mint(test_config(), unused, critique_mint_hash, tx)
}

/// The valid critique submission with the mint `mint`.
fn critique_minting(mint: assets.Value) -> Transaction {
  Transaction {
    ..usual_critique_tx(critique_by(2, proposal_own_ref())),
    mint: mint,
  }
}

test critique_mint_of_new_critique() {
  critique_mint(usual_critique_tx(critique_by(2, proposal_own_ref())))
}

/// Rule: the name is the critique token of the seed.
test critique_mint_with_other_name_fails() fail {
  critique_mint(
    critique_minting(
      assets.from_asset(
        critique_mint_hash,
        utils.critique_token_name(critique_ref(7)),
        1,
      ),
    ),
  )
}

/// Rule: no token besides the tokens of the new positions.
test critique_mint_with_extra_token_fails() fail {
  critique_mint(
    critique_minting(
      assets.from_asset(
        critique_mint_hash,
        utils.critique_token_name(critique_seed()),
        1,
      )
        |> assets.add(
            critique_mint_hash,
            utils.critique_token_name(critique_ref(7)),
            1,
          ),
    ),
  )
}

/// Rule: quantity 1.
test critique_mint_two_units_fails() fail {
  critique_mint(
    critique_minting(
      assets.from_asset(
        critique_mint_hash,
        utils.critique_token_name(critique_seed()),
        2,
      ),
    ),
  )
}

/// The policy finds the endorsement input (GovernanceCrossRefs field
/// endorsement_validator_hash); no critique input is needed.
test endorsement_mint_of_new_endorsement() {
  critique_mint(usual_endorsement_tx(endorsement_by(2, proposal_own_ref())))
}

/// Rule: the name is the endorsement token of the endorser and the proposal.
/// The transaction also spends a critique, so a critique input is present.
test endorsement_mint_with_other_name_fails() fail {
  let withdraw: Data = WithdrawCritique
  let tx = usual_endorsement_tx(endorsement_by(2, proposal_own_ref()))
  critique_mint(
    Transaction {
      ..tx,
      inputs: [critique_input(0, critique_datum(False)), ..tx.inputs],
      mint: assets.from_asset(
        critique_mint_hash,
        utils.endorsement_token_name(agent_id(5), proposal_own_ref()),
        1,
      ),
      redeemers: [redeemer(critique_ref(0), withdraw), ..tx.redeemers],
    },
  )
}

/// A settlement of an endorsement alone burns its token: the policy accepts
/// the endorsement input.
test critique_mint_burn_in_endorsement_settlement() {
  let expire: Data = ExpireProposal
  let burn: Data = BurnEndorsementToken
  critique_mint(
    Transaction {
      ..base_tx(),
      inputs: [
        proposal_input(default_proposal_datum(), proposal_own_ref()),
        endorsement_input(),
      ],
      outputs: [payment_output(critic_cred(), critique_lovelace)],
      mint: assets.merge(proposal_burn(), endorsement_burn()),
      redeemers: [
        redeemer(proposal_utxo_ref(), expire),
        redeemer(endorsement_ref(), burn),
      ],
    },
  )
}
