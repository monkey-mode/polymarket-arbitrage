"""
WebSocket Price Streamer

Maintains a low-latency WebSocket connection to the Polymarket CLOB
and passes best-bid-ask updates to a strategy callback.
"""
import asyncio
import json
import logging
import websockets

from src.v2.exchange.client import PolymarketClient

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketStreamer:
    """Manages real-time price streaming from the Polymarket CLOB."""

    def __init__(self, client: PolymarketClient, target_markets: list):
        self.client = client
        self.target_markets = target_markets
        self.best_prices: dict = {}

    def get_token_ids(self) -> list[str]:
        ids = []
        for m in self.target_markets:
            ids.extend([m["yes"], m["no"]])
        return ids

    async def connect_and_stream(self, strategy_callback) -> None:
        """
        Connects, subscribes, and passes best_bid_ask updates to the callback.
        Reconnects automatically on failure.
        """
        while True:
            try:
                async with websockets.connect(WS_URL) as ws:
                    logger.info("WebSocket connected. Subscribing...")
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                    try:
                        await ws.send(json.dumps({
                            "assets_ids": self.get_token_ids(),
                            "type": "market",
                            "custom_feature_enabled": True,
                        }))

                        async for message in ws:
                            payload = json.loads(message)
                            if isinstance(payload, dict):
                                payload = [payload]

                            for data in payload:
                                if data.get("event_type") == "best_bid_ask":
                                    token_id = data.get("asset_id")
                                    if token_id:
                                        self.best_prices[token_id] = {
                                            "bid": float(data.get("best_bid", "0") or "0"),
                                            "ask": float(data.get("best_ask", "0") or "0"),
                                            "bid_sz": float(data.get("best_bid_size", "1") or "1"),
                                            "ask_sz": float(data.get("best_ask_size", "1") or "1"),
                                        }
                                    await strategy_callback(self.best_prices)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except (asyncio.CancelledError, Exception):
                            pass

            except Exception as e:
                logger.error(f"WebSocket lost: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _heartbeat_loop(self) -> None:
        """Sends periodic keep-alive pings via the CLOB client."""
        while True:
            await asyncio.sleep(15)
            try:
                self.client.heartbeat()
                logger.debug("Heartbeat sent.")
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
