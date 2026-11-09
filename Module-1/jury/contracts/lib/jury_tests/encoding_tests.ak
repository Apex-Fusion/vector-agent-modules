//// The on-chain format (SPEC §5): CBOR test vectors, the datum views and
//// the Module-1 datums of mainnet.
////
//// The CBOR vectors come from an independent Python encoder (cbor2) that
//// follows the tables of §5 (definite-length arrays). `cbor.deserialise`
//// accepts both array forms, and the comparison is on the decoded `Data`.

use aiken/builtin
use aiken/cbor
use aiken/interval
use cardano/address.{Address, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction, placeholder,
}
use jury/datum.{
  CaseFields, ballot_list, case_data, case_fields, juror_data, juror_fields,
}
use jury/mint_rules
use jury/module1.{ChallengeDatum, ClaimDatum} as m1
use jury/redeemers.{Burn, CaseReveal, Close, Commit, OnCase, OnJuror, TopUp}
use jury/tokens.{case_prefix, token_name}
use jury/types.{
  AuditorWins, Ballot, Case, Inconclusive, Juror, JuryDatum, JuryParams,
  OpenVote, Revealed, Settling, Tally,
}
use jury_tests/fixtures.{
  case_seed, case_token, juror, jury, new_case, params, wallet_input,
}

fn decode(bytes: ByteArray) -> Data {
  expect Some(data) = cbor.deserialise(bytes)
  data
}

fn as_data(datum: JuryDatum) -> Data {
  let data: Data = datum
  data
}

const v_agent: ByteArray =
  #"d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d101"

const v_owner: ByteArray =
  #"abababababababababababababababababababababababababababab"

const v_prompt: ByteArray =
  #"9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b"

const v_case: ByteArray =
  #"6361735f11111111111111111111111111111111111111111111111111111111"

const v_juror: ByteArray =
  #"6a72725f22222222222222222222222222222222222222222222222222222222"

// ## CBOR vectors

test vector_juror_datum() {
  let expected =
    Juror {
      agent: v_agent,
      owner: v_owner,
      prompt_hash: v_prompt,
      registered_at: 1_793_232_000_000,
      exit_after: None,
      open: [OpenVote { case: v_case, reveal_end: 1_793_592_000_000 }],
    }
  decode(
    #"d879865820d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d101581cabababababababababababababababababababababababababababab58209b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b1b000001a184f4d400d87a8081d8798258206361735f111111111111111111111111111111111111111111111111111111111b000001a19a69fe00",
  ) == as_data(expected)
}

test vector_juror_datum_with_exit_request() {
  let expected =
    Juror {
      agent: v_agent,
      owner: v_owner,
      prompt_hash: v_prompt,
      registered_at: 1_793_232_000_000,
      exit_after: Some(1_793_836_800_000),
      open: [],
    }
  decode(
    #"d879865820d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d101581cabababababababababababababababababababababababababababab58209b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b9b1b000001a184f4d400d879811b000001a1a901580080",
  ) == as_data(expected)
}

fn vector_case() -> JuryDatum {
  Case {
    challenge: #"63686c5f03030303030303030303030303030303030303030303030303030303",
    claim: #"636c6d5f01010101010101010101010101010101010101010101010101010101",
    claimer: #"c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1",
    auditor: #"c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2",
    challenge_out: OutputReference {
      transaction_id: #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      output_index: 0,
    },
    claim_out: OutputReference {
      transaction_id: #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      output_index: 1,
    },
    claim_hash: #"d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3",
    evidence_hash: #"d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4",
    deadline: 1_793_660_400_000,
    opened_at: 1_793_577_600_000,
    commit_end: 1_793_584_800_000,
    reveal_end: 1_793_592_000_000,
    refund_to: Address {
      payment_credential: VerificationKey(
        #"5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f",
      ),
      stake_credential: None,
    },
    fee: 15_000_000,
    ballots: [
      Ballot {
        agent: v_agent,
        juror: v_juror,
        commit: #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        status: Revealed { vote: AuditorWins },
      },
    ],
    tally: Tally { claimer: 1, auditor: 2, inconclusive: 0 },
    state: Settling { share: 5_000_000 },
  }
}

