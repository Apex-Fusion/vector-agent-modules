// Oracle: Self-Improvement Module — Test Helpers
// Transaction construction builders for validator-level mock transaction tests.
//
// These helpers construct Input, Output, and reference input objects that
// simulate what the validators see in a real transaction, enabling tests
// that exercise the validation functions with realistic transaction data.

use aiken/builtin
use aiken/crypto.{blake2b_256}
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, NoDatum, Output, OutputReference}

use governance_suggestion/params
use governance_suggestion/types.{
  GovernanceConfig, GovernanceCrossRefs, GovernanceParams,
  ProposalDatum, ProposerActivityDatum,
}
use governance_suggestion/utils

use shared/did_verification.{AgentDatum}
use shared/oracle.{GovernanceOracleDatum}

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

pub const refs_token_policy: ByteArray =
  #"ee00000000000000000000000000000000000000000000000000ee00"

pub const registry_policy_id: ByteArray =
  #"ff00000000000000000000000000000000000000000000000000ff00"

pub const registry_script_hash: ByteArray =
  #"1100000000000000000000000000000000000000000000000000001100"

pub const oracle_script_hash: ByteArray =
  #"2200000000000000000000000000000000000000000000000000002200"

pub const params_script_hash: ByteArray =
  #"3300000000000000000000000000000000000000000000000000003300"

pub const treasury_script_hash: ByteArray =
  #"4400000000000000000000000000000000000000000000000000004400"

pub const protocol_params_hash: ByteArray =
  #"5500000000000000000000000000000000000000000000000000005500"

// Zeroed hashes for unused modules
pub const zeroed_hash: ByteArray =
  #"0000000000000000000000000000000000000000000000000000"

// ============================================================================
// Agent Credentials and DIDs
// ============================================================================

pub const proposer_did: ByteArray = "did_proposer_test_001"

pub const proposer_key_hash: ByteArray =
  #"aabb0000000000000000000000000000000000000000000000000000"

pub fn proposer_cred() -> Credential {
  VerificationKey(proposer_key_hash)
}

pub const critic_did: ByteArray = "did_critic_test_002"

pub const critic_key_hash: ByteArray =
  #"ccdd0000000000000000000000000000000000000000000000000000"

pub fn critic_cred() -> Credential {
  VerificationKey(critic_key_hash)
}

pub const oracle_key_hash: ByteArray =
  #"0102030000000000000000000000000000000000000000000000000000"

pub fn oracle_cred() -> Credential {
  VerificationKey(oracle_key_hash)
}

// ============================================================================
// GovernanceConfig + CrossRefs
// ============================================================================

pub fn test_config() -> GovernanceConfig {
  GovernanceConfig {
    refs_token_policy: refs_token_policy,
    prediction_validator_hash: zeroed_hash,
    registry_policy_id: registry_policy_id,
    registry_script_hash: registry_script_hash,
    reputation_validator_hash: zeroed_hash,
    jury_pool_hash: zeroed_hash,
    governance_oracle_hash: oracle_script_hash,
    governance_params_hash: params_script_hash,
    governance_treasury_hash: treasury_script_hash,
    credibility_pool_hash: zeroed_hash,
    protocol_params_hash: protocol_params_hash,
  }
}

pub fn test_cross_refs() -> GovernanceCrossRefs {
  GovernanceCrossRefs {
    proposal_validator_hash: proposal_validator_hash,
    critique_validator_hash: critique_validator_hash,
    proposal_mint_hash: proposal_mint_hash,
    critique_mint_hash: critique_mint_hash,
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

/// GovernanceParams reference input
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
      value: assets.from_lovelace(2_000_000),
      datum: InlineDatum(gov_params),
      reference_script: None,
    },
  }
}

/// Oracle reference input (active, with oracle credential)
pub fn oracle_ref_input() -> Input {
  let oracle_datum =
    GovernanceOracleDatum {
      oracle_credential: oracle_cred(),
      treasury_script_hash: treasury_script_hash,
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
      value: assets.from_lovelace(2_000_000),
      datum: InlineDatum(oracle_datum),
      reference_script: None,
    },
  }
}

/// Agent Registry reference input: UTXO at registry address with DID NFT
pub fn registry_ref_input(did: ByteArray) -> Input {
  let agent_datum =
    AgentDatum {
      owner: proposer_cred(),
      name: "TestAgent",
      description: "Test agent for governance",
      capabilities: ["governance"],
      framework: "test",
      endpoint: "http://localhost",
      registered_at: 0,
    }
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0004",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(registry_script_hash),
        stake_credential: None,
      },
      value: assets.from_asset(registry_policy_id, did, 1)
        |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 2_000_000),
      datum: InlineDatum(agent_datum),
      reference_script: None,
    },
  }
}

/// Protocol params reference input (for stale proposal detection)
pub fn protocol_params_ref_input() -> Input {
  // Dummy datum — stale detection only checks address exists
  Input {
    output_reference: make_ref(
      #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0005",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(protocol_params_hash),
        stake_credential: None,
      },
      value: assets.from_lovelace(2_000_000),
      datum: InlineDatum(Void),
      reference_script: None,
    },
  }
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

/// Standard reference inputs for most actions: [cross_refs, params, registry]
pub fn standard_ref_inputs(did: ByteArray) -> List<Input> {
  [cross_refs_ref_input(), params_ref_input(), registry_ref_input(did)]
}

/// Oracle reference inputs: standard + oracle datum
pub fn oracle_ref_inputs(did: ByteArray) -> List<Input> {
  [
    cross_refs_ref_input(),
    params_ref_input(),
    registry_ref_input(did),
    oracle_ref_input(),
  ]
}

// ============================================================================
// Transaction Input Builders
// ============================================================================

/// Activity UTXO input at proposal validator address with pact_ token
pub fn activity_input(
  agent_did: ByteArray,
  count: Int,
  last_slot: Int,
) -> Input {
  let activity_token = utils.activity_token_name(agent_did)
  let activity_datum =
    ProposerActivityDatum {
      agent_did: agent_did,
      agent_credential: proposer_cred(),
      active_proposal_count: count,
      last_proposal_slot: last_slot,
    }
  Input {
    output_reference: make_ref(
      #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0001",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(proposal_validator_hash),
        stake_credential: None,
      },
      value: assets.from_asset(proposal_mint_hash, activity_token, 1)
        |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 2_000_000),
      datum: InlineDatum(activity_datum),
      reference_script: None,
    },
  }
}

/// Proposal UTXO input at proposal validator address with prop_ token
pub fn proposal_input(
  datum: ProposalDatum,
  own_ref: OutputReference,
) -> Input {
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

/// Activity UTXO output at proposal validator address
pub fn activity_output(
  agent_did: ByteArray,
  count: Int,
  last_slot: Int,
) -> Output {
  let activity_token = utils.activity_token_name(agent_did)
  let activity_datum =
    ProposerActivityDatum {
      agent_did: agent_did,
      agent_credential: proposer_cred(),
      active_proposal_count: count,
      last_proposal_slot: last_slot,
    }
  Output {
    address: Address {
      payment_credential: Script(proposal_validator_hash),
      stake_credential: None,
    },
    value: assets.from_asset(proposal_mint_hash, activity_token, 1)
      |> assets.add(assets.ada_policy_id, assets.ada_asset_name, 2_000_000),
    datum: InlineDatum(activity_datum),
    reference_script: None,
  }
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
    address: Address {
      payment_credential: credential,
      stake_credential: None,
    },
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
    proposer_did: proposer_did,
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
