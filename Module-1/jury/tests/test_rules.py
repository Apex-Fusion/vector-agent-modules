"""Token names (§4), the commit hash (§7), the result rule (§8), settlement
(§6.11 after amendment 1) and the commit rules (§6.4, §6.9)."""

import dataclasses
import hashlib

import pytest
from pycardano import TransactionId, TransactionInput

from conftest import TEST_PARAMS_JSON, h28, h32
from simulation.chain import derive_token_name
from vector_jury import rules
from vector_jury import types as T
from vector_jury.deployment import params_from_json

PARAMS = params_from_json(TEST_PARAMS_JSON)


@pytest.mark.parametrize("index", [0, 1, 23, 24, 255, 256, 65_535])
def test_token_names_match_module1(index):
    tx_id = h32(b"seed tx %d" % index)
    assert rules.juror_token_name((tx_id, index)) == derive_token_name(b"jrr_", tx_id, index)
    assert rules.case_token_name((tx_id, index)) == derive_token_name(b"cas_", tx_id, index)
    assert rules.case_token_name(TransactionInput(TransactionId(tx_id), index)) == \
        rules.case_token_name(f"{tx_id.hex()}#{index}")


def test_token_name_layout():
    tx_id = h32(b"seed")
    name = rules.juror_token_name((tx_id, 7))
    assert len(name) == 32 and name[:4] == b"jrr_" == bytes.fromhex("6a72725f")
    preimage = b"\xd8\x79\x9f\x58\x20" + tx_id + b"\x07\xff"
    assert name[4:] == hashlib.blake2b(preimage, digest_size=32).digest()[:28]
    assert rules.case_token_name((tx_id, 7))[:4] == bytes.fromhex("6361735f")
    # A large index (above 65535) uses the 4-byte head.
    assert rules.output_reference_cbor(tx_id, 70_000)[-6:] == b"\x1a" + (70_000).to_bytes(4, "big") + b"\xff"
    assert rules.is_juror_token(name) and not rules.is_case_token(name)


def test_commit_hash():
    case, agent, salt, record = h32(b"case"), h32(b"agent"), h32(b"salt"), h32(b"record")
    preimage = rules.commit_preimage(case, agent, "AuditorWins", salt, record)
    assert len(preimage) == 129 and preimage[64:65] == b"\x01"
    assert rules.commit_hash(case, agent, "AuditorWins", salt, record) == hashlib.blake2b(preimage, digest_size=32).digest()
    assert [rules.vote_byte(v) for v in T.VOTE_NAMES] == [b"\x00", b"\x01", b"\x02"]
    # The case and the DID bind the commitment.
    assert rules.commit_hash(h32(b"other"), agent, 1, salt, record) != rules.commit_hash(case, agent, 1, salt, record)
    with pytest.raises(ValueError):
        rules.commit_hash(case, agent, 1, b"short", record)
    assert len(rules.new_salt()) == 32 and rules.new_salt() != rules.new_salt()


@pytest.mark.parametrize("tally, result", [
    ((0, 0, 0), "Inconclusive"),
    ((2, 0, 0), "Inconclusive"),        # below quorum
    ((3, 0, 0), "ClaimerWins"),
    ((2, 1, 0), "ClaimerWins"),         # 2/3 exactly
    ((0, 2, 1), "AuditorWins"),         # Inconclusive votes count in n
    ((1, 1, 1), "Inconclusive"),
    ((5, 3, 0), "Inconclusive"),        # 5 of 8 < 2/3
    ((10, 5, 0), "ClaimerWins"),        # 10 of 15 = 2/3
    ((0, 0, 3), "Inconclusive"),
])
def test_outcome(tally, result):
    assert T.vote_name(rules.outcome(tally, PARAMS)) == result
    assert T.vote_name(rules.outcome(T.Tally(*tally), PARAMS)) == result


def case_with(fee, tally, ballots=()):
    return T.Case(h32(b"c"), h32(b"k"), T.OutputReference(h32(b"ch"), 0), T.OutputReference(h32(b"cl"), 0),
                  h32(b"cl"), h32(b"au"), h32(b"h"), h32(b"e"), 9, 1, 2, 3,
                  T.plutus_address("addr1wx2yu92dtp70q0stsv8kmnrv284lh836lp0l936x6gsrggshvzzlc"), fee,
                  list(ballots), T.Tally(*tally), T.Open())


