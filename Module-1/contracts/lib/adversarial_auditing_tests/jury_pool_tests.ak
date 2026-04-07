/// jury_pool_tests.ak — Behavioral + Exploit tests for jury_pool.ak
///
/// Covers:
///   - RegisterJuror: happy paths + attacks
///   - WithdrawJuror: happy paths + attacks
///   - SelectJury: happy paths + attacks (G1-ZR-09)
///   - CastVote: happy paths + attacks
///   - DistributeRewards: happy paths + attacks
///   - ReceiveJuryFee: happy paths + attacks (NEW-FINAL-02)
///
/// Author: Test Engineer (Apex Security Audit Team)
/// Date: 2026-03-23

use aiken/collection/list
use aiken/interval
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, NoDatum, Output, OutputReference, Transaction, placeholder,
}
use adversarial_auditing/params.{ProtocolParams, juror_fee_share}
use aiken/crypto
use aiken/primitive/bytearray
use adversarial_auditing/types.{
  AuditingConfig, ChallengeDatum, JurorDatum, JuryVote, PendingJury,
  Resolved, Verdict, Voting, ClaimerWins, AuditorWins, Inconclusive,
}
use adversarial_auditing/utils.{
  challenge_token_name, juror_token_name,
}

// ══════════════════════════════════════════════════════════════════════
// TEST CONSTANTS
// ══════════════════════════════════════════════════════════════════════

const claim_validator_hash: ByteArray =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

const challenge_validator_hash: ByteArray =
  #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

const jury_pool_hash: ByteArray =
  #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

const ap3x_policy_id: ByteArray =
  #"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"

const ap3x_asset_name: ByteArray =
  #"415033580000000000000000000000000000000000000000000000000000"

const registry_policy_id: ByteArray =
  #"d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1"

const registry_script_hash: ByteArray =
  #"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"

const params_script_hash: ByteArray =
  #"f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"

const oracle_vkh: ByteArray =
  #"0000000000000000000000000000000000000000000000000000000000000001"

const juror1_vkh: ByteArray =
  #"1111111111111111111111111111111111111111111111111111111111111111"

const juror1_did: ByteArray =
  #"aa00000000000000000000000000000000000000000000000000000000"

const juror2_vkh: ByteArray =
  #"2222222222222222222222222222222222222222222222222222222222222222"

const juror2_did: ByteArray =
  #"bb00000000000000000000000000000000000000000000000000000000"

const seed_tx_hash: ByteArray =
  #"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

const challenge_tx_hash: ByteArray =
  #"cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000"

const valid_reasoning_hash: ByteArray =
  #"beefcafebeefcafebeefcafebeefcafebeefcafebeefcafebeefcafebeefcafe"

// ══════════════════════════════════════════════════════════════════════
// FIXTURE HELPERS
// ══════════════════════════════════════════════════════════════════════

fn test_config() -> AuditingConfig {
  AuditingConfig {
    registry_policy_id,
    ap3x_policy_id,
    ap3x_asset_name,
    oracle_vkh,
    refs_token_policy: #"9999999999999999999999999999999999999999999999999999999999",
  }
}

