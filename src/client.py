"""
Polymarket Client Wrapper

Encapsulates all ClobClient interactions behind clean, descriptive methods.
Other modules should NEVER import or call ClobClient directly — use this instead.
"""
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs, CreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL

from src import config

logger = logging.getLogger(__name__)


class PolymarketClient:
    """
    High-level wrapper around py-clob-client's ClobClient.
    Provides named methods for every exchange interaction the bot needs.
    """

    def __init__(self):
        from web3 import Web3

        if not config.WALLET_PRIVATE_KEY or not config.WALLET_ADDRESS:
            raise ValueError("Missing WALLET_PRIVATE_KEY or WALLET_ADDRESS in environment variables.")

        funder_address = Web3.to_checksum_address(config.FUNDER_ADDRESS)

        logger.info(f"Initializing PolymarketClient for {config.HOST} (chain {config.CHAIN_ID})")
        self._client = ClobClient(
            host=config.HOST,
            key=config.WALLET_PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=config.SIGNATURE_TYPE,
            funder=funder_address,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        logger.info("API credentials derived successfully.")

    # ------------------------------------------------------------------ #
    #  Order Placement
    # ------------------------------------------------------------------ #

    def place_limit_order(self, order_args: OrderArgs, options: CreateOrderOptions) -> dict:
        """Create, sign, and post a limit order in one call."""
        return self._client.create_and_post_order(order_args, options)

    def create_market_sell(self, token_id: str, amount: float) -> dict:
        """Create a market sell order and post it as GTC for immediate fill."""
        sell_args = MarketOrderArgs(amount=round(amount, 2), side=SELL, token_id=token_id)
        signed = self._client.create_market_order(sell_args)
        return self._client.post_order(signed, OrderType.GTC)

    def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order by its ID."""
        try:
            self._client.cancel_order(order_id)
        except Exception as e:
            logger.warning(f"Cancel order {order_id} failed: {e}")

    # ------------------------------------------------------------------ #
    #  Contract Addresses (resolved from CLOB config, not hardcoded)
    # ------------------------------------------------------------------ #

    def get_ctf_address(self) -> str:
        """Returns the Conditional Token Framework (ERC-1155) contract address."""
        return self._client.get_conditional_address()

    def get_usdc_address(self) -> str:
        """Returns the USDC.e collateral token address."""
        return self._client.get_collateral_address()

    # ------------------------------------------------------------------ #
    #  Health & Utilities
    # ------------------------------------------------------------------ #

    def heartbeat(self) -> None:
        """Send an authenticated keep-alive ping to the CLOB."""
        self._client.get_ok()
