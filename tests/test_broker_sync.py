"""broker_sync — Flex XML parsing, symbol mapping, failure isolation, dry-run.

All synthetic: no network, no SDK. Locks the read-only sync's data contract
and that a single broker failure never empties the payload of the other.
"""

import json
from types import SimpleNamespace

import broker_sync
import notify


_FLEX_XML = """<FlexQueryResponse queryName="positions" type="AF">
 <FlexStatements count="1">
  <FlexStatement accountId="U1234567" fromDate="2026-07-07" toDate="2026-07-07">
   <OpenPositions>
    <OpenPosition assetCategory="STK" symbol="AMZN" description="AMAZON.COM INC"
      position="12" costBasisPrice="151.25" currency="USD" listingExchange="NASDAQ" />
    <OpenPosition assetCategory="STK" symbol="HPQ" description="HOPEFUL VENTURES"
      position="1000" costBasisPrice="0.52" currency="CAD" listingExchange="TSXV" />
    <OpenPosition assetCategory="STK" symbol="WEIRD" description="MYSTERY AG"
      position="5" costBasisPrice="10" currency="EUR" listingExchange="IBIS" />
    <OpenPosition assetCategory="OPT" symbol="AMZN 260117C00200000" description="CALL"
      position="1" costBasisPrice="5" currency="USD" listingExchange="" />
    <OpenPosition assetCategory="STK" symbol="GONE" description="SOLD OUT"
      position="0" costBasisPrice="9" currency="USD" listingExchange="NYSE" />
   </OpenPositions>
   <CashReport>
    <CashReportCurrency currency="BASE_SUMMARY" endingCash="4321.987" />
    <CashReportCurrency currency="USD" endingCash="4000" />
   </CashReport>
  </FlexStatement>
 </FlexStatements>
</FlexQueryResponse>"""


def test_parse_flex_rows_cash_and_mapping():
    out = broker_sync._parse_flex(_FLEX_XML)
    by = {r["ticker"]: r for r in out["rows"]}
    assert by["AMZN"]["qty"] == 12 and by["AMZN"]["avg"] == 151.25
    assert by["AMZN"]["name"] == "Amazon.Com Inc"          # title-cased description
    assert "HPQ.V" in by                                    # TSXV → .V suffix
    assert "WEIRD" in by and out["unmapped"] == ["WEIRD"]   # unknown venue flagged
    assert "GONE" not in by                                 # zero qty dropped
    assert not any("260117C" in t for t in by)              # options excluded
    assert out["cash"] == 4321.99                           # BASE_SUMMARY only


def test_map_symbol_rules():
    assert broker_sync.map_symbol("AMZN", "NASDAQ", "USD") == ("AMZN", True)
    assert broker_sync.map_symbol("HPQ", "TSXV", "CAD") == ("HPQ.V", True)
    assert broker_sync.map_symbol("RY", "TSE", "CAD") == ("RY.TO", True)
    assert broker_sync.map_symbol("0700", "SEHK", "HKD") == ("0700.HK", True)
    assert broker_sync.map_symbol("BRK B", "NYSE", "USD") == ("BRK.B", True)
    sym, ok = broker_sync.map_symbol("XYZ", "IBIS", "EUR")
    assert sym == "XYZ" and ok is False


def test_map_tiger_objects():
    pos = [
        SimpleNamespace(contract=SimpleNamespace(symbol="NU", currency="USD", market="US"),
                        quantity=50, average_cost=11.2),
        SimpleNamespace(contract=SimpleNamespace(symbol="SOLD", currency="USD", market="US"),
                        quantity=0, average_cost=1),
    ]
    assets = [SimpleNamespace(summary=SimpleNamespace(cash=1234.567))]
    out = broker_sync._map_tiger(pos, assets)
    assert out["rows"] == [{"ticker": "NU", "qty": 50.0, "avg": 11.2}]
    assert out["cash"] == 1234.57


