"""
Module 3: Reputation Staking — shared deploy helpers

Used by deploy_ogmios.py, deploy_mainnet_ogmios.py and deploy_docker.py.
These functions need cbor2 and the aiken CLI. They do not need pycardano or
a chain connection.

  - apply_holder: holder script for a tag, owned by a verification key hash
  - apply_record_token_policy: one-shot record token policy for a seed UTxO
  - build_protocol_params_cbor / build_protocol_params_json: ProtocolParams
    datum (time values in POSIX milliseconds)
  - deployment_inputs_from_env: identity_hash and the new Module-1 values
  - build_reputation_config_cbor: ReputationConfig (12 fields)
  - select_seed_utxo / find_record_utxo: read pycardano UTxO objects

Deployment order (Part 2 §3.8 and §7 of docs/IDENTITY_REGISTRY.md in
agents-marketplace):

  1. config_holder.
  2. config_seal, bound to config_holder and a fixed seed.
  3. identity, bound to both. Its script hash (= policy ID) is identity_hash.
  4. Module-1, a new deployment with identity_party. It gives the new claim,
     challenge and jury pool hashes and the new Module-1 refs token policy.
  5. The seal: the configuration token with the hash of identity_party.
  6. Module-3 (these scripts). ReputationConfig embeds identity_hash and the
     Module-1 values of step 4.
  7. Module-6. GovernanceConfig embeds identity_hash.

Inputs: environment variables, with no defaults (DEPLOYMENT_INPUTS).

  VECTOR_IDENTITY_HASH              identity_hash (step 3)
  VECTOR_MODULE1_CLAIM_HASH         audit_claim_validator_hash (step 4)
  VECTOR_MODULE1_CHALLENGE_HASH     audit_challenge_validator_hash (step 4)
  VECTOR_MODULE1_JURY_POOL_HASH     audit_jury_pool_hash (step 4)
  VECTOR_MODULE1_REFS_TOKEN_POLICY  audit_refs_token_policy (step 4)

The deploy scripts stop when a value is missing, is not 28 bytes of hex, is
all zero or is a value of the old deployments (RETIRED_VALUES).

Operator keys: new Foundation keys deploy the modules and act as their
operators. The wallet key of a deploy script must be one of these keys. Old
module keys are not reused.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional

import cbor2

# ── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOLDER_BLUEPRINT_PATH = REPO_ROOT / "shared" / "holder-scripts" / "plutus.json"

HOLDER_TITLE = "holder.holder.spend"
RECORD_TOKEN_TITLE = "record_token.record_token.mint"

# ── Constants ────────────────────────────────────────────────────────────────

# Holder tags (shared/holder-scripts/validators/holder.ak)
PARAMS_HOLDER_TAG = 1
TREASURY_HOLDER_TAG = 3

# Asset name of the record token that the ProtocolParams record holds
# (shared/lib/shared/record_auth.ak)
PARAMS_TOKEN_NAME = b"params"

# Redeemer of the record token policy: unit (constructor 0, no fields).
# The policy ignores the redeemer.
UNIT_REDEEMER_CBOR = cbor2.dumps(cbor2.CBORTag(121, []))

# Genesis bonus minting closes this long after the deployment (28 days)
GENESIS_MINTING_DURATION_MS = 2_419_200_000

VALID_CAPABILITIES = [
    b"code_review", b"testing", b"deployment", b"documentation",
    b"security_audit", b"architecture", b"data_analysis", b"ml_training",
]


# ── Validation ───────────────────────────────────────────────────────────────

def _hex_bytes(name: str, value: str, size: Optional[int] = None) -> bytes:
    """Decode a hex string. With `size`, the result must have that length."""
    raw = bytes.fromhex(value)
    if size is not None and len(raw) != size:
        raise ValueError(f"{name} must be {size} bytes, got {len(raw)} bytes")
    return raw


# ── Deployment inputs ────────────────────────────────────────────────────────

DEPLOYMENT_ORDER = (
    "Deployment order (Part 2 §3.8 and §7 of docs/IDENTITY_REGISTRY.md): "
    "config_holder, config_seal, identity, Module-1 with identity_party, "
    "the seal, then Module-3, then Module-6."
)

# Values that other deployments give, in ReputationConfig field order:
#   config field: (environment variable, deploy state key, source)
# There are no defaults, because the values exist only after those
# deployments.
DEPLOYMENT_INPUTS = {
    "audit_claim_validator_hash": (
        "VECTOR_MODULE1_CLAIM_HASH", "module1_claim_validator_hash",
        "the new Module-1 deployment",
    ),
    "audit_challenge_validator_hash": (
        "VECTOR_MODULE1_CHALLENGE_HASH", "module1_challenge_validator_hash",
        "the new Module-1 deployment",
    ),
    "audit_jury_pool_hash": (
        "VECTOR_MODULE1_JURY_POOL_HASH", "module1_jury_pool_hash",
        "the new Module-1 deployment",
    ),
    "audit_refs_token_policy": (
        "VECTOR_MODULE1_REFS_TOKEN_POLICY", "module1_refs_token_policy",
        "the new Module-1 deployment",
    ),
    "identity_hash": (
        "VECTOR_IDENTITY_HASH", "identity_hash",
        "the identity deployment (the script hash of `identity`)",
    ),
}

# Values of the old deployments. They are not allowed as inputs: Module-3
# embeds the new Module-1 deployment, and identity_hash is the identity
# contract, not the legacy agent registry.
RETIRED_VALUES = {
    # Module-1 v14 on mainnet (Module-1/deploy/deployment.json)
    "a9d22e8b01d282be8007b8d9e3e8af548aaa56f1c3e433c0eddd8760":
        "old mainnet Module-1 claim validator hash",
    "12700f4aabdd63caab38adfb50455da54a4e4bc0402a4b1d5a90d1fb":
        "old mainnet Module-1 challenge validator hash",
    "2b01c6b3164237757fc82e64780c63ecfc1d5a733ce919a3e2e75f28":
        "old mainnet Module-1 jury pool hash",
    # Module-1 on testnet (Module-1/deploy/lifecycle-results.json)
    "6884d7c86a0761da8a61e6a7a346197aa2949fef8030a3eb84944dda":
        "old testnet Module-1 claim validator hash",
    "781843681859bcababb90a220ad84604cb324aef4757c6a5c46a96fc":
        "old testnet Module-1 challenge validator hash",
    "b15af09128457e09b23c79119aa0c8c85d25c9fd96656f2611fdc962":
        "old testnet Module-1 jury pool hash",
    # Refs token policy of the old Module-1 operator key (both networks)
    "205d5f77ffebf60b764ba4f1873eff3764f3d1d594e5dac477a928f9":
        "old Module-1 refs token policy",
    # Legacy agent registry (policy ID = script hash)
    "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01":
        "legacy agent registry hash",
}


class DeployInputError(ValueError):
    """A value from another deployment is missing or not allowed."""


def _input_problem(field: str, value: Optional[str]) -> Optional[str]:
    """Why `value` is not allowed for the DEPLOYMENT_INPUTS field `field`,
    or None when it is allowed."""
    env_name, _, source = DEPLOYMENT_INPUTS[field]
    if not value:
        return f"{field} is not set. Set {env_name} to the value from {source}."
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return f"{field} ({env_name}) is not hex: {value!r}."
    if len(raw) != 28:
        return f"{field} ({env_name}) must be 28 bytes, got {len(raw)} bytes."
    if raw == bytes(28):
        return f"{field} ({env_name}) is all zero. Set the value from {source}."
    if raw.hex() in RETIRED_VALUES:
        return (f"{field} ({env_name}) is the {RETIRED_VALUES[raw.hex()]}. "
                f"Set the value from {source}.")
    return None


def _input_bytes(field: str, value: Optional[str]) -> bytes:
    """Decode a DEPLOYMENT_INPUTS value. Raises DeployInputError, with the
    deployment order, when the value is missing or not allowed."""
    problem = _input_problem(field, value)
    if problem:
        raise DeployInputError(f"{problem}\n{DEPLOYMENT_ORDER}")
    return bytes.fromhex(value)


def deployment_inputs_from_env(environ: Mapping[str, str] = os.environ) -> dict:
    """Read DEPLOYMENT_INPUTS from the environment: {config field: hex}.

    Raises DeployInputError that lists every missing or invalid value and
    names the deployment order.
    """
    inputs = {}
    problems = []
    for field, (env_name, _, _) in DEPLOYMENT_INPUTS.items():
        value = environ.get(env_name, "").strip()
        problem = _input_problem(field, value)
        if problem:
            problems.append(problem)
        else:
            inputs[field] = bytes.fromhex(value).hex()
    if problems:
        raise DeployInputError("\n".join(problems + [DEPLOYMENT_ORDER]))
    return inputs


def deployment_inputs_state(inputs: dict) -> dict:
    """Deploy state entries of the DEPLOYMENT_INPUTS: {state key: hex}."""
    return {
        state_key: inputs[field]
        for field, (_, state_key, _) in DEPLOYMENT_INPUTS.items()
    }


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


# ── Deploy state ─────────────────────────────────────────────────────────────

def resume_state_error(state: dict, owner_vkh_hex: str, inputs: dict) -> Optional[str]:
    """Check a saved deploy state before a deploy run.

    Returns an error message, or None when the run can continue. A state with
    tx hashes but without params_token_policy is from a deployment before
    record tokens. A state with tx hashes but without identity_hash is from a
    deployment with the legacy agent registry fields. A new deployment must
    not reuse their tx hashes. A state with other DEPLOYMENT_INPUTS values
    than `inputs` belongs to another configuration: its validators differ.
    """
    policy = state.get("params_token_policy")
    if state.get("tx_hashes") and not policy:
        return ("the deploy state belongs to a deployment without record tokens. "
                "Move it to a different path to start a new deployment.")
    if state.get("tx_hashes") and not state.get("identity_hash"):
        return ("the deploy state belongs to a deployment with the legacy agent "
                "registry fields (no identity_hash). Move it to a different path "
                "to start a new deployment.")
    if policy and not state.get("record_token_seed"):
        return "the deploy state has params_token_policy but no record_token_seed."
    owner = state.get("holder_owner_vkey_hash")
    if owner and owner != owner_vkh_hex:
        return (f"the deploy state belongs to the holder owner {owner}, "
                f"not to this wallet ({owner_vkh_hex}).")
    for field, (env_name, state_key, _) in DEPLOYMENT_INPUTS.items():
        saved = state.get(state_key)
        if saved and saved != inputs[field]:
            return (f"the deploy state has {state_key} {saved}, but {env_name} "
                    f"is {inputs[field]}. Use the values of that deployment, or "
                    f"move the state to a different path to start a new deployment.")
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


def select_seed_utxo(utxos):
    """Choose the record token seed from wallet UTxOs: the largest output
    that holds lovelace only (no native tokens, no datum, no script)."""
    plain = [
        u for u in utxos
        if not _multi_asset(u.output.amount)
        and getattr(u.output, "datum", None) is None
        and getattr(u.output, "datum_hash", None) is None
        and getattr(u.output, "script", None) is None
    ]
    if not plain:
        raise RuntimeError(
            "No wallet UTxO with lovelace only is available as the record token seed"
        )
    return max(plain, key=lambda u: (_coin(u.output.amount), utxo_ref(u)))


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


# ── ProtocolParams datum ─────────────────────────────────────────────────────

def genesis_minting_end_ms(now_ms: Optional[int] = None) -> int:
    """POSIX time in ms at which genesis bonus minting closes:
    deployment time + 28 days."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms + GENESIS_MINTING_DURATION_MS


