"""
Module 6: Treasury Funding Pipeline

Creates new treasury batch UTxOs at the treasury holder of the deployment.
Each batch holds --size AP3X and TreasuryBatchDatum(id, True); an adoption of
a proposal can spend it. The batch ids follow the treasury_batch_<id> keys of
the deploy state (scripts/deploy.py makes batches 1 to 3, so the first id is
4 or more). Each batch is one transaction, step treasury-<id>, with the batch
as output 0.

Usage:
    # Dry run of 3 new batches of 500 AP3X each (1,500 AP3X in total):
    python3 scripts/treasury_fund.py --vkey operator.vkey --batches 3 --size 500

    # Send them (sign with the operator key):
    python3 scripts/treasury_fund.py --skey operator.skey --batches 3 --size 500 --submit

    python3 scripts/treasury_fund.py [run|build|sign|submit] (--vkey F | --skey F)
            [--batches N] [--size AP3X] [--submit] [--dry-run-funds AP3X]
            [--state-dir DIR] [--network mainnet|testnet] [--report F]

"run" is a dry run (build each transaction, evaluate it with Ogmios, send
nothing), or a send with --submit (sign with --skey). "build" writes the
unsigned transactions, "sign" signs them offline with --skey, "submit" sends
the signed ones. The tool reads the deploy state of scripts/deploy.py
(default deploy/identity/state/<network>/deploy_state.json) and records
tx_hashes["treasury_batch_<id>"] in it.

Prerequisites:
    - The deploy state of scripts/deploy.py (the plan is in it after a build
      or a send).
    - The operator address holds the batches, the fees and some change.
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

import requests

import deploy
from deploy_common import DeployInputError, treasury_batch_id

import vector_deploy as vd  # noqa: E402  (shared/python, on sys.path through deploy_common)

DEFAULT_BATCHES = 1
DEFAULT_BATCH_SIZE_APEX = 500


def next_batch_ids(tx_hashes: Mapping[str, str], count: int) -> list[int]:
    """`count` batch ids after the treasury_batch_<id> keys of the state. The
    ids of deploy.py (1 to TREASURY_BATCHES) stay free for it."""
    existing = [int(key[len("treasury_batch_"):]) for key in tx_hashes
                if key.startswith("treasury_batch_") and key[len("treasury_batch_"):].isdigit()]
    first = max(existing + [deploy.TREASURY_BATCHES]) + 1
    return list(range(first, first + count))


def is_fund_step(name: str) -> bool:
    """The steps of this tool: treasury-<id> after the batches of deploy.py."""
    batch_id = treasury_batch_id(name)
    return batch_id is not None and batch_id > deploy.TREASURY_BATCHES


def command_fund(args, *, chain: Optional[vd.DeployChain] = None, post: Callable = requests.post,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> None:
    state_dir: Path = args.state_dir or deploy.default_state_dir(args.network)
    state = vd.DeployState.load(state_dir / deploy.STATE_FILE, module=deploy.MODULE, network=args.network)
    deploy.require_state(state)
    operator = vd.operator_key_hash_from_args(args)
    deploy.check_owner(state, operator)
    if args.action == "sign":
        vd.sign_pending(state, vd.load_signing_key(args.skey), operator, log)
        return
    treasury = state.data["holders"]["treasury"]["address"]
    sending = args.submit or args.action in ("build", "submit")
    if args.action == "submit":
        steps = [deploy.treasury_step(state, treasury, treasury_batch_id(name), int(entry.get("lovelace") or 0))
                 for name, entry in state.pending.items() if is_fund_step(name)]
        batch_lovelace = None
    else:
        if args.batches < 1:
            raise DeployInputError("--batches must be 1 or more")
        batch_lovelace = deploy.lovelace(args.size)
        if batch_lovelace <= 0:
            raise DeployInputError("--size must be more than 0 AP3X")
        ids = next_batch_ids(state.data.get("tx_hashes") or {}, args.batches)
        steps = [deploy.treasury_step(state, treasury, batch_id, batch_lovelace) for batch_id in ids]
    if args.action == "build":
        deploy.check_pending(state, is_fund_step)

    chain = chain if chain is not None else deploy.make_chain(args)
    chain.check_network()
    protected, protected_addresses = deploy.protected_inputs(state)
    run = vd.Run(chain, operator, protected=protected, protected_addresses=protected_addresses,
                 dry_run_funds=deploy.lovelace(args.dry_run_funds), log=log)
    run.labels[treasury] = "the Module-6 treasury holder"

    mode = "dry run" if not sending else args.action if args.action != "run" else "send"
    log(f"Module-6 treasury funding on {chain.preset.name} ({mode})")
    log(f"  treasury holder {treasury}")
    if batch_lovelace is not None:
        log(f"  {len(steps)} batch(es) of {batch_lovelace / vd.LOVELACE_PER_AP3X:,.6f} AP3X: ids "
            + ", ".join(step.name.split("-", 1)[1] for step in steps))
    log("")
    built = vd.execute(
        args, run, steps, state,
        tx_dir=state_dir / "tx",
        header={"module": deploy.MODULE, "tool": "treasury_fund", "network": chain.preset.name, "mode": mode,
                "time": vd.utc_now(), "treasury_address": treasury, "batch_lovelace": batch_lovelace},
        after=lambda: deploy.update_state(state),
        post=post, sleep=sleep,
    )
    if args.action == "build":
        for b in built:
            state.pending[b.step.name]["lovelace"] = batch_lovelace
        state.save()
    if sending:
        deploy.update_state(state)
        log(f"\nState: {state.path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    vd.add_run_arguments(parser)
    parser.add_argument("--batches", type=int, default=DEFAULT_BATCHES, help="number of batches to create")
    parser.add_argument("--size", type=float, default=DEFAULT_BATCH_SIZE_APEX, metavar="AP3X",
                        help="AP3X in each batch")
    parser.add_argument("--state-dir", type=Path, help="default: deploy/identity/state/<network>")
    return parser


def main(argv: Optional[Iterable[str]] = None, *, chain: Optional[vd.DeployChain] = None,
         post: Callable = requests.post, sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    try:
        command_fund(args, chain=chain, post=post, sleep=sleep)
    except (vd.DeployError, DeployInputError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
