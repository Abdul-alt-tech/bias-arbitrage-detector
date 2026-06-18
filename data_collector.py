"""
data_collector.py
-----------------
Phase 1 core module. Pulls upcoming UFC markets from configured
platform(s), fetches reference probability from TheOddsAPI,
fetches fighter bias_inputs from UFCStats/ESPN MMA, and writes
schema-compliant records to snapshots.jsonl.

Run manually: python data_collector.py
Called by GitHub Actions scan.yml on schedule.
"""

import json
import os
import requests
from datetime import datetime, timezone
from typing import Optional

from exchanges.kalshi import KalshiAdapter
from exchanges.polymarket import PolymarketAdapter


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = "config.json"
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            "config.json not found. Copy config.template.json to config.json and fill in your values."
        )
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Reference probability from TheOddsAPI
# ---------------------------------------------------------------------------

def fetch_reference_prob(fighter_a: str, fighter_b: str, api_key: str) -> Optional[float]:
    """
    Fetch MMA moneyline odds from TheOddsAPI and convert to
    implied probability for fighter_a winning.

    Returns float (0-1) or None if market not found.

    ⚠️ Phase 0: verify exact sport key for MMA in TheOddsAPI.
    Common values: "mma_mixed_martial_arts" or "ufc"
    """
    sport_key = "mma_mixed_martial_arts"
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        events = resp.json()

        for event in events:
            home = event.get("home_team", "").lower()
            away = event.get("away_team", "").lower()
            fa = fighter_a.lower()
            fb = fighter_b.lower()

            # Match on last name partial match (fighter names vary across sources)
            if (fa.split()[-1] in home or fa.split()[-1] in away) and \
               (fb.split()[-1] in home or fb.split()[-1] in away):

                # Average implied probability across all bookmakers
                probs_a = []
                for bookmaker in event.get("bookmakers", []):
                    for market in bookmaker.get("markets", []):
                        if market["key"] == "h2h":
                            for outcome in market["outcomes"]:
                                name = outcome["name"].lower()
                                if fa.split()[-1] in name:
                                    decimal_odds = outcome["price"]
                                    probs_a.append(1.0 / decimal_odds)

                if probs_a:
                    raw_prob = sum(probs_a) / len(probs_a)
                    # Remove rough vig (normalize to ~true probability)
                    return round(raw_prob, 4)

        print(f"[TheOddsAPI] No match found for {fighter_a} vs {fighter_b}")
        return None

    except requests.RequestException as e:
        print(f"[TheOddsAPI] Error: {e}")
        return None


# ---------------------------------------------------------------------------
# Fighter bias_inputs from UFCStats / ESPN MMA
# ---------------------------------------------------------------------------

def fetch_fighter_stats(fighter_name: str) -> dict:
    """
    Fetch fighter's last 3 results and other bias_inputs.

    Current implementation: ESPN MMA athlete search.
    ⚠️ Phase 0: verify ESPN MMA endpoint availability and
    response structure. May need to fall back to UFCStats scraping
    or manual curation for some fighters.

    Returns a bias_inputs fighter dict.
    """
    # ESPN athlete search
    search_url = "https://site.api.espn.com/apis/common/v3/search"
    params = {
        "query": fighter_name,
        "sport": "mma",
        "limit": 1
    }

    default = {
        "name": fighter_name,
        "last_3_results": [],
        "days_since_last_fight": None,
        "weight_class": None,
        "news_flags": ["stats_unavailable"]
    }

    try:
        resp = requests.get(search_url, params=params, timeout=10)
        if resp.status_code != 200:
            return default

        results = resp.json().get("results", [])
        if not results:
            return default

        athlete_id = results[0].get("id")
        if not athlete_id:
            return default

        # Fetch athlete event log
        log_url = f"https://site.api.espn.com/apis/site/v2/sports/mma/ufc/athletes/{athlete_id}/eventlog"
        log_resp = requests.get(log_url, timeout=10)
        if log_resp.status_code != 200:
            return default

        log_data = log_resp.json()
        events = log_data.get("events", {}).get("items", [])

        last_3 = []
        last_fight_date = None

        for event in sorted(events, key=lambda x: x.get("date", ""), reverse=True)[:3]:
            result = event.get("competitions", [{}])[0].get("competitors", [{}])
            winner = None
            method = "DEC"
            for comp in result:
                if str(comp.get("id")) == str(athlete_id):
                    winner = comp.get("winner", False)
                    # Method parsing varies — default to DEC if unclear
                    status = event.get("competitions", [{}])[0].get("status", {})
                    detail = status.get("type", {}).get("detail", "").upper()
                    if "KO" in detail or "TKO" in detail:
                        method = "KO"
                    elif "SUB" in detail or "SUBMISSION" in detail:
                        method = "SUB"
                    else:
                        method = "DEC"

            last_3.append({
                "result": "W" if winner else "L",
                "method": method
            })

            if last_fight_date is None:
                try:
                    fight_dt = datetime.fromisoformat(event.get("date", "").replace("Z", "+00:00"))
                    days_since = (datetime.now(timezone.utc) - fight_dt).days
                    last_fight_date = days_since
                except (ValueError, TypeError):
                    pass

        return {
            "name": fighter_name,
            "last_3_results": last_3,
            "days_since_last_fight": last_fight_date,
            "weight_class": log_data.get("athlete", {}).get("weightClass", {}).get("text", None),
            "news_flags": []
        }

    except Exception as e:
        print(f"[FighterStats] Error fetching stats for {fighter_name}: {e}")
        return default


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def build_record(market: dict, platform: str, price_data: dict,
                 reference_prob: Optional[float],
                 fighter_a_stats: dict, fighter_b_stats: dict,
                 config: dict) -> dict:
    """
    Assemble a full schema-compliant record (source_of_truth §4).
    scoring.py fills in scoring_output. llm_reasoner.py fills in llm_output.
    alerter.py fills in alert. resolver.py fills in resolution.
    """
    return {
        "market_id": market["market_id"],
        "platform": platform,
        "market_url": market.get("market_url", ""),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "question": market.get("question", ""),
        "sport": "UFC",
        "event_name": market.get("event_name", ""),
        "start_time": market.get("start_time", ""),

        "market_data": {
            "market_prob": price_data.get("market_prob"),
            "prob_24h_ago": price_data.get("prob_24h_ago"),
            "price_change_24h": price_data.get("price_change_24h"),
            "volume_24h": price_data.get("volume_24h"),
            "price_history_source": price_data.get("price_history_source", "unknown")
        },

        "fundamentals": {
            "reference_prob": reference_prob,
            "reference_source": "sportsbook_average_mma"
        },

        "bias_inputs": {
            "fighter_a": fighter_a_stats,
            "fighter_b": fighter_b_stats
        },

        # Filled by scoring.py
        "scoring_output": {
            "raw_edge": None,
            "edge_percent": None,
            "payout_multiple": None,
            "llm_called": False
        },

        # Filled by llm_reasoner.py
        "llm_output": {
            "bias_type": None,
            "raw_confidence": None,
            "calibrated_confidence": None,
            "reason": None
        },

        # Filled by alerter.py
        "alert": {
            "triggered": False,
            "risk_mode": config.get("risk_mode", "conservative"),
            "suggested_bet_pct": None,
            "suggested_bet_value_zmw": None
        },

        # Filled by resolver.py
        "resolution": {
            "actual_result": None,
            "settled_at": None,
            "pnl_zmw": None
        }
    }


