"""
Module 6: Standalone Test — identity-checked proposal submission (Test 6)

Runs ONLY the proposal submission in isolation: the lock, then
SubmitProposal with the proposal token and the activity record
(scripts/governance_identity.py). The transaction reads the identity record
of the agent and its ancestors at the identity address.

Usage:
    VECTOR_AGENT_DID=did:vector:<64 hex> nix-shell shell.nix --run "python scripts/test_validated_submit.py"
    nix-shell shell.nix --run "python scripts/test_validated_submit.py --agent <DID or 64 hex>"

The deploy state (wallets/deploy_state.json) must have identity_hash
(deployment order: identity registry, Module-1, Module-3, Module-6). The
wallet key must be the controller key of the agent's record or a listed key
with the transaction purpose.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

import governance_identity as gi
import vector_identity as vid

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")


async def main(agent_arg=None):
    # Load state
    with open(DEPLOY_STATE_FILE) as f:
        state = json.load(f)
    with open(WALLET_FILE) as f:
        wallet = json.load(f)

    try:
        deployment = gi.GovernanceDeployment.from_state(state, str(DEPLOY_STATE_FILE))
        agent_id = gi.agent_id_from(agent_arg)
    except gi.GovernanceClientError as e:
        print(f"[ERR] {e}")
        return

    from vector_agent import VectorAgent

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))
    skey_path = str(Path("wallets/payment.skey").absolute())

    print("=" * 60)
    print("Test 6: identity-checked proposal submission (standalone)")
    print("=" * 60)
    print(f"Wallet: {wallet['address']}")
    print(f"Proposal spend: {deployment.proposal_spend.hash.hex()[:16]}...")
    print(f"Identity hash: {deployment.identity_hash.hex()}")
    print(f"Agent: {vid.did_from_id(agent_id)}")

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        balance = await agent.get_balance()
        print(f"Balance: {balance.ada} AP3X")

        context = agent.context
        keys = gi.SigningKeyPair(agent._wallet.payment_signing_key, agent._wallet.payment_verification_key)

        # --- Check the reference inputs and the identity record ---
        print("\n--- Checking reference inputs ---")
        try:
            refs = await gi.load_references(context, deployment)
            print(f"[OK] Params record: {refs.params.utxo.input.transaction_id}#{refs.params.utxo.input.index}")
            print(f"[OK] CrossRefs: {refs.cross_refs.utxo.input.transaction_id}#{refs.cross_refs.utxo.input.index}")
            records = await gi.load_identity(context, deployment, agent_id)
            window = await gi.plan_window(context)
            for record in records:
                print(f"[OK] Record: {record.did} at {record.utxo.input.transaction_id}#{record.utxo.input.index}")
            status = vid.effective_of_chain(records, window.lo, window.hi)
            print(f"[OK] Effective status over the planned range: {status.value}")
            vid.require_active(records, window.lo, window.hi)
            vid.require_declared_credential(records[0], keys.key_hash)
            activity = await gi.load_activity(context, deployment, agent_id)
            if activity:
                print(f"[OK] Activity record: {activity.datum.active_proposal_count} active, "
                      f"last proposal at {activity.datum.last_proposal_slot} (POSIX ms)")
            else:
                print("[OK] No activity record: this is the first proposal of the agent")
        except (gi.GovernanceClientError, vid.IdentityError) as e:
            print(f"[ERR] {e}")
            return

        # --- Run the submission (lock, then SubmitProposal) ---
        print("\n--- Running the proposal submission ---")
        proposal_hash = hashlib.blake2b(b"Standalone test 6 proposal", digest_size=32).digest()

        try:
            result = await gi.submit_proposal(
                context, deployment, agent_id=agent_id,
                terms=gi.ProposalTerms(
                    proposal_hash=proposal_hash,
                    storage_uri="ipfs://QmStandaloneTest6",
                    stake_amount=refs.params.params.min_proposal_stake,
                    review_window=refs.params.params.min_review_window,
                ),
                wallet=keys, wallet_address=agent._wallet.payment_address,
            )
            print(f"\n[PASS] Proposal submitted!")
            print(f"  TX: {result.tx}")
            print(f"  Lock TX: {result.lock_tx}")
            print(f"  Proposal: {result.position.transaction_id}#{result.position.index}")
            print(f"  Activity: {result.activity.transaction_id}#{result.activity.index}")
            print(f"  Token: {result.minted[0].hex()}")
            print(f"  Explorer: {state.get('explorer_url', '')}/tx/{result.tx}")
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
    parser = argparse.ArgumentParser(description="Module 6 standalone proposal submission")
    parser.add_argument("--agent", help=f"agent DID or 64-hex identifier (default: ${gi.AGENT_DID_ENV})")
    asyncio.run(main(parser.parse_args().agent))
