"""
Module 6: shared deploy helpers

The deploy tools (deploy.py, deploy_params.py and treasury_fund.py), the
scripts that read a deploy state (smoke_test.py, test_expire.py,
test_expire_e2e.py, update_params.py, treasury_replenish.py), the scripts of
the previous deployment (redeploy_proposal.py, recreate_infra.py,
move_crossrefs.py, through old_deploy_helpers.py) and the client module
governance_identity.py use these helpers. They need cbor2 and the aiken CLI.
They do not need pycardano or a chain connection; only state_endpoints reads
the network presets of shared/python/vector_deploy.py (pycardano).

  - HOLDER_TAGS, apply_holder, apply_record_token_policy: the holder scripts
    and the one-shot record token policy of shared/python/holder_scripts.py.
    Module-6 has its own holder tags (61, 62, 63), so its records and its
    treasury never share an address with another module of the same
    operator key.
  - load_identity_deployment, check_operator_address: the identity
    deployment.json of the agents-marketplace tool, checked with the rules of
    Module-1 (Module-1/scripts/identity_deploy.py, module1_identity_deploy).
  - load_module1_manifest: the Module-1 manifest.json, for the seal check of
    deploy.py.
  - build_governance_config_cbor: GovernanceConfig (11 fields)
  - apply_governance_config: GovernanceConfig applied to the five validators
  - script_hash_to_address: the enterprise address of a script
  - governance_params_data, DEFAULT_GOVERNANCE_PARAMS: GovernanceParams (21
    fields)
  - oracle_datum_data: GovernanceOracleDatum (3 fields)
  - treasury_batch_data: TreasuryBatchDatum (2 fields)
  - build_cross_refs_data: GovernanceCrossRefs (5 fields, with the
    endorsement validator hash last)
  - build_activity_head_data: the head of the activity list
    (ProposerActivityDatum, 5 fields)
  - plutus_data_cbor: the Plutus CBOR of a datum
  - state_tx_hashes, client_settings: the values of a deploy state that the
    clients read
  - add_state_arguments, state_path, read_state, state_endpoints: the --state
    and --network options of the scripts that read a deploy state
  - redeploy_state_error, find_record_utxo: checks of the older scripts

The datum builders give Plutus Data trees (cbor2.CBORTag), in the field order
of the Aiken types in
contracts/governance-suggestion/lib/governance_suggestion/types.ak.

Deployment order (Part 2 §3.8 and §7 of docs/IDENTITY_REGISTRY.md in
agents-marketplace):

  1. config_holder.
  2. config_seal, bound to config_holder and a fixed seed.
  3. identity, bound to both. Its script hash (= policy ID) is identity_hash.
  4. Module-1, a new deployment with identity_party.
  5. The seal: the configuration token with the hash of identity_party.
  6. Module-3. ReputationConfig embeds identity_hash and the Module-1 hashes
     of step 4.
  7. Module-6 (these scripts). GovernanceConfig embeds identity_hash.

Inputs: the identity deployment.json (format vector-identity-deployment/1).
deploy.py reads identity_hash, reference_address, operator_address,
seal_seed_address, config_seed and the seal from it. It stops when
identity_hash is missing, is not 28 bytes of hex, is all zero or is the
legacy agent registry hash. A send also needs the Module-1 manifest.json:
deploy.py sends only when the seal on chain names identity_party (M1) of the
manifest (Module-1 seal_status).

Operator key: one new operator key deploys every module. It owns the
holders, it is the oracle credential and its ScriptPubkey policy mints the
CrossRefs token. It is the key of operator_address of the identity
deployment. Old module keys are not used again.
"""

import importlib.util
import json
import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import cbor2

# ── Paths ────────────────────────────────────────────────────────────────────

MODULE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE_DIR.parent
SHARED_PYTHON = REPO_ROOT / "shared" / "python"
if str(SHARED_PYTHON) not in sys.path:
    sys.path.insert(0, str(SHARED_PYTHON))

import vector_identity as vid  # noqa: E402  (Module-6/scripts)
# The deploy tools and the older scripts import these names from deploy_common.
from holder_scripts import (  # noqa: E402, F401  (shared/python)
    HOLDER_BLUEPRINT_PATH,
    HOLDER_TAGS as _HOLDER_TAGS_BY_MODULE,
    HOLDER_TITLE,
    RECORD_TOKEN_TITLE,
    aiken_apply,
    apply_holder,
    apply_record_token_policy,
    validator_entry,
)

# The older scripts import these names from deploy_common.
_aiken_apply = aiken_apply
_validator = validator_entry

#: The deploy script of Module-1. Its identity deployment checks and its seal
#: check are the rules of every module.
MODULE1_IDENTITY_DEPLOY = REPO_ROOT / "Module-1" / "scripts" / "identity_deploy.py"

