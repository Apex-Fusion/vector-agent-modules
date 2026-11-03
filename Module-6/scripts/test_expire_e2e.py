"""
Module 6: Test 9 — Expire Proposal (End-to-End)

Submits a fresh proposal, waits for the review window to elapse,
then expires it on-chain. Includes temporal diagnostics to verify
that review_window units are consistent with submitted_at and
on-chain current_slot (all should be POSIX ms).

The submission reads the identity record of the agent
(scripts/governance_identity.py: the lock, then SubmitProposal). The expiry
reads no identity record. GovernanceClient.expire_proposal of the Python SDK
(0.2.0 or later) builds it and finds the node of the proposer in the
activity list itself.

Usage:
    VECTOR_AGENT_DID=did:vector:<64 hex> nix-shell shell.nix --run "python scripts/test_expire_e2e.py --network testnet"
    nix-shell shell.nix --run "python scripts/test_expire_e2e.py --agent <DID or 64 hex>"

The deploy state (--state, default deploy/identity/state/<network>/deploy_state.json
of scripts/deploy.py; --network, default mainnet) must have identity_hash
(deployment order: identity registry, Module-1, Module-3, Module-6). The
state of the previous deployment is used only with --state
wallets/deploy_state.json. The wallet key must be the controller key of the
agent's record or a listed key with the transaction purpose.
"""

import argparse
import asyncio
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

import governance_identity as gi
import vector_identity as vid
from deploy_common import add_state_arguments, default_state_path, read_state, state_endpoints, state_path


def blake2b_256(data: str) -> bytes:
    return hashlib.blake2b(data.encode(), digest_size=32).digest()


def posix_ms_to_human(ms: int) -> str:
    """Convert POSIX ms to human-readable datetime string."""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return f"{ms} ms"