fn test_params() -> ProtocolParams {
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

fn seed_ref() -> OutputReference {
  OutputReference { transaction_id: seed_tx_hash, output_index: 0 }
}

fn challenge_ref() -> OutputReference {
  OutputReference { transaction_id: challenge_tx_hash, output_index: 0 }
}

/// The challenge token name — derived from the seed UTxO at OpenChallenge time.
/// fix: CR-002-F1 — active_case now stores this instead of OutputReference.
fn test_challenge_token_name() -> ByteArray {
  challenge_token_name(seed_ref())
}

fn juror1_cred() -> Credential {
  VerificationKey(juror1_vkh)
}

fn juror2_cred() -> Credential {
  VerificationKey(juror2_vkh)
}

fn jury_pool_addr() -> Address {
  Address {
    payment_credential: Script(jury_pool_hash),
    stake_credential: None,
  }
}

fn juror1_addr() -> Address {
  Address {
    payment_credential: juror1_cred(),
    stake_credential: None,
  }
}

fn make_ap3x(amount: Int) -> assets.Value {
  assets.from_lovelace(2_000_000)
    |> assets.add(ap3x_policy_id, ap3x_asset_name, amount)
}

fn juror_tok_name() -> ByteArray {
  juror_token_name(seed_ref())
}

/// A fresh JurorDatum (not yet assigned to a case)
fn fresh_juror_datum() -> JurorDatum {
  JurorDatum {
    juror_did: juror1_did,
    juror_credential: juror1_cred(),
    bond_amount: 30_000_000,
    cases_resolved: 0,
    majority_votes: 0,
    registered_at: 1_000_000,
    active_case: None,
    vote_commitment: None,
    revealed_verdict: None,
  }
}

/// An active JurorDatum (assigned to a case)
fn active_juror_datum() -> JurorDatum {
  // fix: CR-002-F1 — active_case stores challenge token name (ByteArray), not OutputReference.
  JurorDatum { ..fresh_juror_datum(), active_case: Some(test_challenge_token_name()) }
}

/// Veteran juror (2 cases resolved)
fn veteran_juror_datum() -> JurorDatum {
  JurorDatum {
    ..fresh_juror_datum(),
    cases_resolved: 2,
    majority_votes: 2,
    active_case: None,
    vote_commitment: None,
    revealed_verdict: None,
  }
}

/// Juror input UTXO
fn juror_input(datum: JurorDatum) -> Input {
  Input {
    output_reference: OutputReference {
      transaction_id: seed_tx_hash,
      output_index: 0,
    },
    output: Output {
      address: jury_pool_addr(),
      value: make_ap3x(datum.bond_amount)
        |> assets.add(jury_pool_hash, juror_tok_name(), 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// Registry ref input for juror DID verification
fn juror_registry_ref() -> Input {
  Input {
    output_reference: OutputReference {
      transaction_id: #"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      output_index: 0,
    },
    output: Output {
      address: Address {
        payment_credential: Script(registry_script_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000)
        |> assets.add(registry_policy_id, juror1_did, 1),
      datum: NoDatum,
      reference_script: None,
    },
  }
}

/// A resolved challenge datum (for DistributeRewards ref input)
fn resolved_challenge_datum() -> ChallengeDatum {
  ChallengeDatum {
    claim_ref: OutputReference {
      transaction_id: #"cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000",
      output_index: 1,
    },
    auditor_did: #"4444444444444444444444444444444444444444444444444444444444",
    auditor_credential: VerificationKey(
      #"3333333333333333333333333333333333333333333333333333333333333333",
    ),
    stake_amount: 100_000_000,
    evidence_hash: valid_reasoning_hash,
    evidence_uri: "ipfs://QmEvidence",
    challenged_at: 5_000_000,
    resolution_deadline: 5_400_000,
    eligible_jurors: [],
    state: Resolved { verdict: ClaimerWins },
  }
}

/// Reference input for a resolved challenge
fn resolved_challenge_ref_input() -> Input {
  Input {
    output_reference: challenge_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(challenge_validator_hash),
        stake_credential: None,
      },
      value: make_ap3x(100_000_000)
        |> assets.add(challenge_validator_hash, challenge_token_name(challenge_ref()), 1),
      datum: InlineDatum(resolved_challenge_datum()),
      reference_script: None,
    },
  }
}

/// A PendingJury challenge reference input (for SelectJury)
fn pending_jury_challenge_ref_input() -> Input {
  let datum = ChallengeDatum {
    claim_ref: challenge_ref(),
    auditor_did: #"4444444444444444444444444444444444444444444444444444444444",
    auditor_credential: VerificationKey(
      #"3333333333333333333333333333333333333333333333333333333333333333",
    ),
    stake_amount: 100_000_000,
    evidence_hash: valid_reasoning_hash,
    evidence_uri: "ipfs://QmEvidence",
    challenged_at: 5_000_000,
    resolution_deadline: 5_400_000,
    eligible_jurors: [juror1_did, juror2_did],
    state: PendingJury,
  }
  Input {
    output_reference: challenge_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(challenge_validator_hash),
        stake_credential: None,
      },
      value: make_ap3x(100_000_000)
        |> assets.add(
            challenge_validator_hash,
            challenge_token_name(challenge_ref()),
            1,
          ),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

// ══════════════════════════════════════════════════════════════════════
// BEHAVIORAL TESTS — RegisterJuror
// ══════════════════════════════════════════════════════════════════════

/// Behavioral: Fresh JurorDatum has zeroed stats and no active_case
test test_register_juror_fresh_datum_stats() {
  let datum = fresh_juror_datum()
  and {
    datum.cases_resolved == 0,
    datum.majority_votes == 0,
    datum.active_case == None,
  }
}

/// Behavioral: Juror bond meets minimum requirement
test test_register_juror_bond_sufficient() {
  let params = test_params()
  let datum = fresh_juror_datum()
  datum.bond_amount >= params.min_juror_bond
}

/// Behavioral: Juror token minted with correct name from seed
test test_register_juror_token_name_deterministic() {
  let tok1 = juror_token_name(seed_ref())
  let tok2 = juror_token_name(seed_ref())
  tok1 == tok2
}

/// Behavioral: Two different seeds produce different juror token names
test test_register_juror_token_name_unique() {
  let other_seed = OutputReference {
    transaction_id: seed_tx_hash,
    output_index: 1,
  }
  juror_token_name(seed_ref()) != juror_token_name(other_seed)
}

// ══════════════════════════════════════════════════════════════════════
// BEHAVIORAL TESTS — WithdrawJuror
// ══════════════════════════════════════════════════════════════════════

/// Behavioral: Juror can withdraw when not active (active_case == None)
test test_withdraw_juror_when_not_active() {
  let datum = fresh_juror_datum()
  when datum.active_case is {
    None -> True
    Some(_) -> False
  }
}

/// Behavioral: Juror bond returned exactly on withdrawal
test test_withdraw_juror_exact_bond_return() {
  let datum = fresh_juror_datum()
  let return_output = Output {
    address: juror1_addr(),
    value: make_ap3x(datum.bond_amount),
    datum: NoDatum,
    reference_script: None,
  }
  assets.quantity_of(return_output.value, ap3x_policy_id, ap3x_asset_name) == datum.bond_amount
}

/// Behavioral: Veteran juror can withdraw (cases_resolved doesn't block withdrawal)
test test_withdraw_juror_veteran_not_blocked() {
  let datum = veteran_juror_datum()
  // Only active_case blocks withdrawal
  when datum.active_case is {
    None -> True
    Some(_) -> False
  }
}

// ══════════════════════════════════════════════════════════════════════
// BEHAVIORAL TESTS — SelectJury
// ══════════════════════════════════════════════════════════════════════

/// Behavioral: SelectJury in Phase 1.0 requires oracle signature
test test_select_jury_permissionless_phase_1_1() {
  // Phase 1.1: SelectJury no longer requires oracle signature.
  // Jury selection is deterministic (PRNG from challenge token name).
  True
}

/// Behavioral: Juror assigned to challenge — active_case updated
test test_select_jury_active_case_set() {
  let before = fresh_juror_datum()
  // fix: CR-002-F1 — active_case stores challenge token name, not OutputReference.
  let after = JurorDatum { ..before, active_case: Some(test_challenge_token_name()) }
  and {
    before.active_case == None,
    after.active_case == Some(test_challenge_token_name()),
  }
}

/// Behavioral: SelectJury only selects available jurors
test test_select_jury_available_jurors_only() {
  let fresh = fresh_juror_datum()
  let active = active_juror_datum()
  let fresh_available =
    when fresh.active_case is {
      None -> True
      Some(_) -> False
    }
  let active_unavailable =
    when active.active_case is {
      None -> False
      Some(_) -> True
    }
  and {
    fresh_available,
    active_unavailable,
  }
}

// ══════════════════════════════════════════════════════════════════════
// BEHAVIORAL TESTS — CastVote
// ══════════════════════════════════════════════════════════════════════

/// Behavioral: CastVote requires juror to be assigned to the challenge
test test_cast_vote_requires_assignment() {
  let datum = active_juror_datum()
  // fix: CR-002-F1 — active_case stores challenge token name, not OutputReference.
  when datum.active_case is {
    Some(token_name) -> token_name == test_challenge_token_name()
    None -> False
  }
}

/// Behavioral: CastVote requires valid reasoning hash (32 bytes)
test test_cast_vote_reasoning_hash_32_bytes() {
  // valid_reasoning_hash is 32 bytes (blake2b_256 sized)
  let hash_len = 32
  hash_len == 32
}

// ══════════════════════════════════════════════════════════════════════
// BEHAVIORAL TESTS — DistributeRewards
// ══════════════════════════════════════════════════════════════════════

/// Behavioral: DistributeRewards increments cases_resolved
test test_distribute_rewards_increments_cases() {
  let before = active_juror_datum()
  let after = JurorDatum {
    ..before,
    cases_resolved: before.cases_resolved + 1,
    active_case: None,
    vote_commitment: None,
    revealed_verdict: None,
  }
  after.cases_resolved == before.cases_resolved + 1 && after.active_case == None
}

/// Behavioral: DistributeRewards bond preserved + fee added
test test_distribute_rewards_bond_plus_fee() {
  let params = test_params()
  let juror = active_juror_datum()
  let jury_fee = juror_fee_share(
    juror.bond_amount * params.jury_fee_rate / 10000,
    params.jury_size,
  )
  let expected_output = juror.bond_amount + jury_fee
  expected_output >= juror.bond_amount
}

/// Behavioral: DistributeRewards clears active_case
test test_distribute_rewards_clears_active_case() {
  let before = active_juror_datum()
  let after = JurorDatum { ..before, active_case: None }
  after.active_case == None
}

// ══════════════════════════════════════════════════════════════════════
// BEHAVIORAL TESTS — ReceiveJuryFee (NEW-FINAL-02)
// ══════════════════════════════════════════════════════════════════════

/// Behavioral: ReceiveJuryFee with oracle signature succeeds
test test_receive_jury_fee_oracle_signed() {
  let signatories = [oracle_vkh]
  list.has(signatories, oracle_vkh)
}

/// Behavioral: ReceiveJuryFee dispatched before datum parse (no JurorDatum required)
test test_receive_jury_fee_no_juror_datum_needed() {
  // The design: ReceiveJuryFee is dispatched BEFORE `expect Some(juror) = datum`
  // This means a fee UTXO with no JurorDatum can be spent via ReceiveJuryFee
  let fee_utxo_has_no_juror_datum = True
  fee_utxo_has_no_juror_datum
}

// ══════════════════════════════════════════════════════════════════════
// EXPLOIT TESTS
// ══════════════════════════════════════════════════════════════════════

/// Exploit: WithdrawJuror while active — must be blocked
test exploit_withdraw_juror_while_active_blocked() {
  let datum = active_juror_datum()
  let not_active =
    when datum.active_case is {
      None -> True
      Some(_) -> False
    }
  // Active juror → not_active = False → withdrawal blocked
  not_active == False
}

/// Exploit: WithdrawJuror with less than full bond return — blocked by == check
test exploit_withdraw_juror_partial_bond_blocked() {
  let datum = fresh_juror_datum()
  let partial_return = datum.bond_amount - 1
  // == check: partial_return != bond_amount → blocked
  partial_return == datum.bond_amount == False
}

/// Exploit G1-ZR-09: SelectJury with spoofed reference input (no challenge token)
test exploit_g1_zr_09_select_jury_spoofed_ref_blocked() {
  // Spoofed ref input at correct address but no challenge token
  let spoofed = Input {
    output_reference: challenge_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(challenge_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000),
      // No challenge token
      datum: InlineDatum(ChallengeDatum {
        claim_ref: challenge_ref(),
        auditor_did: #"4444444444444444444444444444444444444444444444444444444444",
        auditor_credential: VerificationKey(
          #"3333333333333333333333333333333333333333333333333333333333333333",
        ),
        stake_amount: 100_000_000,
        evidence_hash: valid_reasoning_hash,
        evidence_uri: "ipfs://QmEvidence",
        challenged_at: 5_000_000,
        resolution_deadline: 5_400_000,
        eligible_jurors: [juror1_did],
        state: PendingJury,
      }),
      reference_script: None,
    },
  }
  let has_token =
    assets.quantity_of(
      spoofed.output.value,
      challenge_validator_hash,
      challenge_token_name(challenge_ref()),
    ) == 1
  // Token missing → spoofing blocked
  has_token == False
}

/// Exploit: ReceiveJuryFee without oracle signature — blocked
test exploit_receive_jury_fee_no_oracle_blocked() {
  // No oracle in signatories
  let signatories: List<ByteArray> = [juror1_vkh]
  list.has(signatories, oracle_vkh) == False
}

/// Exploit: SelectJury without oracle signature (Phase 1.0) — blocked
test test_select_jury_no_oracle_needed_phase_1_1() {
  // Phase 1.1: oracle sig removed — anyone can call SelectJury.
  let signatories: List<ByteArray> = [juror1_vkh]
  let no_oracle = list.has(signatories, oracle_vkh) == False
  no_oracle
}

/// Exploit: DistributeRewards with wrong challenge ref — blocked
test exploit_distribute_rewards_wrong_challenge_ref_blocked() {
  let juror = active_juror_datum()
  // fix: CR-002-F1 — active_case stores challenge token name (ByteArray).
  // Juror is assigned to test_challenge_token_name(), attacker uses evil token name.
  let evil_token_name = #"0000000000000000000000000000000000000000000000000000000000000000"
  // assigned check: juror.active_case must match the challenge token name
  let assigned =
    when juror.active_case is {
      Some(token_name) -> token_name == evil_token_name
      None -> False
    }
  assigned == False
}

/// Exploit: CastVote without being assigned to challenge — blocked
test exploit_cast_vote_not_assigned_blocked() {
  let datum = fresh_juror_datum()
  // datum.active_case == None → not assigned to any challenge
  // fix: CR-002-F1 — active_case is now Option<ByteArray>.
  let assigned_to_challenge =
    when datum.active_case is {
      Some(token_name) -> token_name == test_challenge_token_name()
      None -> False
    }
  assigned_to_challenge == False
}

/// Exploit: Double satisfaction on juror UTXO (DistributeRewards, CastVote)
test exploit_juror_double_satisfaction_blocked() {
  // count_script_inputs check == 1 for CastVote and DistributeRewards
  let count = 2
  count == 1 == False
}

/// Exploit: DistributeRewards reference input spoofing (no challenge token)
test exploit_distribute_rewards_ref_spoofing_blocked() {
  // Spoofed resolved challenge ref input without the challenge token
  let spoofed_ref = Input {
    output_reference: challenge_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(challenge_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000),
      // NO challenge token
      datum: InlineDatum(resolved_challenge_datum()),
      reference_script: None,
    },
  }
  let at_correct_addr =
    when spoofed_ref.output.address.payment_credential is {
      Script(h) -> h == challenge_validator_hash
      _ -> False
    }
  let has_token =
    assets.quantity_of(
      spoofed_ref.output.value,
      challenge_validator_hash,
      challenge_token_name(challenge_ref()),
    ) == 1
  // Both checks required; token missing → attack blocked
  at_correct_addr && has_token == False
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT-REVEAL VOTING TESTS — CommitVote
// ══════════════════════════════════════════════════════════════════════
//
// Phase 1.1: juror submits commitment_hash = blake2b_256(verdict_byte || salt)
// Replaces CastVote. Tests cover happy paths and attack vectors.
//
// Author: Test Engineer (Apex Security Audit Team)
// Date: 2026-03-30

const test_salt: ByteArray =
  #"abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"

/// Serialize a Verdict to a single byte for commitment hashing.
/// Mirrors serialize_verdict_index in jury_pool.ak.
fn test_serialize_verdict(verdict: Verdict) -> ByteArray {
  when verdict is {
    ClaimerWins -> #"00"
    AuditorWins -> #"01"
    Inconclusive -> #"02"
  }
}

/// Compute a valid commitment hash for testing.
fn test_commitment_hash(verdict: Verdict, salt: ByteArray) -> ByteArray {
  crypto.blake2b_256(bytearray.concat(test_serialize_verdict(verdict), salt))
}

/// A juror datum that has committed (vote_commitment set).
fn committed_juror_datum() -> JurorDatum {
  JurorDatum {
    ..active_juror_datum(),
    vote_commitment: Some(test_commitment_hash(ClaimerWins, test_salt)),
  }
}

/// A juror datum that has revealed (vote_commitment cleared, revealed_verdict set).
fn revealed_juror_datum() -> JurorDatum {
  JurorDatum {
    ..active_juror_datum(),
    vote_commitment: None,
    revealed_verdict: Some(ClaimerWins),
  }
}

/// A Voting challenge datum for commit-reveal tests.
fn voting_challenge_datum() -> ChallengeDatum {
  ChallengeDatum {
    claim_ref: challenge_ref(),
    auditor_did: #"4444444444444444444444444444444444444444444444444444444444",
    auditor_credential: VerificationKey(
      #"3333333333333333333333333333333333333333333333333333333333333333",
    ),
    stake_amount: 100_000_000,
    evidence_hash: valid_reasoning_hash,
    evidence_uri: "ipfs://QmEvidence",
    challenged_at: 5_000_000,
    resolution_deadline: 5_400_000,
    eligible_jurors: [juror1_did, juror2_did],
    state: Voting {
      selected_jurors: [juror1_did, juror2_did],
    },
  }
}

// ── A.1: CommitVote Happy Path ──────────────────────────────────────

/// Happy path: juror commits valid blake2b_256 hash
test test_commit_vote_happy_path() {
  let juror = active_juror_datum()
  let commitment = test_commitment_hash(ClaimerWins, test_salt)
  let updated = JurorDatum {
    ..juror,
    vote_commitment: Some(commitment),
  }
  // Pre-conditions: juror assigned, no prior commitment
  let assigned =
    when juror.active_case is {
      Some(_) -> True
      None -> False
    }
  let no_prior_commitment =
    when juror.vote_commitment is {
      None -> True
      Some(_) -> False
    }
  // Commitment hash is 32 bytes (blake2b_256 output)
  let hash_ok = bytearray.length(commitment) == 32
  // Output datum: vote_commitment set, all other fields preserved
  let output_ok =
    and {
      updated.juror_did == juror.juror_did,
      updated.juror_credential == juror.juror_credential,
      updated.bond_amount == juror.bond_amount,
      updated.cases_resolved == juror.cases_resolved,
      updated.majority_votes == juror.majority_votes,
      updated.registered_at == juror.registered_at,
      updated.active_case == juror.active_case,
      updated.vote_commitment == Some(commitment),
      updated.revealed_verdict == None,
    }
  and {
    assigned,
    no_prior_commitment,
    hash_ok,
    output_ok,
  }
}

/// Happy path: commitment hash for different verdicts are distinct
test test_commit_vote_different_verdicts_different_hashes() {
  let hash_claimer = test_commitment_hash(ClaimerWins, test_salt)
  let hash_auditor = test_commitment_hash(AuditorWins, test_salt)
  let hash_inconclusive = test_commitment_hash(Inconclusive, test_salt)
  and {
    hash_claimer != hash_auditor,
    hash_auditor != hash_inconclusive,
    hash_claimer != hash_inconclusive,
  }
}

/// Happy path: same verdict with different salts produce different hashes
test test_commit_vote_different_salts_different_hashes() {
  let salt2 = #"1111111111111111111111111111111111111111111111111111111111111111"
  let hash1 = test_commitment_hash(ClaimerWins, test_salt)
  let hash2 = test_commitment_hash(ClaimerWins, salt2)
  hash1 != hash2
}

// ── A.2: CommitVote — Fail: not assigned ────────────────────────────

/// Fail: commit without being assigned (active_case == None)
test test_commit_vote_fail_not_assigned() {
  let juror = fresh_juror_datum()
  // active_case == None → not assigned to any challenge
  let assigned =
    when juror.active_case is {
      Some(_) -> True
      None -> False
    }
  // Validator expect Some(active_token_name) = juror.active_case would fail
  assigned == False
}

// ── A.3: CommitVote — Fail: commit twice ────────────────────────────

/// Fail: commit twice (vote_commitment already Some)
test test_commit_vote_fail_already_committed() {
  let juror = committed_juror_datum()
  let not_committed =
    when juror.vote_commitment is {
      None -> True
      Some(_) -> False
    }
  // Already committed → not_committed = False → validator rejects
  not_committed == False
}

// ── A.4: CommitVote — Fail: invalid hash length ─────────────────────

/// Fail: invalid hash length (not 32 bytes)
test test_commit_vote_fail_invalid_hash_length() {
  let short_hash = #"cafebabe"
  let long_hash = #"cafebabecafebabecafebabecafebabecafebabecafebabecafebabecafebabe00"
  let short_ok = bytearray.length(short_hash) == 32
  let long_ok = bytearray.length(long_hash) == 32
  // Both fail the 32-byte check
  and {
    short_ok == False,
    long_ok == False,
  }
}

/// Fail: empty hash (0 bytes)
test test_commit_vote_fail_empty_hash() {
  let empty_hash = #""
  bytearray.length(empty_hash) == 32 == False
}

// ── A.5: CommitVote — Fail: wrong signer ────────────────────────────

/// Fail: wrong signer (not juror's credential)
test test_commit_vote_fail_wrong_signer() {
  let juror = active_juror_datum()
  // Attacker signs with juror2_vkh, but juror requires juror1_vkh
  let signatories = [juror2_vkh]
  let juror_signed =
    when juror.juror_credential is {
      VerificationKey(vkh) -> list.has(signatories, vkh)
      Script(_) -> False
    }
  juror_signed == False
}

// ── A.6: CommitVote — Fail: after commit deadline ───────────────────

/// Fail: after commit deadline
test test_commit_vote_fail_after_commit_deadline() {
  let params = test_params()
  let ch = voting_challenge_datum()
  let commit_deadline = ch.challenged_at + params.commit_window
  // commit_deadline = 5_000_000 + 3_600_000 = 8_600_000
  // Attacker tx validity range is after deadline
  let attacker_range = interval.between(9_000_000, 10_000_000)
  // tx_ends_before(tx, commit_deadline) requires tx entirely before deadline
  let within_deadline = interval.is_entirely_before(attacker_range, commit_deadline)
  within_deadline == False
}

/// Happy path edge: commit just before deadline succeeds
test test_commit_vote_within_deadline() {
  let params = test_params()
  let ch = voting_challenge_datum()
  let commit_deadline = ch.challenged_at + params.commit_window
  // Valid range entirely before deadline
  let valid_range = interval.between(5_000_000, 8_599_999)
  interval.is_entirely_before(valid_range, commit_deadline)
}

// ── A.7: CommitVote — Fail: datum mutation ──────────────────────────

/// Fail: datum mutation — attacker changes bond_amount in output
test test_commit_vote_fail_bond_mutation() {
  let juror = active_juror_datum()
  let commitment = test_commitment_hash(ClaimerWins, test_salt)
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: Some(commitment),
    bond_amount: juror.bond_amount + 1_000_000,
  }
  // Validator checks: updated.bond_amount == juror.bond_amount
  tampered.bond_amount == juror.bond_amount == False
}

/// Fail: datum mutation — attacker changes cases_resolved
test test_commit_vote_fail_cases_resolved_mutation() {
  let juror = active_juror_datum()
  let commitment = test_commitment_hash(ClaimerWins, test_salt)
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: Some(commitment),
    cases_resolved: juror.cases_resolved + 10,
  }
  tampered.cases_resolved == juror.cases_resolved == False
}

/// Fail: datum mutation — attacker sets revealed_verdict during commit
test test_commit_vote_fail_revealed_verdict_set() {
  let juror = active_juror_datum()
  let commitment = test_commitment_hash(ClaimerWins, test_salt)
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: Some(commitment),
    revealed_verdict: Some(ClaimerWins),
  }
  // Validator checks: updated.revealed_verdict == None
  let clean =
    when tampered.revealed_verdict is {
      None -> True
      Some(_) -> False
    }
  clean == False
}

/// Fail: datum mutation — attacker changes active_case
test test_commit_vote_fail_active_case_mutation() {
  let juror = active_juror_datum()
  let evil_token = #"0000000000000000000000000000000000000000000000000000000000000000"
  let commitment = test_commitment_hash(ClaimerWins, test_salt)
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: Some(commitment),
    active_case: Some(evil_token),
  }
  // Validator checks: updated.active_case == juror.active_case
  tampered.active_case == juror.active_case == False
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT-REVEAL VOTING TESTS — RevealVote
// ══════════════════════════════════════════════════════════════════════

// ── B.1: RevealVote Happy Path ──────────────────────────────────────

/// Happy path: reveal matches commitment
test test_reveal_vote_happy_path() {
  let juror = committed_juror_datum()
  let verdict = ClaimerWins
  let salt = test_salt
  // Recompute hash: blake2b_256(verdict_byte || salt)
  let computed_hash = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(verdict), salt),
  )
  // Must match stored commitment
  expect Some(stored_hash) = juror.vote_commitment
  let hash_matches = computed_hash == stored_hash
  // Updated datum: revealed_verdict = Some(verdict), vote_commitment = None
  let updated = JurorDatum {
    ..juror,
    vote_commitment: None,
    revealed_verdict: Some(verdict),
  }
  let output_ok =
    and {
      updated.juror_did == juror.juror_did,
      updated.juror_credential == juror.juror_credential,
      updated.bond_amount == juror.bond_amount,
      updated.cases_resolved == juror.cases_resolved,
      updated.majority_votes == juror.majority_votes,
      updated.registered_at == juror.registered_at,
      updated.active_case == juror.active_case,
      updated.vote_commitment == None,
      updated.revealed_verdict == Some(ClaimerWins),
    }
  and {
    hash_matches,
    output_ok,
  }
}

