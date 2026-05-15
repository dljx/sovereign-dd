"""Telegram notifications for sovereign-dd — routes to specific topics."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Topic (thread) IDs — send to the relevant channel
TOPIC_TRADE_ALERTS  = os.getenv("TELEGRAM_TOPIC_TRADE_ALERTS", "")
TOPIC_DEEP_DIVES    = os.getenv("TELEGRAM_TOPIC_DEEP_DIVES", "")
TOPIC_SCAN_RESULTS  = os.getenv("TELEGRAM_TOPIC_SCAN_RESULTS", "")

GRADE_EMOJI = {
    "STRONG BUY":  "🟢🟢",
    "BUY":         "🟢",
    "HOLD":        "🟡",
    "SELL":        "🔴",
    "STRONG SELL": "🔴🔴",
}

CONF_EMOJI = {"HIGH": "⭐⭐⭐", "MEDIUM": "⭐⭐", "LOW": "⭐"}


def _send(message: str, topic_id: str = "") -> bool:
    """Send a message to the Telegram bot, optionally in a specific topic thread."""
    if not BOT_TOKEN or not CHAT_ID:
        print("  [notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping")
        return False
    payload: dict = {
        "chat_id":    CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    if topic_id:
        payload["message_thread_id"] = int(topic_id)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=15,
        )
        if not r.ok:
            print(f"  [notify] Telegram error {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"  [notify] Telegram request failed: {e}")
        return False


def alert_buy_signal(d: dict) -> bool:
    """Send a BUY signal alert for a scout discovery → Trade Alerts topic."""
    emoji = GRADE_EMOJI.get(d["grade"], "")
    conf  = CONF_EMOJI.get(d.get("confidence", ""), "")
    lens      = d.get("scout_lens", "")
    rationale = d.get("gemma_rationale", "")
    score_rat = d.get("score_rationale", "")
    dissent   = d.get("dissent", "")
    lens_tag  = f" · <code>{lens}</code>" if lens else ""
    # Prefer score_rationale (explicit penalty explanation); fall back to dissent
    penalty = score_rat or dissent
    msg = (
        f"{emoji} <b>BUY SIGNAL — {d['ticker']}</b>{lens_tag}\n"
        f"<b>Score:</b> {d['score']:.1f}/10 · {d['grade']} · {conf}\n"
        + (f"<b>Gemma flagged:</b> <i>{rationale[:180]}</i>\n" if rationale else "")
        + f"\n<b>Bull case:</b> <i>{d['thesis'][:300]}</i>\n\n"
        + (f"<b>Why not higher:</b> <i>{penalty[:250]}</i>\n\n" if penalty else "")
        + f"<b>Key factor:</b> {d.get('key_swing_factor', '—')[:150]}\n"
        f"⏰ {d['analyzed_at']}"
    )
    return _send(msg, TOPIC_TRADE_ALERTS)


def alert_dd_result(result: dict) -> bool:
    """Send a single DD result to the Deep Dives topic."""
    ticker = result.get("ticker", "?")
    score  = result.get("consensus_score", 0)
    grade  = result.get("consensus_grade", "HOLD")
    conf   = result.get("confidence", "")
    emoji  = GRADE_EMOJI.get(grade, "")
    cconf  = CONF_EMOJI.get(conf, "")

    agent_lines = []
    r1 = result.get("agent_r1_scores", {})
    rf = result.get("agent_final_scores", {})
    for agent, s1 in r1.items():
        sf    = rf.get(agent, s1)
        delta = sf - s1
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
        agent_lines.append(f"  {agent:<14} {s1:.1f} {arrow} {sf:.1f}")

    agents_block = "\n".join(agent_lines)
    thesis = result.get("majority_thesis", "")[:350]

    msg = (
        f"{emoji} <b>SOVEREIGN DD — {ticker}</b>\n"
        f"<b>Score:</b> {score:.2f}/10 · {grade} · {cconf}\n\n"
        f"<pre>{agents_block}</pre>\n\n"
        f"<i>{thesis}</i>"
    )
    return _send(msg, TOPIC_DEEP_DIVES)


def alert_portfolio_summary(results: list[dict]) -> bool:
    """Send a pre-market portfolio scan summary → Scan Results topic."""
    sorted_results = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
    lines = ["📊 <b>SOVEREIGN DD — Pre-Market Portfolio Scan</b>\n"]
    for r in sorted_results:
        emoji = GRADE_EMOJI.get(r.get("grade", ""), "")
        score = r.get("score", 0)
        grade = r.get("grade", "?")
        lines.append(f"{emoji} <b>{r['ticker']:<6}</b>  {score:.1f}/10  {grade}")
    lines.append("\n🕐 Analysis complete — check Deep Dives for full reports")
    return _send("\n".join(lines), TOPIC_SCAN_RESULTS)


def alert_scout_summary(discoveries: list[dict]) -> bool:
    """Send scout run summary → Scan Results topic."""
    if not discoveries:
        msg = "🔍 <b>SOVEREIGN SCOUT</b>\n\nNo BUY signals found in today's scan."
    else:
        lines = [f"🔍 <b>SOVEREIGN SCOUT — {len(discoveries)} signal(s) found</b>\n"]
        for d in discoveries:
            emoji = GRADE_EMOJI.get(d["grade"], "")
            lines.append(f"{emoji} <b>{d['ticker']}</b>  {d['score']:.1f}/10")
        lines.append("\nFull reports sent to Deep Dives ↑")
        msg = "\n".join(lines)
    return _send(msg, TOPIC_SCAN_RESULTS)
