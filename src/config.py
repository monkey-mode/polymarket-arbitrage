import os
from dotenv import load_dotenv

load_dotenv()

WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", WALLET_ADDRESS) # Defaults to the wallet address
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))      # 0=EOA, 1=Proxy, 2=Alt Proxy

HOST = os.getenv("HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
WEB3_PROVIDER_URI = os.getenv("WEB3_PROVIDER_URI", "https://polygon-rpc.com")

PRINT_VERBOSE_SPREADS = os.getenv("PRINT_VERBOSE_SPREADS", "true").lower() == "true"
# Redis Logging Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_LOG_CHANNEL = "polymarket_bot_logs"
REDIS_LOG_MAX_BYTES = int(os.getenv("REDIS_LOG_MAX_BYTES", "1048576")) # 1MB default
REDIS_LOG_BACKUP_COUNT = int(os.getenv("REDIS_LOG_BACKUP_COUNT", "5"))

# Dynamic Fee Architecture Parameters (2026 implementations)
class FeeConfig:
    # Crypto Markets (5-Minute, 15-Minute, 1H, 4H, Daily, Weekly)
    CRYPTO_RATE = 0.0075    # Translates to ~1.5% Peak Effective Fee
    CRYPTO_EXPONENT = 2
    CRYPTO_MAKER_REBATE = 0.20
    
    # Sports Markets (NCAAB, Serie A)
    SPORTS_RATE = 0.0022    # Translates to ~0.44% Peak Effective Fee
    SPORTS_EXPONENT = 1
    SPORTS_MAKER_REBATE = 0.25

# Contract Addresses
class Contracts:
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
    USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Polygon Bridged USDC
