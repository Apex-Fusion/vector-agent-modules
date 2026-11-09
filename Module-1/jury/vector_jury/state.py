"""The jury state on chain: juror and case outputs of the followed
deployments, the canonical case of a challenge (SPEC §11) and the eligibility
of a juror for a case (§6.4, §6.9).

An output counts only when it is at the exact jury address, holds exactly one
token of the jury policy with quantity 1 (``jrr_`` with a Juror datum, or
``cas_`` with a Case datum) and no other asset, and has an inline datum that
decodes. Other outputs at the address (anybody can send there) are skipped.

The canonical case needs the block of each opening transaction. ``Openings``
reads it from Koios (``asset_history``, then ``tx_info``) and caches it: an
opening never moves once it is in a block.
"""

import dataclasses
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pycardano import UTxO

from . import rules
from .chain import Chain, ref_key
from .deployment import Deployment
from .identity import find_identity_record, identity_problems
import cbor2

from .types import Case, DecodeError, Juror, data_from_json, decode_jury_datum


@dataclasses.dataclass(frozen=True, eq=False)
class JurorOutput:
    deployment: Deployment
    utxo: UTxO
    token: bytes
    datum: Juror

    @property
    def ref(self) -> str:
        return ref_key(self.utxo)

    @property
    def lovelace(self) -> int:
        return _coin(self.utxo)

    @property
    def agent(self) -> bytes:
        return bytes(self.datum.agent)

    @property
    def owner(self) -> bytes:
        return bytes(self.datum.owner)


@dataclasses.dataclass(frozen=True, eq=False)
class CaseOutput:
    deployment: Deployment
    utxo: UTxO
    token: bytes
    datum: Case

    @property
    def ref(self) -> str:
        return ref_key(self.utxo)

    @property
    def lovelace(self) -> int:
        return _coin(self.utxo)

    @property
    def challenge(self) -> bytes:
        return bytes(self.datum.challenge)

    @property
    def revealed(self) -> int:
        return rules.revealed_count(self.datum.tally)


def _coin(utxo: UTxO) -> int:
    amount = utxo.output.amount
    return amount if isinstance(amount, int) else amount.coin


def jury_token(utxo: UTxO, policy: bytes) -> Optional[bytes]:
    """The name of the only asset of ``utxo`` when it is one token of
    ``policy`` with quantity 1, else None."""
    amount = utxo.output.amount
    multi = getattr(amount, "multi_asset", None) or {}
    assets = [(bytes(pid.payload), bytes(name.payload), q)
              for pid, names in multi.items() for name, q in names.items() if q != 0]
    if len(assets) != 1:
        return None
    pid, name, quantity = assets[0]
    return name if pid == bytes(policy) and quantity == 1 else None


def classify(deployment: Deployment, utxo: UTxO):
    """A JurorOutput or a CaseOutput for a jury output, else None."""
    if str(utxo.output.address) != str(deployment.address) or utxo.output.script is not None:
        return None
    token = jury_token(utxo, deployment.script_hash)
    if token is None or utxo.output.datum is None:
        return None
    try:
        datum = decode_jury_datum(utxo.output.datum)
    except DecodeError:
        return None
    if rules.is_juror_token(token) and isinstance(datum, Juror):
        return JurorOutput(deployment, utxo, token, datum)
    if rules.is_case_token(token) and isinstance(datum, Case):
        return CaseOutput(deployment, utxo, token, datum)
    return None


def read_outputs(chain: Chain, deployment: Deployment) -> Tuple[List[JurorOutput], List[CaseOutput]]:
    """The juror and case outputs of one deployment (one Ogmios query)."""
    jurors, cases = [], []
    for utxo in chain.utxos_at(deployment.address):
        item = classify(deployment, utxo)
        if isinstance(item, JurorOutput):
            jurors.append(item)
        elif isinstance(item, CaseOutput):
            cases.append(item)
    return jurors, cases


def list_jurors(chain: Chain, deployment: Deployment) -> List[JurorOutput]:
    return read_outputs(chain, deployment)[0]


def list_cases(chain: Chain, deployment: Deployment) -> List[CaseOutput]:
    return read_outputs(chain, deployment)[1]


def find_juror(chain: Chain, deployment: Deployment, token: bytes) -> Optional[JurorOutput]:
    return next((j for j in list_jurors(chain, deployment) if j.token == bytes(token)), None)


def find_case(chain: Chain, deployment: Deployment, token: bytes) -> Optional[CaseOutput]:
    return next((c for c in list_cases(chain, deployment) if c.token == bytes(token)), None)


def find_token(chain: Chain, deployments: Sequence[Deployment], token: bytes):
    """The juror or case output that holds ``token`` in any of the
    deployments, or None."""
    for deployment in deployments:
        jurors, cases = read_outputs(chain, deployment)
        for item in (*jurors, *cases):
            if item.token == bytes(token):
                return item
    return None


