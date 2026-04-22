"""
Chain interaction layer — Ogmios RPC, UTxO resolution, TX submission.
Extracted from testnet/deploy_and_run_v10.py for reuse.
"""
import cbor2
import hashlib
import json
import os
import requests
import time

from pycardano import (
    Address, Network, PaymentSigningKey, PaymentVerificationKey,
    PlutusV3Script, TransactionBuilder, TransactionOutput,
    TransactionInput, UTxO, RawCBOR, RawPlutusData, Redeemer,
    MultiAsset, Asset, AssetName, ScriptHash, Value,
    ExecutionUnits, ScriptPubkey, ScriptAll,
)
from pycardano.hash import TransactionId, VerificationKeyHash
from pycardano.backend.base import ProtocolParameters

from simulation.config import OGMIOS_URL, TX_SUBMIT_URL, SYSTEM_START_UNIX, NETWORK


# ═══════════════════════════════════════════════════════════════════════
# PLUTUS DATA HELPER
# ═══════════════════════════════════════════════════════════════════════

class DP(RawPlutusData):
    """Datum/redeemer wrapper for raw CBOR."""
    def to_cbor(self):
        return cbor2.dumps(self.data)


# ═══════════════════════════════════════════════════════════════════════
# OGMIOS RPC
# ═══════════════════════════════════════════════════════════════════════

