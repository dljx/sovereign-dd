"""Source-ranked market data (2026-07-11): Tiger delayed quotes/adjusted bars
are the pipeline's PRIMARY source for plain US symbols, with Finnhub/yfinance
as fallback. Locks the ranking order and the silent-degradation contract
(Tiger env absent or failing → exactly the old behavior)."""

import pandas as pd

import broker_sync
import dossier


def _mk_hist(days=260, start=100.0):
    idx = pd.date_range("2025-07-01", periods=days, freq="D", tz="UTC")
    close = pd.Series([start + i * 0.1 for i in range(days)], index=idx)
    return pd.DataFrame({
        "Open": close - 0.5, "High": close + 1.0, "Low": close - 1.0,
        "Close": close, "Volume": pd.Series([1e6] * days, index=idx),
    })


# ── _quote ranking ──────────────────────────────────────────────────────────────

def test_quote_prefers_tiger_delayed(monkeypatch):
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes",
                        lambda syms: {"AAPL": {"price": 315.32, "prev_close": 316.22,
                                               "change_pct": -0.2846, "open": 314.72,
                                               "high": 316.91, "low": 312.17}})
    monkeypatch.setattr(dossier, "_fh",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Finnhub called")))
    q = dossier._quote("AAPL")
    assert q["_src"] == "tiger-delayed"
    assert q["c"] == 315.32 and q["pc"] == 316.22
    assert abs(q["d"] - (315.32 - 316.22)) < 1e-6
    assert q["dp"] == -0.2846 and q["h"] == 316.91


def test_quote_falls_back_to_finnhub_when_tiger_empty(monkeypatch):
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes", lambda syms: {})
    monkeypatch.setattr(dossier, "_fh", lambda path, params: {"c": 100.0, "dp": 1.5})
    q = dossier._quote("AAPL")
    assert q == {"c": 100.0, "dp": 1.5}


def test_quote_suffixed_ticker_never_calls_tiger(monkeypatch):
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes",
                        lambda syms: (_ for _ in ()).throw(AssertionError("Tiger called")))
    monkeypatch.setattr(dossier, "_fh", lambda path, params: {"c": 2.0})
    assert dossier._quote("HPQ.V") == {"c": 2.0}


def test_quote_tiger_exception_degrades_silently(monkeypatch):
    monkeypatch.setattr(broker_sync, "tiger_delay_quotes",
                        lambda syms: (_ for _ in ()).throw(RuntimeError("api down")))
    monkeypatch.setattr(dossier, "_fh", lambda path, params: {"c": 3.0})
    assert dossier._quote("AAPL") == {"c": 3.0}


# ── _technicals ranking ─────────────────────────────────────────────────────────

def test_technicals_uses_tiger_bars_first(monkeypatch):
    hist = _mk_hist()
    monkeypatch.setattr(broker_sync, "tiger_daily_bars", lambda t, days=365: hist)

    class _NoYf:
        def __init__(self, t):
            raise AssertionError("yfinance called despite Tiger bars")
    monkeypatch.setattr(dossier.yf, "Ticker", _NoYf)

    out = dossier._technicals("NVDA")
    assert out["price"] == round(float(hist["Close"].iloc[-1]), 2)
    assert "rsi_14" in out and "sma200" in out and "error" not in out


def test_technicals_falls_back_to_yfinance(monkeypatch):
    monkeypatch.setattr(broker_sync, "tiger_daily_bars", lambda t, days=365: None)
    hist = _mk_hist(start=50.0)

    class _FakeYf:
        def __init__(self, t):
            pass
        def history(self, period):
            return hist
    monkeypatch.setattr(dossier.yf, "Ticker", _FakeYf)

    out = dossier._technicals("HPQ.V")
    assert out["price"] == round(float(hist["Close"].iloc[-1]), 2)
    assert "error" not in out
