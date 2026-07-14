"""Data dossier builder â€" async, parallel fetches per ticker, shared macro cache."""

import asyncio
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf
from dotenv import load_dotenv

from cache import cached
from live_events import emit_live

load_dotenv()

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
FRED_KEY    = os.getenv("FRED_API_KEY", "")
FMP_KEY     = os.getenv("FMP_API_KEY", "")
_av_keys = [k.strip() for k in os.getenv("ALPHA_VANTAGE_API_KEYS", os.getenv("ALPHA_VANTAGE_API_KEY", "")).split(",") if k.strip()]

FH = "https://finnhub.io/api/v1"

_av_idx = 0
_av_lock = threading.Lock()      # serialize AV key rotation + rate-limit enforcement
_av_last_call: float = 0.0       # timestamp of most recent AV request
_AV_MIN_INTERVAL = 12.0          # seconds between calls (5 RPM limit = 1 per 12s)

_fmp_lock = threading.Lock()     # FMP rate-limit: 10 RPM free tier
_fmp_last_call: float = 0.0
_FMP_MIN_INTERVAL = 6.0          # seconds between calls (10 RPM = 1 per 6s)

_finviz_lock = threading.Lock()  # no published limit; throttled defensively —
_finviz_last_call: float = 0.0   # same spacing finviz_screener.enrich_candidates
_FINVIZ_MIN_INTERVAL = 1.5       # already uses against this exact site


# ── Cycle type classification ──────────────────────────────────────────────────
_CYCLICAL_SECTORS = {"Energy", "Basic Materials", "Consumer Cyclical", "Real Estate"}
_SECULAR_SECTORS  = {"Technology", "Healthcare", "Communication Services"}
_DEFENSIVE_SECTORS = {"Consumer Defensive", "Utilities"}


def _cycle_type(sector: str) -> str:
    """Classify a sector as SECULAR, CYCLICAL, DEFENSIVE, or HYBRID."""
    if sector in _SECULAR_SECTORS:  return "SECULAR"
    if sector in _CYCLICAL_SECTORS: return "CYCLICAL"
    if sector in _DEFENSIVE_SECTORS: return "DEFENSIVE"
    return "HYBRID"


# SEC SIC description → yfinance/GICS sector name (the exact strings _cycle_type
# keys on). Used ONLY as a cycle_type fallback when yfinance's sector is blank.
# Keyed on keywords in the SIC description so it survives the Manufacturing SIC
# block (2000-3999) that lumps semis, pharma, autos and food under one range.
# Best-effort: an unmatched description returns None → HYBRID (the safe default).
_SIC_SECTOR_PATTERNS = [
    (r"semiconductor|computer|software|electronic component|prepackaged|data processing|internet", "Technology"),
    (r"pharmaceutic|biolog|medicinal|surgical|medical|health|diagnostic|laborator", "Healthcare"),
    (r"telephone|telecommunicat|broadcast|cable|motion picture|publishing|advertis", "Communication Services"),
    # Real estate BEFORE financials — "Real Estate Investment Trusts" contains
    # "invest" and would otherwise be miscaught by the financials pattern.
    (r"real estate|land subdivid|\breit\b", "Real Estate"),
    (r"\bbanks?\b|savings instit|security broker|\binvest|insurance|\bfinanc", "Financial Services"),
    (r"electric services|gas.*(distribut|transmiss)|water suppl|\butilit|electric & other", "Utilities"),
    (r"crude petroleum|natural gas|petroleum refin|\boil\b|\bcoal\b|drilling", "Energy"),
    (r"metal mining|\bgold\b|\bsteel\b|chemical|\bmining\b|paper|forest|agricultur", "Basic Materials"),
    (r"retail|eating.*place|restaurant|apparel|motor vehicle|\bhotel|leisure|\bstore", "Consumer Cyclical"),
    (r"\bfood\b|beverage|grocer|household|tobacco", "Consumer Defensive"),
    (r"machinery|aircraft|construction|electrical equip|engineering|railroad|trucking|air.*transport", "Industrials"),
]


def _sic_to_sector(desc: str | None) -> str | None:
    """Best-effort SEC SIC description → GICS sector (a cycle_type fallback only)."""
    if not desc:
        return None
    d = desc.lower()
    for pattern, sector in _SIC_SECTOR_PATTERNS:
        if re.search(pattern, d):
            return sector
    return None


def _detect_regime(macro: dict) -> str:
    """Classify the current macro regime from FRED indicators.

    Returns one of: EXPANSION | PEAK | LATE_CYCLE | RECESSION | INFLATIONARY | MID_CYCLE
    """
    fed     = macro.get("fed_funds_rate")
    cpi     = macro.get("cpi_yoy")
    unemp   = macro.get("unemployment")
    vix     = macro.get("vix")
    spread  = macro.get("yield_curve_spread")  # 10Y - 2Y

    # Priority order matters — most diagnostic condition first
    if spread is not None and spread < 0:
        return "LATE_CYCLE"          # inverted yield curve is the strongest signal
    if cpi is not None and cpi > 4.5:
        return "INFLATIONARY"
    if unemp is not None and unemp > 5.5:
        return "RECESSION"
    if fed is not None and unemp is not None and fed > 4.5 and unemp < 4.5:
        return "PEAK"                # tight labor, elevated rates
    if vix is not None and vix < 16:
        return "EXPANSION"           # low volatility, risk-on
    return "MID_CYCLE"


async def _fetch_and_emit(ticker: str, coro, source_name: str):
    """Await coro then fire a FETCH_DONE live event for visual dossier progress."""
    result = await coro
    await emit_live(ticker, {"type": "FETCH_DONE", "source": source_name})
    return result

# Macro data (FRED + VIX) is identical for all tickers â€" fetch once per run
_macro_cache: dict = {}
_macro_fetched = False
_macro_async_lock = asyncio.Lock()


# â"€â"€ Sync HTTP helpers (run inside asyncio.to_thread) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def _fh(path: str, params: dict = None) -> dict | list:
    try:
        p = {"token": FINNHUB_KEY, **(params or {})}
        r = requests.get(f"{FH}{path}", params=p, timeout=15)
        if r.status_code == 429:
            import random
            time.sleep(5 + random.uniform(0, 3))
            r = requests.get(f"{FH}{path}", params=p, timeout=15)
        return r.json() if r.ok else {}
    except requests.exceptions.RequestException as e:
        # Exception type only — connection-error strings embed the full URL
        # incl. the key-bearing query params, which is cleartext in local logs
        # (GitHub Actions masks registered secrets; local .env runs don't).
        print(f"  [dossier] Finnhub {path} failed: {type(e).__name__}")
        return {}



def _fred(series: str) -> float | None:
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series, "api_key": FRED_KEY, "file_type": "json",
                    "sort_order": "desc", "limit": 1},
            timeout=10,
        )
        obs = r.json().get("observations", [])
        return float(obs[0]["value"]) if obs and obs[0]["value"] != "." else None
    except Exception:
        return None


def _fred_cpi_yoy() -> float | None:
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "CPIAUCSL", "api_key": FRED_KEY, "file_type": "json",
                    "sort_order": "desc", "limit": 13},
            timeout=10,
        )
        obs = r.json().get("observations", [])
        vals = [float(o["value"]) for o in obs if o["value"] != "."]
        if len(vals) >= 12:
            return round((vals[0] - vals[-1]) / vals[-1] * 100, 1)
        return None
    except Exception:
        return None


def _safe_float(val) -> float | None:
    """Parse AV overview strings ('12.77', 'None', '-') to float or None."""
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_div(a, b) -> float | None:
    try:
        return round(a / b, 4) if a is not None and b is not None and b != 0 else None
    except (TypeError, ZeroDivisionError):
        return None


def _safe_sub(a, b) -> float | None:
    try:
        return round(a - b, 4) if a is not None and b is not None else None
    except TypeError:
        return None


def _av(function: str, params: dict = None) -> dict:
    global _av_idx, _av_last_call
    if not _av_keys:
        return {}
    with _av_lock:
        key = _av_keys[_av_idx % len(_av_keys)]
        _av_idx += 1
        # Enforce 5 RPM: sleep inside the lock so concurrent threads queue up
        wait = _AV_MIN_INTERVAL - (time.time() - _av_last_call)
        if wait > 0:
            time.sleep(wait)
        _av_last_call = time.time()
    try:
        p = {"function": function, "apikey": key, **(params or {})}
        r = requests.get("https://www.alphavantage.co/query", params=p, timeout=15)
        return r.json() if r.ok else {}
    except requests.exceptions.RequestException as e:
        # Type only — see _fh: the exception string can carry the apikey URL.
        print(f"  [dossier] AlphaVantage {function} failed: {type(e).__name__}")
        return {}


def _fwd_eps_cagr(future: list) -> tuple[float, int] | None:
    """(CAGR, years) from the NTM consensus EPS row to the furthest of the next
    TWO fiscal years (rows past FY+3 are analyst-thin and noisy — e.g. NVDA's
    FY2030 row sits BELOW its FY2029 row on a handful of estimates). The
    long-horizon PEG denominator with a KNOWN basis (2026-07-13): third-party
    published PEGs carry unstated growth bases and proved untrustworthy.
    None when either endpoint is missing/non-positive — a CAGR through
    negative EPS is noise, and one annual row can't make a growth rate."""
    if not future or len(future) < 2:
        return None
    base = future[0].get("epsAvg")
    if not base or base <= 0:
        return None
    horizon = [e for e in future[1:3] if e.get("epsAvg") and e.get("epsAvg") > 0]
    if not horizon:
        return None
    tgt = horizon[-1]
    years = future.index(tgt)  # annual rows → index distance = years past NTM row
    if years < 1:
        return None
    return round((tgt["epsAvg"] / base) ** (1 / years) - 1, 4), years


# ── Long-horizon EPS CAGR fallback chain (2026-07-13) ──────────────────────────
# FMP's free tier 402s some symbols (verified: MNDY, MRVL), AND even when FMP
# covers a symbol, _fwd_eps_cagr caps its reach at ~FY+2/+3 (further rows are
# analyst-thin) — so a "peg_lt" built only from FMP/AV was never a TRUE 5-year
# figure for anyone. Exhaustive survey of free alternatives for a real 5-year
# consensus growth rate, all live-verified 2026-07-13:
#   worked live but WRONG BASIS, rejected → Nasdaq api.nasdaq.com/api/analyst/
#     .../earnings-forecast (keyless, undocumented, 3 future FYs for both MNDY
#     & MRVL — looked like a clean win). Cross-checked its numbers against
#     yfinance's forwardEps (what fwd_pe is built on) before trusting it: for
#     MNDY, Nasdaq's FY2026 EPS = 1.59 vs yfinance/AV = 5.39 — a ~3.4x gap,
#     same pattern on MRVL (3.07 vs 6.18). Nasdaq's "earnings forecast" is
#     GAAP-basis; the rest of this file's forward-PE machinery is non-GAAP.
#     A GAAP-basis growth rate divided into a non-GAAP-basis fwd PE would have
#     produced a peg_lt that's WRONG IN THE DANGEROUS DIRECTION — falsely
#     cheap, defeating the entire point of a "known basis" long-horizon PEG.
#     Deliberately not integrated. If revisited, it needs its own EPS-basis
#     normalization first, not a straight ratio.
#   worked + basis-verified consistent, PRIMARY → Finviz's quote page "EPS
#     next 5Y" ("Long term annual growth estimate (5 years)" per its own
#     tooltip) — a genuine 5-year consensus figure, unlike anything FMP/AV/
#     yfinance expose free. Verification chain (not just one spot check):
#       1. Finviz's OWN published PEG reconciles EXACTLY against its OWN
#          displayed Forward P/E ÷ EPS-next-5Y (MNDY: 15.29/12.11% = 1.263
#          computed vs 1.26 published) — confirms forward-PE basis, not
#          trailing, not GAAP.
#       2. Finviz's adjacent "EPS next Y" field (21.14% for MNDY) matched our
#          OWN independently-computed AV-derived near-term growth (0.2114) to
#          4 significant figures — cross-vendor confirmation, not an echo of
#          the same upstream provider.
#       3. _resolve_eps_cagr below ALSO cross-checks Finviz's displayed
#          Forward P/E against our own fwd_pe at RUNTIME, per ticker, before
#          trusting its growth rate — generalizing the manual spot-check into
#          an always-on guard (same 25%-divergence philosophy as
#          signal_analysis.py's split/bad-price guard: real basis mismatches
#          run 2x+, not a few percent).
#     Fetched directly (NOT via finvizfinance's FinvizQuote class, which
#     live-tested to sometimes return a stripped page with no data table at
#     all) using finviz_screener._parse_fundament — the in-house parser this
#     same investigation found and fixed (it had been silently dropping 5 of
#     Finviz's 6 sibling snapshot tables; see finviz_screener.py).
#   fallback, ~1-2yr only → FMP (already fetched by the caller) → Alpha
#     Vantage EARNINGS_ESTIMATES (documented, same key pool as _av() above;
#     basis-verified against yfinance's forwardEps to 4 decimal places).
#   blocked      → Finnhub /stock/eps-estimate (403, premium-gated on our key),
#                  StockAnalysis.com, TipRanks (bot-walled)
#   no forward data → Zacks quote-feed (has pe_f1 but no forward EPS field)

