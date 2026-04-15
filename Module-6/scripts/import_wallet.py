"""
Module 6: Import wallet from BIP39 mnemonic.

Derives CIP-1852 payment keys and writes governance_wallet.json + payment.skey.

Usage:
    nix-shell shell.nix --run "python scripts/import_wallet.py /path/to/mnemonic.txt"
"""

import json
import sys
from pathlib import Path

WALLET_DIR = Path("wallets")
WALLET_FILE = WALLET_DIR / "governance_wallet.json"
SKEY_FILE = WALLET_DIR / "payment.skey"


def import_from_mnemonic(mnemonic: str):
    from pycardano import HDWallet, PaymentSigningKey, PaymentVerificationKey, Address, Network

    wallet = HDWallet.from_mnemonic(mnemonic.strip())
    child = wallet.derive_from_path("m/1852'/1815'/0'/0/0")

    skey = PaymentSigningKey(child.xprivate_key[:32])
    vkey = PaymentVerificationKey.from_signing_key(skey)
    address = Address(payment_part=vkey.hash(), network=Network.MAINNET)

    WALLET_DIR.mkdir(exist_ok=True)

    wallet_data = {
        "address": str(address),
        "skey_hex": skey.payload.hex(),
        "vkey_hex": vkey.payload.hex(),
        "vkey_hash": str(vkey.hash()),
    }
    with open(WALLET_FILE, "w") as f:
        json.dump(wallet_data, f, indent=2)

    skey_data = {
        "type": "PaymentSigningKeyShelley_ed25519",
        "description": "Payment Signing Key",
        "cborHex": "5820" + skey.payload.hex(),
    }
    with open(SKEY_FILE, "w") as f:
        json.dump(skey_data, f, indent=2)

    print(f"Wallet imported!")
    print(f"  Address: {address}")
    print(f"  VKey Hash: {vkey.hash()}")
    print(f"  Saved to: {WALLET_FILE}")
    print(f"  Skey: {SKEY_FILE}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_wallet.py <mnemonic_file>")
        sys.exit(1)

    mnemonic_path = Path(sys.argv[1])
    if not mnemonic_path.exists():
        print(f"ERROR: File not found: {mnemonic_path}")
        sys.exit(1)

    mnemonic = mnemonic_path.read_text().strip()
    print(f"Importing wallet from {mnemonic_path} ({len(mnemonic.split())} words)")
    import_from_mnemonic(mnemonic)
