//// Mint redeemers accept exactly their own token set (W4 item 7), and a
//// stake token is minted only with a CreateStake spend (R4 P-1).
////
//// For each mint redeemer of both validators: a positive control, and the
//// same transaction with one more token of the same policy, which fails.

use aiken/interval
use cardano/address.{VerificationKey}
use cardano/assets
use cardano/transaction.{Input, Output, OutputReference, Spend, Transaction}
use endorsement
use reputation
use reputation_staking/test_helpers.{
  agent_signers, base_tx, key_address, make_identity_ref_input, make_input,
  make_output, make_params_ref_input, script_address, test_agent_did,
  test_agent_did_2, test_ap3x_asset_name, test_ap3x_policy_id, test_config,
  test_controller_hash, test_endorsement_policy_id, test_oracle_credential,
  test_owner_key_hash, test_owner_key_hash_2, test_refs,
  test_refs_token_policy, test_reputation_policy_id, test_stake_id,
  test_tx_hash,
}
use reputation_staking/types.{
  BurnChallengeToken, BurnEndorsementToken, BurnGenesisBonus,
  Challenge, ChallengeWon, CreateStake, Endorsement, EndorsementDatum,
  GenesisBonus, HistoryBonusDatum, IncreaseStake, MintChallengeToken,
  MintEndorsementToken, MintHistoryBonus, MintStakeToken, Open,
  ReputationChallengeDatum, StakeDatum,
}
use reputation_staking/utils.{
  derive_challenge_token_name, derive_endorsement_token_name,
  derive_history_bonus_token_name, derive_stake_token_name,
}

// ──────────────────────────────────────────────────────────────────────────────
// Fixtures
// ──────────────────────────────────────────────────────────────────────────────

const t0: Int = 1_700_000_000_000

fn range() -> transaction.ValidityRange {
  interval.between(t0, t0 + 600_000)
}

/// The CrossValidatorRefs record (holds a refs_token_policy token).
fn refs_ref_input() -> Input {
  make_input(
    test_tx_hash,
    400,
    make_output(
      key_address(test_owner_key_hash),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_refs_token_policy, "reputation_refs", 1),
      test_refs(),
    ),
  )
}

fn common_refs() -> List<Input> {
  [
    make_params_ref_input(),
    refs_ref_input(),
    make_identity_ref_input(test_agent_did, 1),
    make_identity_ref_input(test_agent_did_2, 2),
  ]
}

fn rep(name: ByteArray, quantity: Int) -> assets.Value {
  assets.from_asset(test_reputation_policy_id, name, quantity)
}

fn end(name: ByteArray, quantity: Int) -> assets.Value {
  assets.from_asset(test_endorsement_policy_id, name, quantity)
}

fn stake_token() -> ByteArray {
  derive_stake_token_name(test_agent_did)
}

fn other_stake_token() -> ByteArray {
  derive_stake_token_name(test_agent_did_2)
}

fn mint_rep(redeemer: types.StakeMintAction, tx: Transaction) -> Bool {
  reputation.reputation.mint(test_config(), redeemer, test_reputation_policy_id, tx)
}

fn mint_end(redeemer: types.EndorsementMintAction, tx: Transaction) -> Bool {
  endorsement.endorsement.mint(
    test_config(),
    redeemer,
    test_endorsement_policy_id,
    tx,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// MintStakeToken and CreateStake (R4 P-1)
// ──────────────────────────────────────────────────────────────────────────────

fn seed_ref() -> OutputReference {
  OutputReference { transaction_id: test_tx_hash, output_index: 9 }
}

fn new_stake() -> StakeDatum {
  StakeDatum {
    agent_did: test_agent_did,
    owner_credential: VerificationKey(test_owner_key_hash),
    stake_amount: 10_000_000,
    staked_capabilities: ["code_review"],
    last_updated: t0 + 600_000,
    history_points: 0,
    challenge_locks: [],
    stake_id: seed_ref(),
  }
}

fn seed_input() -> Input {
  Input {
    output_reference: seed_ref(),
    output: make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000),
      new_stake(),
    ),
  }
}

