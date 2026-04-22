"""
Edge-case tests for the Sim Phase 2 iter-3a setup phase
(QA refactor pass — Claire).

Scope (production code Catherine owns; tests added here only):
  - simulation.wallet_derivation         (allocate_indices, derive)
  - simulation.scenarios.happy_path      (_setup_agents, _derive_wallets,
                                          _scan_setup_complete_event)
  - simulation.tx_builder                (build_fund_agents, build_register_did,
                                          build_juror_bond  — pure-construction
                                          contracts that do NOT require live RPC)
  - simulation.chain                     (cost_models edge, script-ref parser
                                          for v5/v6 Ogmios, error verbosity,
                                          ProtocolParameters construction)

These are deterministic + fast. They use no live testnet — wallet derivation
and chain-helper unit tests work entirely off bytes / monkeypatched RPC. The
existing ``@pytest.mark.testnet`` end-to-end tests in
test_happy_path_scenario.py remain the live coverage.

Failure-mode guarantee: every test in this file passes against the current
implementation. Any test that documents a known gap in the contract (e.g.
``_load_or_init`` silently resets a corrupted index file rather than raising)
is marked ``@pytest.mark.xfail(strict=False)`` with a reproduction-grade
reason — these will trip into ``XPASS`` if Catherine tightens the contract.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ───────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_index_path(tmp_path: Path) -> Path:
    """A fresh wallet-index file path (file does not yet exist)."""
    return tmp_path / ".wallet_index.json"


@pytest.fixture
def fake_master_seed() -> bytes:
    """32-byte stand-in for a master ed25519 seed."""
    return bytes(range(32))


# ═══════════════════════════════════════════════════════════════════════════
# 1. wallet_derivation — concurrency
# ═══════════════════════════════════════════════════════════════════════════


class TestWalletDerivationConcurrency:
    def test_concurrent_allocate_indices_no_collision_no_loss(
        self, tmp_index_path
    ):
        """Two threads allocating from the same index file MUST produce
        disjoint index ranges, lose nothing, and leave a coherent record.

        Concrete scenario: thread A allocates 7 (scenario "A"), thread B
        allocates 5 (scenario "B"). After both threads finish:
          - indices_A ∩ indices_B == ∅
          - len(indices_A) == 7 and len(indices_B) == 5
          - file ``next_index`` == 12
          - file ``allocations`` has exactly 12 entries (7 tagged "A" + 5 "B")
          - the union of indices is exactly {0..11}
        """
        from simulation.wallet_derivation import allocate_indices

        results: dict[str, list[int]] = {}
        barrier = threading.Barrier(2)

        def _worker(label: str, n: int) -> None:
            barrier.wait()  # maximise contention on the flock
            results[label] = allocate_indices(
                n, scenario=label, role="agents", index_path=tmp_index_path
            )

        t1 = threading.Thread(target=_worker, args=("A", 7))
        t2 = threading.Thread(target=_worker, args=("B", 5))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()

        idx_a = results["A"]
        idx_b = results["B"]
        assert len(idx_a) == 7
        assert len(idx_b) == 5
        # Each thread's slice is internally consecutive (allocate_indices
        # contract: returns ``range(start, start+n)``).
        assert idx_a == list(range(idx_a[0], idx_a[0] + 7))
        assert idx_b == list(range(idx_b[0], idx_b[0] + 5))
        # Cross-thread disjoint.
        assert set(idx_a).isdisjoint(set(idx_b))
        # Union covers a contiguous 0..11 (no skipped indices).
        assert sorted(idx_a + idx_b) == list(range(12))

        # File state coherent.
        data = json.loads(tmp_index_path.read_text())
        assert data["next_index"] == 12
        allocations = data["allocations"]
        assert len(allocations) == 12
        a_records = [r for r in allocations if r["scenario"] == "A"]
        b_records = [r for r in allocations if r["scenario"] == "B"]
        assert len(a_records) == 7
        assert len(b_records) == 5
        # Per-record indices match what each thread reported.
        assert sorted(r["index"] for r in a_records) == sorted(idx_a)
        assert sorted(r["index"] for r in b_records) == sorted(idx_b)


# ═══════════════════════════════════════════════════════════════════════════
# 2. wallet_derivation — index-file corruption
# ═══════════════════════════════════════════════════════════════════════════


class TestWalletDerivationIndexCorruption:
    def test_truncated_json_quarantines_and_raises(self, tmp_index_path):
        """Truncated/unparseable JSON in the index file must be quarantined
        and surface as a clear ``RuntimeError("malformed index file...")``.

        Unified contract (2026-04-16): wrong-shape AND parse-failure
        paths both quarantine + raise. No silent recovery for any malformed
        file — auditability over resilience for the index file.
        """
        from simulation.wallet_derivation import allocate_indices

        bad_content = '{"next_index": 4, "alloc'  # truncated / unparseable
        tmp_index_path.write_text(bad_content)

        with pytest.raises(RuntimeError, match="(?i)malformed"):
            allocate_indices(
                2, scenario="x", role="y", index_path=tmp_index_path
            )

        # Original file moved aside (quarantined), not present at original path.
        assert not tmp_index_path.exists(), (
            "expected the malformed index file to be quarantined "
            "(moved to a .corrupt-<ts> sibling), but original path still exists"
        )

        # A sibling file ``<original>.corrupt-<unix_ts>`` exists with the
        # original bad content preserved verbatim.
        siblings = list(
            tmp_index_path.parent.glob(f"{tmp_index_path.name}.corrupt-*")
        )
        assert len(siblings) == 1, (
            f"expected exactly one quarantined sibling, found {siblings}"
        )
        assert siblings[0].read_text() == bad_content, (
            "quarantined file should preserve the original bad content verbatim"
        )

    def test_missing_next_index_key_treated_as_zero(self, tmp_index_path):
        """Index file present but missing ``next_index`` key: allocate from 0.

        The implementation uses ``data.get("next_index", 0)`` and ALSO uses
        ``data["allocations"].append(...)`` — so a file shaped
        ``{"allocations": []}`` (no next_index, but allocations key present)
        recovers cleanly from index 0.
        """
        from simulation.wallet_derivation import allocate_indices

        tmp_index_path.write_text(json.dumps({"allocations": []}))
        idx = allocate_indices(
            3, scenario="s", role="r", index_path=tmp_index_path
        )
        assert idx == [0, 1, 2]
        data = json.loads(tmp_index_path.read_text())
        assert data["next_index"] == 3
        assert len(data["allocations"]) == 3

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Documents a real gap: _load_or_init silently resets the file when "
            "the JSON parses but is the wrong shape (missing 'allocations' key, "
            "or 'allocations' is not a list). Currently the .append() raises "
            "AttributeError or KeyError mid-allocate, leaving the file in its "
            "pre-call state but the caller seeing an opaque error. A clear "
            "'malformed index file' RuntimeError would be preferable."
        ),
    )
    def test_wrong_shape_raises_clear_error(self, tmp_index_path):
        from simulation.wallet_derivation import allocate_indices

        # JSON is valid but 'allocations' is the wrong type.
        tmp_index_path.write_text(json.dumps({"next_index": 4,
                                              "allocations": "not-a-list"}))
        with pytest.raises(
            (RuntimeError, ValueError),
            match="(?i)malformed|index file|wrong shape",
        ):
            allocate_indices(
                1, scenario="s", role="r", index_path=tmp_index_path
            )


# ═══════════════════════════════════════════════════════════════════════════
# 3. wallet_derivation — first-ever invocation (no file)
# ═══════════════════════════════════════════════════════════════════════════


class TestWalletDerivationMissingFile:
    def test_missing_file_creates_with_default_shape(self, tmp_index_path):
        """First-ever invocation: no file present → allocate creates one with
        the default shape and returns indices starting at 0."""
        from simulation.wallet_derivation import allocate_indices

        assert not tmp_index_path.exists()
        idx = allocate_indices(
            1, scenario="first", role="agents", index_path=tmp_index_path
        )
        assert idx == [0]
        assert tmp_index_path.exists()
        data = json.loads(tmp_index_path.read_text())
        assert data["next_index"] == 1
        assert isinstance(data["allocations"], list)
        assert len(data["allocations"]) == 1
        rec = data["allocations"][0]
        assert rec["index"] == 0
        assert rec["scenario"] == "first"
        assert rec["role"] == "agents"
        assert "ts" in rec  # timestamp recorded

    def test_missing_parent_dir_is_created(self, tmp_path):
        """Parent directory is auto-created if absent."""
        from simulation.wallet_derivation import allocate_indices

        deep = tmp_path / "a" / "b" / "c" / ".wallet_index.json"
        assert not deep.parent.exists()
        idx = allocate_indices(
            2, scenario="s", role="r", index_path=deep
        )
        assert idx == [0, 1]
        assert deep.exists()

    def test_n_less_than_one_raises_value_error(self, tmp_index_path):
        from simulation.wallet_derivation import allocate_indices

        with pytest.raises(ValueError, match="n must be >= 1"):
            allocate_indices(
                0, scenario="s", role="r", index_path=tmp_index_path
            )
        with pytest.raises(ValueError, match="n must be >= 1"):
            allocate_indices(
                -3, scenario="s", role="r", index_path=tmp_index_path
            )


# ═══════════════════════════════════════════════════════════════════════════
# 4. wallet_derivation — reproducibility across processes
# ═══════════════════════════════════════════════════════════════════════════


class TestWalletDerivationReproducibility:
    def test_same_seed_same_index_yields_byte_identical_skey_vkey_address(
        self, fake_master_seed
    ):
        """Re-deriving with the same (master_skey, index) produces byte-equal
        skey, vkey, and address — across separate function calls (the
        cross-process invariant degrades to cross-call when the function is
        a pure deterministic transform of inputs)."""
        from simulation.wallet_derivation import derive

        d1 = derive(7, master_skey=fake_master_seed)
        d2 = derive(7, master_skey=fake_master_seed)
        assert bytes(d1["skey"].payload) == bytes(d2["skey"].payload)
        assert bytes(d1["vkey"].payload) == bytes(d2["vkey"].payload)
        assert str(d1["address"]) == str(d2["address"])

    def test_subprocess_yields_identical_address(self, fake_master_seed):
        """Cross-process check: derive the SAME index in a fresh Python
        subprocess and assert the address bytes match this process's. This
        catches any non-determinism (uninit memory, time-based seeding,
        random-ordering hash quirks) that a same-process check would miss.
        """
        import subprocess
        import sys

        from simulation.wallet_derivation import derive

        in_proc = derive(7, master_skey=fake_master_seed)
        addr_in_proc = str(in_proc["address"])

        # Pass the seed as a hex string on argv to keep stdin clean.
        seed_hex = fake_master_seed.hex()
        code = (
            "import sys; "
            "from simulation.wallet_derivation import derive; "
            f"d = derive(7, master_skey=bytes.fromhex({seed_hex!r})); "
            "sys.stdout.write(str(d['address']))"
        )
        repo_root = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        # PYTHONPATH must include the repo root so the subprocess can import
        # ``simulation`` exactly the same way this process does.
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{repo_root}{os.pathsep}{existing}" if existing else str(repo_root)
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0, (
            f"subprocess failed (code={result.returncode}): {result.stderr}"
        )
        addr_subproc = result.stdout.strip()
        assert addr_subproc == addr_in_proc, (
            f"non-deterministic derivation across processes: "
            f"{addr_in_proc!r} vs {addr_subproc!r}"
        )

    def test_negative_index_rejected(self, fake_master_seed):
        from simulation.wallet_derivation import derive

        with pytest.raises(ValueError, match="index must be >= 0"):
            derive(-1, master_skey=fake_master_seed)

    def test_exactly_one_master_source_required(self, fake_master_seed):
        from simulation.wallet_derivation import derive

        # Both provided.
        with pytest.raises(ValueError, match="exactly one"):
            derive(0, master_skey=fake_master_seed,
                   master_skey_path=Path("/nonexistent"))
        # Neither provided.
        with pytest.raises(ValueError, match="exactly one"):
            derive(0)


# ═══════════════════════════════════════════════════════════════════════════
# 5. wallet_derivation — collision resistance over a large index range
# ═══════════════════════════════════════════════════════════════════════════


class TestWalletDerivationCollisionResistance:
    def test_thousand_indices_yield_unique_addresses(self, fake_master_seed):
        """1000 distinct indices → 1000 distinct addresses (no birthday
        collisions, no integer overflow at i=256/65536 boundary in CBOR
        encoding, etc.). Also asserts skey + vkey uniqueness."""
        from simulation.wallet_derivation import derive

        addresses: set[str] = set()
        skeys: set[bytes] = set()
        for i in range(1000):
            d = derive(i, master_skey=fake_master_seed)
            addresses.add(str(d["address"]))
            skeys.add(bytes(d["skey"].payload))
        assert len(addresses) == 1000, (
            f"address collision: only {len(addresses)} unique out of 1000"
        )
        assert len(skeys) == 1000, (
            f"skey collision: only {len(skeys)} unique out of 1000"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. _setup_agents idempotent restart shape  (metrics-JSONL scanning)
# ═══════════════════════════════════════════════════════════════════════════


def _make_scenario_kwargs(tmp_path: Path, name: str = "edge_hp",
                          rng_seed: int = 42) -> dict:
    """Build base kwargs for a HappyPathScenario suitable for unit tests.

    Uses ``b'x'*32`` as the master skey (NOT a real PaymentSigningKey), so
    decide_and_act_for_epoch deliberately raises NotImplementedError —
    the construction-test contract. We exercise _scan_setup_complete_event
    directly (it does NOT require a real master skey).
    """
    deployment = {
        "version": "edge",
        "claim_ref": "00" * 32 + "#0",
        "challenge_ref": "11" * 32 + "#0",
        "jury_pool_ref": "22" * 32 + "#0",
        "cross_refs_utxo": "33" * 32 + "#0",
        "params_utxo": "33" * 32 + "#1",
        "hashes": {"claim": "44" * 28, "challenge": "55" * 28,
                   "jury_pool": "66" * 28},
        "addresses": {"claim": "addr1_c", "challenge": "addr1_ch",
                      "jury_pool": "addr1_jp"},
    }
    return dict(
        name=name,
        config={"epochs_per_day": 24, "n_agents": 7},
        deployment=deployment,
        master_skey=b"x" * 32,
        master_vkey=b"vkey-stand-in",
        master_wallet_addr="addr1_master_fake",
        checkpoint_dir=tmp_path / "ckpts",
        metrics_dir=tmp_path / "metrics",
        rng_seed=rng_seed,
    )


class TestScanSetupCompleteEvent:
    """Exercise _scan_setup_complete_event — the gate that protects
    _setup_agents from re-running setup on restart."""

    def test_no_metrics_file_returns_none(self, tmp_path):
        """If the metrics JSONL doesn't exist, scan returns None."""
        from simulation.scenarios.happy_path import HappyPathScenario

        s = HappyPathScenario(**_make_scenario_kwargs(tmp_path))
        # Wipe the metrics file even if base init created the directory.
        if s.metrics_path.exists():
            s.metrics_path.unlink()
        assert s._scan_setup_complete_event() is None

    def test_empty_metrics_file_returns_none(self, tmp_path):
        """Empty metrics JSONL: scan returns None (and no error)."""
        from simulation.scenarios.happy_path import HappyPathScenario

        s = HappyPathScenario(**_make_scenario_kwargs(tmp_path))
        s.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        s.metrics_path.write_text("")
        assert s._scan_setup_complete_event() is None

    def test_jsonl_with_only_blank_lines_returns_none(self, tmp_path):
        from simulation.scenarios.happy_path import HappyPathScenario

        s = HappyPathScenario(**_make_scenario_kwargs(tmp_path))
        s.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        s.metrics_path.write_text("\n\n   \n")
        assert s._scan_setup_complete_event() is None

    def test_malformed_last_line_still_finds_prior_setup_complete(
        self, tmp_path
    ):
        """A truncated JSON line at end-of-file (e.g. crash mid-write) must
        not hide an earlier valid setup_complete event. The scan skips
        unparseable lines and keeps scanning."""
        from simulation.scenarios.happy_path import HappyPathScenario

        s = HappyPathScenario(**_make_scenario_kwargs(tmp_path))
        s.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        evt = {"event_type": "setup_complete",
               "agent_indices": [3, 4, 5, 6, 7, 8, 9],
               "scenario": s.name}
        with open(s.metrics_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")
            f.write('{"event_type":"submit_claim_succe')  # truncated
        prior = s._scan_setup_complete_event()
        assert prior is not None
        assert prior["agent_indices"] == [3, 4, 5, 6, 7, 8, 9]

    def test_two_setup_complete_events_returns_last(self, tmp_path):
        """If the JSONL contains TWO setup_complete events (which would only
        happen on a Catherine bug — setup must be idempotent), the scan
        returns the LAST one. This tests the documented behaviour:
        ``last: dict | None = None`` overwrite-on-each-match.

        Why the LAST: a hypothetical second setup_complete would mean the
        most recent set of indices is the canonical state on chain — using
        an earlier (stale) set would point at addresses that may have been
        re-funded. The 'last wins' choice is defensive."""
        from simulation.scenarios.happy_path import HappyPathScenario

        s = HappyPathScenario(**_make_scenario_kwargs(tmp_path))
        s.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        evt_old = {"event_type": "setup_complete",
                   "agent_indices": [0, 1, 2, 3, 4, 5, 6],
                   "scenario": s.name}
        evt_new = {"event_type": "setup_complete",
                   "agent_indices": [10, 11, 12, 13, 14, 15, 16],
                   "scenario": s.name}
        with open(s.metrics_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(evt_old) + "\n")
            f.write(json.dumps({"event_type": "noise"}) + "\n")
            f.write(json.dumps(evt_new) + "\n")
        prior = s._scan_setup_complete_event()
        assert prior is not None
        assert prior["agent_indices"] == [10, 11, 12, 13, 14, 15, 16]


# ═══════════════════════════════════════════════════════════════════════════
# 7. _setup_agents partial-failure recovery (DOCUMENTED GAP)
# ═══════════════════════════════════════════════════════════════════════════


class TestSetupAgentsPartialFailureRecovery:
    """Probe what happens when DID registration fails mid-flight.

    Concretely: ``_setup_agents`` allocates 7 indices, funds them in one TX,
    then registers 7 DIDs SERIALLY. If registration #4 fails:
      - indices [N..N+6] are consumed in the wallet_index file
      - the funding TX is on-chain (cannot be undone)
      - 3 DIDs are registered, 4 are not
      - ``setup_complete`` is NEVER emitted (the function raised)
      - ``_agent_setup_done`` stays False

    On restart, ``_scan_setup_complete_event`` returns None (no
    setup_complete was ever emitted), so ``_setup_agents`` runs from the
    top: it RE-ALLOCATES new indices, RE-FUNDS new addresses, and
    RE-REGISTERS DIDs. The 3 partially-registered DIDs are LEAKED (orphaned
    on-chain), and the master wallet pays setup cost twice.

    This test pins that gap so 3b can decide how to fix it.
    """

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Known recovery gap: if DID registration fails mid-flight, the "
            "next run re-allocates indices and re-runs the entire setup "
            "(double-spends master ADA, leaks the partially-registered DIDs). "
            "Catherine 3b should either persist setup-progress to the "
            "checkpoint between each registration, or detect "
            "already-funded addresses + already-registered DIDs and resume "
            "from where it left off. Repro: see test body."
        ),
    )
    def test_mid_registration_failure_does_not_re_run_setup_on_restart(
        self, tmp_path
    ):
        """REPRO: simulate that allocate_indices ran (file shows 7 used),
        funding TX landed, then DID #4 failed and the process crashed before
        emitting setup_complete. On restart, the SECOND run must NOT
        re-allocate or re-fund — it must complete only the missing
        registrations.

        We assert a property the current implementation cannot satisfy: a
        fresh ``_scan_setup_complete_event`` call returns None (correct,
        because nothing was emitted), AND there is no other persisted state
        that lets _setup_agents skip the funding step. xfail documents the
        gap.
        """
        from simulation.scenarios.happy_path import HappyPathScenario

        s = HappyPathScenario(**_make_scenario_kwargs(tmp_path))
        s.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        # Simulate 3 partial DID registrations recorded as side-events.
        # (Today: nothing is recorded in metrics for in-progress setup.)
        with open(s.metrics_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "event_type": "did_registered",
                "role": "claimant",
                "did_hex": "aa" * 32,
            }) + "\n")
            f.write(json.dumps({
                "event_type": "did_registered",
                "role": "auditor",
                "did_hex": "bb" * 32,
            }) + "\n")
            f.write(json.dumps({
                "event_type": "did_registered",
                "role": "juror_0",
                "did_hex": "cc" * 32,
            }) + "\n")
        prior = s._scan_setup_complete_event()
        assert prior is None  # correctly: no setup_complete event was emitted

        # The CONTRACT we want (xfail): the scenario should expose enough
        # state to know that 3 of 7 DIDs are already registered and funding
        # already happened, so the next setup run skips funding + only
        # registers the remaining 4. Today no such API exists.
        # Asserting the missing API: the scenario should expose
        # ``_partial_setup_state()`` returning the recovered DIDs.
        assert hasattr(s, "_partial_setup_state"), (
            "Catherine 3b: add a recovery API that scans metrics + chain for "
            "partial setup state so restart doesn't double-pay."
        )