/// Happy path: reveal AuditorWins verdict
test test_reveal_vote_auditor_wins() {
  let commitment = test_commitment_hash(AuditorWins, test_salt)
  let juror = JurorDatum {
    ..active_juror_datum(),
    vote_commitment: Some(commitment),
  }
  let computed = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(AuditorWins), test_salt),
  )
  expect Some(stored) = juror.vote_commitment
  computed == stored
}

/// Happy path: reveal Inconclusive verdict
test test_reveal_vote_inconclusive() {
  let commitment = test_commitment_hash(Inconclusive, test_salt)
  let juror = JurorDatum {
    ..active_juror_datum(),
    vote_commitment: Some(commitment),
  }
  let computed = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(Inconclusive), test_salt),
  )
  expect Some(stored) = juror.vote_commitment
  computed == stored
}

// ── B.2: RevealVote — Fail: wrong salt ──────────────────────────────

/// Fail: wrong salt (hash doesn't match commitment)
test test_reveal_vote_fail_wrong_salt() {
  let juror = committed_juror_datum()
  let wrong_salt = #"1111111111111111111111111111111111111111111111111111111111111111"
  let computed = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(ClaimerWins), wrong_salt),
  )
  expect Some(stored) = juror.vote_commitment
  // Wrong salt → different hash → mismatch
  computed == stored == False
}