async def run_test(agent_arg=None, path=None, network="mainnet"):
    path = Path(path) if path is not None else default_state_path(network)
    state = read_state(path, network)
    endpoints = state_endpoints(state, network)
    skey_path = str(Path("wallets/payment.skey").absolute())

    try:
        deployment = gi.GovernanceDeployment.from_state(state, str(path))
        agent_id = gi.agent_id_from(agent_arg)
    except gi.GovernanceClientError as e:
        print(f"[ERROR] {e}")
        return False
    print(f"Agent: {vid.did_from_id(agent_id)}")

    from vector_agent import VectorAgent
    from vector_agent.governance import GovernanceClient, GovernanceConfigError, GovernanceDeployment

    # GovernanceClient builds the expiry. It derives the five scripts from its
    # embedded blueprint and the values of the deploy state, and it refuses
    # validator hashes that differ. Check this before the submission locks a
    # stake.
    try:
        sdk_deployment = GovernanceDeployment.from_deploy_state(state)
        sdk_deployment.scripts()
    except GovernanceConfigError as e:
        print(f"[ERROR] {e}")
        return False

    ogmios_url = endpoints["ogmios_url"]
    submit_url = endpoints["submit_url"]

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        context = agent.context
        keys = gi.SigningKeyPair(agent._wallet.payment_signing_key, agent._wallet.payment_verification_key)
        gov = GovernanceClient(agent, sdk_deployment)

        # ── Reference inputs and identity record ────────────────────
        try:
            refs = await gi.load_references(context, deployment)
            records = await gi.load_identity(context, deployment, agent_id)
        except (gi.GovernanceClientError, vid.IdentityError) as e:
            print(f"[ERROR] {e}")
            return False
        params = refs.params.params
        print(f"[OK] GovernanceParams record: {refs.params.utxo.input.transaction_id}#{refs.params.utxo.input.index}")
        print(f"[OK] CrossRefs: {refs.cross_refs.utxo.input.transaction_id}#{refs.cross_refs.utxo.input.index}")
        print(f"[OK] Identity record: {records[0].did} ({len(records) - 1} ancestor record(s))")
        print(f"\n--- Temporal Diagnostics ---")
        print(f"  GovernanceParams on-chain values:")
        print(f"    min_review_window:       {params.min_review_window}")
        print(f"    max_review_window:       {params.max_review_window}")
        print(f"    proposal_cooldown:       {params.proposal_cooldown}")
        print(f"    emergency_review_window: {params.emergency_review_window}")

        # ── DIAGNOSTIC: Current time ────────────────────────────────
        clock = gi.SlotClock.from_genesis(await context.async_genesis_param())
        current_slot = await context.async_last_block_slot()
        current_posix_ms = clock.posix_ms(current_slot)

        print(f"\n  Chain tip:")
        print(f"    slot:           {current_slot}")
        print(f"    posix_ms:       {current_posix_ms}")
        print(f"    human:          {posix_ms_to_human(current_posix_ms)}")
        print(f"    slot_length_ms: {clock.slot_length_ms}")
        print(f"    sys_start_ms:   {clock.system_start_ms}")

        # ── Step 1: Submit a fresh proposal ─────────────────────────
        print(f"\n{'='*60}")
        print("Step 1: Submit a fresh proposal")
        print(f"{'='*60}")

        try:
            submitted = await gi.submit_proposal(
                context, deployment, agent_id=agent_id,
                terms=gi.ProposalTerms(
                    proposal_hash=blake2b_256(f"Test 9 expire proposal {current_slot}"),
                    storage_uri="ipfs://QmExpireTestProposal",
                    stake_amount=params.min_proposal_stake,
                    review_window=params.min_review_window,
                ),
                wallet=keys, wallet_address=agent._wallet.payment_address,
            )
        except Exception as e:
            print(f"[ERROR] Submit failed: {e}")
            import traceback; traceback.print_exc()
            return False

        submit_tx = submitted.tx
        print(f"[OK] Proposal submitted: {submit_tx}")
        print(f"     Lock TX: {submitted.lock_tx}")
        print(f"     Explorer: {endpoints['explorer_url']}/tx/{submit_tx}")

        # ── Step 2: Read the on-chain datum to check review window ──
        print(f"\n{'='*60}")
        print("Step 2: Read proposal datum & check review window")
        print(f"{'='*60}")

        try:
            proposal_utxo = await gi.wait_for_output(context, deployment.proposal_spend.address, submitted.position)
        except gi.GovernanceClientError as e:
            print(f"[ERROR] {e}")
            return False

        print(f"[OK] Proposal UTxO: {submit_tx[:16]}...#{proposal_utxo.input.index}")
        print(f"[OK] Activity UTxO: {submit_tx[:16]}...#{submitted.activity.index}")

        # Read datum fields
        datum = gi.ProposalDatum.from_data(proposal_utxo.output.datum)
        submitted_at = datum.submitted_at
        review_window = datum.review_window
        expiry_time = submitted_at + review_window

        # Re-query current time
        current_slot2 = await context.async_last_block_slot()
        current_posix_ms2 = clock.posix_ms(current_slot2)

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
                print(f"       Expire it later with: python scripts/test_expire.py {submit_tx} {proposal_utxo.input.index}")
                return False
            else:
                # 90 s margin: GovernanceClient.expire_proposal starts the validity
                # range 60 s before the tip, and its lower bound must be after the
                # end of the review window.
                wait_s = remaining_s + 90
                print(f"[WAIT] Waiting {wait_s:.0f}s for review window to elapse...")
                await asyncio.sleep(wait_s)

        # ── Step 4: Expire the proposal ─────────────────────────────
        print(f"\n{'='*60}")
        print("Step 4: Expire the proposal")
        print(f"{'='*60}")

        try:
            expire_result = await gov.expire_proposal(f"{submit_tx}#{proposal_utxo.input.index}")
        except Exception as e:
            print(f"[ERROR] Expire failed: {e}")
            import traceback; traceback.print_exc()
            return False

        expire_tx = expire_result["tx_hash"]
        print(f"[OK] Proposal expired: {expire_tx}")
        print(f"     Explorer: {endpoints['explorer_url']}/tx/{expire_tx}")

        # ── Step 5: Verify ──────────────────────────────────────────
        print(f"\n{'='*60}")
        print("Step 5: Verification")
        print(f"{'='*60}")

        print(f"  Proposal token: BURNED (validated by on-chain validator)")
        print(f"  Proposer stake: RETURNED ({datum.stake_amount / 1_000_000:.0f} AP3X, validated by on-chain validator)")
        print(f"  Activity count: DECREMENTED (validated by on-chain validator)")
        print(f"\n[PASS] Test 9 — Expire Proposal: SUCCESS")

        if diff_ms < 60_000:
            print(f"\n[NOTE] Bug O confirmed: review_window units mismatch.")
            print(f"       Effective window was ~{diff_ms/1000:.0f}s instead of spec's ~3-7 days.")
            print(f"       Fix: convert all GovernanceParams temporal values to POSIX ms.")

        return True


async def main(agent_arg=None, path=None, network="mainnet"):
    print("=" * 60)
    print("Module 6: Test 9 — Expire Proposal (End-to-End)")
    print("=" * 60)

    success = await run_test(agent_arg, path, network)

    print(f"\n{'='*60}")
    if success:
        print("RESULT: PASS")
    else:
        print("RESULT: FAIL")
    print("=" * 60)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Module 6 expire test (end-to-end)")
    parser.add_argument("--agent", help=f"agent DID or 64-hex identifier (default: ${gi.AGENT_DID_ENV})")
    add_state_arguments(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.agent, state_path(args), args.network))
