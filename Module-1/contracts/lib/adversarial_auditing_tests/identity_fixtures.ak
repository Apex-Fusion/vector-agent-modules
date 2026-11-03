//// Shared fixtures for the identity tests of Module-1 (identity checks,
//// identity_party, challenge verdict mints). All hashes, keys and times are
//// test values.
////
//// PLACEHOLDER: the verdict policy values are an open item (Part 2 §13.4).
//// `placeholder_verdict_policy` is a test and deployment placeholder only.

use adversarial_auditing/identity_checks.{config_token_name}
use adversarial_auditing/identity_party.{verdict_token_name}
use adversarial_auditing/params.{ProtocolParams}
use adversarial_auditing/types.{
  ApplyVerdict, AuditingConfig, AuditorWins, BurnVerdict, ChallengeDatum,
  ChallengeState, Challenged, ClaimDatum, CleanupResolved, Consent,
  CrossValidatorRefs, ForfeitClaim, IdentityVerdict, Inconclusive, JurorDatum,
  MintVerdict, OracleResolve, PendingOracle, ResolveJury, Resolved,
  TimeoutResolve, Verdict, VerdictAction, VerdictPolicy, VerdictRevoke,
  VerdictRule, VerdictSuspend, Voting,
}
use aiken/collection/list
use aiken/crypto.{blake2b_224}
use aiken/interval.{Interval}
use aiken/primitive/bytearray
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Mint, NoDatum, Output, OutputReference, Spend, Transaction,
  Withdraw, placeholder,
}
use vector_identity.{day_ms, days_30_ms, days_7_ms, right_revoke, right_suspend}
use vector_identity/redeemers.{IdentitySpend, Register, Update}
use vector_identity/types.{
  Accountable, Active, ControllerSet, Distributor, Ed25519, Guardian, Hold,
  IdentityDatum, Key, KeyHash, P256, Party, Provider, Responsible, Role, Root,
  Status, Suspended,
} as vi

// ## Script hashes and keys

pub const identity_hash: ByteArray =
  #"1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d"

pub const config_policy: ByteArray =
  #"cfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcfcf"

pub const config_holder: ByteArray =
  #"c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4"

/// M1: the hash of identity_party in these tests.
pub const module1_hash: ByteArray =
  #"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"

/// Another script hash (not M1).
pub const other_script_hash: ByteArray =
  #"e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2"

pub const claim_hash: ByteArray =
  #"cacacacacacacacacacacacacacacacacacacacacacacacacacacaca"

pub const challenge_hash: ByteArray =
  #"cbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcb"

pub const jury_pool_hash: ByteArray =
  #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

pub const refs_token_policy: ByteArray =
  #"99999999999999999999999999999999999999999999999999999999"

pub const oracle_vkh: ByteArray =
  #"0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a"

/// Ed25519 controller key of agent A (the claimer).
pub const controller_a: ByteArray =
  #"c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"

/// Ed25519 controller key of agent B (the auditor).
pub const controller_b: ByteArray =
  #"b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"

/// A listed Ed25519 key of agent A with the purposes authentication and
/// transaction.
pub const tx_key_a: ByteArray =
  #"e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"

/// A listed Ed25519 key of agent A with authentication and assertion only.
pub const auth_key_a: ByteArray =
  #"e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6"

/// A P-256 key of agent A (authentication and assertion).
pub const p256_key_a: ByteArray =
  #"02a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2"

/// A 64-byte proof of possession (not checked on-chain).
pub const pop_64: ByteArray =
  #"50505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050505050"

/// A key that no record lists.
pub const stranger_key: ByteArray =
  #"5555555555555555555555555555555555555555555555555555555555555555"

/// Key hash of the Accountable key party of the test records.
pub const accountable_key_hash: ByteArray =
  #"acacacacacacacacacacacacacacacacacacacacacacacacacacacac"

/// Agent identifiers (32 bytes).
pub const agent_a: ByteArray =
  #"a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0"

pub const agent_b: ByteArray =
  #"b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1"

pub const agent_c: ByteArray =
  #"c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9c9"

/// Claim token name of the challenged claim.
pub const claim_token: ByteArray =
  #"636c6d5f01010101010101010101010101010101010101010101010101010101"

