//// Tests of Module-1 `identity_party` (Part 2 §3.8, §12.1 "Module-1
//// verdicts", the Module-1 items of "Parties" (F-05), §12.4 benchmarks).
////
//// Each negative test changes one thing in a transaction that a positive
//// test accepts. The verdict policy values are placeholders (Part 2 §13.4).

use adversarial_auditing/identity_party.{
  mint_handler, resolution_verdict_ok, spend_handler, verdict_token_name,
  withdraw_handler,
}
use adversarial_auditing/types.{
  ApplyVerdict, AuditingConfig, AuditorWins, BurnVerdict, ClaimDatum,
  ClaimerWins, CleanupResolved, Consent, CrossValidatorRefs, IdentityVerdict,
  Inconclusive, MintVerdict, PartyApproval, PendingOracle, SubmitEvidence,
  TimeoutResolve, VerdictAction, VerdictPolicy, VerdictRevoke, VerdictRule,
  VerdictSuspend,
}
use adversarial_auditing_tests/identity_fixtures.{
  accountable_key_party, agent_a, agent_b, apply_tx, auditing_config,
  base_verdict, challenge_datum, challenge_input, challenge_ref,
  challenged_claim_datum, claim_hash, claim_input, config_input,
  consent_register_tx, consent_update_tx, controller_a, cross_refs, hash_32, hi,
  identity_hash, identity_spend_tx, jury_resolution_with_verdict_tx,
  largest_record, lo, module1_hash, module1_party, module1_signer,
  oracle_resolution_tx, other_claim_token, other_script_hash, out_ref,
  placeholder_verdict_policy, record_a, record_b, record_input, record_output,
  refs_input, revoke_verdict_policy, script_address, ten_holds, to_data,
  verdict, verdict_hold, verdict_input, verdict_output, verdict_ref,
  wallet_input, with_holds,
}
use aiken/cbor
use aiken/collection/list
use aiken/crypto.{blake2b_256}
use aiken/interval
use cardano/address.{Address, Inline, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  DatumHash, InlineDatum, Input, Mint, NoDatum, Output, OutputReference, Spend,
  Transaction, placeholder,
}
use vector_identity.{day_ms, days_30_ms, days_7_ms}
use vector_identity/redeemers.{
  Adopt, BumpEpoch, ByController, ByParty, ChangeController, IdentitySpend,
  Reclaim, RecoverCancel, RecoverFinalize, RecoverInit, Retire, Revoke,
  SetSuccessor, Suspend, Unsuspend, Update,
}
use vector_identity/types.{
  Accountable, Active, ControllerSet, Hold, IdentityDatum, Party, Provider,
  Responsible, Revoked, Status,
} as vi

// ## Helpers

fn cfg() -> AuditingConfig {
  auditing_config(placeholder_verdict_policy())
}

fn revoke_cfg() -> AuditingConfig {
  auditing_config(revoke_verdict_policy())
}

fn mint_ok(config: AuditingConfig, tx: Transaction) -> Bool {
  mint_handler(config, challenge_hash(), claim_hash, MintVerdict, module1_hash, tx)
}

fn challenge_hash() -> ByteArray {
  identity_fixtures.challenge_hash
}

fn burn_ok(tx: Transaction) -> Bool {
  mint_handler(cfg(), challenge_hash(), claim_hash, BurnVerdict, module1_hash, tx)
}

fn approve(approval: PartyApproval, tx: Transaction) -> Bool {
  withdraw_handler(identity_hash, approval, Script(module1_hash), tx)
}

fn spend_ok(tx: Transaction) -> Bool {
  spend_handler(verdict_ref(), tx)
}

/// The oracle resolution with AuditorWins and the base verdict.
fn oracle_tx() -> Transaction {
  oracle_resolution_tx(AuditorWins, base_verdict())
}

/// `tx` with its last output replaced by `output`.
fn with_last_output(tx: Transaction, output: Output) -> Transaction {
  Transaction {
    ..tx,
    outputs: list.concat(
      list.take(tx.outputs, list.length(tx.outputs) - 1),
      [output],
    ),
  }
}

/// `tx` with the redeemer of the challenge input replaced by `redeemer`.
fn with_challenge_redeemer(tx: Transaction, redeemer: Data) -> Transaction {
  Transaction {
    ..tx,
    redeemers: [
      Pair(Spend(challenge_ref()), redeemer),
      ..list.drop(tx.redeemers, 1)
    ],
  }
}

fn suspend_by_module1() -> IdentitySpend {
  Suspend { actor: ByParty { index: 1 } }
}

fn revoke_by_module1() -> IdentitySpend {
  Revoke { actor: ByParty { index: 1 } }
}

/// The new record of a suspend verdict application on `before`: the holds
/// `others`, then the Module-1 hold until `until`.
fn suspended(
  before: IdentityDatum,
  others: List<Hold>,
  until: Int,
) -> IdentityDatum {
  with_holds(before, list.concat(others, [verdict_hold(until)]))
}

/// A tombstone of `record` with the given status (Part 2 §3.7).
fn tombstone(record: IdentityDatum, status: Status) -> IdentityDatum {
  IdentityDatum {
    ..record,
    keys: [],
    parties: [],
    guardian_threshold: 0,
    pending_recovery: None,
    description: "",
    framework: "",
    endpoint: "",
    capabilities: [],
    status,
  }
}

fn module1_revocation() -> Status {
  Revoked {
    by: module1_signer(),
    reason: 64,
    at: lo,
    invalid_from: None,
    successor: None,
  }
}

fn revoke_verdict() -> IdentityVerdict {
  verdict(agent_a, VerdictRevoke, 0)
}

fn suspend_tx(before: IdentityDatum, after: IdentityDatum) -> Transaction {
  apply_tx(base_verdict(), agent_a, before, after, suspend_by_module1())
}

fn revoke_tx(after: IdentityDatum) -> Transaction {
  apply_tx(
    revoke_verdict(),
    agent_a,
    record_a(Active),
    after,
    revoke_by_module1(),
  )
}

/// A Module-1 hold set one day before `lo`, in force until `until`.
fn module1_hold(until: Int) -> Hold {
  Hold { by: module1_signer(), reason: 64, since: lo - day_ms, until }
}

fn consent_before() -> IdentityDatum {
  record_b(Active)
}

fn consent_after(party: Party) -> IdentityDatum {
  IdentityDatum {
    ..record_b(Active),
    parties: [accountable_key_party(), party],
  }
}

fn consent_tx(party: Party) -> Transaction {
  consent_update_tx(consent_before(), consent_after(party))
}

/// A Consent withdrawal attached to the identity action `action`, in a
/// transaction that would add a valid Module-1 party.
fn consent_with_action(action: IdentitySpend) -> Transaction {
  identity_spend_tx(
    agent_a,
    consent_before(),
    consent_after(module1_party(Accountable)),
    action,
    to_data(Consent),
  )
}

fn two_hour_range() -> interval.Interval {
  interval.between(lo, lo + 7_200_000)
}

// ## Verdict mint: positive cases

test mint_oracle_auditor_wins_suspend() {
  mint_ok(cfg(), oracle_tx())
}

