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
///   - Claimer identity checked in the identity registry (Part 2 §7):
///     effective status Active and a declared credential that is the
///     record's controller or a listed transaction key

use aiken/cbor
use aiken/collection/dict
use aiken/collection/list
use aiken/crypto.{blake2b_224}
use aiken/interval
use cardano/address.{Credential, Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{
  InlineDatum, Input, NoDatum, Output, OutputReference, Transaction,
  placeholder,
}
use adversarial_auditing/identity_checks.{identity_ok}
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
  tx_contains_slot, tx_started_after,
}
use vector_identity/types.{
  Active, ControllerSet, Hold, IdentityDatum, Revoked, Suspended,
} as vi
use adversarial_auditing_tests/identity_fixtures.{
  agent_a, agent_b, auditing_config, auth_key_a, claim_hash, controller_a,
  hash_32, key_address, key_credential, largest_record, lo, out_ref,
  placeholder_verdict_policy, protocol_params, record_a, record_input,
  script_address, stranger_key, tx_key_a, window_range,
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

        // Claimer identity: record found, effective status Active over the
        // transaction window (at most one hour), and the declared credential
        // is the controller or a listed key with the transaction purpose.
        let has_active_did =
          identity_ok(
            config.identity_hash,
            clm_d.claimer_did,
            clm_d.claimer_credential,
            tx,
          )

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

// ══════════════════════════════════════════════════════════════════════
// SUBMIT CLAIM — IDENTITY CHECKS (Part 2 §7, Module-1 row)
// ══════════════════════════════════════════════════════════════════════
//
// Full SubmitClaim transactions with the claimer's identity record as a
// reference input. Each negative test changes one thing.

fn s_seed() -> Input {
  Input {
    output_reference: out_ref(0x5c, 0),
    output: Output {
      address: key_address(blake2b_224(tx_key_a)),
      value: assets.from_lovelace(200_000_000),
      datum: NoDatum,
      reference_script: None,
    },
  }
}

fn s_claim_datum(credential: Credential) -> ClaimDatum {
  ClaimDatum {
    claimer_did: agent_a,
    claimer_credential: credential,
    claim_hash: hash_32,
    claim_type: "data_indexing",
    storage_uri: "ipfs://claim",
    stake_amount: 100_000_000,
    submitted_at: lo + 1,
    challenge_window: 3_600_000,
    state: Open,
  }
}

fn s_record(record: IdentityDatum) -> Input {
  record_input(out_ref(0xa0, 0), agent_a, record)
}

fn s_submit_tx(
  datum: ClaimDatum,
  signer: ByteArray,
  refs: List<Input>,
) -> Transaction {
  let name = claim_token_name(s_seed().output_reference)
  Transaction {
    ..placeholder,
    inputs: [s_seed()],
    reference_inputs: refs,
    outputs: [
      Output {
        address: script_address(claim_hash),
        value: assets.from_lovelace(datum.stake_amount)
          |> assets.add(claim_hash, name, 1),
        datum: InlineDatum(datum),
        reference_script: None,
      },
    ],
    mint: assets.from_asset(claim_hash, name, 1),
    extra_signatories: [signer],
    validity_range: window_range(),
  }
}

fn s_submit(tx: Transaction) -> Bool {
  validate_submit_claim(
    auditing_config(placeholder_verdict_policy()),
    protocol_params(),
    claim_hash,
    tx,
  )
}

/// SubmitClaim by agent A with the declared key `key`.
fn s_submit_with_key(key: ByteArray, refs: List<Input>) -> Transaction {
  s_submit_tx(s_claim_datum(key_credential(key)), blake2b_224(key), refs)
}

test submit_claim_with_listed_transaction_key() {
  s_submit(s_submit_with_key(tx_key_a, [s_record(record_a(Active))]))
}

test submit_claim_with_controller_key() {
  s_submit(s_submit_with_key(controller_a, [s_record(record_a(Active))]))
}

test submit_claim_fails_without_record() {
  !s_submit(s_submit_with_key(tx_key_a, []))
}

test submit_claim_fails_with_legacy_registry_token() {
  let legacy =
    Input {
      output_reference: out_ref(0x1e, 0),
      output: Output {
        address: script_address(
          #"be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01",
        ),
        value: assets.from_lovelace(2_000_000)
          |> assets.add(
              #"be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01",
              agent_a,
              1,
            ),
        datum: NoDatum,
        reference_script: None,
      },
    }
  !s_submit(s_submit_with_key(tx_key_a, [legacy]))
}

test submit_claim_fails_with_record_of_other_agent() {
  !s_submit(
    s_submit_with_key(
      tx_key_a,
      [record_input(out_ref(0xa0, 0), agent_b, record_a(Active))],
    ),
  )
}

test submit_claim_fails_when_suspended() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      status: Suspended {
        holds: [Hold { by: ControllerSet, reason: 6, since: lo, until: lo + 1 }],
      },
    }
  !s_submit(s_submit_with_key(tx_key_a, [s_record(record)]))
}

test submit_claim_fails_when_unaccountable() {
  let record = IdentityDatum { ..record_a(Active), parties: [] }
  !s_submit(s_submit_with_key(tx_key_a, [s_record(record)]))
}

test submit_claim_fails_when_revoked() {
  let record =
    IdentityDatum {
      ..record_a(Active),
      status: Revoked {
        by: ControllerSet,
        reason: 5,
        at: lo - 1,
        invalid_from: None,
        successor: None,
      },
    }
  !s_submit(s_submit_with_key(controller_a, [s_record(record)]))
}

test submit_claim_fails_with_unlisted_key() {
  !s_submit(s_submit_with_key(stranger_key, [s_record(record_a(Active))]))
}

test submit_claim_fails_with_key_without_transaction_purpose() {
  !s_submit(s_submit_with_key(auth_key_a, [s_record(record_a(Active))]))
}

test submit_claim_fails_with_range_over_one_hour() fail {
  s_submit(
    Transaction {
      ..s_submit_with_key(tx_key_a, [s_record(record_a(Active))]),
      validity_range: interval.between(lo, lo + 3_600_001),
    },
  )
}

test submit_claim_fails_with_unbounded_range() fail {
  s_submit(
    Transaction {
      ..s_submit_with_key(tx_key_a, [s_record(record_a(Active))]),
      validity_range: interval.after(lo),
    },
  )
}

// Benchmarks: constants are built at compile time, so the execution units
// are the cost of validate_submit_claim only.

const bench_config: AuditingConfig =
  auditing_config(placeholder_verdict_policy())

const bench_params: ProtocolParams = protocol_params()

const bench_submit_tx: Transaction =
  s_submit_with_key(tx_key_a, [s_record(record_a(Active))])

const bench_submit_largest_tx: Transaction =
  s_submit_with_key(tx_key_a, [s_record(largest_record(Active))])

test bench_submit_claim() {
  validate_submit_claim(bench_config, bench_params, claim_hash, bench_submit_tx)
}

test bench_submit_claim_largest_record() {
  validate_submit_claim(
    bench_config,
    bench_params,
    claim_hash,
    bench_submit_largest_tx,
  )
}
