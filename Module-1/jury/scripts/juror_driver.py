#!/usr/bin/env python3
"""Try the reference jurors on one claim document and one evidence document.

    python Module-1/jury/scripts/juror_driver.py CLAIM EVIDENCE [--dry-run] [--output FILE]

The script makes no case on the chain and sends no transaction. It does for the
two documents what a juror of the jury service does for a case:

1. It renders the model input of the service (``input_template.render``). The
   case facts in the input (the jury, the case, the challenge, the claim, the
   two agents and the deadline) are made up from the two documents. They name
   nothing on the chain. The hash of a document is the blake2b-256 hash of its
   file, and its status is ``ok``.
2. It loads the juror list (``juror.load_jurors``) and buys one model call for
   each juror through the marketplace gateway (``gateway.buy``, the call of the
   service). The calls run one after the other.
3. It reads each answer with the vote parser of the service
   (``juror.parse_vote``) and prints the vote and the reason of each juror.

A juror whose call fails, or whose answer has no valid vote, is shown as such.
The run goes on with the next juror.

The key. The script uses ONE gateway API key. The operator sets it in the
environment variable JUROR_DRIVER_GATEWAY_KEY (``--key-env`` names another
variable). The script reads no key file. Use a new key that the trial funds.
The script refuses a key of a juror: a variable that a juror list names in
``api_key_env`` or that starts with JURY_GATEWAY_KEY_, and a key that has the
value of one of these variables. The script never prints a key. A dry run
needs no key.

A real run buys one model call for each juror, and the gateway charges the key
for each call. One result is a sample: run a pair of documents more than once.

Exit status: 0 when every juror gave a vote. 1 when a call failed or an answer
has no valid vote (the run went to the end). 2 when the script refuses to
start (it bought nothing). 130 after Ctrl-C.
"""

import argparse
import dataclasses
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, TextIO

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from vector_jury import _paths  # noqa: E402
from vector_jury import documents, gateway, input_template, juror, rules  # noqa: E402

#: The format of the output file.
FORMAT = "vector-jury-driver-run/1"
#: The juror list of the service.
FOUNDATION_JURORS = _paths.JURY_DIR / "jurors" / "foundation" / "jurors.json"
#: The environment variable of the key of this script.
KEY_ENV = "JUROR_DRIVER_GATEWAY_KEY"
#: Each key variable of a juror of the service starts with this text
#: (``api_key_env`` in the juror list, ``service/gateway-keys.env.example``).
JUROR_KEY_PREFIX = "JURY_GATEWAY_KEY_"
#: The made-up deadline in the case facts: this long after the start of the run.
DEADLINE_AFTER_MS = 72 * 60 * 60 * 1000
#: The most characters of an answer that the text output shows.
SHOWN_ANSWER = 400
#: What the text output says about a juror without a verified call, by the ``status`` of its run.
NO_ANSWER = {"no-call": "The gateway gave no verified call.", "error": "An error stopped the call."}
#: The Unicode categories that the text output never shows: control characters, format characters (for example
#: the marks that change the direction of the text), surrogates, and line and paragraph separators.
HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class Refusal(Exception):
    """The script does not go on. The text says why."""


# ── Documents and case facts ─────────────────────────────────────────────────


