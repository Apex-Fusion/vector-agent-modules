"""
Module 6: Standalone Test — validated_submit_proposal (Test 6)

Runs ONLY the validated submit flow in isolation (no prior lock/spend tests).
This avoids stale UTxO issues from tests 1-5.

Usage:
    nix-shell shell.nix --run "python scripts/test_validated_submit.py"
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")


async def main():
    # Load state
    with open(DEPLOY_STATE_FILE) as f:
        state = json.load(f)
    with open(WALLET_FILE) as f:
        wallet = json.load(f)

    validators = state.get("validators", {})
    tx_hashes = state.get("tx_hashes", {})
    holders = state.get("holders", {})

    proposal_cbor = validators.get("proposal.proposal_spend.spend", {}).get("compiled_code", "")
    proposal_mint_cbor = validators.get("proposal.proposal_mint.mint", {}).get("compiled_code", "")
    critique_cbor = validators.get("critique.critique_spend.spend", {}).get("compiled_code", "")
    endorsement_cbor = validators.get("critique.endorsement_spend.spend", {}).get("compiled_code", "")
    critique_mint_cbor = validators.get("critique.critique_mint.mint", {}).get("compiled_code", "")

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient, ProposalType

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))
    skey_path = str(Path("wallets/payment.skey").absolute())

    print("=" * 60)
    print("Test 6: validated_submit_proposal (standalone)")
    print("=" * 60)
    print(f"Wallet: {wallet['address']}")
    print(f"Proposal spend: {state.get('proposal_validator_hash', '')[:16]}...")

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        balance = await agent.get_balance()
        print(f"Balance: {balance.ada} AP3X")

        gov = GovernanceClient(
            agent,
            proposal_script_cbor=proposal_cbor,
            critique_script_cbor=critique_cbor,
            endorsement_script_cbor=endorsement_cbor,
            proposal_mint_cbor=proposal_mint_cbor,
            critique_mint_cbor=critique_mint_cbor,
        )

        # --- Discover reference inputs ---
        print("\n--- Discovering reference inputs ---")
        ref_inputs = []

        # 1. GovernanceParams (by exact tx_hash from deploy_state)
        params_addr = holders.get("params", {}).get("address", "")
        expected_params_tx = tx_hashes.get("params_utxo", "")
        if params_addr and expected_params_tx:
            from pycardano import Address
            utxos = await agent.context.async_utxos(Address.from_primitive(params_addr))
            found = False
            for u in utxos:
                if str(u.input.transaction_id) == expected_params_tx:
                    ref_inputs.append({"tx_hash": expected_params_tx, "output_index": u.input.index, "address": params_addr})
                    print(f"[OK] Params: {expected_params_tx[:16]}...#{u.input.index}")
                    found = True
                    break
            if not found:
                print(f"[ERR] Params UTxO {expected_params_tx[:16]}... NOT FOUND at {params_addr[:20]}...")

        # 2. Oracle (by exact tx_hash from deploy_state)
        oracle_addr = holders.get("oracle", {}).get("address", "")
        expected_oracle_tx = tx_hashes.get("oracle_utxo", "")
        if oracle_addr and expected_oracle_tx:
            from pycardano import Address
            utxos = await agent.context.async_utxos(Address.from_primitive(oracle_addr))
            found = False
            for u in utxos:
                if str(u.input.transaction_id) == expected_oracle_tx:
                    ref_inputs.append({"tx_hash": expected_oracle_tx, "output_index": u.input.index, "address": oracle_addr})
                    print(f"[OK] Oracle: {expected_oracle_tx[:16]}...#{u.input.index}")
                    found = True
                    break
            if not found:
                print(f"[ERR] Oracle UTxO {expected_oracle_tx[:16]}... NOT FOUND at {oracle_addr[:20]}...")

        # 3. CrossRefs NFT (by exact tx_hash from deploy_state)
        cross_refs_addr = state.get("cross_refs_address", oracle_addr)
        expected_crossrefs_tx = tx_hashes.get("cross_refs_nft", "")
        if cross_refs_addr and expected_crossrefs_tx:
            from pycardano import Address
            utxos = await agent.context.async_utxos(Address.from_primitive(cross_refs_addr))
            found = False
            for u in utxos:
                if str(u.input.transaction_id) == expected_crossrefs_tx:
                    ref_inputs.append({"tx_hash": expected_crossrefs_tx, "output_index": u.input.index, "address": cross_refs_addr})
                    has_datum = u.output.datum is not None
                    print(f"[OK] CrossRefs: {expected_crossrefs_tx[:16]}...#{u.input.index} (datum={'yes' if has_datum else 'NO!'})")
                    # Verify hashes
                    if has_datum:
                        from pycardano.plutus import RawPlutusData
                        if isinstance(u.output.datum, RawPlutusData) and hasattr(u.output.datum.data, 'value'):
                            rf = u.output.datum.data.value
                            if isinstance(rf, list) and len(rf) == 4:
                                xr_spend = rf[0].hex() if isinstance(rf[0], bytes) else str(rf[0])
                                xr_mint = rf[2].hex() if isinstance(rf[2], bytes) else str(rf[2])
                                exp_spend = state.get("proposal_validator_hash", "")
                                exp_mint = validators.get("proposal.proposal_mint.mint", {}).get("hash", "")
                                ok_spend = xr_spend == exp_spend
                                ok_mint = xr_mint == exp_mint
                                print(f"     proposal_spend: {'OK' if ok_spend else 'MISMATCH! ' + xr_spend[:16] + ' != ' + exp_spend[:16]}")
                                print(f"     proposal_mint:  {'OK' if ok_mint else 'MISMATCH! ' + xr_mint[:16] + ' != ' + exp_mint[:16]}")
                                if not ok_spend or not ok_mint:
                                    print("[ERR] CrossRefs hashes don't match! Redeploy needed.")
                                    return
                    found = True
                    break
            if not found:
                print(f"[ERR] CrossRefs NFT {expected_crossrefs_tx[:16]}... NOT FOUND at {cross_refs_addr[:20]}...")

        # 4. Agent Registry
        registry_hash = state.get("agent_registry_hash", "")
        agent_did_bytes = None
        if registry_hash:
            from pycardano.hash import ScriptHash
            from pycardano.network import Network
            reg_sh = ScriptHash.from_primitive(bytes.fromhex(registry_hash))
            reg_addr = Address(payment_part=reg_sh, network=Network.MAINNET)
            reg_utxos = await agent.context.async_utxos(str(reg_addr))
            for u in reg_utxos:
                if u.output.datum and hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                    for pid, assets in u.output.amount.multi_asset.items():
                        if pid.payload.hex() == registry_hash:
                            for aname in assets:
                                agent_did_bytes = aname.payload
                                ref_inputs.append({"tx_hash": str(u.input.transaction_id), "output_index": u.input.index, "address": str(reg_addr)})
                                print(f"[OK] Agent DID: {agent_did_bytes.hex()[:16]}...")
                                break
                    if agent_did_bytes:
                        break
            if not agent_did_bytes:
                print("[ERR] No registered agent found in registry")
                return

        print(f"\nTotal reference inputs: {len(ref_inputs)}")
        if len(ref_inputs) < 4:
            print("[ERR] Missing reference inputs! Need 4 (params, oracle, crossrefs, registry)")
            return

        gov.set_governance_reference_inputs(ref_inputs)

        # --- Run validated_submit_proposal ---
        print("\n--- Running validated_submit_proposal ---")
        proposal_hash = hashlib.blake2b(b"Standalone test 6 proposal", digest_size=32).digest()

        try:
            result = await gov.validated_submit_proposal(
                proposer_did=agent_did_bytes,
                proposal_hash=proposal_hash,
                proposal_type=ProposalType.general_suggestion(),
                storage_uri="ipfs://QmStandaloneTest6",
                stake_lovelace=25_000_000,
            )
            print(f"\n[PASS] Validated proposal submitted!")
            print(f"  TX: {result['tx_hash']}")
            print(f"  Lock TX: {result['lock_tx_hash']}")
            print(f"  Token: {result['proposal_token_name']}")
            print(f"  Explorer: {state.get('explorer_url', '')}/tx/{result['tx_hash']}")
        except Exception as e:
            err_str = str(e)
            print(f"\n[FAIL] {err_str[:200]}")

            # Try to extract traces from the error
            if 'traces' in err_str:
                import re
                traces_match = re.search(r"'traces': \[([^\]]*)\]", err_str)
                if traces_match and traces_match.group(1).strip():
                    print(f"\n  TRACES: {traces_match.group(1)}")
                else:
                    print(f"\n  (no traces — validator returned false without hitting any fail @\"...\" check)")

            import traceback
            traceback.print_exc()

        final_balance = await agent.get_balance()
        print(f"\nFinal balance: {final_balance.ada} AP3X")


if __name__ == "__main__":
    asyncio.run(main())
