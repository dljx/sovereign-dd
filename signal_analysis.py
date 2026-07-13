"""Forward-return scoreboard: every logged signal vs VWRA over matched windows.

This is the system's arbiter (see docs/METHODOLOGY_REVIEW.md §6): each row in
Supabase scout_history / gems_history is a signal event with an entry price and
a factor stamp; this script measures each signal's forward return MINUS VWRA's
forward return over the same window, then buckets by grade, gate outcome,
verdict, momentum/quality terciles, factors version, and source. Every deferred
methodology change graduates or dies on these numbers — never on vibes.

Outputs: a text report (stdout / --markdown), a JSON snapshot for the dashboard
panel (--json → uploaded to KV dd:scoreboard by upload_kv.py), and an optional
Monday-only Telegram digest (--digest-weekly).

Caveats printed in the report header:
  - Pre-2026-07-03 rows have backfilled same-day-close entry prices (approx).
  - Daily-close granularity; VWRA.L (LSE, USD-denominated) has its own trading
    calendar, so entry/exit dates are matched within a ±3-day tolerance.

Usage:
    python signal_analysis.py [--windows 1,4,12,26,52] [--markdown out.md]
                              [--json out.json] [--digest-weekly]

Env: SUPABASE_URL / SUPABASE_KEY (same convention as upload_kv.py).
"""

import argparse
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

BENCHMARK = "VWRA.L"
# Max calendar-day gap between a target date and the close used for it — covers
# weekends/holidays on either exchange without silently stretching the window.
_DATE_TOLERANCE_DAYS = 3
_PAGE = 1000

_DATA_NOTE = "pre-2026-07-03 entry prices are same-day-close backfills (approx)"

# Default forward-return windows, in weeks. 26/52 (added 2026-07-07) read as
# "pending" until signals age into them — but the buckets must exist to ever
# measure Daryl's stated "in any given year" horizon (n≥30 4-week reads are
# pre-registered ~2026-08-15; 26/52-week reads follow as of ~Dec 2026/Jun 2027).
_DEFAULT_WINDOWS = "1,4,12,26,52"

# Mirror of the append-only register in docs/ADAPTATION_PROTOCOL.md §4 — keep
# the two in sync (rule 5: a selection-affecting change bumps factors.v in the
# same commit as its register row).
_VERSION_REGISTER = {
    None: "legacy ≤2026-07-02 — no factor stamp, backfilled entry prices",
    2: "since 2026-07-03 — true momentum lens, anti-evidence removals, "
       "quality composite, fail-closed gate",
    3: "since 2026-07-07 — gate grades not gates: R:R divergence is red-team "
       "input not an auto-reject, DOWNGRADE surfaces flagged, calm-window "
       "re-verification of UNVERIFIED holds",
}


# ── Supabase fetch ────────────────────────────────────────────────────


def _fetch_rows(table: str, select: str = "*",
                order: str = "discovered_at.asc") -> list[dict]:
    """All rows from a history table, paginated via PostgREST Range headers."""
    rows: list[dict] = []
    start = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"select": select, "order": order},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Range": f"{start}-{start + _PAGE - 1}",
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < _PAGE:
            return rows
        start += _PAGE


