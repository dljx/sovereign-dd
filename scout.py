"""Scout mode — quantitative screener → Gemma triage → full debate.

Every run:
  1. All 7 screener calls fire simultaneously (cheap REST calls, different financial profiles)
  2. One grounded Gemma call picks the 6 most interesting from the combined pool
  3. Full 5-agent debate on those 6 picks — all run in parallel (max 3 concurrent)
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

FMP_KEY = os.getenv("FMP_API_KEY", "")
FMP = "https://financialmodelingprep.com/api/v3"

BUY_THRESHOLD = 7.0

# ── Screener lens definitions ──────────────────────────────────────────────────

SCREENER_LENSES: list[dict] = [
    {
        "name": "value",
        "desc": "Mid-cap stocks that may be trading at discounts — low beta, moderate market cap, all sectors",
        "params": {
            "marketCapMoreThan": 300_000_000,
            "marketCapLowerThan": 15_000_000_000,
            "betaLowerThan": 1.1,
            "volumeMoreThan": 100_000,
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 40,
        },
    },
    {
        "name": "growth",
        "desc": "Technology, healthcare, and communication services companies with meaningful scale",
        "params": {
            "marketCapMoreThan": 500_000_000,
            "marketCapLowerThan": 50_000_000_000,
            "volumeMoreThan": 300_000,
            "sector": "Technology",
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 30,
        },
    },
    {
        "name": "momentum",
        "desc": "Higher-beta, high-volume names where price action may be accelerating",
        "params": {
            "marketCapMoreThan": 1_000_000_000,
            "betaMoreThan": 0.9,
            "volumeMoreThan": 800_000,
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 35,
        },
    },
    {
        "name": "small_cap",
        "desc": "Small-cap ($100M–$2B) stocks with enough liquidity — the hidden gem pool",
        "params": {
            "marketCapMoreThan": 100_000_000,
            "marketCapLowerThan": 2_000_000_000,
            "volumeMoreThan": 50_000,
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 50,
        },
    },
    {
        "name": "contrarian",
        "desc": "Mid-cap names that may have been oversold — moderately volatile, broad sector coverage",
        "params": {
            "marketCapMoreThan": 400_000_000,
            "marketCapLowerThan": 25_000_000_000,
            "betaMoreThan": 0.7,
            "volumeMoreThan": 200_000,
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 35,
        },
    },
    {
        "name": "macro_tailwind",
        "desc": "Industrials — cyclical and rate-sensitive",
        "params": {
            "marketCapMoreThan": 300_000_000,
            "sector": "Industrials",
            "volumeMoreThan": 150_000,
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 25,
        },
    },
    {
        "name": "macro_tailwind",
        "desc": "Energy sector — cyclical and rate-sensitive",
        "params": {
            "marketCapMoreThan": 300_000_000,
            "sector": "Energy",
            "volumeMoreThan": 150_000,
            "isActivelyTrading": "true",
            "exchange": "NYSE,NASDAQ",
            "limit": 20,
        },
    },
]


# ── FMP helpers ────────────────────────────────────────────────────────────────

def _fmp_screen(lens: dict) -> tuple[dict, list[dict]]:
    """Call FMP stock screener for one lens. Returns (lens, results)."""
    if not FMP_KEY:
        return lens, []
    try:
        r = requests.get(
            f"{FMP}/stock-screener",
            params={"apikey": FMP_KEY, **lens["params"]},
            timeout=20,
        )
        if not r.ok:
            return lens, []
        data = r.json()
        return lens, data if isinstance(data, list) else []
    except Exception as e:
        print(f"  [scout] FMP screener error ({lens['name']}): {e}")
        return lens, []


async def _run_all_screeners(portfolio: set[str]) -> list[dict]:
    """Run all lenses in parallel. Returns deduplicated candidate list."""
    results = await asyncio.gather(*[
        asyncio.to_thread(_fmp_screen, lens) for lens in SCREENER_LENSES
    ])

    seen: set[str] = set()
    candidates: list[dict] = []

    for lens, items in results:
        for item in items:
            sym = item.get("symbol", "").upper().strip()
            if not sym or sym in seen or sym in portfolio:
                continue
            if not re.match(r'^[A-Z]{1,5}$', sym):
                continue
            seen.add(sym)
            candidates.append({
                "ticker":   sym,
                "name":     item.get("companyName", sym),
                "sector":   item.get("sector", "—"),
                "industry": item.get("industry", "—"),
                "mcap_b":   round((item.get("marketCap") or 0) / 1e9, 2),
                "price":    item.get("price", 0),
                "beta":     item.get("beta", 0),
                "volume":   item.get("volume", 0),
                "lens":     lens.get("name", ""),
            })

    return candidates


# ── Gemma triage ───────────────────────────────────────────────────────────────

TRIAGE_SYSTEM = """You are a senior equity analyst with deep experience across all market caps and sectors.
You have access to live market data via Google Search. Your job is to identify the 6 most
compelling investment opportunities from a pre-screened candidate list.

