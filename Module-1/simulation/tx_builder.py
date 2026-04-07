"""
TX Builder Library — Reusable transaction constructors for all Module 1 actions.

Each function builds, evaluates, and submits one transaction type.
Extracted from testnet/deploy_and_run_v10.py and generalized for simulation use.
"""
import cbor2
import hashlib
import os

from pycardano import (
    Address, TransactionBuilder, TransactionOutput,
    UTxO, RawCBOR, Redeemer,
    MultiAsset, Asset, AssetName, ScriptHash, Value,
    ExecutionUnits, ScriptPubkey, ScriptAll,
)

from simulation.chain import (
    OgmiosContext, submit_tx, tx_to_bytes, wait_confirm,
    ensure_collateral, get_wallet_utxos_no_collateral,
    evaluate_and_rebuild, resolve_utxo, resolve_ref_utxo,
    claim_token_name, challenge_token_name, juror_token_name,
    select_jurors_prng, slot_to_posix_ms, posix_ms_to_slot,
    SYSTEM_START_UNIX,
)
from simulation.config import AP3X_POLICY_ID, AP3X_ASSET_NAME, NETWORK


# ═══════════════════════════════════════════════════════════════════════
# DEPLOYMENT STATE (set after deploying ref scripts)
# ═══════════════════════════════════════════════════════════════════════

class DeploymentState:
    """Holds references to deployed scripts and on-chain state."""

    def __init__(self, deployment_json: dict):
        self.claim_hash = deployment_json["hashes"]["claim"]
        self.challenge_hash = deployment_json["hashes"]["challenge"]
        self.jury_pool_hash = deployment_json["hashes"]["jury_pool"]

        self.claim_addr = Address(ScriptHash(bytes.fromhex(self.claim_hash)), network=NETWORK)
        self.challenge_addr = Address(ScriptHash(bytes.fromhex(self.challenge_hash)), network=NETWORK)
        self.jury_pool_addr = Address(ScriptHash(bytes.fromhex(self.jury_pool_hash)), network=NETWORK)

        # Reference UTxOs (loaded lazily)
        self._claim_ref = deployment_json.get("claim_ref")
        self._challenge_ref = deployment_json.get("challenge_ref")
        self._jury_pool_ref = deployment_json.get("jury_pool_ref")
        self._cross_refs_utxo = deployment_json.get("cross_refs_utxo")
        self._params_utxo = deployment_json.get("params_utxo")

        self._claim_ref_utxo = None
        self._challenge_ref_utxo = None
        self._jury_pool_ref_utxo = None
        self._cross_refs_resolved = None
        self._params_resolved = None

    def resolve_refs(self):
        """Resolve all reference UTxOs from chain."""
        if self._claim_ref:
            txid, idx = self._claim_ref.split("#")
            self._claim_ref_utxo = resolve_ref_utxo(txid, int(idx))
        if self._challenge_ref:
            txid, idx = self._challenge_ref.split("#")
            self._challenge_ref_utxo = resolve_ref_utxo(txid, int(idx))
        if self._jury_pool_ref:
            txid, idx = self._jury_pool_ref.split("#")
            self._jury_pool_ref_utxo = resolve_ref_utxo(txid, int(idx))
        if self._cross_refs_utxo:
            txid, idx = self._cross_refs_utxo.split("#")
            self._cross_refs_resolved = resolve_utxo(txid, int(idx))
        if self._params_utxo:
            txid, idx = self._params_utxo.split("#")
            self._params_resolved = resolve_utxo(txid, int(idx))

    @property
    def claim_ref_utxo(self):
        if self._claim_ref_utxo is None:
            self.resolve_refs()
        return self._claim_ref_utxo

    @property
    def challenge_ref_utxo(self):
        if self._challenge_ref_utxo is None:
            self.resolve_refs()
        return self._challenge_ref_utxo

    @property
    def jury_pool_ref_utxo(self):
        if self._jury_pool_ref_utxo is None:
            self.resolve_refs()
        return self._jury_pool_ref_utxo

    @property
    def cross_refs_utxo(self):
        if self._cross_refs_resolved is None:
            self.resolve_refs()
        return self._cross_refs_resolved

    @property
    def params_utxo(self):
        if self._params_resolved is None:
            self.resolve_refs()
        return self._params_resolved


# ═══════════════════════════════════════════════════════════════════════
# ACTION: SUBMIT CLAIM
# ═══════════════════════════════════════════════════════════════════════

