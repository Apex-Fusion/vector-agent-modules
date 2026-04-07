/// challenge.ak — Challenge Multi-Validator for Game 1: Adversarial Auditing
///
/// Controls the lifecycle of challenge UTXOs.
/// Each challenge is an independent UTXO referencing a specific claim.
///
/// Mint handler:
///   OpenChallenge  → mint one challenge token, create ChallengeUTXO
///
/// Spend handler:
///   SubmitEvidence → auditor supplements evidence before jury selection
///   OracleResolve  → Phase 1.0: Foundation resolves with verdict (oracle_vkh signs)
///   ResolveJury    → Phase 1.1+: tally jury votes, distribute stakes
///   TimeoutResolve → deadline passed; return both stakes
///
/// Key design decisions (incorporating VRF research 2026-03-23):
///   - eligible_jurors snapshot written at OpenChallenge time (jury pool freeze)
///   - Phase 1.0 OracleResolve requires oracle_vkh signature
///   - Phase 1.1 ResolveJury is permissionless (math is deterministic)
///
/// Security patterns:
///   - count_script_inputs == 1 (double-satisfaction prevention)
///   - Inline datums only (CIP-32)
///   - Token lifecycle: minted on OpenChallenge, burned on resolution
///   - Auditor DID != Claimer DID (anti-self-audit, spec §4.3)
///   - Auditor stake >= Claim stake (economic deterrent)

use aiken/collection/dict
use aiken/collection/list
use aiken/interval
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Input, NoDatum, Output, OutputReference, Transaction, placeholder}
use adversarial_auditing/params.{
  ProtocolParams, compute_jury_fee, supermajority_threshold,
}
use adversarial_auditing/types.{
  AuditingConfig, AuditorWins, ChallengeDatum, ChallengeAction, Challenged,
  ClaimDatum, ClaimerWins, CleanupResolved, CrossValidatorRefs, Inconclusive,
  JurorDatum, Open, OracleResolve, OpenChallenge, PendingJury, PendingOracle,
  Resolved, ResolveJury, SubmitEvidence, TimeoutResolve, TransitionToVoting,
  Verdict, Voting,
}
use adversarial_auditing/utils.{
  ap3x_of, challenge_token_name, claim_token_name, count_script_inputs,
  credential_signed, exactly_one_burned, exactly_one_minted, find_input,
  find_output_at_script, get_cross_refs, is_valid_hash, outputs_at_script,
  select_jurors_prng, sort_dids, tx_contains_slot, tx_ends_before,
  tx_started_after, verify_active_did, vkh_signed,
}

// ══════════════════════════════════════════════════════════════════════
// MULTI-VALIDATOR
// ══════════════════════════════════════════════════════════════════════

validator challenge(config: AuditingConfig, params: ProtocolParams) {
  mint(redeemer: ChallengeAction, policy_id: PolicyId, tx: Transaction) {
    when redeemer is {
      OpenChallenge ->
        validate_open_challenge(config, params, policy_id, tx)
      // Burn paths: OracleResolve, ResolveJury, and TimeoutResolve all burn the challenge token.
      // The corresponding spend handler enforces all conditions — mint handler just permits burn.
      OracleResolve { .. } -> {
        let is_burning =
          list.any(
            dict.to_pairs(assets.tokens(tx.mint, policy_id)),
            fn(p) { p.2nd == -1 },
          )
        is_burning
      }
      ResolveJury -> {
        // fix: Finding-002 Option A — ResolveJury no longer burns the challenge token.
        // The challenge UTxO continues with Resolved{verdict} state.
        // Mint handler permits: no challenge token minted or burned in this tx.
        // The claim token burn (at claim policy) still happens — that's not our policy.
        //
        // fix: CR-002-F2 — Defensive guard note:
        // In a valid ResolveJury tx, no challenge tokens are minted or burned, so
        // the challenge policy won't appear in tx.mint at all — this mint handler
        // simply doesn't execute. If an attacker forces the policy into tx.mint
        // (e.g., by attempting to mint a token with the ResolveJury redeemer),
        // dict.is_empty returns False (entries exist) and the tx correctly fails.
        // This code path is unreachable under normal operation but serves as a
        // defensive guard against unexpected mint attempts.
        let no_challenge_mint =
          dict.is_empty(assets.tokens(tx.mint, policy_id))
        no_challenge_mint
      }
      CleanupResolved -> {
        // fix: Finding-002 Option A — CleanupResolved burns the challenge token
        // after DistributeRewards has consumed the Resolved UTxO as reference input.
        let is_burning =
          list.any(
            dict.to_pairs(assets.tokens(tx.mint, policy_id)),
            fn(p) { p.2nd == -1 },
          )
        is_burning
      }
      TimeoutResolve -> {
        let is_burning =
          list.any(
            dict.to_pairs(assets.tokens(tx.mint, policy_id)),
            fn(p) { p.2nd == -1 },
          )
        is_burning
      }
      _ ->
        fail @"challenge mint: unsupported redeemer for mint"
    }
  }

  spend(
    datum: Option<ChallengeDatum>,
    redeemer: ChallengeAction,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    expect Some(ch) = datum
    // Resolve cross-validator refs from authenticated reference input.
    let refs = get_cross_refs(config, tx.reference_inputs)
    let single_input =
      count_script_inputs(tx.inputs, refs.challenge_validator_hash) == 1
    expect single_input
    when redeemer is {
      SubmitEvidence { new_evidence_hash, new_evidence_uri } ->
        validate_submit_evidence(
          config,
          refs,
          ch,
          tx,
          new_evidence_hash,
          new_evidence_uri,
        )
      OracleResolve { verdict, reasoning_hash } ->
        validate_oracle_resolve(
          config,
          refs,
          ch,
          own_ref,
          tx,
          verdict,
          reasoning_hash,
        )
      ResolveJury ->
        validate_resolve_jury(config, refs, params, ch, own_ref, tx)
      TimeoutResolve ->
        validate_timeout_resolve(config, refs, ch, own_ref, tx)
      TransitionToVoting { selected_jurors } ->
        validate_transition_to_voting(
          config,
          refs,
          params,
          ch,
          own_ref,
          tx,
          selected_jurors,
        )
      CleanupResolved ->
        validate_cleanup_resolved(config, refs, params, ch, own_ref, tx)
      OpenChallenge ->
        fail @"challenge spend: OpenChallenge is a mint action"
    }
  }

  else(_ctx) {
    fail @"challenge: unsupported script purpose"
  }
}

