"""Unit tests for Fix A / Fix B chain helpers.

Covers:
    simulation.chain.pick_pure_ada_collateral
    simulation.chain.prepare_fee_payer_utxos

Both helpers underpin the robustness fixes that eliminate two recurring
sim failures on Vector testnet:

  Fix A — prepare_fee_payer_utxos pre-splits master's wallet so each of
  the 5 commit_vote / reveal_vote TXes has its own dedicated fee-payer
  UTxO. This lets us submit all 5 concurrently and wait_confirm ONCE,
  cutting commit/reveal wall-time by ~5x and absorbing block-inclusion
  latency variance that previously killed every 3rd run against the
  180 s v15 commit_window.

  Fix B — pick_pure_ada_collateral bypasses pycardano's auto-collateral
  picker (which has been observed to select token-laden UTxOs and
  trigger CollateralContainsNonADA at submit) by returning an explicit
  pure-ADA UTxO the caller pins via ``builder.collaterals = [...]``.

These tests use a lightweight in-memory UTxO stand-in and assert only
on pure Python behaviour — no Ogmios contact.
"""
from __future__ import annotations

import pytest

from pycardano import (
    Address, AssetName, Asset, MultiAsset, Network,
    PaymentSigningKey, ScriptHash,
    TransactionId, TransactionInput, TransactionOutput, UTxO, Value,
)

from simulation.chain import (
    pick_pure_ada_collateral, prepare_fee_payer_utxos, ensure_collateral,
)


# ─── Helpers ──────────────────────────────────────────────────────────

def _mk_txid(seed: int) -> TransactionId:
    """Deterministic 32-byte tx id seeded by an int."""
    return TransactionId(seed.to_bytes(32, "big"))


def _mk_utxo(addr: Address, lovelace: int, *, with_token: bool = False,
             idx: int = 0, seed: int = 0) -> UTxO:
    """Build a UTxO at ``addr`` with the given value."""
    ma: MultiAsset | None = None
    if with_token:
        policy = ScriptHash(b"\x11" * 28)
        asset = Asset()
        asset[AssetName(b"FAKE_TOKEN")] = 1
        ma = MultiAsset()
        ma[policy] = asset
    amount: Value | int
    amount = Value(lovelace, ma) if ma else lovelace
    ti = TransactionInput(_mk_txid(seed or (idx + 1)), idx)
    return UTxO(ti, TransactionOutput(addr, amount))


class InMemContext:
    """Tiny fake Ogmios-ish context that exposes only ``utxos(addr)``.

    The helpers under test call ``context.utxos(str(addr))`` and nothing
    else on the context. TransactionBuilder construction inside the
    split path is exercised by the integration helpers (which this
    suite mocks away via ``submit_tx``/``wait_confirm`` monkeypatches).
    """

    def __init__(self) -> None:
        self._utxos: dict[str, list[UTxO]] = {}

    def register(self, addr: Address, utxos: list[UTxO]) -> None:
        self._utxos[str(addr)] = utxos

    def utxos(self, addr) -> list[UTxO]:
        return list(self._utxos.get(str(addr), []))


@pytest.fixture
def ctx() -> InMemContext:
    return InMemContext()


@pytest.fixture
def addr() -> Address:
    """Stable test address — identity not meaningful, only used as a key."""
    skey = PaymentSigningKey.generate()
    vkey = skey.to_verification_key()
    return Address(payment_part=vkey.hash(), network=Network.TESTNET)


# ─── pick_pure_ada_collateral ─────────────────────────────────────────

class TestPickPureAdaCollateral:

    def test_returns_smallest_pure_ada_utxo_meeting_minimum(self, ctx, addr):
        u_small = _mk_utxo(addr, 5_000_000, seed=1)
        u_mid = _mk_utxo(addr, 10_000_000, seed=2)
        u_big = _mk_utxo(addr, 20_000_000, seed=3)
        ctx.register(addr, [u_big, u_mid, u_small])

        picked = pick_pure_ada_collateral(ctx, addr, min_ada_lovelace=5_000_000)

        assert picked is u_small, (
            "expected the SMALLEST eligible UTxO (bound-minimising; "
            "keeps larger UTxOs available for fee-payers and change)"
        )

    def test_skips_utxos_below_minimum(self, ctx, addr):
        u_too_small = _mk_utxo(addr, 2_000_000, seed=1)
        u_ok = _mk_utxo(addr, 7_000_000, seed=2)
        ctx.register(addr, [u_too_small, u_ok])

        picked = pick_pure_ada_collateral(ctx, addr, min_ada_lovelace=5_000_000)

        assert picked is u_ok

    def test_skips_token_laden_utxos(self, ctx, addr):
        """A UTxO carrying ANY multi-asset must be rejected even if the
        ADA amount alone exceeds the minimum — this is the core Fix B
        invariant."""
        u_token = _mk_utxo(addr, 50_000_000, with_token=True, seed=1)
        u_pure = _mk_utxo(addr, 6_000_000, seed=2)
        ctx.register(addr, [u_token, u_pure])

        picked = pick_pure_ada_collateral(ctx, addr, min_ada_lovelace=5_000_000)

        # The 50 ADA token UTxO must NOT be picked even though its coin
        # is larger — CollateralContainsNonADA would reject on submit.
        assert picked is u_pure

    def test_raises_when_no_eligible_utxo_after_scan(self, ctx, addr,
                                                    monkeypatch):
        u_token = _mk_utxo(addr, 50_000_000, with_token=True, seed=1)
        ctx.register(addr, [u_token])

        # Neutralise ensure_collateral (the helper calls it to try a
        # split) so the test doesn't attempt a real TX submission.
        import simulation.chain as _chain
        monkeypatch.setattr(_chain, "ensure_collateral",
                            lambda *a, **kw: None)

        with pytest.raises(RuntimeError, match="pure-ADA UTxO"):
            pick_pure_ada_collateral(ctx, addr, min_ada_lovelace=5_000_000)

    def test_raises_when_wallet_empty(self, ctx, addr, monkeypatch):
        ctx.register(addr, [])

        import simulation.chain as _chain
        monkeypatch.setattr(_chain, "ensure_collateral",
                            lambda *a, **kw: None)

        with pytest.raises(RuntimeError):
            pick_pure_ada_collateral(ctx, addr)


