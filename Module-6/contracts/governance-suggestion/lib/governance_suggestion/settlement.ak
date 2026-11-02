// Module 6: Self-Improvement Module — Settlement Rules
// Rules that bind a spend to a proposal action in the same transaction, that
// continue a UTxO, and that return stakes to their owners.

use aiken/builtin
use aiken/collection/dict
use aiken/collection/list
use aiken/collection/pairs
use cardano/address.{Credential, Script as ScriptCredential}
use cardano/assets.{PolicyId}
use cardano/transaction.{
  DatumHash, InlineDatum, Input, NoDatum, Output, OutputReference, Spend,
  Transaction,
}
use governance_suggestion/types.{
  AmendProposal, GovernanceCrossRefs, ProposalAction,
}
use governance_suggestion/utils
use shared/credential

// ============================================================================
// Constructor indices of the redeemers (types.ak). The constructor order is
// part of the on-chain format; the test `redeemer_indices_match_types`
// checks these values.
// ============================================================================

/// ProposalAction.SubmitProposal
pub const submit_index: Int = 0

/// ProposalAction.WithdrawProposal
pub const withdraw_index: Int = 1

/// ProposalAction.AdoptProposal
pub const adopt_index: Int = 3

/// ProposalAction.RejectProposal
pub const reject_index: Int = 4

/// ProposalAction.ExpireProposal
pub const expire_index: Int = 5

/// ProposalAction.ExpireStaleProposal
pub const expire_stale_index: Int = 6

/// ProposalAction.SpendActivity
pub const spend_activity_index: Int = 8

/// CritiqueAction.MintCritiqueToken and
/// GovernanceEndorsementAction.MintEndorsementToken
pub const mint_index: Int = 0

/// CritiqueAction.RewardCritique
pub const reward_critique_index: Int = 3

/// CritiqueAction.BurnCritiqueToken
pub const burn_critique_index: Int = 4

/// GovernanceEndorsementAction.BurnEndorsementToken
pub const burn_endorsement_index: Int = 2

// ============================================================================
// Inputs, redeemers and datums
// ============================================================================

/// The input that `own_ref` spends. Fails when the transaction does not spend it.
pub fn own_input(tx: Transaction, own_ref: OutputReference) -> Input {
  expect Some(input) =
    list.find(tx.inputs, fn(input) { input.output_reference == own_ref })
  input
}

/// True when the input is at the script `hash`.
pub fn is_at_script(input: Input, hash: ByteArray) -> Bool {
  when input.output.address.payment_credential is {
    ScriptCredential(h) -> h == hash
    _ -> False
  }
}

/// The spend redeemer of an input. Fails when the input has none.
pub fn spend_redeemer(tx: Transaction, input: Input) -> Data {
  expect Some(redeemer) =
    pairs.get_first(tx.redeemers, Spend(input.output_reference))
  redeemer
}

/// The datum of an input, inline or by hash. Fails when the input has none.
pub fn input_datum(tx: Transaction, input: Input) -> Data {
  when input.output.datum is {
    InlineDatum(raw) -> raw
    DatumHash(hash) -> {
      expect Some(raw) = dict.get(tx.datums, hash)
      raw
    }
    NoDatum -> fail @"input without datum"
  }
}

/// The constructor index of a Plutus Data constructor. Fails for other data.
/// The validator of the input decodes the full redeemer or datum, so the
/// index is enough here.
pub fn constr_index(data: Data) -> Int {
  builtin.fst_pair(builtin.un_constr_data(data))
}

/// The first field of a datum: the agent identifier of ProposalDatum,
/// CritiqueDatum and GovernanceEndorsementDatum.
pub fn did_field(datum: Data) -> ByteArray {
  builtin.un_b_data(builtin.head_list(builtin.snd_pair(builtin.un_constr_data(datum))))
}

/// The second field of a datum: the owner credential of ProposalDatum,
/// CritiqueDatum and GovernanceEndorsementDatum.
fn credential_field(datum: Data) -> Data {
  builtin.head_list(
    builtin.tail_list(builtin.snd_pair(builtin.un_constr_data(datum))),
  )
}

// ============================================================================
// Proposal actions in the same transaction
// ============================================================================

/// True for the redeemer index of a proposal action that finalises a
/// proposal: withdrawal, adoption, rejection, expiration and stale
/// expiration. Each of them burns the proposal token.
pub fn is_finalising(index: Int) -> Bool {
  or {
    index == withdraw_index,
    index == adopt_index,
    index == reject_index,
    index == expire_index,
    index == expire_stale_index,
  }
}

/// True when the input is at the proposal validator and is spent with an
/// action other than SpendActivity. An input without a redeemer counts as an
/// action (a transaction always has the redeemer of a script input).
pub fn is_proposal_action(
  refs: GovernanceCrossRefs,
  input: Input,
  tx: Transaction,
) -> Bool {
  is_at_script(input, refs.proposal_validator_hash) && when
    pairs.get_first(tx.redeemers, Spend(input.output_reference))
  is {
    Some(redeemer) -> constr_index(redeemer) != spend_activity_index
    None -> True
  }
}