#: The Module-6 blueprint (the validators without parameters).
CONTRACT_DIR = MODULE_DIR / "contracts" / "governance-suggestion"
BLUEPRINT_PATH = CONTRACT_DIR / "plutus.json"

#: The state of the deployment on the identity registry (scripts/deploy.py):
#: STATE_ROOT/<network>/STATE_FILE. The state is not in git.
STATE_ROOT = MODULE_DIR / "deploy" / "identity" / "state"
STATE_FILE = "deploy_state.json"
#: The format of the states of shared/python/vector_deploy.py.
STATE_FORMAT = "vector-module-deploy/1"
NETWORKS = ("mainnet", "testnet")
#: The explorer of each network (the state of the previous deployment keeps
#: its explorer_url).
EXPLORER_URLS = {"mainnet": "https://vector.apexscan.org", "testnet": "https://vector.testnet.apexscan.org"}

# ── Constants ────────────────────────────────────────────────────────────────

ZEROED_28 = "00" * 28

#: The holder tags of Module-6 (shared/python/holder_scripts.py):
#: [(61, "params"), (62, "oracle"), (63, "treasury")]. The CrossRefs output
#: also sits at the oracle holder.
HOLDER_TAGS = sorted((tag, role) for role, tag in _HOLDER_TAGS_BY_MODULE["module6"].items())

# Asset names of the record tokens (shared/lib/shared/record_auth.ak). The
# head of the activity list holds the `activity` token
# (contracts/governance-suggestion/lib/governance_suggestion/utils.ak,
# activity_head_token).
PARAMS_TOKEN_NAME = b"params"
ORACLE_TOKEN_NAME = b"oracle"
ACTIVITY_TOKEN_NAME = b"activity"

#: The asset name of the CrossRefs token. The refs token policy of Module-1,
#: Module-3 and Module-6 is the same ScriptPubkey(operator) policy; each
#: module has its own token name.
REFS_TOKEN_NAME = b"governance_refs"

# Redeemer data of the record token policy and of the holder: unit
# (constructor 0, no fields). Both scripts ignore the redeemer.
UNIT_REDEEMER = cbor2.CBORTag(121, [])

#: The five applied validators of a deployment: {title: name}. The deploy
#: state keeps the reference script of each one as tx_hashes["<name>_ref"]
#: (output 0 of the transaction).
VALIDATOR_TITLES = {
    "proposal.proposal_mint.mint": "proposal_mint",
    "proposal.proposal_spend.spend": "proposal_spend",
    "critique.critique_mint.mint": "critique_mint",
    "critique.critique_spend.spend": "critique_spend",
    "critique.endorsement_spend.spend": "endorsement_spend",
}
PROPOSAL_SPEND = "proposal.proposal_spend.spend"
PROPOSAL_MINT = "proposal.proposal_mint.mint"
CRITIQUE_SPEND = "critique.critique_spend.spend"
CRITIQUE_MINT = "critique.critique_mint.mint"
ENDORSEMENT_SPEND = "critique.endorsement_spend.spend"

#: GovernanceParams of a new deployment: the values of the live v8
#: deployment (deploy/mainnet/deployment.json, governance_params) and the
#: three Phase 1.2 values. The order is the field order of the Aiken type.
#: Times are POSIX milliseconds, amounts are lovelace, shares are basis
#: points.
DEFAULT_GOVERNANCE_PARAMS = {
    "min_proposal_stake": 25_000_000,
    "min_critique_stake": 5_000_000,
    "min_governance_endorsement": 10_000_000,
    "min_review_window": 259_200_000,
    "max_review_window": 2_419_200_000,
    "max_amendments": 5,
    "max_active_proposals": 3,
    "proposal_cooldown": 86_400_000,
    "proposer_reward_share": 7_000,
    "critic_reward_share": 2_000,
    "protocol_fee_rate": 1_000,
    "min_adoption_reward": 50_000_000,
    "max_adoption_reward": 500_000_000,
    "max_incorporated_critiques": 10,
    "max_treasury_request": 10_000_000_000,
    "emergency_stake_multiplier": 5_000,
    "emergency_review_window": 43_200_000,
    "param_execution_delay": 604_800_000,
    "min_prediction_pool": 100_000_000,
    "credibility_pool_low_threshold": 500_000_000,
    "credibility_pool_critical_threshold": 100_000_000,
}

#: The 21 fields of GovernanceParams, in the order of the Aiken type.
GOVERNANCE_PARAMS_FIELDS = tuple(DEFAULT_GOVERNANCE_PARAMS)


# ── Validation ───────────────────────────────────────────────────────────────