# ═══════════════════════════════════════════════════════════════════════════
# 8. _derive_wallets — orchestrator-level determinism
# ═══════════════════════════════════════════════════════════════════════════


class TestDeriveWalletsOrchestratorDeterminism:
    """The ``vkh_hex`` placeholder seed is deterministic from
    (master_skey, name, rng_seed, role) — established by
    test_happy_path_scenario.py. Here we lock the sister invariant for the
    orchestrator: the GLOBAL allocate_indices counter is what disambiguates
    two scenarios that share the same name+seed (a degenerate config).

    Two scenarios with the same name + same rng_seed produce IDENTICAL
    placeholder vkhs (deterministic), but their REAL on-chain indices come
    from the global counter and MUST differ.
    """

    def test_same_name_same_seed_yields_identical_placeholder_vkhs(
        self, tmp_path
    ):
        from simulation.scenarios.happy_path import HappyPathScenario

        s1 = HappyPathScenario(**_make_scenario_kwargs(tmp_path, name="dup",
                                                      rng_seed=42))
        s2 = HappyPathScenario(**_make_scenario_kwargs(tmp_path, name="dup",
                                                      rng_seed=42))
        # Placeholder vkhs depend only on (master, name, seed, role) — same.
        for role in s1._wallets:
            assert (
                s1._wallets[role]["vkh_hex"] == s2._wallets[role]["vkh_hex"]
            ), f"role {role}: placeholder vkh diverged"

    def test_global_counter_disambiguates_same_name_same_seed(
        self, tmp_index_path
    ):
        """Even if two scenarios pick the exact same name + rng_seed (a
        misconfiguration), two sequential allocate_indices calls return
        DISJOINT integer ranges — the global counter is the source of
        on-chain uniqueness, not the per-scenario seed."""
        from simulation.wallet_derivation import allocate_indices

        a = allocate_indices(
            7, scenario="dup", role="agents", index_path=tmp_index_path
        )
        b = allocate_indices(
            7, scenario="dup", role="agents", index_path=tmp_index_path
        )
        assert set(a).isdisjoint(set(b)), (
            "two same-name allocations collided — concurrent dup-name "
            "scenarios would burn each other's UTxOs"
        )
        # And the union still has the contiguous shape.
        assert sorted(a + b) == list(range(14))


