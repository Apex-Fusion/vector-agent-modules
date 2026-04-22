"""Live testnet smoke verifier for the SlashNonReveal lifecycle.

Runs ONE SlashNonRevealScenario end-to-end on Vector testnet:

  setup → submit → open_challenge → transition_to_voting → select_jury
       → 5x commit → 4x reveal (juror 4 skips) → slash_non_reveal
       → timeout_resolve → 4x reset_stale_active_case → 15x withdraw_juror
       → drain_to_master

Asserts:
  - Every expected lifecycle event present (in order).
  - 1 reveal_vote_skipped event for juror 4.
  - 1 slash_non_reveal_success event.
  - 1 timeout_resolve_success event.
  - 1 verdict event with outcome="inconclusive", mechanism="timeout_resolve".
  - 4 reset_stale_active_case_success events.
  - 15 juror_withdrawn (or skipped) events — slashed juror + 4 reset + 10 unselected.
  - drained_to_master event present.
  - Master spend stays under ~400 ADA (slightly higher tolerance — the TX
    count is higher than the happy path because of slash + timeout + reset).

DO NOT RUN from a sandboxed subagent.
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
    from simulation.scenarios.slash_non_reveal import SlashNonRevealScenario
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

    # Expected sequence — using event types common across steps.
    expected_lifecycle_prefix = [
        "submit_claim_success",
        "open_challenge_success",
        "transition_to_voting_success",
        "select_jury_success",
    ] + ["commit_vote_success"] * 5 \
      + ["reveal_vote_success"] * 4 \
      + ["slash_non_reveal_success",
         "timeout_resolve_success"] \
      + ["reset_stale_active_case_success"] * 4

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        kwargs = dict(
            name=f"slash_non_reveal_smoke_{int(time.time())}",
            config={"epochs_per_day": 24, "n_agents": 17},
            deployment=deployment,
            master_skey=master_skey,
            master_vkey=master_vkey,
            master_wallet_addr=master_addr,
            checkpoint_dir=tmp / "ckpt",
            metrics_dir=tmp / "metrics",
            rng_seed=int(time.time()) & 0xFFFFFFFF,
        )
        s = SlashNonRevealScenario(
            **kwargs, jury_size=5, pool_size=15,
        )
        print(f"  scenario name:   {s.name}")
        print(f"  pool_size:       {s.pool_size}")
        print(f"  jury_size:       {s.jury_size}")
        print(f"  vote_pattern:    {s._vote_pattern}")

        # Budget: setup + 4 lifecycle + 5 commit + 4 reveal + slash
        # + timeout + 4 reset + 15 withdraw + drain = ~36. Use 80 for slack.
        print("\n  Driving lifecycle (n_epochs=80)...")
        t0 = time.time()
        try:
            s.run(n_epochs=80)
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

        expected_types = set(expected_lifecycle_prefix)
        observed_seq = [e["event_type"] for e in events
                        if e.get("event_type") in expected_types]
        if observed_seq != expected_lifecycle_prefix:
            print(f"  FAIL: lifecycle event sequence mismatch")
            print(f"    expected: {expected_lifecycle_prefix}")
            print(f"    observed: {observed_seq}")
            return 1
        print(f"  PASS: {len(expected_lifecycle_prefix)} lifecycle events present in order")

        # reveal_vote_skipped: exactly one, for juror 4.
        skipped_reveals = [
            e for e in events
            if e.get("event_type") == "reveal_vote_skipped"
        ]
        if len(skipped_reveals) != 1:
            print(f"  FAIL: expected 1 reveal_vote_skipped, got {len(skipped_reveals)}")
            return 1
        if skipped_reveals[0].get("juror_index") != 4:
            print(f"  FAIL: wrong juror_index skipped: {skipped_reveals[0]}")
            return 1

        # Verdict event
        verdicts = [e for e in events if e.get("event_type") == "verdict"]
        if len(verdicts) != 1:
            print(f"  FAIL: expected 1 verdict event, got {len(verdicts)}")
            return 1
        if verdicts[0].get("outcome") != "inconclusive":
            print(f"  FAIL: verdict outcome mismatch: {verdicts[0]}")
            return 1
        if verdicts[0].get("mechanism") != "timeout_resolve":
            print(f"  FAIL: verdict mechanism mismatch: {verdicts[0]}")
            return 1
        print(f"  verdict event: outcome={verdicts[0].get('outcome')!r}, "
              f"mechanism={verdicts[0].get('mechanism')!r}")

        # WithdrawJuror events: 15 total expected (slashed + 4 reset + 10 unsel).
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

        after_balance = sum(
            int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
            else int(u.output.amount)
            for u in ctx.utxos(str(master_addr))
        )
        spent = before_balance - after_balance
        print(f"\n  master balance AFTER lifecycle: {after_balance/1_000_000:.3f} ADA")
        print(f"  master spend (NET, after withdraw + drain): {spent/1_000_000:.3f} ADA")
        # Tolerance slightly higher than happy path: slashed juror loses
        # 10% of 25 ADA bond = 2.5 ADA permanently, plus extra TX fees
        # for slash + timeout + 4 reset.
        if spent > 400_000_000:
            print(f"  FAIL: spent {spent/1_000_000:.1f} ADA exceeds 400 ADA bound")
            return 1
        print(f"  PASS: net spend within 400 ADA bound")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
