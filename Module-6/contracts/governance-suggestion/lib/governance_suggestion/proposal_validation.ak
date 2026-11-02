// Module 6: Self-Improvement Module — Proposal Validation Logic
// Spec reference: §5.1–5.6c

use aiken/builtin
use aiken/collection/dict
use aiken/collection/list
use aiken/collection/pairs
use cardano/address.{Script as ScriptCredential}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Spend, Transaction,
}
use governance_suggestion/activity_tracking
use governance_suggestion/emergency
use governance_suggestion/params as gov_params
use governance_suggestion/settlement
use governance_suggestion/types.{
  Amended, Emergency, GameActivation, GeneralSuggestion, GovernanceConfig,
  GovernanceCrossRefs, GovernanceParams, IncorporateCritique, Open,
  ParameterChange, ProposalDatum, ProposalType, ProposerActivityDatum,
  ProtocolUpgrade, Standard, TreasurySpend,
}
use governance_suggestion/utils
use shared/identity

// ============================================================================
// One proposal action per transaction
// ============================================================================

/// True when exactly one input at the proposal validator is spent with an
/// action other than SpendActivity (settlement.is_proposal_action). Every
/// proposal_spend redeemer needs it, so a transaction submits, amends,
/// extends or finalises one proposal, and spends at most the activity UTxO
/// of that proposal's agent besides it.
pub fn one_proposal_action(refs: GovernanceCrossRefs, tx: Transaction) -> Bool {
  list.count(
    tx.inputs,
    fn(input) { settlement.is_proposal_action(refs, input, tx) },
  ) == 1
}

// ============================================================================
// Activity UTxO (SpendActivity)
// ============================================================================

/// Validate the spend of an activity UTxO.
///
/// MUST:
///   1. The datum is a ProposerActivityDatum, and the input holds, under the
///      proposal policy, exactly one token: the activity token of its agent
///      (utils.activity_token_name(agent_did)), quantity 1.
///   2. The one proposal action of the transaction (one_proposal_action) is
///      SubmitProposal or a finalisation, and the datum of its input names
///      the same agent (proposer_did == agent_did).
/// The owner authorises the move through that action: SubmitProposal needs
/// the proposer's identity check and signature, WithdrawProposal the
/// proposer's signature, AdoptProposal and RejectProposal the oracle, and
/// ExpireProposal and ExpireStaleProposal only lower the count. That action
/// checks the continuing activity output (activity_tracking): the same
/// address, the same tokens, at least the same AP3X, and the new count.
pub fn validate_spend_activity(
  refs: GovernanceCrossRefs,
  raw_datum: Data,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  expect activity: ProposerActivityDatum = raw_datum
  let own = settlement.own_input(tx, own_ref)
  expect Some(action_input) =
    list.find(
      tx.inputs,
      fn(input) { settlement.is_proposal_action(refs, input, tx) },
    )
  let index =
    settlement.constr_index(settlement.spend_redeemer(tx, action_input))
  and {
    dict.to_pairs(assets.tokens(own.output.value, refs.proposal_mint_hash)) == [
      Pair(utils.activity_token_name(activity.agent_did), 1),
    ],
    index == settlement.submit_index || settlement.is_finalising(index),
    settlement.did_field(settlement.input_datum(tx, action_input)) == activity.agent_did,
  }
}

// ============================================================================
// §5.1 Proposal Submission
// ============================================================================

