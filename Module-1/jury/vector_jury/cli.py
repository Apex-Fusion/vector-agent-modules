"""``vector-jury``: the command line tool of the Module-1 jury.

Every transaction command is a dry run unless ``--submit`` is given: the
tool builds the transaction, evaluates it with Ogmios and prints it. A
submit on mainnet needs ``APEX_NETWORK_CONFIRM=yes`` in the environment.
Keys are read by path (``--key`` pays the fees; ``--owner-key`` signs for a
juror owner when it is another key). The tool prints addresses and key
hashes, never key material.

Groups: ``doc`` (publish and read documents), ``m1`` (Module-1 claims,
challenges and resolutions), ``juror`` and ``case`` (the jury actions).
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import _paths  # noqa: F401
from . import builders, documents, rules, state
from . import types as T
from .builders import BuildError, Result, finish
from .chain import Chain, ChainError
from .commit_store import CommitStore, make_attempt
from .deployment import Deployment, DeploymentError, default_manifest_path, load_manifests, utc_now
from .module1 import Module1, Module1Error, MODULE1_MANIFEST

from pycardano import Address
from simulation import vector_identity as vi
import vector_deploy as vd

AP3X = rules.AP3X


class CliError(RuntimeError):
    """A command cannot run with the given options."""


# ── Formatting ───────────────────────────────────────────────────────────────


def iso(posix_ms: Optional[int]) -> str:
    if posix_ms is None:
        return "-"
    return datetime.datetime.fromtimestamp(posix_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ap3x(lovelace: int) -> str:
    return f"{lovelace / AP3X:,.6f} AP3X"


def lovelace_of(value: str) -> int:
    """AP3X text (for example "10" or "0.5") as lovelace."""
    try:
        amount = round(float(value) * AP3X)
    except ValueError:
        raise CliError(f"{value!r} is not an AP3X amount") from None
    if amount < 0:
        raise CliError("an amount is not negative")
    return amount


def token_arg(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise argparse.ArgumentTypeError("a token name is 64 hex characters")
    return raw


class Ctx:
    """The shared state of one command: options, keys, chain, deployments."""

    def __init__(self, args):
        self.args = args
        self._chain: Optional[Chain] = None
        self._deployments: Optional[List[Deployment]] = None
        self._m1: Optional[Module1] = None
        self.payer_key = vd.load_signing_key(args.key) if args.key else None
        self.owner_key = vd.load_signing_key(args.owner_key) if args.owner_key else None

    # output

    def out(self, data: Dict[str, Any]) -> None:
        if self.args.json:
            print(json.dumps(data, indent=2, default=str))
            return
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, default=str)
            print(f"{key}: {value}")

    # network, chain, deployments

    @property
    def deployments(self) -> List[Deployment]:
        if self._deployments is None:
            paths = self.args.manifest
            if not paths:
                default = default_manifest_path(self.args.network or "mainnet")
                paths = [default] if default.exists() else []
            self._deployments = load_manifests(paths) if paths else []
        return self._deployments

    @property
    def network(self) -> str:
        if self.args.network:
            return self.args.network
        if self.deployments:
            return self.deployments[0].network
        return "mainnet"

    @property
    def chain(self) -> Chain:
        if self._chain is None:
            self._chain = Chain(self.network, ogmios_url=self.args.ogmios_url, koios_url=self.args.koios_url)
        return self._chain

    @property
    def m1(self) -> Module1:
        if self._m1 is None:
            self._m1 = Module1.load(self.args.module1)
        return self._m1

    def deployment(self) -> Deployment:
        """The deployment for a new juror or case: ``--jury``, else the first
        manifest."""
        if not self.deployments:
            raise CliError("no jury deployment: give --manifest (default "
                           f"{default_manifest_path(self.network)} does not exist)")
        if self.args.jury:
            for d in self.deployments:
                if d.script_hash.hex() == self.args.jury.lower():
                    return d
            raise CliError(f"no manifest has the script hash {self.args.jury}")
        return self.deployments[0]

    def juror(self, token: bytes) -> state.JurorOutput:
        found = state.find_token(self.chain, self.deployments, token)
        if not isinstance(found, state.JurorOutput):
            raise CliError(f"no juror output holds {token.hex()} in the followed deployments")
        return found

    def case(self, token: bytes) -> state.CaseOutput:
        found = state.find_token(self.chain, self.deployments, token)
        if not isinstance(found, state.CaseOutput):
            raise CliError(f"no case output holds {token.hex()} in the followed deployments")
        return found

    # keys

    def payer(self):
        if self.payer_key is None:
            raise CliError("give the fee payer key: --key <skey file>")
        return builders.key_address(self.payer_key)

    def keys(self) -> list:
        return [k for k in (self.payer_key, self.owner_key) if k is not None]

    def guard(self) -> None:
        """The mainnet guard of a submit."""
        if self.args.submit and self.network == "mainnet" and \
                os.environ.get("APEX_NETWORK_CONFIRM", "").strip().lower() != "yes":
            raise CliError("--submit on mainnet needs APEX_NETWORK_CONFIRM=yes in the environment")

    def finish(self, plan) -> Result:
        self.guard()
        result = finish(plan, self.keys(), submit=self.args.submit)
        if self.args.save_tx:
            Path(self.args.save_tx).write_text(result.cbor.hex() + "\n")
        if self.args.submit:
            print(f"submitted {result.tx_id}", file=sys.stderr)
        elif result.missing_signers:
            print("dry run (unsigned: no key for " + ", ".join(h.hex() for h in result.missing_signers) + ")",
                  file=sys.stderr)
        else:
            print("dry run: signed and evaluated, nothing sent", file=sys.stderr)
        return result


def show_result(ctx: Ctx, result: Result, extra: Optional[Dict[str, Any]] = None) -> None:
    data = result.summary()
    if extra:
        data.update(extra)
    ctx.out(data)


def key_info(key) -> Dict[str, str]:
    return {"address": str(builders.key_address(key)), "key_hash": builders.key_hash(key).hex()}


# ── doc ──────────────────────────────────────────────────────────────────────


def cmd_doc_publish(ctx: Ctx) -> None:
    data = Path(ctx.args.file).read_bytes()
    plan = builders.build_publish(ctx.chain, ctx.payer(), data, ctx.args.type, ctx.args.media)
    result = ctx.finish(plan)
    show_result(ctx, result, {"hash": documents.doc_hash(data).hex(), "uri": result.uri,
                              "chunks": len(documents.chunk(data))})


def cmd_doc_get(ctx: Ctx) -> None:
    koios = ctx.chain.koios
    if ctx.args.hash:
        checked = documents.check(ctx.args.uri, ctx.args.hash, koios=koios)
        print(f"status: {checked.status}" + (f" ({checked.error})" if checked.error else ""), file=sys.stderr)
        data = checked.data
        if checked.status != "ok":
            raise CliError(f"the document is {checked.status}: {checked.error}")
    else:
        data = documents.fetch(ctx.args.uri, koios=koios)
    print(f"hash: {documents.doc_hash(data).hex()} ({len(data)} bytes)", file=sys.stderr)
    if ctx.args.out:
        Path(ctx.args.out).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()


# ── m1 ───────────────────────────────────────────────────────────────────────


def _wallet(ctx: Ctx):
    from .module1 import Wallet

    if ctx.payer_key is None:
        raise CliError("the Module-1 builders sign with the wallet key: give --key")
    return Wallet.from_signing_key(ctx.payer_key)


def _challenge(ctx: Ctx):
    """The challenge of ``--challenge``: a challenge token (64 hex) or an
    output reference ``<tx id>#<index>``."""
    from .module1 import challenge_of_ref, find_challenge

    value = ctx.args.challenge
    if "#" in value:
        return challenge_of_ref(ctx.chain, ctx.m1, value)
    try:
        token = token_arg(value)
    except argparse.ArgumentTypeError as error:
        raise CliError(f"--challenge: {error}") from None
    found = find_challenge(ctx.chain, ctx.m1, token)
    if found is None:
        raise CliError(f"no Module-1 challenge output holds the token {value}")
    return found


