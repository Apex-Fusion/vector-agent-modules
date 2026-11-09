"""Module-1 for the jury tools: read challenges and claims (SPEC §5.7), and
build the Module-1 transactions of the relay and of the parties with the
builders of ``simulation.tx_builder``: OracleResolve (with the resolution
document, §10.5), ApplyVerdict, TimeoutResolve, SubmitClaim and
OpenChallenge (oracle mode).

The Module-1 deployment comes from ``Module-1/deploy/identity/mainnet/
manifest.json`` (``simulation/config.py`` points mainnet to an older
deployment, so this module never reads that default).

The simulation builders submit by themselves, and ``ensure_collateral`` can
send a split transaction. ``submit_hook`` replaces ``submit_tx`` of
``simulation.tx_builder`` and ``simulation.chain`` while a builder runs: it
evaluates every transaction with Ogmios, checks its size, and submits only
with ``submit=True`` (the transaction id comes from the node's answer). The
wrappers refuse a fee payer without a lovelace-only output of at least
5 AP3X, so no collateral split is attempted.

``simulation.config`` takes its network from ``APEX_NETWORK`` when it is
first imported, and mainnet needs ``APEX_NETWORK_CONFIRM=yes`` (its safety
rail). ``simulation_modules`` sets ``APEX_NETWORK`` to the network of the
Module-1 deployment and reports the missing confirmation as an error.
"""

import contextlib
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from pycardano import Address, Network, ScriptHash, UTxO

from . import _paths
from . import documents
from .chain import Chain, ref_key
import cbor2

from .types import (ChallengeDatum, ClaimDatum, DecodeError, data_from_json, decode_challenge_datum,
                    decode_claim_datum, same_data)

import vector_deploy as vd

#: The Module-1 deployment of the identity registry on mainnet.
MODULE1_MANIFEST = _paths.MODULE1_DIR / "deploy" / "identity" / "mainnet" / "manifest.json"
#: The largest transaction (bytes).
MAX_TX_SIZE = 16_384
MIN_COLLATERAL = 5_000_000


class Module1Error(RuntimeError):
    """A Module-1 read or build failed, or the environment is not set up."""


@dataclasses.dataclass(frozen=True)
class Module1:
    """The values of a Module-1 deployment manifest that the jury uses."""

    path: Path
    manifest: Dict[str, Any]
    network: str
    claim_hash: bytes
    challenge_hash: bytes
    oracle_vkh: bytes

    @classmethod
    def load(cls, path: Union[str, Path, None] = None) -> "Module1":
        path = Path(path) if path is not None else MODULE1_MANIFEST
        try:
            manifest = json.loads(path.read_text())
            hashes = manifest["hashes"]
            return cls(path, manifest, manifest.get("network", "mainnet"), bytes.fromhex(hashes["claim"]),
                       bytes.fromhex(hashes["challenge"]), bytes.fromhex(manifest["module1"]["oracle_vkh"]))
        except (OSError, ValueError, KeyError) as error:
            raise Module1Error(f"cannot read the Module-1 manifest {path}: {error}") from None

    @property
    def claim_address(self) -> Address:
        return Address(ScriptHash(self.claim_hash), network=Network.MAINNET)

    @property
    def challenge_address(self) -> Address:
        return Address(ScriptHash(self.challenge_hash), network=Network.MAINNET)

    @property
    def resolution_deadline(self) -> int:
        return int(self.manifest["protocol_params"]["resolution_deadline"])


# ── Reads ────────────────────────────────────────────────────────────────────


def _single_token(utxo: UTxO, policy: bytes) -> Optional[bytes]:
    multi = getattr(utxo.output.amount, "multi_asset", None) or {}
    found = [(bytes(n.payload), q) for pid, names in multi.items() if bytes(pid.payload) == bytes(policy)
             for n, q in names.items() if q != 0]
    return found[0][0] if len(found) == 1 and found[0][1] == 1 else None


