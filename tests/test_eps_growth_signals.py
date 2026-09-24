"""v6 (2026-09-24): truthful EPS-growth inputs to the debate.

`ratios_ttm.implied_ntm_growth` was computed as (forwardEps - trailingEps) /
|trailingEps| with yfinance `trailingEps` (GAAP TTM) and `forwardEps` — which
live-verified 2026-09-24 EQUALS the earnings_estimate "+1y" row, i.e. the NEXT
FISCAL YEAR consensus, not next-12-months. The figure therefore spans up to ~18
months of growth and mixes GAAP trailing with non-GAAP forward, so it inflates
with the growth rate: ANET read +64% "NTM growth" against a like-for-like
FY26->FY27 consensus of +26% (revenue +28% — perfectly coupled). The v4 prompt
then called any "implied_ntm_growth towering over fwd_revenue_growth" a
base-effect trap "regardless", and `eps_acceleration` (= fwd growth MINUS that
figure) was described to ValuationEngine as analysts "cutting consensus ... red
flag, downgrade conviction". Across 54 stored dossiers the artifact flagged 63%,
carried ZERO predictive value (Spearman vs 4-week excess -0.05), yet was cited as
bear evidence in 8 of the 12 most recent builds and was ANET's swing factor.

v6 replaces both with honest, like-for-like quantities:
  - eps_growth_ttm_to_next_fy — the same TTM -> next-FY bridge, NAMED for what it
    is and kept only for the recovery-from-depressed-base check; None when the
    trailing base is non-positive or the ratio is absurd (tiny/FX-mismatched
    denominators produced KSPI +60,972%, TLN +6,940%, HPE +1,707%).
  - eps_acceleration — next-FY consensus EPS growth minus current-FY consensus
    EPS growth, both from the SAME Yahoo earnings_estimate frame: genuine
    acceleration/deceleration of the growth rate, not a revision signal.
"""

import math

import pandas as pd

from dossier import _eps_growth_signals, _parse_estimates


# ── eps_growth_ttm_to_next_fy: the recovery bridge, honestly named ─────────

def test_bridge_is_the_trailing_to_next_fy_ratio():
    # ANET live 2026-09-24: trailingEps 3.16, forwardEps (= +1y) 5.18742.
    s = _eps_growth_signals(3.16, 5.18742, cur_fy_growth=0.52, next_fy_growth=0.26)
    assert math.isclose(s["eps_growth_ttm_to_next_fy"], (5.18742 - 3.16) / 3.16, rel_tol=1e-9)


def test_bridge_none_when_trailing_base_non_positive():
    # A loss-making or zero trailing base has no meaningful growth ratio.
    assert _eps_growth_signals(0.0, 2.0, 0.1, 0.1)["eps_growth_ttm_to_next_fy"] is None
    assert _eps_growth_signals(-1.5, 2.0, 0.1, 0.1)["eps_growth_ttm_to_next_fy"] is None


def test_bridge_none_when_absurd():
    # KSPI read +60,972% live — a tiny or FX-mismatched denominator, not growth.
    assert _eps_growth_signals(0.01, 6.1, 0.1, 0.1)["eps_growth_ttm_to_next_fy"] is None
    # A genuine but large recovery (depressed base) is still reported.
    assert math.isclose(_eps_growth_signals(0.5, 2.0, 0.1, 0.1)["eps_growth_ttm_to_next_fy"], 3.0)


def test_bridge_none_when_forward_missing():
    assert _eps_growth_signals(3.0, None, 0.1, 0.1)["eps_growth_ttm_to_next_fy"] is None
    assert _eps_growth_signals(None, 5.0, 0.1, 0.1)["eps_growth_ttm_to_next_fy"] is None


# ── eps_acceleration: real acceleration, same frame, same basis ────────────

def test_acceleration_is_next_fy_growth_minus_current_fy_growth():
    s = _eps_growth_signals(3.16, 5.19, cur_fy_growth=0.30, next_fy_growth=0.45)
    assert math.isclose(s["eps_acceleration"], 0.15)


def test_acceleration_ignores_the_trailing_bridge():
    """The old definition subtracted the TTM->next-FY bridge, so ANET read -0.38.
    A fast grower with a steady growth rate must read ~0, whatever the bridge."""
    s = _eps_growth_signals(3.16, 5.19, cur_fy_growth=0.26, next_fy_growth=0.26)
    assert math.isclose(s["eps_acceleration"], 0.0, abs_tol=1e-12)


def test_acceleration_none_when_current_year_is_itself_a_base_effect():
    """MU live 2026-09-24: current-FY EPS growth +787% (recovery from a depressed
    base) -> next-FY +116% read as acceleration -671%. Arithmetically true,
    informationally empty — a 'slowdown' from a recovery year says nothing about
    the growth rate. Beyond +100% current-FY growth, leave it to the recovery check."""
    assert _eps_growth_signals(44.27, 159.12, 7.87, 1.16)["eps_acceleration"] is None
    assert _eps_growth_signals(1.0, 2.0, -2.5, 0.3)["eps_acceleration"] is None


def test_acceleration_kept_for_a_genuine_hypergrowth_slowdown():
    # NVDA live: +95% -> +68% is a real deceleration and must still be reported.
    s = _eps_growth_signals(7.92, 15.68, 0.95, 0.68)
    assert math.isclose(s["eps_acceleration"], -0.27, abs_tol=1e-9)


def test_acceleration_none_when_either_growth_missing():
    assert _eps_growth_signals(3.0, 4.0, None, 0.2)["eps_acceleration"] is None
    assert _eps_growth_signals(3.0, 4.0, 0.2, None)["eps_acceleration"] is None


def test_non_finite_and_junk_inputs_never_raise():
    nan = float("nan")
    for args in ((nan, nan, nan, nan), ("x", {}, [], object()), (None, None, None, None),
                 (float("inf"), 5.0, 0.1, float("-inf"))):
        s = _eps_growth_signals(*args)
        assert set(s) == {"eps_growth_ttm_to_next_fy", "eps_acceleration"}
        assert all(v is None or math.isfinite(v) for v in s.values())


# ── _parse_estimates now surfaces the current-FY growth it needs ───────────

def _ee():
    return pd.DataFrame({
        "0q":  {"avg": 2.0, "low": 1.8, "high": 2.2, "numberOfAnalysts": 30, "growth": 0.10},
        "+1q": {"avg": 2.5, "low": 2.0, "high": 3.0, "numberOfAnalysts": 29, "growth": 0.20},
        "0y":  {"avg": 8.0, "low": 7.5, "high": 8.5, "numberOfAnalysts": 40, "growth": 0.30},
        "+1y": {"avg": 10.0, "low": 7.0, "high": 15.0, "numberOfAnalysts": 42, "growth": 0.25},
    }).T


def test_parse_estimates_exposes_current_fy_eps_and_growth():
    est = _parse_estimates(_ee(), None, None)
    assert est["est_eps_cur_fy"] == 8.0
    assert est["est_eps_cur_fy_growth"] == 0.30
    assert est["fwd_eps_growth"] == 0.25   # the +1y growth, unchanged