_FINVIZ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _finviz_pct(s) -> float | None:
    """Parse a Finviz percent-string field ('12.11%', '-', None) to a fraction."""
    if not isinstance(s, str):
        return None
    s = s.strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return float(s.rstrip("%")) / 100
    except ValueError:
        return None


def _finviz_growth_5y(ticker: str) -> dict:
    """5-year consensus EPS growth + Forward P/E straight from Finviz's quote
    page (see the module comment above for the verification chain). Fetched
    directly with a browser UA — the finvizfinance library's own FinvizQuote
    fetch was live-tested to sometimes return a stripped page with no data
    table, unrelated to the six-sibling-table parsing bug also fixed this
    session. Only a positive growth rate is usable (PEG is not a meaningful
    concept against expected decline). Returns {} on any failure or missing
    field — never a partial/fabricated result."""
    global _finviz_last_call
    with _finviz_lock:
        wait = _FINVIZ_MIN_INTERVAL - (time.time() - _finviz_last_call)
        if wait > 0:
            time.sleep(wait)
        _finviz_last_call = time.time()
    try:
        r = requests.get(f"https://finviz.com/quote.ashx?t={ticker}",
                         headers={"User-Agent": _FINVIZ_UA}, timeout=15)
        if not r.ok:
            return {}
        from bs4 import BeautifulSoup
        from finviz_screener import _parse_fundament
        info = _parse_fundament(BeautifulSoup(r.text, "html.parser"), ticker)
    except requests.exceptions.RequestException as exc:
        print(f"  [dossier] Finviz {ticker} failed: {type(exc).__name__}")
        return {}
    except Exception:
        return {}
    eps_5y = _finviz_pct(info.get("EPS next 5Y"))
    fwd_pe = _safe_float(info.get("Forward P/E"))
    if eps_5y is None or eps_5y <= 0 or fwd_pe is None:
        return {}
    return {"eps_5y": eps_5y, "fwd_pe": fwd_pe}


def _av_eps_estimates_fwd(ticker: str) -> list[dict] | dict:
    """Forward-year EPS consensus via Alpha Vantage's EARNINGS_ESTIMATES —
    documented, shares the throttled multi-key AV pool _av() already manages.
    Basis-verified consistent with yfinance's forwardEps (see the module
    comment above) — safe to divide fwd_pe by. Fiscal-year-horizon rows only;
    sorted oldest-first, keyed "epsAvg" for _fwd_eps_cagr."""
    data = _av("EARNINGS_ESTIMATES", {"symbol": ticker})
    rows = data.get("estimates") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for e in rows:
        if not isinstance(e, dict) or e.get("horizon") != "fiscal year":
            continue
        edate = e.get("date")
        eps = _safe_float(e.get("eps_estimate_average"))
        if not edate or str(edate) < today or eps is None:
            continue
        out.append({"date": str(edate), "epsAvg": eps})
    out.sort(key=lambda x: x["date"])
    return out


_FWD_PE_DIVERGENCE_MAX = 0.25  # same threshold as signal_analysis's split/bad-price guard


def _resolve_eps_cagr(ticker: str, fmp_cagr: float | None, fmp_years: int | None,
                      fwd_pe_clean: float | None) -> tuple[float | None, int | None, str | None]:
    """Long-horizon EPS CAGR for peg_lt, best-horizon-first: Finviz "EPS next
    5Y" (true 5-year consensus — see the module comment above) when its own
    Forward P/E is within 25% of our independently-computed fwd_pe_clean →
    FMP (already fetched by the caller, ~2yr reach in practice) → Alpha
    Vantage EARNINGS_ESTIMATES (~1-2yr). Finviz is tried FIRST for every
    ticker, not just names FMP's free tier misses — reach and horizon
    shouldn't depend on which vendor happens to cover a name. Falls through
    cleanly (never fabricates) when fwd_pe_clean is unavailable (can't verify
    Finviz's basis — e.g. an ADR-nulled name) or any source is missing/fails.
    Cached independently per source (see cache.cached) so a miss isn't
    re-fetched every run. Returns (cagr, years, source) or (None, None, None)."""
    if fwd_pe_clean and fwd_pe_clean > 0:
        fz = cached(f"finviz:eps5y:{ticker}", 168, _finviz_growth_5y, ticker)
        if isinstance(fz, dict) and fz.get("eps_5y") and fz.get("fwd_pe"):
            divergence = abs(fz["fwd_pe"] - fwd_pe_clean) / fwd_pe_clean
            if divergence <= _FWD_PE_DIVERGENCE_MAX:
                return fz["eps_5y"], 5, "finviz"
    if fmp_cagr:
        return fmp_cagr, fmp_years, "fmp"
    rows = cached(f"av:EPSEST:{ticker}", 24, _av_eps_estimates_fwd, ticker)
    got = _fwd_eps_cagr(rows if isinstance(rows, list) else [])
    if got:
        return got[0], got[1], "av"
    return None, None, None


def _fmp_estimates(ticker: str) -> dict:
    """Fetch annual analyst consensus from FMP stable API (250 req/day free tier).

    Returns NTM EPS avg, NTM revenue avg, computed forward growth rates,
    and analyst count. Uses the two closest future fiscal years to compute growth.
    Falls back to {} on any error or missing key.
    """
    global _fmp_last_call
    if not FMP_KEY:
        return {}
    with _fmp_lock:
        wait = _FMP_MIN_INTERVAL - (time.time() - _fmp_last_call)
        if wait > 0:
            time.sleep(wait)
        _fmp_last_call = time.time()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        r = requests.get(
            "https://financialmodelingprep.com/stable/analyst-estimates",
            params={"symbol": ticker, "period": "annual", "apikey": FMP_KEY},
            timeout=10,
        )
        if not r.ok:
            return {}
        data = r.json()
        if not isinstance(data, list) or not data:
            return {}
        data.sort(key=lambda x: x.get("date", ""))
        future = [e for e in data if e.get("date", "") >= today]
        if not future:
            return {}
        ntm = future[0]
        ntm_idx = data.index(ntm)
        if ntm_idx == 0:
            return {}
        prior = data[ntm_idx - 1]
        ntm_rev   = ntm.get("revenueAvg")
        prior_rev = prior.get("revenueAvg")
        ntm_eps   = ntm.get("epsAvg")
        prior_eps = prior.get("epsAvg")
        result: dict = {
            "fwd_eps_ntm":        ntm_eps,
            "fwd_rev_ntm":        ntm_rev,
            "num_analysts_eps":   ntm.get("numAnalystsEps"),
            "num_analysts_rev":   ntm.get("numAnalystsRevenue"),
            "ntm_date":           ntm.get("date"),
        }
        if ntm_rev and prior_rev and prior_rev > 0:
            result["fwd_rev_growth"] = round((ntm_rev - prior_rev) / prior_rev, 4)
        if ntm_eps is not None and prior_eps and prior_eps != 0:
            result["fwd_eps_growth"] = round((ntm_eps - prior_eps) / abs(prior_eps), 4)
        # Multi-year depth: the free tier returns up to 5 future FYs for covered
        # symbols — FY+1→FY+2/3 EPS CAGR is the durable-growth denominator the
        # long-horizon PEG needs. Uncovered symbols (402) never reach here.
        cagr = _fwd_eps_cagr(future)
        if cagr:
            result["eps_cagr_fwd"], result["eps_cagr_years"] = cagr
        return result
    except Exception as e:
        # Type only — the exception string can carry the apikey-bearing URL.
        print(f"  [dossier] FMP estimates {ticker} failed: {type(e).__name__}")
        return {}


# ── Insider transactions (2026-07-13) ───────────────────────────────────────
# Finnhub's actual response field is "transactionCode" (single-letter SEC Form
# 4 codes), NOT "transactionType" — verified live across 1,511 real
# transactions (AAPL/MRVL/NVDA): transactionType appeared on ZERO of them.
# The old key made buys/sells PERMANENTLY EMPTY, so every dossier ever built
# reported buy_count/sell_count/cluster_buying/net_insider_usd as 0 regardless
# of real insider activity — a silent, 100%-failure-rate bug on a signal
# FundamentalForensics' prompt explicitly asks agents to weigh. "P" = open-
# market purchase, "S" = open-market sale — genuine discretionary market
# activity, unlike "A" (grant/award), "G" (gift), "F" (tax-withholding
# disposition) or "M" (derivative exercise), none of which are a conviction
# signal either way.
def _has_insider_cluster(transactions: list, window_days: int = 14) -> bool:
    """3+ insiders transacting within a window_days-day span — a cluster is a
    stronger signal than any single transaction."""
    from datetime import datetime
    dates = []
    for t in transactions:
        d = str(t.get("transactionDate", ""))[:10]
        try:
            dates.append(datetime.strptime(d, "%Y-%m-%d"))
        except ValueError:
            continue
    if len(dates) < 3:
        return False
    dates.sort()
    for i in range(len(dates) - 2):
        if (dates[i + 2] - dates[i]).days <= window_days:
            return True
    return False


def _insider_tx_value(t: dict) -> float:
    """Dollar value of one transaction. "change" (the actual per-transaction
    share delta) is preferred; "share" (cumulative post-transaction holding)
    is only a fallback for the rare row missing "change"."""
    shares = abs(t.get("change", 0) or t.get("share", 0) or 0)
    price  = t.get("transactionPrice") or 0
    return shares * price


def process_insider_transactions(txns: list) -> dict:
    """Finnhub /stock/insider-transactions rows -> the dossier's `insiders`
    summary block. Pure function — the network fetch happens in build()."""
    txns  = txns or []
    buys  = [t for t in txns if t.get("transactionCode") == "P"]
    sells = [t for t in txns if t.get("transactionCode") == "S"]

    significant_buys  = [t for t in buys  if _insider_tx_value(t) >= 100_000]
    significant_sells = [t for t in sells if _insider_tx_value(t) >= 100_000]
    buyer_names = list({t.get("name", "") for t in buys if t.get("name")})
    total_buy_usd  = round(sum(_insider_tx_value(t) for t in buys))
    total_sell_usd = round(sum(_insider_tx_value(t) for t in sells))

    return {
        "buy_count":        len(buys),
        "sell_count":       len(sells),
        # "share" is the insider's CUMULATIVE post-transaction holding, not the
        # transaction size — summing it across multiple insiders/transactions
        # is meaningless. "change" is the actual per-transaction share delta
        # (verified live: consistently positive for "P" codes, negative for
        # "S"); abs() makes the buy/sell direction explicit rather than
        # trusting the sign convention to hold on every future row.
        "net_shares":       (sum(abs(t.get("change") or 0) for t in buys)
                             - sum(abs(t.get("change") or 0) for t in sells)),
        "cluster_buying":   _has_insider_cluster(buys),
        "significant_buys": len(significant_buys),
        "significant_sells": len(significant_sells),
        "buyer_roles":      buyer_names[:5],
        "total_buy_usd":    total_buy_usd,
        "total_sell_usd":   total_sell_usd,
        "net_insider_usd":  total_buy_usd - total_sell_usd,
        "recent":           txns[:10],
    }


