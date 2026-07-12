"""signal_analysis pure-function tests — date selection, return math, bucketing.

All synthetic: no Supabase, no yfinance.
"""

from datetime import date

import pandas as pd

import signal_analysis as sa


def _series(start: str, days: int, price0: float = 100.0, step: float = 1.0) -> pd.Series:
    idx = pd.bdate_range(start, periods=days)
    return pd.Series([price0 + i * step for i in range(days)], index=idx)


# ── close_near ────────────────────────────────────────────────────────


def test_close_near_exact_trading_day():
    s = _series("2026-06-01", 15)  # weekdays 06-01..06-19
    hit = sa.close_near(s, date(2026, 6, 10))
    assert hit == (date(2026, 6, 10), float(s.loc["2026-06-10"]))


def test_close_near_weekend_falls_back_to_friday():
    s = _series("2026-06-01", 10)  # through Fri 06-12
    hit = sa.close_near(s, date(2026, 6, 13))  # Saturday
    assert hit[0] == date(2026, 6, 12)


def test_close_near_uses_after_side_when_no_prior_close():
    s = _series("2026-06-15", 5)  # starts Monday 06-15
    hit = sa.close_near(s, date(2026, 6, 13))  # 2 days before first close
    assert hit[0] == date(2026, 6, 15)


def test_close_near_beyond_tolerance_is_none():
    s = _series("2026-06-15", 5)
    assert sa.close_near(s, date(2026, 6, 1)) is None


def test_close_near_empty_or_none_series():
    assert sa.close_near(None, date(2026, 6, 1)) is None
    assert sa.close_near(pd.Series(dtype=float), date(2026, 6, 1)) is None


# ── forward_return ────────────────────────────────────────────────────


def test_forward_return_pending_when_window_not_elapsed():
    s = _series("2026-06-01", 30)
    fr = sa.forward_return(s, 100.0, date(2026, 6, 10), weeks=4,
                           today=date(2026, 6, 20))
    assert fr == {"status": "pending"}


def test_forward_return_uses_stored_entry_price():
    s = _series("2026-06-01", 30)  # 06-01=100, +1/bday
    fr = sa.forward_return(s, 100.0, date(2026, 6, 10), weeks=1,
                           today=date(2026, 6, 30))
    # target 06-17 (Wed) close = 100 + 12 bdays = 112
    assert fr["status"] == "ok"
    assert abs(fr["ret"] - 0.12) < 1e-9
    assert fr["exit_date"] == date(2026, 6, 17)
    assert fr["entry_approx"] is False


def test_forward_return_falls_back_to_discovery_close():
    s = _series("2026-06-01", 30)
    fr = sa.forward_return(s, None, date(2026, 6, 10), weeks=1,
                           today=date(2026, 6, 30))
    # entry = 06-10 close (107), exit = 06-17 close (112)
    assert fr["status"] == "ok"
    assert fr["entry_approx"] is True
    assert abs(fr["ret"] - (112.0 / 107.0 - 1.0)) < 1e-9


def test_forward_return_no_data_paths():
    empty = pd.Series(dtype=float)
    assert sa.forward_return(empty, None, date(2026, 6, 10), 1,
                             date(2026, 6, 30))["status"] == "no_data"
    s = _series("2026-06-01", 30)
    assert sa.forward_return(s, 0.0, date(2026, 6, 10), 1,
                             date(2026, 6, 30))["status"] == "no_data"


# ── bucketing ─────────────────────────────────────────────────────────


