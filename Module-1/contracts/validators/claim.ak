/// claim.ak — Claim Multi-Validator for Game 1: Adversarial Auditing
///
/// Controls the lifecycle of claim UTXOs. Each claim is an independent UTXO
/// — fully parallelizable. No global state contention.
///
/// Mint handler:
///   SubmitClaim  → mint one claim token, create ClaimUTXO at state=Open
///
/// Spend handler:
///   WithdrawClaim   → challenge window expired, no challenge; burn token, return stake
///   MarkChallenged  → atomically invoked in OpenChallenge tx; state Open→Challenged
///   ForfeitClaim    → oracle/jury resolution; burn token, allow stake redistribution
///
/// Security patterns:
///   - count_script_inputs == 1 (double-satisfaction prevention)
///   - Inline datums only (CIP-32)
///   - Token lifecycle: minted on SubmitClaim, burned on withdrawal/forfeit
///   - DID verified via Agent Registry reference input (CIP-31)

use aiken/cbor
use aiken/collection/dict
use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, OutputReference, Transaction}
use adversarial_auditing/params.{ProtocolParams, challenge_window_valid}
use adversarial_auditing/types.{
  AuditingConfig, Challenged, ChallengeDatum, ClaimAction, ClaimDatum,
  CrossValidatorRefs, ForfeitClaim, MarkChallenged, Open, Resolved,
  SubmitClaim, WithdrawClaim,
}
use adversarial_auditing/utils.{
  challenge_token_name, claim_token_name, count_script_inputs,
  credential_signed, exactly_one_burned, exactly_one_minted, find_input,
  find_output_at_script, get_cross_refs, is_valid_hash, outputs_at_script,
  tx_contains_slot, tx_started_after, verify_active_did,
}

// ══════════════════════════════════════════════════════════════════════
// MULTI-VALIDATOR
// ══════════════════════════════════════════════════════════════════════

validator claim(config: AuditingConfig, params: ProtocolParams) {
  // ────────────────────────────────────────────────────────────────────
  // MINT HANDLER — token creation only
  // ────────────────────────────────────────────────────────────────────

  mint(redeemer: ClaimAction, policy_id: PolicyId, tx: Transaction) {
    when redeemer is {
      SubmitClaim -> validate_submit_claim(config, params, policy_id, tx)
      // Burn path: WithdrawClaim and ForfeitClaim both burn the claim token.
      // The corresponding spend handler (WithdrawClaim/ForfeitClaim) enforces
      // all conditions — the mint handler just permits negative quantity.
      WithdrawClaim -> {
        // Allow burn: spend handler enforces conditions (state=Open, window expired, etc.)
        let is_burning =
          list.any(
            dict.to_pairs(assets.tokens(tx.mint, policy_id)),
            fn(p) { p.2nd == -1 },
          )
        is_burning
      }
      ForfeitClaim -> {
        // Allow burn: spend handler enforces conditions (state=Challenged, oracle signed, etc.)
        let is_burning =
          list.any(
            dict.to_pairs(assets.tokens(tx.mint, policy_id)),
            fn(p) { p.2nd == -1 },
          )
        is_burning
      }
      _ ->
        fail @"claim mint: unsupported redeemer for mint"
    }
  }

  // ────────────────────────────────────────────────────────────────────
  // SPEND HANDLER — state transitions
  // ────────────────────────────────────────────────────────────────────

  spend(
    datum: Option<ClaimDatum>,
    redeemer: ClaimAction,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    expect Some(clm) = datum
    // Resolve cross-validator refs from authenticated reference input.
    let refs = get_cross_refs(config, tx.reference_inputs)
    // Double-satisfaction guard: exactly one claim script input per tx.
    // Prevents one output from satisfying two validator invocations.
    let single_input =
      count_script_inputs(tx.inputs, refs.claim_validator_hash) == 1
    expect single_input
    when redeemer is {
      WithdrawClaim ->
        validate_withdraw_claim(config, refs, clm, own_ref, tx)
      MarkChallenged ->
        validate_mark_challenged(config, refs, clm, own_ref, tx)
      ForfeitClaim ->
        validate_forfeit_claim(refs, clm, own_ref, tx)
      SubmitClaim ->
        fail @"claim spend: SubmitClaim is a mint action"
    }
  }

  else(_ctx) {
    fail @"claim: unsupported script purpose"
  }
}

