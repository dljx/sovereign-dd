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

from fair_value import (
    compute_fair_values, _value_asset_light, _value_mature_compounder,
    _value_cyclical, _value_financial, _value_infrastructure, _value_early_stage,
    _peer_median,
)


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
    # Real AAPL inputs, NO peer_comps (static-fallback path, deliberately — see
    # test_asset_light_uses_peer_median_over_static_tiers below for the peer path):
    # sbc_adjusted_fcf=85.904B, target_multiple=18 (rule_of_40=25.45),
    # net_debt=62.723B, shares=14.687B -> primary=$101.01.
    # ev_sales_multiple=3 (gross_margin 47.86% < 60%, static fallback), formula
    # switched 2026-07-13 from raw P/S to EV/Sales (net-debt-subtracted, capital-
    # structure-neutral): (3*451.442B - 62.723B)/14.687B -> secondary=$87.94
    # (was $92.21 under the old P/S-direct formula — hand-verified, deliberate).
    # composite = 101.01*0.6 + 87.94*0.4 = $95.78.
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
    assert result["secondary"][0]["fair_value"] == pytest.approx(87.94, abs=0.01)
    assert result["composite"] == pytest.approx(95.78, abs=0.02)


def test_asset_light_uses_peer_median_over_static_tiers():
    # Same AAPL-shaped inputs, but WITH real peer_comps present — the peer
    # median must now drive both legs instead of the static tables.
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
        peer_comps=[
            {"ticker": "P1", "ev_fcf": 40.0, "ev_sales": 9.0},
            {"ticker": "P2", "ev_fcf": 50.0, "ev_sales": 11.0},
            {"ticker": "P3", "ev_fcf": 45.0, "ev_sales": 10.0},
        ],
    )
    result = _value_asset_light(d)
    assert result["key_metrics"]["used_peer_ev_fcf"] is True
    assert result["key_metrics"]["used_peer_ev_sales"] is True
    assert result["primary"]["assumptions"]["target_multiple"] == 45.0  # median of 40/45/50
    assert result["secondary"][0]["assumptions"]["ev_sales_multiple"] == 10.0  # median of 9/10/11
    # A peer-anchored composite for a real mega-cap should land MUCH closer to
    # price than the static-tier 0.31x ratio found live pre-recalibration.
    assert result["composite"] / 315.32 > 0.7


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


# ── _peer_median (shared utility) ───────────────────────────────────────────

def test_peer_median_requires_min_2_valid():
    assert _peer_median([{"ev_fcf": 20}], "ev_fcf") is None
    assert _peer_median([{"ev_fcf": 20}, {"ev_fcf": 30}], "ev_fcf") == 25


def test_peer_median_excludes_none_and_nonpositive():
    peers = [{"ev_fcf": 20}, {"ev_fcf": None}, {"ev_fcf": 0}, {"ev_fcf": -5}, {"ev_fcf": 30}]
    assert _peer_median(peers, "ev_fcf") == 25  # only 20,30 qualify


def test_peer_median_handles_empty_and_none_peers_list():
    assert _peer_median([], "ev_fcf") is None
    assert _peer_median(None, "ev_fcf") is None


# ── Remaining 5 archetypes: peer-median primary, static fallback ───────────
# Each gets a minimal "peer path wins" + "static fallback still works" pair —
# mirroring the asset_light coverage above, not re-deriving every formula.

def test_mature_compounder_uses_peer_ev_fcf_over_sector_table():
    d = _dossier(
        profile={"sector": "Energy", "yf_sector": "Energy"},  # static table = 12x
        financials={"ratios_ttm": {"shares_out": 1_000_000_000, "roic": 15.0, "fcf": 2_000_000_000},
                   "balance": [{"total_debt": 0, "cash": 0}]},
        valuation={"dcf_iv_per_share": None},
        peer_comps=[{"ev_fcf": 20.0}, {"ev_fcf": 24.0}],  # median 22, not the 12x static table
    )
    result = _value_mature_compounder(d)
    assert result["key_metrics"]["used_peer_ev_fcf"] is True
    assert result["secondary"][0]["assumptions"]["ev_fcf_multiple"] == 22.0


def test_mature_compounder_falls_back_to_sector_table_without_peers():
    d = _dossier(
        profile={"sector": "Energy", "yf_sector": "Energy"},
        financials={"ratios_ttm": {"shares_out": 1_000_000_000, "roic": 15.0, "fcf": 2_000_000_000},
                   "balance": [{"total_debt": 0, "cash": 0}]},
        valuation={"dcf_iv_per_share": None},
    )
    result = _value_mature_compounder(d)
    assert result["key_metrics"]["used_peer_ev_fcf"] is False
    assert result["secondary"][0]["assumptions"]["ev_fcf_multiple"] == 12  # Energy static tier