def parse_rows(rows: list[dict], src: str) -> list[dict]:
    """History rows → signal dicts. Tolerates nulls everywhere except ticker
    and discovered_at (a signal without either can't be measured)."""
    out = []
    for r in rows:
        ticker = (r.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            discovered = datetime.fromisoformat(str(r["discovered_at"])).date()
        except Exception:
            continue
        factors = r.get("factors") or {}
        out.append({
            "ticker":      ticker,
            "src":         src,
            "discovered":  discovered,
            "entry_price": r.get("price"),
            "grade":       r.get("grade") or "(none)",
            "score":       r.get("score"),
            "confirmed":   r.get("confirmed"),
            "verdict":     r.get("verdict") or "(none)",
            "mom_12_1":    factors.get("mom_12_1"),
            "quality":     factors.get("quality"),
            "eps_rev_mom": factors.get("eps_rev_mom"),
            "fcf_yield":   factors.get("fcf_yield"),
            "roic":        factors.get("roic"),
            "regime":      factors.get("regime"),   # stamped from 2026-07-11
            "cap_smart_tilt": factors.get("cap_smart_tilt"),  # from 2026-07-12
            "factors_v":   factors.get("v"),
        })
    return out


# ── Price lookups (pure, unit-tested) ─────────────────────────────────


def close_near(closes: pd.Series, target: date,
               tol: int = _DATE_TOLERANCE_DAYS) -> tuple[date, float] | None:
    """Close nearest `target`, preferring the latest trading day ≤ target,
    else the earliest after — both within `tol` calendar days. None if the
    series has no close in range."""
    if closes is None or closes.empty:
        return None
    ts = pd.Timestamp(target)
    before = closes.loc[:ts]
    if len(before):
        d = before.index[-1]
        if (ts - d).days <= tol:
            return d.date(), float(before.iloc[-1])
    after = closes.loc[ts:]
    if len(after):
        d = after.index[0]
        if (d - ts).days <= tol:
            return d.date(), float(after.iloc[0])
    return None


# Stored entry prices are RAW prices at signal time, but the return math runs
# on ADJUSTED closes (auto_adjust=True — correct for splits/dividends *within*
# the series). A corporate action between signal time and today rescales the
# whole adjusted history: after a 2:1 split, a raw $100 entry against a ~$50
# adjusted exit reads as a fake -50% and poisons every bucket the row lands in.
# Beyond this divergence from the adjusted close at the signal date, the stored
# price's basis can no longer be trusted — use the adjusted close instead.
# (Also catches plain bad stored prices, e.g. currency mixups. Normal intraday
# signal-vs-close gaps are far below 25%.)
_ENTRY_DIVERGENCE_MAX = 0.25


def effective_entry(closes: pd.Series, entry_price: float | None,
                    discovered: date) -> tuple[float | None, bool]:
    """(entry, approx): the stored signal price, unless it's missing or diverges
    >_ENTRY_DIVERGENCE_MAX from the adjusted close nearest the signal date
    (split / bad-price guard) — both fall back to that close with approx=True.
    (None, False) when neither is available."""
    hit = close_near(closes, discovered)
    if entry_price and entry_price > 0:
        if hit is None or hit[1] <= 0:
            return float(entry_price), False
        if abs(entry_price - hit[1]) / hit[1] > _ENTRY_DIVERGENCE_MAX:
            return float(hit[1]), True
        return float(entry_price), False
    return (float(hit[1]), True) if hit else (None, False)


def forward_return(closes: pd.Series, entry_price: float | None,
                   discovered: date, weeks: int, today: date) -> dict:
    """Return over `weeks` from a signal. entry_price=None (or a stored price
    whose basis no longer matches the adjusted series — see effective_entry)
    falls back to the close nearest the discovery date (flagged entry_approx).
    Statuses: ok / pending (window not yet elapsed) / no_data (elapsed but
    unpriceable)."""
    target = discovered + timedelta(weeks=weeks)
    if target > today:
        return {"status": "pending"}
    entry, entry_approx = effective_entry(closes, entry_price, discovered)
    if not entry or entry <= 0:
        return {"status": "no_data"}
    exit_hit = close_near(closes, target)
    if exit_hit is None:
        return {"status": "no_data"}
    return {"status": "ok", "ret": exit_hit[1] / entry - 1.0,
            "exit_date": exit_hit[0], "entry_approx": entry_approx}


# ── Bucketing (pure, unit-tested) ─────────────────────────────────────


def tercile_labels(values: list) -> list:
    """low/mid/high per value, terciles over the non-None values. Fewer than 6
    known values → everything known becomes 'all' (terciles would be noise)."""
    known = sorted(v for v in values if v is not None)
    if len(known) < 6:
        return [None if v is None else "all" for v in values]
    q1, q2 = statistics.quantiles(known, n=3)
    return [None if v is None else
            ("low" if v <= q1 else "high" if v > q2 else "mid")
            for v in values]


def bucket_stats(rows: list[dict], key_fn) -> dict:
    """{bucket: {n, hit, mean, median}} over rows' `excess`, grouped by key_fn."""
    groups: dict = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row["excess"])
    out = {}
    for k in sorted(groups, key=str):
        vals = groups[k]
        out[k] = {
            "n":      len(vals),
            "hit":    sum(1 for v in vals if v > 0) / len(vals),
            "mean":   statistics.fmean(vals),
            "median": statistics.median(vals),
        }
    return out


# ── Scoreboard (data) ─────────────────────────────────────────────────

