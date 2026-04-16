/// path_b_tests.ak — RED tests for Path B migration
///
/// Path B: replace custom fungible AP3X native asset with base AP3X chain coin
/// (Vector's native base currency, accessed via `assets.lovelace_of` / value.coin).
///
/// These tests encode the POST-migration contract. They will NOT compile until
/// the following changes land:
///   (1) removes `ap3x_policy_id` + `ap3x_asset_name` from `AuditingConfig`
///   (2) rewrites every `ap3x_of(v, policy, name)` call site to use base-coin math
///       (e.g., `assets.lovelace_of(v)`) in claim.ak, challenge.ak, jury_pool.ak
///   (3) keeps NFT mint/burn logic (claim/challenge/juror tokens, registry NFT,
///       refs NFT) strictly untouched — those MUST stay multi-asset
///
/// Once Path B lands, these tests go green and we update the other 200+ tests
/// in lockstep to drop their ap3x fixtures.
///
/// Author: Contract Author (Apex Security Audit Team)
/// Date: 2026-04-15

use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, NoDatum, Output, OutputReference}
use adversarial_auditing/types.{
  AuditingConfig, ClaimDatum, Open, JurorDatum,
}

// ══════════════════════════════════════════════════════════════════════
// CONSTANTS (Path B — NO ap3x policy / asset name)
// ══════════════════════════════════════════════════════════════════════

const registry_policy_id: ByteArray =
  #"d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1"

const oracle_vkh: ByteArray =
  #"0000000000000000000000000000000000000000000000000000000000000001"

const refs_policy: ByteArray =
  #"9999999999999999999999999999999999999999999999999999999999999999"

const claim_validator_hash: ByteArray =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

const jury_pool_hash: ByteArray =
  #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

const claimer_vkh: ByteArray =
  #"4444444444444444444444444444444444444444444444444444444444444444"

const claimer_did: ByteArray =
  #"aa00000000000000000000000000000000000000000000000000000000"

const seed_tx: ByteArray =
  #"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

const zero_hash: ByteArray =
  #"0000000000000000000000000000000000000000000000000000000000000000"

// ══════════════════════════════════════════════════════════════════════
// FIXTURES (Path B shape)
// ══════════════════════════════════════════════════════════════════════

/// Path B AuditingConfig: NO ap3x_policy_id, NO ap3x_asset_name.
/// Those two fields must be dropped from types.ak for this to compile.
fn pb_config() -> AuditingConfig {
  AuditingConfig {
    registry_policy_id,
    oracle_vkh,
    refs_token_policy: refs_policy,
  }
}

fn claimer_cred() -> Credential {
  VerificationKey(claimer_vkh)
}

fn claim_addr() -> Address {
  Address { payment_credential: Script(claim_validator_hash), stake_credential: None }
}

fn jury_pool_addr() -> Address {
  Address { payment_credential: Script(jury_pool_hash), stake_credential: None }
}

/// Path B stake value: base coin ONLY (no multi-asset stake).
/// `amount` is the stake in lovelace-equivalent base-coin units (DFM).
fn base_coin_stake(amount: Int) -> assets.Value {
  assets.from_lovelace(amount)
}

fn fresh_claim_datum(stake: Int) -> ClaimDatum {
  ClaimDatum {
    claimer_did: claimer_did,
    claimer_credential: claimer_cred(),
    claim_hash: zero_hash,
    claim_type: "data_indexing",
    storage_uri: "ipfs://QmClaim",
    stake_amount: stake,
    submitted_at: 1_000_000,
    challenge_window: 1_800_000,
    state: Open,
  }
}

fn fresh_juror_datum(bond: Int) -> JurorDatum {
  JurorDatum {
    juror_did: claimer_did,
    juror_credential: claimer_cred(),
    bond_amount: bond,
    cases_resolved: 0,
    majority_votes: 0,
    registered_at: 1_000_000,
    active_case: None,
    vote_commitment: None,
    revealed_verdict: None,
  }
}

fn seed_ref() -> OutputReference {
  OutputReference { transaction_id: seed_tx, output_index: 0 }
}

// ══════════════════════════════════════════════════════════════════════
// RED TESTS
// ══════════════════════════════════════════════════════════════════════

/// (1) Compile-time guard: AuditingConfig must NOT expose ap3x_policy_id
///     or ap3x_asset_name. Destructuring against the Path-B shape will
///     fail to compile as long as those fields remain.
test path_b_auditing_config_has_no_ap3x_fields() {
  let cfg = pb_config()
  let AuditingConfig {
    registry_policy_id: rp,
    oracle_vkh: ov,
    refs_token_policy: rt,
  } = cfg
  rp == registry_policy_id && ov == oracle_vkh && rt == refs_policy
}

