"""Recover stuck ADA from a previously-failed happy-path scenario.

When a happy-path lifecycle test fails mid-flight, the funded sub-wallets
and bonded juror UTxOs remain on chain. This script:

  1. Re-derives the 17 sub-wallets at the indices recorded in
     simulation/.wallet_index_{testnet,mainnet}.json (selected by the
     APEX_NETWORK env var) under the given scenario name.
  2. Calls WithdrawJuror for each of the (up to) 15 bonded juror UTxOs at
     jury_pool, returning ~25 ADA each to master.
  3. Drains every sub-wallet's pure-ADA UTxOs back to master.

Limitation: DID lockups (~15 ADA each) are NOT recoverable — the v15
contracts have no DID-revoke flow. So per-scenario this recovers ~1225
ADA out of ~1490 ADA spent (255 ADA DID locks remain unrecoverable).

Usage:
    cd <module-root>
    python3 _recover_stuck_scenario.py <scenario_name>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <scenario_name>")
        return 2
    scenario_name = sys.argv[1]

    # Network-scoped paths sourced from simulation.config so APEX_NETWORK
    # routes recovery to the matching chain. Recovering a testnet scenario
    # with APEX_NETWORK=mainnet set would be catastrophic, so distinct
    # wallet-index files per chain guarantee no confusion.
    from simulation.config import (
        WALLET_SKEY as _WALLET_SKEY,
        DEPLOYMENT_PATH as _DEPLOYMENT_PATH,
        WALLET_INDEX_FILE as _WALLET_INDEX_FILE,
    )
    INDEX_FILE = Path(_WALLET_INDEX_FILE)
    DEPLOYMENT_FILE = Path(_DEPLOYMENT_PATH)
    MASTER_SKEY_FILE = Path(_WALLET_SKEY)

    if not INDEX_FILE.exists():
        print(f"FAIL: index file missing at {INDEX_FILE}")
        return 1
    if not DEPLOYMENT_FILE.exists():
        print(f"FAIL: deployment manifest missing at {DEPLOYMENT_FILE}")
        return 1
    if not MASTER_SKEY_FILE.exists():
        print(f"FAIL: master skey missing at {MASTER_SKEY_FILE}")
        return 1

    idx_data = json.loads(INDEX_FILE.read_text())
    indices = sorted(
        int(a["index"]) for a in idx_data.get("allocations", [])
        if a.get("scenario") == scenario_name
    )
    if not indices:
        print(f"FAIL: no allocations found for scenario {scenario_name!r}")
        return 1
    print(f"  Found {len(indices)} indices for {scenario_name}: "
          f"{indices[0]}..{indices[-1]}")

    from pycardano import (
        Address, PaymentSigningKey, PaymentVerificationKey,
        TransactionBuilder, TransactionOutput,
    )
    from simulation.config import NETWORK
    from simulation.chain import (
        OgmiosContext, submit_tx, tx_to_bytes, wait_confirm,
    )
    from simulation.wallet_derivation import derive
    from simulation import tx_builder as _txb

    deployment = json.loads(DEPLOYMENT_FILE.read_text())
    master_skey = PaymentSigningKey.load(str(MASTER_SKEY_FILE))
    master_vkey = PaymentVerificationKey.from_signing_key(master_skey)
    master_addr = Address(master_vkey.hash(), network=NETWORK)
    print(f"  master_addr: {master_addr}")

    ctx = OgmiosContext()
    before = sum(
        int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
        else int(u.output.amount)
        for u in ctx.utxos(str(master_addr))
    )
    print(f"  master balance BEFORE recovery: {before/1_000_000:.3f} ADA")

    # Re-derive all sub-wallets.
    sub_wallets = []
    for idx in indices:
        d = derive(idx, master_skey=master_skey)
        sub_wallets.append(d)

    # Step 1: WithdrawJuror for each bonded juror UTxO at jury_pool.
    # We need to find each bonded JurorDatum by scanning jury_pool
    # UTxOs and matching against the master_vkh (juror_credential).
    print("\n  Step 1: WithdrawJuror for each bonded juror...")
    deployment_state = _txb.DeploymentState(deployment)
    deployment_state.resolve_refs()
    jury_addr = str(deployment_state.jury_pool_addr)
    jury_utxos = ctx.utxos(jury_addr)
    print(f"    {len(jury_utxos)} UTxOs at jury_pool")

    import cbor2
    from pycardano import ScriptHash
    jp_policy = ScriptHash(bytes.fromhex(deployment_state.jury_pool_hash))
    master_vkh_bytes = bytes(master_vkey.hash())

    bond_utxos = []
    for u in jury_utxos:
        # Filter: must carry a juror NFT under jury_pool policy.
        ma = getattr(u.output.amount, "multi_asset", None)
        if not ma or jp_policy not in ma:
            continue
        # Filter: datum's juror_credential must be master_vkh.
        d_raw = u.output.datum
        if d_raw is None:
            continue
        d_cbor = d_raw.cbor if hasattr(d_raw, "cbor") else bytes(d_raw)
        try:
            datum = cbor2.loads(d_cbor)
            fields = list(datum.value)
            if len(fields) != 9:
                continue
            cred = fields[1]
            if getattr(cred, "tag", None) != 121:
                continue
            cred_vkh = bytes(cred.value[0])
            if cred_vkh != master_vkh_bytes:
                continue
            ac_field = fields[6]
            if getattr(ac_field, "tag", None) != 122:
                # active_case Some(_) - skip; can't withdraw while assigned
                print(f"    skip {u.input.transaction_id.payload.hex()[:8]}# "
                      f"{u.input.index}: active_case=Some(...)")
                continue
        except Exception:
            continue
        bond_utxos.append(u)

    print(f"    {len(bond_utxos)} bond UTxOs match master credential AND "
          f"have active_case=None → withdrawing")

    total_returned = 0
    for u in bond_utxos:
        ref = f"{u.input.transaction_id.payload.hex()}#{u.input.index}"
        try:
            tx, ada_returned = _txb.build_withdraw_juror(
                ctx, deployment_state,
                master_skey, master_vkey, master_addr,
                ref,
            )
            tx_hash = submit_tx(tx_to_bytes(tx))
            print(f"    withdrew {ref[:16]}#{u.input.index}: "
                  f"+{ada_returned/1_000_000:.3f} ADA tx={tx_hash[:16]}")
            total_returned += ada_returned
            wait_confirm(secs=25)
        except Exception as exc:
            print(f"    FAIL withdraw {ref[:16]}#{u.input.index}: {exc!s}")

    print(f"  Step 1 total recovered: {total_returned/1_000_000:.3f} ADA")

    # Step 2: drain every sub-wallet's pure-ADA UTxOs to master.
    print("\n  Step 2: draining sub-wallets to master...")
    drain_total = 0
    for w in sub_wallets:
        addr = w["address"]
        try:
            utxos = ctx.utxos(str(addr))
        except Exception as exc:
            print(f"    skip idx={w['index']}: query failed {exc!s}")
            continue
        pure = [u for u in utxos
                if not (hasattr(u.output.amount, "multi_asset")
                        and u.output.amount.multi_asset)]
        if not pure:
            continue
        total = sum(int(u.output.amount.coin) for u in pure)
        if total < 2_000_000:
            continue
        try:
            builder = TransactionBuilder(ctx)
            builder.fee_buffer = 300_000
            for u in pure:
                builder.add_input(u)
            tx = builder.build_and_sign(
                [w["skey"]], change_address=master_addr,
            )
            tx_hash = submit_tx(tx_to_bytes(tx))
            print(f"    drained idx={w['index']}: {total/1_000_000:.3f} ADA "
                  f"tx={tx_hash[:16]}")
            drain_total += total
            wait_confirm(secs=25)
        except Exception as exc:
            print(f"    FAIL drain idx={w['index']}: {exc!s}")

    print(f"  Step 2 total drained: {drain_total/1_000_000:.3f} ADA")

    after = sum(
        int(u.output.amount.coin) if hasattr(u.output.amount, "coin")
        else int(u.output.amount)
        for u in ctx.utxos(str(master_addr))
    )
    print(f"\n  master balance AFTER recovery: {after/1_000_000:.3f} ADA")
    print(f"  net master change: {(after - before)/1_000_000:+.3f} ADA")
    print(f"\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
