"""validator.validate_dossier — cross-source data-quality checks that feed
data_quality.warnings/confidence straight into every agent's round-1 prompt.
No coverage existed before 2026-07-13; this locks each existing check plus
the peg_lt horizon-visibility check added the same day."""

from validator import validate_dossier, _flag_divergence


def _dossier(**overrides):
    base = {
        "profile": {"market_cap_bn": 10.0, "country": "US", "exchange": "NASDAQ"},
        "financials": {
            "income": [{"net_income": 500_000_000}],
            "ratios_ttm": {"pe": 20.0, "fwd_pe": 18.0, "ps": 5.0, "revenue_ttm": 2_000_000_000,
                          "eps_cagr_fwd_years": 5, "eps_cagr_fwd_src": "finviz"},
        },
        "valuation": {"analyst_consensus": {}},
        "quote": {"price": 100.0},
        "earnings_surprises": [],
        "earnings_calendar": {"upcoming": []},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


# ── _flag_divergence (shared helper) ────────────────────────────────────────

def test_flag_divergence_fires_above_threshold():
    w = []
    _flag_divergence(w, "Trailing PE", {"a": 20.0, "b": 30.0}, threshold=0.25)
    assert len(w) == 1 and "Trailing PE divergence" in w[0]


def test_flag_divergence_silent_within_threshold():
    w = []
    _flag_divergence(w, "Trailing PE", {"a": 20.0, "b": 22.0}, threshold=0.25)
    assert w == []


def test_flag_divergence_needs_two_sources():
    w = []
    _flag_divergence(w, "Trailing PE", {"a": 20.0, "b": None}, threshold=0.25)
    assert w == []


# ── 1. PE cross-check (yfinance vs computed from mcap/net_income) ──────────

def test_pe_cross_check_flags_large_divergence():
    # yfinance PE=20, computed = mcap(10bn)/net_income(500m) = 20 — no divergence.
    d = _dossier()
    r = validate_dossier(d)
    assert not any("Trailing PE divergence" in w for w in r["warnings"])

    # Now make computed PE wildly different (net income much smaller).
    d2 = _dossier(financials={"income": [{"net_income": 50_000_000}],
                              "ratios_ttm": {"pe": 20.0, "fwd_pe": 18.0}})
    r2 = validate_dossier(d2)
    assert any("Trailing PE divergence" in w for w in r2["warnings"])


# ── 3. Foreign/ADR flag ──────────────────────────────────────────────────────

def test_foreign_stock_flag():
    d = _dossier(profile={"market_cap_bn": 10.0, "country": "Taiwan", "exchange": "NYSE"})
    r = validate_dossier(d)
    assert any("FOREIGN STOCK" in w for w in r["warnings"])


def test_adr_mismatch_flag_distinct_from_plain_foreign():
    d = _dossier(profile={"market_cap_bn": 10.0, "country": "Taiwan", "exchange": "NYSE"},
                 financials={"ratios_ttm": {"pe": 20.0, "adr_mismatch": True}})
    r = validate_dossier(d)
    assert any("FOREIGN STOCK / ADR" in w for w in r["warnings"])


# ── 4. Extreme value bounds ──────────────────────────────────────────────────

def test_extreme_pe_flagged():
    d = _dossier(financials={"ratios_ttm": {"pe": 500.0}})
    r = validate_dossier(d)
    assert any("Extreme PE" in w for w in r["warnings"])


def test_negative_pe_flagged():
    d = _dossier(financials={"ratios_ttm": {"pe": -10.0}})
    r = validate_dossier(d)
    assert any("Negative PE" in w for w in r["warnings"])


def test_extreme_forward_pe_flagged():
    d = _dossier(financials={"ratios_ttm": {"pe": 20.0, "fwd_pe": 250.0}})
    r = validate_dossier(d)
    assert any("Extreme forward PE" in w for w in r["warnings"])


# ── 5. Missing forward PE ────────────────────────────────────────────────────

def test_missing_forward_pe_flagged_when_trailing_pe_present():
    d = _dossier(financials={"ratios_ttm": {"pe": 20.0, "fwd_pe": None}})
    r = validate_dossier(d)
    assert any("Forward PE unavailable" in w for w in r["warnings"])


# ── 6. Trading above analyst consensus ──────────────────────────────────────

def test_trading_above_consensus_flagged():
    d = _dossier(quote={"price": 120.0},
                 valuation={"analyst_consensus": {"target_mean": 100.0}})
    r = validate_dossier(d)
    assert any("TRADING ABOVE CONSENSUS" in w for w in r["warnings"])


def test_trading_below_consensus_not_flagged():
    d = _dossier(quote={"price": 90.0},
                 valuation={"analyst_consensus": {"target_mean": 100.0}})
    r = validate_dossier(d)
    assert not any("TRADING ABOVE CONSENSUS" in w for w in r["warnings"])


# ── 7. Large EPS beat pattern ────────────────────────────────────────────────

def test_large_eps_beats_flagged():
    d = _dossier(earnings_surprises=[
        {"beat_quality": "LARGE_BEAT"}, {"beat_quality": "LARGE_BEAT"}, {"beat_quality": "BEAT"}])
    r = validate_dossier(d)
    assert any("LARGE EPS BEATS" in w for w in r["warnings"])


def test_single_large_beat_not_flagged():
    d = _dossier(earnings_surprises=[{"beat_quality": "LARGE_BEAT"}, {"beat_quality": "BEAT"}])
    r = validate_dossier(d)
    assert not any("LARGE EPS BEATS" in w for w in r["warnings"])


# ── 8b. Long-horizon PEG denominator visibility (2026-07-13) ───────────────

def test_short_horizon_peg_lt_flagged():
    d = _dossier(financials={"ratios_ttm": {
        "pe": 20.0, "fwd_pe": 18.0, "eps_cagr_fwd_years": 1, "eps_cagr_fwd_src": "av"}})
    r = validate_dossier(d)
    assert any("LONG-HORIZON PEG IS SHORT-HORIZON" in w and "av" in w for w in r["warnings"])


def test_true_5y_peg_lt_not_flagged():
    d = _dossier(financials={"ratios_ttm": {
        "pe": 20.0, "fwd_pe": 18.0, "eps_cagr_fwd_years": 5, "eps_cagr_fwd_src": "finviz"}})
    r = validate_dossier(d)
    assert not any("LONG-HORIZON PEG" in w for w in r["warnings"])


def test_missing_peg_lt_flagged_when_fwd_pe_present():
    d = _dossier(financials={"ratios_ttm": {
        "pe": 20.0, "fwd_pe": 18.0, "eps_cagr_fwd_years": None, "eps_cagr_fwd_src": None}})
    r = validate_dossier(d)
    assert any("NO LONG-HORIZON PEG" in w for w in r["warnings"])


def test_missing_peg_lt_not_flagged_when_no_fwd_pe_at_all():
    # No forward PE to begin with — the "missing forward PE" warning already
    # covers this case; don't pile on a second, redundant warning.
    d = _dossier(financials={"ratios_ttm": {"pe": 20.0, "fwd_pe": None}})
    r = validate_dossier(d)
    assert not any("NO LONG-HORIZON PEG" in w for w in r["warnings"])


# ── confidence scoring ───────────────────────────────────────────────────────

def test_confidence_high_with_no_warnings():
    assert validate_dossier(_dossier())["data_confidence"] == "HIGH"


def test_confidence_degrades_with_warnings():
    d = _dossier(financials={"ratios_ttm": {"pe": -10.0, "fwd_pe": 250.0}})
    r = validate_dossier(d)
    assert r["data_confidence"] in ("MEDIUM", "LOW")
    assert len(r["warnings"]) >= 2


# ── period-matched PE cross-check (2026-07-14) ──────────────────────────────
# yfinance trailingPE is TTM; computing the cross-check from the latest ANNUAL
# net income fired spurious divergence warnings on off-cycle fast growers
# (live NVDA: 42.5 annual-computed vs 31.1 TTM → false MEDIUM confidence).

def _pe_dossier(yf_pe, annual_ni, ttm_ni=None, mcap_bn=100.0):
    return {
        "profile": {"market_cap_bn": mcap_bn, "country": "US"},
        "financials": {
            "income": [{"net_income": annual_ni}],
            "ratios_ttm": {"pe": yf_pe, "net_income_ttm": ttm_ni},
        },
        "quote": {}, "valuation": {},
    }


def test_pe_check_prefers_ttm_net_income_over_annual():
    # yf PE 31.1 with mcap 100B → TTM NI ≈ 3.215B matches; the STALE annual
    # 2.35B alone would compute 42.5 and fire a false 27% divergence.
    d = _pe_dossier(yf_pe=31.1, annual_ni=2_350_000_000, ttm_ni=3_215_000_000)
    r = validate_dossier(d)
    assert not any("Trailing PE divergence" in w for w in r["warnings"])


def test_pe_check_falls_back_to_annual_when_ttm_missing():
    # Without TTM NI the annual fallback still catches a REAL divergence.
    d = _pe_dossier(yf_pe=10.0, annual_ni=2_000_000_000, ttm_ni=None)
    r = validate_dossier(d)
    assert any("Trailing PE divergence" in w for w in r["warnings"])
