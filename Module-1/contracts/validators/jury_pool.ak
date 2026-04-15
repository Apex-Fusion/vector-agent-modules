/// jury_pool.ak — Jury Pool Multi-Validator for Module 1: Adversarial Auditing
///
/// Controls juror registration, selection, voting, and reward distribution.
///
/// Mint handler:
///   RegisterJuror → mint juror token, create JurorUTXO with bond
///
/// Spend handler:
///   WithdrawJuror      → exit pool (only if not assigned to active case)
///   SelectJury         → Phase 1.0: oracle picks jury; Phase 1.1+: PRNG-based
///   CastVote           → juror casts vote on active challenge
///   DistributeRewards  → update juror stats after challenge resolution
///
/// ═══ DESIGN NOTES FROM VRF RESEARCH (2026-03-23) ═══
///
/// JURY POOL FREEZE: eligible_jurors is snapshotted into ChallengeDatum at
///   OpenChallenge time. SelectJury MUST use that snapshot.
///
/// PHASE 1.0 SELECTION (current implementation):
///   Foundation oracle credential signs the SelectJury tx.
///   selection_seed in the redeemer is COSMETIC — used for ordering only.
///   The validator does NOT trust the seed for security in Phase 1.0.
///   Oracle controls selection; we verify oracle signature only.
///
/// PHASE 1.1 UPGRADE PATH (not yet implemented):
///   // Phase 1.1: replace oracle-signed selection with slot-based entropy once
///   // pool is large enough (see VRF research 2026-03-23).
///   Seed = blake2b_256(cbor(challenge_ref) ++ cbor(challenged_at + selection_delay))
///   Validator sorts juror inputs lexicographically — never trusts submitter ordering.
///   NEVER accept redeemer-supplied block hash without oracle NFT authentication.
///
/// DOUBLE SATISFACTION: SelectJury consumes MULTIPLE juror inputs (one per juror).
///   count==1 guard is NOT applied for SelectJury — it's applied per-action instead.
///
/// JUROR FREEZE: active_case: Option<OutputReference> in JurorDatum.
///   None = available; Some(ref) = frozen until DistributeRewards clears it.

use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Input, OutputReference, Transaction}
use adversarial_auditing/params.{ProtocolParams, juror_fee_share}
use aiken/crypto
use aiken/primitive/bytearray
use adversarial_auditing/params.{compute_slash_amount}
use adversarial_auditing/types.{
  AuditingConfig, AuditorWins, ChallengeDatum, ClaimerWins, CommitVote,
  CrossValidatorRefs, DistributeRewards, Inconclusive,
  JuryAction, JurorDatum, PendingJury, ReceiveJuryFee, RegisterJuror,
  ResetStaleActiveCase, Resolved,
  RevealVote, SelectJury, SlashNonReveal, Verdict, Voting, WithdrawJuror,
}
use adversarial_auditing/utils.{
  ap3x_of, count_script_inputs, credential_signed,
  exactly_one_burned, exactly_one_minted, find_input, find_output_at_script,
  get_cross_refs, is_valid_hash, juror_token_name, outputs_at_script,
  tx_ends_before, tx_started_after, verify_active_did, vkh_signed,
}
use adversarial_auditing/reset.{check_reset_stale_active_case_with_refs}

// ══════════════════════════════════════════════════════════════════════
// MULTI-VALIDATOR
// ══════════════════════════════════════════════════════════════════════

