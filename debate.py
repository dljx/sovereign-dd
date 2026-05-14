"""Debate orchestrator — async parallel rounds, grounded R1, dynamic convergence, synthesis or moderator."""

import asyncio
from statistics import mean

from agents import (
    AGENTS, moderator_prompt, research_prompt, round1_prompt,
    round2_prompt, round3_prompt, synthesis_prompt,
)
from llm import call_gemini_async, extract_json

CONVERGENCE_THRESHOLD = 1.5
MAX_LOOPS = 3


def _grade(score: float) -> str:
    if score >= 8.0: return "STRONG BUY"
    if score >= 6.5: return "BUY"
    if score >= 5.0: return "HOLD"
    if score >= 3.5: return "SELL"
    return "STRONG SELL"


# ── Per-agent async helpers ────────────────────────────────────────────────────

async def _r1_agent(agent: str, ticker: str, dossier: dict, company_name: str) -> tuple[str, dict]:
    """Run grounded research then scored analysis for one agent. Returns (agent, result)."""
    try:
        # Phase A — grounded web research
        sys_r, usr_r = research_prompt(agent, ticker, company_name)
        print(f"  [debate] R1-research / {agent}...")
        web_summary = await call_gemini_async(sys_r, usr_r, grounding=True)

        # Phase B — structured analysis with dossier + research
        sys_p, usr_p = round1_prompt(agent, ticker, dossier, web_summary)
        print(f"  [debate] R1-analysis / {agent}...")
        text = await call_gemini_async(sys_p, usr_p)
        result = extract_json(text)
        result["agent"] = agent
        result["web_research"] = web_summary
        return agent, result
    except Exception as e:
        print(f"  [debate] {agent} failed in R1: {e}")
        return agent, {"agent": agent, "score": 5.0, "grade": "HOLD", "thesis": "", "web_research": "", "_failed": True}


async def _r2_agent(agent: str, ticker: str, scores: dict, all_r1: list, loop: int, target: str = "") -> tuple[str, dict]:
    """Cross-examination for one agent. Returns (agent, result)."""
    try:
        sys_p, usr_p = round2_prompt(agent, ticker, scores[agent], all_r1, loop, target)
        print(f"  [debate] R2-{loop} / {agent} → challenges {target}...")
        text = await call_gemini_async(sys_p, usr_p)
        result = extract_json(text)
        result["agent"] = agent
        return agent, result
    except Exception as e:
        print(f"  [debate] {agent} failed in R2-{loop}: {e}")
        return agent, {"agent": agent, "target_agent": target, "challenge": "", "_failed": True}


async def _r3_agent(
    agent: str, ticker: str, scores: dict,
    r2_results: dict, all_r2: list, loop: int,
) -> tuple[str, dict]:
    """Rebuttal & revised score for one agent. Returns (agent, result)."""
    try:
        challenges = [r for r in r2_results.values() if r.get("target_agent") == agent]
        sys_p, usr_p = round3_prompt(agent, ticker, scores[agent], challenges, all_r2, loop)
        print(f"  [debate] R3-{loop} / {agent}...")
        text = await call_gemini_async(sys_p, usr_p)
        result = extract_json(text)
        result["agent"] = agent
        return agent, result
    except Exception as e:
        print(f"  [debate] {agent} failed in R3-{loop}: {e}")
        return agent, {"agent": agent, "revised_score": scores.get(agent, 5.0), "final_thesis": "", "_failed": True}


# ── Main async orchestrator ────────────────────────────────────────────────────

