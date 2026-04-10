"""
Docker/cardano-cli chain backend for Module 3.

Runs cardano-cli commands inside the Vector testnet Docker container.
Extracted from smoke_test_docker.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional, Set, Tuple

from reputation_staking.constants import (
    DEV_WALLET_DIR,
    DOCKER_CONTAINER,
    DOCKER_SOCKET_PATH,
    NETWORK_FLAG,
)


class DockerChainBackend:
    """Chain backend that runs cardano-cli inside a Docker container."""

    def __init__(
        self,
        container: str = DOCKER_CONTAINER,
        socket_path: str = DOCKER_SOCKET_PATH,
        network_flag: str = NETWORK_FLAG,
        wallet_dir: str = DEV_WALLET_DIR,
    ):
        self.container = container
        self.socket_path = socket_path
        self.network_flag = network_flag
        self.wallet_dir = wallet_dir

    # ── Docker exec ─────────��────────────────────────────────────────────

    def docker_exec(self, cmd: str, check: bool = True) -> str:
        """Run a command inside the Docker container."""
        full_cmd = ["docker", "exec", self.container, "bash", "-c", cmd]
        result = subprocess.run(full_cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Docker command failed: {cmd[:120]}...\n{result.stderr.strip()}"
            )
        return result.stdout.strip()

    def cardano_cli(self, args: str) -> str:
        """Run cardano-cli inside Docker. Auto-adds --socket-path when needed."""
        needs_socket = any(
            kw in args for kw in ["query ", "transaction build ", "transaction submit "]
        )
        socket_arg = f" --socket-path {self.socket_path}" if needs_socket else ""
        return self.docker_exec(f"cardano-cli {args}{socket_arg}")

    # ── File transfer ────────────────────────────────────────────────────

    def write_json(self, remote_path: str, data: Any) -> None:
        """Write a JSON file into the Docker container via docker cp."""
        local_tmp = f"/tmp/m3sdk_{Path(remote_path).name}"
        with open(local_tmp, "w") as f:
            json.dump(data, f)
        subprocess.run(
            ["docker", "cp", local_tmp, f"{self.container}:{remote_path}"],
            check=True,
        )
        os.unlink(local_tmp)

    def write_bytes(self, remote_path: str, data: bytes) -> None:
        """Write raw bytes into the Docker container via docker cp."""
        local_tmp = f"/tmp/m3sdk_{Path(remote_path).name}"
        with open(local_tmp, "wb") as f:
            f.write(data)
        subprocess.run(
            ["docker", "cp", local_tmp, f"{self.container}:{remote_path}"],
            check=True,
        )
        os.unlink(local_tmp)

    # ── Wallet ─────────────────��────────────────────────���────────────────

    def get_wallet_address(self) -> str:
        return self.docker_exec(f"cat {self.wallet_dir}/payment.addr")

    def get_wallet_vkey_hash(self) -> str:
        return self.docker_exec(
            f"cardano-cli conway address key-hash "
            f"--payment-verification-key-file {self.wallet_dir}/payment.vkey"
        )

    # ── UTXO queries ──────────────────────────────��──────────────────────

    def query_utxos(self, address: str) -> dict:
        """Query all UTXOs at an address."""
        output = self.cardano_cli(
            f"conway query utxo {self.network_flag} "
            f"--address {address} --out-file /dev/stdout"
        )
        return json.loads(output)

    def get_current_slot(self) -> int:
        tip = json.loads(self.cardano_cli(f"conway query tip {self.network_flag}"))
        return tip["slot"]

    def get_best_utxo(
        self, address: str, exclude: Set[str] = frozenset()
    ) -> Tuple[str, int]:
        """Get the largest spendable UTXO at address, skipping excluded UTXOs."""
        utxos = self.query_utxos(address)
        best_txin = None
        best_lovelace = 0
        for txin, data in utxos.items():
            if txin in exclude:
                continue
            lovelace = data["value"]["lovelace"]
            if lovelace > best_lovelace:
                best_lovelace = lovelace
                best_txin = txin
        if best_txin is None:
            raise RuntimeError(f"No spendable UTXOs at {address}")
        return best_txin, best_lovelace

    def get_collateral(
        self, address: str, exclude: Set[str] = frozenset()
    ) -> str:
        """Get a collateral UTXO (smallest spendable >= 5 AP3X)."""
        utxos = self.query_utxos(address)
        best = None
        best_lovelace = float("inf")
        for txin, data in utxos.items():
            if txin in exclude:
                continue
            lovelace = data["value"]["lovelace"]
            if lovelace < best_lovelace and lovelace >= 5_000_000:
                best_lovelace = lovelace
                best = txin
        if best is None:
            # Fallback to best (largest) UTXO
            best, _ = self.get_best_utxo(address, exclude)
        return best

    # ── Transaction signing/submission ───────────────────────────────────

    def sign_and_submit(self, label: str, raw_file: str, signed_file: str) -> str:
        """Sign and submit a transaction. Returns tx hash."""
        self.cardano_cli(
            f"conway transaction sign {self.network_flag} "
            f"--tx-body-file {raw_file} "
            f"--signing-key-file {self.wallet_dir}/payment.skey "
            f"--out-file {signed_file}"
        )
        self.cardano_cli(
            f"conway transaction submit {self.network_flag} --tx-file {signed_file}"
        )
        tx_hash = self.docker_exec(
            f"cardano-cli conway transaction txid --tx-file {signed_file}"
        )
        return tx_hash

    # ── UTXO lookup ───────────��───────────────────────────��──────────────

    def find_utxo_with_token(
        self, address: str, policy_id: str, token_name: str
    ) -> Optional[str]:
        """Find a UTXO containing a specific native token."""
        utxos = self.query_utxos(address)
        for utxo_id, info in utxos.items():
            for pid, assets in info["value"].items():
                if pid == policy_id and isinstance(assets, dict) and token_name in assets:
                    return utxo_id
        return None
