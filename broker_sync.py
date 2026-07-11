"""Daily broker → dashboard portfolio sync (read-only fetches).

Pulls positions + cash from IBKR (Flex Web Service) and Tiger Brokers
(OpenAPI), maps them to the dashboard's position-row shape, and POSTs them to
the eye's /api/dd/positions broker-scoped merge endpoint. Each broker present
in the payload fully replaces that broker's rows server-side; a broker whose
fetch fails is simply OMITTED (its existing rows stay untouched) and an ops
alert names it.

READ-ONLY CONTRACT
  - IBKR: the Flex token can only download statements — it structurally cannot
    trade. Data reflects the last statement generation (EOD).
  - Tiger: the OpenAPI credential has NO read-only scope (none exists) — this
    module therefore imports and calls ONLY query methods (get_positions /
    get_assets). Never add a trade import here; the key lives in GitHub
    secrets and this file is the only consumer.

Env: IBKR_FLEX_TOKEN, IBKR_FLEX_QUERY_ID, IBKR_FLEX_QUERY_ID_NAV (probe/NAV),
TIGER_ID, TIGER_ACCOUNT, TIGER_PRIVATE_KEY, SOVEREIGN_EYE_URL,
DD_UPLOAD_SECRET, TELEGRAM_*.
SYNC_DRY_RUN=1 fetches + reports without POSTing (first supervised run).

Usage: python broker_sync.py            # daily positions/cash sync
       python broker_sync.py --probe    # read-only API capability probe
"""

from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

load_dotenv()

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_FLEX_POLL_ATTEMPTS = 6
_FLEX_POLL_WAIT = 15  # seconds; Flex statement generation can lag the request

# listingExchange/currency → dashboard (Yahoo-style) suffix. US listings stay
# bare. Anything not covered is kept raw and flagged in the ops alert.
_EXCHANGE_SUFFIX = {
    "TSXV": ".V", "VENTURE": ".V", "TSX-V": ".V",
    "TSE": ".TO", "TSX": ".TO",
    "SEHK": ".HK",
}
_US_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "PINK", "OTCBB",
                 "NMS", "ISLAND", "IEXG", ""}


def map_symbol(symbol: str, exchange: str = "", currency: str = "USD") -> tuple[str, bool]:
    """(dashboard_ticker, mapped_ok). Unmapped foreign listings keep the raw
    symbol with mapped_ok=False so the sync alert asks a human to verify."""
    sym = (symbol or "").upper().strip().replace(" ", ".")
    ex = (exchange or "").upper().strip()
    if ex in _US_EXCHANGES or (currency or "").upper() == "USD" and not ex:
        return sym, True
    if ex in _EXCHANGE_SUFFIX:
        return f"{sym}{_EXCHANGE_SUFFIX[ex]}", True
    if (currency or "").upper() == "USD":
        return sym, True
    return sym, False


# ── IBKR: Flex Web Service (two-step, statement-download-only token) ──────────

def _flex_download(token: str, query_id: str) -> str | None:
    """Two-step Flex fetch: SendRequest → poll GetStatement. XML text or None."""
    r = requests.get(f"{FLEX_BASE}/SendRequest",
                     params={"t": token, "q": query_id, "v": "3"},
                     headers={"User-Agent": "sovereign-dd"}, timeout=30)
    root = ET.fromstring(r.text)
    if (root.findtext("Status") or "").strip() != "Success":
        print(f"  [sync] IBKR: SendRequest failed: {root.findtext('ErrorMessage') or r.text[:200]}")
        return None
    ref = (root.findtext("ReferenceCode") or "").strip()
    stmt_url = (root.findtext("Url") or f"{FLEX_BASE}/GetStatement").strip()

    for attempt in range(_FLEX_POLL_ATTEMPTS):
        r2 = requests.get(stmt_url, params={"t": token, "q": ref, "v": "3"},
                          headers={"User-Agent": "sovereign-dd"}, timeout=60)
        # A not-ready statement comes back as a small FlexStatementResponse
        # with an error code; the real thing is a FlexQueryResponse.
        if "<FlexQueryResponse" in r2.text:
            return r2.text
        print(f"  [sync] IBKR: statement not ready (attempt {attempt + 1}) — waiting {_FLEX_POLL_WAIT}s")
        time.sleep(_FLEX_POLL_WAIT)
    print("  [sync] IBKR: statement never became ready")
    return None