def _get_vix() -> float | None:
    try:
        return float(yf.Ticker("^VIX").history(period="2d")["Close"].iloc[-1])
    except Exception:
        return None


def _price_momentum(close) -> dict:
    """Canonical price-momentum measures from a ~1y daily close series (fractions).

    ``mom_12_1`` is the Jegadeesh-Titman 12-1 measure: return from ~12 months ago
    to ~1 month ago, skipping the most recent month (short-term reversal). Keys are
    None when the history is too short (recent IPOs) — never fabricated.
    """
    out: dict = {"mom_12_1": None, "mom_6m": None, "mom_1m": None}
    try:
        n = len(close)
        last = float(close.iloc[-1])
        if n >= 21:
            p1m = float(close.iloc[-21])
            if p1m > 0:
                out["mom_1m"] = round((last - p1m) / p1m, 4)
        if n >= 126:
            p6m = float(close.iloc[-126])
            if p6m > 0:
                out["mom_6m"] = round((last - p6m) / p6m, 4)
        if n >= 240:  # ~a full trading year (tolerates short holiday calendars)
            p12 = float(close.iloc[max(0, n - 252)])
            p1 = float(close.iloc[-21])
            if p12 > 0:
                out["mom_12_1"] = round((p1 - p12) / p12, 4)
    except Exception:
        pass
    return out


def quality_composite(ratios: dict) -> float | None:
    """Quality score 0-10 from profitability / balance-sheet ratios already in
    ``ratios_ttm`` (evidence: Novy-Marx 2013 profitability premium, FF RMW, AQR
    QMJ). Monotonic maps averaged over available components; None when fewer
    than 2 components exist so a data-starved name doesn't get a fake mid-score.
    Consumed by the signal factor stamp for outcome measurement — deliberately
    NOT injected into the debate agents' context.
    """
    if not ratios:
        return None
    comps: list[float] = []
    roic = ratios.get("roic")  # percent (25 = 25%)
    if roic is not None:
        comps.append(min(10.0, max(0.0, roic / 2.5)))
    gm = ratios.get("gross_margin")  # percent
    if gm is not None:
        comps.append(min(10.0, max(0.0, gm / 8.0)))
    fcf_y = ratios.get("fcf_yield")  # fraction (0.05 = 5%)
    if fcf_y is not None:
        comps.append(min(10.0, max(0.0, fcf_y * 125.0)))
    de = ratios.get("debt_equity")  # yfinance percentage (30.27 = 0.30x)
    if de is not None:
        comps.append(min(10.0, max(0.0, 10.0 - de / 30.0)))
    if len(comps) < 2:
        return None
    return round(sum(comps) / len(comps), 2)


def _quote(ticker: str) -> dict:
    """Spot quote, Finnhub-shape (c/d/dp/h/l/o/pc). Source-ranked 2026-07-11:
    Tiger delayed briefs primary for plain US symbols (official keyed API, no
    60/min cap; 15-min delay is fine for analysis), Finnhub fallback, and the
    per-field yfinance fallbacks downstream stay as the last resort. Tiger env
    absent (local runs) or any failure → silently the old path."""
    try:
        from broker_sync import tiger_delay_quotes, tiger_symbol_ok
        if tiger_symbol_ok(ticker):
            q = tiger_delay_quotes([ticker]).get(ticker)
            if q and q.get("price"):
                pc = q.get("prev_close")
                return {
                    "c": q["price"],
                    "pc": pc,
                    "d": round(q["price"] - pc, 4) if pc else None,
                    "dp": q.get("change_pct"),
                    "h": q.get("high"), "l": q.get("low"), "o": q.get("open"),
                    "_src": "tiger-delayed",
                }
    except Exception:
        pass
    return _fh("/quote", {"symbol": ticker})


def _earnings_upcoming(ticker: str, earnings_cal_raw) -> list:
    """Upcoming earnings rows. Finnhub primary (has EPS/revenue estimates);
    Tiger's market-wide calendar as a date-only gap-filler — Finnhub's free
    calendar misses many small caps (2026-07-11: Tiger had 609 rows/14d).
    The Tiger call is one request cached 6h and shared across every ticker
    in the run."""
    ec = (earnings_cal_raw.get("earningsCalendar", [])
          if isinstance(earnings_cal_raw, dict) else [])
    upcoming = [
        {
            "date":             e.get("date"),
            "hour":             e.get("hour"),   # "bmo" or "amc"
            "eps_estimate":     e.get("epsEstimate"),
            "revenue_estimate": e.get("revenueEstimate"),
            "quarter":          e.get("quarter"),
            "year":             e.get("year"),
        }
        for e in [x for x in ec if x.get("epsActual") is None][:3]
    ]
    if upcoming:
        return upcoming
    try:
        from broker_sync import tiger_earnings_dates, tiger_symbol_ok
        if tiger_symbol_ok(ticker):
            d = (cached("tiger:earnings_cal:v1", 6, tiger_earnings_dates) or {}).get(ticker)
            if d:
                return [{"date": d, "hour": None, "eps_estimate": None,
                         "revenue_estimate": None, "quarter": None, "year": None,
                         "src": "tiger"}]
    except Exception:
        pass
    return []