/// Validate a new proposal submission.
///
/// The transaction spends `own_ref`, a UTxO at the proposal validator with the
/// datum `spent`, and creates the new proposal: the output that holds the new
/// proposal token utils.proposal_token_name(own_ref). Every check reads the
/// datum of that output (the new datum).
///
/// MUST:
///   1. The validity range has finite bounds and is at most one hour long
///      (shared/identity.window), and the proposer passes the identity check
///      (utils.verify_active_agent) with the new datum.
///   2. The new datum names the proposer and the credential of `spent`, so
///      the locked funds stay with their owner.
///   3. submitted_at is the lower bound of the validity range.
///   4. Stake, hash, type, URI, review window, state Open, no amendment and
///      no incorporated critique (§5.1, §5.1b).
///   5. Rate limit and activity record, and the exact tokens under the
///      proposal policy (activity_tracking.validate_submit_activity).
///   6. The new proposal is at the address of `own_ref`, holds at least the
///      stake, and holds exactly one token: the new proposal token.
pub fn validate_submit_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  spent: ProposalDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  let token = utils.proposal_token_name(own_ref)
  expect Some(output) =
    list.find(
      tx.outputs,
      fn(output) {
        assets.quantity_of(output.value, refs.proposal_mint_hash, token) == 1
      },
    )
  expect InlineDatum(raw) = output.datum
  expect datum: ProposalDatum = raw
  when identity.window(tx.validity_range) is {
    Some(w) -> {
      let identity_valid =
        utils.verify_active_agent(
          config,
          datum.proposer_did,
          datum.proposer_credential,
          w,
          tx.reference_inputs,
          tx.extra_signatories,
        )
      let owner_valid =
        datum.proposer_did == spent.proposer_did && datum.proposer_credential == spent.proposer_credential
      let stake_valid =
        validate_stake_for_priority(config, params, datum, tx.reference_inputs)
      let fields_valid =
        and {
          builtin.length_of_bytearray(datum.proposal_hash) == 32,
          validate_proposal_type(params, datum.proposal_type),
          builtin.length_of_bytearray(datum.storage_uri) > 0,
          validate_review_window(params, datum),
          datum.state == Open,
          datum.amendment_count == 0,
          datum.incorporated_critiques == [],
        }
      let output_valid =
        and {
          output.address == own.output.address,
          utils.lovelace_of(output.value) >= datum.stake_amount,
          assets.without_lovelace(output.value) == assets.from_asset(
            refs.proposal_mint_hash,
            token,
            1,
          ),
        }
      if !identity_valid {
        fail @"FAIL:identity_valid"
      } else if !owner_valid {
        fail @"FAIL:spent_owner"
      } else if datum.submitted_at != w.lo {
        fail @"FAIL:submitted_at"
      } else if !stake_valid {
        fail @"FAIL:stake_valid"
      } else if !fields_valid {
        fail @"FAIL:fields_valid"
      } else if !activity_tracking.validate_submit_activity(
        params,
        refs,
        datum,
        token,
        w.lo,
        own.output.address,
        tx,
      ) {
        fail @"FAIL:rate_valid"
      } else if !output_valid {
        fail @"FAIL:output_valid"
      } else {
        True
      }
    }
    None -> fail @"FAIL:identity_valid"
  }
}

// ============================================================================
// Finalisation (§5.2, §5.4, §5.5, §5.6, §5.6b)
// ============================================================================

/// The rules that every finalisation of the proposal `datum` at `own_ref`
/// shares (withdraw, adopt, reject, expire, expire stale).
///
/// MUST:
///   1. The proposal state is Open or Amended.
///   2. The proposal input holds exactly one token under the proposal policy,
///      and the transaction burns exactly that token, and mints or burns
///      nothing else under the policy.
///   3. The activity record of the proposer goes down by one
///      (activity_tracking.validate_finalize_activity).
///   4. The stake returns to the stored proposer credential: the outputs to
///      it hold at least the lovelace that the transaction returns to it
///      (settlement.owner_paid, which sums all outputs and all returned
///      inputs of that credential).
fn validate_finalisation(
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  and {
    utils.is_proposal_active(datum.state),
    when dict.to_pairs(assets.tokens(own.output.value, refs.proposal_mint_hash)) is {
      [Pair(name, quantity)] ->
        quantity == 1 && dict.to_pairs(
          assets.tokens(tx.mint, refs.proposal_mint_hash),
        ) == [Pair(name, -1)]
      _ -> False
    },
    activity_tracking.validate_finalize_activity(refs, datum.proposer_did, tx),
    settlement.owner_paid(refs, datum.proposer_credential, tx),
  }
}

