// Oracle: Self-Improvement Module — Test Helpers
// Transaction construction builders for validator-level mock transaction tests.
//
// These helpers construct Input, Output, and reference input objects that
// simulate what the validators see in a real transaction, enabling tests
// that exercise the validation functions with realistic transaction data.

use aiken/crypto.{blake2b_256}
use aiken/interval
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, NoDatum, Output, OutputReference, ScriptPurpose, Spend,
  Transaction, ValidityRange,
}
use governance_suggestion/params
use governance_suggestion/types.{
  CritiqueDatum, GovernanceConfig, GovernanceCrossRefs, ProposalDatum,
  ProposerActivityDatum, SpendActivity,
}
use governance_suggestion/utils
use shared/identity_fixtures.{
  agent_id, controller_hash, identity_hash, record, record_input,
}
use shared/oracle.{GovernanceOracleDatum}
use shared/record_auth

// ============================================================================
// Deterministic Hash Constants (28 bytes each, distinct)
// ============================================================================

pub const proposal_validator_hash: ByteArray =
  #"aa00000000000000000000000000000000000000000000000000aa00"

pub const critique_validator_hash: ByteArray =
  #"bb00000000000000000000000000000000000000000000000000bb00"

pub const proposal_mint_hash: ByteArray =
  #"cc00000000000000000000000000000000000000000000000000cc00"

pub const critique_mint_hash: ByteArray =
  #"dd00000000000000000000000000000000000000000000000000dd00"

pub const endorsement_validator_hash: ByteArray =
  #"ab000000000000000000000000000000000000000000000000000ab0"

pub const refs_token_policy: ByteArray =
  #"ee00000000000000000000000000000000000000000000000000ee00"

pub const oracle_script_hash: ByteArray =
  #"2200000000000000000000000000000000000000000000000000002200"

pub const params_script_hash: ByteArray =
  #"3300000000000000000000000000000000000000000000000000003300"

pub const treasury_script_hash: ByteArray =
  #"4400000000000000000000000000000000000000000000000000004400"

pub const protocol_params_hash: ByteArray =
  #"5500000000000000000000000000000000000000000000000000005500"

pub const record_token_policy: ByteArray =
  #"7700000000000000000000000000000000000000000000000000007700"

// Zeroed hashes for unused modules (28 bytes, the same constant the
// validators compare against)
pub const zeroed_hash: ByteArray =
  #"00000000000000000000000000000000000000000000000000000000"

// ============================================================================
// Agents, Credentials and DIDs
// ============================================================================

// The proposer and the critic are test agents of shared/identity_fixtures:
// the DID is agent_id(n) and the credential is the controller key hash
// controller_hash(n).

pub const proposer_agent: Int = 1

pub const proposer_did: ByteArray = agent_id(proposer_agent)

pub const proposer_key_hash: ByteArray = controller_hash(proposer_agent)

pub fn proposer_cred() -> Credential {
  VerificationKey(proposer_key_hash)
}

pub const critic_agent: Int = 2

pub const critic_did: ByteArray = agent_id(critic_agent)

pub const critic_key_hash: ByteArray = controller_hash(critic_agent)

pub fn critic_cred() -> Credential {
  VerificationKey(critic_key_hash)
}

pub const oracle_key_hash: ByteArray =
  #"0102030000000000000000000000000000000000000000000000000000"

pub fn oracle_cred() -> Credential {
  VerificationKey(oracle_key_hash)
}

/// A validity range of 10 minutes that starts at `lo`. A transaction that
/// reads identity records has a range of at most one hour.
pub fn validity_range_from(lo: Int) -> ValidityRange {
  interval.between(lo, lo + 600_000)
}

// ============================================================================
// GovernanceConfig + CrossRefs
// ============================================================================

pub fn test_config() -> GovernanceConfig {
  GovernanceConfig {
    refs_token_policy,
    prediction_validator_hash: zeroed_hash,
    reputation_validator_hash: zeroed_hash,
    jury_pool_hash: zeroed_hash,
    governance_oracle_hash: oracle_script_hash,
    governance_params_hash: params_script_hash,
    governance_treasury_hash: treasury_script_hash,
    credibility_pool_hash: zeroed_hash,
    protocol_params_hash,
    record_token_policy,
    identity_hash,
  }
}

