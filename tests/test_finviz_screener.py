"""Tests for finviz_screener fundamental parsing.

Guards the in-house snapshot-table2 parser that replaced finvizfinance's
ticker_fundament() — the library crashes on Finviz's 2026-06 markup change
(div.quote-links removed), so we parse the stable data table directly.
"""

from bs4 import BeautifulSoup

from finviz_screener import _parse_fundament, _repair_avatar_tickers

# Minimal stand-in for the live Finviz quote page: the sector/industry/country
# screener links + the snapshot-table2 metric grid the bot actually consumes.
_PAGE = """
<html><body>
  <h2 class="quote-header_ticker-wrapper_company">Acme Corp</h2>
  <div class="quote-tabs_links">
    <a href="/screener.ashx?v=111&f=sec_technology">Technology</a>
    <a href="/screener.ashx?v=111&f=ind_softwareinfrastructure">Software</a>
    <a href="/screener.ashx?v=111&f=geo_usa">USA</a>
  </div>
  <table class="snapshot-table2">
    <tr>
      <td>P/E</td><td>22.50</td><td>ROIC</td><td>18.40%</td>
      <td>Gross Margin</td><td>61.20%</td>
    </tr>
    <tr>
      <td>Debt/Eq</td><td>0.35</td><td>Recom</td><td>1.80</td>
      <td>Industry</td><td>ignored-in-table</td>
    </tr>
  </table>
</body></html>
"""


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def test_parse_fundament_extracts_snapshot_metrics():
    f = _parse_fundament(_soup(_PAGE), "ACME")
    assert f["Ticker"] == "ACME"
    assert f["Company"] == "Acme Corp"
    assert f["P/E"] == "22.50"
    assert f["ROIC"] == "18.40%"
    assert f["Gross Margin"] == "61.20%"
    assert f["Debt/Eq"] == "0.35"
    assert f["Recom"] == "1.80"


def test_parse_fundament_recovers_sector_industry_country_from_links():
    f = _parse_fundament(_soup(_PAGE), "ACME")
    assert f["Sector"] == "Technology"
    # The snapshot table also has an "Industry" cell; the link-derived value
    # is set first and must not be clobbered by the table cell.
    assert f["Industry"] == "Software"
    assert f["Country"] == "USA"


def test_parse_fundament_empty_when_table_missing():
    """No snapshot-table2 → no data → {} (so the candidate is filtered out)."""
    html = '<html><body><h2 class="quote-header_ticker-wrapper_company">X</h2></body></html>'
    assert _parse_fundament(_soup(html), "X") == {}


# Finviz's 2026-07-13 markup change: the one big grid is now SIX sibling
# <table class="snapshot-table2"> blocks, not one table with many rows.
# find() (singular) silently kept only the first block — dropping P/E, PEG,
# EPS growth, margins, ROE/ROIC, insider/institutional data and technicals
# from every field pillar_scoring.compute_composite() reads. This locks the
# find_all-and-merge fix in place.
_MULTI_TABLE_PAGE = """
<html><body>
  <h2 class="quote-header_ticker-wrapper_company">Acme Corp</h2>
  <div class="quote-tabs_links">
    <a href="/screener.ashx?v=111&f=sec_technology">Technology</a>
  </div>
  <table class="snapshot-table2">
    <tr><td>Market Cap</td><td>4.26B</td><td>Sales</td><td>1.30B</td></tr>
  </table>
  <table class="snapshot-table2">
    <tr><td>P/E</td><td>36.24</td><td>PEG</td><td>1.27</td></tr>
  </table>
  <table class="snapshot-table2">
    <tr><td>EPS next Y</td><td>21.14%</td><td>EPS next 5Y</td><td>12.11%</td></tr>
  </table>
</body></html>
"""


def test_parse_fundament_merges_all_sibling_snapshot_tables():
    f = _parse_fundament(_soup(_MULTI_TABLE_PAGE), "ACME")
    # Fields from ALL THREE sibling tables must be present, not just the first.
    assert f["Market Cap"] == "4.26B"
    assert f["P/E"] == "36.24" and f["PEG"] == "1.27"
    assert f["EPS next Y"] == "21.14%" and f["EPS next 5Y"] == "12.11%"


