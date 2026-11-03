# Module-1 identity tests (slices W2-M1 and W4-MODFIX)

Test modules: `lib/adversarial_auditing_tests/identity_party_tests.ak` (P),
`lib/adversarial_auditing_tests/identity_checks_tests.ak` (C), and the test
blocks in `validators/challenge.ak` (CH), `validators/claim.ak` (CL) and
`validators/jury_pool.ak` (J). Run `scripts/test-contracts.sh` from `Module-1/`.
The deploy-script tests are in `scripts/test_identity_deploy.py` (D); run
`python3 scripts/test_identity_deploy.py` from `Module-1/`.

## Part 2 §12.1 "Module-1 verdicts"

| Attack | Tests |
|---|---|
| Verdict mint without an oracle resolution | P `mint_fails_without_challenge_input`, `mint_fails_with_submit_evidence`, `mint_fails_with_timeout_resolve`, `mint_fails_with_cleanup_resolved`, `mint_fails_without_challenge_redeemer`, `mint_fails_with_two_challenge_inputs`, `mint_fails_when_challenge_input_has_no_token`, `mint_fails_for_jury_resolution`, `mint_fails_for_jury_resolution_with_claimer_wins` |
| Verdict whose agent, action, duration, challenge or expiry differs from the resolution | P `mint_fails_with_other_agent`, `mint_fails_with_other_action`, `mint_fails_with_other_duration`, `mint_fails_with_other_challenge`, `mint_fails_with_other_expiry`, `mint_fails_with_expiry_from_lower_bound`, `mint_fails_for_inconclusive`, `mint_fails_for_outcome_without_rule`, `mint_fails_when_claim_is_not_bound`, `mint_ignores_claim_validator_of_refs_record`, `mint_fails_with_suspension_over_30_days`, `mint_fails_with_revocation_duration`, `mint_fails_with_verdict_without_expiry` |
| Two verdict mints in one resolution | P `mint_fails_with_quantity_two`, `mint_fails_with_two_names`, `resolution_with_two_verdict_tokens_fails`, `resolution_with_quantity_two_fails` |
| Oracle resolution that the policy turns into a verdict, without the mint | P `resolution_without_verdict_mint_fails`, `resolution_with_mint_of_other_policy_fails`, `resolution_without_configuration_record_fails`, `resolution_with_configuration_record_*_fails`, `resolution_reads_module1_from_configuration_record`; CH `test_vor_fail_auditor_wins_without_verdict_mint`, `test_vor_fail_claimer_wins_with_rule_without_mint`, `test_vor_fail_without_configuration_record` |
| Verdict burn without a verdict application or an expiry reclaim | P `burn_fails_without_withdrawal`, `burn_fails_with_two_tokens`, `burn_with_consent_withdrawal_fails`, `mint_redeemer_burn_rejects_a_mint`, `spend_fails_without_burn`, `spend_fails_with_burn_of_other_token`, `spend_fails_with_two_burns`, `spend_fails_without_withdrawal`, `spend_fails_for_output_without_token`, `reclaim_before_expiry_fails`, `reclaim_at_expiry_fails` |
| Verdict data that does not match the token name | P `mint_fails_when_name_differs_from_datum`, `mint_fails_when_datum_differs_from_name`, `apply_fails_when_verdict_data_does_not_match_name`, `reclaim_fails_when_verdict_data_does_not_match_name`, `verdict_token_name_is_hash_of_serialised_verdict` |
| Suspend verdict applied as a revocation, and the reverse | P `apply_suspend_verdict_as_revocation_fails`, `apply_revoke_verdict_as_suspension_fails` |
| Verdict hold with another `until` | P `apply_suspend_fails_with_longer_until`, `apply_suspend_fails_with_shorter_until`, `apply_suspend_fails_with_other_reason`, `apply_suspend_fails_with_other_since`, `apply_suspend_fails_with_hold_by_other_author`, `apply_suspend_fails_when_status_stays_active` |
| Later, shorter verdict that shortens a Module-1 hold | P `apply_later_shorter_verdict_cannot_shorten_hold` (must pass: `apply_suspend_keeps_longer_module1_hold`, `apply_suspend_extends_shorter_module1_hold`, `apply_suspend_ignores_ended_module1_hold`) |
| Verdict revocation with `invalid_from` or a successor | P `apply_revoke_fails_with_invalid_from`, `apply_revoke_fails_with_successor`, `apply_revoke_fails_with_other_reason`, `apply_revoke_fails_with_other_author`, `apply_revoke_fails_with_other_time` |
| One verdict used twice | P `apply_without_burn_fails`, `apply_without_verdict_input_fails`, `apply_two_verdicts_fails` |
| Verdict applied to another agent | P `apply_to_other_agent_fails`, `apply_suspend_by_other_party_fails`, `apply_suspend_by_controller_fails`, `apply_revoke_by_controller_fails`, `apply_suspend_with_index_out_of_range_fails` |
| Verdict applied after its expiry | P `apply_fails_after_expiry` (must pass: `apply_until_expiry`) |
| Expired verdict reclaim with a wrong payout | P `reclaim_fails_with_payout_to_other_address`, `reclaim_fails_with_less_ap3x`, `reclaim_fails_with_datum_on_payout`, `reclaim_fails_with_other_script_input`, `reclaim_fails_without_burn` (must pass: `reclaim_after_expiry`, `reclaim_with_more_ap3x_than_the_deposit`) |
| Unsuspend of a Module-1 hold | P `apply_unsuspend_of_module1_hold_fails`, `consent_unsuspend_of_module1_hold_fails`, `f05_consent_with_unsuspend_fails` |
| Consent with rights below the highest rights, `not_after`, `removal_after`, `not_before` after `lo` | P `consent_fails_with_rights_below_highest_accountable`, `consent_fails_with_rights_below_highest_responsible`, `consent_fails_with_rights_above_highest`, `consent_fails_with_not_after`, `consent_fails_with_removal_after`, `consent_fails_with_not_before_after_lo`, `consent_fails_with_other_role`, `consent_register_fails_with_bad_terms`, `consent_fails_with_duplicate_module1_party` |
| Consent Update that removes or changes another Module-1 party | P `consent_fails_when_it_removes_other_module1_party`, `consent_fails_when_it_removes_noticed_module1_party`, `consent_fails_when_it_changes_other_module1_party`, `consent_fails_without_new_module1_party`, `consent_fails_when_update_changes_status` |