def _publish_for(ctx: Ctx, path: str, doc_type: str):
    """Publish a document first (claim or evidence): (hash, uri). With
    --submit the tool waits until the chain shows the transaction."""
    data = Path(path).read_bytes()
    result = ctx.finish(builders.build_publish(ctx.chain, ctx.payer(), data, doc_type, "application/json"))
    if ctx.args.submit:
        ctx.chain.wait_for(result.ref(0))
    print(f"{doc_type} document: {result.uri} (tx {result.tx_id})", file=sys.stderr)
    return documents.doc_hash(data), result.uri


def _document_args(ctx: Ctx, doc_type: str):
    if ctx.args.doc:
        return _publish_for(ctx, ctx.args.doc, doc_type)
    if not (ctx.args.hash and ctx.args.uri):
        raise CliError("give --doc FILE, or --hash and --uri")
    return bytes.fromhex(ctx.args.hash), ctx.args.uri


def cmd_m1_list(ctx: Ctx) -> None:
    from .module1 import list_challenges

    rows = []
    for challenge in list_challenges(ctx.chain, ctx.m1, pending_oracle=not ctx.args.all):
        cases = state.cases_of_challenge(ctx.chain, ctx.deployments, challenge.token) if ctx.deployments else []
        rows.append({
            "challenge": challenge.token.hex(), "ref": challenge.ref, "state": challenge.datum.state_name,
            "deadline": iso(challenge.deadline), "challenged_at": iso(challenge.datum.challenged_at),
            "auditor": challenge.datum.auditor_did.hex(), "claim_token": challenge.datum.claim_token.hex(),
            "evidence_uri": challenge.datum.evidence_uri.decode("utf-8", "replace"),
            "cases": [c.token.hex() for c in cases],
        })
    if ctx.args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no challenge")
    for row in rows:
        print(f"{row['challenge']} {row['state']} deadline {row['deadline']} ref {row['ref']} "
              f"cases {len(row['cases'])}")