def ogmios_rpc(method, params=None):
    """Call Ogmios JSON-RPC endpoint."""
    body = {"jsonrpc": "2.0", "method": method, "id": None}
    if params:
        body["params"] = params
    r = requests.post(OGMIOS_URL, json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Ogmios {method} HTTP {r.status_code}: {r.text[:2000]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Ogmios {method}: {data['error']}")
    return data.get("result", data)


# ═══════════════════════════════════════════════════════════════════════
# CONTEXT (Protocol Params + Slot + UTxO queries)
# ═══════════════════════════════════════════════════════════════════════

class OgmiosContext:
    """Lightweight Ogmios-backed chain context for PyCardano."""

    def __init__(self):
        self._pp = None

    def _frac(self, v):
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/")
            return int(a) / int(b)
        return float(v)

    @property
    def protocol_param(self):
        if self._pp is None:
            raw = ogmios_rpc("queryLedgerState/protocolParameters")
            self._pp = ProtocolParameters(
                min_fee_constant=raw.get("minFeeConstant", {}).get("ada", {}).get("lovelace", 155381),
                min_fee_coefficient=raw.get("minFeeCoefficient", 44),
                max_block_size=raw.get("maxBlockBodySize", {}).get("bytes", 90112),
                max_tx_size=raw.get("maxTransactionSize", {}).get("bytes", 16384),
                max_block_header_size=raw.get("maxBlockHeaderSize", {}).get("bytes", 1100),
                key_deposit=raw.get("stakeCredentialDeposit", {}).get("ada", {}).get("lovelace", 2000000),
                pool_deposit=raw.get("stakePoolDeposit", {}).get("ada", {}).get("lovelace", 500000000),
                pool_influence=self._frac(raw.get("stakePoolPledgeInfluence", "3/10")),
                monetary_expansion=self._frac(raw.get("monetaryExpansion", "3/1000")),
                treasury_expansion=self._frac(raw.get("treasuryExpansion", "2/10")),
                decentralization_param=0,
                extra_entropy="",
                protocol_major_version=raw.get("version", {}).get("major", 10),
                protocol_minor_version=raw.get("version", {}).get("minor", 0),
                min_utxo=raw.get("minUtxoDepositCoefficient", 4310),
                min_pool_cost=raw.get("minStakePoolCost", {}).get("ada", {}).get("lovelace", 170000000),
                price_mem=self._frac(raw.get("scriptExecutionPrices", {}).get("memory", "577/10000")),
                price_step=self._frac(raw.get("scriptExecutionPrices", {}).get("cpu", "721/10000000")),
                max_tx_ex_mem=raw.get("maxExecutionUnitsPerTransaction", {}).get("memory", 14000000),
                max_tx_ex_steps=raw.get("maxExecutionUnitsPerTransaction", {}).get("cpu", 10000000000),
                max_block_ex_mem=raw.get("maxExecutionUnitsPerBlock", {}).get("memory", 62000000),
                max_block_ex_steps=raw.get("maxExecutionUnitsPerBlock", {}).get("cpu", 40000000000),
                max_val_size=raw.get("maxValueSize", {}).get("bytes", 5000),
                collateral_percent=raw.get("collateralPercentage", 150),
                max_collateral_inputs=raw.get("maxCollateralInputs", 3),
                coins_per_utxo_word=raw.get("minUtxoDepositCoefficient", 4310),
                coins_per_utxo_byte=raw.get("minUtxoDepositCoefficient", 4310),
                # cost_models MUST mirror chain reality — script_data_hash is
                # computed from (redeemers + datums + language views/cost
                # models). An empty dict here makes pycardano hash language
                # views as empty, but the chain hashes against the real
                # PlutusV3 cost model -> PPViewHashesDontMatch on submit.
                # Mirror v15 deploy script's mapping (Ogmios returns these
                # under raw.plutusCostModels keyed by "plutus:v1/v2/v3").
                cost_models={
                    pc_label: (
                        {i: x for i, x in enumerate(m)}
                        if isinstance(m, list)
                        else {int(k2) if isinstance(k2, str) and k2.isdigit() else k2: x
                              for k2, x in m.items()}
                    )
                    for ogmios_key, pc_label, m in [
                        (k, lbl, raw.get("plutusCostModels", {}).get(k, []))
                        for k, lbl in [
                            ("plutus:v1", "PlutusV1"),
                            ("plutus:v2", "PlutusV2"),
                            ("plutus:v3", "PlutusV3"),
                        ]
                    ]
                    if m
                },
            )
        return self._pp

    @property
    def last_block_slot(self):
        return ogmios_rpc("queryLedgerState/tip").get("slot", 0)

    def genesis_param(self):
        return None

    def utxos(self, address):
        """Query UTxOs at an address via Ogmios."""
        result = ogmios_rpc("queryLedgerState/utxo", {"addresses": [address]})
        utxos = []
        for item in result:
            txid = item["transaction"]["id"]
            idx = item["index"]
            ti = TransactionInput(TransactionId(bytes.fromhex(txid)), idx)

            value = item.get("value", {})
            lovelace = value.get("ada", {}).get("lovelace", 0)

            ma = MultiAsset()
            for policy_hex, assets_map in value.items():
                if policy_hex == "ada":
                    continue
                policy_sh = ScriptHash(bytes.fromhex(policy_hex))
                a = Asset()
                for asset_name_hex, qty in assets_map.items():
                    a[AssetName(bytes.fromhex(asset_name_hex))] = qty
                ma[policy_sh] = a

            pv = Value(lovelace, ma) if ma else lovelace

            datum = None
            if "datum" in item:
                datum_hex = item["datum"]
                datum = RawCBOR(bytes.fromhex(datum_hex))

            script_ref = None
            if "script" in item:
                scr = item["script"]
                # Ogmios v6: {"language": "plutus:v3", "cbor": "..."}
                if scr.get("language") == "plutus:v3":
                    script_ref = PlutusV3Script(bytes.fromhex(scr["cbor"]))
                # Ogmios v5 fallback: {"plutus:v3": "..."}
                elif "plutus:v3" in scr:
                    script_ref = PlutusV3Script(bytes.fromhex(scr["plutus:v3"]))

            addr = Address.from_primitive(item.get("address", address))
            to = TransactionOutput(addr, pv, datum=datum, script=script_ref)
            utxos.append(UTxO(ti, to))
        return utxos


def resolve_utxo(txid_str, idx):
    """Resolve a specific UTxO by TX hash + index."""
    result = ogmios_rpc("queryLedgerState/utxo",
                        {"outputReferences": [{"transaction": {"id": txid_str}, "index": idx}]})
    if not result:
        raise RuntimeError(f"UTxO {txid_str}#{idx} not found")
    item = result[0]

    ti = TransactionInput(TransactionId(bytes.fromhex(txid_str)), idx)
    value = item.get("value", {})
    lovelace = value.get("ada", {}).get("lovelace", 0)

    ma = MultiAsset()
    for policy_hex, assets_map in value.items():
        if policy_hex == "ada":
            continue
        policy_sh = ScriptHash(bytes.fromhex(policy_hex))
        a = Asset()
        for asset_name_hex, qty in assets_map.items():
            a[AssetName(bytes.fromhex(asset_name_hex))] = qty
        ma[policy_sh] = a

    pv = Value(lovelace, ma) if ma else lovelace

    datum = None
    if "datum" in item:
        datum = RawCBOR(bytes.fromhex(item["datum"]))

    script_ref = None
    if "script" in item:
        scr = item["script"]
        # Ogmios v6: {"language": "plutus:v3", "cbor": "..."}
        if scr.get("language") == "plutus:v3":
            script_ref = PlutusV3Script(bytes.fromhex(scr["cbor"]))
        # Ogmios v5 fallback: {"plutus:v3": "..."}
        elif "plutus:v3" in scr:
            script_ref = PlutusV3Script(bytes.fromhex(scr["plutus:v3"]))

    addr = Address.from_primitive(item.get("address", ""))
    to = TransactionOutput(addr, pv, datum=datum, script=script_ref)
    return UTxO(ti, to)


def resolve_ref_utxo(txid, idx):
    """Resolve a reference script UTxO."""
    utxo = resolve_utxo(txid, idx)
    if utxo.output.script is None:
        raise RuntimeError(f"No script at {txid}#{idx}")
    return utxo


# ═══════════════════════════════════════════════════════════════════════
# TX SUBMISSION
# ═══════════════════════════════════════════════════════════════════════

def submit_tx(tx_bytes):
    """Submit a signed TX to the testnet."""
    r = requests.post(TX_SUBMIT_URL,
                      data=tx_bytes,
                      headers={"Content-Type": "application/cbor"},
                      timeout=60)
    if r.status_code != 202:
        # Dump full response to a stable path so we can inspect script traces
        # without the 500-char truncation swallowing the Plutus diagnostics.
        try:
            import os as _os, time as _time
            _dump_dir = _os.path.join("/tmp", "sim-submit-errors")
            _os.makedirs(_dump_dir, exist_ok=True)
            _dump_path = _os.path.join(
                _dump_dir, f"submit_err_{int(_time.time() * 1000)}.txt"
            )
            with open(_dump_path, "w") as _f:
                _f.write(r.text)
        except Exception:
            _dump_path = "<dump failed>"
        raise RuntimeError(
            f"Submit failed ({r.status_code}): full response in {_dump_path}; "
            f"preview: {r.text[:500]}"
        )
    return r.json()


def tx_to_bytes(tx):
    """Convert a signed TX to bytes for submission."""
    tx_cbor = tx.to_cbor()
    return tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)


def wait_confirm(secs=25):
    """Wait for TX confirmation."""
    time.sleep(secs)


# ═══════════════════════════════════════════════════════════════════════
# WALLET HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _is_pure_ada(utxo) -> bool:
    """True when a UTxO has NO multi-asset component (only ADA)."""
    amt = utxo.output.amount
    if isinstance(amt, int):
        return True
    ma = getattr(amt, "multi_asset", None)
    if ma is None:
        return True
    # pycardano's MultiAsset is dict-like; empty dict counts as pure ADA.
    try:
        return len(ma) == 0
    except TypeError:
        return not bool(ma)


def _ada_lovelace(utxo) -> int:
    """Return the lovelace amount of a UTxO (scalar int OR Value.coin)."""
    amt = utxo.output.amount
    if isinstance(amt, int):
        return int(amt)
    return int(getattr(amt, "coin", 0))


def pick_pure_ada_collateral(context, wallet_addr, min_ada_lovelace: int = 5_000_000):
    """Return a pure-ADA UTxO from ``wallet_addr`` suitable for collateral.

    Scans ``context.utxos(wallet_addr)`` and returns the SMALLEST pure-ADA
    UTxO >= ``min_ada_lovelace``. If none exists, auto-splits the wallet
    by calling ``ensure_collateral`` and retries ONCE. Raises if the
    wallet genuinely has no recoverable ADA.

    Rationale: pycardano's TransactionBuilder auto-collateral picker scans
    wallet UTxOs and may select a token-laden UTxO -> the node rejects
    with CollateralContainsNonADA. Pinning collateral explicitly bypasses
    that auto-selection entirely.

    Args:
        context:          OgmiosContext (or compatible).
        wallet_addr:      Address to draw the collateral from.
        min_ada_lovelace: Lower bound on the selected UTxO's coin field.
                          Default 5 ADA — matches v13 collateral convention
                          and comfortably covers collateral_percent=150 of
                          a typical script TX fee.

    Returns:
        UTxO: a pure-ADA UTxO belonging to ``wallet_addr``.

    Raises:
        RuntimeError: wallet has no pure-ADA UTxO >= ``min_ada_lovelace``
                      even after an auto-split attempt.
    """
    def _candidates(utxos):
        return sorted(
            (u for u in utxos
             if _is_pure_ada(u) and _ada_lovelace(u) >= min_ada_lovelace),
            key=_ada_lovelace,
        )

    utxos = context.utxos(str(wallet_addr))
    pure = _candidates(utxos)
    if pure:
        return pure[0]

    # No suitable candidate — coax the wallet into producing one.
    # ensure_collateral below will submit a split TX (5 ADA outputs).
    # After confirmation we retry the scan once.
    # NOTE: use module-level lookup so preflight's monkeypatch of
    # simulation.chain.ensure_collateral takes effect.
    import simulation.chain as _chain_mod
    _chain_mod.ensure_collateral(context, None, None, wallet_addr)
    # ensure_collateral needs keys to sign; without them we can't force
    # a split. In most call sites the collateral ALREADY exists — the
    # helper is idempotent and will no-op — so the second scan should
    # succeed when called after a successful earlier collateral_split.
    utxos = context.utxos(str(wallet_addr))
    pure = _candidates(utxos)
    if pure:
        return pure[0]

    raise RuntimeError(
        f"pick_pure_ada_collateral: wallet {wallet_addr} has no "
        f"pure-ADA UTxO >= {min_ada_lovelace} lovelace. Master may be "
        f"fully consolidated into token-laden UTxOs; run the split "
        f"helper manually or recover ADA from sub-wallets first."
    )


def ensure_collateral(context, skey, vkey, wallet_addr, *,
                      min_ada_lovelace: int = 5_000_000):
    """Ensure ``wallet_addr`` has a pure-ADA UTxO of ``>= min_ada_lovelace``.

    Backward-compatible: callers that used the old ``(context, skey, vkey,
    wallet_addr)`` signature still work unchanged. When a suitable UTxO
    exists we return early (idempotent); otherwise we split the wallet
    into two 5-ADA pure-ADA outputs and wait_confirm.

    NOTE: signing keys may be ``None`` (e.g. from ``pick_pure_ada_collateral``
    when the caller only wants a scan). In that case we skip the split
    attempt and let the caller raise.
    """
    utxos = context.utxos(str(wallet_addr))
    has_suitable = any(
        _is_pure_ada(u) and _ada_lovelace(u) >= min_ada_lovelace
        for u in utxos
    )
    if has_suitable:
        return
    if skey is None or vkey is None:
        # Scan-only mode — no keys -> no split.
        return
    print("    [collateral] Splitting wallet for pure ADA collateral...")
    builder = TransactionBuilder(context)
    builder.fee_buffer = 300_000
    for u in utxos:
        builder.add_input(u)
    builder.add_output(TransactionOutput(wallet_addr, min_ada_lovelace))
    builder.add_output(TransactionOutput(wallet_addr, min_ada_lovelace))
    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx_to_bytes(tx))
    print(f"    [collateral] Split TX: {tx_hash}")
    wait_confirm(secs=25)


