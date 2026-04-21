"""
Module 6: End-to-End Smoke Test
Tests the full governance lifecycle on Vector testnet.

Usage:
    nix-shell shell.nix --run "python scripts/smoke_test.py"

Prerequisites:
    - Wallet funded with at least 50 AP3X
    - Full deployment completed (run deploy.py first)
    - GovernanceParams, Oracle, Treasury UTxOs deployed
    - GovernanceCrossRefs NFT minted

Test phases:
    Phase 1 (Lock):  Tests 1-4 — lock actions (bypass validators, store data at script addresses)
    Phase 2 (Spend): Test 5   — spend actions via on-chain validators
    Phase 3 (Lifecycle): Tests 6-9 — full validator-mediated lifecycle (requires multi-validator
                         mint+spend SDK support; skipped until SDK implements mint transactions)
"""

import asyncio
import json
import os
import sys
import hashlib
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path for sdk imports
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")


def load_deploy_state() -> dict:
    """Load deployment state."""
    if not DEPLOY_STATE_FILE.exists():
        raise FileNotFoundError(
            "Deploy state not found. Run: python scripts/deploy.py"
        )
    with open(DEPLOY_STATE_FILE) as f:
        return json.load(f)


def blake2b_256(data: str) -> bytes:
    """Compute blake2b-256 hash of a string."""
    return hashlib.blake2b(data.encode(), digest_size=32).digest()


