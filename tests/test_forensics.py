"""forensics.compute_forensics (2026-07-17) — FundamentalForensics' mandate
promises "ALL numbers are pre-computed in the dossier" yet its earnings-quality
half (accruals, cash conversion, interest coverage, Piotroski, Altman, dilution)
had nothing pre-computed. Pure functions over statement rows dossier.py already
fetches; every metric None (never a guess) on missing inputs; supply-side only
(nothing feeds risk_reward/blind_spots/scoring)."""

import json

import pandas as pd
import pytest

import agents
from dossier import _ttm_cfo_from_quarterly
from forensics import compute_forensics


# ── fixtures ────────────────────────────────────────────────────────────────

def _income():
    return [
        {"date": "2026-01-31", "revenue": 100_000, "gross_profit": 60_000,
         "operating_income": 40_000, "net_income": 30_000,
         "diluted_shares": 950, "interest_expense": 2_000, "pretax_income": 38_000},
        {"date": "2025-01-31", "revenue": 80_000, "gross_profit": 44_000,
         "operating_income": 30_000, "net_income": 22_000,
         "diluted_shares": 1_000, "interest_expense": 2_500, "pretax_income": 28_000},
        {"date": "2024-01-31", "revenue": 70_000, "gross_profit": 38_000,
         "operating_income": 25_000, "net_income": 18_000,
         "diluted_shares": 1_040, "interest_expense": 2_600, "pretax_income": 23_000},
    ]


def _balance():
    return [
        {"date": "2026-01-31", "total_assets": 200_000, "total_debt": 20_000,
         "stockholders_equity": 120_000, "cash": 30_000,
         "current_assets": 80_000, "current_liabilities": 40_000,
         "retained_earnings": 90_000},
        {"date": "2025-01-31", "total_assets": 180_000, "total_debt": 25_000,
         "stockholders_equity": 100_000, "cash": 25_000,
         "current_assets": 70_000, "current_liabilities": 38_000,
         "retained_earnings": 70_000},
    ]


def _cashflow():
    return [
        {"date": "2026-01-31", "operating_cf": 42_000, "capex": -8_000,
         "free_cash_flow": 34_000},
        {"date": "2025-01-31", "operating_cf": 33_000, "capex": -7_000,
         "free_cash_flow": 26_000},
    ]


def _ratios():
    return {"net_income_ttm": 32_000, "cfo_ttm": 44_000, "fcf": 36_000}


def _full():
    return compute_forensics(_income(), _balance(), _cashflow(), _ratios(),
                             market_cap=600_000, sector="Technology")


# ── accruals ratio ──────────────────────────────────────────────────────────

def test_accruals_ratio_exact():
    # (32k − 44k) / 200k = −0.06 — cash comfortably ahead of earnings.
    assert _full()["accruals_ratio_ttm"] == pytest.approx(-0.06)


def test_accruals_none_when_cfo_ttm_missing():
    r = _ratios(); r.pop("cfo_ttm")
    out = compute_forensics(_income(), _balance(), _cashflow(), r, 600_000, "Technology")
    assert out["accruals_ratio_ttm"] is None


def test_accruals_none_when_no_balance_rows():
    out = compute_forensics(_income(), [], _cashflow(), _ratios(), 600_000, "Technology")
    assert out["accruals_ratio_ttm"] is None


# ── FCF conversion ──────────────────────────────────────────────────────────

def test_fcf_conversion_exact():
    assert _full()["fcf_conversion_ttm"] == pytest.approx(36_000 / 32_000, abs=0.01)


def test_fcf_conversion_none_on_negative_net_income():
    r = _ratios(); r["net_income_ttm"] = -5_000
    out = compute_forensics(_income(), _balance(), _cashflow(), r, 600_000, "Technology")
    assert out["fcf_conversion_ttm"] is None  # meaningless on losses — no number


# ── interest coverage ───────────────────────────────────────────────────────

def test_interest_coverage_exact():
    assert _full()["interest_coverage_fy"] == pytest.approx(40_000 / 2_000)


def test_interest_coverage_none_when_no_interest_expense():
    inc = _income()
    inc[0]["interest_expense"] = None
    out = compute_forensics(inc, _balance(), _cashflow(), _ratios(), 600_000, "Technology")
    assert out["interest_coverage_fy"] is None


