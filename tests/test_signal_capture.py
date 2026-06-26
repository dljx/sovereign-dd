"""Tests for signal-outcome capture — the Supabase scout/gems history row builders.

Guards that every discovery signal records its entry `price` plus the BUY-gate
outcome (`confirmed`/`verdict`), so a signal's forward return becomes computable.
The price is the irreplaceable field: if it's ever dropped, outcome measurement
silently breaks and the data is unrecoverable after the fact.
"""

import upload_kv


def _scout_discovery(**over):
    d = {
        "ticker":           "AAA",
        "score":            7.6,
        "grade":            "STRONG BUY",
        "sector":           "Technology",
        "path":             "momentum",
        "matched_filters":  ["f1", "f2"],
        "thesis":           "x" * 400,           # over the 300 cap
        "price":            123.45,
        "confirmed":        True,
        "verification":     {"verdict": "CONFIRM", "verification_score": 9.1},
    }
    d.update(over)
    return d


def _gems_discovery(**over):
    d = {
        "ticker":               "BBB",
        "score":                7.2,
        "grade":                "BUY",
        "thesis":               "y" * 400,
        "catalyst":             "z" * 400,
        "fair_value_composite": 88.0,
        "price":                50.0,
        "confirmed":            False,
        "verification":         {"verdict": "DOWNGRADE", "verification_score": 6.2},
    }
    d.update(over)
    return d


def test_scout_row_captures_price_and_gate_outcome():
    row = upload_kv._scout_history_row(_scout_discovery())
    assert row["ticker"] == "AAA"
    assert row["price"] == 123.45
    assert row["confirmed"] is True
    assert row["verdict"] == "CONFIRM"          # pulled from nested verification
    assert len(row["thesis"]) == 300            # still capped
    assert "discovered_at" in row


def test_gems_row_captures_price_and_gate_outcome():
    row = upload_kv._gems_history_row(_gems_discovery())
    assert row["ticker"] == "BBB"
    assert row["price"] == 50.0
    assert row["confirmed"] is False
    assert row["verdict"] == "DOWNGRADE"
    assert row["fair_value"] == 88.0
    assert len(row["catalyst"]) == 300


def test_missing_fields_degrade_to_none_not_crash():
    """A discovery lacking price/verification must still build a valid row
    (NULLs in Supabase) rather than raising — robustness over completeness."""
    scout = upload_kv._scout_history_row(
        {"ticker": "CCC", "score": 7.1, "grade": "BUY"}
    )
    assert scout["price"] is None
    assert scout["confirmed"] is None
    assert scout["verdict"] is None             # no verification dict → None, not KeyError

    gems = upload_kv._gems_history_row({"ticker": "DDD"})
    assert gems["price"] is None
    assert gems["verdict"] is None