// ══════════════════════════════════════════════════════════════════════
// SUBMIT CLAIM (mint)
// ══════════════════════════════════════════════════════════════════════
//
// Spec §4.1: claimer stakes AP3X to assert a task completion claim.
// Creates a ClaimUTXO at state=Open with a tracking token.

fn validate_submit_claim(
  config: AuditingConfig,
  params: ProtocolParams,
  policy_id: PolicyId,
  tx: Transaction,
) -> Bool {
  // Derive token name from first consumed input (deterministic seed).
  trace @"DBG: before seed_input expect"
  expect [seed_input, ..] = tx.inputs
  trace @"DBG: after seed_input expect"
  let token_name = claim_token_name(seed_input.output_reference)
  trace @"DBG: token_name derived"

  // Exactly one claim token minted under this policy.
  let correct_mint = exactly_one_minted(tx, policy_id, token_name)
  trace @"DBG: correct_mint computed"
  trace cbor.diagnostic(correct_mint)

  // Find the unique claim output at our validator address.
  // In the mint handler, policy_id IS the claim validator's own script hash.
  let clm_outputs =
    list.filter(
      tx.outputs,
      fn(o) {
        when o.address.payment_credential is {
          Script(h) -> h == policy_id
          _ -> False
        }
      },
    )
  let single_output = list.length(clm_outputs) == 1
  trace @"DBG: single_output computed"
  trace cbor.diagnostic(single_output)
  trace @"DBG: num outputs at script"
  trace cbor.diagnostic(list.length(clm_outputs))

  let output_checks =
    when clm_outputs is {
      [clm_output] -> {
        // Output must carry an inline datum parseable as ClaimDatum.
        trace @"DBG: before InlineDatum expect"
        expect InlineDatum(raw) = clm_output.datum
        trace @"DBG: after InlineDatum expect"
        expect clm_d: ClaimDatum = raw
        trace @"DBG: after ClaimDatum expect"

        // Claim token must be in the output.
        let has_token =
          assets.quantity_of(clm_output.value, policy_id, token_name) == 1

        // State must be Open.
        let state_is_open =
          when clm_d.state is {
            Open -> True
            _ -> False
          }

        // Claimer must have active DID in the Agent Registry.
        let has_active_did =
          verify_active_did(config, clm_d.claimer_did, tx.reference_inputs)

        // Stake must meet minimum.
        let stake_ok = clm_d.stake_amount >= params.min_claim_stake

        // AP3X locked in output must exactly match declared stake_amount.
        let ap3x_locked =
          assets.lovelace_of(clm_output.value) == clm_d.stake_amount

        // Claim hash must be exactly 32 bytes (blake2b_256 output).
        let hash_ok = is_valid_hash(clm_d.claim_hash)

        // Claim type must be non-empty.
        let type_ok = clm_d.claim_type != #""

        // Challenge window within governance bounds.
        let window_ok =
          challenge_window_valid(
            clm_d.challenge_window,
            params.min_challenge_window,
            params.max_challenge_window,
          )

        // submitted_at must fall within the tx validity range — not claimer-supplied freely.
        // Prevents Attack A (backdating to slip past challenge window) and
        // Attack B (future-dating to permanently block challenges).
        let submitted_at_ok = tx_contains_slot(tx, clm_d.submitted_at)

        // Transaction signed by claimer's credential.
        let signed = credential_signed(tx, clm_d.claimer_credential)

        and {
          has_token,
          state_is_open,
          has_active_did,
          stake_ok,
          ap3x_locked,
          hash_ok,
          type_ok,
          window_ok,
          submitted_at_ok,
          signed,
        }
      }
      _ -> False
    }

  and {
    correct_mint,
    single_output,
    output_checks,
  }
}

