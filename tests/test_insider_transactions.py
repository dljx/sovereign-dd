"""dossier.process_insider_transactions (2026-07-13) — Finnhub's real response
field is "transactionCode" (single-letter SEC Form 4 codes: P/S/A/G/F/M/J...),
not "transactionType". The old code checked transactionType == "P - Purchase"
/ "S - Sale", a field verified live to appear on ZERO of 1,511 real
transactions across AAPL/MRVL/NVDA — buys/sells were PERMANENTLY EMPTY, so
buy_count/sell_count/cluster_buying/net_insider_usd always read 0 regardless
of real activity. Fixtures below use the real field shapes captured live."""

from dossier import process_insider_transactions, _has_insider_cluster, _insider_tx_value


def _tx(code, change, share, price, date, name="Insider One"):
    return {"transactionCode": code, "change": change, "share": share,
            "transactionPrice": price, "transactionDate": date, "name": name}


# ── The core bug: transactionCode vs the old (wrong) transactionType ───────

def test_finnhub_real_shape_has_no_transactionType_field():
    # Exactly what Finnhub returns live (captured 2026-07-13) — no
    # "transactionType" key at all, only "transactionCode".
    real_row = {"name": "LEVINSON ARTHUR D", "share": 3699576, "change": -65000,
                "filingDate": "2026-05-29", "transactionDate": "2026-05-27",
                "transactionCode": "G", "transactionPrice": 0, "symbol": "AAPL"}
    assert "transactionType" not in real_row
    assert real_row["transactionCode"] == "G"


def test_purchases_and_sales_classified_by_code_not_absent_type_field():
    txns = [
        _tx("P", 3400, 73392, 78.03, "2026-01-05"),   # open-market buy
        _tx("S", -10000, 227754, 281.92, "2026-01-06"),  # open-market sell
        _tx("G", -65000, 3699576, 0, "2026-01-07"),   # gift — neither
        _tx("A", 5000, 100000, 0, "2026-01-08"),       # grant — neither
        _tx("M", 2000, 50000, 0, "2026-01-09"),         # exercise — neither
    ]
    out = process_insider_transactions(txns)
    assert out["buy_count"] == 1
    assert out["sell_count"] == 1
    assert out["recent"] == txns[:10]  # unfiltered raw feed unaffected


def test_regression_old_field_name_would_have_classified_nothing():
    # Sanity-lock the exact regression: filtering on the WRONG (never-present)
    # field must be provably different from the fix, on the same data.
    txns = [_tx("P", 3400, 73392, 78.03, "2026-01-05"),
            _tx("S", -10000, 227754, 281.92, "2026-01-06")]
    wrong_buys  = [t for t in txns if t.get("transactionType") == "P - Purchase"]
    wrong_sells = [t for t in txns if t.get("transactionType") == "S - Sale"]
    assert wrong_buys == [] and wrong_sells == []  # the bug, reproduced
    out = process_insider_transactions(txns)
    assert out["buy_count"] == 1 and out["sell_count"] == 1  # the fix


# ── net_shares: change (transaction delta), not share (cumulative holding) ─

def test_net_shares_uses_change_not_cumulative_share_count():
    # If net_shares wrongly summed "share" (cumulative holdings), this would
    # net to roughly +73392-227754 = -154362 (a huge, meaningless number
    # dominated by whichever insider happens to hold the most stock).
    txns = [_tx("P", 3400, 73392, 78.03, "2026-01-05"),
            _tx("S", -10000, 227754, 281.92, "2026-01-06")]
    out = process_insider_transactions(txns)
    assert out["net_shares"] == 3400 - 10000  # -6600, from `change`, not `share`


def test_net_shares_positive_when_more_buying():
    txns = [_tx("P", 5000, 1, 1, "2026-01-01"), _tx("P", 5000, 1, 1, "2026-01-02"),
            _tx("S", -1000, 1, 1, "2026-01-03")]
    out = process_insider_transactions(txns)
    assert out["net_shares"] == 9000


# ── $ value, significant transactions, buyer roles ──────────────────────────

def test_significant_transaction_threshold():
    txns = [_tx("P", 2000, 1, 78.03, "2026-01-01"),   # 2000*78.03 = $156,060 — significant
            _tx("P", 100, 1, 50.0, "2026-01-02")]      # 100*50 = $5,000 — not
    out = process_insider_transactions(txns)
    assert out["significant_buys"] == 1
    assert out["total_buy_usd"] == round(2000 * 78.03 + 100 * 50.0)


def test_buyer_roles_deduped_and_capped_at_5():
    txns = [_tx("P", 100, 1, 10, f"2026-01-{i:02d}", name=f"Person {i % 3}") for i in range(1, 10)]
    out = process_insider_transactions(txns)
    assert len(out["buyer_roles"]) <= 5
    assert set(out["buyer_roles"]) <= {"Person 0", "Person 1", "Person 2"}


def test_empty_and_none_input_never_crashes():
    assert process_insider_transactions([]) == process_insider_transactions(None)
    out = process_insider_transactions(None)
    assert out["buy_count"] == 0 and out["sell_count"] == 0 and out["net_shares"] == 0
    assert out["cluster_buying"] is False


# ── _has_insider_cluster ─────────────────────────────────────────────────────

def test_cluster_detected_within_window():
    txns = [_tx("P", 100, 1, 1, d) for d in ("2026-01-01", "2026-01-05", "2026-01-10")]
    assert _has_insider_cluster(txns, window_days=14) is True


def test_cluster_not_detected_when_spread_out():
    txns = [_tx("P", 100, 1, 1, d) for d in ("2026-01-01", "2026-02-01", "2026-03-01")]
    assert _has_insider_cluster(txns, window_days=14) is False


def test_cluster_needs_at_least_three():
    txns = [_tx("P", 100, 1, 1, d) for d in ("2026-01-01", "2026-01-02")]
    assert _has_insider_cluster(txns) is False


# ── _insider_tx_value ────────────────────────────────────────────────────────

def test_tx_value_prefers_change_over_share():
    t = {"change": 100, "share": 999999, "transactionPrice": 10}
    assert _insider_tx_value(t) == 1000  # 100*10, not 999999*10


def test_tx_value_falls_back_to_share_when_change_absent():
    t = {"share": 500, "transactionPrice": 10}
    assert _insider_tx_value(t) == 5000
