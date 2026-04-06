/// Shared Foundation oracle verification utilities.
///
/// Verifies that the Foundation oracle is active and has signed
/// a transaction. Used by Game 6 and future games with oracle actions.

use aiken/builtin
use aiken/collection/list
use cardano/address.{Credential, Script, VerificationKey}
use cardano/transaction.{InlineDatum, Input}

/// Foundation governance oracle datum.
/// Stored at a known script address; validators read via reference input.
pub type GovernanceOracleDatum {
  /// Foundation's payment credential (multi-sig recommended)
  oracle_credential: Credential,
  /// Governance treasury address (source of adoption rewards)
  treasury_script_hash: ByteArray,
  /// Whether governance oracle is active
  oracle_active: Bool,
}

/// Verify that the Foundation oracle is active and has signed the transaction.
/// Checks reference inputs for the oracle UTXO with oracle_active == True,
/// and verifies the transaction is signed by the oracle credential.
pub fn verify_oracle_signature(
  oracle_script_hash: ByteArray,
  reference_inputs: List<Input>,
  signatories: List<ByteArray>,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      let is_at_oracle =
        when input.output.address.payment_credential is {
          Script(hash) -> hash == oracle_script_hash
          _ -> False
        }
      if is_at_oracle {
        // Multiple UTxOs may sit at the oracle holder address (e.g. CrossRefs NFT).
        // Only match the one whose datum is a valid GovernanceOracleDatum.
        // Use is_oracle_datum to safely check the structure before parsing.
        when input.output.datum is {
          InlineDatum(raw_datum) ->
            if is_oracle_datum(raw_datum) {
              expect oracle: GovernanceOracleDatum = raw_datum
              oracle.oracle_active && oracle_credential_signed(
                oracle.oracle_credential,
                signatories,
              )
            } else {
              False
            }
          _ -> False
        }
      } else {
        False
      }
    },
  )
}

/// Check if raw datum data looks like a GovernanceOracleDatum (constructor 0 with 3 fields).
/// Used to safely skip non-oracle datums at the oracle holder address (e.g. CrossRefs NFT).
fn is_oracle_datum(raw: Data) -> Bool {
  // GovernanceOracleDatum is constructor 0 with exactly 3 fields.
  // CrossRefs is constructor 0 with 4 fields.
  // Use un_constr_data to get the fields list and check length.
  let fields = builtin.snd_pair(builtin.un_constr_data(raw))
  list.length(fields) == 3
}

/// Check if the oracle credential has signed.
fn oracle_credential_signed(
  credential: Credential,
  signatories: List<ByteArray>,
) -> Bool {
  when credential is {
    VerificationKey(hash) -> list.any(signatories, fn(s) { s == hash })
    Script(_) -> True
  }
}
