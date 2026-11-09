"""Codecs of SPEC §5: round trips, constructor tags, strict decoding, and the
Module-1 datums of §5.7."""

import dataclasses

import cbor2
import pytest
from pycardano import Address, Network, ScriptHash, VerificationKeyHash
from pycardano.address import PointerAddress
from pycardano.serialization import ByteString

from conftest import h28, h32
from vector_jury import types as T
from vector_jury.deployment import encode_params, params_from_json
from conftest import TEST_PARAMS_JSON


def roundtrip(value, decoder):
    raw = T.to_cbor(value)
    back = decoder(raw)
    assert back == value
    assert T.same_data(back, raw)
    return raw


def case_datum(**changes):
    datum = T.Case(
        challenge=h32(b"challenge"), claim=h32(b"claim"), challenge_out=T.OutputReference(h32(b"ch tx"), 0),
        claim_out=T.OutputReference(h32(b"cl tx"), 1), claimer=h32(b"claimer"), auditor=h32(b"auditor"),
        claim_hash=h32(b"claim doc"), evidence_hash=h32(b"evidence"), deadline=10_000,
        opened_at=1_000, commit_end=2_000, reveal_end=3_000,
        refund_to=T.plutus_address(Address(VerificationKeyHash(h28(b"refund")), network=Network.MAINNET)),
        fee=3_000_000,
        ballots=[T.Ballot(h32(b"a"), b"jrr_" + h28(b"a"), h32(b"c"), T.Committed()),
                 T.Ballot(h32(b"b"), b"jrr_" + h28(b"b"), b"", T.Revealed(T.AuditorWins())),
                 T.Ballot(h32(b"e"), b"jrr_" + h28(b"e"), b"", T.Paid(T.Inconclusive()))],
        tally=T.Tally(0, 1, 1), state=T.Settling(1_500_000),
    )
    return dataclasses.replace(datum, **changes)


def test_juror_and_case_roundtrip():
    juror = T.Juror(h32(b"agent"), h28(b"owner"), h32(b"prompt"), 123, T.Some(456),
                    [T.OpenVote(b"cas_" + h28(b"x"), 789), T.OpenVote(b"cas_" + h28(b"y"), 790)])
    raw = roundtrip(juror, T.decode_jury_datum)
    assert raw[:3] == b"\xd8\x79\x9f"  # Constr 0, indefinite fields
    assert roundtrip(T.Juror(h32(b"agent"), h28(b"owner"), h32(b"p"), 1, T.Nothing(), []), T.decode_juror)
    raw = roundtrip(case_datum(), T.decode_jury_datum)
    assert raw[:3] == b"\xd8\x7a\x9f"  # Constr 1
    fields = cbor2.loads(raw).value
    assert len(fields) == 17  # amendment 2: challenge_out and claim_out, no URIs
    assert fields[2] == cbor2.CBORTag(121, [h32(b"ch tx"), 0]) and fields[3] == cbor2.CBORTag(121, [h32(b"cl tx"), 1])
    assert roundtrip(case_datum(state=T.Open(), ballots=[], tally=T.Tally(0, 0, 0)), T.decode_case)


@pytest.mark.parametrize("value, tag, fields", [
    (T.OnJuror(T.Commit(b"c" * 32)), 121, [cbor2.CBORTag(121, [b"c" * 32])]),
    (T.OnJuror(T.TopUp()), 121, [cbor2.CBORTag(127, [])]),
    (T.OnJuror(T.Exit()), 121, [cbor2.CBORTag(126, [])]),
    (T.OnJuror(T.RequestExit()), 121, [cbor2.CBORTag(125, [])]),
    (T.OnJuror(T.Slash(b"s" * 32)), 121, [cbor2.CBORTag(124, [b"s" * 32])]),
    (T.OnCase(T.CaseCommit(b"j" * 32, b"k" * 32)), 122, [cbor2.CBORTag(121, [b"j" * 32, b"k" * 32])]),
    (T.OnCase(T.CaseReveal(b"j" * 32, T.Inconclusive(), b"s" * 32, b"r" * 32)), 122,
     [cbor2.CBORTag(122, [b"j" * 32, cbor2.CBORTag(123, []), b"s" * 32, b"r" * 32])]),
    (T.OnCase(T.Finalize(T.ClaimerWins())), 122, [cbor2.CBORTag(123, [cbor2.CBORTag(121, [])])]),
    (T.OnCase(T.CaseClaim(b"j" * 32)), 122, [cbor2.CBORTag(124, [b"j" * 32])]),
    (T.OnCase(T.Close()), 122, [cbor2.CBORTag(125, [])]),
])
def test_spend_action_tags(value, tag, fields):
    data = cbor2.loads(T.to_cbor(value))
    assert data.tag == tag and list(data.value) == fields
    assert T.decode_spend_action(T.to_cbor(value)) == value