def _hex_bytes(name: str, value: str, size: Optional[int] = None) -> bytes:
    """Decode a hex string. With `size`, the result must have that length."""
    raw = bytes.fromhex(value)
    if size is not None and len(raw) != size:
        raise ValueError(f"{name} must be {size} bytes, got {len(raw)} bytes")
    return raw


# ── Deployment input ─────────────────────────────────────────────────────────

DEPLOYMENT_ORDER = (
    "Deployment order (Part 2 §3.8 and §7 of docs/IDENTITY_REGISTRY.md): "
    "config_holder, config_seal, identity, Module-1 with identity_party, "
    "the seal, then Module-3, then Module-6."
)

# The legacy agent registry (policy ID = script hash). It is not allowed as
# identity_hash.
LEGACY_REGISTRY_HASH = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"


class DeployInputError(ValueError):
    """A value from another deployment is missing or not allowed."""


def _identity_hash_problem(value: Optional[str]) -> Optional[str]:
    """Why `value` is not allowed as identity_hash, or None when it is allowed."""
    source = "the identity deployment (the script hash of `identity`)"
    if not value:
        return f"identity_hash is not set. Use the identity_hash of {source}."
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return f"identity_hash is not hex: {value!r}."
    if len(raw) != 28:
        return f"identity_hash must be 28 bytes, got {len(raw)} bytes."
    if raw == bytes(28):
        return f"identity_hash is all zero. Use the identity_hash of {source}."
    if raw.hex() == LEGACY_REGISTRY_HASH:
        return f"identity_hash is the legacy agent registry hash. Use the identity_hash of {source}."
    return None


def _identity_hash_bytes(value: Optional[str]) -> bytes:
    """Decode identity_hash. Raises DeployInputError, with the deployment
    order, when the value is missing or not allowed."""
    problem = _identity_hash_problem(value)
    if problem:
        raise DeployInputError(f"{problem}\n{DEPLOYMENT_ORDER}")
    return bytes.fromhex(value)


