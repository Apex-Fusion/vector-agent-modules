# Module-3 identity checks: test map (slice W2-M36)

Scope: Part 2 §7 of `docs/IDENTITY_REGISTRY.md` (agents-marketplace), Module-3
row and the Time rule; the library rules of §3.10 that Module-3 uses
(`find_record`, `effective`, `has_capability`, `controller_signed`); the §12.1
items that apply to a module that reads identity records (time, record at a
wrong address or with a fake token, suspended or unaccountable agent, record of
another agent, subagents).

Run: `aiken check` in `Module-3/reputation-staking`, or
`shared/scripts/test-contracts.sh` (vendored library check first).

Test files:

- `lib/reputation_staking/identity_check_tests.ak` (library functions)
- `validators/identity_check_tests.ak` (handlers of the `endorsement` and
  `reputation` validators: WithdrawChallenge and MintHistoryBonus)
- `shared/lib/shared/identity_tests.ak` (the shared helpers, also run here)

Actions that read an identity record:

| Action | Kind | Agents and checks |
|---|---|---|
| CreateStake | opens | staker: Active, each staked capability in the record, controller signs (and owner_credential signs) |
| IncreaseStake | extends | staker: same, with the stake's capabilities |
| UpdateCapabilities | extends | staker: same, with the new capabilities |
| MintEndorsementToken | opens | endorser: Active, controller signs (and endorser_credential signs); target: Active, each endorsed capability in its record |
| IncreaseEndorsement | extends | same as MintEndorsementToken |
| MintChallengeToken | opens | challenger: Active, controller signs (and challenger_credential signs), record `created_at` + `min_agent_age` ≤ `lo`; target: Active, the challenged capability in its record; target_credential is the target stake's owner_credential |
| MintHistoryBonus | extends | bonus agent: Active, controller signs (and the oracle signs, as before) |
| DecreaseStake | exit | staker: controller signs; any status |
| WithdrawEndorsement | exit | endorser: controller signs; any status |
| WithdrawChallenge | exit | challenger: controller signs; any status |

Every action in the table needs a validity range with finite bounds and
`hi − lo ≤ 3,600,000` (`shared/identity.window`). Other actions read no
identity record and are unchanged.

## Test map

