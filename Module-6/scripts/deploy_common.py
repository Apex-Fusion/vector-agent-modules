"""
Module 6: shared deploy helpers

Used by deploy.py, the operations scripts (update_params.py,
redeploy_proposal.py, recreate_infra.py) and the test scripts that read the
params and oracle records. These functions need cbor2 and the aiken CLI.
They do not need pycardano or a chain connection.

  - apply_holder: holder script for a tag, owned by a verification key hash
  - apply_record_token_policy: one-shot record token policy for a seed UTxO
  - build_governance_config_cbor: GovernanceConfig (12 fields)
  - select_seed_utxo / select_collateral_utxo / find_record_utxo: read
    pycardano UTxO objects
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import cbor2

# ── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOLDER_BLUEPRINT_PATH = REPO_ROOT / "shared" / "holder-scripts" / "plutus.json"

HOLDER_TITLE = "holder.holder.spend"
RECORD_TOKEN_TITLE = "record_token.record_token.mint"

# ── Constants ────────────────────────────────────────────────────────────────

AGENT_REGISTRY_HASH = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
ZEROED_28 = "00" * 28

# Holder tags (shared/holder-scripts/validators/holder.ak). The CrossRefs NFT
# also sits at the oracle holder.
HOLDER_TAGS = [(1, "params"), (2, "oracle"), (3, "treasury")]

# Asset names of the record tokens (shared/lib/shared/record_auth.ak)
PARAMS_TOKEN_NAME = b"params"
ORACLE_TOKEN_NAME = b"oracle"

# Redeemer data of the record token policy: unit (constructor 0, no fields).
# The policy ignores the redeemer.
UNIT_REDEEMER = cbor2.CBORTag(121, [])

MIN_COLLATERAL_LOVELACE = 5_000_000


# ── Validation ───────────────────────────────────────────────────────────────

def _hex_bytes(name: str, value: str, size: Optional[int] = None) -> bytes:
    """Decode a hex string. With `size`, the result must have that length."""
    raw = bytes.fromhex(value)
    if size is not None and len(raw) != size:
        raise ValueError(f"{name} must be {size} bytes, got {len(raw)} bytes")
    return raw


# ── aiken blueprint apply ────────────────────────────────────────────────────

def _aiken_apply(
    blueprint_path: Path,
    module: str,
    validator: str,
    param_cbor_hex: str,
    out_path: Optional[Path] = None,
) -> Optional[dict]:
    """Apply one parameter. Writes to `out_path` when given, else returns the
    applied blueprint."""
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


def _validator(blueprint: dict, title: str) -> dict:
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

    Two `aiken blueprint apply` calls, one for each parameter: first the tag
    (CBOR int), then the owner verification key hash (CBOR bytes, 28 bytes).
    Only the owner key can spend an output at the holder address.
    """
    owner = _hex_bytes("owner_vkh_hex", owner_vkh_hex, 28)
    with tempfile.TemporaryDirectory(prefix="holder-apply-") as tmp:
        tagged = Path(tmp) / "holder-tag.json"
        _aiken_apply(blueprint_path, "holder", "holder", cbor2.dumps(tag).hex(), tagged)
        applied = _aiken_apply(tagged, "holder", "holder", cbor2.dumps(owner).hex())
    v = _validator(applied, HOLDER_TITLE)
    return {"hash": v["hash"], "compiled_code": v["compiledCode"]}


def apply_record_token_policy(
    seed_tx_id_hex: str,
    seed_index: int,
    blueprint_path: Path = HOLDER_BLUEPRINT_PATH,
) -> dict:
    """Apply record_token(seed). Returns {"policy_id", "compiled_code"}.

    The seed is an OutputReference: Plutus constructor 0 with the
    transaction id (32 bytes) and the output index. Tokens of the policy can
    be minted only in the transaction that spends the seed.
    """
    tx_id = _hex_bytes("seed_tx_id_hex", seed_tx_id_hex, 32)
    if seed_index < 0:
        raise ValueError(f"seed_index must not be negative, got {seed_index}")
    seed_cbor = cbor2.dumps(cbor2.CBORTag(121, [tx_id, seed_index])).hex()
    applied = _aiken_apply(blueprint_path, "record_token", "record_token", seed_cbor)
    v = _validator(applied, RECORD_TOKEN_TITLE)
    return {"policy_id": v["hash"], "compiled_code": v["compiledCode"]}


# ── GovernanceConfig CBOR ────────────────────────────────────────────────────

