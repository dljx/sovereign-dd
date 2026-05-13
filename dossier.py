"""Data dossier builder — fetches all source data before agents run."""

import os
import time
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
FMP_KEY = os.getenv("FMP_API_KEY", "")
FRED_KEY = os.getenv("FRED_API_KEY", "")
_av_keys = [k.strip() for k in os.getenv("ALPHA_VANTAGE_API_KEYS", os.getenv("ALPHA_VANTAGE_API_KEY", "")).split(",") if k.strip()]

FH = "https://finnhub.io/api/v1"
FMP = "https://financialmodelingprep.com/api/v3"

_av_idx = 0


def _fh(path: str, params: dict = None) -> dict | list:
    p = {"token": FINNHUB_KEY, **(params or {})}
    r = requests.get(f"{FH}{path}", params=p, timeout=15)
    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(f"{FH}{path}", params=p, timeout=15)
    return r.json() if r.ok else {}


def _fmp(path: str, params: dict = None) -> dict | list:
    p = {"apikey": FMP_KEY, **(params or {})}
    r = requests.get(f"{FMP}{path}", params=p, timeout=15)
    return r.json() if r.ok else {}


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
    """Compute CPI YoY% from last 13 monthly observations (current vs. 12 months ago)."""
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


def _av(function: str, params: dict = None) -> dict:
    global _av_idx
    if not _av_keys:
        return {}
    key = _av_keys[_av_idx % len(_av_keys)]
    _av_idx += 1
    p = {"function": function, "apikey": key, **(params or {})}
    r = requests.get("https://www.alphavantage.co/query", params=p, timeout=15)
    return r.json() if r.ok else {}



def _technicals(ticker: str) -> dict:
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty:
            return {}
        close = hist["Close"]
        sma50 = float(close.rolling(50).mean().iloc[-1])
        sma200 = float(close.rolling(200).mean().iloc[-1])
        price = float(close.iloc[-1])

        # RSI (14)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = float(100 - 100 / (1 + rs.iloc[-1]))

        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = float((ema12 - ema26).iloc[-1])
        signal_line = float((ema12 - ema26).ewm(span=9).mean().iloc[-1])

        # 52w high/low
        w52_high = float(hist["High"].max())
        w52_low = float(hist["Low"].min())
        pct_from_high = round((price - w52_high) / w52_high * 100, 1)

        return {
            "price": round(price, 2),
            "sma50": round(sma50, 2),
            "sma200": round(sma200, 2),
            "above_sma50": price > sma50,
            "above_sma200": price > sma200,
            "rsi_14": round(rsi, 1),
            "macd_line": round(macd_line, 3),
            "macd_signal": round(signal_line, 3),
            "macd_bullish": macd_line > signal_line,
            "52w_high": round(w52_high, 2),
            "52w_low": round(w52_low, 2),
            "pct_from_52w_high": pct_from_high,
        }
    except Exception as e:
        return {"error": str(e)}