test mint_fails_for_jury_resolution() fail {
  // Only oracle resolutions give verdicts; a jury outcome gives none.
  mint_ok(cfg(), jury_resolution_with_verdict_tx(AuditorWins, base_verdict()))
}

test mint_revoke_policy_names_claimer() {
  mint_ok(revoke_cfg(), oracle_resolution_tx(AuditorWins, revoke_verdict()))
}

test mint_claimer_wins_names_auditor() {
  mint_ok(
    revoke_cfg(),
    oracle_resolution_tx(ClaimerWins, verdict(agent_b, VerdictSuspend, day_ms)),
  )
}

test mint_fails_for_jury_resolution_with_claimer_wins() fail {
  mint_ok(
    revoke_cfg(),
    jury_resolution_with_verdict_tx(
      ClaimerWins,
      verdict(agent_b, VerdictSuspend, day_ms),
    ),
  )
}

test verdict_token_name_is_hash_of_serialised_verdict() {
  let v = base_verdict()
  and {
    verdict_token_name(v) == blake2b_256(cbor.serialise(v)),
    verdict_token_name(v) != verdict_token_name(
      IdentityVerdict { ..v, duration: v.duration + 1 },
    ),
    verdict_token_name(v) != verdict_token_name(
      IdentityVerdict { ..v, deposit_to: script_address(other_script_hash) },
    ),
    verdict_token_name(v) != verdict_token_name(
      IdentityVerdict { ..v, expires_at: v.expires_at + 1 },
    ),
  }
}

// ## Verdict mint: no challenge resolution

test mint_fails_without_challenge_input() fail {
  let tx = oracle_tx()
  mint_ok(cfg(), Transaction { ..tx, inputs: list.drop(tx.inputs, 1) })
}

test mint_fails_with_submit_evidence() fail {
  mint_ok(
    cfg(),
    with_challenge_redeemer(
      oracle_tx(),
      to_data(
        SubmitEvidence { new_evidence_hash: hash_32, new_evidence_uri: "x" },
      ),
    ),
  )
}

test mint_fails_with_timeout_resolve() fail {
  mint_ok(cfg(), with_challenge_redeemer(oracle_tx(), to_data(TimeoutResolve)))
}

test mint_fails_with_cleanup_resolved() fail {
  mint_ok(cfg(), with_challenge_redeemer(oracle_tx(), to_data(CleanupResolved)))
}

test mint_fails_without_challenge_redeemer() fail {
  let tx = oracle_tx()
  mint_ok(cfg(), Transaction { ..tx, redeemers: list.drop(tx.redeemers, 1) })
}

test mint_fails_with_two_challenge_inputs() fail {
  let tx = oracle_tx()
  let second =
    Input {
      ..challenge_input(challenge_datum(PendingOracle)),
      output_reference: out_ref(0xcb, 2),
    }
  mint_ok(cfg(), Transaction { ..tx, inputs: [second, ..tx.inputs] })
}

test mint_fails_when_challenge_input_has_no_token() fail {
  let tx = oracle_tx()
  let challenge = challenge_input(challenge_datum(PendingOracle))
  let tokenless =
    Input {
      ..challenge,
      output: Output {
        ..challenge.output,
        value: assets.from_lovelace(100_000_000),
      },
    }
  mint_ok(
    cfg(),
    Transaction { ..tx, inputs: [tokenless, ..list.drop(tx.inputs, 1)] },
  )
}

// ## Verdict mint: verdict differs from the resolution

test mint_fails_with_other_agent() {
  !mint_ok(
    cfg(),
    oracle_resolution_tx(
      AuditorWins,
      verdict(agent_b, VerdictSuspend, days_7_ms),
    ),
  )
}

test mint_fails_with_other_action() {
  !mint_ok(cfg(), oracle_resolution_tx(AuditorWins, revoke_verdict()))
}

test mint_fails_with_other_duration() {
  !mint_ok(
    cfg(),
    oracle_resolution_tx(
      AuditorWins,
      verdict(agent_a, VerdictSuspend, days_7_ms + 1),
    ),
  )
}

test mint_fails_with_other_challenge() {
  !mint_ok(
    cfg(),
    oracle_resolution_tx(
      AuditorWins,
      IdentityVerdict { ..base_verdict(), challenge: out_ref(0x1, 0) },
    ),
  )
}

test mint_fails_for_inconclusive() fail {
  mint_ok(cfg(), oracle_resolution_tx(Inconclusive, base_verdict()))
}

test mint_fails_for_outcome_without_rule() fail {
  mint_ok(
    cfg(),
    oracle_resolution_tx(
      ClaimerWins,
      verdict(agent_b, VerdictSuspend, days_7_ms),
    ),
  )
}

test mint_fails_when_claim_is_not_bound() fail {
  let tx = oracle_tx()
  mint_ok(
    cfg(),
    Transaction {
      ..tx,
      inputs: [
        challenge_input(challenge_datum(PendingOracle)),
        claim_input(other_claim_token, challenged_claim_datum()),
        ..list.drop(tx.inputs, 2)
      ],
    },
  )
}

test mint_fails_with_suspension_over_30_days() fail {
  let long =
    VerdictPolicy {
      auditor_wins: Some(
        VerdictRule { action: VerdictSuspend, duration: days_30_ms + 1 },
      ),
      claimer_wins: None,
    }
  mint_ok(
    auditing_config(long),
    oracle_resolution_tx(
      AuditorWins,
      verdict(agent_a, VerdictSuspend, days_30_ms + 1),
    ),
  )
}

test mint_fails_with_revocation_duration() fail {
  let policy =
    VerdictPolicy {
      auditor_wins: Some(VerdictRule { action: VerdictRevoke, duration: 1 }),
      claimer_wins: None,
    }
  mint_ok(
    auditing_config(policy),
    oracle_resolution_tx(AuditorWins, verdict(agent_a, VerdictRevoke, 1)),
  )
}

test mint_accepts_suspension_of_30_days() {
  let policy =
    VerdictPolicy {
      auditor_wins: Some(
        VerdictRule { action: VerdictSuspend, duration: days_30_ms },
      ),
      claimer_wins: None,
    }
  mint_ok(
    auditing_config(policy),
    oracle_resolution_tx(
      AuditorWins,
      verdict(agent_a, VerdictSuspend, days_30_ms),
    ),
  )
}

// ## Verdict mint: token count, name and output

test mint_fails_with_quantity_two() fail {
  let tx = oracle_tx()
  mint_ok(
    cfg(),
    Transaction {
      ..tx,
      mint: assets.add(
        tx.mint,
        module1_hash,
        verdict_token_name(base_verdict()),
        1,
      ),
    },
  )
}

test mint_fails_with_two_names() fail {
  let tx = oracle_tx()
  mint_ok(
    cfg(),
    Transaction { ..tx, mint: assets.add(tx.mint, module1_hash, "second", 1) },
  )
}

