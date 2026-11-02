#!/usr/bin/env python3
"""Module-1 identity deployment plan and configuration check (Part 2 §3.8, §7).

Commands:

  plan [--config PATH] [--out DIR] [--allow-placeholders]
      Build the Module-1 blueprint (traces silent), apply the parameters in
      the dependency order of the script hashes and write DIR/plutus.json
      (the applied blueprint) and DIR/plan.json. Print the script hashes, the
      addresses, the CrossValidatorRefs datum and the seal datum: the hash of
      identity_party (M1) as a Plutus Data byte string.

  check-seal (--datum HEX | --datum-file PATH) [--blueprint PATH]
      Compare the inline datum of the configuration record (CBOR hex, as
      read from the ledger) with the identity_party hash of the applied
      blueprint. Exit status 1 when they differ. Run it after the seal and
      before anybody appoints Module-1 (Part 2 §3.8 step 6, §12.6).

  encode [--config PATH]
      Print the CBOR of AuditingConfig and ProtocolParams.

The script uses the Python standard library and the `aiken` CLI only. It does
not sign, submit or query anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = MODULE_DIR / "contracts"
DEFAULT_CONFIG = MODULE_DIR / "deploy" / "identity" / "config.json"
DEFAULT_OUT = CONTRACTS_DIR / "build" / "identity-deploy"

DAYS_30_MS = 2_592_000_000

# Validators of the new deployment, in the order of their hash dependencies.
# identity_hash, config_policy and config_holder come from the identity
# deployment (Part 2 §3.8 steps 1-3). claim, challenge and jury_pool take
# (AuditingConfig, ProtocolParams). identity_party takes (AuditingConfig,
# challenge hash). The seal datum is the identity_party hash.
HASH_ORDER = [
    "config_holder, config_policy, identity (inputs from the identity deployment)",
    "claim(AuditingConfig, ProtocolParams)",
    "challenge(AuditingConfig, ProtocolParams)",
    "jury_pool(AuditingConfig, ProtocolParams)",
    "identity_party(AuditingConfig, challenge hash) = M1",
    "seal: config_seal mints identity-config with the inline datum M1",
]


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


def auditing_config(config: dict[str, Any]) -> Constr:
    identity, module1, policy = config["identity"], config["module1"], config["verdict_policy"]
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


def cross_refs(claim: str, challenge: str, jury_pool: str) -> Constr:
    return Constr(0, [bytes.fromhex(claim), bytes.fromhex(challenge), bytes.fromhex(jury_pool)])


def load_config(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


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


def script_addresses(script_hash: str, mainnet: bool) -> dict[str, str]:
    network = 1 if mainnet else 0
    raw = bytes.fromhex(script_hash)
    return {
        "enterprise": bech32("addr" if mainnet else "addr_test", bytes([0x70 | network]) + raw),
        "reward": bech32("stake" if mainnet else "stake_test", bytes([0xF0 | network]) + raw),
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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def command_encode(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(json.dumps(
        {
            "auditing_config_cbor": encode(auditing_config(config)).hex(),
            "protocol_params_cbor": encode(protocol_params(config["protocol_params"])).hex(),
        },
        indent=2,
    ))
    return 0


def command_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    placeholders = config.get("placeholders", [])
    if placeholders and not args.allow_placeholders:
        print(
            "The configuration still has placeholders: " + ", ".join(placeholders)
            + ". Replace them, or use --allow-placeholders for a dry run.",
            file=sys.stderr,
        )
        return 2

    mainnet = config.get("network", "mainnet") == "mainnet"
    config_cbor = encode(auditing_config(config))
    params_cbor = encode(protocol_params(config["protocol_params"]))

    out_dir: Path = args.out
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
        hashes["identity_party"] = blueprint_hash(current, "identity_party")

        blueprint = out_dir / "plutus.json"
        shutil.copyfile(current, blueprint)

    m1 = hashes["identity_party"]
    plan = {
        "not_for_deployment": bool(placeholders),
        "placeholders": placeholders,
        "network": "mainnet" if mainnet else "testnet",
        "trace_level": "silent",
        "config_file": str(args.config),
        "applied_blueprint": str(blueprint),
        "hash_order": HASH_ORDER,
        "parameters": {
            "auditing_config_cbor": config_cbor.hex(),
            "protocol_params_cbor": params_cbor.hex(),
            "identity_party_challenge_hash_cbor": encode(bytes.fromhex(hashes["challenge"])).hex(),
        },
        "hashes": hashes,
        "addresses": {name: script_addresses(value, mainnet) for name, value in hashes.items()},
        "cross_validator_refs_datum_cbor": encode(
            cross_refs(hashes["claim"], hashes["challenge"], hashes["jury_pool"])
        ).hex(),
        "seal": {
            "m1": m1,
            "datum_cbor": encode(bytes.fromhex(m1)).hex(),
            "note": "config_seal mints identity-config to config_holder with this inline datum. "
            "Register the stake credential of identity_party with the legacy stake registration "
            "certificate (no script witness): identity_party fails every publish purpose.",
        },
    }
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    if placeholders:
        print("DRY RUN WITH PLACEHOLDERS - NOT FOR DEPLOYMENT:", ", ".join(placeholders))
    for name in ("claim", "challenge", "jury_pool", "identity_party"):
        print(f"{name:15} {hashes[name]}")
    print(f"{'M1 (seal)':15} {m1}")
    print(f"{'seal datum':15} {plan['seal']['datum_cbor']}")
    print(f"plan: {out_dir / 'plan.json'}")
    print(f"blueprint: {blueprint}")
    return 0


def seal_matches(datum_hex: str, expected_hash: str) -> bool:
    """True when the CBOR datum is the byte string of `expected_hash`."""
    sealed = decode_bytes(bytes.fromhex(datum_hex.strip()))
    if len(sealed) != 28:
        raise ValueError(f"the datum holds {len(sealed)} bytes, not 28")
    return sealed == hash28(expected_hash, "identity_party hash")


def command_check_seal(args: argparse.Namespace) -> int:
    datum_hex = args.datum if args.datum is not None else Path(args.datum_file).read_text()
    blueprint = args.blueprint
    if not blueprint.exists():
        print(f"{blueprint} is missing: run the plan command first.", file=sys.stderr)
        return 2
    expected = blueprint_hash(blueprint, "identity_party")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="apply the parameters and print the hashes")
    plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    plan.add_argument("--out", type=Path, default=DEFAULT_OUT)
    plan.add_argument("--allow-placeholders", action="store_true")
    plan.set_defaults(run=command_plan)

    check = commands.add_parser("check-seal", help="compare the configuration datum with identity_party")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--datum", help="inline datum of the configuration record, CBOR hex")
    source.add_argument("--datum-file", help="file with the CBOR hex of the datum")
    check.add_argument("--blueprint", type=Path, default=DEFAULT_OUT / "plutus.json")
    check.set_defaults(run=command_check_seal)

    enc = commands.add_parser("encode", help="print the parameter CBOR")
    enc.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    enc.set_defaults(run=command_encode)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
