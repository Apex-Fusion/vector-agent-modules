"""
RED tests for simulation.scenarios.happy_path.HappyPathScenario
(Sim Phase 2 iter 3).

Claire — test-engineer pass.

Two test groups:

  A. Construction-only (no marker)
     Exercise the subclass constructor + wallet derivation. These pass even
     in RED (they don't touch decide_and_act_for_epoch). They lock the
     determinism + collision-resistance contract for per-scenario wallets.

  B. Live testnet (`@pytest.mark.testnet @pytest.mark.slow`)
     End-to-end happy path against the v15 sim deployment on Vector testnet.
     RED until Catherine implements the lifecycle helpers — they fail with
     NotImplementedError. SKIPPED by default because the marker is not
     selected.

Run modes:
    pytest simulation/tests/test_happy_path_scenario.py
        -> only construction tests run, all pass.
    pytest -m testnet simulation/tests/test_happy_path_scenario.py
        -> live tests collected, all fail with NotImplementedError (RED).
    pytest simulation/tests/
        -> 357 prior tests + N construction tests pass; testnet tests SKIPPED.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest


# Test suite imports the SUT — module file must exist (it does, after Claire's
# RED skeleton). Method bodies in the lifecycle helpers raise
# NotImplementedError, which is the desired RED failure.
from simulation.scenarios.happy_path import (
    HappyPathScenario,
    ROLE_CLAIMANT,
    ROLE_AUDITOR,
    ROLE_JUROR_PREFIX,
    _derive_role_seed,
)


# ---------------------------------------------------------------------------
# Marker handling
#
# The project has no pyproject.toml / pytest.ini, so the `testnet` and `slow`
# markers are not registered globally. We:
#   1) Register the markers locally so pytest stops emitting
#      PytestUnknownMarkWarning during collection.
#   2) Auto-skip every `@pytest.mark.testnet` item UNLESS the user explicitly
#      passes `-m testnet` (or `--run-testnet`). This makes the default
#      `pytest simulation/tests/` run skip live tests, while
#      `pytest -m testnet ...` opts in.
#
# We can't put this in conftest.py (locked by Phase 1). Instead we use the
# pytest plugin protocol: any module placed via `pytest_plugins` works, but
# the simplest portable hook is to attach a session-scoped autouse fixture
# in this file that calls pytest.skip() when the marker is not selected.
# ---------------------------------------------------------------------------


def _testnet_marker_selected(config: "pytest.Config") -> bool:
    """Return True if the user explicitly opted in to testnet tests.

    Recognised opt-in: `-m` expression that contains the literal token
    "testnet". We deliberately treat "not testnet" as opt-OUT (pytest's
    own marker expression handling will then deselect them anyway, but
    this makes our own gate idempotent).
    """
    marker_expr = config.getoption("-m", default="") or ""
    if "testnet" not in marker_expr:
        return False
    if marker_expr.strip().startswith("not "):
        return False
    return True


@pytest.fixture(autouse=True)
def _gate_testnet_marker(request):
    """Auto-skip @pytest.mark.testnet tests unless `-m testnet` was passed."""
    if request.node.get_closest_marker("testnet") is None:
        return
    if _testnet_marker_selected(request.config):
        return
    pytest.skip("live testnet test — pass `-m testnet` to opt in")


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register the custom markers used in this file (suppresses warnings)."""
    config.addinivalue_line(
        "markers",
        "testnet: live test that hits Vector testnet via Ogmios — opt-in.",
    )
    config.addinivalue_line(
        "markers",
        "slow: long-running test (minutes) — opt-in.",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


# Master skey + v15 deployment manifest — resolved via APEX_WORKSPACE env var
# so the test suite works on any machine. Set APEX_WORKSPACE to the root that
# contains the `testnet/` directory with the skey and deployment manifest.
# Live @pytest.mark.testnet tests are skipped by default; opt in with `-m testnet`.
import os as _os
_WORKSPACE = Path(_os.environ.get("APEX_WORKSPACE", ".")).resolve()
MASTER_SKEY_PATH = _WORKSPACE / "testnet" / "wallet.skey"
V15_DEPLOYMENT_PATH = _WORKSPACE / "testnet" / "game1-sim-deployment.json"


@pytest.fixture
def fake_master_skey_bytes() -> bytes:
    """A deterministic stand-in for a real master skey, used in construction
    tests. Must be 32 bytes so it could be parsed as an ed25519 seed if a
    test ever decided to actually instantiate a PaymentSigningKey from it."""
    return b"x" * 32


@pytest.fixture
def fake_master_skey_bytes_alt() -> bytes:
    """Alternate master skey to assert that derivation differs across master keys."""
    return b"y" * 32


@pytest.fixture
def base_kwargs(tmp_path, fake_master_skey_bytes):
    """Construction kwargs for a HappyPathScenario instance.

    Uses the v15 deployment dict as a stand-in for `deployment` so the field
    is populated, but no live chain calls are made by construction.
    """
    if V15_DEPLOYMENT_PATH.exists():
        deployment = json.loads(V15_DEPLOYMENT_PATH.read_text())
    else:
        # Construction tests must not depend on the v15 manifest — use a
        # plain-dict fallback (live tests separately assert the file exists).
        deployment = {
            "version": "v15-fallback",
            "claim_ref": "00" * 32 + "#0",
            "challenge_ref": "11" * 32 + "#0",
            "jury_pool_ref": "22" * 32 + "#0",
            "cross_refs_utxo": "33" * 32 + "#0",
            "params_utxo": "33" * 32 + "#1",
            "hashes": {
                "claim": "44" * 28,
                "challenge": "55" * 28,
                "jury_pool": "66" * 28,
            },
            "addresses": {
                "claim": "addr1_fake_claim",
                "challenge": "addr1_fake_challenge",
                "jury_pool": "addr1_fake_jury_pool",
            },
        }

    return dict(
        name="hp_test",
        config={"epochs_per_day": 24, "n_agents": 7},
        deployment=deployment,
        master_skey=fake_master_skey_bytes,
        master_vkey=b"vkey-stand-in",
        master_wallet_addr="addr1_master_fake",
        checkpoint_dir=tmp_path / "checkpoints",
        metrics_dir=tmp_path / "metrics",
        rng_seed=42,
    )


# ═══════════════════════════════════════════════════════════════════════════
# A. Construction-only tests (no marker — should PASS even in RED)
# ═══════════════════════════════════════════════════════════════════════════


class TestConstruction:
    def test_subclass_instantiation_inherits_base_state(self, base_kwargs):
        """Subclass __init__ delegates to ScenarioRunner.__init__ correctly.

        Verifies that base-class side-effects (rng creation, dir creation,
        epoch counter init, metrics/checkpoint paths) all happen.
        """
        s = HappyPathScenario(**base_kwargs)

        # Base-class invariants from ScenarioRunner.__init__:
        assert s.name == "hp_test"
        assert s.rng_seed == 42
        assert s.rng is not None
        assert s._epoch == -1
        assert s.checkpoint_path == base_kwargs["checkpoint_dir"] / "hp_test.json"
        assert s.metrics_path == base_kwargs["metrics_dir"] / "hp_test.jsonl"
        # Dirs created on construction.
        assert base_kwargs["checkpoint_dir"].exists()
        assert base_kwargs["metrics_dir"].exists()

    def test_subclass_specific_kwargs_recorded(self, base_kwargs):
        """jury_size and stake_amount are stored on the instance."""
        s = HappyPathScenario(**base_kwargs, jury_size=5, stake_amount=75_000_000)
        assert s.jury_size == 5
        assert s.stake_amount == 75_000_000

    def test_subclass_kwargs_have_defaults(self, base_kwargs):
        """jury_size and stake_amount have sensible defaults."""
        s = HappyPathScenario(**base_kwargs)
        assert s.jury_size == 5
        assert s.stake_amount == 50_000_000

    def test_initial_lifecycle_state(self, base_kwargs):
        """Fresh scenario starts at submit_claim with no claim/challenge yet."""
        s = HappyPathScenario(**base_kwargs)
        assert s._step == "submit_claim"
        assert s._claim_ref is None
        assert s._claim_token_hex is None
        assert s._challenge_ref is None
        assert s._verdict is None
        assert s._tx_hashes == {}

    def test_wallet_roles_present(self, base_kwargs):
        """Construction derives one claimant + one auditor + jury_size jurors."""
        s = HappyPathScenario(**base_kwargs, jury_size=5)

        assert ROLE_CLAIMANT in s._wallets
        assert ROLE_AUDITOR in s._wallets
        for i in range(5):
            assert f"{ROLE_JUROR_PREFIX}_{i}" in s._wallets

        # No extra roles beyond what we expect.
        expected = {ROLE_CLAIMANT, ROLE_AUDITOR} | {
            f"{ROLE_JUROR_PREFIX}_{i}" for i in range(5)
        }
        assert set(s._wallets.keys()) == expected

    def test_wallet_role_count_scales_with_jury_size(self, base_kwargs):
        """jury_size=3 produces 3 juror roles (plus claimant + auditor)."""
        s = HappyPathScenario(**base_kwargs, jury_size=3)
        juror_keys = [k for k in s._wallets if k.startswith(ROLE_JUROR_PREFIX)]
        assert len(juror_keys) == 3
        assert len(s._wallets) == 5  # 3 jurors + claimant + auditor

    def test_wallet_role_accessor_properties(self, base_kwargs):
        """The .claimant_wallet / .auditor_wallet / .juror_wallets helpers
        return the same dicts present in self._wallets."""
        s = HappyPathScenario(**base_kwargs, jury_size=5)

        assert s.claimant_wallet is s._wallets[ROLE_CLAIMANT]
        assert s.auditor_wallet is s._wallets[ROLE_AUDITOR]
        jurors = s.juror_wallets
        assert len(jurors) == 5
        for i, jw in enumerate(jurors):
            assert jw is s._wallets[f"{ROLE_JUROR_PREFIX}_{i}"]


class TestWalletDerivationDeterminism:
    def test_same_seed_yields_same_wallets(self, base_kwargs):
        """Re-instantiating with identical inputs yields identical role seeds."""
        s1 = HappyPathScenario(**base_kwargs)
        s2 = HappyPathScenario(**base_kwargs)

        assert s1._wallets.keys() == s2._wallets.keys()
        for role in s1._wallets:
            assert s1._wallets[role]["seed"] == s2._wallets[role]["seed"]
            assert s1._wallets[role]["vkh_hex"] == s2._wallets[role]["vkh_hex"]

    def test_different_rng_seed_yields_different_wallets(self, base_kwargs):
        """Same name + same master_skey + DIFFERENT rng_seed -> different seeds."""
        s1 = HappyPathScenario(**base_kwargs)
        kwargs2 = dict(base_kwargs)
        kwargs2["rng_seed"] = 999
        s2 = HappyPathScenario(**kwargs2)

        for role in s1._wallets:
            assert s1._wallets[role]["seed"] != s2._wallets[role]["seed"], (
                f"role {role!r}: rng_seed mismatch should change seed"
            )

    def test_different_master_skey_yields_different_wallets(
        self, base_kwargs, fake_master_skey_bytes_alt
    ):
        """Same name + same rng_seed + DIFFERENT master skey -> different seeds."""
        s1 = HappyPathScenario(**base_kwargs)
        kwargs2 = dict(base_kwargs)
        kwargs2["master_skey"] = fake_master_skey_bytes_alt
        s2 = HappyPathScenario(**kwargs2)

        for role in s1._wallets:
            assert s1._wallets[role]["seed"] != s2._wallets[role]["seed"], (
                f"role {role!r}: different master skey should change seed"
            )


class TestWalletDerivationCollisionFree:
    def test_different_scenario_names_yield_disjoint_vkhs(self, base_kwargs):
        """Two scenarios with different `name` MUST NOT share a vkh on any role.

        This is the load-bearing invariant for concurrent scenarios on the
        same master wallet — collisions would burn each other's UTxOs.
        """
        kwargs_a = dict(base_kwargs, name="scenario_alpha")
        kwargs_b = dict(base_kwargs, name="scenario_beta")
        sa = HappyPathScenario(**kwargs_a)
        sb = HappyPathScenario(**kwargs_b)

        vkhs_a = {w["vkh_hex"] for w in sa._wallets.values()}
        vkhs_b = {w["vkh_hex"] for w in sb._wallets.values()}
        assert vkhs_a.isdisjoint(vkhs_b), (
            f"vkh collision across scenario names: {vkhs_a & vkhs_b}"
        )

    def test_no_internal_collision_within_scenario(self, base_kwargs):
        """Within ONE scenario, every role MUST have a unique vkh (no role
        accidentally derives the same key as another role)."""
        s = HappyPathScenario(**base_kwargs, jury_size=5)
        vkhs = [w["vkh_hex"] for w in s._wallets.values()]
        assert len(vkhs) == len(set(vkhs)), (
            f"intra-scenario vkh collision: {vkhs}"
        )

    def test_seeds_are_32_bytes(self, base_kwargs):
        """Derived seeds MUST be 32 bytes — required by ed25519 / pycardano
        PaymentSigningKey()."""
        s = HappyPathScenario(**base_kwargs)
        for role, w in s._wallets.items():
            assert isinstance(w["seed"], (bytes, bytearray)), role
            assert len(w["seed"]) == 32, f"{role}: {len(w['seed'])} bytes"


class TestDeriveRoleSeedHelper:
    """Direct tests for the pure derivation helper — locks the contract that
    Catherine's GREEN must NOT break (any HD derivation refactor must keep
    these invariants)."""

    def test_deterministic(self, fake_master_skey_bytes):
        a = _derive_role_seed(fake_master_skey_bytes, "scn", 7, ROLE_CLAIMANT)
        b = _derive_role_seed(fake_master_skey_bytes, "scn", 7, ROLE_CLAIMANT)
        assert a == b
        assert len(a) == 32

    def test_role_changes_seed(self, fake_master_skey_bytes):
        a = _derive_role_seed(fake_master_skey_bytes, "scn", 7, ROLE_CLAIMANT)
        b = _derive_role_seed(fake_master_skey_bytes, "scn", 7, ROLE_AUDITOR)
        assert a != b

    def test_name_changes_seed(self, fake_master_skey_bytes):
        a = _derive_role_seed(fake_master_skey_bytes, "alpha", 7, ROLE_CLAIMANT)
        b = _derive_role_seed(fake_master_skey_bytes, "beta", 7, ROLE_CLAIMANT)
        assert a != b

    def test_rng_seed_changes_seed(self, fake_master_skey_bytes):
        a = _derive_role_seed(fake_master_skey_bytes, "scn", 1, ROLE_CLAIMANT)
        b = _derive_role_seed(fake_master_skey_bytes, "scn", 2, ROLE_CLAIMANT)
        assert a != b

    def test_master_skey_changes_seed(
        self, fake_master_skey_bytes, fake_master_skey_bytes_alt
    ):
        a = _derive_role_seed(fake_master_skey_bytes, "scn", 7, ROLE_CLAIMANT)
        b = _derive_role_seed(fake_master_skey_bytes_alt, "scn", 7, ROLE_CLAIMANT)
        assert a != b


class TestCheckpointPayloadShape:
    """Lock the JSON-serialisable shape of the subclass checkpoint payload.
    This makes sure restart-mid-lifecycle (Section B) has well-defined state
    to round-trip."""

    def test_checkpoint_payload_initial_shape(self, base_kwargs):
        s = HappyPathScenario(**base_kwargs)
        payload = s._checkpoint_payload()
        assert set(payload.keys()) == {
            "step", "claim_ref", "claim_token_hex",
            "challenge_ref", "tx_hashes", "verdict",
        }
        # All JSON-serialisable.
        encoded = json.dumps(payload)
        assert json.loads(encoded) == payload

    def test_restore_payload_round_trip(self, base_kwargs):
        s1 = HappyPathScenario(**base_kwargs)
        s1._step = "resolve_jury"
        s1._claim_ref = "ab" * 32 + "#0"
        s1._claim_token_hex = "cd" * 28
        s1._challenge_ref = "ef" * 32 + "#0"
        s1._tx_hashes = {"submit_claim": "11" * 32}
        s1._verdict = "ClaimerWins"

        payload = s1._checkpoint_payload()

        s2 = HappyPathScenario(**base_kwargs)
        s2._restore_payload(payload)
        assert s2._step == "resolve_jury"
        assert s2._claim_ref == "ab" * 32 + "#0"
        assert s2._claim_token_hex == "cd" * 28
        assert s2._challenge_ref == "ef" * 32 + "#0"
        assert s2._tx_hashes == {"submit_claim": "11" * 32}
        assert s2._verdict == "ClaimerWins"

    def test_full_checkpoint_via_base_class_round_trip(self, base_kwargs):
        """The base-class checkpoint() persists subclass payload under
        "custom" — restore on a fresh instance reconstructs lifecycle state."""
        s1 = HappyPathScenario(**base_kwargs)
        s1._step = "commit_vote"
        s1._claim_ref = "aa" * 32 + "#0"
        s1.checkpoint()  # writes <ckpt_dir>/hp_test.json

        s2 = HappyPathScenario(**base_kwargs)
        assert s2.restore() is True
        assert s2._step == "commit_vote"
        assert s2._claim_ref == "aa" * 32 + "#0"


class TestDecideAndActIsAbstract:
    """Confirm that the lifecycle hook is RED — Catherine's GREEN target."""

    def test_decide_and_act_for_epoch_raises_not_implemented(self, base_kwargs):
        s = HappyPathScenario(**base_kwargs)
        with pytest.raises(NotImplementedError):
            s.decide_and_act_for_epoch(0)

    @pytest.mark.parametrize("step_method", [
        "_step_submit_claim",
        "_step_open_challenge",
        "_step_transition_to_voting",
        "_step_select_jury",
        "_step_resolve_jury",
        "_step_cleanup_resolved",
    ])
    def test_step_helpers_raise_not_implemented(self, base_kwargs, step_method):
        s = HappyPathScenario(**base_kwargs)
        with pytest.raises(NotImplementedError):
            getattr(s, step_method)(0)

    @pytest.mark.parametrize("step_method", [
        "_step_commit_vote",
        "_step_reveal_vote",
        "_step_distribute_rewards",
    ])
    def test_per_juror_step_helpers_raise_not_implemented(
        self, base_kwargs, step_method
    ):
        s = HappyPathScenario(**base_kwargs)
        with pytest.raises(NotImplementedError):
            getattr(s, step_method)(0, 0)


# ═══════════════════════════════════════════════════════════════════════════
# B. Live testnet tests (`@pytest.mark.testnet @pytest.mark.slow`)
#
# RED until Catherine implements the lifecycle. SKIPPED by default.
#
# IMPORTANT: these tests assume the v15 sim deployment exists on Vector
# testnet at the manifest path above. They will be authoritative once GREEN.
# ═══════════════════════════════════════════════════════════════════════════


# Helpers for live tests --------------------------------------------------


def _expected_event_sequence(jury_size: int = 5) -> list[str]:
    """The ordered list of *_success event types the happy path must emit."""
    seq = [
        "submit_claim_success",
        "open_challenge_success",
        "transition_to_voting_success",
        "select_jury_success",
    ]
    seq += ["commit_vote_success"] * jury_size
    seq += ["reveal_vote_success"] * jury_size
    seq += ["resolve_jury_success"]
    seq += ["distribute_rewards_success"] * jury_size
    seq += ["cleanup_resolved_success"]
    return seq


def _read_jsonl(path: Path) -> list[dict]:
    """Read a metrics JSONL file, skipping malformed lines."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _filter_event_sequence(events: list[dict]) -> list[str]:
    """Pull out the *_success event_type values in order, dropping noise."""
    expected = set(_expected_event_sequence(jury_size=99))  # superset of names
    return [e["event_type"] for e in events
            if e.get("event_type") in expected]


def _parse_ts(ts: Any) -> float:
    """Parse a metrics-event `ts` field into a float epoch seconds.

    Catherine's metrics emit ISO-8601 strings (e.g. "2026-04-16T12:34:56.789Z"
    or with offset). We accept both ISO strings and bare numerics so the test
    is robust to either representation.
    """
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        from datetime import datetime
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    raise TypeError(f"unrecognised ts type: {type(ts).__name__} ({ts!r})")


def _find_setup_complete(events: list[dict]) -> dict:
    """Return the single setup_complete event from a metrics JSONL stream.

    Fails the calling test if zero or >1 setup_complete events are present;
    Catherine's contract is exactly one per scenario lifecycle.
    """
    matches = [e for e in events if e.get("event_type") == "setup_complete"]
    assert len(matches) == 1, (
        f"expected exactly one setup_complete event, got {len(matches)}: {matches}"
    )
    return matches[0]


@pytest.fixture
def real_master_skey():
    """Load the actual testnet master skey. Skips if the file is absent."""
    if not MASTER_SKEY_PATH.exists():
        pytest.skip(f"master skey not present at {MASTER_SKEY_PATH}")
    from pycardano import PaymentSigningKey
    return PaymentSigningKey.load(str(MASTER_SKEY_PATH))


@pytest.fixture
def real_master_vkey(real_master_skey):
    from pycardano import PaymentVerificationKey
    return PaymentVerificationKey.from_signing_key(real_master_skey)


@pytest.fixture
def real_master_addr(real_master_vkey):
    from pycardano import Address
    from simulation.config import NETWORK
    return Address(real_master_vkey.hash(), network=NETWORK)


@pytest.fixture
def v15_deployment():
    if not V15_DEPLOYMENT_PATH.exists():
        pytest.skip(f"v15 deployment manifest not present at {V15_DEPLOYMENT_PATH}")
    return json.loads(V15_DEPLOYMENT_PATH.read_text())


@pytest.fixture
def live_kwargs(tmp_path, real_master_skey, real_master_vkey,
                real_master_addr, v15_deployment):
    return dict(
        name=f"happy_live_{int(time.time())}",
        config={"epochs_per_day": 24, "n_agents": 7},
        deployment=v15_deployment,
        master_skey=real_master_skey,
        master_vkey=real_master_vkey,
        master_wallet_addr=real_master_addr,
        checkpoint_dir=tmp_path / "checkpoints",
        metrics_dir=tmp_path / "metrics",
        rng_seed=20260416,
    )


# Live tests --------------------------------------------------------------


# Module-level timeouts. Setup phase (DID registration + funding + juror UTxO
# creation) gets its own budget — it does NOT count against the lifecycle
# timeout. Catherine emits a `setup_complete` event when the setup phase has
# fully confirmed on-chain; the lifecycle timer starts FROM THAT EVENT.
SETUP_TIMEOUT_SECONDS = 10 * 60   # 10 min for prerequisite TXes to confirm.
LIFECYCLE_TIMEOUT_SECONDS = 16 * 60  # 16 min from setup_complete to cleanup
                                     # (bumped from 8 min — concurrent
                                     # scenarios sharing master wallet
                                     # serialize heavily on UTxO selection;
                                     # 8 min was observed insufficient on
                                     # testnet 2026-04-22).


@pytest.mark.testnet
@pytest.mark.slow
class TestHappyPathLive:
    """End-to-end happy path against Vector testnet v15 sim deployment."""

    # Re-export module constants on the class for backward-compat with any
    # callers that referenced TestHappyPathLive.LIFECYCLE_TIMEOUT_SECONDS.
    SETUP_TIMEOUT_SECONDS = SETUP_TIMEOUT_SECONDS
    LIFECYCLE_TIMEOUT_SECONDS = LIFECYCLE_TIMEOUT_SECONDS

    def test_full_lifecycle_emits_all_event_types_in_order(self, live_kwargs):
        """Run the scenario once; every lifecycle event_type appears in the
        metrics JSONL file in the correct order, AFTER setup_complete, and
        the cleanup_resolved_success event lands within
        LIFECYCLE_TIMEOUT_SECONDS of setup_complete."""
        s = HappyPathScenario(**live_kwargs, jury_size=5)

        # Drive enough epochs for the scenario to walk all 9 lifecycle steps
        # plus per-juror commit/reveal/distribute (so 4 + 3*5 + 2 = 21 ticks
        # is a safe upper bound; loop runs until step == "done").
        s.run(n_epochs=64)

        events = _read_jsonl(s.metrics_path)
        actual = _filter_event_sequence(events)
        expected = _expected_event_sequence(jury_size=5)
        assert actual == expected, (
            f"event sequence mismatch.\n  expected: {expected}\n  actual:   {actual}"
        )

        # Setup-vs-lifecycle timing contract: every lifecycle *_success event
        # must occur AFTER setup_complete, and the final cleanup must land
        # within LIFECYCLE_TIMEOUT_SECONDS of setup_complete.
        setup = _find_setup_complete(events)
        ts_setup = _parse_ts(setup["ts"])
        lifecycle = [e for e in events
                     if e.get("event_type") in set(_expected_event_sequence(99))]
        assert lifecycle, "no lifecycle events recorded"
        for e in lifecycle:
            assert _parse_ts(e["ts"]) > ts_setup, (
                f"lifecycle event {e.get('event_type')!r} at {e.get('ts')!r} "
                f"precedes setup_complete at {setup['ts']!r}"
            )
        cleanup = [e for e in lifecycle
                   if e["event_type"] == "cleanup_resolved_success"][-1]
        delta = _parse_ts(cleanup["ts"]) - ts_setup
        assert delta <= LIFECYCLE_TIMEOUT_SECONDS, (
            f"lifecycle took {delta:.1f}s from setup_complete to "
            f"cleanup_resolved_success; budget is {LIFECYCLE_TIMEOUT_SECONDS}s"
        )

    def test_final_verdict_event_present(self, live_kwargs):
        """A {'event_type':'verdict', 'winner': ...} event closes the run,
        and it occurs AFTER setup_complete (within the lifecycle budget)."""
        s = HappyPathScenario(**live_kwargs, jury_size=5)
        s.run(n_epochs=64)

        events = _read_jsonl(s.metrics_path)
        verdicts = [e for e in events if e.get("event_type") == "verdict"]
        assert len(verdicts) == 1
        assert verdicts[0].get("winner") in ("claimer", "auditor")

        setup = _find_setup_complete(events)
        ts_setup = _parse_ts(setup["ts"])
        ts_verdict = _parse_ts(verdicts[0]["ts"])
        assert ts_verdict > ts_setup
        assert ts_verdict - ts_setup <= LIFECYCLE_TIMEOUT_SECONDS

    def test_no_scenario_error_in_happy_path(self, live_kwargs):
        """Happy path must not produce any scenario_error events."""
        s = HappyPathScenario(**live_kwargs, jury_size=5)
        s.run(n_epochs=64)

        events = _read_jsonl(s.metrics_path)
        errors = [e for e in events if e.get("event_type") == "scenario_error"]
        assert errors == [], f"unexpected errors: {errors}"

        # Sanity: setup_complete still emitted exactly once even on the no-
        # error path; without it the lifecycle-timer contract is moot.
        _find_setup_complete(events)

    def test_each_step_records_tx_hash(self, live_kwargs):
        """Every *_success event carries a non-empty tx_hash field, and each
        such event occurs AFTER setup_complete (lifecycle TXes only)."""
        s = HappyPathScenario(**live_kwargs, jury_size=5)
        s.run(n_epochs=64)

        events = _read_jsonl(s.metrics_path)
        success = [e for e in events
                   if e.get("event_type", "").endswith("_success")
                   and e.get("event_type") != "setup_complete"]
        assert success, "no *_success events emitted"
        setup = _find_setup_complete(events)
        ts_setup = _parse_ts(setup["ts"])
        for e in success:
            tx = e.get("tx_hash")
            assert isinstance(tx, str) and len(tx) >= 32, (
                f"event {e.get('event_type')} missing/short tx_hash: {tx!r}"
            )
            assert _parse_ts(e["ts"]) > ts_setup, (
                f"lifecycle event {e.get('event_type')!r} preceded setup_complete"
            )


@pytest.mark.testnet
@pytest.mark.slow
class TestHappyPathRestart:
    """Mid-lifecycle kill/restart: the scenario resumes from the last
    checkpointed step and completes the rest of the lifecycle without
    double-spending any already-submitted TX."""

    def test_kill_after_transition_to_voting_then_resume_completes(
        self, live_kwargs
    ):
        # Phase 1: drive partway through the lifecycle, then stop.
        # The watcher's deadline timer starts only AFTER setup_complete is
        # observed in the metrics JSONL — setup may take several minutes and
        # MUST NOT count against the lifecycle / restart window.
        s1 = HappyPathScenario(**live_kwargs, jury_size=5)
        stop_evt = threading.Event()

        def _setup_complete_seen() -> bool:
            for e in _read_jsonl(s1.metrics_path):
                if e.get("event_type") == "setup_complete":
                    return True
            return False

        def stop_when_voting_reached():
            # Wait (up to SETUP_TIMEOUT_SECONDS) for the setup phase to
            # finish before arming the lifecycle watch; the restart timer
            # must NOT fire during setup.
            setup_deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
            while time.monotonic() < setup_deadline:
                if _setup_complete_seen():
                    break
                time.sleep(1.0)
            else:
                # Setup never completed; let the run finish on its own and
                # the assertions below will surface the failure clearly.
                return

            # Now poll lifecycle state with a budget tied to lifecycle only.
            deadline = time.monotonic() + LIFECYCLE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if s1._step in (
                    "select_jury", "commit_vote", "reveal_vote",
                    "resolve_jury", "distribute_rewards",
                    "cleanup_resolved", "done",
                ):
                    stop_evt.set()
                    return
                time.sleep(0.5)

        watcher = threading.Thread(target=stop_when_voting_reached, daemon=True)
        watcher.start()
        s1.run(n_epochs=64, stop_event=stop_evt)
        watcher.join(timeout=1.0)

        # Mid-lifecycle invariants — partial progress was checkpointed AFTER
        # setup_complete was emitted.
        assert s1._claim_ref is not None
        assert s1._challenge_ref is not None
        assert s1.checkpoint_path.exists()
        assert _setup_complete_seen(), (
            "setup_complete must have been emitted before the run was stopped"
        )

        # Phase 2: a fresh instance restores the checkpoint and finishes.
        s2 = HappyPathScenario(**live_kwargs, jury_size=5)
        assert s2.restore() is True
        assert s2._claim_ref == s1._claim_ref
        assert s2._challenge_ref == s1._challenge_ref

        # No double-submit: the resumed scenario's tx_hashes for already-
        # completed steps must match the originals.
        original_hashes = dict(s1._tx_hashes)
        s2.run(n_epochs=64)
        for step, tx in original_hashes.items():
            assert s2._tx_hashes.get(step) == tx, (
                f"step {step}: original tx={tx!r}, after-resume tx={s2._tx_hashes.get(step)!r}"
            )

        # Final state must be done with a verdict.
        assert s2._step == "done"
        assert s2._verdict in ("ClaimerWins", "AuditorWins")


@pytest.mark.testnet
@pytest.mark.slow
class TestHappyPathConcurrent:
    """Two HappyPathScenarios on different challenges driven by SimOrchestrator
    must both complete with no UTxO collision and write to separate metrics."""

    def test_two_concurrent_scenarios_both_complete(self, tmp_path,
                                                    real_master_skey,
                                                    real_master_vkey,
                                                    real_master_addr,
                                                    v15_deployment):
        from simulation.orchestrator import SimOrchestrator

        common = dict(
            config={"epochs_per_day": 24, "n_agents": 7},
            deployment=v15_deployment,
            master_skey=real_master_skey,
            master_vkey=real_master_vkey,
            master_wallet_addr=real_master_addr,
            checkpoint_dir=tmp_path / "checkpoints",
            metrics_dir=tmp_path / "metrics",
        )
        s_alpha = HappyPathScenario(
            name="hp_alpha", rng_seed=1001, jury_size=5, **common
        )
        s_beta = HappyPathScenario(
            name="hp_beta", rng_seed=2002, jury_size=5, **common
        )

        # Pre-flight: derived wallets must be disjoint (this is a property
        # of construction, but we re-assert here as a guard against any
        # late-stage refactor breaking the invariant).
        a_vkhs = {w["vkh_hex"] for w in s_alpha._wallets.values()}
        b_vkhs = {w["vkh_hex"] for w in s_beta._wallets.values()}
        assert a_vkhs.isdisjoint(b_vkhs)

        orch = SimOrchestrator(
            checkpoint_dir=tmp_path / "orch_ckpts",
            metrics_dir=tmp_path / "orch_metrics",
        )
        orch.register(s_alpha)
        orch.register(s_beta)
        orch.start_all(n_epochs=64)
        # Two scenarios sharing a master wallet pay setup cost serially in
        # the worst case, so allow 2 * SETUP + LIFECYCLE for the orchestrator.
        ok = orch.wait(timeout=2 * SETUP_TIMEOUT_SECONDS + LIFECYCLE_TIMEOUT_SECONDS)
        assert ok, (
            "scenarios did not finish within "
            f"{2 * SETUP_TIMEOUT_SECONDS + LIFECYCLE_TIMEOUT_SECONDS}s "
            "(2*setup + lifecycle budget)"
        )

        # Both scenarios reach 'done' with a verdict.
        assert s_alpha._step == "done"
        assert s_beta._step == "done"
        assert s_alpha._verdict in ("ClaimerWins", "AuditorWins")
        assert s_beta._verdict in ("ClaimerWins", "AuditorWins")

        # Separate metrics files (not corrupted/interleaved).
        assert s_alpha.metrics_path != s_beta.metrics_path
        events_a = _read_jsonl(s_alpha.metrics_path)
        events_b = _read_jsonl(s_beta.metrics_path)
        ev_a = _filter_event_sequence(events_a)
        ev_b = _filter_event_sequence(events_b)
        expected = _expected_event_sequence(jury_size=5)
        assert ev_a == expected
        assert ev_b == expected

        # Per-scenario lifecycle timing: each scenario's lifecycle delta
        # (setup_complete -> cleanup_resolved_success) is bounded by
        # LIFECYCLE_TIMEOUT_SECONDS independently. Setup costs do NOT count.
        for label, evs in (("alpha", events_a), ("beta", events_b)):
            setup = _find_setup_complete(evs)
            ts_setup = _parse_ts(setup["ts"])
            cleanup = [e for e in evs
                       if e.get("event_type") == "cleanup_resolved_success"][-1]
            delta = _parse_ts(cleanup["ts"]) - ts_setup
            assert delta <= LIFECYCLE_TIMEOUT_SECONDS, (
                f"scenario {label}: lifecycle delta {delta:.1f}s "
                f"exceeds budget {LIFECYCLE_TIMEOUT_SECONDS}s"
            )
            # All lifecycle events in this scenario succeed setup_complete.
            for e in evs:
                if e.get("event_type") in set(_expected_event_sequence(99)):
                    assert _parse_ts(e["ts"]) > ts_setup, (
                        f"scenario {label}: lifecycle event "
                        f"{e.get('event_type')!r} preceded setup_complete"
                    )

        # No UTxO collision: claim_refs differ.
        assert s_alpha._claim_ref != s_beta._claim_ref


@pytest.mark.testnet
@pytest.mark.slow
class TestHappyPathBalances:
    """After the lifecycle, on-chain balances move as expected: jury fees
    distributed, claimer stake returned (or paid out to auditor depending
    on verdict), auditor fee paid out (or stake forfeited)."""

    # Per-scenario master-wallet spend budget: setup phase consumes ~110 ADA
    # of master ADA (DID registrations + funding TXes + juror UTxO creation
    # + per-role base-coin stakes). Use a generous ~150 ADA tolerance so the
    # assertion does not become flaky on fee drift.
    MASTER_SPEND_TOLERANCE_LOVELACE = 150_000_000  # 150 ADA per scenario

    def test_balance_shifts_match_verdict(self, live_kwargs):
        """After completion, every juror's wallet has at least one new UTxO
        from DistributeRewards, and either claimant or auditor's wallet
        reflects the verdict outcome.

        Catherine: this test queries Ogmios for each agent's UTxOs after the
        run completes, computes the delta vs. the pre-run snapshot, and
        asserts the shape (per spec). Implementation requires the chain
        helpers from simulation/chain.py — DO NOT mock.

        Master-wallet note: the setup phase (DID registration + funding +
        juror UTxO creation + base-coin stake bonds) is now expected to
        consume up to ~150 ADA from the master wallet per scenario. The
        tolerance below intentionally bounds the spend rather than requiring
        a tight equality.
        """
        from simulation.chain import OgmiosContext

        s = HappyPathScenario(**live_kwargs, jury_size=5)

        # Snapshot per-role balances BEFORE the run.
        ctx = OgmiosContext()
        before: dict[str, int] = {}
        for role, w in s._wallets.items():
            addr = w.get("address")
            if addr is None:
                # Catherine: in GREEN, _derive_wallets must populate `address`.
                pytest.fail(
                    f"role {role!r} has no .address — Catherine must extend "
                    "wallet_factory.py with master-seeded derivation that "
                    "produces a real Address. See FLAG in happy_path.py."
                )
            before[role] = sum(int(u.output.amount.coin) for u in ctx.utxos(addr))

        # Snapshot the master wallet BEFORE the run for the setup-spend bound.
        master_before = sum(int(u.output.amount.coin)
                            for u in ctx.utxos(s.master_wallet_addr))

        s.run(n_epochs=64)

        # AFTER the run: every juror's coin balance MUST have grown (jury fee).
        after: dict[str, int] = {}
        for role, w in s._wallets.items():
            addr = w["address"]
            after[role] = sum(int(u.output.amount.coin) for u in ctx.utxos(addr))

        for i in range(s.jury_size):
            role = f"{ROLE_JUROR_PREFIX}_{i}"
            assert after[role] > before[role], (
                f"juror {role}: balance did not increase "
                f"(before={before[role]}, after={after[role]})"
            )

        # Verdict-conditional invariant for claimant vs. auditor:
        if s._verdict == "ClaimerWins":
            # Claimant gets their stake back; auditor forfeits theirs.
            assert after[ROLE_CLAIMANT] >= before[ROLE_CLAIMANT]
            assert after[ROLE_AUDITOR] < before[ROLE_AUDITOR]
        elif s._verdict == "AuditorWins":
            assert after[ROLE_AUDITOR] >= before[ROLE_AUDITOR]
            assert after[ROLE_CLAIMANT] < before[ROLE_CLAIMANT]
        else:
            pytest.fail(f"unexpected verdict: {s._verdict!r}")

        # Master-spend bound: setup + lifecycle should not consume more than
        # MASTER_SPEND_TOLERANCE_LOVELACE per scenario.
        master_after = sum(int(u.output.amount.coin)
                           for u in ctx.utxos(s.master_wallet_addr))
        spent = master_before - master_after
        assert spent <= self.MASTER_SPEND_TOLERANCE_LOVELACE, (
            f"master wallet spent {spent} lovelace (~{spent/1_000_000:.1f} ADA), "
            f"exceeds tolerance "
            f"{self.MASTER_SPEND_TOLERANCE_LOVELACE} (~150 ADA)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Setup-phase contract tests (added per Chuck's amend — RED until 3b)
#
# These pin the setup_complete event contract that decouples setup time from
# the lifecycle timeout, and the idempotency requirement on restart.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.testnet
@pytest.mark.slow
class TestHappyPathSetupContract:
    """Pin Catherine's setup_complete contract."""

    def test_setup_complete_event_emitted_before_any_lifecycle_event(
        self, live_kwargs
    ):
        """Strict ordering: the single setup_complete event MUST appear in the
        metrics JSONL strictly before the first submit_claim_success event,
        and before any other lifecycle *_success event.
        """
        s = HappyPathScenario(**live_kwargs, jury_size=5)
        s.run(n_epochs=64)

        events = _read_jsonl(s.metrics_path)
        setup = _find_setup_complete(events)
        ts_setup = _parse_ts(setup["ts"])

        # The setup_complete event also carries the agent_indices and scenario
        # name per Catherine's contract — pin those too.
        assert isinstance(setup.get("agent_indices"), list), setup
        assert setup.get("scenario") == s.name, setup

        # First lifecycle *_success must be submit_claim_success and must
        # come strictly after setup_complete.
        lifecycle_events = [
            e for e in events
            if e.get("event_type") in set(_expected_event_sequence(99))
        ]
        assert lifecycle_events, "no lifecycle *_success events recorded"
        first = lifecycle_events[0]
        assert first["event_type"] == "submit_claim_success", (
            f"first lifecycle event was {first['event_type']!r}, "
            "expected submit_claim_success"
        )
        assert _parse_ts(first["ts"]) > ts_setup, (
            f"submit_claim_success at {first['ts']!r} did not occur strictly "
            f"after setup_complete at {setup['ts']!r}"
        )

    def test_setup_phase_idempotent_on_restart(self, live_kwargs):
        """Killing a scenario AFTER setup_complete but BEFORE submit_claim
        and then restarting MUST NOT re-run the setup phase.

        Verified by:
          (1) Master ADA balance does NOT drop further between the kill and
              the resumed scenario's first lifecycle TX (i.e., no second
              wave of DID-registration / juror-funding TXes).
          (2) The metrics JSONL contains exactly ONE setup_complete event
              across both runs (the resumed run reuses the same metrics
              file path because the scenario name + dirs are identical).
        """
        from simulation.chain import OgmiosContext

        s1 = HappyPathScenario(**live_kwargs, jury_size=5)
        ctx = OgmiosContext()

        master_before = sum(int(u.output.amount.coin)
                            for u in ctx.utxos(s1.master_wallet_addr))

        stop_evt = threading.Event()

        def _setup_complete_seen() -> bool:
            for e in _read_jsonl(s1.metrics_path):
                if e.get("event_type") == "setup_complete":
                    return True
            return False

        def stop_after_setup_before_first_claim():
            # Wait up to SETUP_TIMEOUT for setup_complete; signal stop the
            # instant we observe it, BEFORE submit_claim_success can land.
            deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if _setup_complete_seen() and s1._step == "submit_claim":
                    stop_evt.set()
                    return
                time.sleep(0.5)

        watcher = threading.Thread(
            target=stop_after_setup_before_first_claim, daemon=True
        )
        watcher.start()
        s1.run(n_epochs=64, stop_event=stop_evt)
        watcher.join(timeout=1.0)

        assert _setup_complete_seen(), "setup_complete was not emitted"
        assert s1._step == "submit_claim", (
            f"expected to stop at submit_claim, ended at {s1._step!r}"
        )
        # No lifecycle TXes yet: tx_hashes for submit_claim must be absent.
        assert "submit_claim" not in s1._tx_hashes

        master_after_setup = sum(int(u.output.amount.coin)
                                 for u in ctx.utxos(s1.master_wallet_addr))
        setup_spend = master_before - master_after_setup
        # Sanity: setup did consume something.
        assert setup_spend > 0, (
            f"setup phase consumed nothing (before={master_before}, "
            f"after={master_after_setup})"
        )

        # Resume: a fresh instance restores from checkpoint; setup MUST be
        # skipped and the scenario picks up at submit_claim.
        s2 = HappyPathScenario(**live_kwargs, jury_size=5)
        assert s2.restore() is True
        assert s2._step == "submit_claim"
        assert s2.metrics_path == s1.metrics_path  # same file, append mode

        # Snapshot master balance immediately AFTER restore but BEFORE running.
        master_pre_resume = sum(int(u.output.amount.coin)
                                for u in ctx.utxos(s2.master_wallet_addr))

        s2.run(n_epochs=64)
        assert s2._step == "done"

        # Idempotency check (1): metrics file has exactly ONE setup_complete
        # across both runs.
        all_events = _read_jsonl(s2.metrics_path)
        setup_events = [
            e for e in all_events if e.get("event_type") == "setup_complete"
        ]
        assert len(setup_events) == 1, (
            f"expected exactly 1 setup_complete across both runs, "
            f"got {len(setup_events)}: {setup_events}"
        )

        # Idempotency check (2): between the kill and the FIRST resumed
        # lifecycle TX, master balance must not have leaked further setup
        # cost. Master balance can drop afterward due to lifecycle TX fees,
        # so we bound this by lifecycle-only spend (no second setup wave).
        # The setup phase Catherine emits costs ~110 ADA; lifecycle fees are
        # << 10 ADA. So if the resumed run consumed anywhere close to the
        # setup cost again, it re-ran setup — fail loudly.
        master_final = sum(int(u.output.amount.coin)
                           for u in ctx.utxos(s2.master_wallet_addr))
        resume_spend = master_pre_resume - master_final
        # Allow up to 20 ADA for lifecycle TX fees + change consolidation;
        # anything approaching the setup cost (~110 ADA) means setup re-ran.
        assert resume_spend < 20_000_000, (
            f"resumed run consumed {resume_spend} lovelace "
            f"(~{resume_spend/1_000_000:.1f} ADA) — looks like setup re-ran. "
            f"setup originally cost {setup_spend} lovelace."
        )