def cmd_m1_claim(ctx: Ctx) -> None:
    from .module1 import submit_claim

    wallet = _wallet(ctx)
    claim_hash, uri = _document_args(ctx, "claim")
    result = submit_claim(ctx.m1, wallet, ctx.args.agent, lovelace_of(ctx.args.stake), claim_hash=claim_hash,
                          storage_uri=uri, claim_type=ctx.args.type.encode("utf-8"),
                          challenge_window_ms=ctx.args.window, submit=ctx.args.submit, log=_log)
    ctx.out(result)


def cmd_m1_challenge(ctx: Ctx) -> None:
    from .module1 import open_challenge

    wallet = _wallet(ctx)
    evidence_hash, uri = _document_args(ctx, "evidence")
    result = open_challenge(ctx.m1, wallet, ctx.args.agent, ctx.args.claim, stake=lovelace_of(ctx.args.stake),
                            evidence_hash=evidence_hash, evidence_uri=uri,
                            resolution_deadline_ms=ctx.args.deadline, submit=ctx.args.submit, log=_log)
    ctx.out(result)


def _claim_ref(ctx: Ctx, challenge) -> str:
    from .module1 import find_claim

    if ctx.args.claim:
        return ctx.args.claim
    claim = find_claim(ctx.chain, ctx.m1, challenge.datum.claim_token)
    if claim is None:
        raise CliError(f"no claim output holds {challenge.datum.claim_token.hex()}")
    return claim.ref