def read_document(path: str, what: str) -> bytes:
    """The bytes of a document file, at most as many as the service reads."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(documents.MAX_FETCH_BYTES + 1)
    except OSError as error:
        raise Refusal(f"cannot read the {what} document {path}: {error.strerror or error}") from None
    if len(data) > documents.MAX_FETCH_BYTES:
        raise Refusal(f"the {what} document {path} is larger than {documents.MAX_FETCH_BYTES} bytes")
    return data


def document_uri(label: str, data: bytes) -> bytes:
    """A made-up ``vector-tx:`` URI for a document. It has the form of the URI
    of a published document. Its transaction id belongs to no transaction."""
    seed = b"vector-jury-driver/" + label.encode("ascii") + documents.doc_hash(data)
    return documents.uri_for(rules.blake2b_256(seed)).encode("ascii")


def document_input(label: str, data: bytes) -> input_template.DocumentInput:
    """The document as the service gives it to the template: the URI as bytes,
    the hash that the case holds (hex), the status ``ok`` and the bytes."""
    return input_template.DocumentInput(document_uri(label, data), documents.doc_hash(data).hex(), "ok", data)


def made_up_facts(claim: bytes, evidence: bytes, now_ms: int) -> input_template.CaseFacts:
    """The case facts of the input. The two document hashes make the ids, so
    the same two documents give the same ids. The deadline is 72 hours after
    ``now_ms``."""
    seed = documents.doc_hash(claim) + documents.doc_hash(evidence)

    def made_up(label: str, digest: Callable[[bytes], bytes]) -> str:
        return digest(b"vector-jury-driver/" + label.encode("ascii") + seed).hex()

    return input_template.CaseFacts(
        jury=made_up("jury", rules.blake2b_224), case=made_up("case", rules.blake2b_256),
        challenge=made_up("challenge", rules.blake2b_256), claim=made_up("claim", rules.blake2b_256),
        claimer=made_up("claimer", rules.blake2b_256), auditor=made_up("auditor", rules.blake2b_256),
        deadline_ms=now_ms + DEADLINE_AFTER_MS)


def render_input(facts: input_template.CaseFacts, claim: bytes, evidence: bytes) -> str:
    """The model input of the service for the two documents."""
    return input_template.render(facts, document_input("claim", claim), document_input("evidence", evidence))


# ── The key ──────────────────────────────────────────────────────────────────


def juror_key_variables(specs: Sequence[juror.JurorSpec], environ: Mapping[str, str]) -> Set[str]:
    """The names of the environment variables that hold the key of a juror: the
    ``api_key_env`` of the loaded jurors, and each variable of ``environ`` that
    starts with ``JUROR_KEY_PREFIX``."""
    return {spec.api_key_env for spec in specs} | {name for name in environ if name.startswith(JUROR_KEY_PREFIX)}


def key_problems(key_env: str, environ: Mapping[str, str], specs: Sequence[juror.JurorSpec]) -> List[str]:
    """The reasons why the key of ``key_env`` is a key of a juror (empty when
    it is not). The texts name variables, never values."""
    owners = {spec.api_key_env: spec.name for spec in specs}
    problems = []
    if key_env in owners:
        problems.append(f"{key_env} is the key variable of the juror {owners[key_env]}")
    elif key_env.startswith(JUROR_KEY_PREFIX):
        problems.append(f"{key_env} starts with {JUROR_KEY_PREFIX}, the start of the key variables of the jurors")
    key = environ.get(key_env, "")
    if key:
        for name in sorted(juror_key_variables(specs, environ) - {key_env}):
            if environ.get(name) == key:
                problems.append(f"the key in {key_env} is the key in {name}, a key variable of the jurors")
    return problems


def hide(text: str, secrets: Sequence[str]) -> str:
    """The text without the secrets."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "<hidden>")
    return text


def hide_all(value: Any, secrets: Sequence[str]) -> Any:
    """A JSON value without the secrets in its texts."""
    if isinstance(value, str):
        return hide(value, secrets)
    if isinstance(value, list):
        return [hide_all(item, secrets) for item in value]
    if isinstance(value, dict):
        return {hide(str(key), secrets): hide_all(item, secrets) for key, item in value.items()}
    return value


def printable(text: str) -> str:
    """The text for a terminal: U+FFFD replaces each character of a category in
    ``HIDDEN_CATEGORIES``. Line feed and tab stay. The answer of a model is
    untrusted text."""
    return "".join("\ufffd" if char not in "\n\t" and unicodedata.category(char) in HIDDEN_CATEGORIES else char
                   for char in text)


