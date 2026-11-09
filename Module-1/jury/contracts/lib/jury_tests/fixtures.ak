//// Shared fixtures for the jury tests. All hashes, keys and times are test
//// values. The builders make honest transactions: the outputs follow the
//// rules for the given inputs. A test changes one part to break one rule.

use aiken/collection/list
use aiken/collection/pairs
use aiken/crypto.{blake2b_224}
use aiken/interval.{Interval}
use aiken/primitive/bytearray
use cardano/address.{Address, Script, VerificationKey}
use cardano/assets.{Value}
use cardano/transaction.{
  InlineDatum, Input, Mint, NoDatum, Output, OutputReference, Redeemer,
  ScriptPurpose, Spend, Transaction, placeholder,
}
use jury/ballots.{pay_status, reveal_status, take_entry, update_ballot}
use jury/commit.{commit_hash}
use jury/datum.{
  CaseFields, JurorFields, ballot_list, case_data, juror_data, with_ballots,
}
use jury/mint_rules
use jury/module1 as m1
use jury/outcome.{add_vote, empty_tally, votes}
use jury/redeemers.{
  Burn, CaseAction, CaseClaim, CaseCommit, CaseReveal, Claim, Close, Commit,
  Exit, Finalize, MintAction, OnCase, OnJuror, RequestExit, Reveal, Slash,
  SpendAction, TopUp,
}
use jury/spend_rules
use jury/tokens.{case_prefix, juror_prefix, token_name}
use jury/types.{
  AuditorWins, Ballot, BallotStatus, ClaimerWins, Committed, Inconclusive,
  JuryParams, Open, OpenVote, Paid, Revealed, Settling, Tally, Vote,
}
use vector_identity/types.{
  Accountable, Active, IdentityDatum, KeyHash, Party, Root,
} as vi

// ## Script hashes and keys

/// J: the jury script hash, policy id and payment credential in these tests.
pub const jury: ByteArray =
  #"1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"

pub const identity_hash: ByteArray =
  #"1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d"

pub const m1_claim: ByteArray =
  #"cacacacacacacacacacacacacacacacacacacacacacacacacacacaca"

pub const m1_challenge: ByteArray =
  #"cbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcb"

pub const treasury_hash: ByteArray =
  #"7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e"

/// Another script hash (not J, not Module-1).
pub const other_script: ByteArray =
  #"e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2"

/// Another token policy.
pub const other_policy: ByteArray =
  #"0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"

pub const accountable_key_hash: ByteArray =
  #"acacacacacacacacacacacacacacacacacacacacacacacacacacacac"

/// The key hash of the refund address (the opener of the case).
pub const refund_key_hash: ByteArray =
  #"5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f"

/// The key hash of the wallet that pays the fees.
pub const wallet_key_hash: ByteArray =
  #"3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e"

/// A 32-byte value that no record or juror uses.
pub const stranger: ByteArray =
  #"5555555555555555555555555555555555555555555555555555555555555555"

pub const prompt_hash: ByteArray =
  #"9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b"

pub const record_hash: ByteArray =
  #"4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d"

// ## Amounts (lovelace)

pub const ap3x: Int = 1_000_000

/// The lovelace of a juror output in most tests.
pub const bond: Int = 300_000_000

/// The lovelace of a new case output.
pub const case_lovelace: Int = 10_000_000

/// The lovelace that a committing juror adds to the case output.
pub const ballot_lovelace: Int = 470_000

// ## Times (POSIX milliseconds)

pub const minute: Int = 60_000

pub const hour: Int = 3_600_000

pub const day: Int = 86_400_000

/// `hi` of the opening transaction of the test case: 2026-11-02T00:00:00Z.
pub const opened_at: Int = 1_793_577_600_000

/// `opened_at + commit_window`.
pub const commit_end: Int = 1_793_584_800_000

/// `commit_end + reveal_window`.
pub const reveal_end: Int = 1_793_592_000_000

/// `challenged_at` of the test challenge: one hour before the opening.
pub const challenged_at: Int = 1_793_574_000_000

/// `resolution_deadline` of the test challenge: one day.
pub const resolution_deadline: Int = 86_400_000

/// `challenged_at + resolution_deadline`.
pub const deadline: Int = 1_793_660_400_000

/// `registered_at` of the test jurors: four days before the opening.
pub const registered_at: Int = 1_793_232_000_000

pub fn range(lo: Int, hi: Int) -> Interval {
  interval.between(lo, hi)
}

