"""
scoring.py
----------
Phase 2 module. Reads snapshots.jsonl, computes raw_edge,
edge_percent, and payout_multiple for each unscored record,
and updates the record in place.

Records where reference_prob or market_prob is None are skipped
(marked as scoring_output.skipped = True).

Run manually: python scoring.py
Called by GitHub Actions scan.yml after data_collector.py.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional


def load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


def compute_payout_multiple(market_prob: float, raw_edge: float,
                             reference_prob: float) -> float:
    """
    Payout multiple = 1 / price_of_the_side_we_would_bet.

    We bet the side that is UNDERPRICED by the market relative
    to the reference. If market_prob < reference_prob, the market
    is underpricing fighter_a winning — we bet YES at market_prob.
    If market_prob > reference_prob, the market is overpricing
    fighter_a — we bet NO (i.e. fighter_b wins) at 1 - market_prob.
    """
    if market_prob < reference_prob:
        # Bet YES — market underpricing fighter_a
        bet_price = market_prob
    else:
        # Bet NO — market overpricing fighter_a
        bet_price = 1.0 - market_prob

    if bet_price <= 0:
        return 0.0
    return round(1.0 / bet_price, 4)


def compute_kelly_bet(edge_percent: float, payout_multiple: float,
                      bankroll: float, kelly_fraction: float) -> float:
    """
    Kelly criterion bet size.
    f = (bp - q) / b
    where b = payout_multiple - 1 (net odds),
           p = implied win probability,
           q = 1 - p

    Returns suggested bet as a fraction of bankroll,
    capped at kelly_fraction.
    """
    if payout_multiple <= 1.0 or edge_percent <= 0:
        return 0.0

    b = payout_multiple - 1.0
    # Rough implied win prob from edge% + threshold
    p = min(0.95, 0.5 + (edge_percent / 100.0))
    q = 1.0 - p

    kelly_f = (b * p - q) / b
    kelly_f = max(0.0, kelly_f)
    capped = min(kelly_f, kelly_fraction)
    return round(capped, 4)


def score_record(record: dict, config: dict) -> dict:
    """
    Compute and inject scoring_output fields into a record.
    Returns the updated record.
    """
    risk_mode = config.get("risk_mode", "conservative")
    edge_threshold = config["edge_threshold_pct"][risk_mode] / 100.0
    kelly_fraction = config["kelly_fraction"][risk_mode]
    bankroll = config["bankroll"]["value"]

    market_prob = record.get("market_data", {}).get("market_prob")
    reference_prob = record.get("fundamentals", {}).get("reference_prob")

    # Skip if we don't have both prices
    if market_prob is None or reference_prob is None:
        record["scoring_output"]["skipped"] = True
        record["scoring_output"]["skip_reason"] = "missing market_prob or reference_prob"
        return record

    raw_edge = round(abs(market_prob - reference_prob), 4)
    edge_percent = round((raw_edge - edge_threshold) * 100, 2)
    payout_multiple = compute_payout_multiple(market_prob, raw_edge, reference_prob)

    kelly_frac = compute_kelly_bet(edge_percent, payout_multiple, bankroll, kelly_fraction)
    suggested_bet_zmw = round(bankroll * kelly_frac, 2)

    record["scoring_output"].update({
        "raw_edge": raw_edge,
        "edge_percent": edge_percent,
        "payout_multiple": payout_multiple,
        "llm_called": False,
        "skipped": False
    })

    # Pre-fill alert sizing (alerter.py will set triggered=True if thresholds met)
    record["alert"]["suggested_bet_pct"] = round(kelly_frac * 100, 2)
    record["alert"]["suggested_bet_value_zmw"] = suggested_bet_zmw

    return record


def run():
    config = load_config()
    risk_mode = config.get("risk_mode", "conservative")
    edge_threshold_pct = config["edge_threshold_pct"][risk_mode]

    if not os.path.exists("snapshots.jsonl"):
        print("[Scoring] No snapshots.jsonl found. Run data_collector.py first.")
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
                    print(f"[Scoring] Warning: Skipping malformed JSON at line {line_num}: {e}")
                    continue
    except Exception as e:
        print(f"[Scoring] Error reading snapshots.jsonl: {e}")
        return

    if not records:
        print("[Scoring] No valid records found in snapshots.jsonl")
        return

    scored = 0
    skipped = 0
    llm_candidates = 0

    updated_records = []
    for record in records:
        # Only score records that haven't been scored yet
        if record.get("scoring_output", {}).get("raw_edge") is not None:
            updated_records.append(record)
            continue

        record = score_record(record, config)
        updated_records.append(record)

        if record["scoring_output"].get("skipped"):
            skipped += 1
        else:
            scored += 1
            edge_pct = record["scoring_output"]["edge_percent"]
            if edge_pct > 0:
                llm_candidates += 1
                print(f"  [candidate] {record['market_id']} — "
                      f"edge: {edge_pct:.1f}% | "
                      f"payout: {record['scoring_output']['payout_multiple']}x | "
                      f"bet: {record['alert']['suggested_bet_value_zmw']} ZMW")

    # Rewrite snapshots.jsonl with updated records
    try:
        with open("snapshots.jsonl", "w") as f:
            for r in updated_records:
                f.write(json.dumps(r) + "\n")
    except Exception as e:
        print(f"[Scoring] Error writing snapshots.jsonl: {e}")
        return

    print(f"\n[Scoring] Done. Scored: {scored} | Skipped: {skipped} | "
          f"LLM candidates (edge > {edge_threshold_pct}%): {llm_candidates}")


if __name__ == "__main__":
    run()