class Out:
    """A text stream that shows only printable characters and never a secret."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.secrets: List[str] = []

    def line(self, text: str = "") -> None:
        self.stream.write(printable(hide(text, self.secrets)) + "\n")
        self.stream.flush()


# ── One juror ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class JurorRun:
    """What one juror did in a run. ``status`` is one of these words:
    ``not-run``; ``vote`` (the answer has a valid vote); ``no-vote`` (the
    parser refuses the answer, and the service records Inconclusive for it);
    ``no-call`` (the gateway gave no verified call); ``error`` (an error that
    the driver does not expect); ``interrupted``."""

    name: str
    model: str
    prompt_hash: str
    max_output_tokens: int
    instructions: str
    status: str = "not-run"
    vote: Optional[str] = None
    reason: str = ""
    output_text: str = ""
    refusal: str = ""
    response_status: Optional[str] = None
    incomplete_details: Any = None
    supplier: Optional[str] = None
    escrow: Optional[str] = None
    elapsed_ms: Optional[int] = None
    attempts: int = 0
    error: Optional[str] = None
    gateway_log: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


def new_run(spec: juror.JurorSpec) -> JurorRun:
    return JurorRun(spec.name, spec.model, spec.prompt_hash.hex(), spec.max_output_tokens, spec.instructions)


def run_juror(spec: juror.JurorSpec, client: Any, input_text: str, *, clock: Callable[[], float],
              sleep: Callable[[float], None]) -> JurorRun:
    """Buy one model call for the juror, as the service does, and read the
    answer with the vote parser of the service. A failure does not raise: the
    result says what happened."""
    run = new_run(spec)
    try:
        outcome = gateway.buy(client, spec.suppliers, model=spec.model, instructions=spec.instructions,
                              input_text=input_text, max_output_tokens=spec.max_output_tokens,
                              cutoff=clock() + gateway.MAX_CALL_SECONDS, max_attempts=juror.MAX_PAID_ATTEMPTS,
                              clock=clock, sleep=sleep)
    except Exception as error:  # noqa: BLE001 - the run goes on with the next juror
        run.status, run.error = "error", f"{type(error).__name__}: {error}"
        return run
    run.attempts, run.gateway_log = outcome.attempts, outcome.log
    call = outcome.call
    if call is None:
        run.status, run.error = "no-call", outcome.reason
        return run
    vote, reason, found = juror.parse_vote(call.text)
    run.status = "vote" if found else "no-vote"
    run.vote, run.reason = vote, reason
    run.output_text, run.refusal = call.text, call.refusal
    run.response_status, run.incomplete_details = call.status, call.incomplete_details
    run.supplier, run.escrow, run.elapsed_ms = f"did:vector:{call.supplier_agent}", call.escrow_ref, call.elapsed_ms
    return run


def tally_of(runs: Sequence[JurorRun]) -> Dict[str, int]:
    """The number of jurors for each result. ``no_valid_vote`` counts the
    answers that the parser refuses (the service records Inconclusive for
    them). ``no_answer`` counts the jurors without a verified call. A juror
    that did not run is not counted."""
    tally = {**{name: 0 for name in juror.VOTES}, "no_valid_vote": 0, "no_answer": 0}
    for run in runs:
        if run.status == "vote" and run.vote in tally:
            tally[run.vote] += 1
        elif run.status == "no-vote":
            tally["no_valid_vote"] += 1
        elif run.status != "not-run":
            tally["no_answer"] += 1
    return tally


def one_line(text: str, limit: int) -> str:
    """The text on one line, cut to ``limit`` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + " ..."


def juror_lines(run: JurorRun) -> List[str]:
    """The text that shows the result of one juror."""
    lines = []
    if run.status == "vote":
        lines.append(f"  vote:    {run.vote}")
        lines.append("  reason:  " + (run.reason.replace("\n", "\n           ") or "(none)"))
    elif run.status == "no-vote":
        lines.append("  vote:    none. The parser finds no valid vote in the answer.")
        lines.append("           The service records Inconclusive for such an answer.")
        if run.response_status != "completed" or run.incomplete_details:
            lines.append(f"  status:  the response is {run.response_status} (details: {run.incomplete_details})")
        if run.refusal:
            lines.append(f"  refusal: {one_line(run.refusal, SHOWN_ANSWER)}")
        lines.append(f"  answer:  {one_line(run.output_text, SHOWN_ANSWER) or '(empty)'}")
    else:
        lines.append(f"  vote:    none. {NO_ANSWER.get(run.status, run.status)}")
        lines.append(f"  failed:  {one_line(run.error or run.status, 2 * SHOWN_ANSWER)}")
    if run.escrow:
        lines.append(f"  call:    escrow {run.escrow}, {(run.elapsed_ms or 0) / 1000:.1f} s")
    return lines