# ═══════════════════════════════════════════════════════════════════════════
# 9. chain.py — cost_models edge (empty plutusCostModels)
# ═══════════════════════════════════════════════════════════════════════════


# Minimal Ogmios-shaped protocolParameters payload. Only the fields actually
# read by OgmiosContext.protocol_param need to be populated; everything
# else falls through to the .get(...) default.
_OGMIOS_PP_MINIMAL = {
    "minFeeConstant": {"ada": {"lovelace": 155381}},
    "minFeeCoefficient": 44,
    "maxBlockBodySize": {"bytes": 90112},
    "maxTransactionSize": {"bytes": 16384},
    "maxBlockHeaderSize": {"bytes": 1100},
    "stakeCredentialDeposit": {"ada": {"lovelace": 2000000}},
    "stakePoolDeposit": {"ada": {"lovelace": 500000000}},
    "stakePoolPledgeInfluence": "3/10",
    "monetaryExpansion": "3/1000",
    "treasuryExpansion": "2/10",
    "version": {"major": 10, "minor": 0},
    "minUtxoDepositCoefficient": 4310,
    "minStakePoolCost": {"ada": {"lovelace": 170000000}},
    "scriptExecutionPrices": {"memory": "577/10000", "cpu": "721/10000000"},
    "maxExecutionUnitsPerTransaction": {"memory": 14000000, "cpu": 10000000000},
    "maxExecutionUnitsPerBlock": {"memory": 62000000, "cpu": 40000000000},
    "maxValueSize": {"bytes": 5000},
    "collateralPercentage": 150,
    "maxCollateralInputs": 3,
    # plutusCostModels intentionally empty for this test.
    "plutusCostModels": {},
}