# Factor fields that get tercile-bucketed at entry time (field, bucket key on
# the signal dict). eps_rev_mom/fcf_yield/roic added 2026-07-11 — the factors
# jsonb has carried them since v3 (07-07); older rows show n/a.
_TERCILE_FACTORS = [
    ("mom_12_1",    "mom_bucket"),
    ("quality",     "quality_bucket"),
    ("eps_rev_mom", "eps_rev_bucket"),
    ("fcf_yield",   "fcf_bucket"),
    ("roic",        "roic_bucket"),
    ("cap_smart_tilt", "cap_tilt_bucket"),
]

# Bucket key → (json key, report title). Order is the report/panel order.
_BUCKETS = [
    ("grade",       "grade",              lambda r: r["grade"]),
    ("gate",        "gate",               lambda r: {True: "confirmed", False: "rejected"}.get(r["confirmed"], "unknown")),
    ("verdict",     "verdict",            lambda r: r["verdict"]),
    ("mom_12_1",    "mom_12_1 tercile",   lambda r: r["mom_bucket"] or "n/a"),
    ("quality",     "quality tercile",    lambda r: r["quality_bucket"] or "n/a"),
    ("eps_rev_mom", "eps_rev_mom tercile", lambda r: r["eps_rev_bucket"] or "n/a"),
    ("fcf_yield",   "fcf_yield tercile",  lambda r: r["fcf_bucket"] or "n/a"),
    ("roic",        "roic tercile",       lambda r: r["roic_bucket"] or "n/a"),
    # Macro regime at signal time — the MEASUREMENT half of the rejected macro
    # kill-switch idea: a threshold rule only gets pre-registered if these
    # buckets prove predictive at n≥30/regime (~Jan 2027 read).
    ("regime",      "macro regime",       lambda r: r["regime"] or "unstamped"),
    ("cap_smart_tilt", "smart-money tilt tercile", lambda r: r["cap_tilt_bucket"] or "n/a"),
    ("factors_v",   "factors.v",          lambda r: f"v{r['factors_v']}" if r["factors_v"] else "unstamped"),
    ("source",      "source",             lambda r: r["src"]),
]


def _round_stats(s: dict) -> dict:
    return {"n": s["n"], "hit": round(s["hit"], 4),
            "mean": round(s["mean"], 4), "median": round(s["median"], 4)}


def compute_backtest(signals: list[dict], closes: dict, vwra: pd.Series,
                     today: date, hold_weeks: int = 12) -> dict | None:
    """Follow-the-engine equity curve: hold each CONFIRM equal-/score-weighted
    from its signal date for `hold_weeks`, daily-compounded, vs buy-and-hold
    VWRA over the same span.

    HONEST FRAMEWORK, NOT EVIDENCE (2026-07-12): signals only go back ~weeks,
    n is small, pre-2026-07-03 entry prices are same-day-close backfills, and
    the exit rule (fixed hold) is imposed not learned — so the curve is partly
    a function of `hold_weeks`. No survivorship bias (rejects are logged too,
    but this uses CONFIRMs by design — the 'what the engine told you to buy'
    line). No look-ahead: a signal only enters on/after its own `discovered`
    date. Read as a maturing instrument, not proof, until data accrues."""
    confirms = [s for s in signals
                if s.get("verdict") == "CONFIRM"
                and closes.get(s["ticker"]) is not None
                and s.get("entry_price")]
    if len(confirms) < 3:
        return None

    start = min(s["discovered"] for s in confirms)
    cal = vwra.index[(vwra.index >= pd.Timestamp(start))
                     & (vwra.index <= pd.Timestamp(today))]
    if len(cal) < 5:
        return None

    def daily_ret(ticker):
        s = closes[ticker].reindex(cal, method="ffill")
        return s.pct_change().fillna(0.0)

    eq_num = pd.Series(0.0, index=cal)
    eq_cnt = pd.Series(0.0, index=cal)
    sc_num = pd.Series(0.0, index=cal)
    sc_den = pd.Series(0.0, index=cal)
    for s in confirms:
        entry = pd.Timestamp(s["discovered"])
        mask = (cal >= entry) & (cal < entry + pd.Timedelta(weeks=hold_weeks))
        if not mask.any():
            continue
        dr = daily_ret(s["ticker"]).to_numpy()
        w = float(s.get("score") or 7.0)
        eq_num.iloc[mask] += dr[mask]
        eq_cnt.iloc[mask] += 1.0
        sc_num.iloc[mask] += dr[mask] * w
        sc_den.iloc[mask] += w

    eq_ret = (eq_num / eq_cnt.replace(0.0, 1.0)).fillna(0.0)
    sc_ret = (sc_num / sc_den.replace(0.0, 1.0)).fillna(0.0)
    eq_curve = (1.0 + eq_ret).cumprod()
    sc_curve = (1.0 + sc_ret).cumprod()
    vw = vwra.reindex(cal, method="ffill")
    vw_curve = vw / float(vw.iloc[0])

    def _stats(curve):
        total = float(curve.iloc[-1]) - 1.0
        run_max = curve.cummax()
        max_dd = float(((curve - run_max) / run_max).min())
        yrs = max((today - start).days / 365.25, 1e-6)
        cagr = (float(curve.iloc[-1])) ** (1 / yrs) - 1 if curve.iloc[-1] > 0 else None
        return {"total": round(total, 4),
                "cagr": round(cagr, 4) if cagr is not None else None,
                "max_dd": round(max_dd, 4)}

    eq_s, vw_s = _stats(eq_curve), _stats(vw_curve)
    return {
        "since": str(start),
        "hold_weeks": hold_weeks,
        "n_confirms": len(confirms),
        "labels": [d.strftime("%b %d") for d in cal],
        "equal": [round(float(x) * 100, 2) for x in eq_curve],
        "score": [round(float(x) * 100, 2) for x in sc_curve],
        "vwra":  [round(float(x) * 100, 2) for x in vw_curve],
        "stats": {"equal": eq_s, "vwra": vw_s,
                  "excess_total": round(eq_s["total"] - vw_s["total"], 4)},
        "note": ("CONFIRM signals held {}w each from signal date, daily-compounded, "
                 "vs buy-&-hold VWRA. FRAMEWORK not evidence: small n, pre-07-03 "
                 "entry prices are backfills, the exit rule is imposed. Do not read "
                 "an early curve as proof.").format(hold_weeks),
    }


