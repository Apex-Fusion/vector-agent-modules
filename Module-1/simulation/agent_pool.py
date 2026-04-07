"""
Agent Pool — Simulated agents with diverse strategies.

Each agent has a type (Honest/Auditor/Opportunist/Adversary), probabilistic
parameters, and a decision function that determines actions per epoch.
"""
import hashlib
import json
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from simulation.config import SimConfig, GameParams, SIM_DIR


# ═══════════════════════════════════════════════════════════════════════
# AGENT TYPES
# ═══════════════════════════════════════════════════════════════════════

class AgentType(Enum):
    HONEST = "honest"           # 60% — mostly valid claims, low detection
    AUDITOR = "auditor"         # 20% — valid claims, high detection
    OPPORTUNIST = "opportunist" # 15% — mixed honest/fraud, medium detection
    ADVERSARY = "adversary"     # 5%  — mostly fraud, low detection


# ═══════════════════════════════════════════════════════════════════════
# AGENT MODEL
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SimAgent:
    """A simulated agent participating in Module 1."""
    id: int
    agent_type: AgentType
    did_hex: str                          # Agent Registry DID
    wallet_address: str                   # Wallet address for TX building
    wallet_id: int                        # Index into wallet list

    # Strategy parameters (drawn from distributions at init)
    p_honest: float                       # Prob of submitting honest claim
    p_detect: float                       # Prob of detecting fraud in others' claims
    claim_rate: float = 0.1              # Poisson λ per epoch

    # Juror status
    is_juror: bool = False
    juror_token_hex: str = ""
    juror_utxo_ref: str = ""

    # Tracking (updated during simulation)
    ap3x_balance: int = 0                 # Current AP3X holdings (tracked locally)
    claims_submitted: int = 0
    claims_honest: int = 0
    claims_fraudulent: int = 0
    claims_won: int = 0
    claims_lost: int = 0
    challenges_filed: int = 0
    challenges_won: int = 0
    challenges_lost: int = 0
    votes_cast: int = 0
    votes_majority: int = 0
    total_earned: int = 0
    total_lost: int = 0
    total_fees_earned: int = 0            # Jury fees


# ═══════════════════════════════════════════════════════════════════════
# AGENT DECISIONS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ClaimDecision:
    """Agent decided to submit a claim."""
    agent_id: int
    stake_amount: int
    is_honest: bool
    evidence_hash: bytes


@dataclass
class ChallengeDecision:
    """Agent decided to challenge a claim."""
    agent_id: int
    target_claim_ref: str
    target_claim_token: str
    stake_amount: int


@dataclass
class WithdrawDecision:
    """Agent decided to withdraw an unchallenged claim."""
    agent_id: int
    claim_ref: str
    claim_token: str


@dataclass
class VoteDecision:
    """Juror decided how to vote on a challenge."""
    agent_id: int
    challenge_ref: str
    verdict: str                          # "ClaimerWins" or "AuditorWins"
    salt: bytes
    commitment: bytes


# ═══════════════════════════════════════════════════════════════════════
# POPULATION CREATION
# ═══════════════════════════════════════════════════════════════════════