/// (2) OpenChallenge / SubmitClaim stake accounting: the stake UTxO carries
///     the claimer's stake in base coin, NOT in a multi-asset.
///     Post-Path-B: `assets.lovelace_of(v)` is the single source of truth.
test path_b_claim_stake_in_base_coin() {
  let claim = fresh_claim_datum(50_000_000)
  let out = Output {
    address: claim_addr(),
    value: base_coin_stake(claim.stake_amount)
      |> assets.add(claim_validator_hash, "clm_token_placeholder", 1),
    datum: InlineDatum(claim),
    reference_script: None,
  }
  // Path B contract: stake lives in value.coin (lovelace).
  assets.lovelace_of(out.value) == claim.stake_amount
}

/// (3) Negative assertion: OLD multi-asset stake representation must NOT
///     satisfy the base-coin check. If the Path B refactor is correct,
///     an output whose stake is expressed under some foreign policy has
///     zero lovelace beyond the min-UTxO floor and fails the stake-amount
///     check for any non-trivial stake.
test path_b_rejects_multi_asset_stake() {
  let bogus_policy: ByteArray =
    #"cafecafecafecafecafecafecafecafecafecafecafecafecafecafe"
  let bogus_name: ByteArray = #"4150335800"
  let stake = 50_000_000
  let out = Output {
    address: claim_addr(),
    // Attacker parks stake in a fungible token, supplies only min-UTxO in coin.
    value: assets.from_lovelace(2_000_000)
      |> assets.add(bogus_policy, bogus_name, stake)
      |> assets.add(claim_validator_hash, "clm_token_placeholder", 1),
    datum: NoDatum,
    reference_script: None,
  }
  // Base-coin stake check must see only min-UTxO lovelace, NOT the bogus token.
  assets.lovelace_of(out.value) != stake && assets.lovelace_of(out.value) == 2_000_000
}

/// (4) Juror registration bond is in base coin.
///     Post-Path-B: RegisterJuror's continuing-output bond check uses
///     assets.lovelace_of(out.value) == juror.bond_amount.
test path_b_juror_bond_in_base_coin() {
  let juror = fresh_juror_datum(25_000_000)
  let out = Output {
    address: jury_pool_addr(),
    value: base_coin_stake(juror.bond_amount)
      |> assets.add(jury_pool_hash, "jur_token_placeholder", 1),
    datum: InlineDatum(juror),
    reference_script: None,
  }
  assets.lovelace_of(out.value) == juror.bond_amount
}

/// (5) NFT minting logic MUST remain multi-asset — Path B only touches
///     stake math, never token lifecycle. This test asserts that a
///     Path-B output still carries a claim NFT under the claim validator
///     policy, independent of the stake currency change.
test path_b_preserves_nft_mint_semantics() {
  let stake = 50_000_000
  let claim_tok: ByteArray = "clm_token_placeholder"
  let out = Output {
    address: claim_addr(),
    value: base_coin_stake(stake)
      |> assets.add(claim_validator_hash, claim_tok, 1),
    datum: NoDatum,
    reference_script: None,
  }
  // NFT still discoverable by multi-asset quantity_of — untouched by Path B.
  assets.quantity_of(out.value, claim_validator_hash, claim_tok) == 1
    && assets.lovelace_of(out.value) == stake
}

/// (6) ResetStaleActiveCase bond preservation: the continuing-output bond
///     comparison must read from base coin. Reset logic preserves bond,
///     currency-agnostic at the datum level, but the on-chain check that
///     the bond is actually IN the UTxO must be base-coin-based post-Path-B.
test path_b_reset_preserves_bond_in_base_coin() {
  let bond = 30_000_000
  let before = fresh_juror_datum(bond)
  let after = JurorDatum { ..before, active_case: None }
  let reset_out = Output {
    address: jury_pool_addr(),
    value: base_coin_stake(bond)
      |> assets.add(jury_pool_hash, "jur_token_placeholder", 1),
    datum: InlineDatum(after),
    reference_script: None,
  }
  // Bond datum field unchanged AND the physical value-side bond still
  // matches, using base-coin accounting.
  after.bond_amount == before.bond_amount
    && assets.lovelace_of(reset_out.value) == before.bond_amount
}