const vector_case_cbor: ByteArray =
  #"d87a91582063686c5f030303030303030303030303030303030303030303030303030303035820636c6d5f01010101010101010101010101010101010101010101010101010101d879825820bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00d879825820bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb015820c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c15820c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c25820d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d35820d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d41b000001a19e7db1801b000001a1998e44001b000001a199fc21001b000001a19a69fe00d87982d87981581c5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5fd87a801a00e4e1c081d879845820d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d10158206a72725f222222222222222222222222222222222222222222222222222222225820ccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccd87a81d87a80d87983010200d87a811a004c4b40"

test vector_case_datum() {
  decode(vector_case_cbor) == as_data(vector_case())
}

/// The view decodes the vector, and gives the same datum back.
test vector_case_datum_through_view() {
  let c = case_fields(decode(vector_case_cbor))
  and {
    c.fee == 15_000_000,
    c.reveal_end == 1_793_592_000_000,
    c.state == Settling { share: 5_000_000 },
    ballot_list(c) == [
      Ballot {
        agent: v_agent,
        juror: v_juror,
        commit: #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        status: Revealed { vote: AuditorWins },
      },
    ],
    case_data(c) == decode(vector_case_cbor),
  }
}

test vector_spend_case_reveal() {
  let expected: Data =
    OnCase(
      CaseReveal {
        juror: v_juror,
        vote: Inconclusive,
        salt: #"5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a",
        record_hash: #"4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d",
      },
    )
  decode(
    #"d87a81d87a8458206a72725f22222222222222222222222222222222222222222222222222222222d87b8058205a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a58204d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d",
  ) == expected
}

test vector_spend_commit() {
  let expected: Data = OnJuror(Commit { case: v_case })
  decode(
    #"d87981d8798158206361735f11111111111111111111111111111111111111111111111111111111",
  ) == expected
}