def build_governance_config_cbor(
    refs_token_policy: str,
    oracle_holder_hash: str,
    params_holder_hash: str,
    treasury_holder_hash: str,
    record_token_policy: str,
) -> str:
    """Build GovernanceConfig as Plutus Data CBOR (hex).

    Constructor 0 with 12 fields (all ByteArray). The record token policy is
    the last field.
    """
    fields = [
        _hex_bytes("refs_token_policy", refs_token_policy, 28),        # refs_token_policy
        bytes.fromhex(ZEROED_28),               # prediction_validator_hash
        bytes.fromhex(AGENT_REGISTRY_HASH),     # registry_policy_id
        bytes.fromhex(AGENT_REGISTRY_HASH),     # registry_script_hash
        bytes.fromhex(ZEROED_28),               # reputation_validator_hash (Module-3 not deployed)
        bytes.fromhex(ZEROED_28),               # jury_pool_hash (Phase 1.2)
        _hex_bytes("oracle_holder_hash", oracle_holder_hash, 28),      # governance_oracle_hash
        _hex_bytes("params_holder_hash", params_holder_hash, 28),      # governance_params_hash
        _hex_bytes("treasury_holder_hash", treasury_holder_hash, 28),  # governance_treasury_hash
        bytes.fromhex(ZEROED_28),               # credibility_pool_hash (Phase 1.2)
        bytes.fromhex(ZEROED_28),               # protocol_params_hash
        _hex_bytes("record_token_policy", record_token_policy, 28),    # record_token_policy
    ]
    # Plutus Data constructor: Tag 121 + 0 = constr(0, fields)
    return cbor2.dumps(cbor2.CBORTag(121, fields)).hex()


# ── Deploy state ─────────────────────────────────────────────────────────────

def resume_state_error(state: dict, owner_vkh_hex: str) -> Optional[str]:
    """Check a saved deploy state before a deploy run with record tokens.

    Returns an error message, or None when the run can continue. A state with
    tx hashes but without record_token_policy is from a deployment before
    record tokens: a new deployment must not reuse its tx hashes.
    """
    policy = state.get("record_token_policy")
    if state.get("tx_hashes") and not policy:
        return ("the deploy state belongs to a deployment without record tokens. "
                "Move it to a different path to start a new deployment.")
    if policy and not state.get("record_token_seed"):
        return "the deploy state has record_token_policy but no record_token_seed."
    owner = state.get("holder_owner_vkey_hash")
    if owner and owner != owner_vkh_hex:
        return (f"the deploy state belongs to the holder owner {owner}, "
                f"not to this wallet ({owner_vkh_hex}).")
    return None


# ── UTxO references ──────────────────────────────────────────────────────────

def format_utxo_ref(tx_id_hex: str, index: int) -> str:
    return f"{tx_id_hex}#{index}"


def parse_utxo_ref(ref: str) -> tuple:
    """Split "<tx_id>#<index>" into (tx_id_hex, index)."""
    tx_id_hex, index = ref.split("#")
    return tx_id_hex, int(index)


def utxo_ref(utxo) -> str:
    """Reference "<tx_id>#<index>" of a pycardano UTxO."""
    return format_utxo_ref(bytes(utxo.input.transaction_id).hex(), utxo.input.index)


def _coin(amount) -> int:
    return amount if isinstance(amount, int) else amount.coin


def _multi_asset(amount):
    return None if isinstance(amount, int) else amount.multi_asset


def _lovelace_only(output) -> bool:
    """True when the output holds lovelace only (no native tokens, no datum,
    no script)."""
    return (
        not _multi_asset(output.amount)
        and getattr(output, "datum", None) is None
        and getattr(output, "datum_hash", None) is None
        and getattr(output, "script", None) is None
    )


def select_seed_utxo(utxos):
    """Choose the record token seed from wallet UTxOs: the largest output
    that holds lovelace only."""
    plain = [u for u in utxos if _lovelace_only(u.output)]
    if not plain:
        raise RuntimeError(
            "No wallet UTxO with lovelace only is available as the record token seed"
        )
    return max(plain, key=lambda u: (_coin(u.output.amount), utxo_ref(u)))


def select_collateral_utxo(utxos, avoid_ref: Optional[str] = None):
    """Choose collateral from wallet UTxOs: the smallest lovelace-only output
    of at least 5 AP3X. Prefers an output other than `avoid_ref`."""
    candidates = [
        u for u in utxos
        if _lovelace_only(u.output) and _coin(u.output.amount) >= MIN_COLLATERAL_LOVELACE
    ]
    preferred = [u for u in candidates if utxo_ref(u) != avoid_ref]
    pool = preferred or candidates
    if not pool:
        raise RuntimeError(
            "No collateral UTxO found (need a wallet UTxO of at least 5 AP3X "
            "with lovelace only)"
        )
    return min(pool, key=lambda u: (_coin(u.output.amount), utxo_ref(u)))


def record_token_quantity(output, policy_id_hex: str, token_name: bytes) -> int:
    """Quantity of the token (policy_id_hex, token_name) in a pycardano output."""
    multi_asset = _multi_asset(output.amount)
    if not multi_asset:
        return 0
    for policy, assets in multi_asset.items():
        if bytes(policy).hex() != policy_id_hex:
            continue
        for name, quantity in assets.items():
            if bytes(name) == token_name:
                return quantity
    return 0


def find_record_utxo(utxos, policy_id_hex: str, token_name: bytes):
    """The UTxO that holds exactly one record token (policy_id_hex,
    token_name), or None."""
    for u in utxos:
        if record_token_quantity(u.output, policy_id_hex, token_name) == 1:
            return u
    return None