def compute_scoreboard(signals: list[dict], closes: dict, vwra: pd.Series,
                       windows: list[int], today: date) -> dict:
    """The scoreboard as data — single source for the text report, the JSON
    uploaded to the dashboard (KV dd:scoreboard), and the Telegram digest."""
    n_scout = sum(1 for s in signals if s["src"] == "scout")

    # Terciles are computed once across all signals (entry-time factor profile).
    for field, bucket in _TERCILE_FACTORS:
        labels = tercile_labels([s.get(field) for s in signals])
        for s, lab in zip(signals, labels):
            s[bucket] = lab

    out_windows = []
    for weeks in windows:
        measured, pending, no_data = [], 0, 0
        for s in signals:
            fr = forward_return(closes.get(s["ticker"]), s["entry_price"],
                                s["discovered"], weeks, today)
            if fr["status"] == "pending":
                pending += 1
                continue
            vf = forward_return(vwra, None, s["discovered"], weeks, today)
            if fr["status"] != "ok" or vf["status"] != "ok":
                no_data += 1
                continue
            measured.append({**s, "excess": fr["ret"] - vf["ret"]})

        win: dict = {"weeks": weeks, "measurable": len(measured),
                     "pending": pending, "no_data": no_data}
        if measured:
            win["overall"] = _round_stats(bucket_stats(measured, lambda r: "ALL")["ALL"])
            win["buckets"] = {
                key: [{"k": str(k), **_round_stats(s)}
                      for k, s in bucket_stats(measured, fn).items()]
                for key, _title, fn in _BUCKETS
            }
            ranked = sorted(measured, key=lambda r: r["excess"], reverse=True)
            pick = lambda r: {"ticker": r["ticker"], "excess": round(r["excess"], 4)}
            win["top"] = [pick(r) for r in ranked[:5]]
            win["bottom"] = [pick(r) for r in ranked[-5:][::-1]]
        out_windows.append(win)

    return {
        "v": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": str(today),
        "benchmark": BENCHMARK,
        "n_signals": len(signals),
        "n_scout": n_scout,
        "n_gems": len(signals) - n_scout,
        "note": _DATA_NOTE,
        "versions": [{"v": ("unstamped" if v is None else f"v{v}"), "desc": desc}
                     for v, desc in _VERSION_REGISTER.items()],
        "windows": out_windows,
    }