// ══════════════════════════════════════════════════════════════════════
// OPEN CHALLENGE (mint)
// ══════════════════════════════════════════════════════════════════════
//
// Spec §4.3: auditor stakes AP3X to dispute a claim.
// Also atomically initiates MarkChallenged on the claim UTXO.
//
// VRF research: eligible_jurors snapshot stored in ChallengeDatum
// freezes the jury pool at challenge-open time.

fn validate_open_challenge(
  config: AuditingConfig,
  params: ProtocolParams,
  policy_id: PolicyId,
  tx: Transaction,
) -> Bool {
  // Resolve cross-validator refs from authenticated reference input.
  // In the mint handler, policy_id IS the challenge validator's own hash.
  let refs = get_cross_refs(config, tx.reference_inputs)

  // Derive token name from first consumed input.
  expect [seed_input, ..] = tx.inputs
  let token_name = challenge_token_name(seed_input.output_reference)

  // Exactly one challenge token minted.
  let correct_mint = exactly_one_minted(tx, policy_id, token_name)

  // Find unique challenge output.
  // policy_id == refs.challenge_validator_hash (the validator's own hash in mint context).
  let chal_outputs = outputs_at_script(tx.outputs, policy_id)
  let single_output = list.length(chal_outputs) == 1

  let distribution_result =
    when chal_outputs is {
      [chal_output] -> {
        // Parse challenge datum.
        expect InlineDatum(raw_ch) = chal_output.datum
        expect ch: ChallengeDatum = raw_ch

        // Challenge token must be in the output.
        let has_token =
          assets.quantity_of(chal_output.value, policy_id, token_name) == 1

        // Find the claim being challenged from tx inputs (consumed in same tx).
        let claim_check =
          when list.find(tx.inputs, fn(i) { i.output_reference == ch.claim_ref }) is {
            Some(claim_inp) ->
              when claim_inp.output.datum is {
                InlineDatum(raw_claim) -> {
                  expect claim: ClaimDatum = raw_claim
                  let no_self_audit = ch.auditor_did != claim.claimer_did
                  // Challenge must be within the challenge window.
                  let within_window =
                    tx_ends_before(
                      tx,
                      claim.submitted_at + claim.challenge_window,
                    )
                  let stake_sufficient = ch.stake_amount >= claim.stake_amount
                  let ap3x_locked =
                    ap3x_of(
                      chal_output.value,
                      config.ap3x_policy_id,
                      config.ap3x_asset_name,
                    ) == ch.stake_amount
                  and {
                    no_self_audit,
                    within_window,
                    stake_sufficient,
                    ap3x_locked,
                  }
                }
                _ -> False
              }
            None -> False
          }

        // Auditor has active DID.
        let auditor_did_ok =
          verify_active_did(config, ch.auditor_did, tx.reference_inputs)

        // Evidence hash is 32 bytes.
        let evidence_hash_ok = is_valid_hash(ch.evidence_hash)

        // Initial state matches current phase.
        let state_ok =
          if params.oracle_active {
            when ch.state is {
              PendingOracle -> True
              _ -> False
            }
          } else {
            when ch.state is {
              PendingJury -> True
              _ -> False
            }
          }

        // eligible_jurors must be sorted (deterministic PRNG requires sorted pool).
        // VRF research: frozen at challenge time; sorted for lexicographic PRNG.
        let jurors_sorted = ch.eligible_jurors == sort_dids(ch.eligible_jurors)

        // fix: RT-V7 — minimum pool size to resist juror planting + seed grinding.
        // Require >= 3 * jury_size eligible jurors in jury mode.
        // C(15,5) = 3,003 panels — planting 3 sybils gives only 0.33% per seed.
        // In oracle mode (oracle_active=True), pool is not used — skip check.
        let pool_large_enough =
          if params.oracle_active {
            True
          } else {
            list.length(ch.eligible_jurors) >= 3 * params.jury_size
          }

        // challenged_at must fall within the tx validity range.
        // Prevents backdating (triggers immediate timeout) and future-dating (blocks resolution).
        let challenged_at_ok = tx_contains_slot(tx, ch.challenged_at)

        // Auditor signed.
        let signed = credential_signed(tx, ch.auditor_credential)

        // Claim MarkChallenged must be in this tx: claim output with Challenged state.
        let claim_updated =
          list.any(
            outputs_at_script(tx.outputs, refs.claim_validator_hash),
            fn(o) {
              when o.datum is {
                InlineDatum(raw) -> {
                  expect updated: ClaimDatum = raw
                  when updated.state is {
                    Challenged -> True
                    _ -> False
                  }
                }
                _ -> False
              }
            },
          )

        and {
          has_token,
          claim_check,
          auditor_did_ok,
          evidence_hash_ok,
          state_ok,
          jurors_sorted,
          pool_large_enough,
          challenged_at_ok,
          signed,
          claim_updated,
        }
      }
      _ -> False
    }

  and {
    correct_mint,
    single_output,
    distribution_result,
  }
}

// ══════════════════════════════════════════════════════════════════════
// SUBMIT EVIDENCE (spend)
// ══════════════════════════════════════════════════════════════════════

fn validate_submit_evidence(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  ch: ChallengeDatum,
  tx: Transaction,
  new_evidence_hash: ByteArray,
  new_evidence_uri: ByteArray,
) -> Bool {
  let state_ok =
    when ch.state is {
      PendingOracle -> True
      PendingJury -> True
      _ -> False
    }

  let hash_ok = is_valid_hash(new_evidence_hash)

  let correct_continuation =
    when find_output_at_script(tx.outputs, refs.challenge_validator_hash) is {
      Some(out) ->
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: ChallengeDatum = raw
            and {
              updated.evidence_hash == new_evidence_hash,
              updated.evidence_uri == new_evidence_uri,
              updated.claim_ref == ch.claim_ref,
              updated.auditor_did == ch.auditor_did,
              updated.auditor_credential == ch.auditor_credential,
              updated.stake_amount == ch.stake_amount,
              updated.challenged_at == ch.challenged_at,
              updated.resolution_deadline == ch.resolution_deadline,
              updated.eligible_jurors == ch.eligible_jurors,
              // State MUST be immutable — prevents auditor from transitioning
              // PendingOracle→PendingJury (or any other state) via SubmitEvidence.
              updated.state == ch.state,
              ap3x_of(out.value, config.ap3x_policy_id, config.ap3x_asset_name) == ch.stake_amount,
            }
          }
          _ -> False
        }
      None -> False
    }

  let signed = credential_signed(tx, ch.auditor_credential)

  and {
    state_ok,
    hash_ok,
    correct_continuation,
    signed,
  }
}

