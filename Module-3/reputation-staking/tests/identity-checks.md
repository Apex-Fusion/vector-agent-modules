# Module-3 identity checks: test map (slices W2-M36 and W4-MODFIX)

Scope: Part 2 §7 of `docs/IDENTITY_REGISTRY.md` (agents-marketplace), Module-3
row and the Time rule; the library rules of §3.10 that Module-3 uses
(`find_record`, `effective`, `has_capability`, `controller_signed`,
`active_key` through `declared_credential`); the §12.1 items that apply to a
module that reads identity records (time, record at a wrong address or with
a fake token, suspended or unaccountable agent, record of another agent,
subagents).

Run: `aiken check` in `Module-3/reputation-staking`, or
`shared/scripts/test-contracts.sh` (vendored library check first).

Test files:

- `lib/reputation_staking/identity_check_tests.ak` (library functions)
- `lib/reputation_staking/exit_rule_tests.ak` (rule D-exit: exit payee and
  declared credentials of openings)
- `validators/identity_check_tests.ak` (handlers of the `endorsement` and
  `reputation` validators: WithdrawChallenge and MintHistoryBonus)
- `validators/exit_rule_tests.ak` (rule D-exit for WithdrawChallenge)
- `shared/lib/shared/identity_tests.ak` (the shared helpers, also run here)

Actions that read an identity record:

| Action | Kind | Agents and checks |
|---|---|---|
| CreateStake | opens | staker: Active, each staked capability in the record, controller signs; owner_credential is a declared credential of the record and signs |
| IncreaseStake | extends | staker: Active, the stake's capabilities in the record, controller signs (and owner_credential signs) |
| UpdateCapabilities | extends | staker: same, with the new capabilities |
| MintEndorsementToken | opens | endorser: Active, controller signs; endorser_credential is a declared credential of the record and signs; target: Active, each endorsed capability in its record |
| IncreaseEndorsement | extends | endorser: Active, controller signs (and endorser_credential signs); target: as for MintEndorsementToken |
| MintChallengeToken | opens | challenger: Active, controller signs, record `created_at` + `min_agent_age` ≤ `lo`; challenger_credential is a declared credential of the record and signs. Target: its record is not read (rule D-dodge, R5-8). The transaction spends the target's StakeUTXO at the exact reputation address; the stake lists the challenged capability and its owner_credential is target_credential |
| MintHistoryBonus | extends | bonus agent: Active, controller signs (and the oracle signs, as before) |
| DecreaseStake | exit | staker: controller signs; any status; payout to owner_credential while the record trusts it, else to the controller key |
| WithdrawEndorsement | exit | endorser: controller signs; any status; payout to endorser_credential while the record trusts it, else to the controller key |
| WithdrawChallenge | exit | challenger: controller signs; any status; payout to challenger_credential while the record trusts it, else to the controller key |

A declared credential is `VerificationKey(h)` where `h` is
`blake2b_224(record.controller)` or the hash of a listed key with the
transaction purpose (`identity.declared_credential`). Every action in the
table needs a validity range with finite bounds and `hi − lo ≤ 3,600,000`
(`shared/identity.window`). Other actions read no identity record.

## Test map