def test_interest_coverage_uses_abs_for_negative_convention():
    inc = _income()
    inc[0]["interest_expense"] = -2_000  # some sources report expense as negative
    out = compute_forensics(inc, _balance(), _cashflow(), _ratios(), 600_000, "Technology")
    assert out["interest_coverage_fy"] == pytest.approx(20.0)


# ── Piotroski F ─────────────────────────────────────────────────────────────

def test_piotroski_all_nine_pass_on_improving_fixture():
    p = _full()["piotroski_f"]
    # Fixture is improving on every axis: ROA+ & rising, CFO+ & > NI,
    # leverage falling (20/200 < 25/180), current ratio 2.0 > 1.84,
    # buybacks (950 <= 1000), GM 60% > 55%, turnover 0.5 > 0.44.
    assert p["components_evaluated"] == 9
    assert p["components"] == {k: True for k in p["components"]}
    assert p["score"] == 9


def test_piotroski_dilution_and_margin_fade_lose_points():
    inc = _income()
    inc[0]["diluted_shares"] = 1_100          # dilution
    inc[0]["gross_profit"] = 40_000           # GM 40% < prior 55%
    p = compute_forensics(inc, _balance(), _cashflow(), _ratios(), 600_000, "Technology")["piotroski_f"]
    assert p["components"]["no_dilution"] is False
    assert p["components"]["gross_margin_improving"] is False
    assert p["score"] == 7


def test_piotroski_score_withheld_when_data_thin():
    # Single year of everything → the delta checks are all None.
    p = compute_forensics(_income()[:1], _balance()[:1], _cashflow()[:1],
                          _ratios(), 600_000, "Technology")["piotroski_f"]
    assert p["score"] is None
    assert p["components_evaluated"] < 6
    assert p["components"]["roa_improving"] is None


# ── Altman Z ────────────────────────────────────────────────────────────────

def test_altman_z_exact_and_safe_zone():
    # WC/TA=0.2, RE/TA=0.45, EBIT/TA=0.2, MC/TL=7.5, Rev/TA=0.5
    # Z = 1.2(.2) + 1.4(.45) + 3.3(.2) + 0.6(7.5) + 1.0(.5) = 6.53
    z = _full()["altman_z"]
    assert z["score"] == pytest.approx(6.53, abs=0.01)
    assert z["zone"] == "safe"


def test_altman_gated_off_for_financials():
    z = compute_forensics(_income(), _balance(), _cashflow(), _ratios(),
                          600_000, "Financial Services")["altman_z"]
    assert z["score"] is None
    assert "financial" in z["note"]


def test_altman_none_when_retained_earnings_missing():
    bal = _balance()
    bal[0]["retained_earnings"] = None
    z = compute_forensics(_income(), bal, _cashflow(), _ratios(), 600_000, "Technology")["altman_z"]
    assert z["score"] is None
    assert z["note"] == "missing inputs"


def test_altman_distress_zone_on_stressed_fixture():
    inc = [{"date": "2026-01-31", "revenue": 40_000, "operating_income": -2_000,
            "gross_profit": 8_000, "net_income": -6_000}]
    bal = [{"date": "2026-01-31", "total_assets": 200_000, "total_debt": 120_000,
            "stockholders_equity": 30_000, "current_assets": 30_000,
            "current_liabilities": 60_000, "retained_earnings": -10_000}]
    z = compute_forensics(inc, bal, [], {}, market_cap=20_000, sector="Industrials")["altman_z"]
    assert z["zone"] == "distress"
    assert z["score"] < 1.81


# ── share-count trend ───────────────────────────────────────────────────────

def test_share_count_change_negative_means_buyback():
    sc = _full()["share_count_change"]
    assert sc["pct"] == pytest.approx((950 - 1_040) / 1_040 * 100, abs=0.01)
    assert sc["pct"] < 0
    assert sc["years"] == 2


def test_share_count_none_with_single_row():
    sc = compute_forensics(_income()[:1], [], [], {}, None, "Technology")["share_count_change"]
    assert sc["pct"] is None


# ── assembly contracts ──────────────────────────────────────────────────────