// ══════════════════════════════════════════════════════════════════════
// ORACLE RESOLVE (spend) — Phase 1.0
// ══════════════════════════════════════════════════════════════════════
//
// Spec §4.7: Foundation resolves dispute. No jury fee. Oracle must sign.

fn validate_oracle_resolve(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  ch: ChallengeDatum,
  own_ref: OutputReference,
  tx: Transaction,
  verdict: Verdict,
  reasoning_hash: ByteArray,
) -> Bool {
  // Foundation oracle must sign.
  let oracle_signed = vkh_signed(tx, config.oracle_vkh)

  let state_ok =
    when ch.state is {
      PendingOracle -> True
      _ -> False
    }

  let hash_ok = is_valid_hash(reasoning_hash)

  // Challenge token burned.
  let challenge_token_burned =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash)) is {
          [Pair(token_name, 1)] ->
            exactly_one_burned(tx, refs.challenge_validator_hash, token_name)
          _ -> False
        }
      None -> False
    }

  // Claim token burned in same tx (claim.ak ForfeitClaim runs atomically).
  let claim_token_burned =
    list.any(
      dict.to_pairs(assets.tokens(tx.mint, refs.claim_validator_hash)),
      fn(p) { p.2nd == -1 },
    )

  // Read claimer info from the consumed claim input.
  let claim_stake = find_claim_stake(tx.inputs, refs.claim_validator_hash, config)
  let claimer_cred = find_claimer_cred(tx.inputs, refs.claim_validator_hash)

  // Stake distribution: no jury fee in oracle mode.
  let distribution_ok =
    verify_oracle_distribution(
      config,
      tx,
      verdict,
      claimer_cred,
      ch.auditor_credential,
      claim_stake,
      ch.stake_amount,
    )

  and {
    oracle_signed,
    state_ok,
    hash_ok,
    challenge_token_burned,
    claim_token_burned,
    distribution_ok,
  }
}

// ══════════════════════════════════════════════════════════════════════
// RESOLVE JURY (spend) — Phase 1.1+
// ══════════════════════════════════════════════════════════════════════
//
// Permissionless. Tallies votes, distributes stakes, burns both tokens.

fn validate_resolve_jury(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  ch: ChallengeDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // State must be Voting; extract selected jurors.
  let selected_jurors =
    when ch.state is {
      Voting { selected_jurors } -> selected_jurors
      _ -> fail @"challenge: ResolveJury requires Voting state"
    }

  // Phase 1.1: Read revealed votes from juror reference inputs (commit-reveal).
  // Each juror UTxO at jury_pool_hash with revealed_verdict == Some(_) and
  // active_case matching this challenge's token counts as a valid vote.
  // Find the challenge token name from our own input.
  let challenge_token_name =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash)) is {
          [Pair(tn, 1)] -> tn
          _ -> fail @"challenge: no challenge token in input"
        }
      None -> fail @"challenge: own input not found"
    }

  // Collect revealed verdicts from juror reference inputs at jury_pool address.
  let revealed_votes =
    list.filter_map(
      tx.reference_inputs,
      fn(inp) {
        let at_jury_addr =
          when inp.output.address.payment_credential is {
            Script(h) -> h == refs.jury_pool_hash
            _ -> False
          }
        // fix: CR-CR-02 — verify juror NFT token is present.
        // Without this, attackers can create fake UTxOs at jury_pool address
        // with fabricated JurorDatum containing desired verdicts.
        // Juror tokens are 1-of-1 NFTs — unforgeable.
        let has_juror_token =
          list.length(
            dict.to_pairs(assets.tokens(inp.output.value, refs.jury_pool_hash))
          ) == 1
        if at_jury_addr && has_juror_token {
          when inp.output.datum is {
            InlineDatum(raw) -> {
              expect juror: JurorDatum = raw
              // Must be assigned to THIS challenge
              when juror.active_case is {
                Some(case_token) ->
                  if case_token == challenge_token_name {
                    // Must have revealed verdict
                    when juror.revealed_verdict is {
                      Some(v) -> Some(Pair(juror.juror_did, v))
                      None -> None
                    }
                  } else {
                    None
                  }
                None -> None
              }
            }
            _ -> None
          }
        } else {
          None
        }
      },
    )

  // All jury_size votes must be present.
  let vote_count = list.length(revealed_votes)
  let votes_not_empty = vote_count > 0
  let votes_complete = vote_count == params.jury_size

  // All votes from selected jurors.
  let votes_from_jurors =
    list.all(revealed_votes, fn(p) { list.has(selected_jurors, p.1st) })

  // No duplicate voters.
  let voter_dids = list.map(revealed_votes, fn(p) { p.1st })
  let no_duplicates =
    list.length(voter_dids) == list.length(list.unique(voter_dids))

  // Tally verdicts from on-chain data.
  let verdict = tally_revealed_votes(revealed_votes, params.jury_size)

  // fix: Finding-002 Option A — challenge UTxO continues with Resolved{verdict}
  // instead of being burned. The challenge token is preserved for DistributeRewards
  // to use as a reference input. CleanupResolved burns it later.
  let challenge_continues =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash)) is {
          [Pair(token_name, 1)] -> {
            // Verify continuing output at challenge script address.
            when find_output_at_script(tx.outputs, refs.challenge_validator_hash) is {
              Some(out) ->
                when out.datum is {
                  InlineDatum(raw) -> {
                    expect updated: ChallengeDatum = raw
                    and {
                      // State transitions to Resolved{verdict}.
                      updated.state == Resolved { verdict },
                      // All other fields immutable.
                      updated.claim_ref == ch.claim_ref,
                      updated.auditor_did == ch.auditor_did,
                      updated.auditor_credential == ch.auditor_credential,
                      updated.stake_amount == ch.stake_amount,
                      updated.evidence_hash == ch.evidence_hash,
                      updated.evidence_uri == ch.evidence_uri,
                      updated.challenged_at == ch.challenged_at,
                      updated.resolution_deadline == ch.resolution_deadline,
                      updated.eligible_jurors == ch.eligible_jurors,
                      // Challenge token preserved in output.
                      assets.quantity_of(
                        out.value,
                        refs.challenge_validator_hash,
                        token_name,
                      ) == 1,
                      // AP3X stake preserved (stakes distributed separately to winners).
                      // Note: the AP3X in the continuing output holds the auditor's stake.
                      // Winner payouts and jury fee are separate outputs.
                      ap3x_of(
                        out.value,
                        config.ap3x_policy_id,
                        config.ap3x_asset_name,
                      ) == ch.stake_amount,
                    }
                  }
                  _ -> False
                }
              None -> False
            }
          }
          _ -> False
        }
      None -> False
    }

  // Claim token still burned (claim.ak ForfeitClaim runs atomically).
  let claim_burned =
    list.any(
      dict.to_pairs(assets.tokens(tx.mint, refs.claim_validator_hash)),
      fn(p) { p.2nd == -1 },
    )

  // Stake distribution (jury fee to jury_pool, winner payout).
  let claim_stake = find_claim_stake(tx.inputs, refs.claim_validator_hash, config)
  let claimer_cred = find_claimer_cred(tx.inputs, refs.claim_validator_hash)

  let distribution_ok =
    verify_jury_distribution(
      config,
      refs,
      params,
      tx,
      verdict,
      claimer_cred,
      ch.auditor_credential,
      claim_stake,
      ch.stake_amount,
    )

  // fix: CR-002-F3 — enforce exactly one continuing output at challenge script address.
  // Prevents UTxO pollution from extra outputs (even though they'd lack the challenge token).
  let single_challenge_output =
    list.length(outputs_at_script(tx.outputs, refs.challenge_validator_hash)) == 1

  // Phase 1.1: No oracle signature needed — votes are authenticated on-chain
  // via commit-reveal. ResolveJury is fully permissionless.

  and {
    votes_not_empty,
    votes_complete,
    votes_from_jurors,
    no_duplicates,
    challenge_continues,
    claim_burned,
    distribution_ok,
    single_challenge_output,
  }
}