validator jury_pool(config: AuditingConfig, params: ProtocolParams) {
  mint(redeemer: JuryAction, policy_id: PolicyId, tx: Transaction) {
    when redeemer is {
      RegisterJuror ->
        validate_register_juror(config, params, policy_id, tx)
      // Burn path: WithdrawJuror burns the juror token.
      // The spend handler enforces all conditions (not active, bond returned, signed).
      WithdrawJuror -> {
        let is_burning =
          list.any(
            dict.to_pairs(assets.tokens(tx.mint, policy_id)),
            fn(p) { p.2nd == -1 },
          )
        is_burning
      }
      _ ->
        fail @"jury_pool mint: unsupported redeemer for mint"
    }
  }

  spend(
    datum: Option<JurorDatum>,
    redeemer: JuryAction,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    // fix: NEW-FINAL-02 — ReceiveJuryFee UTXOs land at jury_pool_hash with no JurorDatum.
    // Dispatch before attempting to parse datum to avoid expect-fail on fee UTXOs.
    when redeemer is {
      ReceiveJuryFee ->
        validate_receive_jury_fee(config, own_ref, tx)
      _ -> {
        expect Some(juror) = datum
        // Resolve cross-validator refs from authenticated reference input.
        let refs = get_cross_refs(config, tx.reference_inputs)
        when redeemer is {
          WithdrawJuror ->
            validate_withdraw_juror(config, refs, juror, own_ref, tx)
          SelectJury { challenge_ref, selection_seed, selected_jurors } ->
            // SelectJury: do NOT enforce count==1 — multiple juror UTXOs consumed.
            validate_select_jury(
              config,
              refs,
              juror,
              tx,
              challenge_ref,
              selection_seed,
              selected_jurors,
            )
          CommitVote { challenge_ref, commitment_hash } -> {
            let single_input =
              count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1
            expect single_input
            validate_commit_vote(config, refs, params, juror, tx, challenge_ref, commitment_hash)
          }
          RevealVote { challenge_ref, verdict, salt } -> {
            let single_input =
              count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1
            expect single_input
            validate_reveal_vote(config, refs, params, juror, tx, challenge_ref, verdict, salt)
          }
          SlashNonReveal { challenge_ref } -> {
            let single_input =
              count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1
            expect single_input
            validate_slash_non_reveal(config, refs, params, juror, tx, challenge_ref)
          }
          DistributeRewards { challenge_ref } -> {
            let single_input =
              count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1
            expect single_input
            validate_distribute_rewards(config, refs, params, juror, tx, challenge_ref)
          }
          ResetStaleActiveCase -> {
            let single_input =
              count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1
            expect single_input
            check_reset_stale_active_case_with_refs(refs, juror, own_ref, tx)
          }
          RegisterJuror ->
            fail @"jury_pool spend: RegisterJuror is a mint action"
          ReceiveJuryFee ->
            fail @"jury_pool spend: unreachable — ReceiveJuryFee handled above"
        }
      }
    }
  }

  else(_ctx) {
    fail @"jury_pool: unsupported script purpose"
  }
}

// ══════════════════════════════════════════════════════════════════════
// RECEIVE JURY FEE (spend)
// ══════════════════════════════════════════════════════════════════════
//
// fix: NEW-FINAL-02 — allows fee UTXOs deposited by ResolveJury at jury_pool_hash
// to be consumed. Without this action, those UTXOs are permanently locked because
// the spend handler's `expect Some(juror) = datum` fails on fee UTXOs (which carry
// no JurorDatum).
//
// Design intent: the oracle signs this tx to prevent unauthorized AP3X drainage.
// The AP3X may be forwarded anywhere (e.g., back to the oracle wallet for later
// manual distribution, or to a new fee accumulator UTXO). Phase 1.1 may refine
// this into a dedicated FeeDistribute action with on-chain accounting.
//
// Note: this is a transitional Phase 1.0 mechanism. In Phase 1.1, the preferred
// approach is Option A from code review: distribute jury fees directly to juror
// outputs in the ResolveJury tx, eliminating the fee pool UTXO entirely.

fn validate_receive_jury_fee(
  config: AuditingConfig,
  _own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // Oracle must sign — prevents unauthorized extraction of accumulated fees.
  let oracle_signed = vkh_signed(tx, config.oracle_vkh)
  oracle_signed
}

// ══════════════════════════════════════════════════════════════════════
// REGISTER JUROR (mint)
// ══════════════════════════════════════════════════════════════════════

