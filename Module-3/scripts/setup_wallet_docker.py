#!/usr/bin/env python3
"""
Module 3: Docker Wallet Setup

Generates a dev wallet (payment keys + address) inside the Docker container
at /tmp/m3dev/, then funds it from the fee wallet.

Usage:
    python3 scripts/setup_wallet_docker.py
"""

import json
import subprocess
import sys
import time

DOCKER_CONTAINER = "vector-public-testnet-tools-10_1_4-vector-relay-1"
SOCKET_PATH = "ipc/node.socket"
NETWORK_FLAG = "--mainnet"
DEV_WALLET_DIR = "/tmp/m3dev"
FEE_WALLET_DIR = "/tmp/fee"  # Must already exist with keys
FUND_AMOUNT = 500_000_000_000  # 500,000 AP3X


def docker_exec(cmd: str, check: bool = True) -> str:
    full_cmd = ["docker", "exec", DOCKER_CONTAINER, "bash", "-c", cmd]
    result = subprocess.run(full_cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        raise RuntimeError(f"Docker command failed: {cmd[:120]}...")
    return result.stdout.strip()


def cardano_cli(args: str) -> str:
    needs_socket = any(
        kw in args for kw in ["query ", "transaction build ", "transaction submit "]
    )
    socket_arg = f" --socket-path {SOCKET_PATH}" if needs_socket else ""
    return docker_exec(f"cardano-cli {args}{socket_arg}")


def main():
    print("=" * 60)
    print("Module 3: Dev Wallet Setup (Docker)")
    print("=" * 60)

    # Check Docker container is running
    try:
        docker_exec("echo ok")
    except RuntimeError:
        print(f"ERROR: Docker container '{DOCKER_CONTAINER}' not running")
        sys.exit(1)

    # Check node is synced
    tip = json.loads(cardano_cli(f"conway query tip {NETWORK_FLAG}"))
    print(f"Node: slot {tip['slot']}, sync {tip['syncProgress']}")

    # Check if wallet already exists
    existing = docker_exec(f"test -f {DEV_WALLET_DIR}/payment.addr && echo yes || echo no")
    if existing == "yes":
        addr = docker_exec(f"cat {DEV_WALLET_DIR}/payment.addr")
        print(f"\nWallet already exists at {DEV_WALLET_DIR}")
        print(f"Address: {addr}")

        # Check balance
        utxos = json.loads(cardano_cli(
            f"conway query utxo {NETWORK_FLAG} --address {addr} --out-file /dev/stdout"
        ))
        total = sum(d["value"]["lovelace"] for d in utxos.values())
        print(f"Balance: {total / 1_000_000:.2f} AP3X ({len(utxos)} UTXOs)")

        if total > 0:
            print("Wallet is already funded. Nothing to do.")
            return
        print("Wallet exists but is empty. Funding...")
    else:
        # Generate keys
        print(f"\nGenerating wallet at {DEV_WALLET_DIR}...")
        docker_exec(f"mkdir -p {DEV_WALLET_DIR}")

        cardano_cli(
            f"conway address key-gen "
            f"--verification-key-file {DEV_WALLET_DIR}/payment.vkey "
            f"--signing-key-file {DEV_WALLET_DIR}/payment.skey"
        )

        cardano_cli(
            f"conway address build {NETWORK_FLAG} "
            f"--payment-verification-key-file {DEV_WALLET_DIR}/payment.vkey "
            f"--out-file {DEV_WALLET_DIR}/payment.addr"
        )

        addr = docker_exec(f"cat {DEV_WALLET_DIR}/payment.addr")
        print(f"Address: {addr}")

    # Fund from fee wallet
    print(f"\nFunding from fee wallet ({FUND_AMOUNT / 1_000_000:.0f} AP3X)...")

    fee_addr = docker_exec(f"cat {FEE_WALLET_DIR}/payment.addr")
    fee_utxos = json.loads(cardano_cli(
        f"conway query utxo {NETWORK_FLAG} --address {fee_addr} --out-file /dev/stdout"
    ))
    fee_total = sum(d["value"]["lovelace"] for d in fee_utxos.values())
    print(f"Fee wallet balance: {fee_total / 1_000_000:.2f} AP3X")

    if fee_total < FUND_AMOUNT + 5_000_000:
        print("ERROR: Fee wallet has insufficient funds")
        sys.exit(1)

    # Pick best UTXO from fee wallet
    best_txin = max(fee_utxos.items(), key=lambda x: x[1]["value"]["lovelace"])[0]

    cardano_cli(
        f"conway transaction build {NETWORK_FLAG} "
        f"--tx-in {best_txin} "
        f"--tx-out '{addr}+{FUND_AMOUNT}' "
        f"--change-address {fee_addr} "
        f"--out-file /tmp/m3_fund.raw"
    )

    cardano_cli(
        f"conway transaction sign {NETWORK_FLAG} "
        f"--tx-body-file /tmp/m3_fund.raw "
        f"--signing-key-file {FEE_WALLET_DIR}/payment.skey "
        f"--out-file /tmp/m3_fund.signed"
    )

    cardano_cli(f"conway transaction submit {NETWORK_FLAG} --tx-file /tmp/m3_fund.signed")
    tx_hash = docker_exec("cardano-cli conway transaction txid --tx-file /tmp/m3_fund.signed")
    print(f"Fund TX: {tx_hash}")

    print("Waiting 25s for confirmation...")
    time.sleep(25)

    # Verify
    utxos = json.loads(cardano_cli(
        f"conway query utxo {NETWORK_FLAG} --address {addr} --out-file /dev/stdout"
    ))
    total = sum(d["value"]["lovelace"] for d in utxos.values())
    print(f"\nDev wallet funded: {total / 1_000_000:.2f} AP3X")
    print("Done!")


if __name__ == "__main__":
    main()
