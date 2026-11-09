//// List helpers: the open entries of a juror and the ballots of a case
//// (SPEC §5.2, §5.3).

use aiken/collection/list
use jury/types.{Ballot, Committed, OpenVote, Paid, Revealed, Vote}

/// True when `open` has an entry for the case `case`.
pub fn has_entry(open: List<OpenVote>, case: ByteArray) -> Bool {
  list.any(open, fn(entry) { entry.case == case })
}

/// When `open` has exactly one entry for the case `case`: that entry, and the
/// other entries in their order. Else `None`.
pub fn take_entry(
  open: List<OpenVote>,
  case: ByteArray,
) -> Option<(OpenVote, List<OpenVote>)> {
  when open is {
    [] -> None
    [entry, ..rest] ->
      if entry.case == case {
        if has_entry(rest, case) {
          None
        } else {
          Some((entry, rest))
        }
      } else {
        when take_entry(rest, case) is {
          None -> None
          Some((found, others)) -> Some((found, [entry, ..others]))
        }
      }
  }
}

/// True when a ballot has the DID `agent`.
pub fn has_agent(ballots: List<Ballot>, agent: ByteArray) -> Bool {
  list.any(ballots, fn(ballot) { ballot.agent == agent })
}

/// When exactly one ballot has the juror token `juror` and `next` gives a new
/// ballot for it: that ballot, and the list with the new ballot at its place.
/// Else `None`.
pub fn update_ballot(
  ballots: List<Ballot>,
  juror: ByteArray,
  next: fn(Ballot) -> Option<Ballot>,
) -> Option<(Ballot, List<Ballot>)> {
  when ballots is {
    [] -> None
    [ballot, ..rest] ->
      if ballot.juror == juror {
        if list.any(rest, fn(other) { other.juror == juror }) {
          None
        } else {
          when next(ballot) is {
            None -> None
            Some(new) -> Some((ballot, [new, ..rest]))
          }
        }
      } else {
        when update_ballot(rest, juror, next) is {
          None -> None
          Some((found, updated)) -> Some((found, [ballot, ..updated]))
        }
      }
  }
}

/// The reveal of §6.10 rule 6: a `Committed` ballot gets the status
/// `Revealed { vote }` and an empty commit, so that a reveal never makes the
/// case datum larger.
pub fn reveal_status(vote: Vote) -> fn(Ballot) -> Option<Ballot> {
  fn(ballot: Ballot) {
    when ballot.status is {
      Committed ->
        Some(Ballot { ..ballot, commit: #"", status: Revealed { vote } })
      _ -> None
    }
  }
}

/// The payment of §6.12 rule 4: a `Revealed { vote }` ballot gets the status
/// `Paid { vote }`; the other fields stay.
pub fn pay_status(ballot: Ballot) -> Option<Ballot> {
  when ballot.status is {
    Revealed { vote } -> Some(Ballot { ..ballot, status: Paid { vote } })
    _ -> None
  }
}

/// True when the ballot is `Revealed`.
pub fn is_revealed(ballot: Ballot) -> Bool {
  when ballot.status is {
    Revealed { .. } -> True
    _ -> False
  }
}
