"""dossier._own_multiple_history (2026-07-17) — own-history P/E and P/FCF
bands: per-FY multiple = FY-average monthly close / per-share annual figure.
Closes ValuationEngine's cheap/rich-vs-own-history ask with data already in the
dossier (annual diluted EPS since 07-14 + one cached 5y monthly-closes fetch).
Contracts: negative-denominator years excluded AND counted, <2 usable years →
None band, <6 real months → year skipped, _basis label rides in-band."""

import pytest

from dossier import _own_multiple_history


def _flat_monthly(px=100.0, y0=2022, y1=2026):
    """Constant month-end close across the whole window → every FY average
    is exactly px, so multiples reduce to px / per-share figures."""
    out = {}
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            out[f"{y:04d}-{m:02d}"] = px
    return out


def _income():
    # EPS by year (newest first): 5, 4, 2, -1  → PE points 20, 25, 50 + 1 excluded
    return [
        {"date": "2026-01-31", "net_income": 5_000, "diluted_shares": 1_000},
        {"date": "2025-01-31", "net_income": 4_000, "diluted_shares": 1_000},
        {"date": "2024-01-31", "net_income": 2_000, "diluted_shares": 1_000},
        {"date": "2023-01-31", "net_income": -1_000, "diluted_shares": 1_000},
    ]


def _cashflow():
    # FCF/share: 2.0, 1.0 → P/FCF points 50, 100
    return [
        {"date": "2026-01-31", "free_cash_flow": 2_000},
        {"date": "2025-01-31", "free_cash_flow": 1_000},
    ]


def _ratios():
    return {"pe": 22.0, "fcf_per_share": 2.2}


def test_pe_band_exact_median_high_low():
    out = _own_multiple_history(_flat_monthly(), _income(), _cashflow(), _ratios(), price=110.0)
    pe = out["pe_band"]
    assert pe["median"] == 25.0
    assert pe["high"] == 50.0
    assert pe["low"] == 20.0
    assert pe["years_used"] == 3


def test_negative_eps_year_excluded_and_counted():
    pe = _own_multiple_history(_flat_monthly(), _income(), _cashflow(), _ratios(), 110.0)["pe_band"]
    assert pe["excluded_nonpositive_years"] == 1


def test_current_vs_median_pct():
    pe = _own_multiple_history(_flat_monthly(), _income(), _cashflow(), _ratios(), 110.0)["pe_band"]
    assert pe["current"] == 22.0
    assert pe["current_vs_median_pct"] == pytest.approx(-12.0)


def test_pfcf_band_joins_shares_from_income_by_date():
    out = _own_multiple_history(_flat_monthly(), _income(), _cashflow(), _ratios(), price=110.0)
    pf = out["pfcf_band"]
    assert pf["median"] == 75.0 and pf["high"] == 100.0 and pf["low"] == 50.0
    # current P/FCF from live price / TTM fcf_per_share
    assert pf["current"] == pytest.approx(50.0)


def test_pfcf_row_without_matching_income_date_is_skipped():
    cf = _cashflow() + [{"date": "1999-01-31", "free_cash_flow": 9_000}]
    pf = _own_multiple_history(_flat_monthly(), _income(), cf, _ratios(), 110.0)["pfcf_band"]
    assert pf["years_used"] == 2  # the orphan row contributed nothing


def test_band_none_when_fewer_than_2_usable_years():
    inc = _income()[:1]
    out = _own_multiple_history(_flat_monthly(), inc, [], _ratios(), 110.0)
    assert out["pe_band"]["median"] is None
    assert out["pe_band"]["years_used"] == 1
    assert out["pe_band"]["current"] == 22.0  # current still shown, band honest-None


def test_fy_window_with_under_6_months_is_skipped():
    # Closes exist only for 2026 → the 2024/2023 FY windows have <6 months.
    monthly = {f"2026-{m:02d}": 100.0 for m in range(1, 13)}
    monthly.update({f"2025-{m:02d}": 100.0 for m in range(1, 13)})
    pe = _own_multiple_history(monthly, _income(), [], _ratios(), 110.0)["pe_band"]
    # FY2026 (ends 2026-01) and FY2025 (ends 2025-01, window = 2025-01..2024-02
    # → only Jan-2025 exists → skipped): usable = FY2026 + FY2025? No — count:
    # FY2026 window 2026-01..2025-02 → 12 real months; FY2025 window has 1.
    assert pe["years_used"] == 1
    assert pe["median"] is None


def test_empty_inputs_are_none_safe():
    out = _own_multiple_history({}, [], [], {}, None)
    assert out["pe_band"]["median"] is None
    assert out["pfcf_band"]["median"] is None
    assert "_basis" in out


def test_mid_year_fiscal_end_uses_trailing_12_calendar_months():
    # FY ends 2025-06-30: window = 2025-06 back to 2024-07. Give those months
    # close=200 and everything else 100 — the FY avg must be exactly 200.
    monthly = _flat_monthly(100.0)
    yy, mm = 2025, 6
    for _ in range(12):
        monthly[f"{yy:04d}-{mm:02d}"] = 200.0
        mm -= 1
        if mm == 0:
            yy, mm = yy - 1, 12
    inc = [
        {"date": "2025-06-30", "net_income": 4_000, "diluted_shares": 1_000},  # PE 50
        {"date": "2024-06-30", "net_income": 4_000, "diluted_shares": 1_000},  # PE 25
    ]
    pe = _own_multiple_history(monthly, inc, [], {}, None)["pe_band"]
    assert pe["high"] == 50.0
    assert pe["low"] == 25.0
