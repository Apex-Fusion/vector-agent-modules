"""The reference juror (SPEC §13) for one or more configured jurors.

For each juror, every loop:

- **Commit.** For each canonical case (SPEC §11.2, 10 blocks deep) in
  ``Open`` state, in its commit window, whose genuine challenge is still
  ``PendingOracle``, where the juror is eligible (§6.4), is not a party and
  has no ballot (cases in random order): read and check the two documents,
  build the model input (``input_template``), compute the record budget, buy
  one model call through the gateway pinned to the juror's suppliers
  (``gateway.buy``), read the vote, write the record (§10.4), make a new salt
  (``secrets``), save salt, vote and record durably (``CommitStore``: fsync,
  mode 600), then submit the commit. A case with less than 5 minutes left
  before ``commit_end`` gets no model call.
- **Reveal.** In the reveal window, reveal each open commitment with the
  saved record in the metadata. When that transaction is too large
  (§10.4 overflow): first publish the record (same bytes, type
  ``juror-record``) in a transaction of its own, then reveal with a
  ``juror-record-ref`` document that holds the ``vector-tx:`` URI of it.
- **Contention.** A commit or a reveal whose case output was spent first is
  built again from a fresh read of the case.

Paid calls: a verified model call is saved in the juror's case state before
anything else, so a restart never pays twice for one case. Every request
that may fund an escrow is counted (durably, before it goes out), and the
juror stops at ``MAX_PAID_ATTEMPTS`` per case.

Record (SPEC §10.4): ``request`` holds the request as the gateway hashed it
(the normalised input items and the committed ``max_output_tokens``; the
instructions are in the prompt document). ``response`` is the full response
when it fits, else ``{"cut": true, "output_text": …}`` with the output text
cut to fit. The budget is computed before the model call: 16,384 bytes minus
the size of a reveal transaction with an empty record now (built with the
juror's ballot and open entry added), minus 113 bytes for each of 30 more
ballots and 47 bytes for each open entry of the juror, at most 15,000. A
record fits when its metadata (CBOR) adds at most the budget to the reveal.
A record that cannot fit the reveal is made for the overflow way (at most
15,000 bytes). The caps of the input template keep every record at 15,000
bytes or less, so the juror never skips a commit because of the record
size.

Vote (SPEC §10.4): the ``vote`` of the LAST JSON object in the output text
that has a valid ``vote``. Every ``{`` of the text starts a candidate that
``json`` decodes; "last" is the candidate that ends last (an outer object
wins over the objects inside it). No such object: ``Inconclusive``.

The context ``ctx`` is the one of ``relay.py`` plus ``gateway_url``,
``supplier_keys`` (``gateway.SupplierKeys``), ``commits``
(``CommitStore``) and ``genuine_cache``.
"""

import dataclasses
import json
import os
import random
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cbor2

from . import _paths  # noqa: F401
from . import builders, documents, gateway, input_template, module1, relay, rules, state
from . import types as T
from .chain import ChainError, SubmitError
from .commit_store import make_attempt

MINUTE = 60_000
#: A case with less time than this before ``commit_end`` gets no model call.
MIN_COMMIT_LEFT_MS = 5 * MINUTE
#: The model answer must arrive this long before ``commit_end`` (room for the
#: commit transaction).
COMMIT_RESERVE_MS = 3 * MINUTE
#: The largest transaction and the largest record (SPEC §10.4).
MAX_TX = 16_384
MAX_RECORD = 15_000
#: The reserve of the record budget (SPEC §13 step 1.3).
RESERVE_BALLOTS = 30
BALLOT_BYTES = 113
OPEN_ENTRY_BYTES = 47
#: Bytes kept free in a reveal for the encodings of fee and units.
SIZE_SAFETY = 64
#: The longest reason (characters).
REASON_CHARS = 600
#: Paid gateway attempts per case and juror.
MAX_PAID_ATTEMPTS = 2
#: Builds of a commit or reveal when other transactions spend the case first.
CONTENTION_TRIES = 4
CONTENTION_WAIT = 3.0
#: The format of the juror list (``jurors/foundation/jurors.json``).
JURORS_FORMAT = "vector-jury-foundation-jurors/2"
PROMPT_TYPE = "vector-jury-prompt/1"
RECORD_TYPE = "vector-jury-record/1"
CASE_STATE_FORMAT = "vector-jury-juror-case/1"
VOTES = T.VOTE_NAMES


