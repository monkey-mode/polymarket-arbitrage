"""
Order Executor

Handles FOK order placement, legging safety, emergency liquidation,
and retry logic. All exchange interactions go through PolymarketClient.
"""
import math
import asyncio
import logging

from py_clob_client.clob_types import MarketOrderArgs, OrderType, CreateOrderOptions
from py_clob_client.order_builder.constants import BUY

from src.v2.exchange.client import PolymarketClient

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Places and manages orders on the Polymarket CLOB."""

    def __init__(self, client: PolymarketClient, dry_run: bool = False):
        self.client = client
        self.dry_run = dry_run

    async def execute_arbitrage(
        self,
        market_data: dict,
        amount: float,
        yes_price: float,
        no_price: float,
        yes_fee: float,
        no_fee: float,
    ) -> dict | None:
        """
        Places FOK orders for both YES and NO sides.

        Returns a dict with order responses and match status, or None on failure.
        Handles legging safety: if only one side fills, cancels + liquidates the other.
        """
        yes_token = market_data["yes"]
        no_token = market_data["no"]
        tick_size = market_data["tick_size"]

        price_decimals = max(0, int(-math.log10(tick_size)))
        yes_price_rounded = round(yes_price, price_decimals)
        no_price_rounded = round(no_price, price_decimals)
        amount_rounded = round(amount, 2) 

        try:
            yes_order_args = MarketOrderArgs(
                price=yes_price_rounded,
                amount=amount_rounded,
                side=BUY,
                token_id=yes_token,
            )

            no_order_args = MarketOrderArgs(
                price=no_price_rounded,
                amount=amount_rounded,
                side=BUY,
                token_id=no_token,
            )

            options = CreateOrderOptions(
                tick_size="0.01",
                neg_risk=market_data.get("negRisk", False),
            )

            # Pass FOK as the order_type argument — sets it correctly in the API call
            logger.info(f"YES Order Args: {yes_order_args}")
            resp_yes = self.client.place_limit_order(yes_order_args, options, OrderType.FOK)
            logger.info(f"NO Order Args: {no_order_args}")
            resp_no = self.client.place_limit_order(no_order_args, options, OrderType.FOK)

            logger.info(f"YES Order: {resp_yes}")
            logger.info(f"NO Order: {resp_no}")
            logger.info(f"YES Status: {resp_yes.get('status')} | NO Status: {resp_no.get('status')}")

            # "live" means the order leaked onto the book (was NOT treated as FOK).
            # Cancel any such orders immediately before evaluating match status.
            if resp_yes.get("status") == "live":
                logger.warning("YES order resting on book (expected FOK) — cancelling.")
                self._cancel_if_exists(resp_yes)
            if resp_no.get("status") == "live":
                logger.warning("NO order resting on book (expected FOK) — cancelling.")
                self._cancel_if_exists(resp_no)

            # Only "matched" counts as a successful FOK fill
            yes_matched = resp_yes.get("success") and resp_yes.get("status") == "matched"
            no_matched = resp_no.get("success") and resp_no.get("status") == "matched"

            # --- Legging Safety ---
            if yes_matched != no_matched:
                logger.error(f"Legging Risk! YES: {yes_matched}, NO: {no_matched}")

                if not self.dry_run:
                    matched_resp = resp_yes if yes_matched else resp_no
                    matched_token = yes_token if yes_matched else no_token
                    actual_qty = float(matched_resp.get("takingAmount") or 0)

                    if actual_qty > 0:
                        logger.warning(f"Emergency Cleanup: selling {actual_qty:.3f} orphan tokens.")
                        await self.liquidate_token(matched_token, actual_qty)
                    else:
                        logger.error("Legging detected but zero fill. Manual check required.")
                return None

            if not yes_matched and not no_matched:
                logger.info("FOK triggered — neither side filled. Skipping.")
                return None

            return {
                "yes_matched": True,
                "no_matched": True,
                "amount_rounded": amount_rounded,
            }

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return None

    async def liquidate_token(self, token_id: str, qty: float) -> None:
        """Market-sells a specific token. Used for emergency cleanup and planned exits."""
        if qty <= 0:
            return
        logger.info(f"Liquidating {qty:.3f} shares of {token_id[-8:]}...")
        try:
            resp = self.client.create_market_sell(token_id, qty)
            logger.info(f"EXIT order for {token_id[-8:]}: {resp.get('status', 'Unknown')}")
        except Exception as e:
            logger.error(f"Liquidation failed for {token_id[-8:]}: {e}")

    async def active_exit(self, market_data: dict, inventory: dict) -> None:
        """Liquidates all positions for a market before expiry."""
        yes_token = market_data["yes"]
        no_token = market_data["no"]

        yes_qty = inventory.get(yes_token, 0)
        no_qty = inventory.get(no_token, 0)

        if yes_qty <= 0 and no_qty <= 0:
            return

        logger.info(f"Liquidating positions for {market_data['market_id'][-6:]}: "
                     f"{yes_qty:.3f} YES, {no_qty:.3f} NO")

        await asyncio.gather(
            self.liquidate_token(yes_token, yes_qty),
            self.liquidate_token(no_token, no_qty),
        )

        inventory[yes_token] = 0
        inventory[no_token] = 0

    def _cancel_if_exists(self, resp: dict) -> None:
        """Cancels an order if it has an orderID."""
        order_id = resp.get("orderID")
        if order_id:
            self.client.cancel_order(order_id)