fn new_stake_output() -> Output {
  make_output(
    script_address(test_reputation_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
      |> assets.add(test_reputation_policy_id, stake_token(), 1),
    new_stake(),
  )
}

/// A CreateStake transaction: it spends the seed with the redeemer
/// `spend_redeemer` and mints `mint`.
fn create_tx(spend_redeemer: Data, mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    validity_range: range(),
    inputs: [seed_input()],
    reference_inputs: common_refs(),
    outputs: [new_stake_output()],
    mint,
    redeemers: [Pair(Spend(seed_ref()), spend_redeemer)],
    extra_signatories: agent_signers(),
  }
}

fn create_redeemer() -> Data {
  let r: Data = CreateStake
  r
}

/// Both handlers pass for a new stake.
test create_stake_and_mint_stake_token() {
  let tx = create_tx(create_redeemer(), rep(stake_token(), 1))
  and {
    mint_rep(MintStakeToken, tx),
    reputation.reputation.spend(
      test_config(),
      Some(new_stake()),
      CreateStake,
      seed_ref(),
      tx,
    ),
  }
}

/// W4 item 7: a second stake token in the same mint fails.
test mint_stake_token_with_extra_token_fails() {
  let tx =
    create_tx(
      create_redeemer(),
      assets.merge(rep(stake_token(), 1), rep(other_stake_token(), 1)),
    )
  !mint_rep(MintStakeToken, tx)
}

/// R4 P-1: a stake token is minted only with a CreateStake spend.
test mint_stake_token_with_other_spend_redeemer_fails() {
  let increase: Data = IncreaseStake
  !mint_rep(MintStakeToken, create_tx(increase, rep(stake_token(), 1)))
}

/// R4 P-1, the R4 PoC: CreateStake on another agent's StakeUTXO keeps the
/// minimum stake in a new StakeUTXO and takes the rest. The spend handler
/// refuses a spent input that holds a reputation token.
test create_stake_on_existing_stake_fails() {
  let victim =
    StakeDatum {
      ..new_stake(),
      agent_did: test_agent_did_2,
      owner_credential: VerificationKey(test_owner_key_hash_2),
      stake_amount: 1_000_000_000,
      last_updated: 0,
    }
  let victim_input =
    Input {
      output_reference: seed_ref(),
      output: make_output(
        script_address(test_reputation_policy_id),
        assets.from_lovelace(2_000_000)
          |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 1_000_000_000)
          |> assets.add(test_reputation_policy_id, other_stake_token(), 1),
        victim,
      ),
    }
  let tx =
    Transaction {
      ..create_tx(create_redeemer(), rep(stake_token(), 1)),
      inputs: [victim_input],
      outputs: [
        make_output(
          script_address(test_reputation_policy_id),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 10_000_000)
            |> assets.add(test_reputation_policy_id, other_stake_token(), 1)
            |> assets.add(test_reputation_policy_id, stake_token(), 1),
          new_stake(),
        ),
      ],
    }
  !reputation.reputation.spend(
    test_config(),
    Some(victim),
    CreateStake,
    seed_ref(),
    tx,
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Stake token burns
// ──────────────────────────────────────────────────────────────────────────────

fn burn_stake_tx(mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    inputs: [seed_input()],
    reference_inputs: [make_params_ref_input()],
    mint,
  }
}

/// No mint redeemer burns a stake token: every spend of a stake continues
/// it (a fully decayed stake continues with stake_amount 0).
test burn_stake_token_with_genesis_burn_redeemer_fails() {
  !mint_rep(BurnGenesisBonus, burn_stake_tx(rep(stake_token(), -1)))
}

test burn_stake_token_with_stake_mint_redeemer_fails() {
  !mint_rep(MintStakeToken, burn_stake_tx(rep(stake_token(), -1)))
}

// ──────────────────────────────────────────────────────────────────────────────
// MintHistoryBonus
// ──────────────────────────────────────────────────────────────────────────────

fn bonus_source_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"8888888888888888888888888888888888888888888888888888888888888888",
    output_index: 1,
  }
}