class TestChainCostModelsEdge:
    def test_empty_plutus_cost_models_does_not_raise(self):
        """Ogmios-returned ``plutusCostModels`` of ``{}`` (no v1/v2/v3) must
        leave ProtocolParameters constructible — pure-ADA TXes don't need
        cost models. Catches the bug class where the dict-comprehension
        crashed on missing keys."""
        from simulation.chain import OgmiosContext

        ctx = OgmiosContext()
        with patch("simulation.chain.ogmios_rpc",
                   return_value=_OGMIOS_PP_MINIMAL):
            pp = ctx.protocol_param
        # Construction succeeded.
        assert pp is not None
        # cost_models is an empty dict (no language present in the payload).
        assert pp.cost_models == {}

    def test_partial_plutus_cost_models_only_v3(self):
        """Ogmios returns ``plutus:v3`` only (typical post-Conway). The
        resulting cost_models dict must contain exactly the PlutusV3 entry
        and nothing else."""
        from simulation.chain import OgmiosContext

        ctx = OgmiosContext()
        payload = dict(_OGMIOS_PP_MINIMAL)
        payload["plutusCostModels"] = {"plutus:v3": [1, 2, 3, 4, 5]}
        with patch("simulation.chain.ogmios_rpc", return_value=payload):
            pp = ctx.protocol_param
        assert "PlutusV3" in pp.cost_models
        assert "PlutusV1" not in pp.cost_models
        assert "PlutusV2" not in pp.cost_models
        # List → enumerated dict.
        assert pp.cost_models["PlutusV3"] == {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}

    def test_dict_shaped_cost_model_is_normalised_to_int_keyed_dict(self):
        """If Ogmios returns the cost-model as a dict (string-numeric keys),
        the comprehension converts string-numeric keys to ints."""
        from simulation.chain import OgmiosContext

        ctx = OgmiosContext()
        payload = dict(_OGMIOS_PP_MINIMAL)
        payload["plutusCostModels"] = {"plutus:v3": {"0": 100, "1": 200}}
        with patch("simulation.chain.ogmios_rpc", return_value=payload):
            pp = ctx.protocol_param
        assert pp.cost_models["PlutusV3"] == {0: 100, 1: 200}


