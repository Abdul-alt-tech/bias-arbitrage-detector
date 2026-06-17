# Bias Arbitrage Detector — Source of Truth v1.2

> Supersedes v1.1. This is the frozen technical reference. Every module reads/writes the formats defined here. Changes to this doc should be deliberate — treat it as the contract the code follows.

---

## 1. Core Concept

**Problem:** Prediction markets (Kalshi / Polymarket) overreact to recency, star power, streaks, and narrative — especially in sports.

**Solution:** Don't predict outcomes directly. Compare a *crowd price* (the prediction market) against a *reference price* (sportsbook-derived probability), and use an LLM to judge whether the gap reflects genuine new information or a detectable bias.

**Edge thesis:** Sportsbooks price fundamentals efficiently. Prediction-market crowds sometimes panic, hype, or anchor on narratives. We bet the gap between the two — when an LLM agrees the gap looks like bias rather than information.

**Success definition:** 52-56% win rate on flagged bets over 300-500+ resolved bets. Not 70%. (53% beats most bettors; 55%+ is elite.)

**Starting sport: UFC.** Chosen for v1 because:
- Weekly cadence year-round (no off-season gap, unlike NBA which just ended its 2025-26 season)
- Structurally simple: binary outcome (no draws, no home/away)
- Sportsbook MMA odds (reference price) are reasonably efficient; prediction-market crowd pricing on MMA is less mature than NBA/NFL, plausibly creating wider gaps
- Kalshi has confirmed liquid UFC markets (UFC 318 was >13% of weekly platform volume)

NBA and soccer remain on the roadmap (schema is designed to generalize — see §4 and §11), but are out of scope for the initial build.

---

## 2. Definition of "Statistical Good Bet" v1.2

| Variable | Formula / Rule | Notes |
|---|---|---|
| `reference_prob` | Sportsbook-implied probability (TheOddsAPI, MMA) | The "efficient" baseline |
| `market_prob` | Kalshi or Polymarket YES price (0-1) | What the crowd is paying now |
| `raw_edge` | `abs(market_prob - reference_prob)` | Gross mispricing |
| `edge_threshold` | Config-driven (see §8, risk_mode) | Covers fees + slippage + safety margin |
| `edge_percent` | `raw_edge - edge_threshold` | Net edge |
| `payout_multiple` | `1 / price_of_side_bet` | Reward side of risk/reward — e.g. betting YES at 0.15 pays ~6.7x |
| `raw_confidence` | LLM output, 0.0-1.0 | Raw LLM judgment of "is this bias" |
| `calibrated_confidence` | `raw_confidence * calibration_multiplier[bias_type]` | See §9 |
| **Alert rule** | `edge_percent > edge_threshold AND calibrated_confidence > confidence_cutoff` | Thresholds are risk_mode-dependent (§8) |

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  EXCHANGE ADAPTER LAYER  (platform-independent)                  │
│  exchanges/kalshi.py        exchanges/polymarket.py              │
│  Each exposes:                                                    │
│   - list_upcoming_markets(sport, days_ahead)                     │
│   - get_price_snapshot(market_ref) -> {market_prob, prob_24h_ago}│
│  Normalizes price units (Kalshi cents -> 0-1 probability)        │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 1 — BIAS SCANNER  (build first)                           │
│  data_collector.py                                                │
│   - Pulls upcoming UFC markets from configured platform(s)       │
│   - Pulls reference_prob from TheOddsAPI (MMA)                   │
│   - Pulls fighter-level bias_inputs (UFCStats / ESPN MMA)        │
│   - 24h price history: platform API, falling back to local       │
│     snapshots.jsonl log if platform history unavailable          │
│  scoring.py                                                       │
│   - Computes raw_edge, edge_percent, payout_multiple             │
│   - Filters by risk_mode edge_threshold before calling LLM       │
│  llm_reasoner.py                                                  │
│   - Strict JSON-only prompt (§6), low call volume                │
│  Goal: 70% of value, 20% of work. Catch obvious overreactions.   │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 2 — CONTEXT ENGINE  (add after 200+ logs)                 │
│   + Fighter Elo/ranking history, style-matchup database          │
│   + "When this fighter was on a 2-fight win streak before,       │
│      market did X" historical pattern lookup                     │
│   + Track whether platform settled correctly last time            │
│  Prompt upgrade: "Given fighter_baseline + market_history +       │
│  recent news, is this an overreaction?"                           │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 3 — CALIBRATION ENGINE  (add after 300-500 resolved bets) │
│  calibrator.py                                                    │
│   - Per bias_type win-rate tracking (§9)                          │
│   - Auto-adjusts calibrated_confidence                            │
│   - risk_mode dial: conservative -> aggressive, per bias_type     │
│   - Kelly sizing output                                           │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  PRESENTATION & ALERTING LAYER                                    │
│  alerter.py     -> Email (Gmail SMTP) on alert trigger            │
│  resolver.py    -> Fills resolution.* once event concludes        │
│  dashboard/     -> Streamlit app, blue theme (§10)                │
│                    Live Flags / History / Calibration views       │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  HOSTING LAYER (free, external, laptop-independent)               │
│  GitHub repo + GitHub Actions (cron schedule)                     │
│   - Runs full pipeline every 2-3 hrs regardless of laptop state   │
│   - Commits snapshots.jsonl + calibration.json back to repo       │
│     (also keeps the scheduled workflow "alive")                   │
│  Streamlit Community Cloud reads repo -> dashboard auto-updates   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Data Schema v1.2

