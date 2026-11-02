// Module 6: Critique and endorsement spend tests (validator level).
//
// Each test builds a full transaction and calls the spend handler of the
// critique or endorsement validator. Negative tests name the rule that the
// transaction does not meet.

use aiken/crypto.{blake2b_256}
use cardano/address.{Address, Script}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, ScriptPurpose, Spend,
  Transaction,
}
use critique
use governance_suggestion/tests/test_helpers.{
  critic_cred, critic_did, critique_mint_hash, critique_validator_hash,
  cross_refs_ref_input, default_proposal_datum, make_ref, params_ref_input,
  payment_output, proposal_input, proposal_mint_hash, proposal_own_ref,
  proposer_cred, test_config,
}
use governance_suggestion/types.{
  AdoptProposal, AmendProposal, BurnCritiqueToken, BurnEndorsementToken,
  CritiqueDatum, ExpireProposal, GovernanceEndorsementDatum,
  IncorporateCritique, RewardCritique, Supportive,
}
use governance_suggestion/utils

// ============================================================================
// Fixtures
// ============================================================================

const endorsement_validator_hash: ByteArray =
  #"ab000000000000000000000000000000000000000000000000000ab0"

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
