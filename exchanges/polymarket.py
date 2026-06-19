"""
exchanges/polymarket.py
-----------------------
Polymarket exchange adapter. Implements the ExchangeAdapter interface.

Polymarket API notes:
- No authentication required for read operations
- Gamma API (events): https://gamma-api.polymarket.com
- CLOB API (prices): https://clob.polymarket.com
- Prices are already 0-1 probability (no normalization needed)
- UFC markets use seriesSlug=ufc on the events endpoint
"""

import requests
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from exchanges.base import ExchangeAdapter


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


class PolymarketAdapter(ExchangeAdapter):

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def get_platform_name(self) -> str:
        return "polymarket"

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    def list_upcoming_markets(self, sport: str, days_ahead: int = 7) -> list[dict]:
        """
        List upcoming UFC moneyline markets from Polymarket.
        Uses the events endpoint with seriesSlug=ufc.
        """
        if sport.upper() != "UFC":
            raise NotImplementedError(f"PolymarketAdapter only supports UFC in v1. Got: {sport}")

        try:
            url = f"{GAMMA_API}/events"
            params = {
                "seriesSlug": "ufc",
                "active":     "true",
                "closed":     "false",
                "limit":      100
            }
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            events = resp.json()

            if not isinstance(events, list):
                events = events.get("events", [])

            print(f"[PolymarketAdapter] Raw events returned: {len(events)}")

            markets = []
            now     = datetime.now(timezone.utc)
            cutoff  = now + timedelta(days=days_ahead)

            for event in events:
                event_title = event.get("title", "UFC Event")
                event_slug  = event.get("slug", "")
                event_date  = event.get("eventDate") or event.get("startTime", "")

                for m in event.get("markets", []):
                    # Only include moneyline (winner) markets
                    if m.get("sportsMarketType") != "moneyline":
                        continue

                    # Skip already-closed individual markets
                    if m.get("closed"):
                        continue

                    # Use gameStartTime if available, else endDate
                    game_start = (
                        m.get("gameStartTime")
                        or event.get("startTime")
                        or m.get("endDate", "")
                    )

                    if not game_start:
                        # Accept it anyway — don't filter on missing date
                        pass
                    else:
                        try:
                            gs = datetime.fromisoformat(
                                game_start.replace("Z", "+00:00")
                            )
                            # Skip events that already started more than 4 h ago
                            if gs < now - timedelta(hours=4):
                                continue
                            # Skip events beyond our lookahead window
                            if gs > cutoff:
                                continue
                        except ValueError:
                            pass  # Keep the market if we can't parse the date

                    normalized = self._normalize_market(m, event)
                    markets.append(normalized)

            print(f"[PolymarketAdapter] Moneyline markets after filtering: {len(markets)}")
            return markets

        except requests.RequestException as e:
            print(f"[PolymarketAdapter] Error fetching events: {e}")
            return []

        print(f"[PolymarketAdapter] Raw events returned: {len(events)}")
        if events:
            sample = events[0]
            sample_markets = sample.get("markets", [])
            print(f"[PolymarketAdapter] DEBUG first event title: {sample.get('title')}")
            print(f"[PolymarketAdapter] DEBUG first event has {len(sample_markets)} nested markets")
            if sample_markets:
                print(f"[PolymarketAdapter] DEBUG first market sportsMarketType: {sample_markets[0].get('sportsMarketType')}")
                print(f"[PolymarketAdapter] DEBUG first market closed: {sample_markets[0].get('closed')}")

    # ------------------------------------------------------------------
    # Price snapshot
    # ------------------------------------------------------------------

    def get_price_snapshot(self, market_id: str) -> dict:
        """
        Fetch current price for a Polymarket market.
        market_id may be "poly_{conditionId}" or a raw CLOB token ID.
        Prices are already 0-1.
        """
        # Strip prefix if present
        raw_id = market_id.replace("poly_", "")

        # Try CLOB last-trade-price endpoint
        try:
            url    = f"{CLOB_API}/last-trade-price"
            params = {"token_id": raw_id}
            resp   = self.session.get(url, params=params, timeout=10)

            if resp.status_code == 200:
                data         = resp.json()
                market_prob  = float(data.get("price", 0.5))
                prob_24h_ago, price_history_source = self._get_24h_price(
                    raw_id, market_prob, market_id
                )
                return {
                    "market_prob":          round(market_prob, 4),
                    "prob_24h_ago":         prob_24h_ago,
                    "price_change_24h":     round(market_prob - prob_24h_ago, 4) if prob_24h_ago else None,
                    "volume_24h":           None,
                    "price_history_source": price_history_source
                }
        except Exception as e:
            print(f"[PolymarketAdapter] CLOB price error for {market_id}: {e}")

        # Fallback: use outcomePrices from the raw market dict stored during collection
        return {
            "market_prob":          None,
            "prob_24h_ago":         None,
            "price_change_24h":     None,
            "volume_24h":           None,
            "price_history_source": "error"
        }

    # ------------------------------------------------------------------
    # 24-hour price history
    # ------------------------------------------------------------------

    def _get_24h_price(self, token_id: str, current_prob: float,
                       original_market_id: str) -> tuple:
        """Try platform history, fall back to local snapshots log."""
        try:
            end_ts   = int(datetime.now(timezone.utc).timestamp())
            start_ts = end_ts - (25 * 3600)

            url    = f"{CLOB_API}/prices-history"
            params = {"market": token_id, "startTs": start_ts,
                      "endTs": end_ts, "fidelity": 1440}
            resp   = self.session.get(url, params=params, timeout=10)

            if resp.status_code == 200:
                history = resp.json().get("history", [])
                if history:
                    price = history[0].get("p")
                    if price is not None:
                        return (round(float(price), 4), "platform_api")
        except Exception:
            pass

        return self._local_log_24h_price(original_market_id)

    def _local_log_24h_price(self, market_id: str) -> tuple:
        snapshots_path = "snapshots.jsonl"
        if not os.path.exists(snapshots_path):
            return (None, "not_found")

        target_ts = datetime.now(timezone.utc) - timedelta(hours=24)
        best_entry, best_delta = None, timedelta.max

        try:
            with open(snapshots_path) as f:
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

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    def _normalize_market(self, raw: dict, event: dict = None) -> dict:
        """
        Normalize a Polymarket market dict into the standard format
        expected by data_collector.py.
        """
        condition_id = raw.get("conditionId", "")
        slug         = raw.get("slug", "")
        question     = raw.get("question", "")
        event_title  = event.get("title", "UFC Event") if event else "UFC Event"
        event_slug   = event.get("slug", slug)         if event else slug

        fighter_a, fighter_b = self._parse_fighters(question)

        # Extract YES-side CLOB token ID for price lookups
        clob_tokens = raw.get("clobTokenIds", "[]")
        try:
            token_list = json.loads(clob_tokens) if isinstance(clob_tokens, str) else clob_tokens
            token_id   = token_list[0] if token_list else condition_id
        except Exception:
            token_id = condition_id

        # Prefer gameStartTime, then endDate for the fight time
        start_time = (
            raw.get("gameStartTime")
            or (event.get("startTime") if event else None)
            or raw.get("endDate", "")
        )

        # Pull current price directly from outcomePrices if available
        # so we have a price even before the CLOB call
        outcome_prices = raw.get("outcomePrices", "[]")
        try:
            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
            inline_prob = float(prices[0]) if prices else None
        except Exception:
            inline_prob = None

        return {
            "market_id":    f"poly_{condition_id}",
            "market_url":   f"https://polymarket.com/event/{event_slug}",
            "question":     question,
            "event_name":   event_title,
            "start_time":   start_time,
            "fighter_a":    fighter_a,
            "fighter_b":    fighter_b,
            "_token_id":    token_id,
            "_inline_prob": inline_prob,   # fallback price from discovery
            "_raw":         raw
        }

    def _parse_fighters(self, question: str) -> tuple:
        """
        Parse fighter names from a Polymarket question string.
        Handles formats like:
          "UFC Fight Night: Andre Fili vs. Vinicius Oliveira (...)"
          "Will Topuria beat Gaethje?"
        """
        import re

        # Pattern 1: "Name1 vs. Name2" (with optional parenthetical)
        match = re.search(r"([A-Z][a-zA-Z\s\-']+?)\s+vs\.\s+([A-Z][a-zA-Z\s\-']+?)(?:\s*\(|$)",
                          question)
        if match:
            return match.group(1).strip(), match.group(2).strip()

        # Pattern 2: "Will X beat Y?"
        match = re.search(r"Will (.+?) (?:beat|fight|vs\.?) (.+?)\?", question, re.IGNORECASE)
        if match:
            return match.group(1).strip(), match.group(2).strip()

        return ("Fighter A", "Fighter B")
