"""dossier._market_cap_sanity — cross-source market-cap corroboration.

The 2026-08-27 XZO incident passed EVERY existing guardrail (red-team
verification CONFIRM 9.2, BANGER conditions, the confirmation gate) because the
reasoning was sound and only the INPUT was wrong: market cap was computed as
price x FLOAT, $195M instead of $1.52B. Both independent sources were already
fetched on that same build and both said ~$1.4-1.5B.

This guard catches that class: when the two independent sources CORROBORATE
each other but the dossier's own computed value diverges from them, the
computation is wrong. When the sources DISAGREE (ADRs, local-currency market
caps) the situation is ambiguous and the guard stays silent rather than crying
wolf.
"""

from dossier import _market_cap_sanity


# ── the regression it exists to catch ─────────────────────────────────────

def test_xzo_float_basis_error_is_caught():
    # Live 2026-08-27: computed $0.19B; Finnhub $1,435.6M; yfinance $1.508B.
    msg = _market_cap_sanity(0.19, 1435.6, 1_508_038_272)
    assert msg is not None
    assert "0.19" in msg and "diverges" in msg


def test_bam_float_basis_error_is_caught():
    msg = _market_cap_sanity(16.95, 83_440.0, 83_440_000_000)
    assert msg is not None


# ── healthy cases stay silent ─────────────────────────────────────────────

def test_agreeing_sources_and_matching_computation_is_silent():
    # AAPL shape: computed matches both sources.
    assert _market_cap_sanity(4631.22, 4_630_000.0, 4_630_880_000_000) is None


def test_small_divergence_within_tolerance_is_silent():
    # 10% off corroborating sources — share-count drift, not a basis error.
    assert _market_cap_sanity(110.0, 100_000.0, 100_000_000_000) is None


def test_disagreeing_sources_stay_silent():
    """ADR shape: Finnhub reports the LOCAL-exchange cap in local currency, so
    the two sources legitimately disagree. Ambiguous -> no warning."""
    assert _market_cap_sanity(500.0, 15_000_000.0, 500_000_000_000) is None


def test_single_source_cannot_corroborate():
    assert _market_cap_sanity(0.19, None, 1_508_038_272) is None
    assert _market_cap_sanity(0.19, 1435.6, None) is None
    assert _market_cap_sanity(0.19, None, None) is None


# ── degenerate / hostile inputs must never raise ──────────────────────────

def test_missing_or_nonpositive_computed_value():
    for bad in (None, 0, -5.0, ""):
        assert _market_cap_sanity(bad, 1435.6, 1_508_038_272) is None


def test_non_finite_inputs_are_ignored():
    nan, inf = float("nan"), float("inf")
    assert _market_cap_sanity(nan, 1435.6, 1_508_038_272) is None
    assert _market_cap_sanity(0.19, nan, inf) is None
    # one good source + one NaN source = only one usable source
    assert _market_cap_sanity(0.19, nan, 1_508_038_272) is None


def test_non_numeric_inputs_are_ignored():
    assert _market_cap_sanity("abc", 1435.6, 1_508_038_272) is None
    assert _market_cap_sanity(0.19, "n/a", {"x": 1}) is None
    assert _market_cap_sanity(0.19, [1], 1_508_038_272) is None


def test_zero_and_negative_sources_are_ignored():
    assert _market_cap_sanity(0.19, 0, 1_508_038_272) is None
    assert _market_cap_sanity(0.19, -100.0, 1_508_038_272) is None


def test_nano_cap_below_floor_is_silent():
    """Sub-$1M values make percentage math pure noise."""
    assert _market_cap_sanity(0.0000001, 0.0001, 100.0) is None


def test_never_raises_on_arbitrary_junk():
    for args in ((None, None, None),
                 (float("-inf"), float("inf"), float("nan")),
                 (object(), object(), object()),
                 (True, False, None)):
        assert _market_cap_sanity(*args) is None