def _wilder_rsi(close, period: int = 14) -> float | None:
    """RSI using Wilder's original smoothing (seed = SMA(period), then
    recursive avg = (prev_avg*(period-1) + current)/period) — the textbook
    definition and what every mainstream charting platform (TradingView,
    most brokers) actually displays. A plain N-period rolling-mean RSI
    ("Cutler's RSI") is a real but non-standard variant that diverges from
    Wilder's by ~1+ points (verified live on AAPL, 2026-07-13) — since RSI
    feeds MarketStructure's qualitative entry-timing judgment, it should
    match what a human checking a chart elsewhere would see. None when the
    series is too short."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().to_numpy(copy=True)
    avg_loss = loss.rolling(period).mean().to_numpy(copy=True)
    import numpy as np
    valid = ~np.isnan(avg_gain)
    if not valid.any():
        return None
    start = int(np.argmax(valid))
    gain_arr, loss_arr = gain.to_numpy(), loss.to_numpy()
    for i in range(start + 1, len(avg_gain)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain_arr[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss_arr[i]) / period
    last_gain, last_loss = avg_gain[-1], avg_loss[-1]
    if np.isnan(last_gain) or np.isnan(last_loss):
        return None
    rs = last_gain / last_loss if last_loss != 0 else float("inf")
    return 100.0 - 100.0 / (1.0 + rs)


def _technicals(ticker: str) -> dict:
    try:
        # Tiger adjusted daily bars primary (probe-verified split-adjustment
        # parity with yfinance, 2026-07-11); yfinance scrape as fallback.
        hist = None
        try:
            from broker_sync import tiger_daily_bars
            hist = tiger_daily_bars(ticker)
        except Exception:
            hist = None
        if hist is None or hist.empty:
            try:
                hist = yf.Ticker(ticker).history(period="1y")
            except Exception:
                time.sleep(5)
                hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty:
            return {}
        close = hist["Close"]
        sma50 = float(close.rolling(50).mean().iloc[-1])
        sma200 = float(close.rolling(200).mean().iloc[-1])
        price = float(close.iloc[-1])

        rsi = _wilder_rsi(close, 14)

        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = float((ema12 - ema26).iloc[-1])
        signal_line = float((ema12 - ema26).ewm(span=9).mean().iloc[-1])

        w52_high = float(hist["High"].max())
        w52_low = float(hist["Low"].min())
        pct_from_high = round((price - w52_high) / w52_high * 100, 1)

        momentum = _price_momentum(close)

        return {
            "price": round(price, 2),
            "sma50": round(sma50, 2),
            "sma200": round(sma200, 2),
            "above_sma50": price > sma50,
            "above_sma200": price > sma200,
            "rsi_14": round(rsi, 1) if rsi is not None else None,
            "macd_line": round(macd_line, 3),
            "macd_signal": round(signal_line, 3),
            "macd_bullish": macd_line > signal_line,
            "52w_high": round(w52_high, 2),
            "52w_low": round(w52_low, 2),
            "pct_from_52w_high": pct_from_high,
            **momentum,
        }
    except Exception as e:
        # Return {} not {"error": ...}: cached() stores any truthy result, so an
        # error dict from one transient blip was served for the full 1h TTL and
        # every debate in that window scored the name blind on technicals
        # (2026-07-11 audit). {} stays uncached → the next run retries.
        print(f"  [technicals] {ticker} failed: {type(e).__name__}: {e}")
        return {}


def _ttm_fcf_from_quarterly(t) -> float | None:
    """Sum the last 4 REAL quarterly OCF/Capex statements into a genuine
    trailing-twelve-month FCF. info['freeCashflow'] is an opaque, unaudited
    Yahoo figure verified live (2026-07-13) to diverge 20-70%+ from this across
    a broad sample (NVDA -61%, MU -71%, CAT -52%, MRVL +37%), with an outright
    sign flip on NEE (-$18.5B info-dict vs a real +$2.4B) — traced to WMT/KO/
    TSLA composite fair values landing at 6-10% of price (both DCF and EV/FCF
    legs consume this same field). Too unreliable to feed valuation. Returns
    None if fewer than 4 real (non-NaN) quarters of both OCF and Capex are
    available (e.g. recent IPOs) — caller falls back to the annual statement.
    """
    try:
        qcf = t.quarterly_cashflow
        if qcf is None or qcf.empty:
            return None
        ocf_row = next((r for r in ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
                        if r in qcf.index), None)
        if ocf_row is None or "Capital Expenditure" not in qcf.index:
            return None
        cols = list(qcf.columns[:4])
        if len(cols) < 4:
            return None
        ocf_vals = [qcf.loc[ocf_row, c] for c in cols]
        capex_vals = [qcf.loc["Capital Expenditure", c] for c in cols]
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in ocf_vals + capex_vals):
            return None
        return float(sum(ocf_vals) + sum(capex_vals))
    except Exception:
        return None


def _yf_financials(ticker: str) -> dict:
    try:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
        except Exception:
            time.sleep(5)
            t = yf.Ticker(ticker)
            info = t.info or {}

        def _pct(v):
            return round(v * 100, 2) if v is not None else None

        def _r(v, n=2):
            return round(v, n) if v is not None else None

        # For ADRs / foreign stocks, yfinance mixes underlying share counts with ADR-level price.
        # Detection uses three independent signals:
        #   1. quoteType == "ADR" — explicit yfinance flag
        #   2. sharesOutstanding / floatShares > 2 — implicit ratio heuristic
        #   3. financialCurrency != currency — financials in foreign currency while price is USD
        #      (catches NYSE-listed foreign companies like TSM that yfinance labels as EQUITY)
        shares_out = info.get("sharesOutstanding") or 0
        float_shares = info.get("floatShares") or 0
        _fin_currency = info.get("financialCurrency") or "USD"
        _price_currency = info.get("currency") or "USD"
        _is_adr = info.get("quoteType", "").upper() == "ADR"
        _is_share_ratio_mismatch = float_shares > 0 and shares_out > 0 and shares_out / float_shares > 2
        _is_fx_mismatch = _price_currency == "USD" and _fin_currency != "USD"
        _is_adr_mismatch = _is_adr or _is_share_ratio_mismatch or _is_fx_mismatch
        # Only use floatShares when the ratio heuristic fires — for fx-mismatch ADRs (TSM),
        # sharesOutstanding is already in ADR units and pairs correctly with the USD ADR price.
        _safe_shares = float_shares if _is_share_ratio_mismatch else shares_out

        # P/B and P/S from yfinance are computed as price / (metric / sharesOutstanding).
        # For ADRs this produces wrong values; null them out and let agents use web research.
        _pb = None if _is_adr_mismatch else _r(info.get("priceToBook"))
        _ps = None if _is_adr_mismatch else _r(info.get("priceToSalesTrailing12Months"))
        _fcf_ps = None  # always compute from safe_shares below if fcf available
        _ttm_fcf = _ttm_fcf_from_quarterly(t)
        _fcf = _ttm_fcf if _ttm_fcf is not None else info.get("freeCashflow")
        _fcf_source = "ttm_quarterly_sum" if _ttm_fcf is not None else "info_dict_fallback"
        if _fcf and _safe_shares:
            _fcf_ps = _r(_fcf / _safe_shares)

        ratios = {
            "pe":            _r(info.get("trailingPE")),
            "fwd_pe":        _r(info.get("forwardPE")),
            "pb":            _pb,
            "ps":            _ps,
            "ev_ebitda":     _r(info.get("enterpriseToEbitda")),
            "gross_margin":  _pct(info.get("grossMargins")),
            "net_margin":    _pct(info.get("profitMargins")),
            "roe":           _pct(info.get("returnOnEquity")),
            "roa":           _pct(info.get("returnOnAssets")),
            "debt_equity":   _r(info.get("debtToEquity")),
            "current_ratio": _r(info.get("currentRatio")),
            "fcf":           _fcf,
            "fcf_per_share": _fcf_ps,
            "fcf_source":    _fcf_source,
            "revenue_ttm":   info.get("totalRevenue"),
            "ebitda":        info.get("ebitda"),
            "beta":          _r(info.get("beta")),
            "shares_out":    _safe_shares,
            "short_pct":     _pct(info.get("shortPercentOfFloat")),
            "adr_mismatch":  _is_adr_mismatch,  # flag for downstream consumers
            "op_margin":     _pct(info.get("operatingMargins")),
            "trailing_eps":  info.get("trailingEps"),
            "forward_eps":   info.get("forwardEps"),
        }

        analyst = {
            "target_mean":    _r(info.get("targetMeanPrice")),
            "target_high":    _r(info.get("targetHighPrice")),
            "target_low":     _r(info.get("targetLowPrice")),
            "num_analysts":   info.get("numberOfAnalystOpinions"),
            "recommendation": info.get("recommendationKey", ""),
        }

        income = []
        try:
            fin = t.financials
            if fin is not None and not fin.empty:
                for col in fin.columns[:4]:
                    rev = fin.loc["Total Revenue", col] if "Total Revenue" in fin.index else None
                    gp  = fin.loc["Gross Profit", col] if "Gross Profit" in fin.index else None
                    oi  = fin.loc["Operating Income", col] if "Operating Income" in fin.index else None
                    ni  = fin.loc["Net Income", col] if "Net Income" in fin.index else None
                    rd  = fin.loc["Research And Development", col] if "Research And Development" in fin.index else None
                    cor = fin.loc["Cost Of Revenue", col] if "Cost Of Revenue" in fin.index else None
                    income.append({
                        "date": str(col.date()),
                        "revenue": int(rev) if rev is not None and str(rev) != "nan" else None,
                        "gross_profit": int(gp) if gp is not None and str(gp) != "nan" else None,
                        "operating_income": int(oi) if oi is not None and str(oi) != "nan" else None,
                        "net_income": int(ni) if ni is not None and str(ni) != "nan" else None,
                        "research_development": int(rd) if rd is not None and str(rd) != "nan" else None,
                        "cost_of_revenue": int(cor) if cor is not None and str(cor) != "nan" else None,
                    })
        except Exception as e:
            print(f"  [dossier] income statement parse failed: {e}")

        balance = []
        try:
            bs = t.balance_sheet
            if bs is not None and not bs.empty:
                for col in bs.columns[:2]:
                    _bs = lambda key, c=col: int(bs.loc[key, c]) if key in bs.index and str(bs.loc[key, c]) != "nan" else None
                    balance.append({
                        "date": str(col.date()),
                        "total_assets": _bs("Total Assets"),
                        "total_debt": _bs("Total Debt"),
                        "stockholders_equity": _bs("Stockholders Equity"),
                        "cash": _bs("Cash And Cash Equivalents"),
                        "current_assets": _bs("Current Assets"),
                        "current_liabilities": _bs("Current Liabilities"),
                        "goodwill": _bs("Goodwill"),
                        "intangible_assets": _bs("Other Intangible Assets"),
                        "inventory": _bs("Inventory"),
                    })
        except Exception as e:
            print(f"  [dossier] balance sheet parse failed: {e}")

        cashflow = []
        try:
            cf = t.cashflow
            if cf is not None and not cf.empty:
                for col in cf.columns[:2]:
                    _cf = lambda key, c=col: int(cf.loc[key, c]) if key in cf.index and str(cf.loc[key, c]) != "nan" else None
                    op = _cf("Operating Cash Flow") or _cf("Cash Flow From Continuing Operating Activities")
                    capex = _cf("Capital Expenditure")
                    fcf_val = ((op + capex) if op is not None and capex is not None
                               else (op if op is not None else None))
                    sbc = _cf("Stock Based Compensation")
                    cashflow.append({
                        "date": str(col.date()),
                        "operating_cf": op,
                        "capex": capex,
                        "free_cash_flow": fcf_val,
                        "stock_based_compensation": sbc,
                    })
        except Exception as e:
            print(f"  [dossier] cashflow parse failed: {e}")

        # Fresh NTM consensus from Yahoo analyst estimates — more current than info dict.
        # info['earningsGrowth'] / info['revenueGrowth'] can lag 6-12 months; these
        # attributes parse the live analyst consensus page and update daily/weekly.
        estimates: dict = {}
        try:
            ee = t.earnings_estimate
            if ee is not None and not ee.empty:
                def _ee_val(idx, col):
                    try:
                        return float(ee.loc[idx, col]) if idx in ee.index and col in ee.columns and ee.loc[idx, col] is not None else None
                    except (TypeError, ValueError):
                        return None

                estimates["fwd_eps_growth"]          = _ee_val("+1y", "growth")
                estimates["fwd_eps_ntm"]             = _ee_val("+1y", "avg")
                estimates["est_eps_current_q"]       = _ee_val("0q",  "avg")
                estimates["est_eps_current_q_growth"] = _ee_val("0q", "growth")
                estimates["est_eps_next_q"]          = _ee_val("+1q", "avg")
                estimates["est_eps_next_q_growth"]   = _ee_val("+1q", "growth")
                # Strip None values so _first_not_none chains work cleanly
                estimates = {k: v for k, v in estimates.items() if v is not None}
        except Exception:
            pass
        try:
            re_est = t.revenue_estimate
            if re_est is not None and not re_est.empty and "+1y" in re_est.index:
                row = re_est.loc["+1y"]
                if "growth" in re_est.columns and row["growth"] is not None:
                    try:
                        estimates["fwd_rev_growth"] = float(row["growth"])
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        try:
            et = t.eps_trend
            if et is not None and not et.empty and "+1y" in et.index:
                row = et.loc["+1y"]
                cur = float(row["current"]) if "current" in et.columns and row["current"] is not None else None
                ago30 = float(row["30daysAgo"]) if "30daysAgo" in et.columns and row["30daysAgo"] is not None else None
                if cur is not None and ago30 is not None and ago30 != 0:
                    estimates["eps_revision_momentum"] = round((cur - ago30) / abs(ago30), 4)
        except Exception:
            pass
        # NB (2026-07-13, MNDY case): there is NO usable free long-horizon PEG
        # denominator — Yahoo stopped publishing per-stock LTG (growth_estimates
        # LTG row is NaN across the board) and Alpha Vantage's PEGRatio has an
        # unstated basis that produced 0.28 for MNDY (built off the depressed
        # GAAP base — the exact artifact a long PEG should correct). Durability
        # is therefore guarded by ValuationEngine's base-effect-trap reasoning,
        # not by a second PEG field.

        return {"ratios": ratios, "analyst": analyst,
                "income": income, "balance": balance, "cashflow": cashflow,
                "industry": info.get("industry", ""),
                "sector": info.get("sector", ""),
                "company_name": info.get("longName") or info.get("shortName", ""),
                "market_cap": info.get("marketCap"),
                # NB (2026-07-13): despite the names Yahoo gives these fields,
                # they are NOT forward-looking — verified live across 4 tickers,
                # info['earningsGrowth'] tracks info['earningsQuarterlyGrowth']
                # almost exactly (MRVL: -80.4% vs -80.6%), while the CONFIRMED
                # forward figure (t.earnings_estimate '+1y' growth) read +52.6%
                # — opposite sign. revenueGrowth diverges 30-100%+ from its
                # forward counterpart too, with no consistent conversion factor.
                # Kept under an honest name (trailing, not forward) — genuinely
                # useful for spotting an earnings-recovery base effect (see
                # agents.py's BASE-EFFECT TRAP) — but MUST NOT feed a forward-
                # growth fallback chain (removed from both, below).
                "trailing_revenue_growth_yoy":  info.get("revenueGrowth"),
                "trailing_earnings_growth_yoy": info.get("earningsGrowth"),
                "previous_close": info.get("previousClose"),
                "financials_currency": _fin_currency if _is_fx_mismatch else None,
                "estimates": estimates}
    except Exception as e:
        return {"error": str(e), "ratios": {}, "analyst": {},
                "income": [], "balance": [], "cashflow": [], "industry": "", "sector": ""}


SECTOR_TERMINAL = {
    "Technology": 25, "Healthcare": 20, "Consumer Cyclical": 18,
    "Communication Services": 20, "Financials": 12, "Industrials": 15,
    "Energy": 10, "Utilities": 12, "Consumer Defensive": 16,
    "Real Estate": 14, "Basic Materials": 12,
}

# Maximum blended growth rate allowed in DCF by GICS sector.
# High-growth sectors (Tech, Comms) legitimately sustain 35-40% near-term growth;
# capping them at 25% systematically undervalues compounders like NVDA or META.
# Defensive and capital-constrained sectors get tighter caps.
SECTOR_GROWTH_CAP = {
    "Technology": 0.40,
    "Communication Services": 0.35,
    "Healthcare": 0.30,
    "Consumer Cyclical": 0.25,
    "Industrials": 0.20,
    "Energy": 0.20,
    "Financials": 0.15,
    "Utilities": 0.10,
    "Consumer Defensive": 0.15,
    "Real Estate": 0.15,
    "Basic Materials": 0.15,
}
_DEFAULT_GROWTH_CAP = 0.25


def _compute_roic(yf_fin: dict) -> float | None:
    """ROIC from yfinance: NOPAT / (Equity + Debt). Returns percentage or None."""
    try:
        income = yf_fin.get("income") or []
        balance = yf_fin.get("balance") or []
        if not income or not balance:
            return None
        op_income = income[0].get("operating_income")
        equity = balance[0].get("stockholders_equity")
        debt = balance[0].get("total_debt") or 0
        if op_income is None or equity is None:
            return None
        invested_capital = equity + debt
        if invested_capital <= 0:
            return None
        nopat = op_income * 0.79
        return round(nopat / invested_capital * 100, 2)
    except Exception:
        return None


def _dynamic_dcf(
    fcf: float | None,
    income: list,
    beta: float | None,
    treasury_10y: float | None,
    sector: str,
    shares_out: int | None,
    fwd_revenue_growth: float | None = None,
    fwd_earnings_growth: float | None = None,
    net_debt: float | None = None,
) -> tuple[float | None, dict]:
    """Dynamic DCF using blended growth (historical CAGR + forward analyst estimates),
    CAPM discount rate, and sector-mapped terminal multiple.
    Returns (iv_per_share, assumptions_dict).
    """
    if not fcf or fcf <= 0:
        return None, {}
    try:
        # Growth rate: blend historical revenue CAGR with forward analyst estimates
        revenues = [yr.get("revenue") for yr in income if yr.get("revenue")]
        hist_cagr = None
        if len(revenues) >= 2:
            hist_cagr = (revenues[0] / revenues[-1]) ** (1 / (len(revenues) - 1)) - 1
            rev_years = len(revenues)
        else:
            rev_years = 0

        fwd_growth = fwd_revenue_growth if fwd_revenue_growth is not None else fwd_earnings_growth

        # Use sector-specific growth cap so high-growth sectors (Technology, Communication Services)
        # are not artificially suppressed by a one-size-fits-all 25% ceiling.
        growth_cap = SECTOR_GROWTH_CAP.get(sector, _DEFAULT_GROWTH_CAP)

        if hist_cagr is not None and fwd_growth is not None:
            growth = max(0.02, min(hist_cagr * 0.5 + fwd_growth * 0.5, growth_cap))
            growth_method = "blended"
        elif fwd_growth is not None:
            growth = max(0.02, min(float(fwd_growth), growth_cap))
            growth_method = "forward"
        elif hist_cagr is not None:
            growth = max(0.02, min(float(hist_cagr), growth_cap))
            growth_method = "historical"
        else:
            growth = 0.08
            growth_method = "default"
            rev_years = 0

        # Discount rate: CAPM (risk-free + beta x ERP), clamped to 7-20%
        risk_free = (treasury_10y or 4.3) / 100
        beta_val = max(beta, 0.5) if beta and beta > 0 else 1.0
        discount = max(0.07, min(risk_free + beta_val * 0.055, 0.20))

        # Terminal multiple: sector-mapped
        terminal_mult = SECTOR_TERMINAL.get(sector, 15)

        years = 5
        pv = 0.0
        cf = fcf
        for i in range(1, years + 1):
            cf *= (1 + growth)
            pv += cf / (1 + discount) ** i
        terminal_val = cf * terminal_mult / (1 + discount) ** years
        total_pv = pv + terminal_val

        equity_value = total_pv - (net_debt or 0)
        if equity_value <= 0:
            return None, {"note": "negative_equity_value", "total_pv": round(total_pv, 0), "net_debt": round(net_debt or 0, 0)}
        iv = (round(equity_value / shares_out, 2)
              if shares_out and shares_out > 0
              else round(equity_value, 0))

        assumptions = {
            "growth_rate_pct":    round(growth * 100, 1),
            "growth_method":      growth_method,
            "hist_cagr_pct":      round(hist_cagr * 100, 1) if hist_cagr is not None else None,
            "fwd_growth_pct":     round(fwd_growth * 100, 1) if fwd_growth is not None else None,
            "discount_rate_pct":  round(discount * 100, 1),
            "terminal_multiple":  terminal_mult,
            "years":              years,
            "method":             "blended growth CAGR + CAPM",
            "revenue_years_used": rev_years,
        }
        return iv, assumptions
    except Exception:
        return None, {}


def _compute_change_pct(technicals: dict, yf_fin: dict) -> float | None:
    """Compute daily % change from yfinance when Finnhub quote is unavailable."""
    price = technicals.get("price") if isinstance(technicals, dict) else None
    prev_close = yf_fin.get("previous_close")
    if price is not None and prev_close is not None and prev_close > 0:
        return round((price - prev_close) / prev_close * 100, 4)
    return None


def _apply_fx_conversion(yf_fin: dict, currency: str, verbose: bool = False) -> float | None:
    """Convert non-USD financial statement values to USD using live FX from yfinance.

    Returns the rate used (USD per 1 unit of local currency, e.g. ~0.031 for TWD),
    or None if the rate could not be fetched.
    """
    try:
        fx_ticker = yf.Ticker(f"{currency}USD=X")
        fx_rate = 0.0
        # fast_info is not a dict — use history for reliable rate fetch
        hist = fx_ticker.history(period="1d")
        if not hist.empty:
            fx_rate = float(hist["Close"].iloc[-1])
        if fx_rate <= 0:
            # Fallback: info dict
            info_price = fx_ticker.info.get("regularMarketPrice") or 0
            fx_rate = float(info_price)
        if fx_rate <= 0:
            return None
    except Exception:
        return None

    if verbose:
        print(f"  [dossier] FX {currency}→USD: {fx_rate:.6f}  (converting all financial statements)")

    def _conv_stmt(entries: list) -> list:
        out = []
        for entry in entries:
            converted = {}
            for k, v in entry.items():
                converted[k] = v * fx_rate if (k != "date" and isinstance(v, (int, float))) else v
            out.append(converted)
        return out

    for key in ("income", "balance", "cashflow"):
        if yf_fin.get(key):
            yf_fin[key] = _conv_stmt(yf_fin[key])

    # Convert absolute-dollar fields in ratios TTM; leave ratios/percentages untouched
    ratios = yf_fin.get("ratios", {})
    for field in ("fcf", "revenue_ttm", "ebitda"):
        if ratios.get(field) is not None:
            ratios[field] = ratios[field] * fx_rate
    # Recompute fcf_per_share from the now-USD fcf and ADR share count
    if ratios.get("fcf") is not None and ratios.get("shares_out"):
        ratios["fcf_per_share"] = round(ratios["fcf"] / ratios["shares_out"], 4)

    return fx_rate


def _fetch_peer(peer_ticker: str) -> dict | None:
    try:
        t = yf.Ticker(peer_ticker)
        info = t.info
        ev    = info.get("enterpriseValue")
        # Peer FCF (2026-07-14): same treatment as the subject's own
        # ratios_ttm.fcf — info['freeCashflow'] is the opaque Yahoo figure
        # verified 20-70%+ off a real TTM (see _ttm_fcf_from_quarterly).
        # Live-measured the day after the subject-side fix: the info-dict
        # basis inflated the peer EV/FCF median +110% on a semis peer set
        # (AVGO/AMD/TXN/QCOM/MU) and +25% on industrials, so the subject's
        # now-genuine TTM FCF was being multiplied by a broken-basis peer
        # multiple (live NVDA: composite $471 / MoS +57% on a $203 price;
        # ~$230 on the truthful 57x median). The multiple's denominator must
        # share the subject FCF's basis or the comp is apples-to-oranges.
        _ttm = _ttm_fcf_from_quarterly(t)
        fcf   = _ttm if _ttm is not None else info.get("freeCashflow")
        rev   = info.get("totalRevenue")
        debt  = info.get("totalDebt")
        bvps  = info.get("bookValue")          # per-share book value
        shrs  = info.get("sharesOutstanding")
        # ev_fcf/ev_sales/ev_ic (2026-07-13, fair_value recalibration): live
        # peer trading multiples, sourced from this SAME .info call — zero
        # extra API load. None (never a fabricated 0) when the underlying
        # figure isn't meaningful for this peer (e.g. FCF/EBITDA for banks) —
        # _peer_median filters out non-positive/missing values, so a peer
        # missing one multiple still contributes to the others.
        ev_fcf = round(ev / fcf, 1) if ev and fcf and fcf > 0 else None
        ev_sales = round(ev / rev, 1) if ev and rev and rev > 0 else None
        peer_ic = (debt or 0) + (bvps * shrs if bvps and shrs else 0)
        ev_ic = round(ev / peer_ic, 2) if ev and peer_ic > 0 else None
        p2b = info.get("priceToBook")
        return {
            "ticker":       peer_ticker,
            "pe":           round(info.get("trailingPE") or 0, 1),
            "fwd_pe":       round(info.get("forwardPE") or 0, 1),
            "ev_ebitda":    round(info.get("enterpriseToEbitda") or 0, 1),
            "rev_growth":   round((info.get("revenueGrowth") or 0) * 100, 1),
            "gross_margin": round((info.get("grossMargins") or 0) * 100, 1),
            "ev_fcf":        ev_fcf,
            "ev_sales":      ev_sales,
            # EV/Invested-Capital: peer equity approximated as bookValue(per
            # share) x sharesOutstanding — the same single-call info fields
            # dossier.py already trusts elsewhere, not a full balance-sheet
            # fetch. A labelled approximation, not the exact dossier-side
            # stockholders_equity figure.
            "ev_ic":         ev_ic,
            # P/B as a P/TBV proxy: exact tangible book (netting goodwill/
            # intangibles) needs a peer balance-sheet fetch we don't make;
            # P/B is the closest single-field approximation and is what
            # info.get("priceToBook") already directly provides.
            "price_to_book": round(p2b, 2) if p2b and p2b > 0 else None,
            "fcf_basis":     "ttm_quarterly_sum" if _ttm is not None else "info_dict_fallback",
        }
    except Exception:
        return None


def _usable_peer(p: dict) -> bool:
    """A peer dict carries at least one real valuation-relevant number — not
    just a ticker with everything None (garbage/illiquid Finnhub peer match;
    found live on ETN: /stock/peers suggested ADSE/HTOO, both missing
    ev_fcf/ev_sales/ev_ic/price_to_book/ev_ebitda entirely)."""
    return bool(p) and any(p.get(k) for k in
                           ("ev_fcf", "ev_sales", "ev_ic", "price_to_book", "ev_ebitda"))


def _better_peer_set(primary: list, fallback: list) -> list:
    """Pick whichever peer_comps list has more usable peers. `primary` unless
    `fallback` (only ever fetched when primary looked thin) is STRICTLY
    better — never assumes a fallback fetch is automatically an improvement."""
    if sum(1 for p in fallback if _usable_peer(p)) > sum(1 for p in primary if _usable_peer(p)):
        return fallback
    return primary


# SEC EDGAR is the authoritative, keyless, current-to-the-minute filing source.
# Finnhub's /stock/filings lagged EDGAR by ~a week and froze some mappings
# (e.g. GOOG at 2016) — sovereign-eye's /api/filings moved to EDGAR 2026-07-09;
# the dossier follows so the agents' filing context matches the dashboard.
SEC_UA = {"User-Agent": "SovereignEye/1.0 daryl.lee97@gmail.com"}
_MEANINGFUL_FORMS = ("10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A",
                     "6-K", "20-F", "40-F", "S-1", "DEF 14A")


def _sec_cik_map() -> dict:
    """ticker (upper) → zero-padded 10-digit CIK, from SEC's canonical mapping
    (~900KB). Cached 7d and shared across every ticker in a run."""
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_UA, timeout=15)
        if not r.ok:
            return {}
        out = {}
        for e in r.json().values():
            t = str(e.get("ticker", "")).upper()
            cik = e.get("cik_str")
            if t and cik is not None:
                out[t] = str(cik).zfill(10)
        return out
    except requests.exceptions.RequestException as exc:
        print(f"  [dossier] SEC ticker map failed: {type(exc).__name__}")
        return {}


def _edgar_latest_filing(ticker: str) -> dict:
    """Latest meaningful SEC filing straight from EDGAR (data.sec.gov submissions
    API — structured, authoritative, includes the report period). Returns {} for
    tickers EDGAR doesn't cover (foreign filers, ETFs) so the caller can fall back."""
    cik = (cached("sec:cik_map:v1", 168, _sec_cik_map) or {}).get(ticker.upper())
    if not cik:
        return {}
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=SEC_UA, timeout=15)
        if not r.ok:
            return {}
        j = r.json()
        recent = ((j.get("filings") or {}).get("recent") or {})
        forms   = recent.get("form") or []
        dates   = recent.get("filingDate") or []
        reports = recent.get("reportDate") or []
        accns   = recent.get("accessionNumber") or []
        docs    = recent.get("primaryDocument") or []
        if not forms:
            return {}
        # `recent` arrays are newest-first — prefer the latest 10-K/10-Q, then any
        # meaningful form, then the newest filing of any kind.
        idx = next((i for i, f in enumerate(forms) if f in ("10-K", "10-Q")), None)
        if idx is None:
            idx = next((i for i, f in enumerate(forms) if f in _MEANINGFUL_FORMS), None)
        if idx is None:
            idx = 0
        accn = (accns[idx] if idx < len(accns) else "").replace("-", "")
        doc  = docs[idx] if idx < len(docs) else ""
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}"
        return {
            "form":       forms[idx],
            "filed_date": dates[idx] if idx < len(dates) else None,
            "period":     (reports[idx] or None) if idx < len(reports) else None,
            "url":        f"{base}/{doc}" if accn and doc else (f"{base}/" if accn else ""),
            "src":        "edgar",
            # SEC's own SIC classification — piggybacks on this same fetch (no extra
            # call). Used only as a cycle_type sector fallback when yfinance is blank.
            "sic_description": j.get("sicDescription") or None,
        }
    except requests.exceptions.RequestException as exc:
        print(f"  [dossier] EDGAR {ticker} failed: {type(exc).__name__}")
        return {}
    except Exception:
        return {}