# ── Configuration ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class JurorSpec:
    """A configured juror: its DID, its prompt document and its gateway key
    (the name of an environment variable)."""

    name: str
    agent: bytes
    model: str
    suppliers: Tuple[gateway.Supplier, ...]
    prompt: bytes
    prompt_hash: bytes
    instructions: str
    max_output_tokens: int
    api_key_env: str

    @property
    def did(self) -> str:
        return f"did:vector:{self.agent.hex()}"


class ConfigError(ValueError):
    """The juror list or a prompt document is not valid."""


def _hex(value: Any, size: int, what: str) -> bytes:
    try:
        raw = bytes.fromhex(str(value))
    except ValueError:
        raw = b""
    if len(raw) != size:
        raise ConfigError(f"{what} must be {2 * size} hex characters")
    return raw


def load_jurors(path, *, names: Optional[Sequence[str]] = None) -> Tuple[bytes, List[JurorSpec]]:
    """The owner key hash and the jurors of a ``vector-jury-foundation-
    jurors/2`` file. Each prompt file must hash to its ``prompt_hash`` and
    agree with the list (juror DID, model, suppliers, template, output
    limit). ``names`` selects jurors by name."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from None
    if data.get("format") != JURORS_FORMAT:
        raise ConfigError(f"{path} is not a {JURORS_FORMAT} file")
    owner = _hex(data.get("owner_vkh"), 28, "owner_vkh")
    out = []
    for entry in data.get("jurors") or []:
        if names is not None and entry.get("name") not in names:
            continue
        name = str(entry.get("name") or "")
        agent = _hex(entry.get("agent"), 32, f"{name}.agent")
        suppliers = tuple(gateway.Supplier(_hex(s.get("agent"), 32, f"{name} supplier agent").hex(),
                                           _hex(s.get("supplier_pkh"), 28, f"{name} supplier_pkh").hex())
                          for s in entry.get("suppliers") or [])
        if not suppliers:
            raise ConfigError(f"{name}: no supplier")
        try:
            prompt = (path.parent / str(entry.get("prompt_file") or "")).read_bytes()
        except OSError as error:
            raise ConfigError(f"{name}: cannot read the prompt file: {error}") from None
        prompt_hash = _hex(entry.get("prompt_hash"), 32, f"{name}.prompt_hash")
        if documents.doc_hash(prompt) != prompt_hash:
            raise ConfigError(f"{name}: the prompt file does not hash to prompt_hash")
        try:
            doc = json.loads(prompt)
        except ValueError:
            raise ConfigError(f"{name}: the prompt file is not JSON") from None
        model = doc.get("model") or {}
        listed = [(str(s.get("agent")), str(s.get("supplier_pkh"))) for s in model.get("suppliers") or []]
        problems = []
        if doc.get("type") != PROMPT_TYPE:
            problems.append(f"type is not {PROMPT_TYPE}")
        if doc.get("juror") != f"did:vector:{agent.hex()}":
            problems.append("juror is not the DID of the list")
        if model.get("model") != entry.get("model"):
            problems.append("the model differs from the list")
        if listed != [(f"did:vector:{s.agent}", s.supplier_pkh) for s in suppliers]:
            problems.append("the suppliers differ from the list")
        if doc.get("input_template") != input_template.TEMPLATE:
            problems.append(f"input_template is not {input_template.TEMPLATE}")
        if doc.get("max_output_tokens") != entry.get("max_output_tokens"):
            problems.append("max_output_tokens differs from the list")
        if not isinstance(doc.get("instructions"), str) or not doc["instructions"]:
            problems.append("instructions are missing")
        if not entry.get("api_key_env"):
            problems.append("api_key_env is missing")
        if problems:
            raise ConfigError(f"{name}: " + "; ".join(problems))
        out.append(JurorSpec(name, agent, str(entry["model"]), suppliers, prompt, prompt_hash, doc["instructions"],
                             int(entry["max_output_tokens"]), str(entry["api_key_env"])))
    if names is not None and sorted(j.name for j in out) != sorted(set(names)):
        missing = sorted(set(names) - {j.name for j in out})
        raise ConfigError(f"{path} has no juror named {', '.join(missing)}")
    return owner, out


# ── Vote and record ──────────────────────────────────────────────────────────


def parse_vote(text: str) -> Tuple[str, str, bool]:
    """``(vote, reason, found)`` of an output text (SPEC §10.4): the LAST JSON
    object with a valid ``vote``; ``Inconclusive`` and ``""`` when none."""
    decoder = json.JSONDecoder()
    best: Optional[Tuple[int, dict]] = None
    start = text.find("{")
    while start >= 0:
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            value, end = None, start
        if isinstance(value, dict) and isinstance(value.get("vote"), str) and value["vote"] in VOTES:
            if best is None or end > best[0]:
                best = (end, value)
        start = text.find("{", start + 1)
    if best is None:
        return "Inconclusive", "", False
    reason = best[1].get("reason")
    reason = input_template.clean_text(reason)[:REASON_CHARS] if isinstance(reason, str) else ""
    return best[1]["vote"], reason, True


def encode(obj: dict) -> bytes:
    """The record bytes: compact JSON, UTF-8."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def writable(data: bytes) -> bool:
    """True when a document may go in metadata (no U+0000, no ``\\u0000``)."""
    return b"\x00" not in data and b"\\u0000" not in data