def test_tercile_labels_thirds():
    labels = sa.tercile_labels([1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert labels.count("low") == 3
    assert labels.count("mid") == 3
    assert labels.count("high") == 3


def test_tercile_labels_passes_none_through():
    labels = sa.tercile_labels([None, 1, 2, 3, 4, 5, 6, None])
    assert labels[0] is None and labels[-1] is None
    assert set(labels[1:-1]) <= {"low", "mid", "high"}


def test_tercile_labels_small_sample_collapses_to_all():
    assert sa.tercile_labels([1, 2, None]) == ["all", "all", None]


def test_bucket_stats_math():
    rows = [
        {"grade": "BUY", "excess": 0.10},
        {"grade": "BUY", "excess": -0.05},
        {"grade": "STRONG BUY", "excess": 0.02},
    ]
    stats = sa.bucket_stats(rows, lambda r: r["grade"])
    buy = stats["BUY"]
    assert buy["n"] == 2
    assert buy["hit"] == 0.5
    assert abs(buy["mean"] - 0.025) < 1e-9
    assert abs(buy["median"] - 0.025) < 1e-9
    assert stats["STRONG BUY"]["n"] == 1


# ── parse_rows ────────────────────────────────────────────────────────


def test_parse_rows_tolerates_nulls_and_skips_broken():
    rows = [
        {"ticker": "abc", "discovered_at": "2026-06-11T14:20:00+00:00",
         "price": None, "grade": None, "score": 7.1, "confirmed": None,
         "verdict": None, "factors": None},
        {"ticker": "", "discovered_at": "2026-06-11T14:20:00+00:00"},   # no ticker
        {"ticker": "DEF", "discovered_at": "not-a-date"},               # bad date
        {"ticker": "GHI", "discovered_at": "2026-07-01T00:00:00+00:00",
         "price": 55.5, "confirmed": True, "verdict": "CONFIRM",
         "factors": {"v": 2, "mom_12_1": 0.4, "quality": 7.2}},
    ]
    signals = sa.parse_rows(rows, "scout")
    assert [s["ticker"] for s in signals] == ["ABC", "GHI"]
    a, g = signals
    assert a["entry_price"] is None and a["grade"] == "(none)"
    assert a["verdict"] == "(none)" and a["mom_12_1"] is None
    assert g["discovered"] == date(2026, 7, 1)
    assert g["mom_12_1"] == 0.4 and g["factors_v"] == 2


# ── build_report smoke ────────────────────────────────────────────────


def test_build_report_end_to_end_synthetic():
    closes = {"AAA": _series("2026-06-01", 30, 100, 2.0),   # strong up
              "BBB": _series("2026-06-01", 30, 100, -1.0)}  # down
    vwra = _series("2026-06-01", 30, 200, 0.5)              # gentle up
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "grade": "BUY", "confirmed": True, "verdict": "CONFIRM",
         "factors": {"v": 2, "mom_12_1": 0.5, "quality": 8.0}},
        {"ticker": "BBB", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "grade": "BUY", "confirmed": None, "verdict": None,
         "factors": None},
    ], "scout")
    lines = sa.build_report(signals, closes, vwra, windows=[1],
                            today=date(2026, 6, 30))
    text = "\n".join(lines)
    assert "measurable 2 · pending 0 · no-data 0" in text
    assert "confirmed" in text and "unknown" in text     # gate buckets
    assert "unstamped" in text                            # factors.v bucket
    assert "top:    AAA" in text and "bottom: BBB" in text
    # AAA excess must beat BBB's (sanity on the return math direction)
    assert "⚠ n<10" in text                               # small-sample marker