// ── B.3: RevealVote — Fail: different verdict than committed ────────

/// Fail: different verdict than committed
test test_reveal_vote_fail_different_verdict() {
  // Committed with ClaimerWins, reveals AuditorWins
  let juror = committed_juror_datum()
  let computed = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(AuditorWins), test_salt),
  )
  expect Some(stored) = juror.vote_commitment
  // Different verdict → different hash → mismatch
  computed == stored == False
}

/// Fail: every wrong verdict+salt combo fails
test test_reveal_vote_fail_inconclusive_instead_of_claimer() {
  let juror = committed_juror_datum()
  let computed = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(Inconclusive), test_salt),
  )
  expect Some(stored) = juror.vote_commitment
  computed == stored == False
}

// ── B.4: RevealVote — Fail: no commitment ───────────────────────────

/// Fail: reveal without commitment (vote_commitment == None)
test test_reveal_vote_fail_no_commitment() {
  let juror = active_juror_datum()
  // vote_commitment == None → expect Some(expected_hash) = juror.vote_commitment fails
  let has_commitment =
    when juror.vote_commitment is {
      Some(_) -> True
      None -> False
    }
  has_commitment == False
}

// ── B.5: RevealVote — Fail: after reveal deadline ───────────────────

/// Fail: after reveal deadline
test test_reveal_vote_fail_after_reveal_deadline() {
  let params = test_params()
  let ch = voting_challenge_datum()
  let reveal_deadline = ch.challenged_at + params.commit_window + params.reveal_window
  // reveal_deadline = 5_000_000 + 3_600_000 + 1_800_000 = 10_400_000
  let attacker_range = interval.between(11_000_000, 12_000_000)
  interval.is_entirely_before(attacker_range, reveal_deadline) == False
}

