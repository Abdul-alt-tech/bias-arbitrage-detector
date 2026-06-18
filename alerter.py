"""
alerter.py
----------
Phase 3 module. Reads snapshots.jsonl, identifies records that
cross the alert threshold (edge_percent > threshold AND
calibrated_confidence > confidence_cutoff), sends an email alert
via Gmail SMTP, and marks alert.triggered = True in the record.

Email includes:
- Matchup + event
- Edge%, bias_type, calibrated confidence, payout_multiple
- Suggested Kelly bet size
- LLM reasoning
- Direct link to market (market_url)
- Link to dashboard

Run manually: python alerter.py
Called by GitHub Actions scan.yml after llm_reasoner.py.
"""

import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone


def load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Alert threshold check
# ---------------------------------------------------------------------------

def should_alert(record: dict, config: dict) -> bool:
    """
    Returns True if this record meets the alert criteria.
    """
    risk_mode = config.get("risk_mode", "conservative")
    edge_threshold = config["edge_threshold_pct"][risk_mode]
    confidence_cutoff = config["confidence_cutoff"][risk_mode]

    scoring = record.get("scoring_output", {})
    llm = record.get("llm_output", {})
    alert = record.get("alert", {})

    # Already alerted
    if alert.get("triggered"):
        return False

    # Must have been scored and LLM called
    if not scoring.get("llm_called") or scoring.get("skipped"):
        return False

    edge_pct = scoring.get("edge_percent", 0) or 0
    calibrated_conf = llm.get("calibrated_confidence", 0) or 0
    bias_type = llm.get("bias_type", "none")

    # Must have a real bias detected
    if bias_type == "none":
        return False

    return edge_pct > 0 and calibrated_conf >= confidence_cutoff


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def build_email_html(record: dict, config: dict) -> tuple:
    """
    Returns (subject, html_body) for the alert email.
    """
    risk_mode = config.get("risk_mode", "conservative")
    dashboard_url = config.get("email", {}).get("dashboard_url", "#")

    question = record.get("question", "Unknown market")
    event = record.get("event_name", "UFC Event")
    platform = record.get("platform", "").upper()
    market_url = record.get("market_url", "#")

    market_prob = record["market_data"].get("market_prob", 0)
    reference_prob = record["fundamentals"].get("reference_prob", 0)

    scoring = record["scoring_output"]
    llm = record["llm_output"]
    alert = record["alert"]

    edge_pct = scoring.get("edge_percent", 0)
    payout = scoring.get("payout_multiple", 0)
    bias_type = llm.get("bias_type", "unknown")
    raw_conf = llm.get("raw_confidence", 0)
    cal_conf = llm.get("calibrated_confidence", 0)
    reason = llm.get("reason", "")
    bet_zmw = alert.get("suggested_bet_value_zmw", 0)
    bet_pct = alert.get("suggested_bet_pct", 0)

    subject = f"🔥 BIAS ALERT — {question[:60]} | Edge: {edge_pct:.1f}% | Conf: {int(cal_conf*100)}%"

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; background: #0a1628; color: #e0e8f0; padding: 24px;">
      <div style="max-width: 600px; margin: 0 auto; background: #0f1f3d; border-radius: 12px; padding: 24px; border: 1px solid #1e3a6e;">

        <h2 style="color: #4fc3f7; margin-top: 0;">🔥 Bias Alert — {platform}</h2>

        <div style="background: #0a1628; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
          <p style="margin: 4px 0; font-size: 18px; font-weight: bold;">{question}</p>
          <p style="margin: 4px 0; color: #90a4b7;">{event}</p>
        </div>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
          <tr>
            <td style="padding: 8px; color: #90a4b7;">Market Price</td>
            <td style="padding: 8px; font-weight: bold; color: #fff;">{int(market_prob*100)}%</td>
            <td style="padding: 8px; color: #90a4b7;">Reference Price</td>
            <td style="padding: 8px; font-weight: bold; color: #fff;">{int(reference_prob*100)}%</td>
          </tr>
          <tr>
            <td style="padding: 8px; color: #90a4b7;">Net Edge</td>
            <td style="padding: 8px; font-weight: bold; color: #4fc3f7;">{edge_pct:.1f}%</td>
            <td style="padding: 8px; color: #90a4b7;">Payout Multiple</td>
            <td style="padding: 8px; font-weight: bold; color: #4fc3f7;">{payout:.2f}x</td>
          </tr>
          <tr>
            <td style="padding: 8px; color: #90a4b7;">Bias Type</td>
            <td style="padding: 8px; font-weight: bold; color: #fff; text-transform: capitalize;">{bias_type}</td>
            <td style="padding: 8px; color: #90a4b7;">Confidence</td>
            <td style="padding: 8px; font-weight: bold; color: #fff;">{int(raw_conf*100)}% → <span style="color: #4fc3f7;">{int(cal_conf*100)}% calibrated</span></td>
          </tr>
          <tr>
            <td style="padding: 8px; color: #90a4b7;">Risk Mode</td>
            <td style="padding: 8px; font-weight: bold; color: #fff; text-transform: capitalize;">{risk_mode}</td>
            <td style="padding: 8px; color: #90a4b7;">Kelly Bet</td>
            <td style="padding: 8px; font-weight: bold; color: #4fc3f7;">{bet_pct:.1f}% | {bet_zmw} ZMW (paper)</td>
          </tr>
        </table>

        <div style="background: #0a1628; border-left: 3px solid #4fc3f7; padding: 12px; border-radius: 4px; margin-bottom: 16px;">
          <p style="margin: 0; color: #90a4b7; font-size: 12px; text-transform: uppercase; letter-spacing: 1px;">Why</p>
          <p style="margin: 8px 0 0 0;">{reason}</p>
        </div>

        <div style="display: flex; gap: 12px; margin-top: 16px;">
          <a href="{market_url}" style="background: #1565c0; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold;">
            View Market →
          </a>
          <a href="{dashboard_url}" style="background: #0d47a1; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold;">
            Open Dashboard →
          </a>
        </div>

        <p style="margin-top: 20px; font-size: 11px; color: #4a6580;">
          ⚠️ Paper trading mode. This is not financial advice. All bet sizes are simulated.
          Bias Arbitrage Detector v1.2 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
        </p>
      </div>
    </body>
    </html>
    """

    return subject, html


def send_email(subject: str, html_body: str, config: dict) -> bool:
    """
    Send alert email via Gmail SMTP.
    Returns True on success.
    """
    email_cfg = config.get("email", {})
    sender = email_cfg.get("sender", os.environ.get("EMAIL_SENDER", ""))
    app_password = email_cfg.get("app_password", os.environ.get("EMAIL_APP_PASSWORD", ""))
    recipient = email_cfg.get("recipient", sender)

    if not sender or not app_password:
        print("  [Alerter] Email not configured. Set sender + app_password in config.json")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, app_password)
            server.sendmail(sender, recipient, msg.as_string())

        print(f"  [Alerter] Email sent to {recipient}")
        return True

    except smtplib.SMTPException as e:
        print(f"  [Alerter] SMTP error: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    config = load_config()
    risk_mode = config.get("risk_mode", "conservative")

    if not os.path.exists("snapshots.jsonl"):
        print("[Alerter] No snapshots.jsonl found.")
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
                    print(f"[Alerter] Warning: Skipping malformed JSON at line {line_num}: {e}")
                    continue
    except Exception as e:
        print(f"[Alerter] Error reading snapshots.jsonl: {e}")
        return

    if not records:
        print("[Alerter] No valid records found in snapshots.jsonl")
        return

    alerted = 0
    checked = 0

    for i, record in enumerate(records):
        checked += 1
        if not should_alert(record, config):
            continue

        print(f"  [Alert] {record['market_id']} — {record.get('question', '')[:60]}")

        subject, html = build_email_html(record, config)
        sent = send_email(subject, html, config)

        # Mark as triggered regardless of email success
        # (so we don't re-alert on next run)
        records[i]["alert"]["triggered"] = True
        records[i]["alert"]["triggered_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records[i]["alert"]["email_sent"] = sent
        alerted += 1

    # Rewrite snapshots.jsonl
    try:
        with open("snapshots.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    except Exception as e:
        print(f"[Alerter] Error writing snapshots.jsonl: {e}")
        return

    print(f"\n[Alerter] Done. Checked: {checked} | Alerted: {alerted}")


if __name__ == "__main__":
    run()
