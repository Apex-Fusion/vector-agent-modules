"""
Module 6: Targeted Redeployment — proposal_mint + proposal_spend + CrossRefs

After fixing Bug C (token name uses own_ref) and Bug D (activity_tracking
output policy hash), the proposal_mint and proposal_spend validators changed.
This script:
  1. Applies GovernanceConfig to the fresh blueprint
  2. Deploys only proposal_mint and proposal_spend as new reference scripts
  3. Burns old CrossRefs NFT and mints a new one with updated hashes
  4. Updates deploy_state.json

Usage:
    nix-shell shell.nix --run "python scripts/redeploy_proposal.py"
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import cbor2
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────

GAME6_ROOT = Path(__file__).parent.parent
REPO_ROOT = GAME6_ROOT.parent
CONTRACT_DIR = GAME6_ROOT / "contracts" / "governance-suggestion"
HOLDER_DIR = REPO_ROOT / "shared" / "holder-scripts"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"
HOLDER_BLUEPRINT_PATH = HOLDER_DIR / "plutus.json"
WALLET_FILE = GAME6_ROOT / "wallets" / "governance_wallet.json"
SKEY_PATH = GAME6_ROOT / "wallets" / "payment.skey"
DEPLOY_STATE_FILE = GAME6_ROOT / "wallets" / "deploy_state.json"

OGMIOS_URL = os.getenv("VECTOR_OGMIOS_URL", "https://ogmios.vector.testnet.apexfusion.org")
SUBMIT_URL = os.getenv("VECTOR_SUBMIT_URL", "https://submit.vector.testnet.apexfusion.org/api/submit/tx")
EXPLORER_URL = os.getenv("VECTOR_EXPLORER_URL", "https://vector.testnet.apexscan.org")

sys.path.insert(0, str(GAME6_ROOT))


def load_wallet() -> dict:
    with open(WALLET_FILE) as f:
        return json.load(f)


def load_deploy_state() -> dict:
    with open(DEPLOY_STATE_FILE) as f:
        return json.load(f)


# Import functions from deploy.py
from deploy import (
    build_governance_config_cbor,
    apply_governance_config,
    script_hash_to_testnet_address,
    deploy_reference_script,
    compute_native_script_policy,
    mint_refs_nft,
)


async def redeploy():
    print("=" * 60)
    print("Module 6: Targeted Redeployment — proposal validators + CrossRefs")
    print("=" * 60)

    wallet = load_wallet()
    state = load_deploy_state()
    tx_hashes = state.get("tx_hashes", {})
    holders = state.get("holders", {})
    refs_policy_id = state.get("refs_token_policy", "")

    print(f"\nWallet: {wallet['address']}")

    # Step 1: Re-apply GovernanceConfig to all validators (fresh blueprint)
    print("\n--- Step 1: Re-apply GovernanceConfig to fresh blueprint ---")
    config_cbor = build_governance_config_cbor(
        refs_token_policy=refs_policy_id,
        oracle_holder_hash=holders["oracle"]["hash"],
        params_holder_hash=holders["params"]["hash"],
        treasury_holder_hash=holders["treasury"]["hash"],
    )

    applied_bp = apply_governance_config(config_cbor)

    applied_validators = {}
    for v in applied_bp["validators"]:
        applied_validators[v["title"]] = {
            "hash": v["hash"],
            "compiled_code": v["compiledCode"],
        }

    # Show old vs new hashes
    old_proposal_mint_hash = state.get("validators", {}).get("proposal.proposal_mint.mint", {}).get("hash", "")
    old_proposal_spend_hash = state.get("validators", {}).get("proposal.proposal_spend.spend", {}).get("hash", "")
    new_proposal_mint_hash = applied_validators["proposal.proposal_mint.mint"]["hash"]
    new_proposal_spend_hash = applied_validators["proposal.proposal_spend.spend"]["hash"]

    print(f"\n  proposal_mint:  {old_proposal_mint_hash[:16]}... -> {new_proposal_mint_hash[:16]}...")
    changed_mint = old_proposal_mint_hash != new_proposal_mint_hash
    print(f"    {'CHANGED' if changed_mint else 'SAME'}")

    print(f"  proposal_spend: {old_proposal_spend_hash[:16]}... -> {new_proposal_spend_hash[:16]}...")
    changed_spend = old_proposal_spend_hash != new_proposal_spend_hash
    print(f"    {'CHANGED' if changed_spend else 'SAME'}")

    # Check critique hashes too — they might change because GovernanceConfig is reapplied
    old_critique_mint_hash = state.get("validators", {}).get("critique.critique_mint.mint", {}).get("hash", "")
    old_critique_spend_hash = state.get("validators", {}).get("critique.critique_spend.spend", {}).get("hash", "")
    new_critique_mint_hash = applied_validators["critique.critique_mint.mint"]["hash"]
    new_critique_spend_hash = applied_validators["critique.critique_spend.spend"]["hash"]

    print(f"\n  critique_mint:  {old_critique_mint_hash[:16]}... -> {new_critique_mint_hash[:16]}...")
    changed_crit_mint = old_critique_mint_hash != new_critique_mint_hash
    print(f"    {'CHANGED' if changed_crit_mint else 'SAME'}")

    print(f"  critique_spend: {old_critique_spend_hash[:16]}... -> {new_critique_spend_hash[:16]}...")
    changed_crit_spend = old_critique_spend_hash != new_critique_spend_hash
    print(f"    {'CHANGED' if changed_crit_spend else 'SAME'}")

    old_endorsement_hash = state.get("validators", {}).get("critique.endorsement_spend.spend", {}).get("hash", "")
    new_endorsement_hash = applied_validators["critique.endorsement_spend.spend"]["hash"]
    print(f"\n  endorsement:    {old_endorsement_hash[:16]}... -> {new_endorsement_hash[:16]}...")
    changed_endorse = old_endorsement_hash != new_endorsement_hash
    print(f"    {'CHANGED' if changed_endorse else 'SAME'}")

    if not any([changed_mint, changed_spend, changed_crit_mint, changed_crit_spend, changed_endorse]):
        print("\n  No validators changed. Nothing to redeploy.")
        return

    # Step 2: Deploy changed validators as new reference scripts
    print("\n--- Step 2: Deploy changed reference scripts ---")

    from vector_agent import VectorAgent
    TX_WAIT = 20

    async with VectorAgent(
        ogmios_url=OGMIOS_URL,
        submit_url=SUBMIT_URL,
        skey_path=str(SKEY_PATH.absolute()),
    ) as agent:
        balance = await agent.get_balance()
        print(f"\n  Balance: {balance.ada} AP3X")

        validators_to_redeploy = []
        if changed_mint:
            validators_to_redeploy.append(("proposal.proposal_mint.mint", "proposal_mint"))
        if changed_spend:
            validators_to_redeploy.append(("proposal.proposal_spend.spend", "proposal_spend"))
        if changed_crit_mint:
            validators_to_redeploy.append(("critique.critique_mint.mint", "critique_mint"))
        if changed_crit_spend:
            validators_to_redeploy.append(("critique.critique_spend.spend", "critique_spend"))
        if changed_endorse:
            validators_to_redeploy.append(("critique.endorsement_spend.spend", "endorsement_spend"))

        for title, label in validators_to_redeploy:
            print(f"\n  Deploying {label}...")
            tx = await deploy_reference_script(
                agent,
                applied_validators[title]["compiled_code"],
                label,
            )
            tx_hashes[f"{label}_ref"] = tx
            await asyncio.sleep(TX_WAIT)

        # Step 3: Re-mint CrossRefs NFT with updated hashes
        # The CrossRefs datum needs updated proposal_mint_hash if it changed
        cross_refs_needs_update = changed_mint or changed_spend or changed_crit_mint or changed_crit_spend

        if cross_refs_needs_update:
            print("\n--- Step 3: Re-mint CrossRefs NFT ---")
            proposal_hash = applied_validators["proposal.proposal_spend.spend"]["hash"]
            critique_hash = applied_validators["critique.critique_spend.spend"]["hash"]
            proposal_mint_hash = new_proposal_mint_hash
            critique_mint_hash = new_critique_mint_hash

            cross_refs_data = cbor2.CBORTag(121, [
                bytes.fromhex(proposal_hash),
                bytes.fromhex(critique_hash),
                bytes.fromhex(proposal_mint_hash),
                bytes.fromhex(critique_mint_hash),
            ])

            _, native_script_cbor = compute_native_script_policy(wallet["vkey_hash"])

            # Mint at oracle holder address (same as deploy.py) so the smoke test
            # finds it at the expected location. The old NFT at this address will
            # also be present; the smoke test uses exact tx_hash matching to find
            # the correct one.
            oracle_holder_addr = script_hash_to_testnet_address(holders["oracle"]["hash"])
            tx = await mint_refs_nft(
                agent, native_script_cbor, cross_refs_data, "cross-refs-v2",
                target_address=oracle_holder_addr,
            )
            tx_hashes["cross_refs_nft"] = tx
            state["cross_refs_address"] = oracle_holder_addr

        print(f"\n  Final balance: {(await agent.get_balance()).ada} AP3X")

    # Step 4: Update deploy_state.json
    print("\n--- Step 4: Update deploy_state.json ---")

    state["validators"] = {
        title: {"hash": info["hash"], "compiled_code": info["compiled_code"]}
        for title, info in applied_validators.items()
    }
    state["proposal_validator_hash"] = applied_validators["proposal.proposal_spend.spend"]["hash"]
    state["critique_validator_hash"] = applied_validators["critique.critique_spend.spend"]["hash"]
    state["tx_hashes"] = tx_hashes

    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  Saved to {DEPLOY_STATE_FILE}")

    print("\n" + "=" * 60)
    print("Redeployment complete!")
    print("=" * 60)
    print(f"\nNew hashes:")
    print(f"  proposal_mint:  {new_proposal_mint_hash}")
    print(f"  proposal_spend: {new_proposal_spend_hash}")
    if changed_crit_mint:
        print(f"  critique_mint:  {new_critique_mint_hash}")
    if changed_crit_spend:
        print(f"  critique_spend: {new_critique_spend_hash}")
    print(f"\nNext: python scripts/smoke_test.py")


if __name__ == "__main__":
    asyncio.run(redeploy())
