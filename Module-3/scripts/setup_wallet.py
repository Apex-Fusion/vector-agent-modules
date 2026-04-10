"""
Module 3: Wallet Setup — Create wallet and fund from Vector faucet.

Usage:
    nix-shell shell.nix --run "python scripts/setup_wallet.py"
"""

import asyncio
import os
import json
import httpx
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

FAUCET_API_KEY = os.getenv("FAUCET_API_KEY")
FAUCET_URL = "https://faucet.vector.testnet.apexfusion.org/api/v1/faucet"
WALLET_DIR = Path("wallets")
WALLET_FILE = WALLET_DIR / "reputation_wallet.json"


async def create_wallet():
    """Create a new wallet using pycardano."""
    from pycardano import PaymentSigningKey, PaymentVerificationKey, Address, Network

    skey = PaymentSigningKey.generate()
    vkey = PaymentVerificationKey.from_signing_key(skey)
    address = Address(
        payment_part=vkey.hash(),
        network=Network.MAINNET,
    )

    WALLET_DIR.mkdir(exist_ok=True)

    wallet_data = {
        "address": str(address),
        "skey_hex": skey.payload.hex(),
        "vkey_hex": vkey.payload.hex(),
        "vkey_hash": str(vkey.hash()),
    }

    with open(WALLET_FILE, "w") as f:
        json.dump(wallet_data, f, indent=2)

    # Also save as cardano-cli compatible skey
    skey_file = WALLET_DIR / "payment.skey"
    skey_data = {
        "type": "PaymentSigningKeyShelley_ed25519",
        "description": "Payment Signing Key",
        "cborHex": "5820" + skey.payload.hex(),
    }
    with open(skey_file, "w") as f:
        json.dump(skey_data, f, indent=2)

    print(f"Wallet created!")
    print(f"  Address: {address}")
    print(f"  Saved to: {WALLET_FILE}")
    print(f"  Skey: {skey_file}")

    return str(address)


async def fund_from_faucet(address: str, amount: int = 1000):
    """Request testnet AP3X from the Vector faucet."""
    if not FAUCET_API_KEY:
        print("\nERROR: FAUCET_API_KEY not set in .env")
        print("  Create .env file with: FAUCET_API_KEY=your_key_here")
        return

    print(f"\nRequesting {amount} AP3X from faucet...")
    print(f"  To: {address}")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            FAUCET_URL,
            json={"address": address, "amount": amount},
            headers={
                "Content-Type": "application/json",
                "api-key": FAUCET_API_KEY,
            },
        )

        if response.status_code == 200:
            data = response.json()
            print(f"  Faucet response: {json.dumps(data, indent=2)}")
            if "txHash" in data:
                print(f"  TX Hash: {data['txHash']}")
                print(f"  Explorer: https://vector.testnet.apexscan.org/tx/{data['txHash']}")
        else:
            print(f"  Faucet error ({response.status_code}): {response.text}")


async def main():
    if WALLET_FILE.exists():
        print(f"Wallet already exists at {WALLET_FILE}")
        with open(WALLET_FILE) as f:
            wallet = json.load(f)
        address = wallet["address"]
        print(f"  Address: {address}")
    else:
        address = await create_wallet()

    await fund_from_faucet(address)


if __name__ == "__main__":
    asyncio.run(main())