/// Validate proposal withdrawal (§5.2). The proposer can retract at any time
/// before finalisation.
///
/// MUST: the proposer credential signs, and validate_finalisation.
pub fn validate_withdraw_proposal(
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  and {
    utils.verify_signer(datum.proposer_credential, tx.extra_signatories),
    validate_finalisation(refs, datum, own_ref, tx),
  }
}

// ============================================================================
// §5.3 Amend Proposal
// ============================================================================

/// Validate proposal amendment incorporating critiques.
///
/// An amendment is an action of the proposer on its open proposal, so it has
/// the identity check of a submission. The new datum is the spent datum with
/// the new hash, the new URI, amendment_count + 1, the incorporated critiques
/// appended in the order of the redeemer, and the state Amended with the
/// previous hash. All other fields stay the same.
///
/// MUST:
///   1. The proposal state is Open or Amended.
///   2. The new hash is 32 bytes and differs from the current hash; the new
///      URI is not empty.
///   3. At most max_incorporated_critiques critiques and max_amendments
///      amendments in total.
///   4. The validity range has finite bounds and is at most one hour long, and
///      the proposer of the new datum passes the identity check.
///   5. The incorporated critiques are distinct, and the transaction spends
///      each of them at the critique validator with IncorporateCritique. The
///      critique validator checks that each critique names this proposal.
///   6. The proposal input holds exactly one token under the proposal policy,
///      and an output continues it with the new datum: the same address
///      (payment and stake credential), the same tokens and at least the same
///      AP3X (settlement.continues).
///   7. The transaction mints and burns nothing under the proposal policy.
pub fn validate_amend_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: ProposalDatum,
  new_hash: ByteArray,
  new_uri: ByteArray,
  incorporated_refs: List<OutputReference>,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  let new_datum =
    ProposalDatum {
      ..datum,
      proposal_hash: new_hash,
      storage_uri: new_uri,
      amendment_count: datum.amendment_count + 1,
      incorporated_critiques: list.concat(
        datum.incorporated_critiques,
        incorporated_refs,
      ),
      state: Amended { previous_hash: datum.proposal_hash },
    }
  let new_data: Data = new_datum
  when identity.window(tx.validity_range) is {
    Some(w) ->
      and {
        utils.is_proposal_active(datum.state),
        builtin.length_of_bytearray(new_hash) == 32,
        new_hash != datum.proposal_hash,
        builtin.length_of_bytearray(new_uri) > 0,
        list.length(new_datum.incorporated_critiques) <= params.max_incorporated_critiques,
        new_datum.amendment_count <= params.max_amendments,
        utils.verify_active_agent(
          config,
          new_datum.proposer_did,
          new_datum.proposer_credential,
          w,
          tx.reference_inputs,
          tx.extra_signatories,
        ),
        list.unique(incorporated_refs) == incorporated_refs,
        list.all(
          incorporated_refs,
          fn(critique_ref) { spends_incorporated(refs, critique_ref, tx) },
        ),
        holds_one_proposal_token(refs, own),
        settlement.continues(own, new_data, tx.outputs),
        dict.is_empty(assets.tokens(tx.mint, refs.proposal_mint_hash)),
      }
    None -> False
  }
}

// ============================================================================
// §5.4 Foundation Adoption
// ============================================================================

/// Validate Foundation adoption of a proposal.
///
/// MUST: the oracle signs; the reward amount is within
/// [min_adoption_reward, max_adoption_reward]; the reasoning hash is 32
/// bytes; validate_finalisation (which includes the stake return to the
/// stored proposer credential); the cross-module bonus when Module 3 is
/// deployed.
pub fn validate_adopt_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: ProposalDatum,
  reasoning_hash: ByteArray,
  reward_amount: Int,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  and {
    utils.verify_oracle_signature(
      config,
      tx.reference_inputs,
      tx.extra_signatories,
    ),
    reward_amount >= params.min_adoption_reward,
    reward_amount <= params.max_adoption_reward,
    builtin.length_of_bytearray(reasoning_hash) == 32,
    validate_finalisation(refs, datum, own_ref, tx),
    // 15-16. cross-module bonus: proposer +10 AP3X, each incorporated critic
    // +5 AP3X. Only enforced if Module 3 is deployed
    // (reputation_validator_hash != zeroed)
    validate_cross_game_bonus(config, tx.outputs, tx.mint),
  }
}