def metadata_growth(record: bytes) -> int:
    """The bytes that the record adds to a reveal with an empty record (the
    label 22084 metadata in CBOR)."""
    return len(cbor2.dumps(documents.metadatum(record, "juror-record"))) - \
        len(cbor2.dumps(documents.metadatum(b"", "juror-record")))


@dataclasses.dataclass(frozen=True)
class RecordParts:
    """Everything of a record except the model call, response, vote and
    reason."""

    jury: str
    case: str
    challenge: str
    juror: str
    prompt_hash: str
    prompt_uri: Optional[str]
    claim: Dict[str, str]
    evidence: Dict[str, str]


def record_object(parts: RecordParts, call: gateway.ModelCall, response: dict, vote: str, reason: str) -> dict:
    return {
        "type": RECORD_TYPE,
        "jury": parts.jury,
        "case": parts.case,
        "challenge": parts.challenge,
        "juror": parts.juror,
        "prompt": {"hash": parts.prompt_hash, "uri": parts.prompt_uri},
        "documents": {"claim": parts.claim, "evidence": parts.evidence},
        "request": {"template": input_template.TEMPLATE, "input": gateway.normalized_input(call.input),
                    "max_output_tokens": call.committed_max_output_tokens},
        "model": {
            "supplier": f"did:vector:{call.supplier_agent}",
            "supplier_pub_key": call.supplier_pub_key,
            "model": call.model,
            "escrow": call.escrow_ref,
            "receipt": call.receipt,
            "receipt_signature": call.receipt_signature,
        },
        "response": response,
        "vote": vote,
        "reason": reason,
    }


def fit_record(parts: RecordParts, call: gateway.ModelCall, vote: str, reason: str,
               fits: Callable[[bytes], bool]) -> Optional[Tuple[bytes, str]]:
    """The record with the largest response that ``fits``: the full response,
    else ``{"cut": true, "output_text": …}`` with the longest prefix of the
    output text. ``(bytes, "full"|"cut")``, or None when not even an empty
    output text fits."""
    full = encode(record_object(parts, call, {"output": call.output, "status": call.status,
                                              "incomplete_details": call.incomplete_details}, vote, reason))
    if writable(full) and fits(full):
        return full, "full"
    text = input_template.clean_text(call.text)

    def cut(n: int) -> bytes:
        return encode(record_object(parts, call, {"cut": True, "output_text": text[:n]}, vote, reason))

    if fits(cut(len(text))):
        return cut(len(text)), "cut"
    if not fits(cut(0)):
        return None
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(cut(mid)):
            lo = mid
        else:
            hi = mid - 1
    return cut(lo), "cut"


