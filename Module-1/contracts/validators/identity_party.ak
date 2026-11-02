use adversarial_auditing/identity_party.{
  mint_handler, spend_handler, withdraw_handler,
} as party_rules
use adversarial_auditing/types.{AuditingConfig, PartyApproval, VerdictMint}
use cardano/address.{Credential}
use cardano/assets.{PolicyId}
use cardano/transaction.{OutputReference, Transaction}

/// identity_party.ak — Module-1 as a party of identity records (Part 2 §3.8).
///
/// M1 is the hash of this validator. The identity configuration record names
/// it, and the identity contract accepts a `Script { hash: M1 }` signer only
/// with a withdrawal of this script's stake credential.
///
/// Parameters:
///   config          — the Module-1 AuditingConfig (identity hash, refs token
///                     policy, configuration record keys, verdict policy)
///   challenge_hash  — hash of the Module-1 challenge validator
///
/// Mint handler:   verdict tokens (MintVerdict in a resolution, BurnVerdict
///                 in a verdict application)
/// Spend handler:  verdict outputs, only with the burn of their token
/// Withdraw:       Consent and ApplyVerdict
/// Other purposes fail (publish, vote, propose), so the stake credential
/// cannot be deregistered or delegated.
///
/// The rules are in adversarial_auditing/identity_party.ak.
validator identity_party(config: AuditingConfig, challenge_hash: ByteArray) {
  mint(redeemer: VerdictMint, policy_id: PolicyId, tx: Transaction) {
    mint_handler(config, challenge_hash, redeemer, policy_id, tx)
  }

  spend(
    _datum: Option<Data>,
    _redeemer: Data,
    own_ref: OutputReference,
    tx: Transaction,
  ) {
    spend_handler(own_ref, tx)
  }

  withdraw(redeemer: PartyApproval, account: Credential, tx: Transaction) {
    withdraw_handler(config.identity_hash, redeemer, account, tx)
  }

  else(_ctx) {
    fail @"identity_party: unsupported script purpose"
  }
}