Freeze this JSON. Generalized field names (`reference_prob`, `reference_source`, `platform`) so soccer/NBA can slot in later without breaking the schema.

```json
{
  "market_id": "kalshi_KXUFCFIGHT-26JUN20TOPGAE-TOP",
  "platform": "kalshi",
  "market_url": "https://kalshi.com/markets/kxufcfight/...",
  "timestamp": "2026-06-18T10:00:00Z",
  "question": "Will Ilia Topuria beat Justin Gaethje?",
  "sport": "UFC",
  "event_name": "UFC 320",
  "start_time": "2026-06-20T22:00:00Z",

  "market_data": {
    "market_prob": 0.62,
    "prob_24h_ago": 0.55,
    "price_change_24h": 0.07,
    "volume_24h": 45000,
    "price_history_source": "platform_api"
  },

  "fundamentals": {
    "reference_prob": 0.55,
    "reference_source": "sportsbook_average_mma"
  },

  "bias_inputs": {
    "fighter_a": {
      "name": "Ilia Topuria",
      "last_3_results": [
        {"result": "W", "method": "KO"},
        {"result": "W", "method": "KO"},
        {"result": "W", "method": "DEC"}
      ],
      "days_since_last_fight": 210,
      "weight_class": "Lightweight",
      "news_flags": ["coming off viral KO win"]
    },
    "fighter_b": {
      "name": "Justin Gaethje",
      "last_3_results": [
        {"result": "L", "method": "KO"},
        {"result": "W", "method": "KO"},
        {"result": "L", "method": "SUB"}
      ],
      "days_since_last_fight": 365,
      "weight_class": "Lightweight",
      "news_flags": ["long layoff", "no camp changes reported"]
    }
  },

  "scoring_output": {
    "raw_edge": 0.07,
    "edge_percent": 2.0,
    "payout_multiple": 1.61,
    "llm_called": true
  },

  "llm_output": {
    "bias_type": "star",
    "raw_confidence": 0.78,
    "calibrated_confidence": 0.62,
    "reason": "Topuria's recent KO highlight likely overpricing him. Gaethje's durability concerns may be overweighted."
  },

  "alert": {
    "triggered": false,
    "risk_mode": "conservative",
    "suggested_bet_pct": 1.4,
    "suggested_bet_value_zmw": 2.8
  },

  "resolution": {
    "actual_result": null,
    "settled_at": null,
    "pnl_zmw": null
  }
}
```

### Schema notes
- `platform` + `market_id` prefix together identify the source unambiguously.
- `market_data.price_history_source`: `"platform_api"` or `"local_log"` — tracks which fallback was used for `prob_24h_ago`.
- `fundamentals.reference_source` is generalized on purpose — future sports/markets (CME FedWatch, Deribit IV, etc.) plug into the same field.
- `bias_inputs` is UFC-shaped for v1. Soccer/NBA will use a different internal shape under the same top-level key (team-based instead of fighter-based).
- `scoring_output.edge_percent` and `payout_multiple` are computed by `scoring.py` — never by the LLM.
- `llm_output.raw_confidence` vs `calibrated_confidence`: raw is the LLM's direct output; calibrated is post-§9 adjustment. Before any calibration data exists, calibrated = raw.
- `alert.suggested_bet_value_zmw` is **paper money** until the real-money decision point (§8.3).

---

## 5. Exchange Adapters

Both adapters expose the same 3 functions; everything downstream is platform-agnostic.

