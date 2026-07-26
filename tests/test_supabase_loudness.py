"""_supabase_insert retry + loud-failure contract.

The history tables are the outcome-measurement dataset — a failed insert must
fail the run (so the workflow's Telegram failure alert fires), never vanish
into a log line.
"""

import pytest

import upload_kv


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Zero backoff and point at a fake Supabase so the HTTP path is exercised."""
    monkeypatch.setattr(upload_kv, "_SB_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(upload_kv, "SUPABASE_URL", "https://sb.test")
    monkeypatch.setattr(upload_kv, "SUPABASE_KEY", "test-key")


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self.text = f"status {status}"
        self._body = body or {}

    def json(self):
        return self._body


def test_retries_then_success(monkeypatch):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return _Resp(201)

    monkeypatch.setattr(upload_kv.requests, "post", post)
    assert upload_kv._supabase_insert("scout_history", [{"ticker": "TST"}]) is True
    assert len(calls) == 3


def test_persistent_5xx_returns_false_after_all_attempts(monkeypatch):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _Resp(500)

    monkeypatch.setattr(upload_kv.requests, "post", post)
    assert upload_kv._supabase_insert("scout_history", [{"ticker": "TST"}]) is False
    assert len(calls) == upload_kv._SB_ATTEMPTS


def test_unconfigured_is_silent_noop(monkeypatch):
    monkeypatch.setattr(upload_kv, "SUPABASE_URL", "")
    called = []
    monkeypatch.setattr(upload_kv.requests, "post", lambda *a, **k: called.append(1))
    assert upload_kv._supabase_insert("scout_history", [{"ticker": "TST"}]) is True
    assert not called


def test_empty_rows_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(upload_kv.requests, "post", lambda *a, **k: called.append(1))
    assert upload_kv._supabase_insert("scout_history", []) is True
    assert not called


def test_non_finite_floats_are_sanitized_before_post(monkeypatch):
    """A NaN/Inf in a row (e.g. a scout factor stamp sourced from a yfinance
    frame) must be coerced to null at the insert boundary — the strict JSON
    encoder requests uses rejects non-finite floats with "Out of range float
    values are not JSON compliant", which failed the run 2026-07-25→26.
    scout_history/gems_history rows are NOT pre-sanitized at their build site
    the way dd_history rows are, so the guard has to live here.
    """
    import json as _json
    captured = {}

    def post(url, headers=None, json=None, timeout=None):
        captured["rows"] = json
        # Mirror production: requests serializes with the strict encoder.
        _json.dumps(json, allow_nan=False)
        return _Resp(201)

    monkeypatch.setattr(upload_kv.requests, "post", post)

    rows = [{
        "ticker":  "TST",
        "score":   float("nan"),
        "price":   float("inf"),
        "factors": {"roic": float("-inf"), "mom_12_1": float("nan"), "fcf_yield": 0.031},
        "filters": ["ok", float("nan")],
    }]
    assert upload_kv._supabase_insert("scout_history", rows) is True

    sent = captured["rows"][0]
    assert sent["score"] is None
    assert sent["price"] is None
    assert sent["factors"]["roic"] is None
    assert sent["factors"]["mom_12_1"] is None
    assert sent["factors"]["fcf_yield"] == 0.031  # finite values untouched
    assert sent["filters"] == ["ok", None]


# ── main() exit contract ─────────────────────────────────────────────


def _wire_main(monkeypatch, tmp_path, supabase_status: int):
    """Stub collectors + HTTP so main() reaches the Supabase section with one
    scout row; KV upload always succeeds, Supabase returns `supabase_status`."""
    monkeypatch.chdir(tmp_path)  # Path("output") doesn't exist → no history reads
    monkeypatch.setattr(upload_kv, "UPLOAD_SECRET", "test-secret")
    monkeypatch.setattr(upload_kv, "collect_portfolio_results",
                        lambda d: ([], {}, [], []))
    monkeypatch.setattr(upload_kv, "collect_scout_results",
                        lambda d: [{"ticker": "TST", "score": 7.5}])
    monkeypatch.setattr(upload_kv, "collect_gems_results", lambda d: [])
    monkeypatch.setattr(upload_kv, "collect_watchlist_results", lambda d: [])

    def post(url, headers=None, json=None, timeout=None):
        if "/rest/v1/" in url:
            return _Resp(supabase_status)
        return _Resp(200, {"ok": True, "written": ["dd:scouts"]})

    monkeypatch.setattr(upload_kv.requests, "post", post)


def test_main_exits_nonzero_when_history_insert_fails(monkeypatch, tmp_path):
    _wire_main(monkeypatch, tmp_path, supabase_status=500)
    with pytest.raises(SystemExit) as exc:
        upload_kv.main()
    assert exc.value.code == 1


def test_main_completes_when_history_insert_succeeds(monkeypatch, tmp_path):
    _wire_main(monkeypatch, tmp_path, supabase_status=201)
    upload_kv.main()  # must not raise
