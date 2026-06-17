"""
exchanges/polymarket.py
-----------------------
Polymarket exchange adapter. Implements the ExchangeAdapter interface.

Polymarket API notes:
- No authentication required for read operations
- Gamma API (market discovery): https://gamma-api.polymarket.com
- CLOB API (prices + history): https://clob.polymarket.com
- Prices are already 0-1 probability (no normalization needed)
- UFC market availability: uncertain — verify in Phase 0.
  If no UFC markets exist, this adapter won't return results
  and Kalshi will be the sole platform for v1.

Phase 0 verification needed:
  1. Confirm UFC/MMA markets exist on Polymarket
  2. Confirm CLOB price-history endpoint works for MMA markets
  3. Verify slug/tag format for UFC event filtering
"""

import requests
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from exchanges.base import ExchangeAdapter


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class PolymarketAdapter(ExchangeAdapter):

    def __init__(self):
        # No auth required for reads
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json"
        })

    def get_platform_name(self) -> str:
        return "polymarket"

    def list_upcoming_markets(self, sport: str, days_ahead: int = 7) -> list[dict]:
        """
        List upcoming UFC fight markets on Polymarket via Gamma API.
        Returns normalized market dicts ready for data_collector.py.
        """
        if sport.upper() != "UFC":
            raise NotImplementedError(f"PolymarketAdapter only supports UFC in v1. Got: {sport}")

        try:
            # Gamma API supports tag-based filtering
            # Tag for MMA/UFC — verify exact tag in Phase 0
            url = f"{GAMMA_API}/markets"
            params = {
                "tag": "mma",
                "active": "true",
                "closed": "false",
                "limit": 100
            }
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            markets = []
            cutoff = datetime.now(timezone.utc) + timedelta(days=days_ahead)

            for m in data if isinstance(data, list) else data.get("markets", []):
                try:
                    end_date_str = m.get("endDate") or m.get("end_date_iso", "")
                    if not end_date_str:
                        continue
                    end_date = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    )
                    if end_date > datetime.now(timezone.utc) and end_date <= cutoff:
                        markets.append(self._normalize_market(m))
                except (ValueError, KeyError):
                    continue

            return markets

        except requests.RequestException as e:
            print(f"[PolymarketAdapter] Error fetching markets: {e}")
            return []

    def get_price_snapshot(self, market_id: str) -> dict:
        """
        Fetch current price for a Polymarket market via CLOB API.
        market_id format: "poly_0xabc123..."
        Prices are already 0-1 (no normalization needed).
        """
        token_id = market_id.replace("poly_", "")

        try:
            url = f"{CLOB_API}/last-trade-price"
            params = {"token_id": token_id}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            market_prob = float(data.get("price", 0.5))
            prob_24h_ago, price_history_source = self._get_24h_price(token_id, market_prob)

            return {
                "market_prob": round(market_prob, 4),
                "prob_24h_ago": prob_24h_ago,
                "price_change_24h": round(market_prob - prob_24h_ago, 4) if prob_24h_ago else None,
                "volume_24h": None,  # fetch separately if needed
                "price_history_source": price_history_source
            }

        except requests.RequestException as e:
            print(f"[PolymarketAdapter] Error fetching price for {market_id}: {e}")
            return {
                "market_prob": None,
                "prob_24h_ago": None,
                "price_change_24h": None,
                "volume_24h": None,
                "price_history_source": "error"
            }

    def _get_24h_price(self, token_id: str, current_prob: float) -> tuple:
        """
        Try CLOB price-history endpoint for 24h-ago price.
        Falls back to local snapshots.jsonl if unavailable.
        Returns (prob_24h_ago, source_string).
        """
        try:
            end_ts = int(datetime.now(timezone.utc).timestamp())
            start_ts = end_ts - (25 * 3600)

            url = f"{CLOB_API}/prices-history"
            params = {
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 1440
            }
            resp = self.session.get(url, params=params, timeout=10)

            if resp.status_code == 200:
                history = resp.json().get("history", [])
                if history:
                    oldest = history[0]
                    price = oldest.get("p", None)
                    if price is not None:
                        return (round(float(price), 4), "platform_api")

        except Exception:
            pass

        # Fallback: read from local snapshots.jsonl
        return self._local_log_24h_price(f"poly_{token_id}")

    def _local_log_24h_price(self, market_id: str) -> tuple:
        """
        Look up the closest price to 24h ago from snapshots.jsonl.
        Returns (prob_24h_ago, "local_log") or (None, "not_found").
        """
        snapshots_path = "snapshots.jsonl"
        if not os.path.exists(snapshots_path):
            return (None, "not_found")

        target_ts = datetime.now(timezone.utc) - timedelta(hours=24)
        best_entry = None
        best_delta = timedelta.max

        try:
            with open(snapshots_path, "r") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        if record.get("market_id") != market_id:
                            continue
                        ts = datetime.fromisoformat(
                            record["timestamp"].replace("Z", "+00:00")
                        )
                        delta = abs(ts - target_ts)
                        if delta < best_delta:
                            best_delta = delta
                            best_entry = record
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue

            if best_entry and best_delta <= timedelta(hours=4):
                prob = best_entry.get("market_data", {}).get("market_prob")
                return (prob, "local_log")

        except IOError:
            pass

        return (None, "not_found")

    def _normalize_market(self, raw: dict) -> dict:
        """
        Normalize a raw Polymarket market dict into the standard
        format expected by data_collector.py.

        ⚠️ Field names depend on Gamma API response structure
        — verify against live response in Phase 0.
        """
        # Polymarket uses condition_id or token id as market identifier
        market_id = raw.get("conditionId") or raw.get("condition_id", "")
        slug = raw.get("slug", "")
        question = raw.get("question", raw.get("title", ""))
        fighter_a, fighter_b = self._parse_fighters(question)

        return {
            "market_id": f"poly_{market_id}",
            "market_url": f"https://polymarket.com/event/{slug}",
            "question": question,
            "event_name": raw.get("groupItemTitle", "UFC Event"),
            "start_time": raw.get("endDate", raw.get("end_date_iso", "")),
            "fighter_a": fighter_a,
            "fighter_b": fighter_b,
            "_raw": raw
        }

    def _parse_fighters(self, question: str) -> tuple:
        """
        Parse fighter names from a Polymarket question string.
        e.g. "Will Topuria beat Gaethje?" -> ("Topuria", "Gaethje")
        Improve once real question formats are confirmed in Phase 0.
        """
        import re
        match = re.search(r"Will (.+?) beat (.+?)\?", question, re.IGNORECASE)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return ("Fighter A", "Fighter B")
