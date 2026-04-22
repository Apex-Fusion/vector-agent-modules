"""Zero-ADA preflight harness for the Module-1 lifecycle.

This harness runs the FULL HappyPathScenario lifecycle (submit_claim →
open_challenge → transition_to_voting → select_jury → commit_vote×5 →
reveal_vote×5 → resolve_jury → distribute_rewards×5 → cleanup_resolved →
withdraw_juror×pool_size → drain_to_master) **without submitting any
transactions to the chain**. Every lifecycle TX is evaluated via Ogmios
``evaluateTransaction`` against an in-memory UTxO overlay so the full
Plutus validator stack runs and traces surface, but NO ADA is spent.

Why:
    Prior to this harness each lifecycle bug cost ~1000 ADA of setup +
    ~17 min wallclock before it surfaced. The preflight lets you iterate
    on lifecycle bug fixes with 15-second feedback loops: Ogmios evaluates
    the script context, returns per-validator budgets or error traces,
    and the overlay lets downstream steps see the post-tx state of
    consumed+added UTxOs.

Usage:
    # Against live post-setup chain state (reads master wallet, queries
    # chain for current refs, reuses bonded jurors from a prior run).
    python3 _preflight_lifecycle_eval.py --use-chain-state

    # Stop at first failure (default) or report all.
    python3 _preflight_lifecycle_eval.py --continue-on-error

Exit codes:
    0 — every lifecycle step evaluated OK.
    1 — at least one eval failed (full trace dumped to /tmp/sim-preflight-errors/).

What this harness CAN catch:
    - Any Plutus validator failure on any lifecycle TX (the eval RPC
      returns full trace bytes from the script context).
    - Datum / redeemer CBOR shape mismatches, missing required signers,
      stake / fee arithmetic errors.
    - Budget overruns (eval returns mem/cpu actually consumed).
    - Builder-raised client-side guard errors (shown verbatim).

What this harness CANNOT catch:
    - Mempool / submission-layer errors (double-spend detection, tx-size
      limits enforced by the node but not by eval — eval has looser
      bounds). These only surface on real submit.
    - Time-gate violations that depend on real block slots advancing —
      the overlay advances slots artificially (+25 per step) but the
      real chain tip doesn't move. If a validator compares a datum
      timestamp to a reference fetched directly from the ledger (not
      from the TX's validity range), the overlay can't emulate that.
    - Any bug that only manifests across MULTIPLE sequential blocks
      on the real chain (e.g. a stale reference-input UTxO).

Implementation notes:
    - ``SimulatingOgmiosContext`` wraps ``OgmiosContext``; maintains
      ``_consumed_refs`` + ``_added_utxos`` so that each step's UTxO
      lookups see post-tx state.
    - ``submit_tx`` is monkey-patched to (1) compute the deterministic
      tx_hash via ``Transaction.from_cbor(bytes).id``, (2) call
      ``evaluateTransaction`` with ``additionalUtxo`` derived from the
      overlay so validators can read simulated reference inputs, and
      (3) update the overlay with consumed+added UTxOs.
    - ``ogmios_rpc`` is wrapped so the builder's internal first-pass
      ``evaluateTransaction`` (inside ``evaluate_and_rebuild``) also
      gets the overlay injected automatically.
    - ``wait_confirm`` + ``time.sleep`` are no-op'd so a full lifecycle
      preflight runs in single-digit seconds instead of tens of
      minutes.
"""
from __future__ import annotations

import argparse
import cbor2
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ───────────────────────────────────────────────────────────────────────
# Paths (mirrors _verify_lifecycle_live.py).
# Sourced from simulation.config so APEX_NETWORK selects testnet vs
# mainnet state without any hardcoded path in this module.
# ───────────────────────────────────────────────────────────────────────

from simulation.config import (
    WALLET_SKEY as _WALLET_SKEY,
    DEPLOYMENT_PATH as _DEPLOYMENT_PATH,
)

MASTER_SKEY_PATH = Path(_WALLET_SKEY)
DEPLOYMENT_PATH = Path(_DEPLOYMENT_PATH)
PREFLIGHT_ERROR_DIR = Path("/tmp/sim-preflight-errors")


# ───────────────────────────────────────────────────────────────────────
# Utility: serialize a PyCardano UTxO to Ogmios additionalUtxo JSON.
# ───────────────────────────────────────────────────────────────────────