test mint_fails_when_name_differs_from_datum() {
  let v = base_verdict()
  let other = IdentityVerdict { ..v, duration: v.duration + 1 }
  // The token name belongs to `other`; the datum is `v`.
  let tx =
    Transaction {
      ..oracle_tx(),
      mint: assets.zero
        |> assets.add(
            identity_fixtures.challenge_hash,
            identity_fixtures.challenge_token,
            -1,
          )
        |> assets.add(claim_hash, identity_fixtures.claim_token, -1)
        |> assets.add(module1_hash, verdict_token_name(other), 1),
    }
  !mint_ok(
    cfg(),
    with_last_output(
      tx,
      Output { ..verdict_output(other), datum: InlineDatum(v) },
    ),
  )
}

test mint_fails_when_datum_differs_from_name() {
  let v = base_verdict()
  let other = IdentityVerdict { ..v, agent: agent_b }
  !mint_ok(
    cfg(),
    with_last_output(
      oracle_tx(),
      Output { ..verdict_output(v), datum: InlineDatum(other) },
    ),
  )
}

test mint_fails_with_datum_hash() {
  !mint_ok(
    cfg(),
    with_last_output(
      oracle_tx(),
      Output { ..verdict_output(base_verdict()), datum: DatumHash(hash_32) },
    ),
  )
}

test mint_fails_with_output_stake_credential() {
  !mint_ok(
    cfg(),
    with_last_output(
      oracle_tx(),
      Output {
        ..verdict_output(base_verdict()),
        address: Address {
          payment_credential: Script(module1_hash),
          stake_credential: Some(Inline(VerificationKey(hash_28()))),
        },
      },
    ),
  )
}

fn hash_28() -> ByteArray {
  #"28282828282828282828282828282828282828282828282828282828"
}

test mint_fails_with_output_at_other_script() {
  !mint_ok(
    cfg(),
    with_last_output(
      oracle_tx(),
      Output {
        ..verdict_output(base_verdict()),
        address: script_address(other_script_hash),
      },
    ),
  )
}

test mint_fails_with_foreign_asset() {
  let output = verdict_output(base_verdict())
  !mint_ok(
    cfg(),
    with_last_output(
      oracle_tx(),
      Output { ..output, value: assets.add(output.value, hash_28(), "x", 1) },
    ),
  )
}

test mint_fails_with_reference_script() {
  !mint_ok(
    cfg(),
    with_last_output(
      oracle_tx(),
      Output {
        ..verdict_output(base_verdict()),
        reference_script: Some(hash_28()),
      },
    ),
  )
}

test mint_fails_when_no_output_holds_token() fail {
  let tx = oracle_tx()
  mint_ok(
    cfg(),
    Transaction {
      ..tx,
      outputs: list.take(tx.outputs, list.length(tx.outputs) - 1),
    },
  )
}

test mint_redeemer_burn_rejects_a_mint() {
  !burn_ok(oracle_tx())
}

// ## Challenge side: the resolution requires the verdict mint

fn without_verdict_mint(tx: Transaction) -> Transaction {
  Transaction {
    ..tx,
    mint: assets.zero
      |> assets.add(
          identity_fixtures.challenge_hash,
          identity_fixtures.challenge_token,
          -1,
        )
      |> assets.add(claim_hash, identity_fixtures.claim_token, -1),
  }
}

test resolution_with_verdict_mint() {
  resolution_verdict_ok(cfg(), AuditorWins, oracle_tx())
}

test resolution_without_verdict_mint_fails() {
  !resolution_verdict_ok(cfg(), AuditorWins, without_verdict_mint(oracle_tx()))
}

test resolution_with_two_verdict_tokens_fails() {
  let tx = oracle_tx()
  !resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, mint: assets.add(tx.mint, module1_hash, "second", 1) },
  )
}

test resolution_with_quantity_two_fails() {
  let tx = oracle_tx()
  !resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction {
      ..tx,
      mint: assets.add(
        tx.mint,
        module1_hash,
        verdict_token_name(base_verdict()),
        1,
      ),
    },
  )
}

test resolution_with_mint_of_other_policy_fails() {
  let tx = without_verdict_mint(oracle_tx())
  !resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, mint: assets.add(tx.mint, other_script_hash, "v", 1) },
  )
}

test resolution_without_rule_needs_no_mint() {
  let tx = without_verdict_mint(oracle_tx())
  and {
    resolution_verdict_ok(
      cfg(),
      ClaimerWins,
      Transaction { ..tx, reference_inputs: [] },
    ),
    resolution_verdict_ok(
      cfg(),
      Inconclusive,
      Transaction { ..tx, reference_inputs: [] },
    ),
    resolution_verdict_ok(
      auditing_config(VerdictPolicy { auditor_wins: None, claimer_wins: None }),
      AuditorWins,
      Transaction { ..tx, reference_inputs: [] },
    ),
  }
}

test resolution_without_configuration_record_fails() fail {
  let tx = oracle_tx()
  resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, reference_inputs: list.take(tx.reference_inputs, 1) },
  )
}

test resolution_with_configuration_record_without_token_fails() fail {
  let tx = oracle_tx()
  let record = config_input(module1_hash)
  let fake =
    Input {
      ..record,
      output: Output { ..record.output, value: assets.from_lovelace(2_000_000) },
    }
  resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, reference_inputs: [fake] },
  )
}

test resolution_with_configuration_record_at_other_address_fails() fail {
  let tx = oracle_tx()
  let record = config_input(module1_hash)
  let moved =
    Input {
      ..record,
      output: Output {
        ..record.output,
        address: script_address(other_script_hash),
      },
    }
  resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, reference_inputs: [moved] },
  )
}

test resolution_with_configuration_record_with_stake_credential_fails() fail {
  let tx = oracle_tx()
  let record = config_input(module1_hash)
  let staked =
    Input {
      ..record,
      output: Output {
        ..record.output,
        address: Address {
          payment_credential: Script(identity_fixtures.config_holder),
          stake_credential: Some(Inline(VerificationKey(hash_28()))),
        },
      },
    }
  resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, reference_inputs: [staked] },
  )
}

