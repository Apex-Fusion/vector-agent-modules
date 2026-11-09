"""The relay of the Foundation (SPEC §12): it opens a case for each genuine
Module-1 challenge, signs ``OracleResolve`` with the result of the canonical
case, applies ``AuditorWins`` verdicts, and cleans up (finalize, slash, claim,
close, TimeoutResolve, verdict reclaims).

One loop has three steps:

1. ``observe`` reads the chain once: the genuine challenges in
   ``PendingOracle`` (``module1.genuine_challenges``) with the canonical case
   of each dispute (``state.canonical_for``, SPEC §11.2), the live juror and
   case outputs of the followed deployments, and the verdict outputs whose
   ``deposit_to`` is the relay. The reference juror reads the same ``World``.
2. ``decide`` turns the world into actions (``Action``) and notes (decisions
   without a transaction). It is a pure function of the world, the time and
   the relay's transaction log, so the tests run it with fakes.
3. ``Relay.execute`` builds each action, checks it, writes the log entry
   BEFORE it submits, submits, and waits until the chain shows the new
   output. All transactions of the service go through one queue
   (``ctx.txs``): one transaction at a time.

The relay re-reads the chain on every loop, so a loop that fails half way
does no harm. The transaction log (``Journal``, JSON lines, fsync) is the
memory of the relay across restarts: it never signs a second
``OracleResolve`` for a challenge while one that it signed could still land,
and a TimeoutResolve waits until each of its OracleResolve transactions has
expired.

Spec choices:

- A dispute is keyed by its claim token. The relay opens a case in the
  deployment that was current at the challenge's ``challenged_at``
  (``state.deployment_for``). When no followed deployment covers
  ``challenged_at`` it signs ``Inconclusive`` with basis ``no-jury``.
- "No case" means no case under §11.2 rules 1-2 at any depth: the openings
  that Koios shows (``state.dispute_cases`` through ``canonical_for``), the
  live cases that it does not show yet (``state.case_counts``), and an own
  Open that the chain does not show yet (the log, 30 minutes).
- The relay signs at most one ``OracleResolve`` per challenge: after the
  node took one, it never signs another (a submission that the node refused
  does not count). When that one expires without effect, the relay submits
  TimeoutResolve after the deadline.
- When ``Finalize`` cannot be built or submitted, the relay resolves from
  the live case output after ``reveal_end`` once it is 10 blocks deep
  (``finalized_in`` is null in the resolution document).
- Before each Module-1 transaction the relay checks the stakes
  (``module1.stake_problems``) and refuses on a mismatch.

The context object ``ctx`` (built by ``service.py``) has: ``chain``
(``vector_jury.chain.Chain``), ``m1`` (``module1.Module1``), ``deployments``,
``address`` (the operator key address: fee payer, refund address, verdict
deposit), ``operator_vkh``, ``signing_key`` (None in a dry run without a
key), ``dry_run``, ``txs`` (the transaction queue), ``log(event, **fields)``,
``foundation_agents`` (the configured juror DIDs, 32 bytes each) and
``state_dir``.
"""

import dataclasses
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import cbor2
from pycardano import Address, Transaction, UTxO

from . import _paths  # noqa: F401  (Module-1 and shared/python on sys.path)
from . import builders, documents, module1, rules, state
from . import types as T
from .chain import Chain, ChainError, SubmitError, ref_key
from .deployment import Deployment

import vector_deploy as vd

MINUTE = 60_000
HOUR = 60 * MINUTE
#: The relay waits this long after ``challenged_at`` before it opens a case,
#: so that a party can open the case with a fee (§12.2 step 2).
GRACE_MS = 30 * MINUTE
#: No grace period when the deadline is this close.
NO_GRACE_MS = 6 * HOUR + 40 * MINUTE
#: A case can still open when the deadline is at least this far: ``hi`` at
#: most 10 minutes after now and ``hi <= D - 6 h``.
OPEN_LEAD_MS = 6 * HOUR + 10 * MINUTE
#: The relay never signs an OracleResolve within this time of the deadline.
RESOLVE_GUARD_MS = 10 * MINUTE
#: A slash waits this long after ``reveal_end`` (§12.2 step 5).
SLASH_DELAY_MS = MINUTE
#: The depth of an opening or a Finalize before the relay uses it.
MIN_DEPTH = 10
#: The validity window of an OracleResolve (slots), capped so that hi <= D.
RESOLVE_WINDOW_SLOTS = 1200
#: The shortest OracleResolve window (slots) that the relay still signs.
MIN_RESOLVE_WINDOW_SLOTS = 300
#: Slots kept free before the deadline when the window is capped.
TTL_MARGIN_SLOTS = 30
#: An own Open that the chain does not show yet blocks a second one this long.
PENDING_OPEN_MS = 30 * MINUTE
#: The relay submits at most this many transactions per loop.
MAX_TXS_PER_LOOP = 12
#: A verdict is reclaimed this long after ``expires_at`` (the lower bound of
#: the reclaim must be after it).
RECLAIM_DELAY_MS = 2 * MINUTE
#: After this many failed Finalize attempts of a canonical case, the relay
#: resolves from the tally of the live case output (§12.2 step 3).
FINALIZE_FAILURES = 2

#: The priority of each action kind (lower first).
PRIORITY = {"resolve": 0, "timeout": 0, "open": 1, "finalize": 2, "apply_verdict": 3, "finalize_own": 4,
            "claim": 5, "close": 6, "slash": 7, "reclaim_verdict": 8}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Transaction log ──────────────────────────────────────────────────────────


