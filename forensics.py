"""Deterministic earnings-quality / balance-sheet-stress metrics (2026-07-17).

FundamentalForensics' mandate promises "ALL numbers are pre-computed in the
dossier — your job is to interpret and stress-test them, not recalculate", yet
the earnings-quality half of that mandate (accruals, cash conversion, interest
coverage, Piotroski, Altman, dilution) had nothing pre-computed: the agent had
to re-derive them from raw statement rows mid-debate, or guess. These are pure
functions over statement rows dossier.py already fetches — no new sources, no
network calls.

Contracts:
  - Every metric is None (never a guess or approximation) when an input is
    missing — same no-guess rule as fair_value/_ttm_* helpers.
  - Proxies and sign conventions are labelled IN-BAND via the "_basis" map:
    the whole section rides into the agent prompt as JSON, so the labels are
    the documentation agents actually see.
  - Supply-side only: nothing here feeds risk_reward, blind_spots, or any
    scoring math (ADAPTATION_PROTOCOL v5 register note, 2026-07-17).
"""

from __future__ import annotations

import math


def _f(v):
    """Float or None; rejects NaN/inf (yfinance rows carry NaN as float)."""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _g(row: dict, *keys):
    """First non-None value across key spellings (yfinance snake_case rows vs
    the rare FMP camelCase fallback rows — same dual-key pattern agents.py uses)."""
    for k in keys:
        v = _f((row or {}).get(k))
        if v is not None:
            return v
    return None


