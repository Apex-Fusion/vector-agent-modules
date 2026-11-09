"""The jury service: the relay (SPEC §12) and the reference jurors (SPEC §13)
in one process.

    python -m vector_jury.service --config config.json [--once] [--dry-run]

The relay and the jurors share the operator key wallet (fee payer, juror
owner, oracle key, refund address), so every transaction goes through one
queue (``TxQueue``): one transaction at a time, and the queue waits until the
chain shows an output of a submitted transaction (at most 3 minutes) before
the next one starts. Commits, reveals, resolutions, TimeoutResolve and Open
go first. Model calls run in worker threads, outside the queue.

Each loop (``poll_seconds``) reads the chain once (``relay.observe``), runs
the relay, then starts the jobs of each juror: a commit job runs in a worker
thread (one per juror and case), reveals run in the loop. ``--once`` runs
one loop and waits for its jobs. ``--dry-run`` (or ``"dry_run": true``)
builds and evaluates transactions but submits nothing, makes no paid model
call and writes no state; without a readable key it only reports decisions.

Logs are JSON lines on stdout: ``{"time", "event", ...}``. Keys and API keys
never appear in a log line.

Config file (JSON, ``vector-jury-service/1``; relative paths are relative to
the config file)::

    {
      "format": "vector-jury-service/1",
      "network": "mainnet",
      "ogmios_url": null, "koios_url": null,
      "module1_manifest": ".../Module-1/deploy/identity/mainnet/manifest.json",
      "jury_manifests": [".../Module-1/jury/deploy/mainnet/manifest.json"],
      "operator_key": "/run/secrets/operator.skey",
      "operator_vkh": "470ded7c9d487c2c3832af84aea70987b2c9a01480192ebbf035d028",
      "state_dir": "/var/lib/vector-jury",
      "poll_seconds": 60,
      "relay": true,
      "dry_run": false,
      "gateway_url": "https://api.marketplace.vector.apexfusion.org",
      "jurors_file": ".../Module-1/jury/jurors/foundation/jurors.json",
      "jurors": ["ds-flash-chat-1", "..."]
    }

``jurors`` selects jurors of ``jurors_file`` by name (null: all, []: none).
Each juror reads its gateway API key from the environment variable named by
``api_key_env`` in the juror list.
"""

import argparse
import concurrent.futures
import dataclasses
import heapq
import itertools
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import _paths  # noqa: F401
from . import builders, gateway, juror, module1, relay
from .chain import Chain
from .commit_store import CommitStore
from .deployment import load_manifests

CONFIG_FORMAT = "vector-jury-service/1"
#: The longest wait for a submitted transaction (seconds).
CONFIRM_TIMEOUT = 180
CONFIRM_POLL = 5


class ConfigError(ValueError):
    """The service configuration is not valid."""


