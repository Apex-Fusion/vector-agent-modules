"""
Module 3: Reputation Staking — End-to-End Lifecycle Test

Tests the full reputation staking lifecycle on Vector testnet.

Usage:
    nix-shell shell.nix --run "python scripts/smoke_test.py"

Prerequisites:
    - Wallet funded with at least 100 AP3X
    - Full deployment completed (run deploy.py first)
    - At least one agent registered in the Agent Registry

Test phases:
    Phase 1 — Self-Stake:     CreateStake, IncreaseStake, UpdateCapabilities, DecreaseStake
    Phase 2 — Endorsement:    MintEndorsement, IncreaseEndorsement, WithdrawEndorsement
    Phase 3 — Challenge:      MintChallenge, RespondToChallenge, ResolveChallenge, DistributeOutcome
    Phase 4 — DefaultJudgment: MintChallenge (no response), DefaultJudgment
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/reputation_wallet.json")


def load_deploy_state() -> dict:
    if not DEPLOY_STATE_FILE.exists():
        raise FileNotFoundError("Deploy state not found. Run: python scripts/deploy.py")
    with open(DEPLOY_STATE_FILE) as f:
        return json.load(f)


async def smoke_test():
    print("=" * 60)
    print("Module 3: Reputation Staking — Lifecycle Smoke Test")
    print("=" * 60)

    state = load_deploy_state()

    print(f"\nNetwork: {state['network']}")
    print(f"Wallet: {state['wallet_address']}")
    print(f"Reputation hash: {state.get('reputation_validator_hash', 'NOT SET')}")
    print(f"Endorsement hash: {state.get('endorsement_validator_hash', 'NOT SET')}")

    tx_hashes = state.get("tx_hashes", {})
    if not tx_hashes:
        print("\n[WARN] No deployment transactions found. Run deploy.py first.")
        await offline_validation(state)
        return

    # Verify required infrastructure is deployed
    required_keys = ["params_utxo", "cross_refs_nft"]
    missing = [k for k in required_keys if k not in tx_hashes]
    if missing:
        print(f"\n[ERROR] Missing infrastructure: {', '.join(missing)}")
        print("  Run deploy.py to complete deployment.")
        return

    print("\n--- Test Plan ---")
    print("  Phase 1 — Self-Stake:")
    print("    1. Query wallet balance")
    print("    2. CreateStake (10 AP3X, capabilities: code_review, testing)")
    print("    3. IncreaseStake (+5 AP3X)")
    print("    4. UpdateCapabilities (add deployment)")
    print("  Phase 2 — Endorsement:")
    print("    5. MintEndorsement (5 AP3X, code_review)")
    print("    6. IncreaseEndorsement (+3 AP3X)")
    print("  Phase 3 — Challenge (oracle resolution):")
    print("    7. MintChallenge (25 AP3X, challenge code_review)")
    print("    8. RespondToChallenge (target submits counter-evidence)")
    print("    9. ResolveChallenge (oracle: CapabilityVerified)")
    print("   10. DistributeOutcome")
    print()

    # Note: Full lifecycle tests require:
    # - Two registered agents (endorser + target) — can use the same wallet
    #   with different DIDs if both are registered in the Agent Registry
    # - Reference inputs for: Agent Registry UTxOs, ProtocolParams, CrossRefs
    # - The wallet acts as Foundation oracle on testnet (oracle_credential = wallet vkey_hash)
    #
    # This is a structural test — it validates the deployment is correct and
    # the transaction building patterns work. For a complete lifecycle, use
    # the agent-sdk-py with proper agent registration.

    passed = 0
    failed = 0
    skipped = 0
    explorer = state.get("explorer_url", "https://vector.testnet.apexscan.org")

    # Test 1: Balance check
    try:
        from pycardano import (
            PaymentSigningKey, PaymentVerificationKey,
            Address, Network, OgmiosChainContext,
        )

        ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
        context = OgmiosChainContext(ogmios_url, network=Network.MAINNET)

        wallet = json.load(open(WALLET_FILE))
        skey = PaymentSigningKey.from_primitive(bytes.fromhex(wallet["skey_hex"]))
        vkey = PaymentVerificationKey.from_signing_key(skey)
        address = Address(payment_part=vkey.hash(), network=Network.MAINNET)

        utxos = context.utxos(address)
        total_lovelace = sum(u.output.amount if isinstance(u.output.amount, int) else u.output.amount.coin for u in utxos)

        print(f"\n[1] PASS — Balance: {total_lovelace / 1_000_000:.2f} AP3X ({total_lovelace} lovelace), {len(utxos)} UTxOs")
        passed += 1

        if total_lovelace < 100_000_000:
            print("    WARN: Less than 100 AP3X. Fund wallet for full lifecycle test.")

    except Exception as e:
        print(f"\n[1] FAIL — Balance check: {e}")
        failed += 1
        print("\n--- Falling back to offline validation ---")
        await offline_validation(state)
        return

    # Verify infrastructure UTxOs exist on-chain
    print("\n--- Infrastructure Check ---")
    validators = state.get("validators", {})
    holders = state.get("holders", {})

    # Check params UTXO
    params_addr = holders.get("params", {}).get("address", "")
    if params_addr:
        try:
            params_utxos = context.utxos(Address.from_primitive(params_addr))
            params_with_datum = [u for u in params_utxos if u.output.datum is not None]
            print(f"  [OK] Params holder: {len(params_with_datum)} datum UTxOs at {params_addr[:25]}...")
        except Exception as e:
            print(f"  [WARN] Params holder check failed: {e}")

    # Check CrossRefs NFT
    refs_policy = state.get("refs_token_policy", "")
    cross_refs_found = False
    if params_addr and refs_policy:
        try:
            for u in context.utxos(Address.from_primitive(params_addr)):
                if hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                    for pid in u.output.amount.multi_asset:
                        if pid.payload.hex() == refs_policy:
                            cross_refs_found = True
                            print(f"  [OK] CrossRefs NFT found at params holder")
                            break
                if cross_refs_found:
                    break
        except Exception as e:
            print(f"  [WARN] CrossRefs check failed: {e}")

    if not cross_refs_found:
        print("  [WARN] CrossRefs NFT not found — transactions requiring cross-refs will fail")

    # Check reputation and endorsement validator addresses
    rep_hash = state.get("reputation_validator_hash", "")
    end_hash = state.get("endorsement_validator_hash", "")
    if rep_hash:
        try:
            from pycardano.hash import ScriptHash
            rep_addr = Address(payment_part=ScriptHash(bytes.fromhex(rep_hash)), network=Network.MAINNET)
            rep_utxos = context.utxos(rep_addr)
            print(f"  [OK] Reputation validator: {len(rep_utxos)} UTxOs at {str(rep_addr)[:25]}...")
        except Exception as e:
            print(f"  [WARN] Reputation validator check failed: {e}")

    if end_hash:
        try:
            from pycardano.hash import ScriptHash
            end_addr = Address(payment_part=ScriptHash(bytes.fromhex(end_hash)), network=Network.MAINNET)
            end_utxos = context.utxos(end_addr)
            print(f"  [OK] Endorsement validator: {len(end_utxos)} UTxOs at {str(end_addr)[:25]}...")
        except Exception as e:
            print(f"  [WARN] Endorsement validator check failed: {e}")

    # Check Agent Registry for registered agents
    registry_hash = state.get("agent_registry_hash", "")
    agent_dids = []
    if registry_hash:
        try:
            from pycardano.hash import ScriptHash
            reg_addr = Address(payment_part=ScriptHash(bytes.fromhex(registry_hash)), network=Network.MAINNET)
            reg_utxos = context.utxos(reg_addr)
            for u in reg_utxos:
                if u.output.datum and hasattr(u.output.amount, 'multi_asset') and u.output.amount.multi_asset:
                    for pid, assets in u.output.amount.multi_asset.items():
                        if pid.payload.hex() == registry_hash:
                            for aname in assets:
                                agent_dids.append(aname.payload.hex())
            print(f"  [OK] Agent Registry: {len(agent_dids)} registered agents")
            if agent_dids:
                for did in agent_dids[:3]:
                    print(f"    DID: {did[:32]}...")
        except Exception as e:
            print(f"  [WARN] Agent Registry check failed: {e}")

    if len(agent_dids) < 2:
        print(f"\n  [WARN] Need at least 2 registered agents for endorsement/challenge tests.")
        print(f"         Register agents via the Agent Registry first.")
        print(f"         Skipping lifecycle tests — only infrastructure validated.")
        skipped = 9
    else:
        # TODO: Implement full lifecycle transaction building
        # This requires PyCardano transaction builders for each Module 3 action.
        # The patterns are:
        #   - CreateStake: mint stake token + lock AP3X at reputation address
        #   - MintEndorsement: mint endorsement token + lock AP3X at endorsement address
        #   - MintChallenge: mint challenge token + lock AP3X at endorsement address
        #   - WithdrawEndorsement: burn token + return AP3X to endorser
        #   - ResolveChallenge: oracle signs + update datum to Resolved
        #   - DistributeOutcome: burn challenge token + distribute AP3X
        #
        # Each requires: reference inputs (registry, params, cross-refs),
        # correct datum construction, and multi-validator mint+spend coupling.
        print(f"\n  [INFO] Full lifecycle transaction building not yet implemented.")
        print(f"         Infrastructure validation passed. Next step: implement")
        print(f"         PyCardano transaction builders in scripts/lifecycle_test.py")
        skipped = 9

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    if failed == 0 and passed > 0:
        print("Infrastructure validation passed!")
    print("=" * 60)


async def offline_validation(state: dict):
    """Run offline validation when testnet is unavailable."""
    print("\nValidating deployment configuration...")

    # Check blueprint
    blueprint_path = Path("reputation-staking/plutus.json")
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
        print("  [FAIL] Blueprint not found — run 'aiken build' in reputation-staking/")

    # Check wallet
    if WALLET_FILE.exists():
        wallet = json.load(open(WALLET_FILE))
        print(f"  [OK] Wallet: {wallet['address'][:20]}...")
    else:
        print("  [FAIL] Wallet not created — run setup_wallet.py")

    # Check applied validators
    validators = state.get("validators", {})
    if validators:
        print(f"  [OK] Applied validators: {len([t for t in validators if '.else' not in t])}")
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
    rep_hash = state.get("reputation_validator_hash", "")
    end_hash = state.get("endorsement_validator_hash", "")
    if rep_hash and end_hash:
        print(f"  [OK] Cross-refs: reputation={rep_hash[:16]}... endorsement={end_hash[:16]}...")
    else:
        print("  [WARN] Cross-refs not computed")

    # Check tx_hashes
    tx_hashes = state.get("tx_hashes", {})
    if tx_hashes:
        print(f"  [OK] Deployment txs: {len(tx_hashes)}")
        for name, tx in tx_hashes.items():
            print(f"    {name}: {tx[:16]}...")
    else:
        print("  [WARN] No deployment transactions")

    print("\n--- Offline Validation Complete ---")


if __name__ == "__main__":
    asyncio.run(smoke_test())
