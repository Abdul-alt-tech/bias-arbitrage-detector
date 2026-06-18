"""
calibrator.py
-------------
Phase 5 module. Self-correction engine.

Reads all resolved records from snapshots.jsonl, computes
per-bias_type win rates, and updates calibration.json with
confidence multipliers.

Rules (from source_of_truth §9):
- A bias_type bucket's multiplier stays 1.0 until sample_size >= 20
- multiplier = actual_winrate / implied_winrate_from_raw_confidence
- Also tracks global stats for the dashboard

Run manually: python calibrator.py
Called by GitHub Actions scan.yml after resolver.py.
"""

import json
import os
from datetime import datetime, timezone


BIAS_TYPES = [
    "recency",
    "star",
    "durability_age",
    "layoff",
    "style_matchup",
    "overreaction"
]

MIN_SAMPLE_SIZE = 20


def load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


def load_or_init_calibration() -> dict:
    """Load calibration.json or create a fresh one."""
    cal_path = "calibration.json"
    if os.path.exists(cal_path):
        try:
            with open(cal_path) as f:
                content = f.read().strip()
                if not content:
                    # File exists but is empty, create fresh one
                    return _create_fresh_calibration()
                return json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Calibrator] Warning: Could not load calibration.json ({e}), starting fresh")
            return _create_fresh_calibration()

    return _create_fresh_calibration()


def _create_fresh_calibration() -> dict:
    """Create a fresh calibration structure."""
    return {
        "min_sample_size": MIN_SAMPLE_SIZE,
        "bias_type_multipliers": {
            bt: {
                "multiplier": 1.0,
                "sample_size": 0,
                "actual_winrate": None,
                "implied_winrate": None
            }
            for bt in BIAS_TYPES
        },
        "global_stats": {
            "total_scanned": 0,
            "total_flagged": 0,
            "total_resolved": 0,
            "overall_winrate": None,
            "total_pnl_zmw": 0.0
        },
        "confidence_buckets": {},
        "last_updated": None
    }


def implied_winrate_from_confidence(records_in_bucket: list) -> float:
    """
    Estimate implied win rate from average raw_confidence.
    Rough mapping: confidence = P(bias is real) * P(winning given real bias).
    We treat avg confidence as a rough implied win probability.
    """
    if not records_in_bucket:
        return 0.5
    avg_conf = sum(
        r["llm_output"].get("raw_confidence", 0.5)
        for r in records_in_bucket
    ) / len(records_in_bucket)
    # Map 0-1 confidence to ~0.5-0.8 implied win rate range
    # (0 conf = 50% = coin flip, 1.0 conf = ~80% theoretical max)
    return round(0.5 + (avg_conf * 0.3), 4)


def determine_winner(record: dict) -> bool:
    """
    Returns True if the bet we would have placed won.
    Mirrors compute_pnl logic from resolver.py.
    """
    pnl = record.get("resolution", {}).get("pnl_zmw")
    if pnl is None:
        return None
    return pnl >= 0


def run():
    if not os.path.exists("snapshots.jsonl"):
        print("[Calibrator] No snapshots.jsonl found.")
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
                    print(f"[Calibrator] Warning: Skipping malformed JSON at line {line_num}: {e}")
                    continue
    except Exception as e:
        print(f"[Calibrator] Error reading snapshots.jsonl: {e}")
        return

    if not records:
        print("[Calibrator] No valid records found in snapshots.jsonl")
        return

    cal = load_or_init_calibration()

    # --- Global stats ---
    total_scanned = len(records)
    flagged = [r for r in records if r.get("alert", {}).get("triggered")]
    resolved = [r for r in records
                if r.get("resolution", {}).get("actual_result") is not None]
    resolved_flagged = [r for r in resolved if r.get("alert", {}).get("triggered")]

    total_pnl = sum(
        r.get("resolution", {}).get("pnl_zmw", 0) or 0
        for r in resolved_flagged
    )

    wins = [r for r in resolved_flagged if determine_winner(r)]
    overall_winrate = round(len(wins) / len(resolved_flagged), 4) if resolved_flagged else None

    cal["global_stats"].update({
        "total_scanned": total_scanned,
        "total_flagged": len(flagged),
        "total_resolved": len(resolved_flagged),
        "overall_winrate": overall_winrate,
        "total_pnl_zmw": round(total_pnl, 2)
    })

    # --- Per bias_type calibration ---
    for bias_type in BIAS_TYPES:
        bucket_records = [
            r for r in resolved_flagged
            if r.get("llm_output", {}).get("bias_type") == bias_type
        ]

        sample_size = len(bucket_records)
        if sample_size == 0:
            continue

        bucket_wins = [r for r in bucket_records if determine_winner(r)]
        actual_winrate = round(len(bucket_wins) / sample_size, 4)
        implied_wr = implied_winrate_from_confidence(bucket_records)

        # Only update multiplier if sample size floor is met
        if sample_size >= MIN_SAMPLE_SIZE:
            multiplier = round(actual_winrate / implied_wr, 4) if implied_wr > 0 else 1.0
            # Cap multiplier between 0.3 and 2.0 to prevent extreme adjustments
            multiplier = max(0.3, min(2.0, multiplier))
        else:
            multiplier = 1.0  # No adjustment until floor met

        cal["bias_type_multipliers"][bias_type].update({
            "multiplier": multiplier,
            "sample_size": sample_size,
            "actual_winrate": actual_winrate,
            "implied_winrate": implied_wr
        })

        status = "✓ CALIBRATING" if sample_size >= MIN_SAMPLE_SIZE else f"⏳ {sample_size}/{MIN_SAMPLE_SIZE} samples"
        print(f"  [{bias_type}] winrate: {actual_winrate:.1%} | "
              f"multiplier: {multiplier} | {status}")

    # --- Confidence bucket tracking ---
    # Groups resolved flagged records into 0.1-width confidence bands
    # Useful for the dashboard calibration view
    bucket_stats = {}
    for r in resolved_flagged:
        cal_conf = r.get("llm_output", {}).get("calibrated_confidence", 0) or 0
        bucket_key = f"{int(cal_conf * 10) * 10}-{int(cal_conf * 10) * 10 + 10}"
        if bucket_key not in bucket_stats:
            bucket_stats[bucket_key] = {"total": 0, "wins": 0, "winrate": None}
        bucket_stats[bucket_key]["total"] += 1
        if determine_winner(r):
            bucket_stats[bucket_key]["wins"] += 1

    for bk, bv in bucket_stats.items():
        if bv["total"] > 0:
            bv["winrate"] = round(bv["wins"] / bv["total"], 4)

    cal["confidence_buckets"] = bucket_stats
    cal["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        with open("calibration.json", "w") as f:
            json.dump(cal, f, indent=2)
    except Exception as e:
        print(f"[Calibrator] Error writing calibration.json: {e}")
        return

    print(f"\n[Calibrator] Done.")
    print(f"  Total scanned: {total_scanned} | Flagged: {len(flagged)} | "
          f"Resolved: {len(resolved_flagged)}")
    if overall_winrate:
        print(f"  Overall win rate: {overall_winrate:.1%} | "
              f"Paper P&L: {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} ZMW")
    else:
        print(f"  No resolved flagged bets yet — keep logging!")


if __name__ == "__main__":
    run()
