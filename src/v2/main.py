"""
Polymarket Autonomous Arbitrage Bot — Entry Point

Discovers BTC 5-minute markets, streams prices via WebSocket,
and executes arbitrage when profitable.
"""
import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

from src.v2.exchange.client import PolymarketClient
from src.v2.chain.blockchain import BlockchainManager
from src.v2.exchange.discovery import DiscoveryManager
from src.v2.exchange.websocket_stream import WebSocketStreamer
from src.v2.trading.strategy import ArbitrageStrategy
from src.v2.infra.redis_logger import RedisLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def run_market_cycle(
    market_data: dict,
    client: PolymarketClient,
    blockchain: BlockchainManager,
    dry_run: bool,
    redis_logger: RedisLogger,
    one_shot: bool = False,
) -> None:
    """
    Manages the lifecycle of a single 5-minute market:
    1. Subscribe to price updates.
    2. Run arbitrage until 30s before expiry.
    3. Liquidate remaining positions.
    """
    expiry = market_data.get("end_timestamp")
    if not expiry:
        logger.error(f"Market {market_data['market_id']} missing end_timestamp. Skipping.")
        return

    logger.info(
        f"--- STARTING CYCLE: Market {market_data['market_id'][-6:]} "
        f"(Expires: {datetime.fromtimestamp(expiry).strftime('%H:%M:%S')}) ---"
    )

    token_pairs = [market_data]
    strategy = ArbitrageStrategy(client, token_pairs, blockchain, dry_run=dry_run, redis_logger=redis_logger, one_shot=one_shot)
    streamer = WebSocketStreamer(client, token_pairs)

    stream_task = asyncio.create_task(streamer.connect_and_stream(strategy.on_price_update))

    try:
        while True:
            now = time.time()
            time_left = expiry - now

            if time_left <= 30:
                logger.info(f"Market expiry approaching ({int(time_left)}s left). Liquidating...")
                break

            await asyncio.sleep(10 if time_left > 60 else 1)

        await strategy.active_exit(market_data, streamer.best_prices)

    finally:
        stream_task.cancel()
        try:
            await asyncio.gather(stream_task, return_exceptions=True)
        except Exception:
            pass

    logger.info(f"--- CYCLE COMPLETE: Market {market_data['market_id'][-6:]} ---")


async def main(dry_run: bool, one_shot: bool = False) -> None:
    logger.info("Starting Polymarket Autonomous Arbitrage Bot...")
    if dry_run:
        logger.info("--- RUNNING IN PAPER TRADING (DRY-RUN) MODE ---")

    client = PolymarketClient()
    blockchain = BlockchainManager()
    discovery = DiscoveryManager()
    redis_logger = RedisLogger()

    if not dry_run:
        logger.info("Ensuring on-chain CTF token allowances are set...")
        blockchain.setup_all_approvals()

    while True:
        try:
            markets = discovery.get_upcoming_btc_5m_markets()
            if not markets:
                logger.info("No active markets found. Waiting 30s...")
                await asyncio.sleep(30)
                continue

            token_pairs = discovery.extract_token_pairs(markets)
            if not token_pairs:
                await asyncio.sleep(30)
                continue

            current_market = sorted(token_pairs, key=lambda x: x["end_timestamp"])[0]
            await run_market_cycle(current_market, client, blockchain, dry_run, redis_logger, one_shot=one_shot)
            await asyncio.sleep(5)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Global loop error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument("--dry-run", action="store_true", help="Paper Trading mode")
    parser.add_argument("--one-shot", action="store_true", help="Execute one trade then stop")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.dry_run, one_shot=args.one_shot))
    except KeyboardInterrupt:
        logger.info("Bot terminated by user.")
