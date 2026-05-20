"""Sovereign DD — Consensus-Based Stock Rating Multi-Agent Framework.

Usage:
    python main.py TICKER                          # single ticker
    python main.py TICKER --save                   # single ticker, save JSON
    python main.py --portfolio --save --notify     # all portfolio tickers + Telegram summary
    python main.py --scout --save --notify         # scout mode + Telegram BUY alerts
    python main.py --gems [--save] [--notify]      # gems pipeline + Telegram BUY alerts
    python main.py --scout --gems [--save] [--notify]  # scout + gems concurrently

Portfolio tickers are read from the PORTFOLIO_TICKERS env var (comma-separated).
"""

import asyncio
import json
import os
import sys
os.environ.setdefault("PYTHONUNBUFFERED", "1")
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
from datetime import datetime, timezone
from pathlib import Path

from dossier import build as build_dossier
from debate import run as run_debate
from report import render, console


# ── Helpers ───────────────────────────────────────────────────────────────────

def _portfolio_tickers() -> list[str]:
    raw = os.getenv("PORTFOLIO_TICKERS", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tickers:
        console.print("[red]PORTFOLIO_TICKERS env var is empty or not set[/red]")
        sys.exit(1)
    return tickers


def _save_result(ticker: str, result: dict, dossier: dict, subdir: str = "") -> Path:
    base = Path("output") / subdir if subdir else Path("output")
    base.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = base / f"{ticker}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump({"result": result, "dossier": dossier}, f, indent=2, default=str)
    return out_path


# ── Single ticker ─────────────────────────────────────────────────────────────

async def _run_single(ticker: str, save: bool = False):
    console.rule(f"[bold blue]Sovereign DD — {ticker}[/bold blue]")
    dossier = await build_dossier(ticker, verbose=True)
    result  = await run_debate(ticker, dossier, verbose=True)
    console.rule("[bold]FINAL REPORT[/bold]")
    render(result, dossier)
    if save:
        out_path = _save_result(ticker, result, dossier)
        console.print(f"[dim]Saved to {out_path}[/dim]")
    return result, dossier


# ── Portfolio mode — all tickers in parallel (max 3 concurrent) ───────────────

async def _run_portfolio(save: bool = False, notify: bool = False):
    tickers = _portfolio_tickers()
    console.rule(
        f"[bold blue]Sovereign DD — Portfolio scan "
        f"({len(tickers)} tickers, 3 concurrent)[/bold blue]"
    )

    sem = asyncio.Semaphore(3)

    async def _analyze_one(ticker: str) -> dict:
        async with sem:
            try:
                console.rule(f"[blue]{ticker}[/blue]")
                dossier = await build_dossier(ticker, verbose=True)
                result  = await run_debate(ticker, dossier, verbose=True)
                console.rule(f"[bold]{ticker} FINAL REPORT[/bold]")
                render(result, dossier)
                if save:
                    out_path = _save_result(ticker, result, dossier)
                    console.print(f"[dim]Saved to {out_path}[/dim]")
                return {
                    "ticker": ticker,
                    "score":  result.get("consensus_score", 0),
                    "grade":  result.get("consensus_grade", "?"),
                }
            except Exception as e:
                console.print(f"[red]  {ticker} failed: {e}[/red]")
                return {"ticker": ticker, "score": 0, "grade": "ERROR"}

    summaries = list(await asyncio.gather(*[_analyze_one(t) for t in tickers]))

    if notify and summaries:
        from notify import alert_portfolio_summary
        alert_portfolio_summary(summaries)
        console.print("[dim]Portfolio summary sent to Telegram[/dim]")

    return summaries


# ── Scout mode ────────────────────────────────────────────────────────────────

async def _run_gems(save: bool = False, notify: bool = False):
    from gems import run_gems
    discoveries = await run_gems(verbose=True)

    if notify:
        from notify import alert_buy_signal, alert_scout_summary, alert_dd_result
        for d in discoveries:
            alert_buy_signal(d)
            if d.get("output_file"):
                try:
                    with open(d["output_file"]) as f:
                        data = json.load(f)
                    alert_dd_result(data["result"])
                except Exception:
                    pass
        if discoveries:
            alert_scout_summary(discoveries)
        console.print(f"[dim]Gems alerts sent to Telegram ({len(discoveries)} signal(s))[/dim]")

    return discoveries


async def _run_scout(save: bool = False, notify: bool = False):
    from scout import run_scout, _load_notified, _save_notified, _recently_notified
    portfolio = _portfolio_tickers() if os.getenv("PORTFOLIO_TICKERS") else []
    discoveries = await run_scout(max_tickers=12, portfolio=portfolio, verbose=True)

    if notify:
        from notify import alert_scout_summary, alert_buy_signal, alert_dd_result

        notified   = _load_notified()
        suppressed = _recently_notified(notified)
        alerted    = []

        for d in discoveries:
            ticker = d["ticker"]
            if ticker in suppressed:
                console.print(
                    f"[dim]  [notify] {ticker} already alerted within "
                    f"{os.getenv('SCOUT_NOTIFY_COOLDOWN_HOURS', '168')}h cooldown — skipping[/dim]"
                )
                continue
            alert_buy_signal(d)
            if d.get("output_file"):
                try:
                    with open(d["output_file"]) as f:
                        data = json.load(f)
                    alert_dd_result(data["result"])
                except Exception:
                    pass
            notified[ticker] = {
                "ts":    datetime.now(timezone.utc).timestamp(),
                "score": d["score"],
                "grade": d["grade"],
            }
            _save_notified(notified)
            alerted.append(d)

        alert_scout_summary(alerted)
        console.print(f"[dim]Scout alerts sent to Telegram ({len(alerted)} signal(s))[/dim]")

    return discoveries


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main():
    args = sys.argv[1:]

    save           = "--save"      in args
    notify         = "--notify"    in args
    portfolio_mode = "--portfolio" in args
    scout_mode     = "--scout"     in args
    gems_mode      = "--gems"      in args
    positional     = [a for a in args if not a.startswith("--")]

    if portfolio_mode and scout_mode and gems_mode:
        await asyncio.gather(
            _run_portfolio(save=save, notify=notify),
            _run_scout(save=save, notify=notify),
            _run_gems(save=save, notify=notify),
        )
    elif scout_mode and gems_mode:
        await asyncio.gather(
            _run_scout(save=save, notify=notify),
            _run_gems(save=save, notify=notify),
        )
    elif portfolio_mode and scout_mode:
        await asyncio.gather(
            _run_portfolio(save=save, notify=notify),
            _run_scout(save=save, notify=notify),
        )
    elif portfolio_mode and gems_mode:
        await asyncio.gather(
            _run_portfolio(save=save, notify=notify),
            _run_gems(save=save, notify=notify),
        )
    elif portfolio_mode:
        await _run_portfolio(save=save, notify=notify)
    elif scout_mode:
        await _run_scout(save=save, notify=notify)
    elif gems_mode:
        await _run_gems(save=save, notify=notify)
    elif positional:
        await _run_single(positional[0].upper(), save=save)
    else:
        console.print(
            "[red]Usage:[/red]\n"
            "  python main.py TICKER [--save]\n"
            "  python main.py --portfolio [--save] [--notify]\n"
            "  python main.py --scout [--save] [--notify]\n"
            "  python main.py --gems [--save] [--notify]\n"
            "  python main.py --scout --gems [--save] [--notify]\n"
            "  python main.py --portfolio --gems [--save] [--notify]\n"
            "  python main.py --portfolio --scout --gems [--save] [--notify]\n"
            "  python main.py --portfolio --scout [--save] [--notify]"
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