test resolution_with_configuration_datum_not_28_bytes_fails() fail {
  let tx = oracle_tx()
  let record = config_input(module1_hash)
  let bad =
    Input {
      ..record,
      output: Output { ..record.output, datum: InlineDatum(#"e1e1") },
    }
  resolution_verdict_ok(
    cfg(),
    AuditorWins,
    Transaction { ..tx, reference_inputs: [bad] },
  )
}

test resolution_reads_module1_from_configuration_record() {
  // The record names another script: a mint under module1_hash does not
  // count, a mint under the named script does.
  let tx = oracle_tx()
  let other_config = [config_input(other_script_hash)]
  and {
    !resolution_verdict_ok(
      cfg(),
      AuditorWins,
      Transaction { ..tx, reference_inputs: other_config },
    ),
    resolution_verdict_ok(
      cfg(),
      AuditorWins,
      Transaction {
        ..tx,
        reference_inputs: other_config,
        mint: assets.from_asset(other_script_hash, "verdict", 1),
      },
    ),
  }
}

// ## Burn

fn suspend_application() -> Transaction {
  suspend_tx(record_a(Active), suspended(record_a(Active), [], lo + days_7_ms))
}

test burn_in_verdict_application() {
  burn_ok(suspend_application())
}

test burn_fails_without_withdrawal() {
  !burn_ok(Transaction { ..suspend_application(), withdrawals: [] })
}

test burn_fails_with_two_tokens() {
  let tx = suspend_application()
  !burn_ok(
    Transaction { ..tx, mint: assets.add(tx.mint, module1_hash, "x", -1) },
  )
}

test burn_with_consent_withdrawal_fails() fail {
  // A Consent withdrawal does not accept a burn of a verdict token.
  let tx = consent_tx(module1_party(Accountable))
  approve(
    Consent,
    Transaction {
      ..tx,
      inputs: [verdict_input(base_verdict()), ..tx.inputs],
      mint: assets.from_asset(
        module1_hash,
        verdict_token_name(base_verdict()),
        -1,
      ),
    },
  )
}

// ## Spend

test spend_in_verdict_application() {
  spend_ok(suspend_application())
}

test spend_fails_without_burn() {
  !spend_ok(Transaction { ..suspend_application(), mint: assets.zero })
}

test spend_fails_with_burn_of_other_token() {
  !spend_ok(
    Transaction {
      ..suspend_application(),
      mint: assets.from_asset(module1_hash, "other", -1),
    },
  )
}

test spend_fails_with_two_burns() {
  let tx = suspend_application()
  !spend_ok(
    Transaction { ..tx, mint: assets.add(tx.mint, module1_hash, "x", -1) },
  )
}

test spend_fails_without_withdrawal() {
  !spend_ok(Transaction { ..suspend_application(), withdrawals: [] })
}

test spend_fails_for_output_without_token() {
  let tx = suspend_application()
  let input = verdict_input(base_verdict())
  let tokenless =
    Input {
      ..input,
      output: Output { ..input.output, value: assets.from_lovelace(2_000_000) },
    }
  !spend_ok(
    Transaction { ..tx, inputs: [tokenless, ..list.drop(tx.inputs, 1)] },
  )
}

// ## Verdict application: suspend

test apply_suspend_on_active_record() {
  approve(ApplyVerdict, suspend_application())
}

test apply_suspend_keeps_other_holds() {
  let controller_hold =
    Hold {
      by: ControllerSet,
      reason: 6,
      since: lo - day_ms,
      until: lo + day_ms,
    }
  let before = with_holds(record_a(Active), [controller_hold])
  approve(
    ApplyVerdict,
    suspend_tx(
      before,
      suspended(record_a(Active), [controller_hold], lo + days_7_ms),
    ),
  )
}

test apply_suspend_keeps_longer_module1_hold() {
  let before = with_holds(record_a(Active), [module1_hold(lo + 10 * day_ms)])
  approve(
    ApplyVerdict,
    suspend_tx(before, suspended(record_a(Active), [], lo + 10 * day_ms)),
  )
}

test apply_suspend_extends_shorter_module1_hold() {
  let before = with_holds(record_a(Active), [module1_hold(lo + day_ms)])
  approve(
    ApplyVerdict,
    suspend_tx(before, suspended(record_a(Active), [], lo + days_7_ms)),
  )
}

test apply_suspend_ignores_ended_module1_hold() {
  let before = with_holds(record_a(Active), [module1_hold(lo)])
  approve(
    ApplyVerdict,
    suspend_tx(before, suspended(record_a(Active), [], lo + days_7_ms)),
  )
}

test apply_later_shorter_verdict_cannot_shorten_hold() {
  let before = with_holds(record_a(Active), [module1_hold(lo + 10 * day_ms)])
  !approve(
    ApplyVerdict,
    suspend_tx(before, suspended(record_a(Active), [], lo + days_7_ms)),
  )
}

test apply_suspend_fails_with_longer_until() {
  !approve(
    ApplyVerdict,
    suspend_tx(
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms + 1),
    ),
  )
}

test apply_suspend_fails_with_shorter_until() {
  !approve(
    ApplyVerdict,
    suspend_tx(
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms - 1),
    ),
  )
}

test apply_suspend_fails_with_other_reason() {
  let hold = Hold { ..verdict_hold(lo + days_7_ms), reason: 68 }
  !approve(
    ApplyVerdict,
    suspend_tx(record_a(Active), with_holds(record_a(Active), [hold])),
  )
}

test apply_suspend_fails_with_other_since() {
  let hold = Hold { ..verdict_hold(lo + days_7_ms), since: lo - 1 }
  !approve(
    ApplyVerdict,
    suspend_tx(record_a(Active), with_holds(record_a(Active), [hold])),
  )
}

test apply_suspend_fails_with_hold_by_other_author() {
  let hold = Hold { ..verdict_hold(lo + days_7_ms), by: ControllerSet }
  !approve(
    ApplyVerdict,
    suspend_tx(record_a(Active), with_holds(record_a(Active), [hold])),
  )
}

test apply_suspend_fails_when_status_stays_active() {
  !approve(ApplyVerdict, suspend_tx(record_a(Active), record_a(Active)))
}

test apply_suspend_verdict_as_revocation_fails() {
  !approve(
    ApplyVerdict,
    apply_tx(
      base_verdict(),
      agent_a,
      record_a(Active),
      tombstone(record_a(Active), module1_revocation()),
      revoke_by_module1(),
    ),
  )
}

test apply_suspend_by_controller_fails() {
  !approve(
    ApplyVerdict,
    apply_tx(
      base_verdict(),
      agent_a,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      Suspend { actor: ByController },
    ),
  )
}

test apply_suspend_by_other_party_fails() {
  !approve(
    ApplyVerdict,
    apply_tx(
      base_verdict(),
      agent_a,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      Suspend { actor: ByParty { index: 0 } },
    ),
  )
}

test apply_suspend_with_index_out_of_range_fails() {
  !approve(
    ApplyVerdict,
    apply_tx(
      base_verdict(),
      agent_a,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      Suspend { actor: ByParty { index: 2 } },
    ),
  )
}

test apply_unsuspend_of_module1_hold_fails() fail {
  let before = with_holds(record_a(Active), [module1_hold(lo + days_7_ms)])
  approve(
    ApplyVerdict,
    apply_tx(
      base_verdict(),
      agent_a,
      before,
      record_a(Active),
      Unsuspend { author: module1_signer() },
    ),
  )
}

test consent_unsuspend_of_module1_hold_fails() fail {
  let before = with_holds(record_a(Active), [module1_hold(lo + days_7_ms)])
  approve(
    Consent,
    identity_spend_tx(
      agent_a,
      before,
      record_a(Active),
      Unsuspend { author: module1_signer() },
      to_data(Consent),
    ),
  )
}

// ## Verdict application: revoke

test apply_revoke() {
  approve(
    ApplyVerdict,
    revoke_tx(tombstone(record_a(Active), module1_revocation())),
  )
}

test apply_revoke_on_suspended_record() {
  let before = with_holds(record_a(Active), [module1_hold(lo + days_7_ms)])
  approve(
    ApplyVerdict,
    apply_tx(
      revoke_verdict(),
      agent_a,
      before,
      tombstone(record_a(Active), module1_revocation()),
      revoke_by_module1(),
    ),
  )
}

