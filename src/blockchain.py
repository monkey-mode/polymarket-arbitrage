import json
import logging
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from src import config

logger = logging.getLogger(__name__)

# Basic ABI for ERC20 approve
ERC20_ABI = json.loads('''[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]''')

# Basic ABI for ERC1155 setApprovalForAll and CTF mergePositions
ERC1155_ABI = json.loads('''[
    {"constant":false,"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},
    {"constant":true,"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"view","type":"function"},
    {"constant":false,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"partition","type":"uint256[]"},{"name":"amount","type":"uint256"}],"name":"mergePositions","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},
    {"constant":true,"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}
]''')

# Basic ABI for the NegRiskAdapter
NEG_RISK_ABI = json.loads('''[{"inputs":[{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"convert","outputs":[],"stateMutability":"nonpayable","type":"function"}]''')

class BlockchainManager:
    def __init__(self):
        uri = config.WEB3_PROVIDER_URI
        if uri.startswith("wss://") or uri.startswith("ws://"):
            self.w3 = Web3(Web3.LegacyWebSocketProvider(uri))
        else:
            self.w3 = Web3(Web3.HTTPProvider(uri))
            
        # Inject PoA middleware for Polygon compatibility (extraData field length)
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        self.funder = Web3.to_checksum_address(config.FUNDER_ADDRESS)
        self.pk = config.WALLET_PRIVATE_KEY
        self.signer_address = self.w3.eth.account.from_key(self.pk).address

        self.usdc_contract = self.w3.eth.contract(address=Web3.to_checksum_address(config.Contracts.USDC), abi=ERC20_ABI)
        
        # Outcome tokens reside on CTF Exchange contracts or proxy. For Polymarket, conditional tokens are minted centrally.
        # But we approve the exchanges to spend our CTF tokens.
        # Note: Polymarket tokens use a universal ERC1155 conditional token contract. We would need its address to call setApprovalForAll.
        # Assuming the standard Conditional Token address on Polygon:
        self.ctf_token_address = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        self.ctf_contract = self.w3.eth.contract(address=self.ctf_token_address, abi=ERC1155_ABI)
        
        self.neg_risk_adapter_address = Web3.to_checksum_address(config.Contracts.NEG_RISK_ADAPTER)
        self.neg_risk_contract = self.w3.eth.contract(address=self.neg_risk_adapter_address, abi=NEG_RISK_ABI)

    def send_tx(self, func_call):
        from web3.exceptions import TransactionNotFound, TimeExhausted
        
        # Initial Gas Setup
        try:
            latest_block = self.w3.eth.get_block('latest')
            base_fee = latest_block.get('baseFeePerGas', self.w3.to_wei('30', 'gwei'))
            # Polygon standard: 30 Gwei priority fee is usually enough, 
            # but we use 50 as a safer baseline for arbitrage
            priority_fee = self.w3.to_wei('150', 'gwei')
            max_fee = int(base_fee * 1.5) + priority_fee  # 50% buffer on base fee
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic gas, falling back to 150 Gwei: {e}")
            max_fee = self.w3.to_wei('150', 'gwei')
            priority_fee = self.w3.to_wei('50', 'gwei')

        attempt = 0
        while attempt < 3:
            # Nonce must match the address that is SIGNING the transaction (the one paying for gas)
            nonce = self.w3.eth.get_transaction_count(self.signer_address)
            tx = func_call.build_transaction({
                'chainId': config.CHAIN_ID,
                'gas': 2000000,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'nonce': nonce,
            })
            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.pk)
            
            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                logger.info(f"Transaction sent: {tx_hash.hex()} (Max: {max_fee/1e9:.1f} Gwei, Priority: {priority_fee/1e9:.1f} Gwei)")
                
                # Wait for receipt
                try:
                    receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                    logger.info(f"Transaction receipt: {receipt.status}")
                    return receipt
                except TimeExhausted:
                    logger.warning(f"Transaction still pending after 120s: {tx_hash.hex()}. Bumping gas...")
                    # Bump gas for next attempt with same nonce
                    max_fee = int(max_fee * 1.25)
                    priority_fee = int(priority_fee * 1.25)
                    attempt += 1
                    continue

            except Exception as e:
                err_str = str(e)
                if "underpriced" in err_str or "low priority" in err_str:
                    logger.warning(f"Replacement underpriced. Bumping gas and retrying (Attempt {attempt+1})...")
                    max_fee = int(max_fee * 1.5)
                    priority_fee = int(priority_fee * 1.5)
                    attempt += 1
                    continue
                else:
                    logger.error(f"Blockchain setup failed: {e}")
                    raise e
        
        raise Exception("Failed to send transaction after multiple gas bumps.")

    def approve_usdc(self, spender_address, amount=(2**256 - 1)):
        spender = Web3.to_checksum_address(spender_address)
        allowance = self.usdc_contract.functions.allowance(self.funder, spender).call()
        if allowance < amount / 2:
            logger.info(f"Approving USDC for {spender_address}")
            self.send_tx(self.usdc_contract.functions.approve(spender, amount))
        else:
            logger.info(f"USDC already approved for {spender_address}")

    def approve_ctf_tokens(self, operator_address):
        operator = Web3.to_checksum_address(operator_address)
        is_approved = self.ctf_contract.functions.isApprovedForAll(self.funder, operator).call()
        if not is_approved:
            logger.info(f"Setting CTF Token approval for {operator_address}")
            self.send_tx(self.ctf_contract.functions.setApprovalForAll(operator, True))
        else:
            logger.info(f"CTF Tokens already approved for {operator_address}")

    def setup_all_approvals(self):
        # Approve Core Exchange
        self.approve_usdc(config.Contracts.CTF_EXCHANGE)
        self.approve_ctf_tokens(config.Contracts.CTF_EXCHANGE)

        # Approve Neg Risk Exchange
        self.approve_usdc(config.Contracts.NEG_RISK_CTF_EXCHANGE)
        self.approve_ctf_tokens(config.Contracts.NEG_RISK_CTF_EXCHANGE)
        
        # Approve Neg Risk Adapter
        self.approve_ctf_tokens(config.Contracts.NEG_RISK_ADAPTER)

    def convert_negative_risk(self, condition_id_hex, amount):
        """
        Executes the atomic conversion of NO tokens -> YES tokens via the NegRiskAdapter.
        `amount` is in uint256 token units (likely 6 decimals like USDC).
        """
        condition_id_bytes = Web3.to_bytes(hexstr=condition_id_hex)
        logger.info(f"Converting NO tokens for condition {condition_id_hex}")
        return self.send_tx(self.neg_risk_contract.functions.convert(condition_id_bytes, amount))

    def get_token_balance(self, address, token_id):
        """
        Returns the on-chain balance of an ERC1155 token (outcome share).
        """
        try:
            # Polymarket token IDs are uint256
            tid = int(token_id) if isinstance(token_id, str) and token_id.isdigit() else int(token_id, 16) if isinstance(token_id, str) and token_id.startswith('0x') else int(token_id)
            return self.ctf_contract.functions.balanceOf(Web3.to_checksum_address(address), tid).call()
        except Exception as e:
            logger.error(f"Failed to fetch balance for {token_id}: {e}")
            return 0

    def get_usdc_balance(self, address: str = None) -> float:
        """Returns the USDC.e balance of the given address (or funder) in dollars."""
        target = Web3.to_checksum_address(address or self.funder)
        try:
            usdc = self.w3.eth.contract(
                address=Web3.to_checksum_address(config.Contracts.USDC),
                abi=[{"constant": True, "inputs": [{"name": "account", "type": "address"}],
                      "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                      "type": "function"}],
            )
            return usdc.functions.balanceOf(target).call() / 10**6
        except Exception as e:
            logger.error(f"Failed to fetch USDC balance: {e}")
            return 0.0

