"""Index ETFs / funds are skipped by the portfolio scan (2026-10-23).

After the VALU.L mapping fix the daily DD began running hold-mode stock
debates on two broad index ETFs (VALU.L, IWMO.L): no fundamentals data, a
"category error" verdict from its own agents, and ratings written into the
signal history the scoreboard reads. A stock-pillar debate has nothing to
say about a fund. An unreachable quoteType must KEEP the ticker — never
silently drop a real stock from its daily check.
"""
import main


class _T:
    TYPES = {"VALU.L": "ETF", "IWMO.L": "ETF", "VFIAX": "MUTUALFUND", "MU": "EQUITY"}

    def __init__(self, sym):
        if sym == "BOOM":
            raise RuntimeError("yahoo down")
        self.info = {"quoteType": self.TYPES.get(sym)} if sym in self.TYPES else {}


def test_drop_funds_skips_etfs_and_mutual_funds(monkeypatch):
    monkeypatch.setattr(main.yf, "Ticker", _T)
    kept, skipped = main._drop_funds(["MU", "VALU.L", "IWMO.L", "VFIAX"])
    assert kept == ["MU"] and skipped == ["VALU.L", "IWMO.L", "VFIAX"]


def test_drop_funds_keeps_ticker_when_type_unknown(monkeypatch):
    monkeypatch.setattr(main.yf, "Ticker", _T)
    kept, skipped = main._drop_funds(["BOOM", "NOINFO"])
    assert kept == ["BOOM", "NOINFO"] and skipped == []
