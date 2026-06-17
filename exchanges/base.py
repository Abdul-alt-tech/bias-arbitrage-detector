"""
exchanges/base.py
-----------------
Abstract base class for exchange adapters.
Every adapter (Kalshi, Polymarket) implements these 3 methods.
All downstream code (data_collector, scoring, etc.) only ever
calls these methods — never exchange-specific code directly.
"""

from abc import ABC, abstractmethod
from typing import Optional


class ExchangeAdapter(ABC):

    @abstractmethod
    def list_upcoming_markets(self, sport: str, days_ahead: int) -> list[dict]:
        """
        Return a list of upcoming markets for the given sport.

        Each dict must contain at minimum:
          - market_id (str): platform-prefixed unique ID
          - market_url (str): direct URL to the market
          - question (str): human-readable market question
          - event_name (str): e.g. "UFC 320"
          - start_time (str): ISO 8601 UTC
          - fighter_a (str): name
          - fighter_b (str): name
        """
        pass

    @abstractmethod
    def get_price_snapshot(self, market_id: str) -> dict:
        """
        Return current price data for a market.

        Returns dict with:
          - market_prob (float): current crowd probability, 0-1
          - prob_24h_ago (float | None): price ~24h ago, or None if unavailable
          - price_change_24h (float | None): delta, or None
          - volume_24h (float | None): trading volume last 24h, or None
          - price_history_source (str): "platform_api" or "local_log"
        """
        pass

    @abstractmethod
    def get_platform_name(self) -> str:
        """
        Return the platform name string: "kalshi" or "polymarket"
        """
        pass
