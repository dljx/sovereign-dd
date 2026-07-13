"""fair_value.py — the composite fair value / margin-of-safety this feeds
straight into ValuationEngine's prompt (agents.py summary["fair_value_
composite"]). No coverage existed before 2026-07-13.

Live audit (real dossier.build() calls, not synthetic fixtures) found
compute_fair_values arithmetically CORRECT but the ASSET_LIGHT_GROWTH
engine's fixed multiple tiers (EV/FCF 12-25x, P/S 3-8x — calibrated for
stable mid-cap SaaS comps) badly understate fair value for large/fast-
growing names: AAPL composite came out 0.31x its trading price ($97 vs
$315), MRVL 0.10x ($24 vs $236) — both hand-verified as correct arithmetic
on the wrong assumptions, not a code bug. The orchestrator's own backstop
sanity check exists specifically to catch this class of failure but its
prior band (>5.0x or <0.04x — a 25-95x mispricing before firing) missed
both live cases. This locks the tightened band + the archetype-specific
diagnostic flag added the same day."""

import pytest

from fair_value import compute_fair_values, _value_asset_light


def _dossier(**overrides):
    base = {
        "ticker": "TEST",
        "quote": {"price": 100.0},
        "profile": {"sector": "Technology", "yf_sector": "Technology", "industry": ""},
        "financials": {
            "ratios_ttm": {
                "shares_out": 1_000_000_000, "revenue_ttm": 10_000_000_000,
                "gross_margin": 70.0, "fcf": 2_000_000_000,
                "fwd_revenue_growth": 0.15,
            },
            "income": [
                {"revenue": 10_000_000_000, "net_income": 1_000_000_000,
                 "gross_profit": 7_000_000_000, "research_development": 500_000_000},
                {"revenue": 9_000_000_000, "net_income": 900_000_000,
                 "gross_profit": 6_300_000_000, "research_development": 450_000_000},
            ],
            "balance": [{"total_debt": 0, "cash": 5_000_000_000}],
            "cashflow": [{"free_cash_flow": 2_000_000_000, "stock_based_compensation": 200_000_000}],
        },
        "macro": {}, "valuation": {},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


# ── Regression lock: AAPL/MRVL arithmetic hand-verified 2026-07-13 ─────────
# If the multiple tiers ever get recalibrated, these numbers SHOULD move —
# that's fine, update the expected value deliberately. What must never happen
# again is the composite silently landing at a wild ratio to price with no
# blind_spot flag at all.

def test_asset_light_composite_matches_hand_verified_aapl_arithmetic():
    # Real AAPL inputs (2026-07-13): sbc_adjusted_fcf=85.904B, target_multiple=18
    # (rule_of_40=25.45), net_debt=62.723B, shares=14.687B -> primary=$101.01.
    # ps_multiple=3 (gross_margin 47.86% < 60%) -> secondary=$92.21.
    # composite = 101.01*0.6 + 92.21*0.4 = $97.49.
    d = _dossier(
        quote={"price": 315.32},
        financials={
            "ratios_ttm": {"shares_out": 14_687_356_000, "revenue_ttm": 451_442_016_256,
                          "gross_margin": 47.86, "fcf": 101_090_746_368,
                          "fwd_revenue_growth": 0.1501},
            "income": [{"revenue": 451_442_016_256, "net_income": 1,
                       "gross_profit": 1, "research_development": 1},
                      {"revenue": 424_180_000_000, "net_income": 1,
                       "gross_profit": 1, "research_development": 1}],
            "balance": [{"total_debt": 98_657_000_000, "cash": 35_934_000_000}],
            "cashflow": [{"free_cash_flow": 98_767_000_000,
                         "stock_based_compensation": 12_863_000_000}],
        },
    )
    result = _value_asset_light(d)
    assert result["primary"]["fair_value"] == pytest.approx(101.01, abs=0.01)
    assert result["secondary"][0]["fair_value"] == pytest.approx(92.21, abs=0.01)
    assert result["composite"] == pytest.approx(97.49, abs=0.02)


# ── Backstop sanity check (tightened 2026-07-13) ────────────────────────────

def test_backstop_flags_aapl_style_wide_gap():
    # A realistic asset-light-relaxed case whose composite lands far below
    # price (same shape as the live AAPL finding) must be flagged.
    d = _dossier(quote={"price": 300.0}, financials={
        "ratios_ttm": {"shares_out": 1_000_000_000, "revenue_ttm": 10_000_000_000,
                      "gross_margin": 45.0, "fcf": 3_000_000_000, "fwd_revenue_growth": 0.10},
    })
    out = compute_fair_values(d)
    assert out["composite_fair_value"] is not None
    ratio = out["composite_fair_value"] / 300.0
    assert ratio < 0.35  # confirms this test fixture actually reproduces the failure shape
    assert any("composite_fv_wide_gap" in f for f in out["blind_spot_flags"])


def test_backstop_silent_on_ordinary_disagreement():
    # A composite that's a defensible ~20% away from price (a normal amount
    # for any valuation model to disagree with the market) must NOT be
    # flagged — the backstop exists for wild gaps, not routine judgment calls.
    d = _dossier(quote={"price": 100.0})  # base fixture composite lands near $100-ish range
    out = compute_fair_values(d)
    if out["composite_fair_value"] is not None:
        ratio = out["composite_fair_value"] / 100.0
        if 0.35 <= ratio <= 3.0:
            assert not any("composite_fv_wide_gap" in f for f in out["blind_spot_flags"])


def test_backstop_never_drops_the_value_even_when_flagged():
    # The whole point: flag it, don't hide it — the model might be right.
    d = _dossier(quote={"price": 300.0}, financials={
        "ratios_ttm": {"shares_out": 1_000_000_000, "revenue_ttm": 10_000_000_000,
                      "gross_margin": 45.0, "fcf": 3_000_000_000, "fwd_revenue_growth": 0.10},
    })
    out = compute_fair_values(d)
    assert out["composite_fair_value"] is not None  # still present, not nulled out


# ── STATIC_MULTIPLE_VS_FAST_GROWTH diagnostic flag ──────────────────────────

def test_fast_growth_below_hypergrowth_cutoff_flagged():
    # 42% YoY growth (MRVL's real figure) — real growth, but under the 50%
    # hypergrowth override threshold, so it hits the static fixed-tier path.
    d = _dossier(financials={
        "income": [{"revenue": 10_000_000_000, "net_income": 1, "gross_profit": 1, "research_development": 1},
                  {"revenue": 7_000_000_000, "net_income": 1, "gross_profit": 1, "research_development": 1}],
    })
    result = _value_asset_light(d)
    assert any("STATIC_MULTIPLE_VS_FAST_GROWTH" in b for b in result["blind_spots"])


def test_modest_growth_not_flagged():
    d = _dossier()  # base fixture: 10B vs 9B revenue = ~11% growth
    result = _value_asset_light(d)
    assert not any("STATIC_MULTIPLE_VS_FAST_GROWTH" in b for b in result["blind_spots"])


def test_hypergrowth_names_not_double_flagged():
    # >50% growth triggers the (separately-calibrated, higher-multiple)
    # hypergrowth override — the static-tier warning doesn't apply to it.
    d = _dossier(financials={
        "ratios_ttm": {"gross_margin": 70.0, "fwd_revenue_growth": 0.60},
        "income": [{"revenue": 10_000_000_000, "net_income": 1, "gross_profit": 1, "research_development": 1},
                  {"revenue": 6_000_000_000, "net_income": 1, "gross_profit": 1, "research_development": 1}],
    })
    result = _value_asset_light(d)
    assert result["key_metrics"]["hypergrowth_active"] is True
    assert not any("STATIC_MULTIPLE_VS_FAST_GROWTH" in b for b in result["blind_spots"])