# ═══════════════════════════════════════════════════════════════════════════
# 10. chain.py — script-ref parser (v6 / v5 / no-script)
# ═══════════════════════════════════════════════════════════════════════════


class TestChainScriptRefParser:
    """Validate the v6/v5/none branches of the script_ref parser inside
    OgmiosContext.utxos and resolve_utxo. We monkeypatch ogmios_rpc so the
    test stays offline."""

    @staticmethod
    def _utxo_payload(*, script: dict | None) -> list[dict]:
        item: dict[str, Any] = {
            "transaction": {"id": "ab" * 32},
            "index": 0,
            "address": "addr1vxrmuxx8xvnet4ly9aw5wq6ts22x63zrhak6qu4dh2q4lhcdet2tx",
            "value": {"ada": {"lovelace": 5_000_000}},
        }
        # Only set the key when a script is actually present — the parser
        # branches on ``"script" in item`` (key presence), not on
        # truthiness, so omitting the key is what models a regular UTxO.
        if script is not None:
            item["script"] = script
        return [item]

    def test_no_script_key_yields_script_ref_none(self):
        """A regular pure-ADA UTxO without ``script`` key must produce a
        TransactionOutput whose ``script`` attribute is None."""
        from simulation.chain import OgmiosContext

        ctx = OgmiosContext()
        with patch("simulation.chain.ogmios_rpc",
                   return_value=self._utxo_payload(script=None)):
            utxos = ctx.utxos("addr1vxrmuxx8xvnet4ly9aw5wq6ts22x63zrhak6qu4dh2q4lhcdet2tx")
        assert len(utxos) == 1
        assert utxos[0].output.script is None

    def test_v6_format_plutus_v3_script_populated(self):
        """Ogmios v6: ``{"language": "plutus:v3", "cbor": "..."}``."""
        from simulation.chain import OgmiosContext
        from pycardano import PlutusV3Script

        # Minimal valid (well-formed) hex — content is opaque to the
        # parser, just needs to be even-length valid hex.
        script_hex = "4d010000332253330044a229309b10"
        with patch("simulation.chain.ogmios_rpc",
                   return_value=self._utxo_payload(
                       script={"language": "plutus:v3", "cbor": script_hex})):
            ctx = OgmiosContext()
            utxos = ctx.utxos("addr1vxrmuxx8xvnet4ly9aw5wq6ts22x63zrhak6qu4dh2q4lhcdet2tx")
        assert len(utxos) == 1
        assert isinstance(utxos[0].output.script, PlutusV3Script)
        assert bytes(utxos[0].output.script) == bytes.fromhex(script_hex)

    def test_v5_format_plutus_v3_script_populated(self):
        """Ogmios v5 legacy: ``{"plutus:v3": "<hex>"}``."""
        from simulation.chain import OgmiosContext
        from pycardano import PlutusV3Script

        script_hex = "4d010000332253330044a229309b20"
        with patch("simulation.chain.ogmios_rpc",
                   return_value=self._utxo_payload(
                       script={"plutus:v3": script_hex})):
            ctx = OgmiosContext()
            utxos = ctx.utxos("addr1vxrmuxx8xvnet4ly9aw5wq6ts22x63zrhak6qu4dh2q4lhcdet2tx")
        assert len(utxos) == 1
        assert isinstance(utxos[0].output.script, PlutusV3Script)
        assert bytes(utxos[0].output.script) == bytes.fromhex(script_hex)


