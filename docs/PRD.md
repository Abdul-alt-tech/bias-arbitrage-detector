# PRD — Bias Arbitrage Detector (UFC, v1.2)

> Companion to `source_of_truth_v1.2.md`. That doc is the technical contract (schema, formulas, prompts, architecture). This doc defines scope, requirements, and the build order. If the two ever disagree, the source-of-truth doc wins on technical specifics; this doc wins on priorities/sequencing.

---

## 1. Problem & Opportunity

Prediction markets (Kalshi, Polymarket) are populated partly by retail crowds that price sports outcomes based on narrative, recency, and name recognition rather than the more disciplined pricing sportsbooks already produce. Where a sportsbook-derived reference probability and a prediction-market price diverge by more than fees/slippage allow for, and an LLM judges that divergence to look like bias rather than new information, that gap is a candidate edge.

This isn't a "beat the market by predicting better" system — it's a "the sharp price already exists, bet the gap when the crowd hasn't caught up" system.

## 2. Goals

| Goal | Metric |
|---|---|
| Validate the bias-detection thesis | 300-500 resolved flagged bets logged |
| Win rate on flagged bets | 52-56% (target), with calibration tracking per bias_type |
| Zero infrastructure cost | $0/month hosting, alerting, dashboard |
| Always-on monitoring | Alerts arrive regardless of whether the user's laptop is on |
| Self-improving thresholds | Confidence calibration adjusts per bias_type once 20+ resolved samples exist per bucket |

## 3. Non-Goals (v1 Scope)

Explicitly **out of scope** for this build phase:

- **Real-money execution.** All bet sizing is paper-tracked (`bankroll.mode: "paper"`). See source-of-truth §8.2-8.3 for the conditions under which this changes.
- **Soccer / NBA / other sports.** Schema is designed to generalize (`reference_prob`, `reference_source`, generic `bias_inputs`), but only UFC adapters/prompts/data sources are built now.
- **Automated order placement.** `kalshi_executor.py` / `polymarket_executor.py` are referenced as a future extension point only.
- **World Cup logging.** Discussed as a free side-effect of building `data_collector.py` well, but not a deliverable — only pursue if it costs literally zero extra time once the collector exists.
- **Aggressive risk_mode by default.** Ships as a config option, defaults to conservative, only flipped (globally or per bias_type) once calibration data supports it.

## 4. User Stories

1. *As the operator*, I want the system to run on a schedule without my laptop being on, so I don't miss opportunities.
2. *As the operator*, I want an email when a market crosses my alert threshold, with enough detail to decide quickly whether to act manually.
3. *As the operator*, I want a dashboard I can check anytime that shows the full reasoning behind a flag, its payout potential, and links to verify the underlying claims myself.
4. *As the operator*, I want every scanned market logged — not just flagged ones — so the system can later tell me whether my thresholds are even set correctly.
5. *As the operator*, I want the system to tell me, over time, whether its own confidence scores are trustworthy, broken down by the type of bias it claims to detect.
6. *As the operator*, I want to switch between conservative and aggressive posture with a single config change, once I have evidence to justify it.

## 5. Functional Requirements

### 5.1 Exchange Adapters (`exchanges/`)
- FR-1.1: `base.py` defines a common interface: `list_upcoming_markets(sport, days_ahead)`, `get_price_snapshot(market_ref)`.
- FR-1.2: `kalshi.py` and `polymarket.py` each implement the interface, normalizing price units to 0-1 probability.
- FR-1.3: Config (`config.json`) specifies which platform(s) are active; both may run simultaneously, producing separate logged entries per platform for the same real-world event if both list it.
- FR-1.4: If a platform's 24h price-history endpoint is unavailable for a market, fall back to local `snapshots.jsonl` lookup (closest entry to "now - 24h" for that `market_id`). First sighting may log `prob_24h_ago: null`.