async def run(ticker: str, dossier: dict, verbose: bool = True) -> dict:
    """Run the full debate asynchronously. Returns the final consensus dict."""
    transcript: list[dict] = []
    company_name = dossier.get("profile", {}).get("name", ticker)

    # ── ROUND 1 — All 5 agents in parallel (each: research → analysis) ─────────
    if verbose:
        print("\n+----------------------------------------------+")
        print("|  ROUND 1 -- Grounded Research & Assessment   |")
        print("|  (5 agents running in parallel)              |")
        print("+----------------------------------------------+")

    r1_pairs = await asyncio.gather(
        *[_r1_agent(a, ticker, dossier, company_name) for a in AGENTS]
    )

    r1_results: dict[str, dict] = {}
    for agent, result in r1_pairs:
        r1_results[agent] = result
        transcript.append(result)

    if verbose:
        for agent in AGENTS:
            r = r1_results[agent]
            score  = r.get("score", "?")
            grade  = r.get("grade", "")
            thesis = r.get("thesis", "")[:80]
            finding = r.get("web_finding", "")[:60]
            print(f"    {agent:<14} → {score:>4}  [{grade}]  {thesis}...")
            print(f"    {'':14}   web: {finding}...")

    scores    = {a: float(r1_results[a].get("score", 5.0)) for a in AGENTS}
    scores_r1 = dict(scores)
    all_r1    = list(r1_results.values())

    # ── DEBATE LOOPS — R2 + R3 in parallel per round ──────────────────────────
    loops_run = 0
    r3_results: dict[str, dict] = {}

    for loop in range(1, MAX_LOOPS + 1):
        loops_run = loop

        # Round 2 — all 5 agents challenge simultaneously
        if verbose:
            print(f"\n+----------------------------------------------+")
            print(f"|  LOOP {loop} / ROUND 2 -- Cross-Examination         |")
            print(f"|  (5 agents running in parallel)              |")
            print(f"+----------------------------------------------+")

        # Assign challenge targets by pairing opposites (sorted by score).
        # This prevents pile-on: max 2 challenges per agent, bulls challenge bears.
        _sorted = sorted(AGENTS, key=lambda a: scores[a])
        r2_targets = {
            _sorted[0]: _sorted[4],   # lowest score → challenges highest
            _sorted[4]: _sorted[0],   # highest score → challenges lowest
            _sorted[1]: _sorted[3],   # 2nd lowest → 2nd highest
            _sorted[3]: _sorted[1],   # 2nd highest → 2nd lowest
            _sorted[2]: _sorted[4],   # middle → highest (biggest disagreement)
        }

        r2_pairs = await asyncio.gather(
            *[_r2_agent(a, ticker, scores, all_r1, loop, r2_targets[a]) for a in AGENTS]
        )
        r2_results: dict[str, dict] = {a: r for a, r in r2_pairs}
        for _, r in r2_pairs:
            transcript.append(r)

        if verbose:
            for agent in AGENTS:
                r = r2_results[agent]
                target    = r.get("target_agent", "?")
                challenge = r.get("challenge", "")[:70]
                print(f"    {agent:<14} → challenges {target}: {challenge}...")

        all_r2 = list(r2_results.values())

        # Round 3 — all 5 agents rebut simultaneously
        if verbose:
            print(f"\n+----------------------------------------------+")
            print(f"|  LOOP {loop} / ROUND 3 -- Rebuttal & Revision       |")
            print(f"|  (5 agents running in parallel)              |")
            print(f"+----------------------------------------------+")

        r3_pairs = await asyncio.gather(
            *[_r3_agent(a, ticker, scores, r2_results, all_r2, loop) for a in AGENTS]
        )
        r3_results = {a: r for a, r in r3_pairs}
        for _, r in r3_pairs:
            transcript.append(r)

        if verbose:
            for agent in AGENTS:
                r        = r3_results[agent]
                prev     = scores[agent]
                revised  = float(r.get("revised_score", prev))
                delta    = revised - prev
                arrow    = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
                print(f"    {agent:<14} → {prev} {arrow} {revised}  (Δ {delta:+.1f})")

        # Update scores and check convergence
        scores    = {a: float(r3_results[a].get("revised_score", scores[a])) for a in AGENTS}
        score_vals = list(scores.values())
        spread    = max(score_vals) - min(score_vals)

        if verbose:
            avg = mean(score_vals)
            print(f"\n  Scores after loop {loop}: {scores}")
            print(f"  Spread: {spread:.2f}  Mean: {avg:.2f}  Threshold: {CONVERGENCE_THRESHOLD}")

        if spread <= CONVERGENCE_THRESHOLD:
            if verbose:
                print(f"  ✓ Converged in {loop} loop(s).")
            break
        elif loop < MAX_LOOPS:
            if verbose:
                print(f"  ✗ Not converged — running loop {loop + 1}...")
            # Normalize R3 output to R1 schema for next loop's cross-examination
            all_r1 = [
                {
                    "agent": a,
                    "score": r3_results[a].get("revised_score", scores[a]),
                    "thesis": r3_results[a].get("final_thesis", ""),
                    "rebuttal": r3_results[a].get("rebuttal", ""),
                    "concessions": r3_results[a].get("concessions", ""),
                }
                for a in AGENTS
            ]

    # ── CONSENSUS ──────────────────────────────────────────────────────────────
    if verbose:
        print(f"\n+----------------------------------------------+")
        print(f"|  CONSENSUS                                    |")
        print(f"+----------------------------------------------+")

    score_vals = list(scores.values())
    spread     = max(score_vals) - min(score_vals)
    avg        = mean(score_vals)

    final_positions = [r3_results[a] for a in AGENTS if a in r3_results] or all_r1

    if spread <= CONVERGENCE_THRESHOLD:
        if verbose:
            print(f"  [OK] Scores converged (spread={spread:.2f}) — synthesizing...")
        sys_p, usr_p = synthesis_prompt(ticker, final_positions)
        print(f"  [debate] Synthesis...")
        text = await call_gemini_async(sys_p, usr_p)
        moderator_result = extract_json(text)
        moderator_result.setdefault("consensus_score", round(avg, 2))
        moderator_result.setdefault("consensus_grade", _grade(avg))
        moderator_result.setdefault("confidence", "HIGH" if spread <= 1.0 else "MEDIUM")
    else:
        if verbose:
            print(f"  [!] Did not converge after {MAX_LOOPS} loops (spread={spread:.2f}) — calling moderator...")
        sys_p, usr_p = moderator_prompt(ticker, transcript, loops_run, spread)
        print(f"  [debate] Moderator...")
        text = await call_gemini_async(sys_p, usr_p)
        moderator_result = extract_json(text)

    transcript.append(moderator_result)

    if verbose:
        cs   = moderator_result.get("consensus_score", avg)
        cg   = moderator_result.get("consensus_grade", _grade(avg))
        conf = moderator_result.get("confidence", "?")
        print(f"\n  CONSENSUS: {cs:.2f} / 10  [{cg}]  confidence={conf}")
        print(f"  {moderator_result.get('majority_thesis', '')[:120]}...")

    return {
        "ticker":           ticker,
        "consensus_score":  moderator_result.get("consensus_score", round(avg, 2)),
        "consensus_grade":  moderator_result.get("consensus_grade", _grade(avg)),
        "confidence":       moderator_result.get("confidence", "MEDIUM"),
        "majority_thesis":  moderator_result.get("majority_thesis", ""),
        "dissent":          moderator_result.get("dissent", ""),
        "key_swing_factor": moderator_result.get("key_swing_factor", ""),
        "score_rationale":  moderator_result.get("score_rationale", ""),
        "agent_r1_scores":  scores_r1,
        "agent_final_scores": scores,
        "score_spread":     round(spread, 2),
        "loops_run":        loops_run,
        "converged":        spread <= CONVERGENCE_THRESHOLD,
        "transcript":       transcript,
    }
