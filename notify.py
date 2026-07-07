"""Telegram notifications for sovereign-dd — routes to specific topics."""

import os
import time

import requests
from dotenv import load_dotenv

from text_utils import clip

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Topic (thread) IDs — send to the relevant channel
TOPIC_TRADE_ALERTS  = os.getenv("TELEGRAM_TOPIC_TRADE_ALERTS", "")
TOPIC_DEEP_DIVES    = os.getenv("TELEGRAM_TOPIC_DEEP_DIVES", "")
TOPIC_SCAN_RESULTS  = os.getenv("TELEGRAM_TOPIC_SCAN_RESULTS", "")
# Under-Review watchlist — confirmed-gate rejects. Falls back to Scan Results
# until a dedicated topic ID is set in TELEGRAM_TOPIC_WATCHLIST.
TOPIC_WATCHLIST     = os.getenv("TELEGRAM_TOPIC_WATCHLIST", "") or TOPIC_SCAN_RESULTS

GRADE_EMOJI = {
    "CONVICTION BUY": "🟢🟢🟢",
    "STRONG BUY":     "🟢🟢",
    "BUY":            "🟢",
    "HOLD":           "🟡",
    "SELL":           "🔴",
    "STRONG SELL":    "🔴🔴",
    "AVOID":          "🔴🔴🔴",
    # Hold-mode labels (ADD/HOLD/TRIM/EXIT) — mirror GRADE_COLORS in grading.py
    "ADD":            "🟢🟢",
    "TRIM":           "🔴",
    "EXIT":           "🔴🔴",
}

CONF_EMOJI = {"HIGH": "⭐⭐⭐", "MEDIUM": "⭐⭐", "LOW": "⭐"}


_TG_MAX = 4096

# Retry budget for a single sendMessage call. Backoff is a module constant so
# tests can zero it. 429 waits use Telegram's own retry_after, capped so a
# long flood-wait can't stall a CI run.
_SEND_ATTEMPTS = 3
_SEND_BACKOFF = (2.0, 5.0)
_RETRY_AFTER_CAP = 15.0


def _send(message: str, topic_id: str = "") -> bool:
    """Send a single message to the Telegram bot (caller must ensure len ≤ 4096).

    Retries transient failures (network errors, 5xx, 429) — a BUY alert lost to
    a blip is a missed trade. Other 4xx (e.g. malformed HTML) fail immediately
    since retrying can't fix the payload.
    """
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

    for attempt in range(_SEND_ATTEMPTS):
        wait = _SEND_BACKOFF[min(attempt, len(_SEND_BACKOFF) - 1)]
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload, timeout=15,
            )
            if r.ok:
                return True
            print(f"  [notify] Telegram error {r.status_code}: {r.text[:200]}")
            if r.status_code != 429 and r.status_code < 500:
                return False
            if r.status_code == 429:
                try:
                    wait = min(float(r.json()["parameters"]["retry_after"]), _RETRY_AFTER_CAP)
                except Exception:
                    pass
        except Exception as e:
            print(f"  [notify] Telegram request failed: {e}")
        if attempt < _SEND_ATTEMPTS - 1:
            time.sleep(wait)
    return False


def _split_send(message: str, topic_id: str = "") -> bool:
    """Send message, splitting into ≤4096-char chunks at newline boundaries."""
    if len(message) <= _TG_MAX:
        return _send(message, topic_id)

    parts: list[str] = []
    remaining = message
    while remaining:
        if len(remaining) <= _TG_MAX:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, _TG_MAX)
        if split_at <= 0:
            split_at = _TG_MAX
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    ok = True
    for i, part in enumerate(parts):
        if i > 0:
            part = "↩ <i>(continued)</i>\n" + part
        ok = _send(part, topic_id) and ok
    return ok


def alert_scoreboard_digest(sb: dict) -> bool:
    """Weekly signal-scoreboard summary → Scan Results topic.

    `sb` is the dict from signal_analysis.compute_scoreboard (also uploaded to
    the dashboard as dd:scoreboard) — hit rate and mean/median excess vs the
    benchmark per window, plus best/worst signals."""
    lines = [
        f"📊 <b>SIGNAL SCOREBOARD</b> — vs {sb.get('benchmark', 'VWRA.L')}",
        f"{sb.get('n_signals', 0)} signals logged · excess = signal return − benchmark",
    ]
    for w in sb.get("windows", []):
        o = w.get("overall")
        if not o:
            lines.append(f"\n<b>{w['weeks']}w</b>: nothing measurable yet "
                         f"({w.get('pending', 0)} pending)")
            continue
        small = " ⚠" if o["n"] < 10 else ""
        lines.append(
            f"\n<b>{w['weeks']}w</b> · n={o['n']} ({w.get('pending', 0)} pending){small}\n"
            f"hit {o['hit'] * 100:.0f}% · mean {o['mean'] * 100:+.1f}% "
            f"· median {o['median'] * 100:+.1f}%"
        )
        top, bottom = w.get("top") or [], w.get("bottom") or []
        if top and bottom:
            lines.append(f"best {top[0]['ticker']} {top[0]['excess'] * 100:+.1f}% · "
                         f"worst {bottom[0]['ticker']} {bottom[0]['excess'] * 100:+.1f}%")
    lines.append("\n<i>Small samples read as noise — tuning waits for the "
                 "pre-committed analysis.</i>")
    return _split_send("\n".join(lines), TOPIC_SCAN_RESULTS)