@dataclasses.dataclass(frozen=True, eq=False)
class ChallengeOutput:
    utxo: UTxO
    token: bytes
    datum: ChallengeDatum

    @property
    def ref(self) -> str:
        return ref_key(self.utxo)

    @property
    def deadline(self) -> int:
        return self.datum.deadline


@dataclasses.dataclass(frozen=True, eq=False)
class ClaimOutput:
    utxo: UTxO
    token: bytes
    datum: ClaimDatum

    @property
    def ref(self) -> str:
        return ref_key(self.utxo)


def list_challenges(chain: Chain, m1: Module1, *, pending_oracle: bool = True) -> List[ChallengeOutput]:
    """The challenge outputs (one challenge token, an 11-field datum), only
    those in ``PendingOracle`` by default, earliest deadline first."""
    out = []
    for utxo in chain.utxos_by_payment_credential(m1.challenge_hash):
        token = _single_token(utxo, m1.challenge_hash)
        if token is None or utxo.output.datum is None:
            continue
        try:
            datum = decode_challenge_datum(utxo.output.datum)
        except DecodeError:
            continue
        if pending_oracle and not datum.pending_oracle:
            continue
        out.append(ChallengeOutput(utxo, token, datum))
    return sorted(out, key=lambda c: (c.deadline, c.token))


def list_claims(chain: Chain, m1: Module1) -> List[ClaimOutput]:
    out = []
    for utxo in chain.utxos_by_payment_credential(m1.claim_hash):
        token = _single_token(utxo, m1.claim_hash)
        if token is None or utxo.output.datum is None:
            continue
        try:
            out.append(ClaimOutput(utxo, token, decode_claim_datum(utxo.output.datum)))
        except DecodeError:
            continue
    return out


def find_claim(chain: Chain, m1: Module1, claim_token: bytes) -> Optional[ClaimOutput]:
    return next((c for c in list_claims(chain, m1) if c.token == bytes(claim_token)), None)


def find_challenge(chain: Chain, m1: Module1, token: bytes) -> Optional[ChallengeOutput]:
    return next((c for c in list_challenges(chain, m1, pending_oracle=False) if c.token == bytes(token)), None)


# ── Genuine challenges (SPEC §11.1) ──────────────────────────────────────────


def _assets(entries, policy: bytes) -> Dict[bytes, int]:
    out: Dict[bytes, int] = {}
    for entry in entries or []:
        if entry.get("policy_id") == bytes(policy).hex():
            name = bytes.fromhex(entry.get("asset_name") or "")
            out[name] = out.get(name, 0) + int(entry.get("quantity", 0))
    return out


def _redeemer_constructor(info: dict, tx_hash: str, tx_index: int) -> Optional[tuple]:
    """(constructor, field count) of the spend redeemer of an input, from the
    ``plutus_contracts`` of Koios ``tx_info``."""
    for contract in info.get("plutus_contracts") or []:
        spends = contract.get("spends_input") or {}
        if spends.get("tx_hash") == tx_hash and int(spends.get("tx_index", -1)) == tx_index:
            value = (((contract.get("input") or {}).get("redeemer") or {}).get("datum") or {}).get("value") or {}
            if "constructor" in value:
                return int(value["constructor"]), len(value.get("fields") or [])
    return None


def _koios_datum(entry: dict) -> Optional[ChallengeDatum]:
    """The ChallengeDatum of a Koios ``tx_info`` input or output (inline
    datum bytes, else its JSON form), or None."""
    inline = entry.get("inline_datum") or {}
    try:
        if inline.get("bytes"):
            return decode_challenge_datum(bytes.fromhex(inline["bytes"]))
        if inline.get("value"):
            return decode_challenge_datum(cbor2.dumps(data_from_json(inline["value"])))
    except (DecodeError, ValueError, KeyError):
        return None
    return None


def _same_except_evidence(later: ChallengeDatum, earlier: ChallengeDatum) -> bool:
    """Every datum field is kept except evidence_hash and evidence_uri."""
    moved = dataclasses.replace(later, evidence_hash=earlier.evidence_hash, evidence_uri=earlier.evidence_uri)
    return same_data(moved.data(), earlier.data())


