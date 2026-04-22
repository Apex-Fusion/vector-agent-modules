"""Live testnet smoke verifier for the WithdrawClaim lifecycle.

Runs ONE WithdrawClaimScenario end-to-end on Vector testnet:

  setup → submit → wait_for_challenge_window → withdraw_claim
       → 15x withdraw_juror → drain

Asserts:
  - submit_claim_success event present.
  - wait_for_challenge_window_complete event present.
  - withdraw_claim_success event present.
  - verdict event present with outcome="withdraw_claim".
  - 15 juror_withdrawn events present (or per-juror skip events).
  - drained_to_master event present.
  - Master spend stays under ~350 ADA after drain.

DO NOT RUN from a sandboxed subagent — requires network access.
Chuck kicks this off from the main session after preflight green.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Network-scoped paths sourced from simulation.config so APEX_NETWORK
# routes this verifier to the correct chain.
from simulation.config import WALLET_SKEY as _WALLET_SKEY, DEPLOYMENT_PATH as _DEPLOYMENT_PATH

MASTER_SKEY_PATH = Path(_WALLET_SKEY)
DEPLOYMENT_PATH = Path(_DEPLOYMENT_PATH)


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    if not MASTER_SKEY_PATH.exists():
        print(f"FAIL: master skey missing at {MASTER_SKEY_PATH}")
        return 1
    if not DEPLOYMENT_PATH.exists():
        print(f"FAIL: deployment manifest missing at {DEPLOYMENT_PATH}")
        return 1

    from pycardano import Address, PaymentSigningKey, PaymentVerificationKey
    from simulation.config import NETWORK
    from simulation.scenarios.withdraw_claim import WithdrawClaimScenario
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
    print(f"  master balance BEFORE lifecycle: {before_balance/1_000_000:.3f} ADA")

    expected_lifecycle = [
        "submit_claim_success",
        "wait_for_challenge_window_complete",
        "withdraw_claim_success",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        kwargs = dict(
            name=f"withdraw_claim_smoke_{int(time.time())}",
            config={"epochs_per_day": 24, "n_agents": 17},
            deployment=deployment,
            master_skey=master_skey,
            master_vkey=master_vkey,
            master_wallet_addr=master_addr,
            checkpoint_dir=tmp / "ckpt",
            metrics_dir=tmp / "metrics",
            rng_seed=int(time.time()) & 0xFFFFFFFF,
        )
        s = WithdrawClaimScenario(
            **kwargs, jury_size=5, pool_size=15,
        )
        print(f"  scenario name:   {s.name}")
        print(f"  pool_size:       {s.pool_size}")
        print(f"  jury_size:       {s.jury_size}")

        # Driver budget: setup + submit + wait + withdraw_claim + 15
        # withdraw_juror + drain_to_master = ~20 epochs. Use 64 for slack.
        print("\n  Driving lifecycle (n_epochs=64)...")
        t0 = time.time()
        try:
            s.run(n_epochs=64)
        except Exception as exc:
            print(f"  FAIL: scenario.run raised: {exc!r}")
            import traceback; traceback.print_exc()
            return 1
        dt = time.time() - t0
        print(f"  lifecycle finished in {dt:.1f}s ({dt/60:.1f} min)")
        print(f"  final step: {s._step}")
        print(f"  verdict:    {s._verdict}")

        events = _read_jsonl(s.metrics_path)
        errors = [e for e in events if e.get("event_type") == "scenario_error"]
        if errors:
            print(f"  FAIL: {len(errors)} scenario_error events:")
            for e in errors:
                print(f"    - {e.get('exception_class')}: {e.get('message')}")
            return 1

        setup_evs = [e for e in events if e.get("event_type") == "setup_complete"]
        if len(setup_evs) != 1:
            print(f"  FAIL: expected 1 setup_complete, got {len(setup_evs)}")
            return 1

        observed_seq = [e["event_type"] for e in events
                        if e.get("event_type") in set(expected_lifecycle)]
        if observed_seq != expected_lifecycle:
            print(f"  FAIL: lifecycle event sequence mismatch")
            print(f"    expected: {expected_lifecycle}")
            print(f"    observed: {observed_seq}")
            return 1
        print(f"  PASS: all {len(expected_lifecycle)} lifecycle events present in order")

        verdicts = [e for e in events if e.get("event_type") == "verdict"]
        if len(verdicts) != 1:
            print(f"  FAIL: expected 1 verdict event, got {len(verdicts)}")
            return 1
        if verdicts[0].get("outcome") != "withdraw_claim":
            print(f"  FAIL: verdict outcome mismatch: {verdicts[0]}")
            return 1
        print(f"  verdict event: outcome={verdicts[0].get('outcome')!r}")

        withdrawn_evs = [e for e in events if e.get("event_type") == "juror_withdrawn"]
        skipped_evs = [e for e in events if e.get("event_type") == "juror_withdraw_skipped"]
        total_w = len(withdrawn_evs) + len(skipped_evs)
        if total_w != 15:
            print(f"  FAIL: expected 15 juror_withdraw events, got {total_w}")
            return 1
        print(f"  juror withdraw: {len(withdrawn_evs)} ok, {len(skipped_evs)} skipped")

        drained = [e for e in events if e.get("event_type") == "drained_to_master"]
        if len(drained) != 1:
            print(f"  FAIL: expected 1 drained_to_master event, got {len(drained)}")
            return 1
        print(f"  drained: total_returned={drained[0].get('total_returned_ada_lovelace')/1_000_000:.3f} ADA, "
              f"withdraw_returned={drained[0].get('withdraw_returned_lovelace')/1_000_000:.3f} ADA")

        after_balance = sum(
            int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
            else int(u.output.amount)
            for u in ctx.utxos(str(master_addr))
        )
        spent = before_balance - after_balance
        print(f"\n  master balance AFTER lifecycle: {after_balance/1_000_000:.3f} ADA")
        print(f"  master spend (NET, after withdraw + drain): {spent/1_000_000:.3f} ADA")
        if spent > 350_000_000:
            print(f"  FAIL: spent {spent/1_000_000:.1f} ADA exceeds 350 ADA bound")
            return 1
        print(f"  PASS: net spend within 350 ADA bound")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
