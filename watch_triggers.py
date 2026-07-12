"""Trigger engine — turns standing analysis into timely alerts (2026-07-12).

The platform ANALYZES well but nothing WATCHES: a fresh CONFIRM that dips into
an entry window, or a BUY candidate that runs up past its own fair value, was
silent until the next 4-hour scout cron happened to re-run it. This reads the
current Scout/Gems boards, prices them with Tiger delayed quotes, and fires a
Telegram alert when a rule trips — at most once per (ticker, rule) per cooldown
(dedup state in the eye's alerts:state:v1).

Display/alert only — never mutates a board, never trades. Runs on the daily
broker-sync cron (which already has the eye URL, secret, and Tiger creds).

Env: SOVEREIGN_EYE_URL, DD_UPLOAD_SECRET, TIGER_* (for live quotes),
TELEGRAM_*. WATCH_DRY_RUN=1 reports without sending or stamping.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests

# Entry window: a CONFIRM this far below its signal price is a cheaper re-entry.
_ENTRY_DISCOUNT = 0.07
# Only act on cards analyzed within this many days (a stale signal isn't news).
_MAX_AGE_DAYS = 14
# One alert per (ticker, rule) per this many days.
_COOLDOWN_DAYS = 14


def _base_secret() -> tuple[str, str] | None:
    base = os.getenv("SOVEREIGN_EYE_URL", "").rstrip("/")
    secret = os.getenv("DD_UPLOAD_SECRET", "")
    return (base, secret) if base and secret else None


def _get(path: str):
    cfg = _base_secret()
    if cfg is None:
        return None
    base, secret = cfg
    try:
        r = requests.get(f"{base}{path}",
                         headers={"Authorization": f"Bearer {secret}"}, timeout=20)
        return r.json() if r.ok else None
    except Exception as e:
        print(f"  [watch] GET {path} failed: {type(e).__name__}")
        return None


def _card_age_days(card: dict) -> float | None:
    ts = card.get("analyzed_at") or (card.get("verification") or {}).get("checked_at")
    try:
        d = datetime.fromisoformat(str(ts))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400
    except Exception:
        return None


def evaluate(cards: list, quotes: dict) -> list[dict]:
    """Pure rule evaluation → candidate alerts. `quotes` maps ticker→live price.
    Each alert: {ticker, rule, message, key}."""
    alerts = []
    for c in cards or []:
        ticker = str(c.get("ticker") or "").upper()
        verdict = str((c.get("verification") or {}).get("verdict") or "").upper()
        signal_px = c.get("price")
        live = quotes.get(ticker)
        if not ticker or live is None or not signal_px or signal_px <= 0:
            continue
        age = _card_age_days(c)
        if age is None or age > _MAX_AGE_DAYS:
            continue
        score = c.get("score")

        # Entry window — only on a clean CONFIRM (a DOWNGRADE is not a buy-me).
        if verdict == "CONFIRM":
            disc = (signal_px - live) / signal_px
            if disc >= _ENTRY_DISCOUNT:
                alerts.append({
                    "ticker": ticker, "rule": "entry_window",
                    "key": f"{ticker}|entry_window",
                    "message": (f"now {disc * 100:.0f}% below the signal price "
                                f"(${live:.2f} vs ${signal_px:.2f}), CONFIRM "
                                f"{score}/10 · {age:.0f}d old — entry window open"),
                })

        # Reached fair value — the bargain closed; stop chasing / consider trim.
        fv = c.get("fair_value_composite")
        if fv and fv > 0 and live >= fv:
            alerts.append({
                "ticker": ticker, "rule": "target_reached",
                "key": f"{ticker}|target_reached",
                "message": (f"reached its fair-value estimate "
                            f"(${live:.2f} ≥ ${fv:.2f}) — the margin of safety is gone"),
            })
    return alerts


def _fresh(alerts: list[dict], state: dict) -> list[dict]:
    """Drop alerts whose (ticker,rule) fired within the cooldown window."""
    now = datetime.now(timezone.utc)
    out = []
    for a in alerts:
        prev = (state or {}).get(a["key"])
        try:
            fired = datetime.fromisoformat(str(prev)) if prev else None
            if fired and fired.tzinfo is None:
                fired = fired.replace(tzinfo=timezone.utc)
        except Exception:
            fired = None
        if fired and (now - fired).total_seconds() < _COOLDOWN_DAYS * 86400:
            continue
        out.append(a)
    return out


def run() -> int:
    dry = os.getenv("WATCH_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    if _base_secret() is None:
        print("  [watch] SOVEREIGN_EYE_URL / DD_UPLOAD_SECRET not set — skipping")
        return 0

    cards = []
    for board in ("/api/dd/scouts", "/api/dd/gems"):
        data = _get(board)
        if isinstance(data, list):
            cards.extend(data)
    if not cards:
        print("  [watch] no board cards — nothing to watch")
        return 0

    tickers = sorted({str(c.get("ticker") or "").upper() for c in cards if c.get("ticker")})
    try:
        from broker_sync import tiger_delay_quotes, tiger_symbol_ok
        q = tiger_delay_quotes([t for t in tickers if tiger_symbol_ok(t)])
        quotes = {t: v["price"] for t, v in q.items() if v.get("price")}
    except Exception as e:
        print(f"  [watch] live quotes unavailable ({e}) — skipping")
        return 0
    if not quotes:
        print("  [watch] no live quotes resolved — skipping")
        return 0

    candidates = evaluate(cards, quotes)
    state = _get("/api/dd/alert-state") or {}
    fresh = _fresh(candidates, state)
    print(f"  [watch] {len(cards)} cards · {len(quotes)} priced · "
          f"{len(candidates)} rule hit(s) · {len(fresh)} fresh (post-dedup)")

    if not fresh:
        return 0
    if dry:
        for a in fresh:
            print(f"  [watch] DRY {a['ticker']} {a['rule']}: {a['message']}")
        return 0

    try:
        from notify import alert_trigger
        alert_trigger(fresh)
    except Exception as e:
        print(f"  [watch] telegram skipped ({e})")

    cfg = _base_secret()
    if cfg:
        base, secret = cfg
        try:
            requests.post(f"{base}/api/dd/alert-state",
                          headers={"Authorization": f"Bearer {secret}",
                                   "Content-Type": "application/json"},
                          json={"fired": [a["key"] for a in fresh]}, timeout=20)
        except Exception as e:
            print(f"  [watch] alert-state stamp failed ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(run())