Prioritise stocks with:
- Clear near-term catalysts (earnings, product launches, regulatory approvals, contract wins)
- Improving fundamentals that the market may not have fully priced in
- Unusual valuation discounts relative to quality
- Strong insider buying or institutional accumulation signals
- Sector tailwinds aligned with current macro environment
- Small/mid-cap names with asymmetric upside that institutional coverage has missed

Bias toward less-covered names where genuine alpha exists. Avoid defaulting to household names
unless they have a specific, timely catalyst."""


def _build_triage_prompt(candidates: list[dict], portfolio: set[str]) -> str:
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
        "\nSelect EXACTLY 6 tickers from this list that represent the best risk-adjusted "
        "opportunities based on your web research. Prefer less-covered names with asymmetric upside.",
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
) -> list[dict]:
    from llm import call_gemini_async, extract_json

    if not candidates:
        return []

    prompt = _build_triage_prompt(candidates, portfolio)

    if verbose:
        print(f"  [scout] Triaging {len(candidates)} candidates with grounded Gemma...")

    text = await call_gemini_async(TRIAGE_SYSTEM, prompt, grounding=True, temperature=0.3)

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
        return valid[:6]
    except Exception as e:
        print(f"  [scout] Triage parse error: {e}\n  Raw: {text[:300]}")
        import random
        sample = random.sample(candidates, min(6, len(candidates)))
        return [{"ticker": c["ticker"], "lens": c["lens"], "rationale": "fallback pick"} for c in sample]


# ── Main entry point ───────────────────────────────────────────────────────────

async def run_scout(
    max_tickers: int = 6,
    portfolio: list[str] | None = None,
    verbose: bool = True,
) -> list[dict]:
    """
    Full scout pipeline:
      1. All screener lenses fire simultaneously
      2. Gemma triage picks the 6 most interesting (grounded)
      3. Full 5-agent debate on all picks in parallel (max 3 concurrent)

    Returns list of BUY discovery dicts (score >= BUY_THRESHOLD only).
    """
    from dossier import build as build_dossier
    from debate import run as run_debate

    portfolio_set = {t.upper() for t in (portfolio or [])}

    # Phase 1 — screeners (all lenses in parallel)
    if verbose:
        print(f"\n+----------------------------------------------+")
        print(f"|  SOVEREIGN SCOUT — quantitative screen       |")
        print(f"|  Running all {len(SCREENER_LENSES)} lenses simultaneously...     |")
        print(f"+----------------------------------------------+")

    candidates = await _run_all_screeners(portfolio_set)

    if verbose:
        by_lens: dict[str, int] = {}
        for c in candidates:
            by_lens[c["lens"]] = by_lens.get(c["lens"], 0) + 1
        for lens_name, count in by_lens.items():
            print(f"    {lens_name:<15} → {count} candidates")
        print(f"  Total unique candidates: {len(candidates)}")

    if not candidates:
        print("  [scout] No candidates — check FMP_API_KEY")
        return []

    # Phase 2 — Gemma triage (one grounded call)
    picks = await _triage_with_gemma(candidates, portfolio_set, verbose=verbose)

    if not picks:
        print("  [scout] Triage returned no picks")
        return []

    picks = picks[:max_tickers]

    if verbose:
        print(f"\n+----------------------------------------------+")
        print(f"|  SOVEREIGN SCOUT — running debates           |")
        tickers_str = ", ".join(p["ticker"] for p in picks)
        print(f"|  Picks: {tickers_str:<39}|")
        print(f"|  All {len(picks)} debates run in parallel (max 3)       |")
        print(f"+----------------------------------------------+")

    # Phase 3 — parallel debates (max 3 concurrent)
    out_dir = Path("output/scouts")
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)

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

                dossier = await build_dossier(ticker, verbose=False)
                result  = await run_debate(ticker, dossier, verbose=False)

                score = result.get("consensus_score", 0)
                grade = result.get("consensus_grade", "HOLD")

                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                out_path = out_dir / f"{ticker}_{ts}.json"
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump({"result": result, "dossier": dossier}, f, indent=2, default=str)

                if verbose:
                    print(f"  [scout] {ticker} → {score:.2f}/10 [{grade}]"
                          + (" ← BUY SIGNAL" if score >= BUY_THRESHOLD else ""))

                if score >= BUY_THRESHOLD:
                    return {
                        "ticker":           ticker,
                        "score":            round(score, 2),
                        "grade":            grade,
                        "confidence":       result.get("confidence", ""),
                        "thesis":           result.get("majority_thesis", ""),
                        "key_swing_factor": result.get("key_swing_factor", ""),
                        "scout_lens":       lens,
                        "gemma_rationale":  rationale,
                        "analyzed_at":      ts,
                        "output_file":      str(out_path),
                    }
                return None
            except Exception as e:
                print(f"  [scout] {ticker} failed: {e}")
                return None

    results = await asyncio.gather(*[_debate_one(p) for p in picks])
    discoveries = [r for r in results if r is not None]

    if verbose:
        print(f"\n  Scout complete: {len(discoveries)} BUY signal(s) "
              f"from {len(picks)} debated across all lenses")

    return discoveries
