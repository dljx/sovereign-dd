"""dossier._ttm_fcf_from_quarterly (2026-07-13) — info['freeCashflow'] is an
opaque, unaudited Yahoo figure verified live to diverge 20-70%+ from a real
trailing-twelve-month sum of the last 4 quarterly OCF/Capex statements, with
an outright SIGN FLIP on NEE (-$18.5B info-dict vs a real +$2.4B). This bug
fed BOTH legs of fair_value.py's MATURE_COMPOUNDER archetype (DCF primary +
EV/FCF secondary both read ratios_ttm.fcf), and was traced there after WMT/
KO/TSLA composite fair values landed at 6-10% of price — far worse than the
ASSET_LIGHT_GROWTH miscalibration found earlier the same day. WMT: fixing
this alone moved its composite ratio from 0.094 to 0.223; KO fully resolved
(0.099 -> 0.692, no longer wide-gap-flagged)."""

import pandas as pd
import pytest

from dossier import _ttm_fcf_from_quarterly


class _FakeTicker:
    def __init__(self, qcf=None, raise_on_access=False):
        self._qcf = qcf
        self._raise = raise_on_access

    @property
    def quarterly_cashflow(self):
        if self._raise:
            raise RuntimeError("simulated yfinance network failure")
        return self._qcf


def _qcf(ocf_row, ocf_vals, capex_vals=None, n_cols=4):
    """Build a fake quarterly_cashflow DataFrame, most-recent-quarter-first
    (column order matches yfinance's own convention, relied on elsewhere in
    dossier.py e.g. cf.columns[:2] for the annual statement)."""
    cols = pd.date_range("2026-01-31", periods=n_cols, freq="-3ME")
    data = {ocf_row: (ocf_vals + [None] * n_cols)[:n_cols]}
    if capex_vals is not None:
        data["Capital Expenditure"] = (capex_vals + [None] * n_cols)[:n_cols]
    df = pd.DataFrame(data, index=cols).T
    return df


# ── happy path ────────────────────────────────────────────────────────────

def test_sums_last_4_real_quarters():
    # Real WMT shape, live-verified 2026-07-13: OCF and Capex each sum to the
    # figure independently confirmed against t.cashflow's annual column.
    t = _FakeTicker(_qcf(
        "Operating Cash Flow",
        [4_738_000_000, 14_113_000_000, 9_100_000_000, 12_941_000_000, 5_411_000_000],
        [-6_684_000_000, -8_015_000_000, -7_218_000_000, -6_423_000_000, -4_986_000_000],
    ))
    fcf = _ttm_fcf_from_quarterly(t)
    # Only the first 4 columns (most recent) are used.
    expected = (4_738_000_000 + 14_113_000_000 + 9_100_000_000 + 12_941_000_000) + \
               (-6_684_000_000 - 8_015_000_000 - 7_218_000_000 - 6_423_000_000)
    assert fcf == pytest.approx(expected)
    assert fcf == pytest.approx(12_552_000_000)


def test_falls_back_to_continuing_operating_activities_row():
    t = _FakeTicker(_qcf(
        "Cash Flow From Continuing Operating Activities",
        [1_000, 2_000, 3_000, 4_000],
        [-100, -200, -300, -400],
    ))
    assert _ttm_fcf_from_quarterly(t) == pytest.approx(1000 + 2000 + 3000 + 4000 - 100 - 200 - 300 - 400)


def test_prefers_operating_cash_flow_over_continuing_operations_when_both_present():
    cols = pd.date_range("2026-01-31", periods=4, freq="-3ME")
    df = pd.DataFrame({
        "Operating Cash Flow":                            [10_000] * 4,
        "Cash Flow From Continuing Operating Activities":  [99_999] * 4,
        "Capital Expenditure":                             [-1_000] * 4,
    }, index=cols).T
    t = _FakeTicker(df)
    assert _ttm_fcf_from_quarterly(t) == pytest.approx(10_000 * 4 - 1_000 * 4)


# ── honest None (caller falls back to annual / info-dict) ──────────────────

def test_none_when_fewer_than_4_quarters_available():
    # Recent IPO — only 2 real quarters on record.
    t = _FakeTicker(_qcf("Operating Cash Flow", [1_000, 2_000], [-100, -200], n_cols=2))
    assert _ttm_fcf_from_quarterly(t) is None


def test_none_when_a_quarter_is_nan():
    cols = pd.date_range("2026-01-31", periods=4, freq="-3ME")
    df = pd.DataFrame({
        "Operating Cash Flow":  [1_000, float("nan"), 3_000, 4_000],
        "Capital Expenditure":  [-100, -200, -300, -400],
    }, index=cols).T
    t = _FakeTicker(df)
    assert _ttm_fcf_from_quarterly(t) is None


def test_none_when_capex_row_missing_entirely():
    t = _FakeTicker(_qcf("Operating Cash Flow", [1_000, 2_000, 3_000, 4_000]))
    assert _ttm_fcf_from_quarterly(t) is None


def test_none_when_quarterly_cashflow_is_none():
    t = _FakeTicker(qcf=None)
    assert _ttm_fcf_from_quarterly(t) is None


def test_none_when_quarterly_cashflow_is_empty():
    t = _FakeTicker(qcf=pd.DataFrame())
    assert _ttm_fcf_from_quarterly(t) is None


def test_none_on_exception_not_a_crash():
    t = _FakeTicker(raise_on_access=True)
    assert _ttm_fcf_from_quarterly(t) is None


def test_none_when_neither_ocf_row_name_present():
    cols = pd.date_range("2026-01-31", periods=4, freq="-3ME")
    df = pd.DataFrame({
        "Some Other Row":       [1_000] * 4,
        "Capital Expenditure":  [-100] * 4,
    }, index=cols).T
    t = _FakeTicker(df)
    assert _ttm_fcf_from_quarterly(t) is None


# ── sign-flip regression lock (the NEE case) ────────────────────────────────

def test_handles_a_real_negative_ocf_quarter_correctly_ko_shape():
    # Real KO shape, live-verified 2026-07-13. info['freeCashflow'] read
    # +$3.12B (wrong direction of error) while the true TTM here is +$12.56B
    # — the FY2025 annual figure was dragged down by a one-off -$5.2B Q1-2025
    # OCF quarter that has since rolled out of the trailing-12-month window.
    t = _FakeTicker(_qcf(
        "Operating Cash Flow",
        [2_021_000_000, 3_756_000_000, 5_043_000_000, 3_811_000_000, -5_202_000_000],
        [-266_000_000, -882_000_000, -479_000_000, -442_000_000, -309_000_000],
    ))
    fcf = _ttm_fcf_from_quarterly(t)
    assert fcf == pytest.approx(12_562_000_000)
