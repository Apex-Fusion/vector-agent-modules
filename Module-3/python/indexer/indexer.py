"""
Reputation Indexer — polls chain UTXOs and computes reputation scores.

Scans the reputation and endorsement validator addresses for stakes,
endorsements, challenges, and history bonuses. Computes per-agent
reputation scores using the same formula as on-chain validators.

Each poll also reads:
  - the ProtocolParams record (the output at the params holder with the
    `params` record token), for the decay and epoch values;
  - the identity records at the identity address (identity_hash of the
    deployment). For each indexed agent the indexer stores the effective
    status of its record at the index time (`vector_identity.effective`
    with lo = hi = the index time, ancestors from `record_chain`), or
    'missing' when the record or an ancestor is not at the identity address.

Identity rule (decision, see scoring.py): an agent that is not Active gets
the tier Unverified; its amounts stay. The leaderboard leaves it out unless
the caller asks for all agents.

Stake rule (see scoring.py): no action burns a stake token, so a fully
decayed stake stays with stake_amount 0. The indexer keeps it as a stake
(stake_status below_minimum), not as a missing stake. An agent can have more
than one stake; stake_id (the seed output reference of CreateStake) tells
them apart, and each endorsement names the stake that it backs
(target_stake). A stake row also has challenge_locks: the challenge token
names of the open challenges that lock the stake.

The indexer reads the chain through Ogmios.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, Iterable, List, Mapping, Union

import cbor2
from pycardano import AssetName, ScriptHash

from reputation_staking import vector_identity as vi
from reputation_staking.constants import (
    CHALLENGE_PREFIX,
    ENDORSEMENT_PREFIX,
    HISTORY_BONUS_PREFIX,
    OGMIOS_URL,
    PARAMS_TOKEN_NAME,
    STAKE_PREFIX,
)
from reputation_staking.deployment import Deployment, load_deployment
from reputation_staking.models import (
    ChallengeInfo,
    EndorsementInfo,
    HistoryBonusInfo,
    HistoryBonusSource,
    IdentityStatus,
    ProtocolParamsInfo,
    RepChallengeState,
    StakeInfo,
)
from reputation_staking.ogmios_backend import parse_ogmios_utxo
from reputation_staking.scoring import compute_reputation_score
from reputation_staking.utils import slot_to_posix_ms

from indexer.storage import IndexerStorage
from indexer.sybil import analyze_sybil

logger = logging.getLogger(__name__)

# State field indices within inline datums (cardano-cli JSON format)
# StakeDatum (8 fields): fields[0]=agent_did, [1]=owner_credential,
#             [2]=stake_amount, [3]=capabilities, [4]=last_updated,
#             [5]=history_points, [6]=challenge_locks (hex token names),
#             [7]=stake_id (OutputReference)
# EndorsementDatum (wrapped): fields[0]=inner, inner.fields[0..7]
#             ([6]=slashed_by, not indexed; [7]=target_stake)
# ChallengeDatum (wrapped): fields[0]=inner, inner.fields[0..12]


def _hex_field(datum: dict, idx: int) -> str:
    """Extract a hex bytes field from a datum."""
    return datum["fields"][idx].get("bytes", "")


def _int_field(datum: dict, idx: int) -> int:
    """Extract an int field from a datum."""
    return datum["fields"][idx].get("int", 0)


def _out_ref_field(datum: dict, idx: int) -> str:
    """An OutputReference field as "tx_hash#index"."""
    ref = datum["fields"][idx]
    return f"{_hex_field(ref, 0)}#{_int_field(ref, 1)}"


def _parse_capabilities(datum: dict, idx: int) -> list:
    """Parse a capabilities list field from datum."""
    lst = datum["fields"][idx].get("list", [])
    return [bytes.fromhex(item["bytes"]).decode("utf-8", errors="replace") for item in lst]


def _hex_list(datum: dict, idx: int) -> list:
    """A list of byte strings field as hex strings."""
    return [item["bytes"] for item in datum["fields"][idx].get("list", [])]


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
    """Parse history bonus source variant (HistoryBonusSource constructors)."""
    try:
        return HistoryBonusSource(source_field.get("constructor", -1)).name
    except ValueError:
        return "Unknown"


def identity_statuses(
    utxos: Iterable,
    identity_hash: bytes,
    agent_ids: Iterable[str],
    now_ms: int,
) -> Dict[str, IdentityStatus]:
    """The effective status of each agent's identity record at `now_ms`.

    `utxos` are the pycardano UTxOs at the identity address. A record counts
    only when `vector_identity.record_from_utxo` accepts it. An agent whose
    record or ancestor record is missing gets MISSING.
    """
    records: Dict[bytes, vi.IdentityRecord] = {}
    for utxo in utxos:
        try:
            record = vi.record_from_utxo(utxo, identity_hash)
        except vi.IdentityDecodeError as exc:
            logger.warning("Identity output with a datum that does not decode: %s", exc)
            continue
        if record is not None:
            records[record.agent_id] = record

    statuses: Dict[str, IdentityStatus] = {}
    for agent_hex in agent_ids:
        try:
            chain = vi.record_chain(agent_hex, records.get)
            status = vi.effective_of_chain(chain, now_ms, now_ms)
            statuses[agent_hex] = IdentityStatus(status.value)
        except (vi.IdentityError, TypeError, ValueError):
            statuses[agent_hex] = IdentityStatus.MISSING
    return statuses


class ReputationIndexer:
    """Polls chain UTXOs and computes reputation scores (Ogmios)."""

    def __init__(
        self,
        deploy_state: Union[Mapping, Deployment],
        storage: IndexerStorage,
        poll_interval: int = 60,
        ogmios_url: str = OGMIOS_URL,
    ):
        self.storage = storage
        self.poll_interval = poll_interval
        self.ogmios_url = ogmios_url

        self.deployment = (
            deploy_state if isinstance(deploy_state, Deployment)
            else load_deployment(deploy_state)
        )
        d = self.deployment
        self.reputation_hash = d.reputation_validator_hash
        self.endorsement_hash = d.endorsement_validator_hash
        self.reputation_addr = d.reputation_address
        self.endorsement_addr = d.endorsement_address
        self.identity_hash = bytes.fromhex(d.identity_hash)
        self.identity_addr = d.identity_address

        self.params = ProtocolParamsInfo()

    # ── Chain reads ─────────────────────────────────────────────────────

    def _ogmios_utxos(self, address: str) -> list:
        """The UTxOs at an address as Ogmios JSON items."""
        from reputation_staking.ogmios_backend import ogmios_rpc
        return ogmios_rpc(
            "queryLedgerState/utxo",
            params={"addresses": [address]},
            ogmios_url=self.ogmios_url,
        )

    def _query_utxos(self, address: str) -> dict:
        """The UTxOs at an address in cardano-cli JSON format."""
        return self._ogmios_to_cli_format(self._ogmios_utxos(address))

    def _pycardano_utxos(self, address: str) -> list:
        return [parse_ogmios_utxo(item) for item in self._ogmios_utxos(address)]

    def _get_current_slot(self) -> int:
        from reputation_staking.ogmios_backend import get_current_slot
        return get_current_slot(self.ogmios_url)

    def _load_params(self) -> ProtocolParamsInfo:
        """The values of the ProtocolParams record of the deployment."""
        policy = ScriptHash(bytes.fromhex(self.deployment.params_token_policy))
        name = AssetName(PARAMS_TOKEN_NAME)
        for utxo in self._pycardano_utxos(self.deployment.params_holder_address):
            multi_asset = utxo.output.amount.multi_asset
            if (
                multi_asset and policy in multi_asset
                and multi_asset[policy].get(name, 0) == 1
                and utxo.output.datum is not None
            ):
                return ProtocolParamsInfo.from_datum(utxo.output.datum)
        raise RuntimeError(
            f"no ProtocolParams record at {self.deployment.params_holder_address} with "
            f"the params token of policy {self.deployment.params_token_policy}"
        )

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

            # Inline datum — Ogmios returns it as CBOR hex
            datum = item.get("datum")
            if isinstance(datum, str):
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

    # ── Poll ────────────────────────────────────────────────────────────

    def poll_once(self) -> int:
        """Run a single indexing pass. Returns number of agents indexed."""
        current_slot = self._get_current_slot()
        now_ms = slot_to_posix_ms(current_slot)
        logger.info("Indexing at slot %d (%d ms)", current_slot, now_ms)

        self.params = self._load_params()

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

        # Identity records of the indexed agents
        agents = self._collect_agent_dids()
        statuses = identity_statuses(
            self._pycardano_utxos(self.identity_addr), self.identity_hash, agents, now_ms,
        )

        # Compute scores for all agents
        for agent_did in agents:
            self._compute_agent_score(agent_did, current_slot, now_ms, statuses[agent_did])

        # Run sybil detection
        sybil_flags = analyze_sybil(self.storage)

        self.storage.set_state("last_poll_slot", str(current_slot))
        self.storage.set_state("last_poll_ms", str(now_ms))
        self.storage.set_state("last_poll_time", str(int(time.time())))
        self.storage.set_state("agents_indexed", str(len(agents)))
        self.storage.set_state("sybil_flags", str(len(sybil_flags)))
        self.storage.set_state("activity_window", str(self.params.activity_window))
        self.storage.set_state("epoch_length", str(self.params.epoch_length))
        self.storage.set_state("identity_hash", self.deployment.identity_hash)

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
        """Parse a StakeDatum (8 fields) from inline datum. A stake with
        stake_amount 0 (fully decayed) is a stake like any other."""
        try:
            fields = datum.get("fields", [])
            if len(fields) < 8:
                return
            agent_did = _hex_field(datum, 0)
            owner = _hex_field(datum["fields"][1], 0)  # inside Constr
            stake_amount = _int_field(datum, 2)
            capabilities = _parse_capabilities(datum, 3)
            last_updated = _int_field(datum, 4)
            history_points = _int_field(datum, 5)
            challenge_locks = _hex_list(datum, 6)
            stake_id = _out_ref_field(datum, 7)

            self.storage.upsert_stake(
                utxo_ref, agent_did, owner, stake_amount,
                capabilities, last_updated, history_points, stake_id=stake_id,
                challenge_locks=challenge_locks,
            )
        except (KeyError, IndexError, TypeError):
            logger.debug("Failed to parse stake datum at %s", utxo_ref)

    def _parse_endorsement(self, utxo_ref: str, datum: dict):
        """Parse an EndorsementValidatorDatum.Endorsement (nested Constr)."""
        try:
            # Outer: Constr(0, [inner]) where inner has 8 fields
            if datum.get("constructor") != 0:
                return
            inner = datum["fields"][0]
            if len(inner.get("fields", [])) < 8:
                return
            endorser_did = _hex_field(inner, 0)
            target_did = _hex_field(inner, 2)
            stake_amount = _int_field(inner, 3)
            capabilities = _parse_capabilities(inner, 4)
            created_at = _int_field(inner, 5)
            target_stake = _out_ref_field(inner, 7)

            self.storage.upsert_endorsement(
                utxo_ref, endorser_did, target_did, stake_amount,
                capabilities, created_at, target_stake=target_stake,
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
            source_ref = f"{_hex_field(fields[3], 0)}#{_int_field(fields[3], 1)}"
            created_at = _int_field(datum, 4)

            self.storage.upsert_history_bonus(
                utxo_ref, agent_did, source, bonus_points, source_ref, created_at,
            )
        except (KeyError, IndexError, TypeError):
            logger.debug("Failed to parse history bonus datum at %s", utxo_ref)

    # ── Score Computation ───────────────────────────────────────────────

    def _collect_agent_dids(self) -> List[str]:
        """Collect all agent identifiers (64 hex) from indexed data."""
        dids = set()
        for row in self.storage.conn.execute("SELECT DISTINCT agent_did FROM stakes").fetchall():
            dids.add(row["agent_did"])
        for row in self.storage.conn.execute("SELECT DISTINCT target_did FROM endorsements").fetchall():
            dids.add(row["target_did"])
        for row in self.storage.conn.execute("SELECT DISTINCT target_did FROM challenges").fetchall():
            dids.add(row["target_did"])
        for row in self.storage.conn.execute("SELECT DISTINCT agent_did FROM history_bonuses").fetchall():
            dids.add(row["agent_did"])
        return sorted(dids)

    def _compute_agent_score(
        self,
        agent_did: str,
        current_slot: int,
        now_ms: int,
        identity_status: IdentityStatus,
    ):
        """Compute and store reputation score for an agent."""
        # Build StakeInfo from the agent's stake rows. An agent can have more
        # than one stake (each with its own stake_id): the amounts add up. A
        # stake with stake_amount 0 (fully decayed) is a stake below the
        # minimum, not a missing stake.
        stakes_rows = self.storage.get_stakes_for_agent(agent_did)
        stake = None
        if stakes_rows:
            caps: List[str] = []
            for row in stakes_rows:
                for cap in json.loads(row["capabilities"]) if row["capabilities"] else []:
                    if cap not in caps:
                        caps.append(cap)
            single = stakes_rows[0] if len(stakes_rows) == 1 else None
            stake = StakeInfo(
                agent_did=agent_did,
                owner=stakes_rows[0]["owner_credential"],
                stake_amount=sum(s["stake_amount"] for s in stakes_rows),
                staked_capabilities=caps,
                last_updated=max(s["last_updated"] for s in stakes_rows),
                history_points=sum(s["history_points"] for s in stakes_rows),
                utxo_ref=single["utxo_ref"] if single else None,
                stake_id=single["stake_id"] if single else None,
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
                target_stake=e["target_stake"] or None,
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
            now_ms=now_ms,
            identity_status=identity_status,
            agent_did=agent_did,
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
            identity_status=score.identity_status.value,
            indexed_at_ms=now_ms,
            stake_status=score.stake_status.value,
        )
