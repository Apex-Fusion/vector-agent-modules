"""Jury deployments: the manifest format, the ``JuryParams`` codec, the value
rules of SPEC §3 and the applied validator.

A deployment manifest (``Module-1/jury/deploy/<network>/manifest.json``)::

    {
      "format": "vector-module1-jury-deployment/1",
      "network": "mainnet",
      "params": {"identity_hash": "<56 hex>", "m1_claim": "<56 hex>",
                 "m1_challenge": "<56 hex>", "treasury": "<bech32>",
                 "bond_min": 250000000, ...},          # the 16 JuryParams fields
      "params_cbor": "<hex>",                          # the applied parameter
      "script_hash": "<56 hex>",                       # J: hash, policy id
      "address": "<bech32>",                           # Script(J), no stake
      "reference_script": "<tx id>#<index>",           # the applied script
      "plutus": "plutus.json",                         # applied blueprint, relative to the manifest
      "txs": {"reference_script": "<tx id>"},
      "time": "2026-11-09T12:00:00Z"
    }

Optional keys: ``test`` (true for a deployment with test values),
``reference_address``, ``script_size``, ``compiler``. Readers ignore other
keys. The relay and the reference juror follow a list of manifests.
"""

import dataclasses
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from pycardano import Address, Network, PlutusV3Script, ScriptHash, TransactionId, TransactionInput

from . import _paths
from .types import JuryParams, PARAM_FIELDS, address_of, decode_params, plutus_address, same_data, to_cbor

MANIFEST_FORMAT = "vector-module1-jury-deployment/1"
#: The validator in the blueprint: module ``jury``, validator ``jury``.
VALIDATOR_MODULE = "jury"
VALIDATOR_NAME = "jury"
BLUEPRINT_TITLE_PREFIX = f"{VALIDATOR_MODULE}.{VALIDATOR_NAME}."
#: The enterprise address of the all-zero key hash: nobody can spend it. The
#: reference script output stays there.
REFERENCE_ADDRESS = "addr1vyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqkdl5mw"
#: The unapplied blueprint that ``aiken build`` writes.
BLUEPRINT = _paths.CONTRACTS_DIR / "plutus.json"

#: The mainnet values of SPEC §3.
MAINNET_PARAMS: Dict[str, Any] = {
    "identity_hash": "d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762",
    "m1_claim": "53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523",
    "m1_challenge": "9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08",
    "treasury": "addr1wx2yu92dtp70q0stsv8kmnrv284lh836lp0l936x6gsrggshvzzlc",
    "bond_min": 250_000_000,
    "slash": 25_000_000,
    "juror_floor": 10_000_000,
    "eligibility_delay": 259_200_000,
    "exit_notice": 259_200_000,
    "commit_window": 7_200_000,
    "reveal_window": 7_200_000,
    "resolve_margin": 7_200_000,
    "claim_period": 2_592_000_000,
    "quorum": 3,
    "majority_num": 2,
    "majority_den": 3,
}

_HASH_FIELDS = ("identity_hash", "m1_claim", "m1_challenge")
_INT_FIELDS = tuple(f for f in PARAM_FIELDS if f not in _HASH_FIELDS and f != "treasury")


class DeploymentError(ValueError):
    """A manifest, a parameter set or an applied script is not valid."""


def default_manifest_path(network: str = "mainnet") -> Path:
    return _paths.DEPLOY_DIR / network / "manifest.json"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── JuryParams ───────────────────────────────────────────────────────────────


def params_from_json(data: Mapping[str, Any]) -> JuryParams:
    """JuryParams from the ``params`` object of a manifest (or a params file):
    exactly the 16 field names; hashes as 56 hex; ``treasury`` as bech32;
    integers as JSON numbers."""
    if not isinstance(data, Mapping):
        raise DeploymentError("params must be a JSON object")
    missing = [f for f in PARAM_FIELDS if f not in data]
    extra = [k for k in data if k not in PARAM_FIELDS]
    if missing or extra:
        raise DeploymentError(f"params: missing {missing or 'none'}, unknown {extra or 'none'}")
    values: Dict[str, Any] = {}
    for name in _HASH_FIELDS:
        try:
            raw = bytes.fromhex(str(data[name]))
        except ValueError:
            raw = b""
        if len(raw) != 28:
            raise DeploymentError(f"params.{name} must be 56 hex characters")
        values[name] = raw
    try:
        values["treasury"] = plutus_address(Address.from_primitive(str(data["treasury"])))
    except Exception as error:  # noqa: BLE001 - pycardano raises several types
        raise DeploymentError(f"params.treasury is not an address: {error}") from None
    for name in _INT_FIELDS:
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise DeploymentError(f"params.{name} must be an integer")
        values[name] = value
    return JuryParams(**values)


def params_to_json(params: JuryParams, network: Network = Network.MAINNET) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in PARAM_FIELDS:
        value = getattr(params, name)
        if name in _HASH_FIELDS:
            out[name] = bytes(value).hex()
        elif name == "treasury":
            out[name] = str(address_of(value, network))
        else:
            out[name] = int(value)
    return out


