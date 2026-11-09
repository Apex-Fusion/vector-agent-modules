//// Views of the jury datum for the handlers.
////
//// `JuryDatum` (jury/types) is the on-chain format. The handlers read a datum
//// through these single-constructor views, so that they can use field access
//// and record update:
////
//// - `JurorFields` has the 6 fields of `JuryDatum.Juror`. Both are
////   `Constr 0` with the same fields, so the encodings are the same.
//// - `CaseFields` has the 17 fields of `JuryDatum.Case`, with `ballots` as
////   raw `Data`. `case_fields` and `case_data` change the constructor index
////   between 1 (`Case`) and 0 (the view). The ballots are decoded only by
////   the rules that read them (`ballot_list`), so `Finalize` does not decode
////   them (§9).
////
//// Every jury token output holds a valid `JuryDatum`: the mint handler
//// decodes the datum of each new output in full (§6.1, §6.2), and each
//// continuing datum is a valid datum with typed changes (§6.4 to §6.12).

use aiken/builtin
use cardano/address.{Address}
use cardano/transaction.{OutputReference}
use jury/types.{Ballot, CaseState, OpenVote, Tally}

/// The fields of `JuryDatum.Juror`.
pub type JurorFields {
  agent: ByteArray,
  owner: ByteArray,
  prompt_hash: ByteArray,
  registered_at: Int,
  exit_after: Option<Int>,
  open: List<OpenVote>,
}

/// The fields of `JuryDatum.Case`; `ballots` is a `List<Ballot>` as `Data`.
pub type CaseFields {
  challenge: ByteArray,
  claim: ByteArray,
  challenge_out: OutputReference,
  claim_out: OutputReference,
  claimer: ByteArray,
  auditor: ByteArray,
  claim_hash: ByteArray,
  evidence_hash: ByteArray,
  deadline: Int,
  opened_at: Int,
  commit_end: Int,
  reveal_end: Int,
  refund_to: Address,
  fee: Int,
  ballots: Data,
  tally: Tally,
  state: CaseState,
}

/// Decode a `Juror` datum (`Constr 0` with the 6 fields, all decoded). Fails
/// for any other datum.
pub fn juror_fields(data: Data) -> JurorFields {
  expect builtin.fst_pair(builtin.un_constr_data(data)) == 0
  expect j: JurorFields = data
  j
}

/// The `Juror` datum of `j`.
pub fn juror_data(j: JurorFields) -> Data {
  let data: Data = j
  data
}

/// Decode a `Case` datum (`Constr 1` with the 17 fields). All fields are
/// decoded except the ballots. Fails for any other datum.
pub fn case_fields(data: Data) -> CaseFields {
  let pair = builtin.un_constr_data(data)
  expect builtin.fst_pair(pair) == 1
  expect c: CaseFields = builtin.constr_data(0, builtin.snd_pair(pair))
  c
}

/// The `Case` datum of `c`.
pub fn case_data(c: CaseFields) -> Data {
  let data: Data = c
  builtin.constr_data(1, builtin.snd_pair(builtin.un_constr_data(data)))
}

/// The ballots of `c`, decoded. Fails when they are not a `List<Ballot>`.
pub fn ballot_list(c: CaseFields) -> List<Ballot> {
  expect decoded: List<Ballot> = c.ballots
  decoded
}

/// `c` with the ballots `new`.
pub fn with_ballots(c: CaseFields, new: List<Ballot>) -> CaseFields {
  let data: Data = new
  CaseFields { ..c, ballots: data }
}

/// True when `c` has no ballot.
pub fn no_ballots(c: CaseFields) -> Bool {
  when builtin.un_list_data(c.ballots) is {
    [] -> True
    _ -> False
  }
}