pub fn test_cross_refs() -> GovernanceCrossRefs {
  GovernanceCrossRefs {
    proposal_validator_hash,
    critique_validator_hash,
    proposal_mint_hash,
    critique_mint_hash,
    endorsement_validator_hash,
  }
}

// ============================================================================
// Output Reference Helpers
// ============================================================================

pub fn make_ref(tx_hash: ByteArray, idx: Int) -> OutputReference {
  OutputReference { transaction_id: tx_hash, output_index: idx }
}

/// A deterministic "own ref" for the proposal being submitted
pub fn proposal_own_ref() -> OutputReference {
  make_ref(
    #"1111111111111111111111111111111111111111111111111111111111111111",
    0,
  )
}

/// A deterministic ref for a critique UTXO
pub fn critique_own_ref() -> OutputReference {
  make_ref(
    #"2222222222222222222222222222222222222222222222222222222222222222",
    0,
  )
}

/// The UTxO of the proposal of proposal_input (its seed is proposal_own_ref)
pub fn proposal_utxo_ref() -> OutputReference {
  make_ref(
    #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0002",
    0,
  )
}

/// The UTxO of activity_input
pub fn activity_utxo_ref() -> OutputReference {
  make_ref(
    #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0001",
    0,
  )
}

/// The UTxO of head_input (the head of the activity list)
pub fn head_utxo_ref() -> OutputReference {
  make_ref(
    #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0000",
    0,
  )
}

/// The spend redeemer of the input `ref`
pub fn spend_redeemer(ref: OutputReference, action: Data) -> Pair<ScriptPurpose, Data> {
  Pair(Spend(ref), action)
}
// ============================================================================
// Reference Input Builders
// ============================================================================

/// CrossRefs reference input: UTXO carrying the refs_token_policy token with CrossRefs datum
pub fn cross_refs_ref_input() -> Input {
  let refs = test_cross_refs()
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0001",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(oracle_script_hash),
        stake_credential: None,
      },
      value: assets.from_asset(refs_token_policy, "crossrefs", 1)
        |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 2_000_000),
      datum: InlineDatum(refs),
      reference_script: None,
    },
  }
}

/// GovernanceParams reference input: the params record (holds the `params`
/// record token)
pub fn params_ref_input() -> Input {
  let gov_params = params.default_governance_params()
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0002",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(params_script_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000)
        |> assets.add(record_token_policy, record_auth.params_token_name, 1),
      datum: InlineDatum(gov_params),
      reference_script: None,
    },
  }
}

/// Oracle reference input: the oracle record (active, with oracle credential,
/// holds the `oracle` record token)
pub fn oracle_ref_input() -> Input {
  let oracle_datum =
    GovernanceOracleDatum {
      oracle_credential: oracle_cred(),
      treasury_script_hash,
      oracle_active: True,
    }
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0003",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(oracle_script_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000)
        |> assets.add(record_token_policy, record_auth.oracle_token_name, 1),
      datum: InlineDatum(oracle_datum),
      reference_script: None,
    },
  }
}

/// Identity record of test agent `n` (shared/identity_fixtures): an Active
/// root record at the identity address with the token
/// (identity_hash, agent_id(n)) and controller key controller_key(n)
pub fn identity_ref_input(n: Int) -> Input {
  record_input(agent_id(n), record(n, []))
}

/// Proposal reference input (for critique/endorsement validation — active proposal at proposal validator)
pub fn proposal_ref_input(proposal_datum: ProposalDatum) -> Input {
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0006",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(proposal_datum.stake_amount)
        |> assets.add(
            proposal_mint_hash,
            utils.proposal_token_name(proposal_own_ref()),
            1,
          ),
      datum: InlineDatum(proposal_datum),
      reference_script: None,
    },
  }
}

/// Standard reference inputs for most actions: [cross_refs, params, the
/// identity record of test agent `n`]
pub fn standard_ref_inputs(n: Int) -> List<Input> {
  [cross_refs_ref_input(), params_ref_input(), identity_ref_input(n)]
}

/// Oracle reference inputs: standard + oracle datum
pub fn oracle_ref_inputs(n: Int) -> List<Input> {
  [
    cross_refs_ref_input(),
    params_ref_input(),
    identity_ref_input(n),
    oracle_ref_input(),
  ]
}

// ============================================================================
// Activity list: the head and the nodes (§4.6)
// ============================================================================

