"""cache._sb_set must not silently drop writes.

Before this, _sb_set wrapped its Supabase POST in a bare ``except: pass``. A
payload carrying a NaN/Inf float fails requests' strict JSON encoder ("Out of
range float values are not JSON compliant" — the same encoder that failed the
scout run on 2026-07-25), so the write was dropped SILENTLY: permanent cache
miss, re-fetch every run, wasted quota against tight API budgets, and no
symptom anywhere. Cache writes stay best-effort (never fatal) but must be
sanitized, and when they do fail they must be visible.
"""

import json

import cache


# ── _json_safe: non-finite floats become null, everything else survives ───

def test_non_finite_scalars_become_none():
    assert cache._json_safe(float("nan")) is None
    assert cache._json_safe(float("inf")) is None
    assert cache._json_safe(float("-inf")) is None


def test_finite_values_are_preserved_exactly():
    assert cache._json_safe(0.031) == 0.031
    assert cache._json_safe(0) == 0
    assert cache._json_safe("text") == "text"
    assert cache._json_safe(None) is None
    assert cache._json_safe(True) is True


def test_nested_structures_are_sanitized():
    out = cache._json_safe({
        "a": float("nan"),
        "b": [1.0, float("inf"), {"c": float("-inf"), "d": 2.5}],
        "e": ("x", float("nan")),
    })
    assert out["a"] is None
    assert out["b"][0] == 1.0 and out["b"][1] is None
    assert out["b"][2]["c"] is None and out["b"][2]["d"] == 2.5
    assert list(out["e"]) == ["x", None]


def test_result_is_strictly_json_encodable():
    payload = {"p": float("nan"), "q": [float("inf"), 1.5]}
    json.dumps(cache._json_safe(payload), allow_nan=False)  # must not raise


def test_deeply_nested_payload_does_not_blow_the_stack():
    deep = cur = {}
    for _ in range(2000):
        cur["n"] = {}
        cur = cur["n"]
    cur["v"] = float("nan")
    cache._json_safe(deep)  # must not raise RecursionError


# ── _sb_set: sanitizes, never raises, and failures become visible ─────────

class _Resp:
    def __init__(self, status=201):
        self.status_code = status
        self.ok = status < 400
        self.text = f"status {status}"


def _enable(monkeypatch):
    monkeypatch.setattr(cache, "_ENABLED", True)
    monkeypatch.setattr(cache, "_URL", "https://sb.test")
    monkeypatch.setattr(cache, "_sb_write_error_logged", False)


def test_nan_payload_is_sanitized_before_post(monkeypatch):
    _enable(monkeypatch)
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen["body"] = json
        return _Resp(201)

    monkeypatch.setattr(cache.requests, "post", post)
    cache._sb_set("k", {"a": float("nan"), "b": 1.25})

    body = seen["body"]
    assert body["payload"]["a"] is None and body["payload"]["b"] == 1.25
    json.dumps(body, allow_nan=False)  # mirrors requests' strict encoder


def test_network_error_is_swallowed_but_logged_once(monkeypatch, capsys):
    _enable(monkeypatch)

    def post(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr(cache.requests, "post", post)
    cache._sb_set("k", {"a": 1})   # must not raise
    cache._sb_set("k", {"a": 1})
    out = capsys.readouterr().out
    assert "cache" in out.lower()
    # logged once, not once per call — a broken cache must not flood CI logs
    assert out.lower().count("connectionerror") == 1


def test_http_error_is_reported(monkeypatch, capsys):
    _enable(monkeypatch)
    monkeypatch.setattr(cache.requests, "post", lambda *a, **k: _Resp(500))
    cache._sb_set("k", {"a": 1})
    assert "500" in capsys.readouterr().out


def test_disabled_cache_is_a_silent_noop(monkeypatch):
    monkeypatch.setattr(cache, "_ENABLED", False)
    called = []
    monkeypatch.setattr(cache.requests, "post", lambda *a, **k: called.append(1))
    cache._sb_set("k", {"a": float("nan")})
    assert not called


def test_unserializable_payload_does_not_raise(monkeypatch):
    """An object json cannot encode must fail visibly, not explode the run."""
    _enable(monkeypatch)

    def post(url, json=None, headers=None, timeout=None):
        json_mod = __import__("json")
        json_mod.dumps(json, allow_nan=False)   # requests would raise here
        return _Resp(201)

    monkeypatch.setattr(cache.requests, "post", post)
    cache._sb_set("k", {"obj": object()})   # must not raise
