"""
Module 6: Test 9 — Expire Proposal (End-to-End)

Submits a fresh proposal, waits for the review window to elapse,
then expires it on-chain. Includes temporal diagnostics to verify
that review_window units are consistent with submitted_at and
on-chain current_slot (all should be POSIX ms).

Usage:
    nix-shell shell.nix --run "python scripts/test_expire_e2e.py"
"""

import asyncio
import json
import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")


def blake2b_256(data: str) -> bytes:
    return hashlib.blake2b(data.encode(), digest_size=32).digest()


async def get_genesis_time_params(agent):
    """Get slot-to-POSIX conversion parameters from genesis."""
    try:
        genesis = await agent.context._ogmios._rpc(
            "queryNetwork/genesisConfiguration", {"era": "shelley"}
        )
        sl_ms = genesis.get("slotLength", {}).get("milliseconds", 1000)
        st_str = genesis.get("startTime", "2025-07-09T10:38:04Z")
        st_dt = datetime.fromisoformat(st_str.replace("Z", "+00:00"))
        sys_start_ms = int(st_dt.timestamp() * 1000)
    except Exception:
        sl_ms = 1000
        sys_start_ms = 1752055084000
    return sl_ms, sys_start_ms


def posix_ms_to_human(ms: int) -> str:
    """Convert POSIX ms to human-readable datetime string."""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return f"{ms} ms"


