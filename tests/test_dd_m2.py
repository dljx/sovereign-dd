"""Tests for M2 refactors: grade ladder, scoring gates, valuation smoke."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from grading import grade, BUY_THRESHOLD
import scoring
import fair_value


# ── grade ladder (single source of truth) ───────────────────────────────────

@pytest.mark.parametrize("score,label", [
    (9.5, "CONVICTION BUY"), (9.0, "CONVICTION BUY"),
    (8.0, "STRONG BUY"), (6.5, "BUY"), (5.0, "HOLD"),
    (3.5, "SELL"), (2.0, "STRONG SELL"), (1.0, "AVOID"), (0.0, "AVOID"),
])
def test_grade_ladder(score, label):
    assert grade(score) == label

def test_buy_threshold_value():
    assert BUY_THRESHOLD == 7.0
    # The single source of truth is re-exported everywhere.
    from scout import BUY_THRESHOLD as s
    from gems import BUY_THRESHOLD as g
    assert s == g == BUY_THRESHOLD


# ── consensus gap: removed 2026-07-03 (analyst targets lack predictive power) ─

def test_consensus_gap_adjust_is_gone():
    """The ±0.3 analyst-target adjustment was removed — its return would be a
    regression (see docs/METHODOLOGY_REVIEW.md)."""
    assert not hasattr(scoring, "consensus_gap_adjust")


# ── valuation engine smoke (no exceptions; sane composite) ───────────────────

def _financial_dossier(roe):
    return {
        "financials": {
            "ratios_ttm": {"roe": roe, "shares_out": 10.0},
            "balance": [{"stockholders_equity": 100.0, "goodwill": 0,
                         "intangible_assets": 0, "total_debt": 0, "cash": 0}],
            "income": [{}], "cashflow": [{}],
        },
        "macro": {"yield_curve_spread": 1.0},
        "profile": {"sector": "Financial Services", "industry": "Banks"},
        "quote": {"price": 12.0},
    }

def test_classify_archetype_financial():
    info = fair_value.classify_archetype(_financial_dossier(16.0))
    assert isinstance(info, dict) and info.get("archetype")

def test_compute_fair_values_does_not_raise():
    out = fair_value.compute_fair_values(_financial_dossier(16.0))
    assert isinstance(out, dict)
    assert "composite_fair_value" in out
    assert out.get("valuation_failed") is not True
