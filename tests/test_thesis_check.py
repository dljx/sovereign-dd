"""thesis_check — the anti-thesis-drift pass (2026-07-11).

Locks: parse/degrade behavior of the LLM check (UNKNOWN never blocks a run),
seed-vs-check routing, transition-only alerting, and the push payload shape.
All synthetic — no network, no LLM.
"""

import asyncio
import json

import notify
import thesis_check


def _run(coro):
    return asyncio.run(coro)


_REG = {"thesis": "AWS margin expansion drives the FCF inflection",
        "key_swing": "AWS growth re-acceleration", "set_at": "2026-07-01"}
_RESULT = {"consensus_score": 7.8, "consensus_grade": "BUY",
           "majority_thesis": "today's view", "key_swing_factor": "swing",
           "verification": {"strongest_bear_point": "capex cycle risk"}}
_DOSSIER = {"quote": {"price": 226.0, "change_pct": -1.2},
            "technicals": {"pct_from_52w_high": -8.3, "above_sma200": True,
                           "rsi_14": 55.0}}


def _fake_llm(payload):
    async def fake(system, user, **kw):
        fake.calls.append((system, user, kw))
        return json.dumps(payload) if isinstance(payload, dict) else payload
    fake.calls = []
    return fake


def test_check_thesis_parses_valid_verdict(monkeypatch):
    fake = _fake_llm({"status": "strained", "adherence": 4.6,
                      "reason": "margin story reversed two quarters running"})
    monkeypatch.setattr(thesis_check, "call_gemini_async", fake)
    out = _run(thesis_check.check_thesis("AMZN", _REG, _RESULT, _DOSSIER))
    assert out == {"status": "STRAINED", "adherence": 4.6,
                   "reason": "margin story reversed two quarters running"}
    system, user, kw = fake.calls[0]
    assert kw["model"] == thesis_check.CHECK_MODEL      # approved model only
    sent = json.loads(user)
    assert sent["registered_thesis"].startswith("AWS margin")
    assert sent["today"]["todays_debate_score"] == 7.8
    assert "valuation" in system.lower()                # price-run ≠ break framing


def test_check_thesis_clamps_adherence(monkeypatch):
    monkeypatch.setattr(thesis_check, "call_gemini_async",
                        _fake_llm({"status": "INTACT", "adherence": 14, "reason": "r"}))
    out = _run(thesis_check.check_thesis("T", _REG, _RESULT, _DOSSIER))
    assert out["adherence"] == 10.0


def test_check_thesis_degrades_to_unknown(monkeypatch):
    for bad in ({"status": "MAYBE", "adherence": 5},         # bad enum
                "not json at all",                            # unparseable
                None):                                        # call blew up
        if bad is None:
            async def fake(*a, **k):
                raise RuntimeError("api down")
            monkeypatch.setattr(thesis_check, "call_gemini_async", fake)
        else:
            monkeypatch.setattr(thesis_check, "call_gemini_async", _fake_llm(bad))
        out = _run(thesis_check.check_thesis("T", _REG, _RESULT, _DOSSIER))
        assert out["status"] == "UNKNOWN" and out["adherence"] is None


def test_seed_check_never_calls_llm():
    out = thesis_check.seed_check(_RESULT)
    assert out["status"] == "INTACT" and "registered this run" in out["reason"]


def test_build_checks_payload_shape():
    result = {**_RESULT, "thesis_check": {"status": "BROKEN", "adherence": 2.0,
                                          "reason": "moat cracked"}}
    checks = thesis_check.build_checks({"AMZN": (result, _DOSSIER),
                                        "SKIP": (dict(_RESULT), _DOSSIER)})
    assert set(checks) == {"AMZN"}                       # no thesis_check → skipped
    c = checks["AMZN"]
    assert c["status"] == "BROKEN" and c["seed_thesis"] == "today's view"
    assert c["seed_key_swing"] == "swing" and c["checked_at"]


def test_broken_transitions_fire_once():
    registry = {"AMZN": {"status": "INTACT", "thesis": "t1"},
                "MU":   {"status": "BROKEN", "thesis": "t2"},
                "NEW":  {}}
    checks = {"AMZN": {"status": "BROKEN", "adherence": 2, "reason": "r"},
              "MU":   {"status": "BROKEN", "adherence": 1, "reason": "still"},
              "NEW":  {"status": "BROKEN", "adherence": 3, "reason": "r2"},
              "GOOG": {"status": "INTACT", "adherence": 9, "reason": ""}}
    broken = thesis_check.broken_transitions(registry, checks)
    tickers = {b["ticker"] for b in broken}
    assert tickers == {"AMZN", "NEW"}                    # MU already broken → silent
    amzn = next(b for b in broken if b["ticker"] == "AMZN")
    assert amzn["thesis"] == "t1"


def test_fetch_registry_missing_env_is_none(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_EYE_URL", raising=False)
    monkeypatch.delenv("DD_UPLOAD_SECRET", raising=False)
    assert thesis_check.fetch_registry() is None


def test_push_checks_payload(monkeypatch):
    sent = {}

    class _Resp:
        ok = True
        def json(self):
            return {"seeded": ["A"], "updated": [], "removed": []}

    monkeypatch.setenv("SOVEREIGN_EYE_URL", "https://eye.example")
    monkeypatch.setenv("DD_UPLOAD_SECRET", "s3cret")
    monkeypatch.setattr(thesis_check.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        sent.update(url=url, body=json) or _Resp())
    checks = {"A": {"status": "INTACT", "adherence": 8, "reason": "",
                    "checked_at": "t", "seed_thesis": "x", "seed_key_swing": ""}}
    assert thesis_check.push_checks(checks, held=["A", "B"]) is not None
    assert sent["url"].endswith("/api/dd/thesis")
    assert sent["body"] == {"checks": checks, "held": ["A", "B"]}


def test_alert_thesis_break_formats_and_routes(monkeypatch):
    got = []
    monkeypatch.setattr(notify, "_split_send",
                        lambda msg, topic="": got.append((msg, topic)) or True)
    assert notify.alert_thesis_break([{"ticker": "AMZN", "adherence": 2.0,
                                       "reason": "moat cracked",
                                       "thesis": "AWS margin expansion"}]) is True
    msg, topic = got[0]
    assert topic == notify.TOPIC_TRADE_ALERTS
    assert "THESIS BROKEN" in msg and "AMZN" in msg and "moat cracked" in msg
    assert "does not sell" in msg
    assert notify.alert_thesis_break([]) is False        # empty → silent