def build_submit_claim(context: OgmiosContext, deployment: DeploymentState,
                       skey, vkey, wallet_addr,
                       claimer_did_hex: str, stake_amount: int,
                       challenge_window_ms: int = 1_800_000,
                       evidence_hash: bytes = None) -> dict:
    """Build and submit a SubmitClaim transaction.
    
    Returns: {tx_hash, claim_utxo_ref, claim_token_hex, submitted_at}
    """
    ensure_collateral(context, skey, vkey, wallet_addr)
    current_slot = context.last_block_slot
    submitted_at = slot_to_posix_ms(current_slot)

    wallet_utxos = get_wallet_utxos_no_collateral(context, wallet_addr)
    sorted_utxos = sorted(wallet_utxos,
        key=lambda u: (bytes(u.input.transaction_id).hex(), u.input.index))
    seed_utxo = sorted_utxos[0]
    seed_tx_hash = bytes(seed_utxo.input.transaction_id)
    seed_tx_idx = seed_utxo.input.index

    token_bytes = claim_token_name(seed_tx_hash, seed_tx_idx)
    token_an = AssetName(token_bytes)

    if evidence_hash is None:
        evidence_hash = hashlib.blake2b(
            f"claim-{claimer_did_hex[:16]}-{current_slot}".encode(),
            digest_size=32).digest()

    claim_policy = ScriptHash(bytes.fromhex(deployment.claim_hash))
    ap3x_policy = ScriptHash(bytes.fromhex(AP3X_POLICY_ID))
    ap3x_name = AssetName(bytes.fromhex(AP3X_ASSET_NAME))

    # Claim datum
    claim_datum_cbor = cbor2.dumps(cbor2.CBORTag(121, [
        cbor2.CBORTag(121, [bytes(vkey.hash())]),  # claimer_credential
        bytes.fromhex(claimer_did_hex),             # claimer_did
        stake_amount,                                # stake_amount
        evidence_hash,                               # evidence_hash
        b"ipfs://sim-evidence",                      # evidence_uri
        submitted_at,                                # submitted_at
        challenge_window_ms,                         # challenge_window
        cbor2.CBORTag(121, []),                      # state = Open (Constr0)
    ]))

    # Mint
    mint_ma = MultiAsset()
    ma = Asset()
    ma[token_an] = 1
    mint_ma[claim_policy] = ma

    # Output
    out_nft_ma = MultiAsset()
    na = Asset()
    na[token_an] = 1
    out_nft_ma[claim_policy] = na
    out_stake_ma = MultiAsset()
    sa = Asset()
    sa[ap3x_name] = stake_amount
    out_stake_ma[ap3x_policy] = sa

    seed_ref_cbor = cbor2.CBORTag(121, [seed_tx_hash, seed_tx_idx])
    mint_redeemer_cbor = RawCBOR(cbor2.dumps(cbor2.CBORTag(121, [seed_ref_cbor])))
    mint_redeemer = Redeemer(mint_redeemer_cbor, ExecutionUnits(mem=500_000, steps=200_000_000))

    def build_tx(red):
        b = TransactionBuilder(context)
        b.fee_buffer = 500_000
        for u in wallet_utxos:
            b.add_input(u)
        b.mint = mint_ma
        b.add_minting_script(deployment.claim_ref_utxo, red)
        b.reference_inputs.add(deployment.cross_refs_utxo)
        b.reference_inputs.add(deployment.params_utxo)
        b.add_output(TransactionOutput(
            deployment.claim_addr,
            Value(4_000_000, out_nft_ma + out_stake_ma),
            datum=RawCBOR(claim_datum_cbor)))
        b.required_signers = [vkey.hash()]
        b.validity_start = current_slot - 60
        b.ttl = current_slot + 3600
        return b

    builder = build_tx(mint_redeemer)
    _, budgets = evaluate_and_rebuild(builder, skey, vkey, wallet_addr, context)

    for key, bud in budgets.items():
        if "mint" in key:
            mint_redeemer = Redeemer(mint_redeemer_cbor,
                ExecutionUnits(mem=bud["mem"], steps=bud["cpu"]))

    builder2 = build_tx(mint_redeemer)
    tx = builder2.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx_to_bytes(tx))

    return {
        "tx_hash": tx_hash,
        "claim_utxo_ref": f"{tx_hash}#0",
        "claim_token_hex": token_bytes.hex(),
        "submitted_at": submitted_at,
        "stake_amount": stake_amount,
        "claimer_did": claimer_did_hex,
    }


# ═══════════════════════════════════════════════════════════════════════
# PLACEHOLDER: Additional actions to be implemented in Phase B
# ═══════════════════════════════════════════════════════════════════════

# The following will be extracted from deploy_and_run_v10.py:
# - build_open_challenge()
# - build_transition_to_voting()
# - build_select_jury()
# - build_commit_vote()
# - build_reveal_vote()
# - build_resolve_jury()
# - build_distribute_rewards()
# - build_cleanup_resolved()
# - build_withdraw_claim()
# - build_forfeit_claim()
# - build_timeout_resolve()
# - build_register_juror()
# - build_withdraw_juror()
# - build_slash_non_reveal()
#
# Each follows the same pattern:
# 1. Ensure collateral
# 2. Resolve UTxOs
# 3. Build redeemer + datum update
# 4. Build TX with reference scripts
# 5. Evaluate execution budgets
# 6. Rebuild with correct budgets
# 7. Sign and submit
# 8. Return result dict