async def run_test():
    state = json.load(open(DEPLOY_STATE_FILE))
    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    validators = state.get("validators", {})
    proposal_cbor = validators.get("proposal.proposal_spend.spend", {}).get("compiled_code", "")
    proposal_mint_cbor = validators.get("proposal.proposal_mint.mint", {}).get("compiled_code", "")

    if not proposal_cbor or not proposal_mint_cbor:
        print("[ERROR] Missing compiled code in deploy_state. Run deploy.py first.")
        return False

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient, ProposalType

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

        # ── Configure reference inputs ──────────────────────────────
        ref_inputs = []
        holders = state.get("holders", {})
        tx_hashes = state.get("tx_hashes", {})
        refs_policy = state.get("refs_token_policy", "")

        from pycardano import Address as PycAddr

        async def find_utxos_at(addr_str, label):
            """Query UTxOs at address with diagnostic output."""
            try:
                utxos = await agent.context.async_utxos(PycAddr.from_primitive(addr_str))
                print(f"  [{label}] {len(utxos)} UTxO(s) at {addr_str[:40]}...")
                for u in utxos:
                    has_datum = u.output.datum is not None
                    has_ma = hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset
                    coin = u.output.amount.coin if hasattr(u.output.amount, 'coin') else u.output.amount
                    print(f"    - {str(u.input.transaction_id)[:16]}...#{u.input.index} coin={coin} datum={'yes' if has_datum else 'no'} ma={'yes' if has_ma else 'no'}")
                return utxos
            except Exception as e:
                print(f"  [{label}] ERROR querying {addr_str[:40]}...: {e}")
                return []

        # Params UTXO
        params_addr = holders.get("params", {}).get("address", "")
        if params_addr:
            expected_tx = tx_hashes.get("params_utxo", "")
            params_utxos = await find_utxos_at(params_addr, "params")
            found_params = False
            # Pass 1: exact tx match
            for u in params_utxos:
                tx_hash = str(u.input.transaction_id)
                if u.output.datum is not None and tx_hash == expected_tx:
                    ref_inputs.append({"tx_hash": tx_hash, "output_index": u.input.index, "address": params_addr})
                    print(f"[OK] GovernanceParams UTXO: {tx_hash[:16]}...#{u.input.index}")
                    found_params = True
                    # Print param values
                    try:
                        pdata = u.output.datum.data.value
                        print(f"\n--- Temporal Diagnostics ---")
                        print(f"  GovernanceParams on-chain values:")
                        print(f"    min_review_window:       {pdata[3]}")
                        print(f"    max_review_window:       {pdata[4]}")
                        print(f"    proposal_cooldown:       {pdata[7]}")
                        print(f"    emergency_review_window: {pdata[16]}")
                    except Exception as e:
                        print(f"  [WARN] Could not read params datum: {e}")
                    break
            # Pass 2: any datum UTxO
            if not found_params:
                for u in params_utxos:
                    if u.output.datum is not None:
                        tx_hash = str(u.input.transaction_id)
                        ref_inputs.append({"tx_hash": tx_hash, "output_index": u.input.index, "address": params_addr})
                        print(f"[OK] GovernanceParams UTXO (fallback): {tx_hash[:16]}...#{u.input.index}")
                        found_params = True
                        break
            if not found_params:
                print(f"[WARN] GovernanceParams UTXO not found!")

        # Oracle UTXO
        oracle_addr = holders.get("oracle", {}).get("address", "")
        if oracle_addr:
            expected_tx = tx_hashes.get("oracle_utxo", "")
            oracle_utxos = await find_utxos_at(oracle_addr, "oracle")
            found_oracle = False
            for u in oracle_utxos:
                tx_hash = str(u.input.transaction_id)
                if u.output.datum is not None and (not expected_tx or tx_hash == expected_tx):
                    ref_inputs.append({"tx_hash": tx_hash, "output_index": u.input.index, "address": oracle_addr})
                    print(f"[OK] Oracle UTXO: {tx_hash[:16]}...#{u.input.index}")
                    found_oracle = True
                    break
            if not found_oracle:
                for u in oracle_utxos:
                    if u.output.datum is not None:
                        tx_hash = str(u.input.transaction_id)
                        ref_inputs.append({"tx_hash": tx_hash, "output_index": u.input.index, "address": oracle_addr})
                        print(f"[OK] Oracle UTXO (fallback): {tx_hash[:16]}...#{u.input.index}")
                        found_oracle = True
                        break
            if not found_oracle:
                print(f"[WARN] Oracle UTXO not found!")

        # CrossRefs NFT
        cross_refs_address = state.get("cross_refs_address", "")
        found_crossrefs = False
        for addr_label, addr in [("script", cross_refs_address), ("oracle", oracle_addr)]:
            if not addr or found_crossrefs:
                continue
            try:
                utxos = await find_utxos_at(addr, f"crossrefs@{addr_label}")
                for u in utxos:
                    if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                        for pid in u.output.amount.multi_asset:
                            if pid.payload.hex() == refs_policy:
                                ref_inputs.append({
                                    "tx_hash": str(u.input.transaction_id),
                                    "output_index": u.input.index,
                                    "address": addr,
                                })
                                print(f"[OK] CrossRefs NFT: {str(u.input.transaction_id)[:16]}...#{u.input.index} at {addr_label}")
                                found_crossrefs = True
                                break
                    if found_crossrefs:
                        break
            except Exception as e:
                print(f"  [WARN] CrossRefs scan failed at {addr_label}: {e}")
        if not found_crossrefs:
            print(f"[WARN] CrossRefs NFT not found!")

        # Agent registry
        agent_did_bytes = None
        registry_hash = state.get("agent_registry_hash", "")
        if registry_hash:
            try:
                from pycardano.hash import ScriptHash as SH
                from pycardano.network import Network as Net
                reg_sh = SH.from_primitive(bytes.fromhex(registry_hash))
                reg_addr = PycAddr(payment_part=reg_sh, network=Net.MAINNET)
                reg_utxos = await agent.context.async_utxos(str(reg_addr))
                for u in reg_utxos:
                    if u.output.datum and hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                        for pid, assets in u.output.amount.multi_asset.items():
                            if pid.payload.hex() == registry_hash:
                                for aname in assets:
                                    agent_did_bytes = aname.payload
                                    ref_inputs.append({
                                        "tx_hash": str(u.input.transaction_id),
                                        "output_index": u.input.index,
                                        "address": str(reg_addr),
                                    })
                                    break
                        if agent_did_bytes:
                            break
                if agent_did_bytes:
                    print(f"[OK] Agent DID: {agent_did_bytes.hex()[:16]}...")
            except Exception as e:
                print(f"[WARN] Agent registry lookup failed: {e}")

        if not agent_did_bytes:
            print("[ERROR] No registered agent DID found. Cannot submit proposal.")
            return False

        if ref_inputs:
            gov.set_governance_reference_inputs(ref_inputs)
            print(f"[OK] {len(ref_inputs)} reference inputs configured")

        # Set reference UTxO for CIP-33
        proposal_spend_ref_tx = tx_hashes.get("proposal_spend_ref", "")
        if proposal_spend_ref_tx:
            gov.set_reference_utxos({
                "proposal": {"tx_hash": proposal_spend_ref_tx, "output_index": 0, "address": wallet["address"]}
            })

        # ── DIAGNOSTIC: Current time ────────────────────────────────
        sl_ms, sys_start_ms = await get_genesis_time_params(agent)
        tip = await agent.context._ogmios.query_network_tip()
        current_slot = tip.get("slot", 0)
        current_posix_ms = current_slot * sl_ms + sys_start_ms

        print(f"\n  Chain tip:")
        print(f"    slot:           {current_slot}")
        print(f"    posix_ms:       {current_posix_ms}")
        print(f"    human:          {posix_ms_to_human(current_posix_ms)}")
        print(f"    slot_length_ms: {sl_ms}")
        print(f"    sys_start_ms:   {sys_start_ms}")

        # ── Step 1: Submit a fresh proposal ─────────────────────────
        print(f"\n{'='*60}")
        print("Step 1: Submit a fresh proposal")
        print(f"{'='*60}")

        proposal_hash = blake2b_256(f"Test 9 expire proposal {current_slot}")

        try:
            submit_result = await gov.validated_submit_proposal(
                proposer_did=agent_did_bytes,
                proposal_hash=proposal_hash,
                proposal_type=ProposalType.general_suggestion(),
                storage_uri="ipfs://QmExpireTestProposal",
                stake_lovelace=25_000_000,
            )
        except Exception as e:
            print(f"[ERROR] Submit failed: {e}")
            import traceback; traceback.print_exc()
            return False

        submit_tx = submit_result["tx_hash"]
        print(f"[OK] Proposal submitted: {submit_tx}")
        print(f"     Explorer: {state['explorer_url']}/tx/{submit_tx}")

        # Wait for confirmation
        print("\n    Waiting 20s for tx confirmation...")
        await asyncio.sleep(20)

        # ── Step 2: Read the on-chain datum to check review window ──
        print(f"\n{'='*60}")
        print("Step 2: Read proposal datum & check review window")
        print(f"{'='*60}")

        prop_addr = gov._script_address(proposal_cbor)
        prop_utxos = await agent.context.async_utxos(PycAddr.from_primitive(prop_addr))

        proposal_utxo = None
        activity_utxo = None
        for u in prop_utxos:
            if str(u.input.transaction_id) != submit_tx:
                continue
            if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                for pid, assets in u.output.amount.multi_asset.items():
                    for aname in assets:
                        if aname.payload[:5] == b"prop_":
                            proposal_utxo = u
                        elif aname.payload[:5] == b"pact_":
                            activity_utxo = u

        if not proposal_utxo:
            print("[ERROR] Proposal UTxO not found after submission")
            return False
        if not activity_utxo:
            print("[ERROR] Activity UTxO not found after submission")
            return False

        print(f"[OK] Proposal UTxO: {submit_tx[:16]}...#{proposal_utxo.input.index}")
        print(f"[OK] Activity UTxO: {submit_tx[:16]}...#{activity_utxo.input.index}")

        # Read datum fields
        d = proposal_utxo.output.datum.data
        submitted_at = d.value[6]
        review_window = d.value[7]
        expiry_time = submitted_at + review_window

        # Re-query current time
        tip2 = await agent.context._ogmios.query_network_tip()
        current_slot2 = tip2.get("slot", 0)
        current_posix_ms2 = current_slot2 * sl_ms + sys_start_ms

        print(f"\n  Proposal datum:")
        print(f"    submitted_at:    {submitted_at} ({posix_ms_to_human(submitted_at)})")
        print(f"    review_window:   {review_window}")
        print(f"    expiry_time:     {expiry_time} ({posix_ms_to_human(expiry_time)})")
        print(f"    current_time:    {current_posix_ms2} ({posix_ms_to_human(current_posix_ms2)})")

        # Diagnose units
        diff_ms = expiry_time - submitted_at
        if diff_ms < 60_000:
            print(f"\n  [DIAG] review_window ({review_window}) added to submitted_at gives")
            print(f"         a {diff_ms/1000:.1f} second window. This confirms a units mismatch")
            print(f"         (review_window is in slots/seconds, submitted_at is in POSIX ms).")
            print(f"         BUG O CONFIRMED.")
        elif diff_ms < 3_600_000:
            print(f"\n  [DIAG] Effective review window: {diff_ms/60_000:.1f} minutes")
        else:
            print(f"\n  [DIAG] Effective review window: {diff_ms/3_600_000:.1f} hours")

        # ── Step 3: Wait for review window to elapse ────────────────
        print(f"\n{'='*60}")
        print("Step 3: Wait for review window to elapse")
        print(f"{'='*60}")

        if current_posix_ms2 > expiry_time:
            remaining_ms = 0
            print(f"[OK] Review window already elapsed (Bug O: window is ~{diff_ms/1000:.0f}s)")
        else:
            remaining_ms = expiry_time - current_posix_ms2
            remaining_s = remaining_ms / 1000
            if remaining_s > 600:
                print(f"[WAIT] Review window has NOT elapsed. {remaining_s/3600:.1f} hours remaining.")
                print(f"       Re-run this script after the window expires, or fix the")
                print(f"       review_window units and redeploy GovernanceParams.")
                return False
            else:
                # 90s buffer: validated_expire_proposal sets validity_start=tip-60,
                # so on-chain time is ~60s behind. Need extra margin.
                wait_s = remaining_s + 90
                print(f"[WAIT] Waiting {wait_s:.0f}s for review window to elapse...")
                await asyncio.sleep(wait_s)

        # ── Step 4: Expire the proposal ─────────────────────────────
        print(f"\n{'='*60}")
        print("Step 4: Expire the proposal")
        print(f"{'='*60}")

        try:
            expire_result = await gov.validated_expire_proposal(
                utxo_ref={"tx_hash": submit_tx, "output_index": proposal_utxo.input.index},
                activity_utxo_ref={
                    "tx_hash": str(activity_utxo.input.transaction_id),
                    "output_index": activity_utxo.input.index,
                },
                proposer_did=agent_did_bytes,
            )
        except Exception as e:
            print(f"[ERROR] Expire failed: {e}")
            import traceback; traceback.print_exc()
            return False

        expire_tx = expire_result["tx_hash"]
        print(f"[OK] Proposal expired: {expire_tx}")
        print(f"     Explorer: {state['explorer_url']}/tx/{expire_tx}")

        # ── Step 5: Verify ──────────────────────────────────────────
        print(f"\n{'='*60}")
        print("Step 5: Verification")
        print(f"{'='*60}")

        print(f"  Proposal token: BURNED (validated by on-chain validator)")
        print(f"  Proposer stake: RETURNED (25 AP3X, validated by on-chain validator)")
        print(f"  Activity count: DECREMENTED (validated by on-chain validator)")
        print(f"\n[PASS] Test 9 — Expire Proposal: SUCCESS")

        if diff_ms < 60_000:
            print(f"\n[NOTE] Bug O confirmed: review_window units mismatch.")
            print(f"       Effective window was ~{diff_ms/1000:.0f}s instead of spec's ~3-7 days.")
            print(f"       Fix: convert all GovernanceParams temporal values to POSIX ms.")

        return True


async def main():
    print("=" * 60)
    print("Module 6: Test 9 — Expire Proposal (End-to-End)")
    print("=" * 60)

    success = await run_test()

    print(f"\n{'='*60}")
    if success:
        print("RESULT: PASS")
    else:
        print("RESULT: FAIL")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
