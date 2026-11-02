# Module-6 identity checks and their tests

Source of truth:

- Part 2 (`docs/IDENTITY_REGISTRY.md` in agents-marketplace): §7 (Module-6 row, Time rule), §3.10 (library), §12.1 (test style).
- Part 1 (`docs/did-vector-method.md`): §5 (record format), §7.3 (effective status).

## Where the check is

`utils.verify_active_agent(config, agent_did, agent_credential, reference_inputs, signatories, validity_range)` holds the identity check. It returns `True` only when all of these are true:

1. `shared/identity.window(validity_range)` is `Some(w)`: both bounds are finite, `lo <= hi` and `hi - lo <= 3_600_000`.
2. `shared/identity.active_record(reference_inputs, config.identity_hash, agent_did, w)` is `Some(record)`: `vector_identity.find_record` finds the record, and `vector_identity.effective` over `[w.lo, w.hi]` is `EffActive`.
3. `shared/identity.declared_credential(record, agent_credential)`: the credential is `VerificationKey(h)`, and `h` is `blake2b_224(controller)` or the hash of a listed key with the transaction purpose.
4. `shared/credential.verify_signer(agent_credential, signatories)`: the key `h` signed.

With the transaction's reference inputs, signatories and validity range, this is `shared/identity.credential_opens`.

| Action (redeemer) | Function | Agent and credential | Place in the result |
|---|---|---|---|
| SubmitProposal (`proposal_spend`) | `proposal_validation.validate_submit_proposal` | `proposer_did`, `proposer_credential` | check 1, `fail @"FAIL:identity_valid"` |
| AmendProposal (`proposal_spend`) | `proposal_validation.validate_amend_proposal` | `proposer_did`, `proposer_credential` | check 9, a conjunct of the result |
| MintCritiqueToken (`critique_spend`) | `critique_validation.validate_submit_critique` | `critic_did`, `critic_credential` | check 1, a conjunct of the result |
| MintEndorsementToken (`endorsement_spend`) | `critique_validation.validate_submit_endorsement` | `endorser_did`, `endorser_credential` | check 1, a conjunct of the result |

Each validator passes `tx.reference_inputs`, `tx.extra_signatories` and `tx.validity_range`. The check is a part of the Bool result of each of the four redeemers, so the optimizer cannot move it into another branch. `GovernanceConfig` has `identity_hash` as its last (11th) field. The legacy fields `registry_policy_id` and `registry_script_hash` are removed. No Module-6 file imports `shared/did_verification`.

## Checks and tests

All tests in this table are in `validators/identity_tests.ak`. Each test builds a full transaction and calls the spend handler of the validator. `X` is each of `submit_proposal`, `amend_proposal`, `submit_critique` and `submit_endorsement`. For example, `X_suspended_agent_fails` stands for four tests.