def _latest_filing(ticker: str) -> dict:
    """Latest SEC filing. EDGAR primary (authoritative, current); Finnhub only as
    a fallback for names EDGAR doesn't index."""
    edgar = _edgar_latest_filing(ticker)
    if edgar:
        return edgar
    try:
        filings = _fh("/stock/filings", {"symbol": ticker})
        if isinstance(filings, list) and filings:
            latest = next((f for f in filings if f.get("form") in ("10-K", "10-Q")), filings[0])
            return {
                "form": latest.get("form"),
                "filed_date": latest.get("filedDate"),
                "period": latest.get("reportDate"),
                "url": latest.get("reportUrl", ""),
                "src": "finnhub",
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


# â"€â"€ Async macro cache (fetched once per run, shared across all tickers) â"€â"€â"€â"€â"€â"€â"€

async def _get_macro() -> dict:
    global _macro_cache, _macro_fetched
    async with _macro_async_lock:
        if _macro_fetched:
            return _macro_cache
        # All 5 FRED series + VIX in parallel
        fed, cpi, unemp, t10, t2, vix = await asyncio.gather(
            asyncio.to_thread(_fred, "FEDFUNDS"),
            asyncio.to_thread(_fred_cpi_yoy),
            asyncio.to_thread(_fred, "UNRATE"),
            asyncio.to_thread(_fred, "DGS10"),
            asyncio.to_thread(_fred, "DGS2"),
            asyncio.to_thread(_get_vix),
        )
        spread = round(t10 - t2, 2) if t10 and t2 else None
        _macro_cache = {
            "fed_funds_rate":     fed,
            "cpi_yoy":            cpi,
            "unemployment":       unemp,
            "treasury_10y":       t10,
            "treasury_2y":        t2,
            "vix":                round(vix, 1) if vix else None,
            "yield_curve_spread": spread,
        }
        _macro_fetched = True
        return _macro_cache


# â"€â"€ Main async builder â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

async def build(ticker: str, verbose: bool = True, meta: dict | None = None) -> dict:
    """Build the full data dossier for a ticker. Async — all sources fetched in parallel.

    meta — optional per-ticker override dict from cleaner.clean_ticker_batch().
           Keys: canonical_sector, is_adr, financials_currency.
           Applied after raw data is fetched but before any assembly, so all
           downstream logic (archetype classification, ADR nulling, etc.) sees
           corrected values without knowing about the override layer.
    """
    ticker = ticker.upper()
    if verbose:
        print(f"\n[dossier] Building dossier for {ticker} (parallel fetch)...")

    dossier: dict = {"ticker": ticker, "built_at": datetime.now(timezone.utc).isoformat()}

    since      = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")
    from_date  = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    to_date    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since_yr   = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    fwd_30     = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")

    # â"€â"€ Batch 1: everything that doesn't depend on another result â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    await emit_live(ticker, {"type": "DOSSIER_START"})

    (
        profile_raw,
        quote_raw,
        technicals,
        yf_fin,
        earnings_raw,
        av_overview_raw,
        insiders_raw,
        fh_news_raw,
        sec_raw,
        macro,
        rec_trends_raw,
        insider_sent_raw,
        usa_spending_raw,
        earnings_cal_raw,
        fmp_estimates_raw,
    ) = await asyncio.gather(
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:profile:{ticker}",       72,  _fh, "/stock/profile2", {"symbol": ticker}), "profile"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"quote:{ticker}",             1,  _quote, ticker), "quote"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:tech:{ticker}",           1,  _technicals, ticker), "technicals"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"yf:fin:{ticker}",           12,  _yf_financials, ticker), "financials"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"av:EARNINGS:{ticker}",      24,  _av, "EARNINGS", {"symbol": ticker}), "earnings"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"av:OVERVIEW:{ticker}",      24,  _av, "OVERVIEW", {"symbol": ticker}), "av_overview"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:insiders:{ticker}",      12,  _fh, "/stock/insider-transactions", {"symbol": ticker, "from": since}), "insiders"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:news:{ticker}",           2,  _fh, "/company-news", {"symbol": ticker, "from": from_date, "to": to_date}), "news"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"sec:filing:{ticker}",       48,  _latest_filing, ticker), "sec_filing"),
        _fetch_and_emit(ticker, _get_macro(), "macro"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:rec:{ticker}",           12,  _fh, "/stock/recommendation", {"symbol": ticker}), "rec_trends"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:insider_sent:{ticker}",  12,  _fh, "/stock/insider-sentiment", {"symbol": ticker, "from": since_yr, "to": to_date}), "insider_sentiment"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:usa_spending:{ticker}",  24,  _fh, "/stock/usa-spending", {"symbol": ticker, "from": since, "to": to_date}), "usa_spending"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fh:earnings_cal:{ticker}",   6,  _fh, "/calendar/earnings", {"symbol": ticker, "from": to_date, "to": fwd_30}), "earnings_cal"),
        _fetch_and_emit(ticker, asyncio.to_thread(cached, f"fmp:estimates:{ticker}",     6,  _fmp_estimates, ticker), "fmp_estimates"),
    )

    # ── Metadata overrides (from cleaner.clean_ticker_batch) ─────────────────
    # Applied here — after raw data arrives but before any assembly — so all
    # downstream logic sees corrected values transparently.
    _meta = meta or {}
    if _meta.get("canonical_sector"):
        # Override both yf_fin["sector"] (used for yf_sector / archetype) and
        # profile_raw's industry field so the profile section picks it up.
        yf_fin["sector"] = _meta["canonical_sector"]
        if verbose:
            print(f"  [dossier] {ticker}: sector overridden to {_meta['canonical_sector']!r} (cleaner)")
    if _meta.get("is_adr"):
        # Force ADR flag regardless of what yfinance quoteType or share-ratio heuristic found
        yf_fin.setdefault("ratios", {})["adr_mismatch"] = True
        if verbose:
            print(f"  [dossier] {ticker}: ADR flag forced True (cleaner)")
    if _meta.get("financials_currency") and _meta.get("financials_currency") != "USD":
        currency = _meta["financials_currency"]
        yf_fin["financials_currency"] = currency
        if verbose:
            print(f"  [dossier] {ticker}: financials_currency={currency} (cleaner)")
        _fx_rate = _apply_fx_conversion(yf_fin, currency, verbose=verbose)
        if _fx_rate:
            yf_fin["fx_rate_to_usd"] = _fx_rate
        elif verbose:
            print(f"  [dossier] {ticker}: FX rate unavailable for {currency} — financial statements remain in local currency")
    elif yf_fin.get("financials_currency"):
        # yfinance-native FX detection (no cleaner required — covers single-ticker runs)
        currency = yf_fin["financials_currency"]
        if verbose:
            print(f"  [dossier] {ticker}: financials_currency={currency} (yfinance — applying FX conversion)")
        _fx_rate = _apply_fx_conversion(yf_fin, currency, verbose=verbose)
        if _fx_rate:
            yf_fin["fx_rate_to_usd"] = _fx_rate
        elif verbose:
            print(f"  [dossier] {ticker}: FX rate unavailable for {currency} — financial statements remain in local currency")

    # ── Profile ───────────────────────────────────────────────────────────────
    sector = profile_raw.get("finnhubIndustry") or yf_fin.get("sector") or "Unknown"
    dossier["profile"] = {
        "name":                 profile_raw.get("name") or yf_fin.get("company_name") or ticker,
        "sector":               sector,
        "yf_sector":            yf_fin.get("sector", ""),   # GICS sector (used for archetype classification)
        "industry":             yf_fin.get("industry", ""),
        "exchange":             profile_raw.get("exchange", ""),
        # For ADR stocks, Finnhub/yfinance return the local-exchange market cap in the
        # local currency. Compute from USD price × ADR shares instead (always USD-correct).
        "market_cap_bn":        round(
            (quote_raw.get("c")
             or (technicals.get("price") if isinstance(technicals, dict) else None)
             or yf_fin.get("previous_close") or 0)
            * (yf_fin.get("ratios", {}).get("shares_out") or 0) / 1e9
            if yf_fin.get("ratios", {}).get("adr_mismatch")
            else (profile_raw.get("marketCapitalization") or (yf_fin.get("market_cap") or 0) / 1e6 or 0) / 1000,
            2,
        ),
        "ipo_date":             profile_raw.get("ipo", ""),
        "employees":            profile_raw.get("employeeTotal", ""),
        "country":              profile_raw.get("country", ""),
        "website":              profile_raw.get("weburl", ""),
        "financials_currency":  yf_fin.get("financials_currency") or "USD",
        "fx_rate_to_usd":       yf_fin.get("fx_rate_to_usd"),   # non-None only for converted ADRs
    }
    # cycle_type keys on GICS sector names (see _cycle_type / SECTOR_GROWTH_CAP) —
    # NOT Finnhub's industry string ("Semiconductors" ≠ GICS "Technology"), which
    # silently mislabelled every semiconductor as HYBRID and cost it ~0.3-0.5 pts
    # in scoring.cycle_position_adjust (2026-07-12 fix, Daryl-approved). Resolve
    # GICS-first: yfinance sector → SEC SIC description → HYBRID. profile["sector"]
    # above stays Finnhub's finer industry label for display.
    _cycle_sector = (yf_fin.get("sector")
                     or _sic_to_sector((sec_raw or {}).get("sic_description"))
                     or "Unknown")
    dossier["cycle_type"] = _cycle_type(_cycle_sector)

    # ── Quote ──â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    dossier["quote"] = {
        "price":      quote_raw.get("c") or (technicals.get("price") if isinstance(technicals, dict) else None),
        "change":     quote_raw.get("d"),
        "change_pct": quote_raw.get("dp") or _compute_change_pct(technicals, yf_fin),
        "high":       quote_raw.get("h"),
        "low":        quote_raw.get("l"),
        "open":       quote_raw.get("o"),
        "prev_close": quote_raw.get("pc") or yf_fin.get("previous_close"),
        "src":        quote_raw.get("_src", "finnhub"),  # source-ranking audit trail
    }

    # â"€â"€ Technicals â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    dossier["technicals"] = technicals

    # â"€â"€ Financials â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    # FMP v3 API deprecated for keys created after Aug 2025 — yfinance is the sole source
    fmp_income, fmp_balance, fmp_cashflow = [], [], []

    yf_r = yf_fin.get("ratios", {})

    # AV OVERVIEW fields (returned as strings; missing/invalid → None via _safe_float)
    av_fwd_pe      = _safe_float(av_overview_raw.get("ForwardPE"))
    av_pb          = _safe_float(av_overview_raw.get("PriceToBookRatio"))
    av_trailing_pe = _safe_float(av_overview_raw.get("PERatio"))

    # Slim AV OVERVIEW block for the dossier (2026-07-11 audit: ~50 fields
    # fetched per run, 3 used, the rest dropped). Display/context only —
    # feeds the dashboard Evidence tab and the debate's raw dump; not scoring.
    dossier["av_overview"] = {
        "analyst_target_price": _safe_float(av_overview_raw.get("AnalystTargetPrice")),
        # AV "PEGRatio" deliberately dropped (2026-07-13): unstated growth basis,
        # returned 0.28 for MNDY (computed off the depressed GAAP base) — an
        # agent quoting it as "the PEG" would be worse than having no number.
        "dividend_yield":       _safe_float(av_overview_raw.get("DividendYield")),
        "roe_ttm":              _safe_float(av_overview_raw.get("ReturnOnEquityTTM")),
        "roa_ttm":              _safe_float(av_overview_raw.get("ReturnOnAssetsTTM")),
        "profit_margin":        _safe_float(av_overview_raw.get("ProfitMargin")),
        "quarterly_earnings_growth_yoy": _safe_float(av_overview_raw.get("QuarterlyEarningsGrowthYOY")),
        "quarterly_revenue_growth_yoy":  _safe_float(av_overview_raw.get("QuarterlyRevenueGrowthYOY")),
        "ev_to_ebitda":         _safe_float(av_overview_raw.get("EVToEBITDA")),
        "beta":                 _safe_float(av_overview_raw.get("Beta")),
    } if av_overview_raw else {}

    _pe_trailing = yf_r.get("pe") or av_trailing_pe
    _pe_forward_raw = yf_r.get("fwd_pe")

    # Prefer AV OVERVIEW forward PE — it's derived from analyst consensus estimates and is
    # correctly adjusted for ADR share structure (fixes the 6.25x vs 12.77x MFG discrepancy).
    if av_fwd_pe:
        _fwd_pe_clean: float | None = av_fwd_pe
        if verbose and _pe_forward_raw and _pe_forward_raw > 0:
            divergence = abs(av_fwd_pe - _pe_forward_raw) / max(av_fwd_pe, _pe_forward_raw)
            if divergence > 0.10:
                print(f"  [dossier] {ticker}: AV forward PE {av_fwd_pe}x overrides "
                      f"yfinance/FMP {_pe_forward_raw}x ({divergence:.0%} divergence)")
    else:
        # Fallback: yfinance/FMP forward PE with sanity check.
        # For ADR stocks (where yfinance PE data may mix local-currency EPS with USD
        # price), apply a strict 100% implied-growth threshold — mismatch is currency.
        # For domestic stocks, never null: high-growth names legitimately show 100-200%
        # implied EPS improvement between trailing and forward PE (NVDA, TSLA, PLTR).
        _is_adr = yf_r.get("adr_mismatch", False)
        _threshold = 1.0 if _is_adr else float("inf")
        _fwd_pe_clean = _pe_forward_raw
        if _pe_trailing and _pe_forward_raw and _pe_trailing > 0 and _pe_forward_raw > 0:
            _implied_growth = _pe_trailing / _pe_forward_raw - 1
            if _implied_growth > _threshold:
                _fwd_pe_clean = None
                if verbose:
                    print(f"  [dossier] {ticker}: forward PE ({_pe_forward_raw:.1f}x) vs trailing "
                          f"({_pe_trailing:.1f}x) implies {_implied_growth:.0%} YoY growth — "
                          f"likely ADR/FX mismatch, nulling fwd_pe")

    # Pre-compute growth/valuation derived metrics for ratios_ttm
    _income_list = yf_fin.get("income", [])
    _ttm_rev_growth_pct = None
    if len(_income_list) >= 2:
        _ri0 = _income_list[0].get("revenue")
        _ri1 = _income_list[1].get("revenue")
        if _ri0 and _ri1 and _ri1 != 0:
            _ttm_rev_growth_pct = round((_ri0 - _ri1) / abs(_ri1) * 100, 2)
    _op_margin_pct = yf_r.get("op_margin")  # already percentage from _pct()
    _r40 = round(_ttm_rev_growth_pct + _op_margin_pct, 2) if (
        _ttm_rev_growth_pct is not None and _op_margin_pct is not None
    ) else None

    _trailing_eps = yf_r.get("trailing_eps") or 0
    _forward_eps  = yf_r.get("forward_eps") or 0
    _implied_ntm_growth = _safe_div(_forward_eps - _trailing_eps, abs(_trailing_eps)) if _trailing_eps else None
    # Forward growth: FMP analyst consensus (live, 250 req/day free) is the primary source.
    # Falls back to yfinance t.earnings_estimate (daily Yahoo consensus), then to stale
    # yfinance info dict (earningsGrowth/revenueGrowth can lag 6-12 months).
    _fmp_est  = fmp_estimates_raw if isinstance(fmp_estimates_raw, dict) else {}
    _yf_est   = yf_fin.get("estimates", {})
    def _first_not_none(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    # NOTE (2026-07-13): yf_fin's "trailing_*_growth_yoy" fields are deliberately
    # NOT in these chains — verified live to not represent forward growth
    # despite Yahoo's field names suggesting otherwise (see _yf_financials).
    # When FMP AND yfinance's own +1y estimate both miss, these now correctly
    # resolve to None rather than silently substituting a trailing figure.
    _fwd_earnings_growth = _first_not_none(
        _fmp_est.get("fwd_eps_growth"),
        _yf_est.get("fwd_eps_growth"),
    )
    _fwd_revenue_growth = _first_not_none(
        _fmp_est.get("fwd_rev_growth"),
        _yf_est.get("fwd_rev_growth"),
    )
    _trailing_earnings_growth = yf_fin.get("trailing_earnings_growth_yoy")
    _trailing_revenue_growth  = yf_fin.get("trailing_revenue_growth_yoy")
    _eps_revision_momentum = _yf_est.get("eps_revision_momentum")  # yfinance eps_trend, no FMP equivalent on free tier

    # WACC — computed from existing data, zero new API calls.
    # Ke = risk_free + beta × 5.5% ERP (Damodaran US). Kd = 5% pre-tax (investment-grade default).
    _wacc = None
    _beta_w = yf_r.get("beta")
    _rf_w   = (macro.get("treasury_10y") or 0) / 100
    # yfinance returns debtToEquity as a percentage (e.g. 30.27 = 30.27% = 0.30x ratio)
    _de_w   = (yf_r.get("debt_equity") or 0) / 100
    if _beta_w is not None and _rf_w > 0 and 0 < _beta_w < 5:
        _ke = _rf_w + _beta_w * 0.055
        _dv = _de_w / (1 + _de_w) if _de_w > 0 else 0.0
        _wacc = round((1 - _dv) * _ke + _dv * 0.05 * 0.79, 4)

    # Long-horizon EPS CAGR (peg_lt denominator) — tries Finviz's true 5-year
    # consensus first (cross-checked against _fwd_pe_clean, computed above),
    # falling back to FMP/AV's ~1-2yr reach. Thread-hopped: blocking IO.
    _cagr_fwd, _cagr_years, _cagr_src = await asyncio.to_thread(
        _resolve_eps_cagr, ticker, _fmp_est.get("eps_cagr_fwd"),
        _fmp_est.get("eps_cagr_years"), _fwd_pe_clean)

    dossier["financials"] = {
        "income":   yf_fin.get("income")   or fmp_income,
        "balance":  yf_fin.get("balance")  or fmp_balance,
        "cashflow": yf_fin.get("cashflow") or fmp_cashflow,
        "ratios_ttm": {
            "pe":            _pe_trailing,
            "fwd_pe":        _fwd_pe_clean,
            "pb":            yf_r.get("pb") or av_pb,
            "ps":            yf_r.get("ps"),
            "ev_ebitda":     yf_r.get("ev_ebitda"),
            "gross_margin":  yf_r.get("gross_margin"),
            "net_margin":    yf_r.get("net_margin"),
            "roe":           yf_r.get("roe"),
            "roic":          _compute_roic(yf_fin),
            "roa":           yf_r.get("roa"),
            "debt_equity":   yf_r.get("debt_equity"),
            "current_ratio": yf_r.get("current_ratio"),
            "fcf_per_share": yf_r.get("fcf_per_share"),
            "fcf":           yf_r.get("fcf"),
            "fcf_source":    yf_r.get("fcf_source"),
            "revenue_ttm":   yf_r.get("revenue_ttm"),
            "ebitda":        yf_r.get("ebitda"),
            "beta":          yf_r.get("beta"),
            "short_pct":     yf_r.get("short_pct"),
            "adr_mismatch":  yf_r.get("adr_mismatch", False),
            "shares_out":    yf_r.get("shares_out"),
            # Growth & valuation metrics
            "fwd_revenue_growth":      _fwd_revenue_growth,
            "fwd_earnings_growth":     _fwd_earnings_growth,
            # TRAILING (most-recent-quarter YoY), NOT forward — despite Yahoo's
            # field names, verified NOT to represent forward consensus (see
            # _yf_financials). A large negative trailing figure alongside
            # healthy forward growth is exactly the "earnings recovering from
            # a depressed/impaired base" pattern the BASE-EFFECT TRAP looks for.
            "trailing_earnings_growth_yoy": _trailing_earnings_growth,
            "trailing_revenue_growth_yoy":  _trailing_revenue_growth,
            # NTM PEG: fwd PE ÷ NEXT-YEAR analyst EPS growth. Compresses when a
            # single rebound year spikes EPS off a low base — agents must cite
            # it as "NTM PEG" and apply the base-effect trap (agents.py PATH A).
            "fwd_peg":                 _safe_div(_fwd_pe_clean, (_fwd_earnings_growth or 0) * 100),
            # Long-horizon PEG: fwd PE ÷ long-horizon consensus EPS CAGR, known
            # basis — the durable-growth cross-check on a compressed NTM PEG.
            # Source chain (see _resolve_eps_cagr): Finviz "EPS next 5Y" —
            # TRUE 5yr, runtime-cross-checked against fwd_pe_clean — → FMP
            # (~2yr reach) → Alpha Vantage (~1-2yr). Nasdaq was tried and
            # rejected (wrong EPS basis). "src"/"years" are the audit trail;
            # None only if every source misses.
            "eps_cagr_fwd":            _cagr_fwd,
            "eps_cagr_fwd_years":      _cagr_years,
            "eps_cagr_fwd_src":        _cagr_src,
            "peg_lt":                  _safe_div(_fwd_pe_clean, (_cagr_fwd or 0) * 100),
            "fcf_yield":               _safe_div(yf_r.get("fcf"), yf_fin.get("market_cap")),
            "rule_of_40":              _r40,
            "implied_ntm_growth":      _implied_ntm_growth,
            "eps_acceleration":        _safe_sub(_fwd_earnings_growth, _implied_ntm_growth),
            "eps_revision_momentum":    _eps_revision_momentum,
            "wacc":                     _wacc,
            "fwd_eps_ntm":              _fmp_est.get("fwd_eps_ntm"),
            "fwd_rev_ntm":              _fmp_est.get("fwd_rev_ntm"),
            "num_analysts_eps":         _fmp_est.get("num_analysts_eps"),
            "est_eps_current_q":        _yf_est.get("est_eps_current_q"),
            "est_eps_current_q_growth": _yf_est.get("est_eps_current_q_growth"),
            "est_eps_next_q":           _yf_est.get("est_eps_next_q"),
            "est_eps_next_q_growth":    _yf_est.get("est_eps_next_q_growth"),
        },
    }

    # ── Valuation ──â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    yf_analyst = yf_fin.get("analyst", {})
    fmp_dcf_price = None   # FMP v3 dead
    fmp_targets: list = [] # FMP v3 dead

    fcf_val = yf_r.get("fcf")
    if fcf_val is None:
        cf_list = yf_fin.get("cashflow", [])
        if cf_list and cf_list[0].get("free_cash_flow") is not None:
            fcf_val = cf_list[0]["free_cash_flow"]
    shares_out = yf_r.get("shares_out")
    # Prefer GICS sector from yfinance over Finnhub's non-standard industry strings
    # (e.g. Finnhub returns "Semiconductors" for NVDA, not the GICS "Technology" that
    # SECTOR_GROWTH_CAP keys on — yf_fin["sector"] was already overridden by cleaner if needed)
    gics_sector = yf_fin.get("sector") or sector
    _balance = dossier["financials"].get("balance") or []
    _b0 = _balance[0] if _balance else {}
    _net_debt = (_b0.get("total_debt") or 0) - (_b0.get("cash") or 0)
    computed_dcf, dcf_assumptions = _dynamic_dcf(
        fcf_val,
        income=dossier["financials"].get("income", []),
        beta=yf_r.get("beta"),
        treasury_10y=macro.get("treasury_10y"),
        sector=gics_sector,
        shares_out=shares_out,
        fwd_revenue_growth=_fwd_revenue_growth,
        fwd_earnings_growth=_fwd_earnings_growth,
        net_debt=_net_debt,
    )

    dossier["valuation"] = {
        "dcf_price":        fmp_dcf_price,
        "dcf_iv_per_share": computed_dcf,
        "dcf_assumptions":  dcf_assumptions,
        "analyst_consensus": yf_analyst or {},
        "analyst_targets":   fmp_targets,
    }

    # â"€â"€ Earnings surprises â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    quarterly = earnings_raw.get("quarterlyEarnings", [])[:8]
    _surprises = []
    for e in quarterly:
        sp_raw = e.get("surprisePercentage")
        try:
            sp_f = float(sp_raw) if sp_raw is not None else None
        except (TypeError, ValueError):
            sp_f = None
        _surprises.append({
            "date":          e.get("fiscalDateEnding"),
            "reported_eps":  e.get("reportedEPS"),
            "estimated_eps": e.get("estimatedEPS"),
            "surprise_pct":  sp_raw,
            # >50% surprise often signals a one-time item, not durable earnings power
            "beat_quality":  ("LARGE_BEAT" if sp_f is not None and sp_f > 50 else
                              "BEAT"        if sp_f is not None and sp_f > 0 else
                              "MISS"        if sp_f is not None and sp_f < 0 else None),
        })
    dossier["earnings_surprises"] = _surprises

    # ── Insider transactions (see process_insider_transactions docstring for
    # the 2026-07-13 transactionType->transactionCode fix history) ────────────
    dossier["insiders"] = process_insider_transactions(
        insiders_raw.get("data", []) if isinstance(insiders_raw, dict) else [])

    # â"€â"€ News â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    fh_news = fh_news_raw[:10] if isinstance(fh_news_raw, list) else []
    dossier["news"] = {
        "finnhub": [
            {"date": n.get("datetime"), "headline": n.get("headline"),
             "source": n.get("source"), "summary": n.get("summary", "")}
            for n in fh_news
        ],
    }

    # â"€â"€ Analyst recommendation trends â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    _rec_list = rec_trends_raw if isinstance(rec_trends_raw, list) else []
    _rec_latest = _rec_list[0] if _rec_list else {}
    dossier["recommendation_trends"] = {
        "period":      _rec_latest.get("period"),
        "strong_buy":  _rec_latest.get("strongBuy"),
        "buy":         _rec_latest.get("buy"),
        "hold":        _rec_latest.get("hold"),
        "sell":        _rec_latest.get("sell"),
        "strong_sell": _rec_latest.get("strongSell"),
    } if _rec_latest else {}

    # â"€â"€ Insider sentiment (MSPR — Money-flow Smart Purchasing Ratio) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    _sent_data = insider_sent_raw.get("data", []) if isinstance(insider_sent_raw, dict) else []
    _sent_recent = _sent_data[-3:] if _sent_data else []
    dossier["insider_sentiment_mspr"] = {
        "monthly": [
            {
                "year":     s.get("year"),
                "month":    s.get("month"),
                "mspr":     s.get("mspr"),     # positive = net buying pressure
                "change":   s.get("change"),   # net insider share change this month
            }
            for s in _sent_recent
            # "purchase"/"sales" dropped 2026-07-13: verified live (47 rows,
            # AAPL/MRVL/NVDA) — Finnhub's actual /stock/insider-sentiment shape
            # is only {change, month, mspr, symbol, year}; those two keys were
            # always None. mspr/change are the real signal this endpoint gives.
        ],
        "avg_mspr_3m": (round(sum(s.get("mspr") or 0 for s in _sent_recent) / len(_sent_recent), 4)
                        if _sent_recent else None),
    }

    # â"€â"€ Government contracts (USA Spending) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    # Finnhub returns {"data": [...], "symbol": "..."} for this endpoint
    _contracts = (usa_spending_raw.get("data", []) if isinstance(usa_spending_raw, dict)
                  else usa_spending_raw if isinstance(usa_spending_raw, list) else [])
    dossier["government_contracts"] = {
        "count":       len(_contracts),
        "total_value": sum(c.get("totalValue", 0) or 0 for c in _contracts),
        "recent":      _contracts[:5],
    }

    # â"€â"€ Upcoming earnings calendar â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    dossier["earnings_calendar"] = {
        "upcoming": _earnings_upcoming(ticker, earnings_cal_raw),
    }

    # â"€â"€ SEC filing â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    dossier["sec_filing"] = sec_raw

    # â"€â"€ Macro (shared cache) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    dossier["macro"] = {**macro, "regime": _detect_regime(macro)}

    # ── Batch 2: peer comps (needs sector from profile) â€" 4 peers in parallel â"€
    # Try Finnhub /stock/peers first (actual industry peers), fall back to sector defaults
    SECTOR_PEERS = {
        "Technology":              ["NVDA", "AMD", "INTC", "QCOM", "AVGO"],
        "Energy":                  ["XOM", "CVX", "COP", "SLB", "EOG"],
        "Utilities":               ["NEE", "EXC", "D", "SO", "AEP"],
        "Financials":              ["JPM", "BAC", "GS", "MS", "C"],
        "Healthcare":              ["UNH", "CVS", "CI", "HUM", "ELV"],
        "Consumer Cyclical":       ["AMZN", "HD", "MCD", "NKE", "SBUX"],
        "Consumer Defensive":      ["WMT", "PG", "KO", "PEP", "COST"],
        "Industrials":             ["GE", "HON", "MMM", "CAT", "DE"],
        "Communication Services":  ["GOOGL", "META", "NFLX", "DIS", "CMCSA"],
        "Real Estate":             ["AMT", "PLD", "CCI", "EQIX", "PSA"],
        "Basic Materials":         ["LIN", "APD", "ECL", "SHW", "NEM"],
    }
    fh_peers_raw = await asyncio.to_thread(_fh, "/stock/peers", {"symbol": ticker})
    if isinstance(fh_peers_raw, list) and len(fh_peers_raw) > 1:
        peers = [p for p in fh_peers_raw if p != ticker and re.match(r'^[A-Z]{1,5}$', p)][:4]
    else:
        peers = [p for p in SECTOR_PEERS.get(gics_sector, ["SPY", "QQQ", "DIA", "IWM"]) if p != ticker][:4]
    # 12h-cached per peer (2026-07-14): peers repeat heavily across dossiers
    # (curated SECTOR_PEERS + megacap Finnhub suggestions), and _fetch_peer now
    # makes a second yfinance call (quarterly_cashflow, for the real-TTM FCF
    # basis) — the cache more than pays that back. cached() never stores a
    # falsy result, so a failed fetch (None) retries instead of poisoning 12h.
    peer_results = await asyncio.gather(*[_fetch_and_emit(ticker, asyncio.to_thread(cached, f"yf:peer:v1:{p}", 12, _fetch_peer, p), "peers") for p in peers])
    peer_comps = [r for r in peer_results if r]

    # Quality gate (2026-07-13, fair_value peer-median recalibration): Finnhub's
    # /stock/peers sometimes suggests thinly-traded/mismatched tickers with
    # almost no usable financial data — found live on ETN (ADSE, HTOO: both
    # missing ev_fcf/ev_sales/price_to_book entirely), which starved the
    # peer-median valuation on a real portfolio holding despite Finnhub
    # "successfully" returning 2+ tickers (the old length-only check passed).
    # Rule-based, not hand-picking which companies count as ETN's "true"
    # peers — just requiring that WHICHEVER list is used actually carries
    # usable numbers. Retries with the curated SECTOR_PEERS list only when
    # the Finnhub-suggested set is thin.
    if sum(1 for p in peer_comps if _usable_peer(p)) < 2:
        fallback_tickers = [p for p in SECTOR_PEERS.get(gics_sector, [])
                            if p != ticker and p not in peers][:4]
        if fallback_tickers:
            fallback_results = await asyncio.gather(
                *[_fetch_and_emit(ticker, asyncio.to_thread(cached, f"yf:peer:v1:{p}", 12, _fetch_peer, p), "peers")
                  for p in fallback_tickers])
            peer_comps = _better_peer_set(peer_comps, [r for r in fallback_results if r])

    dossier["peer_comps"] = peer_comps

    # ── Capital flow (Tiger — entitled 2026-07-11) ─────────────────────────────
    # Institutional-vs-retail money flow. Guarded on TIGER_* env (absent →
    # silently skipped on local runs). Display + measurement factor only.
    try:
        from broker_sync import tiger_capital_flow, tiger_symbol_ok
        if tiger_symbol_ok(ticker):
            cf = await asyncio.to_thread(
                cached, f"tiger:capflow:{ticker}", 6, tiger_capital_flow, ticker)
            if cf:
                dossier["capital_flow"] = cf
    except Exception:
        pass

    # Cross-validate key metrics across data sources
    try:
        from validator import validate_dossier
        dossier["data_quality"] = validate_dossier(dossier)
        if verbose and dossier["data_quality"]["warnings"]:
            print(f"  [dossier] {ticker}: data quality warnings: {dossier['data_quality']['warnings']}")
    except Exception as e:
        dossier["data_quality"] = {"warnings": [], "data_confidence": "HIGH"}
        if verbose:
            print(f"  [dossier] {ticker}: validator error (skipped): {e}")

    # ── Archetype-based fair value ──────────────────────────────────────────────
    try:
        from fair_value import compute_fair_values
        dossier["fair_values"] = compute_fair_values(dossier)
        if verbose:
            fv = dossier["fair_values"]
            arch = (fv.get("archetype") or {}).get("archetype", "?")
            cfv = fv.get("composite_fair_value")
            print(f"  [dossier] {ticker}: archetype={arch}, fair_value={cfv}")
    except Exception as e:
        dossier["fair_values"] = {"error": str(e), "composite_fair_value": None}
        if verbose:
            print(f"  [dossier] {ticker}: fair_value error (skipped): {e}")

    await emit_live(ticker, {"type": "DOSSIER_DONE"})
    if verbose:
        print(f"  [dossier] {ticker} done. {len(str(dossier)):,} chars.")
    return dossier