def test_fetch_ibkr_skips_without_config(monkeypatch):
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)
    assert broker_sync.fetch_ibkr() is None


def test_fetch_tiger_skips_without_config(monkeypatch):
    for k in ("TIGER_ID", "TIGER_ACCOUNT", "TIGER_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert broker_sync.fetch_tiger() is None


def _quiet_telegram(monkeypatch):
    monkeypatch.setattr(notify, "alert_portfolio_sync", lambda *a, **k: True)
    monkeypatch.setattr(notify, "alert_ops", lambda *a, **k: True)


def test_run_isolates_a_failed_broker(monkeypatch):
    """IBKR down → payload contains ONLY Tiger; its rows are what gets pushed."""
    _quiet_telegram(monkeypatch)
    monkeypatch.setattr(broker_sync, "fetch_ibkr", lambda: None)
    monkeypatch.setattr(broker_sync, "fetch_tiger",
                        lambda: {"rows": [{"ticker": "NU", "qty": 50, "avg": 11.2}],
                                 "cash": 100.0, "unmapped": []})
    pushed = []
    monkeypatch.setattr(broker_sync, "push_payload",
                        lambda payload: pushed.append(payload) or
                        {"ok": True, "added": [], "removed": [], "updated": [], "total": 5})
    assert broker_sync.run() == 0
    assert list(pushed[0]["brokers"].keys()) == ["Tiger"]
    assert "IBKR" not in (pushed[0]["cash"] or {})


def test_run_both_brokers_down_exits_nonzero(monkeypatch):
    _quiet_telegram(monkeypatch)
    monkeypatch.setattr(broker_sync, "fetch_ibkr", lambda: None)
    monkeypatch.setattr(broker_sync, "fetch_tiger", lambda: None)
    pushed = []
    monkeypatch.setattr(broker_sync, "push_payload", lambda p: pushed.append(p))
    assert broker_sync.run() == 1
    assert not pushed


def test_dry_run_never_pushes(monkeypatch):
    _quiet_telegram(monkeypatch)
    monkeypatch.setenv("SYNC_DRY_RUN", "1")
    monkeypatch.setattr(broker_sync, "fetch_ibkr",
                        lambda: {"rows": [{"ticker": "AMZN", "qty": 1, "avg": 1}],
                                 "cash": None, "unmapped": []})
    monkeypatch.setattr(broker_sync, "fetch_tiger", lambda: None)
    pushed = []
    monkeypatch.setattr(broker_sync, "push_payload", lambda p: pushed.append(p))
    assert broker_sync.run() == 0
    assert not pushed


def test_push_failure_exits_nonzero(monkeypatch):
    _quiet_telegram(monkeypatch)
    monkeypatch.delenv("SYNC_DRY_RUN", raising=False)
    monkeypatch.setattr(broker_sync, "fetch_ibkr",
                        lambda: {"rows": [], "cash": 1.0, "unmapped": []})
    monkeypatch.setattr(broker_sync, "fetch_tiger", lambda: None)
    monkeypatch.setattr(broker_sync, "push_payload", lambda p: None)
    assert broker_sync.run() == 1


# ── alert formatting ────────────────────────────────────────────────────────────

def test_alert_portfolio_sync_silent_when_no_changes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append(msg) or True)
    assert notify.alert_portfolio_sync(
        {"added": [], "removed": [], "updated": [], "total": 16}, {}, [], []) is False
    assert not sent


def test_alert_portfolio_sync_reports_changes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    notify.alert_portfolio_sync(
        {"added": ["IBKR:MSFT"], "removed": ["IBKR:GOOG"], "updated": ["Tiger:NU"],
         "total": 17},
        {"IBKR": 4321.99}, ["Tiger"], ["WEIRD"])
    msg, topic = sent[0]
    assert topic == notify.TOPIC_SCAN_RESULTS
    assert "IBKR:MSFT" in msg and "IBKR:GOOG" in msg and "Tiger:NU" in msg
    assert "4,321.99" in msg
    assert "WEIRD" in msg and "Tiger" in msg


