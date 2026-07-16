"""dossier._parse_estimates (2026-07-17) — the Yahoo analyst-estimate block,
refactored pure so its column semantics are testable, + NEW consensus-tightness
fields: eps/rev_est_dispersion_pct = (high−low)/|avg| on the +1y row. Columns
basis-verified live 2026-07-17 (MU/NVDA/PTC): the frames DO carry avg/low/high/
numberOfAnalysts/growth. Regression guard: the refactor must not change any
legacy field the fair-value chain consumes (fwd_eps_ntm, growths, momentum)."""

import pandas as pd
import pytest

from dossier import _parse_estimates


def _ee(rows=None):
    """earnings_estimate fixture in the live shape (rows 0q/+1q/0y/+1y)."""
    base = {
        "0q":  {"avg": 2.0, "low": 1.8, "high": 2.2, "numberOfAnalysts": 30, "growth": 0.10},
        "+1q": {"avg": 2.5, "low": 2.0, "high": 3.0, "numberOfAnalysts": 29, "growth": 0.20},
        "0y":  {"avg": 8.0, "low": 7.5, "high": 8.5, "numberOfAnalysts": 40, "growth": 0.30},
        "+1y": {"avg": 10.0, "low": 7.0, "high": 15.0, "numberOfAnalysts": 42, "growth": 0.25},
    }
    if rows:
        for k, v in rows.items():
            base[k].update(v)
    return pd.DataFrame(base).T


def _re():
    return pd.DataFrame({
        "+1y": {"avg": 200.0, "low": 150.0, "high": 260.0, "numberOfAnalysts": 35, "growth": 0.40},
    }).T


def _et(cur=10.5, ago30=10.0):
    return pd.DataFrame({"+1y": {"current": cur, "30daysAgo": ago30}}).T


def test_legacy_fields_unchanged_by_refactor():
    est = _parse_estimates(_ee(), _re(), _et())
    assert est["fwd_eps_ntm"] == 10.0
    assert est["fwd_eps_growth"] == 0.25
    assert est["est_eps_current_q"] == 2.0
    assert est["est_eps_next_q_growth"] == 0.20
    assert est["fwd_rev_growth"] == pytest.approx(0.40)
    assert est["eps_revision_momentum"] == pytest.approx(0.05)


def test_eps_dispersion_exact():
    est = _parse_estimates(_ee(), _re(), _et())
    # (15 − 7) / 10 = 80%
    assert est["eps_est_dispersion_pct"] == pytest.approx(80.0)
    assert est["fwd_eps_ntm_low"] == 7.0
    assert est["fwd_eps_ntm_high"] == 15.0
    assert est["num_analysts_eps_yf"] == 42


def test_rev_dispersion_exact():
    est = _parse_estimates(_ee(), _re(), _et())
    # (260 − 150) / 200 = 55%
    assert est["rev_est_dispersion_pct"] == pytest.approx(55.0)


def test_nan_low_high_omits_dispersion_but_keeps_legacy():
    est = _parse_estimates(_ee({"+1y": {"low": float("nan")}}), _re(), _et())
    assert "eps_est_dispersion_pct" not in est
    assert "fwd_eps_ntm_low" not in est
    assert est["fwd_eps_ntm"] == 10.0  # legacy consensus untouched


def test_missing_low_high_columns_tolerated():
    ee = pd.DataFrame({"+1y": {"avg": 10.0, "growth": 0.25}}).T
    est = _parse_estimates(ee, None, None)
    assert est["fwd_eps_ntm"] == 10.0
    assert "eps_est_dispersion_pct" not in est


def test_none_and_empty_frames_yield_empty_dict():
    assert _parse_estimates(None, None, None) == {}
    assert _parse_estimates(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()) == {}


def test_zero_avg_never_divides():
    est = _parse_estimates(_ee({"+1y": {"avg": 0.0}}), _re(), _et())
    assert "eps_est_dispersion_pct" not in est