// ══════════════════════════════════════════════════════════════════════
// WITHDRAW CLAIM (spend)
// ══════════════════════════════════════════════════════════════════════
//
// Spec §4.2: claimer reclaims stake after challenge window expires with no challenge.

fn validate_withdraw_claim(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  clm: ClaimDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // 1. State must be Open (never challenged).
  let state_ok =
    when clm.state is {
      Open -> True
      _ -> False
    }

  // 2. Challenge window must have fully elapsed.
  let window_expired =
    tx_started_after(tx, clm.submitted_at + clm.challenge_window)

  // 3. Find the claim token in this input and confirm it's burned.
  let token_burned =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when
          dict.to_pairs(
            assets.tokens(inp.output.value, refs.claim_validator_hash),
          )
        is {
          [Pair(token_name, 1)] ->
            exactly_one_burned(tx, refs.claim_validator_hash, token_name)
          _ -> False
        }
      None -> False
    }

  // 4. No continuing output at claim address.
  let no_continuing =
    find_output_at_script(tx.outputs, refs.claim_validator_hash) == None

  // 5. Stake returned to claimer (exact amount — prevent AP3X draining via >= ).
  let stake_returned =
    list.any(
      tx.outputs,
      fn(o) {
        and {
          o.address.payment_credential == clm.claimer_credential,
          assets.lovelace_of(o.value) == clm.stake_amount,
        }
      },
    )

  // 6. Signed by claimer.
  let signed = credential_signed(tx, clm.claimer_credential)

  and {
    state_ok,
    window_expired,
    token_burned,
    no_continuing,
    stake_returned,
    signed,
  }
}

// ══════════════════════════════════════════════════════════════════════
// MARK CHALLENGED (spend)
// ══════════════════════════════════════════════════════════════════════
//
// Called atomically within the OpenChallenge tx from challenge.ak.
// Transitions claim state: Open → Challenged.
// Does NOT burn the claim token — token persists until resolution.

fn validate_mark_challenged(
  config: AuditingConfig,
  refs: CrossValidatorRefs,
  clm: ClaimDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // 1. State must be Open.
  let state_ok =
    when clm.state is {
      Open -> True
      _ -> False
    }

  // 2. A challenge token bound to the first consumed input (seed UTXO) must be minted.
  //    We verify the token name matches challenge_token_name(seed_input.output_reference),
  //    which is exactly how validate_open_challenge derives it in challenge.ak.
  //    This prevents binding a challenge against a different claim's UTXO.
  //    The first input of the tx is the seed input (same ordering guarantee as OpenChallenge).
  let challenge_minted =
    when tx.inputs is {
      [seed_input, ..] -> {
        let expected_token_name =
          challenge_token_name(seed_input.output_reference)
        assets.quantity_of(
          tx.mint,
          refs.challenge_validator_hash,
          expected_token_name,
        ) == 1
      }
      _ -> False
    }

  // 3. Derive the claim token name from the consumed input (own_ref).
  //    Used to verify the token is preserved in the continuing output.
  let token_name =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when
          dict.to_pairs(
            assets.tokens(inp.output.value, refs.claim_validator_hash),
          )
        is {
          [Pair(name, 1)] -> name
          _ -> fail @"claim: no token in claim input"
        }
      None -> fail @"claim: own input not found"
    }

  // 4. Find the continuing output at claim address and verify state updated.
  let correct_continuation =
    when find_output_at_script(tx.outputs, refs.claim_validator_hash) is {
      Some(out) ->
        when out.datum is {
          InlineDatum(raw) -> {
            expect updated: ClaimDatum = raw
            // State must now be Challenged
            let state_updated =
              when updated.state is {
                Challenged -> True
                _ -> False
              }
            // All other fields must be identical (immutable)
            let fields_preserved =
              and {
                updated.claimer_did == clm.claimer_did,
                updated.claimer_credential == clm.claimer_credential,
                updated.claim_hash == clm.claim_hash,
                updated.claim_type == clm.claim_type,
                updated.storage_uri == clm.storage_uri,
                updated.stake_amount == clm.stake_amount,
                updated.submitted_at == clm.submitted_at,
                updated.challenge_window == clm.challenge_window,
              }
            // AP3X stake must remain locked in the continuing output
            let ap3x_preserved =
              assets.lovelace_of(out.value) == clm.stake_amount
            // fix: NEW-R2-01 — claim token must stay in continuing output
            let token_preserved =
              assets.quantity_of(
                out.value,
                refs.claim_validator_hash,
                token_name,
              ) == 1
            and {
              state_updated,
              fields_preserved,
              ap3x_preserved,
              token_preserved,
            }
          }
          _ -> False
        }
      None -> False
    }

  and {
    state_ok,
    challenge_minted,
    correct_continuation,
  }
}