def protocol_params_fields(genesis_minting_end_ms: int) -> list:
    """The 22 ProtocolParams values in field order.

    Durations are POSIX milliseconds. genesis_minting_window is an absolute
    POSIX time in milliseconds. activity_window counts epochs.
    """
    return [
        10_000_000,             # min_self_stake (10 AP3X)
        5_000_000,              # min_endorsement (5 AP3X)
        25_000_000,             # min_challenge_stake (25 AP3X)
        86_400_000,             # stake_cooldown (24 h)
        172_800_000,            # endorsement_cooldown (48 h)
        100,                    # decay_rate (1%)
        180,                    # activity_window (180 epochs)
        500,                    # decay_collector_fee (5%)
        1000,                   # history_multiplier (10%)
        3,                      # max_endorsement_multiplier
        5000,                   # slash_rate_endorser (50%)
        500,                    # protocol_fee_rate (5%)
        43_200_000,             # challenge_response_deadline (12 h)
        86_400_000,             # min_agent_age (24 h)
        21_600_000,             # escalation_window (6 h)
        100,                    # default_judgment_fee (1%)
        50,                     # genesis_agent_cap
        100_000_000,            # genesis_bonus_amount (100 AP3X)
        genesis_minting_end_ms, # genesis_minting_window (deploy time + 28 days)
        5_184_000_000,          # genesis_protection_period (60 days)
        list(VALID_CAPABILITIES),  # valid_capabilities
        14_400_000,             # epoch_length (4 h)
    ]