def test_mint_actions():
    seed = T.OutputReference(h32(b"seed"), 3)
    for value, tag in ((T.MintRegister(seed), 121), (T.MintOpen(seed), 122), (T.MintBurn(), 123)):
        raw = T.to_cbor(value)
        assert cbor2.loads(raw).tag == tag
        assert T.decode_mint_action(raw) == value
    # The OutputReference of the redeemer is the Plutus V3 form.
    assert T.to_cbor(seed) == b"\xd8\x79\x9f\x58\x20" + h32(b"seed") + b"\x03\xff"


def test_vote_helpers():
    assert [T.vote_index(n) for n in T.VOTE_NAMES] == [0, 1, 2]
    assert T.vote("AuditorWins") == T.AuditorWins() and T.vote(2) == T.Inconclusive()
    assert T.vote_name(T.ClaimerWins()) == "ClaimerWins"
    with pytest.raises(ValueError):
        T.vote("Maybe")
    assert T.Tally(1, 0, 0).plus("Inconclusive") == T.Tally(1, 0, 1)


def test_params_roundtrip():
    params = params_from_json(TEST_PARAMS_JSON)
    raw = encode_params(params)
    assert T.decode_params(raw) == params
    assert len(cbor2.loads(raw).value) == 16


def test_strict_decoding_rejects_other_shapes():
    good = cbor2.loads(T.to_cbor(T.Juror(h32(b"a"), h28(b"o"), h32(b"p"), 1, T.Nothing(), [])))
    extra = cbor2.CBORTag(121, list(good.value) + [7])
    with pytest.raises(T.DecodeError):
        T.decode_jury_datum(cbor2.dumps(extra))
    short_agent = cbor2.CBORTag(121, [b"a" * 31] + list(good.value)[1:])
    with pytest.raises(T.DecodeError, match="agent"):
        T.decode_jury_datum(cbor2.dumps(short_agent))
    wrong_type = cbor2.CBORTag(121, list(good.value)[:3] + [b"not an int"] + list(good.value)[4:])
    with pytest.raises(T.DecodeError):
        T.decode_jury_datum(cbor2.dumps(wrong_type))
    with pytest.raises(T.DecodeError):
        T.decode_jury_datum(cbor2.dumps(cbor2.CBORTag(123, [])))
    with pytest.raises(T.DecodeError):
        T.decode_juror(T.to_cbor(case_datum()))


def test_addresses():
    for address in (
        Address(ScriptHash(h28(b"s")), network=Network.MAINNET),
        Address(VerificationKeyHash(h28(b"k")), VerificationKeyHash(h28(b"stake")), network=Network.MAINNET),
        Address(VerificationKeyHash(h28(b"k")), ScriptHash(h28(b"stake script")), network=Network.MAINNET),
        Address(VerificationKeyHash(h28(b"k")), PointerAddress(1, 2, 3), network=Network.MAINNET),
    ):
        plutus = T.plutus_address(address)
        assert T.address_of(plutus) == address
    treasury = T.plutus_address("addr1wx2yu92dtp70q0stsv8kmnrv284lh836lp0l936x6gsrggshvzzlc")
    assert treasury == T.PlutusAddress(T.ScriptCredential(bytes.fromhex(
        "944e154d587cf03e0b830f6dcc6c51ebfb9e3af85ff2c746d2203422")), T.NoStake())


def test_module1_datums():
    claim = T.ClaimDatum(h32(b"claimer"), T.M1Credential(False, h28(b"key")), h32(b"hash"), b"service-quality",
                         b"vector-tx:" + b"B" * 43, 50_000_000, 1_000, 3_600_000, 1)
    back = T.decode_claim_datum(claim.cbor())
    assert back == claim and back.challenged and back.state_name == "Challenged"
    challenge = T.ChallengeDatum((h32(b"claim tx"), 1), h32(b"auditor"), T.M1Credential(False, h28(b"a")),
                                 60_000_000, h32(b"evidence"), b"https://e.example/" + b"e" * 70, 5_000, 259_200_000,
                                 (), 0, b"clm_" + h28(b"token"))
    back = T.decode_challenge_datum(challenge.cbor())
    assert back == challenge and back.pending_oracle and back.deadline == 5_000 + 259_200_000
    with pytest.raises(T.DecodeError):
        T.decode_claim_datum(challenge.cbor())
    with pytest.raises(T.DecodeError):
        T.decode_challenge_datum(claim.cbor())