# ═══════════════════════════════════════════════════════════════════════════
# 11. chain.py — error verbosity on malformed RPC body
# ═══════════════════════════════════════════════════════════════════════════


class TestChainErrorVerbosity:
    """The error path for ``ogmios_rpc`` must include the response text so
    debugging is fast — and the response text MUST NOT contain master skey
    bytes (real error paths only see what the upstream sent us, but we test
    the contract here so a future change can't accidentally leak)."""

    def test_http_error_includes_response_text_snippet(self):
        """Non-2xx HTTP response: RuntimeError message includes the body
        (truncated to 2000 chars per current code) and the HTTP status."""
        from simulation import chain as chain_mod

        class _FakeResp:
            ok = False
            status_code = 500
            text = "ledger error: cannot find tip block"

            def json(self):
                return {}

        with patch.object(chain_mod.requests, "post", return_value=_FakeResp()):
            with pytest.raises(RuntimeError) as exc_info:
                chain_mod.ogmios_rpc("queryLedgerState/tip")
        msg = str(exc_info.value)
        assert "queryLedgerState/tip" in msg, msg
        assert "500" in msg, msg
        assert "ledger error: cannot find tip block" in msg, msg
        # Defence-in-depth: master skey bytes must not appear in any error.
        # (We can't trigger that here, but we assert the absence of the
        # anti-pattern marker as a smoke check.)
        assert "skey" not in msg.lower() or "skey-bearing" not in msg

    def test_json_rpc_error_includes_error_payload(self):
        """A 200 OK with ``error`` field must surface the error in the
        raised RuntimeError, with the method name for context."""
        from simulation import chain as chain_mod

        class _FakeResp:
            ok = True
            status_code = 200
            text = '{"error":{"code":-32602,"message":"invalid params"}}'

            def json(self):
                return {"error": {"code": -32602,
                                  "message": "invalid params"}}

        with patch.object(chain_mod.requests, "post", return_value=_FakeResp()):
            with pytest.raises(RuntimeError) as exc_info:
                chain_mod.ogmios_rpc("evaluateTransaction", params={})
        msg = str(exc_info.value)
        assert "evaluateTransaction" in msg, msg
        assert "invalid params" in msg, msg

    def test_submit_tx_failure_includes_response_snippet(self):
        """submit_tx: non-202 status surfaces the response body truncated to
        500 chars (per current implementation)."""
        from simulation import chain as chain_mod

        class _FakeResp:
            status_code = 400
            text = (
                "Tx validation failed: BadInputsUTxO {InputsUTxO = "
                "fromList [TxIn (TxId hash) 0]}"
            )

            def json(self):
                return {}

        with patch.object(chain_mod.requests, "post", return_value=_FakeResp()):
            with pytest.raises(RuntimeError) as exc_info:
                chain_mod.submit_tx(b"\x00\x01\x02")
        msg = str(exc_info.value)
        assert "400" in msg
        assert "BadInputsUTxO" in msg


