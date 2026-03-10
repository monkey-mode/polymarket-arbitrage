import asyncio
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from redis.asyncio import Redis
import src.v2.config as bot_config
from datetime import datetime

logger = logging.getLogger(__name__)

# Custom logging logic moved to namer attribute within RedisLogger class.

class RedisLogger:
    def __init__(self, filename="logs/market_evaluations.log"):
        self.redis = Redis(host=bot_config.REDIS_HOST, port=bot_config.REDIS_PORT, decode_responses=True)
        self.channel = bot_config.REDIS_LOG_CHANNEL

        # Ensure log directory exists
        log_dir = os.path.dirname(filename)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # Cleanup legacy files if they exist (old .log.1 style) to prevent rollover collisions
        for i in range(1, bot_config.REDIS_LOG_BACKUP_COUNT + 2):
            old_style = f"{filename}.{i}"
            if os.path.exists(old_style):
                try:
                    os.remove(old_style)
                    logger.info(f"Cleaned up legacy log file: {old_style}")
                except:
                    pass

        # Set up a dedicated local logger for the file output with rotation
        self.file_logger = logging.getLogger("RedisFileLogger")
        self.file_logger.setLevel(logging.INFO)
        # Prevent logs from propagating to the root logger (and thus stdout)
        self.file_logger.propagate = False

        # Clear existing handlers to avoid duplicates on restart
        self.file_logger.handlers = []

        # Standard RotatingFileHandler
        handler = RotatingFileHandler(
            filename,
            maxBytes=bot_config.REDIS_LOG_MAX_BYTES,
            backupCount=bot_config.REDIS_LOG_BACKUP_COUNT
        )

        # Modern way to handle custom filenames: use the 'namer' and 'rotator' attributes
        def my_namer(default_name):
            # Converts 'name.log.1' -> 'name_1.log'
            if ".log." in default_name:
                base, index = default_name.split(".log.")
                return f"{base}_{index}.log"
            return default_name

        def my_rotator(source, dest):
            # Robust rotator with error handling to prevent FileNotFoundError race conditions
            try:
                if os.path.exists(source):
                    if os.path.exists(dest):
                        os.remove(dest)
                    os.rename(source, dest)
            except FileNotFoundError:
                # File already moved by another handle/process or check was stale
                pass
            except Exception as e:
                # If OS is slow to release locks, wait a tiny bit and try again once
                import time
                time.sleep(0.1)
                try:
                    if os.path.exists(source):
                        if os.path.exists(dest): os.remove(dest)
                        os.rename(source, dest)
                except:
                    pass

        handler.namer = my_namer
        handler.rotator = my_rotator

        # Plain formatter to keep the exact message format
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.file_logger.addHandler(handler)

    async def publish_log(self, message_dict: dict):
        """Asynchronously publish a JSON log message to the Redis channel."""
        try:
            message_dict['timestamp'] = datetime.utcnow().isoformat() + "Z"
            await self.redis.publish(self.channel, json.dumps(message_dict))
        except Exception as e:
            logger.error(f"Redis publish failed: {e}")

    async def run_subscriber(self):
        """Background task that listens to the Redis channel and writes to a rotating file."""
        try:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(self.channel)
            logger.info(f"Redis Subscriber listening on channel: {self.channel}")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        event_type = data.get("event", "LOG")
                        log_line = f"[{data.get('timestamp')}] [{event_type}] Market {data.get('market_id')} | Y:${data.get('yes_ask'):.2f} N:${data.get('no_ask'):.2f} | Cost: ${data.get('total_cost'):.4f} | Fees: ${data.get('fees'):.4f}"

                        # Use the rotating file logger
                        self.file_logger.info(log_line)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.error(f"Redis subscriber encountered an error: {e}")
