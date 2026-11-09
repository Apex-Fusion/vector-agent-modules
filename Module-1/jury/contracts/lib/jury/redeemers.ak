//// Redeemers of the Module-1 jury: SPEC §5.4 and §5.5.
////
//// The constructor order is part of the on-chain format (Plutus Data tags
//// 121, 122, ... in the order shown). Do not reorder or insert constructors.

use cardano/transaction.{OutputReference}
use jury/types.{Vote}

/// Mint redeemer (§5.4).
pub type MintAction {
  /// Mint one juror token (§6.1).
  Register { seed: OutputReference }
  /// Mint one case token (§6.2).
  Open { seed: OutputReference }
  /// Burn jury tokens (§6.3).
  Burn
}

/// Spend redeemer (§5.5).
pub type SpendAction {
  /// Constr 0: an action on a juror output.
  OnJuror(JurorAction)
  /// Constr 1: an action on a case output.
  OnCase(CaseAction)
}

/// Actions on a juror output (§6.4 to §6.8). `case` is a case token name.
pub type JurorAction {
  Commit { case: ByteArray }
  Reveal { case: ByteArray }
  Claim { case: ByteArray }
  Slash { case: ByteArray }
  RequestExit
  Exit
  TopUp
}

/// Actions on a case output (§6.9 to §6.13). `juror` is a juror token name.
pub type CaseAction {
  CaseCommit { juror: ByteArray, commit: ByteArray }
  CaseReveal {
    juror: ByteArray,
    vote: Vote,
    salt: ByteArray,
    record_hash: ByteArray,
  }
  Finalize { result: Vote }
  CaseClaim { juror: ByteArray }
  Close
}
