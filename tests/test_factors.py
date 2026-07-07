"""Tests for the evidence-factor primitives: canonical price momentum
(dossier._price_momentum) and the quality composite (dossier.quality_composite).

The momentum math guards matter: mom_12_1 is the Jegadeesh-Titman 12-1 measure
(12 months ago → 1 month ago, skipping the reversal-prone last month). A recent
IPO must yield None, never a fabricated number — the outcome analysis buckets
on these values.
"""

import pandas as pd

from dossier import _price_momentum, quality_composite


def _series(n: int, anchors: dict[int, float], fill: float = 100.0) -> pd.Series:
    vals = [fill] * n
    for idx, v in anchors.items():
        vals[idx] = v
    return pd.Series(vals)


# ── momentum ──────────────────────────────────────────────────────────────────

def test_momentum_full_year_series():
    # n=252: p12 = iloc[0] = 100, p1m (iloc[-21] = idx 231) = 130, last = 120,
    # p6m (iloc[-126] = idx 126) = 110
    close = _series(252, {0: 100.0, 126: 110.0, 231: 130.0, 251: 120.0})
    m = _price_momentum(close)
    assert m["mom_12_1"] == round((130.0 - 100.0) / 100.0, 4)   # 0.30, excludes last month
    assert m["mom_6m"] == round((120.0 - 110.0) / 110.0, 4)
    assert m["mom_1m"] == round((120.0 - 130.0) / 130.0, 4)     # negative = recent pullback


def test_momentum_short_calendar_year_tolerated():
    # 245 trading days still counts as ~a year (uses the oldest close as the 12m anchor)
    close = _series(245, {0: 100.0, 224: 150.0, 244: 140.0})    # iloc[-21] = idx 224
    m = _price_momentum(close)
    assert m["mom_12_1"] == round((150.0 - 100.0) / 100.0, 4)


def test_momentum_recent_ipo_yields_none_not_fabrication():
    m = _price_momentum(_series(100, {99: 120.0}))
    assert m["mom_12_1"] is None          # < ~1yr of history
    assert m["mom_6m"] is None            # < 126 rows
    assert m["mom_1m"] is not None        # 1-month is computable

    tiny = _price_momentum(_series(10, {}))
    assert tiny == {"mom_12_1": None, "mom_6m": None, "mom_1m": None}


def test_momentum_zero_price_guard():
    close = _series(252, {0: 0.0})        # corrupt anchor price
    assert _price_momentum(close)["mom_12_1"] is None


# ── quality composite ─────────────────────────────────────────────────────────

def test_quality_elite_profile_maxes_out():
    q = quality_composite({"roic": 30.0, "gross_margin": 85.0,
                           "fcf_yield": 0.09, "debt_equity": 0.0})
    assert q == 10.0


def test_quality_junk_profile_scores_low():
    q = quality_composite({"roic": -5.0, "gross_margin": 10.0,
                           "fcf_yield": -0.02, "debt_equity": 600.0})
    assert q is not None and q < 1.0


def test_quality_partial_inputs():
    # roic 15 → 6.0, fcf_yield 0.04 → 5.0 → mean 5.5
    assert quality_composite({"roic": 15.0, "fcf_yield": 0.04}) == 5.5


def test_quality_none_when_data_starved():
    assert quality_composite({}) is None
    assert quality_composite({"roic": 20.0}) is None       # 1 component isn't a composite
    assert quality_composite({"roic": None, "gross_margin": None}) is None


def test_quality_bounded_zero_to_ten():
    q = quality_composite({"roic": 1000.0, "gross_margin": 100.0,
                           "fcf_yield": 5.0, "debt_equity": -50.0})
    assert q is not None and 0.0 <= q <= 10.0


def test_factor_stamp_version_matches_scoreboard_register():
    """ADAPTATION_PROTOCOL rule 5 says factors.v and signal_analysis's
    _VERSION_REGISTER move in the same commit — previously enforced only by a
    comment. This test makes the drift impossible to ship."""
    import signal_analysis as sa
    from upload_kv import _factor_stamp
    stamped = _factor_stamp({"technicals": {}, "financials": {}}, {})["v"]
    latest_registered = max(k for k in sa._VERSION_REGISTER if isinstance(k, int))
    assert stamped == latest_registered, (
        f"upload_kv stamps v{stamped} but the register's latest is v{latest_registered} — "
        "bump both in the same commit (ADAPTATION_PROTOCOL rule 5)")
