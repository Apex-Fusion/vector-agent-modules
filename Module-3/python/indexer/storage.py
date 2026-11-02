"""
SQLite storage for the reputation indexer.

Stores agent scores, stakes, endorsements, challenges, and history bonuses.

Schema versions (PRAGMA user_version):
  1  the first schema.
  2  agent_scores.identity_status (the effective status of the agent's
     identity record at index time: active, suspended, revoked, retired or
     missing) and agent_scores.indexed_at_ms (the index time, POSIX ms).
  3  stakes.stake_id (the seed output reference of CreateStake, "tx#ix"),
     endorsements.target_stake (the stake_id of the stake that the
     endorsement backs) and agent_scores.stake_status (missing,
     below_minimum or staked). No action burns a stake token: a fully
     decayed stake stays with stake_amount 0 and has the stake status
     below_minimum.

`IndexerStorage` migrates an existing database file when it opens it: it
adds the new columns and keeps the rows. A row from an older schema gets
identity_status 'missing' and tier 'Unverified', an empty stake_id and
target_stake, and stake_status 'missing' until the next poll.

Agent identifiers are stored as 64 lowercase hex characters.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


DEFAULT_DB_PATH = "reputation_index.db"

SCHEMA_VERSION = 3

# Columns of later schema versions: (table, column, definition).
_ADDED_COLUMNS = (
    ("agent_scores", "identity_status", "TEXT NOT NULL DEFAULT 'missing'"),
    ("agent_scores", "indexed_at_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_scores", "stake_status", "TEXT NOT NULL DEFAULT 'missing'"),
    ("stakes", "stake_id", "TEXT NOT NULL DEFAULT ''"),
    ("endorsements", "target_stake", "TEXT NOT NULL DEFAULT ''"),
)


class IndexerStorage:
    """SQLite-backed storage for indexed reputation data."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate()

    def _migrate(self):
        """Add the columns of later schema versions to an existing file."""
        added_status = False
        for table, column, definition in _ADDED_COLUMNS:
            columns = {
                row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                added_status = added_status or column == "identity_status"
        if added_status:
            # The rule of scoring.compute_reputation_score: an agent that is
            # not Active has the tier Unverified.
            self.conn.execute(
                "UPDATE agent_scores SET tier = 'Unverified' WHERE identity_status != 'active'"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scores_identity ON agent_scores(identity_status)"
        )
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_scores (
                agent_did TEXT PRIMARY KEY,
                self_stake INTEGER NOT NULL DEFAULT 0,
                endorsement_total INTEGER NOT NULL DEFAULT 0,
                challenge_total INTEGER NOT NULL DEFAULT 0,
                history_bonus INTEGER NOT NULL DEFAULT 0,
                decay INTEGER NOT NULL DEFAULT 0,
                net_score INTEGER NOT NULL DEFAULT 0,
                tier TEXT NOT NULL DEFAULT 'Unverified',
                last_updated_slot INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS stakes (
                utxo_ref TEXT PRIMARY KEY,
                agent_did TEXT NOT NULL,
                owner_credential TEXT NOT NULL,
                stake_amount INTEGER NOT NULL,
                capabilities TEXT NOT NULL,  -- JSON array
                last_updated INTEGER NOT NULL,
                history_points INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS endorsements (
                utxo_ref TEXT PRIMARY KEY,
                endorser_did TEXT NOT NULL,
                target_did TEXT NOT NULL,
                stake_amount INTEGER NOT NULL,
                capabilities TEXT NOT NULL,  -- JSON array
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS challenges (
                utxo_ref TEXT PRIMARY KEY,
                challenger_did TEXT NOT NULL,
                target_did TEXT NOT NULL,
                capability TEXT NOT NULL,
                stake_amount INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'Open',
                outcome TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS history_bonuses (
                utxo_ref TEXT PRIMARY KEY,
                agent_did TEXT NOT NULL,
                source TEXT NOT NULL,
                bonus_points INTEGER NOT NULL DEFAULT 0,
                source_ref TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS indexer_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sybil_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_did TEXT NOT NULL,
                flag_type TEXT NOT NULL,
                severity REAL NOT NULL DEFAULT 0.0,
                details TEXT NOT NULL DEFAULT '',
                related_dids TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_sybil_agent ON sybil_flags(agent_did);
            CREATE INDEX IF NOT EXISTS idx_stakes_agent ON stakes(agent_did);
            CREATE INDEX IF NOT EXISTS idx_endorsements_target ON endorsements(target_did);
            CREATE INDEX IF NOT EXISTS idx_endorsements_endorser ON endorsements(endorser_did);
            CREATE INDEX IF NOT EXISTS idx_challenges_target ON challenges(target_did);
            CREATE INDEX IF NOT EXISTS idx_bonuses_agent ON history_bonuses(agent_did);
        """)
        self.conn.commit()

    # ── Agent Scores ────────────────────────────────────────────────────

    def upsert_score(
        self,
        agent_did: str,
        self_stake: int,
        endorsement_total: int,
        challenge_total: int,
        history_bonus: int,
        decay: int,
        net_score: int,
        tier: str,
        slot: int,
        *,
        identity_status: str,
        indexed_at_ms: int,
        stake_status: str = "missing",
    ):
        """Store the score of an agent. `slot` is the chain tip slot of the
        poll, `indexed_at_ms` its POSIX time, `identity_status` the
        effective status of the agent's identity record at that time and
        `stake_status` the status of its self-stake (missing, below_minimum
        or staked; scoring.stake_status)."""
        self.conn.execute(
            """INSERT INTO agent_scores
               (agent_did, self_stake, endorsement_total, challenge_total,
                history_bonus, decay, net_score, tier, last_updated_slot,
                identity_status, indexed_at_ms, stake_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_did) DO UPDATE SET
                 self_stake=excluded.self_stake,
                 endorsement_total=excluded.endorsement_total,
                 challenge_total=excluded.challenge_total,
                 history_bonus=excluded.history_bonus,
                 decay=excluded.decay,
                 net_score=excluded.net_score,
                 tier=excluded.tier,
                 last_updated_slot=excluded.last_updated_slot,
                 identity_status=excluded.identity_status,
                 indexed_at_ms=excluded.indexed_at_ms,
                 stake_status=excluded.stake_status
            """,
            (agent_did, self_stake, endorsement_total, challenge_total,
             history_bonus, decay, net_score, tier, slot,
             identity_status, indexed_at_ms, stake_status),
        )
        self.conn.commit()

    def get_score(self, agent_did: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM agent_scores WHERE agent_did = ?", (agent_did,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_scores(self, active_only: bool = False) -> list:
        """All scores, highest first. With `active_only`, only the agents
        whose identity record was Active at index time."""
        where = "WHERE identity_status = 'active' " if active_only else ""
        rows = self.conn.execute(
            f"SELECT * FROM agent_scores {where}ORDER BY net_score DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_leaderboard(self, limit: int = 50, include_all: bool = False) -> list:
        """The top agents by score. Agents whose identity record was not
        Active at index time are left out unless `include_all`."""
        where = "" if include_all else "WHERE identity_status = 'active' "
        rows = self.conn.execute(
            "SELECT agent_did, net_score, tier, identity_status FROM agent_scores "
            f"{where}ORDER BY net_score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stakes ──────────────────────────────────────────────────────────

    def upsert_stake(
        self, utxo_ref: str, agent_did: str, owner_credential: str,
        stake_amount: int, capabilities: list, last_updated: int,
        history_points: int, *, stake_id: str = "",
    ):
        """Store a stake UTxO. A stake with stake_amount 0 (fully decayed)
        is a stake like any other. `stake_id` is "tx#ix" of the seed of
        CreateStake."""
        self.conn.execute(
            """INSERT INTO stakes
               (utxo_ref, agent_did, owner_credential, stake_amount,
                capabilities, last_updated, history_points, stake_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(utxo_ref) DO UPDATE SET
                 stake_amount=excluded.stake_amount,
                 capabilities=excluded.capabilities,
                 last_updated=excluded.last_updated,
                 history_points=excluded.history_points,
                 stake_id=excluded.stake_id
            """,
            (utxo_ref, agent_did, owner_credential, stake_amount,
             json.dumps(capabilities), last_updated, history_points, stake_id),
        )
        self.conn.commit()

    def clear_stakes(self):
        self.conn.execute("DELETE FROM stakes")
        self.conn.commit()

    def get_stakes_for_agent(self, agent_did: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM stakes WHERE agent_did = ?", (agent_did,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Endorsements ────────────────────────────────────────────────────

    def upsert_endorsement(
        self, utxo_ref: str, endorser_did: str, target_did: str,
        stake_amount: int, capabilities: list, created_at: int,
        *, target_stake: str = "",
    ):
        """Store an endorsement UTxO. `target_stake` is the stake_id
        ("tx#ix") of the stake that the endorsement backs."""
        self.conn.execute(
            """INSERT INTO endorsements
               (utxo_ref, endorser_did, target_did, stake_amount,
                capabilities, created_at, target_stake)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(utxo_ref) DO UPDATE SET
                 stake_amount=excluded.stake_amount
            """,
            (utxo_ref, endorser_did, target_did, stake_amount,
             json.dumps(capabilities), created_at, target_stake),
        )
        self.conn.commit()

    def clear_endorsements(self):
        self.conn.execute("DELETE FROM endorsements")
        self.conn.commit()

    def get_endorsements_for_agent(self, agent_did: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM endorsements WHERE target_did = ?", (agent_did,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_endorsements_by_agent(self, agent_did: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM endorsements WHERE endorser_did = ?", (agent_did,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Challenges ──────────────────────────────────────────────────────

    def upsert_challenge(
        self, utxo_ref: str, challenger_did: str, target_did: str,
        capability: str, stake_amount: int, state: str,
        outcome: Optional[str], created_at: int,
    ):
        self.conn.execute(
            """INSERT INTO challenges
               (utxo_ref, challenger_did, target_did, capability,
                stake_amount, state, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(utxo_ref) DO UPDATE SET
                 state=excluded.state, outcome=excluded.outcome
            """,
            (utxo_ref, challenger_did, target_did, capability,
             stake_amount, state, outcome, created_at),
        )
        self.conn.commit()

    def clear_challenges(self):
        self.conn.execute("DELETE FROM challenges")
        self.conn.commit()

    def get_challenges_for_agent(self, agent_did: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM challenges WHERE target_did = ?", (agent_did,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── History Bonuses ─────────────────────────────────────────────────

    def upsert_history_bonus(
        self, utxo_ref: str, agent_did: str, source: str,
        bonus_points: int, source_ref: str, created_at: int,
    ):
        self.conn.execute(
            """INSERT INTO history_bonuses
               (utxo_ref, agent_did, source, bonus_points, source_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(utxo_ref) DO UPDATE SET
                 bonus_points=excluded.bonus_points
            """,
            (utxo_ref, agent_did, source, bonus_points, source_ref, created_at),
        )
        self.conn.commit()

    def clear_history_bonuses(self):
        self.conn.execute("DELETE FROM history_bonuses")
        self.conn.commit()

    def get_bonuses_for_agent(self, agent_did: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM history_bonuses WHERE agent_did = ?", (agent_did,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Indexer State ───────────────────────────────────────────────────

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO indexer_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM indexer_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # ── Sybil Flags ──────────────────────────────────────────────────────

    def clear_sybil_flags(self):
        self.conn.execute("DELETE FROM sybil_flags")
        self.conn.commit()

    def upsert_sybil_flag(
        self, agent_did: str, flag_type: str, severity: float,
        details: str, related_dids: str,
    ):
        self.conn.execute(
            "INSERT INTO sybil_flags (agent_did, flag_type, severity, details, related_dids) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_did, flag_type, severity, details, related_dids),
        )
        self.conn.commit()

    def get_sybil_flags(self, agent_did: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM sybil_flags WHERE agent_did = ?", (agent_did,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_sybil_flags(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM sybil_flags ORDER BY severity DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Aggregate Queries ──────────────────────────────────────────────────

    def get_decayable_agents(
        self, now_ms: int, activity_window: int = 180, epoch_length: int = 14_400_000
    ) -> list:
        """Find agents eligible for decay processing at `now_ms` (POSIX ms).

        ClaimDecayRefund needs at least one inactive epoch after the
        activity window: (now - last_updated) // epoch_length >
        activity_window, so last_updated <= now - (activity_window + 1) *
        epoch_length. It also needs a positive decay amount, so a fully
        decayed stake (stake_amount 0, which stays as a stake) is not
        eligible. Each stake of an agent is a row (`stake_id`).
        """
        cutoff_ms = now_ms - (activity_window + 1) * epoch_length

        rows = self.conn.execute(
            "SELECT s.agent_did, s.stake_id, s.stake_amount, s.last_updated, a.tier, "
            "a.net_score, a.identity_status "
            "FROM stakes s "
            "LEFT JOIN agent_scores a ON s.agent_did = a.agent_did "
            "WHERE s.last_updated <= ? AND s.stake_amount > 0",
            (cutoff_ms,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Aggregate statistics for AFI component."""
        total_agents = self.conn.execute(
            "SELECT COUNT(*) FROM agent_scores"
        ).fetchone()[0]
        total_staked = self.conn.execute(
            "SELECT COALESCE(SUM(stake_amount), 0) FROM stakes"
        ).fetchone()[0]
        total_endorsements = self.conn.execute(
            "SELECT COUNT(*) FROM endorsements"
        ).fetchone()[0]
        total_endorsement_value = self.conn.execute(
            "SELECT COALESCE(SUM(stake_amount), 0) FROM endorsements"
        ).fetchone()[0]
        active_challenges = self.conn.execute(
            "SELECT COUNT(*) FROM challenges WHERE state = 'Open'"
        ).fetchone()[0]
        total_challenges = self.conn.execute(
            "SELECT COUNT(*) FROM challenges"
        ).fetchone()[0]
        sybil_flagged = self.conn.execute(
            "SELECT COUNT(DISTINCT agent_did) FROM sybil_flags"
        ).fetchone()[0]

        tier_counts = {}
        for row in self.conn.execute(
            "SELECT tier, COUNT(*) as cnt FROM agent_scores GROUP BY tier"
        ).fetchall():
            tier_counts[row["tier"]] = row["cnt"]

        identity_counts = {}
        for row in self.conn.execute(
            "SELECT identity_status, COUNT(*) as cnt FROM agent_scores "
            "GROUP BY identity_status"
        ).fetchall():
            identity_counts[row["identity_status"]] = row["cnt"]

        stake_counts = {}
        for row in self.conn.execute(
            "SELECT stake_status, COUNT(*) as cnt FROM agent_scores "
            "GROUP BY stake_status"
        ).fetchall():
            stake_counts[row["stake_status"]] = row["cnt"]

        return {
            "total_agents": total_agents,
            "total_staked_dfm": total_staked,
            "total_staked_ap3x": total_staked // 1_000_000,
            "total_endorsements": total_endorsements,
            "total_endorsement_value_dfm": total_endorsement_value,
            "active_challenges": active_challenges,
            "total_challenges": total_challenges,
            "sybil_flagged_agents": sybil_flagged,
            "tier_distribution": tier_counts,
            "identity_status_distribution": identity_counts,
            "stake_status_distribution": stake_counts,
        }

    def get_all_endorsements(self) -> list:
        """Get all endorsements (for sybil analysis)."""
        rows = self.conn.execute(
            "SELECT endorser_did, target_did, stake_amount FROM endorsements"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