// ============================================================================
// §5.5 Foundation Rejection
// ============================================================================

/// Validate Foundation rejection of a proposal.
///
/// MUST: the oracle signs; the reasoning hash is 32 bytes;
/// validate_finalisation.
pub fn validate_reject_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  reasoning_hash: ByteArray,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  and {
    utils.verify_oracle_signature(
      config,
      tx.reference_inputs,
      tx.extra_signatories,
    ),
    builtin.length_of_bytearray(reasoning_hash) == 32,
    validate_finalisation(refs, datum, own_ref, tx),
  }
}

// ============================================================================
// §5.6 Proposal Expiration
// ============================================================================

/// Validate proposal expiration after review window. Callable by anyone.
///
/// MUST: the lower bound of the validity range is after
/// submitted_at + review_window; validate_finalisation.
pub fn validate_expire_proposal(
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  current_slot: Int,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  and {
    current_slot > datum.submitted_at + datum.review_window,
    validate_finalisation(refs, datum, own_ref, tx),
  }
}

// ============================================================================
// §5.6b Expire Stale Proposal
// ============================================================================

/// Validate expiration of a stale ParameterChange proposal.
/// A proposal is stale when its current_value no longer matches on-chain.
/// Callable by anyone (permissionless).
///
/// MUST: the proposal is a ParameterChange; a reference input is at
/// protocol_params_hash; validate_finalisation.
pub fn validate_expire_stale_proposal(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  datum: ProposalDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // Verify the ProtocolParams reference input exists at protocol_params_hash.
  // The staleness check requires the off-chain submitter to include the
  // ProtocolParams UTXO as a reference input; the validator confirms it exists.
  // Full value comparison would require knowing ProtocolParams datum structure;
  // for Phase 1.0 we trust the permissionless caller to only trigger this
  // when the value has actually changed (the Foundation can reject false claims).
  and {
    when datum.proposal_type is {
      ParameterChange { .. } -> True
      _ -> False
    },
    list.any(
      tx.reference_inputs,
      fn(input) {
        settlement.is_at_script(input, config.protocol_params_hash)
      },
    ),
    validate_finalisation(refs, datum, own_ref, tx),
  }
}

// ============================================================================
// §5.6c Extend Review
// ============================================================================

/// Validate Foundation extending the review window.
///
/// MUST:
///   1. The oracle signs, the proposal is Open or Amended,
///      additional_slots > 0 and review_window + additional_slots <=
///      max_review_window.
///   2. The proposal input holds exactly one token under the proposal policy,
///      and an output continues it with the spent datum and the new review
///      window: the same address (payment and stake credential), the same
///      tokens and at least the same AP3X (settlement.continues).
///   3. The transaction mints and burns nothing under the proposal policy.
pub fn validate_extend_review(
  config: GovernanceConfig,
  refs: GovernanceCrossRefs,
  params: GovernanceParams,
  datum: ProposalDatum,
  additional_slots: Int,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let own = settlement.own_input(tx, own_ref)
  let new_window = datum.review_window + additional_slots
  let new_data: Data = ProposalDatum { ..datum, review_window: new_window }
  and {
    utils.verify_oracle_signature(
      config,
      tx.reference_inputs,
      tx.extra_signatories,
    ),
    utils.is_proposal_active(datum.state),
    additional_slots > 0,
    new_window <= params.max_review_window,
    holds_one_proposal_token(refs, own),
    settlement.continues(own, new_data, tx.outputs),
    dict.is_empty(assets.tokens(tx.mint, refs.proposal_mint_hash)),
  }
}

// ============================================================================
// Internal Helpers
// ============================================================================

/// True when the input holds exactly one token under the proposal policy,
/// with quantity 1 (the token of a proposal).
fn holds_one_proposal_token(refs: GovernanceCrossRefs, input: Input) -> Bool {
  when dict.to_pairs(assets.tokens(input.output.value, refs.proposal_mint_hash)) is {
    [Pair(_, quantity)] -> quantity == 1
    _ -> False
  }
}