| Concern | Kalshi | Polymarket |
|---|---|---|
| Price units | Cents (1-99) → normalize to 0-1 | Already 0-1 |
| Auth for reads | API key recommended (needed for automation later anyway) | None required |
| 24h price history | Verify availability per-market; fall back to local log | CLOB price-history endpoint generally available |
| Market discovery | Series/ticker-based (verify UFC series ticker before build) | Event/slug-based via Gamma API |

**Fallback for `prob_24h_ago`:** every snapshot run appends `(market_id, timestamp, market_prob)` to `snapshots.jsonl`. If the platform's history endpoint is unavailable, `data_collector.py` looks up the closest prior entry to "now minus 24h" for that `market_id`. First sighting of a market may have `prob_24h_ago: null` — that's fine, it logs and becomes useful next pass.

---

## 6. LLM Prompt Template v1.2

```
You are a betting bias detector for UFC fight markets. Be skeptical. Default answer = no edge.

Input JSON:
{insert market JSON, excluding the llm_output field}

Task:
1. Which bias is most likely? Choose one: recency, star, durability_age, layoff, style_matchup, overreaction, none
2. Is the gap between market_prob and reference_prob justified by the fighter
   form/news data provided, or does it look like a detectable bias?
3. Return ONLY valid JSON. No text before or after:
   {"bias_type": "X", "confidence": 0.0-1.0, "reason": "max 2 short points, 200 chars total"}

Rules:
- If not confident, set confidence = 0
- Never invent fighter news, injuries, or stats not present in the input
- Do not compute or return edge_percent or payout_multiple — those are
  calculated separately
```

---

## 7. Workflow (Snapshot-Based, Not Continuous Loop)

Each run of the GitHub Actions workflow is a **self-contained scan**:

1. `data_collector.py` — for each configured platform (Kalshi and/or Polymarket), find upcoming UFC markets within the lookahead window, fetch current price + 24h-ago price (platform API or local-log fallback), pull `reference_prob` from TheOddsAPI, pull `bias_inputs` from UFCStats/ESPN MMA.
2. `scoring.py` — compute `raw_edge`, `edge_percent`, `payout_multiple`. Filter to markets where `edge_percent > edge_threshold` (risk_mode-dependent).
3. `llm_reasoner.py` — run the prompt (§6) only on filtered markets (~handful per run).
4. `calibrator.py` — apply `calibration.json` multipliers → `calibrated_confidence`.
5. `alerter.py` — if `calibrated_confidence > confidence_cutoff`, set `alert.triggered = true`, send email.
6. `resolver.py` — for past entries whose `start_time` has passed and `resolution.actual_result` is still null, fetch the actual result and compute paper `pnl_zmw`.
7. Append/update `snapshots.jsonl`, recompute `calibration.json` if new resolutions exist, commit + push.
8. Streamlit dashboard reads the updated repo state automatically.

**Schedule:** every 2-3 hours via GitHub Actions `schedule` (cron). Not guaranteed to the minute — fine for weekly-cadence UFC events.

---

## 8. Risk Mode & Bankroll

### 8.1 Config (`config.json`)

```json
{
  "risk_mode": "conservative",
  "bankroll": {"mode": "paper", "value": 200, "currency": "ZMW"},
  "kelly_fraction": {"conservative": 0.25, "aggressive": 0.75},
  "edge_threshold_pct": {"conservative": 5, "aggressive": 3},
  "confidence_cutoff": {"conservative": 0.7, "aggressive": 0.5},
  "alert_sort": {
    "conservative": "calibrated_confidence",
    "aggressive": "calibrated_confidence_x_payout_multiple"
  }
}
```

- Switching `risk_mode` is the only change needed to go from conservative to aggressive — same pipeline, three numbers and one sort order change.
- `calibration.json` can later store **per-bias_type** risk_mode overrides (e.g., "star" bias validated → aggressive; "recency" bias still conservative).

### 8.2 Paper Trading First

All bet sizes are computed but **not placed**. `bankroll.mode: "paper"` means `pnl_zmw` and `suggested_bet_value_zmw` are tracked numbers in the log, not real transactions.

Rationale:
- The calibration engine needs 300-500 resolved bets before it means anything — that's months regardless of real money.
- At 200 ZMW (~$7-8 USD), individual Kelly-sized bets (4-10 ZMW, ~$0.15-0.35) are likely below practical platform minimums, and currency-conversion/funding fees (especially Kalshi international wire transfers) could exceed any plausible monthly profit.
- Paper trading avoids both problems entirely while producing the same calibration dataset.