def test_everything_none_safe_on_empty_dossier():
    out = compute_forensics([], [], [], {}, None, "")
    assert out["accruals_ratio_ttm"] is None
    assert out["fcf_conversion_ttm"] is None
    assert out["interest_coverage_fy"] is None
    assert out["piotroski_f"]["score"] is None
    assert out["altman_z"]["score"] is None
    assert out["share_count_change"]["pct"] is None


def test_basis_labels_ride_with_the_numbers():
    # The _basis map IS the documentation agents see — it must ship in-band
    # and cover every metric key.
    out = _full()
    for key in ("accruals_ratio_ttm", "fcf_conversion_ttm", "interest_coverage_fy",
                "piotroski_f", "altman_z", "share_count_change"):
        assert key in out["_basis"]
    assert "NEGATIVE = net buybacks" in out["_basis"]["share_count_change"]


def test_camelcase_fmp_fallback_rows_still_compute():
    inc = [{"date": "2026-01-31", "revenue": 100_000, "grossProfit": 60_000,
            "operatingIncome": 40_000, "netIncome": 30_000}]
    bal = [{"date": "2026-01-31", "totalAssets": 200_000, "totalDebt": 20_000,
            "totalStockholdersEquity": 120_000, "retainedEarnings": 90_000,
            "current_assets": 80_000, "current_liabilities": 40_000}]
    out = compute_forensics(inc, bal, [], _ratios(), 600_000, "Technology")
    assert out["accruals_ratio_ttm"] == pytest.approx(-0.06)
    assert out["altman_z"]["score"] is not None


# ── _ttm_cfo_from_quarterly (same contract as the FCF/NI helpers) ───────────

class _FakeTicker:
    def __init__(self, qcf):
        self._qcf = qcf

    @property
    def quarterly_cashflow(self):
        if isinstance(self._qcf, Exception):
            raise self._qcf
        return self._qcf


def _qcf(row, vals, n_cols=4):
    cols = pd.date_range("2026-01-31", periods=n_cols, freq="-3ME")
    return pd.DataFrame({row: (vals + [None] * n_cols)[:n_cols]}, index=cols).T


def test_ttm_cfo_sums_last_4_quarters():
    t = _FakeTicker(_qcf("Operating Cash Flow", [10.0, 20.0, 30.0, 40.0, 99.0], n_cols=5))
    assert _ttm_cfo_from_quarterly(t) == pytest.approx(100.0)


def test_ttm_cfo_falls_back_to_continuing_operations_row():
    t = _FakeTicker(_qcf("Cash Flow From Continuing Operating Activities", [1.0, 2.0, 3.0, 4.0]))
    assert _ttm_cfo_from_quarterly(t) == pytest.approx(10.0)


def test_ttm_cfo_none_when_fewer_than_4_quarters():
    t = _FakeTicker(_qcf("Operating Cash Flow", [10.0, 20.0], n_cols=2))
    assert _ttm_cfo_from_quarterly(t) is None


def test_ttm_cfo_none_when_a_quarter_is_nan():
    t = _FakeTicker(_qcf("Operating Cash Flow", [10.0, float("nan"), 30.0, 40.0]))
    assert _ttm_cfo_from_quarterly(t) is None


def test_ttm_cfo_none_on_exception():
    assert _ttm_cfo_from_quarterly(_FakeTicker(RuntimeError("boom"))) is None


# ── prompt integration: the section reaches agents ──────────────────────────

def test_forensics_section_rides_into_round1_prompt():
    d = {
        "ticker": "FAKE",
        "profile": {"name": "Fake Corp"},
        "quote": {"price": 100.0},
        "financials": {"income": [], "cashflow": [], "balance": [], "ratios_ttm": {}},
        "forensics": compute_forensics(_income(), _balance(), _cashflow(),
                                       _ratios(), 600_000, "Technology"),
    }
    _system, user = agents.round1_prompt("FundamentalForensics", "FAKE", d, "research")
    blob = user.split("=== STRUCTURED DATA DOSSIER ===")[1].split("=" * 32)[0]
    parsed = json.loads(blob)
    # slim strips "financials" but keeps forensics — with numbers AND basis labels.
    assert "financials" not in parsed
    assert parsed["forensics"]["accruals_ratio_ttm"] == pytest.approx(-0.06)
    assert parsed["forensics"]["piotroski_f"]["score"] == 9
    assert "_basis" in parsed["forensics"]