| Rule (source) | Check in the code | Tests |
|---|---|---|
| The agent's effective status is Active (§7 Module-6 row; Part 1 §7.3) | `active_record` → `effective == EffActive` | Pass: `X_active_agent`. Fail: `X_suspended_agent_fails` (hold in force), `X_unaccountable_agent_fails` (no Accountable Party), `X_revoked_agent_fails`, `X_retired_agent_fails` (tombstones) |
| The status covers the whole range `[lo, hi]` (Time rule; `effective` over `[lo, hi]`) | `active_record(…, w)` with `w.lo`, `w.hi` | `X_accountability_ends_before_upper_bound_fails` (the Accountable Party covers `lo` but ends before `hi`) |
| The declared credential is the controller (§7 Module-6 row) | `declared_credential`: `h == blake2b_224(controller)` | Pass: `X_active_agent`. Fail: `X_other_agent_controller_fails` (the controller of another agent declares and signs) |
| … or a listed key with the transaction purpose (§7 Module-6 row; §3.10 `active_key`) | `declared_credential`: `active_key(record, h, 8)` | Pass: `X_listed_transaction_key`. Fail: `X_unlisted_key_fails`, `X_key_without_transaction_purpose_fails` (Ed25519, authentication and assertion only), `X_p256_key_fails` (P-256, even with the transaction bit) |
| A script credential never passes | `declared_credential` and `verify_signer` return `False` for `Script` | `X_script_credential_fails` |
| The key of the credential signed | `verify_signer(agent_credential, signatories)` | `X_unsigned_credential_fails` |
| `find_record`: the exact identity address, no stake credential (§2, §3.10) | `vector_identity.find_record` | `X_record_with_stake_credential_fails`, `X_record_at_other_script_fails` |
| `find_record`: exactly one token of the policy `identity_hash` (§3.10) | `vector_identity.find_record` | `X_record_with_fake_token_fails` (a token of another policy) |
| `find_record`: the token is named with the agent identifier of the datum (§3.10) | `vector_identity.find_record` | `X_record_of_other_agent_fails` (an authentic record input of another identifier with the same record content) |
| The record is a reference input | `find_record` returns `None` | `X_without_record_fails` |
| Time: `hi - lo <= 3_600_000` (§7 Time rule) | `shared/identity.window` | Pass: `X_range_of_one_hour` (exactly one hour). Fail: `X_range_over_one_hour_fails` (one hour + 1 ms) |
| Time: both bounds are finite (§3.2, §7 Time rule) | `shared/identity.window` | `X_range_without_upper_bound_fails`, `X_range_without_lower_bound_fails`, `submit_proposal_check_rejects_range_without_lower_bound`, `amend_proposal_check_rejects_range_without_lower_bound` |
| Time: ages use `lo` (§7 Time rule) | Not applicable: Module-6 has no age rule | — |
| Subagent with an Active parent and the current parent epoch | `effective` reads the parent record | `X_subagent_of_active_parent` |
| Subagent: a suspended parent makes the subagent Suspended | `effective` | `X_subagent_of_suspended_parent_fails` |
| Subagent: a parent epoch above `parent_epoch` makes the subagent Revoked | `effective` | `X_subagent_of_bumped_parent_epoch_fails` |
| Subagent: the parent record must be a reference input (§3.10: "fails when an ancestor is missing") | `effective`: `expect Some(parent_record)` | `X_subagent_without_parent_fails` (`fail` tests) |
| Exits have no identity check (§7 Module-6 row) | `validate_withdraw_proposal`, `validate_withdraw_critique`, `validate_withdraw_endorsement` read no record | `withdraw_proposal_*`, `withdraw_critique_*`, `withdraw_endorsement_*`: `_suspended_agent`, `_revoked_agent`, `_retired_agent`, `_without_record`, `_range_over_one_hour` (all pass) |

Other tests that use the identity fixtures:

- `lib/governance_suggestion/tests/validator_tests.ak`: the existing tests call the validation functions with the identity record of agent 1 or 2 and a 10-minute range. `submit_proposal_missing_did` and `submit_critique_missing_did` now have no identity record. `submit_critique_self_amendment_allowed` shows that the proposer can still post an Amendment critique.
- `lib/governance_suggestion/tests/record_auth_tests.ak`: the adoption test has the identity record of agent 1 as a reference input. Adoption does not read it.
- `lib/governance_suggestion/tests/test_helpers.ak`: the proposer is agent 1 and the critic is agent 2 of `shared/identity_fixtures` (`proposer_did = agent_id(1)`, `proposer_key_hash = controller_hash(1)`). `identity_ref_input(n)` replaces `registry_ref_input(did)`. `standard_ref_inputs(n)` and `oracle_ref_inputs(n)` take the agent number. `validity_range_from(lo)` gives a 10-minute range. `test_config()` has `identity_hash` of the fixtures.

## Where each `fail` test fails

`aiken check` prints the traces of each `fail` test. They show:

- All `submit_proposal_*_fails` tests except two fail at `FAIL:identity_valid`. The identity check is the first check of `validate_submit_proposal`. `submit_proposal_check_rejects_range_without_lower_bound` also fails at `FAIL:identity_valid`.
- `submit_proposal_range_without_lower_bound_fails` and `amend_proposal_range_without_lower_bound_fails` fail at `expect Some(current_slot) = get_lower_bound_slot(tx)` in `proposal_spend`. This existing statement runs before the redeemer branch. The two `*_check_rejects_range_without_lower_bound` tests show that the identity check alone also rejects that range.
- All four `X_subagent_without_parent_fails` tests fail at `expect Some(parent_record) = find_record(refs, identity_hash, parent)` in `vector_identity.effective`.

The negative tests that use `!handler(...)` get `False` from the handler. A mutation run checked that the identity check gives this `False` (see the section "Mutation runs").

## Mutation runs

Each run uses a scratch copy of the project (not committed) and runs `aiken check -m 'identity_tests.{_}' -m 'validator_tests.{missing_did}'`. That is 138 tests: the 121 tests of `validators/identity_tests.ak`, the 2 `*_missing_did` tests and the 15 tests of `shared/identity_tests`. A test that fails under a mutation detects it.