| Item | Expected | Tests |
|---|---|---|
| Active agent, the controller signs | pass | `create_stake_active_agent`, `increase_stake_active_agent`, `update_capabilities_active_agent`, `mint_endorsement_active_agents`, `increase_endorsement_active_agents`, `mint_challenge_active_agents`, `mint_bonus_active_agent`, `decrease_stake_active_agent`, `withdraw_endorsement_active_agent`, `withdraw_challenge_active_agent` |
| Range of exactly one hour | pass | `create_stake_range_of_one_hour`, `increase_stake_range_of_one_hour`, `update_capabilities_range_of_one_hour`, `mint_endorsement_range_of_one_hour`, `increase_endorsement_range_of_one_hour`, `mint_challenge_range_of_one_hour`, `mint_bonus_range_of_one_hour`, `decrease_stake_range_of_one_hour`, `withdraw_endorsement_range_of_one_hour`, `withdraw_challenge_range_of_one_hour` |
| Time: range over one hour (every action that reads a record) | fail | `create_stake_range_over_one_hour_fails`, `increase_stake_range_over_one_hour_fails`, `update_capabilities_range_over_one_hour_fails`, `mint_endorsement_range_over_one_hour_fails`, `increase_endorsement_range_over_one_hour_fails`, `mint_challenge_range_over_one_hour_fails`, `mint_bonus_range_over_one_hour_fails`, `decrease_stake_range_over_one_hour_fails`, `withdraw_endorsement_range_over_one_hour_fails`, `withdraw_challenge_range_over_one_hour_fails` |
| Time: unbounded range | fail | `create_stake_open_range_fails`, `increase_stake_open_range_fails`, `update_capabilities_open_range_fails`, `mint_endorsement_open_range_fails`, `increase_endorsement_open_range_fails`, `mint_challenge_open_range_fails`, `mint_bonus_open_range_fails`, `decrease_stake_open_range_fails`, `withdraw_endorsement_open_range_fails`, `withdraw_challenge_open_range_fails`; `window_rejects_open_ranges` (shared) |
| Suspended agent (hold in force) | opens/extends fail; exits pass | `create_stake_suspended_agent_fails`, `increase_stake_suspended_agent_fails`, `update_capabilities_suspended_agent_fails`, `mint_endorsement_suspended_endorser_fails`, `increase_endorsement_suspended_endorser_fails`, `mint_challenge_suspended_challenger_fails`, `mint_bonus_suspended_agent_fails`; `decrease_stake_suspended_agent`, `withdraw_endorsement_suspended_agent`, `withdraw_challenge_suspended_agent` |
| Unaccountable agent (no Accountable Party covers the range) | opens/extends fail; exits pass | `create_stake_unaccountable_agent_fails`, `create_stake_accountable_party_ending_in_range_fails`, `increase_stake_unaccountable_agent_fails`, `update_capabilities_unaccountable_agent_fails`, `mint_endorsement_unaccountable_endorser_fails`, `increase_endorsement_unaccountable_endorser_fails`, `mint_challenge_unaccountable_challenger_fails`, `mint_bonus_unaccountable_agent_fails`; `decrease_stake_unaccountable_agent`, `withdraw_endorsement_unaccountable_agent`, `withdraw_challenge_unaccountable_agent` |
| Revoked agent (tombstone, no listed keys) | opens/extends fail; exits pass and pay the controller key | `create_stake_revoked_agent_fails`, `increase_stake_revoked_agent_fails`, `update_capabilities_revoked_agent_fails`, `mint_endorsement_revoked_endorser_fails`, `increase_endorsement_revoked_endorser_fails`, `mint_challenge_revoked_challenger_fails`, `mint_bonus_revoked_agent_fails`; `decrease_stake_revoked_agent`, `withdraw_endorsement_revoked_agent`, `withdraw_challenge_revoked_agent`; `decrease_stake_revoked_agent_paying_dropped_key_fails` |
| Retired agent (tombstone, no listed keys) | opens/extends fail; exits pass and pay the controller key | `create_stake_retired_agent_fails`, `increase_stake_retired_agent_fails`, `update_capabilities_retired_agent_fails`, `mint_endorsement_retired_endorser_fails`, `increase_endorsement_retired_endorser_fails`, `mint_challenge_retired_challenger_fails`, `mint_bonus_retired_agent_fails`; `decrease_stake_retired_agent`, `withdraw_endorsement_retired_agent`, `withdraw_challenge_retired_agent`; `withdraw_endorsement_retired_agent_paying_dropped_key_fails` |
| Endorsement target suspended, unaccountable, revoked or retired | fail | `mint_endorsement_suspended_target_fails`, `mint_endorsement_unaccountable_target_fails`, `mint_endorsement_revoked_target_fails`, `mint_endorsement_retired_target_fails`, `increase_endorsement_suspended_target_fails`, `increase_endorsement_revoked_target_fails` |
| Challenge target suspended, unaccountable, revoked or retired, or without the capability in its record (D-dodge, R5-8) | pass while its stake lists the capability | `mint_challenge_suspended_target_allowed`, `mint_challenge_unaccountable_target_allowed`, `mint_challenge_revoked_target_allowed`, `mint_challenge_retired_target_allowed`, `mint_challenge_capability_not_in_target_record_allowed`, `mint_challenge_without_target_record_allowed`, `mint_challenge_target_record_with_fake_token_allowed` |
| Challenge target stake missing, not spent, of another agent, or without the capability | fail | `mint_challenge_without_target_stake_fails`, `mint_challenge_with_target_stake_as_reference_input_fails`, `mint_challenge_with_stake_of_other_agent_fails`, `mint_challenge_stake_without_capability_fails` |
| Wrong credential: the controller does not sign | fail | `create_stake_without_controller_signature_fails`, `increase_stake_without_controller_signature_fails`, `update_capabilities_without_controller_signature_fails`, `mint_endorsement_without_controller_signature_fails`, `increase_endorsement_without_controller_signature_fails`, `mint_challenge_without_controller_signature_fails`, `mint_bonus_without_controller_signature_fails`, `decrease_stake_without_controller_signature_fails`, `withdraw_endorsement_without_controller_signature_fails`, `withdraw_challenge_without_controller_signature_fails`; `f3_create_stake_wrong_owner_succeeds` (audit_exploit_tests) |
| Wrong credential: another agent's controller signs | fail | `create_stake_signed_by_other_controller_fails`, `increase_stake_signed_by_other_controller_fails`, `update_capabilities_signed_by_other_controller_fails`, `mint_endorsement_signed_by_target_controller_fails`, `increase_endorsement_signed_by_target_controller_fails`, `mint_challenge_signed_by_target_controller_fails`, `mint_bonus_signed_by_other_controller_fails`, `decrease_stake_signed_by_other_controller_fails`, `withdraw_endorsement_signed_by_target_controller_fails`, `withdraw_challenge_signed_by_target_controller_fails` |
| The position's credential still signs an opening | fail without it | `create_stake_without_owner_signature_fails`, `mint_endorsement_without_endorser_credential_signature_fails`, `mint_challenge_without_challenger_credential_signature_fails`; `auth_create_stake_unsigned_fails`, `auth_script_credential_owner_fails` (audit_exploit_tests) |
| Missing capability (record does not list a staked or endorsed capability) | fail | `create_stake_capability_not_in_record_fails`, `increase_stake_capability_not_in_record_fails`, `update_capabilities_capability_not_in_record_fails`, `mint_endorsement_capability_not_in_target_record_fails`, `increase_endorsement_capability_not_in_target_record_fails` |
| Record at a wrong address: identity script with a stake credential | fail | `create_stake_record_with_stake_credential_fails`, `increase_stake_record_with_stake_credential_fails`, `mint_endorsement_record_with_stake_credential_fails`, `mint_challenge_record_with_stake_credential_fails`, `mint_bonus_record_with_stake_credential_fails`, `decrease_stake_record_with_stake_credential_fails`, `withdraw_endorsement_record_with_stake_credential_fails`, `withdraw_challenge_record_with_stake_credential_fails` |
| Record at a wrong address: another script | fail | `create_stake_record_at_other_script_fails`, `update_capabilities_record_at_other_script_fails`, `mint_endorsement_target_record_at_other_script_fails`, `increase_endorsement_record_at_other_script_fails`, `mint_challenge_record_at_other_script_fails`, `withdraw_challenge_record_at_other_script_fails` |
| Record with a fake token (same name, other policy) | fail | `create_stake_record_with_fake_token_fails`, `increase_stake_record_with_fake_token_fails`, `update_capabilities_record_with_fake_token_fails`, `mint_endorsement_record_with_fake_token_fails`, `increase_endorsement_record_with_fake_token_fails`, `mint_challenge_record_with_fake_token_fails`, `mint_bonus_record_with_fake_token_fails`, `decrease_stake_record_with_fake_token_fails`, `withdraw_endorsement_record_with_fake_token_fails`, `withdraw_challenge_record_with_fake_token_fails` |
| No record, or only the record of another agent | fail | `create_stake_record_of_other_agent_fails`, `increase_stake_without_record_fails`, `mint_endorsement_without_target_record_fails`, `mint_bonus_without_record_fails`, `decrease_stake_without_record_fails`, `withdraw_endorsement_without_record_fails`, `withdraw_challenge_without_record_fails` |
| Subagent with an Active parent | pass | `create_stake_subagent_active_parent`, `increase_stake_subagent_active_parent`, `update_capabilities_subagent_active_parent`, `mint_endorsement_subagent_active_parent`, `mint_challenge_subagent_active_parent`, `mint_bonus_subagent_active_parent` |
| Subagent: parent suspended, unaccountable or revoked | fail | `create_stake_subagent_suspended_parent_fails`, `create_stake_subagent_revoked_parent_fails`, `increase_stake_subagent_suspended_parent_fails`, `mint_endorsement_subagent_suspended_parent_fails`, `mint_challenge_subagent_unaccountable_parent_fails` |
| Subagent: stale `parent_epoch` (the parent increased its epoch) | fail | `create_stake_subagent_after_parent_epoch_increase_fails`, `update_capabilities_subagent_after_parent_epoch_increase_fails`, `increase_endorsement_subagent_after_parent_epoch_increase_fails`, `mint_challenge_subagent_after_parent_epoch_increase_fails`, `mint_bonus_subagent_after_parent_epoch_increase_fails` |
| Subagent: parent record missing | fail (`fail` test, at the parent lookup) | `create_stake_subagent_without_parent_record_fails`, `increase_stake_subagent_without_parent_record_fails`, `mint_endorsement_subagent_without_parent_record_fails`, `mint_challenge_subagent_without_parent_record_fails`, `mint_bonus_subagent_without_parent_record_fails` |
| Exit by a subagent without its parent's record | pass | `decrease_stake_subagent_without_parent_record`, `withdraw_endorsement_subagent_without_parent_record`, `withdraw_challenge_subagent_without_parent_record` |
| Exit: the target's record is not needed | pass | `withdraw_endorsement_without_target_record` |
| Challenger age from the record's `created_at`, compared with `lo` | pass at the limit; fail 1 ms before | `mint_challenge_challenger_exactly_old_enough`, `mint_challenge_challenger_too_young_fails` |
| target_credential is the target stake's owner_credential | fail otherwise | `mint_challenge_with_other_target_credential_fails`, `target_credential_script_is_not_the_stake_owner` |
| CapabilityVerified distribution without a history bonus mint | pass; underpaid target fails | `distribute_verified_without_history_bonus`, `distribute_verified_underpaying_target_fails` |
| Module-1 ChallengeDatum mirror with `claim_token` and `Resolved { verdict, refund_to }` | pass | `resolve_escalation_claimer_wins`, `resolve_escalation_auditor_wins`, `resolve_escalation_inconclusive`, `resolve_escalation_reads_verdict_next_to_refund_to`; the layout without refund_to fails: `resolve_escalation_resolved_without_refund_to_fails` (module1_integration_tests) |
| D-exit: exit with the stored credential still trusted | pays the stored credential | `decrease_stake_pays_trusted_stored_credential`, `withdraw_endorsement_pays_trusted_stored_credential`, `withdraw_challenge_pays_trusted_stored_credential`; paying the controller instead fails: `decrease_stake_paying_controller_instead_of_trusted_key_fails` |
| D-exit: exit after a controller change | pays the new controller key; the old key fails | `decrease_stake_after_controller_change_pays_new_controller`, `withdraw_endorsement_after_controller_change_pays_new_controller`, `withdraw_challenge_after_controller_change_pays_new_controller`; `decrease_stake_after_controller_change_paying_old_key_fails`, `withdraw_endorsement_after_controller_change_paying_old_key_fails`, `withdraw_challenge_after_controller_change_paying_old_key_fails` |
| D-exit: exit after the removal of the listed key that is the stored credential | pays the controller key; the removed key fails | `decrease_stake_after_key_removal_pays_controller`, `withdraw_endorsement_after_key_removal_pays_controller`, `withdraw_challenge_after_key_removal_pays_controller`; `decrease_stake_after_key_removal_paying_removed_key_fails`, `withdraw_endorsement_after_key_removal_paying_removed_key_fails`, `withdraw_challenge_after_key_removal_paying_removed_key_fails` |
| D-exit: the payout adds up the outputs to the payee | pass | `decrease_stake_payout_in_two_outputs`, `expire_challenge_paid_in_two_outputs` |
| D-exit: opening with a credential that is not a declared credential | fail (also when that key signs) | `create_stake_with_undeclared_owner_fails`, `mint_endorsement_with_undeclared_credential_fails`, `mint_challenge_with_undeclared_credential_fails`; positive: `create_stake_with_listed_key_as_owner`, `create_stake_with_controller_key_as_owner`, `mint_endorsement_with_listed_key`, `mint_challenge_with_listed_key` |