def _utxo_to_ogmios_additional(utxo) -> dict:
    """Convert a pycardano UTxO into the JSON shape Ogmios v6 expects for
    the ``additionalUtxo`` parameter of ``evaluateTransaction``.

    Mirrors the reverse of ``OgmiosContext.utxos`` parsing: txid+idx,
    address bech32, value (ada.lovelace + per-policy assets), and optional
    datum / script references.
    """
    from pycardano import Value as _V

    ti = utxo.input
    out = utxo.output

    txid_hex = bytes(ti.transaction_id).hex()
    idx = int(ti.index)

    # Value: Ogmios format is {"ada": {"lovelace": int}, "<policyHex>":
    # {"<assetNameHex>": int}}.
    amt = out.amount
    if isinstance(amt, int):
        lovelace = int(amt)
        ma = None
    elif isinstance(amt, _V):
        lovelace = int(amt.coin)
        ma = amt.multi_asset if amt.multi_asset else None
    else:  # pragma: no cover — pycardano only emits int | Value
        lovelace = int(getattr(amt, "coin", amt))
        ma = getattr(amt, "multi_asset", None)
    value: dict[str, Any] = {"ada": {"lovelace": lovelace}}
    if ma:
        for pol, assets in ma.items():
            pol_hex = bytes(pol).hex()
            bucket: dict[str, int] = {}
            for an, qty in assets.items():
                bucket[bytes(an).hex()] = int(qty)
            value[pol_hex] = bucket

    item: dict[str, Any] = {
        "transaction": {"id": txid_hex},
        "index": idx,
        "address": str(out.address),
        "value": value,
    }

    if out.datum is not None:
        # pycardano stores datums in one of three shapes:
        #   - RawCBOR           → bytes in `.cbor` attribute.
        #   - RawPlutusData     → implements `.to_cbor()` returning bytes.
        #   - PlutusData subcls → implements `.to_cbor()` returning bytes.
        d = out.datum
        if hasattr(d, "cbor") and isinstance(d.cbor, (bytes, bytearray)):
            datum_bytes = bytes(d.cbor)
        elif hasattr(d, "to_cbor"):
            raw = d.to_cbor()
            datum_bytes = raw if isinstance(raw, (bytes, bytearray)) else bytes.fromhex(raw)
        else:
            datum_bytes = bytes(d)
        item["datum"] = datum_bytes.hex()

    if out.script is not None:
        # Ogmios v6 script reference shape.
        from pycardano import PlutusV3Script, PlutusV2Script, PlutusV1Script
        if isinstance(out.script, PlutusV3Script):
            item["script"] = {"language": "plutus:v3", "cbor": bytes(out.script).hex()}
        elif isinstance(out.script, PlutusV2Script):
            item["script"] = {"language": "plutus:v2", "cbor": bytes(out.script).hex()}
        elif isinstance(out.script, PlutusV1Script):
            item["script"] = {"language": "plutus:v1", "cbor": bytes(out.script).hex()}
        # else: native script — we don't emit (shouldn't occur in module-1)
    return item


def _utxo_ref(utxo) -> str:
    """"{txid_hex}#{idx}" for the consumed-refs set."""
    return f"{bytes(utxo.input.transaction_id).hex()}#{int(utxo.input.index)}"


def _input_ref(txinput) -> str:
    return f"{bytes(txinput.transaction_id).hex()}#{int(txinput.index)}"


# ───────────────────────────────────────────────────────────────────────
# Overlay — shared state between the simulating context and the patched
# submit_tx. A single instance is passed around.
# ───────────────────────────────────────────────────────────────────────

class UTxOOverlay:
    """In-memory model of consumed inputs + simulated outputs.

    After each successful preflight-eval we:
      - Decode the signed Transaction from CBOR bytes.
      - Compute tx_hash = Transaction.id (blake2b_256 of tx body).
      - Add (tx_hash, i) for each output of the tx to ``_added_utxos``.
      - Add every input's ref string to ``_consumed_refs``.
      - Advance ``_slot_offset`` by 25 (one block-worth of slots).

    UTxO lookups (``utxos(addr)``, ``resolve_utxo``) consult the overlay:
    real_chain_result  minus consumed_refs  plus matching added UTxOs.
    """

    def __init__(self, *, starting_slot_offset: int = 0) -> None:
        self._consumed_refs: set[str] = set()
        # list[UTxO] so we iterate in insertion order (stable for debugging).
        self._added_utxos: list = []
        self._slot_offset: int = int(starting_slot_offset)

    # ── mutation ────────────────────────────────────────────────────
    def apply_tx(self, tx, tx_hash_hex: str) -> dict:
        """Record a preflight-eval'd TX's effects in the overlay.

        Returns a small summary dict used for the per-step log line.

        Datum normalization: pycardano deserializes outputs' datums as
        ``RawPlutusData`` (or typed subclasses) which do NOT expose a
        ``.cbor`` attribute — but downstream tx_builder code expects
        ``datum.cbor`` to be a bytes blob. We rewrite each output's datum
        to ``RawCBOR(bytes)`` so the `hasattr(d, 'cbor')` branch in every
        builder's ``resolve_utxo`` consumer lands correctly.
        """
        from pycardano import (
            RawCBOR, TransactionInput, TransactionOutput, UTxO,
        )
        from pycardano.hash import TransactionId

        body = tx.transaction_body
        # Consumed inputs → refs.
        n_inputs = 0
        for ti in body.inputs:
            self._consumed_refs.add(_input_ref(ti))
            n_inputs += 1

        # Produced outputs → UTxOs keyed by (tx_hash, i).
        n_outputs = 0
        tx_id_bytes = bytes.fromhex(tx_hash_hex)
        for i, out in enumerate(body.outputs):
            new_ti = TransactionInput(TransactionId(tx_id_bytes), i)
            # Normalize datum to RawCBOR so downstream consumers work.
            if out.datum is not None and not (
                hasattr(out.datum, "cbor")
                and isinstance(getattr(out.datum, "cbor", None), (bytes, bytearray))
            ):
                d = out.datum
                if hasattr(d, "to_cbor"):
                    raw = d.to_cbor()
                    cbor_bytes = raw if isinstance(raw, (bytes, bytearray)) \
                        else bytes.fromhex(raw)
                    # Build a fresh TransactionOutput with the normalized datum
                    # so we don't mutate the original Transaction object.
                    new_out = TransactionOutput(
                        out.address, out.amount,
                        datum=RawCBOR(cbor_bytes),
                        script=out.script,
                    )
                    self._added_utxos.append(UTxO(new_ti, new_out))
                    n_outputs += 1
                    continue
            self._added_utxos.append(UTxO(new_ti, out))
            n_outputs += 1

        # Model REAL block-inclusion latency. On Vector testnet, submit
        # → block inclusion takes ~10–30s depending on load. Using +15s
        # per tx gives preflight realistic slot progression that mirrors
        # live timing gates. Previously +1 per step caused preflight to
        # pass tight commit_window lifecycles that then failed live (see
        # verify #22, OutsideValidityIntervalUTxO).
        self._slot_offset += 15
        return {"consumed": n_inputs, "added": n_outputs}

    # ── lookup helpers ──────────────────────────────────────────────
    def filter_chain_utxos(self, real_utxos: list, address: str) -> list:
        """Remove consumed UTxOs from a real-chain query result."""
        return [u for u in real_utxos if _utxo_ref(u) not in self._consumed_refs]

    def added_at_address(self, address: str) -> list:
        """Return simulated UTxOs whose address string matches."""
        return [u for u in self._added_utxos
                if str(u.output.address) == address
                and _utxo_ref(u) not in self._consumed_refs]

    def lookup_ref(self, txid_hex: str, idx: int):
        """Find a simulated UTxO by ref; returns None if not in overlay."""
        target = f"{txid_hex}#{idx}"
        if target in self._consumed_refs:
            return "CONSUMED"
        for u in self._added_utxos:
            if _utxo_ref(u) == target:
                return u
        return None

    def all_simulated_utxos(self) -> list:
        """Every simulated (not-yet-consumed) UTxO — used as additionalUtxo."""
        return [u for u in self._added_utxos if _utxo_ref(u) not in self._consumed_refs]


