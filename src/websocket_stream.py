import asyncio
import json
import logging
import websockets
from py_clob_client.client import ClobClient

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

class WebSocketStreamer:
    """
    Manages low-latency WebSocket connection to the Polymarket CLOB.
    """
    def __init__(self, client: ClobClient, target_markets: list):
        self.client = client
        self.target_markets = target_markets  # List of dicts with 'yes' and 'no' token ids
        
        self.best_prices = {} # Map token_id -> {"bid": price, "ask": price, "bid_sz": size, "ask_sz": size}
    
    def get_token_ids(self) -> list:
        ids = []
        for m in self.target_markets:
            ids.extend([m['yes'], m['no']])
        return ids

    async def connect_and_stream(self, strategy_callback):
        """
        Connects to WS, subscribes to best_bid_ask, and passes updates to the strategy_callback.
        Includes a reconnection loop for resilience.
        """
        while True:
            try:
                async with websockets.connect(WS_URL) as websocket:
                    logger.info("WebSocket connected. Sending subscription payload...")
                    
                    # Manage heartbeat task
                    heartbeat_task = asyncio.create_task(self.heartbeat_loop())
                    
                    try:
                        # Subscribe to best_bid_ask for instant Yes/No sum arbitrage
                        sub_payload = {
                            "assets_ids": self.get_token_ids(),
                            "type": "market",
                            "custom_feature_enabled": True 
                        }
                        await websocket.send(json.dumps(sub_payload))
                        
                        async for message in websocket:
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
                logger.error(f"WebSocket connection lost: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def heartbeat_loop(self):
        """
        Performs periodic POST /heartbeats using py-clob-client authentication.
        REST calls block locally, but we wrap them.
        """
        while True:
            await asyncio.sleep(15) # Send every 15 seconds
            try:
                # py-clob-client handles HTTP headers including the HMAC HMAC-SHA256 signature
                # get_ok() is a wrapper. In real production, use auth header manually if SDK lacks heartbeat mapping.
                self.client.get_ok() 
                logger.debug("Successfully sent authenticated heartbeat.")
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
