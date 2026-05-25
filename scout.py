"""Scout mode — quantitative screener → Gemma triage → full debate.

Every run:
  1. All 7 screener calls fire simultaneously (Yahoo Finance free API, no key needed)
  2. One grounded Gemma call picks the 12 most interesting from the combined pool
  3. Full 6-agent debate on those 12 picks — all run in parallel (max 4 concurrent)
"""

import asyncio
import json
import os
import re
import requests
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BUY_THRESHOLD = 6.5

# ── Continuous-mode knobs (override via env) ───────────────────────────────────
SCOUT_HISTORY_FILE   = Path("output/scout_history.json")
SCOUT_NOTIFIED_FILE  = Path("output/scout_notified.json")
SCOUT_COOLDOWN_HOURS        = int(os.getenv("SCOUT_COOLDOWN_HOURS", "48"))
SCOUT_NOTIFY_COOLDOWN_HOURS = int(os.getenv("SCOUT_NOTIFY_COOLDOWN_HOURS", "168"))  # 7 days
SCOUT_DEBATE_COUNT   = int(os.getenv("SCOUT_DEBATE_COUNT", "6"))
SCOUT_MAX_LOOPS      = int(os.getenv("SCOUT_MAX_LOOPS", "3"))


def _load_history() -> dict:
    """Load {ticker: {ts, score, grade}} from disk. Returns {} if missing or corrupt."""
    try:
        if SCOUT_HISTORY_FILE.exists():
            return json.loads(SCOUT_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_history(history: dict) -> None:
    """Persist scout history to disk."""
    try:
        SCOUT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCOUT_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [scout] Warning: could not save history: {e}")


def _recently_scouted(history: dict) -> set[str]:
    """Return set of tickers analyzed within SCOUT_COOLDOWN_HOURS."""
    cutoff = datetime.now(timezone.utc).timestamp() - SCOUT_COOLDOWN_HOURS * 3600
    return {ticker for ticker, entry in history.items() if entry.get("ts", 0) >= cutoff}


def _load_notified() -> dict:
    """Load {ticker: {ts, score, grade}} Telegram notification history. Returns {} if missing."""
    try:
        if SCOUT_NOTIFIED_FILE.exists():
            return json.loads(SCOUT_NOTIFIED_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_notified(notified: dict) -> None:
    """Persist Telegram notification history to disk."""
    try:
        SCOUT_NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCOUT_NOTIFIED_FILE.write_text(json.dumps(notified, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [scout] Warning: could not save notify history: {e}")


def _recently_notified(notified: dict) -> set[str]:
    """Return set of tickers Telegram-alerted within SCOUT_NOTIFY_COOLDOWN_HOURS."""
    cutoff = datetime.now(timezone.utc).timestamp() - SCOUT_NOTIFY_COOLDOWN_HOURS * 3600
    return {ticker for ticker, entry in notified.items() if entry.get("ts", 0) >= cutoff}


# Yahoo Finance predefined screener API — no key required
YF_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# ── Screener lens definitions ──────────────────────────────────────────────────

SCREENER_LENSES: list[dict] = [
    # ── Cross-sector / factor lenses ───────────────────────────────────────────
    {
        "name": "value",
        "desc": "Large-cap undervalued stocks — low P/E, strong fundamentals",
        "scrId": "undervalued_large_caps",
        "count": 250,
    },
    {
        "name": "growth",
        "desc": "Technology growth stocks with strong revenue momentum",
        "scrId": "growth_technology_stocks",
        "count": 250,
    },
    {
        "name": "momentum",
        "desc": "Most actively traded — high volume, momentum plays",
        "scrId": "most_actives",
        "count": 250,
    },
    {
        "name": "small_cap",
        "desc": "Small-cap gainers — hidden gems with asymmetric upside",
        "scrId": "small_cap_gainers",
        "count": 250,
    },
    {
        "name": "aggressive_small_cap",
        "desc": "Aggressive small-caps — high-risk, high-reward growth",
        "scrId": "aggressive_small_caps",
        "count": 250,
    },
    {
        "name": "contrarian",
        "desc": "Day losers — oversold names with potential reversal setups",
        "scrId": "day_losers",
        "count": 250,
    },
    {
        "name": "macro_tailwind",
        "desc": "Undervalued growth — cyclical and macro-sensitive opportunities",
        "scrId": "undervalued_growth_stocks",
        "count": 250,
    },
    {
        "name": "breakout",
        "desc": "Day gainers — strong price action with near-term catalysts",
        "scrId": "day_gainers",
        "count": 250,
    },
    {
        "name": "quality",
        "desc": "Portfolio anchors — quality large-caps with durable franchises",
        "scrId": "portfolio_anchors",
        "count": 250,
    },
    # ── Sector lenses (Morningstar via YF) — ensures full market coverage ──────
    {
        "name": "sector_financials",
        "desc": "Financial services — banks, insurance, asset managers, fintech",
        "scrId": "ms_financial_services",
        "count": 250,
    },
    {
        "name": "sector_healthcare",
        "desc": "Healthcare — biotech, pharma, medtech, managed care",
        "scrId": "ms_healthcare",
        "count": 250,
    },
    {
        "name": "sector_energy",
        "desc": "Energy — oil & gas, pipelines, renewables",
        "scrId": "ms_energy",
        "count": 250,
    },
    {
        "name": "sector_industrials",
        "desc": "Industrials — aerospace, defense, machinery, construction",
        "scrId": "ms_industrials",
        "count": 250,
    },
    {
        "name": "sector_consumer_cyclical",
        "desc": "Consumer cyclical — autos, retail, restaurants, travel",
        "scrId": "ms_consumer_cyclical",
        "count": 250,
    },
    {
        "name": "sector_consumer_defensive",
        "desc": "Consumer defensive — staples, beverages, household products",
        "scrId": "ms_consumer_defensive",
        "count": 250,
    },
    {
        "name": "sector_real_estate",
        "desc": "Real estate — REITs, data centers, industrial, residential",
        "scrId": "ms_real_estate",
        "count": 250,
    },
    {
        "name": "sector_basic_materials",
        "desc": "Basic materials — miners, chemicals, steel, forestry",
        "scrId": "ms_basic_materials",
        "count": 250,
    },
    {
        "name": "sector_communication",
        "desc": "Communication services — media, telecom, social, streaming",
        "scrId": "ms_communication_services",
        "count": 250,
    },
    {
        "name": "sector_utilities",
        "desc": "Utilities — electric, gas, water — defensive yield plays",
        "scrId": "ms_utilities",
        "count": 250,
    },
]


# ── Yahoo Finance screener helpers ─────────────────────────────────────────────

def _yf_screen(lens: dict) -> tuple[dict, list[dict]]:
    """Call Yahoo Finance predefined screener for one lens. Returns (lens, results)."""
    try:
        r = requests.get(
            YF_SCREENER_URL,
            params={
                "formatted": "false",
                "scrIds": lens["scrId"],
                "count": lens.get("count", 50),
                "region": "US",
                "lang": "en-US",
            },
            headers=YF_HEADERS,
            timeout=20,
        )
        if not r.ok:
            print(f"  [scout] YF screener HTTP {r.status_code} ({lens['name']})")
            return lens, []
        data = r.json()
        quotes = (
            data.get("finance", {})
                .get("result", [{}])[0]
                .get("quotes", [])
        )
        return lens, quotes if isinstance(quotes, list) else []
    except Exception as e:
        print(f"  [scout] YF screener error ({lens['name']}): {e}")
        return lens, []


async def _run_all_screeners(portfolio: set[str], exclude: set[str] | None = None) -> list[dict]:
    """Run all lenses in parallel. Returns deduplicated candidate list."""
    skip = portfolio | (exclude or set())
    results = await asyncio.gather(*[
        asyncio.to_thread(_yf_screen, lens) for lens in SCREENER_LENSES
    ])

    seen: set[str] = set()
    candidates: list[dict] = []

    for lens, items in results:
        for item in items:
            sym = (item.get("symbol") or "").upper().strip()
            if not sym or sym in seen or sym in skip:
                continue
            # Only plain US equity tickers (no ETFs like SPY, BRK.B, etc.)
            if not re.match(r'^[A-Z]{1,5}$', sym):
                continue
            # Skip very low market cap (< $100M) — too speculative for debates
            mcap = item.get("marketCap") or 0
            if mcap < 100_000_000:
                continue
            seen.add(sym)
            candidates.append({
                "ticker":   sym,
                "name":     item.get("longName") or item.get("shortName") or sym,
                "sector":   item.get("sector") or "—",
                "industry": item.get("industry") or "—",
                "mcap_b":   round(mcap / 1e9, 2),
                "price":    item.get("regularMarketPrice") or item.get("ask") or 0,
                "beta":     item.get("beta") or 0,
                "volume":   item.get("regularMarketVolume") or 0,
                "lens":     lens.get("name", ""),
            })

    return candidates


# ── Gemma triage ───────────────────────────────────────────────────────────────

def _compute_matched_filters(ratios: dict, sector: str = "") -> list[str]:
    matched = []
    fwd_rev_growth = ratios.get("fwd_revenue_growth") or 0
    gross_margin   = ratios.get("gross_margin") or 0

    if fwd_rev_growth >= 0.50:
        matched.append(f"Rev {fwd_rev_growth*100:.0f}%")
        if gross_margin >= 0.60:
            matched.append(f"GM {gross_margin*100:.0f}%")
        if (ratios.get("eps_acceleration") or 0) > 0:
            matched.append("EPS↑")
        if any(s in (sector or "") for s in ["Technology", "Software", "Semiconductor", "Communication"]):
            matched.append("AI/Tech")
    else:
        fwd_peg = ratios.get("fwd_peg")
        if fwd_peg and fwd_peg < 1.5:
            matched.append(f"Fwd PEG {fwd_peg:.1f}")
        rule40 = ratios.get("rule_of_40") or 0
        if rule40 >= 40:
            matched.append(f"R40={rule40:.0f}")
        if gross_margin > 0.50:
            matched.append(f"GM {gross_margin*100:.0f}%")
        fcf_yield = ratios.get("fcf_yield") or 0
        if fcf_yield > 0.05:
            matched.append(f"FCF {fcf_yield*100:.1f}%")

    roic = ratios.get("roic") or 0
    if roic > 0.15:
        matched.append(f"ROIC {roic*100:.0f}%")

    return matched


TRIAGE_SYSTEM = """You are a senior equity analyst with deep experience across all market caps and sectors.
You have access to live market data via Google Search. Your job is to identify the most
compelling investment opportunities from a pre-screened candidate list.

CANDIDATE SELECTION — apply the appropriate path based on revenue growth rate:

PATH B — HYPERGROWTH CANDIDATES (revenue growth >= 50%):
  Do NOT use forward PEG to evaluate these — it understates quality for fast growers.
  Prioritize when at least 2 of the following 3 fundamental conditions are met:
  - Revenue Q/Q acceleration (this quarter faster than last quarter)
  - Gross margin >= 60% and stable or expanding
  - EPS estimates trending UP vs prior quarter (positive revision momentum)
  Sector bonus (not required): AI infrastructure, cloud, custom silicon, datacenter, cybersecurity — structural tailwind adds conviction.
  A high absolute multiple (30-60x forward earnings) does NOT disqualify a hypergrowth name.

PATH A — STANDARD GROWTH CANDIDATES (revenue growth < 50%):
  Forward PEG IS a valid screening signal here.
  Prioritize when:
  - Forward PEG < 1.5: market underpricing NTM growth
  - Rule of 40 >= 40 AND gross margin > 50%: execution quality
  - FCF yield > 5% with stable or growing revenue: capital-efficient compounder
  Also consider: clear near-term catalysts, insider buying, sector tailwinds, small/mid-cap asymmetric upside.

DEPRIORITIZE across all paths:
  - Revenue growth declining for 2+ consecutive quarters with no explained catalyst
  - Heavy net insider selling (>$10M in 90 days) not attributable to planned 10b5-1 sales
  - Earnings estimates being cut while stock is near 52-week high (distribution phase)

Bias toward less-covered names where genuine alpha exists. Spread picks across at least 3 different lenses."""


def _build_triage_prompt(candidates: list[dict], portfolio: set[str], debate_count: int = 12) -> str:
    lines = [
        f"Below is a pre-screened universe of {len(candidates)} US-listed stocks across "
        f"multiple investment lenses (value, growth, momentum, small_cap, contrarian, macro_tailwind).\n",
        "Use Google Search to research current market conditions and identify which of these "
        "represent the most compelling opportunities RIGHT NOW. Consider earnings season, "
        "sector rotation, macro trends, and any recent news or catalysts.\n",
        f"EXCLUDE tickers already in the portfolio: {', '.join(sorted(portfolio)) or 'none'}.\n",
        "CANDIDATE UNIVERSE:",
        f"{'TICKER':<8} {'NAME':<35} {'SECTOR':<25} {'MCAP($B)':<10} {'BETA':<6} {'LENS':<12}",
        "-" * 96,
    ]
    for c in candidates:
        lines.append(
            f"{c['ticker']:<8} {c['name'][:34]:<35} {c['sector'][:24]:<25} "
            f"{c['mcap_b']:<10.2f} {(c['beta'] or 0):<6.2f} {c['lens']:<12}"
        )
    lines += [
        f"\nSelect EXACTLY {debate_count} tickers from this list that represent the best risk-adjusted "
        "opportunities based on your web research. Prefer less-covered names with asymmetric upside. "
        "Spread your picks across at least 3 different lenses so the output is diversified.",
        "\nReturn your answer as a JSON object with this exact structure:",
        '{"picks": [',
        '  {"ticker": "SYM", "lens": "value", "rationale": "one concise sentence why"},',
        '  ...',
        ']}',
        "Return ONLY the JSON object. No other text.",
    ]
    return "\n".join(lines)


async def _triage_with_gemma(
    candidates: list[dict],
    portfolio: set[str],
    verbose: bool = True,
    debate_count: int = 6,
) -> list[dict]:
    from llm import call_gemini_async, extract_json

    if not candidates:
        return []

    prompt = _build_triage_prompt(candidates, portfolio, debate_count=debate_count)

    if verbose:
        print(f"  [scout] Triaging {len(candidates)} candidates with grounded Gemma...")

    try:
        text = await call_gemini_async(TRIAGE_SYSTEM, prompt, grounding=True, temperature=0.3)
    except Exception as e:
        print(f"  [scout] Triage LLM call failed: {e}")
        print("  [scout] Gemma quota likely exhausted — skipping run to conserve budget")
        return []

    try:
        parsed = extract_json(text)
        picks = parsed.get("picks", []) if isinstance(parsed, dict) else []
        valid_syms = {c["ticker"] for c in candidates}
        valid = [
            p for p in picks
            if isinstance(p, dict)
            and p.get("ticker", "").upper() in valid_syms
            and p.get("ticker", "").upper() not in portfolio
        ]
        if verbose:
            print(f"  [scout] Gemma selected: {[p['ticker'] for p in valid]}")
        return valid[:debate_count]
    except Exception as e:
        print(f"  [scout] Triage parse error: {e}\n  Raw: {text[:300]}")
        import random
        sample = random.sample(candidates, min(debate_count, len(candidates)))
        return [{"ticker": c["ticker"], "lens": c["lens"], "rationale": "fallback pick"} for c in sample]


# ── Main entry point ───────────────────────────────────────────────────────────

async def run_scout(
    max_tickers: int = 12,
    portfolio: list[str] | None = None,
    verbose: bool = True,
) -> list[dict]:
    """
    Full scout pipeline:
      1. All screener lenses fire simultaneously
      2. Gemma triage picks the N most interesting (grounded)
      3. Full 6-agent debate on all picks in parallel (max 4 concurrent)

    Configurable via env vars:
      SCOUT_DEBATE_COUNT   — tickers to debate per run (default 6)
      SCOUT_MAX_LOOPS      — max debate convergence loops (default 3)
      SCOUT_COOLDOWN_HOURS — hours before re-analyzing a ticker (default 48)

    Returns list of BUY discovery dicts (score >= BUY_THRESHOLD only).
    """
    from dossier import build as build_dossier
    from debate import run as run_debate

    portfolio_set = {t.upper() for t in (portfolio or [])}

    # Load dedup history — skip tickers analyzed within SCOUT_COOLDOWN_HOURS
    history = _load_history()
    recently = _recently_scouted(history)
    if verbose and recently:
        print(f"  [scout] Skipping {len(recently)} recently-analyzed ticker(s): "
              f"{', '.join(sorted(recently))}")

    # Phase 1 — screeners (all lenses in parallel)
    if verbose:
        print(f"\n+----------------------------------------------+")
        print(f"|  SOVEREIGN SCOUT — quantitative screen       |")
        print(f"|  Running all {len(SCREENER_LENSES)} lenses simultaneously...  |")
        print(f"+----------------------------------------------+")

    candidates = await _run_all_screeners(portfolio_set, exclude=recently)

    if verbose:
        by_lens: dict[str, int] = {}
        for c in candidates:
            by_lens[c["lens"]] = by_lens.get(c["lens"], 0) + 1
        for lens_name, count in by_lens.items():
            print(f"    {lens_name:<15} → {count} candidates")
        print(f"  Total unique candidates: {len(candidates)} (excluding {len(recently)} recently scouted)")

    if not candidates:
        if verbose:
            print("  [scout] No new candidates — all tickers within cooldown window")
        return []

    # Phase 2 — Gemma triage (one grounded call)
    picks = await _triage_with_gemma(
        candidates, portfolio_set, verbose=verbose, debate_count=SCOUT_DEBATE_COUNT
    )

    if not picks:
        print("  [scout] Triage returned no picks")
        return []

    picks = picks[:max_tickers]

    # Phase 2b — metadata cleaner: one grounded call for the whole batch.
    # Resolves canonical GICS sector, ADR status, and financials currency before
    # the debates run so the dossier builder has clean inputs for every ticker.
    from cleaner import clean_ticker_batch
    batch_meta = await clean_ticker_batch([p["ticker"] for p in picks])

    if verbose:
        print(f"\n+----------------------------------------------+")
        print(f"|  SOVEREIGN SCOUT — running debates           |")
        tickers_str = ", ".join(p["ticker"] for p in picks)
        print(f"|  Picks: {tickers_str:<39}|")
        print(f"|  {len(picks)} debates · max {SCOUT_MAX_LOOPS} loop(s) · 4 concurrent     |")
        print(f"+----------------------------------------------+")

    # Phase 3 — parallel debates (max 4 concurrent — matches number of API keys)
    out_dir = Path("output/scouts")
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(4)
    history_lock = asyncio.Lock()

    async def _debate_one(pick: dict) -> dict | None:
        ticker    = pick["ticker"].upper()
        lens      = pick.get("lens", "")
        rationale = pick.get("rationale", "")
        async with sem:
            try:
                if verbose:
                    print(f"\n  [scout] Analyzing {ticker} ({lens})...")
                    if rationale:
                        print(f"          Gemma rationale: {rationale[:100]}")

                dossier = await build_dossier(ticker, verbose=False,
                                              meta=batch_meta.get(ticker, {}))
                result  = await run_debate(ticker, dossier, verbose=False, max_loops=SCOUT_MAX_LOOPS)

                score = result.get("consensus_score", 0)
                grade = result.get("consensus_grade", "HOLD")

                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                out_path = out_dir / f"{ticker}_{ts}.json"
                _ratios  = dossier.get("ratios_ttm", {})
                _path    = "B" if (_ratios.get("fwd_revenue_growth") or 0) >= 0.50 else "A"
                _matched = _compute_matched_filters(_ratios, dossier.get("sector", ""))
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump({"result": result, "dossier": dossier, "meta": {"path": _path, "matched_filters": _matched}}, f, indent=2, default=str)

                if verbose:
                    print(f"  [scout] {ticker} → {score:.2f}/10 [{grade}]"
                          + (" ← BUY SIGNAL" if score >= BUY_THRESHOLD else ""))

                # Record in history regardless of grade (prevents re-analysis in cooldown window)
                # Save immediately so a mid-run crash doesn't lose completed tickers
                async with history_lock:
                    history[ticker] = {
                        "ts":    datetime.now(timezone.utc).timestamp(),
                        "score": round(score, 2),
                        "grade": grade,
                    }
                    _save_history(history)

                if score >= BUY_THRESHOLD:
                    return {
                        "ticker":           ticker,
                        "score":            round(score, 2),
                        "grade":            grade,
                        "confidence":       result.get("confidence", ""),
                        "thesis":           result.get("majority_thesis", ""),
                        "score_rationale":  result.get("score_rationale", ""),
                        "dissent":          result.get("dissent", ""),
                        "key_swing_factor": result.get("key_swing_factor", ""),
                        "catalyst":         result.get("catalyst", ""),
                        "asymmetry_ratio":  result.get("asymmetry_ratio", ""),
                        "banger":           result.get("banger", {}),
                        "position_guidance": result.get("position_guidance", {}),
                        "cycle_position":   result.get("cycle_position", {}),
                        "path":             _path,
                        "matched_filters":  _matched,
                        "scout_lens":       lens,
                        "gemma_rationale":  rationale,
                        "analyzed_at":      ts,
                        "output_file":      str(out_path),
                    }
                return None
            except Exception as e:
                print(f"  [scout] {ticker} failed: {e}")
                # Mark in history so this ticker isn't re-queued until the cooldown expires.
                # Prevents quota-exhausted tickers from being re-debated every run.
                async with history_lock:
                    history[ticker] = {
                        "ts":    datetime.now(timezone.utc).timestamp(),
                        "score": 0.0,
                        "grade": "FAILED",
                    }
                    _save_history(history)
                return None

    results = await asyncio.gather(*[_debate_one(p) for p in picks])
    discoveries = [r for r in results if r is not None]

    if verbose:
        print(f"\n  Scout complete: {len(discoveries)} BUY signal(s) "
              f"from {len(picks)} debated · history now has {len(history)} ticker(s)")

    return discoveries