def genuine_token(chain: Chain, m1: Module1, ref: str, *,
                  cache: Optional[Dict[str, Optional[bytes]]] = None) -> Optional[bytes]:
    """The challenge token of a genuine challenge output (§11.1), or None.

    Rule 1: the transaction that made the output minted exactly one
    ``m1_challenge`` token (one entry, +1) and the output holds it. Rule 2
    (amendments 5 and 6): the transaction spent a genuine challenge output
    with ``SubmitEvidence`` (Constr 1, 2 fields), minted or burned no
    ``m1_challenge`` token, and the output is its first output at the
    challenge address, holds the same token, keeps every datum field of the
    spent output except ``evidence_hash`` and ``evidence_uri``, and holds
    exactly its ``stake_amount``. The walk follows any number of steps (Koios
    ``tx_info``)."""
    cache = cache if cache is not None else {}
    path: List[str] = []
    current, result = ref, None
    challenge_cred = m1.challenge_hash.hex()
    while True:
        if current in cache:
            result = cache[current]
            break
        path.append(current)
        tx_id, _, index_text = current.partition("#")
        index = int(index_text)
        info = next((r for r in chain.koios.tx_info([tx_id], full=True) if r.get("tx_hash") == tx_id), None)
        if info is None:
            # Koios does not show the transaction yet: the answer is unknown, so
            # nothing is cached and the next loop asks again.
            return None
        outputs = info.get("outputs") or []
        output = next((o for o in outputs if int(o.get("tx_index", -1)) == index), None)
        if output is None or (output.get("payment_addr") or {}).get("cred") != challenge_cred:
            break
        held = _assets(output.get("asset_list"), m1.challenge_hash)
        if len(held) != 1 or list(held.values()) != [1]:
            break
        token = next(iter(held))
        minted = _assets(info.get("assets_minted"), m1.challenge_hash)
        if minted:
            result = token if minted == {token: 1} else None
            break
        first = min(int(o.get("tx_index", -1)) for o in outputs
                    if (o.get("payment_addr") or {}).get("cred") == challenge_cred)
        datum = _koios_datum(output)
        if index != first or datum is None or int(output.get("value", -1)) != datum.stake_amount:
            break
        spent = next((s for s in info.get("inputs") or []
                      if (s.get("payment_addr") or {}).get("cred") == challenge_cred
                      and _assets(s.get("asset_list"), m1.challenge_hash) == {token: 1}), None)
        if spent is None or _redeemer_constructor(info, spent["tx_hash"], int(spent["tx_index"])) != (1, 2):
            break
        earlier = _koios_datum(spent)
        if earlier is None or not _same_except_evidence(datum, earlier):
            break
        current = f"{spent['tx_hash']}#{spent['tx_index']}"
    for visited in path:
        cache[visited] = result
    return result


def is_genuine(chain: Chain, m1: Module1, ref: str, *, cache: Optional[Dict[str, Optional[bytes]]] = None) -> bool:
    """True when the challenge output ``ref`` is genuine (``genuine_token``)."""
    return genuine_token(chain, m1, ref, cache=cache) is not None


def genuine_challenges(chain: Chain, m1: Module1, *, pending_oracle: bool = True,
                       cache: Optional[Dict[str, Optional[bytes]]] = None) -> List["ChallengeOutput"]:
    """The genuine challenge outputs (§11.1), ``PendingOracle`` by default."""
    cache = cache if cache is not None else {}
    return [c for c in list_challenges(chain, m1, pending_oracle=pending_oracle)
            if genuine_token(chain, m1, c.ref, cache=cache) == c.token]