def prepare_fee_payer_utxos(
    context, skey, vkey, wallet_addr, count: int,
    amount_lovelace: int = 10_000_000,
    *,
    reserve_collateral: bool = True,
    min_collateral_lovelace: int = 5_000_000,
):
    """Ensure ``wallet_addr`` holds at least ``count`` distinct pure-ADA UTxOs
    of ``>= amount_lovelace`` each, and return them.

    Used by the batched commit_vote / reveal_vote paths: each of the 5
    per-juror TXes pulls its fee from a dedicated pre-split UTxO so there
    is ZERO contention on master's fee-paying UTxOs across 5 concurrent
    submits.

    Selection & split algorithm:
      1. Scan for pure-ADA UTxOs with coin >= ``amount_lovelace``.
      2. If ``reserve_collateral=True``, OMIT the smallest such candidate
         (it is reserved for the script-collateral slot; see
         ``pick_pure_ada_collateral``).
      3. If the remaining set has >= ``count`` candidates, return the
         ``count`` smallest ones (preserving the largest for change).
      4. Otherwise, submit a split TX that drains ALL current pure-ADA
         UTxOs and emits ``count + 1`` outputs (1 collateral, ``count``
         fee payers) + change; wait_confirm; re-scan.

    Args:
        context:                    OgmiosContext (or compatible).
        skey/vkey:                  wallet signing keys.
        wallet_addr:                wallet address.
        count:                      number of fee-payer UTxOs required.
        amount_lovelace:            minimum lovelace per fee-payer UTxO.
                                    Default 10 ADA — each TX burns
                                    ~300-600 k lovelace fee, leaving
                                    ~9.4 ADA of change, comfortably
                                    above the min-UTxO floor.
        reserve_collateral:         if True, exclude one UTxO from the
                                    returned set so a separate collateral
                                    slot is preserved.
        min_collateral_lovelace:    size of the collateral UTxO produced
                                    by the split (only used when a split
                                    is necessary).

    Returns:
        list[UTxO]: ``count`` fee-payer UTxOs, each pure ADA, each
                    coin >= ``amount_lovelace``.

    Raises:
        RuntimeError: master wallet cannot fund ``count`` UTxOs even
                      after an auto-split (insufficient ADA OR all ADA
                      tied up in token-laden UTxOs we can't touch).
    """
    if count <= 0:
        return []

    def _scan():
        utxos = context.utxos(str(wallet_addr))
        pure_ada = sorted(
            (u for u in utxos if _is_pure_ada(u)),
            key=_ada_lovelace,
        )
        eligible = [u for u in pure_ada if _ada_lovelace(u) >= amount_lovelace]
        if reserve_collateral and len(eligible) > count:
            # Drop the SMALLEST one — it's the collateral candidate.
            eligible = eligible[1:]
        return pure_ada, eligible

    _pure_ada, eligible = _scan()
    if len(eligible) >= count:
        # Return the ``count`` smallest eligible UTxOs; keep the largest
        # for change so wallet consolidation naturally survives.
        return eligible[:count]

    # Need a split. Pull pure-ADA UTxOs (avoid token-laden ones so we
    # don't redistribute legacy tokens into fee slots), and all
    # token-laden inputs need to stay untouched.
    if skey is None or vkey is None:
        raise RuntimeError(
            f"prepare_fee_payer_utxos: wallet has "
            f"{len(eligible)} pure-ADA UTxOs >= {amount_lovelace}; "
            f"need {count}. Signing keys required to auto-split but "
            f"none provided."
        )

    total_pure_ada = sum(_ada_lovelace(u) for u in _pure_ada)
    outputs_needed = count + (1 if reserve_collateral else 0)
    min_required = (
        count * amount_lovelace
        + (min_collateral_lovelace if reserve_collateral else 0)
        + 2_000_000  # change + fee headroom
    )

    # Input selection:
    #   - Always include every pure-ADA UTxO (free to consume).
    #   - If pure-ADA alone is insufficient, pull in token-laden UTxOs
    #     in largest-ADA-first order until we have enough. The
    #     TransactionBuilder's change output will receive the tokens
    #     back automatically via change_address=wallet_addr, so the
    #     split TX never strands any asset.
    split_inputs = list(_pure_ada)
    total_input_ada = total_pure_ada
    if total_input_ada < min_required:
        all_utxos = context.utxos(str(wallet_addr))
        token_utxos = sorted(
            (u for u in all_utxos if not _is_pure_ada(u)),
            key=_ada_lovelace,
            reverse=True,
        )
        for tu in token_utxos:
            split_inputs.append(tu)
            total_input_ada += _ada_lovelace(tu)
            if total_input_ada >= min_required:
                break

    if total_input_ada < min_required:
        raise RuntimeError(
            f"prepare_fee_payer_utxos: wallet has "
            f"{total_input_ada} lovelace across all UTxOs (pure+token) "
            f"but needs {min_required} to split into {count} x "
            f"{amount_lovelace} (plus collateral + headroom). Fund "
            f"the wallet or recover ADA from sub-wallets first."
        )

    print(
        f"    [fee-split] Creating {outputs_needed} pure-ADA UTxOs "
        f"({count} x {amount_lovelace} lovelace"
        f"{f' + 1 collateral x {min_collateral_lovelace}' if reserve_collateral else ''}) "
        f"from {len(split_inputs)} input(s) "
        f"({len(_pure_ada)} pure + {len(split_inputs) - len(_pure_ada)} token-laden)..."
    )
    builder = TransactionBuilder(context)
    builder.fee_buffer = 500_000
    for u in split_inputs:
        builder.add_input(u)
    if reserve_collateral:
        builder.add_output(TransactionOutput(wallet_addr, min_collateral_lovelace))
    for _ in range(count):
        builder.add_output(TransactionOutput(wallet_addr, amount_lovelace))
    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_hash = submit_tx(tx_to_bytes(tx))
    print(f"    [fee-split] Split TX: {tx_hash}")

    # Synthesize the fee-payer UTxOs locally rather than re-querying
    # Ogmios after a wait_confirm. Rationale:
    #   - Every output we care about is deterministic: we know the
    #     transaction_id (tx.id), the output indices (0 = collateral
    #     if reserved, then count fee-payer slots), and the values.
    #   - Returning synthesized UTxOs lets us SKIP a wait_confirm
    #     entirely — critical for the v15 commit_window budget.
    #     wait_confirm inside this helper advanced the virtual slot by
    #     15 s under preflight, pushing the commit_vote deadline check
    #     over the 180 s window.
    #   - On the real chain the split TX's outputs bind to a known
    #     tx_hash and are spendable from the next block; we don't need
    #     Ogmios to confirm the tip to use them.
    tx_hash_bytes = bytes.fromhex(tx_hash) if len(tx_hash) == 64 else None
    synthesized: list = []
    if tx_hash_bytes is not None:
        # Index scheme mirrors how the outputs were added above:
        #   idx 0:          collateral (if reserve_collateral) OR first fee-payer
        #   idx 1..count:   fee-payer slots
        #   idx count+k:    change output (appended by pycardano, NOT needed)
        from pycardano.hash import TransactionId as _TxId
        start = 1 if reserve_collateral else 0
        for i in range(count):
            out_idx = start + i
            ti = TransactionInput(_TxId(tx_hash_bytes), out_idx)
            to = TransactionOutput(wallet_addr, amount_lovelace)
            synthesized.append(UTxO(ti, to))

    if synthesized and len(synthesized) == count:
        return synthesized

    # Fallback: could not synthesize (weird tx_hash) — re-query.
    # Bumped 15→30 on 2026-04-21 for mainnet propagation margin.
    wait_confirm(secs=30)
    _, eligible = _scan()
    if len(eligible) < count:
        raise RuntimeError(
            f"prepare_fee_payer_utxos: post-split scan still shows only "
            f"{len(eligible)} eligible UTxOs (wanted {count}). "
            f"Check Ogmios tip lag or the split TX txid={tx_hash}."
        )
    return eligible[:count]