# ───────────────────────────────────────────────────────────────────────
# SimulatingOgmiosContext — drop-in replacement for OgmiosContext.
# ───────────────────────────────────────────────────────────────────────

class SimulatingOgmiosContext:
    """Wraps an OgmiosContext and returns overlay-aware UTxO views.

    Constructors of this class take an ``overlay``; that overlay is
    typically a shared singleton held by the harness so every
    ``SimulatingOgmiosContext()`` instance (including the ones constructed
    inside ``_step_*`` helpers) sees the same state.

    ``_shared_real_ctx_class`` is captured by the PreflightPatcher BEFORE
    it monkey-patches ``simulation.chain.OgmiosContext`` — without that
    capture, ``from simulation.chain import OgmiosContext`` inside __init__
    would recursively return this same class.
    """

    _shared_overlay: UTxOOverlay | None = None
    _shared_real_ctx_class = None  # set by PreflightPatcher

    def __init__(self, *args, **kwargs) -> None:
        # The scenario constructs ``OgmiosContext()`` with no args in every
        # lifecycle step helper. We ignore args and consult the shared
        # overlay so monkey-patching ``simulation.chain.OgmiosContext`` to
        # this class works without threading an overlay through.
        if SimulatingOgmiosContext._shared_real_ctx_class is None:
            raise RuntimeError(
                "SimulatingOgmiosContext._shared_real_ctx_class not set — the "
                "PreflightPatcher must capture the real OgmiosContext class "
                "before installing the patch."
            )
        self._real = SimulatingOgmiosContext._shared_real_ctx_class()
        self._overlay = SimulatingOgmiosContext._shared_overlay
        if self._overlay is None:
            raise RuntimeError(
                "SimulatingOgmiosContext._shared_overlay not set — preflight "
                "harness must install an overlay before constructing contexts."
            )

    @property
    def protocol_param(self):
        return self._real.protocol_param

    @property
    def last_block_slot(self):
        # Advance the slot as steps simulate — the builders use this for
        # validity_start / ttl, and downstream time-gate checks need it to
        # move forward (e.g. commit_deadline progression).
        return int(self._real.last_block_slot) + self._overlay._slot_offset

    def genesis_param(self):
        return self._real.genesis_param()

    def utxos(self, address):
        """Return overlay-adjusted UTxOs at ``address``.

        Algorithm:
            real   = OgmiosContext.utxos(address)
            kept   = real minus consumed_refs
            added  = simulated UTxOs whose address == address
            return kept + added
        """
        real = self._real.utxos(address)
        kept = self._overlay.filter_chain_utxos(real, str(address))
        added = self._overlay.added_at_address(str(address))
        return kept + added


# ───────────────────────────────────────────────────────────────────────
# Patched helpers: resolve_utxo, submit_tx, ogmios_rpc wrapper.
# ───────────────────────────────────────────────────────────────────────