// ══════════════════════════════════════════════════════════════════════
// TIMEOUT RESOLVE (spend)
// ══════════════════════════════════════════════════════════════════════
//
// Spec §4.8: resolution deadline expired. Return both stakes in full.

fn validate_timeout_resolve(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  ch: ChallengeDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  let deadline_passed =
    tx_started_after(tx, ch.challenged_at + ch.resolution_deadline)

  // PendingOracle must also be covered: if the oracle goes offline in Phase 1.0
  // there must be an on-chain escape hatch to release both stakes.
  let state_ok =
    when ch.state is {
      PendingOracle -> True
      PendingJury -> True
      Voting { .. } -> True
      _ -> False
    }

  let challenge_burned =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash)) is {
          [Pair(token_name, 1)] ->
            exactly_one_burned(tx, refs.challenge_validator_hash, token_name)
          _ -> False
        }
      None -> False
    }

  let claim_burned =
    list.any(
      dict.to_pairs(assets.tokens(tx.mint, refs.claim_validator_hash)),
      fn(p) { p.2nd == -1 },
    )

  let claim_stake = find_claim_stake(tx.inputs, refs.claim_validator_hash, config)
  let claimer_cred = find_claimer_cred(tx.inputs, refs.claim_validator_hash)

  // Exact refund amounts — use == not >= to prevent AP3X draining.
  let claimer_refunded =
    list.any(
      tx.outputs,
      fn(o) {
        and {
          o.address.payment_credential == claimer_cred,
          ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == claim_stake,
        }
      },
    )

  let auditor_refunded =
    list.any(
      tx.outputs,
      fn(o) {
        and {
          o.address.payment_credential == ch.auditor_credential,
          ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == ch.stake_amount,
        }
      },
    )

  and {
    deadline_passed,
    state_ok,
    challenge_burned,
    claim_burned,
    claimer_refunded,
    auditor_refunded,
  }
}

// ══════════════════════════════════════════════════════════════════════
// TRANSITION TO VOTING (spend) — Phase 1.1
// ══════════════════════════════════════════════════════════════════════
//
// Transitions challenge from PendingJury → Voting{selected_jurors}.
// Called atomically in the same TX as jury_pool.ak SelectJury.
// Oracle-controlled: prevents griefing by requiring oracle_vkh signature.
//
// Security:
//   - selected_jurors ⊆ eligible_jurors (frozen snapshot from OpenChallenge)
//   - list.length(selected_jurors) == params.jury_size (exactly 5)
//   - No duplicate jurors in selected_jurors
//   - All other datum fields immutable
//   - Challenge token preserved
//   - AP3X stake preserved

fn validate_transition_to_voting(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  ch: ChallengeDatum,
  own_ref: OutputReference,
  tx: Transaction,
  selected_jurors: List<PolicyId>,
) -> Bool {
  // Current state must be PendingJury.
  let state_ok =
    when ch.state is {
      PendingJury -> True
      _ -> False
    }

  // Phase 1.1: permissionless — anyone can trigger after selection_delay.
  // Time gate: must be after challenged_at + selection_delay.
  let after_selection_delay =
    tx_started_after(tx, ch.challenged_at + params.selection_delay)

  // Extract the challenge token name from the consumed input.
  // This is the PRNG seed — fixed at OpenChallenge time, not manipulable.
  expect Some(inp) = find_input(tx.inputs, own_ref)
  expect [Pair(challenge_token_name, 1)] =
    dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash))

  // Deterministic jury selection: compute on-chain from challenge token name.
  // eligible_jurors is frozen and sorted at OpenChallenge time.
  // select_jurors_prng uses blake2b_256 hash chain for uniform random indices.
  let expected_jurors =
    select_jurors_prng(challenge_token_name, ch.eligible_jurors, params.jury_size)

  // Caller-supplied selected_jurors must match the deterministic selection.
  // Sort both sides because select_jurors_prng returns in accumulator order (reversed).
  let selection_matches =
    sort_dids(selected_jurors) == sort_dids(expected_jurors)

  // Verify continuing output preserves all fields except state → Voting.
  let output_ok =
    when find_output_at_script(tx.outputs, refs.challenge_validator_hash) is {
      Some(out) ->
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: ChallengeDatum = raw
            and {
              // State transitions to Voting with the deterministic juror selection.
              updated.state == Voting { selected_jurors },
              // All other fields immutable.
              updated.claim_ref == ch.claim_ref,
              updated.auditor_did == ch.auditor_did,
              updated.auditor_credential == ch.auditor_credential,
              updated.stake_amount == ch.stake_amount,
              updated.evidence_hash == ch.evidence_hash,
              updated.evidence_uri == ch.evidence_uri,
              updated.challenged_at == ch.challenged_at,
              updated.resolution_deadline == ch.resolution_deadline,
              updated.eligible_jurors == ch.eligible_jurors,
              // Challenge token preserved in output.
              assets.quantity_of(
                out.value,
                refs.challenge_validator_hash,
                challenge_token_name,
              ) == 1,
              // AP3X stake preserved.
              ap3x_of(
                out.value,
                config.ap3x_policy_id,
                config.ap3x_asset_name,
              ) == ch.stake_amount,
            }
          }
          _ -> False
        }
      None -> False
    }

  // fix: CR-002-F3 — enforce exactly one continuing output at challenge script address.
  let single_challenge_output =
    list.length(outputs_at_script(tx.outputs, refs.challenge_validator_hash)) == 1

  and {
    state_ok,
    after_selection_delay,
    selection_matches,
    output_ok,
    single_challenge_output,
  }
}