/// The window of the opening transaction: `hi == opened_at`.
pub fn open_range() -> Interval {
  range(opened_at - 10 * minute, opened_at)
}

/// The window of a registration: `hi == registered_at`.
pub fn register_range() -> Interval {
  range(registered_at - 10 * minute, registered_at)
}

/// A window in the commit window.
pub fn commit_range() -> Interval {
  range(opened_at + 10 * minute, opened_at + 20 * minute)
}

/// A window in the reveal window.
pub fn reveal_range() -> Interval {
  range(commit_end + 10 * minute, commit_end + 20 * minute)
}

/// A window after the reveal window.
pub fn late_range() -> Interval {
  range(reveal_end + minute, reveal_end + 11 * minute)
}

// ## Parameters

pub fn params() -> JuryParams {
  JuryParams {
    identity_hash,
    m1_claim,
    m1_challenge,
    treasury: treasury(),
    bond_min: 250_000_000,
    slash: 25_000_000,
    juror_floor: 10_000_000,
    eligibility_delay: 259_200_000,
    exit_notice: 259_200_000,
    commit_window: 7_200_000,
    reveal_window: 7_200_000,
    resolve_margin: 7_200_000,
    claim_period: 2_592_000_000,
    quorum: 3,
    majority_num: 2,
    majority_den: 3,
  }
}

// ## Addresses, output references and simple inputs

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

/// The jury address.
pub fn jury_address() -> Address {
  script_address(jury)
}

pub fn treasury() -> Address {
  script_address(treasury_hash)
}

/// The `refund_to` of the test case.
pub fn refund_address() -> Address {
  key_address(refund_key_hash)
}

/// A wallet input at `reference` that pays fees.
pub fn wallet_input(reference: OutputReference) -> Input {
  Input {
    output_reference: reference,
    output: Output {
      address: key_address(wallet_key_hash),
      value: assets.from_lovelace(50_000_000),
      datum: NoDatum,
      reference_script: None,
    },
  }
}

/// The fee input of the spend transactions.
pub fn fee_input() -> Input {
  wallet_input(out_ref(0xfe, 0))
}

/// An output at the jury address with lovelace and one jury token.
pub fn jury_output(name: ByteArray, datum: Data, lovelace: Int) -> Output {
  Output {
    address: jury_address(),
    value: assets.from_lovelace(lovelace) |> assets.add(jury, name, 1),
    datum: InlineDatum(datum),
    reference_script: None,
  }
}

/// `output` with `value` added.
pub fn plus(output: Output, value: Value) -> Output {
  Output { ..output, value: assets.merge(output.value, value) }
}

/// `outputs` with the output at `index` replaced by `output`.
pub fn replace(
  outputs: List<Output>,
  index: Int,
  output: Output,
) -> List<Output> {
  list.indexed_map(
    outputs,
    fn(i, o) {
      if i == index {
        output
      } else {
        o
      }
    },
  )
}

/// A tagged output (§6): at `address`, with the inline datum `tag`, the
/// reference of the spent jury input that it pays.
pub fn tagged_output(
  address: Address,
  tag: OutputReference,
  lovelace: Int,
) -> Output {
  Output {
    address,
    value: assets.from_lovelace(lovelace),
    datum: InlineDatum(tag),
    reference_script: None,
  }
}

/// A plain output at `address` (no datum).
pub fn plain_output(address: Address, lovelace: Int) -> Output {
  Output {
    address,
    value: assets.from_lovelace(lovelace),
    datum: NoDatum,
    reference_script: None,
  }
}

/// The redeemer entry of a spend.
pub fn spend_pair(
  reference: OutputReference,
  action: SpendAction,
) -> Pair<ScriptPurpose, Redeemer> {
  let data: Data = action
  Pair(Spend(reference), data)
}

/// The redeemer entry of the jury mint.
pub fn mint_pair(action: MintAction) -> Pair<ScriptPurpose, Redeemer> {
  let data: Data = action
  Pair(Mint(jury), data)
}

// ## Running the handlers

/// The spend handler for the input `reference`, with its redeemer from
/// `tx.redeemers`, as the ledger runs it.
pub fn spend_ok(tx: Transaction, reference: OutputReference) -> Bool {
  expect Some(redeemer) = pairs.get_first(tx.redeemers, Spend(reference))
  expect action: SpendAction = redeemer
  spend_rules.validate(params(), action, reference, tx)
}