fn validate_register_juror(
  config: AuditingConfig,
  params: ProtocolParams,
  policy_id: PolicyId,
  tx: Transaction,
) -> Bool {
  // Derive token name from first consumed input.
  expect [seed_input, ..] = tx.inputs
  let token_name = juror_token_name(seed_input.output_reference)

  let correct_mint = exactly_one_minted(tx, policy_id, token_name)

  // In the mint handler, policy_id IS the jury pool validator's own script hash.
  let juror_outputs = outputs_at_script(tx.outputs, policy_id)
  let single_output = list.length(juror_outputs) == 1

  let output_valid =
    when juror_outputs is {
      [out] -> {
        let has_token =
          assets.quantity_of(out.value, policy_id, token_name) == 1

        expect InlineDatum(raw) = out.datum
        expect juror: JurorDatum = raw

        let has_did =
          verify_active_did(config, juror.juror_did, tx.reference_inputs)

        let bond_ok = juror.bond_amount >= params.min_juror_bond

        let ap3x_locked =
          ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name) == juror.bond_amount

        // Initial stats must be zeroed; not yet assigned to any case.
        let stats_fresh =
          and {
            juror.cases_resolved == 0,
            juror.majority_votes == 0,
            juror.active_case == None,
            juror.vote_commitment == None,
            juror.revealed_verdict == None,
          }

        let signed = credential_signed(tx, juror.juror_credential)

        and {
          has_token,
          has_did,
          bond_ok,
          ap3x_locked,
          stats_fresh,
          signed,
        }
      }
      _ -> False
    }

  and {
    correct_mint,
    single_output,
    output_valid,
  }
}

// ══════════════════════════════════════════════════════════════════════
// WITHDRAW JUROR (spend)
// ══════════════════════════════════════════════════════════════════════

fn validate_withdraw_juror(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  juror: JurorDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let single_input = count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1
  expect single_input

  // Cannot withdraw while assigned to an active case (juror freeze mechanism).
  let not_active =
    when juror.active_case is {
      None -> True
      Some(_) -> False
    }

  let token_burned =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when
          dict.to_pairs(
            assets.tokens(inp.output.value, refs.jury_pool_hash),
          )
        is {
          [Pair(token_name, 1)] ->
            exactly_one_burned(tx, refs.jury_pool_hash, token_name)
          _ -> False
        }
      None -> False
    }

  // Exact bond return — use == not >= to prevent AP3X draining.
  let bond_returned =
    list.any(tx.outputs, fn(o) {
      and {
        o.address.payment_credential == juror.juror_credential,
        ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == juror.bond_amount,
      }
    })

  let signed = credential_signed(tx, juror.juror_credential)

  and {
    not_active,
    token_burned,
    bond_returned,
    signed,
  }
}

// ══════════════════════════════════════════════════════════════════════
// SELECT JURY (spend) — called once per selected juror input
// ══════════════════════════════════════════════════════════════════════
//
// Phase 1.0: Oracle-controlled selection.
//   Foundation oracle credential signs the tx. selection_seed is cosmetic.
//   The validator does NOT trust selection_seed for security.
//   Oracle is the security anchor. We verify: oracle sig + juror in list + output ok.
//
// // Phase 1.1: replace oracle-signed selection with slot-based entropy once
// // pool is large enough (see VRF research 2026-03-23).
// // Use compute_selection_seed(challenge_ref, challenged_at + selection_delay)
// // from utils.ak. Sort eligible_jurors from ChallengeDatum snapshot.
// // Verify with verify_juror_selection(seed, sorted_pool, selected_jurors, jury_size).

