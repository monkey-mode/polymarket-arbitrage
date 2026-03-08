import json
from datetime import datetime
import logging
import asyncio
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from src.config import FeeConfig
import src.config as bot_config
from src.blockchain import BlockchainManager
from src.redis_logger import RedisLogger

logger = logging.getLogger(__name__)

class ArbitrageStrategy:
    def __init__(self, client: ClobClient, markets: list, blockchain: BlockchainManager, dry_run: bool = False, redis_logger: RedisLogger = None):
        self.client = client
        self.markets = markets
        self.blockchain = blockchain
        self.dry_run = dry_run
        self.redis_logger = redis_logger
        
        self.inventory = {} # Map token_id -> position size
        self.last_log_time = 0
        self.transaction_done = False

    def calculate_taker_fee(self, amount: float, price: float, token_id: str, rate_type: str = "crypto") -> float:
        """
        Applies the 2026 Continuous Fee Formula using the live API base fee rate.
        """
        rate = 0.0
        if rate_type == "crypto":
            exp = FeeConfig.CRYPTO_EXPONENT
        else:
            exp = FeeConfig.SPORTS_EXPONENT
            
        fee_raw = amount * rate * (1 - (2 * abs(price - 0.5)) ** exp)
        if fee_raw < 0.0001:
            return 0.0
        return fee_raw

    async def evaluate_market(self, market_data: dict, current_prices: dict, print_summary: bool = False):
        """
        Evaluates a single market for Dutch Book Arbitrage.
        """
        yes_token = market_data['yes']
        no_token = market_data['no']
        
        if yes_token not in current_prices or no_token not in current_prices:
            return

        yes_ask = current_prices[yes_token]['ask']
        no_ask = current_prices[no_token]['ask']

        if yes_ask == 0 or no_ask == 0:
            return
        
        # Fixed amount for test/safety
        target_amount = 5.01
        
        if target_amount < 1:
            return

        yes_fee = self.calculate_taker_fee(target_amount, yes_ask, yes_token)
        no_fee = self.calculate_taker_fee(target_amount, no_ask, no_token)

        yes_cost = (yes_ask * target_amount) + yes_fee
        no_cost = (no_ask * target_amount) + no_fee
        total_cost = yes_cost + no_cost
        
        # expected_payout = target_amount * 1.0
        expected_payout = 5.1
        
        if total_cost < expected_payout and sum(yes_ask,no_ask) < 1.0:
            profit = expected_payout - total_cost
            logger.info(f"Arbitrage Found! Target Shares: {target_amount:.2f}, Cost: ${total_cost:.4f}, Profit: ${profit:.4f}")
            
            if self.redis_logger:
                log_data = {
                    "event": "ARBITRAGE_FOUND",
                    "market_id": market_data['market_id'][-6:],
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "total_cost": total_cost,
                    "fees": yes_fee + no_fee,
                    "profit": profit
                }
                await self.redis_logger.publish_log(log_data)
                
            await self.execute_arbitrage(market_data, target_amount, yes_ask, no_ask, yes_fee, no_fee)
            
            # For testing: stop the program after 1 trade
            if not self.dry_run:
                logger.info("One-shot trade executed. Shutting down for inspection...")
                import os
                os._exit(0)
            return

        elif print_summary and self.redis_logger:
            log_data = {
                "event": "NO_ARB",
                "market_id": market_data['market_id'][-6:],
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "total_cost": total_cost,
                "fees": yes_fee + no_fee
            }
            await self.redis_logger.publish_log(log_data)

    async def active_exit(self, market_data: dict, current_prices: dict = None, forced_token: str = None, forced_qty: float = None):
        """
        Manually liquidates any open positions for the current market by selling them at the bid.
        This is called 30 seconds before market expiry or during emergency atomic cleanup.
        """
        yes_token = market_data['yes']
        no_token = market_data['no']

        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.order_builder.constants import SELL

        async def sell_side(token_id, qty):
            if qty <= 0: return
            logger.info(f"Liquidating {qty} shares of {token_id[-8:]}...")
            try:
                # Use Market Orders for guaranteed exit
                sell_args = MarketOrderArgs(amount=round(qty, 2), side=SELL, token_id=token_id)
                resp = self.client.create_market_order(sell_args)
                posted = self.client.post_order(resp, OrderType.GTC)
                logger.info(f"EXIT Order Posted for {token_id[-8:]}: {posted.get('status', 'Unknown')}")
            except Exception as e:
                logger.error(f"Liquidation failed for {token_id[-8:]}: {e}")

        # If forced_token is provided, we only sell that one (Emergency Cleanup)
        if forced_token and forced_qty:
            await sell_side(forced_token, forced_qty)
            return

        # Otherwise, sell regular inventory (Planned Exit)
        yes_qty = self.inventory.get(yes_token, 0)
        no_qty = self.inventory.get(no_token, 0)
        
        if yes_qty <= 0 and no_qty <= 0:
            return

        logger.info(f"Market expiry approaching! Liquidating positions for {market_data['market_id'][-6:]}")
        logger.info(f"Targeting sale of {yes_qty:.2f} YES and {no_qty:.2f} NO shares.")
        
        await asyncio.gather(
            sell_side(yes_token, yes_qty),
            sell_side(no_token, no_qty)
        )

        # Reset inventory for this market
        self.inventory[yes_token] = 0
        self.inventory[no_token] = 0

    async def execute_arbitrage(self, market_data: dict, amount: float, yes_price: float, no_price: float, yes_fee: float, no_fee: float):
        """
        Executes Fill-Or-Kill orders for both YES and NO. 
        If one fails and the other fills, immediately clean up by selling the fill.
        """
        yes_token = market_data['yes']
        no_token = market_data['no']
        tick_size = market_data['tick_size']

        import math
        price_decimals = max(0, int(-math.log10(tick_size)))
        yes_price_rounded = round(yes_price, price_decimals)
        no_price_rounded = round(no_price, price_decimals)
        amount_rounded = round(amount, 2)

        logger.info(f"Executing Arbitrage FOK Orders: Buying {amount_rounded} pairs.")

        # Construct raw payload
        yes_order_args = OrderArgs(price=yes_price_rounded, size=amount_rounded, side=BUY, token_id=yes_token)
        no_order_args = OrderArgs(price=no_price_rounded, size=amount_rounded, side=BUY, token_id=no_token)

        try:
            from py_clob_client.clob_types import CreateOrderOptions
            options = CreateOrderOptions(tick_size=str(tick_size), neg_risk=market_data.get('negRisk', False))
            
            # Explicitly use FOK (Fill-Or-Kill) to minimize market risk
            yes_order_args.type = OrderType.FOK
            no_order_args.type = OrderType.FOK

            if self.dry_run:
                logger.info(f"[PAPER TRADE] Arbitrage Found (Vol: {amount})")
                
                # Write to JSON for analysis
                trade_record = {
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "market_id": market_data.get('market_id'),
                    "condition_id": market_data.get('condition_id'),
                    "trade": "YES/NO ARB",
                    "amount": amount,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_fee": yes_fee,
                    "no_fee": no_fee,
                    "total_cost": (yes_price + no_price) * amount + yes_fee + no_fee,
                    "theoretical_profit": amount - ((yes_price + no_price) * amount + yes_fee + no_fee)
                }
                
                try:
                    with open("paper_trades.json", "r+") as file:
                        file_data = json.load(file)
                        file_data.append(trade_record)
                        file.seek(0)
                        json.dump(file_data, file, indent=4)
                except Exception as e:
                    logger.error(f"Failed to record paper trade to JSON: {e}")
                
                return

            resp_yes = self.client.create_and_post_order(yes_order_args, options)
            resp_no = self.client.create_and_post_order(no_order_args, options)

            logger.info(f"YES Order Status: {resp_yes.get('status')} | NO Order Status: {resp_no.get('status')}")
            
            yes_matched = resp_yes.get('success') and resp_yes.get('status') == 'matched'
            no_matched = resp_no.get('success') and resp_no.get('status') == 'matched'

            # --- Legging Safety Logic ---
            if yes_matched != no_matched:
                logger.error(f"Legging Risk Detected! YES Matched: {yes_matched}, NO Matched: {no_matched}")
                
                # 1. Cancel hanging orders immediately
                if resp_yes.get('orderID'):
                    try: self.client.cancel_order(resp_yes['orderID'])
                    except: pass
                if resp_no.get('orderID'):
                    try: self.client.cancel_order(resp_no['orderID'])
                    except: pass
                
                # 2. Emergency Liquidation of the side that DID fill
                if not self.dry_run:
                    # Extract ACTUAL filled amount to avoid "insufficient balance" during cleanup
                    try:
                        matched_resp = resp_yes if yes_matched else resp_no
                        matched_token = yes_token if yes_matched else no_token
                        actual_qty = float(matched_resp.get('takingAmount') or 0)
                        
                        if actual_qty > 0:
                            logger.warning(f"Emergency Cleanup: Market-selling {actual_qty:.6f} orphan tokens.")
                            await self.active_exit(market_data, forced_token=matched_token, forced_qty=actual_qty)
                        else:
                            logger.error("Legging detected but caught-side reporting zero fill. Manual check required.")
                    except Exception as ex:
                        logger.error(f"Failed to execute emergency liquidation: {ex}")
                return

            if not yes_matched and not no_matched:
                logger.info("Arbitrage failed to fill at requested prices (FOK triggered). Skipping.")
                return

            # Both matched! Merge tokens to recover USDC capital.
            if not self.dry_run:
                # Give Polygon indexing a moment before querying balances for the merge
                await asyncio.sleep(2)
                try:
                    # Query actual on-chain balances in the Funder wallet for precision merge
                    logger.info("Querying on-chain balances for precision merge...")
                    bal_yes = self.blockchain.get_token_balance(self.blockchain.funder, yes_token)
                    bal_no = self.blockchain.get_token_balance(self.blockchain.funder, no_token)
                    
                    merge_qty_uint = min(bal_yes, bal_no)
                    
                    if merge_qty_uint > 0:
                        logger.info(f"Merging {merge_qty_uint / 10**6:.6f} pairs (On-Chain Balance) back to USDC...")
                        self.blockchain.merge_positions(market_data['condition_id'], merge_qty_uint)
                    else:
                        logger.warning(f"Merge skipped: On-chain balances too low (YES: {bal_yes}, NO: {bal_no})")
                except Exception as ex:
                    logger.error(f"Failed to fetch on-chain balances or merge: {ex}")
                    
            # Track our position for the regular exit logic (mostly useful for 30s-pre-close safety)
            # We track the target_amount to ensure we sell everything we intended to hedge
            self.inventory[market_data['yes']] = self.inventory.get(market_data['yes'], 0) + amount_rounded
            self.inventory[market_data['no']] = self.inventory.get(market_data['no'], 0) + amount_rounded

        except Exception as e:
            logger.error(f"Execution failed: {e}")

    async def on_price_update(self, current_prices: dict):
        """
        Callback from the WebSocket stream.
        """
        tasks = []
        for market in self.markets:
            tasks.append(self.evaluate_market(market, current_prices, print_summary=bot_config.PRINT_VERBOSE_SPREADS))
        await asyncio.gather(*tasks)

    async def run_background_reporting(self, streamer):
        """
        Prints the current known spreads exactly every 10 seconds.
        """
        while True:
            await asyncio.sleep(10)
            logger.info("--- Live Market Spread Scan ---")
            
            for market in self.markets:
                yes_token = market['yes']
                no_token = market['no']
                yes_data = streamer.best_prices.get(yes_token, {})
                no_data = streamer.best_prices.get(no_token, {})
                yes_ask = yes_data.get("ask", 0)
                no_ask = no_data.get("ask", 0)
                
                if yes_ask > 0 or no_ask > 0:
                    logger.info(f"Market {market['market_id'][-6:]}: YES ask @ ${yes_ask:.2f} | NO ask @ ${no_ask:.2f}")
                
            for market in self.markets:
                await self.evaluate_market(market, streamer.best_prices, print_summary=bot_config.PRINT_VERBOSE_SPREADS)
