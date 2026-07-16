"""dossier._insider_transactions (2026-07-17) — cap-aware window chunking.
Finnhub's insider-transactions endpoint silently truncates at ~150 rows per
response; MU's 180d window overflowed and dossier sell totals ran -22% vs
EDGAR Form 4 ground truth (4/5 quieter names reconciled exact-to-the-dollar).
Contracts: capped windows split recursively (min 7d, request budget 16),
boundary duplicates deduped, output shape/order preserved."""

import dossier
from dossier import _FH_INSIDER_CAP, _insider_transactions


def _row(name, date, code="S", change=-100, price=10.0):
    return {"name": name, "transactionDate": date, "transactionCode": code,
            "change": change, "transactionPrice": price}


def test_uncapped_window_is_a_single_request(monkeypatch):
    calls = []

    def fake_fh(path, params):
        calls.append(params)
        return {"data": [_row("A", "2026-06-01"), _row("B", "2026-05-01")]}

    monkeypatch.setattr(dossier, "_fh", fake_fh)
    out = _insider_transactions("MU", "2026-01-17", "2026-07-16")
    assert len(calls) == 1
    assert [t["transactionDate"] for t in out["data"]] == ["2026-06-01", "2026-05-01"]


def test_capped_window_splits_and_recovers_hidden_rows(monkeypatch):
    """Full window returns exactly the cap (truncation signature); halves
    return distinct row sets — the merged result must contain BOTH."""
    def fake_fh(path, params):
        frm, to = params["from"], params["to"]
        span_months = (frm, to)
        if frm == "2026-01-17" and to == "2026-07-16":
            # capped response: only early rows survive the truncation
            return {"data": [_row(f"early{i}", "2026-02-01") for i in range(_FH_INSIDER_CAP)]}
        if frm == "2026-01-17":
            return {"data": [_row(f"early{i}", "2026-02-01") for i in range(40)]}
        return {"data": [_row(f"late{i}", "2026-06-15") for i in range(50)]}

    monkeypatch.setattr(dossier, "_fh", fake_fh)
    out = _insider_transactions("MU", "2026-01-17", "2026-07-16")
    names = {t["name"] for t in out["data"]}
    assert any(n.startswith("late") for n in names), "post-split rows missing"
    assert any(n.startswith("early") for n in names)
    assert len(out["data"]) == 90


def test_boundary_duplicates_are_deduped(monkeypatch):
    dup = _row("SAME PERSON", "2026-04-01")

    def fake_fh(path, params):
        frm, to = params["from"], params["to"]
        if frm == "2026-01-17" and to == "2026-07-16":
            return {"data": [dup] * _FH_INSIDER_CAP}
        return {"data": [dict(dup)]}   # both halves return the same row

    monkeypatch.setattr(dossier, "_fh", fake_fh)
    out = _insider_transactions("MU", "2026-01-17", "2026-07-16")
    assert len(out["data"]) == 1


def test_min_span_and_budget_stop_recursion(monkeypatch):
    calls = []

    def always_capped(path, params):
        calls.append((params["from"], params["to"]))
        return {"data": [_row(f"r{len(calls)}-{i}", params["from"]) for i in range(_FH_INSIDER_CAP)]}

    monkeypatch.setattr(dossier, "_fh", always_capped)
    out = _insider_transactions("MU", "2026-01-17", "2026-07-16")
    assert len(calls) <= 16                      # request budget honored
    assert len(out["data"]) > _FH_INSIDER_CAP    # still recovered more than one page


def test_shape_matches_process_insider_transactions(monkeypatch):
    monkeypatch.setattr(dossier, "_fh",
                        lambda p, q: {"data": [_row("A", "2026-06-01", "P", 50, 20.0)]})
    out = _insider_transactions("MU", "2026-01-17", "2026-07-16")
    summary = dossier.process_insider_transactions(out["data"])
    assert summary["buy_count"] == 1
    assert summary["total_buy_usd"] == 1000