### 5.2 Data Collection (`data_collector.py`)
- FR-2.1: Pull upcoming UFC markets from active platform(s) within a configurable lookahead window (default: covers the next scheduled UFC event).
- FR-2.2: Pull `reference_prob` from TheOddsAPI (MMA odds).
- FR-2.3: Pull `bias_inputs` (last 3 results + method, days since last fight, weight class, news flags) per fighter from UFCStats/ESPN MMA — **data source TBD, see Open Questions**.
- FR-2.4: Write/append one schema-compliant record (source-of-truth §4) per market per platform to `snapshots.jsonl`.

### 5.3 Scoring (`scoring.py`)
- FR-3.1: Compute `raw_edge`, `edge_percent`, `payout_multiple` per source-of-truth §2.
- FR-3.2: Apply `edge_threshold` from `config.json` (risk_mode-dependent) to determine which markets proceed to LLM reasoning.

### 5.4 LLM Reasoning (`llm_reasoner.py`)
- FR-4.1: Use the exact prompt template in source-of-truth §6 — strict JSON output, no text outside the JSON object.
- FR-4.2: Only called for markets that pass FR-3.2 (expected: a handful per run).
- FR-4.3: Never pass `llm_output` fields back into the prompt (avoid feedback loops); never allow the LLM to compute `edge_percent`/`payout_multiple`.

### 5.5 Calibration (`calibrator.py`)
- FR-5.1: Maintain `calibration.json` with per-bias_type `multiplier`, `sample_size`, `actual_winrate` (source-of-truth §9).
- FR-5.2: A bias_type's multiplier remains `1.0` until `sample_size >= min_sample_size` (default 20).
- FR-5.3: Compute `calibrated_confidence = raw_confidence * multiplier`.
- FR-5.4: Recompute on every run where new resolutions exist since the last calibration.

### 5.6 Alerting (`alerter.py`)
- FR-6.1: Alert rule: `edge_percent > edge_threshold AND calibrated_confidence > confidence_cutoff` (both risk_mode-dependent).
- FR-6.2: On trigger, send an email (Gmail SMTP + app password) containing: matchup, edge_percent, bias_type, calibrated_confidence, payout_multiple, one-line reason, and a link to the dashboard entry.
- FR-6.3: Set `alert.triggered = true` and compute `suggested_bet_pct` / `suggested_bet_value_zmw` (Kelly fraction per risk_mode × paper bankroll) regardless of whether email send succeeds.

### 5.7 Resolution (`resolver.py`)
- FR-7.1: For any logged market where `start_time` has passed and `resolution.actual_result` is still null, fetch the actual fight result and compute paper `pnl_zmw`.
- FR-7.2: Resolved records feed `calibrator.py` (FR-5.4).

### 5.8 Dashboard (Streamlit)
- FR-8.1: Three views — Live Flags, History/Log, Calibration/Performance (source-of-truth §10).
- FR-8.2: Blue-dominant theme via Streamlit `[theme]` config; charts (Plotly) themed to match.
- FR-8.3: Live Flags view shows full reasoning text, payout math, suggested bet size, direct `market_url` link, and a generated info-search link per fighter.
- FR-8.4: Reads directly from the GitHub repo's `snapshots.jsonl` / `calibration.json`; no separate database.

### 5.9 Hosting & Scheduling
- FR-9.1: GitHub Actions `schedule` (cron) trigger runs the full pipeline (5.2 → 5.7) every 2-3 hours.
- FR-9.2: All credentials (TheOddsAPI, Kalshi, LLM, Gmail app password) stored as GitHub Actions encrypted secrets — never committed in code.
- FR-9.3: Workflow commits updated `snapshots.jsonl` / `calibration.json` back to the repo at the end of each run.

## 6. Non-Functional Requirements

