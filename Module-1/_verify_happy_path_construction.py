"""Sanity-check the construction-only invariants of HappyPathScenario.

Mirrors the test classes TestConstruction, TestWalletDerivationDeterminism,
TestWalletDerivationCollisionFree, TestDeriveRoleSeedHelper, and
TestCheckpointPayloadShape from test_happy_path_scenario.py. No pytest needed.

Run from the Module-1 repo root:
    cd <module-root>
    python3 _verify_happy_path_construction.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from simulation.scenarios.happy_path import (
    HappyPathScenario, ROLE_CLAIMANT, ROLE_AUDITOR, ROLE_JUROR_PREFIX,
    _derive_role_seed,
)


def _ok(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}{('  -- ' + detail) if detail else ''}")
    return cond


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Network-scoped manifest path sourced from simulation.config.
        from simulation.config import DEPLOYMENT_PATH as _DEPLOYMENT_PATH
        manifest_path = Path(_DEPLOYMENT_PATH)
        deployment = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

        base = dict(
            name="hp_test",
            config={"epochs_per_day": 24, "n_agents": 7},
            deployment=deployment,
            master_skey=b"x" * 32,
            master_vkey=b"vkey-stand-in",
            master_wallet_addr="addr1_master_fake",
            checkpoint_dir=tmp / "ckpt",
            metrics_dir=tmp / "metrics",
            rng_seed=42,
        )
        s = HappyPathScenario(**base)
        ok &= _ok("base init: name", s.name == "hp_test")
        ok &= _ok("base init: rng_seed", s.rng_seed == 42)
        ok &= _ok("base init: rng exists", s.rng is not None)
        ok &= _ok("base init: epoch -1", s._epoch == -1)
        ok &= _ok("base init: ckpt path",
                  s.checkpoint_path == base["checkpoint_dir"] / "hp_test.json")
        ok &= _ok("base init: metrics path",
                  s.metrics_path == base["metrics_dir"] / "hp_test.jsonl")
        ok &= _ok("base init: ckpt dir exists", base["checkpoint_dir"].exists())
        ok &= _ok("base init: metrics dir exists", base["metrics_dir"].exists())

        # Subclass-specific kwargs
        s_alt = HappyPathScenario(**base, jury_size=5, stake_amount=75_000_000)
        ok &= _ok("subclass kwargs: jury_size", s_alt.jury_size == 5)
        ok &= _ok("subclass kwargs: stake_amount", s_alt.stake_amount == 75_000_000)

        # Defaults
        ok &= _ok("defaults: jury_size", s.jury_size == 5)
        ok &= _ok("defaults: stake_amount", s.stake_amount == 50_000_000)

        # Initial lifecycle state
        ok &= _ok("initial: _step", s._step == "submit_claim")
        ok &= _ok("initial: _claim_ref", s._claim_ref is None)
        ok &= _ok("initial: _claim_token_hex", s._claim_token_hex is None)
        ok &= _ok("initial: _challenge_ref", s._challenge_ref is None)
        ok &= _ok("initial: _verdict", s._verdict is None)
        ok &= _ok("initial: _tx_hashes", s._tx_hashes == {})

        # Wallet roles present
        expected = {ROLE_CLAIMANT, ROLE_AUDITOR} | {f"{ROLE_JUROR_PREFIX}_{i}" for i in range(5)}
        ok &= _ok("roles: present", set(s._wallets.keys()) == expected)
        ok &= _ok("roles: claimant in wallets", ROLE_CLAIMANT in s._wallets)
        ok &= _ok("roles: auditor in wallets", ROLE_AUDITOR in s._wallets)
        for i in range(5):
            ok &= _ok(f"roles: juror_{i} present",
                      f"{ROLE_JUROR_PREFIX}_{i}" in s._wallets)

        # jury_size scaling
        s3 = HappyPathScenario(**base, jury_size=3)
        juror_keys = [k for k in s3._wallets if k.startswith(ROLE_JUROR_PREFIX)]
        ok &= _ok("scaling: 3 jurors", len(juror_keys) == 3)
        ok &= _ok("scaling: 5 wallets", len(s3._wallets) == 5)

        # Accessor properties
        ok &= _ok("accessor: claimant_wallet",
                  s.claimant_wallet is s._wallets[ROLE_CLAIMANT])
        ok &= _ok("accessor: auditor_wallet",
                  s.auditor_wallet is s._wallets[ROLE_AUDITOR])
        jurors = s.juror_wallets
        ok &= _ok("accessor: juror_wallets len", len(jurors) == 5)
        for i, jw in enumerate(jurors):
            ok &= _ok(f"accessor: juror_wallets[{i}] is _wallets[juror_{i}]",
                      jw is s._wallets[f"{ROLE_JUROR_PREFIX}_{i}"])

        # Determinism
        s_a = HappyPathScenario(**base)
        s_b = HappyPathScenario(**base)
        all_seed_eq = True
        all_vkh_eq = True
        for role in s_a._wallets:
            if s_a._wallets[role]["seed"] != s_b._wallets[role]["seed"]:
                all_seed_eq = False
            if s_a._wallets[role]["vkh_hex"] != s_b._wallets[role]["vkh_hex"]:
                all_vkh_eq = False
        ok &= _ok("determinism: same seed -> same wallets seed", all_seed_eq)
        ok &= _ok("determinism: same seed -> same wallets vkh", all_vkh_eq)

        # Different rng_seed
        base_alt = dict(base, rng_seed=999)
        s_diff_rng = HappyPathScenario(**base_alt)
        all_diff = all(
            s._wallets[r]["seed"] != s_diff_rng._wallets[r]["seed"]
            for r in s._wallets
        )
        ok &= _ok("determinism: different rng_seed -> different seeds", all_diff)

        # Different master_skey
        base_alt2 = dict(base, master_skey=b"y" * 32)
        s_diff_master = HappyPathScenario(**base_alt2)
        all_diff_m = all(
            s._wallets[r]["seed"] != s_diff_master._wallets[r]["seed"]
            for r in s._wallets
        )
        ok &= _ok("determinism: different master -> different seeds", all_diff_m)

        # Different name -> disjoint vkhs
        s_alpha = HappyPathScenario(**dict(base, name="scenario_alpha"))
        s_beta = HappyPathScenario(**dict(base, name="scenario_beta"))
        a_vkhs = {w["vkh_hex"] for w in s_alpha._wallets.values()}
        b_vkhs = {w["vkh_hex"] for w in s_beta._wallets.values()}
        ok &= _ok("collision: different names -> disjoint vkhs",
                  a_vkhs.isdisjoint(b_vkhs))

        # Internal collision-free
        vkhs = [w["vkh_hex"] for w in s._wallets.values()]
        ok &= _ok("collision: no intra-scenario vkh collision",
                  len(vkhs) == len(set(vkhs)))

        # 32-byte seeds
        all_32 = all(
            isinstance(w["seed"], (bytes, bytearray)) and len(w["seed"]) == 32
            for w in s._wallets.values()
        )
        ok &= _ok("seeds: all 32 bytes", all_32)

        # Helper invariants
        a = _derive_role_seed(b"x" * 32, "scn", 7, ROLE_CLAIMANT)
        b = _derive_role_seed(b"x" * 32, "scn", 7, ROLE_CLAIMANT)
        ok &= _ok("helper: deterministic", a == b and len(a) == 32)
        ok &= _ok("helper: role changes",
                  _derive_role_seed(b"x" * 32, "scn", 7, ROLE_CLAIMANT)
                  != _derive_role_seed(b"x" * 32, "scn", 7, ROLE_AUDITOR))
        ok &= _ok("helper: name changes",
                  _derive_role_seed(b"x" * 32, "alpha", 7, ROLE_CLAIMANT)
                  != _derive_role_seed(b"x" * 32, "beta", 7, ROLE_CLAIMANT))
        ok &= _ok("helper: rng_seed changes",
                  _derive_role_seed(b"x" * 32, "scn", 1, ROLE_CLAIMANT)
                  != _derive_role_seed(b"x" * 32, "scn", 2, ROLE_CLAIMANT))
        ok &= _ok("helper: master changes",
                  _derive_role_seed(b"x" * 32, "scn", 7, ROLE_CLAIMANT)
                  != _derive_role_seed(b"y" * 32, "scn", 7, ROLE_CLAIMANT))

        # Checkpoint payload shape — must include the original 6 keys
        payload = s._checkpoint_payload()
        needed = {"step", "claim_ref", "claim_token_hex",
                  "challenge_ref", "tx_hashes", "verdict"}
        ok &= _ok("ckpt: keys == required exactly", set(payload.keys()) == needed,
                  f"got {set(payload.keys())}")
        enc = json.dumps(payload)
        ok &= _ok("ckpt: JSON round-trip", json.loads(enc) == payload)

        # Round-trip via base class
        s.checkpoint()
        s_r = HappyPathScenario(**base)
        ok &= _ok("ckpt: restore returns True", s_r.restore() is True)

        # restore_payload round-trip
        s1 = HappyPathScenario(**base)
        s1._step = "resolve_jury"
        s1._claim_ref = "ab" * 32 + "#0"
        s1._claim_token_hex = "cd" * 28
        s1._challenge_ref = "ef" * 32 + "#0"
        s1._tx_hashes = {"submit_claim": "11" * 32}
        s1._verdict = "ClaimerWins"
        payload = s1._checkpoint_payload()
        s2 = HappyPathScenario(**base)
        s2._restore_payload(payload)
        ok &= _ok("restore_payload: step", s2._step == "resolve_jury")
        ok &= _ok("restore_payload: claim_ref", s2._claim_ref == "ab" * 32 + "#0")
        ok &= _ok("restore_payload: claim_token_hex", s2._claim_token_hex == "cd" * 28)
        ok &= _ok("restore_payload: challenge_ref", s2._challenge_ref == "ef" * 32 + "#0")
        ok &= _ok("restore_payload: tx_hashes",
                  s2._tx_hashes == {"submit_claim": "11" * 32})
        ok &= _ok("restore_payload: verdict", s2._verdict == "ClaimerWins")

        # decide_and_act_for_epoch raises NotImplementedError under stub master skey
        try:
            s.decide_and_act_for_epoch(0)
            ok &= _ok("decide_and_act_for_epoch raises (stub master)", False, "did not raise")
        except NotImplementedError:
            ok &= _ok("decide_and_act_for_epoch raises (stub master)", True)

        # Step helpers raise NotImplementedError (3a contract)
        single_arg = ["_step_submit_claim", "_step_open_challenge",
                      "_step_transition_to_voting", "_step_select_jury",
                      "_step_resolve_jury", "_step_cleanup_resolved"]
        for m in single_arg:
            try:
                getattr(s, m)(0)
                ok &= _ok(f"step helper {m} raises", False, "did not raise")
            except NotImplementedError:
                ok &= _ok(f"step helper {m} raises", True)
        for m in ["_step_commit_vote", "_step_reveal_vote", "_step_distribute_rewards"]:
            try:
                getattr(s, m)(0, 0)
                ok &= _ok(f"step helper {m} raises", False, "did not raise")
            except NotImplementedError:
                ok &= _ok(f"step helper {m} raises", True)

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