def _div(a, b):
    a, b = _f(a), _f(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def _accruals_ratio_ttm(ratios_ttm: dict, balance: list) -> float | None:
    """(TTM net income − TTM operating cash flow) / latest-FY total assets.
    Positive and large (>~0.10) = earnings running well ahead of cash."""
    ni = _f(ratios_ttm.get("net_income_ttm"))
    cfo = _f(ratios_ttm.get("cfo_ttm"))
    ta = _g(balance[0] if balance else {}, "total_assets", "totalAssets")
    if ni is None or cfo is None or ta is None or ta <= 0:
        return None
    return round((ni - cfo) / ta, 4)


def _fcf_conversion_ttm(ratios_ttm: dict) -> float | None:
    """TTM FCF / TTM net income. None when net income <= 0 — the ratio is
    meaningless on losses and a misleading number is worse than none."""
    fcf = _f(ratios_ttm.get("fcf"))
    ni = _f(ratios_ttm.get("net_income_ttm"))
    if fcf is None or ni is None or ni <= 0:
        return None
    return round(fcf / ni, 2)


def _interest_coverage_fy(income: list) -> float | None:
    """Latest-FY operating income / |interest expense|. None when no interest
    expense is reported (near-zero debt) — absence of the ratio is the fact."""
    i0 = income[0] if income else {}
    oi = _g(i0, "operating_income", "operatingIncome")
    ie = _g(i0, "interest_expense", "interestExpense")
    if oi is None or ie is None or ie == 0:
        return None
    return round(oi / abs(ie), 1)


def _piotroski_f(income: list, balance: list, cashflow: list) -> dict:
    """Piotroski F-score, 9 binary checks over the two most recent fiscal
    years. Each component is True/False, or None when its inputs are missing;
    the headline score is only published when >=6 components are evaluable
    (a 3-component "score" would be noise wearing a number)."""
    i0 = income[0] if len(income) > 0 else {}
    i1 = income[1] if len(income) > 1 else {}
    b0 = balance[0] if len(balance) > 0 else {}
    b1 = balance[1] if len(balance) > 1 else {}
    c0 = cashflow[0] if len(cashflow) > 0 else {}

    ni0 = _g(i0, "net_income", "netIncome")
    roa0 = _div(ni0, _g(b0, "total_assets", "totalAssets"))
    roa1 = _div(_g(i1, "net_income", "netIncome"), _g(b1, "total_assets", "totalAssets"))
    cfo0 = _g(c0, "operating_cf", "operatingCashFlow")
    lev0 = _div(_g(b0, "total_debt", "totalDebt"), _g(b0, "total_assets", "totalAssets"))
    lev1 = _div(_g(b1, "total_debt", "totalDebt"), _g(b1, "total_assets", "totalAssets"))
    cur0 = _div(_g(b0, "current_assets"), _g(b0, "current_liabilities"))
    cur1 = _div(_g(b1, "current_assets"), _g(b1, "current_liabilities"))
    sh0, sh1 = _g(i0, "diluted_shares"), _g(i1, "diluted_shares")
    gm0 = _div(_g(i0, "gross_profit", "grossProfit"), _g(i0, "revenue"))
    gm1 = _div(_g(i1, "gross_profit", "grossProfit"), _g(i1, "revenue"))
    at0 = _div(_g(i0, "revenue"), _g(b0, "total_assets", "totalAssets"))
    at1 = _div(_g(i1, "revenue"), _g(b1, "total_assets", "totalAssets"))

    def _gt(a, b):
        return None if (a is None or b is None) else a > b

    components = {
        "roa_positive":             None if roa0 is None else roa0 > 0,
        "cfo_positive":             None if cfo0 is None else cfo0 > 0,
        "roa_improving":            _gt(roa0, roa1),
        "cfo_exceeds_net_income":   None if (cfo0 is None or ni0 is None) else cfo0 > ni0,
        "leverage_falling":         _gt(lev1, lev0),   # lower debt/assets this year
        "current_ratio_improving":  _gt(cur0, cur1),
        "no_dilution":              None if (sh0 is None or sh1 is None) else sh0 <= sh1,
        "gross_margin_improving":   _gt(gm0, gm1),
        "asset_turnover_improving": _gt(at0, at1),
    }
    evaluated = sum(1 for v in components.values() if v is not None)
    score = sum(1 for v in components.values() if v is True) if evaluated >= 6 else None
    return {"score": score, "components_evaluated": evaluated, "components": components}


def _altman_z(income: list, balance: list, market_cap, sector: str) -> dict:
    """Classic 5-factor Altman Z. Gated off for financials (the model's ratios
    are meaningless on a bank balance sheet). Proxies labelled in _basis."""
    if sector and "financial" in str(sector).lower():
        return {"score": None, "zone": None, "note": "not meaningful for financials"}
    i0 = income[0] if income else {}
    b0 = balance[0] if balance else {}
    ta = _g(b0, "total_assets", "totalAssets")
    equity = _g(b0, "stockholders_equity", "totalStockholdersEquity")
    ca = _g(b0, "current_assets")
    cl = _g(b0, "current_liabilities")
    re = _g(b0, "retained_earnings", "retainedEarnings")
    ebit = _g(i0, "operating_income", "operatingIncome")
    rev = _g(i0, "revenue")
    mcap = _f(market_cap)
    if None in (ta, equity, ca, cl, re, ebit, rev, mcap) or ta <= 0:
        return {"score": None, "zone": None, "note": "missing inputs"}
    tl = ta - equity
    if tl <= 0:
        return {"score": None, "zone": None, "note": "implied liabilities <= 0"}
    z = (1.2 * (ca - cl) / ta + 1.4 * re / ta + 3.3 * ebit / ta
         + 0.6 * mcap / tl + 1.0 * rev / ta)
    zone = "distress" if z < 1.81 else ("grey" if z <= 2.99 else "safe")
    return {"score": round(z, 2), "zone": zone, "note": None}


def _share_count_change(income: list) -> dict:
    """Diluted share-count change across the annual history (up to 4 FYs).
    NEGATIVE = net buybacks, POSITIVE = dilution."""
    rows = [(r.get("date"), _g(r, "diluted_shares")) for r in income or []]
    rows = [(d, s) for d, s in rows if s is not None and s > 0]
    if len(rows) < 2:
        return {"pct": None, "years": None}
    newest, oldest = rows[0][1], rows[-1][1]
    years = len(rows) - 1
    return {"pct": round((newest - oldest) / oldest * 100, 2), "years": years}


def compute_forensics(income: list, balance: list, cashflow: list,
                      ratios_ttm: dict, market_cap, sector: str) -> dict:
    """Assemble the dossier's `forensics` section. Pure — safe to unit-test
    with synthetic rows; every metric independently None-safe."""
    income, balance, cashflow = income or [], balance or [], cashflow or []
    ratios_ttm = ratios_ttm or {}
    return {
        "accruals_ratio_ttm":  _accruals_ratio_ttm(ratios_ttm, balance),
        "fcf_conversion_ttm":  _fcf_conversion_ttm(ratios_ttm),
        "interest_coverage_fy": _interest_coverage_fy(income),
        "piotroski_f":          _piotroski_f(income, balance, cashflow),
        "altman_z":             _altman_z(income, balance, market_cap, sector),
        "share_count_change":   _share_count_change(income),
        "_basis": {
            "accruals_ratio_ttm":  "(net_income_ttm - cfo_ttm) / latest-FY total assets; "
                                   "positive & large (>~0.10) = earnings running ahead of cash",
            "fcf_conversion_ttm":  "fcf_ttm / net_income_ttm; None when TTM net income <= 0 "
                                   "(ratio meaningless on losses)",
            "interest_coverage_fy": "latest-FY operating income / |interest expense|; "
                                    "None when no interest expense reported",
            "piotroski_f":          "9-check F-score over the 2 latest FYs; leverage check uses "
                                    "TOTAL debt/assets (long-term split not in rows); score only "
                                    "published when >=6 checks evaluable",
            "altman_z":             "classic 5-factor; EBIT = operating income proxy; total "
                                    "liabilities = assets - equity; manufacturing calibration, "
                                    "directional only; zones: <1.81 distress / <=2.99 grey / safe",
            "share_count_change":   "diluted shares, newest vs oldest annual row; "
                                    "NEGATIVE = net buybacks, POSITIVE = dilution",
        },
    }