# ── Behavior gap (2026-07-11): signal quality vs execution behavior ───
#
# Decomposes "beat VWRA or abstain" into its two failure points: the paper
# portfolio measures what the SIGNALS earned (every CONFIRM, equal weight,
# buy-and-hold from signal price — no costs, no sizing, no timing); the real
# broker TWR over the same window measures what the human+system actually
# earned. The gap is directional, not precise — but its SIGN says whether
# underperformance lives in signal quality or in behavior.


def _twr_chain(dates: list, navs: list, flows: dict) -> float | None:
    """Flow-adjusted TWR (%) — end-of-day flow convention, the same chain the
    eye computes in nav-history.js (locked by a shared fixture both sides)."""
    chain, prev = 1.0, None
    for d, v in zip(dates, navs):
        if v is None:
            continue
        if prev is not None and prev > 0:
            chain *= (v - float(flows.get(d, 0.0))) / prev
        prev = v
    return round((chain - 1.0) * 100, 2) if prev is not None else None


def fetch_real_nav() -> dict | None:
    """Real broker NAV series from the eye ({dates, nav, flows}) or None.
    Only broker-sourced series count — the quote-derived snapshot fallback is
    not the real account."""
    base = os.getenv("SOVEREIGN_EYE_URL", "").rstrip("/")
    secret = os.getenv("DD_UPLOAD_SECRET", "")
    if not base or not secret:
        return None
    try:
        r = requests.get(f"{base}/api/nav-history",
                         headers={"Authorization": f"Bearer {secret}"}, timeout=30)
        if not r.ok:
            return None
        d = r.json() or {}
        if (d.get("perf") or {}).get("source") != "broker":
            return None
        raw = d.get("raw") or {}
        if not raw.get("dates") or not raw.get("nav"):
            return None
        flows = {f["date"]: float(f["amount"])
                 for f in raw.get("flows") or [] if f.get("date")}
        return {"dates": raw["dates"], "nav": raw["nav"], "flows": flows}
    except Exception as e:
        print(f"[analysis] real NAV fetch failed ({e}) — behavior gap omits real TWR")
        return None


def compute_behavior_gap(signals: list[dict], closes: dict, vwra: pd.Series,
                         today: date, real: dict | None = None) -> dict | None:
    """The 'behavior_gap' scoreboard section, or None (no CONFIRMs yet)."""
    confirms = [s for s in signals if s["verdict"] == "CONFIRM"]
    if not confirms:
        return None
    since = min(s["discovered"] for s in confirms)

    rets, excesses = [], []
    for s in confirms:
        series = closes.get(s["ticker"])
        # Same split/bad-price guard as forward_return — a raw entry against an
        # adjusted exit would fake a huge loss after a corporate action.
        entry, _approx = effective_entry(series, s["entry_price"], s["discovered"])
        exit_hit = close_near(series, today)
        if not entry or entry <= 0 or exit_hit is None:
            continue
        ret = exit_hit[1] / entry - 1.0
        rets.append(ret)
        v_in, v_out = close_near(vwra, s["discovered"]), close_near(vwra, today)
        if v_in and v_out and v_in[1] > 0:
            excesses.append(ret - (v_out[1] / v_in[1] - 1.0))
    if not rets:
        return None

    v_start, v_end = close_near(vwra, since), close_near(vwra, today)
    vwra_pct = (round((v_end[1] / v_start[1] - 1.0) * 100, 2)
                if v_start and v_end and v_start[1] > 0 else None)

    real_twr = None
    if real:
        cutoff = str(since)
        idx = [i for i, d in enumerate(real["dates"]) if str(d) >= cutoff]
        if len(idx) >= 2:
            real_twr = _twr_chain([real["dates"][i] for i in idx],
                                  [real["nav"][i] for i in idx],
                                  real.get("flows") or {})

    return {
        "since": str(since),
        "as_of": str(today),
        "paper": {
            "n": len(rets),
            "mean_return_pct": round(statistics.fmean(rets) * 100, 2),
            "hit": round(sum(1 for r in rets if r > 0) / len(rets), 4),
            "mean_excess_pct": (round(statistics.fmean(excesses) * 100, 2)
                                if excesses else None),
        },
        "vwra_pct": vwra_pct,
        "real_twr_pct": real_twr,
        "note": ("paper = every CONFIRM equal-weight buy-and-hold at signal price, "
                 "no costs/sizing/timing; real = broker TWR over the same window. "
                 "Direction, not precision — small n."),
    }


