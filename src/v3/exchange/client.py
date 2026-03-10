import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()


class PolymarketClient:
    def __init__(self):
        self.host = os.getenv("HOST", "https://clob.polymarket.com")
        self.chain_id = int(os.getenv("CHAIN_ID", "137"))
        self.private_key = os.getenv("WALLET_PRIVATE_KEY")
        self.funder = os.getenv("FUNDER_ADDRESS")
        self.client = self._build()

    def _build(self) -> ClobClient:
        # Step 1: L1 auth — derive API credentials from wallet signature
        temp = ClobClient(self.host, key=self.private_key, chain_id=self.chain_id)
        creds = temp.create_or_derive_api_creds()

        # Step 2: L2 auth — full trading client (POLY_PROXY = exported PK from polymarket.com)
        return ClobClient(
            self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            creds=creds,
            signature_type=1,
            funder=self.funder,
        )

    def ok(self) -> bool:
        return self.client.get_ok()


if __name__ == "__main__":
    pm = PolymarketClient()
    print("Connected:", pm.ok())