def cmd_m1_resolve(ctx: Ctx) -> None:
    from .module1 import oracle_resolve, resolution_document

    wallet = _wallet(ctx)
    challenge = _challenge(ctx)
    if ctx.args.doc:
        doc = Path(ctx.args.doc).read_bytes()
    elif ctx.args.basis == "no-time":
        doc = resolution_document(challenge, T.vote_name(ctx.args.verdict), "no-time")
    else:
        raise CliError("give --doc FILE (the resolution document), or --basis no-time")
    oracle = vd.load_signing_key(ctx.args.oracle_key) if ctx.args.oracle_key else None
    result = oracle_resolve(ctx.m1, wallet, challenge.ref, _claim_ref(ctx, challenge), T.vote_name(ctx.args.verdict),
                            resolution=doc, oracle_skey=oracle, submit=ctx.args.submit, log=_log)
    ctx.out({**result, "resolution_hash": documents.doc_hash(doc).hex()})


def cmd_m1_apply(ctx: Ctx) -> None:
    from .module1 import apply_verdict

    ctx.out(apply_verdict(ctx.m1, _wallet(ctx), ctx.args.verdict_ref, submit=ctx.args.submit, log=_log))


def cmd_m1_timeout(ctx: Ctx) -> None:
    from .module1 import timeout_resolve

    wallet = _wallet(ctx)
    challenge = _challenge(ctx)
    ctx.out(timeout_resolve(ctx.m1, wallet, challenge.ref, _claim_ref(ctx, challenge), submit=ctx.args.submit,
                            log=_log))


def _log(message: str) -> None:
    print(message, file=sys.stderr)


# ── juror ────────────────────────────────────────────────────────────────────


def juror_row(j: state.JurorOutput) -> Dict[str, Any]:
    return {
        "juror": j.token.hex(), "ref": j.ref, "jury": j.deployment.script_hash.hex(), "agent": j.agent.hex(),
        "owner": j.owner.hex(), "lovelace": j.lovelace, "prompt_hash": bytes(j.datum.prompt_hash).hex(),
        "registered_at": iso(j.datum.registered_at),
        "eligible_from": iso(rules.eligible_at(j.datum, j.deployment.params)),
        "exit_after": iso(rules.exit_allowed_at(j.datum)),
        "open": [{"case": bytes(e.case).hex(), "reveal_end": iso(e.reveal_end)} for e in j.datum.open],
    }


def cmd_juror_register(ctx: Ctx) -> None:
    owner_key = ctx.owner_key or ctx.payer_key
    owner = ctx.args.owner or (builders.key_hash(owner_key).hex() if owner_key is not None else None)
    if owner is None:
        raise CliError("give --owner HEX or a key")
    prompt = Path(ctx.args.prompt).read_bytes()
    bond = lovelace_of(ctx.args.bond) if ctx.args.bond else None
    plan = builders.build_register(ctx.chain, ctx.deployment(), ctx.payer(), agent=ctx.args.agent, owner=owner,
                                   prompt=prompt, bond=bond, prompt_media=ctx.args.media)
    show_result(ctx, ctx.finish(plan))


def cmd_juror_request_exit(ctx: Ctx) -> None:
    juror = ctx.juror(ctx.args.juror)
    show_result(ctx, ctx.finish(builders.build_request_exit(ctx.chain, juror.deployment, ctx.payer(), juror=juror)))


def cmd_juror_exit(ctx: Ctx) -> None:
    juror = ctx.juror(ctx.args.juror)
    pay_to = Address.from_primitive(ctx.args.pay_to) if ctx.args.pay_to else None
    show_result(ctx, ctx.finish(builders.build_exit(ctx.chain, juror.deployment, ctx.payer(), juror=juror,
                                                    pay_to=pay_to)))


def cmd_juror_topup(ctx: Ctx) -> None:
    juror = ctx.juror(ctx.args.juror)
    show_result(ctx, ctx.finish(builders.build_top_up(ctx.chain, juror.deployment, ctx.payer(), juror=juror,
                                                      amount=lovelace_of(ctx.args.amount))))


def cmd_juror_slash(ctx: Ctx) -> None:
    juror = ctx.juror(ctx.args.juror)
    show_result(ctx, ctx.finish(builders.build_slash(ctx.chain, juror.deployment, ctx.payer(), juror=juror,
                                                     case=ctx.args.case)))