# ── Logging ──────────────────────────────────────────────────────────────────


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class JsonLog:
    """JSON lines on a stream (stdout), one object per event."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()

    def __call__(self, event: str, **fields) -> None:
        line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event,
                           **_jsonable(fields)}, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()


# ── Transaction queue ────────────────────────────────────────────────────────


class TxQueue:
    """Serializes the transactions of the process: one ``build(submit)`` at a
    time, lower ``priority`` first, then first come. After a submitted
    transaction the queue waits until the chain shows ``Sent.wait_ref`` (at
    most ``timeout`` seconds), so the next transaction sees its outputs."""

    def __init__(self, chain: Chain, *, dry_run: bool, log: Callable[..., None], timeout: float = CONFIRM_TIMEOUT,
                 poll: float = CONFIRM_POLL, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.chain = chain
        self.dry_run = dry_run
        self.log = log
        self.timeout = timeout
        self.poll = poll
        self.sleep = sleep
        self.clock = clock
        self._cond = threading.Condition()
        self._waiting: List = []
        self._busy = False
        self._count = itertools.count()

    def submit(self, label: str, build: Callable[[bool], Optional[relay.Sent]], *,
               priority: int = 1) -> Optional[relay.Sent]:
        ticket = (priority, next(self._count))
        with self._cond:
            heapq.heappush(self._waiting, ticket)
            while self._busy or self._waiting[0] != ticket:
                self._cond.wait()
            heapq.heappop(self._waiting)
            self._busy = True
        try:
            sent = build(not self.dry_run)
            if sent is not None and sent.submitted and sent.wait_ref:
                sent.confirmed = self._wait(label, sent)
            return sent
        finally:
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def _wait(self, label: str, sent: relay.Sent) -> bool:
        deadline = self.clock() + self.timeout
        while True:
            try:
                if self.chain.utxo(sent.wait_ref) is not None:
                    return True
            except Exception as error:  # noqa: BLE001 - keep waiting on a read error
                self.log("tx.wait_error", label=label, tx=sent.tx_id, error=str(error)[:300])
            if self.clock() >= deadline:
                self.log("tx.unconfirmed", label=label, tx=sent.tx_id, wait_ref=sent.wait_ref,
                         seconds=self.timeout)
                return False
            self.sleep(self.poll)


# ── Configuration and context ────────────────────────────────────────────────


@dataclasses.dataclass
class Config:
    path: Path
    network: str
    module1_manifest: Path
    jury_manifests: List[Path]
    operator_key: Optional[Path]
    operator_vkh: bytes
    state_dir: Path
    poll_seconds: float
    relay: bool
    dry_run: bool
    gateway_url: str
    jurors_file: Optional[Path]
    jurors: Optional[List[str]]
    ogmios_url: Optional[str] = None
    koios_url: Optional[str] = None


def _path(base: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path) -> Config:
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from None
    if data.get("format") != CONFIG_FORMAT:
        raise ConfigError(f"{path} is not a {CONFIG_FORMAT} file")
    base = path.resolve().parent
    for key in ("network", "module1_manifest", "jury_manifests", "operator_vkh", "state_dir"):
        if key not in data:
            raise ConfigError(f"{path}: {key} is missing")
    try:
        vkh = bytes.fromhex(str(data["operator_vkh"]))
    except ValueError:
        vkh = b""
    if len(vkh) != 28:
        raise ConfigError("operator_vkh must be 56 hex characters")
    names = data.get("jurors")
    if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
        raise ConfigError("jurors must be a list of juror names or null")
    poll = float(data.get("poll_seconds", 60))
    if poll < 5:
        raise ConfigError("poll_seconds must be at least 5")
    return Config(
        path=path, network=str(data["network"]), module1_manifest=_path(base, data["module1_manifest"]),
        jury_manifests=[_path(base, p) for p in data["jury_manifests"]],
        operator_key=_path(base, data["operator_key"]) if data.get("operator_key") else None,
        operator_vkh=vkh, state_dir=_path(base, data["state_dir"]), poll_seconds=poll,
        relay=bool(data.get("relay", True)), dry_run=bool(data.get("dry_run", False)),
        gateway_url=str(data.get("gateway_url") or gateway.GATEWAY_URL),
        jurors_file=_path(base, data["jurors_file"]) if data.get("jurors_file") else None,
        jurors=names, ogmios_url=data.get("ogmios_url"), koios_url=data.get("koios_url"))


@dataclasses.dataclass
class Context:
    """What the relay and the jurors share (see ``relay.py``, ``juror.py``)."""

    chain: Chain
    m1: module1.Module1
    deployments: list
    address: Any
    operator_vkh: bytes
    signing_key: Any
    dry_run: bool
    txs: TxQueue
    log: Callable[..., None]
    foundation_agents: List[bytes]
    state_dir: Path
    gateway_url: str
    supplier_keys: gateway.SupplierKeys
    commits: CommitStore
    genuine_cache: Dict[str, Optional[bytes]]


def load_signing_key(config: Config, log: Callable[..., None]):
    """The operator signing key, checked against ``operator_vkh``. In a dry
    run a missing or unreadable key gives None (decisions only)."""
    from pycardano import PaymentSigningKey, PaymentVerificationKey

    if config.operator_key is None:
        if config.dry_run:
            return None
        raise ConfigError("operator_key is missing")
    try:
        key = PaymentSigningKey.load(str(config.operator_key))
    except Exception as error:  # noqa: BLE001 - never print the key file
        if config.dry_run:
            log("service.no_key", reason=f"the operator key cannot be read ({type(error).__name__}); "
                                         "Module-1 transactions are reported as decisions only")
            return None
        raise ConfigError(f"cannot read the operator key file: {type(error).__name__}") from None
    if bytes(PaymentVerificationKey.from_signing_key(key).hash().payload) != config.operator_vkh:
        raise ConfigError("the operator key does not match operator_vkh")
    return key


def build_context(config: Config, log: Callable[..., None], specs: List[juror.JurorSpec]) -> Context:
    chain = Chain(config.network, ogmios_url=config.ogmios_url, koios_url=config.koios_url)
    chain.check_network()
    m1 = module1.Module1.load(config.module1_manifest)
    if m1.network != config.network:
        raise ConfigError(f"the Module-1 manifest is for {m1.network}, the service for {config.network}")
    deployments = load_manifests(config.jury_manifests)
    if config.relay and not deployments:
        raise ConfigError("the relay needs at least one jury manifest (else every challenge would be no-jury)")
    if any(d.network != config.network for d in deployments):
        raise ConfigError("a jury manifest is for another network")
    if config.relay and m1.oracle_vkh != config.operator_vkh:
        raise ConfigError("operator_vkh is not the oracle key of the Module-1 deployment")
    key = load_signing_key(config, log)
    return Context(chain=chain, m1=m1, deployments=deployments, address=builders.key_address(config.operator_vkh),
                   operator_vkh=config.operator_vkh, signing_key=key, dry_run=config.dry_run,
                   txs=TxQueue(chain, dry_run=config.dry_run, log=log), log=log,
                   foundation_agents=[s.agent for s in specs], state_dir=config.state_dir,
                   gateway_url=config.gateway_url, supplier_keys=gateway.SupplierKeys(),
                   commits=CommitStore(config.state_dir / "commits"), genuine_cache={})


def load_juror_specs(config: Config) -> List[juror.JurorSpec]:
    if config.jurors_file is None or config.jurors == []:
        return []
    owner, specs = juror.load_jurors(config.jurors_file, names=config.jurors)
    if owner != config.operator_vkh:
        raise ConfigError("the juror list has another owner key than operator_vkh")
    return specs


# ── The loop ─────────────────────────────────────────────────────────────────


class Service:
    def __init__(self, ctx: Context, config: Config, specs: List[juror.JurorSpec]):
        self.ctx = ctx
        self.config = config
        self.caches = relay.Caches(genuine=ctx.genuine_cache)
        self.journal = relay.Journal(config.state_dir / "relay" / "txlog.jsonl")
        self.relay = relay.Relay(ctx, journal=self.journal, caches=self.caches) if config.relay else None
        self.jurors = [juror.Juror(ctx, spec) for spec in specs]
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, 2 * len(self.jurors)),
                                                          thread_name_prefix="juror")
        self.running: Dict[tuple, concurrent.futures.Future] = {}
        self.stop = threading.Event()

    def loop_once(self, *, inline: bool = False) -> None:
        log = self.ctx.log
        try:
            world = relay.observe(self.ctx, self.caches, self.journal)
        except Exception as error:  # noqa: BLE001 - the next loop tries again
            log("service.observe_failed", error=f"{type(error).__name__}: {error}")
            return
        log("service.world", now_ms=world.now_ms, tip_slot=world.tip_slot, block_height=world.block_height,
            disputes=[{**d.describe(), "canonical": (d.canonical_case_token() or b"").hex() or None,
                       "case_known": d.case_known, "error": d.error} for d in world.disputes],
            cases=len(world.cases), jurors=len(world.jurors), verdicts=len(world.verdicts),
            dry_run=self.ctx.dry_run)
        if self.relay is not None:
            try:
                self.relay.run_once(world)
            except Exception as error:  # noqa: BLE001
                log("relay.failed", error=f"{type(error).__name__}: {error}")
        for member in self.jurors:
            try:
                jobs, notes = member.due(world)
            except Exception as error:  # noqa: BLE001
                log("juror.failed", juror=member.spec.name, error=f"{type(error).__name__}: {error}")
                continue
            log("juror.loop", juror=member.spec.name,
                jobs=[{"kind": j.kind, "case": j.case.token.hex()} for j in jobs], skipped=len(notes))
            for note in notes:
                log("juror.note", juror=member.spec.name, **note)
            for job in jobs:
                self._start(member, job, inline=inline)
        self.running = {k: f for k, f in self.running.items() if not f.done()}

    def _run_job(self, member: juror.Juror, job: juror.Job) -> None:
        try:
            result = member.commit(job) if job.kind == "commit" else member.reveal(job)
            self.ctx.log(f"juror.{job.kind}", **result)
        except Exception as error:  # noqa: BLE001 - a failed job is logged; the next loop decides again
            self.ctx.log(f"juror.{job.kind}_failed", juror=member.spec.name, case=job.case.token.hex(),
                         error=f"{type(error).__name__}: {error}")

    def _start(self, member: juror.Juror, job: juror.Job, *, inline: bool) -> None:
        key = (member.spec.name, job.case.token)
        current = self.running.get(key)
        if current is not None and not current.done():
            return
        if inline:
            self._run_job(member, job)
            return
        self.running[key] = self.pool.submit(self._run_job, member, job)

    def run(self, *, once: bool) -> None:
        log = self.ctx.log
        log("service.start", network=self.config.network, relay=self.relay is not None,
            jurors=[m.spec.name for m in self.jurors], deployments=[d.script_hash.hex() for d in self.ctx.deployments],
            dry_run=self.ctx.dry_run, address=str(self.ctx.address), signing_key=self.ctx.signing_key is not None)
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                self.loop_once(inline=once)
                if once:
                    break
                self.stop.wait(max(1.0, self.config.poll_seconds - (time.monotonic() - started)))
        finally:
            log("service.stopping", jobs=len([f for f in self.running.values() if not f.done()]))
            self.pool.shutdown(wait=True)
            log("service.stopped")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vector_jury.service",
                                     description="The Module-1 jury relay and reference jurors (SPEC §12, §13).")
    parser.add_argument("--config", required=True, help="service config file (vector-jury-service/1)")
    parser.add_argument("--once", action="store_true", help="run one loop and wait for its jobs")
    parser.add_argument("--dry-run", action="store_true", help="build and evaluate, submit nothing, no paid call")
    args = parser.parse_args(argv)
    log = JsonLog()
    try:
        config = load_config(args.config)
        if args.dry_run:
            config.dry_run = True
        if config.network == "mainnet" and not config.dry_run and os.environ.get("APEX_NETWORK_CONFIRM") != "yes":
            raise ConfigError("a mainnet run that submits needs APEX_NETWORK_CONFIRM=yes")
        specs = load_juror_specs(config)
        ctx = build_context(config, log, specs)
    except (ConfigError, juror.ConfigError, ValueError, OSError) as error:
        log("service.config_error", error=str(error))
        return 2
    service = Service(ctx, config, specs)

    def stop(signum, _frame):
        log("service.signal", signal=signum)
        service.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    service.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
