#!/usr/bin/env python3
"""
Module 3: Reputation Staking — Full End-to-End Smoke Test on MAINNET

The steps, inputs and identity checks are those of smoke_test_ogmios.py.
This script uses the mainnet Ogmios and submit endpoints, the mainnet system
start, deploy/mainnet/deploy_state.json, deploy/mainnet/smoke_state_ogmios.json
and the wallet key /tmp/m3mainnet_payment.skey (or M3_WALLET_SKEY).

Usage:
    export M3_SMOKE_AGENT_A=did:vector:<64 hex>
    export M3_SMOKE_AGENT_B=did:vector:<64 hex>
    cd Module-3 && PYTHONPATH=python:$PYTHONPATH python3 scripts/smoke_test_mainnet_ogmios.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from smoke_test_ogmios import MAINNET, smoke_test  # noqa: E402

if __name__ == "__main__":
    smoke_test(MAINNET)