- **NFR-1 (Cost):** $0/month at current scale. All chosen services (GitHub Actions, Streamlit Community Cloud, Gmail SMTP, free-tier LLM) must stay within free quotas given expected call volume (a handful of LLM calls and emails per UFC event per week).
- **NFR-2 (Reliability):** Scheduling does not need to-the-minute precision — UFC's weekly cadence tolerates multi-hour scheduling jitter.
- **NFR-3 (Security):** No API keys or trading credentials ever appear in logged data files or committed code.
- **NFR-4 (Portability):** Exchange-specific logic confined to `exchanges/`; sport-specific logic (bias_inputs shape, prompt wording) confined to clearly identified sections so soccer/NBA can be added later without touching scoring/calibration/alerting.
- **NFR-5 (Data integrity):** Every scanned market is logged, not only flagged ones (required for FR-5 and future threshold backtesting).

## 7. Data Model

See `source_of_truth_v1.2.md` §4 for the frozen JSON schema, §9 for `calibration.json`, and §8.1 for `config.json`. This PRD does not duplicate those definitions — any schema change should be made there first.

## 8. Phased Build Plan

| Phase | Deliverable | Exit Criteria |
|---|---|---|
| **0 — Setup & Verification** | Accounts/keys: TheOddsAPI, Kalshi (with API key), GitHub repo, Streamlit Cloud, Gmail app password. Verify: Kalshi UFC series ticker format, Polymarket UFC market availability, TheOddsAPI MMA field mapping. | All accounts created; at least one platform confirmed to have live UFC markets for the next event. |
| **1 — Adapters + Collector** | `exchanges/base.py`, `kalshi.py`, `polymarket.py`, `data_collector.py`. Manual runs only. | `snapshots.jsonl` populated with schema-valid records for an upcoming UFC card, on at least one platform. |
| **2 — Scoring + LLM** | `scoring.py`, `llm_reasoner.py`. Still manual runs. | `edge_percent`, `payout_multiple`, `bias_type`, `raw_confidence`, `reason` populated for relevant markets; LLM output is valid JSON 100% of the time across a test batch. |
| **3 — Automation + Alerting** | GitHub Actions workflow (FR-9), `alerter.py` (email). | A scheduled run completes end-to-end with the laptop off; a test alert email is received. |
| **4 — Dashboard** | Streamlit app, blue theme, Live Flags + History views. | Dashboard reflects latest committed data within one redeploy cycle. |
| **5 — Resolution + Calibration** | `resolver.py`, `calibrator.py`, Calibration view in dashboard. | First resolved bets appear with `pnl_zmw`; calibration multipliers remain 1.0 until sample-size floor is met (by design). |
| **6 — Evidence-Based Tuning** | Re-evaluate `risk_mode`, per-bias_type overrides, and the real-money decision (source-of-truth §8.3) — *only after* Phase 5 has produced 300-500 resolved bets. | Decision documented: stay paper, adjust risk_mode, or proceed to funding analysis. |

Phases 0-4 should be achievable with no live betting and no real money at risk. Phase 5 is where the system starts producing the data that everything else depends on — it simply takes calendar time (weekly UFC cards) to accumulate.

## 9. Risks & Open Questions

- **Kalshi UFC ticker/series format** — not yet verified; needed before FR-1.2/2.1 can be finalized for Kalshi.
- **Polymarket UFC market availability** — uncertain at time of writing; if absent, Kalshi becomes the sole platform for v1 (architecture supports this — FR-1.3).
- **`bias_inputs` data source for fighter news/camp flags (FR-2.3)** — UFCStats and ESPN MMA cover records/results well, but "camp changes" / "late opponent swap" type news may need a different source or manual curation initially. To be resolved during Phase 1.
- **TheOddsAPI MMA market structure** — verify exact field names/format for MMA moneyline odds before building FR-2.2.
- **GitHub Actions schedule drift** — acceptable per NFR-2, but worth confirming actual behavior (a few minutes to low hours of delay under load) doesn't conflict with event start times near fight night.
- **Calibration formula precision (FR-5.3)** — the `multiplier = actual_winrate / implied_winrate_from_raw_confidence` relationship in source-of-truth §9 is directional; exact formula to be finalized once real resolved data exists to test against.