def test_compute_scoreboard_json_shape():
    closes = {"AAA": _series("2026-06-01", 30, 100, 2.0),
              "BBB": _series("2026-06-01", 30, 100, -1.0)}
    vwra = _series("2026-06-01", 30, 200, 0.5)
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "grade": "BUY", "confirmed": True, "verdict": "CONFIRM",
         "factors": {"v": 2, "mom_12_1": 0.5, "quality": 8.0}},
        {"ticker": "BBB", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "grade": "BUY", "confirmed": None, "verdict": None,
         "factors": None},
    ], "scout")
    sb = sa.compute_scoreboard(signals, closes, vwra, windows=[1, 12],
                               today=date(2026, 6, 30))
    assert sb["v"] == 1 and sb["benchmark"] == "VWRA.L"
    assert sb["n_signals"] == 2 and sb["n_scout"] == 2 and sb["n_gems"] == 0
    assert sb["as_of"] == "2026-06-30" and sb["generated_at"]

    w1, w12 = sb["windows"]
    assert w1["weeks"] == 1 and w1["measurable"] == 2 and w1["pending"] == 0
    assert w1["overall"]["n"] == 2
    assert set(w1["buckets"]) == {"grade", "gate", "verdict", "mom_12_1",
                                  "quality", "eps_rev_mom", "fcf_yield", "roic",
                                  "regime", "cap_smart_tilt", "factors_v", "source"}
    gate_keys = {e["k"] for e in w1["buckets"]["gate"]}
    assert gate_keys == {"confirmed", "unknown"}
    assert w1["top"][0]["ticker"] == "AAA" and w1["bottom"][0]["ticker"] == "BBB"
    # Version register rides along for the dashboard/report
    assert {v["v"] for v in sb["versions"]} >= {"unstamped", "v2", "v3"}
    assert all(v["desc"] for v in sb["versions"])
    # rounded floats, JSON-serializable end to end
    import json
    json.dumps(sb)
    assert w1["overall"]["hit"] == round(w1["overall"]["hit"], 4)

    assert w12["measurable"] == 0 and w12["pending"] == 2
    assert "overall" not in w12


def test_render_report_consumes_computed_dict():
    closes = {"AAA": _series("2026-06-01", 30, 100, 2.0)}
    vwra = _series("2026-06-01", 30, 200, 0.5)
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "grade": "BUY"},
    ], "scout")
    sb = sa.compute_scoreboard(signals, closes, vwra, [1], date(2026, 6, 30))
    text = "\n".join(sa.render_report(sb))
    assert "as of 2026-06-30" in text
    assert "measurable 1 · pending 0 · no-data 0" in text
    assert "methodology v2:" in text
    assert "methodology v3:" in text


def test_default_windows_include_year_scale():
    """2026-07-07: Daryl's stated horizon is 'in any given year' — the default
    windows must include 26/52-week reads, not just short-horizon ones."""
    weeks = {int(w) for w in sa._DEFAULT_WINDOWS.split(",")}
    assert {1, 4, 12, 26, 52} <= weeks


def test_digest_due_mondays_only():
    assert sa.digest_due(date(2026, 7, 6)) is True    # Monday
    assert sa.digest_due(date(2026, 7, 4)) is False   # Saturday
    assert sa.digest_due(date(2026, 7, 7)) is False   # Tuesday


def test_build_report_pending_only_window():
    closes = {"AAA": _series("2026-06-01", 30)}
    vwra = _series("2026-06-01", 30)
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0},
    ], "scout")
    lines = sa.build_report(signals, closes, vwra, windows=[12],
                            today=date(2026, 6, 20))
    text = "\n".join(lines)
    assert "measurable 0 · pending 1" in text
    assert "nothing measurable" in text


# ── attribution factors + regime (2026-07-11) ─────────────────────────


def test_parse_rows_carries_attribution_factors():
    rows = [{"ticker": "AAA", "discovered_at": "2026-07-01T00:00:00+00:00",
             "factors": {"v": 3, "eps_rev_mom": 0.12, "fcf_yield": 4.5,
                         "roic": 18.2, "regime": "TIGHTENING"}}]
    s = sa.parse_rows(rows, "scout")[0]
    assert s["eps_rev_mom"] == 0.12 and s["fcf_yield"] == 4.5
    assert s["roic"] == 18.2 and s["regime"] == "TIGHTENING"


def test_scoreboard_buckets_include_new_dimensions():
    closes = {"AAA": _series("2026-06-01", 30, 100, 2.0)}
    vwra = _series("2026-06-01", 30, 200, 0.5)
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "grade": "BUY", "confirmed": True, "verdict": "CONFIRM",
         "factors": {"v": 3, "fcf_yield": 5.0, "roic": 20.0,
                     "eps_rev_mom": 0.1, "regime": "GOLDILOCKS"}},
    ], "scout")
    sb = sa.compute_scoreboard(signals, closes, vwra, [1], date(2026, 6, 30))
    buckets = sb["windows"][0]["buckets"]
    for key in ("eps_rev_mom", "fcf_yield", "roic", "regime"):
        assert key in buckets, key
    assert buckets["regime"][0]["k"] == "GOLDILOCKS"
    # single signal → terciles collapse to 'all' (n<6 rule unchanged)
    assert buckets["fcf_yield"][0]["k"] == "all"


