"""guidance.py (2026-07-17) — primary-source management guidance from the 8-K
Item 2.02 exhibit. Contracts under test: 2.02-item selection, EX-99 exhibit
preference, HTML stripping, the mechanical verbatim-quote containment gate
(no-guess rule applied to LLM extraction), fact-vs-failure separation
(guidance None vs guidance_error), and accession-keyed caching wiring."""

import json

import pytest

import guidance


# ── fakes ───────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload=None, text="", ok=True):
        self._payload, self.text, self.ok = payload, text, ok

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("HTTP error")


_SUBMISSIONS = {
    "filings": {"recent": {
        # newest-first, like EDGAR: a 10-Q, an 8-K WITHOUT 2.02, then the
        # earnings 8-K we want, then an older one that must not win.
        "form":            ["10-Q", "8-K", "8-K", "8-K"],
        "items":           ["", "5.02,9.01", "2.02,9.01", "2.02,9.01"],
        "accessionNumber": ["0001-26-000004", "0001-26-000003", "0001-26-000002", "0001-26-000001"],
        "primaryDocument": ["q.htm", "exec.htm", "er8k.htm", "old8k.htm"],
        "filingDate":      ["2026-07-10", "2026-07-01", "2026-06-25", "2026-03-25"],
    }}
}

_INDEX = {"directory": {"item": [
    {"name": "er8k.htm"},
    {"name": "ex99-3-supplement.htm"},
    {"name": "ex99-1.htm"},
    {"name": "ex99-1.txt"},
]}}

_PRESS_HTML = (
    "<html><head><style>p{color:red}</style>"
    "<script>alert('ignore me')</script></head><body>"
    "<h1>FakeCorp Reports Q4 Results</h1>"
    "<p>Revenue was $10.0&nbsp;billion, up 20% year over year.</p>"
    "<p>For the first quarter of fiscal 2027, we expect revenue of $12.2 billion "
    "to $12.8 billion and non-GAAP diluted EPS of $1.50 to $1.60.</p>"
    "<table><tr><td>Data Center</td><td>$6.0 billion</td></tr></table>"
    "</body></html>" + "<!-- pad -->" * 40
)


def _fake_cached(key, ttl, fn, *args):
    """Passthrough cache: canned CIK map for the map key, execute otherwise."""
    if str(key).startswith("sec:cik_map"):
        return {"FAKE": "0000000123"}
    return fn(*args)


def _route_requests(monkeypatch, submissions=_SUBMISSIONS, index=_INDEX, doc_html=_PRESS_HTML):
    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if "data.sec.gov/submissions/" in url:
            return _Resp(payload=submissions)
        if url.endswith("/index.json"):
            return _Resp(payload=index)
        return _Resp(text=doc_html)

    monkeypatch.setattr(guidance.requests, "get", fake_get)
    return calls


# ── 8-K selection ───────────────────────────────────────────────────────────

def test_latest_earnings_8k_picks_newest_202_item(monkeypatch):
    _route_requests(monkeypatch)
    monkeypatch.setattr(guidance, "cached", _fake_cached)
    f = guidance._latest_earnings_8k("FAKE")
    assert f["accession"] == "000126000002"      # the 2.02 8-K, not the 5.02 one
    assert f["primary_doc"] == "er8k.htm"
    assert f["filing_date"] == "2026-06-25"
    assert f["cik"] == 123


def test_latest_earnings_8k_empty_for_unknown_ticker(monkeypatch):
    monkeypatch.setattr(guidance, "cached", _fake_cached)
    assert guidance._latest_earnings_8k("NOPE") == {}


# ── exhibit choice ──────────────────────────────────────────────────────────

def test_pick_exhibit_prefers_shortest_htm_ex99(monkeypatch):
    _route_requests(monkeypatch)
    assert guidance._pick_exhibit(123, "000126000002", "er8k.htm") == "ex99-1.htm"


def test_pick_exhibit_falls_back_to_primary_doc(monkeypatch):
    _route_requests(monkeypatch, index={"directory": {"item": [{"name": "er8k.htm"}]}})
    assert guidance._pick_exhibit(123, "000126000002", "er8k.htm") == "er8k.htm"


# ── HTML stripping ──────────────────────────────────────────────────────────

def test_strip_html_drops_script_style_and_unescapes():
    txt = guidance._strip_html(_PRESS_HTML)
    assert "alert(" not in txt and "color:red" not in txt
    assert "$10.0 billion" in txt          # &nbsp; unescaped, whitespace collapsed
    assert "revenue of $12.2 billion to $12.8 billion" in txt


# ── extraction + quote containment gate ─────────────────────────────────────

def _fake_llm(monkeypatch, payload):
    import llm
    monkeypatch.setattr(llm, "call_gemini",
                        lambda *a, **k: json.dumps(payload))