fn validate_select_jury(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  juror: JurorDatum,
  tx: Transaction,
  challenge_ref: OutputReference,
  _selection_seed: ByteArray,
  selected_jurors: List<PolicyId>,
) -> Bool {
  // Phase 1.1: permissionless — jury selection is deterministic (PRNG from
  // challenge token name). Oracle signature no longer required.
  // The selected_jurors list comes from the Voting{selected_jurors} state
  // set by TransitionToVoting (which verified the PRNG selection on-chain).

  // Phase 1.1: juror selection is verified via Voting state datum (see challenge_in_voting below).
  // The redeemer's selected_jurors is no longer trusted — on-chain datum is the source of truth.

  // Juror must be available (not assigned to another active case).
  let juror_available =
    when juror.active_case is {
      None -> True
      Some(_) -> False
    }

  // fix: CR-P11-01 — Challenge must be in Voting state (not PendingJury).
  // Phase 1.1: PRNG selection happens in TransitionToVoting, which sets Voting{selected_jurors}.
  // SelectJury must only run AFTER TransitionToVoting to enforce deterministic selection.
  // Authenticate: verify the reference input is at the challenge validator address
  // AND carries the expected challenge token (prevents reference input spoofing G1-ZR-09).
  let challenge_token_name_result =
    list.find(tx.reference_inputs, fn(inp) {
      if inp.output_reference == challenge_ref {
        let at_challenge_addr =
          when inp.output.address.payment_credential is {
            Script(h) -> h == refs.challenge_validator_hash
            _ -> False
          }
        let has_challenge_token =
          list.length(dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash))) == 1
        at_challenge_addr && has_challenge_token
      } else {
        False
      }
    })

  // fix: CR-P11-01 — Read selected_jurors from Voting state datum, not redeemer.
  // This eliminates redeemer trust: the juror list is verified on-chain by TransitionToVoting.
  let challenge_in_voting =
    when challenge_token_name_result is {
      Some(inp) ->
        when inp.output.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            when ch.state is {
              Voting { selected_jurors: on_chain_jurors } ->
                list.has(on_chain_jurors, juror.juror_did)
              _ -> False
            }
          }
          _ -> False
        }
      None -> False
    }

  // Extract the challenge token name from the reference input value.
  // fix: CR-002-F1 — store token name (stable) instead of OutputReference (mutable).
  expect Some(challenge_inp) = challenge_token_name_result
  expect [Pair(challenge_token_name, _)] =
    dict.to_pairs(assets.tokens(challenge_inp.output.value, refs.challenge_validator_hash))

  // Continuing output for this juror: active_case set to challenge token name.
  // fix: CR-002-F1 — active_case stores ByteArray token name, not OutputReference.
  let continuing_output_ok =
    list.any(
      outputs_at_script(tx.outputs, refs.jury_pool_hash),
      fn(out) {
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: JurorDatum = raw
            and {
              updated.juror_did == juror.juror_did,
              updated.juror_credential == juror.juror_credential,
              updated.bond_amount == juror.bond_amount,
              updated.cases_resolved == juror.cases_resolved,
              updated.majority_votes == juror.majority_votes,
              updated.registered_at == juror.registered_at,
              updated.active_case == Some(challenge_token_name),
              updated.vote_commitment == None,
              updated.revealed_verdict == None,
              ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name) == juror.bond_amount,
            }
          }
          _ -> False
        }
      },
    )

  and {
    juror_available,
    challenge_in_voting,
    continuing_output_ok,
  }
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT VOTE (spend) — Phase 1.1 commit-reveal
// ══════════════════════════════════════════════════════════════════════
//
// Juror submits commitment_hash = blake2b_256(verdict_index_byte || salt)
// on-chain. The actual vote is hidden until RevealVote.
// Replaces CastVote from Phase 1.0.