def test_scoreboard_regime_unstamped_for_old_rows():
    closes = {"OLD": _series("2026-06-01", 30, 100, 1.0)}
    vwra = _series("2026-06-01", 30, 200, 0.5)
    signals = sa.parse_rows([
        {"ticker": "OLD", "discovered_at": "2026-06-10T12:00:00+00:00",
         "price": 100.0, "factors": {"v": 2}},
    ], "scout")
    sb = sa.compute_scoreboard(signals, closes, vwra, [1], date(2026, 6, 30))
    assert sb["windows"][0]["buckets"]["regime"][0]["k"] == "unstamped"


# ── behavior gap (2026-07-11) ─────────────────────────────────────────


def test_twr_chain_matches_eye_fixture():
    """SHARED FIXTURE with sovereign-eye tests/nav-broker.test.mjs: 100 → 210
    with a 100 deposit on day 2 must be +10%, not +110%. Both implementations
    must agree on this forever."""
    twr = sa._twr_chain(["2026-07-01", "2026-07-02"], [100.0, 210.0],
                        {"2026-07-02": 100.0})
    assert twr == 10.0
    assert sa._twr_chain(["a", "b"], [100.0, 105.0], {}) == 5.0
    assert sa._twr_chain([], [], {}) is None


def test_behavior_gap_paper_vs_real():
    closes = {"WIN":  _series("2026-06-01", 40, 100, 2.0),
              "LOSE": _series("2026-06-01", 40, 100, -1.0)}
    vwra = _series("2026-06-01", 40, 200, 0.2)
    signals = sa.parse_rows([
        {"ticker": "WIN", "discovered_at": "2026-06-10T00:00:00+00:00",
         "price": 118.0, "confirmed": True, "verdict": "CONFIRM", "factors": None},
        {"ticker": "LOSE", "discovered_at": "2026-06-10T00:00:00+00:00",
         "price": 91.0, "confirmed": True, "verdict": "CONFIRM", "factors": None},
        {"ticker": "WIN", "discovered_at": "2026-06-12T00:00:00+00:00",
         "price": 100.0, "confirmed": True, "verdict": "DOWNGRADE", "factors": None},
    ], "scout")
    real = {"dates": ["2026-06-09", "2026-06-15", "2026-06-30"],
            "nav": [1000.0, 1150.0, 1150.0],   # +15% then flat, no flows
            "flows": {}}
    bg = sa.compute_behavior_gap(signals, closes, vwra, date(2026, 6, 30), real)
    assert bg["since"] == "2026-06-10"
    assert bg["paper"]["n"] == 2                       # DOWNGRADE excluded
    assert bg["paper"]["hit"] == 0.5
    assert bg["vwra_pct"] is not None
    # real slice starts at 2026-06-15 (first date >= since... 06-09 < since)
    assert bg["real_twr_pct"] == 0.0                   # 1150 → 1150
    assert "no costs" in bg["note"]


def test_behavior_gap_none_without_confirms():
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T00:00:00+00:00",
         "price": 100.0, "verdict": "DOWNGRADE", "factors": None},
    ], "scout")
    assert sa.compute_behavior_gap(signals, {}, _series("2026-06-01", 5, 200, 0.1),
                                   date(2026, 6, 30), None) is None


def test_behavior_gap_survives_missing_real():
    closes = {"AAA": _series("2026-06-01", 40, 100, 1.0)}
    vwra = _series("2026-06-01", 40, 200, 0.2)
    signals = sa.parse_rows([
        {"ticker": "AAA", "discovered_at": "2026-06-10T00:00:00+00:00",
         "price": 109.0, "confirmed": True, "verdict": "CONFIRM", "factors": None},
    ], "scout")
    bg = sa.compute_behavior_gap(signals, closes, vwra, date(2026, 6, 30), real=None)
    assert bg["real_twr_pct"] is None and bg["paper"]["n"] == 1