// ══════════════════════════════════════════════════════════════════════
// CLEANUP RESOLVED (spend) — Phase 1.0+
// ══════════════════════════════════════════════════════════════════════
//
// fix: Finding-002 Option A — deferred burn of the challenge token.
// After DistributeRewards has used the Resolved challenge UTxO as a
// reference input for all jurors, CleanupResolved burns the challenge
// token to reclaim the min-ADA and remove the UTxO from the ledger.
//
// Phase 1.0: oracle signature required (simple and safe).
// Phase 1.1+: could verify no juror UTxOs still reference this challenge.

fn validate_cleanup_resolved(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  ch: ChallengeDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // State must be Resolved (any verdict).
  let state_ok =
    when ch.state is {
      Resolved { .. } -> True
      _ -> False
    }

  // fix: CR-P11-04 + RT-V5 — Time lock with buffer for DistributeRewards.
  // Must be after resolution_deadline + cleanup_buffer to give jurors time to claim fees.
  let after_cleanup_allowed =
    tx_started_after(tx, ch.challenged_at + ch.resolution_deadline + params.cleanup_buffer)

  // Phase 1.1: permissionless — no oracle needed.
  // Guards: state_ok + after_resolution + challenge_burned + no_challenge_outputs.

  // Challenge token burned.
  let challenge_burned =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when dict.to_pairs(assets.tokens(inp.output.value, refs.challenge_validator_hash)) is {
          [Pair(token_name, 1)] ->
            exactly_one_burned(tx, refs.challenge_validator_hash, token_name)
          _ -> False
        }
      None -> False
    }

  // fix: CR-002-F4 — verify no outputs remain at challenge script address after burn.
  // Prevents UTxO pollution (tokenless junk outputs at the challenge address).
  let no_challenge_outputs =
    list.length(outputs_at_script(tx.outputs, refs.challenge_validator_hash)) == 0

  and {
    state_ok,
    after_cleanup_allowed,
    challenge_burned,
    no_challenge_outputs,
  }
}

// ══════════════════════════════════════════════════════════════════════
// PRIVATE HELPERS
// ══════════════════════════════════════════════════════════════════════

/// Read AP3X amount locked in the consumed claim input.
///
/// fix: G1-LIFECYCLE-01 — search by claim script address, not OutputReference.
/// After OpenChallenge, the continuing claim UTxO has a different output_reference
/// than what claim_ref stored in ChallengeDatum points to. The original claim UTxO
/// was consumed by OpenChallenge. Searching by script address is safe because
/// count_script_inputs(tx.inputs, refs.claim_validator_hash) == 1 is enforced in
/// both claim.ak and challenge.ak spend handlers.
fn find_claim_stake(
  inputs: List<Input>,
  claim_validator_hash: ByteArray,
  config: AuditingConfig,
) -> Int {
  when
    list.find(
      inputs,
      fn(i) {
        when i.output.address.payment_credential is {
          Script(h) -> h == claim_validator_hash
          _ -> False
        }
      },
    )
  is {
    Some(inp) ->
      ap3x_of(inp.output.value, config.ap3x_policy_id, config.ap3x_asset_name)
    None -> 0
  }
}

/// Read claimer credential from the consumed claim input's datum.
///
/// fix: G1-LIFECYCLE-01 — search by claim script address, not OutputReference.
/// Same reasoning as find_claim_stake above.
fn find_claimer_cred(
  inputs: List<Input>,
  claim_validator_hash: ByteArray,
) -> Credential {
  expect Some(inp) =
    list.find(
      inputs,
      fn(i) {
        when i.output.address.payment_credential is {
          Script(h) -> h == claim_validator_hash
          _ -> False
        }
      },
    )
  expect InlineDatum(raw) = inp.output.datum
  expect claim: ClaimDatum = raw
  claim.claimer_credential
}

/// Tally votes. Spec §4.6: supermajority → that verdict; else Inconclusive.
/// Tally revealed votes from on-chain juror reference inputs.
/// Input: list of Pair<PolicyId, Verdict> (juror_did, verdict).
fn tally_revealed_votes(
  votes: List<Pair<ByteArray, Verdict>>,
  jury_size: Int,
) -> Verdict {
  let threshold = supermajority_threshold(jury_size)
  let claimer_wins_count =
    list.count(votes, fn(p) {
      when p.2nd is {
        ClaimerWins -> True
        _ -> False
      }
    })
  let auditor_wins_count =
    list.count(votes, fn(p) {
      when p.2nd is {
        AuditorWins -> True
        _ -> False
      }
    })
  if claimer_wins_count >= threshold {
    ClaimerWins
  } else if auditor_wins_count >= threshold {
    AuditorWins
  } else {
    Inconclusive
  }
}

/// Verify oracle-mode distribution (no jury fee).
/// Spec §4.7: winner gets both stakes; Inconclusive → both returned.
fn verify_oracle_distribution(
  config: AuditingConfig,
  tx: Transaction,
  verdict: Verdict,
  claimer_cred: Credential,
  auditor_cred: Credential,
  claim_stake: Int,
  auditor_stake: Int,
) -> Bool {
  // Use == not >= to prevent AP3X draining: any surplus stays locked, not leaked.
  let total = claim_stake + auditor_stake
  when verdict is {
    ClaimerWins ->
      list.any(tx.outputs, fn(o) {
        and {
          o.address.payment_credential == claimer_cred,
          ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == total,
        }
      })
    AuditorWins ->
      list.any(tx.outputs, fn(o) {
        and {
          o.address.payment_credential == auditor_cred,
          ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == total,
        }
      })
    Inconclusive ->
      and {
        list.any(tx.outputs, fn(o) {
          and {
            o.address.payment_credential == claimer_cred,
            ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == claim_stake,
          }
        }),
        list.any(tx.outputs, fn(o) {
          and {
            o.address.payment_credential == auditor_cred,
            ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == auditor_stake,
          }
        }),
      }
  }
}