def stake_problems(claim: Optional[UTxO], challenge: Optional[UTxO] = None) -> List[str]:
    """The stake checks before a Module-1 transaction (§12.2): the lovelace of
    the claim output and of the challenge output equal their
    ``stake_amount``."""
    problems = []
    for what, utxo, decode in (("claim", claim, decode_claim_datum), ("challenge", challenge, decode_challenge_datum)):
        if utxo is None:
            continue
        datum = decode(utxo.output.datum)
        if vd.coin_of(utxo) != datum.stake_amount:
            problems.append(f"the {what} output {ref_key(utxo)} holds {vd.coin_of(utxo)} lovelace, not its "
                            f"stake_amount {datum.stake_amount}")
    return problems


def _check_stakes(m1: Module1, claim_ref: str, challenge_ref: Optional[str] = None) -> None:
    sim_chain, _ = simulation_modules(m1.network)

    def utxo(ref):
        tx_id, _, index = ref.partition("#")
        return sim_chain.resolve_utxo(tx_id, int(index))

    problems = stake_problems(utxo(claim_ref), utxo(challenge_ref) if challenge_ref else None)
    if problems:
        raise Module1Error("; ".join(problems))


def challenge_of_ref(chain: Chain, m1: Module1, ref: str) -> ChallengeOutput:
    utxo = chain.require_utxo(ref, "challenge output")
    token = _single_token(utxo, m1.challenge_hash)
    if token is None or utxo.output.datum is None:
        raise Module1Error(f"{ref} is not a Module-1 challenge output")
    return ChallengeOutput(utxo, token, decode_challenge_datum(utxo.output.datum))


# ── Simulation builders ──────────────────────────────────────────────────────


def simulation_modules(network: str):
    """``(simulation.chain, simulation.tx_builder)`` configured for
    ``network``. Raises Module1Error when ``simulation.config`` is (or would
    be) set up for another network, or when mainnet lacks
    APEX_NETWORK_CONFIRM=yes."""
    loaded = sys.modules.get("simulation.config")
    if loaded is not None:
        if loaded.APEX_NETWORK != network:
            raise Module1Error(f"simulation.config is set up for {loaded.APEX_NETWORK}, the Module-1 deployment "
                               f"is on {network}")
    else:
        current = os.environ.get("APEX_NETWORK", "").strip().lower()
        if current and current != network:
            raise Module1Error(f"APEX_NETWORK={current}, but the Module-1 deployment is on {network}")
        os.environ["APEX_NETWORK"] = network
        try:
            import simulation.config  # noqa: F401
        except RuntimeError as error:
            raise Module1Error(f"{error} (the Module-1 builders use simulation/config.py)") from None
    from simulation import chain as sim_chain
    from simulation import tx_builder as sim_tx
    return sim_chain, sim_tx


@dataclasses.dataclass
class SentTx:
    """A transaction that a simulation builder handed to ``submit_tx``."""

    tx_id: str
    size: int
    submitted: bool
    evaluation: Any


@contextlib.contextmanager
def submit_hook(submit: bool, *, network: str, log: Callable[[str], None] = lambda _m: None,
                save_tx: Optional[Union[str, Path]] = None):
    """Replace ``submit_tx`` of the simulation modules while the block runs.
    Each transaction is evaluated with Ogmios (a failure with redeemers stops
    it), its size is checked, and it is submitted only with ``submit=True``.
    With ``save_tx`` the CBOR (hex) of each checked transaction is written to
    that file (the last one stays), for a decode check before a submit.
    Yields the list of ``SentTx``."""
    sim_chain, sim_tx = simulation_modules(network)
    sent: List[SentTx] = []

    def hook(tx_bytes):
        data = tx_bytes if isinstance(tx_bytes, bytes) else bytes.fromhex(tx_bytes)
        tx_id = vd.transaction_id(data)
        from pycardano import Transaction

        has_scripts = bool(Transaction.from_cbor(data).transaction_witness_set.redeemer)
        evaluation = None
        if has_scripts:
            try:
                evaluation = sim_chain.ogmios_rpc("evaluateTransaction", {"transaction": {"cbor": data.hex()}})
            except Exception as error:  # noqa: BLE001 - Ogmios errors
                raise Module1Error(f"Ogmios evaluation of {tx_id} failed, nothing sent: {error}") from None
        if len(data) > MAX_TX_SIZE:
            raise Module1Error(f"{tx_id} has {len(data)} bytes, more than {MAX_TX_SIZE}")
        if save_tx is not None:
            Path(save_tx).write_text(data.hex() + "\n")
        if not submit:
            log(f"dry run: {tx_id} ({len(data)} bytes) evaluated, not sent")
            sent.append(SentTx(tx_id, len(data), False, evaluation))
            return tx_id
        answer = sim_chain.ogmios_rpc("submitTransaction", {"transaction": {"cbor": data.hex()}})
        node_id = answer["transaction"]["id"]
        log(f"submitted {node_id}")
        sent.append(SentTx(node_id, len(data), True, evaluation))
        return node_id

    saved = (sim_tx.submit_tx, sim_chain.submit_tx)
    sim_tx.submit_tx = sim_chain.submit_tx = hook
    try:
        yield sent
    finally:
        sim_tx.submit_tx, sim_chain.submit_tx = saved


