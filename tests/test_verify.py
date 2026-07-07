"""Tests for the BUY confirmation gate (verify.py).

Covers the Stage-1 deterministic quality gate (truth table), the Stage-2
red-team normalization, and verify_buy orchestration/routing incl. the
cost-saving short-circuit (no LLM call on a Stage-1 reject) and fail-open.
"""

import asyncio
import json

import pytest

import llm
import verify
from verify import quality_gate, verify_buy


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_red_team_backoff(monkeypatch):
    """Neutralize the inter-retry sleep so retry tests never wait on real seconds."""
    monkeypatch.setattr(verify, "_RED_TEAM_BACKOFF", 0.0)


def _good_result(**over):
    """A clean BUY that passes every Stage-1 check."""
    r = {
        "consensus_score": 7.6, "consensus_grade": "STRONG BUY", "confidence": "HIGH",
        "converged": True, "score_spread": 1.2, "failed_agents": [], "data_confidence": "HIGH",
        "risk_reward": {"applied": True, "risk_index": 3.0, "rr_ratio": 2.5, "risk_tier": "LOW",
                        "upside_pct": 40.0, "downside_pct": 16.0,
                        "llm_cross_check": {"divergent": False}},
        "majority_thesis": "Wide moat at a discount.", "key_swing_factor": "Margin expansion.",
        "catalyst": "New product ramp.", "asymmetry_ratio": "3:1",
        "agent_final_scores": {"A": 7.5, "B": 7.7},
    }
    r.update(over)
    return r


_DOSSIER = {
    "profile": {"name": "Test Co", "market_cap_bn": 10.0},
    "quote": {"price": 100.0},
    "financials": {"ratios_ttm": {"pe": 20, "roic": 0.15, "debt_equity": 0.4}},
    "fair_values": {"composite_fair_value": 140.0},
}


def _patch_llm(monkeypatch, *, returns=None, raises=None, track=None):
    async def stub(system, user, **kwargs):
        if track is not None:
            track.append(True)
        if raises is not None:
            raise raises
        return returns
    monkeypatch.setattr(llm, "call_gemini_async", stub)


# ── Stage 1: quality_gate truth table ───────────────────────────────────────────

def test_quality_gate_clean_passes():
    ok, reasons = quality_gate(_good_result())
    assert ok and reasons == []


def test_quality_gate_not_converged_fails():
    ok, reasons = quality_gate(_good_result(converged=False))
    assert not ok and any("converge" in r for r in reasons)


def test_quality_gate_wide_spread_fails(monkeypatch):
    monkeypatch.setenv("VERIFY_MAX_SPREAD", "2.0")
    ok, reasons = quality_gate(_good_result(score_spread=2.5))
    assert not ok and any("spread" in r for r in reasons)


def test_quality_gate_low_confidence_fails():
    ok, reasons = quality_gate(_good_result(confidence="LOW"))
    assert not ok and any("confidence LOW" in r for r in reasons)


def test_quality_gate_failed_agent_fails():
    ok, reasons = quality_gate(_good_result(failed_agents=["ValuationEngine"]))
    assert not ok and any("agent(s) failed" in r for r in reasons)


def test_quality_gate_high_risk_index_fails(monkeypatch):
    monkeypatch.setenv("VERIFY_MAX_RISK_INDEX", "6.0")
    rr = _good_result()["risk_reward"] | {"risk_index": 7.5}
    ok, reasons = quality_gate(_good_result(risk_reward=rr))
    assert not ok and any("risk index" in r for r in reasons)


def test_quality_gate_divergent_rr_passes_but_is_flagged():
    """Since v3 (2026-07-07): a R:R cross-check divergence no longer auto-rejects
    at Stage 1 — it's not adverse evidence, just an estimator disagreement. It
    rides into Stage 2 as a lead instead (see _rr_divergence)."""
    rr = _good_result()["risk_reward"] | {"llm_cross_check": {"divergent": True, "llm_rr": 6.0}}
    ok, reasons = quality_gate(_good_result(risk_reward=rr))
    assert ok and reasons == []


