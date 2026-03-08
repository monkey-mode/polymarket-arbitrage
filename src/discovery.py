import time
from datetime import datetime, timezone, timedelta
import requests
import logging

logger = logging.getLogger(__name__)

GAMMA_ENDPOINT = "https://gamma-api.polymarket.com/events"

class DiscoveryManager:
    """
    Interfaces with the Gamma API to discover target markets.
    """
    def __init__(self):
        self.session = requests.Session()

    def get_upcoming_btc_5m_markets(self):
        """
        Mathematically predicts the URL slug for the NEXT active BTC 5-minute market based
        on the UTC UNIX timestamp.
        Slugs follow the pattern: btc-updown-5m-{UNIX_TIMESTAMP}
        """
        now = datetime.now(timezone.utc)
        
        # Determine the next 5-minute boundary
        # If it's 09:32, the current active interval is 09:30 - 09:35, meaning the market resolves at 09:35
        minutes_to_next = 5 - (now.minute % 5)
        if minutes_to_next == 5:
             # Depending on how polymarket rolls, it might be exactly on the 5
             minutes_to_next = 0
             
        # Target the closing time
        next_boundary = now + timedelta(minutes=minutes_to_next)
        next_boundary = next_boundary.replace(second=0, microsecond=0)
        target_timestamp = int(next_boundary.timestamp())
        
        # Because we might want to watch the CURRENT closing one AND the NEXT one
        # Let's generate slugs for the closest 3 intervals
        intervals = [
            target_timestamp,
            target_timestamp + 300, # +5 mins
            target_timestamp + 600  # +10 mins
        ]
        
        valid_markets = []
        for ts in intervals:
            slug = f"btc-updown-5m-{ts}"
            logger.info(f"Checking predicted market slug: {slug}")
            
            try:
                res = self.session.get(GAMMA_ENDPOINT, params={"slug": slug})
                res.raise_for_status()
                events = res.json()
                if not events:
                    continue
                    
                event = events[0]
                for market in event.get("markets", []):
                    if market.get("active") and market.get("enableOrderBook"):
                        valid_markets.append(market)
            except Exception as e:
                logger.error(f"Failed to fetch {slug}: {e}")
                
        logger.info(f"Discovered {len(valid_markets)} specific BTC 5-minute markets.")
        return valid_markets

    def get_btc_5m_markets(self):
        """
        Fetches active BTC 5-minute markets from the Gamma API.
        Filters for markets that are open and have an active orderbook.
        """
        params = {
            "active": "true",
            "closed": "false",
            "tag_id": "1", # Hypothetically 1 is crypto, or query by title. In a real scenario we might search.
            "limit": "100"
        }
        # To specifically find BTC 5-minute, we can grep the title or slug pattern. 
        # Using a more general query and filtering in Python for safety:
        del params["tag_id"]
        
        response = self.session.get(GAMMA_ENDPOINT, params=params)
        response.raise_for_status()
        events = response.json()
        
        valid_markets = []
        for event in events:
            # Look for 5-minute BTC identifiers
            title = event.get("title", "").lower()
            if "btc" in title and "5-minute" in title:
                for market in event.get("markets", []):
                    # Must be active on CLOB and not an Augmented Placeholder
                    if market.get("active") and market.get("enableOrderBook"):
                        # Exclude Augmented Negative Risk 'Other' placeholder ambiguity
                        is_neg_risk = market.get("negRisk", False)
                        is_augmented = market.get("enableNegRisk", False) and is_neg_risk 
                        if is_augmented and "other" in market.get("groupItemTitle", "").lower():
                            continue 
                        
                        valid_markets.append(market)
        
        logger.info(f"Discovered {len(valid_markets)} active BTC 5-minute markets.")
        return valid_markets

    def extract_token_pairs(self, markets):
        """
        Returns a list of dicts containing the YES and NO token IDs for subscription and trading.
        """
        pairs = []
        for market in markets:
            # Different markets store token IDs differently
            # Usually it's in `clobTokenIds` for recent API versions
            raw_outcomes = market.get("outcomes", "[]")
            if isinstance(raw_outcomes, str):
                import json
                try:
                    outcome_names = json.loads(raw_outcomes)
                except:
                    outcome_names = []
            else:
                outcome_names = raw_outcomes
            tokens_raw = market.get("clobTokenIds", "[]")
            if isinstance(tokens_raw, str):
                try:
                    import json
                    tokens = json.loads(tokens_raw)
                except:
                    tokens = []
            else:
                tokens = tokens_raw
            
            if len(tokens) >= 2 and isinstance(outcome_names, list) and len(outcome_names) >= 2:
                # Map names ("Up", "Down", or "Yes", "No") to tokens using indices
                # Often the first two indices match the first two outcomes
                token_map = {}
                for i, out in enumerate(outcome_names):
                    if i < len(tokens):
                        token_map[out.lower()] = tokens[i]
                
                # Assign "yes" and "no" conceptually based on the market type
                yes_key = "up" if "up" in token_map else "yes"
                no_key = "down" if "down" in token_map else "no"
                
                if yes_key in token_map and no_key in token_map:
                    # Parse endDate (e.g., 2026-03-09T00:30:00Z) to Unix timestamp
                    end_ts = 0
                    end_date_str = market.get("endDate")
                    if end_date_str:
                        try:
                            # Python 3.7+ supports isoformat with 'Z'
                            dt = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                            end_ts = int(dt.timestamp())
                        except:
                            pass

                    pairs.append({
                        "condition_id": market.get("conditionId"),
                        "market_id": market.get("id"),
                        "yes": token_map[yes_key],
                        "no": token_map[no_key],
                        "negRisk": market.get("negRisk", False),
                        "tick_size": float(market.get("minimumTickSize", "0.01")),
                        "end_timestamp": end_ts
                    })
        return pairs
