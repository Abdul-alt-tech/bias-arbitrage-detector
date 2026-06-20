"""
llm_reasoner.py
---------------
Phase 2 module. For each record where edge_percent > threshold
and llm_called = False, runs the bias-detection prompt and
injects llm_output into the record.

Supports Groq (default, free tier) and Anthropic Claude.
Provider is configured in config.json under api_keys.llm_provider.

After calibration is applied, also finalizes the dual-mode
would_alert_conservative / would_alert_aggressive flags from
scoring.py — these need calibrated_confidence, which only
exists after this step runs.

Run manually: python llm_reasoner.py
Called by GitHub Actions scan.yml after scoring.py.
"""

import json
import os
import requests
from datetime import datetime, timezone

from scoring import finalize_alert_flags


def load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a betting bias detector for UFC fight markets. Be skeptical. Default answer = no edge.

Your job is to detect whether a gap between market_prob (prediction market crowd price) and reference_prob (sportsbook baseline) is explained by a known cognitive bias, or by genuine new information.

Rules:
- If not confident, set confidence = 0
- Never invent fighter news, injuries, or stats not present in the input
- Do not compute or return edge_percent or payout_multiple — those are calculated separately
- Return ONLY valid JSON. No text before or after the JSON object."""

USER_PROMPT_TEMPLATE = """Input market data:
{market_json}

Task:
1. Which bias is most likely driving the gap between market_prob ({market_prob_pct}%) and reference_prob ({reference_prob_pct}%)?
   Choose one: recency, star, durability_age, layoff, style_matchup, overreaction, none

2. Is this gap justified by the fighter form/news data provided, or does it look like a detectable bias?

3. Return ONLY valid JSON in exactly this format, no other text:
{{"bias_type": "X", "confidence": 0.0, "reason": "max 2 short points, 200 chars total"}}"""


def build_prompt_input(record: dict) -> dict:
    """
    Build the sanitized input dict for the LLM prompt.
    Strips llm_output, alert, resolution, and scoring_output
    to avoid feedback loops.
    """
    safe = {
        "market_id": record.get("market_id"),
        "question": record.get("question"),
        "sport": record.get("sport"),
        "event_name": record.get("event_name"),
        "start_time": record.get("start_time"),
        "market_data": record.get("market_data"),
        "fundamentals": record.get("fundamentals"),
        "bias_inputs": record.get("bias_inputs")
    }
    return safe


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------

def call_groq(prompt_input: dict, market_prob: float, reference_prob: float,
              api_key: str, model: str = "llama3-8b-8192") -> dict:
    """Call Groq API (free tier, fast)."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    user_message = USER_PROMPT_TEMPLATE.format(
        market_json=json.dumps(prompt_input, indent=2),
        market_prob_pct=round(market_prob * 100, 1),
        reference_prob_pct=round(reference_prob * 100, 1)
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ],
        "temperature": 0.1,
        "max_tokens": 200
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()

    # Validate response is not empty before parsing
    if not resp.content or not resp.text.strip():
        raise ValueError("Empty response from Groq API")

    try:
        content = resp.json()["choices"][0]["message"]["content"].strip()
        return parse_llm_response(content)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [LLM] Error parsing Groq response: {e}")
        raise ValueError(f"Invalid Groq response format: {e}")