def test_rr_divergence_extracts_flagged_disagreement():
    rr = _good_result()["risk_reward"] | {"llm_cross_check": {"divergent": True, "llm_rr": 6.0}}
    div = verify._rr_divergence(_good_result(risk_reward=rr))
    assert div == {"llm_rr": 6.0, "computed_rr": rr["rr_ratio"]}


def test_rr_divergence_none_when_not_divergent():
    assert verify._rr_divergence(_good_result()) is None
    assert verify._rr_divergence(_good_result(risk_reward={"applied": False})) is None


# ── surfaces_on_board: the shared routing contract (upload_kv + main.py) ────────

def test_surfaces_on_board_true_for_confirmed():
    assert verify.surfaces_on_board({"confirmed": True, "verification": {"verdict": "CONFIRM"}})


def test_surfaces_on_board_true_for_downgrade_despite_not_confirmed():
    assert verify.surfaces_on_board({"confirmed": False, "verification": {"verdict": "DOWNGRADE"}})


def test_surfaces_on_board_false_for_veto_stage1_unverified():
    for verdict in ("VETO", "REJECTED_STAGE1", "UNVERIFIED"):
        assert not verify.surfaces_on_board({"confirmed": False, "verification": {"verdict": verdict}})


def test_surfaces_on_board_defaults_true_when_confirmed_absent():
    # Old/degraded rows without a `confirmed` field default to True (pre-gate shape).
    assert verify.surfaces_on_board({})


def test_quality_gate_low_data_confidence_fails():
    ok, reasons = quality_gate(_good_result(data_confidence="LOW"))
    assert not ok and any("data confidence LOW" in r for r in reasons)


def test_quality_gate_no_rr_still_passes():
    # Missing R:R is not a disqualifier on its own.
    ok, reasons = quality_gate(_good_result(risk_reward={"applied": False}))
    assert ok and reasons == []


# ── Stage 2 prompt: divergence rides in as a lead, not a verdict ────────────────

def test_prompt_includes_divergence_flag_when_present():
    rr = _good_result()["risk_reward"] | {
        "llm_cross_check": {"divergent": True, "llm_rr": 6.0}
    }
    prompt = verify._build_user_prompt("AAA", _good_result(risk_reward=rr), _DOSSIER)
    assert "CROSS-CHECK FLAG" in prompt
    assert "6.0:1" in prompt and f"{rr['rr_ratio']:.1f}:1" in prompt


def test_prompt_omits_divergence_flag_when_absent():
    prompt = verify._build_user_prompt("AAA", _good_result(), _DOSSIER)
    assert "CROSS-CHECK FLAG" not in prompt


# ── verify_buy orchestration / routing ──────────────────────────────────────────

def test_stage1_reject_skips_llm_call(monkeypatch):
    calls = []
    _patch_llm(monkeypatch, raises=AssertionError("LLM must not be called"), track=calls)
    v = _run(verify_buy("AAA", _good_result(converged=False), _DOSSIER))
    assert v["confirmed"] is False
    assert v["stage"] == 1 and v["verdict"] == "REJECTED_STAGE1"
    assert calls == []  # Stage-1 reject must not spend a call


def test_stage2_confirm(monkeypatch):
    _patch_llm(monkeypatch, returns='{"verdict":"CONFIRM","verification_score":8.2,'
                                    '"confirms_buy":true,"strongest_bear_point":"none material",'
                                    '"falsification_findings":[]}')
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is True and v["verdict"] == "CONFIRM"
    assert v["verification_score"] == 8.2


def test_stage2_veto_rejects(monkeypatch):
    _patch_llm(monkeypatch, returns='{"verdict":"VETO","verification_score":3.0,'
                                    '"confirms_buy":false,"strongest_bear_point":"Guidance cut 2 days ago",'
                                    '"falsification_findings":["Q3 guide cut 15% (PR, Jun 12)"]}')
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is False and v["verdict"] == "VETO"
    assert "Guidance cut" in v["strongest_bear_point"]
    assert v["falsification_findings"]