def test_fetch_real_nav_requires_broker_source(monkeypatch):
    class _Resp:
        ok = True
        def json(self):
            return {"perf": {"source": "snapshots"}, "raw": {"dates": ["d"], "nav": [1]}}
    monkeypatch.setenv("SOVEREIGN_EYE_URL", "https://eye.example")
    monkeypatch.setenv("DD_UPLOAD_SECRET", "s")
    monkeypatch.setattr(sa.requests, "get", lambda *a, **k: _Resp())
    assert sa.fetch_real_nav() is None                 # quote-derived ≠ real account


def test_render_report_includes_behavior_gap():
    sb = {"as_of": "2026-06-30", "n_scout": 1, "n_gems": 0, "benchmark": "VWRA.L",
          "note": "n", "versions": [], "windows": [],
          "behavior_gap": {"since": "2026-06-10", "as_of": "2026-06-30",
                           "paper": {"n": 2, "mean_return_pct": 12.4, "hit": 0.5,
                                     "mean_excess_pct": 8.1},
                           "vwra_pct": 4.3, "real_twr_pct": 21.7, "note": "x"}}
    text = "\n".join(sa.render_report(sb))
    assert "behavior gap" in text and "+12.4%" in text and "+21.7%" in text


# ── holdings archive (dd_history mining, 2026-07-11) ──────────────────


def test_parse_dd_rows_dedupes_to_weekly():
    rows = [
        {"ticker": "AMZN", "run_at": "2026-07-06T09:00:00+00:00", "price": 220.0,
         "score": 7.1, "agent_scores": {"A": 7.0, "B": 8.0}, "archetype": "COMPOUNDER", "mos": 0.2},
        {"ticker": "AMZN", "run_at": "2026-07-08T09:00:00+00:00", "price": 221.0,
         "score": 7.2, "agent_scores": {}, "archetype": "COMPOUNDER", "mos": 0.2},  # same ISO week
        {"ticker": "AMZN", "run_at": "2026-07-13T09:00:00+00:00", "price": 225.0,
         "score": 7.3, "agent_scores": {}, "archetype": "COMPOUNDER", "mos": 0.2},  # next week
        {"ticker": "", "run_at": "2026-07-06T09:00:00+00:00"},                       # no ticker
        {"ticker": "BAD", "run_at": "not-a-date"},                                   # bad date
    ]
    obs = sa.parse_dd_rows(rows)
    assert len(obs) == 2                       # weekly dedupe, first-in-week wins
    assert obs[0]["entry_price"] == 220.0
    assert obs[0]["agent_scores"] == {"A": 7.0, "B": 8.0}


def test_holdings_analysis_agent_tilt_calibration():
    closes = {"WIN": _series("2026-06-01", 40, 100, 2.0)}
    vwra = _series("2026-06-01", 40, 200, 0.2)
    obs = sa.parse_dd_rows([
        {"ticker": "WIN", "run_at": "2026-06-10T00:00:00+00:00", "price": 118.0,
         "score": 7.0, "archetype": "COMPOUNDER", "mos": 0.3,
         "agent_scores": {"Bull": 8.0, "Bear": 6.0, "Mid": 7.0}},   # Bull +1 tilt, Bear −1
    ])
    ha = sa.compute_holdings_analysis(obs, closes, vwra, [1], date(2026, 6, 30))
    b = ha["windows"][0]["buckets"]
    tilts = {e["k"] for e in b["agent_tilt"]}
    assert tilts == {"Bull:bullish", "Bear:bearish"}   # Mid (±0) is no tilt
    assert b["archetype"][0]["k"] == "COMPOUNDER"
    assert ha["n_obs"] == 1 and "small n" in ha["note"]


def test_holdings_analysis_empty_is_none():
    assert sa.compute_holdings_analysis([], {}, _series("2026-06-01", 5, 200, 0.1),
                                        [1], date(2026, 6, 30)) is None
