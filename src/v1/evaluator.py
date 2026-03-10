"""
Market Evaluator

Pure evaluation logic — checks whether an arbitrage opportunity exists
for a given market. No side effects, no order posting.
"""
import logging

from src.config import FeeConfig

logger = logging.getLogger(__name__)


class MarketEvaluator:
    """Evaluates binary markets for Dutch-Book arbitrage opportunities."""

    def __init__(self, target_amount: float = 5.01):
        self.target_amount = target_amount

    def calculate_taker_fee(self, amount: float, price: float, token_id: str, rate_type: str = "crypto") -> float:
        """
        Applies the 2026 Continuous Fee Formula using the live API base fee rate.
        """
        if rate_type == "crypto":
            exp = FeeConfig.CRYPTO_EXPONENT
        else:
            exp = FeeConfig.SPORTS_EXPONENT

        rate = 0.0
        fee_raw = amount * rate * (1 - (2 * abs(price - 0.5)) ** exp)
        return 0.0 if fee_raw < 0.0001 else fee_raw

    def check_liquidity(self, current_prices: dict, yes_token: str, no_token: str) -> bool:
        """Returns True if there is enough size on both sides for our target amount."""
        yes_sz = current_prices[yes_token].get("ask_sz", 0)
        no_sz = current_prices[no_token].get("ask_sz", 0)
        return yes_sz >= self.target_amount and no_sz >= self.target_amount

    def evaluate(self, market_data: dict, current_prices: dict) -> dict | None:
        """
        Evaluates a single market for arbitrage.

        Returns a dict with trade parameters if an opportunity exists, else None.
        """
        market_id = market_data.get("market_id", "?")[-6:]
        yes_token = market_data["yes"]
        no_token = market_data["no"]

        if yes_token not in current_prices or no_token not in current_prices:
            logger.debug(f"Market {market_id}: Waiting for price data")
            return None

        yes_ask = current_prices[yes_token]["ask"]
        no_ask = current_prices[no_token]["ask"]

        if yes_ask == 0 or no_ask == 0:
            logger.debug(f"Market {market_id}: Zero ask price (Y:{yes_ask} N:{no_ask})")
            return None

        # if not self.check_liquidity(current_prices, yes_token, no_token):
        #     yes_sz = current_prices[yes_token].get("ask_sz", 0)
        #     no_sz = current_prices[no_token].get("ask_sz", 0)
        #     logger.debug(
        #         f"Market {market_id}: Insufficient liquidity — "
        #         f"need {self.target_amount}, have Y:{yes_sz:.1f} N:{no_sz:.1f}"
        #     )
        #     return None

        if self.target_amount < 1:
            return None

        yes_fee = self.calculate_taker_fee(self.target_amount, yes_ask, yes_token)
        no_fee = self.calculate_taker_fee(self.target_amount, no_ask, no_token)

        total_cost = (yes_ask * self.target_amount) + yes_fee + (no_ask * self.target_amount) + no_fee
        expected_payout = self.target_amount * 1.0  # 1 USDC per YES+NO pair
        ask_sum = yes_ask + no_ask

        if total_cost > int(self.target_amount):
            logger.debug(f"Market {market_id}: Total cost exceeds target amount")
            return None

        if total_cost < expected_payout and ask_sum < 1.0:
            profit = expected_payout - total_cost
            return {
                "amount": self.target_amount,
                "yes_price": yes_ask,
                "no_price": no_ask,
                "yes_fee": yes_fee,
                "no_fee": no_fee,
                "total_cost": total_cost,
                "profit": profit,
            }

        logger.debug(
            f"Market {market_id}: No arb — "
            f"Y:{yes_ask:.2f} + N:{no_ask:.2f} = {ask_sum:.4f} "
            f"(cost ${total_cost:.4f} vs payout ${expected_payout:.4f})"
        )
        return None
