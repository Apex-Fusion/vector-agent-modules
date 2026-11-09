"""Pure rules of the jury: token names (SPEC §4), the commit hash (§7), the
result rule (§8), settlement (§6.11, §9), the juror invariant and the
eligibility checks that the validator applies (§6.4, §6.9).

The functions take datums of ``vector_jury.types`` and integers. They have
no network access. Each ``*_problems`` function returns the list of the
broken rules as short English sentences (empty when the action is allowed),
so a builder can refuse before it builds and a service can log the reason.
"""

import hashlib
import os
from typing import List, Optional, Sequence, Tuple, Union

from pycardano import TransactionInput

from .types import (Ballot, Case, Committed, Juror, JuryParams, Open, Revealed, Settling, Some, Tally, option_value,
                    vote, vote_index)

#: Token name prefixes (SPEC §4).
JUROR_PREFIX = b"jrr_"
CASE_PREFIX = b"cas_"

#: 1 AP3X in lovelace.
AP3X = 1_000_000


def blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(bytes(data), digest_size=32).digest()


def blake2b_224(data: bytes) -> bytes:
    return hashlib.blake2b(bytes(data), digest_size=28).digest()


# ── Token names (§4) ─────────────────────────────────────────────────────────


def _uint(value: int) -> bytes:
    """CBOR head of an unsigned integer, as Plutus ``serialiseData`` writes it."""
    if value < 0:
        raise ValueError("an output index is not negative")
    if value < 24:
        return bytes([value])
    for size, marker in ((1, 24), (2, 25), (4, 26), (8, 27)):
        if value < 1 << (8 * size):
            return bytes([marker]) + value.to_bytes(size, "big")
    raise ValueError("an output index fits 64 bits")


def output_reference_cbor(tx_id: bytes, index: int) -> bytes:
    """``serialiseData(OutputReference { tx_id, index })``: ``d8799f 5820
    <tx_id> <index> ff``."""
    tx_id = bytes(tx_id)
    if len(tx_id) != 32:
        raise ValueError("a transaction id has 32 bytes")
    return b"\xd8\x79\x9f\x58\x20" + tx_id + _uint(int(index)) + b"\xff"


def _seed(seed: Union[TransactionInput, Tuple[bytes, int], str]) -> Tuple[bytes, int]:
    if isinstance(seed, TransactionInput):
        return bytes(seed.transaction_id), int(seed.index)
    if isinstance(seed, str):
        tx_id, _, index = seed.partition("#")
        return bytes.fromhex(tx_id), int(index)
    tx_id, index = seed
    return (bytes.fromhex(tx_id) if isinstance(tx_id, str) else bytes(tx_id)), int(index)


def token_name(prefix: bytes, seed) -> bytes:
    """``prefix ++ blake2b_256(serialise(seed))[0:28]`` (32 bytes). ``seed`` is
    a TransactionInput, ``(tx_id, index)`` or ``"<tx id>#<index>"``."""
    if len(prefix) != 4:
        raise ValueError("a token name prefix has 4 bytes")
    tx_id, index = _seed(seed)
    return bytes(prefix) + blake2b_256(output_reference_cbor(tx_id, index))[:28]


def juror_token_name(seed) -> bytes:
    return token_name(JUROR_PREFIX, seed)


def case_token_name(seed) -> bytes:
    return token_name(CASE_PREFIX, seed)


def is_juror_token(name: bytes) -> bool:
    return len(name) == 32 and bytes(name[:4]) == JUROR_PREFIX


def is_case_token(name: bytes) -> bool:
    return len(name) == 32 and bytes(name[:4]) == CASE_PREFIX


# ── Commit hash (§7) ─────────────────────────────────────────────────────────


def vote_byte(value) -> bytes:
    """The byte of a vote in the commit preimage: 00, 01 or 02 (§5.1)."""
    return bytes([vote_index(value)])


