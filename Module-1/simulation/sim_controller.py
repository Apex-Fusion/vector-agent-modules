"""
SimController — Main simulation orchestration loop.

Runs discrete epochs: each epoch, agents make decisions, TXs are built
and submitted, state is updated, and metrics are recorded.
"""
import json
import numpy as np
import time
from pathlib import Path

from simulation.config import SimConfig, SIM_DIR, RESULTS_DIR
from simulation.chain import OgmiosContext, slot_to_posix_ms, wait_confirm
from simulation.agent_pool import (
    SimAgent, AgentType, create_population, save_population,
    decide_claims, decide_challenges, decide_withdrawals, decide_votes,
    ClaimDecision, ChallengeDecision, VoteDecision, WithdrawDecision,
)
from simulation.tx_builder import DeploymentState, build_submit_claim
from simulation.world_state import WorldState
from simulation.metrics import MetricsCollector, SimulationEvent


class SimController:
    """Main simulation orchestrator."""

    def __init__(self, config: SimConfig, deployment: DeploymentState,
                 wallets: list, agents: list,
                 scenario_name: str = "baseline"):
        self.config = config
        self.deployment = deployment
        self.wallets = {w["id"]: w for w in wallets}
        self.agents = {a.id: a for a in agents}
        self.agent_list = agents
        self.rng = np.random.default_rng(config.random_seed)

        # State
        self.context = OgmiosContext()
        self.world = WorldState(deployment)
        self.metrics = MetricsCollector(RESULTS_DIR / scenario_name)

        # Ground truth tracking (simulation knows, agents don't)
        self.claim_ground_truth = {}   # claim_ref → is_honest

        self.epoch = 0
        self.scenario_name = scenario_name

    def run(self, n_epochs: int = None):
        """Run the simulation for n_epochs."""
        if n_epochs is None:
            n_epochs = self.config.n_epochs

        print("=" * 70)
        print(f"  SIMULATION: {self.scenario_name}")
        print(f"  {len(self.agent_list)} agents, {n_epochs} epochs")
        print("=" * 70)

        for epoch in range(n_epochs):
            self.epoch = epoch
            self.run_epoch()

        # Final summary
        self.metrics.save()
        save_population(self.agent_list, self.metrics.output_dir / "final_population.json")
        summary = self.metrics.compute_summary()

        print("\n" + "=" * 70)
        print(f"  SIMULATION COMPLETE: {self.scenario_name}")
        print(f"  {summary.get('epochs_completed', 0)} epochs, "
              f"{summary.get('total_txs', 0)} TXs, "
              f"{summary.get('total_duration_seconds', 0):.0f}s")
        print(f"  Claims: {summary.get('total_claims', 0)} "
              f"({summary.get('total_honest_claims', 0)} honest, "
              f"{summary.get('total_fraudulent_claims', 0)} fraudulent)")
        print(f"  Fraud detection: {summary.get('fraud_detection_rate', 0):.1%}")
        print(f"  False accusation: {summary.get('false_accusation_rate', 0):.1%}")
        print("=" * 70)

        return summary

    def run_epoch(self):
        """Run a single epoch: decide → build TXs → submit → update state."""
        slot = self.context.last_block_slot
        current_time_ms = slot_to_posix_ms(slot)

        self.metrics.begin_epoch(self.epoch, slot)

        if self.epoch % 10 == 0:
            print(f"\n--- Epoch {self.epoch} (slot {slot}) ---")

        # 1. Refresh world state
        self.world.refresh_all(self.context)

        # 2. Collect agent decisions
        # 2a. Claims
        claim_decisions = decide_claims(self.agent_list, self.rng,
                                        self.config.game_params.min_claim_stake)

        # 2b. Challenges (against open claims from previous epochs)
        open_claims = [c for c in self.world.claims.values() if c.state == "Open"]
        challenge_decisions = decide_challenges(
            self.agent_list, open_claims,
            self.claim_ground_truth, self.rng,
            self.config.game_params.min_claim_stake)

        # 2c. Withdrawals (unchallenged claims past window)
        withdraw_decisions = decide_withdrawals(
            self.agent_list, open_claims, current_time_ms)

        # 3. Execute decisions (priority: withdrawals > challenges > claims)
        for wd in withdraw_decisions:
            self._execute_withdrawal(wd)

        for cd in challenge_decisions:
            self._execute_challenge(cd)

        for cd in claim_decisions:
            self._execute_claim(cd)

        # 4. Process jury lifecycle for active challenges
        self._process_jury_lifecycle(current_time_ms)

        # 5. End epoch
        self.metrics.end_epoch()
        self.metrics.print_epoch_summary()

    def _execute_claim(self, decision: ClaimDecision):
        """Submit a claim to the chain."""
        agent = self.agents[decision.agent_id]
        wallet = self.wallets[agent.wallet_id]

        try:
            # Phase 1 migration: build_submit_claim now requires the v13
            # 9-field ClaimDatum inputs. decision.evidence_hash carries the
            # blake2b_256 digest of the claim payload, which maps 1:1 to
            # ClaimDatum.claim_hash. claim_type / storage_uri are synthesised
            # here until ClaimDecision grows richer fields — tracked as a
            # follow-up (Claire to widen ClaimDecision before full sim run).
            result = build_submit_claim(
                self.context, self.deployment,
                wallet["skey"], wallet["vkey"], wallet["address"],
                agent.did_hex, decision.stake_amount,
                challenge_window_ms=self.config.challenge_window_ms,
                claim_hash=decision.evidence_hash,
                claim_type=b"data_indexing",
                storage_uri=f"ipfs://sim-claim-{agent.id}".encode(),
            )

            # Update agent state
            agent.ap3x_balance -= decision.stake_amount
            agent.claims_submitted += 1
            if decision.is_honest:
                agent.claims_honest += 1
            else:
                agent.claims_fraudulent += 1

            # Track ground truth
            self.claim_ground_truth[result["claim_utxo_ref"]] = decision.is_honest

            self.metrics.record_event(SimulationEvent(
                epoch=self.epoch,
                slot=self.context.last_block_slot,
                event_type="claim",
                agent_id=agent.id,
                details={
                    "is_honest": decision.is_honest,
                    "stake_amount": decision.stake_amount,
                    "claim_ref": result["claim_utxo_ref"],
                },
                tx_hash=result["tx_hash"],
                success=True,
            ))

            # Bumped 15→30 on 2026-04-21 for mainnet propagation margin.
            # Bumped 30→45 on 2026-04-23 (1.5× scale with happy_path 40→60).
            wait_confirm(secs=45)

        except Exception as e:
            self.metrics.record_event(SimulationEvent(
                epoch=self.epoch,
                slot=self.context.last_block_slot,
                event_type="claim",
                agent_id=agent.id,
                success=False,
                error=str(e)[:200],
            ))
            if self.epoch % 10 == 0:
                print(f"    ⚠ Claim failed for agent {agent.id}: {str(e)[:80]}")

    def _execute_challenge(self, decision: ChallengeDecision):
        """Submit a challenge. Placeholder — needs build_open_challenge in tx_builder."""
        agent = self.agents[decision.agent_id]

        # Check if the challenge is correct (ground truth)
        is_correct = not self.claim_ground_truth.get(decision.target_claim_ref, True)

        self.metrics.record_event(SimulationEvent(
            epoch=self.epoch,
            slot=self.context.last_block_slot,
            event_type="challenge",
            agent_id=agent.id,
            details={
                "target_claim": decision.target_claim_ref,
                "stake_amount": decision.stake_amount,
                "is_correct": is_correct,
            },
            success=False,  # Not yet implemented on-chain
            error="build_open_challenge not yet implemented",
        ))

    def _execute_withdrawal(self, decision: WithdrawDecision):
        """Withdraw an unchallenged claim. Placeholder."""
        self.metrics.record_event(SimulationEvent(
            epoch=self.epoch,
            slot=self.context.last_block_slot,
            event_type="withdraw",
            agent_id=decision.agent_id,
            details={"claim_ref": decision.claim_ref},
            success=False,
            error="build_withdraw_claim not yet implemented",
        ))

    def _process_jury_lifecycle(self, current_time_ms: int):
        """Process jury-related actions for active challenges.
        
        Handles: TransitionToVoting, SelectJury, CommitVote, RevealVote,
        ResolveJury, DistributeRewards, CleanupResolved.
        
        Placeholder — will be implemented when tx_builder has all actions.
        """
        # Phase C not yet implemented
        # For each challenge in PendingJury state past selection_delay:
        #   - TransitionToVoting
        # For each challenge in Voting state:
        #   - SelectJury (if not already selected)
        #   - CommitVote (for selected jurors)
        #   - RevealVote (after commit window)
        #   - ResolveJury (after all votes revealed)
        # For each Resolved challenge:
        #   - DistributeRewards
        #   - CleanupResolved (after buffer)
        pass