def fetch_ibkr() -> dict | None:
    """{rows: [...], cash: float|None, unmapped: [...]} or None (skip broker)."""
    token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.getenv("IBKR_FLEX_QUERY_ID", "").strip()
    if not token or not query_id:
        print("  [sync] IBKR: no Flex token/query configured — skipping")
        return None
    try:
        xml_text = _flex_download(token, query_id)
        if xml_text is None:
            return None
        return _parse_flex(xml_text)
    except Exception as e:
        print(f"  [sync] IBKR fetch failed: {e}")
        return None


def _parse_flex(xml_text: str) -> dict:
    """Parse a Flex XML statement into {rows, cash, unmapped}."""
    root = ET.fromstring(xml_text)
    rows, unmapped = [], []
    elements = 0
    for pos in root.iter("OpenPosition"):
        elements += 1
        if (pos.get("assetCategory") or "STK") not in ("STK", ""):
            continue
        # `position` (share count) requires the "Quantity" field ticked in the
        # Flex query's Open Positions section — see the structural guard below.
        qty = float(pos.get("position") or 0)
        if qty <= 0:
            continue
        avg = float(pos.get("costBasisPrice") or pos.get("avgCost") or 0)
        ticker, ok = map_symbol(pos.get("symbol"), pos.get("listingExchange"),
                                pos.get("currency"))
        if not ok:
            unmapped.append(ticker)
        rows.append({"ticker": ticker, "qty": qty, "avg": round(avg, 4),
                     "name": (pos.get("description") or ticker).title()})

    # Structural guard: position elements EXIST but none parsed into a row →
    # the query is misconfigured (e.g. missing the Quantity field), NOT a
    # genuinely emptied account. Returning [] here would tell the merge
    # endpoint "all IBKR positions sold" and wipe the dashboard rows — refuse
    # instead (fetch_ibkr treats None as broker-unavailable; rows untouched).
    if elements > 0 and not rows:
        print(f"  [sync] IBKR: {elements} position element(s) but 0 parseable rows — "
              "Flex query likely missing the 'Quantity' field; refusing to sync IBKR")
        return None

    cash = None
    for c in root.iter("CashReportCurrency"):
        if (c.get("currency") or "").upper() == "BASE_SUMMARY":
            try:
                cash = round(float(c.get("endingCash")), 2)
            except (TypeError, ValueError):
                pass
    return {"rows": rows, "cash": cash, "unmapped": unmapped}


# ── Tiger: OpenAPI (query methods ONLY — see module docstring) ────────────────

def fetch_tiger() -> dict | None:
    """{rows, cash, unmapped} or None (skip broker)."""
    tiger_id = os.getenv("TIGER_ID", "").strip()
    account = os.getenv("TIGER_ACCOUNT", "").strip()
    private_key = os.getenv("TIGER_PRIVATE_KEY", "").strip()
    if not tiger_id or not account or not private_key:
        print("  [sync] Tiger: no credentials configured — skipping")
        return None
    try:
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.trade.trade_client import TradeClient
    except ImportError:
        print("  [sync] Tiger: tigeropen SDK not installed — skipping")
        return None
    try:
        cfg = TigerOpenClientConfig()
        cfg.tiger_id = tiger_id
        cfg.account = account
        cfg.private_key = private_key
        client = TradeClient(cfg)
        return _map_tiger(client.get_positions(), client.get_assets())
    except Exception as e:
        print(f"  [sync] Tiger fetch failed: {e}")
        return None