def cmd_juror_show(ctx: Ctx) -> None:
    ctx.out(juror_row(ctx.juror(ctx.args.juror)))


def cmd_juror_list(ctx: Ctx) -> None:
    if ctx.args.agent:
        jurors = state.jurors_of_agent(ctx.chain, ctx.deployments, vi.normalize_agent_id(ctx.args.agent))
    else:
        jurors = [j for d in ctx.deployments for j in state.list_jurors(ctx.chain, d)]
    rows = [juror_row(j) for j in jurors]
    if ctx.args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no juror")
    for row in rows:
        print(f"{row['juror']} agent {row['agent']} {ap3x(row['lovelace'])} open {len(row['open'])} "
              f"exit_after {row['exit_after']}")


# ── case ─────────────────────────────────────────────────────────────────────


def _ref_text(ref: T.OutputReference) -> str:
    return f"{bytes(ref.transaction_id).hex()}#{ref.output_index}"


def case_row(c: state.CaseOutput) -> Dict[str, Any]:
    d = c.datum
    return {
        "case": c.token.hex(), "ref": c.ref, "jury": c.deployment.script_hash.hex(), "challenge": c.challenge.hex(),
        "claim": bytes(d.claim).hex(), "claimer": bytes(d.claimer).hex(), "auditor": bytes(d.auditor).hex(),
        "claim_out": _ref_text(d.claim_out), "challenge_out": _ref_text(d.challenge_out),
        "claim_hash": bytes(d.claim_hash).hex(),
        "evidence_hash": bytes(d.evidence_hash).hex(), "deadline": iso(d.deadline), "opened_at": iso(d.opened_at),
        "commit_end": iso(d.commit_end), "reveal_end": iso(d.reveal_end),
        "refund_to": str(T.address_of(d.refund_to)), "fee": d.fee, "lovelace": c.lovelace, "state": d.state_name,
        "share": getattr(d.state, "share", None), "tally": d.tally.counts(),
        "outcome": T.vote_name(rules.outcome(d.tally, c.deployment.params)),
        "ballots": [{"agent": bytes(b.agent).hex(), "juror": bytes(b.juror).hex(), "status": type(b.status).__name__,
                     "vote": T.vote_name(b.status.vote) if hasattr(b.status, "vote") else None} for b in d.ballots],
    }


def cmd_case_open(ctx: Ctx) -> None:
    refund = Address.from_primitive(ctx.args.refund_to) if ctx.args.refund_to else None
    plan = builders.build_open(ctx.chain, ctx.deployment(), ctx.payer(), challenge=_challenge(ctx).utxo,
                               claim=ctx.args.claim, fee=lovelace_of(ctx.args.fee),
                               lovelace=lovelace_of(ctx.args.lovelace) if ctx.args.lovelace else None,
                               refund_to=refund)
    show_result(ctx, ctx.finish(plan))


def cmd_case_commit(ctx: Ctx) -> None:
    case = ctx.case(ctx.args.case)
    juror = ctx.juror(ctx.args.juror)
    record = Path(ctx.args.record).read_bytes()
    store = CommitStore(ctx.args.state_dir)
    jury = case.deployment.script_hash
    if rules.ballot_of(case.datum, juror.token) is not None:
        raise CliError("the juror already has a ballot in this case; reveal it with `case reveal`")
    salt = bytes.fromhex(ctx.args.salt) if ctx.args.salt else None
    earlier = store.matching(ctx.network, jury, case.token, juror.token, ctx.args.vote, record) if salt is None else None
    attempt = earlier or make_attempt(case.token, juror.agent, ctx.args.vote, record, salt=salt,
                                      record_media=ctx.args.media, created=utc_now())
    plan = builders.build_commit(ctx.chain, case.deployment, ctx.payer(), juror=juror, case=case,
                                 vote=attempt.vote, salt=attempt.salt, record_hash=attempt.record_hash)
    if ctx.args.submit:
        path = store.add(ctx.network, jury, case.token, juror.token, attempt)
        print(f"commitment saved before the submit: {path}", file=sys.stderr)
    result = ctx.finish(plan)
    if ctx.args.submit:
        store.mark_sent(ctx.network, jury, case.token, juror.token, attempt.commit, result.tx_id)
    show_result(ctx, result, {"state_file": str(store.file(ctx.network, jury, case.token, juror.token))
                              if ctx.args.submit else None})


