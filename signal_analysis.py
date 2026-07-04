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
    python signal_analysis.py [--windows 1,4,12] [--markdown out.md]
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


# ── Supabase fetch ────────────────────────────────────────────────────


def _fetch_rows(table: str) -> list[dict]:
    """All rows from a history table, paginated via PostgREST Range headers."""
    rows: list[dict] = []
    start = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"select": "*", "order": "discovered_at.asc"},
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


def forward_return(closes: pd.Series, entry_price: float | None,
                   discovered: date, weeks: int, today: date) -> dict:
    """Return over `weeks` from a signal. entry_price=None falls back to the
    close nearest the discovery date (flagged entry_approx). Statuses:
    ok / pending (window not yet elapsed) / no_data (elapsed but unpriceable)."""
    target = discovered + timedelta(weeks=weeks)
    if target > today:
        return {"status": "pending"}
    entry, entry_approx = entry_price, False
    if entry is None:
        hit = close_near(closes, discovered)
        if hit is None:
            return {"status": "no_data"}
        entry, entry_approx = hit[1], True
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

# Bucket key → (json key, report title). Order is the report/panel order.
_BUCKETS = [
    ("grade",     "grade",            lambda r: r["grade"]),
    ("gate",      "gate",             lambda r: {True: "confirmed", False: "rejected"}.get(r["confirmed"], "unknown")),
    ("verdict",   "verdict",          lambda r: r["verdict"]),
    ("mom_12_1",  "mom_12_1 tercile", lambda r: r["mom_bucket"] or "n/a"),
    ("quality",   "quality tercile",  lambda r: r["quality_bucket"] or "n/a"),
    ("factors_v", "factors.v",        lambda r: f"v{r['factors_v']}" if r["factors_v"] else "unstamped"),
    ("source",    "source",           lambda r: r["src"]),
]


def _round_stats(s: dict) -> dict:
    return {"n": s["n"], "hit": round(s["hit"], 4),
            "mean": round(s["mean"], 4), "median": round(s["median"], 4)}


def compute_scoreboard(signals: list[dict], closes: dict, vwra: pd.Series,
                       windows: list[int], today: date) -> dict:
    """The scoreboard as data — single source for the text report, the JSON
    uploaded to the dashboard (KV dd:scoreboard), and the Telegram digest."""
    n_scout = sum(1 for s in signals if s["src"] == "scout")

    # Terciles are computed once across all signals (entry-time factor profile).
    mom_lab = tercile_labels([s["mom_12_1"] for s in signals])
    qual_lab = tercile_labels([s["quality"] for s in signals])
    for s, m, q in zip(signals, mom_lab, qual_lab):
        s["mom_bucket"], s["quality_bucket"] = m, q

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
        "═" * 64,
    ]
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
    ap.add_argument("--windows", default="1,4,12",
                    help="comma-separated forward windows in weeks (default 1,4,12)")
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