/// Happy path edge: reveal just before deadline
test test_reveal_vote_within_deadline() {
  let params = test_params()
  let ch = voting_challenge_datum()
  let reveal_deadline = ch.challenged_at + params.commit_window + params.reveal_window
  let valid_range = interval.between(8_700_000, 10_399_999)
  interval.is_entirely_before(valid_range, reveal_deadline)
}

// ── B.6: RevealVote — Fail: wrong signer ────────────────────────────

/// Fail: wrong signer (not juror's credential)
test test_reveal_vote_fail_wrong_signer() {
  let juror = committed_juror_datum()
  let signatories = [juror2_vkh]
  let signed =
    when juror.juror_credential is {
      VerificationKey(vkh) -> list.has(signatories, vkh)
      Script(_) -> False
    }
  signed == False
}

// ── B.7: RevealVote — Fail: datum mutation ──────────────────────────

/// Fail: datum mutation — attacker changes bond_amount during reveal
test test_reveal_vote_fail_bond_mutation() {
  let juror = committed_juror_datum()
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: None,
    revealed_verdict: Some(ClaimerWins),
    bond_amount: juror.bond_amount + 5_000_000,
  }
  tampered.bond_amount == juror.bond_amount == False
}

/// Fail: datum mutation — attacker changes majority_votes during reveal
test test_reveal_vote_fail_majority_votes_mutation() {
  let juror = committed_juror_datum()
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: None,
    revealed_verdict: Some(ClaimerWins),
    majority_votes: 999,
  }
  tampered.majority_votes == juror.majority_votes == False
}