def test_cyclical_uses_peer_pe_and_ev_ic():
    d = _dossier(
        profile={"sector": "Industrials", "yf_sector": "Industrials", "industry": ""},
        financials={
            "ratios_ttm": {"shares_out": 1_000_000_000, "revenue_ttm": 5_000_000_000, "pe": 20.0},
            "income": [{"net_income": 500_000_000}, {"net_income": 480_000_000}],
            "balance": [{"total_assets": 1, "current_liabilities": 1, "total_debt": 1_000_000_000,
                        "cash": 0, "inventory": 100_000_000, "stockholders_equity": 2_000_000_000}],
            "cashflow": [{"capex": -100_000_000}],
        },
        peer_comps=[{"pe": 16.0, "ev_ic": 2.0}, {"pe": 18.0, "ev_ic": 3.0}],
    )
    result = _value_cyclical(d)
    assert result["key_metrics"]["used_peer_pe"] is True
    assert result["key_metrics"]["used_peer_ev_ic"] is True
    assert result["primary"]["assumptions"]["mid_cycle_pe_target"] == 17.0
    assert result["secondary"][0]["assumptions"]["target_ev_ic_multiple"] == 2.5


def test_cyclical_falls_back_to_static_tables_without_peers():
    d = _dossier(
        profile={"sector": "Industrials", "yf_sector": "Industrials", "industry": ""},
        financials={
            "ratios_ttm": {"shares_out": 1_000_000_000, "revenue_ttm": 5_000_000_000, "pe": 20.0},
            "income": [{"net_income": 500_000_000}, {"net_income": 480_000_000}],
            "balance": [{"total_assets": 1, "current_liabilities": 1, "total_debt": 1_000_000_000,
                        "cash": 0, "inventory": 100_000_000, "stockholders_equity": 2_000_000_000}],
            "cashflow": [{"capex": -100_000_000}],
        },
    )
    result = _value_cyclical(d)
    assert result["key_metrics"]["used_peer_pe"] is False
    assert result["primary"]["assumptions"]["mid_cycle_pe_target"] == 16  # Industrials static tier
    assert result["secondary"][0]["assumptions"]["target_ev_ic_multiple"] == 1.5  # flat static


def test_financial_uses_peer_price_to_book():
    d = _dossier(
        financials={
            "ratios_ttm": {"shares_out": 1_000_000_000, "roe": 20.0},
            "balance": [{"stockholders_equity": 5_000_000_000, "goodwill": 0, "intangible_assets": 0}],
        },
        macro={"yield_curve_spread": 0.5},
        peer_comps=[{"price_to_book": 1.0}, {"price_to_book": 1.4}],
    )
    result = _value_financial(d)
    assert result["key_metrics"]["used_peer_price_to_book"] is True
    assert result["primary"]["assumptions"]["fair_ptbv_target"] == 1.2
    assert any("PTBV_PROXY_IS_PB" in b for b in result["blind_spots"])


def test_financial_falls_back_to_roe_tiered_table_without_peers():
    d = _dossier(
        financials={
            "ratios_ttm": {"shares_out": 1_000_000_000, "roe": 20.0},  # >= 15 -> 1.8x tier
            "balance": [{"stockholders_equity": 5_000_000_000, "goodwill": 0, "intangible_assets": 0}],
        },
        macro={"yield_curve_spread": 0.5},
    )
    result = _value_financial(d)
    assert result["key_metrics"]["used_peer_price_to_book"] is False
    assert result["primary"]["assumptions"]["fair_ptbv_target"] == 1.8
    assert not any("PTBV_PROXY_IS_PB" in b for b in result["blind_spots"])


def test_infrastructure_uses_peer_ev_ebitda_for_secondary_only():
    d = _dossier(
        profile={"industry": "REIT"},
        financials={
            "ratios_ttm": {"shares_out": 1_000_000_000, "ev_ebitda": 17.0, "ebitda": 1_000_000_000},
            "balance": [{"total_debt": 2_000_000_000, "cash": 0}],
            "cashflow": [{"operating_cf": 800_000_000, "capex": -200_000_000}],
        },
        peer_comps=[{"ev_ebitda": 19.0}, {"ev_ebitda": 21.0}],
    )
    result = _value_infrastructure(d)
    assert result["key_metrics"]["used_peer_ev_ebitda"] is True
    assert result["secondary"][0]["assumptions"]["target_ev_multiple"] == 20.0
    # Primary (P/AFFO) is explicitly disclosed as still static — not silently
    # left inconsistent with the now-peer-anchored secondary.
    assert any("PRIMARY_METHOD_STILL_STATIC" in b for b in result["blind_spots"])


