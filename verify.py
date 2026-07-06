"""BUY-signal confirmation gate — a second round of scrutiny before a debate
result is surfaced as an actionable BUY.

Every scout/gems result that crosses BUY_THRESHOLD passes through verify_buy()
before it reaches Telegram or the dashboard:

  Stage 1 — quality_gate (pure Python, 0 LLM calls):
      Reject BUYs whose INTERNAL signals are shaky — agents didn't converge,
      risk index is high, confidence is LOW, an agent failed, or the dossier
      itself is low-confidence. These are all already computed by debate.run(),
      so this stage is free.

      A risk/reward cross-check divergence (agents' bull/bear targets disagree
      with the computed R:R) is NOT a stage-1 reject (changed 2026-07-07,
      v3) — an estimator disagreement isn't itself adverse evidence. It rides
      into Stage 2 as a flagged lead for the prosecutor to investigate instead
      of auto-killing ~half of all BUYs on disagreement alone.

  Stage 2 — red_team (one grounded LLM call):
      Survivors face an adversarial prosecutor whose only job is to KILL the
      bull thesis using fresh Google-grounded research (recent guidance cuts,
      downgrades, deteriorating fundamentals, litigation, accounting flags).
      Returns CONFIRM / DOWNGRADE / VETO.

A BUY is `confirmed` only if it passes Stage 1 AND the prosecutor returns CONFIRM.
Rejected BUYs are not dropped — the caller routes them to an "Under Review"
watchlist with the reason attached.

Fail-safe contract (mirrors cleaner.py): never raises. On a Stage-2 error the
BUY fails CLOSED by default (2026-07-03): no verdict -> not confirmed -> Under
Review. An unverified pick isn't a proven edge, and Under Review still surfaces
it (watchlist topic + dashboard tab). Set VERIFY_FAIL_OPEN=1 to restore
fail-open below VERIFY_FAILCLOSED_SCORE during a known verifier outage.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone


# ── Config (read at call time so env changes take effect without re-import) ──────
def _enabled() -> bool:
    return os.getenv("VERIFY_BUYS", "1").strip().lower() in ("1", "true", "yes", "on")


def _fail_open() -> bool:
    """Default OFF since 2026-07-03 — the gate fails closed. The 90s-starved
    red-team (see _red_team_timeout) had been erroring on ~95% of BUYs and
    fail-open silently reduced the gate to a no-op; a BUY without a verdict now
    routes to Under Review instead of the alerts feed."""
    return os.getenv("VERIFY_FAIL_OPEN", "0").strip().lower() in ("1", "true", "yes", "on")


def _max_spread() -> float:
    try:
        return float(os.getenv("VERIFY_MAX_SPREAD", "2.0"))
    except ValueError:
        return 2.0


def _max_risk_index() -> float:
    try:
        return float(os.getenv("VERIFY_MAX_RISK_INDEX", "6.0"))
    except ValueError:
        return 6.0


def _red_team_attempts() -> int:
    """How many times to (re)try the grounded red-team call before giving up.
    Grounded Gemma calls flake under load — a single transient 5xx/timeout must not
    fail the whole verification (and so silently fail-open a real BUY)."""
    try:
        return max(1, int(os.getenv("VERIFY_RED_TEAM_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _failclosed_score() -> float:
    """At/above this consensus score, a verifier that can't reach a verdict fails
    CLOSED (→ Under Review) instead of fail-open — the highest-stakes BUYs are never
    auto-confirmed by a transient outage. Below it, fail-open (VERIFY_FAIL_OPEN) is
    preserved so the feed keeps flowing."""
    try:
        return float(os.getenv("VERIFY_FAILCLOSED_SCORE", "8.0"))
    except ValueError:
        return 8.0


def _red_team_timeout() -> float:
    """Outer bound per red-team attempt. Default 300s, matching llm.py's
    _CALL_TIMEOUT_SECS — the grounded call must absorb end-of-debate key
    cooldowns (observed 50-80s) PLUS grounded generation. The original 90s
    starved it: ~95% of gated BUYs came back UNVERIFIED (fixed 2026-07-03)."""
    try:
        return float(os.getenv("VERIFY_RED_TEAM_TIMEOUT_SECS", "300"))
    except ValueError:
        return 300.0


_RED_TEAM_BACKOFF = 3.0   # seconds between retry attempts


# ── Stage 1: deterministic quality gate (0 LLM calls) ───────────────────────────

def quality_gate(result: dict) -> tuple[bool, list[str]]:
    """Return (passes, reasons). `passes` is True only when every internal-quality
    check holds; `reasons` lists each failure in human-readable form.

    Reads only fields debate.run() already computed — costs nothing.
    """
    reasons: list[str] = []

    # Agents must have actually agreed — a wide final spread means low conviction.
    spread = result.get("score_spread")
    if not result.get("converged", False):
        reasons.append(f"agents did not converge (spread {spread})")
    elif isinstance(spread, (int, float)) and spread > _max_spread():
        reasons.append(f"score spread {spread} > {_max_spread()}")

    # Confidence + full panel.
    if result.get("confidence") == "LOW":
        reasons.append("debate confidence LOW")
    failed = result.get("failed_agents") or []
    if failed:
        reasons.append(f"{len(failed)} agent(s) failed: {', '.join(failed)}")

    # Risk/reward layer. NB: an llm_cross_check divergence used to auto-reject
    # here too — demoted 2026-07-07 (v3) to a Stage-2 lead, see _rr_divergence.
    rr = result.get("risk_reward") or {}
    if rr.get("applied"):
        ri = rr.get("risk_index")
        if isinstance(ri, (int, float)) and ri > _max_risk_index():
            reasons.append(f"risk index {ri} > {_max_risk_index()}")
    # (no R:R = not a disqualifier on its own; many valid BUYs lack a clean FV)

    # Dossier data quality.
    if result.get("data_confidence") == "LOW":
        reasons.append("dossier data confidence LOW")

    return (len(reasons) == 0, reasons)


def surfaces_on_board(result: dict) -> bool:
    """Whether a BUY-threshold result belongs on the main Scout/Gems board (and
    gets a Trade Alert) rather than Under Review. Single source of truth for
    this routing decision — used by both upload_kv.py (KV/dashboard cards) and
    main.py (Telegram routing) so they can never drift apart.

    v3 (2026-07-07): the confirmation gate grades instead of gating — a
    red-team DOWNGRADE ("real concerns, not fatal") surfaces flagged rather
    than being suppressed identically to a VETO. Only a clean CONFIRM or a
    DOWNGRADE reach the board; VETO / REJECTED_STAGE1 / UNVERIFIED stay in
    Under Review."""
    verdict = (result.get("verification") or {}).get("verdict")
    return bool(result.get("confirmed", True)) or verdict == "DOWNGRADE"


def _rr_divergence(result: dict) -> dict | None:
    """Extract a flagged R:R cross-check divergence for stage-2 use, or None.

    An estimator disagreement (agents' bull/bear targets vs the computed R:R)
    isn't itself adverse evidence, so since v3 it no longer auto-rejects at
    Stage 1 — it rides into the red-team prompt as a lead to investigate."""
    rr = result.get("risk_reward") or {}
    xc = rr.get("llm_cross_check") or {}
    if xc.get("divergent") is not True:
        return None
    llm_rr = xc.get("llm_rr")
    computed_rr = rr.get("rr_ratio")
    if llm_rr is None or computed_rr is None:
        return None
    return {"llm_rr": llm_rr, "computed_rr": computed_rr}


# ── Stage 2: adversarial red-team prosecutor (1 grounded LLM call) ───────────────

RED_TEAM_SYSTEM = """\
You are a forensic short-seller and adversarial investment skeptic. A multi-agent
research panel has rated the stock below a BUY. Your ONLY job is to try to PROVE
THEM WRONG before real money is committed.

Use Google Search to hunt for DISCONFIRMING evidence the panel may have missed or
under-weighted — recent guidance cuts, analyst downgrades, earnings misses,
deteriorating margins or cash flow, rising debt, dilution, insider selling,
litigation, regulatory/accounting red flags, competitive threats, or a valuation
that already prices in perfection. Attack the single load-bearing assumption (the
"key swing factor") hardest.

Be intellectually honest: if after genuine effort you CANNOT find a thesis-breaking
flaw, you must CONFIRM. Do not invent weakness, and do not rubber-stamp.

If the panel notes below flag a risk/reward cross-check divergence (their price targets
disagree with the computed R:R), treat that as a LEAD to investigate, not proof of a
problem — CONFIRM if your research shows the panel's own numbers are the credible ones.

Verdicts:
  CONFIRM   — the bull thesis survives scrutiny; no material disconfirming evidence.
  DOWNGRADE — real concerns that materially weaken conviction, but not fatal.
  VETO      — concrete disconfirming evidence that breaks the thesis.

Output STRICT JSON only — no markdown, no prose outside the JSON:
{
  "verdict": "CONFIRM" | "DOWNGRADE" | "VETO",
  "verification_score": <0-10 number, your independent conviction in the BUY>,
  "confirms_buy": <true only if verdict is CONFIRM>,
  "strongest_bear_point": "<the single most damaging finding, one sentence>",
  "falsification_findings": ["<concrete disconfirming fact + source/date>", "..."]
}
"""

_RED_TEAM_USER = """\
TICKER: {ticker}{name}
Current price: {price}   Market cap: {mcap}

PANEL VERDICT TO STRESS-TEST:
  Consensus: {score}/10 [{grade}]  (confidence {confidence})
  Bull thesis: {thesis}
  Key swing factor (attack this hardest): {swing}
  Stated catalyst: {catalyst}
  Stated asymmetry: {asymmetry}
  Risk/reward: {rr}
{divergence_block}
KEY FUNDAMENTALS (from the panel's dossier):
{fundamentals}

AGENT FINAL SCORES: {agent_scores}

Do your own fresh research. Try to break this BUY. Return the JSON verdict.
"""


def _fmt(v, suffix: str = "") -> str:
    if v is None or v == "":
        return "n/a"
    if isinstance(v, float):
        return f"{v:.2f}{suffix}"
    return f"{v}{suffix}"


def _build_fundamentals(dossier: dict) -> str:
    """Compact, prosecutor-relevant slice of the dossier (kept small on purpose)."""
    fin = (dossier.get("financials") or {})
    r = (fin.get("ratios_ttm") or {})
    fv = (dossier.get("fair_values") or {})
    rows = [
        ("P/E (ttm)",        _fmt(r.get("pe"))),
        ("Fwd P/E",          _fmt(r.get("fwd_pe"))),
        ("P/S",              _fmt(r.get("ps"))),
        ("EV/EBITDA",        _fmt(r.get("ev_ebitda"))),
        ("ROIC",             _fmt(r.get("roic"))),
        ("Debt/Equity",      _fmt(r.get("debt_equity"))),
        ("Net margin",       _fmt(r.get("net_margin"))),
        ("Rev growth (fwd)", _fmt(r.get("fwd_revenue_growth"))),
        ("FCF/share",        _fmt(r.get("fcf_per_share"))),
        ("Composite FV",     _fmt(fv.get("composite_fair_value"))),
    ]
    return "\n".join(f"  {k}: {v}" for k, v in rows)


def _build_user_prompt(ticker: str, result: dict, dossier: dict) -> str:
    profile = (dossier.get("profile") or {})
    quote = (dossier.get("quote") or {})
    rr = (result.get("risk_reward") or {})
    rr_str = "n/a"
    if rr.get("applied"):
        rr_str = (f"{_fmt(rr.get('rr_ratio'))}:1 · {rr.get('risk_tier','?')} risk · "
                  f"upside {_fmt(rr.get('upside_pct'))}% / downside {_fmt(rr.get('downside_pct'))}%")
    name = profile.get("name")

    div = _rr_divergence(result)
    if div:
        divergence_block = (
            f"\n  ⚠ R:R CROSS-CHECK FLAG: the agents' bull/bear price targets imply "
            f"{div['llm_rr']:.1f}:1, but the deterministically computed R:R is "
            f"{div['computed_rr']:.1f}:1. This disagreement alone is not evidence of a "
            "problem — investigate which number is credible and weigh it in your verdict.\n"
        )
    else:
        divergence_block = ""

    return _RED_TEAM_USER.format(
        ticker=ticker,
        name=f" ({name})" if name else "",
        price=_fmt(quote.get("price")),
        mcap=_fmt(profile.get("market_cap_bn") or result.get("market_cap_bn"), "B"),
        score=_fmt(result.get("consensus_score")),
        grade=result.get("consensus_grade", "?"),
        confidence=result.get("confidence", "?"),
        thesis=(result.get("majority_thesis") or "")[:700],
        swing=(result.get("key_swing_factor") or "")[:300],
        catalyst=(result.get("catalyst") or "")[:300],
        asymmetry=result.get("asymmetry_ratio") or "n/a",
        rr=rr_str,
        divergence_block=divergence_block,
        fundamentals=_build_fundamentals(dossier),
        agent_scores=result.get("agent_final_scores", {}),
    )


def _parse_red_team(raw) -> dict:
    """Validate + normalize one red-team JSON response. Returns a verdict dict, or
    {"_error": ...} if the payload isn't a usable verdict (the caller may retry)."""
    if not isinstance(raw, dict):
        return {"_error": "red-team response was not a JSON object"}

    verdict = str(raw.get("verdict", "")).upper().strip()
    if verdict not in ("CONFIRM", "DOWNGRADE", "VETO"):
        return {"_error": f"unrecognized verdict {verdict!r}"}

    try:
        vscore = float(raw.get("verification_score")) if raw.get("verification_score") is not None else None
    except (TypeError, ValueError):
        vscore = None

    findings = raw.get("falsification_findings")
    if not isinstance(findings, list):
        findings = [str(findings)] if findings else []

    return {
        "verdict": verdict,
        "verification_score": vscore,
        "confirms_buy": verdict == "CONFIRM",
        "strongest_bear_point": str(raw.get("strongest_bear_point", ""))[:400],
        "falsification_findings": [str(f)[:300] for f in findings[:6]],
    }


async def red_team(ticker: str, result: dict, dossier: dict, attempts: int | None = None) -> dict:
    """Run the adversarial prosecutor. Returns a normalized verdict dict, or
    {"_error": "..."} on failure (verify_buy decides fail-open vs fail-closed).

    Retries up to `attempts` times (default VERIFY_RED_TEAM_ATTEMPTS) on a transient
    error, a timeout, OR an unparseable verdict — grounded Gemma calls flake under
    load, and one blip must not fail-open a real BUY. Each attempt rotates API keys
    via call_gemini_async."""
    try:
        from llm import call_gemini_async, extract_json
    except ImportError:
        return {"_error": "llm module unavailable"}

    if attempts is None:
        attempts = _red_team_attempts()
    prompt = _build_user_prompt(ticker, result, dossier)

    last_err = "no attempt made"
    for i in range(attempts):
        try:
            # thinking_level=None + small output cap: the verdict JSON is tiny and
            # the latency budget belongs to grounded search, not to thinking tokens
            # (high-thinking grounded calls were a big part of the 90s starvation).
            # max_retries=6: let the INNER key rotation absorb transient 429/5xx
            # within this attempt's window instead of multiplying outer retries.
            text = await asyncio.wait_for(
                call_gemini_async(RED_TEAM_SYSTEM, prompt, grounding=True,
                                  temperature=0.2, thinking_level=None,
                                  max_output_tokens=8192, max_retries=6),
                timeout=_red_team_timeout(),
            )
            parsed = _parse_red_team(extract_json(text))
        except Exception as exc:
            # asyncio.TimeoutError stringifies to "" — keep the class name so the
            # cause stays legible in the saved `note` (this produced the `_error: ""`).
            parsed = {"_error": str(exc) or type(exc).__name__}

        if "_error" not in parsed:
            return parsed
        last_err = parsed["_error"]
        if i < attempts - 1:
            await asyncio.sleep(_RED_TEAM_BACKOFF)

    return {"_error": last_err}


# ── Orchestration ───────────────────────────────────────────────────────────────

async def verify_buy(ticker: str, result: dict, dossier: dict) -> dict:
    """Two-stage confirmation gate. Returns a verification block (always includes
    a `confirmed` bool). Never raises.

    Stage 1 failure short-circuits — no LLM call is spent on an internally-shaky
    BUY; it goes straight to the watchlist with concrete reasons.
    """
    now = datetime.now(timezone.utc).isoformat()

    if not _enabled():
        return {"confirmed": True, "stage": 0, "verdict": "DISABLED",
                "reasons": [], "checked_at": now}

    stage1_pass, reasons = quality_gate(result)
    if not stage1_pass:
        return {
            "confirmed": False,
            "stage": 1,
            "stage1_pass": False,
            "verdict": "REJECTED_STAGE1",
            "reasons": reasons,
            "verification_score": None,
            "strongest_bear_point": reasons[0] if reasons else "",
            "falsification_findings": [],
            "checked_at": now,
        }

    rt = await red_team(ticker, result, dossier)

    if "_error" in rt:
        # No verdict -> fail CLOSED (default since 2026-07-03): an unverified BUY
        # is not a confirmed BUY; it goes to Under Review, not the alerts feed.
        # VERIFY_FAIL_OPEN=1 restores fail-open BELOW the fail-closed score tier
        # (emergency lever for a known verifier outage); at/above the tier a BUY
        # is never auto-confirmed regardless.
        score = result.get("consensus_score", 0) or 0
        top_tier = isinstance(score, (int, float)) and score >= _failclosed_score()
        fail_open = _fail_open() and not top_tier
        held_reason = (f"BUY held — red-team unavailable after retries "
                       f"({str(rt['_error'])[:120]}); not auto-confirmed")
        return {
            "confirmed": fail_open,
            "stage": 2,
            "stage1_pass": True,
            "verdict": "UNVERIFIED",
            "reasons": [] if fail_open else [held_reason],
            "verification_score": None,
            "strongest_bear_point": "" if fail_open else held_reason,
            "falsification_findings": [],
            "note": f"red-team unavailable ({rt['_error']})",
            "checked_at": now,
        }

    return {
        "confirmed": rt["confirms_buy"],
        "stage": 2,
        "stage1_pass": True,
        "verdict": rt["verdict"],
        "reasons": [] if rt["confirms_buy"] else [rt["strongest_bear_point"]],
        "verification_score": rt["verification_score"],
        "strongest_bear_point": rt["strongest_bear_point"],
        "falsification_findings": rt["falsification_findings"],
        "checked_at": now,
    }


# ── Calm-window re-verification (reliability fix, 2026-07-07) ──────────────────

async def reverify_held(discoveries: list[dict], label: str = "scout", verbose: bool = True) -> dict:
    """Give UNVERIFIED holds one more shot once the whole run's debates are done.

    The in-debate red-team call fires at end-of-debate, exactly when API keys
    are deepest in their RPM cooldown (the condition that starved ~95% of BUYs
    into UNVERIFIED before the 2026-07-03 timeout fix — see _red_team_timeout).
    By the time every ticker in this run has finished debating, keys are idle
    again, so one more attempt per held discovery often succeeds where the
    in-debate attempt didn't. This is the fallback lever documented alongside
    that fix, now wired in rather than left as a manual option.

    Mutates each recovered discovery's `verification`/`confirmed` in place AND
    rewrites its output JSON file on disk — upload_kv.py builds Supabase/KV
    rows from the FILES, not this in-memory list (see _scout_card's docstring),
    so the file must carry the recovered verdict too. A discovery that fails
    again stays fail-closed (Under Review) — this sweep only gives verification
    a second chance, it never loosens the gate itself.

    Returns {"held": int, "recovered": int} for the caller's run summary.
    """
    held = [d for d in discoveries
            if (d.get("verification") or {}).get("verdict") == "UNVERIFIED"]
    if not held:
        return {"held": 0, "recovered": 0}

    if verbose:
        print(f"\n  [{label}] Calm-window re-verification: {len(held)} UNVERIFIED hold(s)...")

    recovered = 0
    for d in held:
        ticker = d.get("ticker", "?")
        out_path = d.get("output_file")
        if not out_path:
            continue
        try:
            with open(out_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            if verbose:
                print(f"  [{label}]   {ticker}: could not reload output file: {e}")
            continue

        result  = data.get("result") or {}
        dossier = data.get("dossier") or {}
        v = await verify_buy(ticker, result, dossier)
        result["verification"] = v
        result["confirmed"] = v.get("confirmed", False)
        data["result"] = result

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            if verbose:
                print(f"  [{label}]   {ticker}: could not rewrite output file: {e}")
            continue

        # Mutate the in-memory discovery too — main.py routes/alerts from this
        # list within the same run, so a recovered verdict must be reflected here.
        d["verification"] = v
        d["confirmed"] = v.get("confirmed", False)

        if v.get("verdict") != "UNVERIFIED":
            recovered += 1
            if verbose:
                print(f"  [{label}]   {ticker}: recovered → {v.get('verdict')}")
        elif verbose:
            print(f"  [{label}]   {ticker}: still UNVERIFIED")

    if verbose:
        print(f"  [{label}] Re-verified {len(held)} held BUY(s) · {recovered} recovered")

    return {"held": len(held), "recovered": recovered}