// ══════════════════════════════════════════════════════════════════════
// FORFEIT CLAIM (spend)
// ══════════════════════════════════════════════════════════════════════
//
// Called during resolution when the claimer loses (AuditorWins verdict).
// Burns claim token. Challenge.ak orchestrates stake redistribution.

fn validate_forfeit_claim(
  refs: CrossValidatorRefs,
  clm: ClaimDatum,
  own_ref: OutputReference,
  tx: Transaction,
) -> Bool {
  // 1. State must be Challenged.
  let state_ok =
    when clm.state is {
      Challenged -> True
      _ -> False
    }

  // 2. Claim token must be burned in this tx.
  let token_burned =
    when find_input(tx.inputs, own_ref) is {
      Some(inp) ->
        when
          dict.to_pairs(
            assets.tokens(inp.output.value, refs.claim_validator_hash),
          )
        is {
          [Pair(token_name, 1)] ->
            exactly_one_burned(tx, refs.claim_validator_hash, token_name)
          _ -> False
        }
      None -> False
    }

  // 3. Challenge must be resolving in this TX.
  // fix: Finding-002 + Finding-v10-F1 — accept EITHER:
  //   (a) Challenge token burned (OracleResolve, TimeoutResolve) — original path
  //   (b) Challenge UTxO transitions to Resolved{..} state (ResolveJury)
  //
  // CRITICAL: Do NOT accept bare "challenge_being_spent" — an attacker could
  // pair SubmitEvidence (non-resolution spend) with ForfeitClaim to steal
  // the claimer's stake. Must verify the challenge actually reaches Resolved.
  let challenge_token_burned =
    list.any(
      dict.to_pairs(assets.tokens(tx.mint, refs.challenge_validator_hash)),
      fn(p) { p.2nd == -1 },
    )
  // fix: Finding-v10-F1 + Finding-001 — check for Resolved{..} state in continuing
  // output AND verify it contains a legitimate challenge token.
  // CRITICAL: In eUTxO, anyone can create outputs at any script address without
  // the script executing. Without token verification, an attacker can fabricate
  // a Resolved datum output at the challenge address. Challenge tokens are 1-of-1
  // NFTs minted only through validate_open_challenge — unforgeable.
  let challenge_resolved_via_spend =
    list.any(
      outputs_at_script(tx.outputs, refs.challenge_validator_hash),
      fn(o) {
        // Must contain a legitimate challenge token (1-of-1 NFT)
        let has_challenge_token =
          list.length(
            dict.to_pairs(assets.tokens(o.value, refs.challenge_validator_hash))
          ) == 1
        when o.datum is {
          InlineDatum(raw) -> {
            expect ch: ChallengeDatum = raw
            when ch.state is {
              Resolved { .. } -> has_challenge_token
              _ -> False
            }
          }
          _ -> False
        }
      },
    )
  let challenge_resolving = challenge_token_burned || challenge_resolved_via_spend

  and {
    state_ok,
    token_burned,
    challenge_resolving,
  }
}