def test_parse_flex_refuses_quantityless_statement():
    """Live finding (2026-07-08, Daryl's first real statement): the Flex query
    had 'Position Value' but not 'Quantity', so every element lacked the
    `position` attribute. Parsing that into [] would read as "all sold" and
    wipe the broker's dashboard rows — it must refuse (None → broker skipped)."""
    xml = """<FlexQueryResponse queryName="sovereign-eye" type="AF">
     <FlexStatements count="1"><FlexStatement accountId="U1">
      <OpenPositions>
       <OpenPosition currency="USD" symbol="AMZN" description="AMAZON.COM INC"
         listingExchange="NASDAQ" positionValue="7324.8" costBasisPrice="242.7" />
       <OpenPosition currency="USD" symbol="ANET" description="ARISTA"
         listingExchange="NYSE" positionValue="13862.4" costBasisPrice="143.6" />
      </OpenPositions>
     </FlexStatement></FlexStatements></FlexQueryResponse>"""
    assert broker_sync._parse_flex(xml) is None


def test_parse_flex_true_empty_account_is_not_refused():
    """Zero OpenPosition ELEMENTS (a genuinely emptied account) still returns
    rows=[] so a real full exit can sync through."""
    xml = """<FlexQueryResponse queryName="q" type="AF">
     <FlexStatements count="1"><FlexStatement accountId="U1">
      <OpenPositions></OpenPositions>
      <CashReport>
       <CashReportCurrency currency="BASE_SUMMARY" endingCash="5000" />
      </CashReport>
     </FlexStatement></FlexStatements></FlexQueryResponse>"""
    out = broker_sync._parse_flex(xml)
    assert out is not None and out["rows"] == [] and out["cash"] == 5000.0


# ── probe mode (2026-07-11): pure logic only — network sections are try/except ──

def _days(n, start_day=1):
    return [f"2024-05-{d:02d}" for d in range(start_day, start_day + n)]


def test_bars_adjustment_verdict_adjusted_parity():
    """Tiger closes tracking yfinance adjusted closes within dividend drift."""
    t = {d: 100.0 + i for i, d in enumerate(_days(25))}
    y = {d: (100.0 + i) * 1.02 for i, d in enumerate(_days(25))}
    assert broker_sync.bars_adjustment_verdict(t, y) == "adjusted"


def test_bars_adjustment_verdict_detects_unadjusted_split():
    """Pre-split Tiger closes 10x the adjusted series → unadjusted → never
    swap the technicals source onto it."""
    days = _days(30)
    y = {d: 100.0 for d in days}
    t = {d: (1000.0 if i < 15 else 100.0) for i, d in enumerate(days)}
    assert broker_sync.bars_adjustment_verdict(t, y) == "unadjusted"


def test_bars_adjustment_verdict_thin_overlap_is_inconclusive():
    t = {d: 100.0 for d in _days(10)}
    y = {d: 100.0 for d in _days(10)}
    assert broker_sync.bars_adjustment_verdict(t, y) == "inconclusive"


def test_bars_adjustment_verdict_no_overlap_is_inconclusive():
    assert broker_sync.bars_adjustment_verdict({"2024-05-01": 1.0}, {}) == "inconclusive"


def test_probe_without_credentials_never_pushes(monkeypatch, capsys):
    """--probe with nothing configured: reports and exits 0, and structurally
    cannot POST (push_payload would blow up if reached)."""
    for k in ("TIGER_ID", "TIGER_ACCOUNT", "TIGER_PRIVATE_KEY",
              "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID_NAV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(broker_sync, "push_payload",
                        lambda p: (_ for _ in ()).throw(AssertionError("probe pushed!")))
    assert broker_sync.probe() == 0
    out = capsys.readouterr().out
    assert "read-only" in out and "not configured" in out
