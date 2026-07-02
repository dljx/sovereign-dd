"""Tests for signal-outcome capture — the path from output FILES to Supabase rows.

REGRESSION CONTEXT (2026-07-03): price/confirmed/sector were NULL for every
scout_history row 06-12→07-03 because fields were stamped on scout.py's
in-memory discovery dicts, while the Supabase rows are actually built by
upload_kv from the output files via _scout_card / the gems collector. These
tests therefore exercise the REAL production shapes: a synthetic output file on
disk → collector → card → history row. If the card builder ever stops reading
the dossier, these fail.
"""

import json

import upload_kv
from upload_kv import (
    _factor_stamp,
    _gems_history_row,
    _scout_card,
    _scout_history_row,
    collect_gems_results,
    collect_scout_results,
    collect_watchlist_results,
)


def _output_file(ticker="AAA", score=7.6, confirmed=True, verdict="CONFIRM", price=123.45):
    """A synthetic scout/gems output file — the {result, dossier, meta} shape
    scout.py/gems.py actually write to disk."""
    return {
        "result": {
            "ticker":           ticker,
            "consensus_score":  score,
            "consensus_grade":  "STRONG BUY",
            "confidence":       "HIGH",
            "majority_thesis":  "x" * 400,
            "catalyst":         "z" * 400,
            "fair_value_composite": 150.0,
            "confirmed":        confirmed,
            "verification":     {"verdict": verdict, "verification_score": 9.1,
                                 "strongest_bear_point": "bear case"},
            "risk_reward":      {"rr_ratio": 2.5, "risk_tier": "LOW"},
        },
        "dossier": {
            "profile":    {"sector": "Technology"},
            "quote":      {"price": price},
            "technicals": {"mom_12_1": 0.31, "mom_6m": 0.12, "mom_1m": -0.02},
            "financials": {"ratios_ttm": {
                "roic": 18.2, "gross_margin": 62.0, "fcf_yield": 0.041,
                "debt_equity": 40.0, "eps_revision_momentum": 0.05,
            }},
        },
        "meta": {"path": "A", "matched_filters": ["f1", "f2"]},
    }


def _write(dirpath, name, payload):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / name).write_text(json.dumps(payload), encoding="utf-8")


# ── the card builder reads the dossier (THE 06-26 regression) ─────────────────

def test_scout_card_carries_dossier_fields():
    f = _output_file()
    card = _scout_card("AAA", f["result"], f["meta"], f["dossier"])
    assert card["price"] == 123.45
    assert card["sector"] == "Technology"
    assert card["confirmed"] is True
    assert card["factors"]["v"] == 2
    assert card["factors"]["mom_12_1"] == 0.31
    assert card["factors"]["roic"] == 18.2
    assert card["factors"]["quality"] is not None
    assert 0.0 <= card["factors"]["quality"] <= 10.0


def test_factor_stamp_none_on_missing_dossier():
    assert _factor_stamp(None) is None
    stamp = _factor_stamp({})  # dossier present but empty → stamped Nones, not a crash
    assert stamp is None or stamp.get("mom_12_1") is None


# ── file → collector → card (production path) ─────────────────────────────────

def test_collect_scout_results_stamps_price_and_factors(tmp_path):
    scout_dir = tmp_path / "scouts"
    _write(scout_dir, "AAA_20260703_120000.json", _output_file())
    cards = collect_scout_results(scout_dir)
    assert len(cards) == 1
    c = cards[0]
    assert c["price"] == 123.45 and c["confirmed"] is True
    assert c["factors"]["mom_12_1"] == 0.31


def test_collect_gems_results_stamps_price_and_factors(tmp_path):
    gems_dir = tmp_path / "gems"
    _write(gems_dir, "BBB_20260703_120000.json", _output_file(ticker="BBB", price=50.0))
    cards = collect_gems_results(gems_dir)
    assert len(cards) == 1
    c = cards[0]
    assert c["price"] == 50.0 and c["confirmed"] is True
    assert c["factors"]["quality"] is not None
    assert c["fair_value_composite"] == 150.0


def test_watchlist_rejects_are_tagged_by_source(tmp_path):
    _write(tmp_path / "scouts", "CCC_20260703_120000.json",
           _output_file(ticker="CCC", confirmed=False, verdict="VETO"))
    _write(tmp_path / "gems", "DDD_20260703_120000.json",
           _output_file(ticker="DDD", confirmed=False, verdict="DOWNGRADE"))
    out = collect_watchlist_results(tmp_path)
    by_ticker = {c["ticker"]: c for c in out}
    assert by_ticker["CCC"]["src"] == "scout"
    assert by_ticker["DDD"]["src"] == "gems"
    # rejects still carry the measurement fields
    assert by_ticker["CCC"]["price"] == 123.45
    assert by_ticker["CCC"]["confirmed"] is False


# ── card → Supabase history row ───────────────────────────────────────────────

def test_scout_row_from_real_card_shape():
    f = _output_file()
    card = _scout_card("AAA", f["result"], f["meta"], f["dossier"])
    row = _scout_history_row(card)
    assert row["ticker"] == "AAA"
    assert row["price"] == 123.45
    assert row["confirmed"] is True
    assert row["verdict"] == "CONFIRM"
    assert row["sector"] == "Technology"
    assert row["factors"]["mom_12_1"] == 0.31
    assert len(row["thesis"]) <= 300
    assert "discovered_at" in row


def test_gems_row_from_reject_card_shape():
    f = _output_file(ticker="DDD", confirmed=False, verdict="DOWNGRADE")
    card = _scout_card("DDD", f["result"], f["meta"], f["dossier"])
    card["verification"] = f["result"]["verification"]  # watchlist override
    row = _gems_history_row(card)
    assert row["confirmed"] is False
    assert row["verdict"] == "DOWNGRADE"
    assert row["price"] == 123.45
    assert row["fair_value"] == 150.0
    assert row["factors"]["quality"] is not None


def test_missing_fields_degrade_to_none_not_crash():
    """A card built without a dossier (old files, degraded runs) must still make
    a valid row — NULLs in Supabase, never a KeyError."""
    card = _scout_card("EEE", {"consensus_score": 7.1, "consensus_grade": "BUY"}, {}, None)
    row = _scout_history_row(card)
    assert row["price"] is None
    assert row["sector"] is None
    assert row["factors"] is None
    assert row["verdict"] is None

    gems = _gems_history_row({"ticker": "FFF"})
    assert gems["price"] is None
    assert gems["factors"] is None
