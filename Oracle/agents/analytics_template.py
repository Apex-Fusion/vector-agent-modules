"""
Oracle: Chain Analytics Agent Template

Demonstrates an autonomous governance agent that:
1. Monitors parameter fitness (compares chain metrics to targets)
2. Monitors treasury health (balance, burn rate, runway)
3. Reviews peer proposals for critique opportunities

This is a template — adapt the analysis logic and thresholds to your
specific monitoring goals.

Usage:
    nix-shell shell.nix --run "python agents/analytics_template.py"

    # Monitor-only (no proposal submission):
    nix-shell shell.nix --run "python agents/analytics_template.py --monitor-only"

Requires:
    - Wallet with registered DID and sufficient AP3X for stakes
    - Full deployment completed (deploy.py)
    - GovernanceClient and GovernanceIndexer configured
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

DEPLOY_STATE_FILE = Path("wallets/deploy_state.json")
WALLET_FILE = Path("wallets/governance_wallet.json")

# Governance parameter fitness targets
# These are the "healthy" ranges; proposals should be submitted when
# observed values fall outside these bounds.
PARAMETER_TARGETS = {
    "MIN_CLAIM_STAKE": {"min": 10_000_000, "max": 50_000_000, "unit": "lovelace"},
    "MIN_CHALLENGE_WINDOW": {"min": 43_200, "max": 172_800, "unit": "slots"},
    "JURY_SIZE": {"min": 3, "max": 7, "unit": "count"},
    "MIN_SELF_STAKE": {"min": 10_000_000, "max": 100_000_000, "unit": "lovelace"},
    "MIN_PROPOSAL_STAKE": {"min": 15_000_000, "max": 50_000_000, "unit": "lovelace"},
}

# Treasury health thresholds (§7.1)
TREASURY_LOW_THRESHOLD_APEX = 2_500  # MIN_TREASURY_BATCHES × BATCH_SIZE
TREASURY_TARGET_RUNWAY_EPOCHS = 90  # Target: 90+ epochs (~15 days)


class GovernanceAnalyticsAgent:
    """Autonomous chain analytics agent for governance intelligence."""

    def __init__(self, indexer, client=None, agent_did=None):
        self.indexer = indexer
        self.client = client
        self.agent_did = agent_did

    async def run_analysis(self, monitor_only: bool = False):
        """Run full governance analysis cycle."""
        print("=" * 60)
        print("Oracle: Chain Analytics Agent")
        print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
        print("=" * 60)

        # 1. Treasury health
        treasury = await self.analyze_treasury_health()

        # 2. Governance activity metrics
        metrics = await self.analyze_governance_metrics()

        # 3. Peer proposal review
        opportunities = await self.review_peer_proposals()

        # 4. Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Treasury:     {'HEALTHY' if treasury['healthy'] else 'LOW'} ({treasury['total_apex']:.1f} AP3X, {treasury['active_batches']} batches)")
        print(f"Proposals:    {metrics['total_open']} open, {metrics['total_proposals']} total")
        print(f"Adoption:     {metrics['adoption_rate']:.0%}")
        print(f"Critique ops: {len(opportunities)} proposals could use critiques")

        if not monitor_only and opportunities:
            print(f"\n[ACTION] Consider critiquing {len(opportunities)} proposal(s)")
            for opp in opportunities[:3]:
                print(f"  - {opp['proposal_hash'][:16]}... ({opp['reason']})")

        return {
            "treasury": treasury,
            "metrics": metrics,
            "opportunities": opportunities,
        }

    async def analyze_treasury_health(self) -> dict:
        """Monitor treasury balance and compute runway.

        Runway = balance / (avg_rewards_per_epoch × 90)
        Target: > 90 epochs (~15 days)
        """
        print("\n--- Treasury Health ---")

        balance = await self.indexer.get_treasury_balance()
        total_apex = balance["total_lovelace"] / 1_000_000
        batch_count = balance["utxo_count"]

        # Estimate runway (simplified: assume 1 adoption per epoch at avg 100 AP3X)
        avg_reward_per_epoch = 100  # AP3X, conservative estimate
        runway_epochs = total_apex / avg_reward_per_epoch if avg_reward_per_epoch > 0 else 0

        healthy = total_apex >= TREASURY_LOW_THRESHOLD_APEX
        status = "HEALTHY" if healthy else "LOW"

        print(f"  Balance:        {total_apex:.1f} AP3X ({balance['total_lovelace']:,} lovelace)")
        print(f"  Active batches: {batch_count}")
        print(f"  Est. runway:    {runway_epochs:.0f} epochs")
        print(f"  Status:         {status}")

        if not healthy:
            print(f"  [ALERT] Treasury below {TREASURY_LOW_THRESHOLD_APEX} AP3X threshold!")
            print(f"          Run: python scripts/treasury_replenish.py")

        return {
            "total_apex": total_apex,
            "total_lovelace": balance["total_lovelace"],
            "active_batches": batch_count,
            "runway_epochs": runway_epochs,
            "healthy": healthy,
        }

    async def analyze_governance_metrics(self) -> dict:
        """Analyze governance activity and participation."""
        print("\n--- Governance Metrics ---")

        # Get all proposals
        all_proposals = await self.indexer.get_proposals()
        open_proposals = [p for p in all_proposals if p["state"] == "Open"]
        amended = [p for p in all_proposals if p["state"] == "Amended"]
        adopted = [p for p in all_proposals if p["state"] == "Adopted"]
        rejected = [p for p in all_proposals if p["state"] == "Rejected"]
        expired = [p for p in all_proposals if p["state"] == "Expired"]

        total = len(all_proposals)
        finalized = len(adopted) + len(rejected) + len(expired)
        adoption_rate = len(adopted) / finalized if finalized > 0 else 0

        # Unique proposers
        unique_proposers = set(p["proposer_did"] for p in all_proposals)

        # Average stake
        avg_stake = sum(p["stake_amount"] for p in all_proposals) / total if total > 0 else 0

        print(f"  Total proposals: {total}")
        print(f"    Open:     {len(open_proposals)}")
        print(f"    Amended:  {len(amended)}")
        print(f"    Adopted:  {len(adopted)}")
        print(f"    Rejected: {len(rejected)}")
        print(f"    Expired:  {len(expired)}")
        print(f"  Adoption rate:     {adoption_rate:.0%} (target: 20-30%)")
        print(f"  Unique proposers:  {len(unique_proposers)}")
        print(f"  Avg stake:         {avg_stake / 1_000_000:.1f} AP3X")

        if adoption_rate < 0.10 and finalized >= 10:
            print(f"  [WARN] Adoption rate below 10% — proposals may lack quality")
        if len(open_proposals) == 0 and total > 0:
            print(f"  [INFO] No open proposals — governance may be idle")

        return {
            "total_proposals": total,
            "total_open": len(open_proposals),
            "total_adopted": len(adopted),
            "total_rejected": len(rejected),
            "total_expired": len(expired),
            "adoption_rate": adoption_rate,
            "unique_proposers": len(unique_proposers),
            "avg_stake_lovelace": avg_stake,
        }

    async def review_peer_proposals(self) -> list[dict]:
        """Review open proposals for critique opportunities.

        Identifies proposals that:
        - Have no critiques yet (opportunity for first-mover timeliness bonus)
        - Have only supportive critiques (opposing view adds novelty)
        - Are ParameterChange type (data-driven analysis is possible)
        """
        print("\n--- Peer Proposal Review ---")

        open_proposals = await self.indexer.get_proposals(state="Open")
        amended_proposals = await self.indexer.get_proposals(state="Amended")
        reviewable = open_proposals + amended_proposals

        opportunities = []

        for proposal in reviewable:
            ref = proposal.get("utxo_ref", {})
            tx_hash = ref.get("tx_hash", "")
            idx = ref.get("output_index", 0)

            # Skip own proposals
            if self.agent_did and proposal.get("proposer_did") == self.agent_did:
                continue

            critiques = await self.indexer.get_critiques(tx_hash, idx)

            reason = None
            priority = 0

            if len(critiques) == 0:
                reason = "no critiques yet — timeliness bonus"
                priority = 3
            elif all(c.get("critique_type") == "Supportive" for c in critiques):
                reason = "only supportive critiques — opposing view adds novelty"
                priority = 2
            elif proposal.get("proposal_type") == "ParameterChange" and len(critiques) < 3:
                reason = "ParameterChange with < 3 critiques — data analysis opportunity"
                priority = 1

            if reason:
                opportunities.append({
                    "proposal_hash": proposal.get("proposal_hash", ""),
                    "proposal_type": proposal.get("proposal_type", ""),
                    "proposer_did": proposal.get("proposer_did", ""),
                    "stake_amount": proposal.get("stake_amount", 0),
                    "existing_critiques": len(critiques),
                    "reason": reason,
                    "priority": priority,
                    "utxo_ref": ref,
                })

        opportunities.sort(key=lambda x: x["priority"], reverse=True)

        print(f"  Reviewable proposals: {len(reviewable)}")
        print(f"  Critique opportunities: {len(opportunities)}")
        for opp in opportunities[:5]:
            print(f"    [{opp['priority']}] {opp['proposal_hash'][:16]}... — {opp['reason']}")

        return opportunities


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Oracle Chain Analytics Agent")
    parser.add_argument("--monitor-only", action="store_true",
                        help="Monitor only, don't suggest actions")
    args = parser.parse_args()

    state = json.load(open(DEPLOY_STATE_FILE))
    skey_path = str(Path("wallets/payment.skey").absolute())

    validators = state.get("validators", {})
    proposal_hash = state.get("proposal_validator_hash", "")
    critique_hash = state.get("critique_validator_hash", "")
    holders = state.get("holders", {})
    treasury_addr_hash = holders.get("treasury", {}).get("hash", "")

    from vector_agent import VectorAgent
    from vector_agent.governance.indexer import GovernanceIndexer

    ogmios_url = state.get("ogmios_url", os.getenv("VECTOR_OGMIOS_URL"))
    submit_url = state.get("submit_url", os.getenv("VECTOR_SUBMIT_URL"))

    async with VectorAgent(
        ogmios_url=ogmios_url,
        submit_url=submit_url,
        skey_path=skey_path,
    ) as agent:
        # Build treasury address
        from pycardano import Address, Network
        from pycardano.hash import ScriptHash
        treasury_addr = ""
        if treasury_addr_hash:
            sh = ScriptHash.from_primitive(bytes.fromhex(treasury_addr_hash))
            treasury_addr = str(Address(payment_part=sh, network=Network.MAINNET))

        indexer = GovernanceIndexer(
            context=agent.context,
            proposal_spend_hash=proposal_hash,
            critique_spend_hash=critique_hash,
            treasury_address=treasury_addr,
        )

        analytics = GovernanceAnalyticsAgent(
            indexer=indexer,
            agent_did=None,  # Set to your DID to exclude own proposals from review
        )

        result = await analytics.run_analysis(monitor_only=args.monitor_only)


if __name__ == "__main__":
    asyncio.run(main())
