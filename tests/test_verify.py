"""Tests for the BUY confirmation gate (verify.py).

Covers the Stage-1 deterministic quality gate (truth table), the Stage-2
red-team normalization, and verify_buy orchestration/routing incl. the
cost-saving short-circuit (no LLM call on a Stage-1 reject) and fail-open.
"""

import asyncio

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


def test_quality_gate_divergent_rr_fails():
    rr = _good_result()["risk_reward"] | {"llm_cross_check": {"divergent": True}}
    ok, reasons = quality_gate(_good_result(risk_reward=rr))
    assert not ok and any("diverge" in r for r in reasons)


def test_quality_gate_low_data_confidence_fails():
    ok, reasons = quality_gate(_good_result(data_confidence="LOW"))
    assert not ok and any("data confidence LOW" in r for r in reasons)


def test_quality_gate_no_rr_still_passes():
    # Missing R:R is not a disqualifier on its own.
    ok, reasons = quality_gate(_good_result(risk_reward={"applied": False}))
    assert ok and reasons == []


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