Positive cases: P `mint_*_names_*`, `mint_oracle_auditor_wins_suspend`,
`mint_accepts_suspension_of_30_days`, `mint_sets_expiry_30_days_after_upper_bound`,
`mint_takes_deposit_address_from_verdict_output`,
`apply_suspend_on_active_record`, `apply_suspend_keeps_other_holds`,
`apply_revoke`, `apply_revoke_on_suspended_record`, `apply_until_expiry`,
`reclaim_after_expiry`, `consent_update_adds_*`,
`consent_register_with_module1`, `consent_adopt_with_module1`,
`consent_keeps_existing_module1_party_and_adds_one`,
`consent_on_suspended_record_keeps_status`, `consent_two_new_module1_parties`;
CH `test_vor_auditor_wins_with_verdict_mint`,
`test_vor_claimer_wins_without_rule_needs_no_mint`,
`test_vrj_auditor_wins_without_verdict_mint`, `test_vrj_claimer_wins`.

## Part 2 §12.1 "Parties": Module-1 items

| Attack | Tests |
|---|---|
| F-05: a Module-1 consent withdrawal attached to Revoke, Retire, Suspend, Unsuspend, RecoverInit or RecoverCancel | P `f05_consent_with_revoke_fails`, `f05_consent_with_revoke_by_module1_fails`, `f05_consent_with_retire_fails`, `f05_consent_with_suspend_fails`, `f05_consent_with_unsuspend_fails`, `f05_consent_with_recover_init_fails`, `f05_consent_with_recover_cancel_fails`; also `consent_with_change_controller_fails`, `consent_with_bump_epoch_fails`, `consent_with_recover_finalize_fails`, `consent_with_reclaim_fails`, `consent_with_set_successor_fails` (must pass: `consent_with_update_passes`) |
| Consent with a verdict mint or burn | P `consent_fails_with_verdict_mint`, `burn_with_consent_withdrawal_fails` |