def test_infrastructure_falls_back_to_industry_table_without_peers():
    d = _dossier(
        profile={"industry": "REIT"},
        financials={
            "ratios_ttm": {"shares_out": 1_000_000_000, "ev_ebitda": 17.0, "ebitda": 1_000_000_000},
            "balance": [{"total_debt": 2_000_000_000, "cash": 0}],
            "cashflow": [{"operating_cf": 800_000_000, "capex": -200_000_000}],
        },
    )
    result = _value_infrastructure(d)
    assert result["key_metrics"]["used_peer_ev_ebitda"] is False
    assert result["secondary"][0]["assumptions"]["target_ev_multiple"] == 20  # REIT static tier


def test_early_stage_uses_peer_ev_sales_with_net_debt_subtracted():
    d = _dossier(
        profile={"sector": "Technology", "market_cap_bn": 2.0},
        financials={
            "ratios_ttm": {"shares_out": 100_000_000, "revenue_ttm": 200_000_000},
            "income": [{"revenue": 200_000_000}],
            "balance": [{"cash": 100_000_000, "total_debt": 50_000_000}],
            "cashflow": [{"free_cash_flow": -50_000_000}],
        },
        peer_comps=[{"ev_sales": 10.0}, {"ev_sales": 14.0}],
    )
    result = _value_early_stage(d)
    assert result["key_metrics"]["used_peer_ev_sales"] is True
    assert result["secondary"][0]["assumptions"]["target_ev_rev"] == 12.0  # median of 10/14
    # Fixed 2026-07-13: EV/Revenue must subtract net debt to reach equity
    # value — (12*200M - (50M-100M)) / 100M = (2400M + 50M)/100M = $24.50/sh.
    assert result["secondary"][0]["fair_value"] == pytest.approx(24.5, abs=0.01)


def test_early_stage_falls_back_to_sector_table_without_peers():
    d = _dossier(
        profile={"sector": "Technology", "market_cap_bn": 2.0},
        financials={
            "ratios_ttm": {"shares_out": 100_000_000, "revenue_ttm": 200_000_000},
            "income": [{"revenue": 200_000_000}],
            "balance": [{"cash": 100_000_000, "total_debt": 50_000_000}],
            "cashflow": [{"free_cash_flow": -50_000_000}],
        },
    )
    result = _value_early_stage(d)
    assert result["key_metrics"]["used_peer_ev_sales"] is False
    assert result["secondary"][0]["assumptions"]["target_ev_rev"] == 8  # Technology static tier


# ── hypergrowth leg source-labelling (2026-07-14, visibility only) ──────────
# The static 25x/40x EV/NTM-Revenue override is the last unreformed static-
# multiple path post-v5 and takes 70-100% composite weight when active — the
# label rides fair_value_key_metrics into every agent's prompt. Deliberately
# NOT a blind_spot flag (risk_reward penalizes >=2 flags; asset-light already
# carries NRR_NOT_COMPUTED, so a flag would silently change R/R).

def test_hypergrowth_leg_is_source_labelled_when_active():
    d = _dossier(financials={"ratios_ttm": {
        "shares_out": 1_000_000_000, "revenue_ttm": 10_000_000_000,
        "gross_margin": 72.0, "fcf": 2_000_000_000,
        "fwd_revenue_growth": 0.60,
    }})
    fv = compute_fair_values(d)
    km = fv["archetype_metrics"]
    assert km["hypergrowth_active"] is True
    assert km["hypergrowth_ntm_multiple"] == 25          # 0.5 < growth <= 1.0 tier
    assert km["hypergrowth_multiple_source"] == "static_2024_2026_ai_cycle_calibration"
    # Positive SBC-adjusted FCF + a primary leg -> the 70/30 blend.
    assert km["hypergrowth_composite_weight"] == 0.70
    # Visibility only — must NOT have added a blind_spot flag (risk_reward
    # penalizes >=2 flags and asset-light already carries NRR_NOT_COMPUTED).
    assert not any("HYPERGROWTH" in f for f in fv["blind_spot_flags"])


def test_hypergrowth_labels_absent_when_inactive():
    d = _dossier()   # base fixture: fwd_revenue_growth 0.15 — below the cutoff
    fv = compute_fair_values(d)
    km = fv["archetype_metrics"]
    assert km["hypergrowth_active"] is False
    assert km["hypergrowth_ntm_multiple"] is None
    assert km["hypergrowth_multiple_source"] is None
    assert km["hypergrowth_composite_weight"] is None
