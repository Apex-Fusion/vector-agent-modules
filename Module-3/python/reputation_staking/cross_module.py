"""
Cross-module bonus integration for Module 3: Reputation Staking.

Maps HistoryBonusSource variants to their originating modules and
provides helpers for computing cross-module reputation effects.

Source mapping (from impl spec Section 8.3):
  Module 1 (Adversarial Auditing):
    - ChallengeWon (0): Agent won audit challenge → +10% of challenge stake
    - AuditClaimWon (1): Won audit claim → equivalent bonus
    - JurorDuty (2): Served as juror, voted with majority → +2 AP3X

  Module 6 (Governance):
    - ProposalAdopted (3): Governance proposal adopted → +10 AP3X
    - CritiqueIncorporated (4): Critique incorporated → bonus

  Module 9 (Useful Work):
    - UsefulWorkVerified (6): Verified useful work → +5 AP3X

  Module 12 (Escrow):
    - EscrowCompleted (5): Escrow task completed → +3 AP3X

  Module 3 (Security):
    - SecurityReportValidated (7): Security report validated → bonus

  Genesis:
    - GenesisBonus (8): Early adopter bonus → 100 AP3X equivalent
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from reputation_staking.constants import DFM_PER_AP3X
from reputation_staking.models import HistoryBonusSource


@dataclass
class CrossModuleBonus:
    """Configuration for a cross-module bonus type."""
    source: HistoryBonusSource
    module_name: str
    module_number: int
    description: str
    default_bonus_ap3x: int  # Default bonus in AP3X (0 = percentage-based)
    is_percentage_based: bool  # If True, bonus is % of underlying stake


# Cross-module bonus configuration
CROSS_MODULE_BONUSES: Dict[HistoryBonusSource, CrossModuleBonus] = {
    HistoryBonusSource.ChallengeWon: CrossModuleBonus(
        source=HistoryBonusSource.ChallengeWon,
        module_name="Reputation Staking",
        module_number=3,
        description="Won a reputation challenge",
        default_bonus_ap3x=0,
        is_percentage_based=True,  # 10% of challenge stake
    ),
    HistoryBonusSource.AuditClaimWon: CrossModuleBonus(
        source=HistoryBonusSource.AuditClaimWon,
        module_name="Adversarial Auditing",
        module_number=1,
        description="Won audit challenge as claimer",
        default_bonus_ap3x=0,
        is_percentage_based=True,  # 10% of challenge stake
    ),
    HistoryBonusSource.JurorDuty: CrossModuleBonus(
        source=HistoryBonusSource.JurorDuty,
        module_name="Adversarial Auditing",
        module_number=1,
        description="Served as juror, voted with majority",
        default_bonus_ap3x=2,
        is_percentage_based=False,
    ),
    HistoryBonusSource.ProposalAdopted: CrossModuleBonus(
        source=HistoryBonusSource.ProposalAdopted,
        module_name="Governance Suggestion Engine",
        module_number=6,
        description="Governance proposal adopted",
        default_bonus_ap3x=10,
        is_percentage_based=False,
    ),
    HistoryBonusSource.CritiqueIncorporated: CrossModuleBonus(
        source=HistoryBonusSource.CritiqueIncorporated,
        module_name="Governance Suggestion Engine",
        module_number=6,
        description="Critique incorporated into proposal",
        default_bonus_ap3x=5,
        is_percentage_based=False,
    ),
    HistoryBonusSource.EscrowCompleted: CrossModuleBonus(
        source=HistoryBonusSource.EscrowCompleted,
        module_name="Escrow",
        module_number=12,
        description="Escrow task completed successfully",
        default_bonus_ap3x=3,
        is_percentage_based=False,
    ),
    HistoryBonusSource.UsefulWorkVerified: CrossModuleBonus(
        source=HistoryBonusSource.UsefulWorkVerified,
        module_name="Useful Work",
        module_number=9,
        description="Verified useful work accepted",
        default_bonus_ap3x=5,
        is_percentage_based=False,
    ),
    HistoryBonusSource.SecurityReportValidated: CrossModuleBonus(
        source=HistoryBonusSource.SecurityReportValidated,
        module_name="Security",
        module_number=3,
        description="Security report validated",
        default_bonus_ap3x=5,
        is_percentage_based=False,
    ),
    HistoryBonusSource.GenesisBonus: CrossModuleBonus(
        source=HistoryBonusSource.GenesisBonus,
        module_name="Genesis",
        module_number=0,
        description="Early adopter genesis bonus",
        default_bonus_ap3x=100,
        is_percentage_based=False,
    ),
}


def get_module_for_bonus(source: HistoryBonusSource) -> Optional[CrossModuleBonus]:
    """Look up the cross-module bonus configuration for a source type."""
    return CROSS_MODULE_BONUSES.get(source)


def compute_bonus_value(
    source: HistoryBonusSource,
    underlying_stake_dfm: int = 0,
    history_multiplier_bps: int = 1000,
) -> int:
    """Compute the effective bonus value in DFM.

    Args:
        source: The HistoryBonusSource variant.
        underlying_stake_dfm: For percentage-based bonuses, the underlying stake.
        history_multiplier_bps: Protocol params history_multiplier in basis points.

    Returns:
        Bonus value in DFM.
    """
    config = CROSS_MODULE_BONUSES.get(source)
    if not config:
        return 0

    if config.is_percentage_based:
        # Percentage of underlying stake, using history_multiplier
        return underlying_stake_dfm * history_multiplier_bps // 10_000
    else:
        return config.default_bonus_ap3x * DFM_PER_AP3X


def summarize_cross_module_bonuses(bonuses: list) -> dict:
    """Summarize history bonuses by originating module.

    Args:
        bonuses: List of HistoryBonusInfo or dicts with 'source' field.

    Returns:
        Summary dict with per-module breakdown.
    """
    by_module: Dict[str, dict] = {}

    for bonus in bonuses:
        source_name = bonus.source if isinstance(bonus.source, str) else bonus.source.name
        try:
            source_enum = HistoryBonusSource[source_name]
        except KeyError:
            continue

        config = CROSS_MODULE_BONUSES.get(source_enum)
        if not config:
            continue

        module_key = f"Module {config.module_number}: {config.module_name}"
        if module_key not in by_module:
            by_module[module_key] = {
                "module_number": config.module_number,
                "module_name": config.module_name,
                "count": 0,
                "total_bonus_points": 0,
                "sources": [],
            }

        entry = by_module[module_key]
        entry["count"] += 1
        bp = bonus.bonus_points if hasattr(bonus, "bonus_points") else bonus.get("bonus_points", 0)
        entry["total_bonus_points"] += bp
        entry["sources"].append({
            "source": source_name,
            "description": config.description,
            "bonus_points": bp,
        })

    return by_module