The `fail` tests of the subagent rows fail at the parent lookup of
`effective` (`expect Some(parent_record) = find_record(...)`); the run shows
the trace. The `fail` tests of the challenge stake row fail at the stake
lookup (`spent_target_stake`). A scratch copy with each check removed in turn
(status gate, controller signature, capability check, one-hour bound, exit
signature, target credential, challenger age, exact address) makes the
matching negative tests fail. The W4-MODFIX scratch copies with the old rule
of each item made the item's new tests fail (see the W4-MODFIX report).

## Decisions

1. **Exits (rule D-exit, W4).** DecreaseStake, WithdrawEndorsement and
   WithdrawChallenge need only the controller's signature
   (`controller_signed`), for a record of any status, also a tombstone. The
   payout goes to the stored credential (owner_credential,
   endorser_credential, challenger_credential) while the record still trusts
   it (`identity.declared_credential`). Else it goes to the controller key of
   the record. So after a controller change, a recovery or a key removal,
   the funds go to a key that the record trusts now. The payout checks add
   up all outputs to the payee, and each validator still accepts one script
   input per transaction.
2. **Openings store only a declared credential (W4).** CreateStake,
   MintEndorsementToken and MintChallengeToken need the controller's
   signature, the signature of the stored credential, and a stored
   credential that is a declared credential of the acting agent's record.
   So rule 1 never moves the funds of a credential that the record never
   trusted.