def cmd_case_reveal(ctx: Ctx) -> None:
    case = ctx.case(ctx.args.case)
    juror = ctx.juror(ctx.args.juror)
    found = rules.ballot_of(case.datum, juror.token)
    if found is None:
        raise CliError("the juror has no ballot in this case")
    store = CommitStore(ctx.args.state_dir)
    attempt = store.find(ctx.network, case.deployment.script_hash, case.token, juror.token, found[1].commit)
    if attempt is None:
        raise CliError(f"no stored attempt has the commitment {bytes(found[1].commit).hex()} "
                       f"({store.file(ctx.network, case.deployment.script_hash, case.token, juror.token)})")
    plan = builders.build_reveal(ctx.chain, case.deployment, ctx.payer(), juror=juror, case=case,
                                 vote=attempt.vote, salt=attempt.salt, record=attempt.record,
                                 record_media=attempt.record_media)
    show_result(ctx, ctx.finish(plan))


def cmd_case_finalize(ctx: Ctx) -> None:
    case = ctx.case(ctx.args.case)
    show_result(ctx, ctx.finish(builders.build_finalize(ctx.chain, case.deployment, ctx.payer(), case=case)))


def cmd_case_claim(ctx: Ctx) -> None:
    case = ctx.case(ctx.args.case)
    juror = ctx.juror(ctx.args.juror)
    show_result(ctx, ctx.finish(builders.build_claim(ctx.chain, case.deployment, ctx.payer(), juror=juror,
                                                     case=case)))


def cmd_case_close(ctx: Ctx) -> None:
    case = ctx.case(ctx.args.case)
    show_result(ctx, ctx.finish(builders.build_close(ctx.chain, case.deployment, ctx.payer(), case=case)))


def cmd_case_show(ctx: Ctx) -> None:
    ctx.out(case_row(ctx.case(ctx.args.case)))


def cmd_case_list(ctx: Ctx) -> None:
    if ctx.args.canonical:
        cases = list(state.canonical_cases(ctx.chain, ctx.deployments).values())
    else:
        cases = [c for d in ctx.deployments for c in state.list_cases(ctx.chain, d)]
    if ctx.args.challenge:
        cases = [c for c in cases if c.challenge == ctx.args.challenge]
    rows = [case_row(c) for c in cases]
    if ctx.args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no case")
    for row in rows:
        print(f"{row['case']} challenge {row['challenge']} {row['state']} commit_end {row['commit_end']} "
              f"reveal_end {row['reveal_end']} tally {row['tally']}")