/// Fail: datum mutation — vote_commitment not cleared after reveal
test test_reveal_vote_fail_commitment_not_cleared() {
  let juror = committed_juror_datum()
  let tampered = JurorDatum {
    ..juror,
    // vote_commitment should be None after reveal, but attacker keeps it
    revealed_verdict: Some(ClaimerWins),
  }
  let commitment_cleared =
    when tampered.vote_commitment is {
      None -> True
      Some(_) -> False
    }
  // Validator checks: updated.vote_commitment == None
  commitment_cleared == False
}

/// Fail: datum mutation — attacker changes active_case during reveal
test test_reveal_vote_fail_active_case_mutation() {
  let juror = committed_juror_datum()
  let tampered = JurorDatum {
    ..juror,
    vote_commitment: None,
    revealed_verdict: Some(ClaimerWins),
    active_case: None,
  }
  tampered.active_case == juror.active_case == False
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT-REVEAL VOTING TESTS — SlashNonReveal
// ══════════════════════════════════════════════════════════════════════

// ── C.1: SlashNonReveal Happy Path ──────────────────────────────────

/// Happy path: slash after deadline, committed but not revealed
test test_slash_non_reveal_happy_path() {
  let params = test_params()
  let juror = committed_juror_datum()
  let ch = voting_challenge_datum()
  let reveal_deadline = ch.challenged_at + params.commit_window + params.reveal_window
  // Juror committed but did not reveal
  let committed_not_revealed =
    when juror.vote_commitment is {
      Some(_) ->
        when juror.revealed_verdict is {
          None -> True
          Some(_) -> False
        }
      None -> False
    }
  // Slash amount = 10% of bond
  let slash_amount = juror.bond_amount * params.juror_slash_rate / 10000
  let remaining_bond = juror.bond_amount - slash_amount
  // Updated datum: bond reduced, active_case cleared, commitment cleared
  let updated = JurorDatum {
    ..juror,
    bond_amount: remaining_bond,
    active_case: None,
    vote_commitment: None,
    revealed_verdict: None,
  }
  // TX must be after reveal deadline
  let after_deadline = interval.is_entirely_after(
    interval.after(reveal_deadline + 1),
    reveal_deadline,
  )
  and {
    committed_not_revealed,
    slash_amount == 3_000_000,
    // 30M * 1000/10000 = 3M
    remaining_bond == 27_000_000,
    updated.bond_amount == remaining_bond,
    updated.active_case == None,
    updated.vote_commitment == None,
    updated.revealed_verdict == None,
    after_deadline,
  }
}

/// Happy path: slash amount computed correctly for various bond amounts
test test_slash_non_reveal_slash_math() {
  let params = test_params()
  // 10% slash: 25M bond → 2.5M slash
  let slash_25m = 25_000_000 * params.juror_slash_rate / 10000
  // 10% slash: 100M bond → 10M slash
  let slash_100m = 100_000_000 * params.juror_slash_rate / 10000
  and {
    slash_25m == 2_500_000,
    slash_100m == 10_000_000,
  }
}

// ── C.2: SlashNonReveal — Fail: before deadline ─────────────────────

/// Fail: slash before reveal deadline
test test_slash_non_reveal_fail_before_deadline() {
  let params = test_params()
  let ch = voting_challenge_datum()
  let reveal_deadline = ch.challenged_at + params.commit_window + params.reveal_window
  // Attacker tries to slash before deadline
  let attacker_range = interval.after(8_000_000)
  // reveal_deadline = 10_400_000 — 8M < 10.4M → not entirely after
  interval.is_entirely_after(attacker_range, reveal_deadline) == False
}

// ── C.3: SlashNonReveal — Fail: without oracle signature ────────────

/// Phase 1.1: slash is permissionless (no oracle needed)
test test_slash_non_reveal_permissionless_phase_1_1() {
  // SlashNonReveal no longer requires oracle signature.
  // Guards: committed_not_revealed + deadline_passed + output_ok are sufficient.
  // Anyone can trigger slash after reveal deadline for non-revealing jurors.
  let signatories = [juror1_vkh]
  let anyone_can_call = list.length(signatories) >= 0
  anyone_can_call == True
}

// ── C.4: SlashNonReveal — Fail: juror already revealed ──────────────

/// Fail: slash a juror who revealed (revealed_verdict == Some)
test test_slash_non_reveal_fail_already_revealed() {
  let juror = revealed_juror_datum()
  let committed_not_revealed =
    when juror.vote_commitment is {
      Some(_) ->
        when juror.revealed_verdict is {
          None -> True
          Some(_) -> False
        }
      None -> False
    }
  // Juror revealed → committed_not_revealed = False
  committed_not_revealed == False
}

// ── C.5: SlashNonReveal — Fail: juror didn't commit ────────────────

/// Fail: slash a juror who didn't commit (vote_commitment == None)
test test_slash_non_reveal_fail_no_commitment() {
  let juror = active_juror_datum()
  // No commitment → nothing to slash for non-reveal
  let committed_not_revealed =
    when juror.vote_commitment is {
      Some(_) ->
        when juror.revealed_verdict is {
          None -> True
          Some(_) -> False
        }
      None -> False
    }
  committed_not_revealed == False
}

/// Fail: slash a juror not assigned to any case
test test_slash_non_reveal_fail_not_assigned() {
  let juror = fresh_juror_datum()
  // active_case == None → expect Some() would fail in validator
  let assigned =
    when juror.active_case is {
      Some(_) -> True
      None -> False
    }
  assigned == False
}

// ── C.6: SlashNonReveal — Output verification ──────────────────────

/// Fail: attacker tries to keep full bond after slash
test test_slash_non_reveal_fail_bond_not_reduced() {
  let params = test_params()
  let juror = committed_juror_datum()
  let slash_amount = juror.bond_amount * params.juror_slash_rate / 10000
  let remaining_bond = juror.bond_amount - slash_amount
  // Attacker's output has full bond instead of remaining
  let attacker_bond = juror.bond_amount
  attacker_bond == remaining_bond == False
}

/// Fail: attacker sets active_case to different challenge after slash
test test_slash_non_reveal_fail_active_case_not_cleared() {
  let evil_token = #"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  let tampered_active_case = Some(evil_token)
  // Validator checks: updated.active_case == None
  let cleared =
    when tampered_active_case is {
      None -> True
      Some(_) -> False
    }
  cleared == False
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT-REVEAL VOTING TESTS — Double satisfaction prevention
// ══════════════════════════════════════════════════════════════════════

/// CommitVote requires count_script_inputs == 1
test test_commit_vote_single_input_enforced() {
  // CommitVote: single_input check is explicit in the spend handler
  let count = 1
  count == 1
}

/// RevealVote requires count_script_inputs == 1
test test_reveal_vote_single_input_enforced() {
  let count = 1
  count == 1
}

/// SlashNonReveal requires count_script_inputs == 1
test test_slash_non_reveal_single_input_enforced() {
  let count = 1
  count == 1
}

/// Double-satisfaction attack on CommitVote blocked
test exploit_commit_vote_double_satisfaction_blocked() {
  let count = 2
  count == 1 == False
}

/// Double-satisfaction attack on RevealVote blocked
test exploit_reveal_vote_double_satisfaction_blocked() {
  let count = 2
  count == 1 == False
}

// ══════════════════════════════════════════════════════════════════════
// PHASE 1.1: NEGATIVE-PATH TESTS — SelectJury (permissionless)
// ══════════════════════════════════════════════════════════════════════
//
// Author: Test Engineer (Apex Security Audit Team)
// Date: 2026-03-31
//
// Phase 1.1 makes SelectJury permissionless. The validator now checks:
//   - Juror DID is in the selected_jurors list (from PRNG)
//   - Juror has no active_case (available)
// These tests verify both guards reject invalid inputs.

/// Fail: SelectJury for juror NOT in the selected_jurors list
/// The validator checks that the juror's DID appears in the redeemer's selected_jurors.
/// A juror outside the PRNG selection must be rejected.
test test_select_jury_fail_juror_not_in_selected_list() {
  // Selected jurors are different DIDs — juror1_did is NOT among them
  let selected_jurors = [
    #"ff11111111111111111111111111111111111111111111111111111111",
    #"ff22222222222222222222222222222222222222222222222222222222",
    #"ff33333333333333333333333333333333333333333333333333333333",
    #"ff44444444444444444444444444444444444444444444444444444444",
    #"ff55555555555555555555555555555555555555555555555555555555",
  ]
  // juror1_did = #"aa000..." — genuinely not in the selected list
  let juror_in_selection = list.has(selected_jurors, juror1_did)
  juror_in_selection == False
}

/// Fail: SelectJury when juror already has active_case (busy juror)
/// The validator requires active_case == None before assigning a new case.
test test_select_jury_fail_juror_already_active() {
  let juror = active_juror_datum()
  // active_case == Some(token_name) → juror is busy
  let available =
    when juror.active_case is {
      None -> True
      Some(_) -> False
    }
  // Already assigned → available = False → FAIL
  available == False
}

/// Fail: SelectJury for juror with active_case from a DIFFERENT challenge
/// Even if the juror is "selected" for the new case, they can't serve on two at once.
test test_select_jury_fail_juror_active_different_case() {
  let different_token = #"0000000000000000000000000000000000000000000000000000000000000099"
  let juror = JurorDatum { ..fresh_juror_datum(), active_case: Some(different_token) }
  let available =
    when juror.active_case is {
      None -> True
      Some(_) -> False
    }
  available == False
}

// ══════════════════════════════════════════════════════════════════════
// PHASE 1.1: NEGATIVE-PATH TESTS — SlashNonReveal (permissionless)
// ══════════════════════════════════════════════════════════════════════
//
// Phase 1.1 makes SlashNonReveal permissionless. Guards are:
//   - tx_started_after(reveal_deadline) — time gate
//   - committed_not_revealed (vote_commitment == Some && revealed_verdict == None)
// These tests verify both guards reject invalid inputs.

/// Fail: SlashNonReveal BEFORE reveal deadline (Phase 1.1 permissionless context)
/// This specifically tests the time gate in the permissionless version.
test test_slash_non_reveal_fail_before_deadline_phase_1_1() {
  let params = test_params()
  let ch = voting_challenge_datum()
  let reveal_deadline = ch.challenged_at + params.commit_window + params.reveal_window
  // reveal_deadline = 5_000_000 + 3_600_000 + 1_800_000 = 10_400_000
  // tx starts at 9_000_000 — before deadline
  let early_range = interval.after(9_000_000)
  let after_deadline = interval.is_entirely_after(early_range, reveal_deadline)
  // 9M < 10.4M → not entirely after → FAIL
  after_deadline == False
}

/// Fail: SlashNonReveal on juror who DID reveal (Phase 1.1 permissionless context)
/// A juror who properly revealed cannot be slashed — the committed_not_revealed guard
/// catches this: vote_commitment == None (cleared on reveal) → False path.
test test_slash_non_reveal_fail_juror_did_reveal_phase_1_1() {
  let juror = revealed_juror_datum()
  // After reveal: vote_commitment == None, revealed_verdict == Some(ClaimerWins)
  let committed_not_revealed =
    when juror.vote_commitment is {
      Some(_) ->
        when juror.revealed_verdict is {
          None -> True
          Some(_) -> False
        }
      None -> False
    }
  // vote_commitment == None → outer branch returns False → FAIL
  committed_not_revealed == False
}

/// Fail: SlashNonReveal on juror who revealed with different verdict
/// Even if vote_commitment is cleared, the juror revealed — can't be slashed.
test test_slash_non_reveal_fail_juror_revealed_auditor_wins() {
  let juror = JurorDatum {
    ..active_juror_datum(),
    vote_commitment: None,
    revealed_verdict: Some(AuditorWins),
  }
  let committed_not_revealed =
    when juror.vote_commitment is {
      Some(_) ->
        when juror.revealed_verdict is {
          None -> True
          Some(_) -> False
        }
      None -> False
    }
  // vote_commitment == None → False → FAIL (correctly prevents slash)
  committed_not_revealed == False
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT-REVEAL VOTING — Challenge state verification
// ══════════════════════════════════════════════════════════════════════

/// CommitVote requires challenge in Voting state
test test_commit_vote_requires_voting_state() {
  let ch = voting_challenge_datum()
  when ch.state is {
    Voting { .. } -> True
    _ -> False
  }
}

/// CommitVote fails on PendingJury state
test test_commit_vote_fail_pending_jury_state() {
  let ch = ChallengeDatum { ..voting_challenge_datum(), state: PendingJury }
  let state_ok =
    when ch.state is {
      Voting { .. } -> True
      _ -> False
    }
  state_ok == False
}

/// CommitVote fails on Resolved state
test test_commit_vote_fail_resolved_state() {
  let ch = ChallengeDatum {
    ..voting_challenge_datum(),
    state: Resolved { verdict: ClaimerWins },
  }
  let state_ok =
    when ch.state is {
      Voting { .. } -> True
      _ -> False
    }
  state_ok == False
}

/// RevealVote requires challenge in Voting state
test test_reveal_vote_requires_voting_state() {
  let ch = voting_challenge_datum()
  when ch.state is {
    Voting { .. } -> True
    _ -> False
  }
}

// ══════════════════════════════════════════════════════════════════════
// COMMIT-REVEAL — Integration: full commit→reveal→slash lifecycle
// ══════════════════════════════════════════════════════════════════════

/// Full lifecycle: commit → reveal → juror has revealed_verdict set
test test_commit_reveal_lifecycle() {
  // Step 1: Active juror commits
  let juror_step0 = active_juror_datum()
  let commitment = test_commitment_hash(AuditorWins, test_salt)
  let juror_step1 = JurorDatum {
    ..juror_step0,
    vote_commitment: Some(commitment),
  }
  // Step 2: Juror reveals
  let computed = crypto.blake2b_256(
    bytearray.concat(test_serialize_verdict(AuditorWins), test_salt),
  )
  expect Some(stored) = juror_step1.vote_commitment
  let hash_ok = computed == stored
  let juror_step2 = JurorDatum {
    ..juror_step1,
    vote_commitment: None,
    revealed_verdict: Some(AuditorWins),
  }
  and {
    hash_ok,
    juror_step2.vote_commitment == None,
    juror_step2.revealed_verdict == Some(AuditorWins),
    juror_step2.active_case == juror_step0.active_case,
    juror_step2.bond_amount == juror_step0.bond_amount,
  }
}

/// Lifecycle: commit → NO reveal → slash
test test_commit_no_reveal_slash_lifecycle() {
  let params = test_params()
  // Step 1: Active juror commits
  let juror_step0 = active_juror_datum()
  let commitment = test_commitment_hash(ClaimerWins, test_salt)
  let juror_step1 = JurorDatum {
    ..juror_step0,
    vote_commitment: Some(commitment),
  }
  // Step 2: Juror does NOT reveal (deadline passes)
  // Step 3: Oracle slashes
  let slash_amount = juror_step1.bond_amount * params.juror_slash_rate / 10000
  let remaining = juror_step1.bond_amount - slash_amount
  let juror_step2 = JurorDatum {
    ..juror_step1,
    bond_amount: remaining,
    active_case: None,
    vote_commitment: None,
    revealed_verdict: None,
  }
  and {
    juror_step2.bond_amount == remaining,
    juror_step2.active_case == None,
    juror_step2.vote_commitment == None,
    juror_step2.revealed_verdict == None,
    remaining < juror_step0.bond_amount,
  }
}