def create_population(registrations: list, config: SimConfig,
                      seed: int = None) -> list:
    """Create a population of SimAgents from registered wallets.

    registrations: list of {id, did_hex, wallet_address} from wallet_factory
    config: simulation configuration
    seed: random seed for reproducibility
    """
    if seed is None:
        seed = config.random_seed
    rng = np.random.default_rng(seed)
    n = len(registrations)

    # Agent type mixture
    type_probs = [0.60, 0.20, 0.15, 0.05]
    types = rng.choice(
        [AgentType.HONEST, AgentType.AUDITOR, AgentType.OPPORTUNIST, AgentType.ADVERSARY],
        size=n,
        p=type_probs,
    )

    agents = []
    for i, (reg, agent_type) in enumerate(zip(registrations, types)):
        # Draw strategy parameters from Beta distributions (per spec §4.4)
        p_honest = {
            AgentType.HONEST: rng.beta(19, 1),
            AgentType.AUDITOR: rng.beta(9, 1),
            AgentType.OPPORTUNIST: rng.beta(5, 5),
            AgentType.ADVERSARY: rng.beta(1, 9),
        }[agent_type]

        p_detect = {
            AgentType.HONEST: rng.beta(3, 7),
            AgentType.AUDITOR: rng.beta(8, 2),
            AgentType.OPPORTUNIST: rng.beta(5, 5),
            AgentType.ADVERSARY: rng.beta(2, 8),
        }[agent_type]

        agent = SimAgent(
            id=reg["id"],
            agent_type=agent_type,
            did_hex=reg["did_hex"],
            wallet_address=reg["wallet_address"],
            wallet_id=reg["id"],
            p_honest=float(np.clip(p_honest, 0.01, 0.99)),
            p_detect=float(np.clip(p_detect, 0.01, 0.99)),
            claim_rate=config.claim_rate_per_agent,
            ap3x_balance=config.initial_ap3x_per_agent,
        )
        agents.append(agent)

    # Summary
    type_counts = {}
    for a in agents:
        type_counts[a.agent_type.value] = type_counts.get(a.agent_type.value, 0) + 1
    print(f"  Population: {n} agents — {type_counts}")

    return agents


# ═══════════════════════════════════════════════════════════════════════
# DECISION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def decide_claims(agents: list, rng: np.random.Generator,
                  min_stake: int = 50_000_000) -> list:
    """Each agent decides whether to submit a claim this epoch.

    Returns list of ClaimDecision.
    """
    decisions = []
    for agent in agents:
        # Poisson draw — does this agent submit a claim?
        if rng.poisson(agent.claim_rate) == 0:
            continue

        # Must have enough AP3X
        if agent.ap3x_balance < min_stake:
            continue

        # Stake amount: LogNormal, capped at 30% of balance
        raw_stake = int(rng.lognormal(mean=17.7, sigma=0.5))
        stake = max(min_stake, min(raw_stake, int(agent.ap3x_balance * 0.3)))

        # Honest or fraudulent?
        is_honest = rng.random() < agent.p_honest

        # Evidence hash
        if is_honest:
            evidence_hash = hashlib.blake2b(
                f"honest-claim-{agent.id}-{agent.claims_submitted}".encode(),
                digest_size=32).digest()
        else:
            evidence_hash = hashlib.blake2b(
                f"fraudulent-{agent.id}-{rng.integers(0, 2**32)}".encode(),
                digest_size=32).digest()

        decisions.append(ClaimDecision(
            agent_id=agent.id,
            stake_amount=stake,
            is_honest=is_honest,
            evidence_hash=evidence_hash,
        ))

    return decisions


def decide_challenges(agents: list, open_claims: list,
                      claim_ground_truth: dict,
                      rng: np.random.Generator,
                      min_stake: int = 50_000_000) -> list:
    """Each agent decides whether to challenge any open claims.

    open_claims: list of ClaimState from world_state
    claim_ground_truth: {claim_ref → is_honest} mapping
    Returns list of ChallengeDecision.
    """
    decisions = []
    already_challenged = set()  # prevent duplicate challenges

    for agent in agents:
        for claim in open_claims:
            # Skip own claims
            if claim.agent_id == agent.id:
                continue
            # Skip already-challenged claims (by anyone this epoch)
            if claim.utxo_ref in already_challenged:
                continue

            # Is it fraudulent? (ground truth)
            is_fraudulent = not claim_ground_truth.get(claim.utxo_ref, True)

            if not is_fraudulent:
                # Small chance of false accusation (1 - p_detect applied to honest claims)
                if rng.random() > 0.02:  # 2% false accusation rate
                    continue

            # Detection probability
            if rng.random() > agent.p_detect:
                continue

            # Must have enough stake
            if agent.ap3x_balance < claim.stake_amount:
                continue

            decisions.append(ChallengeDecision(
                agent_id=agent.id,
                target_claim_ref=claim.utxo_ref,
                target_claim_token=claim.claim_token,
                stake_amount=claim.stake_amount,
            ))
            already_challenged.add(claim.utxo_ref)
            break  # One challenge per agent per epoch

    return decisions