## Cross-cutting rules for each action

| Rule | Tests |
|---|---|
| Range over one hour, unbounded range | P `apply_suspend_with_range_over_one_hour_fails`, `apply_revoke_with_range_over_one_hour_fails`, `apply_with_unbounded_range_fails`, `apply_range_rule_runs_when_result_is_false`, `consent_update_with_range_over_one_hour_fails`, `consent_register_with_range_over_one_hour_fails`, `consent_with_unbounded_range_fails`, `consent_range_rule_runs_when_result_is_false`, `mint_fails_with_unbounded_range`, `mint_fails_with_range_over_one_hour`, `reclaim_fails_with_unbounded_range`; C `identity_with_range_over_one_hour_fails`, `identity_with_unbounded_range_fails`, `identity_range_rule_applies_without_record`; CL `submit_claim_fails_with_range_over_one_hour`, `submit_claim_fails_with_unbounded_range`; CH `test_voc_identity_fails_with_range_over_one_hour`; J `register_juror_fails_with_range_over_one_hour`, `register_juror_fails_with_unbounded_range` |
| One identity input, one record output | P `apply_with_two_identity_inputs_fails`, `apply_with_two_record_outputs_fails`, `consent_fails_with_two_identity_inputs`, `consent_fails_with_two_record_outputs`, `consent_fails_with_identity_mint_in_update` |
| Verdict output: address, value, datum, reference script | P `mint_fails_with_datum_hash`, `mint_fails_with_output_stake_credential`, `mint_fails_with_output_at_other_script`, `mint_fails_with_foreign_asset`, `mint_fails_with_reference_script`, `mint_fails_when_no_output_holds_token`, `apply_fails_when_verdict_input_is_not_at_party_address` |
| Withdraw handler only for its own script credential | P `approval_for_key_credential_fails` |

## Part 2 §7 identity checks (claim submission, challenge opening, juror registration)

| Check | Tests |
|---|---|
| Declared credential: controller or listed key with the transaction purpose | C `identity_ok_with_controller_credential`, `identity_ok_with_listed_transaction_key`, `identity_fails_with_key_without_transaction_purpose`, `identity_fails_with_p256_key`, `identity_fails_with_unlisted_key`, `identity_fails_with_script_credential`, `identity_fails_with_controller_of_other_agent`, `declared_credential_rules`; CL `submit_claim_with_*`, `submit_claim_fails_with_unlisted_key`, `submit_claim_fails_with_key_without_transaction_purpose`; CH `test_voc_identity_listed_transaction_key`, `test_voc_identity_fails_with_unlisted_key`, `test_voc_identity_fails_with_key_without_transaction_purpose`; J `register_juror_with_*`, `register_juror_fails_with_unlisted_key`, `register_juror_fails_with_key_without_transaction_purpose` |
| Effective status Active | C `identity_fails_when_suspended`, `identity_ok_after_hold_ends`, `identity_fails_when_revoked`, `identity_fails_when_retired`, `identity_fails_when_unaccountable`, `identity_fails_when_accountable_party_*`, `identity_subagent_*`; CL `submit_claim_fails_when_suspended`, `submit_claim_fails_when_unaccountable`, `submit_claim_fails_when_revoked`; CH `test_voc_identity_fails_when_auditor_suspended`, `test_voc_identity_fails_when_auditor_unaccountable`, `test_voc_identity_fails_when_auditor_retired`; J `register_juror_fails_when_suspended`, `register_juror_fails_when_unaccountable` |
| Only an authentic identity record counts; the old registry token does not | C `identity_fails_without_record`, `identity_fails_with_legacy_registry_token`, `identity_fails_with_record_of_other_agent`, `identity_fails_with_record_at_staked_address`, `identity_fails_with_record_at_key_address`, `identity_fails_with_token_of_other_policy`, `identity_fails_with_datum_hash`; CL `submit_claim_fails_without_record`, `submit_claim_fails_with_legacy_registry_token`, `submit_claim_fails_with_record_of_other_agent`; CH `test_voc_fail_no_identity_record`, `test_voc_identity_fails_with_legacy_registry_token`; J `register_juror_fails_without_record`, `register_juror_fails_with_legacy_registry_token` |
| Challenge bound to its claim (`claim_token`) | CH `test_voc_fail_claim_token_not_bound`, `test_voc_fail_claim_input_without_token`, `test_vrj_fail_when_claim_token_changes`, `test_vse_fail_when_claim_token_changes`; P `mint_fails_when_claim_is_not_bound` |
| Configuration record reader | C `config_record_hash_*` |

