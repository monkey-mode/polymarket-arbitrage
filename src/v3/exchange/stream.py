import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import websockets

from src.v3.exchange.discovery import MarketDiscovery

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10   # seconds (CLOB drops if no PING for >10s)
CYCLE_CHECK_INTERVAL = 5  # how often to check if market has expired


class MarketStreamer:
    """
    Streams best_bid_ask prices for BTC 5-minute markets.
    Automatically transitions to the next market when the current one closes.

    Callback signature:
        async def on_price(market: dict, prices: dict[token_id, {bid, ask}]) -> None
    """

    def __init__(self, discovery: MarketDiscovery):
        self.discovery = discovery
        self.markets: list[dict] = []        # [current, next]
        self.prices: dict[str, dict] = {}    # token_id → {bid, ask}
        self._ws = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, callback: Callable[..., Awaitable[None]]) -> None:
        """Stream forever, cycling markets as they expire."""
        self.markets = self.discovery.get_current_and_next()
        if not self.markets:
            logger.error("No active markets found.")
            return

        self._log_markets()

        while True:
            try:
                await self._stream(callback)
            except Exception as e:
                logger.error(f"Stream error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Internal streaming loop
    # ------------------------------------------------------------------

    async def _stream(self, callback: Callable) -> None:
        async with websockets.connect(WS_URL) as ws:
            self._ws = ws
            logger.info("WebSocket connected.")
            await self._subscribe(ws, self._all_token_ids())

            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            cycle_task     = asyncio.create_task(self._cycle_loop(ws, callback))

            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue
                    for event in self._parse(raw):
                        await self._handle(event, callback)
            finally:
                heartbeat_task.cancel()
                cycle_task.cancel()
                await asyncio.gather(heartbeat_task, cycle_task, return_exceptions=True)

    # ------------------------------------------------------------------
    # Market cycling
    # ------------------------------------------------------------------

    async def _cycle_loop(self, ws, callback: Callable) -> None:
        """Checks every few seconds if the current market has closed."""
        while True:
            await asyncio.sleep(CYCLE_CHECK_INTERVAL)
            now = int(time.time())
            current = self.markets[0]

            if now >= current["end_timestamp"]:
                logger.info(f"Market closed: {current['slug']}. Rotating to next.")
                await self._rotate(ws, callback)

    async def _rotate(self, ws, callback: Callable) -> None:
        """
        Drop the expired market, promote next → current,
        fetch a new upcoming market, and resubscribe.
        """
        expired = self.markets.pop(0)
        old_tokens = [expired["yes"], expired["no"]]

        # Unsubscribe expired tokens
        await ws.send(json.dumps({
            "assets_ids": old_tokens,
            "operation": "unsubscribe",
        }))

        # Remove stale prices
        for t in old_tokens:
            self.prices.pop(t, None)

        # Fetch one more upcoming market to keep the queue at 2
        new_markets = self.discovery.get_current_and_next()
        for m in new_markets:
            if not any(x["slug"] == m["slug"] for x in self.markets):
                self.markets.append(m)
                await self._subscribe(ws, [m["yes"], m["no"]])
                logger.info(f"Subscribed to new market: {m['slug']}")
                break

        self._log_markets()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _handle(self, event: dict, callback: Callable) -> None:
        if event.get("event_type") != "best_bid_ask":
            return

        token_id = event.get("asset_id")
        if not token_id:
            return

        self.prices[token_id] = {
            "bid": float(event.get("best_bid", 0) or 0),
            "ask": float(event.get("best_ask", 0) or 0),
        }

        # Find which market this token belongs to
        market = self._market_for_token(token_id)
        if market:
            await callback(market, self.prices)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _subscribe(self, ws, token_ids: list[str]) -> None:
        await ws.send(json.dumps({
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }))
        logger.info(f"Subscribed to {len(token_ids)} tokens.")

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send("PING")

    def _all_token_ids(self) -> list[str]:
        ids = []
        for m in self.markets:
            ids += [m["yes"], m["no"]]
        return ids

    def _market_for_token(self, token_id: str) -> dict | None:
        for m in self.markets:
            if token_id in (m["yes"], m["no"]):
                return m
        return None

    def _parse(self, raw: str) -> list[dict]:
        payload = json.loads(raw)
        return payload if isinstance(payload, list) else [payload]

    def _log_markets(self) -> None:
        for i, m in enumerate(self.markets):
            label = "CURRENT" if i == 0 else "NEXT"
            logger.info(f"[{label}] {m['slug']}  closes={m['end_timestamp']}")