def summary_lines(runs: Sequence[JurorRun]) -> List[str]:
    """The vote of each juror in short, and the tally."""
    lines = ["Votes:"]
    for run in runs:
        word = {"vote": run.vote, "no-vote": "no valid vote"}.get(run.status, "no answer")
        lines.append(f"  {run.name:<20} {word}")
    lines.append("Tally: " + ", ".join(f"{name.replace('_', ' ')} {count}" for name, count in tally_of(runs).items()))
    return lines


# ── The run ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="juror_driver.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("claim", help="the claim document (a file)")
    parser.add_argument("evidence", help="the evidence document (a file)")
    parser.add_argument("--jurors", default=str(FOUNDATION_JURORS), metavar="FILE",
                        help="the juror list to load (vector-jury-foundation-jurors/2). Default: the juror list of "
                             "the Foundation")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the model input and call nothing (no key needed)")
    parser.add_argument("--output", metavar="FILE",
                        help="write the votes, the prompts (instructions and input) and the model names to FILE "
                             "(JSON)")
    parser.add_argument("--key-env", default=KEY_ENV, metavar="NAME",
                        help=f"the environment variable that holds the gateway API key. Default: {KEY_ENV}")
    return parser


def default_gateway(key: str) -> gateway.Gateway:
    """The gateway client that the service makes for a juror."""
    return gateway.Gateway(key, url=gateway.GATEWAY_URL, keys=gateway.SupplierKeys())