def _map_tiger(positions, assets) -> dict:
    """Map tigeropen Position/PortfolioAccount objects → {rows, cash, unmapped}."""
    rows, unmapped = [], []
    for p in positions or []:
        contract = getattr(p, "contract", None)
        symbol = getattr(contract, "symbol", None) or getattr(p, "symbol", "")
        currency = getattr(contract, "currency", None) or "USD"
        market = getattr(contract, "market", None) or ""
        qty = float(getattr(p, "quantity", 0) or 0)
        if qty <= 0:
            continue
        avg = float(getattr(p, "average_cost", 0) or 0)
        ticker, ok = map_symbol(symbol, market, currency)
        if not ok:
            unmapped.append(ticker)
        rows.append({"ticker": ticker, "qty": qty, "avg": round(avg, 4)})
    cash = None
    for a in assets or []:
        summary = getattr(a, "summary", None)
        c = getattr(summary, "cash", None)
        if c is not None:
            try:
                cash = round(float(c), 2)
            except (TypeError, ValueError):
                pass
            break
    return {"rows": rows, "cash": cash, "unmapped": unmapped}


# ── Probe mode: read-only capability diagnostics (prints only, no POSTs) ──────
#
# `python broker_sync.py --probe` reports what the Tiger/IBKR credentials can
# actually access — quote permissions, delayed quotes, kline quota, whether
# Tiger bars are split-adjusted, get_analytics_asset (NAV history) shape,
# funding history, earnings calendar, short interest. Results gate the
# NAV-history and quote-source-ranking phases (plan 2026-07-11).

def bars_adjustment_verdict(tiger_closes: dict, yf_adj_closes: dict) -> str:
    """Compare Tiger daily closes to yfinance ADJUSTED closes over a window
    spanning a known split (NVDA 2024-06-10 10:1). 'adjusted' → Tiger tracks
    the split-adjusted series (safe to source technicals from it);
    'unadjusted' → raw prices (must NOT swap — momentum would break across
    splits); 'inconclusive' → not enough overlap to judge."""
    ratios = []
    for d in sorted(set(tiger_closes) & set(yf_adj_closes)):
        y = yf_adj_closes[d]
        if y:
            ratios.append(tiger_closes[d] / y)
    if len(ratios) < 20:
        return "inconclusive"
    lo, hi = min(ratios), max(ratios)
    if 0.93 <= lo and hi <= 1.07:  # tolerance for dividend-adjustment drift
        return "adjusted"
    if hi / max(lo, 1e-9) >= 1.5:  # ratio jumps across the split boundary
        return "unadjusted"
    return "inconclusive"


def _show(name: str, value) -> None:
    """Print one probe result compactly, whatever shape tigeropen returned."""
    try:
        if hasattr(value, "columns"):  # pandas DataFrame
            print(f"  [probe] {name}: {len(value)} row(s) · columns={list(value.columns)}")
            print(value.head(3).to_string())
        elif isinstance(value, (list, tuple)):
            print(f"  [probe] {name}: {len(value)} item(s)")
            for item in value[:3]:
                print(f"    {item!r}"[:400])
        else:
            print(f"  [probe] {name}: {value!r}"[:1200])
    except Exception as e:  # printing must never kill the probe
        print(f"  [probe] {name}: <unprintable: {e}>")


def _probe_section(name: str, fn) -> None:
    print(f"\n-- {name} " + "-" * max(0, 62 - len(name)))
    try:
        fn()
    except Exception as e:
        print(f"  [probe] {name} FAILED: {type(e).__name__}: {e}")


