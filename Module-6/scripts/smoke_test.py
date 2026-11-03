"""
Module 6: End-to-End Smoke Test (identity registry deployment)

Runs the governance lifecycle on Vector (--network). Proposals, amendments,
critiques and endorsements go through scripts/governance_identity.py: each
of them reads the identity record of the agent (Part 2 §7, Module-6 row).
Withdrawals and adoption go through GovernanceClient of the Python SDK
(0.2.0 or later), built with GovernanceDeployment.from_deploy_state: exits
read no identity record. The client finds the params record, the CrossRefs,
the oracle record, the node of the proposer in the activity list and the
reference scripts of the deployment itself.

Usage:
    VECTOR_AGENT_DID=did:vector:<64 hex> nix-shell shell.nix --run "python scripts/smoke_test.py --network testnet"
    nix-shell shell.nix --run "python scripts/smoke_test.py --agent <DID> --endorser <DID> --finalise adopt"

Inputs:
    - --state: the deploy state of a Module-6 deployment with identity_hash.
      Default: deploy/identity/state/<network>/deploy_state.json of
      scripts/deploy.py (--network, default mainnet). The state of the
      previous deployment is used only with --state wallets/deploy_state.json.
      Deployment order: identity registry, Module-1, Module-3, Module-6.
    - VECTOR_AGENT_DID or --agent: the agent, as did:vector:<64 hex> or 64
      hex characters. Its identity record must be Active. Its controller key
      or a listed key with the transaction purpose must be the wallet key
      (wallets/payment.skey): the script signs with that key.
    - VECTOR_ENDORSER_DID or --endorser (optional): a second agent with the
      same wallet key for the endorsement steps. An agent cannot endorse its
      own proposal. Without it, the script skips the endorsement steps.
    - --finalise adopt: adopt the proposal at the end. The wallet key must be
      the oracle key. The default is a withdrawal by the proposer.
    - A wallet with at least 50 AP3X.
    - VECTOR_KOIOS_URL (optional): the Koios URL of the network of the
      deployment, with or without /api/v1. GovernanceClient uses the Koios
      URL of the agent: step 4 finds the proposal of the critique by its
      token there. The default of the agent is Vector testnet.

The proposal cooldown (GovernanceParams.proposal_cooldown, 24 hours by
default) allows one proposal per agent in that time. The client refuses a
second proposal before the cooldown ends.

Steps:
    1. Wallet balance
    2. Submit a proposal (lock, then SubmitProposal)           identity check
    3. Submit an Amendment critique (lock, then MintCritiqueToken)  identity check
    4. Withdraw the critique (GovernanceClient)                no identity check
    5. Amend the proposal (AmendProposal)                      identity check
    6. Endorse the proposal (lock, then MintEndorsementToken)  identity check
    7. Withdraw the endorsement (GovernanceClient)             no identity check
    8. Withdraw or adopt the proposal (GovernanceClient)       no identity check
    9. Expire: skipped (use scripts/test_expire_e2e.py)
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path for sdk imports
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

import governance_identity as gi
import vector_identity as vid
from deploy_common import add_state_arguments, read_state, state_endpoints, state_path

WALLET_FILE = Path("wallets/governance_wallet.json")
ENDORSER_DID_ENV = "VECTOR_ENDORSER_DID"
TX_WAIT = 20  # seconds for an SDK transaction to reach the chain


def load_deploy_state(path: Path, network: str) -> dict:
    """Load the deploy state: --state, else the state of scripts/deploy.py on
    --network."""
    return read_state(path, network)


def blake2b_256(data: str) -> bytes:
    """Compute blake2b-256 hash of a string."""
    return hashlib.blake2b(data.encode(), digest_size=32).digest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Module 6 smoke test (identity registry deployment)")
    parser.add_argument("--agent", help=f"agent DID or 64-hex identifier (default: ${gi.AGENT_DID_ENV})")
    parser.add_argument("--endorser", help=f"second agent for the endorsement steps (default: ${ENDORSER_DID_ENV})")
    parser.add_argument("--finalise", choices=("withdraw", "adopt"), default="withdraw",
                        help="how step 8 ends the proposal (default: withdraw)")
    add_state_arguments(parser)
    return parser.parse_args(argv)


def utxo_ref(ref) -> dict:
    """A TransactionInput in the format of GovernanceClient."""
    return {"tx_hash": str(ref.transaction_id), "output_index": ref.index}


def check_agent(label: str, records: list, wallet_key_hash: bytes, window) -> None:
    """Client-side identity checks before any transaction: the record is
    Active over the planned range and declares the wallet key."""
    vid.require_active(records, window.lo, window.hi)
    vid.require_declared_credential(records[0], wallet_key_hash)
    ancestors = ", ".join(r.did for r in records[1:]) or "none"
    print(f"[OK] {label}: {records[0].did} is Active (record {records[0].utxo.input.transaction_id}"
          f"#{records[0].utxo.input.index}, ancestors: {ancestors})")


async def smoke_test(args):
    """Run end-to-end governance smoke test."""
    print("=" * 60)
    print("Module 6: Self-Improvement Module — Smoke Test")
    print("=" * 60)

    path = state_path(args)
    state = load_deploy_state(path, args.network)
    endpoints = state_endpoints(state, args.network)

    print(f"\nNetwork: {state['network']}")
    print(f"State: {path}")
    print(f"Wallet: {state.get('wallet_address', state.get('operator_address', '-'))}")
    print(f"Explorer: {endpoints['explorer_url']}")
    print(f"Proposal hash: {state.get('proposal_validator_hash', 'NOT SET')}")
    print(f"Critique hash: {state.get('critique_validator_hash', 'NOT SET')}")

    # The deployment must read the identity registry (identity_hash).
    try:
        deployment = gi.GovernanceDeployment.from_state(state, str(path))
        agent_id = gi.agent_id_from(args.agent)
        endorser_value = args.endorser or os.environ.get(ENDORSER_DID_ENV, "")
        endorser_id = gi.agent_id_from(endorser_value) if endorser_value else None
    except gi.GovernanceClientError as e:
        print(f"\n[ERROR] {e}")
        return
    print(f"Identity hash: {deployment.identity_hash.hex()}")
    print(f"Agent: {vid.did_from_id(agent_id)}")
    print(f"Endorser: {vid.did_from_id(endorser_id) if endorser_id else 'not set (steps 6-7 skipped)'}")

    # Check if deployment was completed (tx_hashes should be populated)
    tx_hashes = state.get("tx_hashes", {})
    if not tx_hashes:
        print("\n[WARN] No deployment transactions found. Run deploy.py first.")
        print("       Running offline validation instead...")
        await offline_validation(state, str(path))
        return

    print("\n--- Test Plan ---")
    print("  1. Query wallet balance")
    print("  2. Submit a proposal (lock, then SubmitProposal) — identity check")
    print("  3. Submit an Amendment critique (lock, then MintCritiqueToken) — identity check")
    print("  4. Withdraw the critique (GovernanceClient)")
    print("  5. Amend the proposal (AmendProposal) — identity check")
    print("  6. Endorse the proposal with the second agent — identity check")
    print("  7. Withdraw the endorsement (GovernanceClient)")
    print(f"  8. {args.finalise.capitalize()} the proposal (GovernanceClient)")
    print("  9. Expire the proposal (needs the review window — skipped)")

    # Import SDK
    try:
        from vector_agent import VectorAgent
        from vector_agent.governance import GovernanceClient, GovernanceConfigError, GovernanceDeployment
        print("\n[OK] SDK imported successfully")
    except ImportError as e:
        print(f"\n[SKIP] SDK import failed: {e}")
        print('       Install SDK 0.2.0 or later: pip install "apex-fusion-agent-sdk>=0.2.0"')
        print("\n--- Offline Validation ---")
        await offline_validation(state, str(path))
        return

    # GovernanceClient builds the exits. It derives the five scripts from its
    # embedded blueprint and the values of the deploy state, and it refuses
    # validator hashes that differ. Check this before step 2 locks a stake.
    try:
        sdk_deployment = GovernanceDeployment.from_deploy_state(state)
        sdk_deployment.scripts()
    except GovernanceConfigError as e:
        print(f"\n[ERROR] {e}")
        return

    # Connect to the network of the deployment
    ogmios_url = endpoints["ogmios_url"]
    submit_url = endpoints["submit_url"]

    skey_path = str(Path("wallets/payment.skey").absolute())

    try:
        async with VectorAgent(
            ogmios_url=ogmios_url,
            submit_url=submit_url,
            skey_path=skey_path,
        ) as agent:
            context = agent.context
            gov = GovernanceClient(agent, sdk_deployment)

            keys = gi.SigningKeyPair(agent._wallet.payment_signing_key, agent._wallet.payment_verification_key)
            wallet_address = agent._wallet.payment_address

            # Reference inputs and identity records: check them before any transaction
            try:
                refs = await gi.load_references(context, deployment)
                params = refs.params.params
                print(f"\n[OK] GovernanceParams record: {refs.params.utxo.input.transaction_id}#{refs.params.utxo.input.index}")
                print(f"[OK] CrossRefs: {refs.cross_refs.utxo.input.transaction_id}#{refs.cross_refs.utxo.input.index} (names this deployment)")
                if args.finalise == "adopt":
                    oracle = gi.find_oracle(await context.async_utxos(str(deployment.oracle_address)), deployment)
                    print(f"[OK] Oracle record: {oracle.input.transaction_id}#{oracle.input.index}")
                window = await gi.plan_window(context)
                check_agent("Agent", await gi.load_identity(context, deployment, agent_id), keys.key_hash, window)
                if endorser_id:
                    check_agent("Endorser", await gi.load_identity(context, deployment, endorser_id), keys.key_hash, window)
            except (gi.GovernanceClientError, vid.IdentityError) as e:
                print(f"\n[ERROR] {e}")
                return

            passed = 0
            failed = 0
            skipped = 0
            explorer = endpoints["explorer_url"]

            def tx_link(tx_hash):
                return f"{explorer}/tx/{tx_hash}"

            def fail(step, what, error):
                nonlocal failed
                print(f"\n[{step}] FAIL — {what}: {error}")
                traceback.print_exc()
                failed += 1

            # Test 1: Balance
            balance = await gov.get_balance()
            print(f"\n[1] PASS — Balance: {balance['ada']} AP3X ({balance['lovelace']} lovelace)")
            passed += 1

            if balance["lovelace"] < 50_000_000:
                print("    WARN: Less than 50 AP3X. Fund wallet first.")
                return

            # Test 2: Submit proposal (identity check)
            proposal = None
            try:
                proposal = await gi.submit_proposal(
                    context, deployment, agent_id=agent_id,
                    terms=gi.ProposalTerms(
                        proposal_hash=blake2b_256("Smoke test proposal: improve validator error messages"),
                        storage_uri="ipfs://QmSmokeTestProposal",
                        stake_amount=params.min_proposal_stake,
                        review_window=params.min_review_window,
                    ),
                    wallet=keys, wallet_address=wallet_address,
                )
                await gi.wait_for_output(context, deployment.proposal_spend.address, proposal.position)
                print(f"\n[2] PASS — Proposal submitted: {proposal.tx}")
                print(f"    Lock TX: {proposal.lock_tx}")
                print(f"    Token: {proposal.minted[0].hex()}")
                print(f"    Explorer: {tx_link(proposal.tx)}")
                passed += 1
            except Exception as e:
                fail(2, "Submit proposal", e)

            if not proposal:
                print("\n[SKIP] Steps 3-8 need a submitted proposal")
                print(f"\n{'=' * 60}")
                print(f"Results: {passed} passed, {failed} failed, {skipped + 6} skipped")
                return

            # Test 3: Amendment critique of the agent's own proposal (identity check)
            critique = None
            try:
                critique = await gi.submit_critique(
                    context, deployment, agent_id=agent_id, proposal_ref=proposal.seed,
                    terms=gi.CritiqueTerms(
                        critique_hash=blake2b_256("Smoke test critique: good proposal, needs more data"),
                        storage_uri="ipfs://QmSmokeTestCritique",
                        stake_amount=params.min_critique_stake,
                        critique_type=gi.amendment_critique(blake2b_256("Smoke test suggested change")),
                    ),
                    wallet=keys, wallet_address=wallet_address,
                )
                await gi.wait_for_output(context, deployment.critique_spend.address, critique.position)
                print(f"\n[3] PASS — Critique submitted: {critique.tx}")
                print(f"    Explorer: {tx_link(critique.tx)}")
                passed += 1
            except Exception as e:
                fail(3, "Submit critique", e)

            # Test 4: Withdraw the critique (no identity check). The validator
            # reads an active proposal as a reference input; GovernanceClient
            # adds the proposal of the critique.
            if critique:
                try:
                    result = await gov.withdraw_critique(utxo_ref(critique.position))
                    print(f"\n[4] PASS — Critique withdrawn: {result['tx_hash']}")
                    print(f"    Explorer: {tx_link(result['tx_hash'])}")
                    passed += 1
                    print(f"    Waiting {TX_WAIT}s for tx confirmation...")
                    await asyncio.sleep(TX_WAIT)
                except Exception as e:
                    fail(4, "Withdraw critique", e)
            else:
                print("\n[4] SKIP — No critique to withdraw")
                skipped += 1

            # Test 5: Amend the proposal (identity check)
            current = proposal.position
            try:
                amended = await gi.amend_proposal(
                    context, deployment, proposal_ref=proposal.position,
                    new_hash=blake2b_256("Smoke test proposal v2: improve validator error messages"),
                    new_uri="ipfs://QmSmokeTestProposalV2",
                    wallet=keys, wallet_address=wallet_address,
                )
                await gi.wait_for_output(context, deployment.proposal_spend.address, amended.position)
                current = amended.position
                print(f"\n[5] PASS — Proposal amended: {amended.tx}")
                print(f"    Explorer: {tx_link(amended.tx)}")
                passed += 1
            except Exception as e:
                fail(5, "Amend proposal", e)

            # Tests 6-7: Endorse with the second agent, then withdraw
            if endorser_id:
                endorsement = None
                try:
                    endorsement = await gi.submit_endorsement(
                        context, deployment, agent_id=endorser_id, proposal_ref=proposal.seed,
                        stake_amount=params.min_governance_endorsement,
                        wallet=keys, wallet_address=wallet_address,
                    )
                    await gi.wait_for_output(context, deployment.endorsement_spend.address, endorsement.position)
                    print(f"\n[6] PASS — Endorsement submitted: {endorsement.tx}")
                    print(f"    Explorer: {tx_link(endorsement.tx)}")
                    passed += 1
                except Exception as e:
                    fail(6, "Endorse proposal", e)
                if endorsement:
                    try:
                        result = await gov.withdraw_endorsement(utxo_ref(endorsement.position))
                        print(f"\n[7] PASS — Endorsement withdrawn: {result['tx_hash']}")
                        print(f"    Explorer: {tx_link(result['tx_hash'])}")
                        passed += 1
                        print(f"    Waiting {TX_WAIT}s for tx confirmation...")
                        await asyncio.sleep(TX_WAIT)
                    except Exception as e:
                        fail(7, "Withdraw endorsement", e)
                else:
                    print("\n[7] SKIP — No endorsement to withdraw")
                    skipped += 1
            else:
                print(f"\n[6] SKIP — No second agent ({ENDORSER_DID_ENV} or --endorser)")
                print("[7] SKIP — No endorsement to withdraw")
                skipped += 2

            # Test 8: Withdraw or adopt the proposal (no identity check).
            # GovernanceClient spends the node of the proposer in the activity
            # list (one active proposal less); an adoption reads the oracle record.
            try:
                if args.finalise == "adopt":
                    result = await gov.adopt_proposal(
                        utxo_ref(current),
                        reasoning_hash=blake2b_256("Adoption reasoning: proposal meets governance quality bar"),
                        reward_amount=params.min_adoption_reward,
                    )
                    print(f"\n[8] PASS — Proposal adopted: {result['tx_hash']}")
                    print(f"    Reward: {result['reward'] / 1_000_000} AP3X")
                else:
                    result = await gov.withdraw_proposal(utxo_ref(current))
                    print(f"\n[8] PASS — Proposal withdrawn (token burned): {result['tx_hash']}")
                print(f"    Explorer: {tx_link(result['tx_hash'])}")
                passed += 1
            except Exception as e:
                fail(8, f"{args.finalise.capitalize()} proposal", e)

            # Test 9: Expire proposal (requires review window elapsed)
            # The minimum review window is 3 days (GovernanceParams). Use
            # scripts/test_expire_e2e.py to expire a proposal after its window.
            print(f"\n[9] SKIP — Expire proposal (requires review window elapsed, use scripts/test_expire_e2e.py)")
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
        traceback.print_exc()
        print("\n--- Falling back to offline validation ---")
        await offline_validation(state, str(path))


async def offline_validation(state: dict, source: str = "the deploy state"):
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

    # Check the identity registry deployment
    try:
        deployment = gi.GovernanceDeployment.from_state(state, source)
        print(f"  [OK] identity_hash: {deployment.identity_hash.hex()}")
        print(f"       identity address: {deployment.identity_address}")
    except gi.GovernanceClientError as e:
        print(f"  [FAIL] {e}")

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
    asyncio.run(smoke_test(parse_args()))
