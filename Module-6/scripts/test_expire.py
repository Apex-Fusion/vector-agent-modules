"""
Module 6: Test Expire Proposal

Expires a previously-submitted proposal after its review window has elapsed.
This is a time-dependent test that must be run separately from the main
smoke test, at least MIN_REVIEW_WINDOW slots after the proposal was submitted.

Usage:
    # First, note the proposal tx_hash from smoke_test.py test 6
    nix-shell shell.nix --run "python scripts/test_expire.py <proposal_tx_hash> [output_index]"

    # Or expire the most recent proposal at the script address:
    nix-shell shell.nix --run "python scripts/test_expire.py --latest"
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")


async def test_expire():
    state = json.load(open(DEPLOY_STATE_FILE))
    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    validators = state.get("validators", {})
    proposal_cbor = validators.get("proposal.proposal_spend.spend", {}).get("compiled_code", "")
    proposal_mint_cbor = validators.get("proposal.proposal_mint.mint", {}).get("compiled_code", "")

    if not proposal_cbor or not proposal_mint_cbor:
        print("[ERROR] Missing compiled code in deploy_state. Run deploy.py first.")
        return

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        gov = GovernanceClient(
            agent,
            proposal_script_cbor=proposal_cbor,
            proposal_mint_cbor=proposal_mint_cbor,
        )

        # Set reference inputs
        ref_inputs = []
        holders = state.get("holders", {})
        tx_hashes = state.get("tx_hashes", {})
        refs_policy = state.get("refs_token_policy", "")

        from pycardano import Address as PycAddr

        for holder_name, holder_key in [("params", "params_utxo"), ("oracle", "oracle_utxo")]:
            addr = holders.get(holder_name, {}).get("address", "")
            expected_tx = tx_hashes.get(holder_key, "")
            if addr:
                try:
                    utxos = await agent.context.async_utxos(PycAddr.from_primitive(addr))
                    for u in utxos:
                        if u.output.datum is not None:
                            if not expected_tx or str(u.input.transaction_id) == expected_tx:
                                ref_inputs.append({
                                    "tx_hash": str(u.input.transaction_id),
                                    "output_index": u.input.index,
                                    "address": addr,
                                })
                                break
                except Exception as e:
                    print(f"[WARN] Could not find {holder_name} UTXO: {e}")

        # Find CrossRefs NFT
        cross_refs_address = state.get("cross_refs_address", "")
        for addr_label, addr in [("script", cross_refs_address), ("oracle", holders.get("oracle", {}).get("address", ""))]:
            if not addr:
                continue
            try:
                utxos = await agent.context.async_utxos(PycAddr.from_primitive(addr))
                for u in utxos:
                    if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                        for pid in u.output.amount.multi_asset:
                            if pid.payload.hex() == refs_policy:
                                ref_inputs.append({
                                    "tx_hash": str(u.input.transaction_id),
                                    "output_index": u.input.index,
                                    "address": addr,
                                })
                                break
            except Exception:
                pass

        if ref_inputs:
            gov.set_governance_reference_inputs(ref_inputs)
            print(f"[OK] {len(ref_inputs)} reference inputs configured")

        # Find the proposal to expire
        proposal_address = gov._script_address(proposal_cbor)
        proposal_utxos = await agent.context.async_utxos(PycAddr.from_primitive(proposal_address))

        target_tx = None
        target_idx = 0

        if len(sys.argv) > 1 and sys.argv[1] != "--latest":
            target_tx = sys.argv[1]
            target_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        elif "--latest" in sys.argv:
            # Find the most recent proposal token UTxO
            for u in proposal_utxos:
                if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                    for pid, assets_map in u.output.amount.multi_asset.items():
                        for aname in assets_map:
                            if aname.payload[:5] == b"prop_":
                                target_tx = str(u.input.transaction_id)
                                target_idx = u.input.index
                                break
        else:
            print("Usage: python scripts/test_expire.py <tx_hash> [output_index]")
            print("       python scripts/test_expire.py --latest")
            return

        if not target_tx:
            print("[ERROR] No proposal found to expire")
            return

        print(f"[INFO] Target proposal: {target_tx[:16]}...#{target_idx}")

        # Read the proposal datum to check if review window has elapsed
        target_utxo = None
        for u in proposal_utxos:
            if str(u.input.transaction_id) == target_tx and u.input.index == target_idx:
                target_utxo = u
                break

        if not target_utxo:
            print(f"[ERROR] UTxO {target_tx[:16]}...#{target_idx} not found at proposal address")
            return

        if target_utxo.output.datum:
            from pycardano.plutus import RawPlutusData
            d = target_utxo.output.datum.data
            submitted_at = d.value[6]  # submitted_at field
            review_window = d.value[7]  # review_window field
            expiry_time = submitted_at + review_window

            tip = await agent.context._ogmios.query_network_tip()
            current_slot = tip.get("slot", 0)

            # Get genesis for POSIX conversion
            try:
                genesis = await agent.context._ogmios._rpc(
                    "queryNetwork/genesisConfiguration", {"era": "shelley"}
                )
                sl_ms = genesis.get("slotLength", {}).get("milliseconds", 1000)
                st_str = genesis.get("startTime", "2025-07-09T10:38:04Z")
                from datetime import datetime, timezone
                st_dt = datetime.fromisoformat(st_str.replace("Z", "+00:00"))
                sys_start_ms = int(st_dt.timestamp() * 1000)
            except Exception:
                sl_ms = 1000
                sys_start_ms = 1752055084000

            current_posix_ms = current_slot * sl_ms + sys_start_ms

            print(f"[INFO] submitted_at:  {submitted_at} (POSIX ms)")
            print(f"[INFO] review_window: {review_window}")
            print(f"[INFO] expiry_time:   {expiry_time}")
            print(f"[INFO] current_time:  {current_posix_ms} (POSIX ms)")

            if current_posix_ms <= expiry_time:
                remaining_ms = expiry_time - current_posix_ms
                remaining_h = remaining_ms / (3600 * 1000)
                print(f"\n[WAIT] Review window has NOT elapsed yet.")
                print(f"       Time remaining: {remaining_h:.1f} hours ({remaining_ms / 1000:.0f} seconds)")
                print(f"       Re-run this script after the window expires.")
                return

        # Find the activity UTxO for the proposer
        proposer_did = target_utxo.output.datum.data.value[0]  # proposer_did
        activity_ref = None
        for u in proposal_utxos:
            if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                for pid, assets_map in u.output.amount.multi_asset.items():
                    for aname in assets_map:
                        if aname.payload[:5] == b"pact_":
                            activity_ref = {
                                "tx_hash": str(u.input.transaction_id),
                                "output_index": u.input.index,
                            }
                            break

        if not activity_ref:
            print("[ERROR] Activity UTxO not found")
            return

        print(f"\n[INFO] Expiring proposal...")
        result = await gov.validated_expire_proposal(
            utxo_ref={"tx_hash": target_tx, "output_index": target_idx},
            activity_utxo_ref=activity_ref,
            proposer_did=proposer_did,
        )
        print(f"[PASS] Proposal expired: {result['tx_hash']}")
        explorer = state.get("explorer_url", "")
        if explorer:
            print(f"       Explorer: {explorer}/tx/{result['tx_hash']}")


if __name__ == "__main__":
    asyncio.run(test_expire())
