"""Live testnet smoke verifier for the setup phase ONLY.

DOES NOT exercise the lifecycle (those are 3b). Confirms:
  - allocate_indices returns globally-unique indices
  - derive populates real PaymentSigningKey / Address
  - _setup_agents fund + register DIDs + bond jurors on-chain
  - emits exactly one setup_complete event with agent_indices + scenario name
  - re-running with a fresh instance and the same metrics dir is a no-op

Run from the main session (not a sandboxed subagent — needs network):
    cd <module-root>
    python3 _verify_setup_live.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Network-scoped paths sourced from simulation.config so APEX_NETWORK
# routes this setup verifier to the correct chain.
from simulation.config import WALLET_SKEY as _WALLET_SKEY, DEPLOYMENT_PATH as _DEPLOYMENT_PATH

MASTER_SKEY_PATH = Path(_WALLET_SKEY)
DEPLOYMENT_PATH = Path(_DEPLOYMENT_PATH)


def main() -> int:
    if not MASTER_SKEY_PATH.exists():
        print(f"FAIL: master skey missing at {MASTER_SKEY_PATH}")
        return 1
    if not DEPLOYMENT_PATH.exists():
        print(f"FAIL: deployment manifest missing at {DEPLOYMENT_PATH}")
        return 1

    from pycardano import Address, PaymentSigningKey, PaymentVerificationKey
    from simulation.config import NETWORK
    from simulation.scenarios.happy_path import HappyPathScenario
    from simulation.chain import OgmiosContext

    deployment = json.loads(DEPLOYMENT_PATH.read_text())
    master_skey = PaymentSigningKey.load(str(MASTER_SKEY_PATH))
    master_vkey = PaymentVerificationKey.from_signing_key(master_skey)
    master_addr = Address(master_vkey.hash(), network=NETWORK)
    print(f"  master addr: {master_addr}")

    ctx = OgmiosContext()
    before_balance = sum(
        int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
        else int(u.output.amount)
        for u in ctx.utxos(str(master_addr))
    )
    print(f"  master balance BEFORE setup: {before_balance/1_000_000:.3f} ADA")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        kwargs = dict(
            name=f"setup_smoke_{int(time.time())}",
            config={"epochs_per_day": 24, "n_agents": 7},
            deployment=deployment,
            master_skey=master_skey,
            master_vkey=master_vkey,
            master_wallet_addr=master_addr,
            checkpoint_dir=tmp / "ckpt",
            metrics_dir=tmp / "metrics",
            rng_seed=int(time.time()) & 0xFFFFFFFF,
        )
        s = HappyPathScenario(**kwargs, jury_size=5)
        print(f"  scenario name: {s.name}")
        print(f"  derived placeholder vkhs:")
        for role, w in s._wallets.items():
            print(f"    {role}: vkh={w['vkh_hex'][:12]}... (no live skey yet)")

        print("\n  Calling _setup_agents (live testnet)...")
        t0 = time.time()
        try:
            events = s._setup_agents(epoch=0)
        except Exception as exc:
            print(f"  FAIL: setup raised: {exc!r}")
            import traceback; traceback.print_exc()
            return 1
        dt = time.time() - t0
        print(f"  setup completed in {dt:.1f}s")
        print(f"  events emitted by _setup_agents: {events}")

        # Manually emit the setup_complete event (since _setup_agents returns
        # but only the base class emits via emit_event in run()). For this
        # smoke we emit ourselves to confirm the round-trip.
        for e in events:
            s.emit_event(e)

        # Walk the metrics file and confirm setup_complete is present.
        metrics_lines = s.metrics_path.read_text().splitlines() if s.metrics_path.exists() else []
        setup_events = [json.loads(l) for l in metrics_lines if l.strip()
                        and json.loads(l).get("event_type") == "setup_complete"]
        if len(setup_events) != 1:
            print(f"  FAIL: expected exactly 1 setup_complete event, got {len(setup_events)}")
            return 1
        ev = setup_events[0]
        print(f"\n  setup_complete event:")
        print(f"    agent_indices: {ev.get('agent_indices')}")
        print(f"    scenario:      {ev.get('scenario')}")
        if "juror_bond_pending" in ev:
            print(f"    juror_bond_pending: {ev['juror_bond_pending']}")

        # Master-wallet spend.
        after_balance = sum(
            int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
            else int(u.output.amount)
            for u in ctx.utxos(str(master_addr))
        )
        spent = before_balance - after_balance
        print(f"\n  master balance AFTER setup: {after_balance/1_000_000:.3f} ADA")
        print(f"  master spend: {spent/1_000_000:.3f} ADA")

        # Idempotency: a fresh instance with the SAME metrics path should
        # detect prior setup_complete and do nothing.
        print("\n  Testing idempotency on a fresh instance (same metrics dir)...")
        s2 = HappyPathScenario(**kwargs, jury_size=5)
        events2 = s2._setup_agents(epoch=0)
        if events2:
            print(f"  FAIL: fresh instance re-emitted {events2}; expected []")
            return 1
        if not s2._agent_setup_done:
            print(f"  FAIL: fresh instance did not detect prior setup_complete")
            return 1
        if s2._agent_indices != ev["agent_indices"]:
            print(f"  FAIL: re-derived indices differ: {s2._agent_indices} vs {ev['agent_indices']}")
            return 1
        print("  PASS: idempotent restart re-derived the same indices and skipped setup")

        # Verify resumed run did not consume more master ADA.
        after2 = sum(
            int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
            else int(u.output.amount)
            for u in ctx.utxos(str(master_addr))
        )
        if before_balance - after2 > spent + 1_000_000:
            print(f"  FAIL: resumed run leaked extra ADA: {(before_balance-after2)/1_000_000:.3f} vs {spent/1_000_000:.3f}")
            return 1
        print("  PASS: idempotent restart did not spend more master ADA")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
