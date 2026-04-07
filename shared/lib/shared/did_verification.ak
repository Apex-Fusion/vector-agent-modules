/// Shared DID verification utilities for Vector module contracts.
///
/// Verifies agent existence and capabilities in the Agent Registry
/// via reference inputs. Used by Modules 3, 6, and future modules.

use aiken/collection/list
use cardano/address.{Script}
use cardano/assets.{PolicyId}
use cardano/transaction.{InlineDatum, Input}

/// Agent Registry datum — mirrors the deployed AgentDatum type.
/// Defined here so all modules share a single definition.
pub type AgentDatum {
  owner: address.Credential,
  name: ByteArray,
  description: ByteArray,
  capabilities: List<ByteArray>,
  framework: ByteArray,
  endpoint: ByteArray,
  registered_at: Int,
}

/// Verify that an agent with the given DID (NFT asset name) exists in the
/// Agent Registry. Returns the AgentDatum if found.
///
/// The DID is the NFT asset name at the registry script address.
/// An agent is "active" if the UTXO exists (no `active` field in datum).
pub fn find_agent_registry_datum(
  reference_inputs: List<Input>,
  registry_script_hash: ByteArray,
  registry_policy_id: PolicyId,
  agent_did: ByteArray,
) -> AgentDatum {
  expect Some(registry_input) =
    list.find(
      reference_inputs,
      fn(input) {
        let is_at_registry =
          when input.output.address.payment_credential is {
            Script(hash) -> hash == registry_script_hash
            _ -> False
          }
        let has_nft =
          assets.quantity_of(
            input.output.value,
            registry_policy_id,
            agent_did,
          ) == 1
        is_at_registry && has_nft
      },
    )
  expect InlineDatum(raw) = registry_input.output.datum
  expect datum: AgentDatum = raw
  datum
}

/// Verify that an agent DID exists in the registry (boolean check).
pub fn verify_agent_exists(
  reference_inputs: List<Input>,
  registry_script_hash: ByteArray,
  registry_policy_id: PolicyId,
  agent_did: ByteArray,
) -> Bool {
  list.any(
    reference_inputs,
    fn(input) {
      let is_at_registry =
        when input.output.address.payment_credential is {
          Script(hash) -> hash == registry_script_hash
          _ -> False
        }
      let has_nft =
        assets.quantity_of(
          input.output.value,
          registry_policy_id,
          agent_did,
        ) == 1
      is_at_registry && has_nft
    },
  )
}

/// Verify that claimed capabilities are a subset of the agent's registered
/// capabilities in the Agent Registry.
pub fn verify_capabilities_match(
  reference_inputs: List<Input>,
  registry_script_hash: ByteArray,
  registry_policy_id: PolicyId,
  agent_did: ByteArray,
  claimed_capabilities: List<ByteArray>,
) -> Bool {
  let agent_datum =
    find_agent_registry_datum(
      reference_inputs,
      registry_script_hash,
      registry_policy_id,
      agent_did,
    )
  list.all(
    claimed_capabilities,
    fn(cap) { list.has(agent_datum.capabilities, cap) },
  )
}
