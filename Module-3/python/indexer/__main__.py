"""
CLI entrypoint for the Module 3 Reputation Indexer.

Usage:
    # Testnet (default)
    python -m indexer

    # Mainnet
    python -m indexer --network mainnet

    # Custom settings
    python -m indexer --db /path/to/db.sqlite --interval 30

    # Single poll (no loop)
    python -m indexer --once
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("indexer")


NETWORKS = {
    "testnet": {
        "ogmios_url": "https://ogmios.vector.testnet.apexfusion.org",
        "deploy_state": "deploy/deploy_state.json",
    },
    "mainnet": {
        "ogmios_url": "https://ogmios.vector.mainnet.apexfusion.org",
        "deploy_state": "deploy/mainnet/deploy_state.json",
    },
}


def main():
    parser = argparse.ArgumentParser(description="Module 3 Reputation Indexer")
    parser.add_argument(
        "--network", choices=["testnet", "mainnet"], default="testnet",
        help="Vector network (default: testnet)",
    )
    parser.add_argument(
        "--deploy-state", type=str, default=None,
        help="Path to deploy_state.json (overrides --network default)",
    )
    parser.add_argument(
        "--ogmios-url", type=str, default=None,
        help="Ogmios URL (overrides --network default)",
    )
    parser.add_argument(
        "--db", type=str, default="reputation_index.db",
        help="SQLite database path (default: reputation_index.db)",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single poll and exit",
    )
    parser.add_argument(
        "--with-api", action="store_true",
        help="Also start the REST API server on port 8080",
    )
    parser.add_argument(
        "--api-port", type=int, default=8080,
        help="API server port (default: 8080)",
    )
    args = parser.parse_args()

    # Resolve network config
    net_config = NETWORKS[args.network]
    ogmios_url = args.ogmios_url or net_config["ogmios_url"]

    # Resolve deploy state path
    module3_root = Path(__file__).parent.parent.parent  # Module-3/
    deploy_state_path = args.deploy_state or str(module3_root / net_config["deploy_state"])

    logger.info("Network: %s", args.network)
    logger.info("Ogmios: %s", ogmios_url)
    logger.info("Deploy state: %s", deploy_state_path)
    logger.info("Database: %s", args.db)

    # Load deploy state
    try:
        with open(deploy_state_path) as f:
            deploy_state = json.load(f)
    except FileNotFoundError:
        logger.error("Deploy state not found: %s", deploy_state_path)
        sys.exit(1)

    # Patch system start for mainnet
    if args.network == "mainnet":
        import reputation_staking.utils as _utils
        import reputation_staking.constants as _constants
        MAINNET_SYSTEM_START_UNIX_S = 1756485600  # 2025-08-29T16:40:00Z
        _utils.SYSTEM_START_UNIX_S = MAINNET_SYSTEM_START_UNIX_S
        _constants.SYSTEM_START_UNIX_S = MAINNET_SYSTEM_START_UNIX_S
        logger.info("Patched system start for mainnet: %d", MAINNET_SYSTEM_START_UNIX_S)

    # Initialize storage and indexer
    from indexer.storage import IndexerStorage
    from indexer.indexer import ReputationIndexer

    storage = IndexerStorage(args.db)
    indexer = ReputationIndexer(
        deploy_state=deploy_state,
        storage=storage,
        poll_interval=args.interval,
        ogmios_url=ogmios_url,
    )

    if args.once:
        count = indexer.poll_once()
        logger.info("Single poll complete: %d agents indexed", count)
        storage.close()
        return

    if args.with_api:
        # Run API server in a separate thread
        import threading
        import os
        os.environ["INDEXER_DB"] = args.db

        def run_api():
            import uvicorn
            uvicorn.run(
                "indexer.api:app",
                host="0.0.0.0",
                port=args.api_port,
                log_level="info",
            )

        api_thread = threading.Thread(target=run_api, daemon=True)
        api_thread.start()
        logger.info("API server started on port %d", args.api_port)

    try:
        indexer.run()
    except KeyboardInterrupt:
        logger.info("Indexer stopped by user")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
