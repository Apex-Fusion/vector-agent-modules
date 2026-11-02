// Module 6: parameter and oracle record tests.
//
// The governance params record and the oracle record count only when the
// reference input holds the record token of config.record_token_policy.

use aiken/crypto.{blake2b_256}
use cardano/address.{Address, Script}
use cardano/assets
use cardano/transaction.{InlineDatum, Input, Output}
use governance_suggestion/params
use governance_suggestion/proposal_validation
use governance_suggestion/tests/test_helpers.{
  activity_input, activity_output, cross_refs_ref_input, default_proposal_datum,
  identity_ref_input, make_ref, oracle_cred, oracle_key_hash, oracle_script_hash,
  params_ref_input, params_script_hash, payment_output, proposal_input,
  proposal_own_ref, proposal_token_mint, proposer_agent, proposer_cred,
  proposer_did, test_config, test_cross_refs, treasury_script_hash,
}
use governance_suggestion/types.{GovernanceParams}
use shared/oracle.{GovernanceOracleDatum}
use shared/record_auth

/// An output at the params holder with a GovernanceParams datum and no record
/// token.
fn params_without_token(gov_params: GovernanceParams) -> Input {
  Input {
    output_reference: make_ref(
      #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0001",
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

/// An output at the oracle holder with an oracle datum and, optionally, the
/// oracle record token.
fn oracle_record(datum: GovernanceOracleDatum, with_token: Bool) -> Input {
  let base = assets.from_lovelace(2_000_000)
  Input {
    output_reference: make_ref(
      #"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc0002",
      0,
    ),
    output: Output {
      address: Address {
        payment_credential: Script(oracle_script_hash),
        stake_credential: None,
      },
      value: if with_token {
        assets.add(
          base,
          test_helpers.record_token_policy,
          record_auth.oracle_token_name,
          1,
        )
      } else {
        base
      },
      datum: InlineDatum(datum),
      reference_script: None,
    },
  }
}

fn key_oracle() -> GovernanceOracleDatum {
  GovernanceOracleDatum {
    oracle_credential: oracle_cred(),
    treasury_script_hash,
    oracle_active: True,
  }
}

/// Adoption of the default proposal, signed by the oracle key, with the given
/// oracle reference input.
fn adopt_with_oracle(oracle_input: Input) -> Bool {
  let datum = default_proposal_datum()
  let own_ref = proposal_own_ref()
  proposal_validation.validate_adopt_proposal(
    test_config(),
    test_cross_refs(),
    params.default_governance_params(),
    datum,
    blake2b_256("adoption reasoning"),
    100_000_000,
    [
      cross_refs_ref_input(),
      params_ref_input(),
      identity_ref_input(proposer_agent),
      oracle_input,
    ],
    [
      proposal_input(datum, own_ref),
      activity_input(proposer_did, 1, 1_000_000_000),
    ],
    [
      payment_output(proposer_cred(), datum.stake_amount),
      activity_output(proposer_did, 0, 1_000_000_000),
    ],
    [oracle_key_hash],
    proposal_token_mint(own_ref, -1),
  )
}

// ============================================================================
// Governance params record
// ============================================================================

/// The old lookup accepted any output at the params holder.
test read_params_rejects_output_without_record_token() fail {
  let gov_params =
    params.read_governance_params(
      test_config(),
      [params_without_token(params.default_governance_params())],
    )
  gov_params.min_proposal_stake > 0
}

/// The old lookup took the first output at the params holder.
test read_params_skips_output_without_record_token() {
  let forged =
    GovernanceParams {
      ..params.default_governance_params(),
      min_proposal_stake: 1,
    }
  let gov_params =
    params.read_governance_params(
      test_config(),
      [params_without_token(forged), params_ref_input()],
    )
  gov_params == params.default_governance_params()
}

// ============================================================================
// Oracle record
// ============================================================================

test adopt_with_oracle_record() {
  adopt_with_oracle(oracle_record(key_oracle(), True))
}

/// The old check accepted any oracle datum at the oracle holder.
test adopt_rejects_oracle_datum_without_record_token() {
  !adopt_with_oracle(oracle_record(key_oracle(), False))
}

/// The old check accepted a script credential without a signature.
test adopt_rejects_script_oracle_credential() {
  let script_oracle =
    GovernanceOracleDatum {
      ..key_oracle(),
      oracle_credential: Script(oracle_key_hash),
    }
  !adopt_with_oracle(oracle_record(script_oracle, True))
}
