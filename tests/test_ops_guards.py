"""Operational guardrails (2026-07-07 audit batch B).

Covers: ledger corruption fails loud instead of silently re-alerting the world,
verifier-health alerting thresholds, and model-ID resolution that neither burns
generation quota nor cross-contaminates between models.
"""

import json
from types import SimpleNamespace

import pytest

import gems
import llm
import notify
import scout
import verify


# ── _load_ledger: corrupt-but-present must raise, missing is a legit first run ─

def test_ledger_missing_file_is_empty(tmp_path):
    assert scout._load_ledger(tmp_path / "nope.json", "test") == {}


def test_ledger_corrupt_json_raises(tmp_path):
    p = tmp_path / "scout_history.json"
    p.write_text("{truncated-by-ci-cache", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        scout._load_ledger(p, "scout history")


def test_ledger_wrong_shape_raises(tmp_path):
    p = tmp_path / "scout_notified.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected dict"):
        scout._load_ledger(p, "scout notify")


def test_ledger_valid_dict_loads(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"AAA": {"ts": 1}}), encoding="utf-8")
    assert scout._load_ledger(p, "test") == {"AAA": {"ts": 1}}


def test_all_loaders_route_through_ledger(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    for mod, attr, fn in [
        (scout, "SCOUT_HISTORY_FILE", scout._load_history),
        (scout, "SCOUT_NOTIFIED_FILE", scout._load_notified),
        (scout, "SCOUT_SEEN_FILE", scout._load_seen),
        (gems, "GEMS_HISTORY_FILE", gems._load_history),
    ]:
        monkeypatch.setattr(mod, attr, bad)
        with pytest.raises(RuntimeError):
            fn()


# ── alert_verifier_health: fires on the pre-07-03 incident shape only ──────────

def _d(ticker, verdict):
    return {"ticker": ticker, "verification": {"verdict": verdict}}


def test_verifier_health_fires_on_majority_unverified(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "alert_ops", lambda msg: sent.append(msg) or True)
    fired = verify.alert_verifier_health(
        [_d("A", "UNVERIFIED"), _d("B", "UNVERIFIED"), _d("C", "CONFIRM")], "scout")
    assert fired is True
    assert "2/3" in sent[0] and "A, B" in sent[0]


def test_verifier_health_quiet_on_single_blip(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "alert_ops", lambda msg: sent.append(msg) or True)
    assert verify.alert_verifier_health(
        [_d("A", "UNVERIFIED"), _d("B", "CONFIRM"), _d("C", "CONFIRM")], "scout") is False
    assert not sent


def test_verifier_health_quiet_on_healthy_run(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "alert_ops", lambda msg: sent.append(msg) or True)
    assert verify.alert_verifier_health(
        [_d("A", "CONFIRM"), _d("B", "DOWNGRADE")], "gems") is False
    assert not sent


def test_verifier_health_quiet_on_empty_run(monkeypatch):
    assert verify.alert_verifier_health([], "scout") is False


# ── _resolve_model: metadata probe, per-(key, model) cache ─────────────────────

def _fake_client(known: set[str]):
    def get(model):
        if model not in known:
            raise ValueError(f"unknown model {model}")
        return SimpleNamespace(name=model)
    return SimpleNamespace(models=SimpleNamespace(get=get))


def test_resolve_model_prefers_bare_id(monkeypatch):
    monkeypatch.setattr(llm, "_model_ids", {})
    c = _fake_client({"gemma-4-31b-it"})
    assert llm._resolve_model(c, "k1", "gemma-4-31b-it") == "gemma-4-31b-it"


def test_resolve_model_falls_back_to_prefixed(monkeypatch):
    monkeypatch.setattr(llm, "_model_ids", {})
    c = _fake_client({"models/gemma-4-31b-it"})
    assert llm._resolve_model(c, "k1", "gemma-4-31b-it") == "models/gemma-4-31b-it"


def test_resolve_model_cache_is_per_model_not_per_key(monkeypatch):
    """The old cache was keyed by API key alone — an A/B eval's second model
    silently resolved to the first model's ID, running both arms on one model."""
    monkeypatch.setattr(llm, "_model_ids", {})
    c = _fake_client({"model-a", "model-b"})
    assert llm._resolve_model(c, "k1", "model-a") == "model-a"
    assert llm._resolve_model(c, "k1", "model-b") == "model-b"   # not "model-a"


def test_resolve_model_failure_not_cached(monkeypatch):
    monkeypatch.setattr(llm, "_model_ids", {})
    c = _fake_client(set())
    assert llm._resolve_model(c, "k1", "model-a") == "model-a"   # default returned
    assert llm._model_ids == {}                                  # nothing poisoned


# ── llm._is_transient_network (2026-07-14) ──────────────────────────────────
# A single "The read operation timed out" on the scout triage call raised
# immediately (only 429/5xx were retryable) and forfeited a whole 4-hour
# discovery window (live 2026-07-14 09:57 run). Timeouts are as transient as
# a 502; quota/auth/safety errors must never match.

from llm import _is_transient_network


def test_transient_network_matches_the_live_failure():
    assert _is_transient_network("the read operation timed out")


def test_transient_network_matches_connection_drops():
    for s in ("connection reset by peer", "connection aborted",
              "remote end closed connection without response",
              "connect timeout", "service temporarily unavailable"):
        assert _is_transient_network(s), s


def test_transient_network_never_matches_quota_or_auth():
    for s in ("429 resource exhausted: quota exceeded",
              "permission denied: api key invalid",
              "blocked by safety settings", "invalid argument"):
        assert not _is_transient_network(s), s