def commit_preimage(case: bytes, agent: bytes, value, salt: bytes, record_hash: bytes) -> bytes:
    """``case_name ++ agent ++ vote_byte ++ salt ++ record_hash`` (129 bytes)."""
    for what, data, size in (("case token name", case, 32), ("agent", agent, 32), ("salt", salt, 32),
                             ("record hash", record_hash, 32)):
        if len(bytes(data)) != size:
            raise ValueError(f"the {what} must be {size} bytes")
    return bytes(case) + bytes(agent) + vote_byte(value) + bytes(salt) + bytes(record_hash)


def commit_hash(case: bytes, agent: bytes, value, salt: bytes, record_hash: bytes) -> bytes:
    """``blake2b_256(case_name ++ agent ++ vote_byte ++ salt ++ record_hash)``."""
    return blake2b_256(commit_preimage(case, agent, value, salt, record_hash))


def new_salt() -> bytes:
    """32 random bytes from the operating system."""
    return os.urandom(32)


# ── Result rule (§8) and settlement (§6.11, §9) ──────────────────────────────


def _counts(tally: Union[Tally, Sequence[int]]) -> Tuple[int, int, int]:
    if isinstance(tally, Tally):
        return tally.claimer, tally.auditor, tally.inconclusive
    claimer, auditor, inconclusive = tally
    return int(claimer), int(auditor), int(inconclusive)


def outcome(tally: Union[Tally, Sequence[int]], params: JuryParams):
    """The result of a tally (§8): ClaimerWins, AuditorWins or Inconclusive
    (a vote object)."""
    claimer, auditor, inconclusive = _counts(tally)
    n = claimer + auditor + inconclusive
    if n >= params.quorum and claimer * params.majority_den >= n * params.majority_num:
        return vote(0)
    if n >= params.quorum and auditor * params.majority_den >= n * params.majority_num:
        return vote(1)
    return vote(2)


def revealed_count(tally: Union[Tally, Sequence[int]]) -> int:
    return sum(_counts(tally))


def finalize_burns(case: Case) -> bool:
    """True when ``Finalize`` closes the case: ``fee == 0``, ``n == 0`` or
    ``fee < n`` (§6.11 rule 4, amendment 1)."""
    n = revealed_count(case.tally)
    return case.fee == 0 or n == 0 or case.fee < n


def settle_share(case: Case) -> int:
    """``fee / n`` (rounded down) of a case that ``Finalize`` moves to
    ``Settling``. Raises ValueError when Finalize burns the case."""
    if finalize_burns(case):
        raise ValueError("Finalize closes this case (fee 0, no reveal, or fee < n): there is no share")
    return case.fee // revealed_count(case.tally)


def revealed_unpaid(case: Case) -> int:
    """The number of ballots with the status ``Revealed`` (not paid yet)."""
    return sum(1 for b in case.ballots if isinstance(b.status, Revealed))


def close_allowed_at(case: Case, params: JuryParams) -> Optional[int]:
    """The earliest ``lo`` at which ``Close`` is allowed (§6.13), or None when
    the case is not ``Settling``. 0 means at once (every revealed ballot is
    paid)."""
    if not isinstance(case.state, Settling):
        return None
    if revealed_unpaid(case) == 0:
        return 0
    return case.reveal_end + params.claim_period


# ── Juror outputs (§6.4, §6.7) ───────────────────────────────────────────────


def juror_invariant(lovelace: int, params: JuryParams) -> bool:
    """A juror output never holds less than ``juror_floor`` (§6.7)."""
    return lovelace >= params.juror_floor


def slash_amount(lovelace: int, params: JuryParams) -> int:
    """``min(slash, lovelace(own input) - juror_floor)`` (§6.7, amendment 2),
    never negative."""
    return max(0, min(params.slash, lovelace - params.juror_floor))


