//// The result rule (SPEC §8) and the tally.

use jury/types.{AuditorWins, ClaimerWins, Inconclusive, JuryParams, Tally, Vote}

/// The tally of a new case.
pub const empty_tally: Tally = Tally { claimer: 0, auditor: 0, inconclusive: 0 }

/// The number of revealed votes `n`.
pub fn votes(tally: Tally) -> Int {
  tally.claimer + tally.auditor + tally.inconclusive
}

/// `outcome` of §8: a side wins when `n >= quorum` and the side has at least
/// `majority_num / majority_den` of the `n` revealed votes. `ClaimerWins` is
/// tested first. Else `Inconclusive`.
pub fn outcome(params: JuryParams, tally: Tally) -> Vote {
  let n = votes(tally)
  if n < params.quorum {
    Inconclusive
  } else if tally.claimer * params.majority_den >= n * params.majority_num {
    ClaimerWins
  } else if tally.auditor * params.majority_den >= n * params.majority_num {
    AuditorWins
  } else {
    Inconclusive
  }
}

/// `tally` with the field of `vote` plus 1.
pub fn add_vote(tally: Tally, vote: Vote) -> Tally {
  when vote is {
    ClaimerWins -> Tally { ..tally, claimer: tally.claimer + 1 }
    AuditorWins -> Tally { ..tally, auditor: tally.auditor + 1 }
    Inconclusive -> Tally { ..tally, inconclusive: tally.inconclusive + 1 }
  }
}
