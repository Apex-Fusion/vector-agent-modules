"""
Reputation Indexer — polls chain UTXOs and computes reputation scores.

Scans the reputation and endorsement validator addresses for stakes,
endorsements, challenges, and history bonuses. Computes per-agent
reputation scores using the same formula as on-chain validators.

Supports both Ogmios (remote) and Docker (local) backends.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Optional

import cbor2

from reputation_staking.constants import (
    CHALLENGE_PREFIX,
    ENDORSEMENT_PREFIX,
    HISTORY_BONUS_PREFIX,
    OGMIOS_URL,
    STAKE_PREFIX,
)
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    HistoryBonusSource,
    ProtocolParamsInfo,
    RepChallengeState,
    StakeInfo,
)
from reputation_staking.scoring import compute_decay, compute_reputation_score, get_tier

from indexer.storage import IndexerStorage
from indexer.sybil import analyze_sybil

logger = logging.getLogger(__name__)

# State field indices within inline datums (cardano-cli JSON format)
# StakeDatum: fields[0]=agent_did, [1]=owner_credential, [2]=stake_amount,
#             [3]=capabilities, [4]=last_updated, [5]=history_points
# EndorsementDatum (wrapped): fields[0]=inner, inner.fields[0..5]
# ChallengeDatum (wrapped): fields[0]=inner, inner.fields[0..12]


def _hex_field(datum: dict, idx: int) -> str:
    """Extract a hex bytes field from a datum."""
    return datum["fields"][idx].get("bytes", "")


def _int_field(datum: dict, idx: int) -> int:
    """Extract an int field from a datum."""
    return datum["fields"][idx].get("int", 0)


def _parse_capabilities(datum: dict, idx: int) -> list:
    """Parse a capabilities list field from datum."""
    lst = datum["fields"][idx].get("list", [])
    return [bytes.fromhex(item["bytes"]).decode("utf-8", errors="replace") for item in lst]


def _parse_challenge_state(state_field: dict) -> tuple:
    """Parse challenge state from datum. Returns (state_name, outcome_name)."""
    constr = state_field.get("constructor", 0)
    if constr == 0:
        return "Open", None
    elif constr == 1:
        return "Escalated", None
    elif constr == 2:
        # Resolved(outcome)
        outcome_constr = state_field["fields"][0].get("constructor", 0)
        outcomes = {0: "CapabilityVerified", 1: "CapabilityFalsified", 2: "Inconclusive"}
        return "Resolved", outcomes.get(outcome_constr, f"Unknown({outcome_constr})")
    return f"Unknown({constr})", None


def _parse_history_bonus_source(source_field: dict) -> str:
    """Parse history bonus source variant."""
    sources = {
        0: "ChallengeWon", 1: "ChallengeLost", 2: "ChallengeInconclusive",
        3: "EndorsementReceived", 4: "EndorsementGiven", 5: "StakeIncreased",
        6: "CapabilityAdded", 7: "LongTermStaking", 8: "GenesisBonus",
    }
    return sources.get(source_field.get("constructor", -1), "Unknown")


class ReputationIndexer:
    """Polls chain UTXOs and computes reputation scores.

    Supports both Ogmios (remote) and Docker/cardano-cli (local) backends.
    The default is Ogmios, matching Module 1 and Module 6.
    """

    def __init__(
        self,
        deploy_state: dict,
        storage: IndexerStorage,
        poll_interval: int = 60,
        ogmios_url: str = OGMIOS_URL,
        backend=None,
    ):
        self.storage = storage
        self.poll_interval = poll_interval
        self.ogmios_url = ogmios_url
        self._legacy_backend = backend  # Optional: DockerChainBackend for local

        self.reputation_hash = deploy_state["reputation_validator_hash"]
        self.endorsement_hash = deploy_state["endorsement_validator_hash"]
        self.reputation_addr = deploy_state["reputation_address"]
        self.endorsement_addr = deploy_state["endorsement_address"]

        self.params = ProtocolParamsInfo()  # defaults match testnet

    def _query_utxos(self, address: str) -> dict:
        """Query UTxOs — uses Ogmios by default, falls back to legacy backend."""
        if self._legacy_backend:
            return self._legacy_backend.query_utxos(address)
        # Use Ogmios and convert to cardano-cli JSON format for the parsers
        from reputation_staking.ogmios_backend import ogmios_rpc
        result = ogmios_rpc(
            "queryLedgerState/utxo",
            params={"addresses": [address]},
            ogmios_url=self.ogmios_url,
        )
        return self._ogmios_to_cli_format(result)

    def _get_current_slot(self) -> int:
        if self._legacy_backend:
            return self._legacy_backend.get_current_slot()
        from reputation_staking.ogmios_backend import get_current_slot
        return get_current_slot(self.ogmios_url)

    @staticmethod
    def _ogmios_to_cli_format(ogmios_utxos: list) -> dict:
        """Convert Ogmios UTxO list to cardano-cli JSON format.

        The datum/token parsers expect the cardano-cli format:
        { "txhash#idx": { "value": {"lovelace": N, "policy": {"token": N}},
                          "inlineDatum": {...} } }
        """
        result = {}
        for item in ogmios_utxos:
            tx_hash = item["transaction"]["id"]
            idx = item["index"]
            key = f"{tx_hash}#{idx}"

            # Build value
            value = {}
            for policy_hex, assets in item.get("value", {}).items():
                if policy_hex == "ada":
                    value["lovelace"] = assets.get("lovelace", 0)
                else:
                    value[policy_hex] = assets

            # Inline datum — Ogmios returns it as CBOR hex, we need ScriptData JSON
            # For now, pass through the datum field if it's already JSON
            # (the parsers handle both formats)
            datum = item.get("datum")
            if isinstance(datum, str):
                # CBOR hex — decode to JSON
                import cbor2
                try:
                    raw = cbor2.loads(bytes.fromhex(datum))
                    datum = ReputationIndexer._cbor_to_script_data(raw)
                except Exception:
                    datum = None

            entry = {"value": value}
            if datum:
                entry["inlineDatum"] = datum
            result[key] = entry
        return result

    @staticmethod
    def _cbor_to_script_data(obj):
        """Convert a decoded CBOR object to cardano-cli ScriptData JSON."""
        if isinstance(obj, cbor2.CBORTag):
            # CBOR tags 121-127 map to constructors 0-6
            # Tags 1280-... map to constructors 7+
            tag = obj.tag
            if 121 <= tag <= 127:
                constructor = tag - 121
            elif tag >= 1280:
                constructor = tag - 1280 + 7
            else:
                constructor = 0
            fields = obj.value if isinstance(obj.value, list) else [obj.value]
            return {
                "constructor": constructor,
                "fields": [ReputationIndexer._cbor_to_script_data(f) for f in fields],
            }
        elif isinstance(obj, int):
            return {"int": obj}
        elif isinstance(obj, bytes):
            return {"bytes": obj.hex()}
        elif isinstance(obj, str):
            return {"bytes": obj.encode().hex()}
        elif isinstance(obj, list):
            return {"list": [ReputationIndexer._cbor_to_script_data(item) for item in obj]}
        elif isinstance(obj, dict):
            return {"map": [
                {"k": ReputationIndexer._cbor_to_script_data(k),
                 "v": ReputationIndexer._cbor_to_script_data(v)}
                for k, v in obj.items()
            ]}
        return {"int": 0}

    def poll_once(self) -> int:
        """Run a single indexing pass. Returns number of agents indexed."""
        current_slot = self._get_current_slot()
        logger.info("Indexing at slot %d", current_slot)

        # Clear previous data (full rescan each poll)
        self.storage.clear_stakes()
        self.storage.clear_endorsements()
        self.storage.clear_challenges()
        self.storage.clear_history_bonuses()

        # Scan reputation address (stakes + history bonuses)
        rep_utxos = self._query_utxos(self.reputation_addr)
        self._index_reputation_utxos(rep_utxos)

        # Scan endorsement address (endorsements + challenges)
        end_utxos = self._query_utxos(self.endorsement_addr)
        self._index_endorsement_utxos(end_utxos)

        # Compute scores for all agents
        agents = self._collect_agent_dids()
        for agent_did in agents:
            self._compute_agent_score(agent_did, current_slot)

        # Run sybil detection
        sybil_flags = analyze_sybil(self.storage)

        self.storage.set_state("last_poll_slot", str(current_slot))
        self.storage.set_state("last_poll_time", str(int(time.time())))
        self.storage.set_state("agents_indexed", str(len(agents)))
        self.storage.set_state("sybil_flags", str(len(sybil_flags)))

        logger.info(
            "Indexed %d agents at slot %d (%d sybil flags)",
            len(agents), current_slot, len(sybil_flags),
        )
        return len(agents)

    def run(self):
        """Run the indexer polling loop."""
        logger.info("Starting indexer (poll every %ds)", self.poll_interval)
        while True:
            try:
                self.poll_once()
            except Exception:
                logger.exception("Indexer poll failed")
            time.sleep(self.poll_interval)

    # ── UTXO Parsing ────────────────────────────────────────────────────

    def _index_reputation_utxos(self, utxos: dict):
        """Parse stakes and history bonuses from reputation address UTXOs."""
        for utxo_ref, info in utxos.items():
            datum = info.get("inlineDatum")
            if not datum or not isinstance(datum, dict):
                continue

            # Identify by token prefix in the UTXO value
            for policy_id, assets in info["value"].items():
                if policy_id != self.reputation_hash or not isinstance(assets, dict):
                    continue
                for token_name_hex in assets:
                    token_bytes = bytes.fromhex(token_name_hex)
                    if token_bytes.startswith(STAKE_PREFIX):
                        self._parse_stake(utxo_ref, datum)
                    elif token_bytes.startswith(HISTORY_BONUS_PREFIX):
                        self._parse_history_bonus(utxo_ref, datum)

    def _index_endorsement_utxos(self, utxos: dict):
        """Parse endorsements and challenges from endorsement address UTXOs."""
        for utxo_ref, info in utxos.items():
            datum = info.get("inlineDatum")
            if not datum or not isinstance(datum, dict):
                continue

            # Identify by token prefix
            for policy_id, assets in info["value"].items():
                if policy_id != self.endorsement_hash or not isinstance(assets, dict):
                    continue
                for token_name_hex in assets:
                    token_bytes = bytes.fromhex(token_name_hex)
                    if token_bytes.startswith(ENDORSEMENT_PREFIX):
                        self._parse_endorsement(utxo_ref, datum)
                    elif token_bytes.startswith(CHALLENGE_PREFIX):
                        self._parse_challenge(utxo_ref, datum)

    def _parse_stake(self, utxo_ref: str, datum: dict):
        """Parse a StakeDatum from inline datum."""
        try:
            fields = datum.get("fields", [])
            if len(fields) < 6:
                return
            agent_did = _hex_field(datum, 0)
            owner = _hex_field(datum["fields"][1], 0)  # inside Constr
            stake_amount = _int_field(datum, 2)
            capabilities = _parse_capabilities(datum, 3)
            last_updated = _int_field(datum, 4)
            history_points = _int_field(datum, 5)

            self.storage.upsert_stake(
                utxo_ref, agent_did, owner, stake_amount,
                capabilities, last_updated, history_points,
            )
        except (KeyError, IndexError, TypeError):
            logger.debug("Failed to parse stake datum at %s", utxo_ref)

    def _parse_endorsement(self, utxo_ref: str, datum: dict):
        """Parse an EndorsementValidatorDatum.Endorsement (nested Constr)."""
        try:
            # Outer: Constr(0, [inner]) where inner has 6 fields
            if datum.get("constructor") != 0:
                return
            inner = datum["fields"][0]
            endorser_did = _hex_field(inner, 0)
            target_did = _hex_field(inner, 2)
            stake_amount = _int_field(inner, 3)
            capabilities = _parse_capabilities(inner, 4)
            created_at = _int_field(inner, 5)

            self.storage.upsert_endorsement(
                utxo_ref, endorser_did, target_did, stake_amount,
                capabilities, created_at,
            )
        except (KeyError, IndexError, TypeError):
            logger.debug("Failed to parse endorsement datum at %s", utxo_ref)

    def _parse_challenge(self, utxo_ref: str, datum: dict):
        """Parse an EndorsementValidatorDatum.Challenge (nested Constr)."""
        try:
            # Outer: Constr(1, [inner]) where inner has 13 fields
            if datum.get("constructor") != 1:
                return
            inner = datum["fields"][0]
            challenger_did = _hex_field(inner, 0)
            target_did = _hex_field(inner, 2)
            capability_hex = _hex_field(inner, 4)
            capability = bytes.fromhex(capability_hex).decode("utf-8", errors="replace")
            stake_amount = _int_field(inner, 5)
            created_at = _int_field(inner, 8)
            state_name, outcome = _parse_challenge_state(inner["fields"][12])

            self.storage.upsert_challenge(
                utxo_ref, challenger_did, target_did, capability,
                stake_amount, state_name, outcome, created_at,
            )
        except (KeyError, IndexError, TypeError):
            logger.debug("Failed to parse challenge datum at %s", utxo_ref)

    def _parse_history_bonus(self, utxo_ref: str, datum: dict):
        """Parse a HistoryBonusDatum."""
        try:
            fields = datum.get("fields", [])
            if len(fields) < 5:
                return
            agent_did = _hex_field(datum, 0)
            source = _parse_history_bonus_source(fields[1])
            bonus_points = _int_field(datum, 2)
            # source_ref is an OutputReference
            source_ref_fields = fields[3].get("fields", [])
            source_ref = f"{_hex_field(fields[3], 0)}#{_int_field(fields[3], 1)}"
            created_at = _int_field(datum, 4)

            self.storage.upsert_history_bonus(
                utxo_ref, agent_did, source, bonus_points, source_ref, created_at,
            )
        except (KeyError, IndexError, TypeError):
            logger.debug("Failed to parse history bonus datum at %s", utxo_ref)

    # ── Score Computation ───────────────────────────────────────────────

    def _collect_agent_dids(self) -> set:
        """Collect all agent DIDs from indexed data."""
        dids = set()
        for row in self.storage.conn.execute("SELECT DISTINCT agent_did FROM stakes").fetchall():
            dids.add(row["agent_did"])
        for row in self.storage.conn.execute("SELECT DISTINCT target_did FROM endorsements").fetchall():
            dids.add(row["target_did"])
        for row in self.storage.conn.execute("SELECT DISTINCT target_did FROM challenges").fetchall():
            dids.add(row["target_did"])
        for row in self.storage.conn.execute("SELECT DISTINCT agent_did FROM history_bonuses").fetchall():
            dids.add(row["agent_did"])
        return dids

    def _compute_agent_score(self, agent_did: str, current_slot: int):
        """Compute and store reputation score for an agent."""
        # Build StakeInfo from DB rows (aggregate if multiple, though normally 1)
        stakes_rows = self.storage.get_stakes_for_agent(agent_did)
        if stakes_rows:
            total_stake = sum(s["stake_amount"] for s in stakes_rows)
            last_updated = max(s["last_updated"] for s in stakes_rows)
            history_pts = sum(s["history_points"] for s in stakes_rows)
            caps = json.loads(stakes_rows[0]["capabilities"]) if stakes_rows[0]["capabilities"] else []
            stake = StakeInfo(
                agent_did=agent_did,
                owner=stakes_rows[0]["owner_credential"],
                stake_amount=total_stake,
                staked_capabilities=caps,
                last_updated=last_updated,
                history_points=history_pts,
            )
        else:
            # Agent has no stake — build a zero-stake placeholder
            stake = StakeInfo(
                agent_did=agent_did,
                owner="",
                stake_amount=0,
                staked_capabilities=[],
                last_updated=0,
                history_points=0,
            )

        # Build EndorsementInfo list
        endorsements_rows = self.storage.get_endorsements_for_agent(agent_did)
        endorsements = [
            EndorsementInfo(
                endorser_did=e["endorser_did"],
                target_did=e["target_did"],
                stake_amount=e["stake_amount"],
                endorsed_capabilities=json.loads(e["capabilities"]) if e["capabilities"] else [],
                created_at=e["created_at"],
                utxo_ref=e["utxo_ref"],
            )
            for e in endorsements_rows
        ]

        # Build ChallengeInfo list
        challenges_rows = self.storage.get_challenges_for_agent(agent_did)
        challenges = [
            ChallengeInfo(
                challenger_did=c["challenger_did"],
                target_did=c["target_did"],
                challenged_capability=c["capability"],
                stake_amount=c["stake_amount"],
                evidence_hash="",
                evidence_uri="",
                created_at=c["created_at"],
                state=RepChallengeState[c["state"]] if c["state"] in RepChallengeState.__members__ else RepChallengeState.Open,
            )
            for c in challenges_rows
        ]

        # Build HistoryBonusInfo list
        bonuses_rows = self.storage.get_bonuses_for_agent(agent_did)
        history_bonuses = [
            HistoryBonusInfo(
                agent_did=b["agent_did"],
                source=HistoryBonusSource[b["source"]] if b["source"] in HistoryBonusSource.__members__ else HistoryBonusSource.ChallengeWon,
                bonus_points=b["bonus_points"],
                source_ref=b["source_ref"],
                created_at=b["created_at"],
                utxo_ref=b["utxo_ref"],
            )
            for b in bonuses_rows
        ]

        # Compute score using SDK scoring module
        score = compute_reputation_score(
            stake=stake,
            endorsements=endorsements,
            challenges=challenges,
            history_bonuses=history_bonuses,
            params=self.params,
            current_slot=current_slot,
        )

        self.storage.upsert_score(
            agent_did=agent_did,
            self_stake=score.self_stake,
            endorsement_total=score.endorsement_total,
            challenge_total=score.challenge_total,
            history_bonus=score.history_bonus,
            decay=score.decay,
            net_score=score.net_score,
            tier=score.tier.name,
            slot=current_slot,
        )
