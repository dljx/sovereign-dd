"""fire_check — the quarterly FIRE-assumption reviewer.

All synthetic: settings fetch, LLM, and Telegram are stubbed. Locks the
contract that the checkup NEVER writes settings (it has no write path at all)
and that suggestions route through notify.alert_fire_review.
"""

import fire_check
import notify


def _settings():
    return {"monthlyExpenses": 4000, "swr": 3.5, "inflation": 2.5,
            "expectedReturn": 6.0, "birthYear": 1997, "cpfLifeMonthly": 1500,
            "cpf": {"balance": 80000, "growthRate": 4.0, "includeAsAsset": False}}


def test_prompt_carries_all_stored_assumptions():
    p = fire_check.build_prompt(_settings())
    for frag in ("2.5%", "4.0%", "S$1500/month", "1997", "3.5%", "6.0%"):
        assert frag in p


def test_parse_review_normalizes_and_drops_junk():
    rows = fire_check.parse_review([
        {"parameter": "inflation", "stored": "2.5", "current": "3.1 (2026)",
         "verdict": "review", "note": "core inflation ran higher", "source": "MAS"},
        {"parameter": "swr", "verdict": "OK"},
        {"nonsense": True},
        "not a dict",
    ])
    assert [r["parameter"] for r in rows] == ["inflation", "swr"]
    assert rows[0]["verdict"] == "REVIEW"          # case-normalized
    assert rows[1]["verdict"] == "OK"              # anything non-REVIEW → OK


def test_parse_review_non_list_is_empty():
    assert fire_check.parse_review({"parameter": "x"}) == []
    assert fire_check.parse_review(None) == []


def test_no_settings_is_a_quiet_noop(monkeypatch):
    monkeypatch.setattr(fire_check, "fetch_settings", lambda: None)
    called = []
    monkeypatch.setattr(notify, "alert_fire_review", lambda rows: called.append(rows) or True)
    assert fire_check.run() == 0
    assert not called


def test_run_routes_rows_to_telegram(monkeypatch):
    monkeypatch.setattr(fire_check, "fetch_settings", lambda: _settings())
    import llm
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k:
                        '[{"parameter":"inflation","stored":"2.5","current":"3.2 (2026)",'
                        '"verdict":"REVIEW","note":"higher","source":"SingStat"},'
                        '{"parameter":"swr","stored":"3.5","current":"3.25-4",'
                        '"verdict":"OK","note":"","source":"research"}]')
    sent = []
    monkeypatch.setattr(notify, "alert_fire_review", lambda rows: sent.append(rows) or True)
    assert fire_check.run() == 0
    assert len(sent) == 1 and sent[0][0]["parameter"] == "inflation"


def test_llm_failure_returns_nonzero_and_ops_alerts(monkeypatch):
    monkeypatch.setattr(fire_check, "fetch_settings", lambda: _settings())
    import llm
    def boom(*a, **k):
        raise RuntimeError("grounding unavailable")
    monkeypatch.setattr(llm, "call_gemini", boom)
    ops = []
    monkeypatch.setattr(notify, "alert_ops", lambda msg: ops.append(msg) or True)
    assert fire_check.run() == 1
    assert ops and "FIRE assumption check FAILED" in ops[0]


# ── alert_fire_review formatting ────────────────────────────────────────────────

def test_alert_fire_review_flags_lead_and_ok_summarized(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    notify.alert_fire_review([
        {"parameter": "inflation", "stored": "2.5", "current": "3.2 (2026)",
         "verdict": "REVIEW", "note": "core CPI ran hot", "source": "SingStat"},
        {"parameter": "swr", "stored": "3.5", "current": "3.25-4", "verdict": "OK",
         "note": "", "source": ""},
    ])
    msg, topic = sent[0]
    assert topic == notify.TOPIC_SCAN_RESULTS
    assert "inflation" in msg and "3.2 (2026)" in msg and "SingStat" in msg
    assert "unchanged: swr" in msg
    assert "nothing is changed automatically" in msg.lower()


def test_alert_fire_review_all_clear(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    notify.alert_fire_review([
        {"parameter": "swr", "stored": "3.5", "current": "3.25-4", "verdict": "OK",
         "note": "", "source": ""},
    ])
    assert "still look current" in sent[0][0]


def test_extract_rows_keeps_full_array_despite_object_first_extract_json():
    """llm.extract_json returns only the FIRST object of an array — fire_check
    must keep all rows (this bug would have silently dropped 4 of 5 checks)."""
    text = ('```json\n[{"parameter":"a","verdict":"OK"},{"parameter":"b","verdict":"REVIEW"},'
            '{"parameter":"c","verdict":"OK"}]\n```')
    rows = fire_check._extract_rows(text)
    assert len(rows) == 3


def test_extract_rows_array_with_preamble():
    text = 'Here are the results:\n[{"parameter":"a","verdict":"OK"}] hope that helps'
    assert len(fire_check._extract_rows(text)) == 1


def test_extract_rows_lone_object_wrapped():
    assert fire_check._extract_rows('{"parameter":"a","verdict":"OK"}') == [
        {"parameter": "a", "verdict": "OK"}]