/// True when the transaction finalises the proposal that `proposal_ref`
/// names: a proposal input that holds the proposal token
/// utils.proposal_token_name(proposal_ref) is spent with a finalising action.
///
/// When `owner` signs, the finalisation of any proposal is enough. This is the
/// way out for a critique or endorsement whose proposal ended without it.
pub fn settles(
  refs: GovernanceCrossRefs,
  proposal_ref: OutputReference,
  owner: Credential,
  tx: Transaction,
) -> Bool {
  let token = utils.proposal_token_name(proposal_ref)
  let signed = credential.verify_signer(owner, tx.extra_signatories)
  list.any(
    tx.inputs,
    fn(input) {
      and {
        is_at_script(input, refs.proposal_validator_hash),
        is_finalising(constr_index(spend_redeemer(tx, input))),
        signed || assets.quantity_of(
          input.output.value,
          refs.proposal_mint_hash,
          token,
        ) == 1,
      }
    },
  )
}

/// True when the transaction adopts the proposal that `proposal_ref` names:
/// a proposal input that holds the proposal token
/// utils.proposal_token_name(proposal_ref) is spent with AdoptProposal.
pub fn adopts(
  refs: GovernanceCrossRefs,
  proposal_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let token = utils.proposal_token_name(proposal_ref)
  list.any(
    tx.inputs,
    fn(input) {
      and {
        is_at_script(input, refs.proposal_validator_hash),
        constr_index(spend_redeemer(tx, input)) == adopt_index,
        assets.quantity_of(input.output.value, refs.proposal_mint_hash, token) == 1,
      }
    },
  )
}

/// The proposal input whose AmendProposal lists `critique_ref` in its
/// incorporated critiques. None when the transaction has no such amendment.
pub fn amendment_of(
  refs: GovernanceCrossRefs,
  tx: Transaction,
  critique_ref: OutputReference,
) -> Option<Input> {
  list.find(
    tx.inputs,
    fn(input) {
      if is_at_script(input, refs.proposal_validator_hash) {
        expect action: ProposalAction = spend_redeemer(tx, input)
        when action is {
          AmendProposal { incorporated, .. } ->
            list.has(incorporated, critique_ref)
          _ -> False
        }
      } else {
        False
      }
    },
  )
}

// ============================================================================
// Continuing outputs
// ============================================================================

/// True when an output continues `input`: the same address (payment and
/// stake credential), the same tokens, at least the same AP3X, and the inline
/// datum `datum`. A token that only `input` holds makes the output unique.
pub fn continues(input: Input, datum: Data, outputs: List<Output>) -> Bool {
  let value: Data = input.output.value
  list.any(
    outputs,
    fn(output) {
      and {
        output.address == input.output.address,
        assets.match(output.value, value, >=),
        output.datum == InlineDatum(datum),
      }
    },
  )
}

// ============================================================================
// Token burn
// ============================================================================

/// True when every token of `policy` that `input` holds is burned: for each
/// such asset name, the transaction burns all units that its inputs hold.
pub fn burns_held_tokens(
  policy: PolicyId,
  input: Input,
  tx: Transaction,
) -> Bool {
  dict.foldl(
    assets.tokens(input.output.value, policy),
    True,
    fn(name, _quantity, acc) {
      let held =
        list.foldl(
          tx.inputs,
          0,
          fn(i, total) {
            total + assets.quantity_of(i.output.value, policy, name)
          },
        )
      acc && assets.quantity_of(tx.mint, policy, name) == -held
    },
  )
}

// ============================================================================
// Stake return
// ============================================================================

/// True when the outputs to `owner` hold at least the lovelace that the
/// transaction returns to `owner`.
///
/// The transaction returns to `owner` the lovelace of each input that it
/// spends with a settling action and whose datum names `owner` (second
/// datum field):
///   - critique inputs spent with RewardCritique or BurnCritiqueToken,
///   - endorsement inputs spent with BurnEndorsementToken,
///   - proposal inputs spent with a finalising action.
/// The outputs to `owner` are summed, so one output cannot count for two
/// inputs.
pub fn owner_paid(
  refs: GovernanceCrossRefs,
  owner: Credential,
  tx: Transaction,
) -> Bool {
  let paid =
    list.foldl(
      tx.outputs,
      0,
      fn(output: Output, total) {
        if output.address.payment_credential == owner {
          total + assets.lovelace_of(output.value)
        } else {
          total
        }
      },
    )
  paid >= owed_to(refs, owner, tx)
}

/// The lovelace that the transaction returns to `owner` (see owner_paid).
fn owed_to(
  refs: GovernanceCrossRefs,
  owner: Credential,
  tx: Transaction,
) -> Int {
  let owner_data: Data = owner
  list.foldl(
    tx.inputs,
    0,
    fn(input, total) {
      let settled =
        if is_at_script(input, refs.critique_validator_hash) {
          let index = constr_index(spend_redeemer(tx, input))
          index == reward_critique_index || index == burn_critique_index
        } else if is_at_script(input, refs.proposal_validator_hash) {
          is_finalising(constr_index(spend_redeemer(tx, input)))
        } else if is_at_script(input, refs.endorsement_validator_hash) {
          constr_index(spend_redeemer(tx, input)) == burn_endorsement_index
        } else {
          False
        }
      if settled && credential_field(input_datum(tx, input)) == owner_data {
        total + assets.lovelace_of(input.output.value)
      } else {
        total
      }
    },
  )
}