def test_extract_keeps_verified_rows_and_drops_fabricated(monkeypatch):
    _route_requests(monkeypatch)
    _fake_llm(monkeypatch, {
        "guidance": [
            {"metric": "revenue", "period": "Q1 FY2027", "low": 12.2e9, "high": 12.8e9,
             "unit": "USD", "basis": "unstated", "direction": "initiated",
             "verbatim_quote": "we expect revenue of $12.2 billion to $12.8 billion"},
            {"metric": "eps_adjusted", "period": "Q1 FY2027", "low": 9.99, "high": None,
             "unit": "USD_per_share", "basis": "non-GAAP", "direction": "raised",
             "verbatim_quote": "EPS will definitely be $9.99"},   # NOT in the text
        ],
        "segment_revenue": [{"segment": "Data Center", "revenue": 6.0e9, "period": "Q4"}],
    })
    out = guidance._fetch_and_extract(123, "000126000002", "er8k.htm", "FAKE")
    assert len(out["guidance"]) == 1
    assert out["guidance"][0]["metric"] == "revenue"
    assert out["unverified_rows_dropped"] == 1
    assert out["segment_revenue"][0]["segment"] == "Data Center"
    assert out["exhibit"] == "ex99-1.htm"
    assert "_note" in out


def test_extract_quote_check_is_whitespace_and_case_insensitive(monkeypatch):
    _route_requests(monkeypatch)
    _fake_llm(monkeypatch, {"guidance": [
        {"metric": "revenue", "period": "Q1", "low": 1, "high": 2, "unit": "USD",
         "basis": "unstated", "direction": "unclear",
         "verbatim_quote": "We   EXPECT revenue of $12.2 Billion to $12.8 billion"},
    ]})
    out = guidance._fetch_and_extract(123, "000126000002", "er8k.htm", "FAKE")
    assert len(out["guidance"]) == 1
    assert out["unverified_rows_dropped"] == 0


def test_extract_no_guidance_is_a_fact_not_an_error(monkeypatch):
    _route_requests(monkeypatch)
    _fake_llm(monkeypatch, {"guidance": None, "segment_revenue": None})
    out = guidance._fetch_and_extract(123, "000126000002", "er8k.htm", "FAKE")
    assert out["guidance"] is None
    assert "guidance_error" not in out


def test_extract_raises_on_implausibly_short_text(monkeypatch):
    _route_requests(monkeypatch, doc_html="<html><body>tiny</body></html>")
    with pytest.raises(ValueError):
        guidance._fetch_and_extract(123, "000126000002", "er8k.htm", "FAKE")


# ── entry point: fact vs failure, caching wiring ────────────────────────────

def test_guidance_for_ticker_happy_path(monkeypatch):
    _route_requests(monkeypatch)
    seen_keys = []

    def spy_cached(key, ttl, fn, *args):
        if str(key).startswith("sec:cik_map"):
            return {"FAKE": "0000000123"}
        seen_keys.append((key, ttl))
        return {"guidance": [{"metric": "revenue"}], "segment_revenue": None,
                "unverified_rows_dropped": 0, "exhibit": "ex99-1.htm", "_note": "n"}

    monkeypatch.setattr(guidance, "cached", spy_cached)
    out = guidance.guidance_for_ticker("FAKE")
    assert out["guidance"] == [{"metric": "revenue"}]
    assert out["filing_date"] == "2026-06-25"
    assert out["source_url"].endswith("/123/000126000002/er8k.htm")
    # extraction cached by immutable accession, long TTL
    assert seen_keys == [("edgar:guidance:v1:000126000002", 720)]


def test_guidance_for_ticker_no_8k_is_a_note(monkeypatch):
    monkeypatch.setattr(guidance, "cached", _fake_cached)
    monkeypatch.setattr(guidance, "_latest_earnings_8k", lambda t: {})
    out = guidance.guidance_for_ticker("FAKE")
    assert out["guidance"] is None
    assert "no 8-K Item 2.02" in out["note"]
    assert "guidance_error" not in out


def test_guidance_for_ticker_extraction_failure_is_visible_not_fabricated(monkeypatch):
    _route_requests(monkeypatch)

    def failing_cached(key, ttl, fn, *args):
        if str(key).startswith("sec:cik_map"):
            return {"FAKE": "0000000123"}
        raise RuntimeError("flash exploded")

    monkeypatch.setattr(guidance, "cached", failing_cached)
    out = guidance.guidance_for_ticker("FAKE")
    assert out["guidance"] is None
    assert "extraction failed" in out["guidance_error"]
    assert out["source_url"]  # still points at the filing for manual reading


def test_guidance_for_ticker_never_raises(monkeypatch):
    monkeypatch.setattr(guidance, "cached", _fake_cached)
    monkeypatch.setattr(guidance, "_latest_earnings_8k",
                        lambda t: (_ for _ in ()).throw(RuntimeError("edgar down")))
    out = guidance.guidance_for_ticker("FAKE")
    assert out["guidance"] is None
    assert "8-K lookup failed" in out["guidance_error"]