## Resolutions, forfeits and cleanup (W4-MODFIX)

| Rule | Tests |
|---|---|
| OracleResolve, ResolveJury and TimeoutResolve accept only the claim input that holds the challenge's `claim_token`, and only the burn of that claim token | CH `test_vor_fail_with_claim_of_other_challenge`, `test_vrj_fail_with_claim_of_other_challenge`, `test_vtr_fail_with_claim_of_other_challenge`, `test_vor_fail_when_other_claim_token_burned`, `test_vtr_fail_when_other_claim_token_burned` (must pass: `test_vtr_timeout_with_bound_claim`) |
| ForfeitClaim accepts only the resolution of the challenge whose datum names this claim | CL `forfeit_fails_with_burn_of_other_challenge`, `forfeit_fails_with_timeout_of_other_challenge`, `forfeit_fails_with_resolved_output_of_other_challenge`, `forfeit_fails_with_cleanup_of_other_challenge`, `forfeit_fails_without_challenge_input` (must pass: `forfeit_in_oracle_resolution_of_own_challenge`, `forfeit_in_jury_resolution_of_own_challenge`, `forfeit_in_timeout_of_own_challenge`) |
| ResolveJury writes `Resolved { verdict, refund_to }` with the tallied verdict | CH `test_vrj_fail_with_other_outcome_in_state` (must pass: `test_vrj_refund_address_is_free`) |
| CleanupResolved pays all AP3X of the Resolved output to `refund_to`, without datum, and spends no other script input | CH `test_vcr_fail_when_kept_stake_goes_elsewhere`, `test_vcr_fail_with_less_than_kept_stake`, `test_vcr_fail_with_datum_on_refund`, `test_vcr_fail_with_forfeit_of_other_claim`, `test_vcr_fail_with_other_script_input`, `test_vcr_fail_before_cleanup_time` (must pass: `test_vcr_cleanup_pays_refund_address`) |
| The deploy plan refuses all-zero hashes; identity_party gets (AuditingConfig, challenge hash, claim hash) | D `test_plan_refuses_each_all_zero_hash`, `test_plan_refuses_the_default_zero_values_without_placeholders_list`, `test_unsafe_values_lists_placeholders_and_zero_fields_once`, `test_identity_party_parameters` (must pass: `test_complete_config_goes_to_the_build`, `test_dry_run_accepts_zero_values`) |

## Benchmarks (Part 2 §12.4)

The `bench_*` tests use module constants, which Aiken builds at compile time,
so their execution units are the handler cost. See the slice reports for the
numbers.

## Decisions