def get_wallet_utxos_no_collateral(context, wallet_addr):
    """Get wallet UTxOs excluding the smallest pure-ADA UTxO (reserved for collateral)."""
    utxos = context.utxos(str(wallet_addr))
    pure_ada = []
    with_tokens = []
    for u in utxos:
        if hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset:
            with_tokens.append(u)
        else:
            pure_ada.append(u)
    if not pure_ada:
        return utxos
    # Reserve smallest pure-ADA UTxO for collateral
    pure_ada.sort(key=lambda u: u.output.amount if isinstance(u.output.amount, int)
                  else u.output.amount.coin)
    return pure_ada[1:] + with_tokens


def evaluate_and_rebuild(builder, skey, vkey, wallet_addr, context):
    """Evaluate TX to get execution budgets, return (tx_bytes, budgets)."""
    tx = builder.build_and_sign([skey], change_address=wallet_addr)
    tx_bytes = tx_to_bytes(tx)
    tx_hex = tx_bytes.hex()

    eval_result = ogmios_rpc("evaluateTransaction", {"transaction": {"cbor": tx_hex}})

    # Ogmios eval's per-validator budgets diverge from real on-chain
    # execution on Vector testnet — by up to ~5% on mem AND cpu per run.
    # Pin to 2x to absorb that variance. Higher fees are acceptable in
    # sim-testnet; correctness over minimality.
    _BUDGET_SAFETY = 2.0
    budgets = {}
    eval_errors = []
    if isinstance(eval_result, list):
        for item in eval_result:
            v = item.get("validator", {})
            key = f"{v.get('purpose', '?')}:{v.get('index', '?')}"
            if "error" in item:
                eval_errors.append({"validator": v, "error": item["error"]})
                continue
            budget = item.get("budget", {})
            mem_raw = budget.get("memory", 500000)
            cpu_raw = budget.get("cpu", 200000000)
            budgets[key] = {
                "mem": int(mem_raw * _BUDGET_SAFETY),
                "cpu": int(cpu_raw * _BUDGET_SAFETY),
            }
    elif isinstance(eval_result, dict):
        for key, val in eval_result.items():
            if isinstance(val, dict) and "memory" in val:
                budgets[key] = {
                    "mem": int(val["memory"] * _BUDGET_SAFETY),
                    "cpu": int(val["cpu"] * _BUDGET_SAFETY),
                }

    # Debug: dump budgets so we can validate the multiplier is applied.
    try:
        import os as _os, time as _time, json as _json
        _bdir = _os.path.join("/tmp", "sim-submit-errors")
        _os.makedirs(_bdir, exist_ok=True)
        with open(_os.path.join(_bdir, f"eval_budgets_{int(_time.time()*1000)}.json"), "w") as _bf:
            _json.dump(budgets, _bf, indent=2)
    except Exception:
        pass

    if eval_errors:
        try:
            import os as _os, time as _time, json as _json
            _dump_dir = _os.path.join("/tmp", "sim-submit-errors")
            _os.makedirs(_dump_dir, exist_ok=True)
            _dump_path = _os.path.join(
                _dump_dir, f"eval_err_{int(_time.time() * 1000)}.json"
            )
            with open(_dump_path, "w") as _f:
                _json.dump(eval_errors, _f, indent=2, default=str)
        except Exception:
            _dump_path = "<dump failed>"
        raise RuntimeError(
            f"Ogmios evaluateTransaction returned script error(s); full dump "
            f"in {_dump_path}; first error: {eval_errors[0]}"
        )

    return tx_bytes, budgets