@dataclasses.dataclass
class Wallet:
    """A key wallet for the simulation builders (they sign with ``skey``)."""

    skey: Any
    vkey: Any
    address: Address

    @classmethod
    def from_signing_key(cls, skey) -> "Wallet":
        from pycardano import PaymentVerificationKey

        vkey = PaymentVerificationKey.from_signing_key(skey)
        return cls(skey, vkey, Address(vkey.hash(), network=Network.MAINNET))


def _context_and_deployment(m1: Module1):
    sim_chain, sim_tx = simulation_modules(m1.network)
    return sim_chain.OgmiosContext(), sim_tx.DeploymentState(m1.manifest), sim_tx


def _require_collateral(context, wallet: Wallet) -> None:
    for utxo in context.utxos(str(wallet.address)):
        if vd.lovelace_only(utxo) and vd.coin_of(utxo) >= MIN_COLLATERAL:
            return
    raise Module1Error(f"{wallet.address} has no lovelace-only output of at least 5 AP3X for the collateral; "
                       "send one first (no collateral split is made)")


def _run(m1: Module1, wallet: Wallet, submit: bool, log, build: Callable[[Any, Any, Any], dict],
         save_tx: Optional[Union[str, Path]] = None) -> dict:
    context, deployment, sim_tx = _context_and_deployment(m1)
    _require_collateral(context, wallet)
    with submit_hook(submit, network=m1.network, log=log, save_tx=save_tx) as sent:
        result = build(context, deployment, sim_tx)
    return {**result, "submitted": submit, "transactions": [dataclasses.asdict(s) for s in sent]}


def oracle_resolve(m1: Module1, wallet: Wallet, challenge_ref: str, claim_ref: str, verdict: str, *,
                   resolution: Optional[bytes] = None, reasoning_hash: Optional[bytes] = None,
                   oracle_skey=None, submit: bool = False, log=lambda _m: None, save_tx=None) -> dict:
    """OracleResolve with ``verdict``. The resolution document (§10.5) goes in
    the metadata (label 22084, type ``resolution``) and its hash is the
    ``reasoning_hash``."""
    if resolution is not None:
        reasoning_hash = documents.doc_hash(resolution)
        aux = documents.auxiliary_data(resolution, "resolution", "application/json")
    elif reasoning_hash is None:
        raise Module1Error("give the resolution document or the reasoning hash")
    else:
        aux = None
    _check_stakes(m1, claim_ref, challenge_ref)
    return _run(m1, wallet, submit, log, lambda ctx, dep, tx: tx.build_oracle_resolve(
        ctx, dep, wallet.skey, wallet.vkey, wallet.address, challenge_ref, claim_ref, verdict=verdict,
        reasoning_hash=bytes(reasoning_hash), oracle_skey=oracle_skey, auxiliary_data=aux), save_tx)