def test_stage2_downgrade_rejects(monkeypatch):
    _patch_llm(monkeypatch, returns='{"verdict":"DOWNGRADE","verification_score":5.5,'
                                    '"confirms_buy":false,"strongest_bear_point":"Margins peaking",'
                                    '"falsification_findings":[]}')
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is False and v["verdict"] == "DOWNGRADE"


def test_stage2_error_fail_open(monkeypatch):
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "1")
    _patch_llm(monkeypatch, raises=RuntimeError("504 deadline"))
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is True and v["verdict"] == "UNVERIFIED"
    assert "red-team unavailable" in v.get("note", "")


def test_stage2_error_fail_closed(monkeypatch):
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "0")
    _patch_llm(monkeypatch, raises=RuntimeError("504 deadline"))
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is False and v["verdict"] == "UNVERIFIED"


def test_garbage_verdict_treated_as_error(monkeypatch):
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "1")
    _patch_llm(monkeypatch, returns='{"verdict":"MAYBE","verification_score":5}')
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    # Unrecognized verdict -> _error -> fail-open path
    assert v["verdict"] == "UNVERIFIED"


def test_disabled_passthrough(monkeypatch):
    monkeypatch.setenv("VERIFY_BUYS", "0")
    calls = []
    _patch_llm(monkeypatch, raises=AssertionError("must not call when disabled"), track=calls)
    v = _run(verify_buy("AAA", _good_result(converged=False), _DOSSIER))
    assert v["confirmed"] is True and v["verdict"] == "DISABLED"
    assert calls == []


# ── Stage 2: red-team retries (transient flakiness must not fail-open a BUY) ──────

def test_red_team_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "3")
    state = {"n": 0}
    good = ('{"verdict":"CONFIRM","verification_score":8.0,"confirms_buy":true,'
            '"strongest_bear_point":"none material","falsification_findings":[]}')

    async def stub(system, user, **kwargs):
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("503 overloaded")
        return good
    monkeypatch.setattr(llm, "call_gemini_async", stub)

    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is True and v["verdict"] == "CONFIRM"
    assert state["n"] == 3  # retried through two transient failures, then succeeded


def test_red_team_exhausts_attempts(monkeypatch):
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "3")
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "1")
    calls = []
    _patch_llm(monkeypatch, raises=RuntimeError("503 overloaded"), track=calls)
    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))  # 7.6 < 8.0 -> fail-open
    assert v["verdict"] == "UNVERIFIED"
    assert len(calls) == 3  # tried exactly N times before giving up


def test_red_team_timeout_error_is_legible(monkeypatch):
    # asyncio.TimeoutError stringifies to "" — the _error must still name the cause.
    _patch_llm(monkeypatch, raises=asyncio.TimeoutError())
    rt = _run(verify.red_team("AAA", _good_result(), _DOSSIER, attempts=1))
    assert "_error" in rt and rt["_error"]  # non-empty (class name, not "")


def test_red_team_retries_unparseable_verdict(monkeypatch):
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "2")
    state = {"n": 0}
    good = ('{"verdict":"VETO","verification_score":2.0,"confirms_buy":false,'
            '"strongest_bear_point":"fraud","falsification_findings":[]}')

    async def stub(system, user, **kwargs):
        state["n"] += 1
        return '{"verdict":"MAYBE"}' if state["n"] == 1 else good
    monkeypatch.setattr(llm, "call_gemini_async", stub)

    rt = _run(verify.red_team("AAA", _good_result(), _DOSSIER))
    assert rt.get("verdict") == "VETO"  # bad verdict on attempt 1, recovered on 2


# ── 2026-07-03: fail-CLOSED is the default; the call gets a real budget ─────────