/// Verify jury-mode distribution with jury fee.
/// Spec §4.6: loser forfeits stake; winner gets stake-pair minus fee; jury pool gets fee.
fn verify_jury_distribution(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  params: ProtocolParams,
  tx: Transaction,
  verdict: Verdict,
  claimer_cred: Credential,
  auditor_cred: Credential,
  claim_stake: Int,
  auditor_stake: Int,
) -> Bool {
  // Use == not >= to prevent AP3X draining.
  // fix: NEW-R2-03 — jury fee must reach jury pool
  when verdict is {
    ClaimerWins -> {
      let jury_fee = compute_jury_fee(auditor_stake, params.jury_fee_rate)
      let claimer_payout = claim_stake + auditor_stake - jury_fee
      let winner_ok =
        list.any(tx.outputs, fn(o) {
          and {
            o.address.payment_credential == claimer_cred,
            ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == claimer_payout,
          }
        })
      // fix: NEW-R2-03 — jury fee must reach jury pool
      let fee_routed =
        list.any(tx.outputs, fn(o) {
          when o.address.payment_credential is {
            Script(h) ->
              and {
                h == refs.jury_pool_hash,
                ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == jury_fee,
              }
            _ -> False
          }
        })
      and {
        winner_ok,
        fee_routed,
      }
    }
    AuditorWins -> {
      let jury_fee = compute_jury_fee(claim_stake, params.jury_fee_rate)
      let auditor_payout = auditor_stake + claim_stake - jury_fee
      let winner_ok =
        list.any(tx.outputs, fn(o) {
          and {
            o.address.payment_credential == auditor_cred,
            ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == auditor_payout,
          }
        })
      // fix: NEW-R2-03 — jury fee must reach jury pool
      let fee_routed =
        list.any(tx.outputs, fn(o) {
          when o.address.payment_credential is {
            Script(h) ->
              and {
                h == refs.jury_pool_hash,
                ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == jury_fee,
              }
            _ -> False
          }
        })
      and {
        winner_ok,
        fee_routed,
      }
    }
    Inconclusive -> {
      let total_jury_fee =
        compute_jury_fee(claim_stake + auditor_stake, params.jury_fee_rate)
      let half_fee = total_jury_fee / 2
      let claimer_ok =
        list.any(tx.outputs, fn(o) {
          and {
            o.address.payment_credential == claimer_cred,
            ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == claim_stake - half_fee,
          }
        })
      let auditor_ok =
        list.any(tx.outputs, fn(o) {
          and {
            o.address.payment_credential == auditor_cred,
            ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == auditor_stake - half_fee,
          }
        })
      // fix: NEW-R2-03 — jury fee must reach jury pool
      let fee_routed =
        list.any(tx.outputs, fn(o) {
          when o.address.payment_credential is {
            Script(h) ->
              and {
                h == refs.jury_pool_hash,
                ap3x_of(o.value, config.ap3x_policy_id, config.ap3x_asset_name) == total_jury_fee,
              }
            _ -> False
          }
        })
      and {
        claimer_ok,
        auditor_ok,
        fee_routed,
      }
    }
  }
}

// ══════════════════════════════════════════════════════════════════════
// OPEN CHALLENGE — FULL TX MOCK TESTS
// ══════════════════════════════════════════════════════════════════════
//
// These tests construct a complete mock Transaction and run it through
// validate_open_challenge to isolate which validation check fails.
// Written to debug the Step 4 on-chain ValidationTagMismatch blocker.
//
// Strategy: build a KNOWN-GOOD base TX that passes all checks, then
// break one check at a time to verify each condition independently.
//
// Author: Apex Security Audit Team — debugging Step 4 blocker
// Date: 2026-03-29

// ── Test constants ──────────────────────────────────────────────────

const t_claim_validator_hash: ByteArray =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

const t_challenge_validator_hash: ByteArray =
  #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

const t_jury_pool_hash: ByteArray =
  #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

const t_ap3x_policy_id: ByteArray =
  #"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"

const t_ap3x_asset_name: ByteArray =
  #"415033580000000000000000000000000000000000000000000000000000"

const t_registry_policy_id: ByteArray =
  #"d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1"

const t_refs_token_policy: ByteArray =
  #"9999999999999999999999999999999999999999999999999999999999"

const t_oracle_vkh: ByteArray =
  #"0000000000000000000000000000000000000000000000000000000000000001"

const t_claimer_vkh: ByteArray =
  #"1111111111111111111111111111111111111111111111111111111111111111"

const t_claimer_did: ByteArray =
  #"2222222222222222222222222222222222222222222222222222222222"

const t_auditor_vkh: ByteArray =
  #"3333333333333333333333333333333333333333333333333333333333333333"

const t_auditor_did: ByteArray =
  #"4444444444444444444444444444444444444444444444444444444444"

const t_seed_tx_hash: ByteArray =
  #"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

const t_claim_tx_hash: ByteArray =
  #"aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000"

const t_valid_evidence_hash: ByteArray =
  #"cafebabecafebabecafebabecafebabecafebabecafebabecafebabecafebabe"

// ── Test fixture helpers ────────────────────────────────────────────

fn t_config() -> AuditingConfig {
  AuditingConfig {
    registry_policy_id: t_registry_policy_id,
    ap3x_policy_id: t_ap3x_policy_id,
    ap3x_asset_name: t_ap3x_asset_name,
    oracle_vkh: t_oracle_vkh,
    refs_token_policy: t_refs_token_policy,
  }
}

fn t_params_oracle() -> ProtocolParams {
  ProtocolParams {
    min_claim_stake: 50_000_000,
    min_challenge_window: 1_800_000,
    max_challenge_window: 64_800_000,
    jury_size: 5,
    min_juror_bond: 25_000_000,
    jury_fee_rate: 1000,
    selection_delay: 30_000,
    resolution_deadline: 5_400_000,
    juror_slash_rate: 1000,
    min_agent_age: 21_600_000,
    max_concurrent_cases: 5,
    min_jury_pool_size: 10,
    min_jury_pool_total: 250_000_000,
    oracle_active: True,
    commit_window: 3_600_000,
    reveal_window: 1_800_000,
    cleanup_buffer: 1_800_000,
  }
}

fn t_params_jury() -> ProtocolParams {
  ProtocolParams { ..t_params_oracle(), oracle_active: False }
}

fn t_seed_ref() -> OutputReference {
  OutputReference { transaction_id: t_seed_tx_hash, output_index: 0 }
}

fn t_claim_ref() -> OutputReference {
  OutputReference { transaction_id: t_claim_tx_hash, output_index: 0 }
}

fn t_chl_token_name() -> ByteArray {
  challenge_token_name(t_seed_ref())
}

fn t_make_ap3x(amount: Int) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(t_ap3x_policy_id, t_ap3x_asset_name, amount)
}

fn t_cross_refs() -> CrossValidatorRefs {
  CrossValidatorRefs {
    claim_validator_hash: t_claim_validator_hash,
    challenge_validator_hash: t_challenge_validator_hash,
    jury_pool_hash: t_jury_pool_hash,
  }
}