| Item | Expected | Tests |
|---|---|---|
| Active agent, the controller signs | pass | `create_stake_active_agent`, `increase_stake_active_agent`, `update_capabilities_active_agent`, `mint_endorsement_active_agents`, `increase_endorsement_active_agents`, `mint_challenge_active_agents`, `mint_bonus_active_agent`, `decrease_stake_active_agent`, `withdraw_endorsement_active_agent`, `withdraw_challenge_active_agent` |
| Range of exactly one hour | pass | `create_stake_range_of_one_hour`, `increase_stake_range_of_one_hour`, `update_capabilities_range_of_one_hour`, `mint_endorsement_range_of_one_hour`, `increase_endorsement_range_of_one_hour`, `mint_challenge_range_of_one_hour`, `mint_bonus_range_of_one_hour`, `decrease_stake_range_of_one_hour`, `withdraw_endorsement_range_of_one_hour`, `withdraw_challenge_range_of_one_hour` |
| Time: range over one hour (every action that reads a record) | fail | `create_stake_range_over_one_hour_fails`, `increase_stake_range_over_one_hour_fails`, `update_capabilities_range_over_one_hour_fails`, `mint_endorsement_range_over_one_hour_fails`, `increase_endorsement_range_over_one_hour_fails`, `mint_challenge_range_over_one_hour_fails`, `mint_bonus_range_over_one_hour_fails`, `decrease_stake_range_over_one_hour_fails`, `withdraw_endorsement_range_over_one_hour_fails`, `withdraw_challenge_range_over_one_hour_fails` |
| Time: unbounded range | fail | `create_stake_open_range_fails`, `increase_stake_open_range_fails`, `update_capabilities_open_range_fails`, `mint_endorsement_open_range_fails`, `increase_endorsement_open_range_fails`, `mint_challenge_open_range_fails`, `mint_bonus_open_range_fails`, `decrease_stake_open_range_fails`, `withdraw_endorsement_open_range_fails`, `withdraw_challenge_open_range_fails`; `window_rejects_open_ranges` (shared) |
| Suspended agent (hold in force) | opens/extends fail; exits pass | `create_stake_suspended_agent_fails`, `increase_stake_suspended_agent_fails`, `update_capabilities_suspended_agent_fails`, `mint_endorsement_suspended_endorser_fails`, `increase_endorsement_suspended_endorser_fails`, `mint_challenge_suspended_challenger_fails`, `mint_bonus_suspended_agent_fails`; `decrease_stake_suspended_agent`, `withdraw_endorsement_suspended_agent`, `withdraw_challenge_suspended_agent` |
| Unaccountable agent (no Accountable Party covers the range) | opens/extends fail; exits pass | `create_stake_unaccountable_agent_fails`, `create_stake_accountable_party_ending_in_range_fails`, `increase_stake_unaccountable_agent_fails`, `update_capabilities_unaccountable_agent_fails`, `mint_endorsement_unaccountable_endorser_fails`, `increase_endorsement_unaccountable_endorser_fails`, `mint_challenge_unaccountable_challenger_fails`, `mint_bonus_unaccountable_agent_fails`; `decrease_stake_unaccountable_agent`, `withdraw_endorsement_unaccountable_agent`, `withdraw_challenge_unaccountable_agent` |
| Revoked agent (tombstone) | opens/extends fail; exits pass | `create_stake_revoked_agent_fails`, `increase_stake_revoked_agent_fails`, `update_capabilities_revoked_agent_fails`, `mint_endorsement_revoked_endorser_fails`, `increase_endorsement_revoked_endorser_fails`, `mint_challenge_revoked_challenger_fails`, `mint_bonus_revoked_agent_fails`; `decrease_stake_revoked_agent`, `withdraw_endorsement_revoked_agent`, `withdraw_challenge_revoked_agent` |
| Retired agent (tombstone) | opens/extends fail; exits pass | `create_stake_retired_agent_fails`, `increase_stake_retired_agent_fails`, `update_capabilities_retired_agent_fails`, `mint_endorsement_retired_endorser_fails`, `increase_endorsement_retired_endorser_fails`, `mint_challenge_retired_challenger_fails`, `mint_bonus_retired_agent_fails`; `decrease_stake_retired_agent`, `withdraw_endorsement_retired_agent`, `withdraw_challenge_retired_agent` |
| Target suspended, unaccountable, revoked or retired | fail | `mint_endorsement_suspended_target_fails`, `mint_endorsement_unaccountable_target_fails`, `mint_endorsement_revoked_target_fails`, `mint_endorsement_retired_target_fails`, `increase_endorsement_suspended_target_fails`, `increase_endorsement_revoked_target_fails`, `mint_challenge_suspended_target_fails`, `mint_challenge_unaccountable_target_fails`, `mint_challenge_revoked_target_fails`, `mint_challenge_retired_target_fails` |
| Wrong credential: the controller does not sign | fail | `create_stake_without_controller_signature_fails`, `increase_stake_without_controller_signature_fails`, `update_capabilities_without_controller_signature_fails`, `mint_endorsement_without_controller_signature_fails`, `increase_endorsement_without_controller_signature_fails`, `mint_challenge_without_controller_signature_fails`, `mint_bonus_without_controller_signature_fails`, `decrease_stake_without_controller_signature_fails`, `withdraw_endorsement_without_controller_signature_fails`, `withdraw_challenge_without_controller_signature_fails`; `f3_create_stake_wrong_owner_succeeds` (audit_exploit_tests) |
| Wrong credential: another agent's controller signs | fail | `create_stake_signed_by_other_controller_fails`, `increase_stake_signed_by_other_controller_fails`, `update_capabilities_signed_by_other_controller_fails`, `mint_endorsement_signed_by_target_controller_fails`, `increase_endorsement_signed_by_target_controller_fails`, `mint_challenge_signed_by_target_controller_fails`, `mint_bonus_signed_by_other_controller_fails`, `decrease_stake_signed_by_other_controller_fails`, `withdraw_endorsement_signed_by_target_controller_fails`, `withdraw_challenge_signed_by_target_controller_fails` |
| The position's credential still signs an opening | fail without it | `create_stake_without_owner_signature_fails`, `mint_endorsement_without_endorser_credential_signature_fails`, `mint_challenge_without_challenger_credential_signature_fails`; `auth_create_stake_unsigned_fails`, `auth_script_credential_owner_fails` (audit_exploit_tests) |
| Missing capability (record does not list a staked, endorsed or challenged capability) | fail | `create_stake_capability_not_in_record_fails`, `increase_stake_capability_not_in_record_fails`, `update_capabilities_capability_not_in_record_fails`, `mint_endorsement_capability_not_in_target_record_fails`, `increase_endorsement_capability_not_in_target_record_fails`, `mint_challenge_capability_not_in_target_record_fails` |
| Record at a wrong address: identity script with a stake credential | fail | `create_stake_record_with_stake_credential_fails`, `increase_stake_record_with_stake_credential_fails`, `mint_endorsement_record_with_stake_credential_fails`, `mint_challenge_record_with_stake_credential_fails`, `mint_bonus_record_with_stake_credential_fails`, `decrease_stake_record_with_stake_credential_fails`, `withdraw_endorsement_record_with_stake_credential_fails`, `withdraw_challenge_record_with_stake_credential_fails` |
| Record at a wrong address: another script | fail | `create_stake_record_at_other_script_fails`, `update_capabilities_record_at_other_script_fails`, `mint_endorsement_target_record_at_other_script_fails`, `increase_endorsement_record_at_other_script_fails`, `mint_challenge_record_at_other_script_fails`, `withdraw_challenge_record_at_other_script_fails` |
| Record with a fake token (same name, other policy) | fail | `create_stake_record_with_fake_token_fails`, `increase_stake_record_with_fake_token_fails`, `update_capabilities_record_with_fake_token_fails`, `mint_endorsement_record_with_fake_token_fails`, `increase_endorsement_record_with_fake_token_fails`, `mint_challenge_record_with_fake_token_fails`, `mint_challenge_target_record_with_fake_token_fails`, `mint_bonus_record_with_fake_token_fails`, `decrease_stake_record_with_fake_token_fails`, `withdraw_endorsement_record_with_fake_token_fails`, `withdraw_challenge_record_with_fake_token_fails` |
| No record, or only the record of another agent | fail | `create_stake_record_of_other_agent_fails`, `increase_stake_without_record_fails`, `mint_endorsement_without_target_record_fails`, `mint_challenge_without_target_record_fails`, `mint_bonus_without_record_fails`, `decrease_stake_without_record_fails`, `withdraw_endorsement_without_record_fails`, `withdraw_challenge_without_record_fails` |
| Subagent with an Active parent | pass | `create_stake_subagent_active_parent`, `increase_stake_subagent_active_parent`, `update_capabilities_subagent_active_parent`, `mint_endorsement_subagent_active_parent`, `mint_challenge_subagent_active_parent`, `mint_bonus_subagent_active_parent` |
| Subagent: parent suspended, unaccountable or revoked | fail | `create_stake_subagent_suspended_parent_fails`, `create_stake_subagent_revoked_parent_fails`, `increase_stake_subagent_suspended_parent_fails`, `mint_endorsement_subagent_suspended_parent_fails`, `mint_challenge_subagent_unaccountable_parent_fails` |
| Subagent: stale `parent_epoch` (the parent increased its epoch) | fail | `create_stake_subagent_after_parent_epoch_increase_fails`, `update_capabilities_subagent_after_parent_epoch_increase_fails`, `increase_endorsement_subagent_after_parent_epoch_increase_fails`, `mint_challenge_subagent_after_parent_epoch_increase_fails`, `mint_bonus_subagent_after_parent_epoch_increase_fails` |
| Subagent: parent record missing | fail (`fail` test, at the parent lookup) | `create_stake_subagent_without_parent_record_fails`, `increase_stake_subagent_without_parent_record_fails`, `mint_endorsement_subagent_without_parent_record_fails`, `mint_challenge_subagent_without_parent_record_fails`, `mint_bonus_subagent_without_parent_record_fails` |
| Exit by a subagent without its parent's record | pass | `decrease_stake_subagent_without_parent_record`, `withdraw_endorsement_subagent_without_parent_record`, `withdraw_challenge_subagent_without_parent_record` |
| Exit: the target's record is not needed | pass | `withdraw_endorsement_without_target_record` |
| Challenger age from the record's `created_at`, compared with `lo` | pass at the limit; fail 1 ms before | `mint_challenge_challenger_exactly_old_enough`, `mint_challenge_challenger_too_young_fails` |
| target_credential is the target stake's owner_credential | fail otherwise | `mint_challenge_with_other_target_credential_fails`, `target_credential_script_is_not_the_stake_owner` |
| CapabilityVerified distribution without a history bonus mint | pass; underpaid target fails | `distribute_verified_without_history_bonus`, `distribute_verified_underpaying_target_fails` |
| Module-1 ChallengeDatum mirror with `claim_token` | pass | `resolve_escalation_claimer_wins`, `resolve_escalation_auditor_wins`, `resolve_escalation_inconclusive` (module1_integration_tests) |