def test_default_is_fail_closed_at_any_score(monkeypatch):
    """No VERIFY_FAIL_OPEN env set -> a BUY without a verdict is NOT confirmed,
    even below the 8.0 tier. (The 90s-starved red-team had been erroring on ~95%
    of BUYs and fail-open reduced the gate to a no-op.)"""
    monkeypatch.delenv("VERIFY_FAIL_OPEN", raising=False)
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "1")
    _patch_llm(monkeypatch, raises=RuntimeError("503 overloaded"))
    v = _run(verify_buy("AAA", _good_result(consensus_score=7.5), _DOSSIER))
    assert v["confirmed"] is False and v["verdict"] == "UNVERIFIED"
    assert any("held" in r.lower() for r in v["reasons"])
    assert "503" in (v["reasons"][0] + v.get("note", ""))  # underlying cause legible


def test_red_team_call_config_is_lean(monkeypatch):
    """The verdict call must run grounded but LEAN: no thinking budget, small
    output cap, inner retries — the original inherited defaults (thinking=high,
    32k output) were a big part of the 90s starvation."""
    captured = {}

    async def stub(system, user, **kwargs):
        captured.update(kwargs)
        return ('{"verdict":"CONFIRM","verification_score":8.0,"confirms_buy":true,'
                '"strongest_bear_point":"","falsification_findings":[]}')
    monkeypatch.setattr(llm, "call_gemini_async", stub)

    v = _run(verify_buy("AAA", _good_result(), _DOSSIER))
    assert v["confirmed"] is True
    assert captured["grounding"] is True
    assert captured["thinking_level"] is None
    assert captured["max_output_tokens"] == 8192
    assert captured["max_retries"] == 6


def test_red_team_timeout_env_knob(monkeypatch):
    monkeypatch.delenv("VERIFY_RED_TEAM_TIMEOUT_SECS", raising=False)
    assert verify._red_team_timeout() == 300.0     # default matches LLM_CALL_TIMEOUT_SECS
    monkeypatch.setenv("VERIFY_RED_TEAM_TIMEOUT_SECS", "120")
    assert verify._red_team_timeout() == 120.0
    monkeypatch.setenv("VERIFY_RED_TEAM_TIMEOUT_SECS", "garbage")
    assert verify._red_team_timeout() == 300.0     # unparsable -> safe default


# ── Tiered fail-closed: a verifier outage must not auto-confirm a top-tier BUY ───

def test_failclosed_top_tier_holds_despite_fail_open(monkeypatch):
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "1")
    monkeypatch.setenv("VERIFY_FAILCLOSED_SCORE", "8.0")
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "1")
    _patch_llm(monkeypatch, raises=RuntimeError("503 overloaded"))
    v = _run(verify_buy("AAA", _good_result(consensus_score=9.01,
                                            consensus_grade="CONVICTION BUY"), _DOSSIER))
    assert v["confirmed"] is False and v["verdict"] == "UNVERIFIED"
    assert any("held" in r.lower() for r in v["reasons"])
    assert "held" in v["strongest_bear_point"].lower()


def test_failclosed_at_threshold_holds(monkeypatch):
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "1")
    monkeypatch.setenv("VERIFY_FAILCLOSED_SCORE", "8.0")
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "1")
    _patch_llm(monkeypatch, raises=RuntimeError("err"))
    v = _run(verify_buy("AAA", _good_result(consensus_score=8.0), _DOSSIER))
    assert v["confirmed"] is False  # exactly at the tier -> fails closed


def test_failclosed_below_threshold_preserves_fail_open(monkeypatch):
    monkeypatch.setenv("VERIFY_FAIL_OPEN", "1")
    monkeypatch.setenv("VERIFY_FAILCLOSED_SCORE", "8.0")
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "1")
    _patch_llm(monkeypatch, raises=RuntimeError("err"))
    v = _run(verify_buy("AAA", _good_result(consensus_score=7.5), _DOSSIER))
    assert v["confirmed"] is True and v["verdict"] == "UNVERIFIED"  # below tier -> fail-open


# ── Card surfacing: confirmed BUYs carry an auditable slim verdict ───────────────

