"""
Construction-only tests for simulation.scenarios.withdraw_claim
(iter-4 — WithdrawClaimScenario).

The live lifecycle is exercised via the preflight harness and the
``_verify_lifecycle_withdrawclaim.py`` standalone script. Unit-test
scope here is the same CONSTRUCTION contract the HappyPath tests pin:

  - subclass inherits HappyPathScenario state correctly
  - initial lifecycle state is "submit_claim"
  - target_verdict defaults preserve parent expectations
  - wallet derivation + role accessors inherited unchanged
  - checkpoint payload round-trips through the parent's schema
  - decide_and_act_for_epoch raises NotImplementedError under stub
    master_skey (construction-test contract)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulation.scenarios.happy_path import (
    ROLE_CLAIMANT,
    ROLE_AUDITOR,
    ROLE_JUROR_PREFIX,
)
from simulation.scenarios.withdraw_claim import WithdrawClaimScenario


V15_DEPLOYMENT_PATH = Path(
    "/home/jelisaveta/.openclaw/workspace-apex/testnet/game1-sim-deployment.json"
)


@pytest.fixture
def fake_master_skey_bytes() -> bytes:
    return b"x" * 32


@pytest.fixture
def base_kwargs(tmp_path, fake_master_skey_bytes):
    if V15_DEPLOYMENT_PATH.exists():
        deployment = json.loads(V15_DEPLOYMENT_PATH.read_text())
    else:
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
        name="wc_test",
        config={"epochs_per_day": 24, "n_agents": 17},
        deployment=deployment,
        master_skey=fake_master_skey_bytes,
        master_vkey=b"vkey-stand-in",
        master_wallet_addr="addr1_master_fake",
        checkpoint_dir=tmp_path / "checkpoints",
        metrics_dir=tmp_path / "metrics",
        rng_seed=42,
    )


class TestConstruction:
    def test_subclass_instantiation(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        assert s.name == "wc_test"
        assert s.rng_seed == 42

    def test_initial_step_is_submit_claim(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        assert s._step == "submit_claim"
        assert s._claim_ref is None
        assert s._challenge_ref is None
        assert s._verdict is None

    def test_inherits_jury_and_pool_sizes(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs, jury_size=5, pool_size=15)
        assert s.jury_size == 5
        assert s.pool_size == 15

    def test_default_kwargs(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        assert s.jury_size == 5
        assert s.pool_size == 15
        assert s.stake_amount == 50_000_000

    def test_wallet_roles_match_happy_path(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs, jury_size=5)
        assert ROLE_CLAIMANT in s._wallets
        assert ROLE_AUDITOR in s._wallets
        for i in range(5):
            assert f"{ROLE_JUROR_PREFIX}_{i}" in s._wallets

    def test_target_verdict_default_claimerwins(self, base_kwargs):
        # target_verdict is forced to a safe default since this scenario
        # doesn't vote. The vote_pattern is never used on-chain.
        s = WithdrawClaimScenario(**base_kwargs)
        assert s.target_verdict == "ClaimerWins"

    def test_role_accessors_inherited(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs, jury_size=5)
        assert s.claimant is s._wallets[ROLE_CLAIMANT]
        assert s.auditor is s._wallets[ROLE_AUDITOR]
        assert len(s.juror_wallets) == 5


class TestDecideAndActIsAbstract:
    """Construction-test contract — stub master_skey must raise NotImpl."""

    def test_decide_and_act_raises_not_implemented(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        with pytest.raises(NotImplementedError):
            s.decide_and_act_for_epoch(0)


class TestCheckpointPayloadShape:
    """The subclass checkpoint payload must remain a superset of the
    parent's contract so existing restore code works unchanged."""

    def test_initial_shape_matches_parent_schema(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        payload = s._checkpoint_payload()
        # Superset of the parent's canonical 6 keys.
        assert {"step", "claim_ref", "claim_token_hex", "challenge_ref",
                "tx_hashes", "verdict"} <= set(payload.keys())
        assert payload["step"] == "submit_claim"
        assert payload["claim_ref"] is None

    def test_checkpoint_roundtrip_via_base_class(self, base_kwargs):
        s1 = WithdrawClaimScenario(**base_kwargs)
        s1._step = "withdraw_claim"
        s1._claim_ref = "aa" * 32 + "#0"
        s1.checkpoint()

        s2 = WithdrawClaimScenario(**base_kwargs)
        assert s2.restore() is True
        assert s2._step == "withdraw_claim"
        assert s2._claim_ref == "aa" * 32 + "#0"


class TestStepTransitions:
    """Validate the dispatch table handles each declared step name."""

    def test_dispatch_known_steps_raises_on_stub(self, base_kwargs):
        """Each known WithdrawClaim step must raise NotImplementedError
        (construction-test stub context) rather than KeyError (meaning
        the dispatch table is missing that step)."""
        s = WithdrawClaimScenario(**base_kwargs)
        # Bypass the setup gate by faking agent_setup_done.
        s._agent_setup_done = True
        for step in (
            "submit_claim",
            "wait_for_challenge_window",
            "withdraw_claim",
            "withdraw_jurors",
            "drain_to_master",
        ):
            s._step = step
            with pytest.raises(NotImplementedError):
                s.decide_and_act_for_epoch(0)

    def test_unknown_step_raises_runtime_error(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        s._agent_setup_done = True
        s._step = "not_a_real_step"
        # NotImplementedError is raised BEFORE the dispatch because of
        # the stub master_skey guard. Construction tests can't probe
        # the "unknown step" branch without threading a real psk.
        with pytest.raises(NotImplementedError):
            s.decide_and_act_for_epoch(0)

    def test_done_step_returns_empty_list(self, base_kwargs):
        s = WithdrawClaimScenario(**base_kwargs)
        s._agent_setup_done = True
        s._step = "done"
        # Stub master_skey guard fires first — still NotImplemented.
        with pytest.raises(NotImplementedError):
            s.decide_and_act_for_epoch(0)