# Finviz's 2026-07-15 markup change: every screener row now prepends a
# first-letter "avatar" badge to the symbol cell, and Overview().screener_view()
# reads the cell as plain text — concatenating the badge letter onto the ticker
# ('ABNB' -> 'AABNB', 'BKNG' -> 'BBKNG'). The corrupted symbols 404 on
# enrichment (or resolve to wrong data), so pillar scores collapse to neutral
# and Gems went 0-BUY for ~3 weeks. These lock the repair in place.

def test_repair_avatar_tickers_strips_badge_when_present():
    # Badge on every row: each real symbol arrives with its first char doubled.
    # Genuine double-letter names (AA, AAPL) get a THIRD leading char and must
    # end up correct after stripping exactly one.
    raw = ["AABNB", "BBKNG", "DDECK", "EEXEL", "AAAPL", "AAA", "ZZVRA"]
    assert _repair_avatar_tickers(raw) == [
        "ABNB", "BKNG", "DECK", "EXEL", "AAPL", "AA", "ZVRA",
    ]


def test_repair_avatar_tickers_leaves_clean_symbols_untouched():
    # Badge absent (Finviz reverts): only the rare genuine double-letter name
    # has a doubled lead char — too small a fraction to look like a badge — so
    # nothing is stripped and 'AAPL' is never mangled to 'APL'.
    raw = ["ABNB", "MSFT", "AA", "AAON", "GOOG", "NVDA", "META", "TSLA"]
    assert _repair_avatar_tickers(raw) == raw


def test_repair_avatar_tickers_empty():
    assert _repair_avatar_tickers([]) == []


# ── New Finviz screener page (live since ~2026-09-30) ───────────────────────
# screener.ashx now 301s to /screener/stocks, which 403s finvizfinance's
# malformed User-Agent and renders results as embedded JSON (20 rows/page,
# "totalCount", r=21/41/... paging) — no <table>, so screener_view() read
# nothing. All 16 scout screen calls failed every run for 3+ weeks.

import pytest

import finviz_screener

_SCREEN_PAGE = ('<script>{"columns":["ticker"],"flags":{},"data":{"rows":['
         '{"text":{"ticker":"AACI","company":"A"},"numbers":{}},'
         '{"text":{"ticker":"BRK.B","company":"B"},"numbers":{}}],'
         '"totalCount":%d}}</script>')


def test_parse_screener_page_reads_json_rows_and_total():
    tickers, total = finviz_screener._parse_screener_page(_SCREEN_PAGE % 2)
    assert tickers == ["AACI", "BRK.B"] and total == 2


def test_parse_screener_page_empty_result():
    assert finviz_screener._parse_screener_page('{"data":{"rows":[],"totalCount":0}}') == ([], 0)


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_fetch_screen_pages_until_total(monkeypatch):
    pages = {1: _SCREEN_PAGE % 3, 21: ('{"data":{"rows":[{"text":{"ticker":"ZZZ"}}],'
                                '"totalCount":3}}')}
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params), headers["User-Agent"]))
        return _Resp(pages[params.get("r", 1)])

    monkeypatch.setattr(finviz_screener.requests, "get", fake_get)
    monkeypatch.setattr(finviz_screener.time, "sleep", lambda s: None)
    out = finviz_screener._fetch_screen({"v": 111, "f": "cap_small,geo_usa"})
    assert out == ["AACI", "BRK.B", "ZZZ"]
    assert calls[0][0] == "https://finviz.com/screener/stocks"
    assert calls[0][1]["f"] == "cap_small,geo_usa" and calls[1][1]["r"] == 21
    assert "  " not in calls[0][2]   # a clean UA — the library's has a run of spaces


def test_fetch_screen_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(finviz_screener.requests, "get",
                        lambda *a, **k: _Resp("blocked", 403))
    with pytest.raises(RuntimeError):
        finviz_screener._fetch_screen({"v": 111, "f": "geo_usa"})


def test_library_user_agent_is_replaced():
    import finvizfinance.util as fv_util
    assert fv_util.headers["User-Agent"] == finviz_screener._FINVIZ_UA