def _make_patched_resolve_utxo(real_resolve, overlay: UTxOOverlay):
    def _resolve(txid_str, idx):
        hit = overlay.lookup_ref(txid_str, int(idx))
        if hit == "CONSUMED":
            raise RuntimeError(
                f"preflight: UTxO {txid_str}#{idx} is already consumed by an "
                f"earlier simulated TX — builder asked for a spent input."
            )
        if hit is not None:
            return hit
        # Not in overlay — fall back to real chain query.
        return real_resolve(txid_str, idx)
    return _resolve


def _make_patched_ogmios_rpc(real_ogmios_rpc, overlay: UTxOOverlay):
    """Wrap ogmios_rpc so every evaluateTransaction call carries the
    overlay's simulated UTxOs as ``additionalUtxo``.

    This matters for the BUILDER's first-pass evaluate_and_rebuild call —
    without additionalUtxo injection, eval would reject any TX that
    consumes an overlay-simulated input (because those inputs don't exist
    on chain yet).
    """
    def _patched(method, params=None):
        if method == "evaluateTransaction":
            params = dict(params or {})
            added_utxos = overlay.all_simulated_utxos()
            if added_utxos:
                extra = [_utxo_to_ogmios_additional(u) for u in added_utxos]
                # Merge with any caller-supplied additionalUtxo (there
                # shouldn't be any in our codebase, but be defensive).
                existing = params.get("additionalUtxo", [])
                params["additionalUtxo"] = list(existing) + extra
        return real_ogmios_rpc(method, params)
    return _patched


def _make_patched_submit_tx(overlay: UTxOOverlay, ogmios_rpc_fn,
                            results_log: list):
    """Return a replacement ``submit_tx`` that:
      1. Parses the signed Transaction from CBOR bytes.
      2. Calls evaluateTransaction with overlay-injected additionalUtxo.
      3. On eval errors, dumps full JSON + raises.
      4. On success, applies the tx to the overlay and returns tx_hash.
    """
    from pycardano import Transaction

    def _submit(tx_bytes):
        tx = Transaction.from_cbor(tx_bytes)
        tx_hash_hex = bytes(tx.id).hex()
        tx_hex = tx_bytes.hex() if isinstance(tx_bytes, (bytes, bytearray)) \
            else bytes.fromhex(tx_bytes).hex()

        # ogmios_rpc_fn is our WRAPPED variant — it injects additionalUtxo.
        eval_result = ogmios_rpc_fn("evaluateTransaction",
                                    {"transaction": {"cbor": tx_hex}})

        eval_errors = []
        if isinstance(eval_result, list):
            for item in eval_result:
                if "error" in item:
                    eval_errors.append(item)

        if eval_errors:
            PREFLIGHT_ERROR_DIR.mkdir(parents=True, exist_ok=True)
            step_idx = len(results_log) + 1
            dump = {
                "step_index": step_idx,
                "tx_hash": tx_hash_hex,
                "eval_errors": eval_errors,
                "full_eval_result": eval_result,
                "tx_cbor_hex": tx_hex,
            }
            dump_path = PREFLIGHT_ERROR_DIR / (
                f"preflight_step{step_idx:02d}_eval_errors.json"
            )
            dump_path.write_text(json.dumps(dump, indent=2, default=str))
            raise PreflightEvalError(
                step_index=step_idx,
                tx_hash=tx_hash_hex,
                eval_errors=eval_errors,
                dump_path=dump_path,
            )

        # Record the eval budgets for the log.
        budgets = []
        if isinstance(eval_result, list):
            for item in eval_result:
                v = item.get("validator", {})
                b = item.get("budget", {})
                budgets.append(
                    f"{v.get('purpose','?')}:{v.get('index','?')}"
                    f" mem={b.get('memory','?')} cpu={b.get('cpu','?')}"
                )

        # Apply to overlay.
        overlay.apply_tx(tx, tx_hash_hex)
        results_log.append({
            "tx_hash": tx_hash_hex,
            "n_inputs": len(tx.transaction_body.inputs),
            "n_outputs": len(tx.transaction_body.outputs),
            "budgets": budgets,
        })
        return tx_hash_hex
    return _submit


class PreflightEvalError(RuntimeError):
    """Raised when Ogmios evaluateTransaction returns script errors.

    Carries the step index, tx_hash, full error list, and dump path.
    """
    def __init__(self, *, step_index: int, tx_hash: str,
                 eval_errors: list, dump_path: Path) -> None:
        super().__init__(
            f"preflight step {step_index} failed: {len(eval_errors)} "
            f"validator error(s); full dump at {dump_path}"
        )
        self.step_index = step_index
        self.tx_hash = tx_hash
        self.eval_errors = eval_errors
        self.dump_path = dump_path


# ───────────────────────────────────────────────────────────────────────
# Patch installer — applies all monkey-patches for the duration of a
# preflight run.
# ───────────────────────────────────────────────────────────────────────