test apply_revoke_fails_with_invalid_from() {
  let status =
    Revoked {
      by: module1_signer(),
      reason: 64,
      at: lo,
      invalid_from: Some(lo - day_ms),
      successor: None,
    }
  !approve(ApplyVerdict, revoke_tx(tombstone(record_a(Active), status)))
}

test apply_revoke_fails_with_successor() {
  let status =
    Revoked {
      by: module1_signer(),
      reason: 64,
      at: lo,
      invalid_from: None,
      successor: Some(agent_b),
    }
  !approve(ApplyVerdict, revoke_tx(tombstone(record_a(Active), status)))
}

test apply_revoke_fails_with_other_reason() {
  let status =
    Revoked {
      by: module1_signer(),
      reason: 0,
      at: lo,
      invalid_from: None,
      successor: None,
    }
  !approve(ApplyVerdict, revoke_tx(tombstone(record_a(Active), status)))
}

test apply_revoke_fails_with_other_author() {
  let status =
    Revoked {
      by: ControllerSet,
      reason: 64,
      at: lo,
      invalid_from: None,
      successor: None,
    }
  !approve(ApplyVerdict, revoke_tx(tombstone(record_a(Active), status)))
}

test apply_revoke_fails_with_other_time() {
  let status =
    Revoked {
      by: module1_signer(),
      reason: 64,
      at: lo + 1,
      invalid_from: None,
      successor: None,
    }
  !approve(ApplyVerdict, revoke_tx(tombstone(record_a(Active), status)))
}

test apply_revoke_verdict_as_suspension_fails() {
  !approve(
    ApplyVerdict,
    apply_tx(
      revoke_verdict(),
      agent_a,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      suspend_by_module1(),
    ),
  )
}

test apply_revoke_by_controller_fails() {
  !approve(
    ApplyVerdict,
    apply_tx(
      revoke_verdict(),
      agent_a,
      record_a(Active),
      tombstone(record_a(Active), module1_revocation()),
      Revoke { actor: ByController },
    ),
  )
}

// ## Verdict application: scope, replay, agent, data

test apply_to_other_agent_fails() {
  // The verdict names agent_a; the identity input is the record of agent_b.
  !approve(
    ApplyVerdict,
    apply_tx(
      base_verdict(),
      agent_b,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      suspend_by_module1(),
    ),
  )
}

test apply_without_burn_fails() fail {
  // Replay: the verdict output is only referenced and its token stays.
  let tx = suspend_application()
  approve(
    ApplyVerdict,
    Transaction {
      ..tx,
      inputs: list.drop(tx.inputs, 1),
      reference_inputs: [verdict_input(base_verdict()), ..tx.reference_inputs],
      mint: assets.zero,
    },
  )
}

test apply_without_verdict_input_fails() fail {
  // Replay: the token of a verdict that was used before is not in any input.
  let tx = suspend_application()
  approve(ApplyVerdict, Transaction { ..tx, inputs: list.drop(tx.inputs, 1) })
}

test apply_two_verdicts_fails() fail {
  let tx = suspend_application()
  let second = IdentityVerdict { ..base_verdict(), challenge: out_ref(0x2, 0) }
  approve(
    ApplyVerdict,
    Transaction {
      ..tx,
      inputs: [
        Input { ..verdict_input(second), output_reference: out_ref(0xe1, 1) },
        ..tx.inputs
      ],
      mint: assets.add(tx.mint, module1_hash, verdict_token_name(second), -1),
    },
  )
}

test apply_fails_when_verdict_data_does_not_match_name() {
  let tx = suspend_application()
  let input = verdict_input(base_verdict())
  let other = IdentityVerdict { ..base_verdict(), duration: days_30_ms }
  let changed =
    Input {
      ..input,
      output: Output { ..input.output, datum: InlineDatum(other) },
    }
  !approve(
    ApplyVerdict,
    Transaction { ..tx, inputs: [changed, ..list.drop(tx.inputs, 1)] },
  )
}

test apply_fails_when_verdict_input_is_not_at_party_address() {
  let tx = suspend_application()
  let input = verdict_input(base_verdict())
  let moved =
    Input {
      ..input,
      output: Output {
        ..input.output,
        address: script_address(other_script_hash),
      },
    }
  !approve(
    ApplyVerdict,
    Transaction { ..tx, inputs: [moved, ..list.drop(tx.inputs, 1)] },
  )
}

test apply_with_two_identity_inputs_fails() fail {
  let tx = suspend_application()
  approve(
    ApplyVerdict,
    Transaction {
      ..tx,
      inputs: [
        record_input(out_ref(0xb1, 0), agent_b, record_b(Active)),
        ..tx.inputs
      ],
    },
  )
}

test apply_with_two_record_outputs_fails() fail {
  let tx = suspend_application()
  approve(
    ApplyVerdict,
    Transaction {
      ..tx,
      outputs: [record_output(agent_b, record_b(Active)), ..tx.outputs],
    },
  )
}

test apply_suspend_with_range_over_one_hour_fails() fail {
  approve(
    ApplyVerdict,
    Transaction { ..suspend_application(), validity_range: two_hour_range() },
  )
}

test apply_revoke_with_range_over_one_hour_fails() fail {
  approve(
    ApplyVerdict,
    Transaction {
      ..revoke_tx(tombstone(record_a(Active), module1_revocation())),
      validity_range: two_hour_range(),
    },
  )
}

test apply_with_unbounded_range_fails() fail {
  approve(
    ApplyVerdict,
    Transaction { ..suspend_application(), validity_range: interval.after(lo) },
  )
}

test approval_for_key_credential_fails() fail {
  withdraw_handler(
    identity_hash,
    ApplyVerdict,
    VerificationKey(hash_28()),
    suspend_application(),
  )
}

// ## Consent: positive cases

test consent_update_adds_accountable() {
  approve(Consent, consent_tx(module1_party(Accountable)))
}

test consent_update_adds_responsible() {
  approve(Consent, consent_tx(module1_party(Responsible)))
}

test consent_update_not_before_at_lo() {
  approve(
    Consent,
    consent_tx(Party { ..module1_party(Accountable), not_before: lo }),
  )
}

test consent_register_with_module1() {
  approve(
    Consent,
    consent_register_tx(
      identity_fixtures.agent_record(
        controller_a,
        [],
        [accountable_key_party(), module1_party(Accountable)],
        Active,
      ),
    ),
  )
}

test consent_adopt_with_module1() {
  let tx =
    consent_register_tx(
      identity_fixtures.agent_record(
        controller_a,
        [],
        [accountable_key_party(), module1_party(Responsible)],
        Active,
      ),
    )
  approve(
    Consent,
    Transaction {
      ..tx,
      redeemers: [
        Pair(Mint(identity_hash), to_data(Adopt { proof: [] })),
        ..list.drop(tx.redeemers, 1)
      ],
    },
  )
}

test consent_keeps_existing_module1_party_and_adds_one() {
  let before =
    IdentityDatum {
      ..consent_before(),
      parties: [accountable_key_party(), module1_party(Responsible)],
    }
  let after =
    IdentityDatum {
      ..before,
      parties: [
        accountable_key_party(),
        module1_party(Responsible),
        module1_party(Accountable),
      ],
    }
  approve(Consent, consent_update_tx(before, after))
}

