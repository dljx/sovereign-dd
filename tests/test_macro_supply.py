"""Macro credit-spread supply adds (2026-07-17): hy_credit_spread (ICE BofA US
HY OAS) + yield_curve_10y3m ride into the macro dict as DATA for agents and
synthesis. The load-bearing guarantee under test: _detect_regime is UNTOUCHED —
the regime attribution read (~Jan 2027) must not suffer a methodology break, so
the classifier's output must be identical with and without the new fields."""

from dossier import _detect_regime


_BASE_CASES = [
    # (macro-dict, expected regime) — one per classifier branch that matters
    ({"fed_funds_rate": 5.0, "cpi_yoy": 6.0, "unemployment": 4.0, "vix": 20.0,
      "yield_curve_spread": 0.5}, None),   # expected filled at runtime below
    ({"fed_funds_rate": 5.0, "cpi_yoy": 2.0, "unemployment": 4.0, "vix": 14.0,
      "yield_curve_spread": 1.0}, None),
    ({"fed_funds_rate": 2.0, "cpi_yoy": 2.0, "unemployment": 6.5, "vix": 35.0,
      "yield_curve_spread": -0.8}, None),
    ({}, None),
]


def test_regime_identical_with_and_without_credit_fields():
    for macro, _ in _BASE_CASES:
        before = _detect_regime(dict(macro))
        enriched = {**macro, "hy_credit_spread": 9.99, "yield_curve_10y3m": -1.5}
        after = _detect_regime(enriched)
        assert after == before, f"regime changed by supply-only fields: {macro}"


def test_regime_never_reads_the_new_keys_even_alone():
    # Only the new fields present — must classify exactly like an empty dict.
    assert _detect_regime({"hy_credit_spread": 12.0, "yield_curve_10y3m": -2.0}) \
        == _detect_regime({})
