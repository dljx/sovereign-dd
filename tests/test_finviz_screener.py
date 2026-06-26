"""Tests for finviz_screener fundamental parsing.

Guards the in-house snapshot-table2 parser that replaced finvizfinance's
ticker_fundament() — the library crashes on Finviz's 2026-06 markup change
(div.quote-links removed), so we parse the stable data table directly.
"""

from bs4 import BeautifulSoup

from finviz_screener import _parse_fundament

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