test consent_on_suspended_record_keeps_status() {
  let status =
    vi.Suspended {
      holds: [
        Hold {
          by: ControllerSet,
          reason: 6,
          since: lo - day_ms,
          until: lo + day_ms,
        },
      ],
    }
  let before = IdentityDatum { ..consent_before(), status }
  let after =
    IdentityDatum {
      ..before,
      parties: [accountable_key_party(), module1_party(Accountable)],
    }
  approve(Consent, consent_update_tx(before, after))
}

// ## Consent: terms of the new Module-1 party

test consent_fails_with_rights_below_highest_accountable() {
  !approve(
    Consent,
    consent_tx(Party { ..module1_party(Accountable), rights: 1 }),
  )
}

test consent_fails_with_rights_below_highest_responsible() {
  !approve(
    Consent,
    consent_tx(Party { ..module1_party(Responsible), rights: 0 }),
  )
}

test consent_fails_with_rights_above_highest() {
  !approve(
    Consent,
    consent_tx(Party { ..module1_party(Responsible), rights: 3 }),
  )
}

test consent_fails_with_not_after() {
  !approve(
    Consent,
    consent_tx(
      Party { ..module1_party(Accountable), not_after: Some(lo + days_30_ms) },
    ),
  )
}

test consent_fails_with_removal_after() {
  !approve(
    Consent,
    consent_tx(
      Party {
        ..module1_party(Accountable),
        removal_after: Some(lo + days_30_ms),
      },
    ),
  )
}

test consent_fails_with_not_before_after_lo() {
  !approve(
    Consent,
    consent_tx(Party { ..module1_party(Accountable), not_before: lo + 1 }),
  )
}

test consent_fails_with_other_role() {
  !approve(
    Consent,
    consent_tx(Party { ..module1_party(Responsible), role: Provider }),
  )
}

test consent_register_fails_with_bad_terms() {
  !approve(
    Consent,
    consent_register_tx(
      identity_fixtures.agent_record(
        controller_a,
        [],
        [
          accountable_key_party(),
          Party { ..module1_party(Accountable), not_after: Some(lo + day_ms) },
        ],
        Active,
      ),
    ),
  )
}

// ## Consent: other Module-1 parties and status

test consent_fails_when_it_removes_other_module1_party() {
  let before =
    IdentityDatum {
      ..consent_before(),
      parties: [accountable_key_party(), module1_party(Responsible)],
    }
  let after =
    IdentityDatum {
      ..before,
      parties: [accountable_key_party(), module1_party(Accountable)],
    }
  !approve(Consent, consent_update_tx(before, after))
}

test consent_fails_when_it_removes_noticed_module1_party() {
  // Also a party whose removal notice has passed: removal needs its own
  // Update without the Module-1 withdrawal.
  let noticed = Party { ..module1_party(Responsible), removal_after: Some(lo) }
  let before =
    IdentityDatum {
      ..consent_before(),
      parties: [accountable_key_party(), noticed],
    }
  let after =
    IdentityDatum {
      ..before,
      parties: [accountable_key_party(), module1_party(Accountable)],
    }
  !approve(Consent, consent_update_tx(before, after))
}

test consent_fails_when_it_changes_other_module1_party() {
  let before =
    IdentityDatum {
      ..consent_before(),
      parties: [accountable_key_party(), module1_party(Responsible)],
    }
  let after =
    IdentityDatum {
      ..before,
      parties: [
        accountable_key_party(),
        Party {
          ..module1_party(Responsible),
          removal_after: Some(lo + days_30_ms),
        },
        module1_party(Accountable),
      ],
    }
  !approve(Consent, consent_update_tx(before, after))
}

test consent_fails_without_new_module1_party() {
  let before =
    IdentityDatum {
      ..consent_before(),
      parties: [accountable_key_party(), module1_party(Responsible)],
    }
  !approve(
    Consent,
    consent_update_tx(before, IdentityDatum { ..before, name: "renamed" }),
  )
}

test consent_fails_with_duplicate_module1_party() {
  let after =
    IdentityDatum {
      ..consent_before(),
      parties: [
        accountable_key_party(),
        module1_party(Accountable),
        module1_party(Accountable),
      ],
    }
  !approve(Consent, consent_update_tx(consent_before(), after))
}

test consent_fails_when_update_changes_status() fail {
  let after =
    IdentityDatum {
      ..consent_after(module1_party(Accountable)),
      status: vi.Suspended {
        holds: [
          Hold { by: ControllerSet, reason: 6, since: lo, until: lo + day_ms },
        ],
      },
    }
  approve(Consent, consent_update_tx(consent_before(), after))
}

test consent_fails_with_verdict_mint() {
  let tx = consent_tx(module1_party(Accountable))
  !approve(
    Consent,
    Transaction { ..tx, mint: assets.from_asset(module1_hash, "v", 1) },
  )
}

test consent_fails_with_two_identity_inputs() fail {
  let tx = consent_tx(module1_party(Accountable))
  approve(
    Consent,
    Transaction {
      ..tx,
      inputs: [
        record_input(out_ref(0xb1, 0), agent_b, record_b(Active)),
        ..tx.inputs
      ],
    },
  )
}

test consent_fails_with_two_record_outputs() fail {
  let tx = consent_tx(module1_party(Accountable))
  approve(
    Consent,
    Transaction {
      ..tx,
      outputs: [record_output(agent_b, record_b(Active)), ..tx.outputs],
    },
  )
}

test consent_fails_with_identity_mint_in_update() fail {
  let tx = consent_tx(module1_party(Accountable))
  approve(
    Consent,
    Transaction { ..tx, mint: assets.from_asset(identity_hash, agent_b, 1) },
  )
}

test consent_update_with_range_over_one_hour_fails() fail {
  approve(
    Consent,
    Transaction {
      ..consent_tx(module1_party(Accountable)),
      validity_range: two_hour_range(),
    },
  )
}

test consent_register_with_range_over_one_hour_fails() fail {
  approve(
    Consent,
    Transaction {
      ..consent_register_tx(
        identity_fixtures.agent_record(
          controller_a,
          [],
          [accountable_key_party(), module1_party(Accountable)],
          Active,
        ),
      ),
      validity_range: two_hour_range(),
    },
  )
}

test consent_with_unbounded_range_fails() fail {
  approve(
    Consent,
    Transaction {
      ..consent_tx(module1_party(Accountable)),
      validity_range: interval.before(lo),
    },
  )
}

// ## F-05: a Consent withdrawal attached to another identity action

test f05_consent_with_revoke_fails() fail {
  approve(Consent, consent_with_action(Revoke { actor: ByController }))
}

test f05_consent_with_revoke_by_module1_fails() fail {
  approve(Consent, consent_with_action(revoke_by_module1()))
}

test f05_consent_with_retire_fails() fail {
  approve(Consent, consent_with_action(Retire))
}

test f05_consent_with_suspend_fails() fail {
  approve(Consent, consent_with_action(suspend_by_module1()))
}