# ═══════════════════════════════════════════════════════════════════════════
# C. ResolvedParams threading (unit — NO @pytest.mark.testnet)
#
# Option A: the HappyPathScenario must read ProtocolParams off-chain via
# DeploymentState.resolved_params and thread the values through its
# lifecycle helpers (replacing the hardcoded `challenge_window_ms=240_000`
# at _step_submit_claim:1285 with `resolved_params.max_challenge_window`).
#
# These tests are RED against the current code because:
#   (a) simulation.params does not yet exist, and
#   (b) the scenario does not yet call resolve_protocol_params / stash
#       resolved_params on itself or read max_challenge_window from it.
#
# Catherine's GREEN landing the Option A stack (params.py + builder
# threading + scenario integration) flips these to green.
# ═══════════════════════════════════════════════════════════════════════════


class TestResolvedParamsThreading:
    """Unit tests for resolved_params flow inside HappyPathScenario.

    Uses an in-memory patch of simulation.params.resolve_protocol_params so
    the scenario's lazy resolver returns a known-sentinel ResolvedParams
    without touching the chain — these tests MUST stay unit-level (no
    testnet marker).
    """

    def test_scenario_caches_resolved_params_from_deployment_state(
        self, base_kwargs, monkeypatch,
    ):
        """``HappyPathScenario._resolved_params`` (or equivalent accessor)
        must yield the same ResolvedParams the DeploymentState resolves.

        Catherine's design: the scenario accesses resolved_params lazily
        through its DeploymentState cache. The exact attribute name is
        not locked here — we accept either ``s._resolved_params`` or
        ``s.resolved_params`` (both are reasonable conventions) as long
        as the value returned equals the stub.
        """
        from simulation.params import ResolvedParams
        import simulation.params as params_mod

        stub = ResolvedParams(
            min_claim_stake=77_000_001,
            min_challenge_window=91_234,
            max_challenge_window=199_876,
            jury_size=5,
            min_juror_bond=26_000_001,
            jury_fee_rate=1_500,
            selection_delay=88_888,
            resolution_deadline=555_555,
            juror_slash_rate=1_111,
            min_agent_age=12_345_678,
            max_concurrent_cases=5,
            min_jury_pool_size=15,
            min_jury_pool_total=380_000_000,
            oracle_active=False,
            commit_window=99_999,
            reveal_window=77_777,
            cleanup_buffer=33_333,
        )
        monkeypatch.setattr(
            params_mod, "resolve_protocol_params", lambda dep: stub,
        )

        s = HappyPathScenario(**base_kwargs, jury_size=5)

        # The scenario must expose the cached ResolvedParams either via a
        # public or underscored attribute. Accept either; both are
        # consistent with Catherine's design note.
        accessor = getattr(s, "_resolved_params", None)
        if accessor is None:
            accessor = getattr(s, "resolved_params", None)
        assert accessor is not None, (
            "HappyPathScenario must expose resolved_params (either "
            "``_resolved_params`` or ``resolved_params``) so lifecycle "
            "helpers can thread stub values into builders without "
            "re-resolving on every step."
        )
        resolved = accessor() if callable(accessor) else accessor
        assert resolved.min_challenge_window == stub.min_challenge_window
        assert resolved.max_challenge_window == stub.max_challenge_window
        assert resolved.commit_window == stub.commit_window
        assert resolved.reveal_window == stub.reveal_window
        assert resolved.selection_delay == stub.selection_delay
        assert resolved.jury_fee_rate == stub.jury_fee_rate
        assert resolved.resolution_deadline == stub.resolution_deadline
        assert resolved.cleanup_buffer == stub.cleanup_buffer

    def test_step_submit_claim_threads_max_challenge_window_from_resolved_params(
        self, base_kwargs, monkeypatch,
    ):
        """``_step_submit_claim`` must pass
        ``resolved_params.max_challenge_window`` (or the
        ``resolved_params`` object itself) to build_submit_claim — NOT
        the old hardcoded 240_000 ms.

        Approach: patch ``simulation.tx_builder.build_submit_claim`` to
        record its kwargs, stub out the rest of the scenario's lifecycle
        dependencies (master skey gate, OgmiosContext, wait_confirm,
        checkpoint), then invoke _step_submit_claim and assert on the
        captured kwargs.
        """
        from simulation.params import ResolvedParams
        import simulation.params as params_mod

        stub = ResolvedParams(
            min_claim_stake=50_000_000,
            min_challenge_window=60_000,
            max_challenge_window=222_222,   # distinct from old default 240_000
            jury_size=5,
            min_juror_bond=25_000_000,
            jury_fee_rate=1_000,
            selection_delay=30_000,
            resolution_deadline=600_000,
            juror_slash_rate=1_000,
            min_agent_age=21_600_000,
            max_concurrent_cases=5,
            min_jury_pool_size=15,
            min_jury_pool_total=375_000_000,
            oracle_active=False,
            commit_window=180_000,
            reveal_window=180_000,
            cleanup_buffer=60_000,
        )
        monkeypatch.setattr(
            params_mod, "resolve_protocol_params", lambda dep: stub,
        )

        # Scenario needs a real master skey to pass the _require_real_master_skey
        # gate. Build a throwaway real one.
        import hashlib
        from pycardano import PaymentSigningKey, PaymentVerificationKey, Address, Network
        real_seed = hashlib.blake2b(b"threading-test-master", digest_size=32).digest()
        real_skey = PaymentSigningKey(real_seed)
        real_vkey = PaymentVerificationKey.from_signing_key(real_skey)
        real_addr = Address(payment_part=real_vkey.hash(), network=Network.MAINNET)

        kwargs = dict(base_kwargs)
        kwargs["master_skey"] = real_skey
        kwargs["master_vkey"] = real_vkey
        kwargs["master_wallet_addr"] = real_addr

        s = HappyPathScenario(**kwargs, jury_size=5)

        # Prime the scenario's internal state so _step_submit_claim
        # survives the pre-checks (claimant DID registered, etc.).
        s._agent_did_hexes = {
            "claimant": "00" * 28,
            "auditor": "11" * 28,
        }
        for i in range(5):
            s._agent_did_hexes[f"juror_{i}"] = (bytes([i]) * 28).hex()

        # Patch out all the network-facing machinery inside _step_submit_claim:
        captured: dict = {}

        def fake_build_submit_claim(*args, **kwargs_local):
            captured["args"] = args
            captured["kwargs"] = kwargs_local
            return {
                "tx_hash": "aa" * 32,
                "claim_utxo_ref": "aa" * 32 + "#0",
                "claim_token_hex": "cc" * 28,
                "claim_hash": "bb" * 32,
                "submitted_at": 0,
                "submitted_at_ms": 0,
                "stake_amount": s.stake_amount,
                "claimer_did": s._agent_did_hexes["claimant"],
                "challenge_window_ms": kwargs_local.get("challenge_window_ms"),
            }

        import simulation.tx_builder as _txb
        import simulation.chain as _chain
        import simulation.scenarios.happy_path as _hp

        monkeypatch.setattr(_txb, "build_submit_claim", fake_build_submit_claim)
        monkeypatch.setattr(
            _hp, "WAIT_CONFIRM_SECS", 0,
        )

        class _FakeCtx:
            last_block_slot = 100_000_000

            def utxos(self, _addr):
                return []

        monkeypatch.setattr(_chain, "OgmiosContext", lambda *a, **kw: _FakeCtx())
        monkeypatch.setattr(_chain, "wait_confirm", lambda *a, **kw: None)
        monkeypatch.setattr(
            s, "_deployment_state", lambda: object(),
        )
        monkeypatch.setattr(s, "checkpoint", lambda: None)

        s._step_submit_claim(epoch=0)

        assert "kwargs" in captured, (
            "build_submit_claim was never called by _step_submit_claim"
        )
        kw = captured["kwargs"]

        # Accept EITHER of two valid threading strategies:
        #   (a) kwarg `challenge_window_ms` is the stub's max_challenge_window
        #   (b) kwarg `resolved_params` is the stub itself (builder derives
        #       the value internally, as the brief describes for other builders)
        ok = False
        if kw.get("challenge_window_ms") == stub.max_challenge_window:
            ok = True
        if kw.get("resolved_params") is stub:
            ok = True
        assert ok, (
            f"_step_submit_claim did NOT thread the stub's "
            f"max_challenge_window ({stub.max_challenge_window}) into "
            f"build_submit_claim. Got kwargs={kw!r}. Either pass "
            f"challenge_window_ms=resolved_params.max_challenge_window "
            f"or pass resolved_params=... (builder defaults to the "
            f"resolved value)."
        )

        # Explicit regression: MUST NOT be the old hardcoded 240_000 ms.
        assert kw.get("challenge_window_ms") != 240_000, (
            "_step_submit_claim still passes the hardcoded 240_000 ms "
            "default — the Option A refactor has not landed at this site."
        )


