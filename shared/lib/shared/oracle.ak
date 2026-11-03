/// Shared Foundation oracle verification utilities.
///
/// Verifies that the Foundation oracle is active and has signed
/// a transaction. Used by Module 6 and future modules with oracle actions.

use aiken/collection/list
use cardano/address.{Credential, Script, VerificationKey}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Input}
use shared/record_auth

/// Foundation governance oracle datum.
/// Stored in the oracle record: an output at the oracle holder address that
/// holds the `oracle` record token. Validators read it via reference input.
pub type GovernanceOracleDatum {
  /// Foundation's payment credential. Must be a verification key credential.
  oracle_credential: Credential,
  /// Governance treasury address (source of adoption rewards)
  treasury_script_hash: ByteArray,
  /// Whether governance oracle is active
  oracle_active: Bool,
}

/// Verify that the Foundation oracle is active and has signed the transaction.
///
/// The oracle record is the reference input at `oracle_script_hash` that holds
/// the record token (`record_token_policy`, `oracle`). Other outputs at the
/// oracle holder address are ignored. The record must be active, and its
/// credential must be a verification key that signs the transaction.
pub fn verify_oracle_signature(
  oracle_script_hash: ByteArray,
  record_token_policy: PolicyId,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  when
    record_auth.find_record(
      reference_inputs,
      oracle_script_hash,
      record_token_policy,
      record_auth.oracle_token_name,
    )
  is {
    Some(raw) -> {
      expect oracle: GovernanceOracleDatum = raw
      oracle.oracle_active && oracle_credential_signed(
        oracle.oracle_credential,
        signatories,
      )
    }
    None -> False
  }
}

/// Check if the oracle credential has signed.
///
/// Only a verification key credential can sign. A script credential never
/// passes: the oracle must be a key (single key or the key of the operator).
fn oracle_credential_signed(
  credential: Credential,
  signatories: List<ByteArray>,
) -> Bool {
  when credential is {
    VerificationKey(hash) -> list.has(signatories, hash)
    Script(_) -> False
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

const test_oracle_holder: ByteArray =
  #"6060606060606060606060606060606060606060606060606060606060"

const test_policy: PolicyId =
  #"7070707070707070707070707070707070707070707070707070707070"

const test_oracle_key: ByteArray =
  #"0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c"

fn oracle_input(datum: GovernanceOracleDatum, with_token: Bool) -> Input {
  let base = assets.from_lovelace(2_000_000)
  Input {
    output_reference: transaction.OutputReference {
      transaction_id: #"0000000000000000000000000000000000000000000000000000000000000000",
      output_index: 1,
    },
    output: transaction.Output {
      address: address.from_script(test_oracle_holder),
      value: if with_token {
        assets.add(base, test_policy, record_auth.oracle_token_name, 1)
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
    oracle_credential: VerificationKey(test_oracle_key),
    treasury_script_hash: #"",
    oracle_active: True,
  }
}

test oracle_signature_with_record_token() {
  verify_oracle_signature(
    test_oracle_holder,
    test_policy,
    [oracle_input(key_oracle(), True)],
    [test_oracle_key],
  )
}

/// The old check accepted any oracle datum at the oracle holder address.
/// An oracle datum without the record token is no longer accepted.
test oracle_signature_rejects_record_without_token() {
  !verify_oracle_signature(
    test_oracle_holder,
    test_policy,
    [oracle_input(key_oracle(), False)],
    [test_oracle_key],
  )
}

/// The old check returned True for a script credential without any signature.
test oracle_signature_rejects_script_credential() {
  let script_oracle =
    GovernanceOracleDatum {
      ..key_oracle(),
      oracle_credential: Script(test_oracle_key),
    }
  !verify_oracle_signature(
    test_oracle_holder,
    test_policy,
    [oracle_input(script_oracle, True)],
    [test_oracle_key],
  )
}

test oracle_signature_rejects_missing_signature() {
  !verify_oracle_signature(
    test_oracle_holder,
    test_policy,
    [oracle_input(key_oracle(), True)],
    [],
  )
}

test oracle_signature_rejects_inactive_oracle() {
  !verify_oracle_signature(
    test_oracle_holder,
    test_policy,
    [oracle_input(GovernanceOracleDatum { ..key_oracle(), oracle_active: False }, True)],
    [test_oracle_key],
  )
}