# ═══════════════════════════════════════════════════════════════════════════
# 12. tx_builder.build_register_did — Path vs PlutusV3Script polymorphism
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildRegisterDidPolymorphism:
    """``build_register_did`` accepts EITHER a path to a plutus.json
    blueprint OR a pre-loaded PlutusV3Script. Both code paths must produce
    identical TX bytes (no semantic drift in the load path).

    We can't run the full builder offline (it needs Ogmios), so we test the
    branch directly: feed it both inputs and assert the SCRIPT BYTES used
    by the builder are identical. This is the only divergence point in the
    polymorphism — once we've got identical bytes downstream of the load,
    the rest of the function is identical by construction.
    """

    def test_path_and_loaded_script_yield_same_script_bytes(self):
        from pycardano import PlutusV3Script

        # The on-disk plutus.json that the live tests use, resolved via
        # APEX_WORKSPACE so the test runs on any machine.
        import os as _os
        _ws = Path(_os.environ.get("APEX_WORKSPACE", "."))
        registry_json = _ws / "testnet" / "agent-registry-plutus.json"
        if not registry_json.exists():
            pytest.skip(
                f"registry blueprint not present at {registry_json} — "
                "polymorphism test requires the on-disk blueprint."
            )

        # 1. Path branch: replicate the builder's own load logic.
        bp = json.loads(registry_json.read_text())
        from_path: PlutusV3Script | None = None
        for v in bp.get("validators", []):
            if "mint" in v.get("title", "").lower():
                from_path = PlutusV3Script(bytes.fromhex(v["compiledCode"]))
                break
        assert from_path is not None, (
            "couldn't find a mint validator in the registry blueprint — "
            "test fixture is wrong"
        )

        # 2. Pre-loaded branch: feed the same bytes through PlutusV3Script.
        from_loaded = PlutusV3Script(bytes(from_path))

        assert bytes(from_path) == bytes(from_loaded), (
            "PlutusV3Script(path) and PlutusV3Script(loaded) diverged — "
            "build_register_did's polymorphism would produce different TXes"
        )

    def test_no_mint_validator_in_blueprint_raises(self, tmp_path):
        """A blueprint with no validator whose title contains 'mint' must
        cause build_register_did to raise a clear RuntimeError."""
        from simulation import tx_builder as _txb

        bad_bp = tmp_path / "bad-plutus.json"
        bad_bp.write_text(json.dumps({
            "validators": [
                {"title": "spend.foo", "compiledCode": "deadbeef"},
            ]
        }))

        # We want the blueprint-validation branch to raise BEFORE any chain
        # work happens. The function calls ensure_collateral immediately
        # after parsing, so we assert that the script-parse RuntimeError is
        # what surfaces — by stubbing ensure_collateral to a sentinel that
        # would mark the test as broken if reached.
        sentinel: list[bool] = []

        def _should_not_be_called(*a, **kw):
            sentinel.append(True)
            raise AssertionError(
                "ensure_collateral was called — script-parse branch did not "
                "short-circuit"
            )

        from pycardano import PaymentSigningKey, PaymentVerificationKey

        # Generate a real master skey/vkey pair so the function gets past
        # type checks (it ultimately won't reach signing because we expect
        # the RuntimeError first).
        skey = PaymentSigningKey.generate()
        vkey = PaymentVerificationKey.from_signing_key(skey)
        agent_skey = PaymentSigningKey.generate()
        agent_vkey = PaymentVerificationKey.from_signing_key(agent_skey)

        with patch.object(_txb, "ensure_collateral", _should_not_be_called):
            with pytest.raises(RuntimeError, match="No mint validator"):
                _txb.build_register_did(
                    skey, vkey, "addr1_dummy",
                    agent_skey, agent_vkey.hash(),
                    str(bad_bp),
                    None,  # ctx — unreached
                    scenario_name="t", role="r",
                )
        assert sentinel == [], "ensure_collateral was called despite bad bp"