test f05_consent_with_unsuspend_fails() fail {
  approve(Consent, consent_with_action(Unsuspend { author: module1_signer() }))
}

test f05_consent_with_recover_init_fails() fail {
  approve(Consent, consent_with_action(RecoverInit { guardians: [0] }))
}

test f05_consent_with_recover_cancel_fails() fail {
  approve(Consent, consent_with_action(RecoverCancel { guardians: [0] }))
}

test consent_with_change_controller_fails() fail {
  approve(Consent, consent_with_action(ChangeController))
}

test consent_with_bump_epoch_fails() fail {
  approve(Consent, consent_with_action(BumpEpoch))
}

test consent_with_recover_finalize_fails() fail {
  approve(Consent, consent_with_action(RecoverFinalize))
}

test consent_with_reclaim_fails() fail {
  approve(Consent, consent_with_action(Reclaim))
}

test consent_with_set_successor_fails() fail {
  approve(Consent, consent_with_action(SetSuccessor))
}

test consent_with_update_passes() {
  approve(Consent, consent_with_action(Update))
}

// ## Benchmarks (Part 2 §12.4): the largest record
//
// The transactions are module constants. Aiken evaluates constants at
// compile time, so the execution units of a bench test are the cost of the
// handler, not of building the transaction.

/// The largest record with ten holds; the Module-1 hold ends at `lo + 1 day`.
fn largest_suspended() -> IdentityDatum {
  with_holds(largest_record(Active), ten_holds(lo + day_ms))
}

fn largest_after_verdict_hold() -> IdentityDatum {
  // The nine other holds stay (the key parties 6, 7 and 8 count as current
  // authors here; the identity contract decides about removed authors).
  with_holds(
    largest_record(Active),
    list.concat(
      list.take(ten_holds(lo + day_ms), 9),
      [verdict_hold(lo + days_7_ms)],
    ),
  )
}

fn largest_consent_tx() -> Transaction {
  let full = largest_record(Active)
  let without_module1 =
    list.filter(full.parties, fn(party) { party.signer != module1_signer() })
  let before = IdentityDatum { ..full, parties: without_module1 }
  let after =
    IdentityDatum {
      ..full,
      parties: list.concat(without_module1, [module1_party(Accountable)]),
    }
  consent_update_tx(before, after)
}

const bench_suspend_tx: Transaction =
  suspend_tx(largest_suspended(), largest_after_verdict_hold())

const bench_revoke_tx: Transaction =
  apply_tx(
    revoke_verdict(),
    agent_a,
    largest_suspended(),
    tombstone(largest_record(Active), module1_revocation()),
    revoke_by_module1(),
  )

const bench_consent_tx: Transaction = largest_consent_tx()

const bench_oracle_tx: Transaction =
  oracle_resolution_tx(AuditorWins, base_verdict())

const bench_reclaim_tx: Transaction = reclaim_tx(base_verdict())

const bench_config: AuditingConfig =
  auditing_config(placeholder_verdict_policy())

test bench_apply_suspend_largest_record_withdraw() {
  withdraw_handler(
    identity_hash,
    ApplyVerdict,
    Script(module1_hash),
    bench_suspend_tx,
  )
}

test bench_apply_suspend_largest_record_spend() {
  spend_handler(verdict_ref(), bench_suspend_tx)
}

test bench_apply_suspend_largest_record_burn() {
  mint_handler(
    bench_config,
    identity_fixtures.challenge_hash,
    claim_hash,
    BurnVerdict,
    module1_hash,
    bench_suspend_tx,
  )
}

test bench_apply_revoke_largest_record_withdraw() {
  withdraw_handler(
    identity_hash,
    ApplyVerdict,
    Script(module1_hash),
    bench_revoke_tx,
  )
}

test bench_consent_update_largest_record_withdraw() {
  withdraw_handler(
    identity_hash,
    Consent,
    Script(module1_hash),
    bench_consent_tx,
  )
}

test bench_mint_verdict_oracle_resolution() {
  mint_handler(
    bench_config,
    identity_fixtures.challenge_hash,
    claim_hash,
    MintVerdict,
    module1_hash,
    bench_oracle_tx,
  )
}

test bench_resolution_verdict_requirement() {
  resolution_verdict_ok(bench_config, AuditorWins, bench_oracle_tx)
}

test bench_reclaim_spend() {
  spend_handler(verdict_ref(), bench_reclaim_tx)
}

test bench_reclaim_burn() {
  mint_handler(
    bench_config,
    identity_fixtures.challenge_hash,
    claim_hash,
    BurnVerdict,
    module1_hash,
    bench_reclaim_tx,
  )
}

// ## Cross-cutting rules also run when the result would be False

test consent_two_new_module1_parties() {
  approve(
    Consent,
    consent_register_tx(
      identity_fixtures.agent_record(
        controller_a,
        [],
        [
          accountable_key_party(),
          module1_party(Accountable),
          module1_party(Responsible),
        ],
        Active,
      ),
    ),
  )
}

test consent_range_rule_runs_when_result_is_false() fail {
  // No new Module-1 party: without the window failure the result is False.
  let before =
    IdentityDatum {
      ..consent_before(),
      parties: [accountable_key_party(), module1_party(Responsible)],
    }
  !approve(
    Consent,
    Transaction {
      ..consent_update_tx(before, before),
      validity_range: two_hour_range(),
    },
  )
}

test apply_range_rule_runs_when_result_is_false() fail {
  // The status does not change: without the window failure the result is
  // False.
  !approve(
    ApplyVerdict,
    Transaction {
      ..suspend_tx(record_a(Active), record_a(Active)),
      validity_range: two_hour_range(),
    },
  )
}

// ## Verdict expiry: mint, application and reclaim

/// The payout of a reclaim of `v`: all AP3X of the verdict output to
/// `v.deposit_to`, without datum.
fn reclaim_payout(v: IdentityVerdict) -> Output {
  Output {
    address: v.deposit_to,
    value: assets.from_lovelace(2_000_000),
    datum: NoDatum,
    reference_script: None,
  }
}

/// A reclaim of the verdict output of `v`: no withdrawal, the token is
/// burned, the AP3X goes to `v.deposit_to`, and the range starts 1 ms after
/// `v.expires_at`.
fn reclaim_tx(v: IdentityVerdict) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [verdict_input(v), wallet_input()],
    outputs: [reclaim_payout(v)],
    mint: assets.from_asset(module1_hash, verdict_token_name(v), -1),
    validity_range: interval.between(v.expires_at + 1, v.expires_at + 600_000),
    redeemers: [
      Pair(Spend(verdict_ref()), to_data(Void)),
      Pair(Mint(module1_hash), to_data(BurnVerdict)),
    ],
  }
}

fn reclaim_with_payout(payout: Output) -> Transaction {
  Transaction { ..reclaim_tx(base_verdict()), outputs: [payout] }
}

test mint_sets_expiry_30_days_after_upper_bound() {
  and {
    base_verdict().expires_at == hi + days_30_ms,
    mint_ok(cfg(), oracle_tx()),
  }
}

test mint_fails_with_other_expiry() {
  !mint_ok(
    cfg(),
    oracle_resolution_tx(
      AuditorWins,
      IdentityVerdict { ..base_verdict(), expires_at: hi + days_30_ms + 1 },
    ),
  )
}