def call_anthropic(prompt_input: dict, market_prob: float, reference_prob: float,
                   api_key: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    """Call Anthropic Claude API."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }

    user_message = USER_PROMPT_TEMPLATE.format(
        market_json=json.dumps(prompt_input, indent=2),
        market_prob_pct=round(market_prob * 100, 1),
        reference_prob_pct=round(reference_prob * 100, 1)
    )

    payload = {
        "model": model,
        "max_tokens": 200,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_message}
        ]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()

    # Validate response is not empty before parsing
    if not resp.content or not resp.text.strip():
        raise ValueError("Empty response from Anthropic API")

    try:
        content = resp.json()["content"][0]["text"].strip()
        return parse_llm_response(content)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [LLM] Error parsing Anthropic response: {e}")
        raise ValueError(f"Invalid Anthropic response format: {e}")


def parse_llm_response(raw: str) -> dict:
    """
    Parse strict JSON from LLM response.
    Strips any accidental markdown fences.
    Returns a safe default if parsing fails.
    """
    if not raw or not raw.strip():
        print(f"  [LLM] Empty LLM response received")
        return {
            "bias_type": "none",
            "raw_confidence": 0.0,
            "reason": "empty_response"
        }

    clean = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(clean)
        return {
            "bias_type": parsed.get("bias_type", "none"),
            "raw_confidence": float(parsed.get("confidence", 0.0)),
            "reason": str(parsed.get("reason", ""))[:250]
        }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"  [LLM] Failed to parse response: {raw[:100]} | Error: {e}")
        return {
            "bias_type": "none",
            "raw_confidence": 0.0,
            "reason": "parse_error"
        }


# ---------------------------------------------------------------------------
# Calibration application
# ---------------------------------------------------------------------------

def apply_calibration(bias_type: str, raw_confidence: float) -> float:
    """
    Apply calibration multiplier from calibration.json.
    Returns calibrated_confidence.
    Before enough data exists (sample_size < min_sample_size),
    multiplier stays 1.0 — no adjustment.
    """
    cal_path = "calibration.json"
    if not os.path.exists(cal_path):
        return raw_confidence

    try:
        with open(cal_path) as f:
            content = f.read().strip()
            if not content:
                return raw_confidence
            cal = json.loads(content)

        min_samples = cal.get("min_sample_size", 20)
        bucket = cal.get("bias_type_multipliers", {}).get(bias_type, {})
        sample_size = bucket.get("sample_size", 0)
        multiplier = bucket.get("multiplier", 1.0)

        if sample_size < min_samples:
            return raw_confidence  # Not enough data yet

        return round(min(1.0, raw_confidence * multiplier), 4)

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"  [LLM] Error reading calibration.json: {e}")
        return raw_confidence


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    config = load_config()
    risk_mode = config.get("risk_mode", "conservative")
    edge_threshold_pct = config["edge_threshold_pct"][risk_mode]
    api_keys = config.get("api_keys", {})

    provider = api_keys.get("llm_provider", "groq").lower()
    llm_key = api_keys.get("llm_api_key", os.environ.get("LLM_API_KEY", ""))
    llm_model = api_keys.get("llm_model", "llama3-8b-8192")

    if not os.path.exists("snapshots.jsonl"):
        print("[LLM] No snapshots.jsonl found.")
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
                    print(f"[LLM] Warning: Skipping malformed JSON at line {line_num}: {e}")
                    continue
    except Exception as e:
        print(f"[LLM] Error reading snapshots.jsonl: {e}")
        return

    if not records:
        print("[LLM] No valid records found in snapshots.jsonl")
        return

    called = 0
    skipped = 0
    finalized = 0

    for i, record in enumerate(records):
        scoring = record.get("scoring_output", {})
        edge_pct = scoring.get("edge_percent", 0) or 0

        # Only call LLM for records with edge above threshold
        # and that haven't been processed yet
        if scoring.get("llm_called") or scoring.get("skipped"):
            continue

        # Even if the ACTIVE mode's edge is too low, the record might
        # still qualify under the OTHER mode — finalize its dual-mode
        # alert flags as False (no LLM judgment exists for it) rather
        # than leaving them as None.
        if edge_pct <= 0:
            would_qual_agg = scoring.get("would_qualify_aggressive", False)
            if would_qual_agg:
                # Aggressive mode would have looked at this market, but
                # we only call the LLM once per record to control cost —
                # mark as not alertable under either mode since no LLM
                # judgment was made.
                record["scoring_output"]["would_alert_conservative"] = False
                record["scoring_output"]["would_alert_aggressive"] = False
            skipped += 1
            continue

        market_prob = record["market_data"]["market_prob"]
        reference_prob = record["fundamentals"]["reference_prob"]
        prompt_input = build_prompt_input(record)

        print(f"  [LLM] {record['market_id']} — {record.get('question', '')[:50]}")

        try:
            if provider == "groq":
                result = call_groq(prompt_input, market_prob, reference_prob,
                                   llm_key, llm_model)
            elif provider == "anthropic":
                result = call_anthropic(prompt_input, market_prob, reference_prob,
                                        llm_key, llm_model)
            else:
                print(f"  [LLM] Unknown provider: {provider}")
                continue

            calibrated = apply_calibration(result["bias_type"], result["raw_confidence"])

            record["llm_output"].update({
                "bias_type": result["bias_type"],
                "raw_confidence": result["raw_confidence"],
                "calibrated_confidence": calibrated,
                "reason": result["reason"]
            })
            record["scoring_output"]["llm_called"] = True

            # Finalize would_alert_conservative / would_alert_aggressive
            # now that calibrated_confidence is known
            record = finalize_alert_flags(record, config)
            finalized += 1

            records[i] = record
            called += 1

            print(f"    bias: {result['bias_type']} | "
                  f"conf: {result['raw_confidence']} -> calibrated: {calibrated} | "
                  f"would_alert: cons={record['scoring_output']['would_alert_conservative']} "
                  f"agg={record['scoring_output']['would_alert_aggressive']} | "
                  f"{result['reason'][:80]}")

        except Exception as e:
            print(f"  [LLM] Error on {record['market_id']}: {e}")
            continue

    # Rewrite snapshots.jsonl
    try:
        with open("snapshots.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    except Exception as e:
        print(f"[LLM] Error writing snapshots.jsonl: {e}")
        return

    print(f"\n[LLM] Done. Called: {called} | Finalized dual-mode flags: {finalized} | "
          f"Below threshold (skipped): {skipped}")


if __name__ == "__main__":
    run()