# ---------------------------------------------------------------------------
# Snapshot log helpers
# ---------------------------------------------------------------------------

def load_existing_snapshots(path: str = "snapshots.jsonl") -> dict:
    """
    Load existing snapshots into a dict keyed by market_id
    for deduplication (same market_id + same calendar date = skip).
    """
    existing = {}
    if not os.path.exists(path):
        return existing
    with open(path) as f:
        for line in f:
            try:
                record = json.loads(line.strip())
                mid = record.get("market_id")
                ts = record.get("timestamp", "")[:10]  # date only
                existing[f"{mid}_{ts}"] = True
            except json.JSONDecodeError:
                continue
    return existing


def append_snapshot(record: dict, path: str = "snapshots.jsonl"):
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    config = load_config()
    platforms = config.get("platforms", ["kalshi"])
    sport = config.get("sport", "UFC")
    lookahead = config.get("lookahead_days", 7)
    api_keys = config.get("api_keys", {})
    odds_api_key = api_keys.get("the_odds_api", os.environ.get("THE_ODDS_API_KEY", ""))

    # Build adapters
    adapters = []
    if "kalshi" in platforms:
        kalshi_key = api_keys.get("kalshi_api_key", os.environ.get("KALSHI_API_KEY", ""))
        adapters.append(KalshiAdapter(api_key=kalshi_key))
    if "polymarket" in platforms:
        adapters.append(PolymarketAdapter())

    existing = load_existing_snapshots()
    today = datetime.now(timezone.utc).isoformat()[:10]
    new_records = 0

    for adapter in adapters:
        platform = adapter.get_platform_name()
        print(f"\n[Collector] Scanning {platform.upper()} for {sport} markets...")

        markets = adapter.list_upcoming_markets(sport, lookahead)
        print(f"[Collector] Found {len(markets)} upcoming markets on {platform}")

        for market in markets:
            market_id = market["market_id"]
            dedup_key = f"{market_id}_{today}"

            if dedup_key in existing:
                print(f"  [skip] {market_id} already logged today")
                continue

            print(f"  [fetch] {market_id} — {market.get('question', '')[:60]}")

            # Price snapshot
            # For Polymarket, use the CLOB token ID for price fetching
            lookup_id = market.get("_token_id", market_id) if platform == "polymarket" else market_id
            price_data = adapter.get_price_snapshot(lookup_id)
            if price_data.get("market_prob") is None:
                print(f"  [skip] Could not fetch price for {market_id}")
                continue

            # Reference probability
            fighter_a = market.get("fighter_a", "")
            fighter_b = market.get("fighter_b", "")
            reference_prob = fetch_reference_prob(fighter_a, fighter_b, odds_api_key)

            # Fighter stats
            print(f"    Fetching stats: {fighter_a}")
            fa_stats = fetch_fighter_stats(fighter_a)
            print(f"    Fetching stats: {fighter_b}")
            fb_stats = fetch_fighter_stats(fighter_b)

            # Build and log the record
            record = build_record(
                market, platform, price_data,
                reference_prob, fa_stats, fb_stats, config
            )
            append_snapshot(record)
            existing[dedup_key] = True
            new_records += 1
            print(f"  [logged] {market_id}")

    print(f"\n[Collector] Done. {new_records} new records written to snapshots.jsonl")


if __name__ == "__main__":
    run()