class PreflightPatcher:
    """Context manager that installs all preflight monkey-patches.

    Patches:
      - simulation.chain.OgmiosContext  → SimulatingOgmiosContext factory
      - simulation.chain.ogmios_rpc     → injects additionalUtxo on eval
      - simulation.chain.resolve_utxo   → overlay-aware
      - simulation.tx_builder.resolve_utxo  → overlay-aware
      - simulation.tx_builder.submit_tx → eval-only simulator
      - simulation.chain.submit_tx      → same (for direct callers)
      - simulation.tx_builder.wait_confirm  → no-op
      - simulation.chain.wait_confirm   → no-op
      - simulation.tx_builder.ensure_collateral → no-op (don't submit
        collateral-split TXes during preflight)
      - time.sleep                      → near-no-op (cap at 0.01s)

    The patcher restores all originals on __exit__.
    """

    def __init__(self, overlay: UTxOOverlay, results_log: list) -> None:
        self._overlay = overlay
        self._results_log = results_log
        self._saved: list[tuple[Any, str, Any]] = []

    def _save_and_patch(self, module, attr, new_val):
        self._saved.append((module, attr, getattr(module, attr)))
        setattr(module, attr, new_val)

    def __enter__(self):
        import simulation.chain as _chain
        import simulation.tx_builder as _txb
        import time as _time

        SimulatingOgmiosContext._shared_overlay = self._overlay
        # Capture the real OgmiosContext class BEFORE we patch it — otherwise
        # our SimulatingOgmiosContext.__init__ would recurse when it tries to
        # instantiate a real context.
        SimulatingOgmiosContext._shared_real_ctx_class = _chain.OgmiosContext

        # Patch ogmios_rpc (inject additionalUtxo on evaluateTransaction).
        real_rpc = _chain.ogmios_rpc
        patched_rpc = _make_patched_ogmios_rpc(real_rpc, self._overlay)
        self._save_and_patch(_chain, "ogmios_rpc", patched_rpc)

        # Patch resolve_utxo (overlay-aware) in both modules. chain.resolve_utxo
        # calls chain.ogmios_rpc dynamically (module-level), so re-wrap via
        # chain's own attribute for consistency.
        real_resolve = _chain.resolve_utxo
        patched_resolve = _make_patched_resolve_utxo(real_resolve, self._overlay)
        self._save_and_patch(_chain, "resolve_utxo", patched_resolve)
        self._save_and_patch(_txb, "resolve_utxo", patched_resolve)

        # Patch OgmiosContext (every step helper does "from simulation.chain
        # import OgmiosContext" at CALL TIME, so patching the module-level
        # attribute lands on all subsequent lookups).
        self._save_and_patch(_chain, "OgmiosContext", SimulatingOgmiosContext)

        # Patch submit_tx — the preflight eval simulator.
        patched_submit = _make_patched_submit_tx(
            self._overlay, patched_rpc, self._results_log,
        )
        self._save_and_patch(_chain, "submit_tx", patched_submit)
        self._save_and_patch(_txb, "submit_tx", patched_submit)

        # Patch wait_confirm → advance the virtual slot by `secs` without
        # actually sleeping. This mirrors live wall-clock progression and
        # lets preflight catch timing-window failures before we burn ADA
        # on a live verify.
        overlay_ref = self._overlay
        def _wait_advance(secs: int = 0) -> None:
            try:
                overlay_ref._slot_offset += int(max(0, int(secs)))
            except Exception:
                pass
        self._save_and_patch(_chain, "wait_confirm", _wait_advance)
        self._save_and_patch(_txb, "wait_confirm", _wait_advance)

        # Patch ensure_collateral → no-op. In preflight we don't actually
        # need a collateral UTxO to exist because we never submit — the
        # builder consults context.utxos() which returns whatever the real
        # chain has. The only side-effect ensure_collateral has is to
        # submit a split TX if no pure-ADA UTxO exists, which would burn
        # real ADA. Neutralize it.
        def _noop_ensure(*args, **kwargs) -> None:
            return None
        self._save_and_patch(_txb, "ensure_collateral", _noop_ensure)
        self._save_and_patch(_chain, "ensure_collateral", _noop_ensure)

        # Patch time.sleep → cap at 0.01s for snappy preflight. The
        # scenario's reveal/cleanup steps sleep for commit_window seconds;
        # preflight doesn't need real time progression because the overlay
        # advances slots synthetically.
        real_sleep = _time.sleep
        overlay = self._overlay
        def _fast_sleep(secs: float) -> None:
            # Advance the simulated slot by the full sleep duration (1 slot
            # ≈ 1 second on Vector/Cardano) so downstream time-gate checks
            # (reveal_vote's commit-window-closed guard, cleanup's deadline)
            # see the same slot progression a live lifecycle would.
            try:
                overlay._slot_offset += int(max(0, float(secs)))
            except Exception:
                pass
            return real_sleep(min(float(secs), 0.01))
        self._save_and_patch(_time, "sleep", _fast_sleep)

        return self

    def __exit__(self, *exc):
        for module, attr, orig in reversed(self._saved):
            setattr(module, attr, orig)
        SimulatingOgmiosContext._shared_overlay = None
        SimulatingOgmiosContext._shared_real_ctx_class = None
        return False


# ───────────────────────────────────────────────────────────────────────
# Pretty step-name extractor: infer step name from the most recent
# event(s) emitted by the scenario.
# ───────────────────────────────────────────────────────────────────────

