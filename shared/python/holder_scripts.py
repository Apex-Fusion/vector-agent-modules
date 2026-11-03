"""The holder and record token scripts of shared/holder-scripts, applied with
the aiken CLI. Needs cbor2 and the aiken CLI; no chain connection.

- ``apply_holder(tag, owner)``: ``holder(tag, owner)``. Only the owner key can
  spend an output at its address.
- ``apply_record_token_policy(seed)``: ``record_token(seed)``, a one-shot
  policy. Its tokens can be minted only in the transaction that spends the
  seed.

Holder tags (``HOLDER_TAGS``). One operator key owns the holders of every
module, so each module has its own tags: the holder addresses of two modules
are never the same, and the records and treasury funds of one module never
share an address with another module. The holder accepts every tag above 0.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import cbor2

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOLDER_BLUEPRINT_PATH = REPO_ROOT / "shared" / "holder-scripts" / "plutus.json"

HOLDER_TITLE = "holder.holder.spend"
RECORD_TOKEN_TITLE = "record_token.record_token.mint"

#: The holder tags of each module: {module: {role: tag}}.
HOLDER_TAGS = {
    "module1": {"records": 11},
    "module3": {"params": 31, "treasury": 33},
    "module6": {"params": 61, "oracle": 62, "treasury": 63},
}


def _hex_bytes(name: str, value: str, size: int) -> bytes:
    raw = bytes.fromhex(value)
    if len(raw) != size:
        raise ValueError(f"{name} must be {size} bytes, got {len(raw)} bytes")
    return raw


def aiken_apply(
    blueprint_path: Path,
    module: str,
    validator: str,
    param_cbor_hex: str,
    out_path: Optional[Path] = None,
) -> Optional[dict]:
    """Apply one parameter with ``aiken blueprint apply``. Writes to
    ``out_path`` when given, else returns the applied blueprint."""
    cmd = [
        "aiken", "blueprint", "apply",
        "-i", str(blueprint_path),
        "-m", module, "-v", validator,
        param_cbor_hex,
    ]
    if out_path is not None:
        cmd.extend(["-o", str(out_path)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"aiken blueprint apply failed for {module}.{validator}: "
            f"{result.stderr.strip()}"
        )
    if out_path is not None:
        return None
    return json.loads(result.stdout)


def validator_entry(blueprint: dict, title: str) -> dict:
    for v in blueprint["validators"]:
        if v["title"] == title:
            return v
    raise ValueError(f"Validator {title} not found in the applied blueprint")


def apply_holder(
    tag: int,
    owner_vkh_hex: str,
    blueprint_path: Path = HOLDER_BLUEPRINT_PATH,
) -> dict:
    """Apply holder(tag, owner). Returns {"hash", "compiled_code"}.

    Two ``aiken blueprint apply`` calls, one for each parameter: first the tag
    (CBOR int), then the owner verification key hash (CBOR bytes, 28 bytes).
    """
    if tag <= 0:
        raise ValueError(f"a holder tag must be above 0, got {tag}")
    owner = _hex_bytes("owner_vkh_hex", owner_vkh_hex, 28)
    with tempfile.TemporaryDirectory(prefix="holder-apply-") as tmp:
        tagged = Path(tmp) / "holder-tag.json"
        aiken_apply(blueprint_path, "holder", "holder", cbor2.dumps(tag).hex(), tagged)
        applied = aiken_apply(tagged, "holder", "holder", cbor2.dumps(owner).hex())
    v = validator_entry(applied, HOLDER_TITLE)
    return {"hash": v["hash"], "compiled_code": v["compiledCode"]}


def apply_record_token_policy(
    seed_tx_id_hex: str,
    seed_index: int,
    blueprint_path: Path = HOLDER_BLUEPRINT_PATH,
) -> dict:
    """Apply record_token(seed). Returns {"policy_id", "compiled_code"}.

    The seed is an OutputReference: Plutus constructor 0 with the
    transaction id (32 bytes) and the output index.
    """
    tx_id = _hex_bytes("seed_tx_id_hex", seed_tx_id_hex, 32)
    if seed_index < 0:
        raise ValueError(f"seed_index must not be negative, got {seed_index}")
    seed_cbor = cbor2.dumps(cbor2.CBORTag(121, [tx_id, seed_index])).hex()
    applied = aiken_apply(blueprint_path, "record_token", "record_token", seed_cbor)
    v = validator_entry(applied, RECORD_TOKEN_TITLE)
    return {"policy_id": v["hash"], "compiled_code": v["compiledCode"]}
