//// Data types of the Module-1 jury: SPEC §5 (`Module-1/jury/SPEC.md`).
////
//// The constructor indices and the field order are the on-chain format. A
//// record type is `Constr 0` with its fields in the order shown. The
//// constructors of a sum type are numbered from 0 in the order shown. Do not
//// reorder or insert fields or constructors.
////
//// The redeemers (§5.4, §5.5) are in `jury/redeemers`, because
//// `CaseState.Open` and `MintAction.Open` have the same name and Aiken needs
//// one module for each. `jury/module1` has the Module-1 types (§5.7).

use cardano/address.{Address}
use cardano/transaction.{OutputReference}

/// A vote (§5.1). The indices are those of the Module-1 `Verdict`.
pub type Vote {
  /// Byte `00` in the commit preimage.
  ClaimerWins
  /// Byte `01` in the commit preimage.
  AuditorWins
  /// Byte `02` in the commit preimage.
  Inconclusive
}

/// A commitment of a juror that is not revealed yet (§5.2).
pub type OpenVote {
  /// Case token name (32 bytes).
  case: ByteArray,
  /// The `reveal_end` of the case.
  reveal_end: Int,
}

/// The status of a ballot (§5.3).
pub type BallotStatus {
  Committed
  Revealed { vote: Vote }
  Paid { vote: Vote }
}

/// One ballot per committed juror (§5.3).
pub type Ballot {
  /// The juror DID (32 bytes).
  agent: ByteArray,
  /// The juror token name (32 bytes).
  juror: ByteArray,
  /// The commitment (32 bytes, §7).
  commit: ByteArray,
  status: BallotStatus,
}

/// The revealed votes of a case (§5.3).
pub type Tally {
  claimer: Int,
  auditor: Int,
  inconclusive: Int,
}

/// The state of a case (§5.3).
pub type CaseState {
  Open
  Settling { share: Int }
}

/// The inline datum of every jury output (§5.2, §5.3). The two variants are
/// flat: `Juror` is `Constr 0` with 6 fields, `Case` is `Constr 1` with 17
/// fields.
pub type JuryDatum {
  /// Constr 0: a juror output.
  Juror {
    /// The juror DID (32 bytes).
    agent: ByteArray,
    /// Verification key hash that signs the juror's actions (28 bytes).
    owner: ByteArray,
    /// blake2b-256 of the juror prompt document (32 bytes).
    prompt_hash: ByteArray,
    /// `hi` of the registration transaction.
    registered_at: Int,
    /// `Some(t)` after an exit request.
    exit_after: Option<Int>,
    /// Commitments that are not revealed yet. New entries come first.
    open: List<OpenVote>,
  }
  /// Constr 1: a case output.
  Case {
    /// Module-1 challenge token name.
    challenge: ByteArray,
    /// Module-1 claim token name.
    claim: ByteArray,
    /// The challenge output that the opening transaction read.
    challenge_out: OutputReference,
    /// The claim output that the opening transaction read.
    claim_out: OutputReference,
    /// Claimer DID.
    claimer: ByteArray,
    /// Auditor DID.
    auditor: ByteArray,
    claim_hash: ByteArray,
    evidence_hash: ByteArray,
    /// `challenged_at + resolution_deadline` of the challenge.
    deadline: Int,
    /// `hi` of the opening transaction.
    opened_at: Int,
    /// `opened_at + commit_window`.
    commit_end: Int,
    /// `commit_end + reveal_window`.
    reveal_end: Int,
    /// Receives the case lovelace when the case closes, except the paid fee
    /// shares.
    refund_to: Address,
    /// Fee for the revealing jurors, declared at opening.
    fee: Int,
    /// One ballot per committed juror. New ballots come first.
    ballots: List<Ballot>,
    tally: Tally,
    state: CaseState,
  }
}

/// The validator parameter (§3, §5.6). The deploy tool checks the rules of
/// §3 for the values; the validator does not.
pub type JuryParams {
  /// Identity registry script hash and policy id.
  identity_hash: ByteArray,
  /// Module-1 claim script hash, also the claim token policy.
  m1_claim: ByteArray,
  /// Module-1 challenge script hash, also the challenge token policy.
  m1_challenge: ByteArray,
  /// Receives the slashed bond.
  treasury: Address,
  /// Minimum bond to register and to commit (lovelace).
  bond_min: Int,
  /// Most lovelace lost per unrevealed commitment.
  slash: Int,
  /// Lovelace that always stays in a juror output.
  juror_floor: Int,
  /// Milliseconds from registration to the first commit.
  eligibility_delay: Int,
  /// Milliseconds from an exit request to the exit.
  exit_notice: Int,
  /// Milliseconds.
  commit_window: Int,
  /// Milliseconds.
  reveal_window: Int,
  /// Milliseconds that must remain after the reveal window before the
  /// challenge deadline.
  resolve_margin: Int,
  /// Milliseconds after the reveal window for fee claims.
  claim_period: Int,
  /// Minimum number of revealed votes for a side to win.
  quorum: Int,
  majority_num: Int,
  majority_den: Int,
}