def make_record(parts: RecordParts, call: gateway.ModelCall, vote: str, reason: str,
                budget: int) -> Tuple[bytes, str, bool]:
    """``(record, shape, in_reveal)``: the record that fits the reveal budget,
    else the record for the overflow way (at most 15,000 bytes)."""

    def fits_reveal(data: bytes) -> bool:
        return len(data) <= MAX_RECORD and metadata_growth(data) <= budget - SIZE_SAFETY

    fitted = fit_record(parts, call, vote, reason, fits_reveal)
    if fitted is not None:
        return fitted[0], fitted[1], True
    fitted = fit_record(parts, call, vote, reason, lambda data: len(data) <= MAX_RECORD)
    if fitted is None:
        raise JurorError("no record of at most 15,000 bytes: the input template caps do not hold")
    return fitted[0], fitted[1], False


# ── Durable case state ───────────────────────────────────────────────────────


def _write_json(path: Path, data: dict) -> None:
    """Write a JSON file durably: temporary file (mode 600), fsync, rename,
    fsync of the directory (mode 700)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(data, indent=1) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class CaseState:
    """The juror's durable state of one case: the paid attempts, the verified
    model call, the overflow publication and the reveal."""

    def __init__(self, root: Path, juror: str, jury: bytes, case: bytes):
        self.path = Path(root) / juror / bytes(jury).hex() / f"{bytes(case).hex()}.json"
        self.data: Dict[str, Any] = {"format": CASE_STATE_FORMAT, "juror": juror, "jury": bytes(jury).hex(),
                                     "case": bytes(case).hex(), "attempts": 0, "call": None}
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            if loaded.get("format") != CASE_STATE_FORMAT:
                raise ValueError(f"{self.path} is not a {CASE_STATE_FORMAT} file")
            self.data.update(loaded)

    def save(self, **fields) -> None:
        self.data.update(fields)
        _write_json(self.path, self.data)

    @property
    def attempts(self) -> int:
        return int(self.data.get("attempts") or 0)

    @property
    def call(self) -> Optional[gateway.ModelCall]:
        return gateway.ModelCall.from_json(self.data["call"]) if self.data.get("call") else None


# ── The juror ────────────────────────────────────────────────────────────────


class JurorError(RuntimeError):
    """A commit or a reveal cannot go on now (logged)."""


@dataclasses.dataclass
class Job:
    """A commit or a reveal of one juror in one case."""

    kind: str
    juror: str
    case: state.CaseOutput
    juror_output: state.JurorOutput
    dispute: Optional[relay.Dispute] = None

    @property
    def key(self) -> Tuple[str, str, bytes]:
        return self.kind, self.juror, self.case.token


def _contended(error: Exception) -> bool:
    """True when the case or juror output was spent by another transaction."""
    if isinstance(error, SubmitError):
        return True
    text = str(error).lower()
    return isinstance(error, (ChainError, builders.BuildError)) and (
        "not on chain" in text or "badinputs" in text or "unknown" in text or "spent" in text
        or "does not hold the token" in text)


class Juror:
    """One configured juror over a context (see the module documentation)."""

    def __init__(self, ctx, spec: JurorSpec, *, gateway_client: Optional[gateway.Gateway] = None,
                 sleep: Callable[[float], None] = time.sleep, rng: Optional[random.Random] = None):
        self.ctx = ctx
        self.spec = spec
        self._gateway = gateway_client
        self.sleep = sleep
        self.rng = rng or random.SystemRandom()
        self.root = Path(ctx.state_dir) / "jurors"
        self._prompt_uris: Dict[bytes, Optional[str]] = {}

    # ── what is due ──────────────────────────────────────────────────────

    def own_outputs(self, world: relay.World) -> List[state.JurorOutput]:
        return [j for j in world.jurors if j.agent == self.spec.agent and j.owner == bytes(self.ctx.operator_vkh)]

    def eligible_output(self, world: relay.World, case: state.CaseOutput, at_ms: int,
                        *, chain=None) -> Tuple[Optional[state.JurorOutput], List[str]]:
        """The juror output that can commit in ``case`` (the first one that
        passes every rule), and the reasons of the last refusal."""
        problems: List[str] = ["the juror has no output in this deployment"]
        for output in self.own_outputs(world):
            if output.deployment.script_hash != case.deployment.script_hash:
                continue
            problems = []
            if bytes(output.datum.prompt_hash) != self.spec.prompt_hash:
                problems.append("the registered prompt hash is not the configured prompt")
            problems += state.eligibility(output, case, at_ms, chain=chain)
            if not problems:
                return output, []
        return None, problems

    def due(self, world: relay.World) -> Tuple[List[Job], List[Dict[str, Any]]]:
        """The jobs of this loop (commits in random order, then reveals) and
        the notes (why a case is skipped)."""
        commits: List[Job] = []
        reveals: List[Job] = []
        notes: List[Dict[str, Any]] = []
        now = world.now_ms
        own = self.own_outputs(world)
        for case in world.cases:
            datum = case.datum
            if not isinstance(datum.state, T.Open):
                continue
            if now < datum.commit_end:
                if any(bytes(b.agent) == self.spec.agent for b in datum.ballots):
                    continue
                dispute = world.dispute_of_claim(bytes(datum.claim))
                if dispute is None or dispute.canonical_case_token() != case.token or \
                        dispute.token != bytes(datum.challenge):
                    continue
                output, problems = self.eligible_output(world, case, now)
                if output is None:
                    notes.append({"case": case.token.hex(), "skip": "; ".join(problems)})
                    continue
                if datum.commit_end - now < MIN_COMMIT_LEFT_MS and CaseState(
                        self.root, self.spec.name, case.deployment.script_hash, case.token).call is None:
                    notes.append({"case": case.token.hex(), "skip": "less than 5 minutes left to commit"})
                    continue
                commits.append(Job("commit", self.spec.name, case, output, dispute))
            elif now < datum.reveal_end:
                for output in own:
                    if output.deployment.script_hash != case.deployment.script_hash:
                        continue
                    found = rules.ballot_of(datum, output.token)
                    if found and isinstance(found[1].status, T.Committed):
                        reveals.append(Job("reveal", self.spec.name, case, output))
        self.rng.shuffle(commits)
        return commits + reveals, notes

    # ── shared helpers ───────────────────────────────────────────────────

    def _keys(self):
        return [self.ctx.signing_key] if self.ctx.signing_key is not None else []

    def _gateway_client(self) -> gateway.Gateway:
        if self._gateway is None:
            key = os.environ.get(self.spec.api_key_env, "")
            if not key:
                raise JurorError(f"the environment variable {self.spec.api_key_env} (gateway API key) is empty")
            self._gateway = gateway.Gateway(key, url=self.ctx.gateway_url, keys=self.ctx.supplier_keys)
        return self._gateway

    def prompt_uri(self, juror_output: state.JurorOutput) -> Optional[str]:
        """The ``vector-tx:`` URI of the registration (it carries the prompt)."""
        if juror_output.token not in self._prompt_uris:
            mints = self.ctx.chain.asset_mint_txs(juror_output.deployment.script_hash, juror_output.token)
            self._prompt_uris[juror_output.token] = documents.uri_for(mints[0]) if mints else None
        return self._prompt_uris[juror_output.token]

    def documents_of(self, case: state.CaseOutput) -> Tuple[input_template.DocumentInput,
                                                            input_template.DocumentInput]:
        """The claim and evidence documents of a case: URIs from the outputs
        ``claim_out`` and ``challenge_out`` (also when spent), hashes from the
        case, and the hash check."""
        chain = self.ctx.chain
        claim = T.decode_claim_datum(relay.output_datum(chain, relay.ref_text(case.datum.claim_out)))
        challenge = T.decode_challenge_datum(relay.output_datum(chain, relay.ref_text(case.datum.challenge_out)))
        out = []
        for uri, expected in ((claim.storage_uri, case.datum.claim_hash),
                              (challenge.evidence_uri, case.datum.evidence_hash)):
            checked = documents.check(bytes(uri), bytes(expected), koios=chain.koios)
            out.append(input_template.DocumentInput(bytes(uri), bytes(expected).hex(), checked.status, checked.data))
        return out[0], out[1]

    def budget(self, juror_output: state.JurorOutput, case: state.CaseOutput) -> int:
        """The record budget (SPEC §13 step 1.3), in bytes of the reveal
        transaction."""
        chain = self.ctx.chain
        ballot = T.Ballot(juror_output.agent, juror_output.token, bytes(32), T.Committed())
        case_after = dataclasses.replace(case.datum, ballots=[ballot] + list(case.datum.ballots))
        juror_after = dataclasses.replace(juror_output.datum, open=[T.OpenVote(case.token, case.datum.reveal_end)]
                                          + list(juror_output.datum.open))
        plan = builders.build_reveal(
            chain, case.deployment, self.ctx.address,
            juror=state.JurorOutput(juror_output.deployment, juror_output.utxo, juror_output.token, juror_after),
            case=state.CaseOutput(case.deployment, case.utxo, case.token, case_after),
            vote="Inconclusive", salt=bytes(32), record=b"", check=False,
            validity=(chain.ms_to_slot(case.datum.commit_end, ceil=True), chain.ms_to_slot(case.datum.reveal_end)))
        size = builders.plan_size(plan)
        return min(MAX_RECORD, MAX_TX - size - RESERVE_BALLOTS * BALLOT_BYTES
                   - OPEN_ENTRY_BYTES * len(juror_output.datum.open))

    def _finish(self, plan: builders.Plan, submit: bool, wait_output: Optional[str] = None) -> relay.Sent:
        result = builders.finish(plan, self._keys(), submit=False)
        if not submit:
            return relay.Sent(result.tx_id, False, None, {"fee": result.fee, "size": result.size})
        if not result.signed:
            raise JurorError(f"{plan.action}: the transaction is not signed (missing keys)")
        tx_id = self.ctx.chain.submit(result.cbor)
        wait = f"{tx_id}#{result.outputs[wait_output]}" if wait_output in result.outputs else f"{tx_id}#0"
        return relay.Sent(tx_id, True, wait, {"fee": result.fee, "size": result.size})

    def _retry(self, what: str, attempt: Callable[[], Any]):
        """Run ``attempt`` again when another transaction spent the case first."""
        for round_ in range(CONTENTION_TRIES):
            try:
                return attempt()
            except (ChainError, builders.BuildError) as error:
                if round_ + 1 >= CONTENTION_TRIES or not _contended(error):
                    raise
                self.ctx.log("juror.contention", juror=self.spec.name, what=what, error=str(error)[:300])
                self.sleep(CONTENTION_WAIT)
        raise JurorError(f"{what}: the case output keeps moving")

    def _fresh(self, case: state.CaseOutput, juror_token: bytes) -> Tuple[state.CaseOutput, state.JurorOutput]:
        chain = self.ctx.chain
        fresh_case = state.find_case(chain, case.deployment, case.token)
        fresh_juror = state.find_juror(chain, case.deployment, juror_token)
        if fresh_case is None or fresh_juror is None:
            raise JurorError("the case or the juror output is gone")
        return fresh_case, fresh_juror

    # ── commit ───────────────────────────────────────────────────────────

    def commit(self, job: Job) -> Dict[str, Any]:
        """The commit flow of one case (runs in a worker thread: the model call
        takes minutes and runs outside the transaction queue)."""
        ctx, spec, chain = self.ctx, self.spec, self.ctx.chain
        case, deployment = job.case, job.case.deployment
        base = {"juror": spec.name, "case": case.token.hex(), "jury": deployment.script_hash.hex()}
        saved = CaseState(self.root, spec.name, deployment.script_hash, case.token)
        call = saved.call
        if case.datum.commit_end - chain.now_ms() < MIN_COMMIT_LEFT_MS and call is None:
            return {**base, "result": "skipped", "reason": "less than 5 minutes left to commit"}
        claim_doc, evidence_doc = self.documents_of(case)
        facts = input_template.CaseFacts(deployment.script_hash.hex(), case.token.hex(),
                                         bytes(case.datum.challenge).hex(), bytes(case.datum.claim).hex(),
                                         bytes(case.datum.claimer).hex(), bytes(case.datum.auditor).hex(),
                                         case.datum.deadline)
        input_text = input_template.render(facts, claim_doc, evidence_doc)
        budget = self.budget(job.juror_output, case)
        info = {**base, "budget": budget, "claim": claim_doc.status, "evidence": evidence_doc.status,
                "input_bytes": len(input_text.encode("utf-8"))}
        if ctx.dry_run:
            return {**info, "result": "dry-run", "reason": "would buy one model call and commit (no paid call in a "
                                                            "dry run)"}
        if call is None:
            cutoff = (case.datum.commit_end - COMMIT_RESERVE_MS) / 1000
            outcome = gateway.buy(self._gateway_client(), spec.suppliers, model=spec.model,
                                  instructions=spec.instructions, input_text=input_text,
                                  max_output_tokens=spec.max_output_tokens, cutoff=cutoff, attempts=saved.attempts,
                                  max_attempts=MAX_PAID_ATTEMPTS, on_attempt=lambda n: saved.save(attempts=n))
            for entry in outcome.log:
                ctx.log("juror.gateway", **base, **entry)
            if outcome.call is None:
                return {**info, "result": "no-call", "reason": outcome.reason}
            call = outcome.call
            saved.save(call=call.to_json())
        elif call.input != input_text:
            ctx.log("juror.saved_call", **base, note="the saved call has another input; it is used as it is")
        vote, reason, found = parse_vote(call.text)
        if not found and call.refusal:
            reason = input_template.clean_text("refusal: " + call.refusal)[:REASON_CHARS]
        parts = RecordParts(
            jury=deployment.script_hash.hex(), case=case.token.hex(), challenge=bytes(case.datum.challenge).hex(),
            juror=spec.did, prompt_hash=spec.prompt_hash.hex(), prompt_uri=self.prompt_uri(job.juror_output),
            claim={"uri": input_template.uri_text(claim_doc.uri), "hash": claim_doc.expected_hash,
                   "status": claim_doc.status},
            evidence={"uri": input_template.uri_text(evidence_doc.uri), "hash": evidence_doc.expected_hash,
                      "status": evidence_doc.status})
        record, shape, in_reveal = make_record(parts, call, vote, reason, budget)
        juror_token, network = job.juror_output.token, chain.network
        attempt = ctx.commits.matching(network, deployment.script_hash, case.token, juror_token, vote, record)
        if attempt is None:
            attempt = make_attempt(case.token, spec.agent, vote, record, salt=secrets.token_bytes(32),
                                   created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            ctx.commits.add(network, deployment.script_hash, case.token, juror_token, attempt)
        info.update({"vote": vote, "record_bytes": len(record), "record_shape": shape,
                     "record_hash": attempt.record_hash.hex(), "escrow": call.escrow_ref,
                     "overflow_expected": not in_reveal})

        def build(submit: bool) -> relay.Sent:
            def once() -> relay.Sent:
                fresh_case, fresh_juror = self._fresh(case, juror_token)
                if any(bytes(b.agent) == spec.agent for b in fresh_case.datum.ballots):
                    raise JurorError("the juror already has a ballot in this case")
                if not self._still_pending(fresh_case):
                    raise JurorError("the genuine challenge is no longer PendingOracle")
                plan = builders.build_commit(chain, deployment, ctx.address, juror=fresh_juror, case=fresh_case,
                                             vote=vote, salt=attempt.salt, record_hash=attempt.record_hash)
                sent = self._finish(plan, submit, "case")
                if sent.submitted:
                    ctx.commits.mark_sent(network, deployment.script_hash, case.token, juror_token, attempt.commit,
                                          sent.tx_id)
                return sent

            return self._retry("commit", once)

        sent = ctx.txs.submit("commit", build, priority=0)
        return {**info, "result": "committed" if sent.submitted else "dry-run", "tx": sent.tx_id}

    def _still_pending(self, case: state.CaseOutput) -> bool:
        challenges = module1.genuine_challenges(self.ctx.chain, self.ctx.m1, pending_oracle=True,
                                                cache=self.ctx.genuine_cache)
        return any(c.token == bytes(case.datum.challenge) for c in challenges)

    # ── reveal and overflow ──────────────────────────────────────────────

    def reveal(self, job: Job) -> Dict[str, Any]:
        """Reveal one commitment: with the record in the metadata, or the
        overflow way (publish the record, then reveal with a reference)."""
        ctx, spec, chain = self.ctx, self.spec, self.ctx.chain
        case, deployment, juror_token = job.case, job.case.deployment, job.juror_output.token
        base = {"juror": spec.name, "case": case.token.hex(), "jury": deployment.script_hash.hex()}
        found = rules.ballot_of(case.datum, juror_token)
        if found is None or not isinstance(found[1].status, T.Committed):
            return {**base, "result": "skipped", "reason": "no committed ballot of this juror"}
        attempt = ctx.commits.find(chain.network, deployment.script_hash, case.token, juror_token, found[1].commit)
        if attempt is None:
            ctx.log("juror.lost_commitment", **base, commit=bytes(found[1].commit).hex())
            return {**base, "result": "failed", "reason": "no saved attempt matches the commitment on chain"}
        saved = CaseState(self.root, spec.name, deployment.script_hash, case.token)

        def with_record() -> builders.Plan:
            fresh_case, fresh_juror = self._fresh(case, juror_token)
            return builders.build_reveal(chain, deployment, ctx.address, juror=fresh_juror, case=fresh_case,
                                         vote=attempt.vote, salt=attempt.salt, record=attempt.record,
                                         record_media=attempt.record_media)

        overflow = builders.plan_size(with_record()) > MAX_TX - SIZE_SAFETY
        result: Dict[str, Any] = {**base, "vote": attempt.vote, "record_hash": attempt.record_hash.hex()}
        if not overflow:
            def build(submit: bool) -> relay.Sent:
                def once() -> relay.Sent:
                    sent = self._finish(with_record(), submit, "case")
                    if sent.submitted:
                        saved.save(reveal={"tx": sent.tx_id, "overflow": False})
                    return sent

                return self._retry("reveal", once)

            try:
                sent = ctx.txs.submit("reveal", build, priority=0)
                return {**result, "result": "revealed" if sent.submitted else "dry-run", "tx": sent.tx_id,
                        "overflow": False}
            except builders.BuildError as error:
                if "bytes, more than" not in str(error):
                    raise
        return {**result, **self._reveal_overflow(job, attempt, saved)}

    def _reveal_overflow(self, job: Job, attempt, saved: CaseState) -> Dict[str, Any]:
        """SPEC §10.4 overflow: publish the record in a transaction of its own
        (in the reveal window), then reveal with a ``juror-record-ref``
        document that holds the URI of that transaction."""
        ctx, chain = self.ctx, self.ctx.chain
        case, deployment, juror_token = job.case, job.case.deployment, job.juror_output.token
        published = (saved.data.get("publish") or {}).get("tx")
        if published and chain.utxo(f"{published}#0") is None and not chain.tx_block_heights([published]).get(published):
            published = None
        if not published:
            def publish(submit: bool) -> relay.Sent:
                plan = builders.build_publish(chain, ctx.address, attempt.record, "juror-record",
                                              media=attempt.record_media)
                sent = self._finish(plan, submit)
                if sent.submitted:
                    saved.save(publish={"tx": sent.tx_id, "record_hash": attempt.record_hash.hex()})
                return sent

            sent = ctx.txs.submit("publish", publish, priority=0)
            if not sent.submitted:
                return {"result": "dry-run", "overflow": True, "publish_tx": sent.tx_id}
            published = sent.tx_id
        ref = documents.uri_for(published)

        def build(submit: bool) -> relay.Sent:
            def once() -> relay.Sent:
                fresh_case, fresh_juror = self._fresh(case, juror_token)
                plan = builders.build_reveal(chain, deployment, ctx.address, juror=fresh_juror, case=fresh_case,
                                             vote=attempt.vote, salt=attempt.salt, record=None,
                                             record_hash=attempt.record_hash, record_ref=ref)
                sent = self._finish(plan, submit, "case")
                if sent.submitted:
                    saved.save(reveal={"tx": sent.tx_id, "overflow": True, "record_ref": ref})
                return sent

            return self._retry("reveal", once)

        sent = ctx.txs.submit("reveal", build, priority=0)
        return {"result": "revealed" if sent.submitted else "dry-run", "tx": sent.tx_id, "overflow": True,
                "publish_tx": published, "record_ref": ref}