def jurors_of_agent(chain: Chain, deployments: Sequence[Deployment], agent: bytes) -> List[JurorOutput]:
    return [j for d in deployments for j in list_jurors(chain, d) if j.agent == bytes(agent)]


def cases_of_challenge(chain: Chain, deployments: Sequence[Deployment], challenge: bytes) -> List[CaseOutput]:
    return [c for d in deployments for c in list_cases(chain, d) if c.challenge == bytes(challenge)]


# ── Canonical case (§11) ─────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Opening:
    """The opening transaction of a case and its block height."""

    tx_id: str
    block_height: int


class Openings:
    """The openings of cases, read from Koios and cached by (policy, token)."""

    def __init__(self, chain: Chain, cache: Optional[Dict[Tuple[bytes, bytes], Opening]] = None):
        self.chain = chain
        self.cache: Dict[Tuple[bytes, bytes], Opening] = dict(cache or {})

    def of_many(self, cases: Iterable[CaseOutput]) -> Dict[bytes, Optional[Opening]]:
        """The opening of each case by case token (None while Koios does not
        show it)."""
        result: Dict[bytes, Optional[Opening]] = {}
        unknown: Dict[str, List[CaseOutput]] = {}
        for case in cases:
            key = (case.deployment.script_hash, case.token)
            if key in self.cache:
                result[case.token] = self.cache[key]
                continue
            result[case.token] = None
            mints = self.chain.asset_mint_txs(case.deployment.script_hash, case.token)
            if mints:
                unknown.setdefault(mints[0], []).append(case)
        if unknown:
            heights = self.chain.tx_block_heights(list(unknown))
            for tx_id, waiting in unknown.items():
                height = heights.get(tx_id)
                if height is None:
                    continue
                for case in waiting:
                    opening = Opening(tx_id, height)
                    self.cache[(case.deployment.script_hash, case.token)] = opening
                    result[case.token] = opening
        return result

    def of(self, case: CaseOutput) -> Optional[Opening]:
        return self.of_many([case])[case.token]


def canonical_case(cases: Sequence[CaseOutput], openings: Dict[bytes, Optional[Opening]]) -> Optional[CaseOutput]:
    """The case whose opening is first on chain: the lowest block height,
    then the lowest case token name (bytewise). A case whose opening Koios
    does not show yet is in a later block than every case that it shows, so
    it never wins against a known case. None when no opening is known."""
    known = [(openings[c.token].block_height, c.token, i) for i, c in enumerate(cases)
             if openings.get(c.token) is not None]
    if not known:
        return None
    return cases[min(known)[2]]


def canonical_cases(chain: Chain, deployments: Sequence[Deployment],
                    openings: Optional[Openings] = None) -> Dict[bytes, CaseOutput]:
    """The canonical case of each challenge (by challenge token) across the
    followed deployments. Challenges whose cases Koios does not show yet are
    left out."""
    openings = openings or Openings(chain)
    by_challenge: Dict[bytes, List[CaseOutput]] = {}
    for deployment in deployments:
        for case in list_cases(chain, deployment):
            by_challenge.setdefault(case.challenge, []).append(case)
    known = openings.of_many([c for group in by_challenge.values() for c in group])
    out = {}
    for challenge, group in by_challenge.items():
        chosen = canonical_case(group, known)
        if chosen is not None:
            out[challenge] = chosen
    return out


# ── Canonical case after amendment 2 (§11.2) ────────────────────────────────


@dataclasses.dataclass(frozen=True, eq=False)
class CaseOpening:
    """A case as its opening transaction made it (also when it is burned)."""

    deployment: Deployment
    token: bytes
    tx_id: str
    block_height: int
    datum: Case