def params_problems(params: JuryParams) -> List[str]:
    """The value rules of SPEC §3 that ``params`` breaks (the deploy tool
    checks them; the validator does not)."""
    problems = []
    for name in _HASH_FIELDS:
        if len(bytes(getattr(params, name))) != 28:
            problems.append(f"{name} must be 28 bytes")
    for name in _INT_FIELDS:
        if getattr(params, name) <= 0:
            problems.append(f"{name} must be positive")
    if params.bond_min < params.juror_floor + params.slash:
        problems.append(f"bond_min ({params.bond_min}) must be at least juror_floor + slash "
                        f"({params.juror_floor + params.slash})")
    if not params.majority_num < params.majority_den < 2 * params.majority_num:
        problems.append(f"majority_num < majority_den < 2 * majority_num does not hold "
                        f"({params.majority_num}/{params.majority_den})")
    return problems


def encode_params(params: JuryParams) -> bytes:
    """The Plutus Data CBOR of JuryParams (the parameter of the validator)."""
    return to_cbor(params)


# ── Applied script ───────────────────────────────────────────────────────────


def script_hash_of(code: bytes) -> bytes:
    """The Plutus V3 script hash: blake2b-224 of 0x03 and the script bytes."""
    return hashlib.blake2b(b"\x03" + bytes(code), digest_size=28).digest()


def script_address(script_hash: bytes) -> Address:
    """The jury address: payment credential Script(J), no stake credential."""
    return Address(payment_part=ScriptHash(bytes(script_hash)), network=Network.MAINNET)


def blueprint_validator(blueprint: Union[str, Path], prefix: str = BLUEPRINT_TITLE_PREFIX) -> dict:
    """The first validator of a blueprint whose title starts with ``prefix``
    (``jury.jury.``)."""
    try:
        data = json.loads(Path(blueprint).read_text())
    except (OSError, ValueError) as error:
        raise DeploymentError(f"cannot read the blueprint {blueprint}: {error}") from None
    found = [v for v in data.get("validators", []) if v.get("title", "").startswith(prefix)]
    if not found:
        raise DeploymentError(f"{blueprint} has no validator {prefix}*")
    return found[0]


def load_applied_script(blueprint: Union[str, Path], script_hash: Union[bytes, str]) -> PlutusV3Script:
    """The applied jury validator of a blueprint, checked: no open parameter,
    and blake2b_224(0x03 || script bytes) == ``script_hash``."""
    expected = bytes.fromhex(script_hash) if isinstance(script_hash, str) else bytes(script_hash)
    validator = blueprint_validator(blueprint)
    if validator.get("parameters"):
        raise DeploymentError(f"{blueprint}: the validator has open parameters")
    code = bytes.fromhex(validator["compiledCode"])
    actual = script_hash_of(code)
    if actual != expected:
        raise DeploymentError(f"{blueprint}: the script hash is {actual.hex()}, the manifest says {expected.hex()}")
    return PlutusV3Script(code)