def open_entry(juror: Juror, case: bytes):
    """The open entry of ``case`` in the juror datum, or None. Raises
    ValueError when the datum has more than one (cannot happen under the
    contract)."""
    found = [e for e in juror.open if bytes(e.case) == bytes(case)]
    if len(found) > 1:
        raise ValueError("the juror datum has two open entries for one case")
    return found[0] if found else None


def ballot_of(case: Case, juror_token: bytes) -> Optional[Tuple[int, Ballot]]:
    """(position, ballot) of the juror token in the case, or None."""
    found = [(i, b) for i, b in enumerate(case.ballots) if bytes(b.juror) == bytes(juror_token)]
    if len(found) > 1:
        raise ValueError("the case has two ballots of one juror token")
    return found[0] if found else None


def commit_problems(
    juror: Juror,
    juror_lovelace: int,
    juror_token: bytes,
    case: Case,
    case_token: bytes,
    params: JuryParams,
    lo: int,
    hi: int,
) -> List[str]:
    """The rules of §6.4 (Commit, without the identity check) and §6.9
    (CaseCommit) that a commit with the validity range ``[lo, hi]`` breaks."""
    problems = []
    if not isinstance(case.state, Open):
        problems.append("the case is not Open")
    if hi > case.commit_end:
        problems.append(f"the commit window ended at {case.commit_end} (hi {hi})")
    if lo > hi:
        problems.append("the validity range is empty")
    if open_entry(juror, case_token) is not None:
        problems.append("the juror already has an open commitment in this case")
    if lo < juror.registered_at + params.eligibility_delay:
        problems.append(
            f"the juror can vote from {juror.registered_at + params.eligibility_delay} "
            f"(registration + eligibility delay); lo is {lo}"
        )
    if isinstance(juror.exit_after, Some):
        problems.append("the juror requested its exit")
    if juror_lovelace < params.bond_min:
        problems.append(f"the juror output holds {juror_lovelace} lovelace, less than bond_min {params.bond_min}")
    if bytes(juror.agent) in (bytes(case.claimer), bytes(case.auditor)):
        problems.append("the juror is a party of the challenge")
    if any(bytes(b.agent) == bytes(juror.agent) for b in case.ballots):
        problems.append("the juror's DID already has a ballot in this case")
    if ballot_of(case, juror_token) is not None:
        problems.append("the juror token already has a ballot in this case")
    return problems


def reveal_problems(
    juror: Juror,
    juror_token: bytes,
    case: Case,
    case_token: bytes,
    value,
    salt: bytes,
    record_hash: bytes,
    lo: int,
    hi: int,
) -> List[str]:
    """The rules of §6.5 (Reveal) and §6.10 (CaseReveal) that a reveal with the
    validity range ``[lo, hi]`` breaks."""
    problems = []
    if not isinstance(case.state, Open):
        problems.append("the case is not Open")
    if lo < case.commit_end:
        problems.append(f"the reveal window opens at {case.commit_end} (lo {lo})")
    if hi > case.reveal_end:
        problems.append(f"the reveal window ended at {case.reveal_end} (hi {hi})")
    if open_entry(juror, case_token) is None:
        problems.append("the juror has no open commitment in this case")
    found = ballot_of(case, juror_token)
    if found is None:
        problems.append("the case has no ballot of this juror")
    else:
        ballot = found[1]
        if not isinstance(ballot.status, Committed):
            problems.append("the ballot is already revealed")
        elif bytes(ballot.commit) != commit_hash(case_token, ballot.agent, value, salt, record_hash):
            problems.append("the vote, salt and record do not match the commitment")
    return problems


def eligible_at(juror: Juror, params: JuryParams) -> int:
    """The earliest ``lo`` of a commit of this juror (registration +
    eligibility delay)."""
    return juror.registered_at + params.eligibility_delay


def exit_allowed_at(juror: Juror) -> Optional[int]:
    """``t`` of ``exit_after = Some(t)``, or None before an exit request."""
    return option_value(juror.exit_after)