def case_openings(chain: Chain, deployment: Deployment, *,
                  cache: Optional[Dict[Tuple[bytes, bytes], CaseOpening]] = None) -> List[CaseOpening]:
    """Every case that the deployment ever opened (Koios: the ``cas_`` assets
    of policy J, their minting transaction and the inline datum of the case
    output in it), burned cases included. ``cache`` keeps the openings (an
    opening never changes once it is in a block)."""
    cache = cache if cache is not None else {}
    policy = deployment.script_hash.hex()
    names = [bytes.fromhex(a["asset_name"]) for a in chain.koios.policy_asset_list(policy)
             if rules.is_case_token(bytes.fromhex(a.get("asset_name") or ""))]
    missing = {}
    for name in names:
        if (deployment.script_hash, name) not in cache:
            mints = chain.asset_mint_txs(deployment.script_hash, name)
            if mints:
                missing.setdefault(mints[0], []).append(name)
    if missing:
        for info in chain.koios.tx_info(list(missing), full=True):
            tx_id, height = info.get("tx_hash"), info.get("block_height")
            if tx_id not in missing or height is None:
                continue
            for output in info.get("outputs") or []:
                assets = output.get("asset_list") or []
                held = [bytes.fromhex(a["asset_name"]) for a in assets if a.get("policy_id") == policy]
                inline = output.get("inline_datum") or {}
                for name in missing[tx_id]:
                    if held != [name] or not (inline.get("bytes") or inline.get("value")):
                        continue
                    try:
                        raw = bytes.fromhex(inline["bytes"]) if inline.get("bytes") else \
                            cbor2.dumps(data_from_json(inline["value"]))
                        datum = decode_jury_datum(raw)
                    except (DecodeError, ValueError, KeyError):
                        continue
                    if isinstance(datum, Case):
                        cache[(deployment.script_hash, name)] = CaseOpening(deployment, name, tx_id, int(height), datum)
    return [cache[(deployment.script_hash, n)] for n in names if (deployment.script_hash, n) in cache]


@dataclasses.dataclass(frozen=True, eq=False)
class Canonical:
    deployment: Deployment
    opening: CaseOpening
    deep_enough: bool
    live: Optional[CaseOutput]


def deployment_for(deployments: Sequence[Deployment], challenged_at: int) -> Optional[Deployment]:
    """The deployment that is current at ``challenged_at`` (§12.1), or None
    (then the relay resolves with basis ``no-jury``)."""
    return next((d for d in deployments if d.current_for(challenged_at)), None)


def case_counts(chain: Chain, m1, datum: Case, *, genuine_cache=None) -> bool:
    """§11.2 rule 2 for one case datum: its ``challenge_out`` is genuine and
    holds the case's ``challenge`` token."""
    from .module1 import genuine_token

    out = datum.challenge_out
    ref = f"{bytes(out.transaction_id).hex()}#{out.output_index}"
    return genuine_token(chain, m1, ref, cache=genuine_cache) == bytes(datum.challenge)


def dispute_cases(chain: Chain, deployments: Sequence[Deployment], claim_token: bytes, challenged_at: int, m1, *,
                  cache=None, genuine_cache=None) -> List[CaseOpening]:
    """Every case of a dispute under §11.2 rules 1-2, at any depth: opened
    in the deployment current at ``challenged_at``, with this claim token,
    and counted by ``case_counts``. Ordered by (block height, case token)."""
    deployment = deployment_for(deployments, challenged_at)
    if deployment is None:
        return []
    genuine_cache = genuine_cache if genuine_cache is not None else {}
    found = [o for o in case_openings(chain, deployment, cache=cache)
             if bytes(o.datum.claim) == bytes(claim_token) and case_counts(chain, m1, o.datum,
                                                                          genuine_cache=genuine_cache)]
    return sorted(found, key=lambda o: (o.block_height, o.token))


def canonical_for(chain: Chain, deployments: Sequence[Deployment], claim_token: bytes, challenged_at: int, m1, *,
                  min_depth: int = 10, cache=None, genuine_cache=None) -> Optional[Canonical]:
    """The canonical case of a dispute (§11.2): the first of
    ``dispute_cases`` (block, then case token name). ``deep_enough`` is True
    when its opening is at least ``min_depth`` blocks deep; only then is the
    case canonical. ``live`` is its current output, or None when it is spent
    or burned."""
    cases = dispute_cases(chain, deployments, claim_token, challenged_at, m1, cache=cache,
                          genuine_cache=genuine_cache)
    if not cases:
        return None
    first = cases[0]
    deep = chain.block_height() - first.block_height >= min_depth
    return Canonical(first.deployment, first, deep, find_case(chain, first.deployment, first.token))


# ── Eligibility (§6.4, §6.9) ─────────────────────────────────────────────────


def eligibility(juror: JurorOutput, case: CaseOutput, at_ms: int, *, chain: Optional[Chain] = None) -> List[str]:
    """The reasons why ``juror`` cannot commit in ``case`` at ``at_ms`` (empty
    when it can). With ``chain``, the identity record is checked too."""
    problems = []
    if juror.deployment.script_hash != case.deployment.script_hash:
        problems.append("the juror and the case are of different deployments")
    problems += rules.commit_problems(juror.datum, juror.lovelace, juror.token, case.datum, case.token,
                                      case.deployment.params, at_ms, at_ms)
    if chain is not None:
        try:
            record = find_identity_record(chain, case.deployment.params.identity_hash, juror.agent)
        except Exception as error:  # noqa: BLE001 - RecordNotFoundError and decode errors
            problems.append(str(error))
        else:
            problems += identity_problems(record, juror.owner, at_ms, at_ms)
    return problems
