"""Trigger engine — rule evaluation + dedup (2026-07-12). No network/LLM."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify
import watch_triggers as wt


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _card(ticker, price, verdict="CONFIRM", age=3, fv=None, score=7.5):
    c = {"ticker": ticker, "price": price, "score": score,
         "analyzed_at": _iso(age), "verification": {"verdict": verdict}}
    if fv is not None:
        c["fair_value_composite"] = fv
    return c


def test_entry_window_only_on_confirm_and_discount():
    cards = [
        _card("NVDA", 100.0),                          # signal 100
        _card("AMD", 100.0, verdict="DOWNGRADE"),      # downgrade → never entry
        _card("MU", 100.0),                            # not cheap enough
    ]
    quotes = {"NVDA": 92.0, "AMD": 80.0, "MU": 98.0}   # NVDA −8%, AMD −20%, MU −2%
    alerts = wt.evaluate(cards, quotes)
    keys = {a["key"] for a in alerts}
    assert "NVDA|entry_window" in keys
    assert "AMD|entry_window" not in keys              # downgrade excluded
    assert "MU|entry_window" not in keys               # −2% < 7% threshold


def test_stale_card_ignored():
    cards = [_card("NVDA", 100.0, age=30)]
    assert wt.evaluate(cards, {"NVDA": 80.0}) == []


def test_target_reached_when_live_at_or_above_fv():
    cards = [_card("DLO", 14.0, fv=13.5), _card("PAY", 20.0, fv=25.0)]
    quotes = {"DLO": 14.2, "PAY": 21.0}
    keys = {a["key"] for a in wt.evaluate(cards, quotes)}
    assert "DLO|target_reached" in keys                # 14.2 ≥ 13.5
    assert "PAY|target_reached" not in keys            # 21 < 25


def test_no_quote_or_bad_price_skipped():
    cards = [_card("NVDA", 100.0), _card("ZERO", 0.0)]
    assert wt.evaluate(cards, {}) == []                # no quotes
    assert wt.evaluate([_card("ZERO", 0.0)], {"ZERO": 5.0}) == []


def test_dedup_respects_cooldown():
    alerts = [{"ticker": "NVDA", "rule": "entry_window", "key": "NVDA|entry_window", "message": "x"}]
    recent = {"NVDA|entry_window": _iso(2)}            # fired 2d ago (<14d)
    assert wt._fresh(alerts, recent) == []
    old = {"NVDA|entry_window": _iso(20)}              # fired 20d ago (>14d)
    assert len(wt._fresh(alerts, old)) == 1
    assert len(wt._fresh(alerts, {})) == 1             # never fired


# ── gather_quotes: Tiger primary, Yahoo fallback (2026-07-13 coverage fix) ──
# Suffixed listings (.V/.TO) are never Tiger-safe and used to be silently
# unwatched; a Tiger outage used to skip the whole run instead of degrading.


def test_gather_quotes_yahoo_covers_suffixed_tickers(monkeypatch):
    import broker_sync
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes",
                        lambda syms: {t: {"price": 100.0} for t in syms})
    monkeypatch.setattr(wt, "_yahoo_price",
                        lambda s: 0.55 if s == "HPQ.V" else None)
    q = wt.gather_quotes(["NVDA", "HPQ.V"])
    assert q == {"NVDA": 100.0, "HPQ.V": 0.55}   # .V priced via Yahoo, not dropped


def test_gather_quotes_tiger_outage_degrades_to_yahoo(monkeypatch):
    import broker_sync
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes",
                        lambda syms: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(wt, "_yahoo_price", lambda s: 42.0)
    assert wt.gather_quotes(["NVDA", "AMD"]) == {"NVDA": 42.0, "AMD": 42.0}


def test_gather_quotes_unpriceable_ticker_absent(monkeypatch):
    import broker_sync
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes", lambda syms: {})
    monkeypatch.setattr(wt, "_yahoo_price", lambda s: None)
    assert wt.gather_quotes(["GHOST"]) == {}


def test_alert_trigger_formats_and_escapes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send", lambda msg, topic="": sent.append((msg, topic)) or True)
    notify.alert_trigger([{"ticker": "NVDA", "rule": "entry_window",
                           "message": "now 8% below signal (<20x fwd P/E) & cheap"}])
    msg, topic = sent[0]
    assert topic == notify.TOPIC_TRADE_ALERTS
    assert "WATCH TRIGGER" in msg and "NVDA" in msg
    assert "&lt;20x" in msg and "&amp; cheap" in msg   # escaped
    assert notify.alert_trigger([]) is False
