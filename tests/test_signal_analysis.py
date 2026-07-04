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
                                  "quality", "factors_v", "source"}
    gate_keys = {e["k"] for e in w1["buckets"]["gate"]}
    assert gate_keys == {"confirmed", "unknown"}
    assert w1["top"][0]["ticker"] == "AAA" and w1["bottom"][0]["ticker"] == "BBB"
    # Version register rides along for the dashboard/report
    assert {v["v"] for v in sb["versions"]} >= {"unstamped", "v2"}
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