fn bonus_token() -> ByteArray {
  derive_history_bonus_token_name(bonus_source_ref())
}

fn bonus_tx(mint: assets.Value) -> Transaction {
  let bonus =
    HistoryBonusDatum {
      agent_did: test_agent_did,
      source: ChallengeWon,
      bonus_points: 3_000_000,
      source_ref: bonus_source_ref(),
      created_at: t0,
    }
  Transaction {
    ..base_tx(t0),
    validity_range: range(),
    inputs: [
      make_input(
        test_tx_hash,
        7,
        make_output(
          script_address(test_endorsement_policy_id),
          assets.from_lovelace(2_000_000),
          0,
        ),
      ),
    ],
    reference_inputs: common_refs(),
    outputs: [
      make_output(
        script_address(test_reputation_policy_id),
        assets.from_lovelace(2_000_000)
          |> assets.add(test_reputation_policy_id, bonus_token(), 1),
        bonus,
      ),
    ],
    mint,
    extra_signatories: [test_oracle_credential, test_controller_hash(1)],
  }
}

test mint_history_bonus() {
  mint_rep(MintHistoryBonus, bonus_tx(rep(bonus_token(), 1)))
}

test mint_history_bonus_with_extra_token_fails() {
  !mint_rep(
    MintHistoryBonus,
    bonus_tx(assets.merge(rep(bonus_token(), 1), rep(stake_token(), 1))),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// BurnGenesisBonus
// ──────────────────────────────────────────────────────────────────────────────

fn genesis_token() -> ByteArray {
  "genesis_agent_001"
}

fn genesis_tx(mint: assets.Value) -> Transaction {
  let bonus =
    HistoryBonusDatum {
      agent_did: test_agent_did,
      source: GenesisBonus,
      bonus_points: 100_000_000,
      source_ref: bonus_source_ref(),
      created_at: 0,
    }
  Transaction {
    ..base_tx(t0),
    inputs: [
      make_input(
        test_tx_hash,
        11,
        make_output(
          script_address(test_reputation_policy_id),
          assets.from_lovelace(2_000_000)
            |> assets.add(test_reputation_policy_id, genesis_token(), 1),
          bonus,
        ),
      ),
    ],
    reference_inputs: [make_params_ref_input()],
    mint,
  }
}

test burn_genesis_bonus() {
  mint_rep(BurnGenesisBonus, genesis_tx(rep(genesis_token(), -1)))
}

test burn_genesis_bonus_with_extra_token_fails() {
  !mint_rep(
    BurnGenesisBonus,
    genesis_tx(
      assets.merge(rep(genesis_token(), -1), rep("genesis_agent_002", -1)),
    ),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// MintEndorsementToken and BurnEndorsementToken
// ──────────────────────────────────────────────────────────────────────────────

fn endorsement_token() -> ByteArray {
  derive_endorsement_token_name(test_agent_did, test_agent_did_2)
}

fn challenge_token() -> ByteArray {
  derive_challenge_token_name(test_agent_did, test_agent_did_2, "code_review")
}

/// The StakeUTXO of test_agent_did_2 (endorsement and challenge target).
fn target_stake() -> Input {
  make_input(
    test_tx_hash,
    50,
    make_output(
      script_address(test_reputation_policy_id),
      assets.from_lovelace(2_000_000)
        |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 20_000_000)
        |> assets.add(test_reputation_policy_id, other_stake_token(), 1),
      StakeDatum {
        agent_did: test_agent_did_2,
        owner_credential: VerificationKey(test_owner_key_hash_2),
        stake_amount: 20_000_000,
        staked_capabilities: ["code_review", "testing"],
        last_updated: 500,
        history_points: 0,
        challenge_locks: [],
        stake_id: test_stake_id,
      },
    ),
  )
}

fn endorsement_datum() -> EndorsementDatum {
  EndorsementDatum {
    endorser_did: test_agent_did,
    endorser_credential: VerificationKey(test_owner_key_hash),
    target_did: test_agent_did_2,
    stake_amount: 5_000_000,
    endorsed_capabilities: ["code_review"],
    created_at: t0,
    slashed_by: [],
    target_stake: test_stake_id,
  }
}

fn endorsement_output() -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 5_000_000)
      |> assets.add(test_endorsement_policy_id, endorsement_token(), 1),
    Endorsement(endorsement_datum()),
  )
}

