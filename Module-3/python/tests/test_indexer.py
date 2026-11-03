"""Offline tests of the indexer: identity status at index time, the identity
rule of scoring and the leaderboard, canonical DIDs in the output, the stake
fields (stake_id, challenge_locks), the deploy state check of the CLI, and
the SQLite migration of an old file."""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest
from pycardano import PaymentSigningKey

from indexer.indexer import ReputationIndexer, identity_statuses
from indexer.storage import SCHEMA_VERSION, IndexerStorage
from reputation_staking import vector_identity as vi
from reputation_staking.models import IdentityStatus
from reputation_staking.plutus_data import (
    RepChallengeOutcomeVerified,
    RepChallengeStateResolved,
)
from tests.chain_fixtures import (
    AP3X,
    LO,
    TIP_SLOT,
    challenge_token,
    challenge_utxo,
    deploy_state,
    endorsement_utxo,
    identity_record,
    key_hash,
    params_record,
    public_key,
    record_utxo,
    ref,
    stake_datum_of,
    stake_utxo,
)

IDENTITY_HASH = bytes.fromhex("1d" * 28)
CONTROLLER = public_key(PaymentSigningKey.generate())
OWNER = key_hash(PaymentSigningKey.generate())


def agent(n: int) -> bytes:
    return vi.identifier_from_seed(bytes([n]) * 32, 0)


ACTIVE_ID, SUSPENDED_ID, MISSING_ID, ORPHAN_ID, PARENT_ID, CHILD_ID = (agent(n) for n in range(1, 7))


def ogmios_item(utxo) -> dict:
    """The Ogmios JSON of a pycardano UTxO (queryLedgerState/utxo)."""
    value = {"ada": {"lovelace": utxo.output.amount.coin}}
    for policy, assets in (utxo.output.amount.multi_asset or {}).items():
        value[bytes(policy.payload).hex()] = {
            bytes(name.payload).hex(): quantity for name, quantity in assets.items()
        }
    item = {
        "transaction": {"id": bytes(utxo.input.transaction_id).hex()},
        "index": utxo.input.index,
        "address": str(utxo.output.address),
        "value": value,
    }
    if utxo.output.datum is not None:
        item["datum"] = bytes(utxo.output.datum.cbor).hex()
    return item


