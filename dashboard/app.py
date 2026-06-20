"""
dashboard/app.py
----------------
Streamlit dashboard for the Bias Arbitrage Detector.
Blue-dominant theme (configured in dashboard/.streamlit/config.toml).

Four sections in Live Flags:
  - Risk mode badge (header)
  - Active-mode alerts (triggered == True, drives real emails)
  - Aggressive-only candidates (would_alert_aggressive but not
    would_alert_conservative) — informational, never emailed

Three tabs total:
  1. Live Flags    — currently triggered alerts + aggressive-only candidates
  2. History/Log   — all scanned markets, filterable
  3. Calibration   — win rate by bias_type, multiplier history, P&L

Reads directly from snapshots.jsonl and calibration.json in the
repo root. Streamlit Community Cloud auto-redeploys on new commits.

Run locally: streamlit run dashboard/app.py
"""

import json
import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_SAMPLE_SIZE = 20  # Must match calibrator.py's MIN_SAMPLE_SIZE

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Bias Arbitrage Detector",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

SNAPSHOTS_PATH = os.path.join(os.path.dirname(__file__), "..", "snapshots.jsonl")
CALIBRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "calibration.json")


@st.cache_data(ttl=300)  # Refresh every 5 minutes
def load_snapshots() -> list:
    path = os.path.abspath(SNAPSHOTS_PATH)
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return records


