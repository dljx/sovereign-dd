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
    "CONVICTION BUY": "🟢🟢🟢",
    "STRONG BUY":     "🟢🟢",
    "BUY":            "🟢",
    "HOLD":           "🟡",
    "SELL":           "🔴",
    "STRONG SELL":    "🔴🔴",
    "AVOID":          "🔴🔴🔴",
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
        try:
            payload["message_thread_id"] = int(topic_id)
        except ValueError:
            print(f"  [notify] Invalid topic_id '{topic_id}' — sending to main chat")
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
    catalyst  = d.get("catalyst", "")
    asymmetry = d.get("asymmetry_ratio", "")
    banger    = d.get("banger", {})
    pos       = d.get("position_guidance", {})
    cycle_pos = d.get("cycle_position", {})

    lens_tag  = f" · <code>{lens}</code>" if lens else ""
    banger_tag = "\n🔥 <b>BANGER</b> — " + banger.get("reason","")[:150] if isinstance(banger, dict) and banger.get("is_banger") else ""
    penalty = score_rat or dissent

    msg = (
        f"{emoji} <b>BUY SIGNAL — {d['ticker']}</b>{lens_tag}\n"
        f"<b>Score:</b> {d['score']:.1f}/10 · {d['grade']} · {conf}\n"
        + (f"<b>Gemma flagged:</b> <i>{rationale[:180]}</i>\n" if rationale else "")
        + (f"\n<b>Catalyst:</b> <i>{catalyst[:200]}</i>\n" if catalyst else "")
        + (f"<b>Asymmetry:</b> {asymmetry}\n" if asymmetry else "")
        + (f"<b>Cycle:</b> {cycle_pos.get('regime','')} — {cycle_pos.get('phase','')}\n" if isinstance(cycle_pos, dict) and cycle_pos.get("phase") else "")
        + f"\n<b>Bull case:</b> <i>{d['thesis'][:300]}</i>\n\n"
        + (f"<b>Why not higher:</b> <i>{penalty[:250]}</i>\n\n" if penalty else "")
        + f"<b>Key factor:</b> {d.get('key_swing_factor', '—')[:150]}\n"
        + (f"<b>Position:</b> {pos.get('range','?')} ({pos.get('reasoning','')[:100]})\n" if isinstance(pos, dict) and pos.get("range") else "")
        + banger_tag
        + f"\n⏰ {d['analyzed_at']}"
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

    catalyst = result.get("catalyst", "")
    banger = result.get("banger", {})
    banger_line = f"\n🔥 <b>BANGER</b> — {banger.get('reason','')[:120]}" if isinstance(banger, dict) and banger.get("is_banger") else ""

    msg = (
        f"{emoji} <b>SOVEREIGN DD — {ticker}</b>\n"
        f"<b>Score:</b> {score:.2f}/10 · {grade} · {cconf}\n\n"
        f"<pre>{agents_block}</pre>\n\n"
        + (f"<b>Catalyst:</b> <i>{catalyst[:200]}</i>\n\n" if catalyst else "")
        + f"<i>{thesis}</i>"
        + banger_line
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


def alert_gems_signal(d: dict, pillar_scores: dict | None = None) -> bool:
    """Send a BUY signal alert for a gems discovery → Trade Alerts topic."""
    emoji  = GRADE_EMOJI.get(d.get("grade", ""), "")
    conf   = CONF_EMOJI.get(d.get("confidence", ""), "")
    score  = d.get("score", 0)
    grade  = d.get("grade", "?")
    ticker = d.get("ticker", "?")
    composite  = d.get("gems_composite_score", 0)
    rationale  = d.get("gems_pillar_rationale", "") or d.get("gemma_rationale", "")
    catalyst   = d.get("catalyst", "")
    asymmetry  = d.get("asymmetry_ratio", "")
    thesis     = d.get("thesis", "")
    key_swing  = d.get("key_swing_factor", "—")
    pos        = d.get("position_guidance", {})
    banger     = d.get("banger", {})
    cycle_pos  = d.get("cycle_position", {})

    # Pillar score bar (compact, single line)
    pillar_line = ""
    if pillar_scores and isinstance(pillar_scores, dict):
        fp  = pillar_scores.get("financial_physics", 0)
        mp  = pillar_scores.get("moat_proxy", 0)
        tmp = pillar_scores.get("temporal", 0)
        mgm = pillar_scores.get("management", 0)
        chk = pillar_scores.get("chokepoint_proxy", 0)
        pillar_line = (
            f"\n💎 <b>Pillars:</b> "
            f"Fin:{fp:.1f} Moat:{mp:.1f} Tempo:{tmp:.1f} "
            f"Mgmt:{mgm:.1f} Choke:{chk:.1f} → <b>{composite:.1f}/10</b>"
        )

    banger_tag = (
        "\n🔥 <b>BANGER</b> — " + banger.get("reason", "")[:150]
        if isinstance(banger, dict) and banger.get("is_banger") else ""
    )

    msg = (
        f"{emoji} <b>GEMS SIGNAL — {ticker}</b> · <code>gems</code>\n"
        f"<b>Score:</b> {score:.1f}/10 · {grade} · {conf}"
        + pillar_line
        + (f"\n\n<b>Why flagged:</b> <i>{rationale[:200]}</i>" if rationale else "")
        + (f"\n<b>Catalyst:</b> <i>{catalyst[:200]}</i>" if catalyst else "")
        + (f"\n<b>Asymmetry:</b> {asymmetry}" if asymmetry else "")
        + (f"\n<b>Cycle:</b> {cycle_pos.get('regime','')} — {cycle_pos.get('phase','')}" if isinstance(cycle_pos, dict) and cycle_pos.get("phase") else "")
        + f"\n\n<b>Bull case:</b> <i>{thesis[:300]}</i>"
        + (f"\n<b>Key factor:</b> {key_swing[:150]}" if key_swing and key_swing != "—" else "")
        + (f"\n<b>Position:</b> {pos.get('range','?')} ({pos.get('reasoning','')[:100]})" if isinstance(pos, dict) and pos.get("range") else "")
        + banger_tag
        + f"\n⏰ {d.get('analyzed_at', '')}"
    )
    return _send(msg, TOPIC_TRADE_ALERTS)


def alert_gems_summary(discoveries: list[dict]) -> bool:
    """Send gems run summary → Scan Results topic."""
    if not discoveries:
        msg = "💎 <b>SOVEREIGN GEMS</b>\n\nNo BUY signals found in today's scan."
    else:
        lines = [f"💎 <b>SOVEREIGN GEMS — {len(discoveries)} signal(s) found</b>\n"]
        for d in sorted(discoveries, key=lambda x: x.get("score", 0), reverse=True):
            emoji = GRADE_EMOJI.get(d.get("grade", ""), "")
            composite = d.get("gems_composite_score", 0)
            lines.append(
                f"{emoji} <b>{d.get('ticker', '?')}</b>  "
                f"{d.get('score', 0):.1f}/10  "
                f"(pillar: {composite:.1f})"
            )
        lines.append("\nFull reports sent to Deep Dives ↑")
        msg = "\n".join(lines)
    return _send(msg, TOPIC_SCAN_RESULTS)
