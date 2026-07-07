"""cache.cached — what gets stored vs retried (2026-07-07 audit fix).

An empty LIST is a successful "nothing here" answer (no news, no insider
transactions) and must be cached; an empty DICT is ambiguous (the fetchers
return {} on failure) and must stay uncached so the next run retries.
"""

import cache


def _capture_set(monkeypatch):
    stored = []
    monkeypatch.setattr(cache, "_sb_get", lambda key, ttl: None)   # force miss
    monkeypatch.setattr(cache, "_sb_set", lambda key, payload: stored.append((key, payload)))
    return stored


def test_truthy_result_is_cached(monkeypatch):
    stored = _capture_set(monkeypatch)
    out = cache.cached("k", 1, lambda: {"a": 1})
    assert out == {"a": 1} and stored == [("k", {"a": 1})]


def test_empty_list_is_cached(monkeypatch):
    """A ticker with zero news used to refetch the live API on every run forever."""
    stored = _capture_set(monkeypatch)
    out = cache.cached("k", 1, lambda: [])
    assert out == [] and stored == [("k", [])]


def test_empty_dict_is_not_cached(monkeypatch):
    """{} is the fetchers' failure shape — caching it would pin an outage for the TTL."""
    stored = _capture_set(monkeypatch)
    out = cache.cached("k", 1, lambda: {})
    assert out == {} and stored == []


def test_none_is_not_cached(monkeypatch):
    stored = _capture_set(monkeypatch)
    assert cache.cached("k", 1, lambda: None) is None
    assert stored == []


def test_hit_short_circuits_fetch(monkeypatch):
    monkeypatch.setattr(cache, "_sb_get", lambda key, ttl: [])   # cached empty list
    called = []
    out = cache.cached("k", 1, lambda: called.append(1) or {"fresh": True})
    assert out == [] and not called
