"""Standalone verifier for simulation/wallet_derivation.py — no pytest needed.

Run from the Module-1 repo root:
    cd <module-root>
    python3 _verify_wallet_derivation.py

Prints PASS/FAIL for each invariant. Exits 0 iff all pass.
"""
import json
import sys
import tempfile
from pathlib import Path

# Make sure 'simulation' resolves to the local one even when run from elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from simulation.wallet_derivation import allocate_indices, derive
from simulation.config import WALLET_SKEY as _WALLET_SKEY

MASTER_SKEY_PATH = Path(_WALLET_SKEY)


def _ok(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}{('  -- ' + detail) if detail else ''}")
    return cond


def main() -> int:
    all_pass = True

    # Section 1: allocate_indices behavior on a temp file.
    with tempfile.TemporaryDirectory() as tmp:
        idx_path = Path(tmp) / "test_index.json"

        a = allocate_indices(7, scenario="test_a", role="agents", index_path=idx_path)
        b = allocate_indices(5, scenario="test_b", role="agents", index_path=idx_path)

        all_pass &= _ok("allocate test_a returns [0..6]", a == list(range(0, 7)),
                        f"got {a}")
        all_pass &= _ok("allocate test_b returns [7..11]", b == list(range(7, 12)),
                        f"got {b}")

        loaded = json.loads(idx_path.read_text())
        all_pass &= _ok("next_index == 12", loaded["next_index"] == 12,
                        f"got {loaded['next_index']}")
        all_pass &= _ok("12 allocation records", len(loaded["allocations"]) == 12,
                        f"got {len(loaded['allocations'])}")
        # All recorded indices match expectation
        all_recorded = [r["index"] for r in loaded["allocations"]]
        all_pass &= _ok("recorded indices == [0..11]",
                        all_recorded == list(range(0, 12)),
                        f"got {all_recorded}")
        # Scenario tags
        scenarios = [r["scenario"] for r in loaded["allocations"]]
        all_pass &= _ok("first 7 records tagged test_a",
                        scenarios[:7] == ["test_a"] * 7)
        all_pass &= _ok("next 5 records tagged test_b",
                        scenarios[7:12] == ["test_b"] * 5)

    # Section 2: derive determinism. Requires real master skey for byte-equality.
    if not MASTER_SKEY_PATH.exists():
        print(f"  [SKIP] derive determinism: master skey missing at {MASTER_SKEY_PATH}")
    else:
        d0_a = derive(0, master_skey_path=MASTER_SKEY_PATH)
        d0_b = derive(0, master_skey_path=MASTER_SKEY_PATH)
        d1   = derive(1, master_skey_path=MASTER_SKEY_PATH)

        # Compare via CBOR (pycardano objects don't define __eq__ on bytes).
        all_pass &= _ok("derive(0) skey reproducible",
                        bytes(d0_a["skey"].payload) == bytes(d0_b["skey"].payload))
        all_pass &= _ok("derive(0) vkey reproducible",
                        bytes(d0_a["vkey"].payload) == bytes(d0_b["vkey"].payload))
        all_pass &= _ok("derive(0) address reproducible",
                        str(d0_a["address"]) == str(d0_b["address"]))
        all_pass &= _ok("derive(0) != derive(1) skey",
                        bytes(d0_a["skey"].payload) != bytes(d1["skey"].payload))
        all_pass &= _ok("derive(0) != derive(1) address",
                        str(d0_a["address"]) != str(d1["address"]))

        print(f"\n  Sample addresses (first 3 indices):")
        for i in range(3):
            d = derive(i, master_skey_path=MASTER_SKEY_PATH)
            print(f"    index={i}: {d['address']}")

    print("\n" + ("ALL PASS" if all_pass else "SOME FAILED"))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
