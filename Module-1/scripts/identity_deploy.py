#!/usr/bin/env python3
"""Module-1 identity deployment: plan, seal check and on-chain steps
(Part 2 §3.8, §7, §11 step 2).

Commands:

  plan --mode {oracle,jury} --deployment FILE --vkey FILE [--config FILE]
       [--network N] [--state-dir DIR] [--allow-placeholders]
      Read the identity values from the identity deployment.json
      (identity_hash, config_policy, config_holder, refs.identity) and the
      operator key: oracle_vkh is its key hash, refs_token_policy is
      ScriptPubkey(oracle_vkh). Read verdict_policy and protocol_params from
      config.json; the mode gives oracle_active (oracle: true, jury: false).
      Build the Module-1 blueprint (traces silent), apply the parameters in
      the dependency order of the script hashes and write DIR/plan/plutus.json
      (the applied blueprint), DIR/plan/plan.json and the Module-1 manifest
      DIR/manifest.json. Print the script hashes and the seal datum: the hash
      of identity_party (M1) as a Plutus Data byte string. The identity seal
      step reads hashes.identity_party from the manifest.

  check-seal (--datum HEX | --datum-file PATH | --deployment FILE)
             [--blueprint PATH | --state-dir DIR] [--network N]
      Compare the inline datum of the configuration record with the
      identity_party hash of the applied blueprint. With --deployment, read
      the configuration record (seal.config_record) from the chain. Exit
      status 1 when they differ. Run it after the seal and before anybody
      appoints Module-1 (Part 2 §3.8 step 6, §12.6).

  deploy [run|build|sign|submit] --mode M --deployment FILE (--vkey F | --skey F)
         [--submit] [--dry-run-funds AP3X] [--state-dir DIR] [--network N]
      The on-chain steps of Module-1, from the plan in DIR:
        publish-claim, publish-challenge, publish-jury_pool,
        publish-identity_party   reference scripts at the reference address
                                 of the identity deployment (unspendable);
        register-identity_party  the stake credential Script(M1), with the
                                 stake registration certificate without
                                 deposit field (certificate 0). It needs no
                                 script witness; identity_party fails every
                                 publish purpose;
        records                  mints the CrossValidatorRefs token
                                 (ScriptPubkey(operator)) and creates the
                                 CrossValidatorRefs record (output 0, token
                                 and datum [claim, challenge, jury_pool]) and
                                 the ProtocolParams record (output 1) at the
                                 Module-1 records holder (holder tag 11).
      "run" is a dry run (build and evaluate with Ogmios, send nothing) unless
      --submit is given. "build" writes unsigned transactions, "sign" signs
      them offline with --skey, "submit" sends the signed ones. The deploy
      writes the reference UTxOs into the manifest. Sending (run --submit,
      build, submit) needs a plan without placeholders and the seal of M1 on
      chain.

  encode --mode {oracle,jury} [--config FILE] [--deployment FILE] [--vkey FILE]
      Print the CBOR of AuditingConfig and ProtocolParams.

plan, check-seal with --datum and encode use the Python standard library and
the aiken CLI. deploy and check-seal with --deployment need pycardano and the
shared deploy runner (shared/python/vector_deploy.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

MODULE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE_DIR.parent
CONTRACTS_DIR = MODULE_DIR / "contracts"
SHARED_PYTHON = REPO_ROOT / "shared" / "python"
DEFAULT_CONFIG = MODULE_DIR / "deploy" / "identity" / "config.json"
#: The state of a deployment: DIR/manifest.json, DIR/plan/, DIR/tx/. The
#: state directories are not in git.
STATE_ROOT = MODULE_DIR / "deploy" / "identity" / "state"

DAYS_30_MS = 2_592_000_000

#: The Module-1 modes: the value of ProtocolParams.oracle_active.
MODES = {"oracle": True, "jury": False}

#: The format of the identity deployment file of the agents-marketplace tool.
IDENTITY_DEPLOYMENT_FORMAT = "vector-identity-deployment/1"

#: The asset name of the CrossValidatorRefs token. The refs token policy of
#: Module-1, Module-3 and Module-6 is the same ScriptPubkey(operator) policy;
#: each module has its own token name.
REFS_TOKEN_NAME = b"auditing_refs"

#: The holder tag of the Module-1 records (shared/python/holder_scripts.py).
RECORDS_HOLDER_TAG = 11

NETWORKS = ("mainnet", "testnet")

# Validators of the new deployment, in the order of their hash dependencies.
# identity_hash, config_policy and config_holder come from the identity
# deployment (Part 2 §3.8 steps 1-3). claim, challenge and jury_pool take
# (AuditingConfig, ProtocolParams). identity_party takes (AuditingConfig,
# challenge hash, claim hash). The seal datum is the identity_party hash.
HASH_ORDER = [
    "config_holder, config_policy, identity (inputs from the identity deployment)",
    "claim(AuditingConfig, ProtocolParams)",
    "challenge(AuditingConfig, ProtocolParams)",
    "jury_pool(AuditingConfig, ProtocolParams)",
    "identity_party(AuditingConfig, challenge hash, claim hash) = M1",
    "seal: config_seal mints identity-config with the inline datum M1",
]

VALIDATORS = ("claim", "challenge", "jury_pool", "identity_party")


class PlanError(ValueError):
    """An input of the plan or of the deploy is missing or not valid."""


# ---------------------------------------------------------------------------
# Plutus Data and CBOR, encoded as Aiken's cbor.serialise (serialiseData) does:
# non-empty lists as indefinite arrays, empty lists as 0x80, byte strings
# longer than 64 bytes in 64-byte chunks, constructors 0..6 as tags 121..127.
# ---------------------------------------------------------------------------


class Constr:
    """A Plutus Data constructor."""

    def __init__(self, index: int, fields: list[Any]):
        self.index = index
        self.fields = fields


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    for size, marker in ((1, 24), (2, 25), (4, 26), (8, 27)):
        if value < 1 << (8 * size):
            return bytes([(major << 5) | marker]) + value.to_bytes(size, "big")
    raise ValueError("value too large for a CBOR head")


def _bytes(value: bytes) -> bytes:
    if len(value) <= 64:
        return _head(2, len(value)) + value
    chunks = b"".join(
        _head(2, len(value[i : i + 64])) + value[i : i + 64]
        for i in range(0, len(value), 64)
    )
    return b"\x5f" + chunks + b"\xff"


def _int(value: int) -> bytes:
    if value >= 0:
        if value < 1 << 64:
            return _head(0, value)
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return _head(6, 2) + _bytes(raw)
    negative = -1 - value
    if negative < 1 << 64:
        return _head(1, negative)
    raw = negative.to_bytes((negative.bit_length() + 7) // 8, "big")
    return _head(6, 3) + _bytes(raw)


def _list(items: list[Any]) -> bytes:
    if not items:
        return b"\x80"
    return b"\x9f" + b"".join(encode(item) for item in items) + b"\xff"


def encode(value: Any) -> bytes:
    """CBOR of a Plutus Data value (Constr, int, bytes or list)."""
    if isinstance(value, bool):
        raise TypeError("encode a Bool as Constr(0 | 1, [])")
    if isinstance(value, int):
        return _int(value)
    if isinstance(value, (bytes, bytearray)):
        return _bytes(bytes(value))
    if isinstance(value, list):
        return _list(value)
    if isinstance(value, Constr):
        if 0 <= value.index < 7:
            return _head(6, 121 + value.index) + _list(value.fields)
        if 7 <= value.index < 128:
            return _head(6, 1280 + value.index - 7) + _list(value.fields)
        return _head(6, 102) + _head(4, 2) + _int(value.index) + _list(value.fields)
    raise TypeError(f"cannot encode {type(value).__name__} as Plutus Data")


def decode_bytes(data: bytes) -> bytes:
    """Decode CBOR that holds exactly one byte string (definite or chunked)."""

    def read_head(pos: int) -> tuple[int, int, int]:
        initial = data[pos]
        major, info = initial >> 5, initial & 0x1F
        if info < 24:
            return major, info, pos + 1
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        if info not in sizes:
            raise ValueError("unsupported CBOR head")
        size = sizes[info]
        return major, int.from_bytes(data[pos + 1 : pos + 1 + size], "big"), pos + 1 + size

    if not data:
        raise ValueError("empty datum")
    if data[0] == 0x5F:
        pos, out = 1, b""
        while data[pos] != 0xFF:
            major, length, pos = read_head(pos)
            if major != 2:
                raise ValueError("chunk is not a byte string")
            out += data[pos : pos + length]
            pos += length
        pos += 1
    else:
        major, length, pos = read_head(0)
        if major != 2:
            raise ValueError("the datum is not a byte string")
        out = data[pos : pos + length]
        pos += length
    if pos != len(data):
        raise ValueError("trailing bytes after the byte string")
    return out


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def hash28(value: str, name: str) -> bytes:
    raw = bytes.fromhex(value)
    if len(raw) != 28:
        raise ValueError(f"{name} must be 28 bytes (56 hex characters)")
    return raw


# The hash fields of AuditingConfig that a deployment must set. A value with
# only zero bytes counts as a placeholder (the Module-3 and Module-6 deploy
# scripts refuse zero values in the same way): for example, a zero
# refs_token_policy lets claims open, but no claim, challenge or juror can be
# spent after that.
NONZERO_FIELDS = (
    ("identity", "identity_hash"),
    ("identity", "config_policy"),
    ("identity", "config_holder"),
    ("module1", "oracle_vkh"),
    ("module1", "refs_token_policy"),
)

ZERO_28 = "00" * 28


def zero_fields(values: dict[str, Any]) -> list[str]:
    """The paths ("section.name") of NONZERO_FIELDS that hold only zero bytes."""
    return [
        f"{section}.{name}"
        for section, name in NONZERO_FIELDS
        if not any(bytes.fromhex(values[section][name]))
    ]


def unsafe_values(values: dict[str, Any]) -> list[str]:
    """The declared placeholders, then each all-zero hash field and a
    missing identity reference script that the placeholders list does not
    name."""
    placeholders = list(values.get("placeholders", []))
    found = zero_fields(values)
    if not values.get("identity", {}).get("identity_ref"):
        found.append("identity.identity_ref")
    return placeholders + [path for path in found if path not in placeholders]


def verdict_rule(rule: dict[str, Any] | None, name: str) -> Constr:
    """Option<VerdictRule>: None = Constr 1 [], Some(r) = Constr 0 [r]."""
    if rule is None:
        return Constr(1, [])
    duration = int(rule["duration_ms"])
    if rule["action"] == "suspend":
        if not 0 < duration <= DAYS_30_MS:
            raise ValueError(f"{name}: a suspension lasts 1 ms to 30 days")
        action = Constr(0, [])
    elif rule["action"] == "revoke":
        if duration != 0:
            raise ValueError(f"{name}: a revocation has duration_ms 0")
        action = Constr(1, [])
    else:
        raise ValueError(f"{name}: action must be suspend or revoke")
    return Constr(0, [Constr(0, [action, duration])])


def auditing_config(values: dict[str, Any]) -> Constr:
    """AuditingConfig from {"identity": {...}, "module1": {...},
    "verdict_policy": {...}}."""
    identity, module1, policy = values["identity"], values["module1"], values["verdict_policy"]
    return Constr(
        0,
        [
            hash28(identity["identity_hash"], "identity_hash"),
            hash28(module1["oracle_vkh"], "oracle_vkh"),
            hash28(module1["refs_token_policy"], "refs_token_policy"),
            hash28(identity["config_policy"], "config_policy"),
            hash28(identity["config_holder"], "config_holder"),
            Constr(
                0,
                [
                    verdict_rule(policy["auditor_wins"], "auditor_wins"),
                    verdict_rule(policy["claimer_wins"], "claimer_wins"),
                ],
            ),
        ],
    )


PROTOCOL_FIELDS = [
    "min_claim_stake",
    "min_challenge_window",
    "max_challenge_window",
    "jury_size",
    "min_juror_bond",
    "jury_fee_rate",
    "selection_delay",
    "resolution_deadline",
    "juror_slash_rate",
    "min_agent_age",
    "max_concurrent_cases",
    "min_jury_pool_size",
    "min_jury_pool_total",
    "oracle_active",
    "commit_window",
    "reveal_window",
    "cleanup_buffer",
]


def protocol_params(params: dict[str, Any]) -> Constr:
    fields: list[Any] = []
    for name in PROTOCOL_FIELDS:
        value = params[name]
        if name == "oracle_active":
            fields.append(Constr(1 if value else 0, []))
        else:
            fields.append(int(value))
    return Constr(0, fields)


def mode_params(config: dict[str, Any], mode: str) -> dict[str, Any]:
    """The 17 ProtocolParams values of config.json with oracle_active of the
    mode."""
    if mode not in MODES:
        raise PlanError(f"unknown mode {mode!r}; use {' or '.join(MODES)}")
    params = dict(config["protocol_params"])
    if "oracle_active" in params:
        raise PlanError("config.json must not set protocol_params.oracle_active: --mode sets it")
    params["oracle_active"] = MODES[mode]
    return {name: params[name] for name in PROTOCOL_FIELDS}


def cross_refs(claim: str, challenge: str, jury_pool: str) -> Constr:
    return Constr(0, [bytes.fromhex(claim), bytes.fromhex(challenge), bytes.fromhex(jury_pool)])


def load_config(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Identity deployment and operator key
# ---------------------------------------------------------------------------


def _blake2b_224(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=28).digest()


def native_policy_id(key_hash: bytes) -> str:
    """The policy ID of ScriptPubkey(key_hash): blake2b-224 of 0x00 and the
    CBOR [0, key_hash]."""
    return _blake2b_224(b"\x00" + b"\x82\x00\x58\x1c" + bytes(key_hash)).hex()


def vkey_hash_from_file(path: Path) -> bytes:
    """The key hash of a cardano-cli payment verification key file
    (PaymentVerificationKeyShelley_ed25519)."""
    try:
        envelope = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise PlanError(f"cannot read the verification key file {path}: {error}") from None
    if envelope.get("type") != "PaymentVerificationKeyShelley_ed25519":
        raise PlanError(
            f"{path}: expected a PaymentVerificationKeyShelley_ed25519 file (the plan reads the "
            "public key; give --vkey)"
        )
    raw = bytes.fromhex(envelope.get("cborHex", ""))
    if raw[:2] != b"\x58\x20" or len(raw) != 34:
        raise PlanError(f"{path}: the verification key is not 32 bytes")
    return _blake2b_224(raw[2:])


def _ref(value: Any, name: str) -> str:
    tx_id, sep, index = str(value).partition("#")
    try:
        ok = sep == "#" and len(bytes.fromhex(tx_id)) == 32 and index.isdigit()
    except ValueError:
        ok = False
    if not ok:
        raise PlanError(f"{name} must be \"<64 hex>#<index>\", got {value!r}")
    return f"{tx_id.lower()}#{int(index)}"


def _hash_value(data: dict[str, Any], key: str, source: str) -> str:
    value = data.get(key)
    try:
        raw = hash28(str(value), key)
    except (TypeError, ValueError):
        raise PlanError(f"{source}: {key} must be 28 bytes of hex, got {value!r}") from None
    if not any(raw):
        raise PlanError(f"{source}: {key} is all zero")
    return raw.hex()


#: The all-zero key hash: its enterprise address can never spend.
UNSPENDABLE_KEY_HASH = bytes(28)


def address_payment_part(address: str) -> tuple[int, bytes]:
    """(header byte, payment credential) of a bech32 Shelley address."""
    hrp, _, data = address.partition("1")
    values = [_CHARSET.index(c) for c in data[:-6]]
    acc, bits, out = 0, 0, bytearray()
    for value in values:
        acc = (acc << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + [_CHARSET.index(c) for c in data]) != 1:
        raise PlanError(f"{address} has a bad bech32 checksum")
    return out[0], bytes(out[1:29])


def load_identity_deployment(path: Path, network: str, *, allow_placeholders: bool = False) -> dict[str, Any]:
    """The identity deployment.json of the agents-marketplace tool, checked.
    Returns the file content with "sha256" and "file" added."""
    source = str(path)
    try:
        text = Path(path).read_text()
        data = json.loads(text)
    except (OSError, ValueError) as error:
        raise PlanError(f"cannot read the identity deployment {path}: {error}") from None
    if data.get("format") != IDENTITY_DEPLOYMENT_FORMAT:
        raise PlanError(f"{source} is not an identity deployment file (format {IDENTITY_DEPLOYMENT_FORMAT})")
    if data.get("network") != network:
        raise PlanError(f"{source} is for network {data.get('network')!r}, not {network!r}")
    for key in ("identity_hash", "config_policy", "config_holder"):
        _hash_value(data, key, source)
    reference = str(data.get("reference_address") or "")
    header, payment = address_payment_part(reference) if reference else (None, None)
    if header != 0x61 or payment != UNSPENDABLE_KEY_HASH:
        raise PlanError(
            f"{source}: reference_address must be the enterprise address of the all-zero key hash "
            f"(network id 1), got {reference!r}"
        )
    for key in ("operator_address", "seal_seed_address"):
        if not data.get(key):
            raise PlanError(f"{source} has no {key}")
    if data["operator_address"] == data["seal_seed_address"]:
        raise PlanError(f"{source}: the operator address and the seal-seed address must differ")
    _ref(data.get("config_seed"), f"{source} config_seed")
    identity_ref = (data.get("refs") or {}).get("identity")
    if identity_ref:
        _ref(identity_ref, f"{source} refs.identity")
    elif not allow_placeholders:
        raise PlanError(
            f"{source} has no refs.identity: publish the identity reference scripts before the "
            "Module-1 plan (or use --allow-placeholders for a dry run)"
        )
    data["file"] = str(Path(path).resolve())
    data["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return data


def plan_values(
    config: dict[str, Any],
    deployment: Optional[dict[str, Any]],
    operator_key_hash: Optional[bytes],
    mode: str,
) -> dict[str, Any]:
    """The values of the plan: identity, module1, verdict_policy,
    protocol_params (with the oracle_active of the mode) and placeholders.
    A missing deployment or operator key gives all-zero placeholders."""
    if deployment is not None:
        identity = {
            "identity_hash": deployment["identity_hash"],
            "identity_ref": (deployment.get("refs") or {}).get("identity"),
            "config_policy": deployment["config_policy"],
            "config_holder": deployment["config_holder"],
        }
    else:
        identity = {"identity_hash": ZERO_28, "identity_ref": None,
                    "config_policy": ZERO_28, "config_holder": ZERO_28}
    if operator_key_hash is not None:
        module1 = {"oracle_vkh": operator_key_hash.hex(), "refs_token_policy": native_policy_id(operator_key_hash)}
    else:
        module1 = {"oracle_vkh": ZERO_28, "refs_token_policy": ZERO_28}
    module1["refs_token_name"] = REFS_TOKEN_NAME.hex()
    values = {
        "placeholders": list(config.get("placeholders", [])),
        "mode": mode,
        "identity": identity,
        "module1": module1,
        "verdict_policy": {key: config["verdict_policy"][key] for key in ("auditor_wins", "claimer_wins")},
        "protocol_params": mode_params(config, mode),
    }
    return values


# ---------------------------------------------------------------------------
# Addresses (CIP-19)
# ---------------------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            checksum ^= generator[i] if (top >> i) & 1 else 0
    return checksum


def bech32(hrp: str, payload: bytes) -> str:
    acc, bits, data = 0, 0, []
    for byte in payload:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    polymod = _polymod(expanded + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def script_addresses(script_hash: str) -> dict[str, str]:
    """The enterprise and reward addresses of a script. Vector mainnet and
    Vector testnet both use network id 1."""
    raw = bytes.fromhex(script_hash)
    return {
        "enterprise": bech32("addr", bytes([0x71]) + raw),
        "reward": bech32("stake", bytes([0xF1]) + raw),
    }


# ---------------------------------------------------------------------------
# aiken CLI
# ---------------------------------------------------------------------------


def aiken(*args: str) -> str:
    result = subprocess.run(
        ["aiken", *args], cwd=CONTRACTS_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"aiken {' '.join(args[:2])} failed with status {result.returncode}")
    return result.stdout


def blueprint_hash(blueprint: Path, validator: str) -> str:
    out = aiken("blueprint", "hash", "-i", str(blueprint), "-m", validator, "-v", validator)
    value = out.strip().splitlines()[-1].strip()
    hash28(value, f"{validator} hash")
    return value


def apply(blueprint: Path, out: Path, validator: str, parameter: bytes) -> None:
    aiken(
        "blueprint", "apply", "-i", str(blueprint), "-o", str(out),
        "-m", validator, "-v", validator, parameter.hex(),
    )


def records_holder(operator_key_hash_hex: str) -> dict[str, Any]:
    """The Module-1 records holder: holder(11, operator). Only the operator can
    spend its outputs (to retire the records)."""
    if str(SHARED_PYTHON) not in sys.path:
        sys.path.insert(0, str(SHARED_PYTHON))
    from holder_scripts import apply_holder

    holder = apply_holder(RECORDS_HOLDER_TAG, operator_key_hash_hex)
    return {
        "tag": RECORDS_HOLDER_TAG,
        "hash": holder["hash"],
        "address": script_addresses(holder["hash"])["enterprise"],
    }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def default_state_dir(network: str) -> Path:
    return STATE_ROOT / network


def read_manifest(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get("format") != "vector-module-deploy/1" or data.get("module") != "module1":
        raise PlanError(f"{path} is not a Module-1 manifest of this tool; use another --state-dir")
    return data


def apply_all(values: dict[str, Any], out_dir: Path) -> tuple[dict[str, str], dict[str, str], Path]:
    """Build and apply the four validators. Returns (hashes, parameters,
    applied blueprint path)."""
    config_cbor = encode(auditing_config(values))
    params_cbor = encode(protocol_params(values["protocol_params"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        work = Path(tmp)
        raw = work / "plutus.raw.json"
        aiken("build", "--trace-level", "silent", "-o", os.path.relpath(raw, CONTRACTS_DIR))

        current = raw
        step = 0

        def applied(validator: str, parameter: bytes) -> None:
            nonlocal current, step
            step += 1
            nxt = work / f"plutus.{step}.json"
            apply(current, nxt, validator, parameter)
            current = nxt

        for validator in ("claim", "challenge", "jury_pool"):
            applied(validator, config_cbor)
            applied(validator, params_cbor)
        hashes = {v: blueprint_hash(current, v) for v in ("claim", "challenge", "jury_pool")}

        applied("identity_party", config_cbor)
        applied("identity_party", encode(bytes.fromhex(hashes["challenge"])))
        applied("identity_party", encode(bytes.fromhex(hashes["claim"])))
        hashes["identity_party"] = blueprint_hash(current, "identity_party")

        blueprint = out_dir / "plutus.json"
        shutil.copyfile(current, blueprint)
    parameters = {
        "auditing_config_cbor": config_cbor.hex(),
        "protocol_params_cbor": params_cbor.hex(),
        "identity_party_challenge_hash_cbor": encode(bytes.fromhex(hashes["challenge"])).hex(),
        "identity_party_claim_hash_cbor": encode(bytes.fromhex(hashes["claim"])).hex(),
    }
    return hashes, parameters, blueprint


def command_plan(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        deployment = (
            load_identity_deployment(args.deployment, args.network, allow_placeholders=args.allow_placeholders)
            if args.deployment else None
        )
        operator = vkey_hash_from_file(args.vkey) if args.vkey else None
        if deployment is not None and operator is not None:
            check_operator_address(deployment, operator)
        values = plan_values(config, deployment, operator, args.mode)
    except PlanError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    placeholders = unsafe_values(values)
    if placeholders and not args.allow_placeholders:
        print(
            "The plan still has placeholders or all-zero values: "
            + ", ".join(placeholders)
            + ". Replace them, or use --allow-placeholders for a dry run.",
            file=sys.stderr,
        )
        return 2

    state_dir: Path = args.state_dir or default_state_dir(args.network)
    manifest_path = state_dir / "manifest.json"
    try:
        existing = read_manifest(manifest_path)
    except PlanError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    staging = state_dir / "plan.new"
    if staging.exists():
        shutil.rmtree(staging)
    hashes, parameters, _ = apply_all(values, staging)
    if existing and existing.get("steps") and existing.get("hashes") != hashes:
        shutil.rmtree(staging)
        print(
            f"ERROR: {manifest_path} records on-chain steps of a deployment with other script "
            f"hashes ({existing.get('hashes')}). Its hashes cannot change; use another --state-dir "
            "for a new deployment.",
            file=sys.stderr,
        )
        return 2
    plan_dir = state_dir / "plan"
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    staging.rename(plan_dir)
    blueprint = plan_dir / "plutus.json"

    m1 = hashes["identity_party"]
    holder = records_holder(values["module1"]["oracle_vkh"]) if operator is not None else None
    plan = {
        "not_for_deployment": bool(placeholders),
        "placeholders": placeholders,
        "network": args.network,
        "mode": args.mode,
        "trace_level": "silent",
        "config_file": str(args.config),
        "applied_blueprint": str(blueprint),
        "hash_order": HASH_ORDER,
        "parameters": parameters,
        "hashes": hashes,
        "addresses": {name: script_addresses(value) for name, value in hashes.items()},
        "cross_validator_refs_datum_cbor": encode(
            cross_refs(hashes["claim"], hashes["challenge"], hashes["jury_pool"])
        ).hex(),
        "seal": {
            "m1": m1,
            "datum_cbor": encode(bytes.fromhex(m1)).hex(),
            "note": "config_seal mints identity-config to config_holder with this inline datum. "
            "Register the stake credential of identity_party with the stake registration "
            "certificate without deposit field (no script witness): identity_party fails every "
            "publish purpose.",
        },
    }
    (state_dir / "plan" / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    manifest = dict(existing or {})
    manifest.update({
        "format": "vector-module-deploy/1",
        "module": "module1",
        "network": args.network,
        "not_for_deployment": bool(placeholders),
        "placeholders": placeholders,
        "mode": args.mode,
        "identity_deployment": (
            {"file": deployment["file"], "sha256": deployment["sha256"], "format": deployment["format"]}
            if deployment else None
        ),
        "identity": values["identity"],
        "module1": dict(values["module1"], records_holder=holder),
        "verdict_policy": values["verdict_policy"],
        "protocol_params": values["protocol_params"],
        "parameters": parameters,
        "hashes": hashes,
        "addresses": {name: script_addresses(value)["enterprise"] for name, value in hashes.items()},
        "identity_party_reward_address": script_addresses(m1)["reward"],
        "reference_address": deployment["reference_address"] if deployment else None,
        "plan": str(state_dir / "plan" / "plan.json"),
    })
    manifest.setdefault("steps", {})
    manifest.setdefault("pending", {})
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    if placeholders:
        print("DRY RUN WITH PLACEHOLDERS - NOT FOR DEPLOYMENT:", ", ".join(placeholders))
    print(f"{'mode':15} {args.mode} (oracle_active {str(MODES[args.mode]).lower()})")
    for name in VALIDATORS:
        print(f"{name:15} {hashes[name]}")
    print(f"{'M1 (seal)':15} {m1}")
    print(f"{'seal datum':15} {plan['seal']['datum_cbor']}")
    print(f"{'refs policy':15} {values['module1']['refs_token_policy']}")
    if holder:
        print(f"{'records holder':15} {holder['hash']} ({holder['address']})")
    print(f"plan: {state_dir / 'plan' / 'plan.json'}")
    print(f"manifest: {manifest_path}")
    return 0


def check_operator_address(deployment: dict[str, Any], operator_key_hash: bytes) -> None:
    """The operator key must be the operator of the identity deployment."""
    header, payment = address_payment_part(deployment["operator_address"])
    if header != 0x61 or payment != bytes(operator_key_hash):
        raise PlanError(
            f"the operator key {operator_key_hash.hex()} is not the key of the operator address "
            f"{deployment['operator_address']} of the identity deployment"
        )


# ---------------------------------------------------------------------------
# Seal
# ---------------------------------------------------------------------------


def seal_matches(datum_hex: str, expected_hash: str) -> bool:
    """True when the CBOR datum is the byte string of `expected_hash`."""
    sealed = decode_bytes(bytes.fromhex(datum_hex.strip()))
    if len(sealed) != 28:
        raise ValueError(f"the datum holds {len(sealed)} bytes, not 28")
    return sealed == hash28(expected_hash, "identity_party hash")


def _shared_runner():
    if str(SHARED_PYTHON) not in sys.path:
        sys.path.insert(0, str(SHARED_PYTHON))
    import vector_deploy

    return vector_deploy


def chain_seal_datum(vd, chain, deployment: dict[str, Any]) -> str:
    """The inline datum (CBOR hex) of the configuration record of the seal,
    read from the chain. The record must hold the configuration token at the
    config_holder address."""
    seal = deployment.get("seal")
    if not seal or not seal.get("config_record"):
        raise PlanError(f"{deployment['file']} has no seal yet (seal.config_record)")
    ref = vd.parse_ref(seal["config_record"])
    found = chain.ogmios.utxos(refs=[ref])
    if not found:
        raise PlanError(f"the configuration record {seal['config_record']} is not on chain")
    record = found[0]
    holder = vd.script_address(bytes.fromhex(deployment["config_holder"]))
    if record.output.address != holder:
        raise PlanError(f"the configuration record is at {record.output.address}, not at config_holder {holder}")
    policy = bytes.fromhex(deployment["config_policy"])
    amount = record.output.amount
    if not any(bytes(pid.payload) == policy for pid in (amount.multi_asset or {})):
        raise PlanError("the configuration record holds no token of config_policy")
    datum = vd.datum_cbor(record)
    if datum is None:
        raise PlanError("the configuration record has no inline datum")
    return datum.hex()


def command_check_seal(args: argparse.Namespace) -> int:
    blueprint = args.blueprint or ((args.state_dir or default_state_dir(args.network)) / "plan" / "plutus.json")
    if not blueprint.exists():
        print(f"{blueprint} is missing: run the plan command first.", file=sys.stderr)
        return 2
    expected = blueprint_hash(blueprint, "identity_party")
    if args.deployment is not None:
        vd = _shared_runner()
        try:
            deployment = load_identity_deployment(args.deployment, args.network)
            preset = vd.network_preset(args.network, ogmios_url=args.ogmios_url)
            chain = vd.DeployChain(vd.Ogmios(preset.ogmios_url), preset)
            chain.check_network()
            datum_hex = chain_seal_datum(vd, chain, deployment)
        except (PlanError, vd.DeployError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        written = (deployment.get("seal") or {}).get("module1_hash")
        if written and written != expected:
            print(f"MISMATCH: deployment.json seal.module1_hash is {written}, identity_party is {expected}",
                  file=sys.stderr)
            return 1
    else:
        datum_hex = args.datum if args.datum is not None else Path(args.datum_file).read_text()
    try:
        ok = seal_matches(datum_hex, expected)
    except ValueError as error:
        print(f"MISMATCH: {error}", file=sys.stderr)
        return 1
    if ok:
        print(f"OK: the configuration datum names identity_party {expected}")
        return 0
    sealed = decode_bytes(bytes.fromhex(datum_hex.strip())).hex()
    print(f"MISMATCH: the configuration datum names {sealed}, identity_party is {expected}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


def load_applied_scripts(blueprint: Path, hashes: dict[str, str]) -> dict[str, bytes]:
    """The compiled code of the four applied validators of the plan, checked
    against the manifest hashes (blake2b-224 of 0x03 and the script)."""
    data = json.loads(Path(blueprint).read_text())
    scripts: dict[str, bytes] = {}
    for name in VALIDATORS:
        found = [v for v in data["validators"] if v["title"].startswith(f"{name}.{name}.")]
        if not found or found[0].get("parameters"):
            raise PlanError(f"{blueprint}: {name} is missing or has open parameters")
        code = bytes.fromhex(found[0]["compiledCode"])
        if _blake2b_224(b"\x03" + code).hex() != hashes[name]:
            raise PlanError(f"{blueprint}: the compiled code of {name} does not have the hash {hashes[name]}")
        scripts[name] = code
    return scripts


def check_manifest(manifest: dict[str, Any], deployment: dict[str, Any], operator: bytes, mode: str) -> None:
    """The manifest of the plan must match the identity deployment, the
    operator key and the mode of the deploy."""
    if manifest.get("mode") != mode:
        raise PlanError(f"the plan is for mode {manifest.get('mode')!r}, not {mode!r}; run plan again")
    identity = manifest.get("identity") or {}
    for key in ("identity_hash", "config_policy", "config_holder"):
        if identity.get(key) != deployment[key]:
            raise PlanError(f"the manifest has identity.{key} {identity.get(key)}, the identity "
                            f"deployment has {deployment[key]}; run plan again")
    if identity.get("identity_ref") != (deployment.get("refs") or {}).get("identity"):
        raise PlanError("the manifest identity.identity_ref differs from refs.identity of the identity "
                        "deployment; run plan again")
    module1 = manifest.get("module1") or {}
    if module1.get("oracle_vkh") != operator.hex():
        raise PlanError(f"the plan has oracle_vkh {module1.get('oracle_vkh')}, the key is {operator.hex()}")
    if module1.get("refs_token_policy") != native_policy_id(operator):
        raise PlanError("the plan refs_token_policy is not ScriptPubkey(operator)")
    if manifest.get("reference_address") != deployment["reference_address"]:
        raise PlanError("the manifest reference_address differs from the identity deployment")
    expected = encode(auditing_config(manifest)).hex()
    if (manifest.get("parameters") or {}).get("auditing_config_cbor") != expected:
        raise PlanError("the plan parameters do not encode the manifest values; run plan again")
    if (manifest.get("parameters") or {}).get("protocol_params_cbor") != encode(
        protocol_params(manifest["protocol_params"])
    ).hex():
        raise PlanError("the plan ProtocolParams do not encode the manifest values; run plan again")


def update_manifest(state) -> None:
    """Write the reference UTxOs of the recorded steps into the manifest
    (the keys that the Module-1 clients read)."""
    data = state.data
    steps = data.get("steps", {})
    for name, key in (("claim", "claim_ref"), ("challenge", "challenge_ref"),
                      ("jury_pool", "jury_pool_ref"), ("identity_party", "identity_party_ref")):
        ref = steps.get(f"publish-{name}", {}).get("ref")
        if ref:
            data[key] = ref
    records = steps.get("records", {})
    if records.get("cross_refs"):
        data["cross_refs_utxo"] = records["cross_refs"]
        data["params_utxo"] = records["params"]
    registration = steps.get("register-identity_party")
    if registration:
        data["identity_party_registration"] = {
            key: registration.get(key) for key in ("tx", "deposit", "found_on_chain") if registration.get(key) is not None
        }
    state.save()


def module1_steps(vd, run, manifest: dict[str, Any], scripts: dict[str, bytes]) -> list:
    """The on-chain steps of Module-1."""
    from pycardano import (
        Address, PlutusV3Script, ScriptHash, StakeCredential, StakeRegistration,
    )
    from pycardano.serialization import RawCBOR

    reference = Address.from_primitive(manifest["reference_address"])
    holder = Address.from_primitive(manifest["module1"]["records_holder"]["address"])
    hashes = manifest["hashes"]
    operator = bytes.fromhex(manifest["module1"]["oracle_vkh"])
    native = vd.operator_native_script(operator)
    policy = vd.native_policy_id(native)
    name = bytes.fromhex(manifest["module1"]["refs_token_name"])
    refs_datum = encode(cross_refs(hashes["claim"], hashes["challenge"], hashes["jury_pool"]))
    params_datum = encode(protocol_params(manifest["protocol_params"]))
    m1 = bytes.fromhex(hashes["identity_party"])

    steps = []
    for validator in VALIDATORS:
        script = PlutusV3Script(scripts[validator])
        script_hash = bytes.fromhex(hashes[validator])

        def build(run, script=script):
            builder = run.builder()
            builder.add_output(vd.exact_output(run.chain, reference, script=script))
            return builder

        steps.append(vd.Step(
            name=f"publish-{validator}",
            title=f"publish the {validator} reference script ({len(scripts[validator]):,} bytes)",
            build=build,
            find=lambda run, h=script_hash: vd.find_reference_script(run, reference, h),
            record=lambda tx: {"ref": f"{tx}#0"},
        ))

    def build_registration(run):
        builder = run.builder()
        builder.certificates = [StakeRegistration(StakeCredential(ScriptHash(m1)))]
        return builder

    def find_registration(run):
        # Ogmios lists only delegated reward accounts, and Script(M1) can
        # never be delegated: Koios shows the registration.
        if run.chain.koios.registered(vd.stake_address(m1, script=True)):
            return {"tx": None, "registered": True}
        return None

    steps.append(vd.Step(
        name="register-identity_party",
        title="register the stake credential Script(M1) (certificate 0, no script witness)",
        build=build_registration,
        find=find_registration,
        record=lambda tx: {"deposit": run.protocol.key_deposit},
    ))

    def build_records(run):
        builder = run.builder()
        builder.mint = vd.tokens(policy, {name: 1})
        builder.native_scripts = [native]
        builder.add_output(vd.exact_output(run.chain, holder, assets=vd.tokens(policy, {name: 1}),
                                           datum=RawCBOR(refs_datum)))
        builder.add_output(vd.exact_output(run.chain, holder, datum=RawCBOR(params_datum)))
        return builder

    def find_records(run):
        for utxo in run.live(addresses=[str(holder)]):
            if vd.has_token(utxo, policy, name) and vd.datum_equals(utxo, refs_datum):
                tx = bytes(utxo.input.transaction_id).hex()
                params = [u for u in run.live(refs=[vd.parse_ref(f"{tx}#{utxo.input.index + 1}")])
                          if vd.datum_equals(u, params_datum)]
                if not params:
                    raise vd.DeployError(f"records: the CrossValidatorRefs record {vd.ref_key(utxo.input)} "
                                         "is on chain, but the ProtocolParams record of its transaction is not")
                return {"tx": tx, "cross_refs": vd.ref_key(utxo.input), "params": vd.ref_key(params[0].input)}
        return None

    steps.append(vd.Step(
        name="records",
        title="mint the CrossValidatorRefs token; create the CrossValidatorRefs and ProtocolParams records",
        build=build_records,
        find=find_records,
        record=lambda tx: {"cross_refs": f"{tx}#0", "params": f"{tx}#1"},
    ))
    return steps


def command_deploy(args: argparse.Namespace) -> int:
    vd = _shared_runner()
    state_dir: Path = args.state_dir or default_state_dir(args.network)
    manifest_path = state_dir / "manifest.json"
    try:
        if not manifest_path.exists():
            raise PlanError(f"{manifest_path} does not exist: run the plan command first")
        state = vd.DeployState.load(manifest_path, module="module1", network=args.network)
        manifest = state.data
        if args.action == "sign":
            operator = vd.operator_key_hash_from_args(args)
            vd.sign_pending(state, vd.load_signing_key(args.skey), operator, print)
            return 0
        operator = vd.operator_key_hash_from_args(args)
        deployment = load_identity_deployment(args.deployment, args.network)
        check_operator_address(deployment, operator)
        check_manifest(manifest, deployment, operator, args.mode)
        sending = args.submit or args.action in ("build", "submit")
        if manifest.get("placeholders") and (sending or not args.allow_placeholders):
            raise PlanError(
                "the plan has placeholders (" + ", ".join(manifest["placeholders"]) + "). "
                + ("Sending needs a plan without placeholders." if sending
                   else "Use --allow-placeholders for a dry run.")
            )
        scripts = load_applied_scripts(state_dir / "plan" / "plutus.json", manifest["hashes"])
        holder = records_holder(operator.hex())
        if manifest["module1"].get("records_holder") != holder:
            raise PlanError("the manifest records holder is not holder(11, operator); run plan again")

        preset = vd.network_from_args(args)
        chain = vd.DeployChain(vd.Ogmios(preset.ogmios_url), preset)
        chain.check_network()
        try:
            sealed = chain_seal_datum(vd, chain, deployment)
            seal_ok = seal_matches(sealed, manifest["hashes"]["identity_party"])
            seal_note = "the seal on chain names M1" if seal_ok else "the seal on chain names ANOTHER hash"
        except PlanError as error:
            seal_ok, seal_note = False, str(error)
        print(f"Seal check: {seal_note}")
        if sending and not seal_ok:
            raise PlanError("Module-1 is deployed only after the seal of M1 (check-seal must pass)")

        run = vd.Run(
            chain, operator,
            protected={deployment["config_seed"]},
            protected_addresses={deployment["seal_seed_address"]},
            dry_run_funds=int(args.dry_run_funds * vd.LOVELACE_PER_AP3X),
            labels={
                deployment["reference_address"]: "the reference address (unspendable)",
                holder["address"]: "the Module-1 records holder",
            },
        )
        if str(run.operator_address) != deployment["operator_address"]:
            raise PlanError("the operator key does not match operator_address of the identity deployment")
        steps = module1_steps(vd, run, manifest, scripts)
        print(f"Module-1 deploy on {preset.name} ({'send' if sending else 'dry run'}), mode {args.mode}, "
              f"M1 {manifest['hashes']['identity_party']}")
        vd.execute(
            args, run, steps, state,
            tx_dir=state_dir / "tx",
            header={"module": "module1", "network": preset.name, "mode": args.mode,
                    "hashes": manifest["hashes"], "time": vd.utc_now()},
            after=lambda: update_manifest(state),
        )
        if sending:
            update_manifest(state)
    except (PlanError, vd.DeployError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def command_encode(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        deployment = (
            load_identity_deployment(args.deployment, args.network, allow_placeholders=True)
            if args.deployment else None
        )
        operator = vkey_hash_from_file(args.vkey) if args.vkey else None
        values = plan_values(config, deployment, operator, args.mode)
    except PlanError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(
        {
            "mode": args.mode,
            "placeholders": unsafe_values(values),
            "auditing_config_cbor": encode(auditing_config(values)).hex(),
            "protocol_params_cbor": encode(protocol_params(values["protocol_params"])).hex(),
        },
        indent=2,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub, *, config: bool = True) -> None:
        sub.add_argument("--mode", choices=sorted(MODES), required=True)
        if config:
            sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        sub.add_argument("--deployment", type=Path, help="the identity deployment.json")
        sub.add_argument("--state-dir", type=Path, help="default: deploy/identity/state/<network>")

    plan = commands.add_parser("plan", help="apply the parameters, write the plan and the manifest")
    common(plan)
    plan.add_argument("--vkey", type=Path, help="the operator verification key file")
    plan.add_argument("--network", choices=NETWORKS, default="mainnet")
    plan.add_argument("--allow-placeholders", action="store_true")
    plan.set_defaults(run=command_plan)

    check = commands.add_parser("check-seal", help="compare the configuration datum with identity_party")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--datum", help="inline datum of the configuration record, CBOR hex")
    source.add_argument("--datum-file", help="file with the CBOR hex of the datum")
    source.add_argument("--deployment", type=Path, help="read seal.config_record of this identity deployment from the chain")
    check.add_argument("--blueprint", type=Path, help="default: <state-dir>/plan/plutus.json")
    check.add_argument("--state-dir", type=Path)
    check.add_argument("--network", choices=NETWORKS, default="mainnet")
    check.add_argument("--ogmios-url")
    check.set_defaults(run=command_check_seal)

    deploy = commands.add_parser("deploy", help="the on-chain steps (dry run by default)")
    common(deploy, config=False)
    deploy.add_argument("--allow-placeholders", action="store_true", help="dry run of a plan with placeholders")
    if (argv if argv is not None else sys.argv[1:])[:1] == ["deploy"]:
        # The deploy options need pycardano (shared/python/vector_deploy.py);
        # plan, check-seal and encode run on the standard library.
        _shared_runner().add_run_arguments(deploy)
    deploy.set_defaults(run=command_deploy)

    enc = commands.add_parser("encode", help="print the parameter CBOR")
    common(enc)
    enc.add_argument("--vkey", type=Path)
    enc.add_argument("--network", choices=NETWORKS, default="mainnet")
    enc.set_defaults(run=command_encode)

    args = parser.parse_args(argv)
    if args.command == "deploy" and args.deployment is None and args.action != "sign":
        parser.error("deploy needs --deployment (the identity deployment.json)")
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