Each `fail` test above fails at the parent lookup of `effective`
(`expect Some(parent_record) = find_record(...)`); the run shows the trace.
A scratch copy with each check removed in turn (status gate, controller
signature, capability check, one-hour bound, exit signature, target
credential, challenger age, exact address) makes the matching negative tests
fail.

## Decisions

1. **Exits.** DecreaseStake, WithdrawEndorsement and WithdrawChallenge need
   only the controller's signature (`controller_signed`), for a record of any
   status, also a tombstone. The signature of the stored credential
   (owner_credential, endorser_credential, challenger_credential) is no
   longer needed. The payouts are unchanged: they still go to the stored
   credential, so the controller authorises an exit but cannot redirect its
   funds.
2. **Openings keep the old signature.** The actions that open or extend a
   position need the controller's signature in addition to the existing
   signature of the stored credential. The stored credential receives the
   payouts later, so it stays a verification key that signed once.
3. **Agents involved.** The acting agent (staker, endorser, challenger, bonus
   agent) needs status Active and the controller's signature. The target of
   an endorsement or a challenge needs status Active and the capabilities in
   its record; it does not sign. A suspended target therefore cannot get new
   endorsements or challenges (see Gaps).
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
   SlashStake, SlashEndorsement, ResolveChallenge, ResolveEscalation,
   DefaultJudgment, DistributeOutcome and ExpireChallenge are not agent
   actions and read no identity record.
9. **Config.** The legacy registry fields are removed; `identity_hash` is the
   last field of ReputationConfig.

## Gaps (spec or design questions; not changed here)

- **Recovery and payouts.** After a controller change or a recovery, the
  stored credentials stay. An exit then pays the old credential. Options for
  the module owners: let the controller name the payout address of an exit,
  or let an opening action replace the stored credential.
- **Avoiding challenges.** A target can block new challenges while it is
  suspended or after it removes a capability from its record. It also cannot
  use that capability in the marketplace then.
- **Mint policies (not identity).** The reputation and endorsement mint
  handlers check that one token with the expected prefix is minted, but not
  that no other token of the policy is minted in the same transaction.
- **MintHistoryBonus input rule (not identity).** A bonus mint needs a spend at
  the endorsement validator, so bonuses for Module-1 events need such a spend
  too.