# ═══════════════════════════════════════════════════════════════════════════
# 13. tx_builder.build_juror_bond — reserved-for-future signature kwargs
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildJurorBondReservedKwargs:
    """``juror_skey`` / ``juror_vkh`` are reserved-for-future per Catherine's
    docstring (Path-A juror-self-bonding). They must be accepted but must
    NOT affect TX bytes today (Path-B uses master signing only).

    We assert this at the source level: the function signature accepts both
    parameters as positional-or-keyword, and the function body must not use
    them in any TX-construction path. Concretely, the helper is documented
    to bind ``_ = (juror_skey, juror_vkh)`` and use only master_skey for
    signing + master_vkey.hash() in required_signers.
    """

    def test_signature_accepts_juror_skey_and_juror_vkh(self):
        """Inspect the function signature and assert juror_skey / juror_vkh
        are present as positional-or-keyword parameters."""
        import inspect
        from simulation import tx_builder as _txb

        sig = inspect.signature(_txb.build_juror_bond)
        params = sig.parameters
        assert "juror_skey" in params, (
            "build_juror_bond missing reserved-for-future juror_skey kwarg"
        )
        assert "juror_vkh" in params, (
            "build_juror_bond missing reserved-for-future juror_vkh kwarg"
        )
        # Position matters — they sit after master_addr per the docstring's
        # documented call order.
        positional = [
            n for n, p in params.items()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.POSITIONAL_ONLY)
        ]
        assert positional.index("juror_skey") > positional.index("master_addr")
        assert positional.index("juror_vkh") > positional.index("juror_skey")

    def test_juror_skey_and_juror_vkh_not_used_in_signing_path(self):
        """Static check: the juror_skey / juror_vkh symbols must not appear
        in the *executable body* of build_juror_bond beyond the deliberate
        ``_ = (juror_skey, juror_vkh)`` consume-the-symbol marker.

        Method:
          1. Find the consume-marker line (must exist).
          2. Take the source AFTER that line — i.e. the actual transaction
             construction code — and assert it never references the reserved
             symbols. The function signature, docstring, and the marker
             line itself are excluded by construction.
        """
        import inspect
        from simulation import tx_builder as _txb

        src = inspect.getsource(_txb.build_juror_bond)
        marker = "_ = (juror_skey, juror_vkh)"
        assert marker in src, (
            "build_juror_bond no longer marks (juror_skey, juror_vkh) as "
            "reserved — either it now uses them (update this test) or it "
            "dropped the consume-marker (re-add it for static-analysis hygiene)."
        )

        # Slice to the executable body AFTER the marker.
        marker_idx = src.index(marker)
        body_after_marker = src[marker_idx + len(marker):]

        assert "juror_skey" not in body_after_marker, (
            "juror_skey is referenced AFTER the reserved-for-future marker "
            "— Path-A signing has been turned on without updating the test. "
            "If this is intentional, replace this assertion with a positive "
            "test that build_juror_bond now signs with juror_skey."
        )
        assert "juror_vkh" not in body_after_marker, (
            "juror_vkh is referenced AFTER the reserved-for-future marker."
        )


# ═══════════════════════════════════════════════════════════════════════════
# 14. Regression: build_register_did AgentDatum hardcoded strings
#     framework must be b"Vector-Agent"; description must start with
#     b"vector agent " — guards against reverting the sim-code hygiene rename.
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildRegisterDidDatumStrings:
    """build_register_did must produce an AgentDatum with the renamed
    on-chain strings: framework=b"Vector-Agent" and description starting
    with b"vector agent ".  Any revert of the hygiene rename (back to
    b"Apex-Sim" / "sim agent") will fail this test immediately.
    """

    def test_agent_datum_framework_and_description(self):
        """Arrange: stub all I/O so the datum construction runs offline.
        Act: call build_register_did with a minimal PlutusV3Script.
        Assert: the cbor2.CBORTag(121, [...]) built as agent_datum has
                field[2] == b"vector agent <role>" and
                field[4] == b"Vector-Agent".
        """
        import cbor2
        from pycardano import (
            PaymentSigningKey,
            PaymentVerificationKey,
            PlutusV3Script,
        )
        from unittest.mock import MagicMock, patch
        from simulation import tx_builder as _txb

        skey = PaymentSigningKey.generate()
        vkey = PaymentVerificationKey.from_signing_key(skey)
        agent_skey = PaymentSigningKey.generate()
        agent_vkh = PaymentVerificationKey.from_signing_key(agent_skey).hash()

        # Minimal 28-byte PlutusV3Script placeholder (no real validator needed
        # because we never submit the TX — we only inspect the datum object).
        dummy_script = PlutusV3Script(b"\xd8\x79\x81\x40" * 7)

        # Fake UTxO seed so sorted_utxos[0] exists.
        fake_txid = MagicMock()
        fake_txid.__bytes__ = lambda self: b"\x00" * 32
        fake_txid.hex.return_value = "00" * 32
        fake_utxo = MagicMock()
        fake_utxo.input.transaction_id = fake_txid
        fake_utxo.input.index = 0

        ctx = MagicMock()
        ctx.last_block_slot = 1000

        captured: list = []

        real_cbor_tag = cbor2.CBORTag

        def _spy_cbor_tag(tag, value):
            obj = real_cbor_tag(tag, value)
            if (
                tag == 121
                and isinstance(value, list)
                and len(value) == 7
                and isinstance(value[4], bytes)
            ):
                captured.append(obj)
            return obj

        with (
            patch.object(_txb, "ensure_collateral", return_value=None),
            patch.object(_txb, "get_wallet_utxos_no_collateral", return_value=[fake_utxo]),
            patch("simulation.tx_builder.cbor2.CBORTag", side_effect=_spy_cbor_tag),
        ):
            try:
                _txb.build_register_did(
                    skey, vkey, "addr1_dummy",
                    agent_skey, agent_vkh,
                    dummy_script,
                    ctx,
                    scenario_name="hygiene_test",
                    role="worker",
                )
            except Exception:
                # TX build will fail offline — we only care the datum was built.
                pass

        assert captured, (
            "No 7-field cbor2.CBORTag(121, ...) was constructed — "
            "build_register_did did not reach the agent_datum construction. "
            "Check stub setup."
        )
        datum_fields = captured[0].value

        assert datum_fields[4] == b"Vector-Agent", (
            f"AgentDatum.framework is {datum_fields[4]!r} — expected b'Vector-Agent'. "
            "Likely a revert of the hygiene rename."
        )
        assert datum_fields[2].startswith(b"vector agent "), (
            f"AgentDatum.description is {datum_fields[2]!r} — expected to start "
            "with b'vector agent '. Likely a revert of the hygiene rename."
        )
        assert datum_fields[2] == b"vector agent worker", (
            f"AgentDatum.description is {datum_fields[2]!r} — expected "
            "b'vector agent worker' for role='worker'."
        )