test vector_spend_close_and_top_up() {
  let close: Data = OnCase(Close)
  let top_up: Data = OnJuror(TopUp)
  and {
    decode(#"d87a81d87d80") == close,
    decode(#"d87981d87f80") == top_up,
  }
}

test vector_mint_open_and_burn() {
  let open: Data =
    redeemers.Open {
      seed: OutputReference {
        transaction_id: #"0000000000000000000000000000000000000000000000000000000000000007",
        output_index: 3,
      },
    }
  let burn: Data = Burn
  and {
    decode(
      #"d87a81d879825820000000000000000000000000000000000000000000000000000000000000000703",
    ) == open,
    decode(#"d87b80") == burn,
  }
}

/// The mainnet parameter of §3, as `JuryParams` (§5.6).
pub fn mainnet_params() -> JuryParams {
  JuryParams {
    identity_hash: #"d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762",
    m1_claim: #"53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523",
    m1_challenge: #"9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08",
    treasury: Address {
      payment_credential: Script(
        #"944e154d587cf03e0b830f6dcc6c51ebfb9e3af85ff2c746d2203422",
      ),
      stake_credential: None,
    },
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

test vector_mainnet_params() {
  let expected: Data = mainnet_params()
  decode(
    #"d87990581cd958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762581c53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523581c9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08d87982d87a81581c944e154d587cf03e0b830f6dcc6c51ebfb9e3af85ff2c746d2203422d87a801a0ee6b2801a017d78401a009896801a0f7314001a0f7314001a006ddd001a006ddd001a006ddd001a9a7ec800030203",
  ) == expected
}

/// The rules of §3 for the mainnet values.
test mainnet_params_follow_section_3() {
  let p = mainnet_params()
  and {
    p.bond_min >= p.juror_floor + p.slash,
    p.majority_num < p.majority_den,
    p.majority_den < 2 * p.majority_num,
  }
}

// ## The views

test juror_view_has_the_juror_encoding() {
  let j = juror(1)
  juror_data(j) == as_data(
    Juror {
      agent: j.agent,
      owner: j.owner,
      prompt_hash: j.prompt_hash,
      registered_at: j.registered_at,
      exit_after: j.exit_after,
      open: j.open,
    },
  )
}

test case_view_has_the_case_encoding() {
  let c = new_case()
  case_data(c) == as_data(
    Case {
      challenge: c.challenge,
      claim: c.claim,
      claimer: c.claimer,
      auditor: c.auditor,
      challenge_out: c.challenge_out,
      claim_out: c.claim_out,
      claim_hash: c.claim_hash,
      evidence_hash: c.evidence_hash,
      deadline: c.deadline,
      opened_at: c.opened_at,
      commit_end: c.commit_end,
      reveal_end: c.reveal_end,
      refund_to: c.refund_to,
      fee: c.fee,
      ballots: [],
      tally: c.tally,
      state: c.state,
    },
  )
}

test juror_view_round_trip() {
  juror_fields(juror_data(juror(3))) == juror(3)
}

test case_view_round_trip() {
  case_fields(case_data(new_case())) == new_case()
}

test juror_view_rejects_case_datum() fail {
  juror_fields(case_data(new_case())).agent == #""
}

test case_view_rejects_juror_datum() fail {
  case_fields(juror_data(juror(1))).fee == 0
}

/// The 6 fields of a juror datum under constructor 1 are not a `Juror`.
test juror_view_rejects_six_fields_with_constructor_1() fail {
  let j: Data = juror(1)
  let fields = builtin.snd_pair(builtin.un_constr_data(j))
  juror_fields(builtin.constr_data(1, fields)).agent == #""
}

/// 17 case fields under constructor 0 are not a `Case`.
test case_view_rejects_seventeen_fields_with_constructor_0() fail {
  let fields = builtin.snd_pair(builtin.un_constr_data(case_data(new_case())))
  case_fields(builtin.constr_data(0, fields)).fee == 0
}

// ## Module-1 datums of mainnet
//
// The inline datums of the outputs 0 (challenge) and 1 (claim) of the
// mainnet transaction bb96f9494ab3bd3fe35cc866d85f10afa36d334d41ad153dcfd4b462c4360e14
// (Koios `tx_info`, re-encoded as CBOR). The challenge is `PendingOracle`
// and the claim is `Challenged`.

const mainnet_challenge_cbor: ByteArray =
  #"d8798bd8798258205e12d94ac9c779dcb1f541e369b925e3bd63ddee71e6833acd785b5feb0c286300582044ca2b6984627a40698506276e58b01d1596206c92cfa0627e8e5c89db638249d87981581c312ff0d59647f6c146ed0e069444f1e442ead9e2243a2c6573f0da721a02faf0805820572f63e3f66392ef5e75c5c4da5caa87040489ec4e029c5a2952be19fc1e097e583a68747470733a2f2f6d61726b6574706c6163652e766563746f722e61706578667573696f6e2e6f72672f65766964656e63652f65312e6a736f6e1b000001a1a5f1b6a01a0f73140080d879805820636c6d5f031b5af8af4be12744bc3a7f4784b43bd0773efed8046952e88f51d3"

const mainnet_claim_cbor: ByteArray =
  #"d879895820b399a61facdaf136d5114cdc428555272c807ab8f3f68051e1a076b0781a63c4d87981581c8c1a4155461a657b0c8a6c58600e07b59a2f211c2771741f38f0435f5820572c9f354f40196a0ccdddf03682a9856bb625e0dfa4eb8d09271b59e31a44b04f736572766963652d7175616c697479583a68747470733a2f2f6d61726b6574706c6163652e766563746f722e61706578667573696f6e2e6f72672f65766964656e63652f63312e6a736f6e1a02faf0801b000001a1a5f106d81a0036ee80d87a80"

const mainnet_challenge_token: ByteArray =
  #"63686c5f5de280d9af6b9185a42318e7c9727a4088527af826e8d3c0bc3cb19e"

const mainnet_claim_token: ByteArray =
  #"636c6d5f031b5af8af4be12744bc3a7f4784b43bd0773efed8046952e88f51d3"

test mainnet_challenge_decodes() {
  expect ch: ChallengeDatum = decode(mainnet_challenge_cbor)
  and {
    ch.auditor_did == #"44ca2b6984627a40698506276e58b01d1596206c92cfa0627e8e5c89db638249",
    ch.challenged_at == 1_793_785_444_000,
    ch.resolution_deadline == 259_200_000,
    when ch.state is {
      m1.PendingOracle -> True
      _ -> False
    },
    ch.claim_token == mainnet_claim_token,
  }
}

test mainnet_claim_decodes() {
  expect cl: ClaimDatum = decode(mainnet_claim_cbor)
  and {
    cl.claimer_did == #"b399a61facdaf136d5114cdc428555272c807ab8f3f68051e1a076b0781a63c4",
    cl.storage_uri == "https://marketplace.vector.apexfusion.org/evidence/c1.json",
    when cl.state is {
      m1.Challenged -> True
      _ -> False
    },
  }
}

/// A case for the mainnet challenge, with the mainnet Module-1 hashes.
test open_with_mainnet_challenge_and_claim() {
  let p = mainnet_params()
  expect ch: ChallengeDatum = decode(mainnet_challenge_cbor)
  expect cl: ClaimDatum = decode(mainnet_claim_cbor)
  // One hour after the challenge.
  let hi = ch.challenged_at + 3_600_000
  let c =
    CaseFields {
      ..new_case(),
      challenge: mainnet_challenge_token,
      claim: ch.claim_token,
      claimer: cl.claimer_did,
      auditor: ch.auditor_did,
      challenge_out: fixtures.out_ref(0xb9, 0),
      claim_out: fixtures.out_ref(0xb9, 1),
      claim_hash: cl.claim_hash,
      evidence_hash: ch.evidence_hash,
      deadline: ch.challenged_at + ch.resolution_deadline,
      opened_at: hi,
      commit_end: hi + p.commit_window,
      reveal_end: hi + p.commit_window + p.reveal_window,
    }
  let refs =
    [
      Input {
        output_reference: fixtures.out_ref(0xb9, 0),
        output: Output {
          address: fixtures.script_address(p.m1_challenge),
          value: assets.from_lovelace(50_000_000)
            |> assets.add(p.m1_challenge, mainnet_challenge_token, 1),
          datum: InlineDatum(decode(mainnet_challenge_cbor)),
          reference_script: None,
        },
      },
      Input {
        output_reference: fixtures.out_ref(0xb9, 1),
        output: Output {
          address: fixtures.script_address(p.m1_claim),
          value: assets.from_lovelace(50_000_000)
            |> assets.add(p.m1_claim, mainnet_claim_token, 1),
          datum: InlineDatum(decode(mainnet_claim_cbor)),
          reference_script: None,
        },
      },
    ]
  let name = token_name(case_prefix, case_seed())
  let tx =
    Transaction {
      ..placeholder,
      inputs: [wallet_input(case_seed())],
      reference_inputs: refs,
      outputs: [fixtures.jury_output(name, case_data(c), 10_000_000)],
      mint: assets.from_asset(jury, name, 1),
      validity_range: interval.between(hi - 600_000, hi),
    }
  and {
    name == case_token(),
    mint_rules.validate(p, redeemers.Open { seed: case_seed() }, jury, tx),
  }
}

/// The test parameter differs from the mainnet one only in the script
/// hashes and the treasury.
test test_params_match_mainnet_values() {
  let p = params()
  let m = mainnet_params()
  JuryParams {
    ..p,
    identity_hash: m.identity_hash,
    m1_claim: m.m1_claim,
    m1_challenge: m.m1_challenge,
    treasury: m.treasury,
  } == m
}
