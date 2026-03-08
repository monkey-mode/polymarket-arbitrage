import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone
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

async def run_market_cycle(market_data: dict, client, blockchain, dry_run: bool, redis_logger: RedisLogger):
    """
    Manages the lifecycle of a single 5-minute market:
    1. Subscribes to updates.
    2. Runs arbitrage until 30s before expiry.
    3. Liquidates remaining positions.
    """
    expiry = market_data.get('end_timestamp')
    if not expiry:
        logger.error(f"Market {market_data['market_id']} missing end_timestamp. Skipping.")
        return

    logger.info(f"--- STARTING CYCLE: Market {market_data['market_id'][-6:]} (Expires: {datetime.fromtimestamp(expiry).strftime('%H:%M:%S')}) ---")
    
    # 1. Initialize Streamer and Strategy for this market
    token_pairs = [market_data]
    strategy = ArbitrageStrategy(client, token_pairs, blockchain, dry_run=dry_run, redis_logger=redis_logger)
    streamer = WebSocketStreamer(client, token_pairs)

    # 2. Start WebSocket stream in a background task
    stream_task = asyncio.create_task(streamer.connect_and_stream(strategy.on_price_update))
    logger_task = asyncio.create_task(redis_logger.run_subscriber())

    try:
        # 3. Wait until 30 seconds before expiry
        while True:
            now = time.time()
            time_left = expiry - now
            
            if time_left <= 30:
                logger.info(f"Market expiry approaching ({int(time_left)}s left). Starting liquidation phase...")
                break
            
            if time_left > 60:
                await asyncio.sleep(10)
            else:
                await asyncio.sleep(1)

        # 4. Trigger Liquidation Exit
        await strategy.active_exit(market_data, streamer.best_prices)
        
    finally:
        # Cleanup tasks for this cycle
        stream_task.cancel()
        logger_task.cancel()
        try:
            await asyncio.gather(stream_task, logger_task, return_exceptions=True)
        except:
            pass
    
    logger.info(f"--- CYCLE COMPLETE: Market {market_data['market_id'][-6:]} ---")

async def main(dry_run: bool):
    logger.info("Starting Polymarket Autonomous Arbitrage Bot...")
    if dry_run:
        logger.info("--- RUNNING IN PAPER TRADING (DRY-RUN) MODE ---")

    client = initialize_client()
    blockchain = BlockchainManager()
    discovery = DiscoveryManager()
    redis_logger = RedisLogger()

    if not dry_run:
        logger.info("Ensuring on-chain CTF token allowances are set...")
        blockchain.setup_all_approvals()

    while True:
        try:
            # Discover target markets
            markets = discovery.get_upcoming_btc_5m_markets()
            if not markets:
                logger.info("No active specific markets found. Waiting 30s...")
                await asyncio.sleep(30)
                continue
            
            token_pairs = discovery.extract_token_pairs(markets)
            if not token_pairs:
                await asyncio.sleep(30)
                continue

            # Target the SOONEST expiring market among discovered
            current_market = sorted(token_pairs, key=lambda x: x['end_timestamp'])[0]
            
            # Run the cycle for this market
            await run_market_cycle(current_market, client, blockchain, dry_run, redis_logger)
            
            # Small buffer before looking for next market
            await asyncio.sleep(5)
            
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Global loop error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument("--dry-run", action="store_true", help="Run without posting live orders (Paper Trading)")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.dry_run))
    except KeyboardInterrupt:
        logger.info("Bot execution terminated by user.")