# ── Holdings archive analysis (2026-07-11): dd_history was write-only ──
#
# Every daily portfolio debate has been archived to dd_history since May
# and never read by any analysis (audit: the richest table, write-only).
# This asks it the calibration question: whose scores actually predict
# forward excess — which AGENT's tilt, which archetype, which margin-of-
# safety band. Measurement only; no methodology change.

_TILT_THRESHOLD = 0.5  # an agent ≥0.5 above/below panel mean is a real tilt


def parse_dd_rows(rows: list[dict]) -> list[dict]:
    """dd_history rows → one observation per ticker per ISO week (daily
    re-analyses of the same holding are autocorrelated; keeping them all
    would let one name's streak masquerade as n=60)."""
    seen: set = set()
    out = []
    for r in rows:
        ticker = (r.get("ticker") or "").strip().upper()
        try:
            run = datetime.fromisoformat(str(r["run_at"])).date()
        except Exception:
            continue
        iso = run.isocalendar()
        key = (ticker, iso[0], iso[1])
        if not ticker or key in seen:
            continue
        seen.add(key)
        agents = r.get("agent_scores") or {}
        out.append({
            "ticker":       ticker,
            "discovered":   run,           # named like signals so forward_return applies
            "entry_price":  r.get("price"),
            "score":        r.get("score"),
            "archetype":    r.get("archetype") or "(none)",
            "mos":          r.get("mos"),
            "agent_scores": agents if isinstance(agents, dict) else {},
        })
    return out


def compute_holdings_analysis(obs: list[dict], closes: dict, vwra: pd.Series,
                              windows: list[int], today: date) -> dict | None:
    """Per-agent tilt calibration + archetype/MOS buckets over dd_history.
    Same window/bucket shape as the signal scoreboard so the dashboard
    heatmap renderer can be reused."""
    if not obs:
        return None
    mos_labels = tercile_labels([o["mos"] for o in obs])
    for o, m in zip(obs, mos_labels):
        o["mos_bucket"] = m

    def _tilts(o) -> list[str]:
        agents = o["agent_scores"]
        vals = [v for v in agents.values() if isinstance(v, (int, float))]
        if len(vals) < 2:
            return []
        mean_score = sum(vals) / len(vals)
        out = []
        for agent, v in agents.items():
            if not isinstance(v, (int, float)):
                continue
            dev = v - mean_score
            if dev >= _TILT_THRESHOLD:
                out.append(f"{agent}:bullish")
            elif dev <= -_TILT_THRESHOLD:
                out.append(f"{agent}:bearish")
        return out

    out_windows = []
    for weeks in windows:
        measured = []
        for o in obs:
            fr = forward_return(closes.get(o["ticker"]), o["entry_price"],
                                o["discovered"], weeks, today)
            if fr["status"] != "ok":
                continue
            vf = forward_return(vwra, None, o["discovered"], weeks, today)
            if vf["status"] != "ok":
                continue
            measured.append({**o, "excess": fr["ret"] - vf["ret"]})
        win: dict = {"weeks": weeks, "measurable": len(measured)}
        if measured:
            # agent tilt: one row can appear in several agent buckets — that's
            # the point (each agent's calibration is scored independently)
            tilt_rows = [{**m, "_tilt": t} for m in measured for t in _tilts(m)]
            win["buckets"] = {
                "agent_tilt": [{"k": str(k), **_round_stats(s)}
                               for k, s in bucket_stats(tilt_rows, lambda r: r["_tilt"]).items()] if tilt_rows else [],
                "archetype":  [{"k": str(k), **_round_stats(s)}
                               for k, s in bucket_stats(measured, lambda r: r["archetype"]).items()],
                "mos":        [{"k": str(k), **_round_stats(s)}
                               for k, s in bucket_stats(measured, lambda r: r["mos_bucket"] or "n/a").items()],
            }
        out_windows.append(win)
    return {
        "n_obs": len(obs),
        "note": ("dd_history holdings re-analyses, deduped to one obs per ticker "
                 "per ISO week. agent_tilt = that agent scored ≥0.5 above/below "
                 "the panel mean. Calibration read, not a lever — small n."),
        "windows": out_windows,
    }