def _probe_ibkr_nav() -> None:
    from collections import Counter
    token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    nav_qid = os.getenv("IBKR_FLEX_QUERY_ID_NAV", "").strip()
    if not token or not nav_qid:
        print("  [probe] IBKR_FLEX_QUERY_ID_NAV not configured — manual step pending "
              "(Client Portal → Performance & Reports → Flex Queries → create "
              "'sovereign-nav' with Change in NAV + Cash Transactions, last 365 days, "
              "XML; add its ID as a repo secret)")
        return
    xml_text = _flex_download(token, nav_qid)
    if xml_text is None:
        return
    root = ET.fromstring(xml_text)
    tags = Counter(el.tag for el in root.iter())
    print(f"  [probe] NAV statement elements: {dict(tags.most_common(15))}")
    for tag in ("ChangeInNAV", "CashTransaction", "EquitySummaryByReportDateInBase"):
        el = next(root.iter(tag), None)
        if el is not None:
            print(f"  [probe] sample <{tag}>: {dict(list(el.attrib.items())[:14])}")


def probe() -> int:
    """Read-only capability probe — prints findings, never POSTs anything."""
    from datetime import datetime, timedelta, timezone

    print("[probe] broker API capability probe — read-only, nothing is written")
    today = datetime.now(timezone.utc).date()

    tiger_id = os.getenv("TIGER_ID", "").strip()
    account = os.getenv("TIGER_ACCOUNT", "").strip()
    private_key = os.getenv("TIGER_PRIVATE_KEY", "").strip()
    if tiger_id and account and private_key:
        try:
            from tigeropen.common.consts import Market
            from tigeropen.quote.quote_client import QuoteClient
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient

            cfg = TigerOpenClientConfig()
            cfg.tiger_id = tiger_id
            cfg.account = account
            cfg.private_key = private_key
            # NEVER default-construct QuoteClient: is_grab_permission defaults
            # to True, which SEIZES the quote entitlement from the owner's
            # Tiger app session. Every QuoteClient here must pass False.
            qc = QuoteClient(cfg, is_grab_permission=False)
            tc = TradeClient(cfg)
        except Exception as e:
            print(f"  [probe] Tiger client construction FAILED: {type(e).__name__}: {e}")
            qc = tc = None

        if qc is not None:
            _probe_section("quote permissions (no grab)",
                           lambda: (_show("permissions", qc.get_quote_permission()),
                                    _show("addon entitlement", qc.get_addon_entitlement())))
            _probe_section("delayed briefs AAPL/NVDA",
                           lambda: _show("delay briefs", qc.get_stock_delay_briefs(["AAPL", "NVDA"])))
            _probe_section("kline quota",
                           lambda: _show("quota", qc.get_kline_quota(with_details=True)))

            def _bars_parity():
                bars = qc.get_bars(["NVDA"], begin_time="2024-05-01",
                                   end_time="2024-08-01", limit=80)
                tiger_closes = {
                    datetime.fromtimestamp(row["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"):
                        float(row["close"])
                    for _, row in bars.iterrows()
                }
                import yfinance as yf
                hist = yf.Ticker("NVDA").history(start="2024-05-01", end="2024-08-01")
                yf_closes = {idx.strftime("%Y-%m-%d"): float(c)
                             for idx, c in hist["Close"].items()}
                common = sorted(set(tiger_closes) & set(yf_closes))
                ratios = [tiger_closes[d] / yf_closes[d] for d in common if yf_closes[d]]
                print(f"  [probe] bars: tiger={len(tiger_closes)} yf={len(yf_closes)} "
                      f"overlap={len(common)}")
                if ratios:
                    print(f"  [probe] tiger/yf close ratio: min={min(ratios):.4f} "
                          f"max={max(ratios):.4f}")
                print(f"  [probe] ADJUSTMENT VERDICT: "
                      f"{bars_adjustment_verdict(tiger_closes, yf_closes)} "
                      f"(window spans NVDA 2024-06-10 10:1 split)")

            _probe_section("bars adjustment parity (NVDA across 2024 split)", _bars_parity)
            _probe_section("earnings calendar US next 14d",
                           lambda: _show("calendar", qc.get_corporate_earnings_calendar(
                               Market.US, str(today), str(today + timedelta(days=14)))))
            _probe_section("short interest AAPL",
                           lambda: _show("short interest", qc.get_short_interest(["AAPL"])))

        if tc is not None:
            def _analytics():
                r = tc.get_analytics_asset(
                    start_date=str(today - timedelta(days=365)), end_date=str(today))
                if isinstance(r, dict):
                    hist = r.get("history") or []
                    print(f"  [probe] analytics keys: {list(r.keys())}")
                    print(f"  [probe] history: {len(hist)} point(s)")
                    if hist:
                        print(f"  [probe] first: {hist[0]!r}"[:400])
                        print(f"  [probe] last:  {hist[-1]!r}"[:400])
                        dts = [h.get("dt") for h in hist if isinstance(h, dict) and h.get("dt")]
                        if dts:
                            print(f"  [probe] span {dts[0]} → {dts[-1]} ({len(dts)} dated)")
                else:
                    _show("analytics result", r)

            _probe_section("NAV analytics (get_analytics_asset, 365d)", _analytics)
            _probe_section("funding history (deposits/withdrawals)",
                           lambda: _show("funding", tc.get_funding_history()))
    else:
        print("  [probe] Tiger credentials not configured — skipping Tiger sections")

    _probe_section("IBKR NAV Flex query", _probe_ibkr_nav)
    print("\n[probe] done — no writes were made anywhere")
    return 0


# ── Push + report ──────────────────────────────────────────────────────────────

def push_payload(payload: dict) -> dict | None:
    base = os.getenv("SOVEREIGN_EYE_URL", "").rstrip("/")
    secret = os.getenv("DD_UPLOAD_SECRET", "")
    if not base or not secret:
        print("  [sync] SOVEREIGN_EYE_URL / DD_UPLOAD_SECRET not set — cannot push")
        return None
    r = requests.post(f"{base}/api/dd/positions",
                      headers={"Authorization": f"Bearer {secret}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=30)
    if not r.ok:
        print(f"  [sync] push failed: HTTP {r.status_code} {r.text[:200]}")
        return None
    return r.json()


def run() -> int:
    dry = os.getenv("SYNC_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    results = {"IBKR": fetch_ibkr(), "Tiger": fetch_tiger()}
    fetched = {b: r for b, r in results.items() if r is not None}
    failed_brokers = [b for b, r in results.items() if r is None]

    if not fetched:
        print("  [sync] both brokers unavailable — nothing to sync")
        return 1

    brokers = {b: r["rows"] for b, r in fetched.items()}
    cash = {b: r["cash"] for b, r in fetched.items() if r.get("cash") is not None}
    unmapped = [t for r in fetched.values() for t in r.get("unmapped", [])]

    for b, rows in brokers.items():
        print(f"  [sync] {b}: {len(rows)} position(s)"
              + (f" · cash {cash[b]:.2f}" if b in cash else ""))

    payload = {"brokers": brokers, "cash": cash or None}

    if dry:
        import json as _json
        print("  [sync] DRY RUN — payload below, nothing pushed")
        print(_json.dumps(payload, indent=2))
        return 0

    resp = push_payload(payload)
    if resp is None:
        return 1
    print(f"  [sync] merged: +{len(resp.get('added', []))} "
          f"-{len(resp.get('removed', []))} ~{len(resp.get('updated', []))} "
          f"· {resp.get('total')} row(s) total")

    try:
        from notify import alert_portfolio_sync, alert_ops
        alert_portfolio_sync(resp, cash, failed_brokers, unmapped)
        if failed_brokers:
            alert_ops(f"Broker sync: {', '.join(failed_brokers)} unavailable this run — "
                      f"its rows were left untouched. One-off is fine; repeated runs "
                      f"mean credentials/API changed.")
    except Exception as e:
        print(f"  [sync] telegram skipped ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(probe() if "--probe" in sys.argv[1:] else run())