| Mutation in `utils.verify_active_agent` | Observed result |
|---|---|
| M1: the function returns `True` | 82 tests fail: 80 negative identity tests and the 2 `*_missing_did` tests. 10 negative tests still pass, as expected: the 8 `X_unsigned_credential_fails` and `X_script_credential_fails` tests (the existing `signer_valid` check also rejects them) and the 2 validator-level `*_range_without_lower_bound_fails` tests of `proposal_spend` (the existing `expect Some(current_slot)` rejects them). No positive test and no exit test fails. |
| M2: a fixed window replaces `identity.window(validity_range)` | 12 tests fail, all of them range tests: `X_range_over_one_hour_fails`, `X_range_without_upper_bound_fails`, `submit_critique_range_without_lower_bound_fails`, `submit_endorsement_range_without_lower_bound_fails` and the 2 `*_check_rejects_range_without_lower_bound` tests. |
| M3: the window `[lo, lo]` replaces `[lo, hi]` | 4 tests fail: the 4 `X_accountability_ends_before_upper_bound_fails` tests. |

## Decisions

- **AmendProposal has the identity check.** An amendment is an action of the proposer on its open position, and it changes the position. It uses the `proposer_did` and the `proposer_credential` of the spent proposal. The continuing output keeps both fields (an existing rule).
- **The signature is part of the identity check.** `verify_active_agent` also checks the signature, like `shared/identity.credential_opens`. The existing `signer_valid` checks stay unchanged. They are now redundant for these four actions.
- **No capability check.** The Module-6 row names no capability. Module-3 checks capabilities; Module-6 does not.
- **Subagents can act.** A subagent passes when each ancestor record is a reference input, each ancestor is Active over the range and no parent increased its subagent epoch. The Module-6 row has no subagent rule.
- **The status covers the whole range.** A hold in force at `lo`, or an Accountable Party that ends before `hi`, rejects the action.
- **No identity check on the other redeemers.** WithdrawProposal, WithdrawCritique and WithdrawEndorsement are exits. Adopt, Reject, ExtendReview (Foundation), Expire, ExpireStale (anybody), SpendActivity, IncorporateCritique, RewardCritique, BurnCritiqueToken, BurnEndorsementToken and the two mint policies settle positions or follow another action. The agent that owns the position does not act in them. So stakes and rewards go back whatever the agent's status, and a suspended, revoked or retired agent can always exit. This is the safest choice for funds.
- **No time rule on the other redeemers.** They read no identity record. `proposal_spend` still needs a finite lower bound for every redeemer (an existing rule).
- **P-256 keys never count.** A P-256 key with the transaction bit is rejected. The registry does not accept such a key, and the module does not depend on that rule.
- **Trace name.** `FAIL:did_valid` is now `FAIL:identity_valid`.

## Gaps (reported, not changed)

1. **The creation actions check the spent datum, not the new position.** SubmitProposal, MintCritiqueToken and MintEndorsementToken validate the datum of the spent UTxO (`datum_opt`). SubmitProposal checks only that some output at the proposal validator holds at least the stake; it does not compare that output's datum. MintCritiqueToken and MintEndorsementToken check no output, and `critique_mint` needs only some input at the critique validator. So the identity check covers the agent of the spent datum. The datum of the created proposal, critique or endorsement can name another agent or credential. A fix needs exactly one created output whose datum equals the checked datum, at the validator address, with the minted token.
2. **"Self" means the same identifier.** The self-critique and self-endorsement checks compare identifiers only, and they ignore `proposal_ref` (the W1-D note). One controller can hold several identifiers, and a subagent has its own identifier. So a proposer can critique or endorse its own proposal through another record. The spec has no rule for this.
3. **Position times are not the transaction time.** `submitted_at` (proposal, critique) and `created_at` (endorsement) are not compared with `lo`. Expiry uses `submitted_at + review_window`. Module-3 binds `created_at` to `lo`; Module-6 does not.
4. **The emergency reputation gate ignores the proposer.** `emergency.verify_proposer_reputation` accepts any Module-3 stake output with at least 100 AP3X and does not use `proposer_did`. It is skipped today because `reputation_validator_hash` is zero.
5. **The Module-6 row does not name amendments.** This change gates them (see Decisions).
6. **Amendment outputs.** The continuing proposal output of AmendProposal has no address or value check (the W1-D note). The identity check does not change this.