# ═══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def run_simulation(scenario_name: str = "baseline",
                   n_agents: int = 50,
                   n_epochs: int = 100,
                   deployment_path: str = None):
    """Run a complete simulation from setup to results."""
    config = SimConfig(n_agents=n_agents, n_epochs=n_epochs)

    # Load deployment
    if deployment_path is None:
        deployment_path = str(SIM_DIR.parent / "testnet" / "module1-v10-deployment.json")
    with open(deployment_path) as f:
        deploy_json = json.load(f)
    deployment = DeploymentState(deploy_json)

    # Load or create wallets
    from simulation.wallet_factory import load_wallets
    try:
        wallets = load_wallets()
    except RuntimeError:
        print("No wallets found. Run wallet_factory.setup_simulation_wallets() first.")
        return None

    # Load registrations
    reg_path = SIM_DIR / "agent_registrations.json"
    if not reg_path.exists():
        print("No registrations found. Run wallet_factory.register_agents() first.")
        return None
    registrations = json.loads(reg_path.read_text())

    # Create population
    agents = create_population(registrations[:n_agents], config)

    # Run
    controller = SimController(config, deployment, list({w["id"]: w for w in wallets}.values()),
                                agents, scenario_name)
    return controller.run(n_epochs)


if __name__ == "__main__":
    import sys
    scenario = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    epochs = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    agents = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    run_simulation(scenario_name=scenario, n_epochs=epochs, n_agents=agents)