# ═══════════════════════════════════════════════════════════════════════
# TOKEN NAME DERIVATION (matches Aiken utils.ak)
# ═══════════════════════════════════════════════════════════════════════

def derive_token_name(prefix: bytes, seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    """Derive token name: prefix(4) + blake2b_256(cbor(OutputReference))[0:28].

    Aiken's `cbor.serialise(OutputReference)` produces indefinite-length Plutus
    Data (d8 79 9f ... ff), not the definite-length form python-cbor2 emits by
    default. Hand-assemble the bytes to match, so the blake2b hash agrees with
    the on-chain validator.
    """
    if seed_tx_idx <= 23:
        idx_cbor = bytes([seed_tx_idx])
    elif seed_tx_idx <= 255:
        idx_cbor = bytes([0x18, seed_tx_idx])
    else:
        idx_cbor = bytes([0x19]) + seed_tx_idx.to_bytes(2, "big")
    seed_ref_cbor = b"\xd8\x79\x9f\x58\x20" + seed_tx_hash + idx_cbor + b"\xff"
    h = hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()
    return prefix + h[:28]


def claim_token_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    return derive_token_name(b"clm_", seed_tx_hash, seed_tx_idx)


def challenge_token_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    return derive_token_name(b"chl_", seed_tx_hash, seed_tx_idx)


def juror_token_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    return derive_token_name(b"jur_", seed_tx_hash, seed_tx_idx)


def agent_did_name(seed_tx_hash: bytes, seed_tx_idx: int) -> bytes:
    """Agent DID = blake2b_256(cbor(OutputReference))[0:28] — no prefix."""
    seed_ref_cbor = cbor2.dumps(cbor2.CBORTag(121, [seed_tx_hash, seed_tx_idx]))
    h = hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()
    return h[:28]


# ═══════════════════════════════════════════════════════════════════════
# PRNG (matches Aiken select_jurors_prng)
# ═══════════════════════════════════════════════════════════════════════

def select_jurors_prng(seed: bytes, eligible: list, n: int) -> list:
    """Deterministic jury selection matching Aiken's select_jurors_prng.

    Uses blake2b_256 hash chain with Fisher-Yates style removal.
    seed: challenge token name (bytes)
    eligible: sorted list of DID bytes
    n: jury_size
    """
    remaining = list(eligible)
    selected = []
    for i in range(n):
        if not remaining:
            break
        i_cbor = cbor2.dumps(i)
        index_hash = hashlib.blake2b(seed + i_cbor, digest_size=32).digest()
        raw_index = int.from_bytes(index_hash[:8], "big")
        index = raw_index % len(remaining)
        selected.append(remaining.pop(index))
    return selected


# ═══════════════════════════════════════════════════════════════════════
# SLOT / TIME CONVERSION
# ═══════════════════════════════════════════════════════════════════════

def slot_to_posix_ms(slot: int) -> int:
    """Convert slot number to POSIX milliseconds."""
    return (SYSTEM_START_UNIX + slot) * 1000


def posix_ms_to_slot(posix_ms: int) -> int:
    """Convert POSIX milliseconds to slot number."""
    return (posix_ms // 1000) - SYSTEM_START_UNIX
