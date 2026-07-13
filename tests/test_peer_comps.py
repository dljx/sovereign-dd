"""dossier._fetch_peer / _usable_peer / _better_peer_set — the peer-derived
multiples that fair_value.py's 2026-07-13 recalibration runs on. Locks the
quality gate found live on ETN: Finnhub's /stock/peers suggested ADSE/HTOO
(2 tickers — passed the old length-only check) but neither carried a single
usable valuation field, silently starving the peer-median recalibration on
a real portfolio holding. Fixed with a rule-based (not hand-picked-tickers)
quality gate: retry with the curated SECTOR_PEERS list when the Finnhub-
suggested set is thin on actual data."""

from dossier import _usable_peer, _better_peer_set


def _peer(ticker, **fields):
    base = {"ticker": ticker, "pe": 0, "fwd_pe": 0, "ev_ebitda": 0,
            "rev_growth": 0, "gross_margin": 0, "ev_fcf": None, "ev_sales": None,
            "ev_ic": None, "price_to_book": None}
    base.update(fields)
    return base


# ── _usable_peer ─────────────────────────────────────────────────────────────

def test_usable_peer_true_when_any_valuation_field_present():
    assert _usable_peer(_peer("X", ev_fcf=20.0)) is True
    assert _usable_peer(_peer("X", ev_sales=5.0)) is True
    assert _usable_peer(_peer("X", price_to_book=2.5)) is True


def test_usable_peer_false_when_all_valuation_fields_none():
    # The exact live shape of Finnhub's ETN-suggested ADSE/HTOO peers.
    assert _usable_peer(_peer("ADSE")) is False


def test_usable_peer_false_for_none_or_empty():
    assert _usable_peer(None) is False
    assert _usable_peer({}) is False


def test_usable_peer_ignores_zero_pe_and_ev_ebitda_placeholders():
    # pe/ev_ebitda default to 0 (not None) elsewhere in _fetch_peer's output
    # for historical reasons — a bare 0 must not count as "usable" (it's the
    # missing-data placeholder, not a real zero multiple). ev_ebitda IS one of
    # the checked keys, so a genuine positive value there DOES count.
    assert _usable_peer(_peer("X", ev_ebitda=0)) is False
    assert _usable_peer(_peer("X", ev_ebitda=15.0)) is True


# ── _better_peer_set ─────────────────────────────────────────────────────────

def test_better_peer_set_prefers_fallback_when_strictly_better():
    primary = [_peer("ADSE"), _peer("HTOO")]                       # 0 usable
    fallback = [_peer("GE", ev_fcf=68.1), _peer("HON", ev_fcf=33.4)]  # 2 usable
    assert _better_peer_set(primary, fallback) == fallback


def test_better_peer_set_keeps_primary_when_fallback_not_better():
    primary = [_peer("A", ev_fcf=10.0), _peer("B", ev_fcf=12.0)]   # 2 usable
    fallback = [_peer("C")]                                        # 0 usable
    assert _better_peer_set(primary, fallback) == primary


def test_better_peer_set_keeps_primary_on_tie():
    primary = [_peer("A", ev_fcf=10.0)]
    fallback = [_peer("B", ev_sales=5.0)]
    # Equal usable counts (1 vs 1) — must not swap on a tie ("never assumes
    # the fallback is automatically right").
    assert _better_peer_set(primary, fallback) == primary