def test_scout_card_includes_slim_verification():
    import upload_kv
    result = _good_result(verification={
        "verdict": "CONFIRM", "verification_score": 8.4,
        "strongest_bear_point": "valuation rich", "falsification_findings": ["x", "y"],
        "stage": 2, "reasons": [],
    })
    card = upload_kv._scout_card("AAA", result, {})
    assert card["verification"]["verdict"] == "CONFIRM"
    assert card["verification"]["verification_score"] == 8.4
    assert "falsification_findings" not in card["verification"]  # slim, not the full blob


def test_slim_verification_empty_when_absent():
    import upload_kv
    assert upload_kv._slim_verification(None) == {}
    assert upload_kv._scout_card("AAA", _good_result(), {})["verification"] == {}


def test_gems_card_includes_slim_verification(tmp_path):
    """Gems board cards must carry the slim verdict too (separate builder from _scout_card)."""
    import json, upload_kv
    result = _good_result(verification={
        "verdict": "CONFIRM", "verification_score": 9.2,
        "strongest_bear_point": "GAAP NI down on R&D spend",
        "falsification_findings": ["x"], "stage": 2,
    })
    (tmp_path / "AAA_20260624_000000.json").write_text(
        json.dumps({"result": result, "dossier": {}, "meta": {}}, default=str),
        encoding="utf-8",
    )
    gems = upload_kv.collect_gems_results(tmp_path)
    assert len(gems) == 1
    assert gems[0]["verification"]["verdict"] == "CONFIRM"
    assert gems[0]["verification"]["verification_score"] == 9.2
    assert "falsification_findings" not in gems[0]["verification"]  # slim, not full blob


# ── reverify_held: calm-window re-verification sweep (2026-07-07) ──────────────

def _write_output(tmp_path, ticker, result, dossier=None):
    path = tmp_path / f"{ticker}_20260707_000000.json"
    path.write_text(json.dumps({"result": result, "dossier": dossier or _DOSSIER}, default=str),
                    encoding="utf-8")
    return path


def test_reverify_held_noop_when_nothing_held():
    d = {"ticker": "AAA", "confirmed": True, "verification": {"verdict": "CONFIRM"}}
    stats = _run(verify.reverify_held([d]))
    assert stats == {"held": 0, "recovered": 0}


def test_reverify_held_skips_non_unverified(monkeypatch, tmp_path):
    calls = []
    _patch_llm(monkeypatch, raises=AssertionError("must not call"), track=calls)
    path = _write_output(tmp_path, "AAA", _good_result())
    d = {"ticker": "AAA", "verification": {"verdict": "CONFIRM"}, "confirmed": True,
         "output_file": str(path)}
    stats = _run(verify.reverify_held([d]))
    assert stats == {"held": 0, "recovered": 0}
    assert calls == []


def test_reverify_held_recovers_unverified(monkeypatch, tmp_path):
    _patch_llm(monkeypatch, returns='{"verdict":"CONFIRM","verification_score":8.5,'
                                    '"confirms_buy":true,"strongest_bear_point":"none material",'
                                    '"falsification_findings":[]}')
    path = _write_output(tmp_path, "AAA", _good_result(
        verification={"verdict": "UNVERIFIED"}, confirmed=False))
    d = {"ticker": "AAA", "verification": {"verdict": "UNVERIFIED"}, "confirmed": False,
         "output_file": str(path)}

    stats = _run(verify.reverify_held([d]))

    assert stats == {"held": 1, "recovered": 1}
    # in-memory discovery updated (main.py alerts/routes from this same list)
    assert d["verification"]["verdict"] == "CONFIRM" and d["confirmed"] is True
    # output file on disk updated too (upload_kv reads files, not this list)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["result"]["verification"]["verdict"] == "CONFIRM"
    assert saved["result"]["confirmed"] is True


