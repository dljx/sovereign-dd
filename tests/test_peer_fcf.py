"""dossier._fetch_peer FCF basis (2026-07-14) — completes the 2026-07-13
ratios_ttm.fcf fix on the PEER side of the comps. The subject's FCF moved to
a genuine last-4-quarters TTM sum, but the peer EV/FCF multiples it gets
multiplied by were still built on info['freeCashflow'] — live-measured to
inflate the peer median +110% on a semis set (AVGO/AMD/TXN/QCOM/MU: e.g. MU
136.0x info-basis vs 39.7x real-TTM) and +25% on industrials. Net effect: a
truthful subject FCF x an inflated peer multiple = systematically overstated
fair values (live NVDA composite $471 / MoS +57% on a $203 price, vs ~$230
on the truthful median). The denominator basis of the multiple must match
the subject FCF's basis or the comp is apples-to-oranges."""

import pandas as pd
import pytest

import dossier
from dossier import _fetch_peer


class _FakePeerTicker:
    def __init__(self, info, qcf=None):
        self.info = info
        self._qcf = qcf

    @property
    def quarterly_cashflow(self):
        return self._qcf


def _qcf(ocf_vals, capex_vals):
    cols = pd.date_range("2026-01-31", periods=4, freq="-3ME")
    return pd.DataFrame({
        "Operating Cash Flow":  ocf_vals,
        "Capital Expenditure":  capex_vals,
    }, index=cols).T


_INFO = {
    "enterpriseValue":     1_000_000,
    "freeCashflow":        10_000,      # broken basis → EV/FCF would read 100.0x
    "totalRevenue":        500_000,
    "totalDebt":           0,
    "bookValue":           10.0,
    "sharesOutstanding":   1_000,
    "priceToBook":         3.0,
    "trailingPE":          20.0,
    "forwardPE":           18.0,
    "enterpriseToEbitda":  12.0,
    "revenueGrowth":       0.10,
    "grossMargins":        0.50,
}


def test_peer_ev_fcf_uses_real_ttm_when_4_quarters_exist(monkeypatch):
    # Real TTM = 4x10k OCF - 4x2.5k capex = 30k → EV/FCF 33.3x, NOT the
    # info-dict 100.0x — the 3x-scale divergence measured live on MU.
    t = _FakePeerTicker(_INFO, _qcf([10_000.0] * 4, [-2_500.0] * 4))
    monkeypatch.setattr(dossier.yf, "Ticker", lambda s: t)
    p = _fetch_peer("FAKE")
    assert p["ev_fcf"] == pytest.approx(1_000_000 / 30_000, abs=0.1)
    assert p["fcf_basis"] == "ttm_quarterly_sum"


def test_peer_falls_back_to_info_dict_when_quarters_thin(monkeypatch):
    # <4 real quarters (recent IPO) → same info-dict fallback the subject
    # side uses, honestly labelled.
    t = _FakePeerTicker(_INFO, None)
    monkeypatch.setattr(dossier.yf, "Ticker", lambda s: t)
    p = _fetch_peer("FAKE")
    assert p["ev_fcf"] == pytest.approx(100.0)
    assert p["fcf_basis"] == "info_dict_fallback"


def test_peer_negative_real_ttm_kills_ev_fcf_instead_of_reviving_via_fallback(monkeypatch):
    # A REAL negative TTM must not silently fall back to a stale positive
    # info-dict figure — negative FCF means EV/FCF is meaningless (None),
    # and _peer_median already filters None out of the sample.
    t = _FakePeerTicker(_INFO, _qcf([1_000.0] * 4, [-50_000.0] * 4))
    monkeypatch.setattr(dossier.yf, "Ticker", lambda s: t)
    p = _fetch_peer("FAKE")
    assert p["ev_fcf"] is None
    assert p["fcf_basis"] == "ttm_quarterly_sum"


def test_other_peer_multiples_unaffected_by_fcf_basis(monkeypatch):
    # ev_sales / ev_ic / price_to_book are built from statement-adjacent info
    # fields that were never implicated — the fix must not disturb them.
    t = _FakePeerTicker(_INFO, _qcf([10_000.0] * 4, [-2_500.0] * 4))
    monkeypatch.setattr(dossier.yf, "Ticker", lambda s: t)
    p = _fetch_peer("FAKE")
    assert p["ev_sales"] == pytest.approx(2.0)
    assert p["ev_ic"] == pytest.approx(100.0)      # EV 1M / (0 debt + 10x1000 book)
    assert p["price_to_book"] == pytest.approx(3.0)
    assert p["ticker"] == "FAKE"