fn endorse_tx(mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    validity_range: range(),
    reference_inputs: [target_stake(), ..common_refs()],
    outputs: [endorsement_output()],
    mint,
    extra_signatories: agent_signers(),
  }
}

test mint_endorsement_token() {
  mint_end(MintEndorsementToken, endorse_tx(end(endorsement_token(), 1)))
}

test mint_endorsement_token_with_extra_token_fails() {
  !mint_end(
    MintEndorsementToken,
    endorse_tx(
      assets.merge(end(endorsement_token(), 1), end(challenge_token(), 1)),
    ),
  )
}

fn burn_endorsement_tx(mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    inputs: [
      Input {
        output_reference: OutputReference {
          transaction_id: test_tx_hash,
          output_index: 12,
        },
        output: endorsement_output(),
      },
    ],
    reference_inputs: common_refs(),
    mint,
  }
}

test burn_endorsement_token() {
  mint_end(BurnEndorsementToken, burn_endorsement_tx(end(endorsement_token(), -1)))
}

test burn_endorsement_token_with_extra_token_fails() {
  !mint_end(
    BurnEndorsementToken,
    burn_endorsement_tx(
      assets.merge(
        end(endorsement_token(), -1),
        end(derive_endorsement_token_name(test_agent_did_2, test_agent_did), -1),
      ),
    ),
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// MintChallengeToken and BurnChallengeToken
// ──────────────────────────────────────────────────────────────────────────────

fn challenge_output() -> Output {
  make_output(
    script_address(test_endorsement_policy_id),
    assets.from_lovelace(2_000_000)
      |> assets.add(test_ap3x_policy_id, test_ap3x_asset_name, 25_000_000)
      |> assets.add(test_endorsement_policy_id, challenge_token(), 1),
    Challenge(
      ReputationChallengeDatum {
        challenger_did: test_agent_did,
        challenger_credential: VerificationKey(test_owner_key_hash),
        target_did: test_agent_did_2,
        target_credential: VerificationKey(test_owner_key_hash_2),
        challenged_capability: "code_review",
        stake_amount: 25_000_000,
        evidence_hash: #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        evidence_uri: "ipfs://evidence",
        created_at: t0,
        counter_evidence_hash: "",
        counter_evidence_uri: "",
        response_submitted_at: 0,
        state: Open,
      },
    ),
  )
}

fn challenge_tx(mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    validity_range: range(),
    inputs: [target_stake()],
    reference_inputs: common_refs(),
    outputs: [challenge_output()],
    mint,
    extra_signatories: agent_signers(),
  }
}

test mint_challenge_token() {
  mint_end(MintChallengeToken, challenge_tx(end(challenge_token(), 1)))
}

test mint_challenge_token_with_extra_token_fails() {
  !mint_end(
    MintChallengeToken,
    challenge_tx(
      assets.merge(end(challenge_token(), 1), end(endorsement_token(), 1)),
    ),
  )
}

fn burn_challenge_tx(mint: assets.Value) -> Transaction {
  Transaction {
    ..base_tx(t0),
    inputs: [
      Input {
        output_reference: OutputReference {
          transaction_id: test_tx_hash,
          output_index: 13,
        },
        output: challenge_output(),
      },
    ],
    reference_inputs: common_refs(),
    mint,
  }
}

test burn_challenge_token() {
  mint_end(BurnChallengeToken, burn_challenge_tx(end(challenge_token(), -1)))
}

test burn_challenge_token_with_extra_token_fails() {
  !mint_end(
    BurnChallengeToken,
    burn_challenge_tx(
      assets.merge(
        end(challenge_token(), -1),
        end(
          derive_challenge_token_name(test_agent_did, test_agent_did_2, "testing"),
          -1,
        ),
      ),
    ),
  )
}