def build_protocol_params_cbor(genesis_minting_end_ms: int) -> bytes:
    """ProtocolParams as Plutus Data CBOR: constructor 0 with 22 fields."""
    return cbor2.dumps(cbor2.CBORTag(121, protocol_params_fields(genesis_minting_end_ms)))


def build_protocol_params_json(genesis_minting_end_ms: int) -> dict:
    """ProtocolParams as cardano-cli JSON ScriptData (detailed schema):
    constructor 0 with 22 fields."""
    fields = []
    for value in protocol_params_fields(genesis_minting_end_ms):
        if isinstance(value, list):
            fields.append({"list": [{"bytes": item.hex()} for item in value]})
        else:
            fields.append({"int": value})
    return {"constructor": 0, "fields": fields}


# ── ReputationConfig ─────────────────────────────────────────────────────────

def build_reputation_config_cbor(
    *,
    refs_token_policy: str,
    audit_claim_validator_hash: str,
    audit_challenge_validator_hash: str,
    audit_jury_pool_hash: str,
    audit_refs_token_policy: str,
    params_script_hash: str,
    treasury_script_hash: str,
    ap3x_policy_id: str,
    ap3x_asset_name: str,
    oracle_credential: str,
    params_token_policy: str,
    identity_hash: str,
) -> str:
    """ReputationConfig as Plutus Data CBOR hex.

    Constructor 0 with 12 fields in the order of the Aiken type
    (reputation-staking/lib/reputation_staking/config.ak). identity_hash is
    the last field. All arguments are hex strings. ap3x_policy_id and
    ap3x_asset_name are empty for the native coin.

    The four Module-1 values and identity_hash come from other deployments
    (DEPLOYMENT_INPUTS). When one of them is missing, all zero or a value of
    the old deployments, the function raises DeployInputError with the
    deployment order.
    """
    ap3x_policy = _hex_bytes("ap3x_policy_id", ap3x_policy_id)
    if ap3x_policy and len(ap3x_policy) != 28:
        raise ValueError(f"ap3x_policy_id must be empty or 28 bytes, got {len(ap3x_policy)} bytes")
    ap3x_name = _hex_bytes("ap3x_asset_name", ap3x_asset_name)
    if len(ap3x_name) > 32:
        raise ValueError(f"ap3x_asset_name must be at most 32 bytes, got {len(ap3x_name)} bytes")
    fields = [
        _hex_bytes("refs_token_policy", refs_token_policy, 28),
        _input_bytes("audit_claim_validator_hash", audit_claim_validator_hash),
        _input_bytes("audit_challenge_validator_hash", audit_challenge_validator_hash),
        _input_bytes("audit_jury_pool_hash", audit_jury_pool_hash),
        _input_bytes("audit_refs_token_policy", audit_refs_token_policy),
        _hex_bytes("params_script_hash", params_script_hash, 28),
        _hex_bytes("treasury_script_hash", treasury_script_hash, 28),
        ap3x_policy,
        ap3x_name,
        _hex_bytes("oracle_credential", oracle_credential, 28),
        _hex_bytes("params_token_policy", params_token_policy, 28),
        _input_bytes("identity_hash", identity_hash),
    ]
    return cbor2.dumps(cbor2.CBORTag(121, fields)).hex()