def alert_buy_signal(d: dict) -> bool:
    """Send a BUY signal alert for a scout discovery → Trade Alerts topic.

    v3 (2026-07-07): a red-team DOWNGRADE ("real concerns, not fatal") is no
    longer suppressed identically to a VETO — it surfaces here too, visibly
    tagged with the prosecutor's score and strongest bear point, so risk-reward
    gets graded rather than binary-gated. See docs/ADAPTATION_PROTOCOL.md."""
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

    filters   = d.get("matched_filters", [])
    path      = d.get("path", "")

    lens_tag   = f" · <code>{lens}</code>" if lens else ""
    path_tag   = f" [PATH {path}]" if path else ""
    banger_tag = "\n🔥 <b>BANGER</b> — " + clip(banger.get("reason"), 150) if isinstance(banger, dict) and banger.get("is_banger") else ""
    penalty    = score_rat or dissent
    filter_line = f"<b>Filters met:</b> {' · '.join(filters)}\n" if filters else ""

    # Confirmation-gate verdict line — a clean CONFIRM shows the prosecutor's score;
    # a fail-open (UNVERIFIED) BUY is now visibly flagged instead of silently passed;
    # a DOWNGRADE surfaces too (v3) with its score + strongest bear point front and
    # center, since it's graded evidence, not a reason to suppress the signal.
    v = d.get("verification", {}) or {}
    vverdict = str(v.get("verdict", "")).upper()
    vscore   = v.get("verification_score")
    bear     = v.get("strongest_bear_point", "")
    _vs = f" · {vscore:.1f}/10" if isinstance(vscore, (int, float)) else ""
    if vverdict == "CONFIRM":
        verify_line = f"🛡️ <b>Verified:</b> prosecutor CONFIRM{_vs}\n"
        header = f"{emoji} <b>BUY SIGNAL — {d['ticker']}</b>{path_tag}{lens_tag}"
    elif vverdict == "DOWNGRADE":
        verify_line = (f"⚠️ <b>Red-team DOWNGRADE{_vs}:</b> <i>{clip(bear, 350)}</i>\n"
                       if bear else f"⚠️ <b>Red-team DOWNGRADE{_vs}</b>\n")
        header = f"⚠️ <b>FLAGGED BUY — {d['ticker']}</b>{path_tag}{lens_tag} <i>(red-team DOWNGRADE)</i>"
    elif vverdict == "UNVERIFIED":
        verify_line = "🛡️ <i>Unverified — red-team unavailable, auto-passed</i>\n"
        header = f"{emoji} <b>BUY SIGNAL — {d['ticker']}</b>{path_tag}{lens_tag}"
    else:
        verify_line = ""
        header = f"{emoji} <b>BUY SIGNAL — {d['ticker']}</b>{path_tag}{lens_tag}"

    msg = (
        f"{header}\n"
        f"<b>Score:</b> {d['score']:.1f}/10 · {d['grade']} · {conf}\n"
        + verify_line
        + filter_line
        + (f"<b>Gemma flagged:</b> <i>{clip(rationale, 350)}</i>\n" if rationale else "")
        + (f"\n<b>Catalyst:</b> <i>{clip(catalyst, 400)}</i>\n" if catalyst else "")
        + (f"<b>Asymmetry:</b> {asymmetry}\n" if asymmetry else "")
        + (f"<b>R/R:</b> {d['rr']:.1f}:1 ({d.get('risk','?')} risk)\n" if d.get("rr") is not None else "")
        + (f"<b>Cycle:</b> {cycle_pos.get('regime','')} — {cycle_pos.get('phase','')}\n" if isinstance(cycle_pos, dict) and cycle_pos.get("phase") else "")
        + f"\n<b>Bull case:</b> <i>{clip(d.get('thesis'), 600)}</i>\n\n"
        + (f"<b>Why not higher:</b> <i>{clip(penalty, 450)}</i>\n\n" if penalty else "")
        + f"<b>Key factor:</b> {clip(d.get('key_swing_factor') or '—', 300)}\n"
        + (f"<b>Position:</b> {pos.get('range','?')} ({clip(pos.get('reasoning'), 200)})\n" if isinstance(pos, dict) and pos.get("range") else "")
        + banger_tag
        + f"\n⏰ {d['analyzed_at']}"
    )
    return _split_send(msg, TOPIC_TRADE_ALERTS)