@pytest.mark.parametrize("fee, tally, burns, share", [
    (0, (1, 1, 1), True, None),          # fee 0
    (5_000_000, (0, 0, 0), True, None),  # no reveal
    (2, (1, 1, 1), True, None),          # fee < n
    (3, (1, 1, 1), False, 1),
    (10_000_000, (2, 1, 0), False, 3_333_333),
])
def test_finalize_rule(fee, tally, burns, share):
    case = case_with(fee, tally)
    assert rules.finalize_burns(case) is burns
    if burns:
        with pytest.raises(ValueError):
            rules.settle_share(case)
    else:
        assert rules.settle_share(case) == share


def juror(**changes):
    datum = T.Juror(h32(b"juror"), h28(b"owner"), h32(b"prompt"), 1_000, T.Nothing(), [])
    return dataclasses.replace(datum, **changes)


def test_commit_problems():
    case_token = b"cas_" + h28(b"case")
    token = b"jrr_" + h28(b"juror")
    case = dataclasses.replace(case_with(0, (0, 0, 0)), commit_end=10_000_000_000)
    lo = 1_000 + PARAMS.eligibility_delay
    ok = rules.commit_problems(juror(), PARAMS.bond_min, token, case, case_token, PARAMS, lo, lo + 1000)
    assert ok == []
    early = rules.commit_problems(juror(), PARAMS.bond_min, token, case, case_token, PARAMS, lo - 1, lo + 1000)
    assert any("can vote from" in p for p in early)
    late = rules.commit_problems(juror(), PARAMS.bond_min, token, case, case_token, PARAMS, lo, case.commit_end + 1)
    assert any("commit window" in p for p in late)
    party = rules.commit_problems(juror(agent=case.claimer), PARAMS.bond_min, token, case, case_token, PARAMS, lo, lo)
    assert any("party" in p for p in party)
    exiting = rules.commit_problems(juror(exit_after=T.Some(5)), PARAMS.bond_min, token, case, case_token, PARAMS,
                                    lo, lo)
    assert any("exit" in p for p in exiting)
    # Amendment 2: no limit on open commitments (only bond_min counts).
    busy = juror(open=[T.OpenVote(b"cas_" + h28(b"%d" % i), 5) for i in range(40)])
    assert rules.commit_problems(busy, PARAMS.bond_min, token, case, case_token, PARAMS, lo, lo) == []
    assert any("bond_min" in p for p in rules.commit_problems(busy, PARAMS.bond_min - 1, token, case, case_token,
                                                               PARAMS, lo, lo))
    twice = dataclasses.replace(case, ballots=[T.Ballot(h32(b"juror"), b"jrr_" + h28(b"other"), h32(b"x"),
                                                        T.Committed())])
    assert any("already has a ballot" in p for p in rules.commit_problems(juror(), PARAMS.bond_min, token, twice,
                                                                           case_token, PARAMS, lo, lo))


def test_juror_invariant_and_close_time():
    assert rules.juror_invariant(PARAMS.juror_floor, PARAMS)
    assert not rules.juror_invariant(PARAMS.juror_floor - 1, PARAMS)
    assert rules.slash_amount(PARAMS.bond_min, PARAMS) == PARAMS.slash
    assert rules.slash_amount(PARAMS.juror_floor + 7, PARAMS) == 7
    assert rules.slash_amount(PARAMS.juror_floor, PARAMS) == 0
    settling = dataclasses.replace(case_with(3, (1, 1, 1)), state=T.Settling(1), ballots=[
        T.Ballot(h32(b"a"), b"jrr_" + h28(b"a"), h32(b"x"), T.Revealed(T.ClaimerWins()))])
    assert rules.close_allowed_at(settling, PARAMS) == settling.reveal_end + PARAMS.claim_period
    paid = dataclasses.replace(settling, ballots=[
        T.Ballot(h32(b"a"), b"jrr_" + h28(b"a"), h32(b"x"), T.Paid(T.ClaimerWins()))])
    assert rules.close_allowed_at(paid, PARAMS) == 0
    assert rules.close_allowed_at(case_with(3, (1, 1, 1)), PARAMS) is None
