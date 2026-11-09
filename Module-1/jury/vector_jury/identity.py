"""The identity check of the jury (SPEC §5.8), client side.

The jury reads identity records with the vendored model
``simulation.vector_identity`` (the same copy as Module-1). A record is an
output at the identity address (``Script(identity_hash)``, no stake) with
exactly one identity token, named by the agent id, and an inline datum.
"""

from typing import Dict, List, Union

from . import _paths  # noqa: F401
from simulation import vector_identity as vi

#: The longest validity range of a transaction with an identity check.
MAX_RANGE_MS = vi.MAX_RANGE_MS

RecordNotFoundError = vi.RecordNotFoundError
IdentityRecord = vi.IdentityRecord


def identity_records(chain, identity_hash: bytes) -> Dict[bytes, vi.IdentityRecord]:
    """The authentic records at the identity address, by agent id."""
    from .deployment import script_address

    records: Dict[bytes, vi.IdentityRecord] = {}
    for utxo in chain.utxos_at(script_address(identity_hash)):
        try:
            record = vi.record_from_utxo(utxo, identity_hash)
        except vi.IdentityDecodeError:
            continue
        if record is not None and record.agent_id not in records:
            records[record.agent_id] = record
    return records


def find_identity_record(chain, identity_hash: bytes, agent: Union[bytes, str]) -> vi.IdentityRecord:
    """The record of ``agent`` (32 bytes, 64 hex or a DID). Raises
    ``RecordNotFoundError``."""
    agent_id = vi.normalize_agent_id(agent)
    record = identity_records(chain, identity_hash).get(agent_id)
    if record is None:
        raise vi.RecordNotFoundError(agent_id)
    return record


def identity_problems(record: vi.IdentityRecord, owner: bytes, lo: int, hi: int) -> List[str]:
    """The steps of the identity check (§5.8, without the signature) that fail
    for the record with owner ``owner`` over ``[lo, hi]``."""
    problems = []
    if lo > hi or hi - lo > MAX_RANGE_MS:
        problems.append(f"the validity range [{lo}, {hi}] must have lo <= hi and hi - lo <= {MAX_RANGE_MS}")
    datum = record.datum
    if not vi.independent(datum):
        problems.append(f"{record.did} is a subagent; a juror must be independent")
    if not vi.declared_credential(datum, owner):
        problems.append(f"the owner key {bytes(owner).hex()} is neither the controller of {record.did} "
                        "nor a listed key with the transaction purpose")
    if vi.independent(datum):
        status = vi.effective(datum, lambda _agent: None, lo, hi)
        if status != vi.Effective.ACTIVE:
            problems.append(f"{record.did} is {status.value} over the validity range")
    return problems