/// The address of the proposal validator (no stake credential)
pub fn proposal_address() -> Address {
  Address {
    payment_credential: Script(proposal_validator_hash),
    stake_credential: None,
  }
}

/// The datum of a node of the activity list
pub fn node_datum(
  agent_did: ByteArray,
  agent_credential: Credential,
  count: Int,
  last_slot: Int,
  next_did: ByteArray,
) -> ProposerActivityDatum {
  ProposerActivityDatum {
    agent_did,
    agent_credential,
    active_proposal_count: count,
    last_proposal_slot: last_slot,
    next_did,
  }
}

/// The value of a node: 2 AP3X and the activity token of its agent
pub fn node_value(agent_did: ByteArray) -> assets.Value {
  assets.from_asset(proposal_mint_hash, utils.activity_token_name(agent_did), 1)
    |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 2_000_000)
}

/// The node `datum` at the UTxO `ref`
pub fn node_input(ref: OutputReference, datum: ProposerActivityDatum) -> Input {
  Input { output_reference: ref, output: node_output(datum) }
}

/// The node `datum` as an output
pub fn node_output(datum: ProposerActivityDatum) -> Output {
  Output {
    address: proposal_address(),
    value: node_value(datum.agent_did),
    datum: InlineDatum(datum),
    reference_script: None,
  }
}

/// The datum of the head of the activity list, with `next_did`
pub fn head_datum(next_did: ByteArray) -> ProposerActivityDatum {
  ProposerActivityDatum {
    agent_did: #"",
    agent_credential: oracle_cred(),
    active_proposal_count: 0,
    last_proposal_slot: 0,
    next_did,
  }
}

/// The value of the head: 3 AP3X and the `activity` record token
pub fn head_value() -> assets.Value {
  assets.from_lovelace(3_000_000)
    |> assets.add(record_token_policy, utils.activity_head_token, 1)
}

/// The head with `next_did` at head_utxo_ref
pub fn head_input(next_did: ByteArray) -> Input {
  Input { output_reference: head_utxo_ref(), output: head_output(next_did) }
}

/// The head with `next_did` as an output
pub fn head_output(next_did: ByteArray) -> Output {
  Output {
    address: proposal_address(),
    value: head_value(),
    datum: InlineDatum(head_datum(next_did)),
    reference_script: None,
  }
}

// ============================================================================
// Transaction Input Builders
// ============================================================================