class OfflineIndexer(ReputationIndexer):
    """The indexer with Ogmios answers from a dict {address: [UTxO]}."""

    def __init__(self, chain: dict, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chain = chain

    def _ogmios_utxos(self, address: str) -> list:
        return [ogmios_item(u) for u in self.chain.get(address, [])]

    def _get_current_slot(self) -> int:
        return TIP_SLOT


def suspended_record():
    hold = vi.Hold(vi.Signer.controller_set(), 1, LO - 1000, LO + vi.DAY_MS)
    return identity_record(CONTROLLER, status=vi.Status.suspended([hold]))


def build_chain(state: dict) -> dict:
    chain: dict = {}

    def add(utxo):
        chain.setdefault(str(utxo.output.address), []).append(utxo)

    add(params_record())
    # Identity records: active, suspended, a subagent of a missing parent,
    # a parent with an active subagent. MISSING_ID has no record.
    add(record_utxo(ACTIVE_ID, identity_record(CONTROLLER)))
    add(record_utxo(SUSPENDED_ID, suspended_record()))
    add(record_utxo(ORPHAN_ID, identity_record(
        CONTROLLER, lineage=vi.Lineage(agent(99), 0, 1))))
    add(record_utxo(PARENT_ID, identity_record(CONTROLLER)))
    add(record_utxo(CHILD_ID, identity_record(
        CONTROLLER, lineage=vi.Lineage(PARENT_ID, 0, 1))))
    # Stakes of 600 AP3X (Trusted when Active)
    for agent_id in (ACTIVE_ID, SUSPENDED_ID, MISSING_ID, ORPHAN_ID, CHILD_ID):
        add(stake_utxo(agent_id, OWNER, amount=600 * AP3X, last_updated=LO - vi.DAY_MS))
    add(endorsement_utxo(CHILD_ID, ACTIVE_ID, OWNER, amount=50 * AP3X))
    add(challenge_utxo(CHILD_ID, ACTIVE_ID, OWNER, OWNER,
                       state=RepChallengeStateResolved(outcome=RepChallengeOutcomeVerified())))
    return chain


@pytest.fixture
def indexed(tmp_path):
    state = deploy_state(key_hash(PaymentSigningKey.generate()))
    storage = IndexerStorage(str(tmp_path / "index.db"))
    indexer = OfflineIndexer(build_chain(state), state, storage)
    count = indexer.poll_once()
    return indexer, storage, count


class TestIdentityStatusAtIndexTime:
    def test_poll_stores_effective_status(self, indexed):
        _, storage, count = indexed
        assert count == 5
        status = {aid.hex(): storage.get_score(aid.hex())["identity_status"]
                  for aid in (ACTIVE_ID, SUSPENDED_ID, MISSING_ID, ORPHAN_ID, CHILD_ID)}
        assert status == {
            ACTIVE_ID.hex(): "active",
            SUSPENDED_ID.hex(): "suspended",
            MISSING_ID.hex(): "missing",
            ORPHAN_ID.hex(): "missing",  # the parent record is not there
            CHILD_ID.hex(): "active",
        }

    def test_not_active_agents_are_unverified_with_amounts(self, indexed):
        _, storage, _ = indexed
        active = storage.get_score(ACTIVE_ID.hex())
        assert active["tier"] == "Trusted"
        for agent_id in (SUSPENDED_ID, MISSING_ID, ORPHAN_ID):
            score = storage.get_score(agent_id.hex())
            assert score["tier"] == "Unverified"
            assert score["self_stake"] == 600 * AP3X
            assert score["net_score"] == 600 * AP3X

    def test_leaderboard_leaves_out_not_active_agents(self, indexed):
        _, storage, _ = indexed
        listed = {row["agent_did"] for row in storage.get_leaderboard(50)}
        assert listed == {ACTIVE_ID.hex(), CHILD_ID.hex()}
        everyone = {row["agent_did"] for row in storage.get_leaderboard(50, include_all=True)}
        assert everyone == {a.hex() for a in (ACTIVE_ID, SUSPENDED_ID, MISSING_ID, ORPHAN_ID, CHILD_ID)}

    def test_poll_state_in_milliseconds(self, indexed):
        indexer, storage, _ = indexed
        assert storage.get_state("last_poll_ms") == str(LO)
        assert storage.get_state("last_poll_slot") == str(TIP_SLOT)
        assert storage.get_state("epoch_length") == "14400000"
        assert storage.get_state("identity_hash") == "1d" * 28
        assert storage.get_score(ACTIVE_ID.hex())["indexed_at_ms"] == LO
        # The params record values (not slot defaults)
        assert indexer.params.epoch_length == 14_400_000

    def test_api_output_has_canonical_dids(self, indexed, monkeypatch):
        from fastapi.testclient import TestClient
        import indexer.api as api_module

        _, storage, _ = indexed
        monkeypatch.setattr(api_module, "_storage", storage)
        client = TestClient(api_module.app)
        did = vi.did_from_id(ACTIVE_ID)
        data = client.get(f"/v1/reputation/agent/{did}").json()
        assert data["score"]["agent_did_uri"] == did
        assert data["score"]["identity_status"] == "active"
        [endorsement] = data["endorsements_received"]
        assert endorsement["endorser_did_uri"] == vi.did_from_id(CHILD_ID)
        [challenge] = data["challenges"]
        assert challenge["challenger_did_uri"] == vi.did_from_id(CHILD_ID)
        board = client.get("/v1/reputation/leaderboard").json()
        assert {row["agent_did_uri"] for row in board} == {
            vi.did_from_id(ACTIVE_ID), vi.did_from_id(CHILD_ID),
        }


class TestIdentityStatuses:
    def test_revoked_parent_epoch_and_retired(self):
        parent = identity_record(CONTROLLER, subagent_epoch=2)
        child = identity_record(CONTROLLER, lineage=vi.Lineage(PARENT_ID, 1, 1))
        retired = identity_record(CONTROLLER, status=vi.Status.retired(LO - 1))
        utxos = [
            record_utxo(PARENT_ID, parent),
            record_utxo(CHILD_ID, child),
            record_utxo(ACTIVE_ID, retired),
        ]
        statuses = identity_statuses(
            utxos, IDENTITY_HASH, [CHILD_ID.hex(), ACTIVE_ID.hex(), "not hex"], LO,
        )
        assert statuses == {
            CHILD_ID.hex(): IdentityStatus.REVOKED,
            ACTIVE_ID.hex(): IdentityStatus.RETIRED,
            "not hex": IdentityStatus.MISSING,
        }

    def test_record_at_another_script_does_not_count(self):
        record = record_utxo(ACTIVE_ID, identity_record(CONTROLLER))
        statuses = identity_statuses([record], bytes.fromhex("2e" * 28), [ACTIVE_ID.hex()], LO)
        assert statuses == {ACTIVE_ID.hex(): IdentityStatus.MISSING}


class TestStakeFields:
    def test_stake_rows_have_stake_id_and_challenge_locks(self, tmp_path, monkeypatch):
        """A second stake of ACTIVE_ID with the lock of a challenge: each
        stake row has its stake_id and its challenge locks (hex), in the
        storage and in the API."""
        from fastapi.testclient import TestClient
        import indexer.api as api_module

        state = deploy_state(key_hash(PaymentSigningKey.generate()))
        chain = build_chain(state)
        lock = challenge_token(CHILD_ID, ACTIVE_ID)
        locked = stake_utxo(ACTIVE_ID, OWNER, amount=50 * AP3X, challenge_locks=(lock,))
        chain[str(locked.output.address)].append(locked)
        storage = IndexerStorage(str(tmp_path / "index.db"))
        OfflineIndexer(chain, state, storage).poll_once()

        rows = {row["utxo_ref"]: row for row in storage.get_stakes_for_agent(ACTIVE_ID.hex())}
        assert len(rows) == 2
        stake_id = stake_datum_of(locked).stake_id
        assert rows[ref(locked)]["stake_id"] == f"{bytes(stake_id.transaction_id).hex()}#{stake_id.output_index}"
        assert json.loads(rows[ref(locked)]["challenge_locks"]) == [lock.hex()]

        monkeypatch.setattr(api_module, "_storage", storage)
        data = TestClient(api_module.app).get(f"/v1/reputation/agent/{vi.did_from_id(ACTIVE_ID)}").json()
        stakes = {s["utxo_ref"]: s for s in data["stakes"]}
        assert stakes[ref(locked)]["challenge_locks"] == [lock.hex()]
        assert stakes[ref(locked)]["stake_id"] == rows[ref(locked)]["stake_id"]
        [other] = [s for key, s in stakes.items() if key != ref(locked)]
        assert other["challenge_locks"] == []
        assert other["stake_id"]


class TestDeployStateCheck:
    def test_indexer_refuses_old_state(self, tmp_path):
        old = deploy_state(OWNER)
        del old["identity_hash"]
        old["agent_registry_hash"] = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
        storage = IndexerStorage(str(tmp_path / "index.db"))
        with pytest.raises(ValueError, match="agent_registry_hash"):
            ReputationIndexer(old, storage)

    def test_cli_refuses_old_state_file(self, tmp_path, monkeypatch, caplog):
        from indexer import __main__ as cli

        old = deploy_state(OWNER)
        del old["identity_hash"]
        old["agent_registry_hash"] = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
        path = tmp_path / "deploy_state.json"
        path.write_text(json.dumps(old))
        monkeypatch.setattr(sys, "argv", [
            "indexer", "--deploy-state", str(path), "--db", str(tmp_path / "x.db"), "--once",
        ])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "Move the file" in caplog.text
        assert "identity registry, Module-1, Module-3, Module-6" in caplog.text

    def test_cli_default_paths_are_the_deploy_script_files(self):
        from indexer import __main__ as cli

        assert cli.NETWORKS["testnet"]["deploy_state"] == "deploy/deploy_state.json"
        assert cli.NETWORKS["mainnet"]["deploy_state"] == "deploy/mainnet/deploy_state.json"


OLD_SCHEMA = """
CREATE TABLE agent_scores (
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
CREATE TABLE stakes (
    utxo_ref TEXT PRIMARY KEY,
    agent_did TEXT NOT NULL,
    owner_credential TEXT NOT NULL,
    stake_amount INTEGER NOT NULL,
    capabilities TEXT NOT NULL,
    last_updated INTEGER NOT NULL,
    history_points INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE indexer_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class TestSqliteMigration:
    def make_old_file(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO agent_scores VALUES (?, 2000000000, 0, 0, 0, 0, 2000000000, 'Elite', 123)",
            ("aa" * 32,),
        )
        conn.execute(
            "INSERT INTO stakes VALUES ('tx#0', ?, 'owner', 2000000000, '[\"code_review\"]', 5, 0)",
            ("aa" * 32,),
        )
        conn.execute("INSERT INTO indexer_state VALUES ('last_poll_slot', '123')")
        conn.commit()
        conn.close()

    def test_old_file_gets_new_columns_and_keeps_rows(self, tmp_path):
        path = str(tmp_path / "old.db")
        self.make_old_file(path)
        storage = IndexerStorage(path)
        assert storage.schema_version() == SCHEMA_VERSION
        columns = {r["name"] for r in storage.conn.execute("PRAGMA table_info(agent_scores)")}
        assert {"identity_status", "indexed_at_ms"} <= columns
        row = storage.get_score("aa" * 32)
        assert row["net_score"] == 2_000_000_000
        assert row["last_updated_slot"] == 123
        assert row["identity_status"] == "missing"
        # The identity rule holds for the old row until the next poll
        assert row["tier"] == "Unverified"
        assert storage.get_leaderboard(10) == []
        assert len(storage.get_stakes_for_agent("aa" * 32)) == 1
        assert storage.get_state("last_poll_slot") == "123"
        # New tables of the schema exist
        storage.upsert_endorsement("e#0", "bb" * 32, "aa" * 32, 5, ["code_review"], 1)
        storage.close()

    def test_migration_runs_once(self, tmp_path):
        path = str(tmp_path / "old.db")
        self.make_old_file(path)
        IndexerStorage(path).close()
        storage = IndexerStorage(path)
        storage.upsert_score(
            "aa" * 32, 1, 0, 0, 0, 0, 1, "Novice", 5,
            identity_status="active", indexed_at_ms=LO,
        )
        storage.close()
        reopened = IndexerStorage(path)
        row = reopened.get_score("aa" * 32)
        assert row["identity_status"] == "active" and row["tier"] == "Novice"
        assert [r["agent_did"] for r in reopened.get_leaderboard(10)] == ["aa" * 32]
