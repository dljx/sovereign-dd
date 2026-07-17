"""TPM-admission guards (2026-07-18).

Gemma's 16K tokens/min/project quota (2026-07-17 Google change) rejects any
single prompt above the cap on EVERY key, forever — an oversized prompt is not
throttled, it is unservable. Two call sites could build one: the moderator
(multi-loop transcripts: 5 agents' 6K-sanitized research + 20-30 debate
entries) and guidance's gemma fallback (60K-char exhibits ≈ 15K tok enter the
prompt with NO sanitize cap). R1 prompts were already safe — _sanitize_untrusted
caps research at 6K chars — but the raw research string rides result/transcript
artifacts, so its stored copy is bounded too. Contracts: prompts under the caps
are byte-identical to before; oversized ones shed weight in a fixed order.
"""

import asyncio
import json

import agents
import debate
import guidance
from tests.test_guidance import _route_requests, _fake_cached, _PRESS_HTML


def _run(coro):
    return asyncio.run(coro)


# ── moderator_prompt ────────────────────────────────────────────────────────

def _entries(n, agent_prefix, **extra):
    return [{"agent": f"{agent_prefix}-{i}", "score": 6.0, **extra} for i in range(n)]


def test_moderator_prompt_unchanged_under_cap():
    transcript = _entries(15, "agent", web_research="abc research", thesis="fine")
    _, user = agents.moderator_prompt("MU", transcript, 3, 3.0)
    assert "abc research" in user                    # research kept verbatim
    assert "[omitted for length]" not in user
    assert "_elided" not in user


def test_moderator_prompt_stage1_drops_web_research():
    blob = "R" * 6000
    transcript = _entries(15, "agent", web_research=blob, thesis="t")
    _, user = agents.moderator_prompt("MU", transcript, 3, 3.0)
    assert len(user) <= agents._MODERATOR_MAX_USER_CHARS
    assert blob not in user
    assert "[omitted for length]" in user
    assert "_elided" not in user                     # 15 entries — nothing elided
    for i in range(15):                              # every position survives
        assert f"agent-{i}" in user


def test_moderator_prompt_stage2_elides_middle_loops():
    pad = {"thesis": "T" * 2000}
    transcript = (_entries(5, "r1") + _entries(20, "mid", **pad)
                  + _entries(10, "final", **pad))
    for t in transcript[:5]:
        t.update(pad)
    _, user = agents.moderator_prompt("MU", transcript, 3, 3.0)
    assert len(user) <= agents._MODERATOR_MAX_USER_CHARS
    assert "_elided" in user and "20 middle-loop entries" in user
    assert "r1-0" in user and "r1-4" in user         # R1 positions kept
    assert "final-0" in user and "final-9" in user   # final loop kept
    assert "mid-0" not in user and "mid-19" not in user


# ── R1 web-research bounds (debate._r1_agent) ───────────────────────────────

def test_r1_web_research_bounded_in_storage_and_prompt(monkeypatch):
    """Two independent bounds: the STORED result["web_research"] (rides output
    files + the KV board) is capped at _WEB_RESEARCH_MAX_CHARS, and the PROMPT
    copy is capped harder by agents._sanitize_untrusted's 6K limit."""
    calls = []

    async def fake_llm(system, user, **kw):
        calls.append(user)
        if kw.get("grounding"):                       # the research call
            return "W" * 100_000
        return json.dumps({"score": 7.0, "grade": "BUY", "thesis": "t"})

    monkeypatch.setattr(debate, "call_gemini_async", fake_llm)
    agent, result = _run(debate._r1_agent("ValuationEngine", "MU", {}, "Micron"))
    assert not result.get("_failed")
    assert result["score"] == 7.0
    assert len(result["web_research"]) == debate._WEB_RESEARCH_MAX_CHARS
    analysis_user = calls[1]
    assert 5_900 <= analysis_user.count("W") <= 6_100  # sanitize cap holds


# ── guidance gemma-fallback truncation ──────────────────────────────────────

def test_gemma_fallback_gets_truncated_exhibit(monkeypatch):
    # Exhibit strips to >_GEMMA_MAX_TEXT_CHARS; the guided quote sits early so
    # it survives truncation and the containment gate alike.
    big_html = _PRESS_HTML + "<p>" + "pad " * 20000 + "</p>"
    _route_requests(monkeypatch, doc_html=big_html)
    monkeypatch.setattr(guidance, "cached", _fake_cached)

    seen = {}

    def fake_call(system, user, model=None, **kw):
        if model == "gemini-3.5-flash":
            raise RuntimeError(
                "All API keys have exhausted their daily quota for "
                "gemini-3.5-flash. Quota resets at midnight UTC.")
        seen["gemma_user"] = user
        return json.dumps({"guidance": [
            {"metric": "revenue", "period": "Q1 FY2027", "low": 12.2e9,
             "high": 12.8e9, "direction": "initiated",
             "verbatim_quote": "we expect revenue of $12.2 billion to $12.8 billion"},
        ]})

    import llm
    monkeypatch.setattr(llm, "call_gemini", fake_call)

    out = guidance._fetch_and_extract(123, "000126000002", "er8k.htm", "FAKE")
    assert out["extracted_by"] == "gemma-4-31b-it"
    assert len(out["guidance"]) == 1                  # quote gate still passes
    # wrapper overhead (ticker line + fence markers) is ~100 chars
    assert len(seen["gemma_user"]) <= guidance._GEMMA_MAX_TEXT_CHARS + 200
