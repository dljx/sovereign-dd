"""dossier._adr_share_basis — ADR/foreign share-count correction.

yfinance mixes UNDERLYING share counts with ADR-level prices for foreign
listings, so per-share math and market cap need a corrected share basis.
The original detector treated a bare `sharesOutstanding / floatShares > 2`
ratio as sufficient evidence of that mixing. It is not: a legitimately
low-float DOMESTIC company (recent IPO, controlled subsidiary, dual-class)
trips the same ratio while its share count and price are already in the same
units. Found live 2026-08-27 — XZO (Exzeo Group, 82.6% owned by HCI Group,
ratio 7.8x) had its market cap computed as price x FLOAT: $195M instead of
$1.52B. That understatement manufactured a false "cash-box" thesis (market
cap below cash) which scored a 7.79 BUY / BANGER / ENTER_NOW. BAM (Brookfield
Asset Management, ~73% held by Brookfield Corp, ratio 4.9x) was corrupted the
same way: $83B shown as ~$17B.

The ratio is only meaningful when the listing is ACTUALLY foreign — an
explicit ADR quoteType, or financials reported in a non-USD currency while
the price is USD. A US-domiciled, USD-reporting company can never have the
units mismatch the ratio is a proxy for.
"""

from dossier import _adr_share_basis


# ── the regression: domestic low-float names must NOT be treated as ADRs ──

def test_domestic_low_float_ipo_is_not_an_adr():
    """XZO shape (live 2026-08-27): 7.8x ratio, but USD/USD EQUITY."""
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 90_085_918,
        "floatShares": 11_548_114,
        "quoteType": "EQUITY",
        "currency": "USD",
        "financialCurrency": "USD",
    })
    assert mismatch is False
    # Must keep TOTAL shares — using float here is what produced $195M.
    assert shares == 90_085_918


def test_controlled_subsidiary_low_float_is_not_an_adr():
    """BAM shape (live 2026-08-27): 4.9x ratio, USD/USD."""
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 1_597_230_353,
        "floatShares": 324_385_834,
        "quoteType": "EQUITY",
        "currency": "USD",
        "financialCurrency": "USD",
    })
    assert mismatch is False
    assert shares == 1_597_230_353


def test_domestic_normal_float_unaffected():
    """AER shape: ratio ~1.1 — was ALSO a false positive (nulled its P/B, P/S)."""
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 157_685_101,
        "floatShares": 143_350_092,
        "quoteType": "EQUITY",
        "currency": "USD",
        "financialCurrency": "USD",
    })
    assert mismatch is False
    assert shares == 157_685_101


# ── real ADRs must keep working exactly as before ─────────────────────────

def test_fx_mismatch_adr_uses_float_shares():
    """TSM shape: NYSE-listed, priced in USD, financials in TWD, ratio > 2.
    This is the case the ratio heuristic exists for — it must still fire."""
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 25_930_380_000,
        "floatShares": 5_186_076_000,
        "quoteType": "EQUITY",
        "currency": "USD",
        "financialCurrency": "TWD",
    })
    assert mismatch is True
    assert shares == 5_186_076_000


def test_explicit_adr_quotetype_is_flagged():
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 4_000_000_000,
        "floatShares": 500_000_000,
        "quoteType": "ADR",
        "currency": "USD",
        "financialCurrency": "USD",
    })
    assert mismatch is True
    assert shares == 500_000_000


def test_fx_mismatch_without_ratio_flags_but_keeps_shares_out():
    """A foreign-currency reporter whose share counts already pair with the
    USD price: flag it (P/B, P/S are unreliable) but do NOT swap in float."""
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 1_000_000_000,
        "floatShares": 900_000_000,
        "quoteType": "EQUITY",
        "currency": "USD",
        "financialCurrency": "EUR",
    })
    assert mismatch is True
    assert shares == 1_000_000_000


# ── missing / degenerate data ─────────────────────────────────────────────

def test_missing_float_is_safe():
    mismatch, shares = _adr_share_basis({
        "sharesOutstanding": 50_000_000, "quoteType": "EQUITY",
        "currency": "USD", "financialCurrency": "USD",
    })
    assert mismatch is False
    assert shares == 50_000_000


def test_empty_info_does_not_crash():
    mismatch, shares = _adr_share_basis({})
    assert mismatch is False
    assert shares == 0
