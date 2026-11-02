"""
The Module 3 deployment that a client, the indexer or the dashboard uses.

The deploy scripts (scripts/deploy_ogmios.py, deploy_mainnet_ogmios.py and
deploy_docker.py) write deploy/deploy_state.json (testnet) and
deploy/mainnet/deploy_state.json (mainnet). `load_deployment` reads such a
state and checks it. There are no default values: a missing or malformed
value is an error that names the deployment order.

A state without `identity_hash` belongs to an old deployment (it has
`agent_registry_hash`, the legacy agent registry) or to a deployment without
record tokens (no `params_token_policy`). The SDK refuses such a state. Move
the old file to a different path before a new deploy run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from pycardano import Address

from reputation_staking import vector_identity as vi
from reputation_staking.plutus_data import ReputationConfig
from reputation_staking.utils import script_hash_to_address

DEPLOYMENT_ORDER = (
    "Deployment order: identity registry, Module-1, Module-3, Module-6 "
    "(Part 2 §3.8 and §7 of docs/IDENTITY_REGISTRY.md in agents-marketplace; "
    "Module-3 runs scripts/deploy_ogmios.py, deploy_mainnet_ogmios.py or "
    "deploy_docker.py)."
)

# Deploy state keys with a 28-byte hash or policy ID (hex).
_HASH_KEYS = (
    "reputation_validator_hash",
    "endorsement_validator_hash",
    "refs_token_policy",
    "params_holder_hash",
    "treasury_holder_hash",
    "module1_claim_validator_hash",
    "module1_challenge_validator_hash",
    "module1_jury_pool_hash",
    "module1_refs_token_policy",
    "wallet_vkey_hash",
)

# Deploy state keys with the address of a script: (address key, hash key).
_ADDRESS_KEYS = (
    ("reputation_address", "reputation_validator_hash"),
    ("endorsement_address", "endorsement_validator_hash"),
    ("params_holder_address", "params_holder_hash"),
    ("treasury_holder_address", "treasury_holder_hash"),
)

# Transactions of the deployment (tx_hashes); each output is at index 0.
_TX_KEYS = ("reputation_spend_ref", "endorsement_spend_ref", "cross_refs_nft")


class DeploymentStateError(ValueError):
    """A deploy state is missing, belongs to an old deployment or has a
    missing or malformed value."""


@dataclass(frozen=True)
class Deployment:
    """The values of one Module 3 deployment (hex strings, bech32 addresses).

    `oracle_vkey_hash` is the `oracle_credential` of ReputationConfig: the
    deploy scripts set it to the deploy wallet key (`wallet_vkey_hash`).
    """

    network: str
    reputation_validator_hash: str
    endorsement_validator_hash: str
    reputation_address: str
    endorsement_address: str
    refs_token_policy: str
    params_holder_hash: str
    params_holder_address: str
    params_token_policy: str
    treasury_holder_hash: str
    treasury_holder_address: str
    identity_hash: str
    module1_claim_validator_hash: str
    module1_challenge_validator_hash: str
    module1_jury_pool_hash: str
    module1_refs_token_policy: str
    oracle_vkey_hash: str
    ap3x_policy_id: str
    ap3x_asset_name: str
    reputation_spend_ref: str
    endorsement_spend_ref: str
    cross_refs_nft: str

    @property
    def identity_address(self) -> str:
        """The identity address IA: the enterprise script address of
        identity_hash (no stake part)."""
        return script_hash_to_address(self.identity_hash)

    def reputation_config(self) -> ReputationConfig:
        """The ReputationConfig (12 fields) that the deploy scripts applied to
        both validators."""
        return ReputationConfig(
            refs_token_policy=bytes.fromhex(self.refs_token_policy),
            audit_claim_validator_hash=bytes.fromhex(self.module1_claim_validator_hash),
            audit_challenge_validator_hash=bytes.fromhex(self.module1_challenge_validator_hash),
            audit_jury_pool_hash=bytes.fromhex(self.module1_jury_pool_hash),
            audit_refs_token_policy=bytes.fromhex(self.module1_refs_token_policy),
            params_script_hash=bytes.fromhex(self.params_holder_hash),
            treasury_script_hash=bytes.fromhex(self.treasury_holder_hash),
            ap3x_policy_id=bytes.fromhex(self.ap3x_policy_id),
            ap3x_asset_name=bytes.fromhex(self.ap3x_asset_name),
            oracle_credential=bytes.fromhex(self.oracle_vkey_hash),
            params_token_policy=bytes.fromhex(self.params_token_policy),
            identity_hash=bytes.fromhex(self.identity_hash),
        )


def _fail(source: str, problem: str) -> DeploymentStateError:
    return DeploymentStateError(f"{source}: {problem}\n{DEPLOYMENT_ORDER}")


def _old_state_problem(state: Mapping[str, Any]) -> Optional[str]:
    if not state.get("identity_hash"):
        if state.get("agent_registry_hash"):
            return (
                "the deploy state has agent_registry_hash and no identity_hash. "
                "It belongs to an old deployment with the legacy agent registry. "
                "Move the file to a different path and use the state of a new "
                "deployment."
            )
        return (
            "the deploy state has no identity_hash. It belongs to an old "
            "deployment or to an incomplete deploy run. Move the file to a "
            "different path and use the state of a new deployment."
        )
    if not state.get("params_token_policy"):
        return (
            "the deploy state has no params_token_policy. It belongs to a "
            "deployment without record tokens. Move the file to a different "
            "path and use the state of a new deployment."
        )
    return None


def _hash(source: str, state: Mapping[str, Any], key: str) -> str:
    value = state.get(key)
    if not value:
        raise _fail(source, f"the deploy state has no {key}. The deploy run is not complete.")
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError):
        raise _fail(source, f"{key} is not hex: {value!r}.") from None
    if len(raw) != 28:
        raise _fail(source, f"{key} must be 28 bytes, got {len(raw)} bytes.")
    return raw.hex()


def _optional_hex(source: str, state: Mapping[str, Any], key: str, max_len: int) -> str:
    value = state.get(key) or ""
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError):
        raise _fail(source, f"{key} is not hex: {value!r}.") from None
    if len(raw) > max_len:
        raise _fail(source, f"{key} must be at most {max_len} bytes, got {len(raw)} bytes.")
    return raw.hex()


def _address(source: str, state: Mapping[str, Any], key: str, script_hash: str) -> str:
    value = state.get(key)
    if not value:
        raise _fail(source, f"the deploy state has no {key}. The deploy run is not complete.")
    try:
        Address.from_primitive(value)
    except Exception:
        raise _fail(source, f"{key} is not an address: {value!r}.") from None
    expected = script_hash_to_address(script_hash)
    if value != expected:
        raise _fail(
            source,
            f"{key} {value} is not the script address of its hash ({expected}).",
        )
    return value


def load_deployment(
    state: Union[Mapping[str, Any], str, Path], source: Optional[str] = None
) -> Deployment:
    """Check a deploy state (a dict or the path of a JSON file) and return the
    deployment. Raises DeploymentStateError with the deployment order when
    the file is missing, the state belongs to an old deployment or a value is
    missing or malformed."""
    if isinstance(state, (str, Path)):
        path = Path(state)
        source = source or str(path)
        if not path.exists():
            raise _fail(source, "the deploy state file does not exist.")
        with open(path) as f:
            state = json.load(f)
    source = source or "deploy state"
    if not isinstance(state, Mapping):
        raise _fail(source, "the deploy state is not a JSON object.")

    problem = _old_state_problem(state)
    if problem:
        raise _fail(source, problem)

    identity_hash = _hash(source, state, "identity_hash")
    if bytes.fromhex(identity_hash) == vi.LEGACY_REGISTRY_HASH:
        raise _fail(
            source,
            "identity_hash is the hash of the legacy agent registry, not of the "
            "identity contract. Move the file to a different path and use the "
            "state of a new deployment.",
        )
    params_token_policy = _hash(source, state, "params_token_policy")
    hashes = {key: _hash(source, state, key) for key in _HASH_KEYS}
    addresses = {
        key: _address(source, state, key, hashes[hash_key])
        for key, hash_key in _ADDRESS_KEYS
    }

    tx_hashes = state.get("tx_hashes") or {}
    refs = {}
    for key in _TX_KEYS:
        tx_hash = tx_hashes.get(key)
        try:
            ok = tx_hash is not None and len(bytes.fromhex(tx_hash)) == 32
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise _fail(
                source,
                f"tx_hashes.{key} is missing or is not a 32-byte transaction ID. "
                "The deploy run is not complete.",
            )
        refs[key] = f"{tx_hash.lower()}#0"

    return Deployment(
        network=str(state.get("network") or ""),
        reputation_validator_hash=hashes["reputation_validator_hash"],
        endorsement_validator_hash=hashes["endorsement_validator_hash"],
        reputation_address=addresses["reputation_address"],
        endorsement_address=addresses["endorsement_address"],
        refs_token_policy=hashes["refs_token_policy"],
        params_holder_hash=hashes["params_holder_hash"],
        params_holder_address=addresses["params_holder_address"],
        params_token_policy=params_token_policy,
        treasury_holder_hash=hashes["treasury_holder_hash"],
        treasury_holder_address=addresses["treasury_holder_address"],
        identity_hash=identity_hash,
        module1_claim_validator_hash=hashes["module1_claim_validator_hash"],
        module1_challenge_validator_hash=hashes["module1_challenge_validator_hash"],
        module1_jury_pool_hash=hashes["module1_jury_pool_hash"],
        module1_refs_token_policy=hashes["module1_refs_token_policy"],
        oracle_vkey_hash=hashes["wallet_vkey_hash"],
        ap3x_policy_id=_optional_hex(source, state, "ap3x_policy_id", 28),
        ap3x_asset_name=_optional_hex(source, state, "ap3x_asset_name", 32),
        reputation_spend_ref=refs["reputation_spend_ref"],
        endorsement_spend_ref=refs["endorsement_spend_ref"],
        cross_refs_nft=refs["cross_refs_nft"],
    )