/// True when the transaction spends `critique_ref` at the critique validator
/// with IncorporateCritique.
fn spends_incorporated(
  refs: GovernanceCrossRefs,
  critique_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let incorporate: Data = IncorporateCritique
  and {
    list.any(
      tx.inputs,
      fn(input) {
        input.output_reference == critique_ref && settlement.is_at_script(
          input,
          refs.critique_validator_hash,
        )
      },
    ),
    pairs.get_first(tx.redeemers, Spend(critique_ref)) == Some(incorporate),
  }
}

/// Validate stake amount based on proposal priority.
/// Standard proposals check minimum stake; emergency proposals delegate to
/// the emergency library for full §5.1b validation (type, stake, reputation).
fn validate_stake_for_priority(
  config: GovernanceConfig,
  params: GovernanceParams,
  datum: ProposalDatum,
  reference_inputs: List<Input>,
) -> Bool {
  when datum.priority is {
    Standard -> datum.stake_amount >= params.min_proposal_stake
    Emergency ->
      emergency.validate_emergency_proposal(
        config,
        params,
        datum,
        reference_inputs,
      )
  }
}

/// Validate review window based on priority
fn validate_review_window(
  params: GovernanceParams,
  datum: ProposalDatum,
) -> Bool {
  when datum.priority is {
    Standard ->
      datum.review_window >= params.min_review_window && datum.review_window <= params.max_review_window
    Emergency -> datum.review_window == params.emergency_review_window
  }
}

/// Validate proposal type is well-formed
fn validate_proposal_type(
  params: GovernanceParams,
  proposal_type: ProposalType,
) -> Bool {
  when proposal_type is {
    ParameterChange { param_name, .. } ->
      gov_params.is_governable_param(param_name)
    TreasurySpend { amount, .. } ->
      amount > 0 && amount <= params.max_treasury_request
    ProtocolUpgrade { upgrade_description_hash } ->
      builtin.length_of_bytearray(upgrade_description_hash) == 32
    GameActivation { game_id } -> game_id > 0
    GeneralSuggestion -> True
  }
}

// ============================================================================
// §5.4 Cross-Module Bonus (items 15-16)
// ============================================================================

/// Zeroed hash constant (28 bytes) — when Module 3 is not deployed,
/// reputation_validator_hash is set to all zeros. Script hashes are 28 bytes.
const zeroed_hash: ByteArray =
  #"00000000000000000000000000000000000000000000000000000000"

/// Validate that cross-module bonus outputs exist for an adopted proposal.
/// Proposer gets +10 AP3X bonus, each incorporated critic gets +5 AP3X.
/// Bonuses are HistoryBonusDatum UTXOs sent to the Module 3 reputation validator.
///
/// If Module 3 is not deployed (reputation_validator_hash == zeroed), this is skipped.
fn validate_cross_game_bonus(
  config: GovernanceConfig,
  outputs: List<Output>,
  mint: assets.Value,
) -> Bool {
  // Skip if Module 3 not deployed
  if config.reputation_validator_hash == zeroed_hash {
    True
  } else {
    // Proposer bonus: output at reputation validator address with bonus token minted
    let proposer_bonus_exists =
      list.any(
        outputs,
        fn(output) {
          when output.address.payment_credential is {
            ScriptCredential(hash) -> hash == config.reputation_validator_hash
            _ -> False
          }
        },
      )

    // Verify at least 1 history bonus token is minted under the reputation policy
    // Token prefix "hbonus_" = first 7 bytes
    let bonus_token_minted =
      list.any(
        assets.flatten(mint),
        fn(entry) {
          let (pid, name, qty) = entry
          pid == config.reputation_validator_hash && builtin.slice_bytearray(
            0,
            7,
            name,
          ) == "hbonus_" && qty > 0
        },
      )

    proposer_bonus_exists && bonus_token_minted
  }
}
// Emergency proposal reputation gate is now in governance_suggestion/emergency.ak
// Use emergency.verify_proposer_reputation() for direct access.