def decide_votes(juror_agents: list, active_challenges: list,
                 claim_ground_truth: dict,
                 rng: np.random.Generator) -> list:
    """Jurors decide how to vote on challenges they're selected for.

    For simulation: jurors vote based on their p_detect (ability to assess truth).
    Returns list of VoteDecision.
    """
    decisions = []

    for challenge in active_challenges:
        for agent in juror_agents:
            if agent.did_hex not in challenge.selected_jurors:
                continue
            if agent.juror_utxo_ref == "":
                continue

            # Determine ground truth for the challenged claim
            is_claim_honest = claim_ground_truth.get(challenge.claim_ref, True)

            # Juror's assessment (probabilistic)
            if rng.random() < agent.p_detect:
                # Correctly assesses
                verdict = "AuditorWins" if not is_claim_honest else "ClaimerWins"
            else:
                # Incorrectly assesses — votes opposite
                verdict = "ClaimerWins" if not is_claim_honest else "AuditorWins"

            # Generate commitment
            salt = rng.bytes(32)
            verdict_byte = b"\x00" if verdict == "ClaimerWins" else b"\x01"
            commitment = hashlib.blake2b(verdict_byte + salt, digest_size=32).digest()

            decisions.append(VoteDecision(
                agent_id=agent.id,
                challenge_ref=challenge.utxo_ref,
                verdict=verdict,
                salt=salt,
                commitment=commitment,
            ))

    return decisions


def decide_withdrawals(agents: list, open_claims: list,
                       current_time_ms: int) -> list:
    """Agents withdraw unchallenged claims whose window has expired.

    Returns list of WithdrawDecision.
    """
    decisions = []
    for claim in open_claims:
        if claim.state != "Open":
            continue
        # Check if challenge window expired
        deadline = claim.submitted_at + claim.challenge_window
        if current_time_ms <= deadline:
            continue
        # Find the agent who owns this claim
        agent = next((a for a in agents if a.did_hex == claim.claimer_did), None)
        if agent is None:
            continue

        decisions.append(WithdrawDecision(
            agent_id=agent.id,
            claim_ref=claim.utxo_ref,
            claim_token=claim.claim_token,
        ))

    return decisions


# ═══════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════

def save_population(agents: list, path: Path = None):
    """Save agent population state to JSON."""
    if path is None:
        path = SIM_DIR / "population.json"
    data = []
    for a in agents:
        data.append({
            "id": a.id,
            "type": a.agent_type.value,
            "did_hex": a.did_hex,
            "wallet_address": a.wallet_address,
            "p_honest": round(a.p_honest, 4),
            "p_detect": round(a.p_detect, 4),
            "is_juror": a.is_juror,
            "ap3x_balance": a.ap3x_balance,
            "claims_submitted": a.claims_submitted,
            "claims_honest": a.claims_honest,
            "claims_fraudulent": a.claims_fraudulent,
            "challenges_filed": a.challenges_filed,
            "total_earned": a.total_earned,
            "total_lost": a.total_lost,
        })
    path.write_text(json.dumps(data, indent=2))
    print(f"  Saved population ({len(agents)} agents) to {path}")


def load_population(path: Path = None) -> list:
    """Load agent population from JSON."""
    if path is None:
        path = SIM_DIR / "population.json"
    data = json.loads(path.read_text())
    agents = []
    for d in data:
        agents.append(SimAgent(
            id=d["id"],
            agent_type=AgentType(d["type"]),
            did_hex=d["did_hex"],
            wallet_address=d["wallet_address"],
            wallet_id=d["id"],
            p_honest=d["p_honest"],
            p_detect=d["p_detect"],
            is_juror=d.get("is_juror", False),
            ap3x_balance=d.get("ap3x_balance", 0),
            claims_submitted=d.get("claims_submitted", 0),
            claims_honest=d.get("claims_honest", 0),
            claims_fraudulent=d.get("claims_fraudulent", 0),
            challenges_filed=d.get("challenges_filed", 0),
            total_earned=d.get("total_earned", 0),
            total_lost=d.get("total_lost", 0),
        ))
    print(f"  Loaded {len(agents)} agents from {path}")
    return agents