# ── Report (text rendering of the scoreboard) ─────────────────────────


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _bucket_lines(title: str, entries: list[dict]) -> list[str]:
    lines = [f"  {title}:"]
    for s in entries:
        flag = "  ⚠ n<10" if s["n"] < 10 else ""
        lines.append(f"    {s['k']:<14} n={s['n']:<3} hit {s['hit'] * 100:4.0f}%  "
                     f"mean {_pct(s['mean']):>7}  median {_pct(s['median']):>7}{flag}")
    return lines


def render_report(sb: dict) -> list[str]:
    lines = [
        "═" * 64,
        "SIGNAL SCOREBOARD — forward returns vs VWRA (matched windows)",
        f"as of {sb['as_of']} · scout {sb['n_scout']} + gems {sb['n_gems']} signals "
        f"· benchmark {sb['benchmark']}",
        "excess = stock forward return − VWRA forward return.",
        f"note: {sb['note']};",
        "      daily-close granularity, dates matched within ±3 days.",
    ]
    for ver in sb.get("versions", []):
        lines.append(f"methodology {ver['v']}: {ver['desc']}")
    lines.append("═" * 64)
    for win in sb["windows"]:
        lines += ["", f"── {win['weeks']}-week forward ──",
                  f"  measurable {win['measurable']} · pending {win['pending']} "
                  f"· no-data {win['no_data']}"]
        if not win.get("overall"):
            lines.append("  (nothing measurable in this window yet)")
            continue
        lines += _bucket_lines("overall", [{"k": "ALL", **win["overall"]}])
        for key, title, _fn in _BUCKETS:
            lines += _bucket_lines(title, win["buckets"][key])
        fmt = lambda e: f"{e['ticker']} {_pct(e['excess'])}"
        lines.append("  top:    " + " · ".join(fmt(e) for e in win["top"]))
        lines.append("  bottom: " + " · ".join(fmt(e) for e in win["bottom"]))

    bg = sb.get("behavior_gap")
    if bg:
        p = bg["paper"]
        lines += [
            "", f"── behavior gap (since {bg['since']}) ──",
            f"  paper ({p['n']} CONFIRMs, eq-weight, buy&hold): {p['mean_return_pct']:+.1f}%"
            + (f" · excess {p['mean_excess_pct']:+.1f}%" if p["mean_excess_pct"] is not None else "")
            + f" · hit {p['hit'] * 100:.0f}%",
            "  VWRA same window: "
            + (f"{bg['vwra_pct']:+.1f}%" if bg["vwra_pct"] is not None else "n/a"),
            "  real account TWR: "
            + (f"{bg['real_twr_pct']:+.1f}%" if bg["real_twr_pct"] is not None
               else "n/a (no broker NAV series)"),
            f"  note: {bg['note']}",
        ]

    ha = sb.get("holdings_analysis")
    if ha:
        lines += ["", f"── holdings archive (dd_history · {ha['n_obs']} weekly obs) ──"]
        for win in ha["windows"]:
            if not win.get("buckets"):
                continue
            lines.append(f"  {win['weeks']}-week · measurable {win['measurable']}")
            lines += _bucket_lines("agent tilt", win["buckets"]["agent_tilt"][:10])
        lines.append(f"  note: {ha['note']}")
    return lines


def build_report(signals: list[dict], closes: dict, vwra: pd.Series,
                 windows: list[int], today: date) -> list[str]:
    """Compute + render in one call (kept as the simple entry point for tests)."""
    return render_report(compute_scoreboard(signals, closes, vwra, windows, today))


def digest_due(today: date) -> bool:
    """Weekly digest fires on Mondays only."""
    return today.weekday() == 0


# ── Orchestration ─────────────────────────────────────────────────────


def _download_closes(tickers: list[str], start: date) -> dict:
    """{ticker: close Series} via one batched yfinance call (chunked >100)."""
    import yfinance as yf  # lazy — tests never touch the network

    closes: dict = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        data = yf.download(chunk, start=str(start), auto_adjust=True,
                           progress=False, group_by="ticker", threads=True)
        if data is None or data.empty:
            continue
        for t in chunk:
            try:
                series = data[t]["Close"] if len(chunk) > 1 else data["Close"]
                series = series.dropna()
                series.index = pd.DatetimeIndex(series.index).tz_localize(None).normalize()
                if not series.empty:
                    closes[t] = series
            except Exception:
                continue
    return closes


