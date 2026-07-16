"""dossier._edgar_insider_transactions (2026-07-17) — EDGAR Form 4 as the
PRIMARY insider source. Finnhub's feed was proven incomplete (MU June-2026:
1 of 50 EDGAR-verified sell rows, -22% on 180d sell totals) while 4/5 quiet
names reconciled exact — so the authoritative filing stream takes over, in
the Finnhub row shape, with chunked Finnhub only as no-CIK fallback."""

import dossier
from dossier import _edgar_insider_transactions, _insider_transactions_primary, process_insider_transactions


_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner><reportingOwnerId>
    <rptOwnerName>MEHROTRA SANJAY</rptOwnerName>
  </reportingOwnerId></reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-10</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>850.5</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-11</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><value>840</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-12</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>99999</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-01-01</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>7777</value></transactionShares>
        <transactionPricePerShare><value>100</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


class _Resp:
    def __init__(self, payload=None, text="", ok=True):
        self._payload, self.text, self.ok = payload, text, ok

    def json(self):
        return self._payload


def _wire(monkeypatch, forms):
    """forms: list of (form, accession, filingDate, primaryDocument)."""
    def fake_get(url, headers=None, timeout=None, **kw):
        if "data.sec.gov/submissions/" in url:
            f, a, d, p = zip(*forms) if forms else ([], [], [], [])
            return _Resp(payload={"filings": {"recent": {
                "form": list(f), "accessionNumber": list(a),
                "filingDate": list(d), "primaryDocument": list(p)}}})
        return _Resp(text=_FORM4_XML)

    monkeypatch.setattr(dossier.requests, "get", fake_get)
    monkeypatch.setattr(dossier, "cached",
                        lambda key, ttl, fn, *a: {"FAKE": "0000000123"}
                        if str(key).startswith("sec:cik_map") else fn(*a))
    monkeypatch.setattr(dossier.time, "sleep", lambda s: None)


def test_parses_p_and_s_rows_with_signed_change(monkeypatch):
    _wire(monkeypatch, [("4", "0001-26-000001", "2026-06-12", "xslF345X05/wk-form4.xml")])
    out = _edgar_insider_transactions("FAKE", "2026-01-17")
    rows = out["data"]
    assert len(rows) == 2                       # A-code and pre-window S dropped
    sell = next(r for r in rows if r["transactionCode"] == "S")
    buy = next(r for r in rows if r["transactionCode"] == "P")
    assert sell["change"] == -1000 and sell["transactionPrice"] == 850.5
    assert buy["change"] == 200 and buy["transactionPrice"] == 840
    assert sell["name"] == "MEHROTRA SANJAY"
    assert out["source"] == "edgar_form4"


def test_amendments_and_other_forms_skipped(monkeypatch):
    _wire(monkeypatch, [
        ("4/A", "0001-26-000002", "2026-06-13", "a.xml"),
        ("10-Q", "0001-26-000003", "2026-06-14", "q.htm"),
    ])
    out = _edgar_insider_transactions("FAKE", "2026-01-17")
    assert out["data"] == []                    # covered CIK, zero rows = the truth


def test_totals_flow_through_summary(monkeypatch):
    _wire(monkeypatch, [("4", "0001-26-000001", "2026-06-12", "wk-form4.xml")])
    out = _edgar_insider_transactions("FAKE", "2026-01-17")
    s = process_insider_transactions(out["data"])
    assert s["sell_count"] == 1 and s["total_sell_usd"] == 850_500
    assert s["buy_count"] == 1 and s["total_buy_usd"] == 168_000
    assert s["net_shares"] == 200 - 1000


def test_none_without_cik_and_primary_falls_back(monkeypatch):
    monkeypatch.setattr(dossier, "cached",
                        lambda key, ttl, fn, *a: {} if str(key).startswith("sec:cik_map") else fn(*a))
    assert _edgar_insider_transactions("NOPE.V", "2026-01-17") is None
    monkeypatch.setattr(dossier, "_insider_transactions",
                        lambda t, s: {"data": [{"transactionCode": "P", "change": 1,
                                                "transactionPrice": 5, "name": "X",
                                                "transactionDate": "2026-06-01"}]})
    out = _insider_transactions_primary("NOPE.V", "2026-01-17")
    assert out["data"][0]["transactionCode"] == "P"   # chunked Finnhub fallback used