# ── Manifests ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Deployment:
    """One jury deployment (a loaded manifest)."""

    network: str
    params: JuryParams
    params_cbor: bytes
    script_hash: bytes
    address: Address
    reference_script: TransactionInput
    plutus: Optional[Path]
    txs: Dict[str, str]
    time: str
    test: bool = False
    #: The window of challenges that this deployment takes (SPEC §12.1):
    #: current when ``cases_from <= challenged_at < cases_until`` (POSIX ms;
    #: None = open end).
    cases_from: Optional[int] = None
    cases_until: Optional[int] = None
    path: Optional[Path] = None
    raw: Dict[str, Any] = dataclasses.field(default_factory=dict)
    _script: Optional[PlutusV3Script] = dataclasses.field(default=None, repr=False)

    @property
    def policy_id(self) -> bytes:
        return self.script_hash

    @property
    def label(self) -> str:
        return self.script_hash.hex()

    @property
    def identity_address(self) -> Address:
        """The identity address: Script(identity_hash), no stake."""
        return script_address(self.params.identity_hash)

    def current_for(self, challenged_at: int) -> bool:
        """True when this deployment is current for a challenge (§12.1). A
        deployment without ``cases_from`` is current for no challenge."""
        return self.cases_from is not None and self.cases_from <= challenged_at and \
            (self.cases_until is None or challenged_at < self.cases_until)

    @property
    def treasury_address(self) -> Address:
        return address_of(self.params.treasury)

    def script(self) -> PlutusV3Script:
        """The applied validator from ``plutus`` (checked against the hash)."""
        if self._script is None:
            if self.plutus is None:
                raise DeploymentError("the manifest names no applied blueprint (plutus)")
            self._script = load_applied_script(self.plutus, self.script_hash)
        return self._script

    def to_json(self) -> Dict[str, Any]:
        data = dict(self.raw)
        data.update({
            "format": MANIFEST_FORMAT,
            "network": self.network,
            "params": params_to_json(self.params),
            "params_cbor": self.params_cbor.hex(),
            "script_hash": self.script_hash.hex(),
            "address": str(self.address),
            "reference_script": f"{bytes(self.reference_script.transaction_id).hex()}#{self.reference_script.index}",
            "txs": dict(self.txs),
            "time": self.time,
        })
        if self.plutus is not None:
            base = self.path.parent if self.path is not None else None
            data["plutus"] = str(self.plutus.relative_to(base)) if base and self.plutus.is_relative_to(base) \
                else str(self.plutus)
        for key in ("cases_from", "cases_until"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        if self.test:
            data["test"] = True
        return data


def _optional_int(data: Mapping[str, Any], key: str) -> Optional[int]:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeploymentError(f"{key} must be an integer (POSIX ms) or null")
    return value


def _hex(value: Any, size: int, what: str) -> bytes:
    try:
        raw = bytes.fromhex(str(value))
    except ValueError:
        raw = b""
    if len(raw) != size:
        raise DeploymentError(f"{what} must be {2 * size} hex characters")
    return raw


def deployment_from_json(data: Mapping[str, Any], *, path: Optional[Path] = None) -> Deployment:
    """A Deployment from manifest JSON, checked: the format, the params and
    their CBOR, the address of the script hash, the reference script UTxO."""
    if not isinstance(data, Mapping) or data.get("format") != MANIFEST_FORMAT:
        raise DeploymentError(f"{path or 'the manifest'} is not a {MANIFEST_FORMAT} manifest")
    for key in ("network", "params", "params_cbor", "script_hash", "address", "reference_script"):
        if key not in data:
            raise DeploymentError(f"{path or 'the manifest'} has no {key!r}")
    params = params_from_json(data["params"])
    try:
        params_cbor = bytes.fromhex(data["params_cbor"])
        applied = decode_params(params_cbor)
    except Exception as error:  # noqa: BLE001
        raise DeploymentError(f"params_cbor is not a JuryParams: {error}") from None
    if not same_data(applied, params):
        raise DeploymentError("params_cbor does not encode the params of the manifest")
    script_hash = _hex(data["script_hash"], 28, "script_hash")
    address = script_address(script_hash)
    if str(data["address"]) != str(address):
        raise DeploymentError(f"address {data['address']} is not the address of the script hash ({address})")
    tx_id, sep, index = str(data["reference_script"]).partition("#")
    if sep != "#" or not index.isdigit():
        raise DeploymentError("reference_script must be \"<tx id>#<index>\"")
    reference = TransactionInput(TransactionId(_hex(tx_id, 32, "reference_script tx id")), int(index))
    plutus = data.get("plutus")
    plutus_path = None
    if plutus:
        plutus_path = Path(plutus)
        if not plutus_path.is_absolute() and path is not None:
            plutus_path = Path(path).parent / plutus_path
    return Deployment(
        network=str(data["network"]),
        params=params,
        params_cbor=params_cbor,
        script_hash=script_hash,
        address=address,
        reference_script=reference,
        plutus=plutus_path,
        txs={str(k): str(v) for k, v in (data.get("txs") or {}).items()},
        time=str(data.get("time", "")),
        test=bool(data.get("test", False)),
        cases_from=_optional_int(data, "cases_from"),
        cases_until=_optional_int(data, "cases_until"),
        path=Path(path) if path is not None else None,
        raw=dict(data),
    )


def load_manifest(path: Union[str, Path]) -> Deployment:
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise DeploymentError(f"cannot read {path}: {error.strerror}") from None
    except ValueError:
        raise DeploymentError(f"{path} is not JSON") from None
    return deployment_from_json(data, path=path)


def load_manifests(paths: Iterable[Union[str, Path]]) -> List[Deployment]:
    """The deployments of several manifests, in order. They must be of one
    network and have distinct script hashes."""
    deployments = [load_manifest(p) for p in paths]
    networks = {d.network for d in deployments}
    if len(networks) > 1:
        raise DeploymentError(f"the manifests are of several networks: {sorted(networks)}")
    hashes = [d.script_hash for d in deployments]
    if len(set(hashes)) != len(hashes):
        raise DeploymentError("two manifests have the same script hash")
    # SPEC §12.1: the windows cases_from / cases_until do not overlap.
    windows = sorted((d.cases_from, d.cases_until, d.script_hash.hex()) for d in deployments if d.cases_from is not None)
    for (start_a, end_a, a), (start_b, _, b) in zip(windows, windows[1:]):
        if end_a is None or end_a > start_b:
            raise DeploymentError(f"the windows of the deployments {a} and {b} overlap")
    return deployments


def write_manifest(path: Union[str, Path], data: Mapping[str, Any]) -> Path:
    """Write manifest JSON (checked first) atomically."""
    path = Path(path)
    deployment_from_json(data, path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    return path
