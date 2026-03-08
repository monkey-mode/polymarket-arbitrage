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

    def calculate_taker_fee(self, amount: float, price: float, token_id: str, rate_type: str = "crypto") -> float:
        """
        Applies the 2026 Continuous Fee Formula using the live API base fee rate:
        Fee = Amount * Rate * (1 - (2 * abs(Price - 0.5)) ** Exponent)
        Fees < 0.0001 USDC are rounded down to 0.
        """
        # # Fetch the official base fee rate from the API (cached automatically by py-clob-client)
        # base_fee_bps = self.client.get_fee_rate_bps(token_id)
        
        # # If the API explicitly returns 0, the market is subsidized and has no fee
        # if base_fee_bps == 0:
        #     return 0.0
            
        # Convert Basis Points (e.g. 150) to raw multiplier (0.0150)
        # rate = base_fee_bps / 10000.0
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
            return # Data missing

        yes_ask = current_prices[yes_token]['ask']
        no_ask = current_prices[no_token]['ask']
        yes_ask_sz = current_prices[yes_token]['ask_sz']
        no_ask_sz = current_prices[no_token]['ask_sz']

        if yes_ask == 0 or no_ask == 0:
            return # No liquidity

        # Determine the maximum synthetic volume we can cross
        # Target size set to 1.0 (1 USD payout equivalent)
        target_amount = min(1.0, min(yes_ask_sz, no_ask_sz))
        if target_amount < 1:
            return

        # Calculate exact cost + dynamic fees
        # Fee is extracted in outcome shares for buys, effectively increasing the cost per share.
        yes_fee = self.calculate_taker_fee(target_amount, yes_ask, yes_token)
        no_fee = self.calculate_taker_fee(target_amount, no_ask, no_token)

        # Cost = Price * Amount + Fees 
        yes_cost = (yes_ask * target_amount) + yes_fee
        no_cost = (no_ask * target_amount) + no_fee
        total_cost = yes_cost + no_cost
        
        # Payout is guaranteed 1 USDC per pair
        expected_payout = target_amount * 1.0

        # Does target < 1.00 ratio hold true?
        if total_cost < expected_payout:
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
        elif print_summary and self.redis_logger:
            # Broadcast the evaluation strictly to Redis Pub/Sub instead of stdout
            log_data = {
                "event": "NO_ARB",
                "market_id": market_data['market_id'][-6:],
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "total_cost": total_cost,
                "fees": yes_fee + no_fee
            }
            await self.redis_logger.publish_log(log_data)

    async def execute_arbitrage(self, market_data: dict, amount: float, yes_price: float, no_price: float, yes_fee: float, no_fee: float):
        """
        Executes Fill-Or-Kill orders to capture the arbitrage.
        Includes negRisk boolean as requested in API documentation issue #79.
        """
        yes_token = market_data['yes']
        no_token = market_data['no']
        tick_size = market_data['tick_size']

        # Round price and amount to tick size and min size to prevent "invalid signature" 
        # and "invalid tick size" errors. Polymarket requires exact multiples.
        # Most BTC 5m markets have tick_size 0.01 or 0.001.
        import math
        price_decimals = max(0, int(-math.log10(tick_size)))
        
        # Round prices 
        yes_price_rounded = round(yes_price, price_decimals)
        no_price_rounded = round(no_price, price_decimals)
        
        # Size must be rounded to 1 decimal place (0.1 increment) for most CLOB markets
        # or follow the min size. For BTC 5m, 0.1 or 1.0 is standard.
        # We'll round to 2 decimals to be safe but usually 0.1 is the limit.
        amount_rounded = round(amount, 2)

        logger.info(f"Executing Arbitrage FOK Orders. Buying {amount_rounded} shares of YES @ {yes_price_rounded} & {amount_rounded} shares of NO @ {no_price_rounded}")

        # Construct raw payload YES
        yes_order_args = OrderArgs(
            price=yes_price_rounded,
            size=amount_rounded,
            side=BUY,
            token_id=yes_token,
        )

        no_order_args = OrderArgs(
            price=no_price_rounded,
            size=amount_rounded,
            side=BUY,
            token_id=no_token,
        )

        try:
            # We must use FOK to prevent legged trades
            # Pass negRisk parameter for negative risk markets to prevent signature invalidation
            # The client abstract creation, signing, and posting
            from py_clob_client.clob_types import CreateOrderOptions
            
            options = CreateOrderOptions(
                tick_size=str(tick_size),
                neg_risk=market_data.get('negRisk', False)
            )

            if self.dry_run:
                logger.info(f"[PAPER TRADE] Theoretical YES Fill @ {yes_price} (Vol: {amount})")
                logger.info(f"[PAPER TRADE] Theoretical NO Fill  @ {no_price} (Vol: {amount})")
                
                total_cost = (yes_price + no_price) * amount + yes_fee + no_fee
                expected_value = amount * 1.0
                profit = expected_value - total_cost
                logger.info(f"[PAPER TRADE] Gross Spread Cost: {total_cost:.4f}. Theoretical PnL: {profit:.4f}")
                
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
                    "total_cost": total_cost,
                    "theoretical_profit": profit
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

            logger.info(f"YES Order POST: {resp_yes}")
            logger.info(f"NO Order POST: {resp_no}")
            
            # Post-arbitrage, if successful we should convert Negative Risk back to USDC if applicable.
            if market_data.get('negRisk') and not self.dry_run:
                # USDC has 6 decimals on Polygon. Merging the tokens returns 1 USDC per pair.
                logger.info(f"Merging Negative Risk tokens to recover capital for condition {market_data['condition_id']}")
                try:
                    self.blockchain.convert_negative_risk(market_data['condition_id'], int(amount_rounded * 10**6))
                except Exception as ex:
                    logger.error(f"Failed to convert Negative Risk tokens: {ex}")

        except Exception as e:
            logger.error(f"Execution failed: {e}")

    async def on_price_update(self, current_prices: dict):
        """
        Callback from the WebSocket stream.
        """
        # Note: we use asyncio.gather for parallel evaluation
        tasks = []
        for market in self.markets:
            tasks.append(self.evaluate_market(market, current_prices, print_summary=bot_config.PRINT_VERBOSE_SPREADS))
        await asyncio.gather(*tasks)

    async def run_background_reporting(self, streamer):
        """
        Prints the current known spreads exactly every 10 seconds.
        Decoupled from websocket updates so it prints even when the market is illiquid.
        """
        while True:
            await asyncio.sleep(10)
            logger.info("--- Live Market Spread Scan ---")
            
            for market in self.markets:
                yes_token = market['yes']
                no_token = market['no']
                # Grab latest known prices from the streamer object
                yes_data = streamer.best_prices.get(yes_token, {})
                no_data = streamer.best_prices.get(no_token, {})
                yes_ask = yes_data.get("ask", 0)
                no_ask = no_data.get("ask", 0)
                
                if yes_ask > 0 or no_ask > 0:
                    logger.info(f"Market {market['market_id'][-6:]}: YES ask @ ${yes_ask:.2f} | NO ask @ ${no_ask:.2f}")
                
            # Attempt an evaluation pass simply to trigger the detailed `No Arb` reason prints
            for market in self.markets:
                await self.evaluate_market(market, streamer.best_prices, print_summary=bot_config.PRINT_VERBOSE_SPREADS)