_STEP_EVENT_TO_NAME = {
    "submit_claim_success":        "build_submit_claim",
    "open_challenge_success":      "build_open_challenge",
    "transition_to_voting_success": "build_transition_to_voting",
    "select_jury_success":         "build_select_jury",
    "commit_vote_success":         "build_commit_vote",
    "reveal_vote_success":         "build_reveal_vote",
    "reveal_vote_skipped":         "(reveal intentionally skipped)",
    "resolve_jury_success":        "build_resolve_jury",
    "distribute_rewards_success":  "build_distribute_rewards",
    "cleanup_resolved_success":    "build_cleanup_resolved",
    "juror_withdrawn":             "build_withdraw_juror",
    "juror_withdraw_skipped":      "build_withdraw_juror (skipped)",
    "drained_to_master":           "drain_to_master",
    "drain_subwallet":             "drain_subwallet",
    "drain_skipped":               "drain_skipped",
    "verdict":                     "(verdict emitted)",
    # iter-4: WithdrawClaim scenario.
    "wait_for_challenge_window_complete": "wait_for_challenge_window",
    "withdraw_claim_success":      "build_withdraw_claim",
    # iter-4: SlashNonReveal scenario.
    "slash_non_reveal_success":    "build_slash_non_reveal",
    "timeout_resolve_success":     "build_timeout_resolve",
    "reset_stale_active_case_success": "build_reset_stale_active_case",
    "reset_stale_active_case_skipped": "build_reset_stale_active_case (skipped)",
}


def _infer_step_name(events: list[dict]) -> str:
    # Prefer a concrete build_* name over the verdict marker (the verdict
    # event is emitted alongside resolve_jury / withdraw_claim /
    # timeout_resolve — use the underlying build_* label for the log).
    prefer_last = [
        e for e in reversed(events)
        if e.get("event_type") in _STEP_EVENT_TO_NAME
        and e.get("event_type") != "verdict"
    ]
    if not prefer_last:
        prefer_last = [
            e for e in reversed(events)
            if e.get("event_type") in _STEP_EVENT_TO_NAME
        ]
    for e in prefer_last:
        et = e.get("event_type")
        base = _STEP_EVENT_TO_NAME[et]
        # Per-juror labels for clarity.
        if et in ("commit_vote_success", "reveal_vote_success",
                  "distribute_rewards_success",
                  "reset_stale_active_case_success"):
            ji = e.get("juror_index", "?")
            return f"{base}[juror {ji}]"
        if et == "juror_withdrawn":
            return f"{base}[pool {e.get('pool_index','?')}]"
        return base
    return "(unknown)"


# ───────────────────────────────────────────────────────────────────────
# Driver: loop decide_and_act_for_epoch until self._step == "done".
# ───────────────────────────────────────────────────────────────────────

def _advance_stuck_step(scenario) -> None:
    """Manually bump past a step that raised BEFORE emitting a TX.

    Only used under ``--continue-on-error``. Advances per-juror indices
    when inside a loop-step (commit_vote / reveal_vote / distribute_rewards
    / withdraw_jurors / reset_stale_active), otherwise flips the scenario's
    ``_step`` to the next logical stage in the lifecycle dispatch.
    """
    step = scenario._step
    _next = {
        "submit_claim": "open_challenge",
        "open_challenge": "transition_to_voting",
        "transition_to_voting": "select_jury",
        "select_jury": "commit_vote",
        "resolve_jury": "distribute_rewards",
        "cleanup_resolved": "withdraw_jurors",
        "drain_to_master": "done",
        # iter-4: WithdrawClaim scenario.
        "wait_for_challenge_window": "withdraw_claim",
        "withdraw_claim": "withdraw_jurors",
        # iter-4: SlashNonReveal scenario.
        "slash_non_reveal": "timeout_resolve",
        "timeout_resolve": "reset_stale_active",
    }
    if step == "commit_vote":
        scenario._commit_index += 1
        if scenario._commit_index >= scenario.jury_size:
            scenario._step = "reveal_vote"
            scenario._reveal_index = 0
    elif step == "reveal_vote":
        scenario._reveal_index += 1
        if scenario._reveal_index >= scenario.jury_size:
            # SlashNonReveal subclass transitions to slash_non_reveal.
            next_step = "slash_non_reveal" if hasattr(
                scenario, "_slash_index"
            ) else "resolve_jury"
            scenario._step = next_step
    elif step == "distribute_rewards":
        scenario._distribute_index += 1
        if scenario._distribute_index >= scenario.jury_size:
            scenario._step = "cleanup_resolved"
    elif step == "reset_stale_active":
        scenario._reset_index += 1
        # 4 revealers to reset.
        if scenario._reset_index >= 4:
            scenario._step = "withdraw_jurors"
            scenario._withdraw_index = 0
    elif step == "withdraw_jurors":
        scenario._withdraw_index += 1
        if scenario._withdraw_index >= scenario.pool_size:
            scenario._step = "drain_to_master"
    elif step in _next:
        scenario._step = _next[step]