### 8.3 Real-Money Decision Point

Move to real funding only after:
- 300-500 resolved paper bets with documented win rate, AND
- A specific bankroll size is chosen based on platform minimum-trade-size constraints and conversion-fee analysis at that time (likely 5,000+ ZMW, per original v1.1 math, re-verified against then-current fees).

---

## 9. Calibration Engine (`calibration.json`)

```json
{
  "min_sample_size": 20,
  "bias_type_multipliers": {
    "recency":        {"multiplier": 1.0, "sample_size": 0, "actual_winrate": null},
    "star":           {"multiplier": 1.0, "sample_size": 0, "actual_winrate": null},
    "durability_age": {"multiplier": 1.0, "sample_size": 0, "actual_winrate": null},
    "layoff":         {"multiplier": 1.0, "sample_size": 0, "actual_winrate": null},
    "style_matchup":  {"multiplier": 1.0, "sample_size": 0, "actual_winrate": null},
    "overreaction":   {"multiplier": 1.0, "sample_size": 0, "actual_winrate": null}
  },
  "global_stats": {
    "total_scanned": 0,
    "total_flagged": 0,
    "total_resolved": 0,
    "overall_winrate": null
  },
  "last_updated": null
}
```

**Rules:**
- A `bias_type` bucket's `multiplier` stays at `1.0` (no adjustment) until `sample_size >= min_sample_size` (20). Below that floor, every bucket behaves as if uncalibrated — this prevents early overfitting to small samples (e.g., "recency went 2-for-2 so far" is noise, not signal).
- Once the floor is met, `multiplier = actual_winrate / implied_winrate_from_raw_confidence` (roughly — exact formula to be refined once real data exists).
- Log **every scanned market**, not just flagged ones — this gives `calibrator.py` a much larger backtest dataset to retroactively test alternative thresholds (`edge_threshold`, `confidence_cutoff`).

---

## 10. Dashboard (Streamlit Community Cloud)

- Connects directly to the GitHub repo (public or private), auto-redeploys on new commits.
- **Theme:** blue-dominant via `[theme]` config — navy/dark-blue background, lighter blue panels/cards, cyan/bright-blue accent for buttons, active tabs, and `st.metric` highlights. Charts (Plotly) themed to match.
- **Three views:**
  1. **Live Flags** — currently-triggered alerts: market_prob vs reference_prob, edge_percent, bias_type, raw vs calibrated confidence, payout_multiple, suggested Kelly size, full LLM reasoning text, direct link to `market_url`, and a generated "more info" search link for each fighter.
  2. **History/Log** — every scanned market, filterable by bias_type / resolution status / platform.
  3. **Calibration/Performance** — win rate by bias_type and confidence bucket, multiplier history, overall track record (total flagged, resolved, win rate, paper P&L).

---

## 11. Hosting & Tech Stack

| Component | Choice | Notes |
|---|---|---|
| Code + scheduler | GitHub repo + GitHub Actions (`schedule` cron) | Free; private repo's 2,000 min/month free tier is far more than needed |
| Secrets | GitHub Actions encrypted secrets | TheOddsAPI key, Kalshi API key, LLM API key, Gmail app password |
| LLM | Groq/Gemini free tier, or Claude Haiku (effectively free at this volume) | ~5 calls/week |
| Email alerts | Gmail SMTP + app password | Headline + link to dashboard |
| Persistence | `snapshots.jsonl` + `calibration.json`, git-committed each run | Self-sustains the scheduled workflow (commit = activity) |
| Dashboard | Streamlit Community Cloud | Blue theme (§10) |

**Future automation:** `kalshi_executor.py` / `polymarket_executor.py` would implement a shared `place_order()` interface, added as a final pipeline step, gated by `risk_mode` and called only after the real-money decision point (§8.3). Same hosting, one more secret (trading credentials).

---

## 12. What Makes This "Statistical," Not Gambling

- **Backtestable**: every scanned market is logged, not just flagged ones — gives a true base rate, avoids survivorship bias.
- **Falsifiable**: if 100+ resolved bets show <50% win rate for a bias_type, that bucket's multiplier should reflect it (§9) — no hopium.
- **Bankroll discipline**: Kelly-based sizing, fraction tunable per risk_mode (§8), never unbounded.
- **Paper-first**: no real capital is risked until the calibration engine has enough data to be meaningful.
