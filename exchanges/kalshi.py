"""
exchanges/kalshi.py
-------------------
Kalshi exchange adapter. Implements the ExchangeAdapter interface.

Kalshi API notes:
- Base URL: https://api.kalshi.com/trade-api/v2
- Prices come back as cents (1-99) — normalized to 0-1 here
- Auth: API key + secret (HMAC signature) for write operations
  For read operations (market listing, prices), an API key in the
  header is sufficient.
- UFC series ticker: verify at https://kalshi.com/markets before
  build (see PRD Open Questions). Default assumption: "KXUFC"

Phase 0 verification needed:
  1. Confirm UFC series ticker format
  2. Confirm price-history endpoint availability for MMA markets
  3. Test auth flow with your API key
"""

import requests
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from exchanges.base import ExchangeAdapter


KALSHI_BASE_URL = "https://api.kalshi.com/trade-api/v2"
UFC_SERIES_TICKER = "KXUFC"  # ⚠️ Verify this in Phase 0


class KalshiAdapter(ExchangeAdapter):

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })

    def get_platform_name(self) -> str:
        return "kalshi"

    def list_upcoming_markets(self, sport: str, days_ahead: int = 7) -> list[dict]:
        """
        List upcoming UFC fight markets on Kalshi.
        Returns normalized market dicts ready for data_collector.py.
        """
        if sport.upper() != "UFC":
            raise NotImplementedError(f"KalshiAdapter only supports UFC in v1. Got: {sport}")

        try:
            url = f"{KALSHI_BASE_URL}/markets"
            params = {
                "series_ticker": UFC_SERIES_TICKER,
                "status": "open",
                "limit": 100
            }
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            markets = []
            cutoff = datetime.now(timezone.utc) + timedelta(days=days_ahead)

            for m in data.get("markets", []):
                try:
                    close_time = datetime.fromisoformat(
                        m.get("close_time", "").replace("Z", "+00:00")
                    )
                    if close_time > datetime.now(timezone.utc) and close_time <= cutoff:
                        markets.append(self._normalize_market(m))
                except (ValueError, KeyError):
                    continue

            return markets

        except requests.RequestException as e:
            print(f"[KalshiAdapter] Error fetching markets: {e}")
            return []

    def get_price_snapshot(self, market_id: str) -> dict:
        """
        Fetch current price for a Kalshi market.
        market_id format: "kalshi_KXUFC..."
        Normalizes cents (1-99) to 0-1 probability.
        """
        ticker = market_id.replace("kalshi_", "")

        try:
            url = f"{KALSHI_BASE_URL}/markets/{ticker}"
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("market", {})

            # Kalshi YES price is in cents (1-99)
            yes_price_cents = data.get("yes_bid", data.get("last_price", 50))
            market_prob = float(yes_price_cents) / 100.0

            # Attempt to get 24h history
            prob_24h_ago, price_history_source = self._get_24h_price(ticker, market_prob)

            return {
                "market_prob": round(market_prob, 4),
                "prob_24h_ago": prob_24h_ago,
                "price_change_24h": round(market_prob - prob_24h_ago, 4) if prob_24h_ago else None,
                "volume_24h": data.get("volume_24h", None),
                "price_history_source": price_history_source
            }

        except requests.RequestException as e:
            print(f"[KalshiAdapter] Error fetching price for {market_id}: {e}")
            return {
                "market_prob": None,
                "prob_24h_ago": None,
                "price_change_24h": None,
                "volume_24h": None,
                "price_history_source": "error"
            }

    def _get_24h_price(self, ticker: str, current_prob: float) -> tuple:
        """
        Try platform candlestick API for 24h-ago price.
        Falls back to local snapshots.jsonl if unavailable.
        Returns (prob_24h_ago, source_string).
        """
        try:
            end_ts = datetime.now(timezone.utc)
            start_ts = end_ts - timedelta(hours=25)

            url = f"{KALSHI_BASE_URL}/markets/{ticker}/candlesticks"
            params = {
                "start_ts": int(start_ts.timestamp()),
                "end_ts": int(end_ts.timestamp()),
                "period_interval": 1440  # 24h candle
            }
            resp = self.session.get(url, params=params, timeout=10)

            if resp.status_code == 200:
                candles = resp.json().get("candlesticks", [])
                if candles:
                    open_price = candles[0].get("yes_ask", {}).get("open", None)
                    if open_price:
                        return (round(float(open_price) / 100.0, 4), "platform_api")

        except Exception:
            pass

        # Fallback: read from local snapshots.jsonl
        return self._local_log_24h_price(f"kalshi_{ticker}")

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
        Normalize a raw Kalshi market dict into the standard
        format expected by data_collector.py.

        ⚠️ Field names here depend on Kalshi's actual API response
        structure — verify against live API response in Phase 0.
        """
        ticker = raw.get("ticker", "")
        title = raw.get("title", "")

        # Best-effort fighter name parsing from market title
        # e.g. "Will Topuria beat Gaethje?" -> ["Topuria", "Gaethje"]
        fighter_a, fighter_b = self._parse_fighters(title)

        return {
            "market_id": f"kalshi_{ticker}",
            "market_url": f"https://kalshi.com/markets/{ticker.lower().replace('_', '-')}",
            "question": title,
            "event_name": raw.get("event_title", "UFC Event"),
            "start_time": raw.get("close_time", ""),
            "fighter_a": fighter_a,
            "fighter_b": fighter_b,
            "_raw": raw
        }

    def _parse_fighters(self, title: str) -> tuple:
        """
        Naively parse fighter names from a market title.
        e.g. "Will Topuria beat Gaethje?" -> ("Topuria", "Gaethje")
        Improve this once you see real Kalshi title formats in Phase 0.
        """
        import re
        match = re.search(r"Will (.+?) beat (.+?)\?", title, re.IGNORECASE)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return ("Fighter A", "Fighter B")
