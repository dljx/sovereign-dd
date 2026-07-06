"""notify._send retry behavior — transient failures must not eat alerts."""

import pytest

import notify


@pytest.fixture(autouse=True)
def _fast_send(monkeypatch):
    """Zero the backoff and fake credentials so _send exercises the HTTP path."""
    monkeypatch.setattr(notify, "_SEND_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(notify, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(notify, "CHAT_ID", "test-chat")


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self.ok = status == 200
        self.text = f"status {status}"
        self._body = body or {}

    def json(self):
        return self._body


def test_transient_exceptions_then_success(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return _Resp(200)

    monkeypatch.setattr(notify.requests, "post", post)
    assert notify._send("hi") is True
    assert len(calls) == 3


def test_5xx_retries_then_success(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(1)
        return _Resp(502) if len(calls) == 1 else _Resp(200)

    monkeypatch.setattr(notify.requests, "post", post)
    assert notify._send("hi") is True
    assert len(calls) == 2


def test_non_retryable_400_fails_once(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(1)
        return _Resp(400)

    monkeypatch.setattr(notify.requests, "post", post)
    assert notify._send("hi") is False
    assert len(calls) == 1


def test_429_honors_retry_after(monkeypatch):
    calls, sleeps = [], []

    def post(url, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(429, {"parameters": {"retry_after": 7}})
        return _Resp(200)

    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(notify.time, "sleep", lambda s: sleeps.append(s))
    assert notify._send("hi") is True
    assert sleeps == [7.0]


def test_429_retry_after_capped(monkeypatch):
    calls, sleeps = [], []

    def post(url, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(429, {"parameters": {"retry_after": 300}})
        return _Resp(200)

    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(notify.time, "sleep", lambda s: sleeps.append(s))
    assert notify._send("hi") is True
    assert sleeps == [notify._RETRY_AFTER_CAP]


def test_exhausted_retries_returns_false(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(1)
        raise TimeoutError("slow")

    monkeypatch.setattr(notify.requests, "post", post)
    assert notify._send("hi") is False
    assert len(calls) == 3


def test_missing_credentials_short_circuits(monkeypatch):
    monkeypatch.setattr(notify, "BOT_TOKEN", "")
    called = []
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: called.append(1))
    assert notify._send("hi") is False
    assert not called


def test_scoreboard_digest_formats_and_routes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    sb = {
        "benchmark": "VWRA.L", "n_signals": 116,
        "windows": [
            {"weeks": 1, "pending": 23,
             "overall": {"n": 93, "hit": 0.5591, "mean": 0.017, "median": 0.006},
             "top": [{"ticker": "PAY", "excess": 0.262}],
             "bottom": [{"ticker": "LEGN", "excess": -0.23}]},
            {"weeks": 4, "pending": 116},
        ],
    }
    assert notify.alert_scoreboard_digest(sb) is True
    msg, topic = sent[0]
    assert topic == notify.TOPIC_SCAN_RESULTS
    assert "hit 56%" in msg and "+1.7%" in msg
    assert "PAY" in msg and "LEGN" in msg
    assert "nothing measurable yet (116 pending)" in msg


# ── alert_buy_signal: v3 DOWNGRADEs surface flagged, not suppressed ────────────

def _buy_signal_fixture(**over):
    d = {
        "ticker": "AAA", "score": 8.4, "grade": "STRONG BUY", "confidence": "HIGH",
        "thesis": "Wide moat compounder.", "key_swing_factor": "Margin expansion.",
        "catalyst": "", "asymmetry_ratio": "", "analyzed_at": "2026-07-07T00:00:00Z",
        "matched_filters": [], "path": "A", "banger": {}, "position_guidance": {},
        "cycle_position": {}, "verification": {},
    }
    d.update(over)
    return d


def test_buy_signal_confirm_header_unflagged(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    d = _buy_signal_fixture(verification={"verdict": "CONFIRM", "verification_score": 8.8})
    assert notify.alert_buy_signal(d) is True
    msg, topic = sent[0]
    assert topic == notify.TOPIC_TRADE_ALERTS
    assert "BUY SIGNAL — AAA" in msg
    assert "FLAGGED" not in msg
    assert "prosecutor CONFIRM" in msg and "8.8/10" in msg


def test_buy_signal_downgrade_is_flagged_not_suppressed(monkeypatch):
    """v3 (2026-07-07): a red-team DOWNGRADE surfaces in Trade Alerts, visibly
    tagged with its score + bear point, instead of being routed to Under Review
    like a VETO."""
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    d = _buy_signal_fixture(verification={
        "verdict": "DOWNGRADE", "verification_score": 5.5,
        "strongest_bear_point": "Margins peaking",
    })
    assert notify.alert_buy_signal(d) is True
    msg, topic = sent[0]
    assert topic == notify.TOPIC_TRADE_ALERTS  # still Trade Alerts, not Watchlist
    assert "FLAGGED BUY — AAA" in msg
    assert "red-team DOWNGRADE" in msg
    assert "5.5/10" in msg and "Margins peaking" in msg


def test_buy_signal_unverified_unchanged(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": sent.append((msg, topic)) or True)
    d = _buy_signal_fixture(verification={"verdict": "UNVERIFIED"})
    notify.alert_buy_signal(d)
    msg, _ = sent[0]
    assert "BUY SIGNAL — AAA" in msg and "FLAGGED" not in msg
    assert "Unverified" in msg