@lru_cache(maxsize=1)
def module1_identity_deploy():
    """Module-1/scripts/identity_deploy.py as a module: the identity
    deployment checks (load_identity_deployment, check_operator_address) and
    the seal check (seal_status). It needs only the Python standard library;
    seal_status reads the chain with the shared deploy runner."""
    spec = importlib.util.spec_from_file_location("module1_identity_deploy", MODULE1_IDENTITY_DEPLOY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_identity_deployment(path, network: str) -> dict:
    """The identity deployment.json of the agents-marketplace tool, checked
    with the rules of Module-1 (identity_deploy.load_identity_deployment):
    the format, the network, identity_hash, config_policy and config_holder
    (28 bytes, not zero), reference_address (the enterprise address of the
    all-zero key hash), operator_address and seal_seed_address (both set and
    different) and config_seed. Module-6 does not read refs.identity, so the
    file can come before the identity reference scripts. identity_hash must
    not be the legacy agent registry hash.

    Returns identity_hash, config_policy, config_holder, reference_address,
    operator_address, seal_seed_address, config_seed (lower case), seal (the
    seal of the file, or None), sealed, network, file and sha256: the values
    of Module-1 seal_status too. Raises DeployInputError."""
    module1 = module1_identity_deploy()
    try:
        data = module1.load_identity_deployment(Path(path), network, allow_placeholders=True)
    except module1.PlanError as error:
        raise DeployInputError(str(error)) from None
    identity_hash = _identity_hash_bytes(str(data["identity_hash"]).lower()).hex()
    tx_id, _, index = str(data["config_seed"]).partition("#")
    return {
        "network": data["network"],
        "identity_hash": identity_hash,
        "config_policy": str(data["config_policy"]).lower(),
        "config_holder": str(data["config_holder"]).lower(),
        "reference_address": data["reference_address"],
        "operator_address": data["operator_address"],
        "seal_seed_address": data["seal_seed_address"],
        "config_seed": f"{tx_id.lower()}#{int(index)}",
        "seal": data.get("seal") or None,
        "sealed": bool(data.get("seal")),
        "file": data["file"],
        "sha256": data["sha256"],
    }


def check_operator_address(identity: Mapping[str, Any], operator_key_hash: bytes) -> None:
    """The operator key must be the key of operator_address of the identity
    deployment (an enterprise key address). Raises DeployInputError."""
    module1 = module1_identity_deploy()
    try:
        module1.check_operator_address(identity, bytes(operator_key_hash))
    except module1.PlanError as error:
        raise DeployInputError(str(error)) from None


def load_module1_manifest(path, identity: Mapping[str, Any]) -> dict:
    """The Module-1 manifest.json (Module-1/scripts/identity_deploy.py plan)
    for the seal check: a manifest of Module-1 (format
    vector-module-deploy/1, module "module1") of the network of the identity
    deployment, with the identity_hash of the identity deployment and the
    28-byte hash hashes.identity_party (M1). Raises DeployInputError that
    lists every problem."""
    try:
        manifest = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise DeployInputError(f"cannot read the Module-1 manifest {path}: {error}") from None
    if not isinstance(manifest, dict):
        raise DeployInputError(f"{path} is not a Module-1 manifest")
    problems = []
    if manifest.get("format") != STATE_FORMAT or manifest.get("module") != "module1":
        problems.append(f"{path} is not a manifest of Module-1/scripts/identity_deploy.py.")
    if manifest.get("network") != identity.get("network"):
        problems.append(f"the Module-1 manifest is for network {manifest.get('network')!r}, the identity "
                        f"deployment for {identity.get('network')!r}.")
    if (manifest.get("identity") or {}).get("identity_hash") != identity.get("identity_hash"):
        problems.append("the Module-1 manifest names another identity_hash than the identity deployment.")
    m1 = str((manifest.get("hashes") or {}).get("identity_party") or "")
    try:
        valid = len(bytes.fromhex(m1)) == 28
    except ValueError:
        valid = False
    if not valid:
        problems.append("the Module-1 manifest has no 28-byte hashes.identity_party (M1).")
    if problems:
        raise DeployInputError("\n".join(problems + [DEPLOYMENT_ORDER]))
    return manifest


# ── GovernanceConfig CBOR ────────────────────────────────────────────────────

def build_governance_config_cbor(
    refs_token_policy: str,
    oracle_holder_hash: str,
    params_holder_hash: str,
    treasury_holder_hash: str,
    record_token_policy: str,
    identity_hash: str,
) -> str:
    """Build GovernanceConfig as Plutus Data CBOR (hex).

    Constructor 0 with 11 fields (all ByteArray) in the order of the Aiken
    type: tag 121 and a definite array (head 0x8b). `aiken blueprint apply`
    keeps the CBOR form of its argument, so the script hashes depend on it:
    the SDKs apply the same definite form. identity_hash is the last field.
    When identity_hash is missing, all zero or the legacy agent registry
    hash, the function raises DeployInputError with the deployment order.

    reputation_validator_hash and jury_pool_hash stay zero, as before: the
    validators then skip the Module-3 checks that use them. A non-zero value
    must be the hash of the new Module-3 reputation validator (it embeds
    identity_hash and the new Module-1 hashes) or of the new Module-1 jury
    pool. It must never be a hash of the old deployments.
    """
    fields = [
        _hex_bytes("refs_token_policy", refs_token_policy, 28),        # refs_token_policy
        bytes.fromhex(ZEROED_28),               # prediction_validator_hash
        bytes.fromhex(ZEROED_28),               # reputation_validator_hash (zero, see above)
        bytes.fromhex(ZEROED_28),               # jury_pool_hash (zero, see above)
        _hex_bytes("oracle_holder_hash", oracle_holder_hash, 28),      # governance_oracle_hash
        _hex_bytes("params_holder_hash", params_holder_hash, 28),      # governance_params_hash
        _hex_bytes("treasury_holder_hash", treasury_holder_hash, 28),  # governance_treasury_hash
        bytes.fromhex(ZEROED_28),               # credibility_pool_hash (Phase 1.2)
        bytes.fromhex(ZEROED_28),               # protocol_params_hash
        _hex_bytes("record_token_policy", record_token_policy, 28),    # record_token_policy
        _identity_hash_bytes(identity_hash),                           # identity_hash
    ]
    # Plutus Data constructor: Tag 121 + 0 = constr(0, fields)
    return cbor2.dumps(cbor2.CBORTag(121, fields)).hex()


def apply_governance_config(config_cbor_hex: str, blueprint_path: Path = BLUEPRINT_PATH) -> dict:
    """Apply GovernanceConfig to the five Module-6 validators with
    `aiken blueprint apply` and return the applied blueprint. The
    intermediate blueprints go to a temporary directory: nothing is written
    into contracts/."""
    with tempfile.TemporaryDirectory(prefix="module6-apply-") as tmp:
        current = Path(blueprint_path)
        for number, title in enumerate(VALIDATOR_TITLES, start=1):
            module, validator, _ = title.split(".")
            out = Path(tmp) / f"applied-{number}.json"
            aiken_apply(current, module, validator, config_cbor_hex, out)
            current = out
        return json.loads(current.read_text())


def script_hash_to_address(script_hash_hex: str) -> str:
    """The enterprise address of a script (bech32 "addr"; Vector mainnet and
    Vector testnet both use the network id 1)."""
    raw = _hex_bytes("script_hash", script_hash_hex, 28)
    return module1_identity_deploy().script_addresses(raw.hex())["enterprise"]


# ── Datums ───────────────────────────────────────────────────────────────────

def plutus_data_cbor(tree: Any) -> bytes:
    """The Plutus CBOR of a Plutus Data tree, as Aiken's cbor.serialise
    writes it (vector_identity.serialise_data). The deploy tools write each
    inline datum in this form."""
    return vid.serialise_data(tree)


def check_governance_params(values: Mapping[str, Any]) -> dict:
    """The 21 GovernanceParams values, in the order of the Aiken type. Each
    value must be an integer and not negative. Raises DeployInputError for a
    missing, unknown or bad value."""
    missing = [name for name in GOVERNANCE_PARAMS_FIELDS if name not in values]
    unknown = sorted(set(values) - set(GOVERNANCE_PARAMS_FIELDS))
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unknown:
            parts.append("unknown " + ", ".join(unknown))
        raise DeployInputError(f"GovernanceParams has 21 fields: {'; '.join(parts)}")
    bad = [name for name in GOVERNANCE_PARAMS_FIELDS
           if isinstance(values[name], bool) or not isinstance(values[name], int) or values[name] < 0]
    if bad:
        raise DeployInputError("GovernanceParams values must be integers, 0 or more: " + ", ".join(bad))
    return {name: values[name] for name in GOVERNANCE_PARAMS_FIELDS}


def governance_params_data(values: Mapping[str, Any] = DEFAULT_GOVERNANCE_PARAMS) -> cbor2.CBORTag:
    """GovernanceParams as Plutus Data: constructor 0 with the 21 integers in
    the order of the Aiken type (governance_identity.GovernanceParams gives
    the same tree)."""
    checked = check_governance_params(values)
    return cbor2.CBORTag(121, [checked[name] for name in GOVERNANCE_PARAMS_FIELDS])


def governance_params_from_data(tree: Any) -> Optional[dict]:
    """The 21 GovernanceParams values of a decoded datum, or None when the
    datum is not a GovernanceParams datum."""
    if not isinstance(tree, cbor2.CBORTag) or tree.tag != 121:
        return None
    fields = list(tree.value)
    if len(fields) != len(GOVERNANCE_PARAMS_FIELDS):
        return None
    if any(isinstance(v, bool) or not isinstance(v, int) for v in fields):
        return None
    return dict(zip(GOVERNANCE_PARAMS_FIELDS, fields))


def oracle_datum_data(oracle_vkh_hex: str, treasury_holder_hash: str) -> cbor2.CBORTag:
    """GovernanceOracleDatum as Plutus Data: constructor 0 with 3 fields.

    oracle_credential is VerificationKey(oracle) (constructor 0 with the
    28-byte key hash). The validators refuse a Script credential.
    treasury_script_hash is the treasury holder hash. oracle_active is True
    (Aiken Bool: constructor 1, no fields).
    """
    oracle = _hex_bytes("oracle_vkh_hex", oracle_vkh_hex, 28)
    treasury = _hex_bytes("treasury_holder_hash", treasury_holder_hash, 28)
    return cbor2.CBORTag(121, [cbor2.CBORTag(121, [oracle]), treasury, cbor2.CBORTag(122, [])])


def treasury_batch_data(batch_id: int, active: bool = True) -> cbor2.CBORTag:
    """TreasuryBatchDatum as Plutus Data: constructor 0 with batch_id and
    active (Aiken Bool: True is constructor 1, False is constructor 0)."""
    if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id < 1:
        raise ValueError(f"batch_id must be an integer of 1 or more, got {batch_id!r}")
    return cbor2.CBORTag(121, [batch_id, cbor2.CBORTag(122 if active else 121, [])])


# ── GovernanceCrossRefs datum ────────────────────────────────────────────────

def build_cross_refs_data(
    proposal_validator_hash: str,
    critique_validator_hash: str,
    proposal_mint_hash: str,
    critique_mint_hash: str,
    endorsement_validator_hash: str,
) -> cbor2.CBORTag:
    """Build the GovernanceCrossRefs datum as Plutus Data (cbor2.CBORTag).

    Constructor 0 with 5 fields (all ByteArray) in the order of the Aiken type
    (contracts/governance-suggestion/lib/governance_suggestion/types.ak):
    proposal_validator_hash, critique_validator_hash, proposal_mint_hash,
    critique_mint_hash, endorsement_validator_hash. The critique mint policy
    reads endorsement_validator_hash to find endorsement inputs, so the
    endorsement tokens can be minted and burned.

    Each value is the hash of an applied validator of this deployment: 28
    bytes and not all zero. Else ValueError.
    """
    fields = []
    for name, value in (
        ("proposal_validator_hash", proposal_validator_hash),
        ("critique_validator_hash", critique_validator_hash),
        ("proposal_mint_hash", proposal_mint_hash),
        ("critique_mint_hash", critique_mint_hash),
        ("endorsement_validator_hash", endorsement_validator_hash),
    ):
        raw = _hex_bytes(name, value or "", 28)
        if raw == bytes(28):
            raise ValueError(f"{name} is all zero")
        fields.append(raw)
    # Plutus Data constructor: Tag 121 + 0 = constr(0, fields)
    return cbor2.CBORTag(121, fields)


# ── Activity list head ───────────────────────────────────────────────────────

def build_activity_head_data(owner_vkh_hex: str) -> cbor2.CBORTag:
    """Build the datum of the head of the activity list as Plutus Data
    (cbor2.CBORTag).

    ProposerActivityDatum (contracts/governance-suggestion/lib/
    governance_suggestion/types.ak): constructor 0 with 5 fields agent_did,
    agent_credential, active_proposal_count, last_proposal_slot, next_did.
    The head has agent_did = b"", count 0, last_proposal_slot 0 and
    next_did = b"" (an empty list). The validators do not read its
    agent_credential; it is VerificationKey(owner), the deployer key
    (constructor 0 with the 28-byte key hash).

    The record step of deploy.py puts the head at the proposal_spend address
    with the `activity` record token (ACTIVITY_TOKEN_NAME). The first
    submission of each agent inserts the node of the agent into the list.
    """
    owner = _hex_bytes("owner_vkh_hex", owner_vkh_hex, 28)
    credential = cbor2.CBORTag(121, [owner])
    # Plutus Data constructor: Tag 121 + 0 = constr(0, fields)
    return cbor2.CBORTag(121, [b"", credential, 0, 0, b""])


# ── Deploy state ─────────────────────────────────────────────────────────────

#: The tx_hashes key of each step of deploy.py whose output 0 the clients
#: read ("<tx>#0"). treasury-<id> gives treasury_batch_<id>.
STEP_TX_KEYS = {
    "publish-holder-params": "holder_params_ref",
    "publish-holder-oracle": "holder_oracle_ref",
    "publish-holder-treasury": "holder_treasury_ref",
    **{f"publish-{name}": f"{name}_ref" for name in VALIDATOR_TITLES.values()},
    "cross-refs": "cross_refs_nft",
}

#: The step of deploy.py that creates the records, and the prefix of the
#: steps of deploy_params.py that replace the params record.
RECORDS_STEP = "records"
PARAMS_UPDATE_PREFIX = "params-update-"
TREASURY_PREFIX = "treasury-"


def treasury_batch_id(step_name: str) -> Optional[int]:
    """The batch id of a step "treasury-<id>", else None."""
    if step_name.startswith(TREASURY_PREFIX) and step_name[len(TREASURY_PREFIX):].isdigit():
        return int(step_name[len(TREASURY_PREFIX):])
    return None


def _tx_of(ref: Optional[str]) -> Optional[str]:
    return str(ref).partition("#")[0] if ref else None


def current_params_entry(steps: Mapping[str, Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The newest step record that names the params record: the record step
    of deploy.py or a replacement by deploy_params.py (the latest time; at
    the same time, the later step)."""
    entries = [
        (entry.get("time") or "", order, entry)
        for order, (name, entry) in enumerate(steps.items())
        if (name == RECORDS_STEP or name.startswith(PARAMS_UPDATE_PREFIX)) and entry.get("params")
    ]
    return max(entries, key=lambda item: item[:2])[2] if entries else None


def state_tx_hashes(steps: Mapping[str, Mapping[str, Any]]) -> dict:
    """tx_hashes of a deploy state from the recorded steps, with the key
    names of the earlier deploy states: the transaction of each reference
    script, treasury batch and the CrossRefs output (output 0 of each), and
    of the params record, the oracle record and the head of the activity
    list. The head moves with each first submission of an agent: the
    clients find it by its record token."""
    tx_hashes = {}
    for name, entry in steps.items():
        key = STEP_TX_KEYS.get(name)
        batch_id = treasury_batch_id(name)
        if batch_id is not None:
            key = f"treasury_batch_{batch_id}"
        if key and entry.get("tx"):
            tx_hashes[key] = entry["tx"]
    params = current_params_entry(steps)
    if params:
        tx_hashes["params_utxo"] = _tx_of(params["params"])
    records = steps.get(RECORDS_STEP) or {}
    for field, key in (("oracle", "oracle_utxo"), ("activity_head", "activity_head")):
        if records.get(field):
            tx_hashes[key] = _tx_of(records[field])
    return tx_hashes


def client_settings(state: Mapping[str, Any]) -> dict:
    """The environment values of the external clients for a deploy state:

    - "mcp-server": the GOV_* values of the mcp-server branch for the
      identity registry (packages/builder/src/vector/gov-deployment.ts): the
      five validator hashes, the record and refs token policies, the params,
      CrossRefs and treasury addresses and the five reference-script UTxOs.
      That branch finds the params record, the CrossRefs output and the head
      of the activity list by their tokens, so it has no *_UTXO values.
    - "agent-sdk-py": the VECTOR_MODULE6_* values of
      vector_agent.governance.deployment.ENV_VARS. The zero hashes of
      GovernanceConfig are the SDK defaults, so they are not set.

    A value that comes from a step is None until the state records the
    step."""
    steps = state.get("steps") or {}
    validators = state.get("validators") or {}
    holders = state.get("holders") or {}

    def script_hash(title: str) -> Optional[str]:
        return (validators.get(title) or {}).get("hash")

    def holder(role: str, key: str) -> Optional[str]:
        return (holders.get(role) or {}).get(key)

    def script_ref(title: str) -> Optional[str]:
        return (steps.get(f"publish-{VALIDATOR_TITLES[title]}") or {}).get("ref")

    params = (current_params_entry(steps) or {}).get("params")
    oracle = (steps.get(RECORDS_STEP) or {}).get("oracle")
    cross_refs = (steps.get("cross-refs") or {}).get("cross_refs")
    return {
        "mcp-server": {
            "GOV_PROPOSAL_SPEND_HASH": script_hash(PROPOSAL_SPEND),
            "GOV_PROPOSAL_MINT_HASH": script_hash(PROPOSAL_MINT),
            "GOV_CRITIQUE_SPEND_HASH": script_hash(CRITIQUE_SPEND),
            "GOV_CRITIQUE_MINT_HASH": script_hash(CRITIQUE_MINT),
            "GOV_ENDORSEMENT_SPEND_HASH": script_hash(ENDORSEMENT_SPEND),
            "GOV_RECORD_TOKEN_POLICY": state.get("record_token_policy"),
            "GOV_REFS_TOKEN_POLICY": state.get("refs_token_policy"),
            "GOV_PARAMS_ADDRESS": holder("params", "address"),
            "GOV_CROSSREFS_ADDRESS": state.get("cross_refs_address"),
            "GOV_TREASURY_ADDRESS": holder("treasury", "address"),
            "GOV_PROPOSAL_SPEND_REF": script_ref(PROPOSAL_SPEND),
            "GOV_PROPOSAL_MINT_REF": script_ref(PROPOSAL_MINT),
            "GOV_CRITIQUE_SPEND_REF": script_ref(CRITIQUE_SPEND),
            "GOV_CRITIQUE_MINT_REF": script_ref(CRITIQUE_MINT),
            "GOV_ENDORSEMENT_SPEND_REF": script_ref(ENDORSEMENT_SPEND),
        },
        "agent-sdk-py": {
            "VECTOR_MODULE6_IDENTITY_HASH": state.get("identity_hash"),
            "VECTOR_MODULE6_RECORD_TOKEN_POLICY": state.get("record_token_policy"),
            "VECTOR_MODULE6_REFS_TOKEN_POLICY": state.get("refs_token_policy"),
            "VECTOR_MODULE6_ORACLE_HOLDER_HASH": holder("oracle", "hash"),
            "VECTOR_MODULE6_PARAMS_HOLDER_HASH": holder("params", "hash"),
            "VECTOR_MODULE6_TREASURY_HOLDER_HASH": holder("treasury", "hash"),
            "VECTOR_MODULE6_PROPOSAL_SPEND_HASH": script_hash(PROPOSAL_SPEND),
            "VECTOR_MODULE6_PROPOSAL_SPEND_REF_UTXO": script_ref(PROPOSAL_SPEND),
            "VECTOR_MODULE6_PROPOSAL_MINT_REF_UTXO": script_ref(PROPOSAL_MINT),
            "VECTOR_MODULE6_CRITIQUE_SPEND_REF_UTXO": script_ref(CRITIQUE_SPEND),
            "VECTOR_MODULE6_CRITIQUE_MINT_REF_UTXO": script_ref(CRITIQUE_MINT),
            "VECTOR_MODULE6_ENDORSEMENT_SPEND_REF_UTXO": script_ref(ENDORSEMENT_SPEND),
            "VECTOR_MODULE6_PARAMS_UTXO": params,
            "VECTOR_MODULE6_ORACLE_UTXO": oracle,
            "VECTOR_MODULE6_CROSSREFS_UTXO": cross_refs,
        },
    }


# ── The scripts that read a deploy state ─────────────────────────────────────

def default_state_path(network: str) -> Path:
    """The state of scripts/deploy.py on `network`."""
    if network not in NETWORKS:
        raise DeployInputError(f"unknown network {network!r}; use one of {', '.join(NETWORKS)}")
    return STATE_ROOT / network / STATE_FILE


def add_state_arguments(parser) -> None:
    """--network and --state of the scripts that read a deploy state."""
    parser.add_argument("--network", choices=NETWORKS, default="mainnet",
                        help="the network of the deployment (default: mainnet)")
    parser.add_argument("--state", type=Path,
                        help="the deploy state (default: deploy/identity/state/<network>/deploy_state.json of "
                             "scripts/deploy.py; the previous deployment: --state wallets/deploy_state.json)")


def state_path(args) -> Path:
    """The --state path, else the state of scripts/deploy.py on --network."""
    return Path(args.state) if args.state is not None else default_state_path(args.network)


def read_state(path, network: str) -> dict:
    """A deploy state. The state of scripts/deploy.py (format
    vector-module-deploy/1) must be a Module-6 state of `network`. The state
    of the previous deployment (wallets/deploy_state.json) has no format and
    keeps its endpoints. Raises FileNotFoundError or DeployInputError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist. Run scripts/deploy.py (build, or run --submit) for "
                                "the deployment on the identity registry, or give --state.")
    state = json.loads(path.read_text())
    if state.get("format") == STATE_FORMAT:
        if state.get("module") != "module6":
            raise DeployInputError(f"{path} is a state of {state.get('module')}, not of Module-6")
        if state.get("network") != network:
            raise DeployInputError(f"{path} is a state of {state.get('network')}; give --network "
                                   f"{state.get('network')}")
    return state


def state_endpoints(state: Mapping[str, Any], network: str) -> dict:
    """ogmios_url, submit_url and explorer_url for a deploy state. The state
    of the previous deployment keeps them. The state of scripts/deploy.py has
    none: then VECTOR_OGMIOS_URL, VECTOR_SUBMIT_URL and VECTOR_EXPLORER_URL
    give them, else the preset of `network` (shared/python/vector_deploy.py,
    needs pycardano)."""
    import vector_deploy

    preset = vector_deploy.network_preset(network)
    return {
        "ogmios_url": state.get("ogmios_url") or preset.ogmios_url,
        "submit_url": state.get("submit_url") or preset.submit_url,
        "explorer_url": state.get("explorer_url") or os.environ.get("VECTOR_EXPLORER_URL") or EXPLORER_URLS[network],
    }


def redeploy_state_error(state: dict, proposal_utxos=()) -> Optional[str]:
    """Check a deploy state before scripts/redeploy_proposal.py changes the
    validators of the deployment.

    Returns the reason to refuse, or None. The head of the activity list
    holds the `activity` token of the one-shot record token policy and sits
    at the proposal_spend address of the deployment; the nodes hold the
    activity tokens of the proposal_mint policy. New proposal validators do
    not use that list, and the record token policy cannot mint a second
    head, so after a redeploy an agent could get a second activity record.
    The redeploy is refused when the deploy state names the head
    (tx_hashes["activity_head"], the record step) or when `proposal_utxos`
    (the outputs at the proposal_spend address of the deployment) hold the
    `activity` record token.
    """
    policy = state.get("record_token_policy") or ""
    head_tx = (state.get("tx_hashes") or {}).get("activity_head")
    head = find_record_utxo(proposal_utxos, policy, ACTIVITY_TOKEN_NAME) if policy else None
    if not head_tx and head is None:
        return None
    where = f"transaction {head_tx}" if head_tx else utxo_ref(head)
    return (f"the deployment has the head of the activity list ({where}). A redeploy "
            "would leave the activity list at the old proposal validator, and the "
            "one-shot record token policy cannot mint a new head, so agents could "
            "get a second activity record. Start a new deployment with "
            "scripts/deploy.py (it writes a new deploy state).")


# ── UTxO references ──────────────────────────────────────────────────────────

def format_utxo_ref(tx_id_hex: str, index: int) -> str:
    return f"{tx_id_hex}#{index}"


def utxo_ref(utxo) -> str:
    """Reference "<tx_id>#<index>" of a pycardano UTxO."""
    return format_utxo_ref(bytes(utxo.input.transaction_id).hex(), utxo.input.index)


def _multi_asset(amount):
    return None if isinstance(amount, int) else amount.multi_asset


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


def find_record_utxo(utxos: Iterable, policy_id_hex: str, token_name: bytes):
    """The UTxO that holds exactly one record token (policy_id_hex,
    token_name), or None."""
    for u in utxos:
        if record_token_quantity(u.output, policy_id_hex, token_name) == 1:
            return u
    return None
