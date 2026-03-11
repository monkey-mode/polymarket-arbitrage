"""
Arbitrage Strategy — Orchestrator

Thin coordinator that composes MarketEvaluator, OrderExecutor, and PositionMerger
to discover and execute arbitrage opportunities on Polymarket binary markets.
"""
import json
import logging
import asyncio
from datetime import datetime

from src.v2.exchange.client import PolymarketClient
from src.v2.chain.blockchain import BlockchainManager
from src.v2.trading.evaluator import MarketEvaluator
from src.v2.trading.executor import OrderExecutor
from src.v2.trading.merger import PositionMerger
from src.v2.infra.redis_logger import RedisLogger
import src.v2.config as bot_config

logger = logging.getLogger(__name__)


class ArbitrageStrategy:
    """
    Top-level orchestrator.

    - `evaluator`  checks if an arb opportunity exists (pure math, no side effects)
    - `executor`   places FOK orders and handles legging risk
    - `merger`     merges YES+NO pairs back into USDC on-chain
    """

    def __init__(
        self,
        client: PolymarketClient,
        markets: list,
        blockchain: BlockchainManager,
        dry_run: bool = False,
        redis_logger: RedisLogger = None,
        one_shot: bool = False,
    ):
        self.client = client
        self.markets = markets
        self.blockchain = blockchain
        self.dry_run = dry_run
        self.redis_logger = redis_logger
        self.one_shot = one_shot

        self.evaluator = MarketEvaluator(target_amount=5.01)
        self.executor = OrderExecutor(client, dry_run=dry_run)
        self.merger = PositionMerger(client, blockchain)

        self.inventory: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    #  Core Loop
    # ------------------------------------------------------------------ #

    async def on_price_update(self, current_prices: dict) -> None:
        """Callback from the WebSocket stream — evaluates every tracked market."""
        tasks = [
            self._process_market(market, current_prices)
            for market in self.markets
        ]
        await asyncio.gather(*tasks)

    async def _process_market(self, market_data: dict, current_prices: dict) -> None:
        """Evaluate a single market and execute if profitable."""
        import time
        end_ts = market_data.get("end_timestamp", 0)
        if end_ts and (end_ts - time.time()) <= 30:
            return  # Too close to market close — skip new trades

        result = self.evaluator.evaluate(market_data, current_prices)

        if result is None:
            # Optionally log the spread if verbose
            if bot_config.PRINT_VERBOSE_SPREADS and self.redis_logger:
                yes_token = market_data["yes"]
                no_token = market_data["no"]
                if yes_token in current_prices and no_token in current_prices:
                    await self._publish_no_arb(market_data, current_prices)
            return

        logger.info(
            f"Arbitrage Found! Shares: {result['amount']:.2f}, "
            f"Cost: ${result['total_cost']:.4f}, Profit: ${result['profit']:.4f}"
        )

        if self.redis_logger:
            await self.redis_logger.publish_log({
                "event": "ARBITRAGE_FOUND",
                "market_id": market_data["market_id"][-6:],
                "yes_ask": result["yes_price"],
                "no_ask": result["no_price"],
                "total_cost": result["total_cost"],
                "fees": result["yes_fee"] + result["no_fee"],
                "profit": result["profit"],
            })

        if self.dry_run:
            self._record_paper_trade(market_data, result)
            return

        # Fund check: skip if wallet doesn't have enough USDC
        usdc_balance = self.blockchain.get_usdc_balance()
        if usdc_balance < result["total_cost"]:
            logger.warning(
                f"Insufficient funds: need ${result['total_cost']:.2f}, "
                f"have ${usdc_balance:.2f} USDC. Skipping until funded."
            )
            return

        # Execute the trade
        execution = await self.executor.execute_arbitrage(
            market_data,
            result["amount"],
            result["yes_price"],
            result["no_price"],
            result["yes_fee"],
            result["no_fee"],
        )

        if execution is None:
            if self.one_shot:
                logger.info("One-shot mode: trade failed. Stopping for manual inspection.")
                raise SystemExit(0)
            return  # Failed or legged — already handled by executor

        # Track inventory for planned exit
        amt = execution["amount_rounded"]
        self.inventory[market_data["yes"]] = self.inventory.get(market_data["yes"], 0) + amt
        self.inventory[market_data["no"]] = self.inventory.get(market_data["no"], 0) + amt

        if self.one_shot:
            logger.info("One-shot mode: trade executed. Stopping for manual inspection.")
            raise SystemExit(0)

    # ------------------------------------------------------------------ #
    #  Exit & Reporting
    # ------------------------------------------------------------------ #

    async def active_exit(self, market_data: dict, current_prices: dict = None,
                          forced_token: str = None, forced_qty: float = None) -> None:
        """Merges accumulated pairs then liquidates remaining positions before market close."""
        if forced_token and forced_qty:
            await self.executor.liquidate_token(forced_token, forced_qty)
            return

        # Merge all accumulated YES+NO pairs back into USDC before closing
        await self.merger.merge(market_data, market_data["yes"], market_data["no"])

        # Liquidate any remaining unmatched positions
        await self.executor.active_exit(market_data, self.inventory)

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _record_paper_trade(self, market_data: dict, result: dict) -> None:
        """Appends a simulated trade to paper_trades.json."""
        logger.info(f"[PAPER TRADE] Arb Found (Vol: {result['amount']})")
        trade_record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "market_id": market_data.get("market_id"),
            "condition_id": market_data.get("condition_id"),
            "trade": "YES/NO ARB",
            "amount": result["amount"],
            "yes_price": result["yes_price"],
            "no_price": result["no_price"],
            "yes_fee": result["yes_fee"],
            "no_fee": result["no_fee"],
            "total_cost": result["total_cost"],
            "theoretical_profit": result["profit"],
        }
        try:
            with open("paper_trades.json", "r+") as f:
                data = json.load(f)
                data.append(trade_record)
                f.seek(0)
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to record paper trade: {e}")

    async def _publish_no_arb(self, market_data: dict, current_prices: dict) -> None:
        """Publishes a NO_ARB event to Redis for monitoring."""
        yes_ask = current_prices[market_data["yes"]]["ask"]
        no_ask = current_prices[market_data["no"]]["ask"]
        total_cost = (yes_ask + no_ask) * self.evaluator.target_amount
        await self.redis_logger.publish_log({
            "event": "NO_ARB",
            "market_id": market_data["market_id"][-6:],
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "total_cost": total_cost,
            "fees": 0,
        })