# ═══════════════════════════════════════════════════════════════════════════
# Resolve-jury submit-retry regression (Sim Phase 2 hot-fix, 2026-04-23)
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveJurySubmitRetry:
    """Regression coverage for the `_step_resolve_jury` submit-retry loop.

    Mainnet AuditorWins runs (1 PASS / 6 FAIL = 14 % success on
    2026-04-23) repeatedly hit an evaluate/submit divergence inside
    build_resolve_jury — preflight evaluateTransaction returns OK, then
    submitTransaction immediately rejects with PlutusFailure under
    IsValid True (chain-state drift between the two Ogmios calls).
    Chuck-approved fix: rebuild the TX from fresh inputs and retry,
    bounded by attempts and the on-chain resolution_deadline window.

    These unit tests pin the contract:
      1. On a divergence-shaped RuntimeError, the step retries.
      2. Each retry rebuilds the TX (build_resolve_jury called again).
      3. An 8 s sleep separates attempts.
      4. A `resolve_jury_retry` event is emitted per retry so the
         metrics JSONL stream surfaces the fact a retry happened.
      5. Non-divergence RuntimeErrors bubble unchanged (no retry).
    """

    def _stub_resolved_params(self, monkeypatch, resolution_deadline=600_000):
        from simulation.params import ResolvedParams
        import simulation.params as params_mod

        stub = ResolvedParams(
            min_claim_stake=50_000_000,
            min_challenge_window=60_000,
            max_challenge_window=240_000,
            jury_size=5,
            min_juror_bond=25_000_000,
            jury_fee_rate=1_000,
            selection_delay=30_000,
            resolution_deadline=resolution_deadline,
            juror_slash_rate=1_000,
            min_agent_age=21_600_000,
            max_concurrent_cases=5,
            min_jury_pool_size=15,
            min_jury_pool_total=375_000_000,
            oracle_active=False,
            commit_window=180_000,
            reveal_window=180_000,
            cleanup_buffer=60_000,
        )
        monkeypatch.setattr(
            params_mod, "resolve_protocol_params", lambda dep: stub,
        )
        return stub

    def _make_scenario(self, base_kwargs):
        """Build a scenario with a real master skey and primed lifecycle
        state so _step_resolve_jury bypasses pre-checks and runs the
        retry loop body."""
        import hashlib
        from pycardano import (
            Address, Network, PaymentSigningKey, PaymentVerificationKey,
        )

        seed = hashlib.blake2b(
            b"resolve-jury-retry-test", digest_size=32,
        ).digest()
        skey = PaymentSigningKey(seed)
        vkey = PaymentVerificationKey.from_signing_key(skey)
        addr = Address(payment_part=vkey.hash(), network=Network.MAINNET)

        kwargs = dict(base_kwargs)
        kwargs["master_skey"] = skey
        kwargs["master_vkey"] = vkey
        kwargs["master_wallet_addr"] = addr

        s = HappyPathScenario(**kwargs, jury_size=5)

        # Lifecycle state required by _step_resolve_jury.
        s._challenge_ref = "ab" * 32 + "#0"
        s._claim_ref = "cd" * 32 + "#0"
        s._selected_pool_indices = [0, 1, 2, 3, 4]
        s._juror_utxo_refs = [
            (bytes([i]).hex().rjust(2, "0") * 32) + f"#{i}"
            for i in range(15)
        ]
        return s

    def _patch_chain_and_params(
        self, monkeypatch, sleep_calls, challenged_at_ms=1_500_000,
    ):
        """Replace OgmiosContext, resolve_utxo, wait_confirm, and time.sleep
        with stubs that mimic a healthy chain view (enough to compute the
        deadline guard and let the retry loop exercise its branches)."""
        import simulation.chain as _chain
        import simulation.scenarios.happy_path as _hp

        # SYSTEM_START_UNIX is read inside the step. The scenario computes
        # now_ms from (SYSTEM_START_UNIX + ctx.last_block_slot) * 1000 and
        # compares against challenged_at_ms + resolution_deadline. We pin
        # both to anchor "plenty of window remaining" for the retry path.
        monkeypatch.setattr(_chain, "SYSTEM_START_UNIX", 0, raising=False)

        class _FakeCtx:
            last_block_slot = 1_000   # now_ms = 1_000_000

        monkeypatch.setattr(_chain, "OgmiosContext", lambda *a, **kw: _FakeCtx())
        monkeypatch.setattr(_chain, "wait_confirm", lambda *a, **kw: None)

        # Default: challenged_at = 1_500_000 ms ⇒ deadline_abs =
        # 1_500_000 + resolution_deadline. Combined with last_block_slot
        # = 1_000 (now_ms = 1_000_000), the default 600_000 deadline
        # leaves 1_100_000 ms remaining — well above the 30 s floor —
        # so the retry loop is free to retry. Tests that exercise the
        # deadline-floor abort path override challenged_at_ms.

        class _FakeDatumValue:
            value = [None] * 10  # field[6] populated below
            tag = 121

        class _FakeDatum:
            cbor = None  # the fake datum has no cbor attribute path

        class _FakeOutput:
            datum = _FakeDatum()

        class _FakeUtxo:
            output = _FakeOutput()

        # We bypass the cbor2.loads path by patching cbor2 inside the step.
        # Easier: patch resolve_utxo to return a sentinel and patch cbor2.loads
        # at the module level used inside the step.
        fake_utxo = _FakeUtxo()
        # Datum.cbor must exist — give it raw bytes so the `hasattr(..., "cbor")`
        # branch picks it up. The actual contents are irrelevant because we
        # also stub cbor2.loads below.
        fake_utxo.output.datum.cbor = b"\x80"

        monkeypatch.setattr(
            _chain, "resolve_utxo", lambda *a, **kw: fake_utxo,
        )

        # Inside the step the datum CBOR is parsed via `cbor2.loads(...)`.
        # Stub it to return an object exposing .value[6] = challenged_at_ms.
        import cbor2 as _cbor2

        class _FakeLoaded:
            value = [0, 0, 0, 0, 0, 0, challenged_at_ms, 0, 0, 0]

        monkeypatch.setattr(_cbor2, "loads", lambda *a, **kw: _FakeLoaded())

        # Capture every sleep call — assertions check the 8 s gap between
        # retries.
        import time as _time
        monkeypatch.setattr(_time, "sleep", lambda secs: sleep_calls.append(secs))

        # Speed: avoid real waits anywhere else.
        monkeypatch.setattr(_hp, "WAIT_CONFIRM_SECS", 0)

        return _FakeCtx

    def test_retries_once_on_evaluate_submit_divergence(
        self, base_kwargs, monkeypatch,
    ):
        """First build_resolve_jury raises PlutusFailure; second succeeds.
        Step must complete with one retry, one 8 s sleep, and a
        resolve_jury_retry event in the returned event list."""
        self._stub_resolved_params(monkeypatch)
        sleep_calls: list = []
        self._patch_chain_and_params(monkeypatch, sleep_calls)

        s = self._make_scenario(base_kwargs)
        monkeypatch.setattr(s, "_deployment_state", lambda: object())
        monkeypatch.setattr(s, "checkpoint", lambda: None)

        call_count = {"n": 0}
        success_payload = {
            "tx_hash": "ee" * 32,
            "verdict": "AuditorWins",
            "resolved_challenge_ref": "ee" * 32 + "#0",
            "jury_fee": 1_000_000,
            "claimer_payout": None,
            "auditor_payout": 89_000_000,
        }

        def fake_build_resolve_jury(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Mimic the exact mainnet failure shape — RuntimeError whose
                # message contains both substrings the classifier matches.
                raise RuntimeError(
                    "Submit failed (400): "
                    "ConwayUtxowFailure (UtxoFailure (UtxosFailure "
                    "(ValidationTagMismatch (IsValid True) "
                    "(FailedUnexpectedly (PlutusFailure ...))))"
                )
            return dict(success_payload)

        import simulation.tx_builder as _txb
        monkeypatch.setattr(
            _txb, "build_resolve_jury", fake_build_resolve_jury,
        )

        events = s._step_resolve_jury(epoch=0)

        assert call_count["n"] == 2, (
            f"build_resolve_jury must be called twice (one fail + one "
            f"retry), got {call_count['n']} calls."
        )
        assert sleep_calls == [8], (
            f"Exactly one 8 s sleep must separate the two attempts. "
            f"Got sleep calls: {sleep_calls!r}."
        )

        retry_events = [
            e for e in events if e.get("event_type") == "resolve_jury_retry"
        ]
        assert len(retry_events) == 1, (
            f"Step must emit exactly one resolve_jury_retry event after "
            f"a single retry. Got events: "
            f"{[e.get('event_type') for e in events]!r}."
        )
        assert retry_events[0]["attempt"] == 1
        assert retry_events[0]["reason"] == "evaluate_submit_divergence"

        success_events = [
            e for e in events if e.get("event_type") == "resolve_jury_success"
        ]
        assert len(success_events) == 1
        assert success_events[0]["verdict"] == "AuditorWins"
        assert success_events[0]["tx_hash"] == "ee" * 32

        # Scenario state must reflect the successful attempt.
        assert s._verdict == "AuditorWins"
        assert s._step == "distribute_rewards"
        assert s._tx_hashes["resolve_jury"] == "ee" * 32

    def test_no_retry_on_unrelated_runtime_error(
        self, base_kwargs, monkeypatch,
    ):
        """A RuntimeError that is NOT the divergence pattern must bubble
        immediately — no sleep, no retry, no resolve_jury_retry event."""
        self._stub_resolved_params(monkeypatch)
        sleep_calls: list = []
        self._patch_chain_and_params(monkeypatch, sleep_calls)

        s = self._make_scenario(base_kwargs)
        monkeypatch.setattr(s, "_deployment_state", lambda: object())
        monkeypatch.setattr(s, "checkpoint", lambda: None)

        call_count = {"n": 0}

        def fake_build_resolve_jury(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError(
                "Submit failed (400): OutsideValidityIntervalUTxO ..."
            )

        import simulation.tx_builder as _txb
        monkeypatch.setattr(
            _txb, "build_resolve_jury", fake_build_resolve_jury,
        )

        with pytest.raises(RuntimeError, match="OutsideValidityIntervalUTxO"):
            s._step_resolve_jury(epoch=0)

        assert call_count["n"] == 1, (
            "Non-divergence errors must NOT trigger a retry — "
            "build_resolve_jury called more than once."
        )
        assert sleep_calls == [], (
            f"No sleep should occur when the error is not a "
            f"divergence-pattern PlutusFailure. Got: {sleep_calls!r}."
        )

    def test_aborts_when_deadline_floor_breached(
        self, base_kwargs, monkeypatch,
    ):
        """If the on-chain resolution_deadline window has less than the
        floor (30 s) of remaining budget, the retry loop must NOT sleep
        — it must re-raise the divergence error immediately."""
        # Anchor challenged_at so deadline_abs lands JUST below now_ms +
        # floor: with default resolution_deadline=600_000 ms, last_block_slot
        # = 1_000 ⇒ now_ms = 1_000_000. We want
        #   remaining = challenged_at + 600_000 - 1_000_000 < 30_000
        # ⇒ challenged_at < 430_000. Pick challenged_at = 400_000:
        #   deadline_abs = 1_000_000  ⇒ remaining = 0 ms (< 30_000 floor).
        self._stub_resolved_params(monkeypatch, resolution_deadline=600_000)
        sleep_calls: list = []
        self._patch_chain_and_params(
            monkeypatch, sleep_calls, challenged_at_ms=400_000,
        )

        s = self._make_scenario(base_kwargs)
        monkeypatch.setattr(s, "_deployment_state", lambda: object())
        monkeypatch.setattr(s, "checkpoint", lambda: None)

        call_count = {"n": 0}

        def fake_build_resolve_jury(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError(
                "Submit failed (400): FailedUnexpectedly (PlutusFailure ...)"
            )

        import simulation.tx_builder as _txb
        monkeypatch.setattr(
            _txb, "build_resolve_jury", fake_build_resolve_jury,
        )

        with pytest.raises(RuntimeError, match="FailedUnexpectedly"):
            s._step_resolve_jury(epoch=0)

        assert call_count["n"] == 1, (
            "When the deadline floor is breached, the retry loop must "
            "abort BEFORE re-attempting."
        )
        assert sleep_calls == [], (
            f"No retry sleep when out of deadline budget. Got: "
            f"{sleep_calls!r}."
        )