1. **Verdict agent.** The agent of a verdict is the losing side: AuditorWins
   names the claimer, ClaimerWins names the auditor. Inconclusive outcomes and
   timeouts give no verdict. The verdict policy holds one optional rule for
   each decisive outcome.
2. **Oracle resolutions only.** Only OracleResolve gives a verdict. The
   auditor names the eligible jurors of a challenge, so a jury outcome is not
   safe for an identity verdict. ResolveJury neither mints nor requires a
   verdict, and the verdict mint fails for every other challenge redeemer.
   Jury mode still moves the claim and challenge stakes. With
   `oracle_active = false`, no challenge reaches OracleResolve, so no verdict
   is minted.
3. **Challenge reference.** `challenge` in a verdict is the output reference
   of the challenge input that the resolution spends. A UTxO is spent once, so
   a verdict token name is minted at most once.
4. **Claim binding.** `ChallengeDatum` has a last field `claim_token`.
   OpenChallenge checks that the consumed claim input holds that claim token,
   and every continuing challenge output keeps it. OracleResolve, ResolveJury
   and TimeoutResolve take the claim stake and the claimer credential only
   from the claim input that holds this token, and they require the burn of
   this token. The verdict mint reads the claimer from the same input, at the
   claim validator that identity_party takes as a parameter.
5. **Verdict policy in AuditingConfig.** The policy, `config_policy` and
   `config_holder` are fields of `AuditingConfig`. All Module-1 validators get
   the same value, so the challenge validator and identity_party cannot use
   different policies.
6. **Policy rule shape.** A suspension lasts more than 0 ms and at most 30
   days; a revocation has duration 0. The mint checks the rule it uses.
7. **Verdict output.** The token goes to the exact enterprise address of
   identity_party with AP3X and the token only, the verdict as inline datum
   and no reference script.
8. **Verdict expiry.** A verdict holds `deposit_to` and `expires_at`, and the
   token name hashes all fields. `expires_at` is the upper bound of the
   resolution transaction plus 30 days; the resolution range has finite
   bounds and lasts at most one hour. `deposit_to` comes from the verdict
   output: the resolution builder funds the AP3X of the verdict output and
   names its own address (for example the enterprise address of the key that
   pays the fee). An application needs `hi <= expires_at`. A reclaim needs
   `expires_at < lo`, so the two never overlap. A reclaim burns the token,
   pays at least the AP3X of the verdict output to exactly `deposit_to`
   without datum, has no identity_party withdrawal and spends no other script
   input. Anybody can submit it. A verdict that nobody applies within 30 days
   lapses.
9. **Consent.** One or two new Module-1 parties with different roles; every
   old Module-1 party stays unchanged, also one whose removal notice has
   passed (remove it in a separate Update first). A Consent never accepts a
   mint or burn of the verdict policy.
10. **Burn and spend.** The burn and the spend of a verdict output require the
    identity_party withdrawal (an application, which only ApplyVerdict
    accepts) or the expiry reclaim of decision 8.
11. **ForfeitClaim.** A claim is forfeited only in the resolution of its own
    challenge: the challenge input whose datum names this claim's token must
    have its challenge token burned (OracleResolve, TimeoutResolve) or kept in
    a continuing output with the state Resolved (ResolveJury). A burn or a
    Resolved output of another challenge does not count.
12. **CleanupResolved payout.** ResolveJury keeps AP3X equal to the auditor
    stake in the Resolved output; its payouts already use both stakes, so the
    ResolveJury transaction funds that AP3X and names `refund_to` in the
    Resolved state. CleanupResolved pays all AP3X of the Resolved output to
    `refund_to` without datum, and spends no other script input (so it cannot
    forfeit a claim). Anybody can submit it after the cleanup time.
13. **Window.** Consent, ApplyVerdict, the verdict mint, the reclaim and the
    three identity checks force the one-hour window rule before any branch.
