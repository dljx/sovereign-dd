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


# ── earnings calendar ranking (Finnhub primary, Tiger date-only gap-filler) ────

def _passthrough_cached(key, ttl, fn, *a, **k):
    return fn(*a, **k)


def test_earnings_upcoming_finnhub_primary(monkeypatch):
    monkeypatch.setattr(dossier, "cached", _passthrough_cached)
    monkeypatch.setattr(broker_sync, "tiger_earnings_dates",
                        lambda *a: (_ for _ in ()).throw(AssertionError("Tiger called")))
    raw = {"earningsCalendar": [
        {"date": "2026-07-20", "hour": "amc", "epsEstimate": 1.2, "epsActual": None},
        {"date": "2026-04-20", "hour": "amc", "epsEstimate": 1.0, "epsActual": 1.1},  # reported
    ]}
    up = dossier._earnings_upcoming("AAPL", raw)
    assert len(up) == 1 and up[0]["date"] == "2026-07-20" and up[0]["eps_estimate"] == 1.2


def test_earnings_upcoming_tiger_fills_finnhub_gap(monkeypatch):
    monkeypatch.setattr(dossier, "cached", _passthrough_cached)
    monkeypatch.setattr(broker_sync, "tiger_earnings_dates",
                        lambda *a: {"ENVHF": "2026-07-11"})
    up = dossier._earnings_upcoming("ENVHF", {"earningsCalendar": []})
    assert up == [{"date": "2026-07-11", "hour": None, "eps_estimate": None,
                   "revenue_estimate": None, "quarter": None, "year": None,
                   "src": "tiger"}]


def test_earnings_upcoming_suffixed_skips_tiger(monkeypatch):
    monkeypatch.setattr(dossier, "cached", _passthrough_cached)
    monkeypatch.setattr(broker_sync, "tiger_earnings_dates",
                        lambda *a: (_ for _ in ()).throw(AssertionError("Tiger called")))
    assert dossier._earnings_upcoming("HPQ.V", {}) == []


def test_earnings_upcoming_tiger_failure_is_silent(monkeypatch):
    monkeypatch.setattr(dossier, "cached", _passthrough_cached)
    monkeypatch.setattr(broker_sync, "tiger_earnings_dates",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert dossier._earnings_upcoming("AAPL", None) == []


# ── cycle_type sector ranking (2026-07-12): GICS sector, NOT Finnhub industry ──
# cycle_type feeds scoring.cycle_position_adjust, and _cycle_type keys on GICS
# sector names. Finnhub's "Semiconductors" matches nothing → HYBRID, which
# under-scored every semiconductor. These lock the taxonomy the classifier expects.

def test_cycle_type_keys_on_gics_sector_names():
    # The exact yfinance/GICS strings the resolver passes through.
    assert dossier._cycle_type("Technology") == "SECULAR"          # semis land here
    assert dossier._cycle_type("Healthcare") == "SECULAR"
    assert dossier._cycle_type("Communication Services") == "SECULAR"
    assert dossier._cycle_type("Utilities") == "DEFENSIVE"
    assert dossier._cycle_type("Consumer Cyclical") == "CYCLICAL"
    assert dossier._cycle_type("Industrials") == "HYBRID"          # genuinely mixed
    # The regression: Finnhub's industry string is NOT a GICS sector → HYBRID.
    assert dossier._cycle_type("Semiconductors") == "HYBRID"


def test_sic_to_sector_fallback_maps_common_descriptions():
    m = dossier._sic_to_sector
    assert m("Semiconductors & Related Devices") == "Technology"
    assert m("Services-Prepackaged Software") == "Technology"
    assert m("Pharmaceutical Preparations") == "Healthcare"
    assert m("National Commercial Banks") == "Financial Services"   # \bbanks?\b
    assert m("Electric Services") == "Utilities"
    assert m("Crude Petroleum & Natural Gas") == "Energy"
    # "Investment" must not steal REITs for financials (real-estate rule is first).
    assert m("Real Estate Investment Trusts") == "Real Estate"


def test_sic_to_sector_returns_none_when_unmatched():
    assert dossier._sic_to_sector(None) is None
    assert dossier._sic_to_sector("") is None
    assert dossier._sic_to_sector("Blank Space Widgets") is None    # → HYBRID default


# ── _fwd_eps_cagr: long-horizon PEG denominator (2026-07-13) ──────────────────
# FMP analyst-estimates returns up to 5 future FYs on covered symbols; the
# FY+1→FY+2/3 EPS CAGR is the known-basis long-growth read (rows past FY+3 are
# analyst-thin — NVDA's FY2030 row sat BELOW FY2029 on a handful of estimates).


def _fy(eps):
    return {"epsAvg": eps}


def test_fwd_eps_cagr_two_year_horizon():
    # AAPL-shaped: 8.755 → 10.643 over 2 years = ~10.25% CAGR; FY+3/+4 ignored.
    fut = [_fy(8.755), _fy(9.643), _fy(10.643), _fy(11.631), _fy(12.82)]
    cagr, years = dossier._fwd_eps_cagr(fut)
    assert years == 2
    assert abs(cagr - ((10.643 / 8.755) ** 0.5 - 1)) < 1e-4


def test_fwd_eps_cagr_single_extra_year():
    cagr, years = dossier._fwd_eps_cagr([_fy(2.0), _fy(2.4)])
    assert years == 1 and abs(cagr - 0.2) < 1e-9


def test_fwd_eps_cagr_guards():
    assert dossier._fwd_eps_cagr([]) is None
    assert dossier._fwd_eps_cagr([_fy(5.0)]) is None                 # one row ≠ growth
    assert dossier._fwd_eps_cagr([_fy(-1.0), _fy(2.0)]) is None      # negative base
    assert dossier._fwd_eps_cagr([_fy(2.0), _fy(-1.0)]) is None      # negative target
    assert dossier._fwd_eps_cagr([_fy(2.0), _fy(None)]) is None      # missing target
    # Missing middle year: skips to the FY+2 row, years distance stays honest.
    cagr, years = dossier._fwd_eps_cagr([_fy(2.0), _fy(None), _fy(2.88)])
    assert years == 2 and abs(cagr - 0.2) < 1e-4