test mint_fails_with_expiry_from_lower_bound() {
  !mint_ok(
    cfg(),
    oracle_resolution_tx(
      AuditorWins,
      IdentityVerdict { ..base_verdict(), expires_at: lo + days_30_ms },
    ),
  )
}

test mint_takes_deposit_address_from_verdict_output() {
  mint_ok(
    cfg(),
    oracle_resolution_tx(
      AuditorWins,
      IdentityVerdict {
        ..base_verdict(),
        deposit_to: script_address(other_script_hash),
      },
    ),
  )
}

test mint_fails_with_unbounded_range() fail {
  mint_ok(cfg(), Transaction { ..oracle_tx(), validity_range: interval.after(lo) })
}

test mint_fails_with_range_over_one_hour() fail {
  mint_ok(cfg(), Transaction { ..oracle_tx(), validity_range: two_hour_range() })
}

/// The verdict layout without `deposit_to` and `expires_at`.
type VerdictWithoutExpiry {
  agent: ByteArray,
  action: VerdictAction,
  duration: Int,
  challenge: OutputReference,
}

test mint_fails_with_verdict_without_expiry() fail {
  let v = base_verdict()
  let short =
    VerdictWithoutExpiry {
      agent: v.agent,
      action: v.action,
      duration: v.duration,
      challenge: v.challenge,
    }
  let name = blake2b_256(cbor.serialise(short))
  let tx = oracle_tx()
  mint_ok(
    cfg(),
    Transaction {
      ..with_last_output(
        tx,
        Output {
          address: script_address(module1_hash),
          value: assets.from_lovelace(2_000_000)
            |> assets.add(module1_hash, name, 1),
          datum: InlineDatum(short),
          reference_script: None,
        },
      ),
      mint: assets.zero
        |> assets.add(identity_fixtures.challenge_hash, identity_fixtures.challenge_token, -1)
        |> assets.add(claim_hash, identity_fixtures.claim_token, -1)
        |> assets.add(module1_hash, name, 1),
    },
  )
}

test apply_until_expiry() {
  let v = IdentityVerdict { ..base_verdict(), expires_at: hi }
  approve(
    ApplyVerdict,
    apply_tx(
      v,
      agent_a,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      suspend_by_module1(),
    ),
  )
}

test apply_fails_after_expiry() {
  let v = IdentityVerdict { ..base_verdict(), expires_at: hi - 1 }
  !approve(
    ApplyVerdict,
    apply_tx(
      v,
      agent_a,
      record_a(Active),
      suspended(record_a(Active), [], lo + days_7_ms),
      suspend_by_module1(),
    ),
  )
}

test reclaim_after_expiry() {
  let tx = reclaim_tx(base_verdict())
  and {
    spend_ok(tx),
    burn_ok(tx),
  }
}

test reclaim_with_more_ap3x_than_the_deposit() {
  spend_ok(
    reclaim_with_payout(
      Output {
        ..reclaim_payout(base_verdict()),
        value: assets.from_lovelace(5_000_000),
      },
    ),
  )
}

test reclaim_at_expiry_fails() {
  let v = base_verdict()
  let tx =
    Transaction {
      ..reclaim_tx(v),
      validity_range: interval.between(v.expires_at, v.expires_at + 600_000),
    }
  and {
    !spend_ok(tx),
    !burn_ok(tx),
  }
}

test reclaim_before_expiry_fails() {
  let tx = Transaction { ..reclaim_tx(base_verdict()), validity_range: interval.between(lo, hi) }
  and {
    !spend_ok(tx),
    !burn_ok(tx),
  }
}

test reclaim_fails_with_payout_to_other_address() {
  !spend_ok(
    reclaim_with_payout(
      Output {
        ..reclaim_payout(base_verdict()),
        address: script_address(other_script_hash),
      },
    ),
  )
}

test reclaim_fails_with_less_ap3x() {
  !spend_ok(
    reclaim_with_payout(
      Output {
        ..reclaim_payout(base_verdict()),
        value: assets.from_lovelace(1_999_999),
      },
    ),
  )
}

test reclaim_fails_with_datum_on_payout() {
  !spend_ok(
    reclaim_with_payout(
      Output { ..reclaim_payout(base_verdict()), datum: InlineDatum(base_verdict()) },
    ),
  )
}

test reclaim_fails_with_other_script_input() {
  let tx = reclaim_tx(base_verdict())
  !spend_ok(
    Transaction {
      ..tx,
      inputs: [
        claim_input(identity_fixtures.claim_token, challenged_claim_datum()),
        ..tx.inputs
      ],
    },
  )
}

test reclaim_fails_without_burn() {
  !spend_ok(Transaction { ..reclaim_tx(base_verdict()), mint: assets.zero })
}

test reclaim_fails_with_unbounded_range() fail {
  let v = base_verdict()
  spend_ok(
    Transaction {
      ..reclaim_tx(v),
      validity_range: interval.after(v.expires_at + 1),
    },
  )
}

test reclaim_fails_when_verdict_data_does_not_match_name() {
  let v = base_verdict()
  let tx = reclaim_tx(v)
  let input = verdict_input(v)
  let other = IdentityVerdict { ..v, expires_at: lo - 1 }
  !spend_ok(
    Transaction {
      ..tx,
      inputs: [
        Input {
          ..input,
          output: Output { ..input.output, datum: InlineDatum(other) },
        },
        wallet_input(),
      ],
      validity_range: window_range_of(lo),
    },
  )
}

/// A range of 10 minutes from `start`.
fn window_range_of(start: Int) -> interval.Interval {
  interval.between(start, start + 600_000)
}

// ## The claim validator is a parameter of identity_party

test mint_ignores_claim_validator_of_refs_record() fail {
  // The CrossValidatorRefs reference input names another claim validator,
  // and the claim input at that script names another claimer. The verdict
  // mint reads the claim only at the claim validator of its parameters.
  let named = other_script_hash
  let tx =
    oracle_resolution_tx(AuditorWins, verdict(agent_b, VerdictSuspend, days_7_ms))
  let refs = refs_input()
  let other_refs =
    Input {
      ..refs,
      output: Output {
        ..refs.output,
        datum: InlineDatum(
          CrossValidatorRefs { ..cross_refs(), claim_validator_hash: named },
        ),
      },
    }
  let other_claim =
    Input {
      output_reference: identity_fixtures.claim_ref(),
      output: Output {
        address: script_address(named),
        value: assets.from_lovelace(100_000_000)
          |> assets.add(named, identity_fixtures.claim_token, 1),
        datum: InlineDatum(
          ClaimDatum { ..challenged_claim_datum(), claimer_did: agent_b },
        ),
        reference_script: None,
      },
    }
  mint_ok(
    cfg(),
    Transaction {
      ..tx,
      inputs: list.map(
        tx.inputs,
        fn(input) {
          if input.output_reference == identity_fixtures.claim_ref() {
            other_claim
          } else {
            input
          }
        },
      ),
      reference_inputs: [other_refs, config_input(module1_hash)],
    },
  )
}
