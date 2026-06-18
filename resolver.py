"""
resolver.py
-----------
Phase 5 module. For each logged market whose start_time has
passed and resolution.actual_result is still null, fetches the
actual fight result from ESPN MMA and computes paper P&L.

Feeds calibrator.py with resolved records.

Run manually: python resolver.py
Called by GitHub Actions scan.yml at the end of each run.
"""

import json
import os
import requests
from datetime import datetime, timezone, timedelta


def load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Result fetcher (ESPN MMA)
# ---------------------------------------------------------------------------

def fetch_fight_result(fighter_a: str, fighter_b: str) -> dict:
    """
    Attempt to fetch the result of a completed UFC fight
    from ESPN's MMA API.

    Returns dict with:
      - winner (str): fighter name who won, or None
      - method (str): KO/TKO/SUB/DEC/NC/None
      - confirmed (bool): whether result was found

    ⚠️ ESPN MMA API endpoint/format needs verification in Phase 0.
    This implementation uses the public scoreboard endpoint.
    """
    try:
        # ESPN MMA scoreboard — shows recent completed events
        url = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return {"winner": None, "method": None, "confirmed": False}

        if not resp.content or not resp.text.strip():
            return {"winner": None, "method": None, "confirmed": False}

        data = resp.json()
        events = data.get("events", [])

        fa_last = fighter_a.split()[-1].lower()
        fb_last = fighter_b.split()[-1].lower()

        for event in events:
            for competition in event.get("competitions", []):
                competitors = competition.get("competitors", [])
                names = [c.get("athlete", {}).get("displayName", "").lower()
                         for c in competitors]

                if any(fa_last in n for n in names) and any(fb_last in n for n in names):
                    status = competition.get("status", {}).get("type", {})
                    if not status.get("completed", False):
                        continue

                    for comp in competitors:
                        if comp.get("winner"):
                            winner_name = comp.get("athlete", {}).get("displayName", "")
                            detail = status.get("detail", "").upper()
                            method = "DEC"
                            if "KO" in detail or "TKO" in detail:
                                method = "KO"
                            elif "SUB" in detail:
                                method = "SUB"
                            elif "NC" in detail or "NO CONTEST" in detail:
                                method = "NC"
                            return {
                                "winner": winner_name,
                                "method": method,
                                "confirmed": True
                            }

        return {"winner": None, "method": None, "confirmed": False}

    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  [Resolver] ESPN error: {e}")
        return {"winner": None, "method": None, "confirmed": False}


# ---------------------------------------------------------------------------
# P&L calculator
# ---------------------------------------------------------------------------

def compute_pnl(record: dict, actual_winner: str) -> float:
    """
    Compute paper P&L for this record based on what side was bet.

    If market_prob < reference_prob, we bet YES (fighter_a wins).
    If market_prob > reference_prob, we bet NO (fighter_b wins).

    Returns P&L in ZMW (positive = win, negative = loss).
    """
    alert = record.get("alert", {})
    bet_zmw = alert.get("suggested_bet_value_zmw", 0) or 0

    if bet_zmw == 0:
        return 0.0

    market_prob = record["market_data"]["market_prob"]
    reference_prob = record["fundamentals"]["reference_prob"]
    scoring = record.get("scoring_output", {})
    payout_multiple = scoring.get("payout_multiple", 1.0) or 1.0

    fighter_a = record.get("bias_inputs", {}).get("fighter_a", {}).get("name", "")
    fighter_b = record.get("bias_inputs", {}).get("fighter_b", {}).get("name", "")

    # Determine which side we bet
    fa_last = fighter_a.split()[-1].lower() if fighter_a else ""
    winner_last = actual_winner.split()[-1].lower() if actual_winner else ""

    if market_prob < reference_prob:
        # We bet fighter_a (YES)
        bet_on_a = True
    else:
        # We bet fighter_b (NO on fighter_a)
        bet_on_a = False

    winner_is_a = fa_last and fa_last in winner_last

    if (bet_on_a and winner_is_a) or (not bet_on_a and not winner_is_a):
        # Win: profit = bet * (payout_multiple - 1)
        return round(bet_zmw * (payout_multiple - 1.0), 2)
    else:
        # Loss: lose the stake
        return round(-bet_zmw, 2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    if not os.path.exists("snapshots.jsonl"):
        print("[Resolver] No snapshots.jsonl found.")
        return

    records = []
    try:
        with open("snapshots.jsonl") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[Resolver] Warning: Skipping malformed JSON at line {line_num}: {e}")
                    continue
    except Exception as e:
        print(f"[Resolver] Error reading snapshots.jsonl: {e}")
        return

    if not records:
        print("[Resolver] No valid records found in snapshots.jsonl")
        return

    resolved_count = 0
    checked = 0
    now = datetime.now(timezone.utc)

    for i, record in enumerate(records):
        res = record.get("resolution", {})

        # Skip already resolved
        if res.get("actual_result") is not None:
            continue

        start_time_str = record.get("start_time", "")
        if not start_time_str:
            continue

        try:
            start_time = datetime.fromisoformat(
                start_time_str.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        # Only resolve events that started at least 4 hours ago
        # (enough time for most UFC fights to complete)
        if now < start_time + timedelta(hours=4):
            continue

        checked += 1
        fighter_a = record.get("bias_inputs", {}).get("fighter_a", {}).get("name", "")
        fighter_b = record.get("bias_inputs", {}).get("fighter_b", {}).get("name", "")

        print(f"  [Resolve] {record['market_id']} — {fighter_a} vs {fighter_b}")

        result = fetch_fight_result(fighter_a, fighter_b)

        if not result["confirmed"]:
            print(f"    Result not found yet — will retry next run")
            continue

        winner = result["winner"]
        method = result["method"]
        pnl = compute_pnl(record, winner)

        records[i]["resolution"].update({
            "actual_result": winner,
            "result_method": method,
            "settled_at": now.isoformat().replace("+00:00", "Z"),
            "pnl_zmw": pnl
        })

        resolved_count += 1
        print(f"    Winner: {winner} ({method}) | P&L: {'+' if pnl >= 0 else ''}{pnl} ZMW")

    # Rewrite snapshots.jsonl
    try:
        with open("snapshots.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    except Exception as e:
        print(f"[Resolver] Error writing snapshots.jsonl: {e}")
        return

    print(f"\n[Resolver] Done. Checked: {checked} | Resolved: {resolved_count}")


if __name__ == "__main__":
    run()