def main() -> int:
    # Windows consoles default to cp1252, which can't render the report glyphs.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", default=_DEFAULT_WINDOWS,
                    help=f"comma-separated forward windows in weeks (default {_DEFAULT_WINDOWS} — "
                         "26/52 read as 'pending' until signals age into them, ~Dec 2026 / "
                         "~Jun 2027, but the buckets must exist to ever measure Daryl's stated "
                         "'in any given year' horizon)")
    ap.add_argument("--markdown", default="", help="also write the text report to this file")
    ap.add_argument("--json", default="",
                    help="write the scoreboard JSON snapshot to this file "
                         "(picked up by upload_kv.py → KV dd:scoreboard)")
    ap.add_argument("--digest-weekly", action="store_true",
                    help="send the Telegram scoreboard digest (Mondays only)")
    args = ap.parse_args()
    windows = [int(w) for w in args.windows.split(",") if w.strip()]

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[analysis] SUPABASE_URL / SUPABASE_KEY not set — nothing to measure")
        return 1

    print("[analysis] Fetching signal history from Supabase...")
    signals = (parse_rows(_fetch_rows("scout_history"), "scout")
               + parse_rows(_fetch_rows("gems_history"), "gems"))
    if not signals:
        print("[analysis] No signal rows found.")
        return 1
    print(f"  {len(signals)} signal(s), "
          f"{sum(1 for s in signals if s['entry_price'] is None)} without entry price")

    today = date.today()
    tickers = sorted({s["ticker"] for s in signals})
    start = min(s["discovered"] for s in signals) - timedelta(days=7)
    print(f"[analysis] Downloading closes for {len(tickers)} tickers + {BENCHMARK} "
          f"since {start}...")
    closes = _download_closes(tickers + [BENCHMARK], start)
    vwra = closes.pop(BENCHMARK, None)
    if vwra is None or vwra.empty:
        print(f"[analysis] No price data for benchmark {BENCHMARK} — aborting")
        return 1
    missing = [t for t in tickers if t not in closes]
    if missing:
        print(f"  no price data for {len(missing)}: {', '.join(missing[:10])}"
              + (" …" if len(missing) > 10 else ""))

    sb = compute_scoreboard(signals, closes, vwra, windows, today)
    bg = compute_behavior_gap(signals, closes, vwra, today, real=fetch_real_nav())
    if bg:
        sb["behavior_gap"] = bg
    try:
        bt = compute_backtest(signals, closes, vwra, today)
        if bt:
            sb["backtest"] = bt
            print(f"[analysis] backtest: {bt['n_confirms']} CONFIRMs, "
                  f"equal {bt['stats']['equal']['total'] * 100:+.1f}% vs VWRA "
                  f"{bt['stats']['vwra']['total'] * 100:+.1f}% "
                  f"(excess {bt['stats']['excess_total'] * 100:+.1f}%)")
    except Exception as e:
        print(f"[analysis] backtest skipped ({e})")

    # Holdings archive (dd_history) — isolated: a failure here must never
    # cost the signal scoreboard.
    try:
        dd_rows = _fetch_rows(
            "dd_history",
            select="ticker,run_at,price,score,agent_scores,archetype,mos",
            order="run_at.asc")
        obs = parse_dd_rows(dd_rows)
        extra = sorted({o["ticker"] for o in obs} - set(closes))
        if extra and obs:
            closes.update(_download_closes(
                extra, min(o["discovered"] for o in obs) - timedelta(days=7)))
        ha = compute_holdings_analysis(obs, closes, vwra, windows, today)
        if ha:
            sb["holdings_analysis"] = ha
            print(f"[analysis] holdings archive: {ha['n_obs']} weekly obs "
                  f"from {len(dd_rows)} dd_history rows")
    except Exception as e:
        print(f"[analysis] holdings analysis skipped ({e})")

    report = render_report(sb)
    print("\n".join(report))

    if args.markdown:
        from pathlib import Path
        Path(args.markdown).write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"\n[analysis] Report written to {args.markdown}")

    if args.json:
        from pathlib import Path
        Path(args.json).write_text(json.dumps(sb, separators=(",", ":")) + "\n",
                                   encoding="utf-8")
        print(f"[analysis] Scoreboard JSON written to {args.json}")

    if args.digest_weekly:
        if digest_due(today):
            from notify import alert_scoreboard_digest
            ok = alert_scoreboard_digest(sb)
            print(f"[analysis] Weekly digest {'sent' if ok else 'FAILED'}")
        else:
            print("[analysis] --digest-weekly: not Monday — skipping digest")

    return 0


if __name__ == "__main__":
    sys.exit(main())