/// Another claim token name.
pub const other_claim_token: ByteArray =
  #"636c6d5f02020202020202020202020202020202020202020202020202020202"

/// Challenge token name.
pub const challenge_token: ByteArray =
  #"63686c5f03030303030303030303030303030303030303030303030303030303"

pub const hash_32: ByteArray =
  #"cafebabecafebabecafebabecafebabecafebabecafebabecafebabecafebabe"

// ## Times

/// 2026-11-02T00:00:00Z.
pub const lo: Int = 1_793_577_600_000

/// `lo` + 10 minutes.
pub const hi: Int = 1_793_578_200_000

pub fn window_range() -> Interval {
  interval.between(lo, hi)
}

// ## Configuration

/// PLACEHOLDER (Part 2 §13.4 open item): AuditorWins suspends the claimer
/// for 7 days; ClaimerWins gives no verdict. Not an agreed value.
pub fn placeholder_verdict_policy() -> VerdictPolicy {
  VerdictPolicy {
    auditor_wins: Some(
      VerdictRule { action: VerdictSuspend, duration: days_7_ms },
    ),
    claimer_wins: None,
  }
}

/// A test policy: AuditorWins revokes the claimer; ClaimerWins suspends the
/// auditor for 1 day.
pub fn revoke_verdict_policy() -> VerdictPolicy {
  VerdictPolicy {
    auditor_wins: Some(VerdictRule { action: VerdictRevoke, duration: 0 }),
    claimer_wins: Some(VerdictRule { action: VerdictSuspend, duration: day_ms }),
  }
}

/// A policy without verdicts.
pub fn empty_verdict_policy() -> VerdictPolicy {
  VerdictPolicy { auditor_wins: None, claimer_wins: None }
}

pub fn auditing_config(policy: VerdictPolicy) -> AuditingConfig {
  AuditingConfig {
    identity_hash,
    oracle_vkh,
    refs_token_policy,
    config_policy,
    config_holder,
    verdict_policy: policy,
  }
}

