"""Run the compiled validators of a module on a pycardano transaction.

The module tests build a transaction with a client builder and the applied
validators of the module blueprint (`plutus.json`). This module runs
`aiken tx simulate` on that transaction and its resolved inputs. The
evaluator is the uplc machine of the Aiken compiler that builds the
blueprints (Aiken v1.1.21). Nothing goes to a network.

- `apply_validator` applies blueprint parameters with `aiken blueprint apply`
  (the same command as the deploy scripts).
- `simulate` evaluates every redeemer of the transaction. aiken evaluates the
  redeemers in the order of the redeemer list and stops at the first failure.
  When the transaction fails, `simulate` evaluates it again with each
  redeemer first, so each redeemer gets its own verdict. The order of the
  redeemers does not change the transaction id (it is part of the witness
  set).
- `aiken tx simulate` uses aiken's default Plutus V3 cost model and allows
  14,000,000 memory units per transaction, so its execution units are
  indicative only. It does not check phase-1 rules (fees, balance, the
  script data hash).

Times: the evaluator converts the slots of the validity range to POSIX
milliseconds with `zero_time_ms + (slot - zero_slot) * slot_length_ms`. Pass
the system start of the network that the client uses.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Sequence

import cbor2
from pycardano import PlutusV3Script, Transaction, TransactionInput, UTxO, plutus_script_hash

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FAILURE = re.compile(r"failed script execution\s+(\w+\[\d+\])\s*([^\n]*)")
#: The redeemer tags as aiken v1.1.21 names the purposes in its reports.
_PURPOSES = {0: "Spend", 1: "Mint", 2: "Publish", 3: "Withdraw", 4: "Vote", 5: "Propose"}


class HarnessError(RuntimeError):
    """aiken could not evaluate the transaction (decoding, a missing input,
    a missing script or redeemer, or no aiken on PATH)."""


def aiken_available() -> bool:
    """True when the `aiken` command is on PATH."""
    return shutil.which("aiken") is not None


# ── Blueprint parameters ─────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class AppliedScript:
    """A validator of a blueprint with all parameters applied."""

    title: str
    hash: str
    compiled_code: str

    @property
    def script(self) -> PlutusV3Script:
        return PlutusV3Script(bytes.fromhex(self.compiled_code))

    @property
    def size(self) -> int:
        """Size of the applied script in bytes (the reference script size)."""
        return len(self.compiled_code) // 2


_APPLIED: dict = {}


def apply_validator(blueprint: Path, module: str, validator: str, *params_cbor_hex: str) -> AppliedScript:
    """Apply the parameters (Plutus Data CBOR hex, in the blueprint order) to
    the validator `module.validator` of `blueprint`. The result is cached for
    the blueprint content and the parameters."""
    blueprint = Path(blueprint)
    key = (str(blueprint.resolve()), blueprint.stat().st_mtime_ns, module, validator, params_cbor_hex)
    if key in _APPLIED:
        return _APPLIED[key]
    if not aiken_available():
        raise HarnessError("aiken is not on PATH")
    with tempfile.TemporaryDirectory(prefix="aiken-apply-") as tmp:
        current = blueprint
        for i, param in enumerate(params_cbor_hex):
            out = Path(tmp) / f"applied-{i}.json"
            result = subprocess.run(
                ["aiken", "blueprint", "apply", "-i", str(current), "-m", module, "-v", validator, "-o", str(out), param],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise HarnessError(f"aiken blueprint apply failed for {module}.{validator}: {result.stderr.strip()}")
            current = out
        applied = json.loads(Path(current).read_text())
    prefix = f"{module}.{validator}."
    found = [v for v in applied["validators"] if v["title"].startswith(prefix)]
    if not found:
        raise HarnessError(f"{module}.{validator} is not in {blueprint}")
    v = found[0]
    if v.get("parameters"):
        raise HarnessError(f"{module}.{validator} has open parameters: {[p['title'] for p in v['parameters']]}")
    script = AppliedScript(v["title"], v["hash"], v["compiledCode"])
    if plutus_script_hash(script.script).payload.hex() != script.hash:
        raise HarnessError(f"{module}.{validator}: the blueprint hash does not match the compiled code")
    _APPLIED[key] = script
    return script


# ── Resolved inputs ──────────────────────────────────────────────────────────


def _key(ref: TransactionInput) -> tuple:
    return (bytes(ref.transaction_id), ref.index)


def resolved_inputs(tx: Transaction, utxos: Iterable[UTxO]) -> list:
    """The UTxOs of the inputs, the reference inputs and the collateral inputs
    of `tx`, taken from `utxos`. Raises HarnessError for a missing one."""
    pool = {_key(u.input): u for u in utxos}
    body = tx.transaction_body
    refs = list(body.inputs) + list(body.reference_inputs or []) + list(body.collateral or [])
    found, missing = [], []
    seen = set()
    for ref in refs:
        k = _key(ref)
        if k in seen:
            continue
        seen.add(k)
        if k in pool:
            found.append(pool[k])
        else:
            missing.append(f"{k[0].hex()}#{k[1]}")
    if missing:
        raise HarnessError("unresolved inputs: " + ", ".join(missing))
    return found


# ── Evaluation ───────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Verdict:
    """The result of one redeemer."""

    purpose: str
    passed: bool
    mem: Optional[int] = None
    cpu: Optional[int] = None
    reason: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Simulation:
    passed: bool
    verdicts: tuple
    #: The first failing purpose in the evaluation order, for example "Spend[1]".
    failed_at: Optional[str] = None
    #: aiken's report of the first failure.
    report: str = ""

    def verdict(self, purpose: str) -> Verdict:
        for v in self.verdicts:
            if v.purpose == purpose:
                return v
        raise KeyError(f"no redeemer {purpose}; the transaction has {[v.purpose for v in self.verdicts]}")

    @property
    def failing(self) -> list:
        """The purposes of the redeemers that fail when they run first."""
        return [v.purpose for v in self.verdicts if not v.passed]

    def describe(self) -> str:
        return "; ".join(
            f"{v.purpose}: {'pass' if v.passed else 'FAIL'}"
            + (f" (mem {v.mem}, cpu {v.cpu})" if v.mem is not None else "")
            + (f" {v.reason}" if v.reason else "")
            for v in self.verdicts
        )


# ── Raw CBOR of the transaction ──────────────────────────────────────────────


def _head(data: bytes, pos: int) -> tuple:
    """(major type, argument or None for an indefinite length, position
    after the head) of the CBOR item at `pos`."""
    initial = data[pos]
    major, info = initial >> 5, initial & 0x1F
    pos += 1
    if info < 24:
        return major, info, pos
    if info == 31:
        return major, None, pos
    size = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
    if size is None:
        raise HarnessError(f"bad CBOR head 0x{initial:02x}")
    return major, int.from_bytes(data[pos:pos + size], "big"), pos + size


def _skip(data: bytes, pos: int) -> int:
    """The position after the CBOR item at `pos` (no semantic decoding, so
    tags such as 258 keep their bytes)."""
    major, arg, pos = _head(data, pos)
    if major in (0, 1):
        return pos
    if major in (2, 3):
        if arg is not None:
            return pos + arg
        while data[pos] != 0xFF:
            pos = _skip(data, pos)
        return pos + 1
    if major in (4, 5):
        width = 1 if major == 4 else 2
        if arg is not None:
            for _ in range(arg * width):
                pos = _skip(data, pos)
            return pos
        while data[pos] != 0xFF:
            pos = _skip(data, pos)
        return pos + 1
    if major == 6:
        return _skip(data, pos)
    if arg is None:
        raise HarnessError("unexpected CBOR break")
    return pos


def _items(data: bytes, pos: int, count: Optional[int]) -> tuple:
    """The raw items that follow a head at `pos` (`count` items, or until a
    break for an indefinite length), and the position after them."""
    items = []
    while (count is None and data[pos] != 0xFF) or (count is not None and len(items) < count):
        end = _skip(data, pos)
        items.append(data[pos:end])
        pos = end
    return items, (pos + 1 if count is None else pos)


def _array_head(count: int) -> bytes:
    if count < 24:
        return bytes([0x80 + count])
    if count < 256:
        return bytes([0x98, count])
    return bytes([0x99]) + count.to_bytes(2, "big")


def _map_head(count: int) -> bytes:
    if count < 24:
        return bytes([0xA0 + count])
    return bytes([0xB8, count])


def _split(data: bytes) -> tuple:
    """(raw transaction items, raw witness-set entries as (key, raw value),
    the index of the redeemers entry or None)."""
    major, count, pos = _head(data, 0)
    if major != 4:
        raise HarnessError("the transaction is not a CBOR array")
    items, end = _items(data, pos, count)
    if end != len(data):
        raise HarnessError("trailing bytes after the transaction")
    witness = items[1]
    major, count, pos = _head(witness, 0)
    if major != 5:
        raise HarnessError("the witness set is not a CBOR map")
    raw, _ = _items(witness, pos, None if count is None else 2 * count)
    entries = [(cbor2.loads(raw[i]), raw[i + 1]) for i in range(0, len(raw), 2)]
    index = next((i for i, (k, _v) in enumerate(entries) if k == 5), None)
    return items, entries, index


def _redeemers(raw: bytes) -> tuple:
    """(map form?, raw redeemers, purposes). The list form has one raw entry
    per redeemer; the map form has (raw key, raw value) pairs."""
    major, count, pos = _head(raw, 0)
    if major == 4:
        parts, _ = _items(raw, pos, count)
        purposes = []
        for part in parts:
            _m, _n, p = _head(part, 0)
            tag_end = _skip(part, p)
            index_end = _skip(part, tag_end)
            purposes.append((cbor2.loads(part[p:tag_end]), cbor2.loads(part[tag_end:index_end])))
        return False, parts, purposes
    if major == 5:
        flat, _ = _items(raw, pos, None if count is None else 2 * count)
        parts = [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]
        purposes = [tuple(cbor2.loads(k)) for k, _v in parts]
        return True, parts, purposes
    raise HarnessError("the redeemers are neither a list nor a map")


def _with_order(data: bytes, order: Sequence[int]) -> bytes:
    """The transaction with its redeemers in `order`. Only the redeemers
    entry of the witness set changes; the body keeps its bytes."""
    items, entries, index = _split(data)
    if index is None:
        return data
    is_map, parts, _purposes = _redeemers(entries[index][1])
    if is_map:
        redeemers = _map_head(len(parts)) + b"".join(parts[i][0] + parts[i][1] for i in order)
    else:
        redeemers = _array_head(len(parts)) + b"".join(parts[i] for i in order)
    entries[index] = (5, redeemers)
    witness = _map_head(len(entries)) + b"".join(cbor2.dumps(k) + v for k, v in entries)
    return _array_head(len(items)) + items[0] + witness + b"".join(items[2:])


def _purposes(data: bytes) -> list:
    _items_, entries, index = _split(data)
    if index is None:
        return []
    _is_map, _parts, purposes = _redeemers(entries[index][1])
    return [f"{_PURPOSES.get(tag, tag)}[{i}]" for tag, i in purposes]


def _outputs_array(utxos: Sequence[UTxO]) -> bytes:
    raw = [u.output.to_cbor() for u in utxos]
    raw = [r if isinstance(r, bytes) else bytes.fromhex(r) for r in raw]
    return _array_head(len(raw)) + b"".join(raw)


def _run(data: bytes, utxos: Sequence[UTxO], zero_time_ms: int, zero_slot: int, slot_length_ms: int):
    with tempfile.TemporaryDirectory(prefix="aiken-simulate-") as tmp:
        tx_file, inputs_file, outputs_file = (Path(tmp) / n for n in ("tx.hex", "inputs.hex", "outputs.hex"))
        tx_file.write_text(data.hex())
        inputs_file.write_text(cbor2.dumps([[bytes(u.input.transaction_id), u.input.index] for u in utxos]).hex())
        outputs_file.write_text(_outputs_array(utxos).hex())
        result = subprocess.run(
            [
                "aiken", "tx", "simulate", str(tx_file), str(inputs_file), str(outputs_file),
                "--zero-time", str(zero_time_ms), "--zero-slot", str(zero_slot), "--slot-length", str(slot_length_ms),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
    report = _ANSI.sub("", result.stderr)
    if result.returncode == 0:
        return True, json.loads(result.stdout), None, None, report
    failure = _FAILURE.search(report)
    if failure is None:
        raise HarnessError(f"aiken could not evaluate the transaction:\n{report}")
    return False, [], failure.group(1), failure.group(2).strip(), report


def simulate(
    tx: Transaction,
    utxos: Sequence[UTxO],
    *,
    zero_time_ms: int,
    zero_slot: int = 0,
    slot_length_ms: int = 1000,
) -> Simulation:
    """Evaluate every redeemer of `tx` with the resolved inputs `utxos`
    (`resolved_inputs`)."""
    if not aiken_available():
        raise HarnessError("aiken is not on PATH")
    data = tx.to_cbor()
    data = data if isinstance(data, bytes) else bytes.fromhex(data)
    purposes = _purposes(data)
    order = list(range(len(purposes)))
    ok, budgets, failed_at, reason, report = _run(_with_order(data, order), utxos, zero_time_ms, zero_slot, slot_length_ms)
    if ok:
        if len(budgets) != len(purposes):
            raise HarnessError(f"aiken evaluated {len(budgets)} of {len(purposes)} redeemers:\n{report}")
        verdicts = tuple(Verdict(p, True, b["mem"], b["cpu"]) for p, b in zip(purposes, budgets))
        return Simulation(True, verdicts, report=report)
    verdicts = []
    for i, purpose in enumerate(purposes):
        if i == 0:
            alone_failed_at, alone_reason = failed_at, reason
        else:
            alone = _run(_with_order(data, [i] + [j for j in order if j != i]), utxos, zero_time_ms, zero_slot, slot_length_ms)
            alone_failed_at, alone_reason = alone[2], alone[3]
        passed = alone_failed_at != purpose
        verdicts.append(Verdict(purpose, passed, reason=None if passed else alone_reason))
    return Simulation(False, tuple(verdicts), failed_at=failed_at, report=report)
