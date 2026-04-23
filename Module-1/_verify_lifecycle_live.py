"""Live testnet smoke verifier for the FULL Module-1 lifecycle.

Runs ONE happy-path scenario end-to-end on Vector testnet:

  setup → submit → challenge → transition → select → 5x commit → 5x reveal
        → resolve → 5x distribute → cleanup → 15x withdraw → drain

Asserts:
  - Every lifecycle *_success event is present in the metrics JSONL.
  - A {"event_type": "verdict", ...} event is emitted.
  - 15 juror_withdrawn events present (or per-juror skip events).
  - drained_to_master event present.
  - Master spend stays under ~300 ADA after drain (proves WithdrawJuror
    + drain returned the bonds).

Run from the main session (not a sandboxed subagent — needs network):
    cd <module-root>
    python3 _verify_lifecycle_live.py

Flags:
    --auditor-wins / --inconclusive
        Override the target verdict (default: ClaimerWins).
    --plutus-trace
        Call Ogmios evaluateTransaction BEFORE every submitTransaction.
        If eval returns script errors, dump the full error response to
        ``/tmp/sim-submit-errors/plutus_trace_<ts>.txt`` and raise a
        RuntimeError whose message carries the first 500 chars of the
        response — the scenario runner captures that message into the
        scenario_error event so post-mortem has the failing validator
        trace WITHOUT burning the live-submit cost. Default off (no
        overhead on normal runs).
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Network-scoped paths sourced from simulation.config so APEX_NETWORK
# routes this verifier to the correct chain. Default is testnet; set
# APEX_NETWORK=mainnet APEX_NETWORK_CONFIRM=yes to target mainnet.
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


PLUTUS_TRACE_DIR = Path("/tmp/sim-submit-errors")


def _install_plutus_trace_preflight() -> None:
    """Wrap ``submit_tx`` in both ``simulation.chain`` and
    ``simulation.tx_builder`` with an Ogmios ``evaluateTransaction``
    preflight.

    Behaviour per wrapped submit call:
      1. Call the real ``ogmios_rpc('evaluateTransaction', ...)`` with the
         TX's CBOR hex.
      2. If the eval RPC returns script errors, dump the FULL JSON
         response to ``/tmp/sim-submit-errors/plutus_trace_<ts>.txt``
         and raise ``RuntimeError`` whose message is
         ``plutus_trace preflight failed: dump=<path>; preview=<first500>``.
         The scenario's ``scenario_error`` event metadata then carries
         that preview (via its ``message`` field) and the dump path.
      3. On eval success (empty or all-ok result), fall through to the
         real ``submit_tx``.

    tx_builder does ``from simulation.chain import submit_tx`` at module
    load time, so patching ``simulation.chain.submit_tx`` alone would NOT
    intercept calls from ``tx_builder.build_*`` helpers — we patch both
    module attributes to guarantee coverage.
    """
    import simulation.chain as _chain
    import simulation.tx_builder as _txb

    real_submit_chain = _chain.submit_tx
    real_rpc = _chain.ogmios_rpc
    PLUTUS_TRACE_DIR.mkdir(parents=True, exist_ok=True)

    def _wrapped_submit(tx_bytes):
        tx_hex = (
            tx_bytes.hex()
            if isinstance(tx_bytes, (bytes, bytearray))
            else bytes.fromhex(tx_bytes).hex()
        )
        try:
            eval_result = real_rpc(
                "evaluateTransaction",
                {"transaction": {"cbor": tx_hex}},
            )
        except Exception as rpc_exc:
            # Don't let the preflight hide a live submit path — log and
            # fall through to real submit. An Ogmios RPC hiccup here
            # shouldn't cost us the TX.
            print(
                f"  [plutus-trace] evaluateTransaction RPC raised "
                f"{type(rpc_exc).__name__}: {rpc_exc!s} — falling through "
                f"to live submit."
            )
            return real_submit_chain(tx_bytes)

        eval_errors: list = []
        if isinstance(eval_result, list):
            eval_errors = [it for it in eval_result if "error" in it]

        if eval_errors:
            ts_ms = int(time.time() * 1000)
            dump_path = PLUTUS_TRACE_DIR / f"plutus_trace_{ts_ms}.txt"
            payload = {
                "tx_cbor_hex": tx_hex,
                "eval_result": eval_result,
                "eval_errors": eval_errors,
            }
            dump_text = json.dumps(payload, indent=2, default=str)
            try:
                dump_path.write_text(dump_text)
            except Exception:
                # Dump failure is non-fatal — the preview in the raise
                # still carries the error summary.
                pass

            # Preview: a compact single-line rendering of eval_errors so
            # the scenario_error message field is useful without having
            # to open the dump. Truncate at 500 chars.
            preview_src = json.dumps(eval_errors, default=str)
            preview = preview_src[:500]
            raise RuntimeError(
                f"plutus_trace preflight failed: dump={dump_path}; "
                f"preview={preview}"
            )

        # Eval OK — fall through to real submit.
        return real_submit_chain(tx_bytes)

    _chain.submit_tx = _wrapped_submit
    _txb.submit_tx = _wrapped_submit


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
    print(f"  master balance BEFORE lifecycle: {before_balance/1_000_000:.3f} ADA")

    expected_lifecycle = [
        "submit_claim_success",
        "open_challenge_success",
        "transition_to_voting_success",
        "select_jury_success",
    ] + ["commit_vote_success"] * 5 \
      + ["reveal_vote_success"] * 5 \
      + ["resolve_jury_success"] \
      + ["distribute_rewards_success"] * 5 \
      + ["cleanup_resolved_success"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        kwargs = dict(
            name=f"lifecycle_smoke_{int(time.time())}",
            config={"epochs_per_day": 24, "n_agents": 17},
            deployment=deployment,
            master_skey=master_skey,
            master_vkey=master_vkey,
            master_wallet_addr=master_addr,
            checkpoint_dir=tmp / "ckpt",
            metrics_dir=tmp / "metrics",
            rng_seed=int(time.time()) & 0xFFFFFFFF,
        )
        # Allow overriding the verdict outcome from the CLI:
        #   python3 _verify_lifecycle_live.py                  # ClaimerWins
        #   python3 _verify_lifecycle_live.py --auditor-wins   # AuditorWins
        #   python3 _verify_lifecycle_live.py --inconclusive   # Inconclusive
        #   python3 _verify_lifecycle_live.py --plutus-trace   # eval-before-submit
        import sys as _sys
        tv = "ClaimerWins"
        if "--auditor-wins" in _sys.argv:
            tv = "AuditorWins"
        elif "--inconclusive" in _sys.argv:
            tv = "Inconclusive"
        plutus_trace = "--plutus-trace" in _sys.argv
        s = HappyPathScenario(
            **kwargs, jury_size=5, pool_size=15, target_verdict=tv,
        )
        print(f"  scenario name:   {s.name}")
        print(f"  pool_size:       {s.pool_size}")
        print(f"  jury_size:       {s.jury_size}")
        print(f"  target_verdict:  {s.target_verdict}")
        print(f"  vote_pattern:    {s._vote_pattern}")
        print(f"  plutus_trace:    {plutus_trace}")

        if plutus_trace:
            # Wrap submit_tx in both simulation.chain AND simulation.tx_builder
            # so every lifecycle TX (scenario-driven and builder-driven) gets
            # the evaluateTransaction preflight. Errors dump to
            # /tmp/sim-submit-errors/plutus_trace_<ts>.txt and the exception
            # message carries a 500-char preview that flows through to the
            # scenario_error event.
            _install_plutus_trace_preflight()
            print(
                f"  plutus_trace: preflight installed — "
                f"dumps under {PLUTUS_TRACE_DIR}/plutus_trace_<ts>.txt"
            )

        # Drive enough epochs to walk: setup (1) + 4 lifecycle steps +
        # 5 commit + 5 reveal + resolve (1) + 5 distribute + cleanup (1)
        # + 15 withdraw + drain (1) = 38 epochs minimum. Use 64 for
        # headroom (matches the test fixture's n_epochs).
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
        # Check no scenario_error events
        errors = [e for e in events if e.get("event_type") == "scenario_error"]
        if errors:
            print(f"  FAIL: {len(errors)} scenario_error events:")
            for e in errors:
                print(f"    - {e.get('exception_class')}: {e.get('message')}")
            return 1

        # Check setup_complete present exactly once
        setup_evs = [e for e in events if e.get("event_type") == "setup_complete"]
        if len(setup_evs) != 1:
            print(f"  FAIL: expected 1 setup_complete, got {len(setup_evs)}")
            return 1
        print(f"  setup_complete ts: {setup_evs[0].get('ts')}")

        # Check expected lifecycle event sequence
        observed_seq = [e["event_type"] for e in events
                        if e.get("event_type") in set(expected_lifecycle)]
        if observed_seq != expected_lifecycle:
            print(f"  FAIL: lifecycle event sequence mismatch")
            print(f"    expected: {expected_lifecycle}")
            print(f"    observed: {observed_seq}")
            return 1
        print(f"  PASS: all {len(expected_lifecycle)} lifecycle events present in order")

        # Verdict event
        verdicts = [e for e in events if e.get("event_type") == "verdict"]
        if len(verdicts) != 1:
            print(f"  FAIL: expected 1 verdict event, got {len(verdicts)}")
            return 1
        print(f"  verdict event: outcome={verdicts[0].get('outcome')!r}")

        # WithdrawJuror events: per-juror, expect 15 (or skipped equivalents)
        withdrawn_evs = [e for e in events if e.get("event_type") == "juror_withdrawn"]
        skipped_evs = [e for e in events if e.get("event_type") == "juror_withdraw_skipped"]
        total_w = len(withdrawn_evs) + len(skipped_evs)
        if total_w != 15:
            print(f"  FAIL: expected 15 (juror_withdrawn + juror_withdraw_skipped) "
                  f"events, got {total_w} ({len(withdrawn_evs)} ok + "
                  f"{len(skipped_evs)} skipped)")
            return 1
        print(f"  juror withdraw: {len(withdrawn_evs)} ok, {len(skipped_evs)} skipped")
        if skipped_evs:
            for e in skipped_evs[:3]:
                print(f"    skipped pool_index={e.get('pool_index')}: {e.get('reason')}")

        # Drained event
        drained = [e for e in events if e.get("event_type") == "drained_to_master"]
        if len(drained) != 1:
            print(f"  FAIL: expected 1 drained_to_master event, got {len(drained)}")
            return 1
        print(f"  drained: total_returned={drained[0].get('total_returned_ada_lovelace')/1_000_000:.3f} ADA, "
              f"withdraw_returned={drained[0].get('withdraw_returned_lovelace')/1_000_000:.3f} ADA")

        # Master balance check
        after_balance = sum(
            int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
            else int(u.output.amount)
            for u in ctx.utxos(str(master_addr))
        )
        spent = before_balance - after_balance
        print(f"\n  master balance AFTER lifecycle: {after_balance/1_000_000:.3f} ADA")
        print(f"  master spend (NET, after withdraw + drain): {spent/1_000_000:.3f} ADA")

        # Tolerate up to ~350 ADA — 15 + 2 = 17 unrecoverable DID locks
        # (15 ADA each = 255 ADA) + lifecycle TX fees + slop.
        if spent > 350_000_000:
            print(f"  FAIL: spent {spent/1_000_000:.1f} ADA exceeds 350 ADA bound")
            return 1
        print(f"  PASS: net spend within 350 ADA bound")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