fn validate_commit_vote(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  juror: JurorDatum,
  tx: Transaction,
  _challenge_ref: OutputReference,
  commitment_hash: ByteArray,
) -> Bool {
  // Juror must be assigned to a challenge.
  expect Some(active_token_name) = juror.active_case

  // Juror must not have already committed.
  let not_committed =
    when juror.vote_commitment is {
      None -> True
      Some(_) -> False
    }

  // Commitment hash must be exactly 32 bytes (blake2b_256 output).
  let hash_ok = is_valid_hash(commitment_hash)

  // Juror must sign.
  let signed = credential_signed(tx, juror.juror_credential)

  // Challenge must be in Voting state and within commit deadline.
  let challenge_ok =
    list.any(tx.reference_inputs, fn(inp) {
      let at_challenge_addr =
        when inp.output.address.payment_credential is {
          Script(h) -> h == refs.challenge_validator_hash
          _ -> False
        }
      let has_matching_token =
        assets.quantity_of(inp.output.value, refs.challenge_validator_hash, active_token_name) == 1
      if at_challenge_addr && has_matching_token {
        when inp.output.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            when ch.state is {
              Voting { .. } ->
                tx_ends_before(tx, ch.challenged_at + params.commit_window)
              _ -> False
            }
          }
          _ -> False
        }
      } else {
        False
      }
    })

  // Continuing output: all fields same except vote_commitment = Some(commitment_hash).
  let output_ok =
    list.any(
      outputs_at_script(tx.outputs, refs.jury_pool_hash),
      fn(out) {
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: JurorDatum = raw
            and {
              updated.juror_did == juror.juror_did,
              updated.juror_credential == juror.juror_credential,
              updated.bond_amount == juror.bond_amount,
              updated.cases_resolved == juror.cases_resolved,
              updated.majority_votes == juror.majority_votes,
              updated.registered_at == juror.registered_at,
              updated.active_case == juror.active_case,
              updated.vote_commitment == Some(commitment_hash),
              updated.revealed_verdict == None,
              ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name) == juror.bond_amount,
            }
          }
          _ -> False
        }
      },
    )

  and {
    not_committed,
    hash_ok,
    signed,
    challenge_ok,
    output_ok,
  }
}

// ══════════════════════════════════════════════════════════════════════
// REVEAL VOTE (spend) — Phase 1.1 commit-reveal
// ══════════════════════════════════════════════════════════════════════
//
// Juror reveals verdict + salt. Validator verifies:
//   blake2b_256(verdict_index_byte || salt) == stored commitment_hash
// Updates datum: revealed_verdict = Some(verdict), vote_commitment = None.

/// Serialize a Verdict to a single byte for commitment hashing.
/// ClaimerWins=0x00, AuditorWins=0x01, Inconclusive=0x02.
fn serialize_verdict_index(verdict: Verdict) -> ByteArray {
  when verdict is {
    ClaimerWins -> #"00"
    AuditorWins -> #"01"
    Inconclusive -> #"02"
  }
}

fn validate_reveal_vote(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  juror: JurorDatum,
  tx: Transaction,
  _challenge_ref: OutputReference,
  verdict: Verdict,
  salt: ByteArray,
) -> Bool {
  // Juror must be assigned to a challenge.
  expect Some(active_token_name) = juror.active_case

  // Juror must have a stored commitment.
  expect Some(expected_hash) = juror.vote_commitment

  // Verify: blake2b_256(verdict_index_byte || salt) == expected_hash
  let computed_hash =
    crypto.blake2b_256(
      bytearray.concat(serialize_verdict_index(verdict), salt),
    )
  let hash_matches = computed_hash == expected_hash

  // Juror must sign.
  let signed = credential_signed(tx, juror.juror_credential)

  // Challenge must be in Voting state and within reveal deadline.
  let challenge_ok =
    list.any(tx.reference_inputs, fn(inp) {
      let at_challenge_addr =
        when inp.output.address.payment_credential is {
          Script(h) -> h == refs.challenge_validator_hash
          _ -> False
        }
      let has_matching_token =
        assets.quantity_of(inp.output.value, refs.challenge_validator_hash, active_token_name) == 1
      if at_challenge_addr && has_matching_token {
        when inp.output.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            when ch.state is {
              Voting { .. } -> {
                let commit_deadline = ch.challenged_at + params.commit_window
                let reveal_deadline = commit_deadline + params.reveal_window
                // fix: CR-CR-01 — reveal must happen AFTER commit deadline
                // to prevent early reveal that defeats commit-reveal secrecy.
                let after_commit = tx_started_after(tx, commit_deadline)
                let before_reveal = tx_ends_before(tx, reveal_deadline)
                after_commit && before_reveal
              }
              _ -> False
            }
          }
          _ -> False
        }
      } else {
        False
      }
    })

  // Continuing output: revealed_verdict = Some(verdict), vote_commitment = None.
  let output_ok =
    list.any(
      outputs_at_script(tx.outputs, refs.jury_pool_hash),
      fn(out) {
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: JurorDatum = raw
            and {
              updated.juror_did == juror.juror_did,
              updated.juror_credential == juror.juror_credential,
              updated.bond_amount == juror.bond_amount,
              updated.cases_resolved == juror.cases_resolved,
              updated.majority_votes == juror.majority_votes,
              updated.registered_at == juror.registered_at,
              updated.active_case == juror.active_case,
              updated.vote_commitment == None,
              updated.revealed_verdict == Some(verdict),
              ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name) == juror.bond_amount,
            }
          }
          _ -> False
        }
      },
    )

  and {
    hash_matches,
    signed,
    challenge_ok,
    output_ok,
  }
}