def _drive_lifecycle(scenario, *, continue_on_error: bool,
                     results_log: list, max_epochs: int = 120) -> int:
    """Drive the scenario one epoch at a time under preflight patches.

    Returns 0 on success, non-zero on first error (unless continue_on_error).
    """
    step_idx = 0
    epoch = (scenario._epoch + 1) if hasattr(scenario, "_epoch") else 0
    any_error = False
    # Track failures per step so --continue-on-error doesn't infinite-loop
    # if a builder raises BEFORE any TX is submitted (e.g. a client-side
    # guard or a NameError in happy_path.py). After 1 failure on a given
    # logical step we abandon it and bump past the stuck step.
    consecutive_same_step_fails: dict[tuple, int] = {}

    while epoch < max_epochs:
        if scenario._step == "done":
            break
        prev_len = len(results_log)
        step_key = (scenario._step,
                    getattr(scenario, "_commit_index", 0),
                    getattr(scenario, "_reveal_index", 0),
                    getattr(scenario, "_distribute_index", 0),
                    getattr(scenario, "_withdraw_index", 0))
        try:
            events = scenario.decide_and_act_for_epoch(epoch)
        except PreflightEvalError as exc:
            step_idx = len(results_log) + 1
            print(
                f"Step {step_idx:>2}: (preflight-eval)            "
                f"... EVAL FAIL  (dump: {exc.dump_path})"
            )
            for ve in exc.eval_errors[:3]:
                v = ve.get("validator", {})
                err = ve.get("error", {})
                print(f"    Validator: {v.get('purpose','?')}:{v.get('index','?')}")
                msg = err.get("message", err) if isinstance(err, dict) else err
                print(f"    Error:     {str(msg)[:300]}")
                traces = ve.get("traces") or err.get("data", {}).get("traces") \
                    if isinstance(err, dict) else None
                if traces:
                    print(f"    Traces:    {traces[:5]}")
            any_error = True
            if not continue_on_error:
                return 1
            # Continue: advance epoch past the failing step.
            epoch += 1
            continue
        except Exception as exc:
            # Non-eval error (client-side guard, builder bug, etc.).
            step_idx = len(results_log) + 1
            print(
                f"Step {step_idx:>2}: (builder/client error at "
                f"{scenario._step!r})       ... FAIL"
            )
            print(f"    {type(exc).__name__}: {exc!s}")
            if not continue_on_error:
                traceback.print_exc()
            any_error = True
            if not continue_on_error:
                return 1
            # Avoid infinite loop when a step raises BEFORE emitting a
            # TX (so the step counter can't auto-advance). Bump to the
            # next logical step manually.
            consecutive_same_step_fails[step_key] = \
                consecutive_same_step_fails.get(step_key, 0) + 1
            if consecutive_same_step_fails[step_key] >= 1:
                _advance_stuck_step(scenario)
            epoch += 1
            continue

        # events may include multiple entries per epoch (e.g. resolve_jury
        # emits both resolve_jury_success AND verdict; drain_to_master
        # emits drain_subwallet×N + drained_to_master). Pair each TX
        # (recorded in results_log since prev_len) with its event name.
        new_txs = results_log[prev_len:]
        step_name = _infer_step_name(events)
        for i, rec in enumerate(new_txs):
            step_idx = prev_len + i + 1
            budget_str = "; ".join(rec["budgets"]) if rec["budgets"] else "(no scripts)"
            print(
                f"Step {step_idx:>2}: {step_name:<42} "
                f"... EVAL OK   "
                f"tx={rec['tx_hash'][:16]}… "
                f"in={rec['n_inputs']} out={rec['n_outputs']} "
                f"[{budget_str[:80]}]"
            )
        if not new_txs and events:
            # Events emitted with no TX (e.g. juror_withdraw_skipped when
            # no ref was recorded, or drain_skipped for empty subwallets).
            for e in events:
                et = e.get("event_type", "?")
                if et in ("juror_withdraw_skipped", "drain_skipped"):
                    print(
                        f"Step  -: {step_name:<42} "
                        f"... SKIPPED   reason={e.get('reason','?')[:80]}"
                    )

        epoch += 1

    if any_error:
        print("\nPreflight FAILED (one or more steps had errors; see dumps above).")
        return 1

    if scenario._step != "done":
        print(
            f"\nPreflight STALLED at step={scenario._step!r} after {epoch} "
            f"epochs (max_epochs={max_epochs})."
        )
        return 2

    print(
        f"\nPreflight PASSED: all {len(results_log)} lifecycle TXes "
        f"evaluated OK (zero ADA spent)."
    )
    return 0