def write_report(path: Path, report: Dict[str, Any]) -> None:
    """Write the report as a JSON file in one step (a temporary file, then a
    rename). Non-ASCII characters are escaped, so that a terminal shows no
    control character of a model answer."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(report, indent=1) + "\n")
        os.replace(tmp, path)
    except OSError as error:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise Refusal(f"cannot write {path}: {error.strerror or error}") from None


def document_line(label: str, path: str, data: bytes, shown: Mapping[str, Any]) -> str:
    """One line about a document of the input: its size, its hash and whether
    the template cut its text."""
    kept = len(str(shown["text"]).encode("utf-8"))
    cut = f"cut, the juror sees the first {kept} bytes" if shown["cut"] else "not cut"
    return f"  {label + ':':<10} {path} ({len(data)} bytes, blake2b-256 {documents.doc_hash(data).hex()}, {cut})"


def drive(args: argparse.Namespace, environ: Mapping[str, str], out: Out, make_gateway: Callable[[str], Any],
          clock: Callable[[], float], sleep: Callable[[float], None]) -> int:
    claim = read_document(args.claim, "claim")
    evidence = read_document(args.evidence, "evidence")
    try:
        _owner, specs = juror.load_jurors(args.jurors)
    except juror.ConfigError as error:
        raise Refusal(f"the juror list {args.jurors}: {error}") from None
    if not specs:
        raise Refusal(f"the juror list {args.jurors} has no juror")
    problems = key_problems(args.key_env, environ, specs)
    if problems:
        raise Refusal("the driver refuses this key. " + "; ".join(problems) + f". Use a new gateway API key in "
                      f"{KEY_ENV}, or in another variable that is not a key variable of a juror.")
    key = environ.get(args.key_env, "")
    if not key and not args.dry_run:
        raise Refusal(f"the environment variable {args.key_env} (gateway API key) is empty. A real run needs a "
                      "new gateway API key that the trial funds.")
    out.secrets = [key, *(environ[name] for name in juror_key_variables(specs, environ) if environ.get(name))]

    started = clock()
    facts = made_up_facts(claim, evidence, int(started * 1000))
    input_text = render_input(facts, claim, evidence)
    shown = json.loads(input_text)["documents"]
    runs = [new_run(spec) for spec in specs]
    report: Dict[str, Any] = {
        "format": FORMAT, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "dry_run": args.dry_run, "template": input_template.TEMPLATE, "jurors_file": str(args.jurors),
        "facts": dataclasses.asdict(facts),
        "documents": {
            "claim": {"file": args.claim, "bytes": len(claim), "hash": documents.doc_hash(claim).hex(),
                      "cut": shown["claim"]["cut"]},
            "evidence": {"file": args.evidence, "bytes": len(evidence), "hash": documents.doc_hash(evidence).hex(),
                         "cut": shown["evidence"]["cut"]}},
        "input": input_text, "jurors": [], "tally": {}}
    write_failed = False

    def save(*, first: bool = False) -> None:
        nonlocal write_failed
        report["jurors"] = [dataclasses.asdict(item) for item in runs]
        report["tally"] = tally_of(runs)
        if not args.output:
            return
        try:
            write_report(Path(args.output), hide_all(report, out.secrets))
        except Refusal as problem:
            if first:
                raise
            write_failed = True
            out.line(f"Warning: {problem}")

    # The first write comes before the first paid call: a path that cannot be written stops the run now.
    save(first=True)
    out.line("Documents:")
    out.line(document_line("claim", args.claim, claim, shown["claim"]))
    out.line(document_line("evidence", args.evidence, evidence, shown["evidence"]))
    out.line(f"Model input: {input_template.TEMPLATE}, {len(input_text.encode('utf-8'))} bytes. The facts of the case "
             "in it are made up. They name nothing on the chain.")
    out.line(f"Jurors ({len(runs)}) from {args.jurors}:")
    for spec in specs:
        out.line(f"  {spec.name:<20} {spec.model}, prompt {spec.prompt_hash.hex()[:16]}, "
                 f"{len(spec.suppliers)} supplier(s)")
    if args.dry_run:
        out.line()
        out.line("Model input (every juror gets this text; the output file keeps the exact text):")
        out.line(input_text)
        out.line()
        out.line(f"Dry run: nothing was sent. A real run buys one model call for each of the {len(runs)} jurors.")
        if args.output:
            out.line(f"Output: {args.output}")
        return 0

    out.line(f"Key: the variable {args.key_env} (the value stays hidden).")
    out.line(f"This run buys one model call for each of the {len(runs)} jurors. The gateway charges the key for "
             "each call. The calls run one after the other.")
    client = make_gateway(key)
    for number, spec in enumerate(specs, 1):
        out.line()
        out.line(f"[{number}/{len(runs)}] {spec.name} ({spec.model}) ...")
        try:
            runs[number - 1] = run_juror(spec, client, input_text, clock=clock, sleep=sleep)
        except KeyboardInterrupt:
            runs[number - 1].status = "interrupted"
            save()
            out.line("Interrupted. The call that was running can still be charged."
                     + (" The output file holds the jurors that finished." if args.output else ""))
            return 130
        save()
        for line in juror_lines(runs[number - 1]):
            out.line(line)
    out.line()
    for line in summary_lines(runs):
        out.line(line)
    if args.output:
        out.line(f"Output: {args.output}")
    return 0 if all(item.status == "vote" for item in runs) and not write_failed else 1


def main(argv: Optional[Sequence[str]] = None, *, environ: Optional[Mapping[str, str]] = None,
         stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
         make_gateway: Callable[[str], Any] = default_gateway, clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(argv)
    if stdout is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    out = Out(stdout or sys.stdout)
    try:
        return drive(args, os.environ if environ is None else environ, out, make_gateway, clock, sleep)
    except Refusal as refusal:
        err = Out(stderr or sys.stderr)
        err.secrets = out.secrets
        err.line(f"juror_driver: {refusal}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
