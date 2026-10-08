"""v7 (2026-10-08): period-honest return/size inputs.

ROIC was NOPAT/(equity+debt) from the latest ANNUAL statement only — up to ~14
months stale. Live MU: FY ending 2025-08 gave 11.16% (below its 17% WACC, the
swing factor of both A/B debates) while the last four real quarters give ~44%.
revenue_ttm came from info['totalRevenue'] (updated off the earnings press
release) while net_income_ttm / fcf are quarterly-statement sums (updated off
the 10-Q) — MU's revenue ran a quarter ahead of its NI/FCF in the same dossier.
"""
import pandas as pd
import pytest

from dossier import _resolve_roic, _ttm_revenue_from_quarterly, _ttm_roic_from_quarterly


class _FakeTicker:
    def __init__(self, qis=None, qbs=None):
        self._qis, self._qbs = qis, qbs

    @property
    def quarterly_income_stmt(self):
        return self._qis

    @property
    def quarterly_balance_sheet(self):
        return self._qbs


def _frame(rows: dict, n_cols=4):
    cols = pd.date_range("2026-05-31", periods=n_cols, freq="-3ME")
    return pd.DataFrame({k: (v + [None] * n_cols)[:n_cols] for k, v in rows.items()}, index=cols).T


# ── _ttm_revenue_from_quarterly ─────────────────────────────────────────────

def test_ttm_revenue_sums_last_4_quarters():
    t = _FakeTicker(qis=_frame({"Total Revenue": [41.0, 24.0, 14.0, 11.0, 9.0]}, n_cols=5))
    assert _ttm_revenue_from_quarterly(t) == pytest.approx(90.0)


def test_ttm_revenue_falls_back_to_operating_revenue_row():
    t = _FakeTicker(qis=_frame({"Operating Revenue": [1.0, 2.0, 3.0, 4.0]}))
    assert _ttm_revenue_from_quarterly(t) == pytest.approx(10.0)


def test_ttm_revenue_none_when_fewer_than_4_quarters():
    t = _FakeTicker(qis=_frame({"Total Revenue": [1.0, 2.0, 3.0]}, n_cols=3))
    assert _ttm_revenue_from_quarterly(t) is None


def test_ttm_revenue_none_when_a_quarter_is_nan():
    t = _FakeTicker(qis=_frame({"Total Revenue": [1.0, float("nan"), 3.0, 4.0]}))
    assert _ttm_revenue_from_quarterly(t) is None


# ── _ttm_roic_from_quarterly ────────────────────────────────────────────────

def _mu_like():
    # Live MU shape (2026-10-08, $bn): last 4 quarters op income 33.32/16.14/
    # 6.14/3.69; latest-quarter equity 100.72, total debt 6.38.
    qis = _frame({"Operating Income": [33.32, 16.14, 6.14, 3.69, 2.17]}, n_cols=5)
    qbs = _frame({"Stockholders Equity": [100.72, 72.46, 58.81],
                  "Total Debt": [6.38, 10.8, 12.42]}, n_cols=3)
    return _FakeTicker(qis, qbs)


def test_ttm_roic_mu_live_shape():
    # 59.29 * 0.79 / 107.10 = 43.73% — vs the stale annual 11.16%.
    assert _ttm_roic_from_quarterly(_mu_like()) == pytest.approx(43.73, abs=0.01)


def test_ttm_roic_uses_latest_quarter_capital_not_oldest():
    qis = _frame({"Operating Income": [10.0, 10.0, 10.0, 10.0]})
    qbs = _frame({"Stockholders Equity": [79.0, 1.0], "Total Debt": [0.0, 0.0]}, n_cols=2)
    assert _ttm_roic_from_quarterly(_FakeTicker(qis, qbs)) == pytest.approx(40.0)


def test_ttm_roic_missing_debt_counts_as_zero():
    qis = _frame({"Operating Income": [10.0, 10.0, 10.0, 10.0]})
    qbs = _frame({"Common Stock Equity": [79.0]}, n_cols=1)
    assert _ttm_roic_from_quarterly(_FakeTicker(qis, qbs)) == pytest.approx(40.0)


def test_ttm_roic_none_when_fewer_than_4_quarters():
    qis = _frame({"Operating Income": [10.0, 10.0, 10.0]}, n_cols=3)
    qbs = _frame({"Stockholders Equity": [79.0]}, n_cols=1)
    assert _ttm_roic_from_quarterly(_FakeTicker(qis, qbs)) is None


def test_ttm_roic_none_when_balance_sheet_missing():
    qis = _frame({"Operating Income": [10.0, 10.0, 10.0, 10.0]})
    assert _ttm_roic_from_quarterly(_FakeTicker(qis, None)) is None


def test_ttm_roic_none_when_invested_capital_not_positive():
    qis = _frame({"Operating Income": [10.0, 10.0, 10.0, 10.0]})
    qbs = _frame({"Stockholders Equity": [-50.0], "Total Debt": [20.0]}, n_cols=1)
    assert _ttm_roic_from_quarterly(_FakeTicker(qis, qbs)) is None


# ── _resolve_roic: TTM first, annual fallback, basis recorded ───────────────

_ANNUAL = {"income": [{"operating_income": 100.0}],
           "balance": [{"stockholders_equity": 790.0, "total_debt": 0.0}]}


def test_resolve_roic_prefers_ttm():
    assert _resolve_roic({**_ANNUAL, "ratios": {"roic_ttm": 43.73}}) == (43.73, "ttm_quarterly")


def test_resolve_roic_falls_back_to_annual():
    assert _resolve_roic({**_ANNUAL, "ratios": {"roic_ttm": None}}) == (10.0, "annual")


def test_resolve_roic_none_when_both_missing():
    assert _resolve_roic({"ratios": {}}) == (None, None)


# ── FX conversion covers every absolute TTM field (v7, 2026-10-08) ──────────
# Live INTR (BRL reporter): fcf was converted to USD but net_income_ttm stayed
# in BRL, so forensics.fcf_conversion_ttm read 0.19 instead of ~0.95 — and
# 31B's INTR debate made that 0.19 its swing factor. The accruals ratio
# ((NI - CFO) / converted total assets) was ~5x off the same way.

import dossier


class _FxTicker:
    def __init__(self, symbol):
        self.info = {}

    def history(self, period="1d"):
        return pd.DataFrame({"Close": [0.2]})


def test_fx_conversion_converts_all_absolute_ttm_fields(monkeypatch):
    monkeypatch.setattr(dossier.yf, "Ticker", _FxTicker)
    yf_fin = {"ratios": {"fcf": 1000.0, "revenue_ttm": 5000.0, "ebitda": 2000.0,
                         "net_income_ttm": 1100.0, "cfo_ttm": 1200.0,
                         "roic": 12.5, "shares_out": 10}}
    assert dossier._apply_fx_conversion(yf_fin, "BRL") == pytest.approx(0.2)
    r = yf_fin["ratios"]
    for field, local in (("fcf", 1000.0), ("revenue_ttm", 5000.0), ("ebitda", 2000.0),
                         ("net_income_ttm", 1100.0), ("cfo_ttm", 1200.0)):
        assert r[field] == pytest.approx(local * 0.2), field
    assert r["roic"] == 12.5  # a percentage — must NOT be scaled
    # FCF conversion is currency-neutral again: 200 / 220, not 200 / 1100
    assert r["fcf"] / r["net_income_ttm"] == pytest.approx(1000.0 / 1100.0)