/// Activity UTXO input at proposal validator address with pact_ token: the
/// node of `agent_did` (the proposer's credential) as the last entry of the
/// activity list
pub fn activity_input(agent_did: ByteArray, count: Int, last_slot: Int) -> Input {
  node_input(
    activity_utxo_ref(),
    node_datum(agent_did, proposer_cred(), count, last_slot, #""),
  )
}

/// Proposal UTXO input at proposal validator address with prop_ token
pub fn proposal_input(datum: ProposalDatum, own_ref: OutputReference) -> Input {
  let token_name = utils.proposal_token_name(own_ref)
  Input {
    output_reference: make_ref(
      #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0002",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(datum.stake_amount)
        |> assets.add(proposal_mint_hash, token_name, 1),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

// ============================================================================
// Transaction Output Builders
// ============================================================================

/// Activity UTXO output at proposal validator address (the proposer's
/// credential): the node of `agent_did` as the last entry of the list
pub fn activity_output(
  agent_did: ByteArray,
  count: Int,
  last_slot: Int,
) -> Output {
  activity_output_for(agent_did, proposer_cred(), count, last_slot)
}

/// Activity UTXO output at proposal validator address with the credential
/// `agent_credential`: the node of `agent_did` as the last entry of the list
pub fn activity_output_for(
  agent_did: ByteArray,
  agent_credential: Credential,
  count: Int,
  last_slot: Int,
) -> Output {
  node_output(node_datum(agent_did, agent_credential, count, last_slot, #""))
}

/// Proposal UTXO output at proposal validator address
pub fn proposal_output(datum: ProposalDatum) -> Output {
  Output {
    address: Address {
      payment_credential: Script(proposal_validator_hash),
      stake_credential: None,
    },
    value: assets.from_lovelace(datum.stake_amount)
      |> assets.add(
          proposal_mint_hash,
          utils.proposal_token_name(proposal_own_ref()),
          1,
        ),
    datum: InlineDatum(datum),
    reference_script: None,
  }
}

/// Payment output to a credential (for stake returns, rewards)
pub fn payment_output(credential: Credential, lovelace: Int) -> Output {
  Output {
    address: Address { payment_credential: credential, stake_credential: None },
    value: assets.from_lovelace(lovelace),
    datum: NoDatum,
    reference_script: None,
  }
}

// ============================================================================
// Mint Value Builders
// ============================================================================

/// Mint value for a proposal token (positive = mint, negative = burn)
pub fn proposal_token_mint(own_ref: OutputReference, qty: Int) -> assets.Value {
  let token_name = utils.proposal_token_name(own_ref)
  assets.from_asset(proposal_mint_hash, token_name, qty)
}

/// Mint value for an activity token
pub fn activity_token_mint(agent_did: ByteArray, qty: Int) -> assets.Value {
  let token_name = utils.activity_token_name(agent_did)
  assets.from_asset(proposal_mint_hash, token_name, qty)
}

// ============================================================================
// Default Proposal Datum
// ============================================================================

pub fn default_proposal_datum() -> ProposalDatum {
  ProposalDatum {
    proposer_did,
    proposer_credential: proposer_cred(),
    proposal_hash: blake2b_256("test proposal content v1"),
    proposal_type: types.GeneralSuggestion,
    storage_uri: "ipfs://QmTestProposal123",
    stake_amount: 25_000_000,
    submitted_at: 1_000_000_000,
    review_window: 604_800_000,
    priority: types.Standard,
    amendment_count: 0,
    incorporated_critiques: [],
    state: types.Open,
  }
}

// ============================================================================
// Submission and finalisation transactions
// ============================================================================

/// The UTxO that a proposal submission spends (the seed proposal_own_ref):
/// the stake and the datum `datum`, at the proposal validator
pub fn proposal_seed_input(datum: ProposalDatum) -> Input {
  Input {
    output_reference: proposal_own_ref(),
    output: Output {
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(datum.stake_amount),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The UTxO that a critique submission spends (the seed `seed`): 7 AP3X and
/// the datum `datum`, at the critique validator
pub fn critique_seed_input(seed: OutputReference, datum: CritiqueDatum) -> Input {
  Input {
    output_reference: seed,
    output: Output {
      address: Address {
        payment_credential: Script(critique_validator_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(7_000_000),
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

/// The new critique of a critique submission with the seed `seed`: 7 AP3X,
/// the critique token and the datum `datum`, at the critique validator
pub fn critique_output(seed: OutputReference, datum: CritiqueDatum) -> Output {
  Output {
    address: Address {
      payment_credential: Script(critique_validator_hash),
      stake_credential: None,
    },
    value: assets.from_lovelace(7_000_000)
      |> assets.add(critique_mint_hash, utils.critique_token_name(seed), 1),
    datum: InlineDatum(datum),
    reference_script: None,
  }
}

/// A finalisation of the proposal `datum` (seed proposal_own_ref) with the
/// proposal action `action`: it spends the proposal (proposal_utxo_ref) and
/// the proposer's activity record (count 1), burns the proposal token, and
/// has the redeemers of both inputs. The caller gives the outputs.
pub fn finalisation_tx(
  action: Data,
  datum: ProposalDatum,
  reference_inputs: List<Input>,
  outputs: List<Output>,
  signatories: List<ByteArray>,
  validity_range: ValidityRange,
) -> Transaction {
  let spend_activity: Data = SpendActivity
  Transaction {
    ..transaction.placeholder,
    inputs: [
      proposal_input(datum, proposal_own_ref()),
      activity_input(datum.proposer_did, 1, 1_000_000_000),
    ],
    reference_inputs: reference_inputs,
    outputs: outputs,
    mint: proposal_token_mint(proposal_own_ref(), -1),
    extra_signatories: signatories,
    validity_range: validity_range,
    redeemers: [
      spend_redeemer(proposal_utxo_ref(), action),
      spend_redeemer(activity_utxo_ref(), spend_activity),
    ],
  }
}

/// The outputs of a finalisation of `datum`: the stake to the proposer and the
/// activity record with count 0
pub fn finalisation_outputs(datum: ProposalDatum) -> List<Output> {
  [
    payment_output(datum.proposer_credential, datum.stake_amount),
    activity_output(datum.proposer_did, 0, 1_000_000_000),
  ]
}