@st.cache_data(ttl=300)
def load_calibration() -> dict:
    path = os.path.abspath(CALIBRATION_PATH)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def records_to_df(records: list) -> pd.DataFrame:
    """Flatten records into a DataFrame for filtering/display."""
    rows = []
    for r in records:
        md = r.get("market_data", {})
        fund = r.get("fundamentals", {})
        score = r.get("scoring_output", {})
        llm = r.get("llm_output", {})
        alert = r.get("alert", {})
        res = r.get("resolution", {})
        fa = r.get("bias_inputs", {}).get("fighter_a", {})
        fb = r.get("bias_inputs", {}).get("fighter_b", {})

        rows.append({
            "market_id": r.get("market_id", ""),
            "platform": r.get("platform", "").upper(),
            "question": r.get("question", ""),
            "event_name": r.get("event_name", ""),
            "start_time": r.get("start_time", ""),
            "timestamp": r.get("timestamp", ""),
            "market_url": r.get("market_url", ""),
            "market_prob": md.get("market_prob"),
            "prob_24h_ago": md.get("prob_24h_ago"),
            "price_change_24h": md.get("price_change_24h"),
            "reference_prob": fund.get("reference_prob"),
            "raw_edge": score.get("raw_edge"),
            "edge_percent": score.get("edge_percent"),
            "edge_percent_conservative": score.get("edge_percent_conservative"),
            "edge_percent_aggressive": score.get("edge_percent_aggressive"),
            "would_alert_conservative": score.get("would_alert_conservative"),
            "would_alert_aggressive": score.get("would_alert_aggressive"),
            "payout_multiple": score.get("payout_multiple"),
            "llm_called": score.get("llm_called", False),
            "bias_type": llm.get("bias_type"),
            "raw_confidence": llm.get("raw_confidence"),
            "calibrated_confidence": llm.get("calibrated_confidence"),
            "reason": llm.get("reason"),
            "triggered": alert.get("triggered", False),
            "risk_mode": alert.get("risk_mode"),
            "bet_pct": alert.get("suggested_bet_pct"),
            "bet_zmw": alert.get("suggested_bet_value_zmw"),
            "other_mode": alert.get("other_mode"),
            "other_mode_bet_pct": alert.get("other_mode_suggested_bet_pct"),
            "other_mode_bet_zmw": alert.get("other_mode_suggested_bet_value_zmw"),
            "actual_result": res.get("actual_result"),
            "pnl_zmw": res.get("pnl_zmw"),
            "fighter_a": fa.get("name", ""),
            "fighter_b": fb.get("name", ""),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Colour helpers (Plotly)
# ---------------------------------------------------------------------------

BLUE_PALETTE = {
    "bg": "#0a1628",
    "panel": "#0f1f3d",
    "accent": "#4fc3f7",
    "accent2": "#1565c0",
    "text": "#e0e8f0",
    "muted": "#90a4b7",
    "win": "#4caf50",
    "loss": "#ef5350",
    "aggressive": "#ff9800"
}


def plotly_layout(title=""):
    return dict(
        title=title,
        paper_bgcolor=BLUE_PALETTE["panel"],
        plot_bgcolor=BLUE_PALETTE["bg"],
        font=dict(color=BLUE_PALETTE["text"]),
        title_font=dict(color=BLUE_PALETTE["accent"]),
        xaxis=dict(gridcolor="#1e3a6e", color=BLUE_PALETTE["muted"]),
        yaxis=dict(gridcolor="#1e3a6e", color=BLUE_PALETTE["muted"]),
        margin=dict(l=20, r=20, t=40, b=20)
    )


# ---------------------------------------------------------------------------
# Load data (must happen before header, since header reads risk_mode)
# ---------------------------------------------------------------------------

records = load_snapshots()
cal = load_calibration()
df = records_to_df(records) if records else pd.DataFrame()

# ---------------------------------------------------------------------------
# Header (with risk mode badge)
# ---------------------------------------------------------------------------

current_risk_mode = "conservative"
if records:
    current_risk_mode = records[-1].get("alert", {}).get("risk_mode", "conservative")

risk_color = BLUE_PALETTE["accent"] if current_risk_mode == "conservative" else BLUE_PALETTE["aggressive"]
risk_label = current_risk_mode.upper()

st.markdown(f"""
<div style="background: linear-gradient(135deg, #0a1628 0%, #0d2045 100%);
     padding: 24px; border-radius: 12px; margin-bottom: 24px;
     border: 1px solid #1e3a6e;">
  <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
    <div>
      <h1 style="color: #4fc3f7; margin: 0; font-size: 28px;">
        🔥 Bias Arbitrage Detector
      </h1>
      <p style="color: #90a4b7; margin: 8px 0 0 0;">
        UFC Fight Market Edge Scanner — Paper Trading Mode
      </p>
    </div>
    <div style="background: {risk_color}22; border: 1px solid {risk_color};
         border-radius: 8px; padding: 8px 16px; text-align: center;">
      <span style="color: #90a4b7; font-size: 10px; text-transform: uppercase;
            letter-spacing: 1px; display: block;">Active Risk Mode</span>
      <span style="color: {risk_color}; font-size: 16px; font-weight: bold;">
        {risk_label}
      </span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Top-level metrics
# ---------------------------------------------------------------------------

global_stats = cal.get("global_stats", {})
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric("Markets Scanned", global_stats.get("total_scanned", len(records)))
with col2:
    st.metric("Alerts Triggered", global_stats.get("total_flagged", 0))
with col3:
    st.metric("Resolved Bets", global_stats.get("total_resolved", 0))
with col4:
    wr = global_stats.get("overall_winrate")
    st.metric("Win Rate", f"{wr:.1%}" if wr else "—")
with col5:
    pnl = global_stats.get("total_pnl_zmw", 0)
    delta_color = "normal" if pnl >= 0 else "inverse"
    st.metric("Paper P&L", f"{'+' if pnl >= 0 else ''}{pnl:.2f} ZMW",
              delta=f"{'+' if pnl >= 0 else ''}{pnl:.2f}", delta_color=delta_color)

st.markdown("---")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3 = st.tabs(["🔥 Live Flags", "📋 History / Log", "📊 Calibration"])


# ============================================================
# TAB 1: LIVE FLAGS
# ============================================================
with tab1:
    if df.empty:
        st.info("No data yet. Run data_collector.py to start scanning.")
    else:
        flagged = df[df["triggered"] == True].copy()

        if flagged.empty:
            st.info("No active alerts right now under the active risk mode. "
                    "The scanner is running — check back after the next UFC event is priced up.")
        else:
            # Sort by calibrated_confidence desc
            flagged = flagged.sort_values("calibrated_confidence", ascending=False)

            for _, row in flagged.iterrows():
                with st.container():
                    st.markdown(f"""
                    <div style="background: #0f1f3d; border-radius: 10px; padding: 20px;
                         margin-bottom: 16px; border: 1px solid #1e3a6e;
                         border-left: 4px solid #4fc3f7;">
                      <div style="display: flex; justify-content: space-between; align-items: start;">
                        <div>
                          <span style="background: #1565c0; color: white; padding: 2px 10px;
                                border-radius: 12px; font-size: 11px; font-weight: bold;">
                            {row.get('platform', '')}
                          </span>
                          <span style="background: #0d47a1; color: #4fc3f7; padding: 2px 10px;
                                border-radius: 12px; font-size: 11px; margin-left: 8px;
                                text-transform: capitalize;">
                            {row.get('bias_type', '')}
                          </span>
                        </div>
                        <span style="color: #90a4b7; font-size: 12px;">{row.get('event_name', '')}</span>
                      </div>
                      <h3 style="color: #e0e8f0; margin: 12px 0 8px 0; font-size: 18px;">
                        {row.get('question', '')}
                      </h3>
                    </div>
                    """, unsafe_allow_html=True)

                    c1, c2, c3, c4, c5 = st.columns(5)
                    mp = row.get("market_prob") or 0
                    rp = row.get("reference_prob") or 0
                    ep = row.get("edge_percent") or 0
                    pm = row.get("payout_multiple") or 0
                    cc = row.get("calibrated_confidence") or 0

                    c1.metric("Market Price", f"{mp:.0%}")
                    c2.metric("Reference Price", f"{rp:.0%}")
                    c3.metric("Net Edge", f"{ep:.1f}%",
                              delta=f"{ep:.1f}%", delta_color="normal")
                    c4.metric("Payout Multiple", f"{pm:.2f}x")
                    c5.metric("Confidence (calibrated)", f"{cc:.0%}")

                    # Kelly bet
                    bet_zmw = row.get("bet_zmw") or 0
                    bet_pct = row.get("bet_pct") or 0
                    st.markdown(f"""
                    <div style="background: #0a1628; border-radius: 6px; padding: 12px;
                         margin: 8px 0; border: 1px solid #1e3a6e;">
                      <span style="color: #90a4b7; font-size: 12px;">KELLY BET (PAPER)</span><br>
                      <span style="color: #4fc3f7; font-size: 20px; font-weight: bold;">
                        {bet_pct:.1f}% of bankroll = {bet_zmw:.2f} ZMW
                      </span>
                    </div>
                    """, unsafe_allow_html=True)

                    # LLM reasoning
                    reason = row.get("reason", "")
                    if reason:
                        st.markdown(f"""
                        <div style="background: #0a1628; border-left: 3px solid #4fc3f7;
                             padding: 12px; border-radius: 4px; margin: 8px 0;">
                          <span style="color: #90a4b7; font-size: 11px; text-transform: uppercase;
                                letter-spacing: 1px;">Why this is a bias</span><br>
                          <span style="color: #e0e8f0;">{reason}</span>
                        </div>
                        """, unsafe_allow_html=True)

                    # Links
                    market_url = row.get("market_url", "")
                    fa = row.get("fighter_a", "")
                    fb = row.get("fighter_b", "")
                    search_a = f"https://www.google.com/search?q={fa.replace(' ', '+')}+UFC+news"
                    search_b = f"https://www.google.com/search?q={fb.replace(' ', '+')}+UFC+news"

                    lc1, lc2, lc3 = st.columns(3)
                    if market_url:
                        lc1.markdown(f"[🎯 Place Bet → {row.get('platform', '')}]({market_url})")
                    lc2.markdown(f"[🔍 {fa} news]({search_a})")
                    lc3.markdown(f"[🔍 {fb} news]({search_b})")

                    st.markdown("---")

        # ----------------------------------------------------------------
        # Aggressive-only candidates section
        # Markets that would alert under aggressive mode but NOT under
        # conservative — informational only, never emailed regardless
        # of which mode is currently active.
        # ----------------------------------------------------------------
        agg_only = df[
            (df["would_alert_aggressive"] == True) &
            (df["would_alert_conservative"] == False)
        ].copy()

        if not agg_only.empty:
            st.markdown("&nbsp;")
            st.markdown(f"""
            <div style="background: {BLUE_PALETTE['aggressive']}11;
                 border: 1px solid {BLUE_PALETTE['aggressive']};
                 border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;">
              <span style="color: {BLUE_PALETTE['aggressive']}; font-weight: bold;">
                ⚡ Aggressive-Only Candidates
              </span>
              <span style="color: #90a4b7; font-size: 13px;">
                — would alert under aggressive mode, but not under conservative.
                Never emailed. For visibility only.
              </span>
            </div>
            """, unsafe_allow_html=True)

            agg_only = agg_only.sort_values("calibrated_confidence", ascending=False)

            for _, row in agg_only.iterrows():
                with st.container():
                    ep_agg = row.get("edge_percent_aggressive") or 0
                    cc = row.get("calibrated_confidence") or 0
                    other_bet_zmw = row.get("other_mode_bet_zmw") or row.get("bet_zmw") or 0
                    other_bet_pct = row.get("other_mode_bet_pct") or row.get("bet_pct") or 0

                    st.markdown(f"""
                    <div style="background: #0f1f3d; border-radius: 10px; padding: 16px;
                         margin-bottom: 12px; border: 1px solid #1e3a6e;
                         border-left: 4px solid {BLUE_PALETTE['aggressive']};">
                      <div style="display: flex; justify-content: space-between; align-items: start;">
                        <div>
                          <span style="background: {BLUE_PALETTE['aggressive']}33; color: {BLUE_PALETTE['aggressive']};
                                padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: bold;">
                            {row.get('platform', '')}
                          </span>
                          <span style="background: #0d47a1; color: #4fc3f7; padding: 2px 10px;
                                border-radius: 12px; font-size: 11px; margin-left: 8px;
                                text-transform: capitalize;">
                            {row.get('bias_type', '')}
                          </span>
                        </div>
                        <span style="color: #90a4b7; font-size: 12px;">{row.get('event_name', '')}</span>
                      </div>
                      <h4 style="color: #e0e8f0; margin: 10px 0 6px 0; font-size: 15px;">
                        {row.get('question', '')}
                      </h4>
                      <span style="color: #90a4b7; font-size: 12px;">
                        Edge (aggressive): <b style="color: {BLUE_PALETTE['aggressive']};">{ep_agg:.1f}%</b>
                        &nbsp;|&nbsp; Confidence: <b>{cc:.0%}</b>
                        &nbsp;|&nbsp; Would-be Kelly bet: <b>{bet_pct:.1f}% = {other_bet_zmw:.2f} ZMW</b>
                      </span>
                    </div>
                    """, unsafe_allow_html=True)


# ============================================================
# TAB 2: HISTORY / LOG
# ============================================================
with tab2:
    if df.empty:
        st.info("No data yet.")
    else:
        # Filters
        fc1, fc2, fc3 = st.columns(3)

        with fc1:
            bias_options = ["All"] + sorted(df["bias_type"].dropna().unique().tolist())
            bias_filter = st.selectbox("Bias Type", bias_options)

        with fc2:
            status_options = ["All", "Triggered", "Not Triggered", "Aggressive-Only", "Resolved"]
            status_filter = st.selectbox("Status", status_options)

        with fc3:
            platform_options = ["All"] + sorted(df["platform"].dropna().unique().tolist())
            platform_filter = st.selectbox("Platform", platform_options)

        filtered = df.copy()
        if bias_filter != "All":
            filtered = filtered[filtered["bias_type"] == bias_filter]
        if status_filter == "Triggered":
            filtered = filtered[filtered["triggered"] == True]
        elif status_filter == "Not Triggered":
            filtered = filtered[filtered["triggered"] == False]
        elif status_filter == "Aggressive-Only":
            filtered = filtered[
                (filtered["would_alert_aggressive"] == True) &
                (filtered["would_alert_conservative"] == False)
            ]
        elif status_filter == "Resolved":
            filtered = filtered[filtered["actual_result"].notna()]
        if platform_filter != "All":
            filtered = filtered[filtered["platform"] == platform_filter]

        st.markdown(f"**{len(filtered)} records**")

        display_cols = [
            "timestamp", "platform", "question", "market_prob",
            "reference_prob", "edge_percent_conservative", "edge_percent_aggressive",
            "payout_multiple", "bias_type", "calibrated_confidence",
            "would_alert_conservative", "would_alert_aggressive", "triggered",
            "actual_result", "pnl_zmw"
        ]
        available = [c for c in display_cols if c in filtered.columns]
        st.dataframe(
            filtered[available].sort_values("timestamp", ascending=False),
            use_container_width=True,
            height=500
        )


# ============================================================
# TAB 3: CALIBRATION / PERFORMANCE
# ============================================================
with tab3:
    if not cal:
        st.info("No calibration data yet. Needs resolved bets to populate.")
    else:
        # Win rate by bias type
        bt_data = cal.get("bias_type_multipliers", {})
        bt_rows = []
        for bt, vals in bt_data.items():
            if vals.get("sample_size", 0) > 0:
                bt_rows.append({
                    "Bias Type": bt.replace("_", " ").title(),
                    "Sample Size": vals["sample_size"],
                    "Win Rate": vals.get("actual_winrate"),
                    "Multiplier": vals.get("multiplier"),
                    "Calibrating": "✓" if vals["sample_size"] >= MIN_SAMPLE_SIZE else f"⏳ {vals['sample_size']}/20"
                })

        if bt_rows:
            bt_df = pd.DataFrame(bt_rows)

            st.subheader("Win Rate by Bias Type")
            fig_bt = go.Figure()
            for _, row in bt_df.iterrows():
                wr = row["Win Rate"] or 0
                color = BLUE_PALETTE["win"] if wr >= 0.52 else (
                    BLUE_PALETTE["loss"] if wr < 0.50 else BLUE_PALETTE["accent"]
                )
                fig_bt.add_trace(go.Bar(
                    name=row["Bias Type"],
                    x=[row["Bias Type"]],
                    y=[wr * 100],
                    marker_color=color,
                    text=f"{wr:.1%}",
                    textposition="auto"
                ))
            fig_bt.add_hline(y=52, line_dash="dash",
                             line_color="#4fc3f7",
                             annotation_text="52% target")
            fig_bt.add_hline(y=50, line_dash="dot",
                             line_color="#ef5350",
                             annotation_text="break-even")
            fig_bt.update_layout(
                **plotly_layout("Win Rate by Bias Type (%)"),
                showlegend=False,
                yaxis_range=[40, 70]
            )
            st.plotly_chart(fig_bt, use_container_width=True)

            st.dataframe(bt_df, use_container_width=True)
        else:
            st.info("No resolved bets by bias type yet.")

        # Confidence buckets
        cb_data = cal.get("confidence_buckets", {})
        if cb_data:
            st.subheader("Win Rate by Confidence Bucket")
            cb_rows = [
                {
                    "Confidence Range": f"{k}%",
                    "Total": v["total"],
                    "Wins": v["wins"],
                    "Win Rate": v.get("winrate")
                }
                for k, v in sorted(cb_data.items())
                if v["total"] > 0
            ]
            cb_df = pd.DataFrame(cb_rows)

            fig_cb = px.bar(
                cb_df, x="Confidence Range", y=[r["Win Rate"] or 0 for r in cb_rows],
                color_discrete_sequence=[BLUE_PALETTE["accent"]]
            )
            fig_cb.update_layout(**plotly_layout("Actual Win Rate vs Confidence Level (%)"))
            st.plotly_chart(fig_cb, use_container_width=True)

        # P&L over time
        if not df.empty:
            resolved_df = df[df["pnl_zmw"].notna()].sort_values("timestamp")
            if not resolved_df.empty:
                st.subheader("Cumulative Paper P&L (ZMW)")
                resolved_df["cumulative_pnl"] = resolved_df["pnl_zmw"].cumsum()

                fig_pnl = go.Figure()
                fig_pnl.add_trace(go.Scatter(
                    x=resolved_df["timestamp"],
                    y=resolved_df["cumulative_pnl"],
                    fill="tozeroy",
                    line=dict(color=BLUE_PALETTE["accent"], width=2),
                    fillcolor="rgba(79,195,247,0.1)"
                ))
                fig_pnl.add_hline(y=0, line_dash="dot", line_color=BLUE_PALETTE["loss"])
                fig_pnl.update_layout(**plotly_layout("Cumulative Paper P&L Over Time (ZMW)"))
                st.plotly_chart(fig_pnl, use_container_width=True)

        # Last updated
        last_updated = cal.get("last_updated")
        if last_updated:
            st.caption(f"Calibration last updated: {last_updated}")