/// Refs UTxO as reference input — authenticated by refs_token_policy NFT
fn t_refs_input() -> Input {
  Input {
    output_reference: OutputReference {
      transaction_id: #"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      output_index: 0,
    },
    output: Output {
      address: Address {
        payment_credential: Script(#"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000)
        |> assets.add(t_refs_token_policy, #"72656673", 1),
      datum: InlineDatum(t_cross_refs()),
      reference_script: None,
    },
  }
}

/// Registry reference input for auditor DID verification
fn t_auditor_registry_input() -> Input {
  Input {
    output_reference: OutputReference {
      transaction_id: #"ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00",
      output_index: 0,
    },
    output: Output {
      address: Address {
        payment_credential: Script(#"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000)
        |> assets.add(t_registry_policy_id, t_auditor_did, 1),
      datum: NoDatum,
      reference_script: None,
    },
  }
}

/// Open claim datum that the auditor is challenging
fn t_open_claim_datum() -> ClaimDatum {
  ClaimDatum {
    claimer_did: t_claimer_did,
    claimer_credential: VerificationKey(t_claimer_vkh),
    claim_hash: t_valid_evidence_hash,
    claim_type: "data_indexing",
    storage_uri: "ipfs://QmClaim",
    stake_amount: 100_000_000,
    submitted_at: 1000,
    challenge_window: 3_600_000,
    state: Open,
  }
}

/// Claim input UTxO (consumed in the same TX by MarkChallenged)
fn t_claim_input() -> Input {
  Input {
    output_reference: t_claim_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(t_claim_validator_hash),
        stake_credential: None,
      },
      value: t_make_ap3x(100_000_000)
        |> assets.add(t_claim_validator_hash, claim_token_name(t_seed_ref()), 1),
      datum: InlineDatum(t_open_claim_datum()),
      reference_script: None,
    },
  }
}

/// Seed input (wallet UTxO — first consumed input for token name derivation)
fn t_seed_input() -> Input {
  Input {
    output_reference: t_seed_ref(),
    output: Output {
      address: Address {
        payment_credential: VerificationKey(t_auditor_vkh),
        stake_credential: None,
      },
      value: assets.from_lovelace(50_000_000),
      datum: NoDatum,
      reference_script: None,
    },
  }
}

/// Challenge datum for the new challenge UTxO
fn t_challenge_datum() -> ChallengeDatum {
  ChallengeDatum {
    claim_ref: t_claim_ref(),
    auditor_did: t_auditor_did,
    auditor_credential: VerificationKey(t_auditor_vkh),
    stake_amount: 100_000_000,
    evidence_hash: t_valid_evidence_hash,
    evidence_uri: "ipfs://QmEvidence",
    challenged_at: 2000,
    resolution_deadline: 5_400_000,
    eligible_jurors: [],
    state: PendingOracle,
  }
}

/// Challenge output UTxO
fn t_challenge_output() -> Output {
  Output {
    address: Address {
      payment_credential: Script(t_challenge_validator_hash),
      stake_credential: None,
    },
    value: t_make_ap3x(100_000_000)
      |> assets.add(t_challenge_validator_hash, t_chl_token_name(), 1),
    datum: InlineDatum(t_challenge_datum()),
    reference_script: None,
  }
}

/// Claim continuing output (state changed to Challenged)
fn t_claim_continuing_output() -> Output {
  Output {
    address: Address {
      payment_credential: Script(t_claim_validator_hash),
      stake_credential: None,
    },
    value: t_make_ap3x(100_000_000)
      |> assets.add(t_claim_validator_hash, claim_token_name(t_seed_ref()), 1),
    datum: InlineDatum(ClaimDatum { ..t_open_claim_datum(), state: Challenged }),
    reference_script: None,
  }
}

/// Build the KNOWN-GOOD base transaction for OpenChallenge.
/// This is the "golden" TX that should pass all validation checks.
fn t_base_open_challenge_tx() -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [t_seed_input(), t_claim_input()],
    reference_inputs: [t_refs_input(), t_auditor_registry_input()],
    outputs: [t_challenge_output(), t_claim_continuing_output()],
    mint: assets.zero
      |> assets.add(t_challenge_validator_hash, t_chl_token_name(), 1),
    extra_signatories: [t_auditor_vkh],
    validity_range: interval.between(1500, 3000),
  }
}

// ── HAPPY PATH: validate_open_challenge passes with correct TX ──────

test test_voc_happy_path_oracle_mode() {
  let config = t_config()
  let params = t_params_oracle()
  let tx = t_base_open_challenge_tx()
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_happy_path_jury_mode() {
  let config = t_config()
  let params = t_params_jury()
  let tx = t_base_open_challenge_tx()
  // In jury mode, state must be PendingJury with >= 3*jury_size eligible jurors
  let eligible_pool = [
    #"0100000000000000000000000000000000000000000000000000000000",
    #"0200000000000000000000000000000000000000000000000000000000",
    #"0300000000000000000000000000000000000000000000000000000000",
    #"0400000000000000000000000000000000000000000000000000000000",
    #"0500000000000000000000000000000000000000000000000000000000",
    #"0600000000000000000000000000000000000000000000000000000000",
    #"0700000000000000000000000000000000000000000000000000000000",
    #"0800000000000000000000000000000000000000000000000000000000",
    #"0900000000000000000000000000000000000000000000000000000000",
    #"0a00000000000000000000000000000000000000000000000000000000",
    #"0b00000000000000000000000000000000000000000000000000000000",
    #"0c00000000000000000000000000000000000000000000000000000000",
    #"0d00000000000000000000000000000000000000000000000000000000",
    #"0e00000000000000000000000000000000000000000000000000000000",
    #"0f00000000000000000000000000000000000000000000000000000000",
  ]
  let jury_datum =
    ChallengeDatum {
      ..t_challenge_datum(),
      state: PendingJury,
      eligible_jurors: eligible_pool,
    }
  let jury_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(jury_datum),
  }
  let jury_tx = Transaction {
    ..tx,
    outputs: [jury_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, jury_tx)
}

// ── CHECK 1: correct_mint — exactly one challenge token minted ──────

test test_voc_fail_no_mint() fail {
  let config = t_config()
  let params = t_params_oracle()
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    mint: assets.zero,
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_fail_mint_two_tokens() fail {
  let config = t_config()
  let params = t_params_oracle()
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    mint: assets.zero
      |> assets.add(t_challenge_validator_hash, t_chl_token_name(), 2),
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_fail_mint_wrong_name() fail {
  let config = t_config()
  let params = t_params_oracle()
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    mint: assets.zero
      |> assets.add(t_challenge_validator_hash, #"deadbeef", 1),
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 2: single_output — one output at challenge script ─────────

test test_voc_fail_no_challenge_output() fail {
  let config = t_config()
  let params = t_params_oracle()
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_fail_two_challenge_outputs() fail {
  let config = t_config()
  let params = t_params_oracle()
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [t_challenge_output(), t_challenge_output(), t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 3: has_token — challenge token in output ──────────────────

test test_voc_fail_output_missing_token() fail {
  let config = t_config()
  let params = t_params_oracle()
  let bad_output = Output {
    address: Address {
      payment_credential: Script(t_challenge_validator_hash),
      stake_credential: None,
    },
    value: t_make_ap3x(100_000_000),
    // No challenge token in value
    datum: InlineDatum(t_challenge_datum()),
    reference_script: None,
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 4: no_self_audit — auditor_did != claimer_did ─────────────

test test_voc_fail_self_audit() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Challenge datum with auditor_did == claimer_did
  let self_audit_datum = ChallengeDatum { ..t_challenge_datum(), auditor_did: t_claimer_did }
  let bad_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(self_audit_datum),
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 5: within_window — challenge within claim's challenge window ──

test test_voc_fail_outside_window() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Claim window: submitted_at=1000 + challenge_window=3_600_000 = 3_601_000
  // Make tx validity end AFTER the window (entirely after the window)
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    // validity range well past window: ends_before(3_601_000) will be False
    validity_range: interval.between(4_000_000, 5_000_000),
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 6: stake_sufficient — auditor stake >= claim stake ────────

test test_voc_fail_insufficient_stake() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Challenge with stake < claim_stake (99M < 100M)
  let low_stake_datum = ChallengeDatum { ..t_challenge_datum(), stake_amount: 99_000_000 }
  let bad_output = Output {
    address: Address {
      payment_credential: Script(t_challenge_validator_hash),
      stake_credential: None,
    },
    value: t_make_ap3x(99_000_000)
      |> assets.add(t_challenge_validator_hash, t_chl_token_name(), 1),
    datum: InlineDatum(low_stake_datum),
    reference_script: None,
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 7: ap3x_locked — AP3X in output matches stake_amount ──────

test test_voc_fail_ap3x_mismatch() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Output has 50M AP3X but datum says stake=100M
  let bad_output = Output {
    address: Address {
      payment_credential: Script(t_challenge_validator_hash),
      stake_credential: None,
    },
    value: t_make_ap3x(50_000_000)
      |> assets.add(t_challenge_validator_hash, t_chl_token_name(), 1),
    datum: InlineDatum(t_challenge_datum()),
    reference_script: None,
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 8: auditor_did_ok — DID verified in registry ──────────────

test test_voc_fail_no_registry_ref() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Remove auditor registry reference input
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    reference_inputs: [t_refs_input()],
    // Only refs UTxO, no registry
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 9: evidence_hash_ok — hash must be 32 bytes ───────────────

test test_voc_fail_bad_evidence_hash() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Short hash (16 bytes instead of 32)
  let bad_datum = ChallengeDatum { ..t_challenge_datum(), evidence_hash: #"cafebabecafebabe" }
  let bad_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(bad_datum),
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 10: state_ok — oracle mode expects PendingOracle ──────────

test test_voc_fail_wrong_state_oracle_mode() fail {
  let config = t_config()
  let params = t_params_oracle()
  // PendingJury in oracle mode
  let bad_datum = ChallengeDatum { ..t_challenge_datum(), state: PendingJury }
  let bad_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(bad_datum),
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_fail_wrong_state_jury_mode() fail {
  let config = t_config()
  let params = t_params_jury()
  // PendingOracle in jury mode
  let tx = t_base_open_challenge_tx()
  // t_challenge_datum() has PendingOracle state, but jury mode expects PendingJury
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 11: jurors_sorted — eligible_jurors must be sorted ────────

test test_voc_fail_unsorted_jurors() fail {
  let config = t_config()
  let params = t_params_jury()
  // Unsorted jurors
  let bad_datum = ChallengeDatum {
    ..t_challenge_datum(),
    state: PendingJury,
    eligible_jurors: [
      #"bb00000000000000000000000000000000000000000000000000000000",
      #"aa00000000000000000000000000000000000000000000000000000000",
    ],
  }
  let bad_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(bad_datum),
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 12: challenged_at_ok — must be in validity range ──────────

test test_voc_fail_challenged_at_before_range() fail {
  let config = t_config()
  let params = t_params_oracle()
  // challenged_at=100, validity_range=[1500, 3000] → 100 not in range
  let bad_datum = ChallengeDatum { ..t_challenge_datum(), challenged_at: 100 }
  let bad_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(bad_datum),
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_fail_challenged_at_after_range() fail {
  let config = t_config()
  let params = t_params_oracle()
  // challenged_at=5000, validity_range=[1500, 3000] → 5000 not in range
  let bad_datum = ChallengeDatum { ..t_challenge_datum(), challenged_at: 5000 }
  let bad_output = Output {
    ..t_challenge_output(),
    datum: InlineDatum(bad_datum),
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [bad_output, t_claim_continuing_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 13: signed — auditor credential signed ────────────────────

test test_voc_fail_no_auditor_sig() fail {
  let config = t_config()
  let params = t_params_oracle()
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    extra_signatories: [],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 14: claim_updated — claim output with Challenged state ────

test test_voc_fail_claim_not_updated() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Claim continuing output still in Open state (not Challenged)
  let bad_claim_output = Output {
    address: Address {
      payment_credential: Script(t_claim_validator_hash),
      stake_credential: None,
    },
    value: t_make_ap3x(100_000_000)
      |> assets.add(t_claim_validator_hash, claim_token_name(t_seed_ref()), 1),
    datum: InlineDatum(t_open_claim_datum()),
    // Still Open, not Challenged
    reference_script: None,
  }
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [t_challenge_output(), bad_claim_output],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

test test_voc_fail_no_claim_output() fail {
  let config = t_config()
  let params = t_params_oracle()
  // No claim output at all
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    outputs: [t_challenge_output()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}

// ── CHECK 15: claim_ref must exist in TX inputs ─────────────────────

test test_voc_fail_claim_not_in_inputs() fail {
  let config = t_config()
  let params = t_params_oracle()
  // Remove claim input — only seed input remains
  let tx = Transaction {
    ..t_base_open_challenge_tx(),
    inputs: [t_seed_input()],
  }
  validate_open_challenge(config, params, t_challenge_validator_hash, tx)
}