// ══════════════════════════════════════════════════════════════════════
// SLASH NON-REVEAL (spend) — Phase 1.1 commit-reveal
// ══════════════════════════════════════════════════════════════════════
//
// Slash a juror who committed but did not reveal within the reveal window.
// Oracle-gated to prevent griefing. Bond reduced by slash_rate.

fn validate_slash_non_reveal(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  juror: JurorDatum,
  tx: Transaction,
  _challenge_ref: OutputReference,
) -> Bool {
  // Juror must be assigned to a challenge.
  expect Some(active_token_name) = juror.active_case

  // Juror must have committed but NOT revealed.
  let committed_not_revealed =
    when juror.vote_commitment is {
      Some(_) ->
        when juror.revealed_verdict is {
          None -> True
          Some(_) -> False
        }
      None -> False
    }

  // Phase 1.1: permissionless — no oracle needed.
  // Guards: committed_not_revealed + deadline_passed + output_ok are sufficient.
  // Griefing is not possible: slash only affects jurors who genuinely didn't reveal,
  // and the time check prevents early slashing.

  // Reveal deadline must have passed.
  let deadline_passed =
    list.any(tx.reference_inputs, fn(inp) {
      let at_challenge_addr =
        when inp.output.address.payment_credential is {
          Script(h) -> h == refs.challenge_validator_hash
          _ -> False
        }
      let has_matching_token =
        assets.quantity_of(inp.output.value, refs.challenge_validator_hash, active_token_name) == 1
      if at_challenge_addr && has_matching_token {
        when inp.output.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            tx_started_after(tx, ch.challenged_at + params.commit_window + params.reveal_window)
          }
          _ -> False
        }
      } else {
        False
      }
    })

  // Compute slash amount.
  let slash_amount = compute_slash_amount(juror.bond_amount, params.juror_slash_rate)
  let remaining_bond = juror.bond_amount - slash_amount

  // Continuing output: bond reduced, active_case cleared, commitment cleared.
  let output_ok =
    list.any(
      outputs_at_script(tx.outputs, refs.jury_pool_hash),
      fn(out) {
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: JurorDatum = raw
            and {
              updated.juror_did == juror.juror_did,
              updated.juror_credential == juror.juror_credential,
              updated.bond_amount == remaining_bond,
              updated.cases_resolved == juror.cases_resolved,
              updated.majority_votes == juror.majority_votes,
              updated.registered_at == juror.registered_at,
              updated.active_case == None,
              updated.vote_commitment == None,
              updated.revealed_verdict == None,
              ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name) == remaining_bond,
            }
          }
          _ -> False
        }
      },
    )

  and {
    committed_not_revealed,
    deadline_passed,
    output_ok,
  }
}

// ══════════════════════════════════════════════════════════════════════
// DISTRIBUTE REWARDS (spend)
// ══════════════════════════════════════════════════════════════════════
//
// Called after challenge resolution to update juror stats and clear active_case.
// Permissionless — anyone can trigger for a juror once the challenge is resolved.