/// The mint handler with the redeemer `action`.
pub fn mint_ok(tx: Transaction, action: MintAction) -> Bool {
  mint_rules.validate(params(), action, jury, tx)
}

// ## Identity records

/// The Ed25519 controller key of juror `n`.
pub fn controller(n: Int) -> ByteArray {
  bytearray.push(
    #"c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
    n,
  )
}

/// The owner of juror `n`: the hash of its controller key.
pub fn owner(n: Int) -> ByteArray {
  blake2b_224(controller(n))
}

/// The DID of juror `n`.
pub fn juror_agent(n: Int) -> ByteArray {
  bytearray.push(
    #"d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1",
    n,
  )
}

pub fn accountable_party() -> Party {
  Party {
    role: Accountable,
    signer: KeyHash { hash: accountable_key_hash },
    organisation: None,
    not_before: registered_at - 30 * day,
    not_after: None,
    rights: 3,
    removal_after: None,
  }
}

/// An active root record with the controller key of juror `n`.
pub fn juror_record(n: Int) -> IdentityDatum {
  IdentityDatum {
    controller: controller(n),
    keys: [],
    parties: [accountable_party()],
    guardian_threshold: 0,
    pending_recovery: None,
    lineage: Root,
    subagent_epoch: 0,
    name: "juror",
    description: "",
    framework: "",
    endpoint: "",
    capabilities: [],
    created_at: registered_at - 30 * day,
    status: Active,
  }
}

/// An identity record reference input.
pub fn record_input(tag: Int, id: ByteArray, record: IdentityDatum) -> Input {
  Input {
    output_reference: out_ref(tag, 0),
    output: Output {
      address: script_address(identity_hash),
      value: assets.from_lovelace(10_000_000)
        |> assets.add(identity_hash, id, 1),
      datum: InlineDatum(record),
      reference_script: None,
    },
  }
}

/// The record of juror `n` as a reference input.
pub fn juror_record_input(n: Int) -> Input {
  record_input(0x90 + n, juror_agent(n), juror_record(n))
}

// ## Jurors

/// The seed of the registration of juror `n`.
pub fn juror_seed(n: Int) -> OutputReference {
  out_ref(0x40 + n, 0)
}

/// The juror token of juror `n`.
pub fn juror_token(n: Int) -> ByteArray {
  token_name(juror_prefix, juror_seed(n))
}

/// The output reference of the juror output of juror `n`.
pub fn juror_ref(n: Int) -> OutputReference {
  out_ref(0x50 + n, 0)
}

/// Juror `n`: registered four days before the opening, no exit, no open
/// entry.
pub fn juror(n: Int) -> JurorFields {
  JurorFields {
    agent: juror_agent(n),
    owner: owner(n),
    prompt_hash,
    registered_at,
    exit_after: None,
    open: [],
  }
}

/// `j` with an open entry for `case` (first).
pub fn with_entry(j: JurorFields, case: ByteArray, end: Int) -> JurorFields {
  JurorFields { ..j, open: [OpenVote { case, reveal_end: end }, ..j.open] }
}

pub fn juror_input(n: Int, j: JurorFields, lovelace: Int) -> Input {
  Input {
    output_reference: juror_ref(n),
    output: jury_output(juror_token(n), juror_data(j), lovelace),
  }
}

/// The juror output of juror `n`.
pub fn juror_output(n: Int, j: JurorFields, lovelace: Int) -> Output {
  jury_output(juror_token(n), juror_data(j), lovelace)
}

/// `j` without its entry for `case`.
pub fn without_entry(j: JurorFields, case: ByteArray) -> JurorFields {
  expect Some((_, rest)) = take_entry(j.open, case)
  JurorFields { ..j, open: rest }
}

// ## Module-1 data

pub const claimer_did: ByteArray =
  #"c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1"

pub const auditor_did: ByteArray =
  #"c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2"

pub const claim_doc_hash: ByteArray =
  #"d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3"

pub const evidence_doc_hash: ByteArray =
  #"d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"

pub const claim_uri: ByteArray =
  "vector-tx:cu8SkBxPLwcOVfFWzxXRMnyXaobj6K09AVMaxgYDNdY"

pub const evidence_uri: ByteArray = "https://example.org/evidence.json"

/// The claim token of the challenged claim.
pub const claim_token: ByteArray =
  #"636c6d5f01010101010101010101010101010101010101010101010101010101"