def test_reverify_held_repeat_failure_stays_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIFY_RED_TEAM_ATTEMPTS", "1")
    monkeypatch.delenv("VERIFY_FAIL_OPEN", raising=False)
    _patch_llm(monkeypatch, raises=RuntimeError("still down"))
    path = _write_output(tmp_path, "AAA", _good_result(
        verification={"verdict": "UNVERIFIED"}, confirmed=False))
    d = {"ticker": "AAA", "verification": {"verdict": "UNVERIFIED"}, "confirmed": False,
         "output_file": str(path)}

    stats = _run(verify.reverify_held([d]))

    assert stats == {"held": 1, "recovered": 0}
    assert d["verification"]["verdict"] == "UNVERIFIED" and d["confirmed"] is False
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["result"]["verification"]["verdict"] == "UNVERIFIED"


def test_reverify_held_skips_missing_output_file():
    d = {"ticker": "AAA", "verification": {"verdict": "UNVERIFIED"}, "confirmed": False}
    stats = _run(verify.reverify_held([d]))
    assert stats == {"held": 1, "recovered": 0}  # counted as held, but nothing to reload
    assert d["verification"]["verdict"] == "UNVERIFIED"  # untouched


def test_reverify_held_skips_unreadable_output_file(tmp_path):
    bad_path = tmp_path / "AAA_bad.json"
    bad_path.write_text("not valid json", encoding="utf-8")
    d = {"ticker": "AAA", "verification": {"verdict": "UNVERIFIED"}, "confirmed": False,
         "output_file": str(bad_path)}
    stats = _run(verify.reverify_held([d]))
    assert stats == {"held": 1, "recovered": 0}


def test_reverify_held_mixed_batch_only_recovers_relevant(monkeypatch, tmp_path):
    """A confirmed discovery in the same batch must not spend an LLM call —
    only UNVERIFIED holds are re-verified."""
    calls = []

    async def stub(system, user, **kwargs):
        calls.append(1)
        return ('{"verdict":"DOWNGRADE","verification_score":5.0,"confirms_buy":false,'
                '"strongest_bear_point":"margins peaking","falsification_findings":[]}')
    monkeypatch.setattr(llm, "call_gemini_async", stub)

    confirmed_path = _write_output(tmp_path, "BBB", _good_result(
        verification={"verdict": "CONFIRM"}, confirmed=True))
    held_path = _write_output(tmp_path, "AAA", _good_result(
        verification={"verdict": "UNVERIFIED"}, confirmed=False))
    discoveries = [
        {"ticker": "BBB", "verification": {"verdict": "CONFIRM"}, "confirmed": True,
         "output_file": str(confirmed_path)},
        {"ticker": "AAA", "verification": {"verdict": "UNVERIFIED"}, "confirmed": False,
         "output_file": str(held_path)},
    ]

    stats = _run(verify.reverify_held(discoveries))

    assert stats == {"held": 1, "recovered": 1}
    assert len(calls) == 1  # only the held ticker spent a call
    assert discoveries[0]["verification"]["verdict"] == "CONFIRM"  # untouched
    assert discoveries[1]["verification"]["verdict"] == "DOWNGRADE"  # recovered (as a grade)


# ── _parse_red_team: source text is never truncated (2026-07-07 audit) ─────────

def test_parse_red_team_preserves_full_bear_point_and_findings():
    """The verdict parser is the SOURCE of bear-point/findings text — slicing here
    corrupted every downstream word-boundary clip() (an already-cut 400-char string
    made upload_kv's clip(bear, 400) a no-op). Truncation is display-layer-only."""
    bear = "word " * 200                       # 1000 chars, way past the old [:400]
    findings = ["finding " + "y" * 400, "z"]   # past the old [:300]
    parsed = verify._parse_red_team({
        "verdict": "DOWNGRADE", "verification_score": 5.5,
        "strongest_bear_point": bear, "falsification_findings": findings,
    })
    assert parsed["strongest_bear_point"] == bear
    assert parsed["falsification_findings"][0] == findings[0]


def test_parse_red_team_still_caps_findings_count():
    parsed = verify._parse_red_team({
        "verdict": "CONFIRM", "verification_score": 8.0,
        "strongest_bear_point": "", "falsification_findings": [str(i) for i in range(10)],
    })
    assert len(parsed["falsification_findings"]) == 6  # list cap is not text truncation
