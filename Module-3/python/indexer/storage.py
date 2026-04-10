"""
SQLite storage for the reputation indexer.

Stores agent scores, stakes, endorsements, challenges, and history bonuses.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = "reputation_index.db"


class IndexerStorage:
    """SQLite-backed storage for indexed reputation data."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

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
    ):
        self.conn.execute(
            """INSERT INTO agent_scores
               (agent_did, self_stake, endorsement_total, challenge_total,
                history_bonus, decay, net_score, tier, last_updated_slot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_did) DO UPDATE SET
                 self_stake=excluded.self_stake,
                 endorsement_total=excluded.endorsement_total,
                 challenge_total=excluded.challenge_total,
                 history_bonus=excluded.history_bonus,
                 decay=excluded.decay,
                 net_score=excluded.net_score,
                 tier=excluded.tier,
                 last_updated_slot=excluded.last_updated_slot
            """,
            (agent_did, self_stake, endorsement_total, challenge_total,
             history_bonus, decay, net_score, tier, slot),
        )
        self.conn.commit()

    def get_score(self, agent_did: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM agent_scores WHERE agent_did = ?", (agent_did,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_scores(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM agent_scores ORDER BY net_score DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_leaderboard(self, limit: int = 50) -> list:
        rows = self.conn.execute(
            "SELECT agent_did, net_score, tier FROM agent_scores "
            "ORDER BY net_score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stakes ──────────────────────────────────────────────────────────

    def upsert_stake(
        self, utxo_ref: str, agent_did: str, owner_credential: str,
        stake_amount: int, capabilities: list, last_updated: int,
        history_points: int,
    ):
        self.conn.execute(
            """INSERT INTO stakes
               (utxo_ref, agent_did, owner_credential, stake_amount,
                capabilities, last_updated, history_points)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(utxo_ref) DO UPDATE SET
                 stake_amount=excluded.stake_amount,
                 capabilities=excluded.capabilities,
                 last_updated=excluded.last_updated,
                 history_points=excluded.history_points
            """,
            (utxo_ref, agent_did, owner_credential, stake_amount,
             json.dumps(capabilities), last_updated, history_points),
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
    ):
        self.conn.execute(
            """INSERT INTO endorsements
               (utxo_ref, endorser_did, target_did, stake_amount,
                capabilities, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(utxo_ref) DO UPDATE SET
                 stake_amount=excluded.stake_amount
            """,
            (utxo_ref, endorser_did, target_did, stake_amount,
             json.dumps(capabilities), created_at),
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

    def close(self):
        self.conn.close()
