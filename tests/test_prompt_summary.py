"""agents.round1_prompt financials_summary period-labelling (2026-07-14) —
income[]/cashflow[] hold ANNUAL statements, but the summary used to expose them
under *_ttm-suffixed keys, and free_cash_flow PREFERRED the annual figure over
ratios.fcf (the genuine, EDGAR-reconciled TTM: 7/8 tickers exact-to-the-dollar
vs SEC XBRL). For an off-cycle grower the two differ hugely — NVDA's FY (ended
Jan) FCF is ~half its real TTM — so agents were citing "TTM" numbers that
weren't. The summary now labels annual values latest_fy_*, leads with the true
fcf_ttm (+ its fcf_source provenance), and supplies SBC for every archetype
(two agents' prompts explicitly instruct subtracting SBC from FCF, but the
value only reached ASSET_LIGHT prompts via fair_value key_metrics)."""

import json

import agents


def _dossier():
    return {
        "ticker": "FAKE",
        "profile": {"name": "Fake Corp"},
        "quote": {"price": 100.0},
        "financials": {
            "income": [
                {"date": "2026-01-31", "revenue": 100_000, "gross_profit": 60_000,
                 "operating_income": 40_000, "net_income": 30_000},
                {"date": "2025-01-31", "revenue": 80_000, "gross_profit": 48_000,
                 "operating_income": 30_000, "net_income": 22_000},
            ],
            "cashflow": [
                {"date": "2026-01-31", "operating_cf": 45_000, "capex": -5_000,
                 "free_cash_flow": 40_000, "stock_based_compensation": 7_000},
            ],
            "balance": [
                {"date": "2026-01-31", "total_debt": 10_000,
                 "stockholders_equity": 50_000, "cash": 20_000},
            ],
            # TTM figures deliberately DIFFERENT from the annual statement so a
            # mislabel is detectable.
            "ratios_ttm": {"revenue_ttm": 130_000, "fcf": 55_000,
                           "fcf_source": "ttm_quarterly_sum"},
        },
    }


def _summary(d):
    _system, user = agents.round1_prompt("ValuationEngine", "FAKE", d, "research")
    blob = user.split("=== STRUCTURED DATA DOSSIER ===")[1].split("=" * 32)[0]
    return json.loads(blob)["financials_summary"]


def test_ttm_keys_carry_ttm_values_not_annual():
    s = _summary(_dossier())
    assert s["revenue_ttm"] == 130_000          # NOT the 100k annual figure
    assert s["fcf_ttm"] == 55_000               # NOT the 40k annual figure
    assert s["fcf_ttm_source"] == "ttm_quarterly_sum"


def test_annual_values_still_present_under_honest_labels():
    s = _summary(_dossier())
    assert s["latest_fy_date"] == "2026-01-31"
    assert s["latest_fy_revenue"] == 100_000
    assert s["latest_fy_net_income"] == 30_000
    assert s["latest_fy_free_cash_flow"] == 40_000
    assert s["latest_fy_operating_cf"] == 45_000
    assert s["latest_fy_capex"] == -5_000
    # No key claims TTM while carrying an annual value.
    for stale in ("gross_profit_ttm", "operating_income_ttm", "net_income_ttm",
                  "free_cash_flow", "operating_cf", "capex"):
        assert stale not in s


def test_sbc_supplied_for_every_archetype():
    s = _summary(_dossier())
    assert s["latest_fy_sbc"] == 7_000


def test_growth_yoy_still_computed_from_annual_series():
    # agents._growth returns a PERCENTAGE (25.0), not a fraction.
    s = _summary(_dossier())
    assert s["revenue_growth_yoy"] == 25.0


def test_missing_statements_degrade_gracefully():
    d = _dossier()
    d["financials"] = {"ratios_ttm": {"revenue_ttm": 130_000, "fcf": 55_000,
                                      "fcf_source": "info_dict_fallback"}}
    s = _summary(d)
    assert s["revenue_ttm"] == 130_000
    assert s["fcf_ttm"] == 55_000
    assert s["revenue_growth_yoy"] is None
    assert "latest_fy_revenue" not in s
