import asyncio
import logging
from src.redis_logger import RedisLogger

# Configure Logging for the subscriber process itself
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Subscriber")

async def main():
    logger.info("Starting Redis Subscriber Service...")
    redis_logger = RedisLogger()
    try:
        await redis_logger.run_subscriber()
    except asyncio.CancelledError:
        logger.info("Subscriber service stopping...")
    except Exception as e:
        logger.error(f"Subscriber service failed: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
