"""
v3 Entry Point

Wires together:
  PolymarketClient  — authenticated CLOB connection
  MarketDiscovery   — finds current + next BTC 5m markets
  MarketStreamer    — WebSocket price feed with auto market cycling
"""
import asyncio
import logging

from src.v3.exchange.client import PolymarketClient
from src.v3.exchange.discovery import MarketDiscovery
from src.v3.exchange.stream import MarketStreamer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def on_price(market: dict, prices: dict) -> None:
    """Called on every best_bid_ask update. Plug strategy logic here."""
    yes_ask = prices.get(market["yes"], {}).get("ask")
    no_ask  = prices.get(market["no"],  {}).get("ask")

    if yes_ask is None or no_ask is None:
        return

    total = yes_ask + no_ask
    logger.info(
        f"{market['slug']} | YES ask={yes_ask:.4f}  NO ask={no_ask:.4f}  "
        f"total={total:.4f}  {'ARB ✓' if total < 1.0 else '-'}"
    )


async def main() -> None:
    # Step 1 — authenticate
    client = PolymarketClient()
    logger.info(f"CLOB connected: {client.ok()}")

    # Step 2 — discover markets
    discovery = MarketDiscovery()
    markets = discovery.get_current_and_next()
    for m in markets:
        logger.info(f"Found market: {m['slug']}  ends={m['end_timestamp']}")

    # Step 3 — stream prices + auto-cycle
    streamer = MarketStreamer(discovery)
    await streamer.run(on_price)


if __name__ == "__main__":
    asyncio.run(main())