# ── parser ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vector-jury", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", action="append", type=Path,
                        help="jury deployment manifest (repeatable; default deploy/<network>/manifest.json)")
    parser.add_argument("--jury", help="script hash of the deployment for new jurors and cases (default: the first)")
    parser.add_argument("--module1", type=Path, default=MODULE1_MANIFEST, help="Module-1 deployment manifest")
    parser.add_argument("--network", choices=("mainnet", "testnet"), help="default: the network of the manifests")
    parser.add_argument("--key", type=Path, help="fee payer signing key file")
    parser.add_argument("--owner-key", type=Path, help="juror owner signing key file (when not the fee payer)")
    parser.add_argument("--submit", action="store_true", help="sign and send (default: dry run)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--save-tx", metavar="FILE", help="write the transaction CBOR (hex) to FILE, for a decode "
                                                         "check before --submit")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="commit attempts (salts and records); default ~/.vector-jury/commits")
    parser.add_argument("--ogmios-url", help="default: $VECTOR_OGMIOS_URL or the network preset")
    parser.add_argument("--koios-url", help="default: $VECTOR_KOIOS_URL or the network preset")
    groups = parser.add_subparsers(dest="group", required=True)

    def command(group, name, func, help_text):
        sub = group.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(func=func)
        return sub

    doc = groups.add_parser("doc", help="documents on chain (label 22084)").add_subparsers(dest="command", required=True)
    p = command(doc, "publish", cmd_doc_publish, "publish a document in a transaction of its own")
    p.add_argument("file")
    p.add_argument("--type", required=True, choices=documents.TYPES)
    p.add_argument("--media", default="application/json")
    p = command(doc, "get", cmd_doc_get, "read a document (vector-tx: or https:) and check its hash")
    p.add_argument("uri")
    p.add_argument("--hash", help="expected blake2b-256 (hex)")
    p.add_argument("--out", help="write the document to this file (default: stdout)")

    m1 = groups.add_parser("m1", help="Module-1 claims, challenges and resolutions").add_subparsers(
        dest="command", required=True)
    p = command(m1, "list", cmd_m1_list, "list Module-1 challenges (PendingOracle by default)")
    p.add_argument("--all", action="store_true", help="all challenge states")
    p = command(m1, "claim", cmd_m1_claim, "submit a claim (the claim document is published first)")
    p.add_argument("--agent", required=True, help="claimer DID or agent id")
    p.add_argument("--stake", required=True, help="claim stake in AP3X")
    p.add_argument("--doc", help="claim document file (published on chain)")
    p.add_argument("--hash", help="claim hash (hex) of a document published elsewhere")
    p.add_argument("--uri", help="URI of that document")
    p.add_argument("--type", default="service-quality", help="claim type")
    p.add_argument("--window", type=int, help="challenge window in ms")
    p = command(m1, "challenge", cmd_m1_challenge, "open a challenge (oracle mode; the evidence is published first)")
    p.add_argument("--agent", required=True, help="auditor DID or agent id")
    p.add_argument("--claim", required=True, help="claim output <tx id>#<index>")
    p.add_argument("--stake", required=True, help="auditor stake in AP3X")
    p.add_argument("--doc", help="evidence document file (published on chain)")
    p.add_argument("--hash", help="evidence hash (hex) of a document published elsewhere")
    p.add_argument("--uri", help="URI of that document")
    p.add_argument("--deadline", type=int, help="resolution deadline in ms (default: the Module-1 parameter)")
    p = command(m1, "resolve", cmd_m1_resolve, "OracleResolve with a resolution document (oracle key)")
    p.add_argument("--challenge", required=True, help="challenge token (64 hex) or output <tx id>#<index>")
    p.add_argument("--claim", help="claim output (default: the claim of the challenge)")
    p.add_argument("--verdict", required=True, choices=T.VOTE_NAMES)
    p.add_argument("--doc", help="resolution document file")
    p.add_argument("--basis", choices=("no-time",), help="write the no-time resolution document")
    p.add_argument("--oracle-key", type=Path, help="oracle signing key (default: --key)")
    p = command(m1, "apply", cmd_m1_apply, "ApplyVerdict of an identity verdict output")
    p.add_argument("--verdict-ref", required=True, help="verdict output <tx id>#<index>")
    p = command(m1, "timeout", cmd_m1_timeout, "TimeoutResolve after the deadline")
    p.add_argument("--challenge", required=True, help="challenge token (64 hex) or output <tx id>#<index>")
    p.add_argument("--claim", help="claim output (default: the claim of the challenge)")

    juror = groups.add_parser("juror", help="juror outputs").add_subparsers(dest="command", required=True)
    p = command(juror, "register", cmd_juror_register, "register a juror (the prompt document goes in the metadata)")
    p.add_argument("--agent", required=True, help="juror DID or agent id")
    p.add_argument("--prompt", required=True, help="prompt document file")
    p.add_argument("--owner", help="owner key hash (default: --owner-key, else --key)")
    p.add_argument("--bond", help="bond in AP3X (default: bond_min)")
    p.add_argument("--media", default="application/json")
    p = command(juror, "request-exit", cmd_juror_request_exit, "request the exit (exit notice)")
    p.add_argument("--juror", required=True, type=token_arg)
    p = command(juror, "exit", cmd_juror_exit, "exit after the notice: burn the token, take the bond")
    p.add_argument("--juror", required=True, type=token_arg)
    p.add_argument("--pay-to", help="address for the juror lovelace (default: the change of --key)")
    p = command(juror, "topup", cmd_juror_topup, "add lovelace to a juror output")
    p.add_argument("--juror", required=True, type=token_arg)
    p.add_argument("--amount", required=True, help="AP3X")
    p = command(juror, "slash", cmd_juror_slash, "slash a juror that did not reveal (anybody)")
    p.add_argument("--juror", required=True, type=token_arg)
    p.add_argument("--case", required=True, type=token_arg)
    p = command(juror, "show", cmd_juror_show, "show a juror output")
    p.add_argument("--juror", required=True, type=token_arg)
    p = command(juror, "list", cmd_juror_list, "list the juror outputs")
    p.add_argument("--agent", help="only this DID or agent id")

    case = groups.add_parser("case", help="jury cases").add_subparsers(dest="command", required=True)
    p = command(case, "open", cmd_case_open, "open a case for a PendingOracle challenge")
    p.add_argument("--challenge", required=True, help="challenge token (64 hex) or output <tx id>#<index>")
    p.add_argument("--claim", help="claim output (default: the claim of the challenge)")
    p.add_argument("--fee", default="0", help="fee for the revealing jurors in AP3X")
    p.add_argument("--lovelace", help="AP3X in the case output (default: 10, or the fee plus the ledger minimum)")
    p.add_argument("--refund-to", help="address for the rest of the case lovelace (default: the fee payer)")
    p = command(case, "commit", cmd_case_commit, "commit a vote (the salt and the record are saved first)")
    p.add_argument("--case", required=True, type=token_arg)
    p.add_argument("--juror", required=True, type=token_arg)
    p.add_argument("--vote", required=True, choices=T.VOTE_NAMES)
    p.add_argument("--record", required=True, help="juror record document file")
    p.add_argument("--media", default="application/json")
    p.add_argument("--salt", help="32-byte salt (hex; default: random, or the salt of the same earlier attempt)")
    p = command(case, "reveal", cmd_case_reveal, "reveal the saved vote with its record")
    p.add_argument("--case", required=True, type=token_arg)
    p.add_argument("--juror", required=True, type=token_arg)
    p = command(case, "finalize", cmd_case_finalize, "finalize a case after the reveal window (anybody)")
    p.add_argument("--case", required=True, type=token_arg)
    p = command(case, "claim", cmd_case_claim, "claim the fee share of a revealed ballot")
    p.add_argument("--case", required=True, type=token_arg)
    p.add_argument("--juror", required=True, type=token_arg)
    p = command(case, "close", cmd_case_close, "close a Settling case (anybody)")
    p.add_argument("--case", required=True, type=token_arg)
    p = command(case, "show", cmd_case_show, "show a case output")
    p.add_argument("--case", required=True, type=token_arg)
    p = command(case, "list", cmd_case_list, "list the case outputs")
    p.add_argument("--challenge", type=token_arg, help="only the cases of this challenge token")
    p.add_argument("--canonical", action="store_true", help="only the canonical case of each challenge")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = Ctx(args)
        ctx.guard()
        if ctx.payer_key is not None:
            print("fee payer: " + json.dumps(key_info(ctx.payer_key)), file=sys.stderr)
        if ctx.owner_key is not None:
            print("owner: " + json.dumps(key_info(ctx.owner_key)), file=sys.stderr)
        args.func(ctx)
    except (CliError, BuildError, ChainError, DeploymentError, documents.DocumentError, Module1Error,
            vd.DeployError, ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
