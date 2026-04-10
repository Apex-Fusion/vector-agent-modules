"""
Abstract chain backend protocol for Module 3.

Backends implement this protocol to provide chain interaction.
Current implementations: DockerChainBackend (cardano-cli via Docker).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, Set, Tuple


class ChainBackend(Protocol):
    """Protocol for chain interaction backends."""

    def query_utxos(self, address: str) -> dict:
        """Query all UTXOs at an address. Returns cardano-cli JSON format."""
        ...

    def get_current_slot(self) -> int:
        """Get the current tip slot number."""
        ...

    def get_wallet_address(self) -> str:
        """Get the dev wallet's bech32 address."""
        ...

    def get_wallet_vkey_hash(self) -> str:
        """Get the dev wallet's verification key hash."""
        ...

    def get_best_utxo(self, address: str, exclude: Set[str]) -> Tuple[str, int]:
        """Get the largest spendable UTXO (txin, lovelace). Skips excluded UTXOs."""
        ...

    def get_collateral(self, address: str, exclude: Set[str]) -> str:
        """Get a collateral UTXO (smallest spendable, >= 5 AP3X)."""
        ...

    def sign_and_submit(self, label: str, raw_file: str, signed_file: str) -> str:
        """Sign a transaction and submit it. Returns tx hash."""
        ...

    def write_json(self, remote_path: str, data: Any) -> None:
        """Write a JSON file to the execution environment."""
        ...

    def write_bytes(self, remote_path: str, data: bytes) -> None:
        """Write raw bytes to the execution environment."""
        ...

    def cardano_cli(self, args: str) -> str:
        """Run a cardano-cli command. Returns stdout."""
        ...

    def find_utxo_with_token(
        self, address: str, policy_id: str, token_name: str
    ) -> Optional[str]:
        """Find a UTXO at address containing a specific token. Returns utxo_id or None."""
        ...