# ─── prepare_fee_payer_utxos ──────────────────────────────────────────

class TestPrepareFeePayerUtxos:

    def test_returns_requested_count_when_wallet_already_split(self, ctx, addr):
        # 6 pure-ADA UTxOs of 10 ADA each. Asking for 5 with
        # reserve_collateral=True should return 5 (dropping the smallest
        # for the collateral slot).
        utxos = [_mk_utxo(addr, 10_000_000, seed=i + 1) for i in range(6)]
        ctx.register(addr, utxos)

        result = prepare_fee_payer_utxos(
            ctx, None, None, addr, count=5,
            amount_lovelace=10_000_000,
            reserve_collateral=True,
        )

        assert len(result) == 5
        # All returned UTxOs must be pure ADA AND >= 10 ADA.
        for u in result:
            amt = u.output.amount
            coin = amt if isinstance(amt, int) else amt.coin
            assert coin >= 10_000_000

    def test_reserves_smallest_for_collateral(self, ctx, addr):
        """When reserve_collateral=True, the smallest eligible UTxO
        must NOT appear in the returned set — it's reserved for
        pick_pure_ada_collateral to find."""
        # 3 UTxOs: 10/11/12 ADA.
        u10 = _mk_utxo(addr, 10_000_000, seed=1)
        u11 = _mk_utxo(addr, 11_000_000, seed=2)
        u12 = _mk_utxo(addr, 12_000_000, seed=3)
        ctx.register(addr, [u10, u11, u12])

        result = prepare_fee_payer_utxos(
            ctx, None, None, addr, count=2,
            amount_lovelace=10_000_000,
            reserve_collateral=True,
        )

        # Must exclude the smallest (u10 = 10 ADA = collateral slot).
        assert u10 not in result
        assert len(result) == 2

    def test_does_not_reserve_when_flag_disabled(self, ctx, addr):
        u10 = _mk_utxo(addr, 10_000_000, seed=1)
        u11 = _mk_utxo(addr, 11_000_000, seed=2)
        ctx.register(addr, [u10, u11])

        result = prepare_fee_payer_utxos(
            ctx, None, None, addr, count=2,
            amount_lovelace=10_000_000,
            reserve_collateral=False,
        )

        assert len(result) == 2
        assert u10 in result and u11 in result

    def test_raises_without_keys_when_split_needed(self, ctx, addr):
        # Only 2 eligible, caller asks for 5 — split needed.
        utxos = [_mk_utxo(addr, 10_000_000, seed=i + 1) for i in range(2)]
        ctx.register(addr, utxos)

        with pytest.raises(RuntimeError, match="Signing keys required"):
            prepare_fee_payer_utxos(
                ctx, None, None, addr, count=5,
                amount_lovelace=10_000_000,
                reserve_collateral=True,
            )

    def test_raises_when_insufficient_total_ada(self, ctx, addr):
        # One small UTxO — way short of 5 × 10 ADA = 50 ADA target,
        # AND no token-laden fallback inputs available.
        ctx.register(addr, [_mk_utxo(addr, 5_000_000, seed=1)])

        skey = PaymentSigningKey.generate()
        vkey = skey.to_verification_key()

        with pytest.raises(RuntimeError, match="but needs"):
            prepare_fee_payer_utxos(
                ctx, skey, vkey, addr, count=5,
                amount_lovelace=10_000_000,
                reserve_collateral=True,
            )

    def test_consumes_token_laden_utxo_when_pure_ada_insufficient(
        self, ctx, addr, monkeypatch,
    ):
        """Fix A requirement: when the master wallet is consolidated
        into a token-laden UTxO (typical steady-state on testnet after
        drains), the split TX must pull that UTxO in. The change output
        receives the tokens back, so fee-payer slots stay pure-ADA.

        We stub TransactionBuilder.build_and_sign (the first call that
        would otherwise hit pycardano's balancing) so the helper's
        pre-build path is exercised without a real TX round-trip —
        verifying that the token-laden UTxO was added via add_input
        rather than rejected for insufficient pure-ADA."""
        # 5 ADA pure + 18652 ADA token-laden (mirrors the real master's
        # steady state at the time this fix was written).
        u_pure = _mk_utxo(addr, 5_000_000, seed=1)
        u_token = _mk_utxo(addr, 18_652_000_000, with_token=True, seed=2)
        ctx.register(addr, [u_pure, u_token])

        import simulation.chain as _chain
        from pycardano import TransactionBuilder

        captured_inputs = []

        class FakeTx:
            def to_cbor(self):
                return b"\x00" * 32

        def _capture(self, signing_keys, change_address=None):
            # Record the builder's accumulated inputs at build time.
            for inp in self.inputs:
                captured_inputs.append(inp)
            return FakeTx()

        monkeypatch.setattr(
            TransactionBuilder, "build_and_sign", _capture,
        )
        monkeypatch.setattr(
            _chain, "submit_tx",
            lambda tx_bytes: "fake_split_tx_" + "00" * 14,
        )
        monkeypatch.setattr(_chain, "wait_confirm", lambda *a, **kw: None)

        # Post-split scan won't find fresh UTxOs (nothing was actually
        # submitted — our fake builder just captured inputs), so we
        # expect a "post-split scan" RuntimeError. The key assertion
        # is that the helper REACHED that point — meaning the
        # token-laden fallback input was accepted, NOT rejected on
        # insufficient pure-ADA.
        with pytest.raises(RuntimeError, match="post-split scan"):
            prepare_fee_payer_utxos(
                ctx,
                PaymentSigningKey.generate(),
                PaymentSigningKey.generate().to_verification_key(),
                addr,
                count=5,
                amount_lovelace=10_000_000,
                reserve_collateral=True,
            )

        # Assert the token-laden UTxO was pulled into the split TX.
        assert u_token in captured_inputs, (
            "token-laden UTxO should be consumed when pure-ADA alone "
            "is insufficient for the requested split"
        )
        assert u_pure in captured_inputs, (
            "pure-ADA UTxOs should always be consumed first"
        )

    def test_count_zero_returns_empty(self, ctx, addr):
        ctx.register(addr, [_mk_utxo(addr, 10_000_000, seed=1)])
        result = prepare_fee_payer_utxos(
            ctx, None, None, addr, count=0, amount_lovelace=10_000_000,
        )
        assert result == []

    def test_smaller_than_requested_are_not_eligible(self, ctx, addr):
        """UTxOs smaller than ``amount_lovelace`` must NOT count toward
        the available-count. This is the correctness invariant that
        lets the caller rely on every returned UTxO being big enough
        to cover its per-TX fee + min-UTxO change floor."""
        u_small = _mk_utxo(addr, 3_000_000, seed=1)
        u_small2 = _mk_utxo(addr, 4_000_000, seed=2)
        u_big = _mk_utxo(addr, 30_000_000, seed=3)
        ctx.register(addr, [u_small, u_small2, u_big])

        # Only 1 eligible UTxO -> needs a split, but no keys -> raise.
        with pytest.raises(RuntimeError, match="Signing keys required"):
            prepare_fee_payer_utxos(
                ctx, None, None, addr, count=2,
                amount_lovelace=10_000_000,
                reserve_collateral=False,
            )


# ─── ensure_collateral backward compatibility ────────────────────────

class TestEnsureCollateralBackcompat:
    """Fix B changed ensure_collateral internals; legacy callers must
    still work unchanged."""

    def test_noop_when_suitable_utxo_exists(self, ctx, addr):
        ctx.register(addr, [_mk_utxo(addr, 10_000_000, seed=1)])
        # Should return None without attempting a split (signalled by
        # the fact that no submission occurs — we don't monkeypatch
        # submit_tx, so a real submit attempt would blow up).
        result = ensure_collateral(ctx, None, None, addr)
        assert result is None

    def test_accepts_min_ada_lovelace_kwarg(self, ctx, addr):
        """New keyword arg must be opt-in; default preserves legacy
        5 ADA threshold."""
        ctx.register(addr, [_mk_utxo(addr, 6_000_000, seed=1)])
        # 6 ADA passes default min 5 ADA -> no-op.
        assert ensure_collateral(ctx, None, None, addr) is None
        # 6 ADA fails explicit min 10 ADA -> tries to split; with keys=None
        # it short-circuits without submitting (scan-only mode).
        assert ensure_collateral(
            ctx, None, None, addr, min_ada_lovelace=10_000_000,
        ) is None