async def smoke_test():
    """Run end-to-end governance smoke test."""
    print("=" * 60)
    print("Module 6: Self-Improvement Module — Smoke Test")
    print("=" * 60)

    state = load_deploy_state()

    print(f"\nNetwork: {state['network']}")
    print(f"Wallet: {state['wallet_address']}")
    print(f"Explorer: {state['explorer_url']}")
    print(f"Proposal hash: {state.get('proposal_validator_hash', 'NOT SET')}")
    print(f"Critique hash: {state.get('critique_validator_hash', 'NOT SET')}")

    # Check if deployment was completed (tx_hashes should be populated)
    tx_hashes = state.get("tx_hashes", {})
    if not tx_hashes:
        print("\n[WARN] No deployment transactions found. Run deploy.py first.")
        print("       Running offline validation instead...")
        await offline_validation(state)
        return

    print("\n--- Test Plan ---")
    print("  Phase 1 — Lock actions (data stored at script addresses):")
    print("    1. Query wallet balance")
    print("    2. Submit a GeneralSuggestion proposal (25 AP3X stake)")
    print("    3. Submit a Supportive critique (5 AP3X stake)")
    print("    4. Endorse the proposal (10 AP3X stake)")
    print("  Phase 2 — On-chain spend actions (validators execute):")
    print("    5. Withdraw the endorsement (get 10 AP3X back)")
    print("  Phase 3 — Validated submit & withdraw (mint+spend):")
    print("    6. Submit proposal with validation (mint token + activity tracking)")
    print("    7. Withdraw proposal (burn token + recover stake)")
    print("    8. Adopt proposal (requires oracle — skipped)")
    print("    9. Expire proposal (requires time — skipped)")

    # Import SDK
    try:
        from vector_agent import VectorAgent
        from vector_agent.governance import (
            GovernanceClient,
            ProposalType,
            ProposalAction,
            CritiqueType,
        )
        print("\n[OK] SDK imported successfully")
    except ImportError as e:
        print(f"\n[SKIP] SDK import failed: {e}")
        print("       Install SDK: pip install apex-fusion-agent-sdk")
        print("\n--- Offline Validation ---")
        await offline_validation(state)
        return

    # Connect to Vector testnet
    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    wallet = json.load(open(WALLET_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    # Extract APPLIED compiled script CBOR from deploy state
    validators = state.get("validators", {})
    proposal_cbor = validators.get("proposal.proposal_spend.spend", {}).get("compiled_code", "")
    critique_cbor = validators.get("critique.critique_spend.spend", {}).get("compiled_code", "")
    endorsement_cbor = validators.get("critique.endorsement_spend.spend", {}).get("compiled_code", "")
    proposal_mint_cbor = validators.get("proposal.proposal_mint.mint", {}).get("compiled_code", "")
    critique_mint_cbor = validators.get("critique.critique_mint.mint", {}).get("compiled_code", "")

    if not proposal_cbor or not critique_cbor:
        print("\n[ERROR] Missing compiled code in deploy state. Run: python scripts/deploy.py")
        return

    try:
        async with VectorAgent(
            ogmios_url=ogmios_url,
            submit_url=submit_url,
            skey_path=skey_path,
        ) as agent:
            gov = GovernanceClient(
                agent,
                proposal_script_cbor=proposal_cbor,
                critique_script_cbor=critique_cbor,
                endorsement_script_cbor=endorsement_cbor,
                proposal_mint_cbor=proposal_mint_cbor,
                critique_mint_cbor=critique_mint_cbor,
            )
            # Set proposal reference UTxO for CIP-33 (reduces tx size for validated_submit)
            proposal_spend_ref_tx = tx_hashes.get("proposal_spend_ref", "")
            if proposal_spend_ref_tx:
                gov.set_reference_utxos({"proposal": {"tx_hash": proposal_spend_ref_tx, "output_index": 0, "address": wallet["address"]}})

            # Find reference inputs for GovernanceParams, Oracle, and CrossRefs
            ref_inputs = []
            holders = state.get("holders", {})

            # Search for params UTXO at params holder address
            params_addr = holders.get("params", {}).get("address", "")
            oracle_addr = holders.get("oracle", {}).get("address", "")
            refs_policy = state.get("refs_token_policy", "")

            if params_addr:
                try:
                    from pycardano import Address
                    expected_params_tx = tx_hashes.get("params_utxo", "")
                    params_utxos = await agent.context.async_utxos(Address.from_primitive(params_addr))
                    # Prefer the UTxO from current deployment
                    found = False
                    for u in params_utxos:
                        tx_hash = str(u.input.transaction_id)
                        if u.output.datum is not None and tx_hash == expected_params_tx:
                            idx = u.input.index
                            ref_inputs.append({"tx_hash": tx_hash, "output_index": idx, "address": params_addr})
                            print(f"\n[OK] GovernanceParams UTXO: {tx_hash[:16]}...#{idx}")
                            found = True
                            break
                    if not found:
                        for u in params_utxos:
                            if u.output.datum is not None:
                                tx_hash = str(u.input.transaction_id)
                                idx = u.input.index
                                ref_inputs.append({"tx_hash": tx_hash, "output_index": idx, "address": params_addr})
                                print(f"\n[OK] GovernanceParams UTXO (fallback): {tx_hash[:16]}...#{idx}")
                                break
                except Exception as e:
                    print(f"\n[WARN] Could not find params UTXO: {e}")

            if oracle_addr:
                try:
                    from pycardano import Address
                    expected_oracle_tx = tx_hashes.get("oracle_utxo", "")
                    oracle_utxos = await agent.context.async_utxos(Address.from_primitive(oracle_addr))
                    found = False
                    for u in oracle_utxos:
                        tx_hash = str(u.input.transaction_id)
                        if u.output.datum is not None and tx_hash == expected_oracle_tx:
                            idx = u.input.index
                            ref_inputs.append({"tx_hash": tx_hash, "output_index": idx, "address": oracle_addr})
                            print(f"[OK] Oracle UTXO: {tx_hash[:16]}...#{idx}")
                            found = True
                            break
                    if not found:
                        for u in oracle_utxos:
                            if u.output.datum is not None:
                                tx_hash = str(u.input.transaction_id)
                                idx = u.input.index
                                ref_inputs.append({"tx_hash": tx_hash, "output_index": idx, "address": oracle_addr})
                                print(f"[OK] Oracle UTXO (fallback): {tx_hash[:16]}...#{idx}")
                                break
                except Exception as e:
                    print(f"[WARN] Could not find oracle UTXO: {e}")

            # Find CrossRefs NFT — prefer exact tx_hash from deploy_state,
            # fall back to scanning for refs_policy token.
            expected_crossrefs_tx = tx_hashes.get("cross_refs_nft", "")
            try:
                cross_refs_found = False
                cross_refs_address = state.get("cross_refs_address", "")
                search_addresses = []
                if cross_refs_address:
                    search_addresses.append(("script holder", cross_refs_address))
                # Also check oracle holder (common location for CrossRefs)
                oracle_holder = holders.get("oracle", {}).get("address", "")
                if oracle_holder and oracle_holder != cross_refs_address:
                    search_addresses.append(("oracle holder", oracle_holder))
                search_addresses.append(("wallet", wallet["address"]))

                from pycardano import Address as PycAddr
                for addr_label, addr in search_addresses:
                    if cross_refs_found:
                        break
                    try:
                        addr_utxos = await agent.context.async_utxos(
                            PycAddr.from_primitive(addr) if not isinstance(addr, str) or addr.startswith("addr") else addr
                        )
                    except Exception:
                        addr_utxos = await agent.get_utxos() if addr_label == "wallet" else []
                    # First pass: exact tx_hash match from deploy_state
                    # Second pass: any UTxO with refs_policy token
                    for _pass_name in ("exact", "scan"):
                      for u in addr_utxos:
                        if _pass_name == "exact" and not expected_crossrefs_tx:
                            break
                        if _pass_name == "exact" and str(u.input.transaction_id) != expected_crossrefs_tx:
                            continue
                        has_refs_token = False
                        if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                            for pid in u.output.amount.multi_asset:
                                if pid.payload.hex() == refs_policy:
                                    has_refs_token = True
                                    break
                        if has_refs_token:
                            tx_hash = str(u.input.transaction_id)
                            idx = u.input.index
                            ref_inputs.append({"tx_hash": tx_hash, "output_index": idx, "address": addr})
                            has_datum = u.output.datum is not None
                            print(f"[OK] CrossRefs NFT UTXO: {tx_hash[:16]}...#{idx} at {addr_label} (datum={'yes' if has_datum else 'NO'})")
                            # Verify CrossRefs datum hashes match deploy_state
                            if has_datum:
                                from pycardano.plutus import RawPlutusData as _RPD
                                _rd = u.output.datum
                                if isinstance(_rd, _RPD) and hasattr(_rd.data, 'value'):
                                    _rf = _rd.data.value
                                    if isinstance(_rf, list) and len(_rf) == 4:
                                        _xr_prop_spend = _rf[0].hex() if isinstance(_rf[0], bytes) else str(_rf[0])
                                        _xr_crit_spend = _rf[1].hex() if isinstance(_rf[1], bytes) else str(_rf[1])
                                        _xr_prop_mint = _rf[2].hex() if isinstance(_rf[2], bytes) else str(_rf[2])
                                        _xr_crit_mint = _rf[3].hex() if isinstance(_rf[3], bytes) else str(_rf[3])
                                        _exp_prop_spend = state.get("proposal_validator_hash", "")
                                        _exp_prop_mint = validators.get("proposal.proposal_mint.mint", {}).get("hash", "")
                                        print(f"     CrossRefs.proposal_spend: {_xr_prop_spend[:16]}... {'OK' if _xr_prop_spend == _exp_prop_spend else 'MISMATCH! expected ' + _exp_prop_spend[:16]}")
                                        print(f"     CrossRefs.proposal_mint:  {_xr_prop_mint[:16]}... {'OK' if _xr_prop_mint == _exp_prop_mint else 'MISMATCH! expected ' + _exp_prop_mint[:16]}")
                            cross_refs_found = True
                            break
                      if cross_refs_found:
                          break
                if not cross_refs_found:
                    print("[WARN] CrossRefs NFT not found")
            except Exception as e:
                print(f"[WARN] Could not find CrossRefs NFT: {e}")

            # Find a registered agent DID from the Agent Registry
            agent_did_bytes = None
            agent_registry_ref = None
            registry_hash = state.get("agent_registry_hash", "")
            if registry_hash:
                try:
                    from pycardano import Address as PycAddr
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
                                        agent_registry_ref = {
                                            "tx_hash": str(u.input.transaction_id),
                                            "output_index": u.input.index,
                                            "address": str(reg_addr),
                                        }
                                        break
                            if agent_did_bytes:
                                break
                    if agent_did_bytes:
                        print(f"[OK] Agent DID: {agent_did_bytes.hex()[:16]}... (from registry)")
                        ref_inputs.append(agent_registry_ref)
                    else:
                        print("[WARN] No registered agents found in registry")
                except Exception as e:
                    print(f"[WARN] Agent registry lookup failed: {e}")

            if ref_inputs:
                gov.set_governance_reference_inputs(ref_inputs)
                print(f"[OK] {len(ref_inputs)} reference inputs configured")
            else:
                print("[WARN] No reference inputs found — spend actions will fail")

            passed = 0
            failed = 0
            skipped = 0
            explorer = state["explorer_url"]

            def tx_link(tx_hash):
                return f"{explorer}/tx/{tx_hash}"

            # ==============================================================
            # Phase 1: Lock actions
            # ==============================================================
            print("\n--- Phase 1: Lock Actions ---")

            # Test 1: Balance
            balance = await gov.get_balance()
            print(f"\n[1] PASS — Balance: {balance['ada']} AP3X ({balance['lovelace']} lovelace)")
            passed += 1

            if balance["lovelace"] < 50_000_000:
                print("    WARN: Less than 50 AP3X. Fund wallet first.")
                return

            # Test 2: Submit proposal
            proposal_result = None
            try:
                proposal_hash = blake2b_256("Smoke test proposal: improve validator error messages")
                proposal_result = await gov.submit_proposal(
                    proposer_did="did_smoke_test_agent",
                    proposal_hash=proposal_hash,
                    proposal_type=ProposalType.general_suggestion(),
                    storage_uri="ipfs://QmSmokeTestProposal",
                    stake_lovelace=25_000_000,
                )
                print(f"\n[2] PASS — Proposal submitted: {proposal_result['tx_hash']}")
                print(f"    Explorer: {tx_link(proposal_result['tx_hash'])}")
                passed += 1
            except Exception as e:
                print(f"\n[2] FAIL — Submit proposal: {e}")
                failed += 1

            if not proposal_result:
                print("\n[SKIP] Tests 3-5 require a submitted proposal")
                print(f"\n{'=' * 60}")
                print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
                return

            # Wait for tx confirmation
            print("\n    Waiting 20s for tx confirmation...")

            await asyncio.sleep(20)

            # Test 3: Submit critique
            critique_result = None
            try:
                critique_hash = blake2b_256("Smoke test critique: good proposal, needs more data")
                critique_result = await gov.submit_critique(
                    critic_did="did_smoke_test_critic",
                    proposal_ref_tx=proposal_result["tx_hash"],
                    proposal_ref_idx=0,
                    critique_hash=critique_hash,
                    storage_uri="ipfs://QmSmokeTestCritique",
                    critique_type=CritiqueType.SUPPORTIVE,
                    stake_lovelace=5_000_000,
                )
                print(f"\n[3] PASS — Critique submitted: {critique_result['tx_hash']}")
                print(f"    Explorer: {tx_link(critique_result['tx_hash'])}")
                passed += 1
            except Exception as e:
                print(f"\n[3] FAIL — Submit critique: {e}")
                failed += 1

            # Wait for critique tx to confirm (UTxO set changes)
            print("    Waiting 20s for tx confirmation...")
            await asyncio.sleep(20)

            # Test 4: Endorse proposal
            endorse_result = None
            try:
                endorse_result = await gov.endorse_proposal(
                    endorser_did="did_smoke_test_endorser",
                    proposal_ref_tx=proposal_result["tx_hash"],
                    proposal_ref_idx=0,
                    stake_lovelace=10_000_000,
                )
                print(f"\n[4] PASS — Endorsement submitted: {endorse_result['tx_hash']}")
                print(f"    Explorer: {tx_link(endorse_result['tx_hash'])}")
                passed += 1
            except Exception as e:
                print(f"\n[4] FAIL — Endorse proposal: {e}")
                failed += 1

            # ==============================================================
            # Phase 2: On-chain spend actions (validators execute)
            # ==============================================================
            print("\n--- Phase 2: On-Chain Spend Actions ---")

            # Test 5: Withdraw endorsement (spend — on-chain validator executes)
            if endorse_result:
                print("\n    Waiting 20s for endorsement tx confirmation...")
                await asyncio.sleep(20)
                try:
                    withdraw_result = await gov.withdraw_endorsement(
                        utxo_ref={"tx_hash": endorse_result["tx_hash"], "output_index": 0},
                    )
                    print(f"\n[5] PASS — Endorsement withdrawn: {withdraw_result['tx_hash']}")
                    print(f"    Explorer: {tx_link(withdraw_result['tx_hash'])}")
                    passed += 1
                except Exception as e:
                    print(f"\n[5] FAIL — Withdraw endorsement: {e}")
                    import traceback; traceback.print_exc()
                    failed += 1
            else:
                print("\n[5] SKIP — No endorsement to withdraw")
                skipped += 1

            # ==============================================================
            # Phase 3: Validated submit + withdraw (mint+spend)
            # ==============================================================
            print("\n--- Phase 3: Validated Submit & Withdraw (mint+spend) ---")

            # Test 6: Validated submit proposal (two-step: lock then consume+mint)
            validated_result = None
            if not agent_did_bytes:
                print(f"\n[6] SKIP — No registered agent DID available")
                skipped += 1
            else:
              try:
                v_proposal_hash = blake2b_256("Validated smoke test proposal: full lifecycle")
                validated_result = await gov.validated_submit_proposal(
                    proposer_did=agent_did_bytes,
                    proposal_hash=v_proposal_hash,
                    proposal_type=ProposalType.general_suggestion(),
                    storage_uri="ipfs://QmValidatedSmokeTestProposal",
                    stake_lovelace=25_000_000,
                )
                print(f"\n[6] PASS — Validated proposal submitted: {validated_result['tx_hash']}")
                print(f"    Lock TX: {validated_result['lock_tx_hash']}")
                print(f"    Token: {validated_result['proposal_token_name']}")
                print(f"    Explorer: {tx_link(validated_result['tx_hash'])}")
                passed += 1
              except Exception as e:
                print(f"\n[6] FAIL — Validated submit proposal: {e}")
                import traceback; traceback.print_exc()
                failed += 1

            # Test 7: Validated withdraw (burn token + recover stake)
            if validated_result:
                print("\n    Waiting 20s for tx confirmation...")

                await asyncio.sleep(20)

                # Find the activity UTxO from the SAME tx as the submit
                try:
                    from pycardano import Address as PycAddress
                    prop_addr = gov._script_address(proposal_cbor)
                    prop_utxos = await agent.context.async_utxos(PycAddress.from_primitive(prop_addr))
                    submit_tx_hash = validated_result["tx_hash"]
                    activity_ref = None
                    for u in prop_utxos:
                        if str(u.input.transaction_id) != submit_tx_hash:
                            continue
                        if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                            for pid, assets in u.output.amount.multi_asset.items():
                                for aname in assets:
                                    if aname.payload[:5] == b"pact_":
                                        activity_ref = {
                                            "tx_hash": str(u.input.transaction_id),
                                            "output_index": u.input.index,
                                        }
                                        break

                    if activity_ref:
                        withdraw_result = await gov.validated_withdraw_proposal(
                            utxo_ref={"tx_hash": validated_result["tx_hash"], "output_index": 0},
                            activity_utxo_ref=activity_ref,
                            proposer_did=agent_did_bytes,
                        )
                        print(f"\n[7] PASS — Proposal withdrawn (token burned): {withdraw_result['tx_hash']}")
                        print(f"    Explorer: {tx_link(withdraw_result['tx_hash'])}")
                        passed += 1
                    else:
                        print(f"\n[7] FAIL — Activity UTxO not found at proposal address")
                        failed += 1
                except Exception as e:
                    print(f"\n[7] FAIL — Validated withdraw proposal: {e}")
                    import traceback; traceback.print_exc()
                    failed += 1
            else:
                print(f"\n[7] SKIP — No validated proposal to withdraw")
                skipped += 1

            # Test 8: Validated adopt proposal (oracle = wallet on testnet)
            # Submit a fresh proposal, then adopt it using the wallet as oracle signer.
            if not agent_did_bytes:
                print(f"\n[8] SKIP — No registered agent DID available")
                skipped += 1
            else:
              # Step 8a: Submit a proposal to adopt
              adopt_result = None
              try:
                adopt_proposal_hash = blake2b_256("Smoke test proposal for adoption lifecycle")
                adopt_result = await gov.validated_submit_proposal(
                    proposer_did=agent_did_bytes,
                    proposal_hash=adopt_proposal_hash,
                    proposal_type=ProposalType.general_suggestion(),
                    storage_uri="ipfs://QmAdoptSmokeTest",
                    stake_lovelace=25_000_000,
                )
                print(f"\n    [8a] Proposal submitted for adoption: {adopt_result['tx_hash'][:16]}...")
              except Exception as e:
                print(f"\n[8] FAIL — Submit for adoption: {e}")
                import traceback; traceback.print_exc()
                failed += 1

              # Step 8b: Adopt the proposal
              if adopt_result:
                print("    Waiting 20s for confirmation...")
                await asyncio.sleep(20)
                try:
                    from pycardano import Address as PycAddr2
                    prop_addr2 = gov._script_address(proposal_cbor)
                    prop_utxos2 = await agent.context.async_utxos(PycAddr2.from_primitive(prop_addr2))
                    # Find the activity UTxO from the SAME transaction as the submit
                    # (the submit creates both proposal and activity outputs)
                    submit_tx = adopt_result["tx_hash"]
                    adopt_activity_ref = None
                    for u in prop_utxos2:
                        if str(u.input.transaction_id) != submit_tx:
                            continue
                        if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                            for pid, assets_map in u.output.amount.multi_asset.items():
                                for aname in assets_map:
                                    if aname.payload[:5] == b"pact_":
                                        adopt_activity_ref = {
                                            "tx_hash": str(u.input.transaction_id),
                                            "output_index": u.input.index,
                                        }
                                        break

                    if not adopt_activity_ref:
                        print(f"\n[8] FAIL — Activity UTxO not found for adoption test")
                        failed += 1
                    else:
                        reasoning = blake2b_256("Adoption reasoning: proposal meets governance quality bar")
                        adopt_tx_result = await gov.validated_adopt_proposal(
                            utxo_ref={"tx_hash": adopt_result["tx_hash"], "output_index": 0},
                            activity_utxo_ref=adopt_activity_ref,
                            proposer_did=agent_did_bytes,
                            reasoning_hash=reasoning,
                            reward_amount=50_000_000,  # 50 AP3X (minimum)
                        )
                        print(f"\n[8] PASS — Proposal adopted: {adopt_tx_result['tx_hash']}")
                        print(f"    Reward: {adopt_tx_result['reward'] / 1_000_000} AP3X")
                        print(f"    Explorer: {tx_link(adopt_tx_result['tx_hash'])}")
                        passed += 1
                except Exception as e:
                    print(f"\n[8] FAIL — Adopt proposal: {e}")
                    import traceback; traceback.print_exc()
                    failed += 1

            # Test 9: Expire proposal (requires review window elapsed)
            # This test cannot complete in a single run — the minimum review window
            # is 64,800 slots (~3 days). Use scripts/test_expire.py to expire a
            # previously submitted proposal after its window elapses.
            print(f"\n[9] SKIP — Expire proposal (requires review window elapsed, use scripts/test_expire.py)")
            skipped += 1

            # Final balance
            try:
                final_balance = await gov.get_balance()
                print(f"\nFinal balance: {final_balance['ada']} AP3X ({final_balance['lovelace']} lovelace)")
            except Exception:
                pass

            print(f"\n{'=' * 60}")
            print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
            if failed == 0:
                print("All attempted tests passed!")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        print("\n--- Falling back to offline validation ---")
        await offline_validation(state)


async def offline_validation(state: dict):
    """Run offline validation checks when testnet is unavailable."""
    print("\nValidating deployment configuration...")

    # Check blueprint
    blueprint_path = Path("contracts/governance-suggestion/plutus.json")
    if blueprint_path.exists():
        with open(blueprint_path) as f:
            bp = json.load(f)
        print(f"  [OK] Blueprint: {bp['preamble']['title']} v{bp['preamble']['version']}")
        print(f"  [OK] Validators: {len(bp['validators'])}")

        for v in bp["validators"]:
            code_size = len(v["compiledCode"]) // 2
            status = "OK" if code_size < 16384 else "WARN (>16KB)"
            print(f"    [{status}] {v['title']}: {code_size} bytes, hash={v['hash'][:16]}...")
    else:
        print("  [FAIL] Blueprint not found")

    # Check wallet
    if WALLET_FILE.exists():
        wallet = json.load(open(WALLET_FILE))
        print(f"  [OK] Wallet: {wallet['address'][:20]}...")
    else:
        print("  [FAIL] Wallet not created")

    # Check applied validators
    validators = state.get("validators", {})
    if validators:
        print(f"  [OK] Applied validators: {len(validators)}")
        for title, info in validators.items():
            if ".else" not in title:
                print(f"    {title}: {info['hash'][:16]}...")
    else:
        print("  [WARN] No applied validators — run deploy.py")

    # Check holder scripts
    holders = state.get("holders", {})
    if holders:
        print(f"  [OK] Holder scripts: {len(holders)}")
        for name, info in holders.items():
            print(f"    {name}: {info['hash'][:16]}... -> {info.get('address', 'N/A')[:20]}...")
    else:
        print("  [WARN] No holder scripts — run deploy.py")

    # Check refs policy
    refs_policy = state.get("refs_token_policy", "")
    if refs_policy:
        print(f"  [OK] Refs token policy: {refs_policy[:16]}...")
    else:
        print("  [WARN] No refs token policy")

    # Check cross-refs
    proposal_hash = state.get("proposal_validator_hash", "")
    critique_hash = state.get("critique_validator_hash", "")
    if proposal_hash and critique_hash:
        print(f"  [OK] Cross-refs: proposal={proposal_hash[:16]}... critique={critique_hash[:16]}...")
    else:
        print("  [WARN] Cross-refs not computed")

    # Check tx_hashes
    tx_hashes = state.get("tx_hashes", {})
    if tx_hashes:
        print(f"  [OK] Deployment txs: {len(tx_hashes)}")
        for name, tx in tx_hashes.items():
            print(f"    {name}: {tx[:16]}...")
    else:
        print("  [WARN] No deployment transactions — deploy.py not yet run with on-chain deployment")

    # Validate SDK types
    try:
        from vector_agent.governance import (
            ProposalType, ProposalAction, CritiqueType,
            build_proposal_datum, build_governance_params,
            build_treasury_batch_datum, build_oracle_datum,
        )
        print("  [OK] SDK types import correctly")

        # Test datum construction
        import cbor2
        params = build_governance_params()
        params_bytes = cbor2.dumps(params.data)
        print(f"  [OK] GovernanceParams datum: {len(params_bytes)} bytes CBOR")

        batch = build_treasury_batch_datum(1, True)
        batch_bytes = cbor2.dumps(batch.data)
        print(f"  [OK] TreasuryBatch datum: {len(batch_bytes)} bytes CBOR")

    except Exception as e:
        print(f"  [FAIL] SDK: {e}")

    print("\n--- Offline Validation Complete ---")


if __name__ == "__main__":
    asyncio.run(smoke_test())