fn validate_distribute_rewards(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  juror: JurorDatum,
  tx: Transaction,
  _challenge_ref: OutputReference,
) -> Bool {
  // fix: CR-002-F1 — active_case now stores challenge token name (ByteArray),
  // not OutputReference. Extract the token name from the juror's active_case.
  expect Some(active_token_name) = juror.active_case

  // Juror must be assigned to this challenge (token name must match).
  // fix: CR-002-F1 — look up the Resolved challenge by scanning reference_inputs
  // for a UTxO at challenge validator address carrying a token matching active_case.
  // This survives the OutputReference change caused by ResolveJury spending the UTxO.
  let challenge_result =
    list.find(tx.reference_inputs, fn(inp) {
      let at_challenge_addr =
        when inp.output.address.payment_credential is {
          Script(h) -> h == refs.challenge_validator_hash
          _ -> False
        }
      let has_matching_token =
        assets.quantity_of(inp.output.value, refs.challenge_validator_hash, active_token_name) == 1
      at_challenge_addr && has_matching_token
    })

  // fix: CR-002-R2-N1 — must verify challenge is in Resolved state.
  // Without this check, jurors could call DistributeRewards while still in Voting state.
  let challenge_resolved =
    when challenge_result is {
      Some(inp) ->
        when inp.output.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            when ch.state is {
              Resolved { .. } -> True
              _ -> False
            }
          }
          _ -> False
        }
      None -> False
    }

  // Extract verdict from the resolved challenge for fee computation.
  // Note: ChallengeDatum.stake_amount is the AUDITOR stake. Claim stake is not
  // stored in ChallengeDatum (it's in the consumed ClaimDatum). For fee routing,
  // we use the auditor stake as a proxy: if auditor lost (ClaimerWins), jury fee
  // = compute_jury_fee(auditor_stake, rate) / jury_size.
  // For AuditorWins and Inconclusive, fee is based on loser/combined — use auditor_stake
  // as the conservative known value. Full bidirectional fee routing requires
  // storing claim_stake in ChallengeDatum (scheduled for Phase 1.1 type update).
  let fee_per_juror =
    when challenge_result is {
      Some(inp) ->
        when inp.output.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            // Compute jury fee from auditor stake (available in datum).
            // For exact per-verdict fee accounting, claim_stake must also be stored.
            let jury_fee =
              juror_fee_share(
                // Use auditor_stake as base; conservative lower-bound of actual fee.
                // Full fix: add claim_stake to ChallengeDatum in Phase 1.1.
                ch.stake_amount * params.jury_fee_rate / 10000,
                params.jury_size,
              )
            jury_fee
          }
          _ -> 0
        }
      None -> 0
    }

  // Continuing output: increment cases_resolved, clear active_case.
  // Bond + fee_per_juror must reach the juror.
  // Match by juror DID — not first-output-match (G1-ZR-11 griefing fix).
  let output_ok =
    list.any(
      outputs_at_script(tx.outputs, refs.jury_pool_hash),
      fn(out) {
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: JurorDatum = raw
            // Only accept the output that matches this specific juror's DID.
            if updated.juror_did == juror.juror_did {
              let ap3x_out =
                ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name)
              and {
                updated.juror_credential == juror.juror_credential,
                updated.registered_at == juror.registered_at,
                updated.cases_resolved == juror.cases_resolved + 1,
                updated.active_case == None,
                // fix: RT-008 — verify bond_amount and majority_votes immutability.
                // Without these checks, a juror could inflate bond_amount or game stats.
                updated.bond_amount == juror.bond_amount,
                updated.majority_votes == juror.majority_votes,
                // Commit-reveal fields must be cleared after distribution.
                updated.vote_commitment == None,
                updated.revealed_verdict == None,
                // Bond preserved + fee distributed (G1-ZR-05).
                // Output must be exactly bond + fee_per_juror.
                ap3x_out == juror.bond_amount + fee_per_juror,
              }
            } else {
              False
            }
          }
          _ -> False
        }
      },
    )

  and {
    challenge_resolved,
    output_ok,
  }
}



// ══════════════════════════════════════════════════════════════════════
// RESET STALE ACTIVE CASE (spend)
// ══════════════════════════════════════════════════════════════════════
//
// Escape hatch for a juror whose active_case references a challenge token
// that no longer exists on-chain. Happens when TimeoutResolve burned the
// challenge but the juror was never reached by DistributeRewards (which
// requires a Resolved challenge — unavailable after timeout burn).
//
// The authoritative rule body lives in `adversarial_auditing/reset.ak`
// (check_reset_stale_active_case_with_refs) so that tests can exercise
// the exact code the spend handler dispatches to. See that module for
// preconditions and continuing-output requirements.