def alert_watchlist(d: dict) -> bool:
    """Send an 'Under Review' alert for a BUY that crossed the threshold but
    failed the confirmation gate → Watchlist topic (falls back to Scan Results).

    Leads with the reason it was held back (Stage-1 quality reasons or the
    red-team's strongest bear point) so it reads as a near-miss, not a signal."""
    v = d.get("verification", {}) or {}
    verdict  = v.get("verdict", "?")
    vscore   = v.get("verification_score")
    bear     = v.get("strongest_bear_point", "")
    reasons  = v.get("reasons", []) or []
    findings = v.get("falsification_findings", []) or []

    if v.get("stage") == 1:
        held = "Failed internal quality checks — " + "; ".join(reasons)
    else:
        held = bear or (reasons[0] if reasons else "Flagged by red-team review")

    conf        = CONF_EMOJI.get(d.get("confidence", ""), "")
    lens        = d.get("scout_lens", "")
    lens_tag    = f" · <code>{lens}</code>" if lens else ""
    vscore_line = f" · verify {vscore:.1f}/10" if isinstance(vscore, (int, float)) else ""

    findings_block = ""
    if findings:
        findings_block = ("\n<b>Disconfirming findings:</b>\n"
                          + "\n".join(f"• <i>{clip(str(f), 200)}</i>" for f in findings[:4]) + "\n")

    msg = (
        f"⚠️ <b>UNDER REVIEW — {d['ticker']}</b>{lens_tag}\n"
        f"<b>Score:</b> {d['score']:.1f}/10 · {d.get('grade','?')} · {conf} · <b>{verdict}</b>{vscore_line}\n"
        f"<i>Crossed the BUY line but failed the confirmation gate — not a confirmed signal.</i>\n\n"
        f"<b>Why held back:</b> {clip(held, 500)}\n"
        + findings_block
        + (f"<b>R/R:</b> {d['rr']:.1f}:1 ({d.get('risk','?')} risk)\n" if d.get("rr") is not None else "")
        + f"\n<b>Bull case (unconfirmed):</b> <i>{clip(d.get('thesis'), 450)}</i>\n"
        + f"\n⏰ {d.get('analyzed_at','')}"
    )
    return _split_send(msg, TOPIC_WATCHLIST)


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
    thesis = clip(result.get("majority_thesis"), 700)

    catalyst = result.get("catalyst", "")
    banger = result.get("banger", {})
    banger_line = f"\n🔥 <b>BANGER</b> — {clip(banger.get('reason'), 250)}" if isinstance(banger, dict) and banger.get("is_banger") else ""

    rrd = result.get("risk_reward") or {}
    rr_line = (
        f"<b>R/R:</b> {rrd['rr_ratio']:.1f}:1 ({rrd.get('risk_tier','?')} risk)\n"
        if rrd.get("applied") and rrd.get("rr_ratio") is not None else ""
    )

    msg = (
        f"{emoji} <b>SOVEREIGN DD — {ticker}</b>\n"
        f"<b>Score:</b> {score:.2f}/10 · {grade} · {cconf}\n"
        + rr_line + "\n"
        f"<pre>{agents_block}</pre>\n\n"
        + (f"<b>Catalyst:</b> <i>{clip(catalyst, 400)}</i>\n\n" if catalyst else "")
        + f"<i>{thesis}</i>"
        + banger_line
    )
    return _split_send(msg, TOPIC_DEEP_DIVES)


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
    return _split_send("\n".join(lines), TOPIC_SCAN_RESULTS)


def alert_scout_summary(discoveries: list[dict], title: str = "SOVEREIGN SCOUT") -> bool:
    """Send scout/gems run summary → Scan Results topic.

    An empty scan still sends — a heartbeat that distinguishes "nothing
    qualified today" from a silently stalled pipeline. It does NOT repeat
    investment advice (changed 2026-07-07, Daryl's request): VWRA is his own
    baseline allocation, not something this system needs to recommend every
    time it has nothing to say. Silence on a pick simply means no pick.
    """
    if not discoveries:
        msg = (
            f"🔍 <b>{title}</b>\n\n"
            "No new opportunities cleared the bar this scan."
        )
    else:
        lines = [f"🔍 <b>{title} — {len(discoveries)} signal(s) found</b>\n"]
        for d in discoveries:
            emoji = GRADE_EMOJI.get(d["grade"], "")
            lines.append(f"{emoji} <b>{d['ticker']}</b>  {d['score']:.1f}/10")
        lines.append("\nFull reports sent to Deep Dives ↑")
        msg = "\n".join(lines)
    return _split_send(msg, TOPIC_SCAN_RESULTS)
