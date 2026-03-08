import logging
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

from src import config

logger = logging.getLogger(__name__)

def initialize_client() -> ClobClient:
    """
    Initializes the ClobClient with Level 2 credentials using the EOA signature type.
    """
    if not config.WALLET_PRIVATE_KEY or not config.WALLET_ADDRESS:
        raise ValueError("Missing WALLET_PRIVATE_KEY or WALLET_ADDRESS in environment variables.")

    logger.info(f"Initializing ClobClient for host {config.HOST} on chain {config.CHAIN_ID}")
    
    # Using EOA wallet (Signature Type 0 as per the PDF)
    from web3 import Web3
    funder_address = Web3.to_checksum_address(config.FUNDER_ADDRESS)
    
    client = ClobClient(
        host=config.HOST,
        key=config.WALLET_PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        signature_type=config.SIGNATURE_TYPE, 
        funder=funder_address
    )

    client.set_api_creds(client.create_or_derive_api_creds())
    logger.info("Successfully derived and set API credentials.")
    return client