3. **Agents involved.** The acting agent (staker, endorser, challenger, bonus
   agent) needs status Active and the controller's signature. The target of
   an endorsement needs status Active and the capabilities in its record; it
   does not sign. The target of a challenge is not read (rule D-dodge, R5-8):
   its StakeUTXO decides. The stake must list the challenged capability, its
   owner_credential must be target_credential, and the challenge spends it
   (the stake takes the lock of the challenge). A target record is not
   needed: the stake exists only after an Active opening, and requiring the
   record would let a missing record block challenges.
4. **History bonus.** MintHistoryBonus needs the bonus agent's Active record
   and its controller's signature. Because of this, the CapabilityVerified
   distribution no longer requires a history bonus mint: else a target that
   is not Active, or that does not sign, would lock the challenge stake. The
   distribution can still mint the bonus.
5. **target_credential.** MintChallengeToken checks that target_credential is
   the owner_credential of the target's StakeUTXO. The CapabilityVerified
   payout and RespondToChallenge use target_credential, so it must be the
   target's own credential.
6. **Challenger age.** `record.created_at + min_agent_age <= lo`
   (`created_at` is the upper bound of the registering transaction).
7. **Subagents** may stake, endorse and challenge (the Module-3 row has no
   `independent` rule). Their status includes their ancestors (`effective`).
   An exit reads no ancestor.
8. **Unchanged actions.** RespondToChallenge and EscalateToAudit keep their
   credential signatures; they read no identity record. ClaimDecayRefund,
   SlashStake, LockForChallenge, ReleaseChallenge, SlashEndorsement,
   ResolveChallenge, ResolveEscalation, DefaultJudgment, DistributeOutcome
   and ExpireChallenge are not agent actions and read no identity record.
   The permissionless payouts (DistributeOutcome, ExpireChallenge,
   SlashEndorsement) still pay the stored credentials.
9. **Config.** The legacy registry fields are removed; `identity_hash` is the
   last field of ReputationConfig.

## Gaps (spec or design questions; not changed here)

- **Permissionless payouts after a key change.** DistributeOutcome,
  ExpireChallenge and SlashEndorsement pay the stored credentials, also when
  the record no longer trusts them (rule D-exit covers exits at the owner's
  request only).
- **MintHistoryBonus input rule (not identity).** A bonus mint needs a spend at
  the endorsement validator, so bonuses for Module-1 events need such a spend
  too.