def apply_verdict(m1: Module1, wallet: Wallet, verdict_ref: str, *, submit: bool = False,
                  log=lambda _m: None, save_tx=None) -> dict:
    """ApplyVerdict of an identity verdict output (anybody)."""
    return _run(m1, wallet, submit, log, lambda ctx, dep, tx: tx.build_apply_verdict(
        ctx, dep, wallet.skey, wallet.vkey, wallet.address, verdict_ref), save_tx)


def timeout_resolve(m1: Module1, wallet: Wallet, challenge_ref: str, claim_ref: str, *, submit: bool = False,
                    log=lambda _m: None, save_tx=None) -> dict:
    """TimeoutResolve after the deadline: each side gets its stake back."""
    _check_stakes(m1, claim_ref, challenge_ref)
    return _run(m1, wallet, submit, log, lambda ctx, dep, tx: tx.build_timeout_resolve(
        ctx, dep, wallet.skey, wallet.vkey, wallet.address, challenge_ref, claim_ref), save_tx)


def submit_claim(m1: Module1, wallet: Wallet, claimer: Union[bytes, str], stake: int, *, claim_hash: bytes,
                 storage_uri: Union[bytes, str], claim_type: bytes = b"service-quality",
                 challenge_window_ms: Optional[int] = None, submit: bool = False, log=lambda _m: None,
                 save_tx=None) -> dict:
    """SubmitClaim: the claim document hash and URI (``vector-tx:`` of the
    publish transaction, or another URI)."""
    claimer_hex = claimer.hex() if isinstance(claimer, bytes) else claimer
    uri = storage_uri.encode("utf-8") if isinstance(storage_uri, str) else bytes(storage_uri)
    return _run(m1, wallet, submit, log, lambda ctx, dep, tx: tx.build_submit_claim(
        ctx, dep, wallet.skey, wallet.vkey, wallet.address, claimer_hex, stake,
        challenge_window_ms=challenge_window_ms, claim_hash=bytes(claim_hash), claim_type=bytes(claim_type),
        storage_uri=uri), save_tx)


def open_challenge(m1: Module1, wallet: Wallet, auditor: Union[bytes, str], claim_ref: str, *, stake: int,
                   evidence_hash: bytes, evidence_uri: Union[bytes, str],
                   resolution_deadline_ms: Optional[int] = None, submit: bool = False,
                   log=lambda _m: None, save_tx=None) -> dict:
    """OpenChallenge in oracle mode (state PendingOracle, no juror list)."""
    auditor_hex = auditor.hex() if isinstance(auditor, bytes) else auditor
    uri = evidence_uri.encode("utf-8") if isinstance(evidence_uri, str) else bytes(evidence_uri)
    deadline = m1.resolution_deadline if resolution_deadline_ms is None else int(resolution_deadline_ms)
    _check_stakes(m1, claim_ref)
    return _run(m1, wallet, submit, log, lambda ctx, dep, tx: tx.build_open_challenge(
        ctx, dep, wallet.skey, wallet.vkey, wallet.address, auditor_hex, claim_ref, [],
        stake_amount=stake, evidence_hash=bytes(evidence_hash), evidence_uri=uri,
        resolution_deadline_ms=deadline, oracle_active=True), save_tx)


def resolution_document(challenge: ChallengeOutput, verdict: str, basis: str, *, jury: Optional[dict] = None) -> bytes:
    """The resolution document of SPEC §10.5 (compact JSON, UTF-8).
    ``jury`` is the ``jury`` object (None for ``no-time``)."""
    if basis not in ("jury", "no-time", "no-result", "no-jury"):
        raise ValueError("basis must be jury, no-result, no-time or no-jury")
    doc = {
        "type": "vector-jury-resolution/1",
        "challenge_out": challenge.ref,
        "challenge": challenge.token.hex(),
        "claim": challenge.datum.claim_token.hex(),
        "verdict": verdict,
        "basis": basis,
        "jury": jury,
    }
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
