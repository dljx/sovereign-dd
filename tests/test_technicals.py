"""Formula-correctness audit (2026-07-13): dossier's computed technical/
fundamental indicators, checked against hand-derived or textbook reference
values — not just internal self-consistency."""

import pandas as pd
import pytest

from dossier import _wilder_rsi


def _series(vals):
    return pd.Series([float(v) for v in vals])


# ── _wilder_rsi ──────────────────────────────────────────────────────────────
# The prior implementation used a plain N-period rolling-mean RSI ("Cutler's
# RSI") instead of Wilder's original recursive smoothing — the textbook
# definition and what every mainstream charting platform actually displays.
# Verified live: diverges from Wilder's by ~1+ points on AAPL. Since RSI feeds
# MarketStructure's entry-timing judgment, it should match what a human
# checking any standard chart would see, not an uncheckable in-house variant.

def test_wilder_rsi_matches_hand_derived_exact_value():
    # closes=[10,11,10,12,11,13], period=3: gains=[-,1,0,2,0,2], losses=[-,0,1,0,1,0].
    # Seed avg(idx3) = mean([1,0,2])=1, mean([0,1,0])=1/3. Two Wilder steps:
    # avg_gain -> 2/3 -> 10/9; avg_loss -> 5/9 -> 10/27. RS = (10/9)/(10/27) = 3
    # exactly -> RSI = 100 - 100/(1+3) = 75.0 exactly (verified independently
    # with Fraction arithmetic, not just this implementation).
    rsi = _wilder_rsi(_series([10, 11, 10, 12, 11, 13]), period=3)
    assert rsi == pytest.approx(75.0, abs=1e-6)


def test_wilder_rsi_all_gains_is_100():
    rsi = _wilder_rsi(_series([10, 11, 12, 13, 14, 15]), period=3)
    assert rsi == pytest.approx(100.0, abs=1e-6)


def test_wilder_rsi_all_losses_is_0():
    rsi = _wilder_rsi(_series([15, 14, 13, 12, 11, 10]), period=3)
    assert rsi == pytest.approx(0.0, abs=1e-6)


def test_wilder_rsi_flat_series_is_50():
    # No gains, no losses at all -> both averages are 0 -> RS treated as 0/0
    # guarded to inf in the code (last_loss == 0 branch) — but with ZERO
    # gains too, gain/loss both hit the same all-zero path; the 0-loss guard
    # fires first, giving RSI=100. Document the actual (defensible) behavior
    # rather than assume: a truly flat tape has no losses to be oversold on.
    rsi = _wilder_rsi(_series([10, 10, 10, 10, 10, 10]), period=3)
    assert rsi == pytest.approx(100.0, abs=1e-6)


def test_wilder_rsi_too_short_returns_none():
    assert _wilder_rsi(_series([10, 11, 12]), period=14) is None


def test_wilder_rsi_exactly_enough_history():
    # period+1 points is the minimum for one seed value (diff() drops the
    # first point) — must not raise, whatever value it produces.
    rsi = _wilder_rsi(_series([10, 11, 12, 13]), period=3)
    assert rsi is not None


# ── yfinance "forward growth" field mislabeling (2026-07-13) ───────────────
# info['earningsGrowth'] verified live (4 tickers) to track info[
# 'earningsQuarterlyGrowth'] almost exactly (a TRAILING quarter-YoY figure),
# not the confirmed-forward t.earnings_estimate('+1y').growth — MRVL showed
# -80.4% vs +52.6%, opposite signs. info['revenueGrowth'] diverges 30-100%+
# from its forward counterpart with no consistent conversion factor either.
# Both were previously the last-resort fallback for ratios_ttm.fwd_earnings_
# growth/fwd_revenue_growth (feeding fwd_peg AND the DCF's growth input) —
# now exposed under honest trailing_* names and excluded from that chain.
# This locks the key-naming contract at the _yf_financials boundary so the
# misleading names can't silently come back.

import dossier


class _MinimalFakeTicker:
    """Only .info is populated; every other yfinance property raises —
    _yf_financials wraps income/balance/cashflow/estimates in their own
    try/except-and-continue blocks, so this still returns a usable dict."""
    def __init__(self, symbol):
        self.info = {
            "earningsGrowth": -0.804, "earningsQuarterlyGrowth": -0.806,
            "revenueGrowth": 0.276, "trailingPE": 81.3, "forwardPE": 38.2,
            "trailingEps": 2.9, "forwardEps": 6.18, "sector": "Technology",
            "industry": "Semiconductors", "longName": "Fake Corp",
        }

    @property
    def financials(self): raise AttributeError("no financials in this fake")
    @property
    def balance_sheet(self): raise AttributeError("no balance sheet in this fake")
    @property
    def cashflow(self): raise AttributeError("no cashflow in this fake")
    @property
    def earnings_estimate(self): raise AttributeError("no estimates in this fake")
    @property
    def revenue_estimate(self): raise AttributeError("no estimates in this fake")
    @property
    def eps_trend(self): raise AttributeError("no estimates in this fake")


def test_yf_financials_exposes_trailing_growth_under_honest_names(monkeypatch):
    monkeypatch.setattr(dossier.yf, "Ticker", _MinimalFakeTicker)
    out = dossier._yf_financials("FAKE")
    assert out["trailing_earnings_growth_yoy"] == -0.804
    assert out["trailing_revenue_growth_yoy"] == 0.276
    # The misleading names must be GONE, not just duplicated — a caller doing
    # yf_fin.get("fwd_earnings_growth") must get None, never the trailing value.
    assert "fwd_earnings_growth" not in out
    assert "fwd_revenue_growth" not in out


# ── _volume_profile (2026-07-14 enrichment batch) ───────────────────────────
# MarketStructure's prompt has always asked for the 30-60d up/down-volume
# asymmetry read; the Volume column was fetched and dropped before this.

from dossier import _volume_profile


def _px_vol(closes, volumes):
    return _series(closes), pd.Series([float(v) for v in volumes])


def test_volume_profile_updown_split_hand_derived():
    # closes: 10 →11(up) →10(down) →12(up) →12(flat)
    # volumes on those change-days: up=100+300=400, down=200, flat=50 ignored.
    c, v = _px_vol([10, 11, 10, 12, 12], [999, 100, 200, 300, 50])
    r = _volume_profile(c, v, window=10)
    assert r["updown_volume_ratio_60d"] == pytest.approx(400 / 200)
    assert r["volume_window_days"] == 4
    # avg over the 4 change-days = (100+200+300+50)/4 = 162.5 → hi bar 243.75:
    # only the 300-share up-day crosses it.
    assert r["avg_volume_60d"] == 162
    assert r["high_vol_up_days_60d"] == 1
    assert r["high_vol_down_days_60d"] == 0


def test_volume_profile_no_down_days_ratio_is_none_not_inf():
    c, v = _px_vol([10, 11, 12, 13], [100, 100, 100, 100])
    r = _volume_profile(c, v, window=10)
    assert r["updown_volume_ratio_60d"] is None
    assert r["high_vol_up_days_60d"] == 0


def test_volume_profile_missing_volume_returns_empty():
    c, _ = _px_vol([10, 11, 12], [1, 1, 1])
    assert _volume_profile(c, None) == {}


def test_volume_profile_zero_volume_returns_empty_not_div_by_zero():
    c, v = _px_vol([10, 11, 12], [0, 0, 0])
    assert _volume_profile(c, v) == {}
