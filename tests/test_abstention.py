"""The empty scan must still send a heartbeat, not go silent — abstention is
the system's most common correct output (see docs/METHODOLOGY_REVIEW.md §1).

Changed 2026-07-07 (Daryl's request): the message no longer repeats "invest in
VWRA" — that's his own baseline, not advice this system needs to restate every
time it has no pick. Silence on a pick means no pick, nothing more."""

import notify


def _capture(monkeypatch):
    sent = {}

    def fake_split_send(msg, topic_id=""):
        sent["msg"] = msg
        sent["topic"] = topic_id
        return True

    monkeypatch.setattr(notify, "_split_send", fake_split_send)
    return sent


def test_empty_scan_still_sends_a_heartbeat(monkeypatch):
    sent = _capture(monkeypatch)
    assert notify.alert_scout_summary([]) is True
    assert "SOVEREIGN SCOUT" in sent["msg"]
    assert "VWRA" not in sent["msg"]


def test_gems_title_flows_through(monkeypatch):
    sent = _capture(monkeypatch)
    notify.alert_scout_summary([], title="SOVEREIGN GEMS")
    assert "SOVEREIGN GEMS" in sent["msg"]
    assert "VWRA" not in sent["msg"]


def test_signals_present_no_abstention_text(monkeypatch):
    sent = _capture(monkeypatch)
    notify.alert_scout_summary([{"ticker": "AAA", "score": 7.5, "grade": "BUY"}])
    assert "AAA" in sent["msg"]
    assert "VWRA" not in sent["msg"]
