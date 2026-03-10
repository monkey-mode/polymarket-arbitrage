import json
import time
import logging
from datetime import datetime, timezone, timedelta
import requests

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/events"


class MarketDiscovery:
    """Discovers active BTC 5-minute markets from the Gamma API."""

    def __init__(self):
        self.session = requests.Session()

    def _current_and_next_timestamps(self) -> list[int]:
        """Returns Unix timestamps for current + next two 5-minute boundaries."""
        now = datetime.now(timezone.utc)
        # Round down to current 5-minute floor
        floored = now.replace(second=0, microsecond=0)
        floored -= timedelta(minutes=floored.minute % 5)
        base = int(floored.timestamp())
        return [base + i * 300 for i in range(3)]  # current, +5m, +10m

    def _fetch_market(self, ts: int) -> dict | None:
        slug = f"btc-updown-5m-{ts}"
        try:
            res = self.session.get(GAMMA_URL, params={"slug": slug}, timeout=5)
            res.raise_for_status()
            events = res.json()
            if not events:
                return None
            for market in events[0].get("markets", []):
                if market.get("active") and market.get("enableOrderBook"):
                    return self._parse(market, ts)
        except Exception as e:
            logger.warning(f"Failed to fetch {slug}: {e}")
        return None

    def _parse(self, market: dict, ts: int) -> dict:
        outcomes = market.get("outcomes", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        tokens = market.get("clobTokenIds", "[]")
        if isinstance(tokens, str):
            tokens = json.loads(tokens)

        token_map = {o.lower(): tokens[i] for i, o in enumerate(outcomes) if i < len(tokens)}
        yes_key = "up" if "up" in token_map else "yes"
        no_key  = "down" if "down" in token_map else "no"

        end_ts = ts  # fallback
        end_date = market.get("endDate")
        if end_date:
            try:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                end_ts = int(dt.timestamp())
            except Exception:
                pass

        return {
            "slug": f"btc-updown-5m-{ts}",
            "condition_id": market.get("conditionId"),
            "yes": token_map[yes_key],
            "no": token_map[no_key],
            "tick_size": float(market.get("minimumTickSize", "0.01")),
            "end_timestamp": end_ts,
        }

    def get_current_and_next(self) -> list[dict]:
        """Returns up to two active markets: current and the one after it."""
        markets = []
        for ts in self._current_and_next_timestamps():
            m = self._fetch_market(ts)
            if m:
                markets.append(m)
            if len(markets) == 2:
                break
        return markets
