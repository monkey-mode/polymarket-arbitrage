"""
Position Merger

Handles merging YES + NO token pairs back into USDC via the CTF contract.
Contract addresses are resolved from the CLOB client, not hardcoded.
"""
import asyncio
import logging

from web3 import Web3

from src.client import PolymarketClient
from src.blockchain import BlockchainManager

logger = logging.getLogger(__name__)

# Minimal ABI for the mergePositions call on the CTF contract
MERGE_ABI = [{
    "constant": False,
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "partition", "type": "uint256[]"},
        {"name": "amount", "type": "uint256"},
    ],
    "name": "mergePositions",
    "outputs": [],
    "payable": False,
    "stateMutability": "nonpayable",
    "type": "function",
}]


class PositionMerger:
    """Merges matched YES+NO pairs back into USDC collateral."""

    def __init__(self, client: PolymarketClient, blockchain: BlockchainManager):
        self.client = client
        self.blockchain = blockchain

    async def merge(self, market_data: dict, yes_token: str, no_token: str) -> bool:
        """
        Queries on-chain balances, then calls mergePositions on the CTF contract.

        Returns True if the merge succeeded, False otherwise.
        """
        # Give Polygon indexing a moment before querying balances
        await asyncio.sleep(2)

        try:
            logger.info("Querying on-chain balances for precision merge...")
            bal_yes = self.blockchain.get_token_balance(self.blockchain.funder, yes_token)
            bal_no = self.blockchain.get_token_balance(self.blockchain.funder, no_token)

            merge_qty = min(bal_yes, bal_no)
            if merge_qty == 0:
                logger.warning(f"Merge skipped: balances too low (YES: {bal_yes}, NO: {bal_no})")
                return False

            # Resolve addresses from the CLOB client (not hardcoded)
            ctf_address = Web3.to_checksum_address(self.client.get_ctf_address())
            collateral = Web3.to_checksum_address(self.client.get_usdc_address())

            condition_id = Web3.to_bytes(hexstr=market_data["condition_id"])
            parent_id = b"\x00" * 32
            partition = [1, 2]  # YES=1, NO=2 for binary markets

            ctf_contract = self.blockchain.w3.eth.contract(address=ctf_address, abi=MERGE_ABI)

            logger.info(f"Merging {merge_qty / 10**6:.6f} pairs → USDC via CTF @ {ctf_address}")
            self.blockchain.send_tx(
                ctf_contract.functions.mergePositions(
                    collateral, parent_id, condition_id, partition, merge_qty
                )
            )
            return True

        except Exception as ex:
            logger.error(f"Merge failed: {ex}")
            return False