/// The challenge token.
pub const challenge_token: ByteArray =
  #"63686c5f03030303030303030303030303030303030303030303030303030303"

pub fn claim_datum(state: m1.ClaimState) -> m1.ClaimDatum {
  m1.ClaimDatum {
    claimer_did,
    claimer_credential: VerificationKey(owner(201)),
    claim_hash: claim_doc_hash,
    claim_type: "service-quality",
    storage_uri: claim_uri,
    stake_amount: 50_000_000,
    submitted_at: challenged_at - hour,
    challenge_window: 2 * hour,
    state,
  }
}

pub fn challenge_datum(state: m1.ChallengeState) -> m1.ChallengeDatum {
  m1.ChallengeDatum {
    claim_ref: out_ref(0xca, 0),
    auditor_did,
    auditor_credential: VerificationKey(owner(202)),
    stake_amount: 50_000_000,
    evidence_hash: evidence_doc_hash,
    evidence_uri,
    challenged_at,
    resolution_deadline,
    eligible_jurors: [],
    state,
    claim_token,
  }
}

/// The claim as a reference input: at the claim address, with the claim
/// token.
pub fn claim_input(datum: m1.ClaimDatum) -> Input {
  Input {
    output_reference: out_ref(0xc8, 1),
    output: Output {
      address: script_address(m1_claim),
      value: assets.from_lovelace(50_000_000)
        |> assets.add(m1_claim, claim_token, 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The challenge as a reference input: at the challenge address, with the
/// challenge token.
pub fn challenge_input(datum: m1.ChallengeDatum) -> Input {
  Input {
    output_reference: out_ref(0xc8, 0),
    output: Output {
      address: script_address(m1_challenge),
      value: assets.from_lovelace(50_000_000)
        |> assets.add(m1_challenge, challenge_token, 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The reference inputs of an opening: the challenge (`PendingOracle`) and
/// its claim (`Challenged`).
pub fn m1_refs() -> List<Input> {
  [
    challenge_input(challenge_datum(m1.PendingOracle)),
    claim_input(claim_datum(m1.Challenged)),
  ]
}

// ## Cases

/// The seed of the opening of the test case.
pub fn case_seed() -> OutputReference {
  out_ref(0x30, 0)
}

/// The case token of the test case.
pub fn case_token() -> ByteArray {
  token_name(case_prefix, case_seed())
}

/// The case token of a second case.
pub fn case_token_b() -> ByteArray {
  token_name(case_prefix, out_ref(0x31, 0))
}

/// The output reference of the case output.
pub fn case_ref() -> OutputReference {
  out_ref(0x60, 0)
}

/// The output reference of the output of the second case.
pub fn case_ref_b() -> OutputReference {
  out_ref(0x61, 0)
}

/// The ballots `list` as `Data`.
pub fn ballots_data(list: List<Ballot>) -> Data {
  let data: Data = list
  data
}

/// The test case at its opening: no fee, no ballot.
pub fn new_case() -> CaseFields {
  CaseFields {
    challenge: challenge_token,
    claim: claim_token,
    claimer: claimer_did,
    auditor: auditor_did,
    challenge_out: out_ref(0xc8, 0),
    claim_out: out_ref(0xc8, 1),
    claim_hash: claim_doc_hash,
    evidence_hash: evidence_doc_hash,
    deadline,
    opened_at,
    commit_end,
    reveal_end,
    refund_to: refund_address(),
    fee: 0,
    ballots: ballots_data([]),
    tally: empty_tally,
    state: Open,
  }
}

/// A case with these ballots and this tally.
pub fn case_with(ballots: List<Ballot>, tally: Tally) -> CaseFields {
  CaseFields { ..with_ballots(new_case(), ballots), tally }
}

/// `c` with the state `Settling { share }`.
pub fn settling(c: CaseFields, share: Int) -> CaseFields {
  CaseFields { ..c, state: Settling { share } }
}

/// The case input of the case `name` at `reference`.
pub fn case_input_at(
  reference: OutputReference,
  name: ByteArray,
  c: CaseFields,
  lovelace: Int,
) -> Input {
  Input {
    output_reference: reference,
    output: jury_output(name, case_data(c), lovelace),
  }
}

/// The input of the test case.
pub fn case_input(c: CaseFields, lovelace: Int) -> Input {
  case_input_at(case_ref(), case_token(), c, lovelace)
}

/// The output of the test case.
pub fn case_output(c: CaseFields, lovelace: Int) -> Output {
  jury_output(case_token(), case_data(c), lovelace)
}

// ## Ballots

/// The salt of juror `n`.
pub fn salt(n: Int) -> ByteArray {
  bytearray.push(
    #"5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a",
    n,
  )
}

/// The commitment of juror `n` to `vote` in the case `case`.
pub fn commitment(case: ByteArray, n: Int, vote: Vote) -> ByteArray {
  commit_hash(case, juror_agent(n), vote, salt(n), record_hash)
}

/// The ballot of juror `n` in the test case, committed to `vote`, with the
/// status `status`.
pub fn ballot(n: Int, vote: Vote, status: BallotStatus) -> Ballot {
  Ballot {
    agent: juror_agent(n),
    juror: juror_token(n),
    commit: commitment(case_token(), n, vote),
    status,
  }
}

pub fn committed(n: Int, vote: Vote) -> Ballot {
  ballot(n, vote, Committed)
}

/// A revealed ballot: its commit is empty (§6.10 rule 6).
pub fn revealed(n: Int, vote: Vote) -> Ballot {
  Ballot { ..ballot(n, vote, Revealed { vote }), commit: #"" }
}

/// A paid ballot: its commit stays empty (§6.12 rule 4).
pub fn paid(n: Int, vote: Vote) -> Ballot {
  Ballot { ..ballot(n, vote, Paid { vote }), commit: #"" }
}

/// `count` committed ballots of the jurors 100, 101, ... (other jurors).
pub fn other_ballots(count: Int) -> List<Ballot> {
  if count <= 0 {
    []
  } else {
    [committed(99 + count, ClaimerWins), ..other_ballots(count - 1)]
  }
}

// ## Commit (§6.4, §6.9)

/// The honest juror output of a commit of juror `n` to the test case.
pub fn commit_juror_out(
  n: Int,
  j: JurorFields,
  lovelace: Int,
  c: CaseFields,
) -> Output {
  juror_output(n, with_entry(j, case_token(), c.reveal_end), lovelace)
}

/// The honest case output of a commit of juror `n` (DID `agent`).
pub fn commit_case_out(
  n: Int,
  agent: ByteArray,
  c: CaseFields,
  lovelace: Int,
  commit: ByteArray,
) -> Output {
  let new = Ballot { agent, juror: juror_token(n), commit, status: Committed }
  case_output(with_ballots(c, [new, ..ballot_list(c)]), lovelace)
}

/// A commit transaction with the given outputs.
pub fn commit_tx_with(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  commit: ByteArray,
  outputs: List<Output>,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [
      case_input(c, c_lovelace),
      juror_input(n, j, j_lovelace),
      fee_input(),
    ],
    reference_inputs: [juror_record_input(n)],
    outputs,
    redeemers: [
      spend_pair(
        case_ref(),
        OnCase(CaseCommit { juror: juror_token(n), commit }),
      ),
      spend_pair(juror_ref(n), OnJuror(Commit { case: case_token() })),
    ],
    extra_signatories: [owner(n)],
    validity_range: commit_range(),
  }
}

/// The honest commit of juror `n` (datum `j`) to the test case `c`. The
/// juror adds `ballot_lovelace` to the case output.
pub fn commit_tx(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  commit: ByteArray,
) -> Transaction {
  commit_tx_with(
    n,
    j,
    j_lovelace,
    c,
    c_lovelace,
    commit,
    [
      commit_juror_out(n, j, j_lovelace, c),
      commit_case_out(n, j.agent, c, c_lovelace + ballot_lovelace, commit),
    ],
  )
}

/// The base commit: juror 1 commits to `AuditorWins` in the new test case.
pub fn base_commit() -> Transaction {
  commit_tx(
    1,
    juror(1),
    bond,
    new_case(),
    case_lovelace,
    commitment(case_token(), 1, AuditorWins),
  )
}

// ## Reveal (§6.5, §6.10)

/// Juror `n` with its open entry for the test case.
pub fn committed_juror(n: Int) -> JurorFields {
  with_entry(juror(n), case_token(), reveal_end)
}

/// The honest outputs of a reveal of juror `n`: the juror output without the
/// entry, the case output with the ballot `Revealed { vote }` and the tally.
pub fn reveal_outputs(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  vote: Vote,
) -> List<Output> {
  expect Some((_, updated)) =
    update_ballot(ballot_list(c), juror_token(n), reveal_status(vote))
  [
    juror_output(n, without_entry(j, case_token()), j_lovelace),
    case_output(
      CaseFields { ..with_ballots(c, updated), tally: add_vote(c.tally, vote) },
      c_lovelace,
    ),
  ]
}

/// A reveal transaction with the case action `action` and these outputs.
pub fn reveal_tx_with(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  action: CaseAction,
  outputs: List<Output>,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [
      case_input(c, c_lovelace),
      juror_input(n, j, j_lovelace),
      fee_input(),
    ],
    outputs,
    redeemers: [
      spend_pair(case_ref(), OnCase(action)),
      spend_pair(juror_ref(n), OnJuror(Reveal { case: case_token() })),
    ],
    extra_signatories: [owner(n)],
    validity_range: reveal_range(),
  }
}

/// The reveal action of juror `n` for `vote` with its salt and record.
pub fn reveal_action(n: Int, vote: Vote) -> CaseAction {
  CaseReveal { juror: juror_token(n), vote, salt: salt(n), record_hash }
}

/// The honest reveal of juror `n` (committed to `vote`) in the case `c`.
pub fn reveal_tx(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  vote: Vote,
) -> Transaction {
  reveal_tx_with(
    n,
    j,
    j_lovelace,
    c,
    c_lovelace,
    reveal_action(n, vote),
    reveal_outputs(n, j, j_lovelace, c, c_lovelace, vote),
  )
}

/// The case of the base reveal: juror 1 committed to `AuditorWins`, juror 2
/// committed and juror 3 revealed `ClaimerWins`.
pub fn reveal_case() -> CaseFields {
  case_with(
    [
      committed(2, ClaimerWins),
      committed(1, AuditorWins),
      revealed(3, ClaimerWins),
    ],
    Tally { claimer: 1, auditor: 0, inconclusive: 0 },
  )
}

/// The base reveal: juror 1 reveals `AuditorWins`.
pub fn base_reveal() -> Transaction {
  reveal_tx(
    1,
    committed_juror(1),
    bond,
    reveal_case(),
    case_lovelace,
    AuditorWins,
  )
}

// ## Claim (§6.6, §6.12)

/// The share of the base claim.
pub const share: Int = 5_000_000

/// The case of the base claim: fee 15 AP3X, three revealed ballots, share
/// 5 AP3X.
pub fn claim_case() -> CaseFields {
  settling(
    CaseFields {
      ..case_with(
        [
          revealed(1, AuditorWins),
          revealed(2, AuditorWins),
          revealed(3, ClaimerWins),
        ],
        Tally { claimer: 1, auditor: 2, inconclusive: 0 },
      ),
      fee: 15_000_000,
    },
    share,
  )
}

/// The lovelace of the case output of the base claim.
pub const claim_case_lovelace: Int = 25_000_000

/// The honest outputs of a claim of juror `n`: the juror output with
/// `share` more, the case output with `share` less and the ballot `Paid`.
pub fn claim_outputs(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  amount: Int,
) -> List<Output> {
  expect Some((_, updated)) =
    update_ballot(ballot_list(c), juror_token(n), pay_status)
  [
    juror_output(n, j, j_lovelace + amount),
    case_output(with_ballots(c, updated), c_lovelace - amount),
  ]
}

/// A claim transaction with these outputs.
pub fn claim_tx_with(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  outputs: List<Output>,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [
      case_input(c, c_lovelace),
      juror_input(n, j, j_lovelace),
      fee_input(),
    ],
    outputs,
    redeemers: [
      spend_pair(case_ref(), OnCase(CaseClaim { juror: juror_token(n) })),
      spend_pair(juror_ref(n), OnJuror(Claim { case: case_token() })),
    ],
    extra_signatories: [owner(n)],
    validity_range: range(reveal_end + hour, reveal_end + 2 * hour),
  }
}

/// The honest claim of juror `n` with the share `amount`.
pub fn claim_tx(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  c: CaseFields,
  c_lovelace: Int,
  amount: Int,
) -> Transaction {
  claim_tx_with(
    n,
    j,
    j_lovelace,
    c,
    c_lovelace,
    claim_outputs(n, j, j_lovelace, c, c_lovelace, amount),
  )
}

/// The base claim: juror 1 claims its share.
pub fn base_claim() -> Transaction {
  claim_tx(1, juror(1), bond, claim_case(), claim_case_lovelace, share)
}

// ## Slash (§6.7)

/// A slash transaction of juror `n` for the case `case`, with these outputs.
pub fn slash_tx_with(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  case: ByteArray,
  outputs: List<Output>,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [juror_input(n, j, j_lovelace), fee_input()],
    outputs,
    redeemers: [spend_pair(juror_ref(n), OnJuror(Slash { case }))],
    validity_range: late_range(),
  }
}

/// The honest slash: the juror output loses `slash` and the entry, the
/// treasury gets `min(slash, lovelace - juror_floor)` in an output tagged
/// with the juror input, when that amount is positive.
pub fn slash_tx(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  case: ByteArray,
) -> Transaction {
  let amount = slash_amount(j_lovelace)
  let treasury_out =
    if amount > 0 {
      [tagged_output(treasury(), juror_ref(n), amount)]
    } else {
      []
    }
  slash_tx_with(
    n,
    j,
    j_lovelace,
    case,
    [
      juror_output(n, without_entry(j, case), j_lovelace - amount),
      ..treasury_out
    ],
  )
}

/// The slash amount of §6.7 for a juror output with `lovelace`.
pub fn slash_amount(lovelace: Int) -> Int {
  let p = params()
  if p.slash < lovelace - p.juror_floor {
    p.slash
  } else {
    lovelace - p.juror_floor
  }
}

/// The base slash: juror 1 did not reveal in the test case.
pub fn base_slash() -> Transaction {
  slash_tx(1, committed_juror(1), bond, case_token())
}

// ## RequestExit, Exit, TopUp (§6.8)

/// A transaction of juror `n` with the action `action` and these outputs.
pub fn juror_tx(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  action: SpendAction,
  outputs: List<Output>,
  validity: Interval,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [juror_input(n, j, j_lovelace), fee_input()],
    outputs,
    redeemers: [spend_pair(juror_ref(n), action)],
    extra_signatories: [owner(n)],
    validity_range: validity,
  }
}

/// The window of the base exit request.
pub fn request_range() -> Interval {
  range(opened_at - 10 * minute, opened_at)
}

/// The honest exit request of juror `n`.
pub fn request_exit_tx(n: Int, j: JurorFields, j_lovelace: Int) -> Transaction {
  juror_tx(
    n,
    j,
    j_lovelace,
    OnJuror(RequestExit),
    [
      juror_output(
        n,
        JurorFields { ..j, exit_after: Some(opened_at + params().exit_notice) },
        j_lovelace,
      ),
    ],
    request_range(),
  )
}

/// The time after which the base juror may exit.
pub const exit_after: Int = 1_793_836_800_000

/// The honest exit of juror `n`: the token is burned and the value goes to
/// the owner.
pub fn exit_tx(n: Int, j: JurorFields, j_lovelace: Int) -> Transaction {
  Transaction {
    ..juror_tx(
      n,
      j,
      j_lovelace,
      OnJuror(Exit),
      [plain_output(key_address(owner(n)), j_lovelace)],
      range(exit_after, exit_after + 10 * minute),
    ),
    mint: assets.from_asset(jury, juror_token(n), -1),
    redeemers: [spend_pair(juror_ref(n), OnJuror(Exit)), mint_pair(Burn)],
  }
}

/// Juror `n` after its exit request.
pub fn leaving_juror(n: Int) -> JurorFields {
  JurorFields { ..juror(n), exit_after: Some(exit_after) }
}

/// The honest top-up of juror `n` with `amount`.
pub fn top_up_tx(
  n: Int,
  j: JurorFields,
  j_lovelace: Int,
  amount: Int,
) -> Transaction {
  juror_tx(
    n,
    j,
    j_lovelace,
    OnJuror(TopUp),
    [juror_output(n, j, j_lovelace + amount)],
    range(opened_at, opened_at + 10 * minute),
  )
}

// ## Finalize (§6.11) and Close (§6.13)

/// The honest finalization of the case `c`.
pub fn finalize_tx(c: CaseFields, c_lovelace: Int, result: Vote) -> Transaction {
  let n = votes(c.tally)
  if c.fee == 0 || n == 0 || c.fee < n {
    burn_tx(
      c,
      c_lovelace,
      Finalize { result },
      [tagged_output(c.refund_to, case_ref(), c_lovelace)],
      late_range(),
    )
  } else {
    Transaction {
      ..placeholder,
      inputs: [case_input(c, c_lovelace), fee_input()],
      outputs: [
        case_output(
          CaseFields { ..c, state: Settling { share: c.fee / n } },
          c_lovelace,
        ),
      ],
      redeemers: [spend_pair(case_ref(), OnCase(Finalize { result }))],
      validity_range: late_range(),
    }
  }
}

/// A transaction that spends the test case with `action` and burns its
/// token.
pub fn burn_tx(
  c: CaseFields,
  c_lovelace: Int,
  action: CaseAction,
  outputs: List<Output>,
  validity: Interval,
) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [case_input(c, c_lovelace), fee_input()],
    outputs,
    mint: assets.from_asset(jury, case_token(), -1),
    redeemers: [spend_pair(case_ref(), OnCase(action)), mint_pair(Burn)],
    validity_range: validity,
  }
}

/// The case of the base finalization: three revealed votes, no fee.
pub fn finalize_case() -> CaseFields {
  case_with(
    [
      revealed(1, AuditorWins),
      revealed(2, AuditorWins),
      revealed(3, ClaimerWins),
      committed(4, Inconclusive),
    ],
    Tally { claimer: 1, auditor: 2, inconclusive: 0 },
  )
}

/// The honest close of the case `c` in the window `validity`.
pub fn close_tx(
  c: CaseFields,
  c_lovelace: Int,
  validity: Interval,
) -> Transaction {
  burn_tx(
    c,
    c_lovelace,
    Close,
    [tagged_output(c.refund_to, case_ref(), c_lovelace)],
    validity,
  )
}

/// The case of the base close: all revealed ballots are paid.
pub fn paid_case() -> CaseFields {
  settling(
    CaseFields {
      ..case_with(
        [
          paid(1, AuditorWins),
          paid(2, AuditorWins),
          paid(3, ClaimerWins),
          committed(4, Inconclusive),
        ],
        Tally { claimer: 1, auditor: 2, inconclusive: 0 },
      ),
      fee: 15_000_000,
    },
    share,
  )
}

// ## Opening (§6.2) and registration (§6.1)

/// The honest opening of the case `c` with `lovelace` in the case output.
pub fn open_tx(c: CaseFields, lovelace: Int, refs: List<Input>) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [wallet_input(case_seed())],
    reference_inputs: refs,
    outputs: [case_output(c, lovelace)],
    mint: assets.from_asset(jury, case_token(), 1),
    redeemers: [mint_pair(redeemers.Open { seed: case_seed() })],
    validity_range: open_range(),
  }
}

/// The base opening: the new test case, no fee, 10 AP3X.
pub fn base_open() -> Transaction {
  open_tx(new_case(), case_lovelace, m1_refs())
}

/// Juror `n` in its registration output.
pub fn new_juror(n: Int) -> JurorFields {
  juror(n)
}

/// The honest registration of juror `n` with the datum `j` and `lovelace`.
pub fn register_tx(n: Int, j: JurorFields, lovelace: Int) -> Transaction {
  Transaction {
    ..placeholder,
    inputs: [wallet_input(juror_seed(n))],
    reference_inputs: [juror_record_input(n)],
    outputs: [juror_output(n, j, lovelace)],
    mint: assets.from_asset(jury, juror_token(n), 1),
    redeemers: [mint_pair(redeemers.Register { seed: juror_seed(n) })],
    extra_signatories: [owner(n)],
    validity_range: register_range(),
  }
}

/// The base registration of juror 1 with the minimum bond.
pub fn base_register() -> Transaction {
  register_tx(1, new_juror(1), params().bond_min)
}

// ## Combining transactions

/// The parts of `a` and `b` in one transaction, with the window of `a`.
pub fn merge(a: Transaction, b: Transaction) -> Transaction {
  Transaction {
    ..a,
    inputs: list.concat(a.inputs, b.inputs),
    reference_inputs: list.concat(a.reference_inputs, b.reference_inputs),
    outputs: list.concat(a.outputs, b.outputs),
    redeemers: list.concat(a.redeemers, b.redeemers),
    extra_signatories: list.concat(a.extra_signatories, b.extra_signatories),
    mint: assets.merge(a.mint, b.mint),
  }
}

/// `tx` with the redeemer of `reference` replaced by `action`.
pub fn with_redeemer(
  tx: Transaction,
  reference: OutputReference,
  action: SpendAction,
) -> Transaction {
  Transaction {
    ..tx,
    redeemers: [
      spend_pair(reference, action),
      ..pairs.delete_all(tx.redeemers, Spend(reference))
    ],
  }
}
