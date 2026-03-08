import argparse
import asyncio
import logging
from src.client import initialize_client
from src.blockchain import BlockchainManager
from src.discovery import DiscoveryManager
from src.websocket_stream import WebSocketStreamer
from src.strategy import ArbitrageStrategy
from src.redis_logger import RedisLogger

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main(dry_run: bool):
    logger.info("Starting Polymarket Arbitrage Bot...")
    if dry_run:
        logger.info("--- RUNNING IN PAPER TRADING (DRY-RUN) MODE ---")

    # 1. Initialize API Client
    try:
        client = initialize_client()
    except ValueError as e:
        logger.error(f"Initialization Error: {e}")
        return

    # 2. Blockchain Setup (Approvals)
    blockchain = BlockchainManager()
    logger.info("Ensuring on-chain CTF token allowances are set...")
    # NOTE: Set to skip automatic sending on mainnet without explicit user approval
    try:
        if not dry_run:
            blockchain.setup_all_approvals()
        else:
            logger.info("Approvals skipped for dry-run/dev.")
    except Exception as e:
        logger.error(f"Blockchain setup failed: {e}")
        return

    # 3. Discover Markets via Gamma API
    discovery = DiscoveryManager()
    markets = discovery.get_upcoming_btc_5m_markets()
    if not markets:
        logger.info("No active specific markets found matching criteria. Exiting.")
        return
        
    logger.info(f"Discovered {len(markets)} active target markets.")
    token_pairs = discovery.extract_token_pairs(markets)
    logger.info(f"Subscribing to {len(token_pairs) * 2} specific active tokens.")

    # 4. Initialize Strategy Engine with Redis Logger
    redis_logger = RedisLogger()
    strategy = ArbitrageStrategy(client, token_pairs, blockchain, dry_run=dry_run, redis_logger=redis_logger)

    # 5. Connect WebSocket and Stream
    streamer = WebSocketStreamer(client, token_pairs)
    
    try:
        # Run websocket stream, background stdout reporter, AND redis background subscriber concurrently
        await asyncio.gather(
            streamer.connect_and_stream(strategy.on_price_update),
            # strategy.run_background_reporting(streamer),
            redis_logger.run_subscriber()
        )
    except Exception as e:
        logger.error(f"WebSocket Stream encountered an error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument("--dry-run", action="store_true", help="Run without posting live orders (Paper Trading)")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.dry_run))
    except KeyboardInterrupt:
        logger.info("Bot execution terminated by user.")