class Journal:
    """The relay's transaction log: one JSON object per line, appended and
    synced to disk (mode 600). The entry of a transaction is written with the
    status ``sending`` BEFORE the transaction goes out, then again with its
    result (``submitted``, ``rejected``, ``confirmed``, ``unconfirmed``).
    Dry runs write ``dry-run`` entries, which never count."""

    COUNTS = ("sending", "submitted", "confirmed", "unconfirmed")

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: List[dict] = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        self._entries.append(json.loads(line))
                    except ValueError:
                        continue

    def append(self, entry: dict) -> None:
        entry = {"time": _now_iso(), **entry}
        data = (json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._entries.append(entry)

    def entries(self) -> List[dict]:
        with self._lock:
            return list(self._entries)

    def transactions(self, action: str) -> Dict[str, dict]:
        """The last entry of each transaction of ``action``, merged with the
        fields of its earlier entries."""
        merged: Dict[str, dict] = {}
        for entry in self.entries():
            if entry.get("action") == action and entry.get("tx"):
                merged[entry["tx"]] = {**merged.get(entry["tx"], {}), **entry}
        return merged

    def resolutions(self, challenge: bytes) -> List[dict]:
        """The OracleResolve transactions for a challenge (by challenge token)
        that count: sent to the node and not refused by it."""
        return [e for e in self.transactions("resolve").values()
                if e.get("challenge") == bytes(challenge).hex() and e.get("status") in self.COUNTS]

    def pending_open(self, claim: bytes, now_ms: int) -> bool:
        """True when the relay sent an Open for this claim token in the last
        ``PENDING_OPEN_MS`` (the chain may not show the case yet)."""
        for entry in self.transactions("open").values():
            if entry.get("claim") == bytes(claim).hex() and entry.get("status") in self.COUNTS and \
                    now_ms - int(entry.get("at_ms", 0)) < PENDING_OPEN_MS:
                return True
        return False


# ── The world ────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Finalized:
    """The end of the voting of a case: the Finalize transaction (None for
    the fallback without Finalize), its block, and the tally of the case
    output that it spent."""

    tx_id: Optional[str]
    block_height: int
    tally: T.Tally
    deep: bool


@dataclasses.dataclass(eq=False)
class Dispute:
    """A genuine challenge in ``PendingOracle`` and what the relay knows of
    its cases. ``error`` is set when a read failed; then nothing is done."""

    challenge: module1.ChallengeOutput
    claim: Optional[module1.ClaimOutput]
    deployment: Optional[Deployment]
    canonical: Optional[state.Canonical] = None
    case_known: bool = False
    finalized: Optional[Finalized] = None
    stake_problems: List[str] = dataclasses.field(default_factory=list)
    error: Optional[str] = None

    @property
    def token(self) -> bytes:
        return self.challenge.token

    @property
    def claim_token(self) -> bytes:
        return bytes(self.challenge.datum.claim_token)

    @property
    def deadline(self) -> int:
        return self.challenge.datum.deadline

    @property
    def challenged_at(self) -> int:
        return self.challenge.datum.challenged_at

    def canonical_case_token(self) -> Optional[bytes]:
        if self.canonical is None or not self.canonical.deep_enough:
            return None
        return bytes(self.canonical.opening.token)

    def describe(self) -> Dict[str, Any]:
        return {"challenge": self.token.hex(), "claim": self.claim_token.hex(), "challenge_ref": self.challenge.ref,
                "deadline": self.deadline}


def output_datum(chain: Chain, ref: str):
    """The Plutus Data tree of the inline datum of an output, also when it is
    spent: Ogmios for a live output, else Koios ``tx_info`` of the
    transaction that made it. None when it has no inline datum."""
    utxo = chain.utxo(ref)
    if utxo is not None:
        return None if utxo.output.datum is None else T.tree(utxo.output.datum)
    tx_id, _, index = ref.partition("#")
    for info in chain.koios.tx_info([tx_id], full=True):
        if info.get("tx_hash") != tx_id:
            continue
        for output in info.get("outputs") or []:
            if int(output.get("tx_index", -1)) == int(index):
                return _datum_of(output)
    raise ChainError(f"the output {ref} is neither live nor shown by Koios")


@dataclasses.dataclass(eq=False)
class VerdictOutput:
    """A verdict output at the identity_party address whose ``deposit_to`` is
    the relay. ``problems`` are the reasons why ApplyVerdict cannot run now
    (empty when it can)."""

    utxo: UTxO
    verdict: Any
    problems: List[str] = dataclasses.field(default_factory=list)

    @property
    def ref(self) -> str:
        return ref_key(self.utxo)


@dataclasses.dataclass(eq=False)
class World:
    now_ms: int
    tip_slot: int
    block_height: int
    disputes: List[Dispute]
    cases: List[state.CaseOutput]
    jurors: List[state.JurorOutput]
    verdicts: List[VerdictOutput]

    def dispute_of_claim(self, claim: bytes) -> Optional[Dispute]:
        return next((d for d in self.disputes if d.claim_token == bytes(claim)), None)

    def dispute_of_challenge(self, challenge: bytes) -> Optional[Dispute]:
        return next((d for d in self.disputes if d.token == bytes(challenge)), None)


@dataclasses.dataclass
class Caches:
    """Data that never changes once it is deep in the chain, kept between
    loops: case openings, genuine challenge outputs, Finalize transactions,
    and the Finalize failures of this process."""

    openings: Dict[Tuple[bytes, bytes], state.CaseOpening] = dataclasses.field(default_factory=dict)
    genuine: Dict[str, Optional[bytes]] = dataclasses.field(default_factory=dict)
    finalized: Dict[Tuple[bytes, bytes], Finalized] = dataclasses.field(default_factory=dict)
    finalize_failed: Dict[Tuple[bytes, bytes], int] = dataclasses.field(default_factory=dict)


def ref_text(out: T.OutputReference) -> str:
    return f"{bytes(out.transaction_id).hex()}#{out.output_index}"


def _datum_of(entry: dict):
    """The Plutus Data tree of a Koios inline datum (bytes when present,
    else the JSON value)."""
    inline = entry.get("inline_datum") or {}
    if inline.get("bytes"):
        return cbor2.loads(bytes.fromhex(inline["bytes"]))
    if inline.get("value") is not None:
        return T.data_from_json(inline["value"])
    return None


def case_history(chain: Chain, deployment: Deployment, token: bytes) -> List[Tuple[str, int, Optional[T.Case]]]:
    """The transactions of a case token in chain order: (tx id, block
    height, the Case datum of the case output that the transaction made, or
    None when the transaction burned the token). Koios ``asset_txs`` and
    ``tx_info``."""
    policy = deployment.script_hash.hex()
    rows = chain.koios.get("asset_txs", {"_asset_policy": policy, "_asset_name": bytes(token).hex(),
                                         "_history": "true"})
    hashes = [r["tx_hash"] for r in rows if r.get("tx_hash")]
    if not hashes:
        return []
    infos = chain.koios.tx_info(hashes, full=True)
    out = []
    for info in infos:
        height = info.get("block_height")
        if height is None:
            continue
        datum = None
        for output in info.get("outputs") or []:
            held = [a for a in output.get("asset_list") or []
                    if a.get("policy_id") == policy and a.get("asset_name") == bytes(token).hex()]
            if not held:
                continue
            tree = _datum_of(output)
            if tree is not None:
                try:
                    decoded = T.decode_jury_datum(cbor2.dumps(tree))
                except T.DecodeError:
                    decoded = None
                datum = decoded if isinstance(decoded, T.Case) else None
        out.append((info["tx_hash"], int(height), int(info.get("tx_block_index") or 0), datum))
    out.sort(key=lambda row: (row[1], row[2]))
    return [(tx, height, datum) for tx, height, _index, datum in out]


def find_finalize(chain: Chain, deployment: Deployment, token: bytes, tip_height: int) -> Optional[Finalized]:
    """The Finalize transaction of a case: the first transaction that spent an
    ``Open`` case output and burned the token or made a ``Settling`` output.
    The tally is the one of the spent ``Open`` output."""
    previous = None
    for tx_id, height, datum in case_history(chain, deployment, token):
        if previous is not None and isinstance(previous.state, T.Open) and \
                (datum is None or isinstance(datum.state, T.Settling)):
            return Finalized(tx_id, height, previous.tally, tip_height - height >= MIN_DEPTH)
        previous = datum
    return None


def _is_relay(address: Address, plutus_address) -> bool:
    try:
        return str(T.address_of(plutus_address)) == str(address)
    except Exception:  # noqa: BLE001 - a refund address that pycardano cannot show
        return False


def list_verdicts(ctx, now_ms: int) -> List[VerdictOutput]:
    """The verdict outputs at the identity_party address of the Module-1
    deployment whose ``deposit_to`` is the relay address, with the reasons why
    ApplyVerdict cannot run now."""
    from simulation import identity_party as ip
    from simulation import vector_identity as vi

    from .identity import find_identity_record

    manifest = ctx.m1.manifest
    party_hash = bytes.fromhex(manifest["hashes"]["identity_party"])
    address = manifest["addresses"]["identity_party"]
    relay = ip.PlutusAddress(ip.PlutusCredential(bytes(ctx.operator_vkh)))
    identity_hash = bytes.fromhex(manifest["identity"]["identity_hash"])
    out = []
    for utxo in ctx.chain.utxos_at(address):
        multi = getattr(utxo.output.amount, "multi_asset", None) or {}
        names = [bytes(n.payload) for pid, assets in multi.items() if bytes(pid.payload) == party_hash
                 for n, q in assets.items() if q == 1]
        if len(names) != 1 or utxo.output.datum is None:
            continue
        try:
            verdict = ip.verdict_from_output(utxo.output.datum, names[0])
        except (vi.IdentityError, vi.IdentityDecodeError, ValueError):
            continue
        if verdict.deposit_to != relay:
            continue
        item = VerdictOutput(utxo, verdict)
        if verdict.expires_at >= now_ms:
            lo, hi = now_ms - MINUTE, now_ms + 20 * MINUTE
            try:
                record = find_identity_record(ctx.chain, identity_hash, verdict.agent)
                ip.verdict_application(verdict, record, party_hash, lo, min(hi, verdict.expires_at))
            except Exception as error:  # noqa: BLE001 - IdentityError, RecordNotFoundError
                item.problems.append(str(error))
        out.append(item)
    return out


def observe(ctx, caches: Caches, journal: Optional[Journal] = None) -> World:
    """Read the chain once for the relay and the jurors."""
    chain = ctx.chain
    tip = chain.tip_slot()
    now = chain.slot_to_ms(tip)
    height = chain.block_height()
    challenges = module1.genuine_challenges(chain, ctx.m1, pending_oracle=True, cache=caches.genuine)
    claims = {c.token: c for c in module1.list_claims(chain, ctx.m1)}
    jurors: List[state.JurorOutput] = []
    cases: List[state.CaseOutput] = []
    for deployment in ctx.deployments:
        found_jurors, found_cases = state.read_outputs(chain, deployment)
        jurors += found_jurors
        cases += found_cases
    disputes = []
    for challenge in challenges:
        claim = claims.get(bytes(challenge.datum.claim_token))
        dispute = Dispute(challenge, claim, state.deployment_for(ctx.deployments, challenge.datum.challenged_at))
        try:
            _observe_dispute(ctx, caches, journal, dispute, cases, now, height)
        except Exception as error:  # noqa: BLE001 - a read error stops only this dispute
            dispute.error = f"{type(error).__name__}: {error}"
        disputes.append(dispute)
    try:
        verdicts = list_verdicts(ctx, now)
    except Exception as error:  # noqa: BLE001
        ctx.log("relay.observe_error", what="verdicts", error=f"{type(error).__name__}: {error}")
        verdicts = []
    return World(now, tip, height, disputes, cases, jurors, verdicts)


def _observe_dispute(ctx, caches: Caches, journal: Optional[Journal], dispute: Dispute,
                     cases: Sequence[state.CaseOutput], now: int, height: int) -> None:
    chain = ctx.chain
    if dispute.claim is not None:
        dispute.stake_problems = module1.stake_problems(dispute.claim.utxo, dispute.challenge.utxo)
    else:
        dispute.stake_problems = ["the claim output of the challenge is not on chain"]
    deployment = dispute.deployment
    if deployment is None:
        return
    dispute.canonical = state.canonical_for(chain, ctx.deployments, dispute.claim_token, dispute.challenged_at,
                                            ctx.m1, min_depth=MIN_DEPTH, cache=caches.openings,
                                            genuine_cache=caches.genuine)
    known = [o for (policy, _name), o in caches.openings.items()
             if policy == deployment.script_hash and bytes(o.datum.claim) == dispute.claim_token]
    known += [c for c in cases if c.deployment.script_hash == deployment.script_hash
              and bytes(c.datum.claim) == dispute.claim_token]
    dispute.case_known = any(state.case_counts(chain, ctx.m1, c.datum, genuine_cache=caches.genuine) for c in known)
    if journal is not None and journal.pending_open(dispute.claim_token, now):
        dispute.case_known = True
    canonical = dispute.canonical
    if canonical is None or not canonical.deep_enough or now < canonical.opening.datum.reveal_end:
        return
    key = (deployment.script_hash, bytes(canonical.opening.token))
    live = canonical.live
    if live is not None and isinstance(live.datum.state, T.Open):
        if caches.finalize_failed.get(key, 0) >= FINALIZE_FAILURES:
            tx_id = bytes(live.utxo.input.transaction_id).hex()
            made = chain.tx_block_heights([tx_id]).get(tx_id)
            if made is not None:
                dispute.finalized = Finalized(None, made, live.datum.tally, height - made >= MIN_DEPTH)
        return
    cached = caches.finalized.get(key)
    if cached is not None and cached.deep:
        dispute.finalized = cached
        return
    found = find_finalize(chain, deployment, canonical.opening.token, height)
    if found is not None:
        caches.finalized[key] = found
    dispute.finalized = found


# ── Decisions ────────────────────────────────────────────────────────────────


@dataclasses.dataclass(eq=False)
class Action:
    """A transaction that the relay wants to submit."""

    kind: str
    reason: str
    dispute: Optional[Dispute] = None
    case: Optional[state.CaseOutput] = None
    juror: Optional[state.JurorOutput] = None
    case_token: Optional[bytes] = None
    verdict: Optional[VerdictOutput] = None
    resolution: Optional[Dict[str, Any]] = None

    def describe(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"action": self.kind, "reason": self.reason}
        if self.dispute is not None:
            out.update(self.dispute.describe())
        if self.case is not None:
            out["case"] = self.case.token.hex()
            out["jury"] = self.case.deployment.script_hash.hex()
        if self.juror is not None:
            out["juror"] = self.juror.token.hex()
        if self.case_token is not None:
            out["case"] = bytes(self.case_token).hex()
        if self.verdict is not None:
            out["verdict_ref"] = self.verdict.ref
        if self.resolution is not None:
            out["verdict"] = self.resolution["verdict"]
            out["basis"] = self.resolution["basis"]
        return out


@dataclasses.dataclass(frozen=True)
class Note:
    """A decision without a transaction (logged; printed in a dry run)."""

    subject: str
    message: str


def _resolution(dispute: Dispute, verdict: str, basis: str, jury: Optional[dict]) -> Dict[str, Any]:
    return {"verdict": verdict, "basis": basis, "jury": jury}


def _jury_resolution(dispute: Dispute) -> Dict[str, Any]:
    canonical, final = dispute.canonical, dispute.finalized
    params = canonical.deployment.params
    verdict = T.vote_name(rules.outcome(final.tally, params))
    basis = "no-result" if verdict == "Inconclusive" else "jury"
    jury = {
        "deployment": canonical.deployment.script_hash.hex(),
        "case": bytes(canonical.opening.token).hex(),
        "opened_in": canonical.opening.tx_id,
        "finalized_in": final.tx_id,
        "tally": final.tally.counts(),
    }
    return _resolution(dispute, verdict, basis, jury)


def _decide_dispute(dispute: Dispute, world: World, journal: Journal, actions: List[Action],
                    notes: List[Note]) -> None:
    now, deadline = world.now_ms, dispute.deadline
    subject = f"challenge {dispute.token.hex()}"
    if dispute.error:
        notes.append(Note(subject, f"skipped: a read failed ({dispute.error})"))
        return
    if dispute.claim is None:
        notes.append(Note(subject, "skipped: the claim output is not on chain"))
        return
    if now > deadline:
        open_ttls = [int(e.get("ttl_slot", 0)) for e in journal.resolutions(dispute.token)]
        if any(ttl >= world.tip_slot for ttl in open_ttls):
            notes.append(Note(subject, "after the deadline: an own OracleResolve can still land; waiting"))
        elif dispute.stake_problems:
            notes.append(Note(subject, "after the deadline, but the stakes do not match: "
                                       + "; ".join(dispute.stake_problems)))
        else:
            actions.append(Action("timeout", "the deadline passed without a resolution", dispute))
        return
    if journal.resolutions(dispute.token):
        notes.append(Note(subject, "an own OracleResolve is out; the relay never signs a second one"))
        return
    canonical = dispute.canonical
    if canonical is not None and canonical.deep_enough:
        _decide_canonical(dispute, world, actions, notes)
        return
    if canonical is not None:
        notes.append(Note(subject, f"case {bytes(canonical.opening.token).hex()} is not {MIN_DEPTH} blocks deep yet"))
        return
    if dispute.case_known:
        notes.append(Note(subject, "a case exists but is not canonical yet (indexer or depth); waiting"))
        return
    if dispute.deployment is not None and deadline - now >= OPEN_LEAD_MS:
        if now < dispute.challenged_at + GRACE_MS and deadline - now > NO_GRACE_MS:
            notes.append(Note(subject, f"grace period: a case opens from {dispute.challenged_at + GRACE_MS}"))
            return
        actions.append(Action("open", "no case exists for the dispute", dispute))
        return
    basis, why = ("no-time", "no case can finish before the deadline") if dispute.deployment is not None else \
        ("no-jury", "no followed deployment covers challenged_at")
    if deadline - now < RESOLVE_GUARD_MS:
        notes.append(Note(subject, f"{why}, and the deadline is less than 10 minutes away; "
                                   "TimeoutResolve after the deadline"))
        return
    if dispute.stake_problems:
        notes.append(Note(subject, f"{why}, but the stakes do not match: " + "; ".join(dispute.stake_problems)))
        return
    actions.append(Action("resolve", why, dispute, resolution=_resolution(dispute, "Inconclusive", basis, None)))


def _decide_canonical(dispute: Dispute, world: World, actions: List[Action], notes: List[Note]) -> None:
    now, deadline = world.now_ms, dispute.deadline
    canonical = dispute.canonical
    subject = f"challenge {dispute.token.hex()}"
    reveal_end = canonical.opening.datum.reveal_end
    if now < reveal_end:
        notes.append(Note(subject, f"case {bytes(canonical.opening.token).hex()} votes until {reveal_end}"))
        return
    live = canonical.live
    final = dispute.finalized
    if live is not None and isinstance(live.datum.state, T.Open) and (final is None or final.tx_id is not None):
        actions.append(Action("finalize", "the reveal window of the canonical case ended", dispute, case=live))
        return
    if final is None:
        notes.append(Note(subject, "waiting until the indexer shows the Finalize transaction"))
        return
    if not final.deep:
        notes.append(Note(subject, f"the case end ({final.tx_id or 'live output'}) is not {MIN_DEPTH} blocks deep"))
        return
    if deadline - now < RESOLVE_GUARD_MS:
        notes.append(Note(subject, "the deadline is less than 10 minutes away; TimeoutResolve after the deadline"))
        return
    if dispute.stake_problems:
        notes.append(Note(subject, "the stakes do not match: " + "; ".join(dispute.stake_problems)))
        return
    actions.append(Action("resolve", "the canonical case is final", dispute, resolution=_jury_resolution(dispute)))


def decide(world: World, *, address: Address, operator_vkh: bytes, foundation_agents: Iterable[bytes],
           journal: Journal) -> Tuple[List[Action], List[Note]]:
    """The actions and notes of one loop (SPEC §12.2), highest priority first."""
    actions: List[Action] = []
    notes: List[Note] = []
    now = world.now_ms
    for dispute in world.disputes:
        _decide_dispute(dispute, world, journal, actions, notes)
    finalizing = {a.case.token for a in actions if a.kind == "finalize" and a.case is not None}
    canonical_tokens = {d.canonical_case_token() for d in world.disputes} - {None}
    # Verdicts with deposit_to = the relay: apply at once, reclaim when lapsed.
    for item in world.verdicts:
        if item.verdict.expires_at + RECLAIM_DELAY_MS < now:
            actions.append(Action("reclaim_verdict", "the verdict lapsed", verdict=item))
        elif item.verdict.expires_at >= now and not item.problems:
            actions.append(Action("apply_verdict", "an AuditorWins verdict waits", verdict=item))
        elif item.problems:
            notes.append(Note(f"verdict {item.ref}", "not applied: " + "; ".join(item.problems)))
    # The relay's own cases that are not canonical: finalize after reveal_end.
    for case in world.cases:
        if isinstance(case.datum.state, T.Open) and now >= case.datum.reveal_end and \
                case.token not in finalizing and case.token not in canonical_tokens and \
                (_is_relay(address, case.datum.refund_to) or
                 _is_relay(case.deployment.treasury_address, case.datum.refund_to)):
            actions.append(Action("finalize_own", "an own case that is not canonical ended", case=case))
    # Settling cases: claim the shares of the Foundation jurors, then close.
    agents = {bytes(a) for a in foundation_agents}
    own_jurors = {j.token: j for j in world.jurors if j.owner == bytes(operator_vkh) and j.agent in agents}
    for case in world.cases:
        if not isinstance(case.datum.state, T.Settling):
            continue
        claims = [own_jurors[bytes(b.juror)] for b in case.datum.ballots
                  if isinstance(b.status, T.Revealed) and bytes(b.juror) in own_jurors
                  and own_jurors[bytes(b.juror)].deployment.script_hash == case.deployment.script_hash]
        for juror in claims:
            actions.append(Action("claim", "a Foundation juror has an unpaid share", case=case, juror=juror))
        allowed = rules.close_allowed_at(case.datum, case.deployment.params)
        if not claims and allowed is not None and allowed <= now:
            actions.append(Action("close", "Close is allowed", case=case))
    # Slash every open entry whose reveal window ended a minute ago.
    for juror in world.jurors:
        for entry in juror.datum.open:
            if entry.reveal_end <= now - SLASH_DELAY_MS:
                actions.append(Action("slash", "a commitment was not revealed", juror=juror,
                                      case_token=bytes(entry.case)))
    actions.sort(key=lambda a: PRIORITY[a.kind])
    return actions, notes


# ── Execution ────────────────────────────────────────────────────────────────


class ActionError(RuntimeError):
    """An action cannot run now (logged; the next loop decides again)."""


@dataclasses.dataclass
class Sent:
    """A transaction that the queue handled: built (dry run) or submitted.
    ``wait_ref`` is an output that shows when the transaction is in a block;
    the queue sets ``confirmed`` after it waited for it."""

    tx_id: str
    submitted: bool
    wait_ref: Optional[str]
    info: Dict[str, Any] = dataclasses.field(default_factory=dict)
    confirmed: Optional[bool] = None


def _capture(m1: module1.Module1, build: Callable[[Any, Any, Any], dict]) -> Tuple[dict, bytes]:
    """Run a Module-1 simulation builder with a ``submit_tx`` that keeps the
    transaction instead of sending it. Exactly one transaction is allowed."""
    sim_chain, sim_tx = module1.simulation_modules(m1.network)
    captured: List[bytes] = []

    def hook(tx_bytes):
        data = tx_bytes if isinstance(tx_bytes, bytes) else bytes.fromhex(tx_bytes)
        if captured:
            raise module1.Module1Error("the Module-1 builder made a second transaction")
        captured.append(data)
        return vd.transaction_id(data)

    saved = (sim_tx.submit_tx, sim_chain.submit_tx)
    sim_tx.submit_tx = sim_chain.submit_tx = hook
    try:
        result = build(sim_chain.OgmiosContext(), sim_tx.DeploymentState(m1.manifest), sim_tx)
    finally:
        sim_tx.submit_tx, sim_chain.submit_tx = saved
    if len(captured) != 1:
        raise module1.Module1Error("the Module-1 builder made no transaction")
    return result, captured[0]


class Relay:
    """The relay loop over a context (see the module documentation)."""

    def __init__(self, ctx, *, journal: Optional[Journal] = None, caches: Optional[Caches] = None):
        self.ctx = ctx
        self.journal = journal or Journal(Path(ctx.state_dir) / "relay" / "txlog.jsonl")
        self.caches = caches or Caches()

    # ── one loop ─────────────────────────────────────────────────────────

    def decide(self, world: World) -> Tuple[List[Action], List[Note]]:
        return decide(world, address=self.ctx.address, operator_vkh=self.ctx.operator_vkh,
                      foundation_agents=self.ctx.foundation_agents, journal=self.journal)

    def run_once(self, world: World) -> List[Dict[str, Any]]:
        """Decide and execute the actions of one loop. Returns the results."""
        actions, notes = self.decide(world)
        self.ctx.log("relay.loop", disputes=len(world.disputes), actions=[a.describe() for a in actions],
                     notes=len(notes))
        for note in notes:
            self.ctx.log("relay.note", subject=note.subject, message=note.message)
        results = []
        for action in actions[:MAX_TXS_PER_LOOP]:
            results.append(self.execute(action, world))
        if len(actions) > MAX_TXS_PER_LOOP:
            self.ctx.log("relay.deferred", count=len(actions) - MAX_TXS_PER_LOOP)
        return results

    def execute(self, action: Action, world: World) -> Dict[str, Any]:
        summary = action.describe()
        handler = getattr(self, f"_do_{action.kind}")
        priority = 0 if action.kind in ("resolve", "timeout", "open") else 1
        try:
            sent = self.ctx.txs.submit(action.kind, lambda submit: handler(action, world, submit), priority=priority)
        except Exception as error:  # noqa: BLE001 - one failed action never stops the loop
            if action.kind in ("finalize", "finalize_own") and action.case is not None:
                key = (action.case.deployment.script_hash, action.case.token)
                self.caches.finalize_failed[key] = self.caches.finalize_failed.get(key, 0) + 1
            self.ctx.log("relay.action_failed", **summary, error=f"{type(error).__name__}: {error}")
            return {**summary, "result": "failed", "error": str(error)}
        if sent is None:
            self.ctx.log("relay.decision", **summary, result="decision only (dry run without a key)")
            return {**summary, "result": "decision"}
        if sent.submitted:
            self.journal.append({"action": action.kind, "tx": sent.tx_id,
                                 "status": "confirmed" if sent.confirmed else "unconfirmed"})
        self.ctx.log("relay.tx", **summary, tx=sent.tx_id, submitted=sent.submitted, confirmed=sent.confirmed,
                     **sent.info)
        return {**summary, "result": "submitted" if sent.submitted else "dry-run", "tx": sent.tx_id}

    # ── helpers ──────────────────────────────────────────────────────────

    def _keys(self):
        return [self.ctx.signing_key] if self.ctx.signing_key is not None else []

    def _jury_tx(self, action: Action, plan: builders.Plan, submit: bool, wait_output: Optional[str],
                 log_fields: Dict[str, Any]) -> Sent:
        """Finish a jury plan, log it, submit it when asked."""
        result = builders.finish(plan, self._keys(), submit=False)
        entry = {"action": action.kind, "tx": result.tx_id, **log_fields}
        wait_ref = result.ref(wait_output) if wait_output else f"{result.tx_id}#0"
        if not submit:
            self.journal.append({**entry, "status": "dry-run"})
            return Sent(result.tx_id, False, wait_ref, {"fee": result.fee, "size": result.size})
        if not result.signed:
            raise ActionError(f"{action.kind}: the transaction is not signed (missing keys)")
        self.journal.append({**entry, "status": "sending"})
        try:
            tx_id = self.ctx.chain.submit(result.cbor)
        except SubmitError as error:
            self.journal.append({**entry, "status": "rejected", "error": str(error)[:500]})
            raise ActionError(f"the node refused {action.kind}: {error}") from None
        self.journal.append({**entry, "status": "submitted"})
        return Sent(tx_id, True, wait_ref, {"fee": result.fee, "size": result.size})

    def _m1_tx(self, action: Action, build: Callable[[Any, Any, Any], dict], submit: bool,
               log_fields: Dict[str, Any], check: Callable[[bytes], None] = lambda _tx: None) -> Optional[Sent]:
        """Build a Module-1 transaction with the simulation builders, check it
        (evaluation, size, ``check``), log it, and submit it when asked. A dry
        run without a signing key only reports the decision."""
        if self.ctx.signing_key is None:
            if submit:
                raise ActionError("no signing key")
            return None
        if not any(vd.lovelace_only(u) and vd.coin_of(u) >= builders.MIN_COLLATERAL
                   for u in self.ctx.chain.utxos_at(self.ctx.address)):
            raise ActionError(f"{self.ctx.address} has no lovelace-only output of at least 5 AP3X for collateral")
        result, data = _capture(self.ctx.m1, build)
        if Transaction.from_cbor(data).transaction_witness_set.redeemer:
            self.ctx.chain.evaluate(data)
        if len(data) > module1.MAX_TX_SIZE:
            raise ActionError(f"{action.kind}: {len(data)} bytes, more than {module1.MAX_TX_SIZE}")
        check(data)
        tx_id = vd.transaction_id(data)
        entry = {"action": action.kind, "tx": tx_id, "ttl_slot": result.get("ttl_slot"), **log_fields}
        info = {k: result[k] for k in ("verdict_ref", "ttl_slot") if result.get(k) is not None}
        if not submit:
            self.journal.append({**entry, "status": "dry-run"})
            return Sent(tx_id, False, f"{tx_id}#0", info)
        self.journal.append({**entry, "status": "sending"})
        try:
            node_id = self.ctx.chain.submit(data)
        except SubmitError as error:
            self.journal.append({**entry, "status": "rejected", "error": str(error)[:500]})
            raise ActionError(f"the node refused {action.kind}: {error}") from None
        self.journal.append({**entry, "status": "submitted"})
        return Sent(node_id, True, f"{node_id}#0", info)

    def _wallet(self) -> module1.Wallet:
        return module1.Wallet.from_signing_key(self.ctx.signing_key)

    def _fresh_dispute(self, dispute: Dispute) -> Tuple[module1.ChallengeOutput, module1.ClaimOutput]:
        """The challenge and claim outputs as the chain shows them now, checked
        again: genuine, PendingOracle, stakes."""
        chain, m1 = self.ctx.chain, self.ctx.m1
        challenge = next((c for c in module1.genuine_challenges(chain, m1, cache=self.caches.genuine)
                          if c.token == dispute.token), None)
        if challenge is None:
            raise ActionError("the challenge is no longer a genuine PendingOracle output")
        claim = module1.find_claim(chain, m1, challenge.datum.claim_token)
        if claim is None:
            raise ActionError("the claim output is not on chain")
        problems = module1.stake_problems(claim.utxo, challenge.utxo)
        if problems:
            raise ActionError("; ".join(problems))
        return challenge, claim

    def _fresh_case(self, case: state.CaseOutput) -> state.CaseOutput:
        found = state.find_case(self.ctx.chain, case.deployment, case.token)
        if found is None:
            raise ActionError(f"the case {case.token.hex()} is not live any more")
        return found

    def _fresh_juror(self, juror: state.JurorOutput) -> state.JurorOutput:
        found = state.find_juror(self.ctx.chain, juror.deployment, juror.token)
        if found is None:
            raise ActionError(f"the juror {juror.token.hex()} is not live any more")
        return found

    # ── actions ──────────────────────────────────────────────────────────

    def _do_open(self, action: Action, world: World, submit: bool) -> Sent:
        dispute = action.dispute
        challenge, claim = self._fresh_dispute(dispute)
        # SPEC §12.2 step 2: the relay's case refund goes to the treasury of the
        # deployment. The treasury is a script address, so it is never a Module-1
        # party credential, and a refund never shares an output with a Module-1
        # payout (§6.2 rule 9, §14).
        refund_to = dispute.deployment.treasury_address
        plan = builders.build_open(self.ctx.chain, dispute.deployment, self.ctx.address, challenge=challenge.utxo,
                                   claim=claim.utxo, fee=0, lovelace=builders.CASE_LOVELACE,
                                   refund_to=refund_to)
        fields = {"challenge": dispute.token.hex(), "claim": dispute.claim_token.hex(),
                  "case": plan.info["case_token"].hex(), "jury": dispute.deployment.script_hash.hex(),
                  "at_ms": world.now_ms}
        return self._jury_tx(action, plan, submit, "case", fields)

    def _do_finalize(self, action: Action, world: World, submit: bool) -> Sent:
        case = self._fresh_case(action.case)
        plan = builders.build_finalize(self.ctx.chain, case.deployment, self.ctx.address, case=case)
        output = "refund" if plan.info.get("burns") else "case"
        fields = {"case": case.token.hex(), "jury": case.deployment.script_hash.hex(), "result": plan.info["result"],
                  "tally": plan.info["tally"]}
        return self._jury_tx(action, plan, submit, output, fields)

    _do_finalize_own = _do_finalize

    def _do_resolve(self, action: Action, world: World, submit: bool) -> Optional[Sent]:
        dispute, resolution = action.dispute, action.resolution
        challenge, claim = self._fresh_dispute(dispute)
        if self.journal.resolutions(dispute.token):
            raise ActionError("an own OracleResolve for this challenge is already out")
        doc = module1.resolution_document(challenge, resolution["verdict"], resolution["basis"],
                                          jury=resolution["jury"])
        chain = self.ctx.chain
        tip = chain.tip_slot()
        deadline_slot = chain.ms_to_slot(dispute.deadline)
        window = min(RESOLVE_WINDOW_SLOTS, deadline_slot - TTL_MARGIN_SLOTS - (tip - 60))
        if window < MIN_RESOLVE_WINDOW_SLOTS:
            raise ActionError("the deadline is too close for an OracleResolve")
        aux = documents.auxiliary_data(doc, "resolution", "application/json")
        wallet = self._wallet() if self.ctx.signing_key is not None else None

        def build(context, deployment, sim_tx):
            return sim_tx.build_oracle_resolve(
                context, deployment, wallet.skey, wallet.vkey, wallet.address, challenge.ref, claim.ref,
                verdict=resolution["verdict"], reasoning_hash=documents.doc_hash(doc), window_slots=window,
                auxiliary_data=aux)

        def check(data: bytes) -> None:
            ttl = Transaction.from_cbor(data).transaction_body.ttl
            if ttl is None or chain.slot_to_ms(ttl) > dispute.deadline:
                raise ActionError(f"the OracleResolve upper bound {ttl} is after the deadline {dispute.deadline}")

        fields = {"challenge": dispute.token.hex(), "challenge_ref": challenge.ref, "claim": dispute.claim_token.hex(),
                  "verdict": resolution["verdict"], "basis": resolution["basis"],
                  "resolution_hash": documents.doc_hash(doc).hex()}
        sent = self._m1_tx(action, build, submit, fields, check)
        if sent is not None:
            sent.info["resolution"] = json.loads(doc)
        return sent

    def _do_timeout(self, action: Action, world: World, submit: bool) -> Optional[Sent]:
        dispute = action.dispute
        challenge, claim = self._fresh_dispute(dispute)
        if any(int(e.get("ttl_slot", 0)) >= self.ctx.chain.tip_slot() for e in self.journal.resolutions(dispute.token)):
            raise ActionError("an own OracleResolve can still land")
        wallet = self._wallet() if self.ctx.signing_key is not None else None

        def build(context, deployment, sim_tx):
            return sim_tx.build_timeout_resolve(context, deployment, wallet.skey, wallet.vkey, wallet.address,
                                                challenge.ref, claim.ref)

        fields = {"challenge": dispute.token.hex(), "challenge_ref": challenge.ref, "claim": dispute.claim_token.hex()}
        return self._m1_tx(action, build, submit, fields)

    def _do_apply_verdict(self, action: Action, world: World, submit: bool) -> Optional[Sent]:
        ref = action.verdict.ref
        if self.ctx.chain.utxo(ref) is None:
            raise ActionError(f"the verdict output {ref} is spent")
        wallet = self._wallet() if self.ctx.signing_key is not None else None

        def build(context, deployment, sim_tx):
            return sim_tx.build_apply_verdict(context, deployment, wallet.skey, wallet.vkey, wallet.address, ref)

        return self._m1_tx(action, build, submit, {"verdict_ref": ref,
                                                   "agent": bytes(action.verdict.verdict.agent).hex()})

    def _do_reclaim_verdict(self, action: Action, world: World, submit: bool) -> Optional[Sent]:
        ref = action.verdict.ref
        if self.ctx.chain.utxo(ref) is None:
            raise ActionError(f"the verdict output {ref} is spent")
        wallet = self._wallet() if self.ctx.signing_key is not None else None

        def build(context, deployment, sim_tx):
            return sim_tx.build_reclaim_verdict(context, deployment, wallet.skey, wallet.vkey, wallet.address, ref)

        return self._m1_tx(action, build, submit, {"verdict_ref": ref})

    def _do_slash(self, action: Action, world: World, submit: bool) -> Sent:
        juror = self._fresh_juror(action.juror)
        if rules.open_entry(juror.datum, action.case_token) is None:
            raise ActionError("the open entry is gone (revealed or slashed)")
        plan = builders.build_slash(self.ctx.chain, juror.deployment, self.ctx.address, juror=juror,
                                    case=action.case_token)
        fields = {"juror": juror.token.hex(), "case": bytes(action.case_token).hex(),
                  "jury": juror.deployment.script_hash.hex()}
        return self._jury_tx(action, plan, submit, "juror", fields)

    def _do_claim(self, action: Action, world: World, submit: bool) -> Sent:
        case = self._fresh_case(action.case)
        juror = self._fresh_juror(action.juror)
        plan = builders.build_claim(self.ctx.chain, case.deployment, self.ctx.address, juror=juror, case=case)
        fields = {"juror": juror.token.hex(), "case": case.token.hex(), "jury": case.deployment.script_hash.hex()}
        return self._jury_tx(action, plan, submit, "case", fields)

    def _do_close(self, action: Action, world: World, submit: bool) -> Sent:
        case = self._fresh_case(action.case)
        plan = builders.build_close(self.ctx.chain, case.deployment, self.ctx.address, case=case)
        fields = {"case": case.token.hex(), "jury": case.deployment.script_hash.hex()}
        return self._jury_tx(action, plan, submit, "refund", fields)
