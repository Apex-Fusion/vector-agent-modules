import os
"""
Metrics Collector — Records simulation events and computes statistics.
"""
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class EpochMetrics:
    """Per-epoch metrics snapshot."""
    epoch: int
    timestamp: float                    # wall clock
    slot: int                           # chain slot

    # Claims
    claims_submitted: int = 0
    claims_honest: int = 0
    claims_fraudulent: int = 0
    claims_withdrawn: int = 0

    # Challenges
    challenges_filed: int = 0
    challenges_correct: int = 0         # challenged a fraud
    challenges_incorrect: int = 0       # challenged an honest claim

    # Jury
    transitions_to_voting: int = 0
    jury_selections: int = 0
    votes_committed: int = 0
    votes_revealed: int = 0
    resolutions: int = 0
    rewards_distributed: int = 0
    cleanups: int = 0
    slashes: int = 0

    # Economics
    total_ap3x_staked: int = 0
    total_ap3x_redistributed: int = 0
    total_jury_fees: int = 0

    # TX stats
    txs_submitted: int = 0
    txs_confirmed: int = 0
    txs_failed: int = 0
    avg_tx_fee_ada: float = 0.0

    # Timing
    epoch_duration_seconds: float = 0.0

    # Verdicts
    verdicts_claimer_wins: int = 0
    verdicts_auditor_wins: int = 0
    verdicts_inconclusive: int = 0


@dataclass
class SimulationEvent:
    """Individual simulation event for detailed logging."""
    epoch: int
    slot: int
    event_type: str                     # "claim", "challenge", "vote", "resolve", etc.
    agent_id: int
    details: dict = field(default_factory=dict)
    tx_hash: str = ""
    success: bool = True
    error: str = ""


class MetricsCollector:
    """Collects events and computes epoch-level and aggregate metrics."""

    def __init__(self, output_dir: Path = None):
        if output_dir is None:
            output_dir = Path(os.environ.get("APEX_WORKSPACE", ".")) / "simulation-results" / "latest"
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.epochs: list[EpochMetrics] = []
        self.events: list[SimulationEvent] = []
        self.current_epoch: Optional[EpochMetrics] = None
        self._epoch_start_time: float = 0

    def begin_epoch(self, epoch: int, slot: int):
        """Start a new epoch."""
        self.current_epoch = EpochMetrics(
            epoch=epoch,
            timestamp=time.time(),
            slot=slot,
        )
        self._epoch_start_time = time.time()

    def end_epoch(self):
        """Finalize current epoch metrics."""
        if self.current_epoch:
            self.current_epoch.epoch_duration_seconds = time.time() - self._epoch_start_time
            self.epochs.append(self.current_epoch)
            self.current_epoch = None

    def record_event(self, event: SimulationEvent):
        """Record a simulation event."""
        self.events.append(event)

        # Update current epoch counters
        e = self.current_epoch
        if e is None:
            return

        if event.success:
            e.txs_confirmed += 1
        else:
            e.txs_failed += 1
        e.txs_submitted += 1

        if event.event_type == "claim":
            e.claims_submitted += 1
            if event.details.get("is_honest"):
                e.claims_honest += 1
            else:
                e.claims_fraudulent += 1
            e.total_ap3x_staked += event.details.get("stake_amount", 0)

        elif event.event_type == "challenge":
            e.challenges_filed += 1
            if event.details.get("is_correct"):
                e.challenges_correct += 1
            else:
                e.challenges_incorrect += 1
            e.total_ap3x_staked += event.details.get("stake_amount", 0)

        elif event.event_type == "withdraw":
            e.claims_withdrawn += 1

        elif event.event_type == "transition_to_voting":
            e.transitions_to_voting += 1

        elif event.event_type == "select_jury":
            e.jury_selections += 1

        elif event.event_type == "commit_vote":
            e.votes_committed += 1

        elif event.event_type == "reveal_vote":
            e.votes_revealed += 1

        elif event.event_type == "resolve_jury":
            e.resolutions += 1
            verdict = event.details.get("verdict", "")
            if verdict == "ClaimerWins":
                e.verdicts_claimer_wins += 1
            elif verdict == "AuditorWins":
                e.verdicts_auditor_wins += 1
            else:
                e.verdicts_inconclusive += 1

        elif event.event_type == "distribute_rewards":
            e.rewards_distributed += 1
            e.total_jury_fees += event.details.get("fee", 0)

        elif event.event_type == "cleanup":
            e.cleanups += 1

        elif event.event_type == "slash":
            e.slashes += 1

    def save(self):
        """Save all metrics to files."""
        # Epoch metrics
        epochs_path = self.output_dir / "epoch_metrics.json"
        epochs_path.write_text(json.dumps([asdict(e) for e in self.epochs], indent=2))

        # Events log
        events_path = self.output_dir / "events.jsonl"
        with open(events_path, "w") as f:
            for ev in self.events:
                f.write(json.dumps(asdict(ev)) + "\n")

        # Summary
        summary = self.compute_summary()
        summary_path = self.output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2))

        print(f"  Metrics saved to {self.output_dir}")

    def compute_summary(self) -> dict:
        """Compute aggregate statistics across all epochs."""
        if not self.epochs:
            return {}

        total_claims = sum(e.claims_submitted for e in self.epochs)
        total_honest = sum(e.claims_honest for e in self.epochs)
        total_fraud = sum(e.claims_fraudulent for e in self.epochs)
        total_challenges = sum(e.challenges_filed for e in self.epochs)
        total_correct_challenges = sum(e.challenges_correct for e in self.epochs)
        total_incorrect_challenges = sum(e.challenges_incorrect for e in self.epochs)
        total_resolutions = sum(e.resolutions for e in self.epochs)
        total_txs = sum(e.txs_submitted for e in self.epochs)
        total_confirmed = sum(e.txs_confirmed for e in self.epochs)
        total_failed = sum(e.txs_failed for e in self.epochs)
        total_duration = sum(e.epoch_duration_seconds for e in self.epochs)

        return {
            "epochs_completed": len(self.epochs),
            "total_claims": total_claims,
            "total_honest_claims": total_honest,
            "total_fraudulent_claims": total_fraud,
            "fraud_rate": total_fraud / max(total_claims, 1),
            "total_challenges": total_challenges,
            "fraud_detection_rate": total_correct_challenges / max(total_fraud, 1),
            "false_accusation_rate": total_incorrect_challenges / max(total_challenges, 1),
            "total_resolutions": total_resolutions,
            "total_txs": total_txs,
            "tx_success_rate": total_confirmed / max(total_txs, 1),
            "total_duration_seconds": total_duration,
            "avg_epoch_seconds": total_duration / max(len(self.epochs), 1),
            "throughput_txs_per_second": total_txs / max(total_duration, 1),
        }

    def print_epoch_summary(self):
        """Print summary of the last completed epoch."""
        if not self.epochs:
            return
        e = self.epochs[-1]
        print(f"  Epoch {e.epoch}: "
              f"{e.claims_submitted} claims ({e.claims_honest}h/{e.claims_fraudulent}f), "
              f"{e.challenges_filed} challenges ({e.challenges_correct} correct), "
              f"{e.resolutions} resolved, "
              f"{e.txs_confirmed}/{e.txs_submitted} TXs, "
              f"{e.epoch_duration_seconds:.1f}s")