def _yf_financials(ticker: str) -> dict:
    """Pull income, balance, cashflow, ratios, and analyst targets from yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        # ── Ratios from .info ──────────────────────────────────────────────
        def _pct(v):
            return round(v * 100, 2) if v is not None else None

        def _r(v, n=2):
            return round(v, n) if v is not None else None

        ratios = {
            "pe":           _r(info.get("trailingPE")),
            "fwd_pe":       _r(info.get("forwardPE")),
            "pb":           _r(info.get("priceToBook")),
            "ps":           _r(info.get("priceToSalesTrailing12Months")),
            "ev_ebitda":    _r(info.get("enterpriseToEbitda")),
            "gross_margin": _pct(info.get("grossMargins")),
            "net_margin":   _pct(info.get("profitMargins")),
            "roe":          _pct(info.get("returnOnEquity")),
            "roa":          _pct(info.get("returnOnAssets")),
            "debt_equity":  _r(info.get("debtToEquity")),
            "current_ratio": _r(info.get("currentRatio")),
            "fcf":          info.get("freeCashflow"),
            "fcf_per_share": _r(info.get("freeCashflow") / info.get("sharesOutstanding", 1)
                                if info.get("freeCashflow") and info.get("sharesOutstanding") else None),
            "revenue_ttm":  info.get("totalRevenue"),
            "ebitda":       info.get("ebitda"),
            "beta":         _r(info.get("beta")),
            "shares_out":   info.get("sharesOutstanding"),
            "short_pct":    _pct(info.get("shortPercentOfFloat")),
        }

        # ── Analyst targets from .info ─────────────────────────────────────
        analyst = {
            "target_mean":   _r(info.get("targetMeanPrice")),
            "target_high":   _r(info.get("targetHighPrice")),
            "target_low":    _r(info.get("targetLowPrice")),
            "num_analysts":  info.get("numberOfAnalystOpinions"),
            "recommendation": info.get("recommendationKey", ""),
        }

        # ── Income statement (annual, last 4 years) ────────────────────────
        income = []
        try:
            fin = t.financials  # columns = dates, rows = line items
            if fin is not None and not fin.empty:
                for col in fin.columns[:4]:
                    rev = fin.loc["Total Revenue", col] if "Total Revenue" in fin.index else None
                    gp  = fin.loc["Gross Profit", col] if "Gross Profit" in fin.index else None
                    oi  = fin.loc["Operating Income", col] if "Operating Income" in fin.index else None
                    ni  = fin.loc["Net Income", col] if "Net Income" in fin.index else None
                    income.append({
                        "date": str(col.date()),
                        "revenue": int(rev) if rev is not None and str(rev) != "nan" else None,
                        "gross_profit": int(gp) if gp is not None and str(gp) != "nan" else None,
                        "operating_income": int(oi) if oi is not None and str(oi) != "nan" else None,
                        "net_income": int(ni) if ni is not None and str(ni) != "nan" else None,
                    })
        except Exception:
            pass

        # ── Balance sheet (annual, last 2 years) ───────────────────────────
        balance = []
        try:
            bs = t.balance_sheet
            if bs is not None and not bs.empty:
                for col in bs.columns[:2]:
                    def _bs(key):
                        return int(bs.loc[key, col]) if key in bs.index and str(bs.loc[key, col]) != "nan" else None
                    balance.append({
                        "date": str(col.date()),
                        "total_assets": _bs("Total Assets"),
                        "total_debt": _bs("Total Debt"),
                        "stockholders_equity": _bs("Stockholders Equity"),
                        "cash": _bs("Cash And Cash Equivalents"),
                        "current_assets": _bs("Current Assets"),
                        "current_liabilities": _bs("Current Liabilities"),
                    })
        except Exception:
            pass

        # ── Cash flow (annual, last 2 years) ───────────────────────────────
        cashflow = []
        try:
            cf = t.cashflow
            if cf is not None and not cf.empty:
                for col in cf.columns[:2]:
                    def _cf(key):
                        return int(cf.loc[key, col]) if key in cf.index and str(cf.loc[key, col]) != "nan" else None
                    op = _cf("Operating Cash Flow") or _cf("Cash Flow From Continuing Operating Activities")
                    capex = _cf("Capital Expenditure")
                    fcf_val = (op + capex) if op and capex else None
                    cashflow.append({
                        "date": str(col.date()),
                        "operating_cf": op,
                        "capex": capex,
                        "free_cash_flow": fcf_val,
                    })
        except Exception:
            pass

        return {"ratios": ratios, "analyst": analyst,
                "income": income, "balance": balance, "cashflow": cashflow}
    except Exception as e:
        return {"error": str(e), "ratios": {}, "analyst": {},
                "income": [], "balance": [], "cashflow": []}


def _simple_dcf(fcf: float | None, growth_rate: float = 0.08,
                discount_rate: float = 0.10, terminal_multiple: float = 15,
                years: int = 5, shares_out: int | None = None) -> float | None:
    """5-year DCF with terminal value. Returns per-share IV if shares_out provided."""
    if not fcf or fcf <= 0:
        return None
    try:
        pv = 0.0
        cf = fcf
        for i in range(1, years + 1):
            cf *= (1 + growth_rate)
            pv += cf / (1 + discount_rate) ** i
        terminal = cf * terminal_multiple / (1 + discount_rate) ** years
        total_pv = pv + terminal
        if shares_out and shares_out > 0:
            return round(total_pv / shares_out, 2)
        return round(total_pv, 0)
    except Exception:
        return None


def _peer_comps(ticker: str, sector: str) -> list[dict]:
    """Get 4 sector peers via yfinance."""
    SECTOR_PEERS = {
        "Technology": ["NVDA", "AMD", "INTC", "QCOM", "AVGO"],
        "Energy": ["NEE", "EXC", "D", "SO", "AEP"],
        "Utilities": ["NEE", "EXC", "D", "SO", "AEP"],
        "Financials": ["JPM", "BAC", "GS", "MS", "C"],
        "Healthcare": ["UNH", "CVS", "CI", "HUM", "ELV"],
        "Consumer Cyclical": ["AMZN", "HD", "MCD", "NKE", "SBUX"],
        "Consumer Defensive": ["WMT", "PG", "KO", "PEP", "COST"],
        "Industrials": ["GE", "HON", "MMM", "CAT", "DE"],
        "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "CMCSA"],
        "Real Estate": ["AMT", "PLD", "CCI", "EQIX", "PSA"],
        "Basic Materials": ["LIN", "APD", "ECL", "SHW", "NEM"],
    }
    peers = [p for p in SECTOR_PEERS.get(sector, ["SPY", "QQQ", "DIA", "IWM"]) if p != ticker][:4]
    comps = []
    for p in peers:
        try:
            info = yf.Ticker(p).info
            comps.append({
                "ticker": p,
                "pe": round(info.get("trailingPE") or 0, 1),
                "fwd_pe": round(info.get("forwardPE") or 0, 1),
                "ev_ebitda": round(info.get("enterpriseToEbitda") or 0, 1),
                "rev_growth": round((info.get("revenueGrowth") or 0) * 100, 1),
                "gross_margin": round((info.get("grossMargins") or 0) * 100, 1),
            })
        except Exception:
            pass
    return comps


def _latest_filing(ticker: str) -> dict:
    """Fetch latest 10-K or 10-Q metadata via Finnhub."""
    try:
        filings = _fh("/stock/filings", {"symbol": ticker})
        if isinstance(filings, list) and filings:
            latest = next((f for f in filings if f.get("form") in ("10-K", "10-Q")), filings[0])
            return {
                "form": latest.get("form"),
                "filed_date": latest.get("filedDate"),
                "period": latest.get("reportDate"),
                "url": latest.get("reportUrl", ""),
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


def build(ticker: str, verbose: bool = True) -> dict:
    """Build the full data dossier for a ticker. Returns a dict."""
    ticker = ticker.upper()
    if verbose:
        print(f"\n[dossier] Building dossier for {ticker}...")

    dossier: dict = {"ticker": ticker, "built_at": datetime.now(timezone.utc).isoformat()}

    # 1. Profile
    if verbose: print("  -> profile...")
    profile = _fh("/stock/profile2", {"symbol": ticker})
    dossier["profile"] = {
        "name": profile.get("name", ticker),
        "sector": profile.get("finnhubIndustry", "Unknown"),
        "exchange": profile.get("exchange", ""),
        "market_cap_bn": round((profile.get("marketCapitalization") or 0) / 1000, 2),
        "ipo_date": profile.get("ipo", ""),
        "employees": profile.get("employeeTotal", ""),
        "country": profile.get("country", ""),
        "website": profile.get("weburl", ""),
    }

    # 2. Quote
    if verbose: print("  -> quote...")
    quote = _fh("/quote", {"symbol": ticker})
    dossier["quote"] = {
        "price": quote.get("c"),
        "change": quote.get("d"),
        "change_pct": quote.get("dp"),
        "high": quote.get("h"),
        "low": quote.get("l"),
        "open": quote.get("o"),
        "prev_close": quote.get("pc"),
    }

    # 3. Technicals (yfinance)
    if verbose: print("  -> technicals...")
    dossier["technicals"] = _technicals(ticker)

    # 4. Financials — yfinance primary, FMP fallback
    if verbose: print("  -> financials (yfinance primary)...")
    yf_fin = _yf_financials(ticker)

    # FMP fallback (only fetched if yfinance returned nothing useful)
    fmp_ratios_data = {}
    fmp_income, fmp_balance, fmp_cashflow = [], [], []
    if not yf_fin.get("income"):
        if verbose: print("  -> financials fallback (FMP)...")
        fmp_income_raw = _fmp(f"/income-statement/{ticker}", {"limit": 4})
        fmp_balance_raw = _fmp(f"/balance-sheet-statement/{ticker}", {"limit": 2})
        fmp_cashflow_raw = _fmp(f"/cash-flow-statement/{ticker}", {"limit": 2})
        fmp_ratios_raw = _fmp(f"/ratios-ttm/{ticker}")
        fmp_ratios_data = fmp_ratios_raw[0] if isinstance(fmp_ratios_raw, list) and fmp_ratios_raw else {}
        fmp_income = fmp_income_raw[:4] if isinstance(fmp_income_raw, list) else []
        fmp_balance = fmp_balance_raw[:2] if isinstance(fmp_balance_raw, list) else []
        fmp_cashflow = fmp_cashflow_raw[:2] if isinstance(fmp_cashflow_raw, list) else []

    yf_r = yf_fin.get("ratios", {})
    dossier["financials"] = {
        "income":   yf_fin.get("income") or fmp_income,
        "balance":  yf_fin.get("balance") or fmp_balance,
        "cashflow": yf_fin.get("cashflow") or fmp_cashflow,
        "ratios_ttm": {
            "pe":           yf_r.get("pe")           or fmp_ratios_data.get("peRatioTTM"),
            "fwd_pe":       yf_r.get("fwd_pe")       or fmp_ratios_data.get("priceEarningsRatioTTM"),
            "pb":           yf_r.get("pb")            or fmp_ratios_data.get("priceToBookRatioTTM"),
            "ps":           yf_r.get("ps")            or fmp_ratios_data.get("priceToSalesRatioTTM"),
            "ev_ebitda":    yf_r.get("ev_ebitda")    or fmp_ratios_data.get("enterpriseValueMultipleTTM"),
            "gross_margin": yf_r.get("gross_margin") or fmp_ratios_data.get("grossProfitMarginTTM"),
            "net_margin":   yf_r.get("net_margin")   or fmp_ratios_data.get("netProfitMarginTTM"),
            "roe":          yf_r.get("roe")           or fmp_ratios_data.get("returnOnEquityTTM"),
            "roic":         fmp_ratios_data.get("returnOnCapitalEmployedTTM"),  # FMP only
            "roa":          yf_r.get("roa"),
            "debt_equity":  yf_r.get("debt_equity")  or fmp_ratios_data.get("debtEquityRatioTTM"),
            "current_ratio": yf_r.get("current_ratio") or fmp_ratios_data.get("currentRatioTTM"),
            "fcf_per_share": yf_r.get("fcf_per_share") or fmp_ratios_data.get("freeCashFlowPerShareTTM"),
            "fcf":          yf_r.get("fcf"),
            "revenue_ttm":  yf_r.get("revenue_ttm"),
            "ebitda":       yf_r.get("ebitda"),
            "beta":         yf_r.get("beta"),
            "short_pct":    yf_r.get("short_pct"),
        },
    }

    # 5. Valuation — yfinance analyst targets + simple DCF; FMP as fallback
    if verbose: print("  -> valuation & analyst targets...")
    yf_analyst = yf_fin.get("analyst", {})

    # FMP DCF fallback
    fmp_dcf_price = None
    if not yf_analyst.get("target_mean"):
        dcf_raw = _fmp(f"/discounted-cash-flow/{ticker}")
        fmp_dcf_price = (dcf_raw[0].get("dcf") if isinstance(dcf_raw, list) and dcf_raw else
                         dcf_raw.get("dcf") if isinstance(dcf_raw, dict) else None)
        fmp_targets_raw = _fmp(f"/price-target/{ticker}")
        fmp_targets = fmp_targets_raw[:5] if isinstance(fmp_targets_raw, list) else []
    else:
        fmp_targets = []

    # Simple DCF from yfinance FCF — prefer .info FCF, fall back to cashflow table
    fcf_val = yf_r.get("fcf")
    if not fcf_val:
        cf_list = yf_fin.get("cashflow", [])
        if cf_list and cf_list[0].get("free_cash_flow"):
            fcf_val = cf_list[0]["free_cash_flow"]
    shares_out = yf_r.get("shares_out")
    computed_dcf = _simple_dcf(fcf_val, shares_out=shares_out)

    dossier["valuation"] = {
        "dcf_price": fmp_dcf_price,
        "dcf_iv_per_share": computed_dcf,   # computed from FCF (conservative 8% growth, 10% discount)
        "analyst_consensus": yf_analyst or {},
        "analyst_targets": fmp_targets,      # FMP format fallback
    }

    # 6. Earnings surprises (Alpha Vantage)
    if verbose: print("  -> earnings surprises (Alpha Vantage)...")
    earnings = _av("EARNINGS", {"symbol": ticker})
    quarterly = earnings.get("quarterlyEarnings", [])[:8]
    dossier["earnings_surprises"] = [
        {
            "date": e.get("fiscalDateEnding"),
            "reported_eps": e.get("reportedEPS"),
            "estimated_eps": e.get("estimatedEPS"),
            "surprise_pct": e.get("surprisePercentage"),
        }
        for e in quarterly
    ]

    # 7. Insider transactions (Finnhub)
    if verbose: print("  -> insider transactions...")
    since = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")
    insiders = _fh("/stock/insider-transactions", {"symbol": ticker, "from": since})
    txns = insiders.get("data", []) if isinstance(insiders, dict) else []
    buys = [t for t in txns if t.get("transactionType") in ("P - Purchase",)]
    sells = [t for t in txns if t.get("transactionType") in ("S - Sale",)]
    dossier["insiders"] = {
        "buy_count": len(buys),
        "sell_count": len(sells),
        "net_shares": sum(t.get("share", 0) for t in buys) - sum(t.get("share", 0) for t in sells),
        "recent": txns[:10],
    }

    # 8. News (Finnhub + Tavily)
    if verbose: print("  -> news...")
    from_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fh_news = _fh("/company-news", {"symbol": ticker, "from": from_date, "to": to_date})
    fh_news = fh_news[:10] if isinstance(fh_news, list) else []

    dossier["news"] = {
        "finnhub": [{"date": n.get("datetime"), "headline": n.get("headline"),
                     "source": n.get("source"), "summary": n.get("summary", "")}
                    for n in fh_news],
    }

    # 9. SEC Filings
    if verbose: print("  -> SEC filings...")
    dossier["sec_filing"] = _latest_filing(ticker)

    # 10. Macro context (FRED + yfinance VIX)
    if verbose: print("  -> macro (FRED)...")
    try:
        vix = float(yf.Ticker("^VIX").history(period="2d")["Close"].iloc[-1])
    except Exception:
        vix = None
    dossier["macro"] = {
        "fed_funds_rate": _fred("FEDFUNDS"),
        "cpi_yoy": _fred_cpi_yoy(),
        "unemployment": _fred("UNRATE"),
        "treasury_10y": _fred("DGS10"),
        "treasury_2y": _fred("DGS2"),
        "vix": round(vix, 1) if vix else None,
    }
    t10 = dossier["macro"]["treasury_10y"]
    t2 = dossier["macro"]["treasury_2y"]
    dossier["macro"]["yield_curve_spread"] = round(t10 - t2, 2) if t10 and t2 else None

    # 11. Peer comps
    if verbose: print("  -> peer comps...")
    sector = dossier["profile"]["sector"]
    dossier["peer_comps"] = _peer_comps(ticker, sector)

    if verbose:
        print(f"  [dossier] Done. {len(str(dossier)):,} chars of data.")
    return dossier