# ───────────────────────────────────────────────────────────────────────
# Main entry point.
# ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--use-chain-state", action="store_true",
                    help="Use live chain state (reads master wallet + queries chain).")
    ap.add_argument("--checkpoint", type=Path, default=None,
                    help="Path to a metrics JSONL from a prior verify run "
                    "(used to recover setup_complete agent_indices).")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="Report all failing steps instead of stopping at first.")
    ap.add_argument("--max-epochs", type=int, default=120,
                    help="Upper bound on driver loop iterations.")
    ap.add_argument("--jury-size", type=int, default=5)
    ap.add_argument("--pool-size", type=int, default=15)
    ap.add_argument(
        "--target-verdict",
        choices=("ClaimerWins", "AuditorWins", "Inconclusive"),
        default="ClaimerWins",
        help="Desired on-chain verdict; drives the per-juror vote pattern.",
    )
    ap.add_argument(
        "--scenario-mode",
        choices=("happy", "withdraw_claim", "slash_non_reveal"),
        default="happy",
        help="Which scenario to drive. 'happy' is the standard full lifecycle "
        "with a resolved verdict; 'withdraw_claim' exercises the no-challenge "
        "happy path; 'slash_non_reveal' exercises the SlashNonReveal + "
        "TimeoutResolve + ResetStaleActiveCase sequence.",
    )
    ap.add_argument("--rng-seed", type=int, default=None)
    ap.add_argument("--scenario-name", type=str, default=None,
                    help="Scenario name (default: preflight_<ts>).")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()

    if not args.use_chain_state and args.checkpoint is None:
        print("Must pass --use-chain-state OR --checkpoint <path>.", file=sys.stderr)
        return 2

    if not MASTER_SKEY_PATH.exists():
        print(f"FAIL: master skey missing at {MASTER_SKEY_PATH}", file=sys.stderr)
        return 2
    if not DEPLOYMENT_PATH.exists():
        print(f"FAIL: deployment missing at {DEPLOYMENT_PATH}", file=sys.stderr)
        return 2

    from pycardano import Address, PaymentSigningKey, PaymentVerificationKey
    from simulation.config import NETWORK
    from simulation.scenarios.happy_path import HappyPathScenario
    from simulation.scenarios.withdraw_claim import WithdrawClaimScenario
    from simulation.scenarios.slash_non_reveal import SlashNonRevealScenario

    deployment = json.loads(DEPLOYMENT_PATH.read_text())
    master_skey = PaymentSigningKey.load(str(MASTER_SKEY_PATH))
    master_vkey = PaymentVerificationKey.from_signing_key(master_skey)
    master_addr = Address(master_vkey.hash(), network=NETWORK)

    rng_seed = args.rng_seed if args.rng_seed is not None \
        else (int(time.time()) & 0xFFFFFFFF)
    name = args.scenario_name or f"preflight_{int(time.time())}"

    overlay = UTxOOverlay()
    results_log: list = []

    PREFLIGHT_ERROR_DIR.mkdir(parents=True, exist_ok=True)

    # tmpdir for metrics / checkpoint so we don't clobber a live verify run.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        metrics_dir = tmp / "metrics"
        checkpoint_dir = tmp / "ckpt"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # If checkpoint (prior metrics JSONL) was supplied, seed the
        # scenario's metrics file so _scan_setup_complete_event finds the
        # setup_complete event and skips on-chain setup.
        if args.checkpoint is not None:
            src = Path(args.checkpoint)
            if not src.exists():
                print(f"FAIL: checkpoint {src} missing", file=sys.stderr)
                return 2
            # Copy the prior events into the scenario's metrics file so
            # the built-in restart path finds setup_complete.
            dst = metrics_dir / f"{name}.jsonl"
            dst.write_text(src.read_text())
            # Rewrite each event's `scenario` field to our new name so the
            # scan picks it up. (If the source already uses our name the
            # replacement is a no-op.)
            lines = []
            for line in dst.read_text().splitlines():
                try:
                    ev = json.loads(line)
                    if "scenario" in ev:
                        ev["scenario"] = name
                        lines.append(json.dumps(ev))
                        continue
                except Exception:
                    pass
                lines.append(line)
            dst.write_text("\n".join(lines) + "\n")

        scenario_cls = {
            "happy": HappyPathScenario,
            "withdraw_claim": WithdrawClaimScenario,
            "slash_non_reveal": SlashNonRevealScenario,
        }[args.scenario_mode]

        common_kwargs = dict(
            name=name,
            config={"epochs_per_day": 24, "n_agents": 2 + args.pool_size},
            deployment=deployment,
            master_skey=master_skey,
            master_vkey=master_vkey,
            master_wallet_addr=master_addr,
            checkpoint_dir=checkpoint_dir,
            metrics_dir=metrics_dir,
            rng_seed=rng_seed,
            jury_size=args.jury_size,
            pool_size=args.pool_size,
            target_verdict=args.target_verdict,
        )
        scenario = scenario_cls(**common_kwargs)

        print(f"Preflight harness running against {'chain' if args.use_chain_state else 'checkpoint'}")
        print(f"  mode:     {args.scenario_mode}")
        print(f"  scenario: {scenario.name}")
        print(f"  master:   {master_addr}")
        print(f"  jury:     {args.jury_size}    pool: {args.pool_size}")
        print(f"  seed:     {rng_seed}")
        print()

        # Drive under preflight patches.
        t0 = time.time()
        try:
            with PreflightPatcher(overlay, results_log):
                rc = _drive_lifecycle(
                    scenario,
                    continue_on_error=args.continue_on_error,
                    results_log=results_log,
                    max_epochs=args.max_epochs,
                )
        except Exception as exc:
            traceback.print_exc()
            print(f"Preflight crashed: {type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        dt = time.time() - t0
        print(f"\nTotal: {len(results_log)} tx evaluated in {dt:.1f}s "
              f"(final step = {scenario._step!r})")
        return rc


if __name__ == "__main__":
    sys.exit(main())