pub fn protocol_params() -> ProtocolParams {
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

pub fn cross_refs() -> CrossValidatorRefs {
  CrossValidatorRefs {
    claim_validator_hash: claim_hash,
    challenge_validator_hash: challenge_hash,
    jury_pool_hash,
  }
}

// ## Output references, addresses and simple inputs

/// A test output reference. `tag` (0..255) is the first byte of the
/// transaction id.
pub fn out_ref(tag: Int, index: Int) -> OutputReference {
  OutputReference {
    transaction_id: bytearray.push(
      #"78787878787878787878787878787878787878787878787878787878787878",
      tag,
    ),
    output_index: index,
  }
}

pub fn script_address(hash: ByteArray) -> Address {
  Address { payment_credential: Script(hash), stake_credential: None }
}

pub fn key_address(key_hash: ByteArray) -> Address {
  Address {
    payment_credential: VerificationKey(key_hash),
    stake_credential: None,
  }
}

pub fn key_credential(key: ByteArray) -> Credential {
  VerificationKey(blake2b_224(key))
}

/// A wallet input that pays fees and the verdict output deposit.
pub fn wallet_input() -> Input {
  Input {
    output_reference: out_ref(0xf, 0),
    output: Output {
      address: funder_address(),
      value: assets.from_lovelace(500_000_000),
      datum: NoDatum,
      reference_script: None,
    },
  }
}

/// The address of the wallet input: the funder of the verdict output
/// deposit (`deposit_to`) and of the AP3X that a Resolved output keeps
/// (`refund_to`) in these tests.
pub fn funder_address() -> Address {
  key_address(blake2b_224(controller_b))
}

/// The configuration record: at the exact config_holder address, with the
/// config_policy token `identity-config` and the inline datum `m1`.
pub fn config_input(m1: ByteArray) -> Input {
  Input {
    output_reference: out_ref(0xcf, 0),
    output: Output {
      address: script_address(config_holder),
      value: assets.from_lovelace(2_000_000)
        |> assets.add(config_policy, config_token_name, 1),
      datum: InlineDatum(m1),
      reference_script: None,
    },
  }
}

/// The CrossValidatorRefs record, authenticated by the refs token.
pub fn refs_input() -> Input {
  Input {
    output_reference: out_ref(0xee, 0),
    output: Output {
      address: key_address(oracle_vkh),
      value: assets.from_lovelace(2_000_000)
        |> assets.add(refs_token_policy, "refs", 1),
      datum: InlineDatum(cross_refs()),
      reference_script: None,
    },
  }
}

// ## Identity records

pub fn ed25519_key(key: ByteArray, purposes: Int) -> Key {
  Key { key_type: Ed25519, key, purposes, pop: None }
}

pub fn p256_key(key: ByteArray) -> Key {
  Key { key_type: P256, key, purposes: 3, pop: Some(pop_64) }
}

/// The highest rights of a role that Module-1 can hold.
pub fn highest_rights(role: Role) -> Int {
  when role is {
    Accountable -> right_suspend + right_revoke
    _ -> right_suspend
  }
}

pub fn accountable_key_party() -> Party {
  Party {
    role: Accountable,
    signer: KeyHash { hash: accountable_key_hash },
    organisation: None,
    not_before: lo - day_ms,
    not_after: None,
    rights: right_suspend + right_revoke,
    removal_after: None,
  }
}

/// A Module-1 party with the given role and the role's highest rights.
pub fn module1_party(role: Role) -> Party {
  Party {
    role,
    signer: vi.Script { hash: module1_hash },
    organisation: None,
    not_before: lo - day_ms,
    not_after: None,
    rights: highest_rights(role),
    removal_after: None,
  }
}

pub fn agent_record(
  controller: ByteArray,
  keys: List<Key>,
  parties: List<Party>,
  status: Status,
) -> IdentityDatum {
  IdentityDatum {
    controller,
    keys,
    parties,
    guardian_threshold: 0,
    pending_recovery: None,
    lineage: Root,
    subagent_epoch: 0,
    name: "test agent",
    description: "",
    framework: "",
    endpoint: "https://agent.example",
    capabilities: ["data_indexing"],
    created_at: lo - 30 * day_ms,
    status,
  }
}

/// Agent A (the claimer): controller_a, a transaction key, an
/// authentication key and a P-256 key; an Accountable key party and a
/// Module-1 Accountable party.
pub fn record_a(status: Status) -> IdentityDatum {
  agent_record(
    controller_a,
    [ed25519_key(tx_key_a, 9), ed25519_key(auth_key_a, 3), p256_key(p256_key_a)],
    [accountable_key_party(), module1_party(Accountable)],
    status,
  )
}

/// Agent B (the auditor): controller_b, no listed key, an Accountable key
/// party.
pub fn record_b(status: Status) -> IdentityDatum {
  agent_record(controller_b, [], [accountable_key_party()], status)
}

pub fn record_output(id: ByteArray, record: IdentityDatum) -> Output {
  Output {
    address: script_address(identity_hash),
    value: assets.from_lovelace(10_000_000) |> assets.add(identity_hash, id, 1),
    datum: InlineDatum(record),
    reference_script: None,
  }
}

pub fn record_input(
  reference: OutputReference,
  id: ByteArray,
  record: IdentityDatum,
) -> Input {
  Input { output_reference: reference, output: record_output(id, record) }
}

/// A hold by `by` with reason 6 that started one day before `lo`.
pub fn hold(by: vi.Signer, until: Int) -> Hold {
  Hold { by, reason: 6, since: lo - day_ms, until }
}

pub fn module1_signer() -> vi.Signer {
  vi.Script { hash: module1_hash }
}

/// The largest record of Part 2 §12.4: 8 keys, 8 parties (one of them the
/// Module-1 Accountable party), 16 capabilities of 64 bytes, a 64-byte name
/// and framework, a 256-byte description and endpoint, and `status`.
pub fn largest_record(status: Status) -> IdentityDatum {
  IdentityDatum {
    controller: controller_a,
    keys: [
      ed25519_key(tx_key_a, 27),
      ed25519_key(auth_key_a, 27),
      ed25519_key(bytearray.push(text_31, 1), 27),
      ed25519_key(bytearray.push(text_31, 2), 27),
      ed25519_key(bytearray.push(text_31, 3), 27),
      ed25519_key(bytearray.push(text_31, 4), 27),
      p256_key(p256_key_a),
      p256_key(bytearray.push(text_32, 3)),
    ],
    parties: [
      accountable_key_party(),
      Party { ..module1_party(Accountable), organisation: Some(text_64) },
      Party {
        ..accountable_key_party(),
        role: Responsible,
        signer: KeyHash { hash: party_hash(1) },
        rights: right_suspend,
        organisation: Some(text_64),
      },
      Party {
        ..accountable_key_party(),
        role: Responsible,
        signer: ControllerSet,
        rights: right_suspend,
      },
      Party {
        ..accountable_key_party(),
        role: Provider,
        signer: KeyHash { hash: party_hash(2) },
        rights: right_suspend,
        organisation: Some(text_64),
      },
      Party {
        ..accountable_key_party(),
        role: Distributor,
        signer: KeyHash { hash: party_hash(3) },
        rights: 0,
        organisation: Some(text_64),
      },
      Party {
        ..accountable_key_party(),
        role: Guardian,
        signer: KeyHash { hash: party_hash(4) },
        rights: 4,
      },
      Party {
        ..accountable_key_party(),
        role: Guardian,
        signer: KeyHash { hash: party_hash(5) },
        rights: 4,
      },
    ],
    guardian_threshold: 2,
    pending_recovery: None,
    lineage: Root,
    subagent_epoch: 0,
    name: text_64,
    description: text_256,
    framework: text_64,
    endpoint: text_256,
    capabilities: [
      bytearray.push(text_63, 0x41),
      bytearray.push(text_63, 0x42),
      bytearray.push(text_63, 0x43),
      bytearray.push(text_63, 0x44),
      bytearray.push(text_63, 0x45),
      bytearray.push(text_63, 0x46),
      bytearray.push(text_63, 0x47),
      bytearray.push(text_63, 0x48),
      bytearray.push(text_63, 0x49),
      bytearray.push(text_63, 0x4a),
      bytearray.push(text_63, 0x4b),
      bytearray.push(text_63, 0x4c),
      bytearray.push(text_63, 0x4d),
      bytearray.push(text_63, 0x4e),
      bytearray.push(text_63, 0x4f),
      bytearray.push(text_63, 0x50),
    ],
    created_at: lo - 30 * day_ms,
    status,
  }
}

/// Ten holds in force, each by a different author: the controller, the
/// Module-1 party (until `module1_until`), six key parties and two removed
/// key parties.
pub fn ten_holds(module1_until: Int) -> List<Hold> {
  [
    hold(ControllerSet, lo + day_ms),
    hold(KeyHash { hash: accountable_key_hash }, lo + day_ms),
    hold(KeyHash { hash: party_hash(1) }, lo + day_ms),
    hold(KeyHash { hash: party_hash(2) }, lo + day_ms),
    hold(KeyHash { hash: party_hash(4) }, lo + day_ms),
    hold(KeyHash { hash: party_hash(5) }, lo + day_ms),
    hold(KeyHash { hash: party_hash(6) }, lo + day_ms),
    hold(KeyHash { hash: party_hash(7) }, lo + day_ms),
    hold(KeyHash { hash: party_hash(8) }, lo + day_ms),
    Hold {
      by: module1_signer(),
      reason: 64,
      since: lo - day_ms,
      until: module1_until,
    },
  ]
}

/// The key hash of the n-th test key party (28 bytes).
pub fn party_hash(n: Int) -> ByteArray {
  bytearray.push(#"7070707070707070707070707070707070707070707070707070", n)
    |> bytearray.push(0x70)
}

const text_31: ByteArray =
  #"74747474747474747474747474747474747474747474747474747474747474"

const text_32: ByteArray =
  #"0274747474747474747474747474747474747474747474747474747474747474"

const text_63: ByteArray =
  "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijk"

const text_64: ByteArray =
  "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijkl"

const text_256: ByteArray =
  "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijkl"

// ## Claims and challenges

pub fn challenged_claim_datum() -> ClaimDatum {
  ClaimDatum {
    claimer_did: agent_a,
    claimer_credential: key_credential(tx_key_a),
    claim_hash: hash_32,
    claim_type: "data_indexing",
    storage_uri: "ipfs://claim",
    stake_amount: 100_000_000,
    submitted_at: lo - 7_200_000,
    challenge_window: 3_600_000,
    state: Challenged,
  }
}

pub fn claim_ref() -> OutputReference {
  out_ref(0xca, 0)
}

/// The challenged claim: at the claim address with the claim token `token`.
pub fn claim_input(token: ByteArray, datum: ClaimDatum) -> Input {
  Input {
    output_reference: claim_ref(),
    output: Output {
      address: script_address(claim_hash),
      value: assets.from_lovelace(datum.stake_amount)
        |> assets.add(claim_hash, token, 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

pub fn challenge_datum(state: ChallengeState) -> ChallengeDatum {
  ChallengeDatum {
    claim_ref: out_ref(0xc0, 0),
    auditor_did: agent_b,
    auditor_credential: key_credential(controller_b),
    stake_amount: 100_000_000,
    evidence_hash: hash_32,
    evidence_uri: "ipfs://evidence",
    challenged_at: lo - 5_400_000,
    resolution_deadline: 5_400_000 * 4,
    eligible_jurors: [],
    state,
    claim_token,
  }
}

/// The reference of the challenge input of the resolution.
pub fn challenge_ref() -> OutputReference {
  out_ref(0xcb, 1)
}

pub fn challenge_input(datum: ChallengeDatum) -> Input {
  Input {
    output_reference: challenge_ref(),
    output: Output {
      address: script_address(challenge_hash),
      value: assets.from_lovelace(datum.stake_amount)
        |> assets.add(challenge_hash, challenge_token, 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

// ## Verdicts

pub fn verdict(
  agent: ByteArray,
  action: VerdictAction,
  duration: Int,
) -> IdentityVerdict {
  IdentityVerdict {
    agent,
    action,
    duration,
    challenge: challenge_ref(),
    deposit_to: funder_address(),
    expires_at: hi + days_30_ms,
  }
}

/// The verdict of the base resolution under the placeholder policy.
pub fn base_verdict() -> IdentityVerdict {
  verdict(agent_a, VerdictSuspend, days_7_ms)
}

/// The verdict output: at the exact identity_party address, with AP3X, the
/// verdict token and the verdict as inline datum.
pub fn verdict_output(v: IdentityVerdict) -> Output {
  Output {
    address: script_address(module1_hash),
    value: assets.from_lovelace(2_000_000)
      |> assets.add(module1_hash, verdict_token_name(v), 1),
    datum: InlineDatum(v),
    reference_script: None,
  }
}

pub fn verdict_ref() -> OutputReference {
  out_ref(0xe1, 0)
}

pub fn verdict_input(v: IdentityVerdict) -> Input {
  Input { output_reference: verdict_ref(), output: verdict_output(v) }
}

fn data(value: Data) -> Data {
  value
}

/// A value as Data (for redeemers in test transactions).
pub fn to_data(value: Data) -> Data {
  value
}

/// OracleResolve of the base challenge (state PendingOracle) with `outcome`:
/// the challenge and claim tokens are burned, the winner gets both stakes,
/// and the token of `v` is minted to its verdict output.
pub fn oracle_resolution_tx(outcome: Verdict, v: IdentityVerdict) -> Transaction {
  let challenge = challenge_input(challenge_datum(PendingOracle))
  let winner =
    when outcome is {
      AuditorWins -> key_credential(controller_b)
      _ -> key_credential(tx_key_a)
    }
  Transaction {
    ..placeholder,
    inputs: [
      challenge,
      claim_input(claim_token, challenged_claim_datum()),
      wallet_input(),
    ],
    reference_inputs: [refs_input(), config_input(module1_hash)],
    outputs: [
      Output {
        address: Address { payment_credential: winner, stake_credential: None },
        value: assets.from_lovelace(200_000_000),
        datum: NoDatum,
        reference_script: None,
      },
      verdict_output(v),
    ],
    mint: assets.zero
      |> assets.add(challenge_hash, challenge_token, -1)
      |> assets.add(claim_hash, claim_token, -1)
      |> assets.add(module1_hash, verdict_token_name(v), 1),
    extra_signatories: [oracle_vkh],
    validity_range: window_range(),
    redeemers: [
      Pair(
        Spend(challenge_ref()),
        data(OracleResolve { verdict: outcome, reasoning_hash: hash_32 }),
      ),
      Pair(Spend(claim_ref()), data(ForfeitClaim)),
      Pair(Mint(module1_hash), data(MintVerdict)),
    ],
  }
}

const juror_base: ByteArray =
  #"11111111111111111111111111111111111111111111111111111111111111"

/// The five selected jurors of the jury resolution.
pub fn juror_dids() -> List<ByteArray> {
  [
    bytearray.push(juror_base, 1),
    bytearray.push(juror_base, 2),
    bytearray.push(juror_base, 3),
    bytearray.push(juror_base, 4),
    bytearray.push(juror_base, 5),
  ]
}

fn juror_input(n: Int, did: ByteArray, vote: Verdict) -> Input {
  Input {
    output_reference: out_ref(0x40 + n, 0),
    output: Output {
      address: script_address(jury_pool_hash),
      value: assets.from_lovelace(25_000_000)
        |> assets.add(jury_pool_hash, bytearray.push(#"6a75725f", n), 1),
      datum: InlineDatum(
        JurorDatum {
          juror_did: did,
          juror_credential: key_credential(stranger_key),
          bond_amount: 25_000_000,
          cases_resolved: 0,
          majority_votes: 0,
          registered_at: lo - day_ms,
          active_case: Some(challenge_token),
          vote_commitment: None,
          revealed_verdict: Some(vote),
        },
      ),
      reference_script: None,
    },
  }
}

/// Five revealed jurors: four vote `outcome`, one votes Inconclusive.
pub fn juror_inputs(outcome: Verdict) -> List<Input> {
  when juror_dids() is {
    [j1, j2, j3, j4, j5] ->
      [
        juror_input(1, j1, outcome),
        juror_input(2, j2, outcome),
        juror_input(3, j3, outcome),
        juror_input(4, j4, outcome),
        juror_input(5, j5, Inconclusive),
      ]
    _ -> fail
  }
}

/// ResolveJury of the base challenge (state Voting) with `outcome` (tally
/// of `juror_inputs(outcome)`): the challenge continues with Resolved (the
/// wallet funds the kept AP3X, refund_to = `funder_address()`), the claim
/// token is burned, the winner gets both stakes minus the jury fee, and the
/// jury pool gets the fee. A jury resolution gives no verdict, so the
/// transaction mints none.
pub fn jury_resolution_tx(outcome: Verdict) -> Transaction {
  let voting = challenge_datum(Voting { selected_jurors: juror_dids() })
  let (winner, payout) =
    when outcome is {
      AuditorWins -> (key_credential(controller_b), 190_000_000)
      _ -> (key_credential(tx_key_a), 190_000_000)
    }
  Transaction {
    ..placeholder,
    inputs: [
      challenge_input(voting),
      claim_input(claim_token, challenged_claim_datum()),
      wallet_input(),
    ],
    reference_inputs: [refs_input(), ..juror_inputs(outcome)],
    outputs: [
      Output {
        address: script_address(challenge_hash),
        value: assets.from_lovelace(100_000_000)
          |> assets.add(challenge_hash, challenge_token, 1),
        datum: InlineDatum(
          ChallengeDatum {
            ..voting,
            state: Resolved { verdict: outcome, refund_to: funder_address() },
          },
        ),
        reference_script: None,
      },
      Output {
        address: Address { payment_credential: winner, stake_credential: None },
        value: assets.from_lovelace(payout),
        datum: NoDatum,
        reference_script: None,
      },
      Output {
        address: script_address(jury_pool_hash),
        value: assets.from_lovelace(10_000_000),
        datum: NoDatum,
        reference_script: None,
      },
    ],
    mint: assets.from_asset(claim_hash, claim_token, -1),
    extra_signatories: [],
    validity_range: window_range(),
    redeemers: [
      Pair(Spend(challenge_ref()), data(ResolveJury)),
      Pair(Spend(claim_ref()), data(ForfeitClaim)),
    ],
  }
}

/// `jury_resolution_tx(outcome)` that also tries to mint the verdict `v`
/// (verdict output, MintVerdict redeemer and the configuration record).
/// identity_party must reject it: jury resolutions give no verdict.
pub fn jury_resolution_with_verdict_tx(
  outcome: Verdict,
  v: IdentityVerdict,
) -> Transaction {
  let tx = jury_resolution_tx(outcome)
  Transaction {
    ..tx,
    reference_inputs: [config_input(module1_hash), ..tx.reference_inputs],
    outputs: list.concat(tx.outputs, [verdict_output(v)]),
    mint: assets.add(tx.mint, module1_hash, verdict_token_name(v), 1),
    redeemers: list.concat(
      tx.redeemers,
      [Pair(Mint(module1_hash), data(MintVerdict))],
    ),
  }
}

/// The base challenge in state Resolved with `outcome`; refund_to is
/// `funder_address()`.
pub fn resolved_challenge_datum(outcome: Verdict) -> ChallengeDatum {
  challenge_datum(Resolved { verdict: outcome, refund_to: funder_address() })
}

/// The payout of the AP3X that a Resolved output keeps: to
/// `funder_address()`, without datum.
pub fn refund_output(amount: Int) -> Output {
  Output {
    address: funder_address(),
    value: assets.from_lovelace(amount),
    datum: NoDatum,
    reference_script: None,
  }
}

/// CleanupResolved of the challenge `datum` (state Resolved, refund_to =
/// `funder_address()`) after the cleanup time: the challenge token is burned
/// and all AP3X of the Resolved output goes to refund_to.
pub fn cleanup_tx(datum: ChallengeDatum) -> Transaction {
  let start =
    datum.challenged_at + datum.resolution_deadline + protocol_params().cleanup_buffer + 1
  Transaction {
    ..placeholder,
    inputs: [challenge_input(datum), wallet_input()],
    outputs: [refund_output(datum.stake_amount)],
    mint: assets.from_asset(challenge_hash, challenge_token, -1),
    validity_range: interval.between(start, start + 600_000),
    redeemers: [Pair(Spend(challenge_ref()), data(CleanupResolved))],
  }
}

/// TimeoutResolve of the base challenge (state PendingOracle) after its
/// resolution deadline: the challenge and claim tokens are burned, and each
/// side gets its stake back.
pub fn timeout_resolution_tx() -> Transaction {
  let datum = challenge_datum(PendingOracle)
  let start = datum.challenged_at + datum.resolution_deadline + 1
  Transaction {
    ..placeholder,
    inputs: [
      challenge_input(datum),
      claim_input(claim_token, challenged_claim_datum()),
      wallet_input(),
    ],
    outputs: [
      Output {
        address: key_address(blake2b_224(tx_key_a)),
        value: assets.from_lovelace(100_000_000),
        datum: NoDatum,
        reference_script: None,
      },
      Output {
        address: key_address(blake2b_224(controller_b)),
        value: assets.from_lovelace(100_000_000),
        datum: NoDatum,
        reference_script: None,
      },
    ],
    mint: assets.from_asset(challenge_hash, challenge_token, -1)
      |> assets.add(claim_hash, claim_token, -1),
    validity_range: interval.between(start, start + 600_000),
    redeemers: [
      Pair(Spend(challenge_ref()), data(TimeoutResolve)),
      Pair(Spend(claim_ref()), data(ForfeitClaim)),
    ],
  }
}

/// `tx` with its claim input replaced by a claim at the same output
/// reference that holds `other_claim_token`, and with the burn of
/// `other_claim_token` instead of `claim_token`.
pub fn with_claim_of_other_challenge(tx: Transaction) -> Transaction {
  Transaction {
    ..tx,
    inputs: list.map(
      tx.inputs,
      fn(input) {
        if input.output_reference == claim_ref() {
          claim_input(other_claim_token, challenged_claim_datum())
        } else {
          input
        }
      },
    ),
    mint: tx.mint
      |> assets.add(claim_hash, claim_token, 1)
      |> assets.add(claim_hash, other_claim_token, -1),
  }
}

/// `tx` with the datum of its challenge input naming `other_claim_token`:
/// the challenge of another claim.
pub fn with_challenge_of_other_claim(tx: Transaction) -> Transaction {
  Transaction {
    ..tx,
    inputs: list.map(
      tx.inputs,
      fn(input) {
        if input.output_reference == challenge_ref() {
          expect InlineDatum(data) = input.output.datum
          expect datum: ChallengeDatum = data
          challenge_input(
            ChallengeDatum { ..datum, claim_token: other_claim_token },
          )
        } else {
          input
        }
      },
    ),
  }
}

// ## Identity transactions with a Module-1 withdrawal

pub fn record_ref() -> OutputReference {
  out_ref(0xa0, 0)
}

fn module1_withdrawal() -> Pairs<Credential, Int> {
  [Pair(Script(module1_hash), 0)]
}

/// An identity spend of the record of `id` from `before` to `after` with the
/// identity redeemer `action`, the Module-1 withdrawal with `approval`, and
/// the configuration record as reference input. The controller of `before`
/// signs.
pub fn identity_spend_tx(
  id: ByteArray,
  before: IdentityDatum,
  after: IdentityDatum,
  action: IdentitySpend,
  approval: Data,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [record_input(record_ref(), id, before), wallet_input()],
    reference_inputs: [config_input(module1_hash)],
    outputs: [record_output(id, after)],
    mint: assets.zero,
    withdrawals: module1_withdrawal(),
    extra_signatories: [blake2b_224(before.controller)],
    validity_range: window_range(),
    redeemers: [
      Pair(Spend(record_ref()), data(action)),
      Pair(Withdraw(Script(module1_hash)), approval),
    ],
  }
}

/// A Consent Update of agent A's record from `before` to `after`.
pub fn consent_update_tx(
  before: IdentityDatum,
  after: IdentityDatum,
) -> Transaction {
  identity_spend_tx(agent_a, before, after, Update, data(Consent))
}

/// A Register of the record `record` (identifier agent_c) with the Module-1
/// withdrawal Consent.
pub fn consent_register_tx(record: IdentityDatum) -> Transaction {
  let seed = out_ref(0x5e, 0)
  Transaction {
    ..placeholder,
    inputs: [
      Input {
        output_reference: seed,
        output: Output {
          address: key_address(blake2b_224(record.controller)),
          value: assets.from_lovelace(50_000_000),
          datum: NoDatum,
          reference_script: None,
        },
      },
    ],
    reference_inputs: [config_input(module1_hash)],
    outputs: [record_output(agent_c, record)],
    mint: assets.from_asset(identity_hash, agent_c, 1),
    withdrawals: module1_withdrawal(),
    extra_signatories: [blake2b_224(record.controller)],
    validity_range: window_range(),
    redeemers: [
      Pair(Mint(identity_hash), data(Register { seed })),
      Pair(Withdraw(Script(module1_hash)), data(Consent)),
    ],
  }
}

/// A verdict application: the verdict output of `v` is spent and its token
/// burned, and the record of `id` goes from `before` to `after` with the
/// identity redeemer `action`.
pub fn apply_tx(
  v: IdentityVerdict,
  id: ByteArray,
  before: IdentityDatum,
  after: IdentityDatum,
  action: IdentitySpend,
) -> Transaction {
  let base = identity_spend_tx(id, before, after, action, data(ApplyVerdict))
  Transaction {
    ..base,
    inputs: [verdict_input(v), ..base.inputs],
    mint: assets.from_asset(module1_hash, verdict_token_name(v), -1),
    extra_signatories: [],
    redeemers: [
      Pair(Spend(verdict_ref()), data(Void)),
      Pair(Mint(module1_hash), data(BurnVerdict)),
      ..base.redeemers
    ],
  }
}

/// `record` with status Suspended by `holds`.
pub fn with_holds(record: IdentityDatum, holds: List<Hold>) -> IdentityDatum {
  IdentityDatum { ..record, status: Suspended { holds } }
}

/// The Module-1 verdict hold of a verdict application at `lo`.
pub fn verdict_hold(until: Int) -> Hold {
  Hold { by: module1_signer(), reason: 64, since: lo, until }
}

/// `record` with only the status changed to `status`.
pub fn with_status(record: IdentityDatum, status: Status) -> IdentityDatum {
  IdentityDatum { ..record, status }
}

/// An Active record of agent A for identity checks.
pub fn active_record_a() -> IdentityDatum {
  record_a(Active)
}
